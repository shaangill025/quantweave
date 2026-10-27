"""The optimizer run log (0049, `optimizer_store.RunLog`; T050 increment 6): real
non-superuser logins; the superuser `conn` observes or tampers. SYNTHETIC only;
GATE-LIFTED tests use `lifted_assess`, so nothing here is release proof."""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import optimizer_store as os_
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from test_improvement import ASSESSOR
from test_optimizer_improvement import EXP
from test_optimizer_measure import MP


def _load(name: str, path: Path) -> Any:  # as apps/worker/tests: no package here
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


Conn = psycopg.Connection[Any]
ot = _load("qw_assessor_run_optimizer_tests",
           Path(__file__).parent / "test_assessor_run_optimizer.py")  # fmt: skip
INST: str = ot.INST
Crash: type[Exception] = ot.Crash
code: Callable[[Callable[[], object]], str] = ot.code
crash_on: Callable[[int], Callable[[], object]] = ot.crash_on
entries: Callable[[Conn], int] = ot.entries
lifted: Callable[..., aro.Run] = ot.lifted
lifted_assess: aro.Assess = ot.lifted_assess
login: Callable[[dict[str, str], tn.RuntimeRole], Iterator[Conn]] = ot.login
now: Callable[[Conn], datetime] = ot.now
stored_world: Callable[[Conn, Conn], aro.Request] = ot.stored_world
pytestmark = pytest.mark.db
TABLES = ("optimizer_run", "optimizer_run_outcome")
GUARDS = ("optimizer_run_guard()", "optimizer_run_outcome_guard()",
          "optimizer_run_verdict_guard()")  # fmt: skip
ROLES = ("qw_app", "qw_worker", "qw_assessor", "qw_release", "qw_ingest")
PLAN = MP.plan_id
MINE = os_.Reservation(EXP, "cand-opt-1", "verdict-opt-db-1", "a" * 64, "b" * 64)


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_assessor")


@pytest.fixture
def world(asr: Conn, runtime_urls: dict[str, str]) -> aro.Request:
    for wrk in login(runtime_urls, "qw_worker"):  # closed once the world is stored
        r = stored_world(asr, wrk)
    return r


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


def verdict(asr: Conn, vid: str = "verdict-opt-db-1") -> im.Verdict:
    """An INSUFFICIENT verdict of the world's candidate (nothing measured)."""
    return lifted_assess(os_.load(asr, INST, now(asr)), EXP, "cand-opt-1", None,
                         ASSESSOR, now(asr), vid)  # fmt: skip


def raw_run(c: Conn, attempt: int, plan: str = PLAN, at: str = "clock_timestamp()",
            **kw: str) -> None:  # fmt: skip
    """A direct qw_assessor insert that skips the store (event hash "0" * 64)."""
    v = {"experiment_id": EXP, "candidate_id": "cand-opt-1",
         "verdict_id": "verdict-opt-db-1", "fixture_hash": "a" * 64,
         "inputs_digest": "b" * 64} | kw  # fmt: skip
    c.execute("INSERT INTO app.optimizer_run (installation_id, plan_id, attempt, "
              f"{', '.join(v)}, recorded_at, event_hash) VALUES (%s, %s, %s, "
              f"{', '.join(['%s'] * len(v))}, {at}, repeat('0', 64))",
              (INST, plan, attempt, *v.values()))  # fmt: skip


