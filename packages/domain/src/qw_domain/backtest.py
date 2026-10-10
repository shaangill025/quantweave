"""Chronological walk-forward evaluation over a frozen dataset with explicit costs,
frozen baselines, honest trial reports and scoped claims (T033 increment 1).
Spec §14 (event-driven contract: "A signal created from a finalized bar cannot fill
before modeled submission"; metrics, baselines "selected before the evaluation
period"; register all trials), §8 (no single validated badge; synthetic-only is
limited), §17, R040, R059, R065, R084.
- Evaluation runs through a granted train/development access of the T032 trial
  ledger (`open_view` re-checks rights and the dataset); promotion accesses are
  refused here (the protected assessor protocol is separate). The protocol (structured
  claim, costs, fill lag, HAC lags, z, baselines) must hash to the plan's registered
  `protocol_hash` (`protocol_changed`); every trial records that hash.
- At each decision time (a bar's knowledge time) the strategy sees a `PitContext`:
  bars known by then and facts/membership through the split view. Asking for a later
  time, or returning a decision based on later inputs, is a `look_ahead` refusal.
  The guard is cooperative: a strategy closure holding other data is not detected.
- The decision at bar i fills at the open of bar i + lag (lag >= 1), which must start
  strictly after the decision; otherwise `same_bar_fill`. Period j runs open_j to
  open_{j+1}: gross = w * R, cost = |w - drifted previous weight| * (commission +
  half spread + slippage, total < 1), net = gross - cost; net <= -1 is refused.
  Weights are long-only in [0, 1]; no liquidation cost is charged at the window end.
- Baselines (buy-and-hold of a declared series with the same costs, or a declared
  per-period cash rate) are compared period by period with an excess-return interval.
- Claims are structured (metric, direction, baseline) and carry instrument, dataset,
  window, costs, interval and the ledger's trial count. No multiple-testing
  adjustment is named by the spec, so significance is never claimed; train/development
  evidence is capped at `limited`.
LIMITATIONS: one instrument per evaluation; price series are caller-supplied, not
yet part of the hashed dataset or its rights gate; no shorting, borrow, partial
fills, volume limits or corporate actions; times are caller-supplied.
Stdlib only.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from qw_domain.decimal_math import CTX, PRECISION, div
from qw_domain.eval_stats import HacInterval, StatsError, Summary, mean, summarize
from qw_domain.filings import AsOfView
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    EvidenceClass,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    Window,
    check_id,
    digest,
)
from qw_domain.strategy_registry import EvidenceState
from qw_domain.trial_ledger import (
    Access,
    Outcome,
    Split,
    SplitView,
    Trial,
    TrialLedger,
    open_view,
)


def _dec(value: object, what: str, *, positive: bool = False) -> Decimal:
    ok = type(value) is Decimal and value.is_finite()
    if not ok or value < 0 or (positive and value == 0):  # type: ignore[operator]
        raise ResearchError(what, "a finite non-negative Decimal is required")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PriceBar:
    start: datetime
    end: datetime
    known_at: datetime
    open: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        for name in ("start", "end", "known_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))
        if not self.start < self.end <= self.known_at:
            raise ResearchError("bar", "start < end <= known_at")
        _dec(self.open, "open", positive=True)
        _dec(self.close, "close", positive=True)

    def to_wire(self) -> list[str]:
        times = [format_instant(t) for t in (self.start, self.end, self.known_at)]
        return [*times, str(self.open), str(self.close)]


@dataclass(frozen=True, slots=True)
class PriceSeries:
    series_id: str
    tenant_id: str
    instrument: InstrumentId
    evidence_class: EvidenceClass
    bars: tuple[PriceBar, ...]

    def __post_init__(self) -> None:
        check_id(self.series_id, "series")
        check_id(self.tenant_id, "tenant")
        object.__setattr__(self, "bars", tuple(self.bars))
        if type(self.instrument) is not InstrumentId:
            raise ResearchError("instrument", "an InstrumentId is required")
        if not isinstance(self.evidence_class, EvidenceClass):
            raise ResearchError("evidence_class", "an EvidenceClass is required")
        if not all(type(b) is PriceBar for b in self.bars):
            raise ResearchError("bar", "PriceBar records only")
        if any(a.end > b.start for a, b in zip(self.bars, self.bars[1:], strict=False)):
            raise ResearchError("bar_order", "bars are chronological, non-overlapping")

    @property
    def content_hash(self) -> str:
        return digest({"series_id": self.series_id, "tenant_id": self.tenant_id,
                       "instrument": self.instrument.to_wire(),
                       "evidence_class": self.evidence_class.value,
                       "bars": [b.to_wire() for b in self.bars]})  # fmt: skip


@dataclass(frozen=True, slots=True)
class CostModel:
    """Fractions of traded notional, charged on every unit of turnover."""

    commission_rate: Decimal
    half_spread: Decimal
    slippage_rate: Decimal

    def __post_init__(self) -> None:
        for name in ("commission_rate", "half_spread", "slippage_rate"):
            _dec(getattr(self, name), name)
        if self.rate >= 1:  # keeps the cost of one unit of turnover below 100%
            raise ResearchError("cost", "total cost rate must lie in [0, 1)")

    @property
    def rate(self) -> Decimal:
        return CTX.add(CTX.add(self.commission_rate, self.half_spread),
                       self.slippage_rate)  # fmt: skip

    def to_wire(self) -> dict[str, str]:
        return {"commission_rate": str(self.commission_rate),
                "half_spread": str(self.half_spread),
                "slippage_rate": str(self.slippage_rate)}  # fmt: skip


class BaselineKind(StrEnum):
    BUY_AND_HOLD = "buy_and_hold"
    CASH = "cash"


@dataclass(frozen=True, slots=True)
class Baseline:
    baseline_id: str
    kind: BaselineKind
    series_id: str | None  # the held series for buy-and-hold
    cash_rate: Decimal  # per period, for cash

    def __post_init__(self) -> None:
        check_id(self.baseline_id, "baseline")
        if not isinstance(self.kind, BaselineKind):
            raise ResearchError("baseline", "a BaselineKind is required")
        if (self.kind is BaselineKind.BUY_AND_HOLD) != (self.series_id is not None):
            raise ResearchError("baseline", "buy-and-hold names exactly one series")
        if self.series_id is not None:
            check_id(self.series_id, "series")
        if type(self.cash_rate) is not Decimal or not self.cash_rate.is_finite():
            raise ResearchError("cash_rate", "a finite Decimal is required")


class ClaimMetric(StrEnum):
    NET_MEAN = "net_mean_return"
    EXCESS_MEAN = "excess_mean_return"  # over the named baseline


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


@dataclass(frozen=True, slots=True)
class DeclaredClaim:
    """Structured, never free text; the evaluation adds instrument, window, costs."""

    metric: ClaimMetric
    direction: Direction
    baseline_id: str | None

    def to_wire(self) -> dict[str, str | None]:
        return {"metric": self.metric.value, "direction": self.direction.value,
                "baseline_id": self.baseline_id}  # fmt: skip


@dataclass(frozen=True, slots=True)
class EvaluationProtocol:
    protocol_id: str
    plan_id: str
    claim: DeclaredClaim
    costs: CostModel
    fill_lag_bars: int
    hac_lags: int
    z: Decimal
    baselines: tuple[Baseline, ...]
    declared_at: datetime
    approved_by: str

    def __post_init__(self) -> None:
        for name in ("protocol_id", "plan_id", "approved_by"):
            check_id(getattr(self, name), name)
        if type(self.costs) is not CostModel:
            raise ResearchError("costs", "an explicit CostModel is required")
        if type(self.fill_lag_bars) is not int or self.fill_lag_bars < 1:
            raise ResearchError("same_bar_fill", "fills need a lag of at least 1 bar")
        if type(self.hac_lags) is not int or self.hac_lags < 0:
            raise ResearchError("hac_lags", "a non-negative int")
        _dec(self.z, "z", positive=True)
        object.__setattr__(self, "baselines", tuple(self.baselines))
        ids = [b.baseline_id for b in self.baselines if type(b) is Baseline]
        if not ids or len(set(ids)) != len(self.baselines):
            raise ResearchError("baselines", "distinct declared baselines required")
        object.__setattr__(self, "declared_at", ensure_aware_utc(self.declared_at))
        c = self.claim
        if (
            type(c) is not DeclaredClaim
            or not isinstance(c.metric, ClaimMetric)
            or not isinstance(c.direction, Direction)
            or (c.metric is ClaimMetric.EXCESS_MEAN) != (c.baseline_id in ids)
        ):
            raise ResearchError("claim", "a structured claim on a declared baseline")

    @property
    def content_hash(self) -> str:
        return digest({
            "protocol_id": self.protocol_id, "plan_id": self.plan_id,
            "claim": self.claim.to_wire(), "costs": self.costs.to_wire(),
            "fill_lag_bars": self.fill_lag_bars, "hac_lags": self.hac_lags,
            "z": str(self.z), "declared_at": format_instant(self.declared_at),
            "approved_by": self.approved_by,
            "baselines": [[b.baseline_id, b.kind.value, b.series_id, str(b.cash_rate)]
                          for b in self.baselines],
        })  # fmt: skip


@dataclass(frozen=True, slots=True)
class Decision:
    target_weight: Decimal
    inputs_known_at: datetime


@dataclass(frozen=True, slots=True)
class PitContext:
    """What a strategy may see at decision time `now`."""

    now: datetime
    view: SplitView
    visible: tuple[PriceBar, ...]

    def _until_now(self, t: datetime) -> datetime:
        t = ensure_aware_utc(t)
        if t > self.now:
            raise ResearchError("look_ahead", f"{format_instant(t)} is after now")
        return t

    def bars(self) -> tuple[PriceBar, ...]:
        return self.visible

    def facts_at(self, t: datetime) -> AsOfView:
        return self.view.facts_at(self._until_now(t))

    def members(self, on: date) -> frozenset[InstrumentId]:
        if on > self.now.date():
            raise ResearchError("look_ahead", "membership on a later date")
        return self.view.members(on, self.now)


type Strategy = Callable[[PitContext], Decision]


@dataclass(frozen=True, slots=True)
class Period:
    start: datetime
    end: datetime
    weight: Decimal
    turnover: Decimal
    cost: Decimal
    gross: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class BaselineResult:
    baseline: Baseline
    periods: tuple[Period, ...]
    summary: Summary
    excess: Summary  # strategy net minus baseline net, per period


@dataclass(frozen=True, slots=True)
class Evaluation:
    access: Access
    dataset_hash: str
    evidence_class: EvidenceClass
    protocol: EvaluationProtocol
    series_hash: str
    baseline_hashes: tuple[str, ...]
    instrument: InstrumentId
    periods: tuple[Period, ...]
    summary: Summary
    gross_mean: Decimal
    total_cost: Decimal
    baselines: tuple[BaselineResult, ...]


def _usable(s: PriceSeries, view: SplitView, ds: ResearchDataset) -> list[PriceBar]:
    if type(s) is not PriceSeries:
        raise ResearchError("series", "a PriceSeries is required")
    if s.tenant_id != ds.manifest.tenant_id:
        raise ResearchError("tenant_mismatch", s.series_id)
    if s.evidence_class is not ds.manifest.evidence_class:
        raise ResearchError("evidence_class_mismatch", s.series_id)
    a = view.access
    if a.instruments is not None and s.instrument not in a.instruments:
        raise ResearchError("instrument_not_granted", s.series_id)
    if s.instrument in view.plan.holdout_instruments:
        raise ResearchError("holdout_instrument", s.series_id)
    w: Window = a.window
    return [b for b in s.bars if w.start <= b.start and b.known_at < w.end]


def _simulate(
    bars: list[PriceBar], lag: int, weights: list[Decimal], rate: Decimal
) -> tuple[Period, ...]:
    drift, out = Decimal(0), []
    for k, w in enumerate(weights):
        here, nxt = bars[lag + k], bars[lag + k + 1]
        r = CTX.subtract(div(nxt.open, here.open), 1)
        gross = CTX.multiply(w, r)
        turnover = abs(CTX.subtract(w, drift))
        cost = CTX.multiply(turnover, rate)
        drift = div(CTX.multiply(w, CTX.add(1, r)), CTX.add(1, gross))
        out.append(Period(here.start, nxt.start, w, turnover, cost, gross,
                          CTX.subtract(gross, cost)))  # fmt: skip
    return tuple(out)


def walk_forward(
    ledger: TrialLedger, dataset: ResearchDataset, rights: ResearchRights,
    access_id: str, protocol: EvaluationProtocol, series: PriceSeries,
    strategy: Strategy, baseline_series: Mapping[str, PriceSeries],
) -> Evaluation:  # fmt: skip
    view = open_view(ledger, dataset, access_id, rights)
    a, plan = view.access, view.plan
    if a.split is Split.PROMOTION:
        raise ResearchError("promotion_protocol", "holdout runs via the assessor")
    if protocol.plan_id != plan.plan_id:
        raise ResearchError("protocol_plan", protocol.protocol_id)
    if protocol.content_hash != plan.protocol_hash:
        raise ResearchError("protocol_changed", "not the protocol frozen with the plan")
    bars = _usable(series, view, dataset)
    lag, costs = protocol.fill_lag_bars, protocol.costs
    if len(bars) < lag + 2:
        raise ResearchError("insufficient_bars", series.series_id)
    held: dict[str, list[PriceBar]] = {}
    for b in protocol.baselines:
        if b.series_id is not None:
            if b.series_id not in baseline_series:
                raise ResearchError("baseline_missing", b.series_id)
            other = _usable(baseline_series[b.series_id], view, dataset)
            if [(x.start, x.end) for x in other] != [(x.start, x.end) for x in bars]:
                raise ResearchError("baseline_misaligned", b.baseline_id)
            held[b.series_id] = other
    weights: list[Decimal] = []
    for i in range(len(bars) - lag - 1):
        now = bars[i].known_at
        if bars[i + lag].start <= now:
            raise ResearchError("same_bar_fill", format_instant(bars[i + lag].start))
        d = strategy(PitContext(now, view, tuple(b for b in bars if b.known_at <= now)))
        if type(d) is not Decision:
            raise ResearchError("decision", "a Decision is required")
        if ensure_aware_utc(d.inputs_known_at) > now:
            raise ResearchError("look_ahead", "decision uses later inputs")
        w = _dec(d.target_weight, "weight")
        if w > 1:
            raise ResearchError("weight", "long-only weights lie in [0, 1]")
        weights.append(w)
    periods = _simulate(bars, lag, weights, costs.rate)
    if any(p.net <= -1 for p in periods):
        raise ResearchError("total_loss", "a period lost the whole capital")
    z, lags = protocol.z, protocol.hac_lags
    results = []
    for b in protocol.baselines:
        if b.series_id is not None:
            bp = _simulate(
                held[b.series_id], lag, [Decimal(1)] * len(weights), costs.rate
            )
        else:
            bp = tuple(Period(p.start, p.end, Decimal(0), Decimal(0), Decimal(0),
                              b.cash_rate, b.cash_rate) for p in periods)  # fmt: skip
        excess = [CTX.subtract(p.net, q.net) for p, q in zip(periods, bp, strict=True)]
        results.append(BaselineResult(b, bp, summarize([p.net for p in bp], lags, z),
                                      summarize(excess, lags, z)))  # fmt: skip
    hashes = tuple(baseline_series[k].content_hash for k in sorted(held))
    return Evaluation(
        a, plan.dataset_hash, dataset.manifest.evidence_class, protocol,
        series.content_hash, hashes, series.instrument, periods,
        summarize([p.net for p in periods], lags, z), mean([p.gross for p in periods]),
        CTX.plus(sum((p.cost for p in periods), Decimal(0))), tuple(results),
    )  # fmt: skip


def _metrics(ev: Evaluation) -> dict[str, Decimal]:
    s = ev.summary
    m = {"net_mean": s.mean, "gross_mean": ev.gross_mean, "total_cost": ev.total_cost,
         "periods": Decimal(s.n), "max_drawdown": s.max_drawdown}  # fmt: skip
    for k, v in (("volatility", s.volatility), ("sharpe_like", s.sharpe_like)):
        if v is not None:
            m[k] = v
    if s.interval is not None:
        m["ci_lower"], m["ci_upper"] = s.interval.lower, s.interval.upper
    return m


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    trial: Trial
    evaluation: Evaluation | None
    reason: str | None  # refusal code of a failed trial


def run_trial(
    ledger: TrialLedger, trial_id: str, hypothesis: str, recorded_at: datetime,
    dataset: ResearchDataset, rights: ResearchRights, access_id: str,
    protocol: EvaluationProtocol, series: PriceSeries, strategy: Strategy,
    baseline_series: Mapping[str, PriceSeries],
) -> tuple[TrialLedger, TrialOutcome]:  # fmt: skip
    """Evaluate and record the trial whatever happens; a refusal is a failed trial."""
    a = ledger.access(access_id)
    ev: Evaluation | None = None
    reason: str | None = None
    try:
        ev = walk_forward(ledger, dataset, rights, access_id, protocol, series,
                          strategy, baseline_series)  # fmt: skip
    except ResearchError as exc:
        reason = exc.code
    except StatsError as exc:
        reason = f"stats: {exc}"
    c = a.candidate
    t = Trial(trial_id, a.plan_id, c.family, hypothesis, c.material_hash,
              c.config_hash, ledger.plan(a.plan_id).dataset_hash,
              protocol.content_hash, a.split,
              Outcome.FAILED if ev is None else Outcome.COMPLETED,
              {} if ev is None else _metrics(ev), access_id, recorded_at)  # fmt: skip
    return ledger.record(t), TrialOutcome(t, ev, reason)


@dataclass(frozen=True, slots=True)
class TrialReport:
    family: str | None
    rows: tuple[dict[str, object], ...]  # every trial, any outcome, by trial id
    trial_count: int  # from TrialLedger.trial_count
    unrecorded: int  # accesses that saw data with no recorded trial


def _row(t: Trial) -> dict[str, object]:
    return {
        "trial_id": t.trial_id,
        "split": t.split.value,
        "outcome": t.outcome.value,
        "metrics": t.metrics_wire(),
        "recorded_at": format_instant(t.recorded_at),
    }


def trial_report(trials: Iterable[Trial], trial_count: int) -> TrialReport:
    ts = sorted(trials, key=lambda t: t.trial_id)
    if not all(type(t) is Trial for t in ts):
        raise ResearchError("trial", "Trial records only")
    ids = [t.trial_id for t in ts]
    families = {t.family for t in ts}
    if len(set(ids)) != len(ids) or len(families) > 1:
        raise ResearchError("trials", "distinct trials of one family")
    if type(trial_count) is not int or trial_count < len(ids):
        raise ResearchError("trial_count", "below the number of reported trials")
    rows = tuple(_row(t) for t in ts)
    return TrialReport(families.pop() if families else None, rows, trial_count,
                       trial_count - len(ids))  # fmt: skip


def ledger_report(ledger: TrialLedger, family: str) -> TrialReport:
    """The report with the ledger's own trial count (the multiple-testing count)."""
    return trial_report(ledger.trials(family), ledger.trial_count(family))


