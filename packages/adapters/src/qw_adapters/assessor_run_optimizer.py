"""The assessor-run optimizer trial (T050; owner decisions 39 to 43, 59 to 62, 87
and 91): the strategy promotion's pattern (`assessor_run`) at installation scope.
`trial` runs in the qw-assessor process on its qw_assessor login only (decision
40; any other login is `assessor_login`), never as the producer:
- it loads the optimizer registry (protocol, candidate) and V0's stored cycle
  (`cycle_missing`) from the decision-87 stores, refuses an optimizer identity or
  an unregistered assessor (`check_assessor`: `self_certification`,
  `assessor_not_registered`), a verdict id already logged (`run_recorded` for this
  candidate, else `duplicate`);
- decision 62: `assess` with nothing measured; a certain BLOCKED is refused before
  any input is loaded or read is recorded (`sandbox_unqualified`, or `blocked`).
  The gate and the verdict are `oi.assess_measured` (`measured_assess`, fixed:
  `trial` takes no port), T044's sandbox gate over `measure`'s metrics: for the
  optimizer target it is `sandbox_unqualified` until T007 qualifies its sandbox,
  so a real trial records nothing;
- past that gate (`_after_gate`) it needs the inputs (`stored_inputs`: the plan's
  0051 promotion fixture, then V0's development inputs and the owner's limits,
  which no store holds: `dev_inputs_unstored`) and the run log, which `trial`
  passes durably (`optimizer_store.RunLog`, 0049). Without inputs it is
  `trial_inputs_unavailable`, without a log `run_log_unavailable`, before any
  read. With them:
  `check_measure` refuses every input fault, then decisions 42 and 59 reserve the
  read (attempt 1, then at most three re-runs under the same read and `inputs`
  digest: `rerun_mismatch`, `rerun_used`); `measure` opens the promotion fixture;
  decision 60 records the outcome digest once (`verdict_conflict` if a re-run
  differs); `assess` gives the verdict and qw_assessor appends it to the registry
  (decision 87).
`beat` (the job lease heartbeat) runs after each recorded phase.
LIMITATIONS: the post-gate path completes only in GATE-LIFTED tests (private
phases, a test-only `Development`); the request's `at` and assessor are the caller's,
checked against the registry; decisions 92 and 93 (only the bootstrap tenant's
job runs; tenants see one generic code) are the qw-assessor loop's, before this
module is called (`qw_worker.assessor`)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import astuple, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import research_improvement as ri
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
from qw_domain.research_data import check_id, digest
from qw_domain.trial_ledger import SplitPlan

from qw_adapters import optimizer_fixture_store as ofs
from qw_adapters import optimizer_store as os_
from qw_adapters.tenancy import Conn, TenancyError, check_runtime_role

MAX_ATTEMPTS = os_.MAX_ATTEMPTS  # the reservation and three re-runs (decision 59)
Reservation = os_.Reservation  # decision 42's write-ahead record of the plan's read
_KEYS = ("installation_id", "experiment_id", "candidate_id", "source_cycle_id",
         "verdict_id", "assessor", "at")  # fmt: skip


class TrialError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Request:
    """One optimizer trial (the job payload); a re-run repeats it exactly."""

    installation_id: str
    experiment_id: str
    candidate_id: str
    source_cycle_id: str  # V0's stored cycle that V1 was proposed from
    verdict_id: str
    assessor: im.Party
    at: datetime  # the read and the verdict

    @classmethod
    def from_payload(cls, w: object) -> Request:
        if not isinstance(w, dict) or sorted(w) != sorted(_KEYS):
            raise TrialError("payload_invalid", "exactly " + ", ".join(_KEYS))
        try:
            i, e, c, s, v = (check_id(w[k], k) for k in _KEYS[:5])
            a = w["assessor"]
            if not isinstance(a, list) or len(a) != 4:
                raise TypeError("assessor")
            return cls(i, e, c, s, v, im.Party(*a), parse_instant(w["at"]))
        except (ValueError, TypeError):
            raise TrialError("payload_invalid", "fields") from None

    def to_payload(self) -> dict[str, object]:
        return dict(zip(_KEYS, (self.installation_id, self.experiment_id,
                                self.candidate_id, self.source_cycle_id,
                                self.verdict_id, list(astuple(self.assessor)),
                                format_instant(self.at)), strict=True))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Inputs:
    plan: SplitPlan
    base: ri.ResearchConfig  # V0's research base (the stored cycle's base_hash)
    dev: ri.Fixture  # V0's development fixture (the stored cycle's fixture_hash)
    fixture: ri.Fixture  # the protected promotion fixture the plan binds
    limits: oi.Limits  # the owner's


# (request, protocol, V0's stored cycle) -> the inputs; none exists in production
InputsProvider = Callable[[Request, im.Protocol, oi.CycleReceipt], Inputs]
# V0's cycle -> its research base, dev fixture and limits (GATE-LIFTED tests only)
Development = Callable[[oi.CycleReceipt], tuple[ri.ResearchConfig, ri.Fixture,
                                                oi.Limits]]  # fmt: skip
# (registry, experiment, candidate, measurement or None, assessor, at, verdict id)
Assess = Callable[[im.ExperimentRegistry, str, str, oi.Measured | None, im.Party,
                   datetime, str], im.Verdict]  # fmt: skip


class RunLog(Protocol):
    """The installation-scope write-ahead store decisions 42, 59 and 60 need. An
    implementation must make each call durable and serialise calls per plan."""

    def attempts(self, plan_id: str) -> Sequence[Reservation]: ...
    def reserve(self, plan_id: str, attempt: int, r: Reservation) -> None: ...
    def outcome(self, plan_id: str) -> str | None: ...
    def put_outcome(self, plan_id: str, attempt: int, digest_: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Run:
    verdict: im.Verdict
    wrote: bool
    attempt: int
    metrics: Mapping[str, Decimal]


measured_assess: Assess = oi.assess_measured  # the gate and verdict; no port


def stored_inputs(conn: Conn, development: Development | None = None,
                  ) -> InputsProvider:  # fmt: skip
    """The plan's 0051 fixture, then V0's development inputs, which must hash to
    the stored cycle's (`dev_inputs_mismatch`); a missing one is never replaced."""

    def get(r: Request, p: im.Protocol, cycle: oi.CycleReceipt) -> Inputs:
        got = ofs.load(conn, r.installation_id, p.plan_id)
        if got is None:
            raise TrialError("trial_inputs_unavailable", f"no fixture for {p.plan_id}")
        if development is None:
            raise TrialError("dev_inputs_unstored", "no store holds them")
        base, dev, limits = development(cycle)
        if (base.content_hash, dev.content_hash) != (cycle.base_hash,
                                                     cycle.fixture_hash):  # fmt: skip
            raise TrialError("dev_inputs_mismatch", cycle.cycle_id)
        return Inputs(got[0], base, dev, got[1], limits)

    return get


