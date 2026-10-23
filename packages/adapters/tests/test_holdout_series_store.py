"""0036 and qw_adapters.holdout_series_store (owner decisions 41, 51 to 53): the
protected holdout and baseline series, preregistered by the assessor login before
any protocol names the plan, one per plan, instrument and role, readable by
qw_assessor alone, append-only and re-hashed on read. Refusals come from the
database through real logins; the superuser `conn` observes or tampers.
SYNTHETIC series only."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from qw_adapters import holdout_series_store as hs
from qw_adapters import tenancy as tn
from qw_domain.backtest import PriceBar, PriceSeries
from qw_domain.research_data import EvidenceClass
from qw_domain.strategy_improvement import HoldoutData
from test_research_data import iid

_spec = importlib.util.spec_from_file_location(
    "qw_estore_tests", Path(__file__).with_name("test_experiment_store.py")
)
assert _spec is not None and _spec.loader is not None
st = sys.modules.setdefault("qw_estore_tests", importlib.util.module_from_spec(_spec))
if not hasattr(st, "put"):
    _spec.loader.exec_module(st)
wa = st.wa
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
PLAN = "plan-synth-1"  # the plan the test_experiment_store protocol names
OTHER = "plan-synth-2"
DAY0 = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
COLS = (
    "tenant_id, plan_id, instrument_id, series_role, series_id, content_hash, "
    "body, recorded_at, event_hash"
)


def px(
    t: uuid.UUID,
    sid: str = "px-synth-h",
    n: int = 3,
    opens: tuple[str, ...] = ("100.25", "101", "99.875"),
) -> PriceSeries:
    bars = tuple(
        PriceBar(
            DAY0 + timedelta(days=k),
            DAY0 + timedelta(days=k, hours=6, minutes=30),
            DAY0 + timedelta(days=k, hours=6, minutes=30),
            Decimal(o),
            Decimal(o),
        )
        for k, o in enumerate(opens)
    )
    return PriceSeries(sid, str(t), iid(n), EvidenceClass.SYNTHETIC, bars)


def prereg(conn: Conn, t: uuid.UUID, plan: str, role: str, s: PriceSeries) -> None:
    with tn.tenant_transaction(conn, t) as tx:
        hs.preregister(tx, plan, role, s)


def data(conn: Conn, t: uuid.UUID, plan: str, n: int = 3) -> HoldoutData:
    with tn.tenant_transaction(conn, t) as tx:
        return hs.holdout_data(tx, plan, iid(n))


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(hs.HoldoutSeriesError) as e:
        fn()
    return e.value.code


@pytest.fixture
def t(app_conn: Conn) -> uuid.UUID:
    with tn.provision_tenant(app_conn) as tx:
        return tx.tenant_id


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The assessor login, checked as exactly qw_assessor."""
    with tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as c:
        yield c


@pytest.fixture
def login_in(conn: Conn, database_url: str) -> Iterator[Callable[..., str]]:
    """Throwaway passwordless LOGIN roles in one group (trust auth, as in conftest)."""
    names: list[str] = []

    def make(group: str) -> str:
        names.append(name := f"qwtest_hs_{uuid.uuid4().hex[:12]}")
        conn.execute(
            pgsql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE {}").format(
                pgsql.Identifier(name), pgsql.Identifier(group)
            )
        )
        return make_conninfo(database_url, user=name)

    yield make
    for name in names:
        conn.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(name)))


def raw_row(t: uuid.UUID, plan: str, role: str, s: PriceSeries) -> dict[str, Any]:
    body = s.to_wire()
    return {
        "tenant_id": t,
        "plan_id": plan,
        "instrument_id": s.instrument.uuid,
        "series_role": role,
        "series_id": s.series_id,
        "content_hash": s.content_hash,
        "body": Jsonb(body),
        "event_hash": "0" * 64,
    }