@dataclass(frozen=True, slots=True)
class ScopedClaim:
    evaluation: Evaluation
    trial_count: int
    evidence_class: EvidenceClass
    evidence_ceiling: EvidenceState
    limitations: tuple[str, ...]
    significance_claimed: bool  # always False: no adjustment is specified

    def to_wire(self) -> dict[str, object]:
        ev, s = self.evaluation, self.evaluation.summary
        ci: dict[str, object] = (
            s.interval.to_wire() if s.interval else
            {"ci_method": None, "ci_lower": None, "ci_upper": None}
        )  # fmt: skip

        def excess(i: HacInterval | None) -> object:
            return None if i is None else [str(i.lower), str(i.upper)]

        return {
            "hypothesis": ev.protocol.claim.to_wire(),
            "protocol_hash": ev.protocol.content_hash,
            "dataset_hash": ev.dataset_hash, "series_hash": ev.series_hash,
            "instrument": ev.instrument.to_wire(), "split": ev.access.split.value,
            "window": ev.access.window.to_wire(), "costs": ev.protocol.costs.to_wire(),
            "fill_lag_bars": ev.protocol.fill_lag_bars, "periods": s.n,
            "net_mean": str(s.mean), "gross_mean": str(ev.gross_mean),
            "max_drawdown": str(s.max_drawdown), **ci,
            "baselines": [{"baseline_id": b.baseline.baseline_id,
                           "excess_mean": str(b.excess.mean),
                           "excess_ci": excess(b.excess.interval)}
                          for b in ev.baselines],
            "trial_count": self.trial_count,
            "multiple_testing": "count_reported_no_adjustment_named_by_spec",
            "significance_claimed": self.significance_claimed,
            "evidence_class": self.evidence_class.value,
            "evidence_ceiling": self.evidence_ceiling.value,
            "limitations": list(self.limitations),
        }  # fmt: skip