def test_exact_privileges_hardened_guards_and_forbidden_logins(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str], world: aro.Request
) -> None:
    held = conn.execute(
        "SELECT t, r, k FROM unnest(%s::text[]) t CROSS JOIN unnest(%s::text[]) r "
        "CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
        "'REFERENCES', 'TRIGGER']) k WHERE has_table_privilege(r, 'app.' || t, k) "
        "OR (k IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') "
        "AND has_any_column_privilege(r, 'app.' || t, k))", (list(TABLES), list(ROLES)),
    ).fetchall()  # fmt: skip
    assert set(held) == {(t, "qw_assessor", k) for t in TABLES
                         for k in ("SELECT", "INSERT")}  # fmt: skip
    for g in GUARDS:
        for role in ROLES:
            q = "SELECT has_function_privilege(%s, %s, 'EXECUTE')"
            assert conn.execute(q, (role, f"app.{g}")).fetchone() == (False,)
        src = conn.execute(
            f"SELECT prosrc FROM pg_proc WHERE oid = 'app.{g}'::regprocedure"
        ).fetchone()
        assert src is not None and "pg_advisory_xact_lock" in src[0], g  # its own lock
    os_.RunLog(asr, INST).reserve(PLAN, 1, MINE)
    for role in ("qw_app", "qw_worker", "qw_release"):
        for c in login(runtime_urls, role):  # type: ignore[arg-type]
            log = os_.RunLog(c, INST)
            for fn in (partial(log.attempts, PLAN), partial(log.outcome, PLAN),
                       partial(log.reserve, PLAN, 2, MINE),
                       partial(log.put_outcome, PLAN, 1, "c" * 64)):  # fmt: skip
                assert code(fn) == "forbidden", role
    assert [count(conn, t) for t in TABLES] == [1, 0]


def test_append_only_for_every_login_and_the_owner(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str], world: aro.Request
) -> None:
    log = os_.RunLog(asr, INST)
    log.reserve(PLAN, 1, MINE)
    log.put_outcome(PLAN, 1, "c" * 64)
    for t in TABLES:
        for stmt in (f"UPDATE app.{t} SET attempt = attempt", f"DELETE FROM app.{t}",
                     f"TRUNCATE app.{t} CASCADE"):  # fmt: skip
            for role in ("qw_assessor", "qw_worker", "qw_release"):
                for c in login(runtime_urls, role):
                    with pytest.raises(errors.InsufficientPrivilege):
                        c.execute(stmt)
            with pytest.raises(errors.RaiseException, match="append-only"):
                conn.execute(stmt)  # the superuser too: the trigger refuses
            with conn.transaction(), pytest.raises(errors.RaiseException):
                conn.execute("SET LOCAL ROLE qw_migrate")  # the owner
                conn.execute(stmt)
    assert [count(conn, t) for t in TABLES] == [1, 1]


def test_db_clock_window_refusals(
    asr: Conn, conn: Conn, monkeypatch: pytest.MonkeyPatch, world: aro.Request
) -> None:
    log, real = os_.RunLog(asr, INST), os_._now

    def skewed(d: timedelta) -> Callable[..., datetime]:
        def at(c: Conn, lock: str | None = None, shared: bool = False) -> datetime:
            return real(c, lock, shared) + d

        return at

    calls = (partial(log.reserve, PLAN, 1, MINE),
             partial(log.put_outcome, PLAN, 1, "c" * 64))  # fmt: skip
    for n, call in enumerate(calls):
        if n:
            log.reserve(PLAN, 1, MINE)  # the real clock
        for shift in (timedelta(minutes=-2), timedelta(minutes=1)):
            with monkeypatch.context() as m:
                m.setattr(os_, "_now", skewed(shift))
                assert code(call) == "clock", (n, shift)
    assert [count(conn, t) for t in TABLES] == [1, 0]