def inputs_digest(r: Request, p: im.Protocol, c: im.Candidate, cycle: oi.CycleReceipt,
                  i: Inputs) -> str:  # fmt: skip
    lim = i.limits
    return digest([
        "optimizer_trial_inputs", r.installation_id, p.content_hash, c.content_hash,
        cycle.content_hash, i.base.content_hash, i.dev.content_hash,
        i.fixture.content_hash, im.plan_digest(i.plan),
        [lim.max_depth, lim.patience, lim.monthly_cap.to_wire(),
         lim.cost_per_proposal.to_wire()],
        list(astuple(r.assessor)), r.verdict_id, format_instant(r.at),
    ])  # fmt: skip


def reserve(log: RunLog, plan_id: str, mine: Reservation) -> int:
    """Decisions 42 and 59: attempt 1 records the read; each re-run repeats it."""
    runs = list(log.attempts(plan_id))
    if len(runs) >= MAX_ATTEMPTS:
        raise TrialError("rerun_used", plan_id)
    if runs and runs[0] != mine:
        raise TrialError("rerun_mismatch", plan_id)
    log.reserve(plan_id, len(runs) + 1, mine)
    return len(runs) + 1


def record_outcome(log: RunLog, plan_id: str, attempt: int, d: str) -> None:
    """Decision 60: the first outcome digest is kept; a different one conflicts."""
    got = log.outcome(plan_id)
    if got is None:
        log.put_outcome(plan_id, attempt, d)
    elif got != d:
        raise TrialError("verdict_conflict", "the trial outcome differs")