def claim_for(ev: Evaluation, ledger: TrialLedger) -> ScopedClaim:
    """Scoped claim for an evaluation recorded as a completed trial in `ledger`; the
    trial count is the ledger's own (failed and unrecorded trials included)."""
    a = ev.access
    known = a.access_id in {x.access_id for x in ledger.accesses()}
    if not known or ledger.access(a.access_id) != a:
        raise ResearchError("trial_not_recorded", a.access_id)
    done = [t for t in ledger.trials(a.candidate.family)
            if t.access_id == a.access_id and t.outcome is Outcome.COMPLETED
            and t.protocol_hash == ev.protocol.content_hash]  # fmt: skip
    if not done:
        raise ResearchError("trial_not_recorded", a.access_id)
    nets, pr = [p.net for p in ev.periods], ev.protocol
    if (
        dict(done[0].metrics) != _metrics(ev)  # the numbers must be the recorded ones
        or summarize(nets, pr.hac_lags, pr.z) != ev.summary  # and follow the periods
    ):
        raise ResearchError("metrics_mismatch", done[0].trial_id)
    report = ledger_report(ledger, a.candidate.family)
    limits = ["development_window_not_promotion", "no_multiple_testing_adjustment"]
    limits += ev.summary.limitations
    if ev.evidence_class is EvidenceClass.SYNTHETIC:
        limits.insert(0, "synthetic_only")
    elif ev.evidence_class is EvidenceClass.HISTORICAL:
        limits.append("historical_not_prospective")
    return ScopedClaim(ev, report.trial_count, ev.evidence_class,
                       EvidenceState.LIMITED, tuple(limits), False)  # fmt: skip


