"""Optimizer installation tables (`0047_optimizer_store.sql`; T050, owner decision
87): qw_assessor writes and replays the registry chain, qw_worker writes cycles
blind, qw_assessor writes verified lineage blind, and only the operator's
qw_release login reads cycles and lineage. Real non-superuser logins
(`runtime_connection(url, role=...)`); the superuser `conn` observes or tampers.
SYNTHETIC data only: the T050 domain fixtures, and their recursion is GATE-LIFTED
(`gate_lifted_install`), so nothing here is release proof."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from qw_adapters import optimizer_store as os_
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain.instants import ensure_aware_utc
from test_optimizer_improvement import (
    EXP,
    LIMITS,
    V0,
    V1,
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
)
from test_optimizer_lineage import lineage_receipt
from test_optimizer_measure import MP
from test_release import deployment
from test_research_improvement import BASE, DEV
from test_research_improvement import protocol as research_protocol

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("gate_lifted_install")]
Conn = psycopg.Connection[Any]
INST = "tenant-synth-a"  # the fixtures' installation scope id (SYNTHETIC)
P = MP  # the protocol `logged` logs (lineage_receipt's registry)
TABLES = ("optimizer_registry_entry", "optimizer_cycle", "optimizer_lineage")
ROLES = ("qw_app", "qw_worker", "qw_assessor", "qw_release", "qw_ingest")
E = os_.OptimizerStoreError


def login(urls: dict[str, str], role: tn.RuntimeRole) -> Iterator[Conn]:
    with tn.runtime_connection(urls[role], role=role) as c:
        yield c


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_assessor")


@pytest.fixture
def wrk(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_worker")


@pytest.fixture
def rel(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_release")


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(E) as e:
        fn()
    return e.value.code


def now(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


def logged(asr: Conn) -> tuple[im.ExperimentRegistry, Any, im.Candidate,
                               oi.LineageReceipt]:  # fmt: skip
    reg, lg, nxt, lin = lineage_receipt()
    for e in reg.entries:
        assert put(asr, e)[1]
    return reg, lg, nxt, lin


def put(asr: Conn, e: im.Entry) -> tuple[Any, bool]:
    """Append `e`; a deciding verdict follows its run and outcome (0049)."""
    if type(e) is im.Verdict:
        plan = os_.load(asr, INST, now(asr)).protocol(e.experiment_id).plan_id
        log = os_.RunLog(asr, INST)
        r = os_.Reservation(e.experiment_id, e.candidate_id, e.verdict_id, "a" * 64,
                            "b" * 64)  # fmt: skip
        log.reserve(plan, 1, r)
        log.put_outcome(plan, 1, "c" * 64)
    return os_.append(asr, INST, e)


def v0(c: Conn, cid: str = "cycle-db-1", at: datetime | None = None) -> oi.CycleReceipt:
    reg = im.ExperimentRegistry(INST).register(lineage_receipt()[0].protocol(EXP))
    r = oi.run_cycle(cid, V0, reg, EXP, deployment(), BASE, DEV, LIMITS, (),
                     at or now(c))  # fmt: skip
    assert isinstance(r, oi.CycleReceipt), r
    return r


def test_registry_chain_round_trips_and_refuses_what_the_domain_refuses(
    asr: Conn, rel: Conn, conn: Conn
) -> None:
    reg, _, _, _ = lineage_receipt()
    p, (_, c, v) = reg.protocol(EXP), reg.entries
    assert type(c) is im.Candidate and type(v) is im.Verdict
    assert code(lambda: os_.append(asr, INST, c)) == "unknown_experiment"
    first, wrote = os_.append(asr, INST, p)
    assert wrote and first.event_hash == os_.entry_hash(INST, 1, "protocol", EXP,
        p.wire(), first.recorded_at, None)  # fmt: skip
    again, wrote = os_.append(asr, INST, p)  # identical: nothing written
    assert (again.seq, again.event_hash, wrote) == (1, first.event_hash, False)
    assert code(lambda: os_.append(asr, INST, replace(
        p, hypothesis="SYNTHETIC: other"))) == "protocol_frozen"  # fmt: skip
    for bad in (
        replace(p, scope_id="inst-other"),
        replace(p, scope=im.Scope.TENANT),
        research_protocol(scope=im.Scope.INSTALLATION, scope_id=INST),
    ):
        assert code(partial(os_.append, asr, INST, bad)) == "scope"
    assert code(lambda: os_.append(asr, INST, v)) == "unknown_candidate"
    os_.append(asr, INST, c)
    assert code(lambda: os_.append(asr, INST, replace(v, assessor=p.producer))) == (
        "assessor_mismatch")  # fmt: skip
    put(asr, v)
    for who in (asr, rel):
        got = os_.load(who, INST, now(who))
        assert [e.wire() for e in got.entries] == [e.wire() for e in reg.entries]
        assert os_.load(who, "inst-other", now(who)).entries == ()
    as_of = os_.load(rel, INST, first.recorded_at).entries  # later entries unseen
    assert [e.wire() for e in as_of] == [p.wire()]
    assert code(lambda: os_.load(rel, INST, now(rel) + timedelta(hours=1))) == "as_of"
    p2 = replace(p, experiment_id="exp-2", budget_reservation_id="bud-2",
                 baseline_version="opt-v0b", scorecard=replace(p.scorecard,
                 thresholds=tuple(t for t in p.scorecard.thresholds
                                  if t.metric != "promotion_gain")))  # fmt: skip
    assert code(lambda: os_.append(asr, INST, p2)) == "gain_goal_missing"
    assert count(conn, "optimizer_registry_entry") == 3


@pytest.mark.parametrize("change", ["seq", "prev", "time"])
def test_a_re_hashed_registry_forgery_that_breaks_the_chain_is_integrity(
    asr: Conn, rel: Conn, conn: Conn, change: str
) -> None:
    log = [put(asr, e)[0] for e in lineage_receipt()[0].entries]
    x, prev = log[2], log[1]
    seq, ph, at = x.seq, prev.event_hash, x.recorded_at
    if change == "seq":
        seq = 4
    elif change == "prev":
        ph = log[0].event_hash
    else:
        at = prev.recorded_at - timedelta(microseconds=1)
    h = os_.entry_hash(INST, seq, x.kind, x.entry_id, x.entry.wire(), at, ph)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE app.optimizer_registry_entry SET seq = %s, prev_hash = %s,"
                     " recorded_at = %s, event_hash = %s WHERE seq = 3",
                     (seq, ph, at, h))  # fmt: skip
    assert code(lambda: os_.load(rel, INST, now(rel))) == "integrity"


def test_exact_privileges_hardened_guards_and_forbidden_logins(
    asr: Conn, wrk: Conn, rel: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    held = conn.execute(
        "SELECT t, r, k FROM unnest(%s::text[]) t CROSS JOIN unnest(%s::text[]) r "
        "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
        "'REFERENCES', 'TRIGGER']) k WHERE has_table_privilege(r, 'app.' || t, k) "
        "OR (k IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') "
        "AND has_any_column_privilege(r, 'app.' || t, k))", (list(TABLES), list(ROLES)),
    ).fetchall()  # fmt: skip
    assert set(held) == {
        ("optimizer_registry_entry", "qw_assessor", "SELECT"),
        ("optimizer_registry_entry", "qw_assessor", "INSERT"),
        ("optimizer_cycle", "qw_worker", "INSERT"),
        ("optimizer_cycle", "qw_assessor", "SELECT"),  # owner decision 91
        ("optimizer_lineage", "qw_assessor", "INSERT"),
        *((t, "qw_release", "SELECT") for t in TABLES),
    }
    for t in TABLES:  # the definer guards: callable by no runtime role
        for role in ROLES:
            q = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
            assert conn.execute(q, (role, f"app.{t}_guard()")).fetchone() == (False,)
    reg, _, _, lin = logged(asr)
    r = v0(wrk)
    with tn.runtime_connection(runtime_urls["qw_app"], role="qw_app") as app:
        for who in (app, wrk):
            assert code(partial(os_.append, who, INST, reg.entries[0])) == "forbidden"
            assert code(partial(os_.load, who, INST, now(who))) == "forbidden"
        for who in (app, asr, rel):
            assert code(partial(os_.append_cycle, who, INST, EXP, r, P)) == "forbidden"
        for who in (app, wrk, rel):
            assert code(partial(os_.append_lineage, who, INST, lin)) == "forbidden"
        for who in (app, wrk):
            assert code(partial(os_.load_cycles, who, INST, EXP)) == "forbidden"
        for who in (app, wrk, asr):
            assert code(partial(os_.load_lineage, who, INST, "x")) == "forbidden"
    new = replace(
        reg.protocol(EXP),
        experiment_id="exp-rel",
        budget_reservation_id="bud-rel",
        baseline_version="opt-v0r",
    )
    assert code(partial(os_.append, rel, INST, new)) == "forbidden"  # reads only
    assert [count(conn, t) for t in TABLES] == [3, 0, 0]


def test_cycles_round_trip_and_refuse_duplicates_back_dating_and_forged_runners(
    asr: Conn, wrk: Conn, rel: Conn, conn: Conn
) -> None:
    assert code(lambda: os_.append_cycle(wrk, INST, EXP, v0(wrk), P)) == (
        "runner_not_baseline"
    )  # no logged protocol: no baseline to run
    logged(asr)
    old = v0(wrk, "cycle-db-0", now(wrk) - timedelta(minutes=2))  # no later cycle
    assert code(lambda: os_.append_cycle(wrk, INST, EXP, old, P)) == "back_dated"
    r = v0(wrk)
    s = os_.append_cycle(wrk, INST, EXP, r, P)
    assert os_.load_cycles(rel, INST, EXP) == (s,) and s.receipt.wire() == r.wire()
    assert os_.load_cycles(asr, INST, EXP) == (s,)  # decision 91
    assert os_.load_cycles(rel, INST, "exp-other") == ()
    assert code(lambda: os_.append_cycle(wrk, INST, EXP, r, P)) == "duplicate"
    later = v0(wrk)
    assert later.cycle_id == r.cycle_id and code(
        lambda: os_.append_cycle(wrk, INST, EXP, later, P)) == "duplicate"  # fmt: skip
    for at in (now(wrk) - timedelta(minutes=2), r.at - timedelta(microseconds=1),
               now(wrk) + timedelta(minutes=1)):  # fmt: skip
        bad = replace(r, cycle_id="cycle-db-2", at=at)
        assert code(partial(os_.append_cycle, wrk, INST, EXP, bad, P)) == "back_dated"
    v1, vx = V1, replace(r.config, version_id="opt-vx")  # vx: V0's hash, renamed
    forged = [  # (config, authority): each differs from the baseline in one way more
        (v1, "release:rel-opt-1"),  # the installed V1 claim: not checkable yet
        (replace(v1, version_id="opt-v0"), "baseline:opt-v0"),  # hash only
        (r.config, "release:rel-opt-1"),  # authority only
        (vx, "baseline:opt-v0"),  # version only
    ]
    for i, (cfg, auth) in enumerate(forged):
        f = replace(
            r,
            cycle_id=f"f-{i}",
            config=cfg,
            runner_hash=cfg.content_hash,
            runner_version=cfg.version_id,
            authority=auth,
            at=now(wrk),
        )
        assert code(partial(os_.append_cycle, wrk, INST, EXP, f, P)) == (
            "runner_not_baseline"), i  # fmt: skip
    other = replace(r, cycle_id="cycle-db-3", at=now(wrk))
    assert code(lambda: os_.append_cycle(wrk, INST, "exp-other", other, P)) == (
        "runner_not_baseline")  # fmt: skip
    nxt = os_.append_cycle(wrk, INST, EXP, v0(wrk, "cycle-db-4"), P)
    assert [x.receipt.cycle_id for x in os_.load_cycles(rel, INST, EXP)] == [
        "cycle-db-1", "cycle-db-4"] and nxt.cycle_ref != s.cycle_ref  # fmt: skip
    assert count(conn, "optimizer_cycle") == 2
    with pytest.raises(TypeError):
        os_.append_cycle(wrk, INST, EXP, r.wire(), P)  # type: ignore[arg-type]


def test_lineage_loads_unverified_and_verify_lineage_accepts_it(
    asr: Conn, rel: Conn, conn: Conn
) -> None:
    lin = lineage_receipt()[3]
    assert code(lambda: os_.append_lineage(asr, INST, lin)) == "not_logged"
    _, lg, nxt, lin = logged(asr)
    at = os_.append_lineage(asr, INST, lin)
    got = os_.load_lineage(rel, INST, lin.claim.lineage_id)
    assert type(got) is oi.UnverifiedLineage and got == lin.claim
    assert os_.load_lineage(rel, INST, "lineage-none") is None
    stored = os_.load(rel, INST, now(rel))
    assert oi.verify_lineage(got, stored, lg, BASE, DEV, nxt,
                             accept_synthetic_evidence=True) == lin  # fmt: skip
    assert code(lambda: os_.append_lineage(asr, INST, lin)) == "duplicate"
    with pytest.raises(TypeError):
        os_.append_lineage(asr, INST, got)  # type: ignore[arg-type]
    row = conn.execute("SELECT recorded_at FROM app.optimizer_lineage").fetchone()
    assert row == (at,) and count(conn, "optimizer_lineage") == 1


@pytest.mark.parametrize("table", TABLES)
def test_db_clock_window_refusals(
    asr: Conn, wrk: Conn, conn: Conn, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    reg, _, _, lin = lineage_receipt()
    if table != "optimizer_registry_entry":
        logged(asr)
    r = v0(wrk)
    real = os_._now
    for shift in (timedelta(minutes=-2), timedelta(minutes=1)):

        def skewed(c: Conn, lock: str | None = None, shared: bool = False,
                   d: timedelta = shift) -> datetime:  # fmt: skip
            return real(c, lock, shared) + d

        monkeypatch.setattr(os_, "_now", skewed)
        if table == "optimizer_registry_entry":
            got = code(partial(os_.append, asr, INST, reg.entries[0]))
        elif table == "optimizer_cycle":
            got = code(partial(os_.append_cycle, wrk, INST, EXP, r, P))
        else:
            got = code(partial(os_.append_lineage, asr, INST, lin))
        assert got == "clock", shift
    assert count(conn, table) == 0


def test_append_only_for_every_login_and_the_owner(
    asr: Conn, wrk: Conn, rel: Conn, conn: Conn
) -> None:
    _, _, _, lin = logged(asr)
    os_.append_cycle(wrk, INST, EXP, v0(wrk), P)
    os_.append_lineage(asr, INST, lin)
    for t in TABLES:
        for stmt in (f"UPDATE app.{t} SET body = body", f"DELETE FROM app.{t}",
                     f"TRUNCATE app.{t}"):  # fmt: skip
            for c in (asr, wrk, rel):
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(stmt)
            with pytest.raises(errors.RaiseException, match="append-only"):
                conn.execute(stmt)  # the superuser too: the trigger refuses
            with conn.transaction(), pytest.raises(errors.RaiseException):
                conn.execute("SET LOCAL ROLE qw_migrate")  # the owner
                conn.execute(stmt)
    assert [count(conn, t) for t in TABLES] == [3, 1, 1]


def raw_entry(c: Conn, seq: int, prev: str | None, body: dict[str, str], *,
              inst: str = INST, at: str = "clock_timestamp()") -> None:  # fmt: skip
    """A direct qw_assessor insert that skips the store (event hash "0" * 64)."""
    kind = "verdict" if "verdict_id" in body else "protocol"
    if kind == "verdict":  # 0049: a deciding verdict needs a run
        body = {"decision": "blocked"} | body
    eid = body.get("verdict_id", body.get("experiment_id"))
    c.execute("INSERT INTO app.optimizer_registry_entry (installation_id, seq, kind, "
              "entry_id, body, recorded_at, prev_hash, event_hash) VALUES (%s, %s, "
              f"%s, %s, %s, {at}, %s, repeat('0', 64))",
              (inst, seq, kind, eid, Jsonb(body), prev))  # fmt: skip


def test_the_database_guards_hold_the_chain_head_and_the_lineage_citations(
    asr: Conn, conn: Conn
) -> None:
    first, _ = os_.append(asr, INST, lineage_receipt()[0].entries[0])
    h, vb = first.event_hash, {"verdict_id": "v-x"}
    back = f"'{(first.recorded_at - timedelta(microseconds=1)).isoformat()}'"
    for seq, prev, at in (
        (3, h, "clock_timestamp()"),
        (2, "1" * 64, "now()"),
        (2, None, "now()"),
        (1, None, "now()"),
        (2, h, back),
    ):
        with pytest.raises((errors.CheckViolation, errors.UniqueViolation)):
            raw_entry(asr, seq, prev, vb, at=at)
    proto = {"experiment_id": "exp-x", "target": "research", "scope": "installation",
             "scope_id": INST}  # fmt: skip
    with pytest.raises(errors.CheckViolation, match="optimizer_registry_entry_check"):
        raw_entry(asr, 2, h, proto)  # 0047 CHECK: optimizer protocols only
    lin = lineage_receipt()[3]
    cl, opt = lin.claim, proto | {"target": "recursive_optimizer"}
    cited = {"verdict_id": cl.verdict_id, "experiment_id": EXP,
             "candidate_id": cl.candidate_id}  # fmt: skip
    raw_entry(asr, 1, None, cited, inst="inst-a")  # a verdict and no protocol
    for inst, other in (("inst-b", {"candidate_id": "cand-other"}),
                        ("inst-c", {"experiment_id": "exp-other"})):  # fmt: skip
        raw_entry(asr, 1, None, opt | {"experiment_id": EXP, "scope_id": inst},
                  inst=inst)  # fmt: skip
        raw_entry(asr, 2, "0" * 64, cited | other, inst=inst)
    for inst in ("inst-a", "inst-b", "inst-c"):
        assert code(partial(os_.append_lineage, asr, inst, lin)) == "not_logged"
    assert count(conn, "optimizer_lineage") == 0


TAMPER = [
    (
        "optimizer_registry_entry",
        "body = jsonb_set(body, '{hypothesis}', '\"SYNTHETIC: forged\"') WHERE seq = 1",
    ),
    ("optimizer_registry_entry", "event_hash = repeat('a', 64) WHERE seq = 2"),
    ("optimizer_registry_entry", "prev_hash = repeat('b', 64) WHERE seq = 3"),
    ("optimizer_registry_entry", "seq = 4 WHERE seq = 3"),
    ("optimizer_registry_entry", "DELETE WHERE seq = 2"),  # a broken chain
    (
        "optimizer_registry_entry",
        "recorded_at = recorded_at - interval '1 hour' WHERE seq = 3",
    ),
    ("optimizer_cycle", "body = jsonb_set(body, '{declared_cost}', '\"0.1\"')"),
    ("optimizer_cycle", "body = jsonb_set(body, '{proposals}', '[{\"knob\": 1}]')"),
    ("optimizer_cycle", "event_hash = repeat('a', 64)"),
    ("optimizer_cycle", "recorded_at = recorded_at - interval '1 microsecond'"),
    ("optimizer_cycle", "experiment_id = 'exp-moved'"),
    (
        "optimizer_lineage",
        "body = jsonb_set(body, '{install_operator_id}', '\"op-forged\"')",
    ),
    ("optimizer_lineage", "event_hash = repeat('a', 64)"),
    ("optimizer_lineage", "installation_id = 'inst-moved'"),
    (
        "optimizer_lineage",
        "lineage_id = 'lineage-moved', body = jsonb_set(body, "
        "'{lineage_id}', '\"lineage-moved\"')",
    ),
]


@pytest.mark.parametrize(("table", "change"), TAMPER)
def test_tampering_on_read_is_integrity(
    asr: Conn, wrk: Conn, rel: Conn, conn: Conn, table: str, change: str
) -> None:
    _, _, _, lin = logged(asr)
    os_.append_cycle(wrk, INST, EXP, v0(wrk), P)
    os_.append_lineage(asr, INST, lin)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"DELETE FROM app.{table} {change.removeprefix('DELETE ')}"
                     if change.startswith("DELETE") else
                     f"UPDATE app.{table} SET {change}")  # fmt: skip
    if table == "optimizer_registry_entry":
        assert code(lambda: os_.load(rel, INST, now(rel))) == "integrity"
        p = lineage_receipt()[0].entries[0]
        assert code(lambda: os_.append(asr, INST, p)) == "integrity"
    elif table == "optimizer_cycle":
        exp = "exp-moved" if "moved" in change else EXP
        assert code(lambda: os_.load_cycles(rel, INST, exp)) == "integrity"
    else:
        lid = "lineage-moved" if "lineage-moved" in change else lin.claim.lineage_id
        inst = "inst-moved" if "inst-moved" in change else INST
        assert code(lambda: os_.load_lineage(rel, inst, lid)) == "integrity"


def raw_cycle(c: Conn, body: Any, p: im.Protocol = P) -> None:
    """A direct qw_worker insert that skips the store; its columns are the body's,
    so only `optimizer_cycle_wire` can refuse a well-keyed one."""
    cid = body["cycle_id"]
    c.execute("INSERT INTO app.optimizer_cycle (cycle_ref, installation_id, "
              "experiment_id, cycle_id, runner_hash, authority, body, receipt_hash, "
              "recorded_at, event_hash, protocol) VALUES (gen_random_uuid(), %s, %s, "
              "%s, %s, %s, %s, %s, clock_timestamp(), repeat('0', 64), %s)",
              (INST, EXP, cid, body.get("runner_hash", "a" * 64),
               body.get("authority", "baseline:opt-v0"), Jsonb(body),
               body.get("receipt_hash", "b" * 64),
               Jsonb(p.wire())))  # fmt: skip


def test_a_raw_worker_insert_of_a_malformed_cycle_is_refused(
    asr: Conn, wrk: Conn, rel: Conn, conn: Conn
) -> None:
    logged(asr)
    w: dict[str, Any] = v0(wrk).wire()
    at = w["at"]
    keep = ("cycle_id", "runner_hash", "runner_version", "authority", "receipt_hash")
    junk = {k: w[k] for k in keep}  # passes the guard
    bad = [
        junk | {"at": at},  # not the wire's key set
        w | {"extra": "x"},
        w | {"proposals": "none"},
        w | {"declared_cost": 0.5},
        w | {"at": "now"},  # read under the session's settings, before 0047's fix
        w | {"at": at.replace("T", " ")},
        w | {"at": at.removesuffix("Z") + "+00:00"},
        w | {"at": at.removesuffix("Z")},  # naive
    ]
    for b in bad:
        with pytest.raises(errors.CheckViolation, match="optimizer_cycle_wire"):
            raw_cycle(wrk, b)
    forged = replace(P, hypothesis="SYNTHETIC: forged", plan_id="plan-forged")
    with pytest.raises(errors.CheckViolation, match="protocol_mismatch"):
        raw_cycle(wrk, w, forged)  # 0051: not the protocol it is logged under
    wrk.execute("SET TimeZone = 'America/New_York'")  # the session cannot shift it
    wrk.execute("SET DateStyle = 'SQL, DMY'")
    raw_cycle(wrk, w)  # the canonical wire is accepted, read as UTC
    assert count(conn, "optimizer_cycle") == 1
    assert code(lambda: os_.load_cycles(rel, INST, EXP)) == "integrity"  # bad hash


def test_concurrent_appends_serialize_on_the_installation_locks(
    asr: Conn, wrk: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    import threading

    reg = lineage_receipt()[0]
    os_.append(asr, INST, reg.entries[0])
    t0 = now(wrk)
    late, early = v0(wrk, "race-late", t0 + timedelta(milliseconds=1)), v0(
        wrk, "race-early", t0)  # fmt: skip
    out: dict[str, object] = {}

    def run(role: tn.RuntimeRole, fn: Callable[[Conn], object]) -> None:
        with tn.runtime_connection(runtime_urls[role], role=role) as other:
            try:
                out[role] = fn(other)
            except E as exc:
                out[role] = exc.code
            except psycopg.Error as exc:
                out[role] = f"{type(exc).__name__}: {exc}"

    races: list[tuple[tn.RuntimeRole, Conn, Callable[[Conn], object],
                      Callable[[Conn], object]]] = [
        # the registry head: the second replays only after the first commits
        ("qw_assessor", asr, partial(os_.append, installation_id=INST,
                                     e=reg.entries[1]),
         partial(os_.append, installation_id=INST, e=reg.entries[1])),
        # a cycle racing an earlier-dated one: the guard sees the later one
        ("qw_worker", wrk, partial(os_.append_cycle, installation_id=INST,
                                   experiment_id=EXP, receipt=late, protocol=P),
         partial(os_.append_cycle, installation_id=INST, experiment_id=EXP,
                 receipt=early, protocol=P)),
    ]  # fmt: skip
    for role, holder, first, second in races:
        with holder.transaction():  # the first append holds its lock until commit
            first(holder)
            t = threading.Thread(target=run, args=(role, second))
            t.start()
            t.join(2)
            assert t.is_alive(), role  # the second waits
        t.join(30)
        assert not t.is_alive(), role
    got = out["qw_assessor"]
    assert isinstance(got, tuple) and got[1] is False and got[0].seq == 2
    assert out["qw_worker"] == "back_dated"
    assert [count(conn, t) for t in TABLES[:2]] == [2, 1]
    row = conn.execute(
        "SELECT event_hash FROM app.optimizer_registry_entry WHERE seq = 2"
    ).fetchone()  # raw inserts: the guard's lock
    assert row is not None
    h = row[0]
    with asr.transaction():
        raw_entry(asr, 3, h, {"verdict_id": "v-r"})
        t = threading.Thread(target=run, args=("qw_assessor", lambda c: raw_entry(
            c, 3, h, {"verdict_id": "v-s"})))  # fmt: skip
        t.start()
        t.join(2)
    t.join(30)
    assert "registry_moved" in str(out["qw_assessor"])  # not the seq unique index


def test_receipt_and_lineage_hashes_are_unique_per_installation(
    asr: Conn, wrk: Conn, rel: Conn
) -> None:
    _, _, _, lin = logged(asr)
    r = v0(wrk)
    os_.append_cycle(wrk, INST, EXP, r, P)
    os_.append_lineage(asr, INST, lin)
    cl, other = lin.claim, "inst-2"  # a second installation logging the same ids
    raw_entry(
        asr,
        1,
        None,
        replace(P, scope_id=other).wire(),  # type: ignore[arg-type]
        inst=other,
    )
    raw_entry(asr, 2, "0" * 64, {"verdict_id": cl.verdict_id, "experiment_id": EXP,
              "candidate_id": cl.candidate_id}, inst=other)  # fmt: skip
    q = replace(P, scope_id=other)
    assert os_.append_cycle(wrk, other, EXP, r, q).receipt == r
    os_.append_lineage(asr, other, lin)
    assert code(lambda: os_.append_cycle(wrk, other, EXP, r, q)) == "duplicate"
    assert code(lambda: os_.append_lineage(asr, other, lin)) == "duplicate"
    assert os_.load_lineage(rel, other, cl.lineage_id) == cl