def test_reruns_are_capped_and_must_repeat_the_first_attempt(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    log = os_.RunLog(asr, INST)
    for plan, r in (("plan-x", MINE), (PLAN, replace(MINE, experiment_id="exp-x")),
                    (PLAN, replace(MINE, candidate_id="cand-x"))):  # fmt: skip
        assert code(partial(log.reserve, plan, 1, r)) == "not_logged"
    assert code(partial(log.reserve, PLAN, 2, MINE)) == "attempt_moved"
    log.reserve(PLAN, 1, MINE)
    for bad in (replace(MINE, inputs="c" * 64), replace(MINE, fixture_hash="c" * 64),
                replace(MINE, verdict_id="verdict-x")):  # fmt: skip
        assert code(partial(log.reserve, PLAN, 2, bad)) == "rerun_mismatch"
    for n in (1, 3):  # a stale or skipped attempt number
        assert code(partial(log.reserve, PLAN, n, MINE)) == "attempt_moved"
    # the guard holds without the store: raw qw_assessor inserts
    for plan, n, kw, want in (
        (PLAN, 2, {"verdict_id": "verdict-x"}, "rerun_mismatch"),
        (PLAN, 2, {"inputs_digest": "c" * 64}, "rerun_mismatch"),
        (PLAN, 2, {"fixture_hash": "c" * 64}, "rerun_mismatch"),
        (PLAN, 2, {"candidate_id": "cand-x"}, "rerun_mismatch"),
        (PLAN, 3, {}, "attempt_moved"),
        ("plan-y", 1, {}, "not_logged"),
        ("plan-y", 2, {}, "attempt_moved"),
    ):
        with pytest.raises(errors.CheckViolation, match=want):
            raw_run(asr, n, plan, **kw)
    for n in (2, 3, 4):
        log.reserve(PLAN, n, MINE)
    assert log.attempts(PLAN) == (MINE,) * aro.MAX_ATTEMPTS
    assert code(partial(log.reserve, PLAN, 5, MINE)) == "rerun_used"
    assert log.attempts("plan-x") == () and log.outcome(PLAN) is None
    with pytest.raises(errors.CheckViolation, match="rerun_used"):
        raw_run(asr, 5)
    assert count(conn, "optimizer_run") == aro.MAX_ATTEMPTS


def test_the_outcome_digest_is_first_once_and_before_the_verdict(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    log, before = os_.RunLog(asr, INST), entries(conn)
    log.reserve(PLAN, 1, MINE)
    assert code(lambda: os_.append(asr, INST, verdict(asr))) == "run_reserved"
    assert code(partial(log.put_outcome, PLAN, 2, "c" * 64)) == "not_evaluating"
    log.reserve(PLAN, 2, MINE)
    assert code(partial(log.put_outcome, PLAN, 1, "c" * 64)) == "not_evaluating"
    log.put_outcome(PLAN, 2, "c" * 64)
    log.put_outcome(PLAN, 2, "c" * 64)  # the same digest: nothing new
    assert code(partial(log.put_outcome, PLAN, 2, "d" * 64)) == "verdict_conflict"
    assert log.outcome(PLAN) == "c" * 64 and count(conn, TABLES[1]) == 1
    other = verdict(asr, "verdict-opt-db-2")  # not the run's verdict
    assert code(lambda: os_.append(asr, INST, other)) == "run_reserved"
    assert entries(conn) == before
    os_.append(asr, INST, verdict(asr))  # the run's, after its outcome
    assert entries(conn) == before + 1


def test_no_reservation_once_the_run_is_recorded_and_no_verdict_without_one(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    log, v = os_.RunLog(asr, INST), verdict(asr)
    for d, why in ((im.Decision.ACCEPT_FOR_REVIEW, ()),
                   (im.Decision.REJECT, ("goal:promotion_gain",))):  # fmt: skip
        decided = replace(v, decision=d, reasons=why)
        assert code(lambda x=decided: os_.append(asr, INST, x)) == "not_reserved"  # type: ignore[misc]
    blocked = replace(v, verdict_id="verdict-opt-db-0", decision=im.Decision.BLOCKED,
                      reasons=("sandbox_unqualified",))  # fmt: skip
    assert os_.append(asr, INST, blocked)[1]  # no run: BLOCKED is logged
    assert code(partial(log.reserve, PLAN, 1, MINE)) == "run_recorded"  # candidate
    assert count(conn, TABLES[0]) == 0


def test_a_recorded_run_takes_no_new_attempt(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    log = os_.RunLog(asr, INST)
    log.reserve(PLAN, 1, MINE)
    log.put_outcome(PLAN, 1, "c" * 64)
    os_.append(asr, INST, verdict(asr))  # the run's verdict
    assert code(partial(log.reserve, PLAN, 2, MINE)) == "run_recorded"
    with pytest.raises(errors.CheckViolation, match="run_recorded"):
        raw_run(asr, 2)
    assert count(conn, TABLES[0]) == 1


def test_time_inside_a_run_never_goes_back(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    os_.RunLog(asr, INST).reserve(PLAN, 1, MINE)
    row = conn.execute("SELECT recorded_at FROM app.optimizer_run").fetchone()
    assert row is not None
    back = f"'{(row[0] - timedelta(microseconds=1)).isoformat()}'"
    with pytest.raises(errors.CheckViolation, match="back_dated"):
        raw_run(asr, 2, at=back)
    with pytest.raises(errors.CheckViolation, match="back_dated"):
        asr.execute("INSERT INTO app.optimizer_run_outcome (installation_id, plan_id, "
                    f"attempt, outcome_digest, recorded_at, event_hash) VALUES (%s, %s,"
                    f" 1, repeat('c', 64), {back}, repeat('0', 64))",
                    (INST, PLAN))  # fmt: skip
    assert [count(conn, t) for t in TABLES] == [1, 0]


def test_two_connections_reserve_a_plan_once(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str], world: aro.Request
) -> None:
    out: dict[str, object] = {}

    def run(key: str, fn: Callable[[Conn], object]) -> None:
        for other in login(runtime_urls, "qw_assessor"):
            try:
                out[key] = fn(other)
            except os_.OptimizerStoreError as exc:
                out[key] = exc.code
            except psycopg.Error as exc:
                out[key] = f"{type(exc).__name__}: {exc}"

    def raw_outcome(c: Conn) -> None:  # the evaluating attempt is read under the lock
        c.execute("INSERT INTO app.optimizer_run_outcome (installation_id, plan_id, "
                  "attempt, outcome_digest, recorded_at, event_hash) VALUES (%s, %s, "
                  "2, repeat('c', 64), clock_timestamp(), repeat('0', 64))",
                  (INST, PLAN))  # fmt: skip

    def store(c: Conn) -> None:
        os_.RunLog(c, INST).reserve(PLAN, 1, MINE)

    races: list[tuple[str, Callable[[Conn], object], Callable[[Conn], object]]] = [
        ("store", store, store),
        ("raw", partial(raw_run, attempt=2), partial(raw_run, attempt=2)),
        ("outcome", partial(raw_run, attempt=3), raw_outcome),  # attempt 2 is stale
    ]
    for key, first, second in races:
        with asr.transaction():  # the first holds the lock until it commits
            first(asr)
            t = threading.Thread(target=run, args=(key, second))
            t.start()
            t.join(2)
            assert t.is_alive(), key  # the second waits
        t.join(30)
        assert not t.is_alive(), key
    assert out["store"] == "attempt_moved"
    assert "attempt_moved" in str(out["raw"])  # not the primary key
    assert "not_evaluating" in str(out["outcome"])
    assert [count(conn, t) for t in TABLES] == [3, 0]


TAMPER = [
    ("optimizer_run", "inputs_digest = repeat('c', 64) WHERE attempt = 2"),
    ("optimizer_run", "verdict_id = 'verdict-x' WHERE attempt = 1"),
    ("optimizer_run", "event_hash = repeat('c', 64) WHERE attempt = 2"),
    ("optimizer_run", "recorded_at = recorded_at - interval '1 microsecond'"),
    ("optimizer_run", "attempt = 3 WHERE attempt = 2"),
    (  # attempts 1 and 2 swapped: each row keeps its own valid time and hash
        "optimizer_run",
        "(recorded_at, event_hash) = (SELECT o.recorded_at, o.event_hash FROM "
        "app.optimizer_run o WHERE o.attempt = 3 - optimizer_run.attempt)",
    ),
    ("optimizer_run", "DELETE WHERE attempt = 1"),
    ("optimizer_run", "installation_id = 'inst-moved'"),
    ("optimizer_run_outcome", "outcome_digest = repeat('d', 64)"),
    ("optimizer_run_outcome", "attempt = 1"),
    ("optimizer_run_outcome", "event_hash = repeat('d', 64)"),
    ("optimizer_run_outcome", "plan_id = 'plan-moved'"),
]


@pytest.mark.parametrize(("table", "change"), TAMPER)
def test_tampering_on_read_is_integrity(
    asr: Conn, conn: Conn, world: aro.Request, table: str, change: str
) -> None:
    log = os_.RunLog(asr, INST)
    log.reserve(PLAN, 1, MINE)
    log.reserve(PLAN, 2, MINE)
    log.put_outcome(PLAN, 2, "c" * 64)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"DELETE FROM app.{table} {change.removeprefix('DELETE ')}"
                     if change.startswith("DELETE") else
                     f"UPDATE app.{table} SET {change}")  # fmt: skip
    moved = os_.RunLog(asr, "inst-moved")
    if table == "optimizer_run":
        read = moved if "inst-moved" in change else log
        assert code(partial(read.attempts, PLAN)) == "integrity"
        assert code(partial(read.reserve, PLAN, 3, MINE)) == "integrity"
    else:
        plan = "plan-moved" if "moved" in change else PLAN
        assert code(partial(log.outcome, plan)) == "integrity"
        assert code(partial(log.put_outcome, plan, 2, "c" * 64)) == "integrity"


def test_the_real_trial_passes_the_durable_log_and_records_nothing(
    asr: Conn, conn: Conn, world: aro.Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, logs = entries(conn), list[object]()
    real = aro._after_gate

    def spy(*a: Any) -> aro.Run:
        logs.append(a[8])  # the `log` argument
        return real(*a)

    monkeypatch.setattr(aro, "_after_gate", spy)
    assert code(lambda: aro.trial(asr, world)) == "sandbox_unqualified"
    assert logs == []  # blocked before anything past the gate
    # GATE-LIFTED: past the gate the durable log is passed, and no inputs exist
    monkeypatch.setattr(aro, "measured_assess", lifted_assess)
    assert code(lambda: aro.trial(asr, world)) == "trial_inputs_unavailable"
    assert logs == [os_.RunLog(asr, INST)] and type(logs[0]) is os_.RunLog
    assert [count(conn, t) for t in TABLES] == [0, 0] and entries(conn) == before


def test_gate_lifted_trial_on_the_durable_log(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str], world: aro.Request,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """GATE-LIFTED SYNTHETIC: a crash after the reservation, decision 42's re-run
    under the recorded read, the verdict from `measure`'s metrics, then decision
    60's conflict and decision 59's cap on a second world (another installation's
    log would be keyed apart; here the plan's own log is used once)."""
    log, before = os_.RunLog(asr, INST), entries(conn)
    with pytest.raises(Crash):
        lifted(asr, world, log, crash_on(1))
    assert len(log.attempts(PLAN)) == 1 and log.outcome(PLAN) is None
    later = replace(world, at=world.at + timedelta(seconds=1))
    assert code(lambda: lifted(asr, later, log)) == "rerun_mismatch"
    with pytest.raises(Crash):
        lifted(asr, world, log, crash_on(2))  # after the outcome, before the verdict
    first = log.outcome(PLAN)
    real = oi.measure

    def other(*a: Any) -> Any:  # a re-run whose outcome differs (non-determinism)
        return dict(real(*a)) | {"promotion_gain": real(*a)["promotion_gain"] + 1}

    with monkeypatch.context() as m:
        m.setattr(oi, "measure", other)
        assert code(lambda: lifted(asr, world, log)) == "verdict_conflict"
    assert log.outcome(PLAN) == first and entries(conn) == before
    run = lifted(asr, world, log)
    assert (run.attempt, run.wrote, run.verdict.decision) == (
        4, True, im.Decision.ACCEPT_FOR_REVIEW)  # fmt: skip
    assert run.verdict.trial_ids == (f"measured:{PLAN}",)
    assert "synthetic_only" in run.verdict.limitations
    assert code(lambda: lifted(asr, world, log)) == "run_recorded"
    assert [count(conn, t) for t in TABLES] == [4, 1] and entries(conn) == before + 1
    assert code(partial(log.reserve, PLAN, 5, log.attempts(PLAN)[0])) == (
        "run_recorded")  # fmt: skip