def evaluation_receipt(
    receipt_id: str, experiment_id: str, ev: Evaluation, ledger: TrialLedger,
    executed_at: datetime, environment: Mapping[str, str] | None = None,
) -> dict[str, object]:  # fmt: skip
    """Shaped like the data dictionary's `evaluation_receipt` entity."""
    check_id(receipt_id, "receipt")
    check_id(experiment_id, "experiment")
    claim = claim_for(ev, ledger)
    report = ledger_report(ledger, ev.access.candidate.family)
    manifest = digest({"protocol": ev.protocol.content_hash, "dataset": ev.dataset_hash,
                       "series": ev.series_hash, "baselines": list(ev.baseline_hashes),
                       "access": ev.access.access_id})  # fmt: skip
    env = {"engine": "qw_domain.backtest", "decimal_precision": str(PRECISION),
           **(environment or {})}  # fmt: skip
    periods = [[format_instant(p.start), str(p.weight), str(p.turnover), str(p.cost),
                str(p.net)] for p in ev.periods]  # fmt: skip
    return {"id": receipt_id, "experiment_id": experiment_id,
            "test_manifest_hash": manifest, "environment": env,
            "results": {"claim": claim.to_wire(), "trials": list(report.rows),
                        "unrecorded_trials": report.unrecorded, "periods": periods},
            "executed_at": format_instant(executed_at),
            "status": "completed"}  # fmt: skip
