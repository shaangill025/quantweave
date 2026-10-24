"""The assessment request store (owner decisions 43, 64, 65 and 67): qw_worker
submits a promotion request, holding the caller-supplied trial ledger and budget
book in full, the strategy reference (id, version, implementation hash) and every
other input by content hash; qw_assessor only reads it back, re-hashed and
re-parsed, and `check` refuses a run whose inputs differ. Append-only, forced
RLS. Real logins (`runtime_connection(url, role=...)`); the superuser `conn`
observes or tampers. SYNTHETIC data only (the test_assessor_run world)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_strategy_improvement as fx
from psycopg import errors
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from qw_adapters import assessment_request_store as ars
from qw_adapters import assessor_run as ar
from qw_adapters import strategy_implementations as sim
from qw_adapters import tenancy as tn
from qw_domain import strategy_improvement as si
from qw_domain.research_data import digest
from qw_domain.rights import Registry
from test_improvement import PRODUCER
from test_research_data import iid

_spec = importlib.util.spec_from_file_location(
    "qw_arun_tests", Path(__file__).with_name("test_assessor_run.py")
)
assert _spec is not None and _spec.loader is not None
_run = sys.modules.setdefault("qw_arun_tests", importlib.util.module_from_spec(_spec))
if not hasattr(_run, "world"):
    _spec.loader.exec_module(_run)
request, research, world = _run.request, _run.research, _run.world
World = Any  # test_assessor_run.World, loaded at run time
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
EXP = fx.iproto().experiment_id
ROLES = ("qw_app", "qw_worker", "qw_release", "qw_ingest", "qw_assessor")


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as c:
        yield c


@pytest.fixture
def wrk(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as c:
        yield c


@pytest.fixture
def w(app_conn: Conn) -> World:
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    w = world(t)
    return replace(w, ledger=research(w, w.ledger, 1))  # a plan and one access


def submit(c: Conn, t: uuid.UUID, r: ar.Request) -> str:
    with tn.tenant_transaction(c, t) as tx:
        return ars.submit(tx, r)


def load(c: Conn, t: uuid.UUID, verdict_id: str = "v-p") -> ars.StoredRequest | None:
    with tn.tenant_transaction(c, t) as tx:
        return ars.load(tx, EXP, verdict_id)


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(ars.AssessmentRequestError) as e:
        fn()
    return e.value.code


def rows(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM app.assessment_request").fetchone()
    assert row is not None
    return int(row[0])


def test_round_trip_keeps_ledger_book_strategy_and_input_hashes(
    w: World, wrk: Conn, asr: Conn
) -> None:
    r = request(w)
    h = submit(wrk, w.t, r)
    s = load(asr, w.t)
    assert s is not None and s.request_hash == h
    assert (s.experiment_id, s.verdict_id) == (EXP, "v-p")
    assert s.ledger == w.ledger and s.book == w.book  # in full (decision 67)
    n = len(w.ledger.entries)
    assert n == 2 and s.ledger.checkpoint(n) == w.ledger.checkpoint(n)
    m = fx.STRATS.manifest(fx.BASE.strategy_id, "cand-1")
    # decisions 65, 68 and 69: the reviewed base strategy and its registry hash
    assert s.strategy == sim.StrategyRef(fx.BASE.strategy_id, fx.BASE.version,
        sim.implementation_hash(fx.BASE.strategy_id, fx.BASE.version,
                                _run.synth_win)) == r.strategy  # fmt: skip
    assert s.body["strategy"] == [fx.BASE.strategy_id, "0.1.0",
                                  r.strategy.implementation_hash]  # fmt: skip
    st = w.study
    assert s.body["inputs"] == {  # independent oracle for each binding
        "study": digest(["strategy_study", st.protocol.content_hash,
                         st.space.content_hash, st.evaluation.content_hash,
                         st.base.material_hash]),
        "registry": digest(["experiment_registry", w.reg.scope_id,
                            [[type(e).__name__, e.wire()] for e in w.reg.entries]]),
        "strategy_manifest": m.material_hash,
        "dataset": w.ds.manifest.content_hash,
        "rights": s.body["inputs"]["rights"],
    }  # fmt: skip
    assert s.body["at"].endswith("Z") and s.body["book"] == w.book.to_wire()
    ars.check(s, r)  # the same inputs pass
    # decision 77: rights are checked at the run's time of use, so the time alone
    # is not bound (the terms and each feed's decision are)
    ars.check(s, replace(r, rights=replace(w.rights, at=fx.AT_P)))
    assert load(asr, w.t, "v-other") is None


@pytest.mark.parametrize(
    ("field", "change"),
    [
        ("ledger", lambda w: {"ledger": research(w, w.ledger, 2)}),
        ("book", lambda w: {"book": replace(w.book, version=w.book.version + 1)}),
        ("at", lambda w: {"at": fx.AT_P + fx.H}),
        ("instrument", lambda w: {"instrument": iid(4)}),
        ("access_id", lambda w: {"access_id": "acc-q"}),
        ("assessor", lambda w: {"assessor": PRODUCER}),
        ("strategy", lambda w: {"strategy": _run.ref(_run.synth_held)}),
        ("inputs.rights", lambda w: {"rights": replace(w.rights, jurisdiction="QC")}),
        ("inputs.rights", lambda w: {"rights": replace(w.rights, retain_for=fx.H)}),
        ("inputs.rights", lambda w: {"rights": replace(w.rights, registry=Registry())}),
        (
            "inputs.registry",
            lambda w: {
                "registry": w.reg.register_candidate(
                    fx.cand(si.derive(fx.BASE, fx.SPACE, {"lookback": 80}, "cand-2"))
                )
            },
        ),
    ],
)
def test_check_refuses_a_run_whose_inputs_differ(
    w: World, wrk: Conn, asr: Conn, field: str, change: Callable[[World], Any]
) -> None:
    submit(wrk, w.t, request(w))
    s = load(asr, w.t)
    assert s is not None
    with pytest.raises(ars.AssessmentRequestError, match=field) as e:
        ars.check(s, request(w, **change(w)))
    assert e.value.code == "request_mismatch"


def test_one_request_per_verdict_and_tenant_scope(
    w: World, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    submit(wrk, w.t, request(w))
    assert code(lambda: submit(wrk, w.t, request(w))) == "request_exists"
    other = world(uuid.uuid4())  # a ledger of another tenant
    assert code(lambda: submit(wrk, w.t, request(other))) == "tenant_mismatch"
    assert rows(conn) == 1


def test_only_qw_worker_inserts_and_only_qw_assessor_reads(
    w: World, wrk: Conn, asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    held = conn.execute(
        "SELECT r, k FROM unnest(%s::text[]) r CROSS JOIN unnest(ARRAY['SELECT', "
        "'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) k WHERE "
        "has_table_privilege(r, 'app.assessment_request', k) OR (k IN ('SELECT', "
        "'INSERT', 'UPDATE', 'REFERENCES') AND has_any_column_privilege(r, "
        "'app.assessment_request', k))", (list(ROLES),)).fetchall()  # fmt: skip
    assert set(held) == {("qw_worker", "INSERT"), ("qw_assessor", "SELECT")}
    r = request(w)
    assert code(lambda: submit(asr, w.t, r)) == "forbidden"
    for role in ("qw_app", "qw_release"):
        with tn.runtime_connection(runtime_urls[role], role=role) as c:
            assert code(partial(submit, c, w.t, r)) == "forbidden"
            assert code(partial(load, c, w.t)) == "forbidden"
    submit(wrk, w.t, r)
    assert code(lambda: load(wrk, w.t)) == "forbidden"  # the worker cannot read
    assert load(asr, w.t) is not None


def test_an_ingest_login_holds_nothing(
    w: World, wrk: Conn, conn: Conn, database_url: str
) -> None:
    submit(wrk, w.t, request(w))
    name = f"qwtest_ar_{uuid.uuid4().hex[:12]}"
    conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE qw_ingest")
                 .format(pgsql.Identifier(name)))  # fmt: skip
    try:
        with psycopg.connect(make_conninfo(database_url, user=name),
                             autocommit=True) as c:  # fmt: skip
            for stmt in ("SELECT body FROM app.assessment_request",
                         "DELETE FROM app.assessment_request"):  # fmt: skip
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(stmt)
    finally:
        conn.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(name)))


def test_rls_hides_and_refuses_other_tenants(
    w: World, wrk: Conn, asr: Conn, app_conn: Conn
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    submit(wrk, w.t, request(w))
    assert load(asr, b) is None
    with tn.tenant_transaction(asr, b):
        assert rows(asr) == 0
    assert code(lambda: submit(wrk, b, request(w))) == "tenant_mismatch"
    with (
        tn.tenant_transaction(wrk, b),
        pytest.raises(errors.InsufficientPrivilege, match="row-level"),
    ):
        raw_insert(wrk, w.t, "v-raw")  # tenant w.t in session b


def raw_insert(
    c: Conn, t: uuid.UUID, vid: str, at: str = "clock_timestamp()", lt: str = ""
) -> None:
    body = {"experiment_id": EXP, "verdict_id": vid,
            "ledger": {"tenant_id": lt or str(t)},
            "book": {}, "strategy": ["s", "v", "a" * 64]}  # fmt: skip
    c.execute(
        "INSERT INTO app.assessment_request (tenant_id, experiment_id, verdict_id, "
        f"body, recorded_at, request_hash) VALUES (%s, %s, %s, %s, {at}, %s)",
        (t, EXP, vid, psycopg.types.json.Jsonb(body), "0" * 64),
    )


def test_database_clock_and_body_columns(w: World, wrk: Conn, conn: Conn) -> None:
    for at in ("clock_timestamp() - interval '2 minutes'",
               "clock_timestamp() + interval '1 minute'"):  # fmt: skip
        with tn.tenant_transaction(wrk, w.t), pytest.raises(errors.CheckViolation):
            raw_insert(wrk, w.t, "v-clock", at)
    with tn.tenant_transaction(wrk, w.t), pytest.raises(errors.CheckViolation):
        raw_insert(wrk, w.t, "v-lt", lt=str(uuid.uuid4()))  # another tenant's ledger
    with tn.tenant_transaction(wrk, w.t), pytest.raises(errors.CheckViolation):
        wrk.execute("INSERT INTO app.assessment_request (tenant_id, experiment_id, "
                    "verdict_id, body, recorded_at, request_hash) VALUES (%s, %s, "
                    "'v-x', '{\"verdict_id\": \"v-y\"}', clock_timestamp(), %s)",
                    (w.t, EXP, "0" * 64))  # fmt: skip
    assert rows(conn) == 0


def test_append_only_for_every_login_and_the_owner(
    w: World, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    submit(wrk, w.t, request(w))
    for stmt in ("UPDATE app.assessment_request SET verdict_id = 'v-z'",
                 "DELETE FROM app.assessment_request",
                 "TRUNCATE app.assessment_request"):  # fmt: skip
        for c in (wrk, asr):
            with tn.tenant_transaction(c, w.t), pytest.raises(
                    errors.InsufficientPrivilege):  # fmt: skip
                c.execute(stmt)
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(stmt)  # the superuser too: the trigger refuses
    s = load(asr, w.t)
    assert s is not None and s.ledger == w.ledger


@pytest.mark.parametrize(
    "change",
    [
        # the ledger's granted research access dropped: one trial fewer counted
        "body = jsonb_set(body, '{ledger,entries}', (body #> '{ledger,entries}') - 1)",
        "body = jsonb_set(body, '{book,version}', '7')",
        "body = jsonb_set(body, '{book,version}', '\"0\"')",  # not canonical
        "body = jsonb_set(body, '{inputs,dataset}', to_jsonb(repeat('c', 64)))",
        "body = jsonb_set(body, '{strategy,2}', to_jsonb(repeat('d', 64)))",
        "request_hash = repeat('a', 64)",
        "recorded_at = recorded_at - interval '1 second'",
        "verdict_id = 'v-q', body = jsonb_set(body, '{verdict_id}', '\"v-q\"')",
    ],
)
def test_tampering_is_integrity(
    w: World, wrk: Conn, asr: Conn, conn: Conn, change: str
) -> None:
    submit(wrk, w.t, request(w))
    vid = "v-q" if "v-q" in change else "v-p"
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"UPDATE app.assessment_request SET {change} "
                     "WHERE tenant_id = %s", (w.t,))  # fmt: skip
    assert code(lambda: load(asr, w.t, vid)) == "integrity"
