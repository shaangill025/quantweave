"""Preselected baselines and cash-flow-matched scorecards (T042 increment 1; spec §5
"Benchmark simulation", §14 "Baselines and cash flows", R064, R090).

Plan. An `EvaluationPlan` pins the baseline versions (`BenchmarkPolicy`, which pins
its series version) and the period before it starts: `frozen_at` and every
baseline's `selected_at` must be before the start date begins in any time zone
(00:00 at UTC+14, as in `benchmark`). `revise` appends a new plan version and is
refused once the period has begun (`baseline_change_after_start`). `digest` is a
sha256 of an explicit canonical form (id, version, currency, period, zone,
`frozen_at`, every baseline field as wire strings), recomputed on any `replace`; the
scorecard records it with `frozen_at`, so a baseline swap at the same version is
visible. `frozen_at` and the revision time are caller-asserted (no trusted clock); a
persisted append-only plan registry is later work.

Scorecard. One real account, an optional virtual portfolio and every planned baseline,
over [start, end) in the plan's reporting calendar (`tz`), each a separate `Side`:
- One source for the real side: the caller passes the account's recorded history and
  only market inputs (`MarketInputs`: marks, multipliers, FX) for the opening and
  ending instants. Both valuations are `value_account(materialize(history, at))` and
  the flows come from the same history, so flows and valuations cannot come from
  different journal states. Flows are the cash leg of external-capital and
  transfer-clearing postings (one account alone, so a transfer is external, spec §5)
  of events effective after the opening and at or before the ending instant;
  reversals net out. A holdings snapshot in the history is `snapshot_not_history`:
  trades and flows are never reconstructed from snapshots, mirror snapshots included.
  Same-date flows are netted; a flow on the end date is refused (`flow_on_end_date`,
  as in `simulate_benchmark`) rather than placed.
- The virtual side is valued by `value_virtual` from the same `MarketInputs`, so both
  sides share the instants. A partial valuation is `Unavailable`, never zero.
- Caller duties (not checked here): pass `Journal.events()` (its idempotency keys
  remove duplicates); an in-kind transfer counts at its recorded cost, not market
  value. Baseline levels are end-of-day while the valuations are at their instants.
- Deposits are flows, never returns: investment gain = ending - opening - net flows.
  Every side reports Modified Dietz (labelled approximate; no per-flow valuations
  here) and MWR over the same dated flows; a baseline also reports its exact TWR.
- Matching: the virtual side must open at the actual opening value and carry the
  same net capital flows per date (the `capital` of its entries after the opening),
  else `opening_not_matched` / `cash_flows_not_matched`. Each baseline invests the
  actual opening and flows. When the actual side is unavailable, the virtual side and
  the baselines are `actual_unavailable` (the reason names the actual code): no flow
  is fabricated. The virtual account id must differ from the real one.
- Comparisons are gain differences against a baseline only (actual - baseline,
  simulated - baseline); no figure combines real and virtual records.
- Descriptive only: no significance or edge claim. Active-return attribution is not
  defined by the spec and is labelled, not computed. Scope is the one account
  (`full_financial_situation` is always False). Trade fees sit in trade cost and
  proceeds (spec §5) and so in the gain; standalone fee and income postings are shown.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from qw_domain.benchmark import BenchmarkPolicy, BenchmarkSeries, simulate_benchmark
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    CurrencyMismatchError,
    Money,
    Multiplier,
    Quantity,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.journal import materialize
from qw_domain.postings import BookAccount, Holding, JournalEvent
from qw_domain.returns import (
    ApproximateReturn,
    DatedFlow,
    ExternalFlow,
    MoneyWeightedReturn,
    PeriodReturn,
    modified_dietz,
    money_weighted_return,
)
from qw_domain.simulation import VirtualPortfolio
from qw_domain.sources import SourceSnapshot
from qw_domain.valuation import (
    FxQuote,
    Mark,
    Unavailable,
    ValuationBasis,
    value_account,
    value_position,
)

SCORECARD_METHOD = "cash_flow_matched_scorecard/1"
INTERPRETATION = "descriptive; no significance or edge claim"
_EARLIEST_OFFSET = timedelta(hours=14)
_EXTERNAL = (BookAccount.EXTERNAL_CAPITAL, BookAccount.TRANSFER_CLEARING)


class PlanError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _period_begins(start: date) -> datetime:
    return datetime.combine(start, time(), UTC) - _EARLIEST_OFFSET


def _canonical(b: BenchmarkPolicy) -> list[str | None]:
    fee, inc = b.fee_per_trade, b.unit_increment
    return [
        b.id, b.version, b.currency, b.kind.value, b.series_id, b.series_version,
        b.selected_at.isoformat(), b.reinvestment.value, b.execution.value,
        None if fee is None else f"{fee.currency}:{fee.amount.to_wire()}",
        None if inc is None else inc.to_wire(),
    ]  # fmt: skip


@dataclass(frozen=True, slots=True)
class EvaluationPlan:
    plan_id: str
    version: int
    currency: str
    start: date
    end: date
    tz: ZoneInfo
    baselines: tuple[BenchmarkPolicy, ...]
    frozen_at: datetime
    digest: str = field(init=False)  # recomputed on every construction

    def __post_init__(self) -> None:
        object.__setattr__(self, "frozen_at", ensure_aware_utc(self.frozen_at))
        object.__setattr__(self, "baselines", tuple(self.baselines))
        if not self.plan_id or type(self.version) is not int or self.version < 1:
            raise PlanError("plan_invalid", "plan id and a positive version needed")
        if not isinstance(self.tz, ZoneInfo):
            raise TypeError("tz must be a ZoneInfo")
        if require_date(self.end, "end") <= require_date(self.start, "start"):
            raise PlanError("plan_invalid", "the period needs a positive length")
        if not self.baselines or any(
            type(b) is not BenchmarkPolicy for b in self.baselines
        ):
            raise PlanError("plan_invalid", "at least one BenchmarkPolicy baseline")
        if len({b.id for b in self.baselines}) != len(self.baselines):
            raise PlanError("plan_invalid", "duplicate baseline id")
        currencies = {self.currency, *(b.currency for b in self.baselines)}
        if len(currencies) > 1:
            raise CurrencyMismatchError(sorted(currencies))
        if self.frozen_at >= _period_begins(self.start):
            raise PlanError("baseline_selected_late", "frozen after the period began")
        if any(b.selected_at > self.frozen_at for b in self.baselines):
            raise PlanError(
                "baseline_selected_after_freeze", "a baseline postdates the plan"
            )
        content = [
            self.plan_id, str(self.version), self.currency, self.start.isoformat(),
            self.end.isoformat(), self.tz.key, self.frozen_at.isoformat(),
            [_canonical(b) for b in self.baselines],
        ]  # fmt: skip
        text = json.dumps(content, separators=(",", ":"))
        object.__setattr__(self, "digest", hashlib.sha256(text.encode()).hexdigest())

    def revise(
        self, baselines: Sequence[BenchmarkPolicy], at: datetime
    ) -> "EvaluationPlan":
        at = ensure_aware_utc(at)
        if at >= _period_begins(self.start):
            raise PlanError("baseline_change_after_start", "the period has begun")
        if at < self.frozen_at:
            raise PlanError("revision_out_of_order", "earlier than the current plan")
        return replace(
            self, version=self.version + 1, baselines=tuple(baselines), frozen_at=at
        )


@dataclass(frozen=True, slots=True)
class ActualInput:
    """One real account and its recorded history (`Journal.events()`)."""

    account_id: str
    history: Sequence[JournalEvent | SourceSnapshot]


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Market inputs for one valuation instant; nothing account-specific."""

    as_of: datetime
    marks: Mapping[InstrumentId, Mark]
    multipliers: Mapping[InstrumentId, Multiplier]
    max_age: timedelta
    fx: Mapping[str, FxQuote] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))

    def basis(self, currency: str) -> ValuationBasis:
        return ValuationBasis(currency, self.as_of, self.max_age)