def raw_insert(c: Conn, row: dict[str, Any], at: str = "clock_timestamp()") -> None:
    c.execute(
        f"INSERT INTO app.holdout_series ({COLS}) VALUES (%(tenant_id)s, "
        "%(plan_id)s, %(instrument_id)s, %(series_role)s, %(series_id)s, "
        f"%(content_hash)s, %(body)s, {at}, %(event_hash)s)",
        row,
    )


# --- round trips ---


def test_preregistered_series_read_back_as_holdout_data(
    asr: Conn, t: uuid.UUID
) -> None:
    main, base = px(t), px(t, "px-bench", 9, ("50", "50.5", "49.75"))
    prereg(asr, t, PLAN, "holdout", main)
    prereg(asr, t, PLAN, "px-bench", base)  # decision 51: baselines too
    got = data(asr, t, PLAN)
    assert got == HoldoutData(main, {"px-bench": base})
    assert got.baseline_series["px-bench"].bars[2].open == Decimal("49.75")
    with tn.tenant_transaction(asr, t) as tx:
        rows = hs.series(tx, PLAN)
    assert [(r.series_role, r.series.content_hash) for r in rows] == sorted(
        [("holdout", main.content_hash), ("px-bench", base.content_hash)]
    )
    assert data(asr, t, PLAN, 3).series == main


def test_missing_or_ambiguous_series_are_refused(asr: Conn, t: uuid.UUID) -> None:
    assert code(lambda: data(asr, t, PLAN)) == "series_missing"
    prereg(asr, t, PLAN, "holdout", px(t))
    prereg(asr, t, PLAN, "px-bench", px(t, "px-bench", 8))
    assert data(asr, t, PLAN).baseline_series["px-bench"].instrument == iid(8)
    # decision 54: a second load of a baseline, under another instrument, is
    # refused at load; the first stays the plan's only baseline
    assert (
        code(lambda: prereg(asr, t, PLAN, "px-bench", px(t, "px-bench", 9)))
        == "series_exists"
    )
    assert data(asr, t, PLAN).baseline_series["px-bench"].instrument == iid(8)
    assert code(lambda: data(asr, t, PLAN, 4)) == "series_missing"


# --- decision 52: one row per plan, instrument and role ---


def test_one_series_per_plan_instrument_and_role(asr: Conn, t: uuid.UUID) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))
    again = px(t, opens=("100", "101", "102"))  # friendlier data, same key
    assert code(lambda: prereg(asr, t, PLAN, "holdout", again)) == "series_exists"
    with tn.tenant_transaction(asr, t), pytest.raises(errors.UniqueViolation):
        raw_insert(asr, raw_row(t, PLAN, "holdout", again))
    prereg(asr, t, OTHER, "holdout", again)  # the fix is a new plan
    assert data(asr, t, PLAN).series == px(t)


def test_role_and_tenant_must_match_the_series(asr: Conn, t: uuid.UUID) -> None:
    s = px(t, "px-bench")
    assert code(lambda: prereg(asr, t, PLAN, "px-other", s)) == "role_mismatch"
    with tn.tenant_transaction(asr, t), pytest.raises(errors.CheckViolation):
        raw_insert(asr, raw_row(t, PLAN, "px-other", s))  # the database's check
    foreign = px(uuid.uuid4())
    assert code(lambda: prereg(asr, t, PLAN, "holdout", foreign)) == "tenant_mismatch"
    with tn.tenant_transaction(asr, t), pytest.raises(errors.CheckViolation):
        raw_insert(asr, raw_row(t, PLAN, "holdout", foreign) | {"tenant_id": t})


# --- decision 41: preregistered, refused once a protocol names the plan ---


