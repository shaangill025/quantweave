"""The assessor-run optimizer trial (T050 increment 5; decisions 39 to 43, 59 to 62,
87 and 91). Real non-superuser logins (`runtime_connection(url, role=...)`): the
qw_assessor login reads the registry and V0's stored cycle, which qw_worker wrote.
SYNTHETIC data only (the T050 domain fixtures). The real path ends at
`sandbox_unqualified`; tests marked GATE-LIFTED inject `lifted_assess` (the
production `oi.assess_measured` with T044's sandbox gate lifted for that call
only), the inputs and an in-memory or the durable run log; production passes no
inputs and cannot lift the gate, so nothing here is release proof."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
import pytest
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import optimizer_store as os_
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain.instants import ensure_aware_utc
from test_improvement import ASSESSOR, PRODUCER
from test_optimizer_improvement import EXP, LIMITS, V0
from test_optimizer_measure import MP, RP
from test_release import deployment
from test_research_improvement import BASE, DEV, FIXTURE, PLAN
from test_trial_ledger import T0

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
INST = "tenant-synth-a"  # the fixtures' installation scope id (SYNTHETIC)
E = aro.TrialError


def login(urls: dict[str, str], role: tn.RuntimeRole) -> Iterator[Conn]:
    with tn.runtime_connection(urls[role], role=role) as c:
        yield c


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_assessor")


@pytest.fixture
def wrk(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_worker")


def now(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def entries(c: Conn) -> int:
    row = c.execute("SELECT count(*) FROM app.optimizer_registry_entry").fetchone()
    assert row is not None
    return int(row[0])


def stored_world(asr: Conn, wrk: Conn) -> aro.Request:
    """The protocol logged, V0's cycle stored by qw_worker, the receipt-derived V1
    logged; the request names them, assessed by the registered assessor."""
    os_.append(asr, INST, MP)
    reg = im.ExperimentRegistry(INST).register(MP)
    r0 = oi.run_cycle("cycle-v0-db", V0, reg, EXP, deployment(), BASE, DEV, LIMITS,
                      (), now(wrk))  # fmt: skip
    assert isinstance(r0, oi.CycleReceipt), r0
    os_.append_cycle(wrk, INST, EXP, r0)
    sc = oi.propose_self_change("cand-opt-1", "opt-v1", r0, reg, EXP, RP, LIMITS, (),
                                r0.at)  # fmt: skip
    assert isinstance(sc, oi.SelfChange), sc
    os_.append(asr, INST, sc.candidate)
    return aro.Request(INST, EXP, "cand-opt-1", r0.cycle_id, "verdict-opt-db-1",
                       ASSESSOR, now(asr))  # fmt: skip


@pytest.fixture
def world(asr: Conn, wrk: Conn) -> aro.Request:
    return stored_world(asr, wrk)


@dataclass
class MemoryLog:
    """GATE-LIFTED SYNTHETIC: an in-memory `RunLog`, not durable (no installation
    run log exists yet)."""

    runs: dict[str, list[tuple[int, aro.Reservation]]] = field(default_factory=dict)
    done: dict[str, tuple[int, str]] = field(default_factory=dict)

    def attempts(self, plan_id: str) -> tuple[aro.Reservation, ...]:
        return tuple(r for _, r in self.runs.get(plan_id, []))

    def reserve(self, plan_id: str, attempt: int, r: aro.Reservation) -> None:
        self.runs.setdefault(plan_id, []).append((attempt, r))

    def outcome(self, plan_id: str) -> str | None:
        return self.done[plan_id][1] if plan_id in self.done else None

    def put_outcome(self, plan_id: str, attempt: int, digest_: str) -> None:
        self.done[plan_id] = (attempt, digest_)


@contextmanager
def gate_lifted() -> Iterator[None]:
    """GATE-LIFTED SYNTHETIC: T044's sandbox gate lifted for the optimizer target,
    inside this block of this test process only (T007 is not qualified)."""
    saved = im.SANDBOX_TARGETS
    im.SANDBOX_TARGETS = saved - {oi.OPT}
    try:
        yield
    finally:
        im.SANDBOX_TARGETS = saved


def lifted_assess(reg: im.ExperimentRegistry, eid: str, cid: str,
                  m: oi.Measured | None, assessor: im.Party, at: datetime,
                  vid: str) -> im.Verdict:  # fmt: skip
    """GATE-LIFTED SYNTHETIC: the production verdict, `oi.assess_measured`, with
    the sandbox gate lifted for this call."""
    with gate_lifted():
        return oi.assess_measured(reg, eid, cid, m, assessor, at, vid)


def inputs(seen: list[str] | None = None, **kw: Any) -> aro.InputsProvider:
    def get(r: aro.Request, p: im.Protocol, cycle: oi.CycleReceipt) -> aro.Inputs:
        if seen is not None:
            seen.append(cycle.cycle_id)
        return replace(aro.Inputs(PLAN, BASE, DEV, FIXTURE, LIMITS), **kw)

    return get


def lifted(asr: Conn, r: aro.Request, log: aro.RunLog,
           beat: Callable[[], object] = lambda: None,
           assess: aro.Assess = lifted_assess, **kw: Any) -> aro.Run:  # fmt: skip
    """GATE-LIFTED: `trial`'s two private phases with the test-only ports (`kw`:
    the inputs); `trial` itself takes none."""
    reg, p, c, cycle = aro._before_gate(asr, r, assess)
    return aro._after_gate(asr, r, beat, reg, p, c, cycle, inputs(**kw), log,
                           assess)  # fmt: skip


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(Exception) as e:  # any coded refusal
        fn()
    got = getattr(e.value, "code", None)
    assert isinstance(got, str), e.value
    return got


class Crash(Exception):
    pass


def crash_on(n: int) -> Callable[[], object]:
    calls = [0]

    def beat() -> None:
        calls[0] += 1
        if calls[0] == n:
            raise Crash("SYNTHETIC process death")

    return beat


def test_the_real_trial_is_blocked_before_any_read_and_records_nothing(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    before, log, seen = entries(conn), MemoryLog(), list[str]()
    assert code(lambda: aro.trial(asr, world)) == "sandbox_unqualified"
    # even with every port injected, the gate blocks before any input or read
    assert code(lambda: lifted(asr, world, log, assess=aro.measured_assess,
                               seen=seen)) == "sandbox_unqualified"  # fmt: skip
    assert (seen, log.runs, log.done, entries(conn)) == ([], {}, {}, before)
    assert aro.measured_assess is oi.assess_measured
    v = aro.measured_assess(os_.load(asr, INST, now(asr)), EXP, "cand-opt-1", None,
                            ASSESSOR, world.at, "v-x")  # fmt: skip
    assert (v.decision, v.reasons) == (im.Decision.BLOCKED, ("sandbox_unqualified",))
    # past the gate (GATE-LIFTED), the missing ports fail closed before any read
    reg, p, c, cycle = aro._before_gate(asr, world, lifted_assess)
    gated = partial(aro._after_gate, asr, world, lambda: None, reg, p, c, cycle)
    assert code(lambda: gated(None, log, lifted_assess)) == "trial_inputs_unavailable"
    assert code(lambda: gated(inputs(seen), None, lifted_assess)) == (
        "run_log_unavailable")  # fmt: skip

    def budget_blocked(*a: Any) -> im.Verdict:  # any other certain BLOCKED
        return replace(lifted_assess(*a), decision=im.Decision.BLOCKED,
                       reasons=("budget_not_reserved",))  # fmt: skip

    assert code(lambda: lifted(asr, world, log, assess=budget_blocked,
                               seen=seen)) == "blocked"  # fmt: skip
    assert (seen, log.runs, entries(conn)) == ([], {}, before)


def test_trial_takes_no_port_and_fails_closed_past_the_gate(
    asr: Conn, conn: Conn, world: aro.Request, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No caller can lift the gate through `trial`'s signature; GATE-LIFTED: even
    with the module's `t044_assess` replaced, no inputs or run log exist."""
    assert list(inspect.signature(aro.trial).parameters) == ["assessor", "r", "beat"]
    before = entries(conn)
    monkeypatch.setattr(aro, "measured_assess", lifted_assess)
    assert code(lambda: aro.trial(asr, world)) == "trial_inputs_unavailable"
    assert entries(conn) == before