def _now(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def trial(assessor: Conn, r: Request, beat: Callable[[], object] = lambda: None,
          ) -> Run:  # fmt: skip
    """Run `r` as the independent assessor. No port can be passed: the gate is the
    module's `measured_assess`, the run log is the durable one, and the inputs are
    the stores' (`stored_inputs`), so past the gate it fails closed."""
    reg, p, c, cycle = _before_gate(assessor, r, measured_assess)
    log = os_.RunLog(assessor, r.installation_id)
    return _after_gate(assessor, r, beat, reg, p, c, cycle, stored_inputs(assessor),
                       log, measured_assess)  # fmt: skip


def _before_gate(assessor: Conn, r: Request, assess: Assess) -> tuple[
    im.ExperimentRegistry, im.Protocol, im.Candidate, oi.CycleReceipt
]:  # fmt: skip
    try:
        check_runtime_role(assessor, "qw_assessor")
    except TenancyError as exc:
        raise TrialError("assessor_login", str(exc)) from None
    inst, eid, cid = r.installation_id, r.experiment_id, r.candidate_id
    reg = os_.load(assessor, inst, _now(assessor))
    p, c = reg.protocol(eid), reg.candidate(cid)
    oi.check_assessor(p, c, r.assessor)
    for e in reg.entries:
        if type(e) is im.Verdict and e.verdict_id == r.verdict_id:
            raise TrialError("run_recorded" if e.candidate_id == cid else "duplicate",
                             r.verdict_id)  # fmt: skip
    cycles = [s.receipt for s in os_.load_cycles(assessor, inst, eid)
              if s.receipt.cycle_id == r.source_cycle_id]  # fmt: skip
    if not cycles:
        raise TrialError("cycle_missing", r.source_cycle_id)
    dry = assess(reg, eid, cid, None, r.assessor, r.at, r.verdict_id)
    if dry.decision is im.Decision.BLOCKED:  # decision 62: certain before the read
        code = "sandbox_unqualified" if "sandbox_unqualified" in dry.reasons else (
            "blocked")  # fmt: skip
        raise TrialError(code, ",".join(dry.reasons))
    return reg, p, c, cycles[0]


def _after_gate(assessor: Conn, r: Request, beat: Callable[[], object],
                reg: im.ExperimentRegistry, p: im.Protocol, c: im.Candidate,
                cycle: oi.CycleReceipt, inputs: InputsProvider | None,
                log: RunLog | None, assess: Assess) -> Run:  # fmt: skip
    """Only GATE-LIFTED tests reach this with inputs."""
    if inputs is None:
        raise TrialError("trial_inputs_unavailable", "no protected fixture store")
    if log is None:
        raise TrialError("run_log_unavailable", "no installation run log")
    i = inputs(r, p, cycle)
    args = (p, i.plan, c, cycle, i.base, i.dev, i.fixture, i.limits, r.assessor)
    oi.check_measure(*args)
    mine = Reservation(r.experiment_id, r.candidate_id, r.verdict_id,
                       i.fixture.content_hash,
                       inputs_digest(r, p, c, cycle, i))  # fmt: skip
    attempt = reserve(log, p.plan_id, mine)
    beat()
    metrics = oi.measure(*args)  # the read: only now is the fixture scored
    done = digest(
        ["optimizer_trial_outcome", sorted([k, str(v)] for k, v in metrics.items())]
    )
    record_outcome(log, p.plan_id, attempt, done)
    beat()
    v = assess(reg, p.experiment_id, c.candidate_id, oi.Measured(i.plan, metrics),
               r.assessor, r.at, r.verdict_id)  # fmt: skip
    _, wrote = os_.append(assessor, r.installation_id, v)
    return Run(v, wrote, attempt, metrics)