def test_loading_is_refused_after_a_protocol_names_the_plan(
    asr: Conn, t: uuid.UUID, wa: Any, conn: Conn
) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))  # before the freeze
    reg = st.reg_for(t)
    assert reg.protocol(st.EXP).plan_id == PLAN
    st.put(wa, t, reg, st.booked(reg), st.im.Scheduled(st.EXP, "bud-exp-1"))
    late = px(t, "px-bench", 9)
    assert code(lambda: prereg(asr, t, PLAN, "px-bench", late)) == "plan_frozen"
    with (
        tn.tenant_transaction(asr, t),
        pytest.raises(errors.CheckViolation, match="plan_frozen"),
    ):
        raw_insert(asr, raw_row(t, PLAN, "px-bench", late))
    prereg(asr, t, OTHER, "px-bench", late)  # control: an unnamed plan still loads
    assert data(asr, t, PLAN).series == px(t)  # the preregistered row still reads
    row = conn.execute(
        "SELECT count(*) FROM app.holdout_series WHERE tenant_id = %s AND plan_id = %s",
        (t, PLAN),
    ).fetchone()
    assert row == (1,)


def test_a_protocol_of_another_tenant_does_not_freeze(
    asr: Conn, t: uuid.UUID, app_conn: Conn, wa: Any
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    reg = st.reg_for(b)
    st.put(wa, b, reg, st.booked(reg), st.im.Scheduled(st.EXP, "bud-exp-1"))
    prereg(asr, t, PLAN, "holdout", px(t))
    assert code(lambda: prereg(asr, b, PLAN, "holdout", px(b))) == "plan_frozen"


def test_a_load_waits_for_the_experiment_lock(
    asr: Conn, t: uuid.UUID, conn: Conn
) -> None:
    """The freeze check runs under the lock a protocol insert takes, so a load
    cannot pass the check while a protocol naming the plan is being stored."""
    key = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"
    with conn.transaction():
        conn.execute(key, (f"experiment:{t}",))
        asr.execute("SET lock_timeout = '200ms'")
        try:
            got = code(lambda: prereg(asr, t, PLAN, "holdout", px(t)))
        finally:
            asr.execute("RESET lock_timeout")
    assert got == "database"  # LockNotAvailable: it waited, then timed out
    prereg(asr, t, PLAN, "holdout", px(t))  # the lock released: it loads


def test_recorded_at_must_be_the_database_clock(asr: Conn, t: uuid.UUID) -> None:
    for at in (
        "clock_timestamp() - interval '2 minutes'",
        "clock_timestamp() + interval '1 minute'",
    ):
        with (
            tn.tenant_transaction(asr, t),
            pytest.raises(errors.CheckViolation, match="database clock"),
        ):
            raw_insert(asr, raw_row(t, PLAN, "holdout", px(t)), at)


# --- roles and RLS (decision 41: the assessor login alone) ---


def test_no_other_role_holds_any_privilege(conn: Conn, asr: Conn) -> None:
    privs = (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    )
    for role in ("qw_app", "qw_worker", "qw_ingest", "qw_release", "public"):
        for p in privs:
            row = conn.execute(
                "SELECT has_table_privilege(%s, 'app.holdout_series', %s)", (role, p)
            ).fetchone()
            assert row == (False,), (role, p)
        row = conn.execute(
            "SELECT has_any_column_privilege(%s, "
            "'app.holdout_series', 'SELECT, INSERT, UPDATE, "
            "REFERENCES')",
            (role,),
        ).fetchone()
        assert row == (False,), role
    got = {
        p
        for p in privs
        if conn.execute(
            "SELECT has_table_privilege('qw_assessor', 'app.holdout_series', %s)", (p,)
        ).fetchone()
        == (True,)
    }
    assert got == {"SELECT"}  # INSERT is by column (below), nothing else
    row = conn.execute(
        "SELECT has_any_column_privilege('qw_assessor', 'app.holdout_series', 'UPDATE')"
    ).fetchone()
    assert row == (False,)


def test_app_worker_and_release_logins_are_refused(
    asr: Conn, t: uuid.UUID, runtime_urls: dict[str, str]
) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_worker", "qw_release")
    for role in roles:
        with tn.runtime_connection(runtime_urls[role], role=role) as c:
            assert code(lambda: data(c, t, PLAN)) == "forbidden"
            assert code(lambda: prereg(c, t, OTHER, "holdout", px(t))) == "forbidden"
            for stmt in (
                "SELECT plan_id FROM app.holdout_series",
                "SELECT count(*) FROM app.holdout_series",
                "DELETE FROM app.holdout_series",
                "UPDATE app.holdout_series SET series_id = 'x'",
                "TRUNCATE app.holdout_series",
            ):
                with (
                    tn.tenant_transaction(c, t),
                    pytest.raises(errors.InsufficientPrivilege),
                ):
                    c.execute(stmt)
            with (
                tn.tenant_transaction(c, t),
                pytest.raises(errors.InsufficientPrivilege),
            ):
                raw_insert(c, raw_row(t, OTHER, "holdout", px(t)))


def test_an_ingest_login_is_refused(
    asr: Conn, t: uuid.UUID, login_in: Callable[..., str]
) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))
    url = login_in("qw_ingest")
    with pytest.raises(tn.TenancyError), tn.runtime_connection(url):
        pass  # not a runtime login at all
    with psycopg.connect(url, autocommit=True) as c:  # and the table refuses it
        for stmt in (
            "SELECT plan_id FROM app.holdout_series",
            "DELETE FROM app.holdout_series",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                c.execute(stmt)


def test_rls_hides_and_refuses_other_tenants(
    asr: Conn, t: uuid.UUID, app_conn: Conn
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    prereg(asr, t, PLAN, "holdout", px(t))
    with tn.tenant_transaction(asr, b):
        assert asr.execute("SELECT count(*) FROM app.holdout_series").fetchone() == (0,)
    assert code(lambda: data(asr, b, PLAN)) == "series_missing"
    with (
        tn.tenant_transaction(asr, b),
        pytest.raises(errors.InsufficientPrivilege, match="row-level"),
    ):
        raw_insert(asr, raw_row(t, OTHER, "holdout", px(t)))  # tenant t, session b
    assert asr.execute("SELECT count(*) FROM app.holdout_series").fetchone() == (0,)
    with pytest.raises(errors.InsufficientPrivilege, match="row-level"):  # no tenant
        raw_insert(asr, raw_row(t, OTHER, "holdout", px(t)))


# --- append-only and tamper detection ---


def test_append_only_for_the_assessor_and_the_owner(
    asr: Conn, t: uuid.UUID, conn: Conn
) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))
    for stmt in (
        "UPDATE app.holdout_series SET series_id = 'x'",
        "DELETE FROM app.holdout_series",
        "TRUNCATE app.holdout_series",
    ):
        with tn.tenant_transaction(asr, t), pytest.raises(errors.InsufficientPrivilege):
            asr.execute(stmt)
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(stmt)  # the superuser too: the trigger refuses
    assert data(asr, t, PLAN).series == px(t)


@pytest.mark.parametrize(
    "change",
    [
        "body = jsonb_set(body, '{bars,1,3}', '\"150\"')",  # a friendlier price
        "body = jsonb_set(body, '{bars,1,3}', '150')",  # a JSON number
        "event_hash = repeat('a', 64)",
        "content_hash = repeat('b', 64)",
        "recorded_at = recorded_at - interval '1 second'",
        "series_role = 'px-synth-h'",  # a holdout row relabelled as its baseline
    ],
)
def test_tampering_is_integrity(
    asr: Conn, t: uuid.UUID, conn: Conn, change: str
) -> None:
    prereg(asr, t, PLAN, "holdout", px(t))
    if "series_role" not in change:  # decision 54: one row per plan and baseline
        prereg(asr, t, PLAN, "px-synth-h", px(t, n=5))  # a valid baseline row
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(
            f"UPDATE app.holdout_series SET {change} WHERE tenant_id = %s "
            "AND series_role = 'holdout'",
            (t,),
        )
    assert code(lambda: data(asr, t, PLAN)) == "integrity"
    with (
        tn.tenant_transaction(asr, t) as tx,
        pytest.raises(hs.HoldoutSeriesError, match="integrity"),
    ):
        hs.series(tx, PLAN)