def test_only_the_assessor_login_runs_a_trial(
    wrk: Conn, runtime_urls: dict[str, str], world: aro.Request
) -> None:
    assert code(lambda: aro.trial(wrk, world)) == "assessor_login"  # the producer
    for c in login(runtime_urls, "qw_release"):
        assert code(partial(lifted, c, world, MemoryLog())) == "assessor_login"


def test_self_certification_and_unknown_inputs_are_refused_before_the_gate(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    before, log = entries(conn), MemoryLog()
    cases = [
        (replace(world, assessor=PRODUCER), "self_certification"),
        (replace(world, assessor=replace(ASSESSOR, version="opt-v1")),
         "self_certification"),
        (replace(world, assessor=replace(ASSESSOR, actor_id="assessor-x")),
         "assessor_not_registered"),
        (replace(world, source_cycle_id="cycle-other"), "cycle_missing"),
        (replace(world, candidate_id="cand-x"), "unknown_candidate"),
        (replace(world, experiment_id="exp-x"), "unknown_experiment"),
    ]  # fmt: skip
    for r, want in cases:
        assert code(lambda r=r: lifted(asr, r, log)) == want, want  # type: ignore[misc]
        assert code(lambda r=r: aro.trial(asr, r)) == want, want  # type: ignore[misc]
    assert (log.runs, entries(conn)) == ({}, before)


def test_gate_lifted_trial_writes_its_verdict_and_reruns_reuse_the_read(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str], world: aro.Request
) -> None:
    """GATE-LIFTED SYNTHETIC: a crash after the reservation, then decision 42's
    re-run under the recorded read and inputs; the verdict is appended once, on
    the durable log (0049 refuses a deciding verdict without a run)."""
    log, before, plan = os_.RunLog(asr, INST), entries(conn), MP.plan_id
    with pytest.raises(Crash):
        lifted(asr, world, log, crash_on(1))  # after the reservation, before reading
    assert (len(log.attempts(plan)), log.outcome(plan), entries(conn)) == (
        1, None, before)  # fmt: skip
    # an input fault is refused before the read; a changed input is not a re-run
    assert code(lambda: lifted(asr, world, log, dev=FIXTURE)) == "not_development"
    later = replace(world, at=world.at + timedelta(seconds=1))
    assert code(lambda: lifted(asr, later, log)) == "rerun_mismatch"
    changed = replace(LIMITS, patience=LIMITS.patience + 1)
    assert code(lambda: lifted(asr, world, log, limits=changed)) == "rerun_mismatch"
    assert len(log.attempts(plan)) == 1
    run = lifted(asr, world, log)
    assert (run.attempt, run.wrote, run.verdict.decision) == (
        2, True, im.Decision.ACCEPT_FOR_REVIEW)  # fmt: skip
    assert run.metrics["promotion_gain"] >= Decimal(1)
    first, second = log.attempts(plan)
    assert first == second and log.outcome(plan) is not None  # by attempt 2 (run)
    assert entries(conn) == before + 1
    for c in login(runtime_urls, "qw_release"):
        got = os_.load(c, INST, now(c)).verdicts("cand-opt-1")
        assert [v.wire() for v in got] == [run.verdict.wire()]
    assert code(lambda: lifted(asr, world, log)) == "run_recorded"  # a redelivery
    other = replace(world, verdict_id="verdict-opt-db-2")
    assert code(lambda: lifted(asr, other, log)) == "rerun_mismatch"  # one read
    assert len(log.attempts(plan)) == 2 and entries(conn) == before + 1