class Role(StrEnum):
    ACTUAL = "actual"
    SIMULATED = "simulated"
    BASELINE = "baseline"


@dataclass(frozen=True, slots=True)
class Side:
    role: Role
    ref: str
    opening: Money
    ending: Money
    contributions: Money
    withdrawals: Money  # positive amount
    income: Money | Unavailable | None  # None: not applicable (baseline)
    fees: Money | Unavailable  # standalone fee postings; proxy fees for a baseline
    investment_gain: Money
    approx_return: ApproximateReturn | Unavailable
    mwr: MoneyWeightedReturn | Unavailable
    exact_twr: PeriodReturn | Unavailable | None = None  # baseline only


@dataclass(frozen=True, slots=True)
class Scorecard:
    plan_id: str
    plan_version: int
    plan_digest: str
    plan_frozen_at: datetime
    currency: str
    start: date
    end: date
    scope: str
    flows: tuple[ExternalFlow, ...]
    actual: Side | Unavailable
    simulated: Side | Unavailable | None
    baselines: tuple[tuple[str, Side | Unavailable], ...]
    gain_vs_baselines: tuple[tuple[str, Role, Money], ...]
    full_financial_situation: Literal[False] = False
    attribution: tuple[str, ...] = ("not_defined_by_spec:active_return_allocation",)
    interpretation: str = INTERPRETATION
    method: str = SCORECARD_METHOD


