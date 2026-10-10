"""Prospective shadow capture and scoring (T033 part C). Spec §14 ("Prospective
capture uses frozen ... versions and timestamps decisions before outcomes"; actual,
shadow-policy and recommendation dashboards stay separate; no simulation operation
calls a broker), §17 ("Prospective decisions must be frozen before outcomes"), §8
(prospective observations; evidence enum `prospective_limited`; no single badge).
- `open_run` binds a run to a version registered in the strategy registry, its
  configuration, and a trial recorded in the T032 ledger for that candidate under
  the protocol frozen with the plan (`candidate_mismatch`, `protocol_changed`). The
  run starts no earlier than the trial and the protocol (`not_frozen`) and declares
  the bar grid, a capture delay below the bar spacing and the minimum number of
  scored slots for a claim. `declaration_hash` covers all of it; `score` refuses a
  run whose hash differs from the one recorded at registration.
- A `ShadowDecision` is a target weight or an abstention (None), taken at
  `decided_at` from inputs known by then (`look_ahead`; `inputs_known_at` is
  caller-declared, so the guard is cooperative), captured within the run's delay
  (`backfill`) and in time order. The book is append-only: an identical re-capture
  is a no-op, a different record under the same id is refused
  (`conflicting_duplicate`), so a retry never resets its timestamps. Nothing here
  places, routes or simulates an order.
- `score` reads bars known by `as_of` on the declared grid. A decision belongs to
  the last grid bar whose window `end` is at or before `decided_at` (the window
  end, not the bar's `known_at`: a decision taken before a late bar arrived still
  belongs to that slot). It fills, as in `backtest`, at the open of the bar
  `fill_lag_bars` later; it is scored only if both decided and captured before that
  bar starts (else `late_decisions` / `late_captured`), and only such decisions can
  supersede one another. A slot is due once its fill bar has started by `as_of`;
  a decision for a slot not yet due is pending. Returns, turnover and costs use the
  backtest formulas and the protocol's `CostModel`. A period whose end bar has not
  ended by `as_of` is `pending_outcome`; an ended but missing or unknown bar makes
  it `unavailable`; never zero. The first slot starts flat; after any unscored slot
  the held weight is unknown, so turnover is charged at the worst case
  max(w, 1 - w).
- Buy-and-hold baselines are held over every due slot with their own bars and
  compared on the common scored slots; cash earns its declared rate. The report
  carries every count, `eval_stats` intervals and the declaration and grid hashes,
  and never blends backtest figures. Synthetic bars cap evidence at `limited`,
  prospective bars at `prospective_limited`; below the declared minimum the
  ceiling is `none` and no claim is permitted. Significance is never claimed.
LIMITATIONS: no persistence, so the registration time inside the declaration is
not independently verified; no trial-ledger entry for the shadow run itself;
capture and as-of times must come from the server clock (caller-supplied here);
one instrument per run. Stdlib only.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from qw_domain.backtest import (
    BaselineKind,
    EvaluationProtocol,
    Period,
    PriceBar,
    PriceSeries,
)
from qw_domain.decimal_math import CTX, div
from qw_domain.eval_stats import Summary, summarize
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    EvidenceClass,
    ResearchError,
    Window,
    check_id,
    digest,
)
from qw_domain.strategy_registry import EvidenceState, StrategyRegistry
from qw_domain.trial_ledger import Candidate, TrialLedger


@dataclass(frozen=True, slots=True)
class ShadowRun:
    run_id: str
    tenant_id: str
    strategy_id: str
    version: str
    candidate: Candidate
    trial_id: str
    protocol: EvaluationProtocol
    instrument: InstrumentId
    min_scored: int  # declared before outcomes; below it no claim is permitted
    max_capture_delay: timedelta
    grid: tuple[Window, ...]  # the declared bar grid, frozen with the run
    registered_at: datetime

    @property
    def protocol_hash(self) -> str:
        return self.protocol.content_hash

    @property
    def grid_hash(self) -> str:
        return digest([w.to_wire() for w in self.grid])

    @property
    def declaration_hash(self) -> str:
        """Everything fixed at registration; `score` needs the recorded value."""
        c = self.candidate
        return digest({
            "run_id": self.run_id, "tenant_id": self.tenant_id,
            "strategy": [self.strategy_id, self.version, c.family, c.material_hash,
                         c.config_hash],
            "trial_id": self.trial_id, "protocol_hash": self.protocol_hash,
            "instrument": self.instrument.to_wire(), "min_scored": self.min_scored,
            "max_capture_delay_s": str(self.max_capture_delay.total_seconds()),
            "grid_hash": self.grid_hash,
            "registered_at": format_instant(self.registered_at),
        })  # fmt: skip


def open_run(
    registry: StrategyRegistry, ledger: TrialLedger, run_id: str, strategy_id: str,
    version: str, config: Mapping[str, object], trial_id: str,
    protocol: EvaluationProtocol, instrument: InstrumentId, min_scored: int,
    max_capture_delay: timedelta, grid: Sequence[Window], registered_at: datetime,
) -> ShadowRun:  # fmt: skip
    check_id(run_id, "run")
    m = registry.manifest(strategy_id, version)
    found = [t for t in ledger.trials() if t.trial_id == trial_id]
    if not found:
        raise ResearchError("unknown_trial", trial_id)
    t = found[0]
    cand = Candidate(t.family, m.material_hash, m.config_hash(config))
    if (t.material_hash, t.config_hash) != (cand.material_hash, cand.config_hash):
        raise ResearchError("candidate_mismatch", trial_id)
    if type(protocol) is not EvaluationProtocol or (
        protocol.content_hash != t.protocol_hash
    ):
        raise ResearchError("protocol_changed", "not the protocol of the trial")
    registered_at = ensure_aware_utc(registered_at)
    if registered_at < max(t.recorded_at, protocol.declared_at):
        raise ResearchError("not_frozen", "the run starts after trial and protocol")
    if type(min_scored) is not int or min_scored < 1:
        raise ResearchError("min_scored", "a positive int")
    grid = tuple(grid)
    if (
        len(grid) < protocol.fill_lag_bars + 2
        or not all(type(w) is Window for w in grid)
        or any(a.end > b.start for a, b in pairwise(grid))
    ):
        raise ResearchError("grid", "chronological, non-overlapping Windows")
    spacing = min(b.start - a.start for a, b in pairwise(grid))
    d = max_capture_delay
    if type(d) is not timedelta or not timedelta(0) <= d < spacing:
        raise ResearchError("max_capture_delay", "non-negative, below the bar spacing")
    if type(instrument) is not InstrumentId:
        raise ResearchError("instrument", "an InstrumentId is required")
    return ShadowRun(run_id, ledger.tenant_id, strategy_id, version, cand, trial_id,
                     protocol, instrument, min_scored, d, grid,
                     registered_at)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    decision_id: str
    run_id: str
    protocol_hash: str
    decided_at: datetime
    inputs_known_at: datetime
    captured_at: datetime
    target_weight: Decimal | None  # None: an explicit abstention

    def __post_init__(self) -> None:
        check_id(self.decision_id, "decision")
        for name in ("decided_at", "inputs_known_at", "captured_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))
        w = self.target_weight
        if w is not None and (type(w) is not Decimal or not w.is_finite()
                              or not 0 <= w <= 1):  # fmt: skip
            raise ResearchError("weight", "a Decimal in [0, 1] or None")


@dataclass(slots=True)
class _State:
    by_id: dict[str, ShadowDecision] = field(default_factory=dict)
    last: datetime | None = None


def _apply(s: _State, r: ShadowRun, d: ShadowDecision) -> None:
    if type(d) is not ShadowDecision:
        raise ResearchError("entry", type(d).__name__)
    if d.decision_id in s.by_id:
        raise ResearchError("duplicate", d.decision_id)
    if d.run_id != r.run_id:
        raise ResearchError("run_mismatch", d.decision_id)
    if d.protocol_hash != r.protocol_hash:
        raise ResearchError("protocol_changed", d.decision_id)
    if d.inputs_known_at > d.decided_at:
        raise ResearchError("look_ahead", f"{d.decision_id} uses later inputs")
    if d.decided_at < r.registered_at:
        raise ResearchError("before_registration", d.decision_id)
    if not d.decided_at <= d.captured_at <= d.decided_at + r.max_capture_delay:
        raise ResearchError("backfill", f"{d.decision_id} not captured when made")
    if s.last is not None and d.captured_at < s.last:
        raise ResearchError("time_order", "decisions are captured in time order")
    s.by_id[d.decision_id] = d
    s.last = d.captured_at


@dataclass(frozen=True, slots=True)
class ShadowBook:
    """Append-only per run; construction replays every entry."""

    run: ShadowRun
    entries: tuple[ShadowDecision, ...] = ()
    _state: _State = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.run) is not ShadowRun:
            raise ResearchError("run", "a ShadowRun is required")
        object.__setattr__(self, "entries", tuple(self.entries))
        state = _State()
        for d in self.entries:
            _apply(state, self.run, d)
        object.__setattr__(self, "_state", state)

    def capture(self, d: ShadowDecision) -> "ShadowBook":
        old = self._state.by_id.get(getattr(d, "decision_id", ""))
        if old is not None:
            if old == d:
                return self  # identical replay
            raise ResearchError("conflicting_duplicate", old.decision_id)
        return ShadowBook(self.run, (*self.entries, d))


class SlotStatus(StrEnum):
    SCORED = "scored"
    ABSTAINED = "abstained"
    MISSED = "missed"  # no decision captured before the fill bar opened
    UNAVAILABLE = "unavailable"  # decided; an outcome bar has ended but is missing
    PENDING_OUTCOME = "pending_outcome"  # the period-end bar has not ended yet


@dataclass(frozen=True, slots=True)
class Slot:
    window: Window  # the grid bar after which the decision is taken
    status: SlotStatus
    decision_id: str | None
    period: Period | None


@dataclass(frozen=True, slots=True)
class BaselineExcess:
    baseline_id: str
    excess: Summary | None  # strategy net minus baseline net on common slots
    common: int


@dataclass(frozen=True, slots=True)
class ShadowReport:
    run: ShadowRun
    as_of: datetime
    series_hash: str
    evidence_class: EvidenceClass
    slots: tuple[Slot, ...]
    counts: Mapping[str, int]
    summary: Summary | None
    baselines: tuple[BaselineExcess, ...]
    evidence_ceiling: EvidenceState
    claim_permitted: bool
    limitations: tuple[str, ...]
    significance_claimed: bool = False

    def to_wire(self) -> dict[str, object]:
        r, s = self.run, self.summary

        def ci(x: Summary | None) -> object:
            i = None if x is None else x.interval
            return None if i is None else i.to_wire()

        return {
            "record_kind": "prospective_shadow", "run_id": r.run_id,
            "strategy_id": r.strategy_id, "version": r.version,
            "material_hash": r.candidate.material_hash,
            "config_hash": r.candidate.config_hash, "trial_id": r.trial_id,
            "protocol_hash": r.protocol_hash, "series_hash": self.series_hash,
            "declaration_hash": r.declaration_hash, "grid_hash": r.grid_hash,
            "instrument": r.instrument.to_wire(), "as_of": format_instant(self.as_of),
            "costs": r.protocol.costs.to_wire(),
            "fill_lag_bars": r.protocol.fill_lag_bars, "counts": dict(self.counts),
            "net_mean": None if s is None else str(s.mean), "interval": ci(s),
            "baselines": [{"baseline_id": b.baseline_id, "common_slots": b.common,
                           "excess_mean": None if b.excess is None
                           else str(b.excess.mean), "excess_interval": ci(b.excess)}
                          for b in self.baselines],
            "min_scored": r.min_scored, "claim_permitted": self.claim_permitted,
            "significance_claimed": self.significance_claimed,
            "evidence_class": self.evidence_class.value,
            "evidence_ceiling": self.evidence_ceiling.value,
            "limitations": list(self.limitations),
        }  # fmt: skip


def _on_grid(
    s: PriceSeries, r: ShadowRun, grid: Sequence[Window], as_of: datetime,
    instrument: InstrumentId | None,
) -> dict[int, PriceBar]:  # fmt: skip
    """Grid index -> bar known by `as_of`; `instrument` None for a baseline."""
    if (
        type(s) is not PriceSeries
        or s.tenant_id != r.tenant_id
        or (instrument is not None and s.instrument != instrument)
    ):
        raise ResearchError("series", "a PriceSeries of the run's tenant/instrument")
    if s.evidence_class is EvidenceClass.HISTORICAL:
        raise ResearchError("not_prospective", s.series_id)
    index = {(w.start, w.end): k for k, w in enumerate(grid)}
    out: dict[int, PriceBar] = {}
    for b in s.bars:
        if (k := index.get((b.start, b.end))) is None:
            raise ResearchError("off_grid", format_instant(b.start))
        if b.known_at <= as_of:
            out[k] = b
    return out


def _chain(
    weights: list[Decimal | None], fills: list[tuple[PriceBar, PriceBar] | None],
    rate: Decimal,
) -> tuple[list[Period | None], bool]:  # fmt: skip
    """Backtest period formulas; a None weight or fill breaks the chain."""
    drift: Decimal | None = Decimal(0)  # the run starts flat
    out: list[Period | None] = []
    worst = False
    for w, f in zip(weights, fills, strict=True):
        if w is None or f is None:
            out.append(None)
            drift = None
            continue
        here, nxt = f
        r = CTX.subtract(div(nxt.open, here.open), 1)
        gross = CTX.multiply(w, r)
        if drift is None:  # held weight unknown after a gap: worst case
            turnover, worst = max(w, CTX.subtract(1, w)), True
        else:
            turnover = abs(CTX.subtract(w, drift))
        cost = CTX.multiply(turnover, rate)
        net = CTX.subtract(gross, cost)
        if net <= -1:
            raise ResearchError("total_loss", "a period lost the whole capital")
        drift = div(CTX.multiply(w, CTX.add(1, r)), CTX.add(1, gross))
        out.append(Period(here.start, nxt.start, w, turnover, cost, gross, net))
    return out, worst


def score(
    book: ShadowBook, series: PriceSeries, as_of: datetime,
    baseline_series: Mapping[str, PriceSeries], declared_hash: str,
) -> ShadowReport:  # fmt: skip
    """Score the book as known at `as_of` from bars known by then. `declared_hash`
    is the run's declaration hash as recorded at registration."""
    r, pr = book.run, book.run.protocol
    if r.declaration_hash != declared_hash:
        raise ResearchError("declaration_mismatch", r.run_id)
    as_of, grid, lag = ensure_aware_utc(as_of), r.grid, pr.fill_lag_bars
    bars = _on_grid(series, r, grid, as_of, r.instrument)
    # a slot is due once its decision window has closed: the fill bar has started
    due = [k for k, w in enumerate(grid[: len(grid) - lag])
           if r.registered_at <= w.end and grid[k + lag].start <= as_of]  # fmt: skip
    names = ("due", "scored", "abstained", "missed", "unavailable", "pending_outcome",
             "late_decisions", "late_captured", "superseded", "pending",
             "unscheduled")  # fmt: skip
    counts = dict.fromkeys(names, 0)
    counts["due"] = len(due)
    chosen: dict[int, ShadowDecision] = {}
    for d in book.entries:
        if d.captured_at > as_of:
            continue
        ended = [k for k, w in enumerate(grid) if w.end <= d.decided_at]
        if not ended or grid[ended[-1]].end < r.registered_at:
            counts["unscheduled"] += 1
            continue
        if (k := ended[-1]) not in due:
            counts["pending"] += 1  # its slot's window is still open (or off the grid)
            continue
        fill = grid[k + lag].start
        if d.decided_at >= fill:
            counts["late_decisions"] += 1
            continue
        if d.captured_at >= fill:  # recorded once the fill price could be known
            counts["late_captured"] += 1
            continue
        if k in chosen:
            counts["superseded"] += 1  # the latest decision before the fill applies
        if k not in chosen or chosen[k].decided_at <= d.decided_at:
            chosen[k] = d

    def fills(px: Mapping[int, PriceBar]) -> list[tuple[PriceBar, PriceBar] | None]:
        pairs = [(px.get(k + lag), px.get(k + lag + 1)) for k in due]
        return [(a, b) if a is not None and b is not None else None for a, b in pairs]

    def ended_by_as_of(k: int) -> bool:
        return k + lag + 1 < len(grid) and grid[k + lag + 1].end <= as_of

    weights = [chosen[k].target_weight if k in chosen else None for k in due]
    periods, worst = _chain(weights, fills(bars), pr.costs.rate)
    slots = []
    for k, w, p in zip(due, weights, periods, strict=True):
        pick = chosen.get(k)
        status = (SlotStatus.MISSED if pick is None else SlotStatus.ABSTAINED
                  if w is None else SlotStatus.SCORED if p is not None
                  else SlotStatus.UNAVAILABLE if ended_by_as_of(k)
                  else SlotStatus.PENDING_OUTCOME)  # fmt: skip
        counts[status.value] += 1
        slots.append(Slot(grid[k], status, pick.decision_id if pick else None, p))
    nets = [p.net for p in periods if p is not None]
    lags, z = pr.hac_lags, pr.z
    results = []
    for b in pr.baselines:
        if b.kind is BaselineKind.CASH:
            base: list[Decimal | None] = [b.cash_rate] * len(due)
        else:  # held over every due slot with its own bars, then compared
            other = baseline_series.get(b.series_id or "")
            if other is None:
                raise ResearchError("baseline_missing", b.baseline_id)
            if other.evidence_class is not series.evidence_class:
                raise ResearchError("evidence_class_mismatch", b.baseline_id)
            px = _on_grid(other, r, grid, as_of, None)
            ones: list[Decimal | None] = [Decimal(1)] * len(due)
            held, gap = _chain(ones, fills(px), pr.costs.rate)
            worst = worst or gap
            base = [None if q is None else q.net for q in held]
        ex = [CTX.subtract(p.net, q) for p, q in zip(periods, base, strict=True)
              if p is not None and q is not None]  # fmt: skip
        excess = summarize(ex, lags, z) if ex else None
        results.append(BaselineExcess(b.baseline_id, excess, len(ex)))
    summary = summarize(nets, lags, z) if nets else None
    limits = ["prospective_shadow_not_backtest", "no_multiple_testing_adjustment"]
    limits += [] if summary is None else list(summary.limitations)
    if worst:
        limits.append("gap_turnover_worst_case")
    permitted = counts["scored"] >= r.min_scored
    if not permitted:
        ceiling = EvidenceState.NONE
        limits.insert(0, "below_declared_minimum")
    elif series.evidence_class is EvidenceClass.SYNTHETIC:
        ceiling = EvidenceState.LIMITED
    else:
        ceiling = EvidenceState.PROSPECTIVE_LIMITED
    if series.evidence_class is EvidenceClass.SYNTHETIC:
        limits.insert(0, "synthetic_only")
    return ShadowReport(r, as_of, series.content_hash, series.evidence_class,
                        tuple(slots), MappingProxyType(counts), summary,
                        tuple(results), ceiling, permitted, tuple(limits))  # fmt: skip