def test_gate_lifted_reruns_are_capped_and_a_changed_outcome_conflicts(
    asr: Conn, conn: Conn, world: aro.Request
) -> None:
    """GATE-LIFTED SYNTHETIC: decision 59's three re-runs, decision 60's digest."""
    log, before, plan = MemoryLog(), entries(conn), MP.plan_id
    with pytest.raises(Crash):
        lifted(asr, world, log, crash_on(2))  # after the outcome, before the verdict
    attempt, digest_ = log.done[plan]
    log.done[plan] = (attempt, "0" * 64)  # a different recorded outcome
    assert code(lambda: lifted(asr, world, log)) == "verdict_conflict"
    log.done[plan] = (attempt, digest_)
    for _ in range(2):
        with pytest.raises(Crash):
            lifted(asr, world, log, crash_on(1))
    assert len(log.attempts(plan)) == aro.MAX_ATTEMPTS
    assert code(lambda: lifted(asr, world, log)) == "rerun_used"
    assert entries(conn) == before  # no verdict was ever appended


def test_the_request_round_trips_its_payload_and_refuses_malformed_ones() -> None:
    world = aro.Request(INST, EXP, "cand-opt-1", "cycle-v0-db", "verdict-1",
                        ASSESSOR, T0)  # fmt: skip
    w = world.to_payload()
    assert aro.Request.from_payload(w) == world
    bad: list[object] = [
        [], w | {"extra": "x"}, {k: v for k, v in w.items() if k != "at"},
        w | {"verdict_id": "bad id"}, w | {"assessor": ["a", "b", "c"]},
        w | {"assessor": "a"}, w | {"at": "2026-10-26T00:00:00"}, w | {"at": 1},
    ]  # fmt: skip
    for b in bad:
        assert code(partial(aro.Request.from_payload, b)) == "payload_invalid", b
