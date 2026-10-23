"""The experiment registry log (owner decisions 71 and 79 to 81; provisional
migration 0042, 0044 at commit): qw_assessor reads it and appends protocols and
candidates through the domain registry's rules; qw_worker, which cannot read it,
appends verdicts after the head the assessor side hands over; the log replays as
of an instant, so later entries never change a request's registry hash. Real logins
(`runtime_connection(url, role=...)`); the superuser `conn` observes or tampers.
SYNTHETIC data only (the test_improvement and test_assessor_run worlds)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from qw_adapters import assessment_request_store as ars
from qw_adapters import experiment_registry_store as ers
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain.budget import BudgetBook
from qw_domain.feedback import Access, ImprovementEvidence, Purpose
from qw_domain.instants import ensure_aware_utc
from qw_domain.research_data import digest
from test_improvement import (
    ASSESSOR,
    AT,
    CAPS,
    EXP,
    GOOD,
    PRODUCER,
    cand,
    promo_ledger,
    proto,
)

_spec = importlib.util.spec_from_file_location(  # as test_assessment_request_store
    "qw_arun_tests", Path(__file__).with_name("test_assessor_run.py")
)
assert _spec is not None and _spec.loader is not None
tars = sys.modules.setdefault("qw_arun_tests", importlib.util.module_from_spec(_spec))
if not hasattr(tars, "world"):
    _spec.loader.exec_module(tars)
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
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
def t(app_conn: Conn) -> uuid.UUID:
    with tn.provision_tenant(app_conn) as tx:
        return tx.tenant_id


def entries(t: uuid.UUID) -> tuple[im.Protocol, im.Candidate, im.Verdict]:
    p = proto(scope_id=str(t))
    reg = im.ExperimentRegistry(str(t)).register(p).register_candidate(cand())
    book, _ = im.schedule(reg, (EXP,), BudgetBook.open(str(t), AT, CAPS), AT)
    v = im.assess(reg, EXP, "cand-1", promo_ledger(GOOD), book, ASSESSOR,
                  AT + timedelta(days=1), "verdict-1")  # fmt: skip
    return p, cand(), v


def put(c: Conn, t: uuid.UUID, e: Any, **kw: Any) -> tuple[ers.LogEntry, bool]:
    with tn.tenant_transaction(c, t) as tx:
        return ers.append(tx, e, **kw)


def head(asr: Conn, t: uuid.UUID, v: im.Verdict) -> ers.Head | None:
    with tn.tenant_transaction(asr, t) as tx:
        return ers.verdict_head(tx, v)


def verdict(wrk: Conn, t: uuid.UUID, v: im.Verdict, h: ers.Head) -> ers.LogEntry:
    with tn.tenant_transaction(wrk, t) as tx:
        return ers.append_verdict(tx, v, h)


def log_all(asr: Conn, wrk: Conn, t: uuid.UUID) -> list[ers.LogEntry]:
    p, c, v = entries(t)
    out = [put(asr, t, p)[0], put(asr, t, c)[0]]
    h = head(asr, t, v)
    assert h is not None
    return [*out, verdict(wrk, t, v, h)]


def now(c: Conn, t: uuid.UUID) -> datetime:
    with tn.tenant_transaction(c, t) as tx:
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        return ensure_aware_utc(row[0])


def load(c: Conn, t: uuid.UUID, at: datetime | None = None) -> im.ExperimentRegistry:
    when = at or now(c, t)
    with tn.tenant_transaction(c, t) as tx:
        return ers.load(tx, str(t), when)


def wires(reg: im.ExperimentRegistry) -> list[object]:
    return [e.wire() for e in reg.entries]


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(ers.ExperimentRegistryError) as e:
        fn()
    return e.value.code


def rows(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM app.experiment_registry_entry").fetchone()
    assert row is not None
    return int(row[0])


def test_append_replays_through_the_domain_and_load_round_trips(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    p, c, v = entries(t)
    got = [put(asr, t, p), put(asr, t, c)]
    h = head(asr, t, v)
    assert h == ers.Head(2, got[1][0].event_hash)  # handed over to the worker
    x = verdict(wrk, t, v, h)
    assert [(e.seq, e.kind, e.entry_id) for e in (got[0][0], got[1][0], x)] == [
        (1, "protocol", EXP),
        (2, "candidate", "cand-1"),
        (3, "verdict", "verdict-1"),
    ]
    for e, (y, _) in zip((p, c), got, strict=True):  # identical: nothing written
        again, wrote = put(asr, t, e)
        assert (again.seq, again.event_hash, wrote) == (y.seq, y.event_hash, False)
    assert head(asr, t, v) is None  # logged once
    assert rows(conn) == 3
    reg = load(asr, t)
    assert wires(reg) == [p.wire(), c.wire(), v.wire()]  # wire order: see _replay
    assert reg.verdicts("cand-1") == (v,) and reg.protocol(EXP).content_hash == (
        p.content_hash)  # fmt: skip
    assert ars.registry_hash(reg) == digest(["experiment_registry", str(t), [
        ["Protocol", p.wire()], ["Candidate", c.wire()],
        ["Verdict", v.wire()]]])  # fmt: skip
    first = got[0][0]  # independent oracle for the chain
    assert first.event_hash == digest(["experiment_registry_entry", str(t), 1,
        "protocol", EXP, p.wire(), first.recorded_at.isoformat().replace(
            "+00:00", "Z"), None])  # fmt: skip
    assert x.recorded_at >= got[1][0].recorded_at


def test_append_refuses_what_the_domain_refuses_and_writes_nothing(
    t: uuid.UUID, asr: Conn, conn: Conn
) -> None:
    p, c, v = entries(t)
    assert code(lambda: put(asr, t, c)) == "unknown_experiment"
    assert code(lambda: head(asr, t, v)) == "unknown_candidate"
    put(asr, t, p)
    assert code(lambda: put(asr, t, replace(p, hypothesis="SYNTHETIC: other"))) == (
        "protocol_frozen")  # fmt: skip
    assert code(lambda: put(asr, t, proto(scope_id=str(uuid.uuid4())))) == "scope"
    inst = proto(scope_id=str(t), scope=im.Scope.INSTALLATION, experiment_id="e-2")
    assert code(lambda: put(asr, t, inst)) == "scope"
    put(asr, t, c)
    store = ImprovementEvidence()  # a logged T043 development selection
    use = store.select(str(t), EXP, Purpose.PERSONAL_ADAPTATION, Access.DEVELOPMENT,
                       AT).use  # fmt: skip
    used = cand("cand-e", artifact_hash="e" * 64, evidence_uses=(use,))
    assert code(lambda: put(asr, t, used)) == "evidence_not_logged"
    assert code(lambda: put(asr, t, used, evidence=ImprovementEvidence())) == (
        "evidence_not_logged")  # fmt: skip
    assert put(asr, t, used, evidence=store)[0].seq == 3
    assert code(lambda: head(asr, t, replace(v, assessor=PRODUCER))) == (
        "assessor_mismatch")  # fmt: skip
    assert code(lambda: head(asr, t, replace(v, candidate_hash="0" * 64))) == (
        "hash_mismatch")  # fmt: skip
    assert code(lambda: put(asr, t, cand("cand-2"))) == "identical_artifact"
    other = proto(scope_id=str(t), experiment_id="e-3", budget_reservation_id="b-3",
                  baseline_version="base-3")  # fmt: skip
    put(asr, t, other)  # decision 80: a candidate id is unique in the tenant
    again = cand(experiment_id="e-3", parent_id="base-3", artifact_hash="7" * 64)
    assert code(lambda: put(asr, t, again)) == "duplicate"
    assert rows(conn) == 4
    for bad in (im.ExperimentRegistry(str(t)), v):
        with pytest.raises(TypeError):
            put(asr, t, bad)


def test_decision_79_the_worker_writes_blind_after_the_handed_over_head(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    p, c, v = entries(t)
    put(asr, t, p)
    put(asr, t, c)
    h = head(asr, t, v)
    assert h is not None
    put(asr, t, cand("cand-2", artifact_hash="2" * 64))  # the log moves on
    assert code(lambda: verdict(wrk, t, v, h)) == "registry_moved"
    for stale in (ers.Head(3, h.event_hash), ers.Head(9, None)):
        assert code(partial(verdict, wrk, t, v, stale)) == "registry_moved"
    assert rows(conn) == 3
    fresh = head(asr, t, v)
    assert fresh is not None and verdict(wrk, t, v, fresh).seq == 4
    with pytest.raises(TypeError):
        verdict(wrk, t, v, (3, None))  # type: ignore[arg-type]


def test_only_the_assessor_reads_and_each_role_writes_its_own_kinds(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    held = conn.execute(
        "SELECT r, k FROM unnest(%s::text[]) r CROSS JOIN unnest(ARRAY['SELECT', "
        "'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) k WHERE "
        "has_table_privilege(r, 'app.experiment_registry_entry', k) OR (k IN "
        "('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') AND has_any_column_privilege("
        "r, 'app.experiment_registry_entry', k))", (list(ROLES),),
    ).fetchall()  # fmt: skip
    assert set(held) == {("qw_worker", "INSERT"), ("qw_assessor", "INSERT"),
                         ("qw_assessor", "SELECT")}  # fmt: skip
    guard = "app.experiment_registry_entry_guard()"
    for r in ROLES:  # the definer guard is callable by no runtime role
        assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                            (r, guard)).fetchone() == (False,)  # fmt: skip
    p, _, v = entries(t)
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_release")
    for role in roles:
        with tn.runtime_connection(runtime_urls[role], role=role) as rc:
            assert code(partial(put, rc, t, p)) == "forbidden"
            with tn.tenant_transaction(rc, t) as tx:
                assert code(partial(ers.load, tx, str(t), AT)) == "forbidden"
    logged = log_all(asr, wrk, t)
    assert code(lambda: put(wrk, t, cand("cand-2", artifact_hash="2" * 64))) == (
        "forbidden"
    )  # the worker cannot replay, so it cannot append these kinds
    with tn.tenant_transaction(wrk, t) as tx:
        assert code(partial(ers.load, tx, str(t), AT)) == "forbidden"
    with tn.tenant_transaction(wrk, t) as tx:
        assert code(partial(ers.verdict_head, tx, v)) == "forbidden"
    for sql in ("SELECT count(*) FROM app.experiment_registry_entry",
                "SELECT body -> 'scores' FROM app.experiment_registry_entry",
                "SELECT event_hash FROM app.experiment_registry_entry"):  # fmt: skip
        with tn.tenant_transaction(wrk, t), pytest.raises(errors.InsufficientPrivilege):
            wrk.execute(sql)
    top = ers.Head(3, logged[2].event_hash)
    with tn.tenant_transaction(wrk, t) as tx:  # a protocol by the worker: refused
        assert code(partial(ers._insert, tx, replace(p, experiment_id="e-w"), top,
                            ers._now(tx, False))) == "forbidden"  # fmt: skip
    with tn.tenant_transaction(asr, t) as tx:  # a verdict by the assessor: refused
        assert code(partial(ers.append_verdict, tx, replace(v, verdict_id="v-a"),
                            top)) == "forbidden"  # fmt: skip
    assert rows(conn) == 3


def raw(c: Conn, t: uuid.UUID, seq: int, prev: str | None, body: object, *,
        kind: str = "protocol", eid: str = EXP, at: str = "clock_timestamp()",
        h: str = "0" * 64) -> None:  # fmt: skip
    c.execute("INSERT INTO app.experiment_registry_entry (tenant_id, seq, kind, "
              "entry_id, body, recorded_at, prev_hash, event_hash) VALUES (%s, %s, "
              f"%s, %s, %s, {at}, %s, %s)",
              (t, seq, kind, eid, Jsonb(body), prev, h))  # fmt: skip


def test_rls_hides_and_refuses_other_tenants(
    t: uuid.UUID, wrk: Conn, asr: Conn, app_conn: Conn
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    put(asr, t, entries(t)[0])
    assert load(asr, b).entries == ()
    with tn.tenant_transaction(asr, b) as tx:
        assert code(partial(ers.load, tx, str(t), AT)) == "scope"
    assert code(lambda: put(asr, b, entries(t)[0])) == "scope"
    for c, kind, key in ((asr, "protocol", "experiment_id"),
                         (wrk, "verdict", "verdict_id")):  # fmt: skip
        with (
            tn.tenant_transaction(c, b),
            pytest.raises(errors.InsufficientPrivilege, match="row-level"),
        ):
            raw(c, t, 1, None, {key: EXP}, kind=kind)  # tenant t in session b


def test_database_guard_holds_the_head_clock_and_columns(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    first, _ = put(asr, t, entries(t)[0])
    h, back = first.event_hash, first.recorded_at - timedelta(microseconds=1)
    bad: list[dict[str, Any]] = [
        {"seq": 3, "prev": h},  # a gap
        {"seq": 2, "prev": "1" * 64},  # not the head's hash
        {"seq": 2, "prev": None},  # no previous hash
        {"seq": 1, "prev": None},  # the head again
        {"seq": 2, "prev": h, "at": "clock_timestamp() - interval '2 minutes'"},
        {"seq": 2, "prev": h, "at": "clock_timestamp() + interval '1 minute'"},
        {"seq": 2, "prev": h, "body": {"verdict_id": "v-2"}},  # body and id
        {"seq": 2, "prev": h, "kind": "protocol", "eid": EXP, "by": asr},  # again
    ]
    for case in bad:
        kind, eid = case.get("kind", "verdict"), case.get("eid", "v-1")
        key = {"protocol": "experiment_id", "candidate": "candidate_id"}.get(
            kind, "verdict_id")  # fmt: skip
        body = case.get("body", {key: eid})
        refused = (errors.CheckViolation, errors.UniqueViolation)
        with tn.tenant_transaction(case.get("by", wrk), t), pytest.raises(refused):
            raw(case.get("by", wrk), t, case["seq"], case["prev"], body, kind=kind,
                eid=eid, at=case.get("at", "clock_timestamp()"))  # fmt: skip
    with tn.tenant_transaction(wrk, t), pytest.raises(errors.CheckViolation):
        raw(
            wrk,
            t,
            2,
            h,
            {"verdict_id": "v-1"},
            kind="verdict",
            eid="v-1",
            at=f"'{back.isoformat()}'::timestamptz",
        )  # time backwards
    with conn.transaction(), pytest.raises(errors.CheckViolation):  # the owner path
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(t),))
        raw(conn, t, 2, h, {"x": "k"}, kind="trial", eid="k")
    assert rows(conn) == 1


def test_append_only_for_every_login_and_the_owner(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn
) -> None:
    put(asr, t, entries(t)[0])
    for stmt in ("UPDATE app.experiment_registry_entry SET entry_id = 'e-z'",
                 "DELETE FROM app.experiment_registry_entry",
                 "TRUNCATE app.experiment_registry_entry"):  # fmt: skip
        for c in (wrk, asr):
            with tn.tenant_transaction(c, t), pytest.raises(
                    errors.InsufficientPrivilege):  # fmt: skip
                c.execute(stmt)
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(stmt)  # the superuser too: the trigger refuses
    assert rows(conn) == 1


@pytest.mark.parametrize(
    "change",
    [
        "body = jsonb_set(body, '{hypothesis}', '\"SYNTHETIC: forged\"') WHERE seq = 1",
        "body = jsonb_set(body, '{payload,lookback_months}', '\"12\"') WHERE seq = 2",
        "body = jsonb_set(body, '{decision}', '\"accept_for_review\"') WHERE seq = 3",
        "body = jsonb_set(body, '{scores,net_return}', '2') WHERE seq = 3",
        "event_hash = repeat('a', 64) WHERE seq = 2",
        "prev_hash = repeat('b', 64) WHERE seq = 3",
        "recorded_at = recorded_at - interval '1 microsecond' WHERE seq = 2",
        "entry_id = 'cand-x', body = jsonb_set(body, '{candidate_id}', '\"cand-x\"') "
        "WHERE seq = 2",
        "kind = 'candidate', entry_id = 'v-x', body = jsonb_set(body, "
        "'{candidate_id}', '\"v-x\"') WHERE seq = 3",
        "seq = 4 WHERE seq = 3",  # a gap
        "DELETE FROM app.experiment_registry_entry WHERE seq = 2",  # a dropped entry
    ],
)
def test_tampering_and_chain_breaks_are_integrity(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn, change: str
) -> None:
    log_all(asr, wrk, t)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(change if change.startswith("DELETE") else
                     f"UPDATE app.experiment_registry_entry SET {change}")  # fmt: skip
    assert code(lambda: load(asr, t)) == "integrity"
    assert code(lambda: put(asr, t, cand("cand-3", artifact_hash="3" * 64))) == (
        "integrity")  # fmt: skip


@pytest.mark.parametrize("change", ["seq", "prev", "time"])
def test_a_re_hashed_forgery_that_breaks_the_chain_is_integrity(
    t: uuid.UUID, wrk: Conn, asr: Conn, conn: Conn, change: str
) -> None:
    logged = log_all(asr, wrk, t)
    x, prev = logged[2], logged[1]  # the verdict entry, re-hashed after the change
    seq, ph, at = x.seq, prev.event_hash, x.recorded_at
    if change == "seq":
        seq = 4
    elif change == "prev":
        ph = logged[0].event_hash
    else:
        at = prev.recorded_at - timedelta(microseconds=1)
    h = ers.entry_hash(t, seq, x.kind, x.entry_id, x.entry.wire(), at, ph)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE app.experiment_registry_entry SET seq = %s, prev_hash "
                     "= %s, recorded_at = %s, event_hash = %s WHERE seq = 3",
                     (seq, ph, at, h))  # fmt: skip
    assert code(lambda: load(asr, t)) == "integrity"


def test_replay_refuses_a_sequence_the_domain_refuses(
    t: uuid.UUID, wrk: Conn, asr: Conn
) -> None:
    v = entries(t)[2]  # a blind qw_worker verdict, correctly chained, no protocol
    verdict(wrk, t, v, ers.Head(0, None))
    assert code(lambda: load(asr, t)) == "integrity"
    assert code(lambda: put(asr, t, entries(t)[0])) == "integrity"


def test_replay_as_of_an_instant_ignores_later_entries(
    t: uuid.UUID, wrk: Conn, asr: Conn
) -> None:
    p, c, _ = entries(t)
    put(asr, t, p)
    mid = now(asr, t)
    put(asr, t, c)
    assert wires(load(asr, t, mid)) == [p.wire()]
    assert wires(load(asr, t)) == [p.wire(), c.wire()]
    assert load(asr, t, mid - timedelta(days=1)).entries == ()
    late = now(asr, t) + timedelta(minutes=1)
    with tn.tenant_transaction(asr, t) as tx:
        assert code(partial(ers.load, tx, str(t), late)) == "as_of"
    with tn.tenant_transaction(asr, t) as tx, pytest.raises(ValueError):
        ers.load(tx, str(t), mid.replace(tzinfo=None))  # naive


def test_decision_71_a_later_entry_never_changes_a_requests_registry_hash(
    app_conn: Conn, wrk: Conn, asr: Conn
) -> None:
    with tn.provision_tenant(app_conn) as ptx:
        w = tars.world(ptx.tenant_id)
    w = replace(w, ledger=tars.research(w, w.ledger, 1))  # as test_assessment_request
    for e in w.reg.entries:
        put(asr, w.t, e)
    reg = load(asr, w.t)  # the assessor side reads; the worker submits blind
    r = replace(tars.request(w), registry=reg)
    with tn.tenant_transaction(wrk, w.t) as tx:
        ars.submit(tx, r)
    assert wires(reg) == wires(w.reg)
    later = replace(w.reg.candidate("cand-1"), candidate_id="cand-2",
                    artifact_hash="9" * 64)  # fmt: skip
    put(asr, w.t, later)
    with tn.tenant_transaction(asr, w.t) as tx:
        stored = ars.load(tx, w.study.protocol.experiment_id, "v-p")
        assert stored is not None
        then = ers.load(tx, str(w.t), stored.recorded_at)
    assert ars.registry_hash(then) == stored.body["inputs"]["registry"]
    ars.check(stored, replace(r, registry=then))  # the run's inputs still match
    current = load(asr, w.t)
    assert wires(current) == [*wires(w.reg), later.wire()]
    with pytest.raises(ars.AssessmentRequestError, match=r"inputs\.registry"):
        ars.check(stored, replace(r, registry=current))
