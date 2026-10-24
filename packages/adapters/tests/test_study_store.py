"""0045 and qw_adapters.study_store (owner decisions
70, 72 and 74): the strategy search space and the evaluation protocol, recorded by
qw_worker, read by qw_assessor alone, one content per id, append-only, the
protocol marked caller-asserted, every row re-hashed on read. Refusals come from
the database through real logins checked to be exactly their role; the superuser
`conn` observes or tampers. SYNTHETIC values only."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from qw_adapters import study_store as ss
from qw_adapters import tenancy as tn
from qw_domain.instants import format_instant
from qw_domain.research_data import digest
from test_strategy_improvement import EVAL, SPACE
from test_study_wire import ODD as ODD_ID1
from test_study_wire import WIDE

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
TABLES = ("strategy_search_space", "evaluation_protocol")
ODD = replace(ODD_ID1, protocol_id="proto-synth-odd")  # EVAL holds proto-synth-1
KINDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture
def wk(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The worker login, checked as exactly qw_worker."""
    with tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as c:
        yield c


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The assessor login, checked as exactly qw_assessor."""
    with tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as c:
        yield c


@pytest.fixture
def t(app_conn: Conn) -> uuid.UUID:
    with tn.provision_tenant(app_conn) as tx:
        return tx.tenant_id


def put(c: Conn, t: uuid.UUID, value: object) -> bool:
    with tn.tenant_transaction(c, t) as tx:
        if isinstance(value, type(SPACE)):
            return ss.record_space(tx, value)
        return ss.record_evaluation(tx, value)  # type: ignore[arg-type]


def get_space(c: Conn, t: uuid.UUID, h: str) -> ss.StoredSpace | None:
    with tn.tenant_transaction(c, t) as tx:
        return ss.space(tx, h)


def get_eval(c: Conn, t: uuid.UUID, h: str) -> ss.StoredEvaluation | None:
    with tn.tenant_transaction(c, t) as tx:
        return ss.evaluation(tx, h)


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(ss.StudyStoreError) as e:
        fn()
    return e.value.code


def raw_space(c: Conn, t: uuid.UUID, at: str = "clock_timestamp()",
              space: Any = SPACE) -> None:  # fmt: skip
    c.execute(
        "INSERT INTO app.strategy_search_space (tenant_id, space_id, content_hash, "
        f"body, recorded_at, event_hash) VALUES (%s, %s, %s, %s, {at}, %s)",
        (t, space.space_id, space.content_hash, Jsonb(space.to_wire()), "0" * 64),
    )


def raw_eval(c: Conn, t: uuid.UUID, mark: object = True,
             at: str = "clock_timestamp()", p: Any = EVAL) -> None:  # fmt: skip
    c.execute(
        "INSERT INTO app.evaluation_protocol (tenant_id, protocol_id, content_hash, "
        "body, approved_by_caller_asserted, recorded_at, event_hash) VALUES "
        f"(%s, %s, %s, %s, %s, {at}, %s)",
        (t, p.protocol_id, p.content_hash, Jsonb(p.to_wire()), mark, "0" * 64),
    )


# --- round trips ---


def test_recorded_inputs_read_back_exactly(wk: Conn, asr: Conn, t: uuid.UUID) -> None:
    for value in (SPACE, WIDE, EVAL, ODD):
        assert put(wk, t, value) is True
    for s in (SPACE, WIDE):
        got = get_space(asr, t, s.content_hash)
        assert got is not None
        assert got.space.to_wire() == s.to_wire()
        assert got.space.content_hash == s.content_hash
    for p in (EVAL, ODD):
        e = get_eval(asr, t, p.content_hash)
        assert e is not None and e.protocol == p
        assert e.protocol.content_hash == p.content_hash
        assert e.approved_by_caller_asserted is True  # decision 72's mark
    odd = get_eval(asr, t, ODD.content_hash)
    assert odd is not None and str(odd.protocol.z) == "1.960"  # exponent kept
    assert str(odd.protocol.costs.commission_rate) == "0.0010"
    assert get_space(asr, t, EVAL.content_hash) is None  # missing: None
    assert get_eval(asr, t, "f" * 64) is None


def test_event_hash_binds_tenant_content_mark_and_time(
    wk: Conn, t: uuid.UUID, conn: Conn
) -> None:
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    (sh, sat), = conn.execute("SELECT event_hash, recorded_at FROM "
                              "app.strategy_search_space").fetchall()  # fmt: skip
    (eh, mark, eat), = conn.execute(
        "SELECT event_hash, approved_by_caller_asserted, recorded_at FROM "
        "app.evaluation_protocol").fetchall()  # fmt: skip
    assert sh == digest(["strategy_search_space", str(t), SPACE.content_hash,
                         SPACE.to_wire(), format_instant(sat)])  # fmt: skip
    assert mark is True
    assert eh == digest(["evaluation_protocol", str(t), EVAL.content_hash,
                         EVAL.to_wire(), True, format_instant(eat)])  # fmt: skip


# --- decision 74: one content per id; an identical re-record is a no-op ---


def test_identical_re_record_is_a_no_op(wk: Conn, t: uuid.UUID, conn: Conn) -> None:
    assert put(wk, t, SPACE) is True and put(wk, t, EVAL) is True
    before = conn.execute("SELECT recorded_at, event_hash FROM "
                          "app.strategy_search_space").fetchall()  # fmt: skip
    assert put(wk, t, SPACE) is False and put(wk, t, EVAL) is False
    after = conn.execute("SELECT recorded_at, event_hash FROM "
                         "app.strategy_search_space").fetchall()  # fmt: skip
    assert after == before  # the first row and its time are kept
    names = conn.execute(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE contype "
        "IN ('p', 'u') AND conrelid = ANY (%s::regclass[]) ORDER BY 1, 2",
        ([f"app.{x}" for x in TABLES],)).fetchall()  # fmt: skip
    assert names == [  # the store reads a primary key conflict as the no-op
        ("app.evaluation_protocol", "evaluation_protocol_one_content_per_id"),
        ("app.evaluation_protocol", "evaluation_protocol_pkey"),
        ("app.strategy_search_space", "strategy_search_space_one_content_per_id"),
        ("app.strategy_search_space", "strategy_search_space_pkey"),
    ]
    for table in TABLES:
        assert conn.execute(f"SELECT count(*) FROM app.{table}").fetchone() == (1,)


def test_a_different_freeze_under_the_same_id_is_refused(
    wk: Conn, asr: Conn, t: uuid.UUID, app_conn: Conn
) -> None:
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    wider = replace(SPACE, max_trials=SPACE.max_trials + 1)  # same space id
    approver = replace(EVAL, approved_by="someone-else")  # same protocol id
    assert code(lambda: put(wk, t, wider)) == "study_input_exists"
    assert code(lambda: put(wk, t, approver)) == "study_input_exists"
    with tn.tenant_transaction(wk, t), pytest.raises(errors.UniqueViolation):
        raw_space(wk, t, space=wider)  # the database's own refusal
    with tn.tenant_transaction(wk, t), pytest.raises(errors.UniqueViolation):
        raw_eval(wk, t, p=approver)
    assert get_space(asr, t, wider.content_hash) is None
    got = get_space(asr, t, SPACE.content_hash)
    assert got is not None and got.space == SPACE
    assert put(wk, t, replace(wider, space_id="space-synth-2")) is True  # a new id
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    assert put(wk, b, wider) is True  # ids are per tenant


# --- decision 72: the caller-asserted mark ---


def test_the_mark_must_be_true(wk: Conn, t: uuid.UUID) -> None:
    with (tn.tenant_transaction(wk, t),
          pytest.raises(errors.CheckViolation, match="caller_asserted")):  # fmt: skip
        raw_eval(wk, t, mark=False)
    with tn.tenant_transaction(wk, t), pytest.raises(errors.NotNullViolation):
        raw_eval(wk, t, mark=None)
    with tn.tenant_transaction(wk, t):
        raw_eval(wk, t)  # control: true is accepted


def test_recorded_at_must_be_the_database_clock(wk: Conn, t: uuid.UUID) -> None:
    for at in ("clock_timestamp() - interval '2 minutes'",
               "clock_timestamp() + interval '1 minute'"):  # fmt: skip
        for table in TABLES:
            with (
                tn.tenant_transaction(wk, t),
                pytest.raises(errors.CheckViolation, match="database clock"),
            ):
                if table == "evaluation_protocol":
                    raw_eval(wk, t, at=at)
                else:
                    raw_space(wk, t, at)


def test_body_must_name_the_row_id(wk: Conn, t: uuid.UUID) -> None:
    pairs = (("strategy_search_space", "space_id", SPACE),
             ("evaluation_protocol", "protocol_id", EVAL))  # fmt: skip
    for table, col, value in pairs:
        mark = ", approved_by_caller_asserted" if col == "protocol_id" else ""
        flag = ", true" if mark else ""
        with tn.tenant_transaction(wk, t), pytest.raises(errors.CheckViolation):
            wk.execute(
                f"INSERT INTO app.{table} (tenant_id, {col}, content_hash, body, "
                f"recorded_at, event_hash{mark}) VALUES (%s, 'other', %s, %s, "
                f"clock_timestamp(), %s{flag})",
                (t, value.content_hash, Jsonb(value.to_wire()), "0" * 64))  # fmt: skip


# --- roles and RLS (decision 72: worker writes, assessor reads) ---


def test_exact_privileges(wk: Conn, conn: Conn) -> None:
    def held(role: str) -> set[tuple[str, str]]:
        rows = conn.execute(
            "SELECT t, k FROM unnest(%s::text[]) t CROSS JOIN unnest(%s::text[]) k "
            "WHERE has_table_privilege(%s, 'app.' || t, k) OR (k IN ('SELECT', "
            "'INSERT', 'UPDATE', 'REFERENCES') AND "
            "has_any_column_privilege(%s, 'app.' || t, k))",
            (list(TABLES), list(KINDS), role, role)).fetchall()  # fmt: skip
        return {(a, b) for a, b in rows}

    assert held("qw_worker") == {(t, "INSERT") for t in TABLES}  # by column only
    assert held("qw_assessor") == {(t, "SELECT") for t in TABLES}
    for role in ("qw_app", "qw_ingest", "qw_release", "public"):
        assert held(role) == set(), role
    for table in TABLES:  # the worker's INSERT is by column, not on the table
        assert conn.execute("SELECT has_table_privilege('qw_worker', %s, 'INSERT')",
                            (f"app.{table}",)).fetchone() == (False,)  # fmt: skip
    cols = conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON "
        "c.oid = a.attrelid WHERE c.relnamespace = 'app'::regnamespace AND "
        "c.relname = ANY (%s) AND a.attnum > 0 AND NOT a.attisdropped AND "
        "has_column_privilege('qw_worker', c.oid, a.attnum, 'INSERT')",
        (list(TABLES),)).fetchall()  # fmt: skip
    assert sorted(cols) == sorted(
        [("strategy_search_space", c) for c in ("tenant_id", *ss._SPACE)]
        + [("evaluation_protocol", c) for c in ("tenant_id", *ss._EVAL)])  # fmt: skip


def test_logins_outside_their_role_are_forbidden(
    wk: Conn, asr: Conn, t: uuid.UUID, runtime_urls: dict[str, str]
) -> None:
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    assert code(lambda: get_space(wk, t, SPACE.content_hash)) == "forbidden"
    assert code(lambda: get_eval(wk, t, EVAL.content_hash)) == "forbidden"
    assert code(lambda: put(asr, t, WIDE)) == "forbidden"
    assert code(lambda: put(asr, t, ODD)) == "forbidden"
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_release")
    for role in roles:
        with tn.runtime_connection(runtime_urls[role], role=role) as c:
            assert code(lambda: put(c, t, WIDE)) == "forbidden"
            assert code(lambda: put(c, t, ODD)) == "forbidden"
            assert code(lambda: get_space(c, t, SPACE.content_hash)) == "forbidden"
            assert code(lambda: get_eval(c, t, EVAL.content_hash)) == "forbidden"


def test_rls_hides_and_refuses_other_tenants(
    wk: Conn, asr: Conn, t: uuid.UUID, app_conn: Conn
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    assert get_space(asr, b, SPACE.content_hash) is None
    assert get_eval(asr, b, EVAL.content_hash) is None
    for table in TABLES:
        with tn.tenant_transaction(asr, b):
            assert asr.execute(f"SELECT count(*) FROM app.{table}").fetchone() == (0,)
    for table in TABLES:
        for session in (b, None):  # tenant t's row in session b; no tenant set
            with (
                tn.tenant_transaction(wk, session) if session else wk.transaction(),
                pytest.raises(errors.InsufficientPrivilege, match="row-level"),
            ):
                if table == "evaluation_protocol":
                    raw_eval(wk, t, p=ODD)
                else:
                    raw_space(wk, t, space=WIDE)


# --- append-only and tamper detection ---


def test_append_only_for_every_login_and_the_owner(
    wk: Conn, asr: Conn, t: uuid.UUID, conn: Conn
) -> None:
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    for table in TABLES:
        for stmt in (f"UPDATE app.{table} SET event_hash = event_hash",
                     f"DELETE FROM app.{table}", f"TRUNCATE app.{table}"):  # fmt: skip
            for c in (wk, asr):
                with (tn.tenant_transaction(c, t),
                      pytest.raises(errors.InsufficientPrivilege)):  # fmt: skip
                    c.execute(stmt)
            with pytest.raises(errors.RaiseException, match="append-only"):
                conn.execute(stmt)  # the superuser too: the trigger refuses
    assert get_space(asr, t, SPACE.content_hash) is not None


def tamper(conn: Conn, table: str, change: str) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"UPDATE app.{table} SET {change}")


@pytest.mark.parametrize(
    "change",
    [
        "body = jsonb_set(body, '{max_trials}', '9')",  # a bigger budget
        "body = jsonb_set(body, '{max_trials}', '\"3\"')",  # a string count
        "body = jsonb_set(body, '{dimensions,0,maximum}', '\"250\"')",  # wider
        "body = jsonb_set(body, '{dimensions,0,maximum}', '200')",  # a JSON number
        "body = jsonb_set(body, '{extra}', 'true')",
        "event_hash = repeat('a', 64)",
        "recorded_at = recorded_at - interval '1 second'",
    ],
)  # fmt: skip
def test_space_tampering_is_integrity(
    wk: Conn, asr: Conn, t: uuid.UUID, conn: Conn, change: str
) -> None:
    put(wk, t, SPACE)
    tamper(conn, "strategy_search_space", change)
    assert code(lambda: get_space(asr, t, SPACE.content_hash)) == "integrity"


@pytest.mark.parametrize(
    "change",
    [
        "body = jsonb_set(body, '{approved_by}', '\"someone-else\"')",
        "body = jsonb_set(body, '{z}', '\"1.96\"')",  # same value, other exponent
        "body = jsonb_set(body, '{z}', '1.96')",  # a JSON number
        "body = jsonb_set(body, '{costs,commission_rate}', '\"0\"')",  # cheaper
        "body = jsonb_set(body, '{declared_at}', '\"2021-01-01T00:00:00+00:00\"')",
        "body = body - 'hac_lags'",
        "event_hash = repeat('a', 64)",
        "recorded_at = recorded_at + interval '1 second'",
    ],
)  # fmt: skip
def test_protocol_tampering_is_integrity(
    wk: Conn, asr: Conn, t: uuid.UUID, conn: Conn, change: str
) -> None:
    put(wk, t, ODD)
    tamper(conn, "evaluation_protocol", change)
    assert code(lambda: get_eval(asr, t, ODD.content_hash)) == "integrity"


def test_a_row_under_another_content_hash_is_integrity(
    wk: Conn, asr: Conn, t: uuid.UUID, conn: Conn
) -> None:
    """A row relabelled to answer for another hash is refused, not served."""
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    for table, h in (("strategy_search_space", WIDE.content_hash),
                     ("evaluation_protocol", ODD.content_hash)):  # fmt: skip
        tamper(conn, table, f"content_hash = '{h}'")
    assert code(lambda: get_space(asr, t, WIDE.content_hash)) == "integrity"
    assert code(lambda: get_eval(asr, t, ODD.content_hash)) == "integrity"


def test_consistent_event_hash_does_not_hide_a_relabel_or_a_cleared_mark(
    wk: Conn, asr: Conn, t: uuid.UUID, conn: Conn
) -> None:
    """The event hash is no secret: a tamperer can recompute it. The read still
    refuses a body that does not hash to the key it answers for, and a mark other
    than true (the CHECK dropped first; this test database is thrown away)."""
    put(wk, t, SPACE)
    put(wk, t, EVAL)
    (at,), = conn.execute("SELECT recorded_at FROM "
                          "app.strategy_search_space").fetchall()  # fmt: skip
    forged = digest(["strategy_search_space", str(t), WIDE.content_hash,
                     SPACE.to_wire(), format_instant(at)])  # fmt: skip
    tamper(
        conn,
        "strategy_search_space",
        f"content_hash = '{WIDE.content_hash}', event_hash = '{forged}'",
    )
    assert code(lambda: get_space(asr, t, WIDE.content_hash)) == "integrity"
    conn.execute("ALTER TABLE app.evaluation_protocol DROP CONSTRAINT "
                 "evaluation_protocol_approved_by_caller_asserted")  # fmt: skip
    (at,), = conn.execute("SELECT recorded_at FROM "
                          "app.evaluation_protocol").fetchall()  # fmt: skip
    forged = digest(["evaluation_protocol", str(t), EVAL.content_hash,
                     EVAL.to_wire(), False, format_instant(at)])  # fmt: skip
    tamper(
        conn,
        "evaluation_protocol",
        f"approved_by_caller_asserted = false, event_hash = '{forged}'",
    )
    assert code(lambda: get_eval(asr, t, EVAL.content_hash)) == "integrity"