def value_virtual(portfolio: VirtualPortfolio, m: MarketInputs) -> Money | Unavailable:
    """Virtual cash plus units held at `m.as_of`, each valued by `value_position`
    (fresh mark, explicit multiplier); any gap is Unavailable."""
    if type(portfolio) is not VirtualPortfolio:
        raise TypeError("value_virtual takes a VirtualPortfolio")
    basis, acct = m.basis(portfolio.currency), portfolio.account_id
    total = portfolio.cash(m.as_of)
    for iid in portfolio.instruments():
        units = portfolio.units(iid, m.as_of)
        if not units:
            continue
        mult = m.multipliers.get(iid)
        if mult is None:
            return Unavailable("multiplier_missing", str(iid))
        held = Holding(acct, iid, Quantity(units), None)
        pv = value_position(held, m.marks.get(iid), mult, basis)
        if isinstance(pv, Unavailable):
            return pv
        with localcontext(DOMAIN_CONTEXT):
            total += pv.reporting.amount.value
    return Money.of(total, portfolio.currency)


def _local(at: datetime, tz: ZoneInfo) -> date:
    return at.astimezone(tz).date()


def _flows(net: Mapping[date, Decimal], currency: str) -> tuple[ExternalFlow, ...]:
    return tuple(
        ExternalFlow(d, Money.of(v, currency)) for d, v in sorted(net.items()) if v
    )


def _side(
    plan: EvaluationPlan,
    role: Role,
    ref: str,
    opening: Money,
    ending: Money,
    flows: tuple[ExternalFlow, ...],
    income: Money | Unavailable | None,
    fees: Money | Unavailable,
    exact_twr: PeriodReturn | Unavailable | None = None,
) -> Side:
    zero = Money.of(0, plan.currency)
    ins = sum((f.amount for f in flows if f.amount.amount.value > 0), zero)
    outs = sum((f.amount for f in flows if f.amount.amount.value < 0), zero)
    mwr_flows = [
        DatedFlow(plan.start, zero - opening),
        *(DatedFlow(f.on, zero - f.amount) for f in flows),
        DatedFlow(plan.end, ending),
    ]
    return Side(
        role, ref, opening, ending, ins, zero - outs, income, fees,
        ending - opening - ins - outs,
        modified_dietz(opening, ending, plan.start, plan.end, flows),
        money_weighted_return(mwr_flows), exact_twr,
    )  # fmt: skip


def _actual(
    plan: EvaluationPlan, a: ActualInput, opening: MarketInputs, ending: MarketInputs
) -> tuple[Side, tuple[ExternalFlow, ...]] | Unavailable:
    history: list[JournalEvent] = []
    for r in a.history:
        if type(r) is SourceSnapshot:
            return Unavailable(
                "snapshot_not_history",
                "holdings snapshots are not reconstructed into trades or flows",
            )
        if type(r) is not JournalEvent:
            raise TypeError("actual history holds only real JournalEvent records")
        history.append(r)
    navs: list[Money] = []
    for m in (opening, ending):
        positions = materialize(history, m.as_of)
        v = value_account(positions, a.account_id, m.marks, m.multipliers, m.fx,
                          m.basis(plan.currency))  # fmt: skip
        if isinstance(v.nav, Unavailable):
            return v.nav
        navs.append(v.nav)
    open_at, end_at, rc = opening.as_of, ending.as_of, plan.currency
    net: defaultdict[date, Decimal] = defaultdict(Decimal)
    income: Money | Unavailable = Money.of(0, rc)
    fees: Money | Unavailable = Money.of(0, rc)
    with localcontext(DOMAIN_CONTEXT):
        for ev in history:
            if ev.account_id != a.account_id or not open_at < ev.effective_at <= end_at:
                continue
            for p in ev.money:
                foreign = p.amount.currency != rc
                if p.account in _EXTERNAL:
                    if foreign:
                        return Unavailable(
                            "flow_currency_unsupported", p.amount.currency
                        )
                    net[_local(ev.effective_at, plan.tz)] -= p.amount.amount.value
                elif p.account is BookAccount.INCOME and isinstance(income, Money):
                    income = (
                        Unavailable("income_currency_unsupported", p.amount.currency)
                        if foreign
                        else income - p.amount
                    )
                elif p.account is BookAccount.EXPENSE and isinstance(fees, Money):
                    fees = (
                        Unavailable("fee_currency_unsupported", p.amount.currency)
                        if foreign
                        else fees + p.amount
                    )
    if net.get(plan.end):
        return Unavailable("flow_on_end_date", "flows must fall in [start, end)")
    flows = _flows(net, rc)
    side = _side(plan, Role.ACTUAL, f"account:{a.account_id}", navs[0], navs[1],
                 flows, income, fees)  # fmt: skip
    return side, flows


def _virtual(
    plan: EvaluationPlan,
    p: VirtualPortfolio,
    opening: MarketInputs,
    ending: MarketInputs,
    actual: Side,
    flows: tuple[ExternalFlow, ...],
) -> Side | Unavailable:
    if p.currency != plan.currency:
        raise CurrencyMismatchError([p.currency, plan.currency])
    v0, v1 = value_virtual(p, opening), value_virtual(p, ending)
    if isinstance(v0, Unavailable):
        return v0
    if isinstance(v1, Unavailable):
        return v1
    if v0 != actual.opening:
        return Unavailable("opening_not_matched", "virtual opening differs")
    net: defaultdict[date, Decimal] = defaultdict(Decimal)
    income, fees = Decimal(0), Decimal(0)
    with localcontext(DOMAIN_CONTEXT):
        for e in p.entries:
            if opening.as_of < e.effective_at <= ending.as_of:
                net[_local(e.effective_at, plan.tz)] += e.capital
                income, fees = income + e.income, fees + e.fees
    if _flows(net, plan.currency) != flows:
        return Unavailable("cash_flows_not_matched", "virtual capital flows differ")
    extra = (Money.of(income, plan.currency), Money.of(fees, plan.currency))
    ref = f"virtual:{p.account_id}"
    return _side(plan, Role.SIMULATED, ref, v0, v1, flows, *extra)


def build_scorecard(
    plan: EvaluationPlan,
    actual: ActualInput,
    opening: MarketInputs,
    ending: MarketInputs,
    series: Mapping[str, BenchmarkSeries],
    virtual: VirtualPortfolio | None = None,
) -> Scorecard:
    if type(plan) is not EvaluationPlan or type(actual) is not ActualInput:
        raise TypeError("build_scorecard needs an EvaluationPlan and an ActualInput")
    if type(opening) is not MarketInputs or type(ending) is not MarketInputs:
        raise TypeError("opening and ending take MarketInputs")
    if virtual is not None and type(virtual) is not VirtualPortfolio:
        raise TypeError("the simulated side takes a VirtualPortfolio")
    if virtual is not None and virtual.account_id == actual.account_id:
        raise ValueError("a virtual portfolio cannot share the real account id")
    head = (plan.plan_id, plan.version, plan.digest, plan.frozen_at, plan.currency,
            plan.start, plan.end, f"account:{actual.account_id}")  # fmt: skip
    keys = [
        f"{b.id}@{b.version}:{b.series_id}@{b.series_version}" for b in plan.baselines
    ]
    dates = (_local(opening.as_of, plan.tz), _local(ending.as_of, plan.tz))
    out: tuple[Side, tuple[ExternalFlow, ...]] | Unavailable
    if dates != (plan.start, plan.end):
        out = Unavailable("valuation_date_mismatch", "valuations off period dates")
    else:
        out = _actual(plan, actual, opening, ending)
    if isinstance(out, Unavailable):
        why = f"actual side is {out.code}; its flows and opening are not established"
        missing = Unavailable("actual_unavailable", why)
        sim0 = None if virtual is None else missing
        return Scorecard(*head, (), out, sim0, tuple((k, missing) for k in keys), ())
    act, flows = out
    sim = None
    if virtual is not None:
        sim = _virtual(plan, virtual, opening, ending, act, flows)
    rows: list[tuple[str, Side | Unavailable]] = []
    for key, policy in zip(keys, plan.baselines, strict=True):
        s = series.get(policy.series_id)
        if s is None:
            rows.append((key, Unavailable("benchmark_series_missing", key)))
            continue
        bm = simulate_benchmark(policy, s, act.opening, plan.start, plan.end, flows)
        if isinstance(bm, Unavailable):
            rows.append((key, bm))
            continue
        args = (act.opening, bm.ending_value, flows, None, bm.fees, bm.twr)
        rows.append((key, _side(plan, Role.BASELINE, key, *args)))
    gaps = tuple(
        (key, side.role, side.investment_gain - b.investment_gain)
        for key, b in rows
        if isinstance(b, Side)
        for side in (act, sim)
        if isinstance(side, Side)
    )
    return Scorecard(*head, flows, act, sim, tuple(rows), gaps)
