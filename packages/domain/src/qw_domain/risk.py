"""Pre-trade policy/risk evaluation and portfolio-feasible sizes (T020).

Spec §5 "Position sizing", §7 "Eligibility ordering", "Portfolio-feasible sets",
"Scoped loss pauses" and "Typed reasons"; T008 F-11, F-12. Conventions
(`RISK_METHOD`):
- Inputs come from the caller. Cash availability (net of commitments), account
  freshness and reconciliation are T017 outputs passed in. Any missing input blocks
  with a typed reason; nothing defaults to zero.
- Limits are the adopted policy's own entries for the action's account, currency and
  sleeve. A ratio limit is a fraction of its named denominator. That denominator is
  converted into the scope currency with a fresh FX quote, then reduced by pending
  withdrawals and the action's costs (post-trade, conservative).
- Pending deposits never add cash or capital. Pending withdrawals reduce cash and
  every denominator. Unexecuted sale proceeds are not cash.
- Every check is affine in the quantity q: headroom h(q) = c + k*q, exact
  (`Fraction`). Each check with k < 0 caps q at c/-k; a buy that cures an existing
  breach (k > 0, c < 0) needs q >= -c/k; a constant breach admits no size. The largest
  feasible q is the smallest cap floored to the lot increment, or 0 when that is below
  a minimum, so it never exceeds a limit. A breach blocks and reports this size as an
  explicit alternative; nothing is silently resized.
- Buys must keep every headroom >= 0. A sell is long-only (at most the held units).
  A sell passes a check whose headroom it does not worsen (a checked reduction), but
  it must still keep headroom >= 0 on any check that it worsens. Selling stock that
  covers a short call is never treated as a reduction.
- Planned loss per unit = entry - worst exit, where the worst exit is the lowest of
  the stop and any caller-supplied stop-gap exit prices. A gap through the stop is
  sized at the gap price, and a stop is never a guaranteed maximum. An unknown or
  non-positive per-unit loss means abstain. Stress shocks are caller-supplied scenario
  returns per instrument; this module invents none.
- Stocks and ETFs only; option packages are T034.
Stdlib only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction

from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    Money,
    PositiveQuantity,
    Price,
    Quantity,
    Ratio,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.onboarding import Limit, LimitMetric, SizingScope
from qw_domain.policy import PolicyHistory, SizingRequest, check_sizing
from qw_domain.risk_pauses import PauseBook
from qw_domain.valuation import FxQuote, Mark, MarkKind, Unavailable, ValuationBasis

RISK_METHOD = "pre_trade_affine_headroom/1"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Outcome(StrEnum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


class RiskCode(StrEnum):
    POLICY_NOT_ADOPTED = "POLICY_NOT_ADOPTED"  # detail: the SizingBlock code
    POLICY_EXCLUSION = "POLICY_EXCLUSION"
    INSTRUMENT_INELIGIBLE = "INSTRUMENT_INELIGIBLE"
    RISK_PAUSED = "RISK_PAUSED"
    ACCOUNT_STALE = "ACCOUNT_STALE"
    ACCOUNT_CONFLICT = "ACCOUNT_CONFLICT"
    CASH_UNCONFIRMED = "CASH_UNCONFIRMED"
    PRICE_STALE = "PRICE_STALE"
    MARK_UNSUITABLE = "MARK_UNSUITABLE"  # a buy needs an ask or last price
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    FX_UNAVAILABLE = "FX_UNAVAILABLE"
    DENOMINATOR_UNAVAILABLE = "DENOMINATOR_UNAVAILABLE"
    EXPOSURE_UNKNOWN = "EXPOSURE_UNKNOWN"
    LOSS_INPUT_INVALID = "LOSS_INPUT_INVALID"
    STRESS_SHOCK_MISSING = "STRESS_SHOCK_MISSING"
    SHORT_NOT_PERMITTED = "SHORT_NOT_PERMITTED"
    CASH_RESERVE_LIMIT = "CASH_RESERVE_LIMIT"
    WITHDRAWAL_RESERVE_BREACH = "WITHDRAWAL_RESERVE_BREACH"
    ALLOCATION_LIMIT = "ALLOCATION_LIMIT"
    CONCENTRATION_LIMIT = "CONCENTRATION_LIMIT"
    PLANNED_LOSS_LIMIT = "PLANNED_LOSS_LIMIT"
    STRESS_LIMIT = "STRESS_LIMIT"


# Size-dependent reasons: only these admit a smaller alternative.
LIMIT_CODES = frozenset(
    {
        RiskCode.SHORT_NOT_PERMITTED,
        RiskCode.CASH_RESERVE_LIMIT,
        RiskCode.WITHDRAWAL_RESERVE_BREACH,
        RiskCode.ALLOCATION_LIMIT,
        RiskCode.CONCENTRATION_LIMIT,
        RiskCode.PLANNED_LOSS_LIMIT,
        RiskCode.STRESS_LIMIT,
    }
)


class CheckKind(StrEnum):
    CASH_RESERVE = "cash_reserve"
    ALLOCATION = "allocation"  # the policy allocation for the scope (R010)
    ISSUER_CONCENTRATION = "issuer_concentration"
    SECTOR_CONCENTRATION = "sector_concentration"
    POSITION_CONCENTRATION = "position_concentration"
    PLANNED_TRADE_LOSS = "planned_trade_loss"
    STRESS_LOSS = "stress_loss"


_CODE = {
    CheckKind.CASH_RESERVE: RiskCode.CASH_RESERVE_LIMIT,
    CheckKind.ALLOCATION: RiskCode.ALLOCATION_LIMIT,
    CheckKind.ISSUER_CONCENTRATION: RiskCode.CONCENTRATION_LIMIT,
    CheckKind.SECTOR_CONCENTRATION: RiskCode.CONCENTRATION_LIMIT,
    CheckKind.POSITION_CONCENTRATION: RiskCode.CONCENTRATION_LIMIT,
    CheckKind.PLANNED_TRADE_LOSS: RiskCode.PLANNED_LOSS_LIMIT,
    CheckKind.STRESS_LOSS: RiskCode.STRESS_LIMIT,
}
# loss_pause and drawdown are pause triggers (risk_pauses), not pre-trade limits.
_EVALUATED = frozenset(m for m in LimitMetric if m.value in set(CheckKind))


@dataclass(frozen=True, slots=True)
class ProposedAction:
    tenant_id: str
    account_id: str
    currency: str  # the cash/scope currency of the account
    sleeve_id: str | None
    strategy_id: str
    denominator: str  # sizing basis for `check_sizing`
    instrument: InstrumentId
    issuer_id: str
    sector_id: str
    horizon: str  # matched against `horizon:<x>` policy exclusions
    side: Side
    quantity: PositiveQuantity
    lot: PositiveQuantity  # a power of ten
    stop: Price | None  # planned exit for a buy
    fixed_cost: Money
    unit_cost: Money
    leveraged_or_inverse: bool | None  # None: unknown
    gap_exits: tuple[Price, ...] = ()  # stop-gap scenario exit prices
    covers_short_call: bool = False

    def __post_init__(self) -> None:
        if type(self.quantity) is not PositiveQuantity or type(self.side) is not Side:
            raise TypeError("quantity must be PositiveQuantity and side a Side")
        if self.lot.value.normalize().as_tuple().digits != (1,):
            raise ValueError("lot must be a power of ten")
        for cost in (self.fixed_cost, self.unit_cost):
            if cost.currency != self.currency or cost.amount.value < 0:
                raise ValueError("costs must be non-negative in the scope currency")


@dataclass(frozen=True, slots=True)
class RiskInputs:
    as_of: datetime
    market_max_age: timedelta
    fx_max_age: timedelta
    account_max_age: timedelta
    account_observed_at: datetime | None
    reconciled: bool | None
    available_cash: Money | None  # broker-available, net of commitments (T017)
    allocation_used: Money | None  # capital already used under the allocation
    marks: Mapping[InstrumentId, Mark]
    fx: Mapping[str, FxQuote]  # by denominator currency; base = scope currency
    denominators: Mapping[str, Money | Unavailable]
    held_units: Mapping[InstrumentId, Quantity]
    position_values: Mapping[InstrumentId, Money]  # scope currency, for stress
    issuer_exposure: Mapping[str, Money]
    sector_exposure: Mapping[str, Money]
    stress_shocks: Mapping[InstrumentId, Ratio]  # scenario return per instrument
    pending_deposits: tuple[Money, ...] = ()
    pending_withdrawals: tuple[Money, ...] = ()
    value_tolerance: Money | None = None  # position value vs units x mark; None: exact

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        if self.account_observed_at is not None:
            when = ensure_aware_utc(self.account_observed_at)
            object.__setattr__(self, "account_observed_at", when)


@dataclass(frozen=True, slots=True)
class Reason:
    code: RiskCode
    detail: str


@dataclass(frozen=True, slots=True)
class LimitCheck:
    metric: CheckKind
    scope: str
    unit: str
    denominator: str | None
    denominator_amount: Money | None
    value: Money
    threshold: Money
    ratio: Ratio | None  # value / denominator for ratio limits
    headroom: Money
    passed: bool
    basis: str  # "post_trade" or "reduction_not_worsening"
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    outcome: Outcome
    reasons: tuple[Reason, ...]
    limits: tuple[LimitCheck, ...]
    requested: PositiveQuantity
    max_feasible: Quantity | None
    alternative: PositiveQuantity | None
    warnings: tuple[str, ...]
    policy_hash: str | None
    method: str = RISK_METHOD


@dataclass(frozen=True, slots=True)
class _Affine:
    """c + k*q, exact."""

    c: Fraction
    k: Fraction

    def at(self, q: Fraction) -> Fraction:
        return self.c + self.k * q


@dataclass(frozen=True, slots=True)
class _Check:
    kind: CheckKind
    limit: Limit | None
    value: _Affine
    threshold: _Affine
    den: _Affine | None
    at_least: bool  # value must stay >= threshold (cash reserve)
    provenance: tuple[str, ...]
    pre: _Affine  # >= 0 when the post-trade measure is not above the pre-trade one

    def headroom(self) -> _Affine:
        v, t, s = self.value, self.threshold, 1 if self.at_least else -1
        return _Affine(s * (v.c - t.c), s * (v.k - t.k))


def _money(x: Fraction, currency: str, *, up: bool) -> Money:
    scaled = x * 10**12
    n = -(-scaled.numerator // scaled.denominator) if up else scaled // 1
    with localcontext(DOMAIN_CONTEXT):
        return Money.of(Decimal(n).scaleb(-12), currency)


def _ratio(x: Fraction, *, up: bool) -> Ratio:
    n = -(-(x.numerator * 10**18) // x.denominator) if up else x * 10**18 // 1
    with localcontext(DOMAIN_CONTEXT):
        return Ratio(Decimal(n).scaleb(-18))


_INF = Fraction(10**40)
type _Range = tuple[Fraction, Fraction] | None


def _region(f: _Affine) -> _Range:
    """The q >= 0 where c + k*q >= 0."""
    if f.k > 0:
        return max(Fraction(0), Fraction(-f.c, f.k)), _INF
    if f.c < 0:
        return None
    return Fraction(0), _INF if f.k == 0 else Fraction(f.c, -f.k)


def _union(a: _Range, b: _Range) -> _Range:
    """The union if it is one interval, else the higher one (a safe subset)."""
    if a is None or b is None:
        return a or b
    if a[0] <= b[1] and b[0] <= a[1]:
        return min(a[0], b[0]), max(a[1], b[1])
    return max(a, b, key=lambda r: r[1])


def _amount(m: Money | None, currency: str) -> Fraction:
    if type(m) is not Money or m.currency != currency:
        raise ValueError(f"expected Money in {currency}")
    return Fraction(m.amount.value)


def evaluate(
    history: PolicyHistory | None,
    action: ProposedAction,
    inputs: RiskInputs,
    pauses: PauseBook,
) -> RiskEvaluation:
    """Evaluate one proposed action against the adopted policy; see the module
    conventions. Every gate reports its typed reason; limits are computed only when
    every input they need is present."""
    a, x, ccy = action, inputs, action.currency
    buy = a.side is Side.BUY
    reasons: list[Reason] = []
    warnings: list[str] = []

    def block(code: RiskCode, detail: str = "") -> None:
        reasons.append(Reason(code, detail))

    request = SizingRequest(a.tenant_id, a.account_id, ccy, a.sleeve_id, a.denominator)
    decision = check_sizing(history, request)
    for r in decision.reasons:
        block(RiskCode.POLICY_NOT_ADOPTED, r.code.value)
    limits: tuple[Limit, ...] = ()
    allocation = Fraction(0)
    current = history.adopted if history is not None else None
    scope = SizingScope(a.account_id, ccy, a.sleeve_id)
    if decision.allowed and current is not None:
        draft = current[0].draft
        if f"horizon:{a.horizon}" in {e.subject for e in draft.exclusions}:
            block(RiskCode.POLICY_EXCLUSION, f"horizon:{a.horizon}")
        limits = tuple(
            lim
            for lim in draft.limits
            if lim.scope.covers(scope) and lim.metric in _EVALUATED
        )
        allocation = next(
            _amount(al.amount, ccy) for al in draft.allocations if al.scope == scope
        )
    if buy and a.leveraged_or_inverse is not False:
        known = a.leveraged_or_inverse is not None
        block(RiskCode.INSTRUMENT_INELIGIBLE, "leveraged_or_inverse" if known else "")
    reducing = not buy and not a.covers_short_call
    if pauses.tenant_id != a.tenant_id:
        block(RiskCode.RISK_PAUSED, "pause book of another tenant")
    elif not reducing:
        for p in pauses.blocking(a.tenant_id, a.account_id, a.sleeve_id, a.strategy_id):
            block(RiskCode.RISK_PAUSED, f"{p.scope.kind}:{p.scope.scope_id}")

    seen = x.account_observed_at
    if seen is None or seen > x.as_of or x.as_of - seen > x.account_max_age:
        block(RiskCode.ACCOUNT_STALE, f"bound {x.account_max_age}")
    if x.reconciled is not True:
        block(RiskCode.ACCOUNT_CONFLICT, "unknown" if x.reconciled is None else "")
    if buy and x.available_cash is None:
        block(RiskCode.CASH_UNCONFIRMED, "available_cash")
    if buy and x.allocation_used is None:
        block(RiskCode.CASH_UNCONFIRMED, "allocation_used")

    mark, price = x.marks.get(a.instrument), Fraction(0)
    if mark is None:
        block(RiskCode.PRICE_STALE, "mark_missing")
    elif mark.instrument_id != a.instrument:
        raise ValueError("mark is for another instrument")
    elif mark.currency != ccy:
        block(RiskCode.CURRENCY_MISMATCH, f"mark in {mark.currency}; FX not presumed")
    elif mark.price.value <= 0:
        block(RiskCode.PRICE_STALE, "mark_not_positive")
    elif buy and mark.kind not in (MarkKind.ASK, MarkKind.LAST):
        block(RiskCode.MARK_UNSUITABLE, mark.kind.value)
    elif stale := ValuationBasis(ccy, x.as_of, x.market_max_age).stale(
        mark.observed_at, "mark"
    ):
        block(RiskCode.PRICE_STALE, stale.code)
    else:
        price = Fraction(mark.price.value)

    dens: dict[str, Fraction] = {}
    den_prov: dict[str, tuple[str, ...]] = {}
    for name in sorted({lim.denominator for lim in limits if lim.unit == "ratio"}):
        d = x.denominators.get(name)
        if d is None or isinstance(d, Unavailable):
            block(RiskCode.DENOMINATOR_UNAVAILABLE, f"{name}: {d.code if d else ''}")
            continue
        den_prov[name] = (f"denominator:{name}",)
        value = Fraction(d.amount.value)
        if d.currency != ccy:
            fx = x.fx.get(d.currency)
            if fx is None:
                block(RiskCode.FX_UNAVAILABLE, f"{ccy}/{d.currency}")
                continue
            basis = ValuationBasis(ccy, x.as_of, x.fx_max_age)
            stale = basis.stale(fx.observed_at, "fx")
            if stale or (fx.base, fx.quote) != (ccy, d.currency):
                block(RiskCode.FX_UNAVAILABLE, stale.code if stale else "fx_pair")
                continue
            value = Fraction(value, Fraction(fx.rate.value))
            den_prov[name] += (f"fx:{fx.source}@{format_instant(fx.observed_at)}",)
        if value <= 0:
            block(RiskCode.DENOMINATOR_UNAVAILABLE, f"{name}: not positive")
        dens[name] = value

    flows = (*x.pending_withdrawals, *x.pending_deposits)
    money = [*x.issuer_exposure.values(), *x.sector_exposure.values(), *flows]
    if any(m.currency != ccy for m in [*money, *x.position_values.values()]):
        block(RiskCode.CURRENCY_MISMATCH, f"exposures and flows must be in {ccy}")
    metrics = {lim.metric for lim in limits}
    held = x.held_units.get(a.instrument)
    if held is None:
        block(RiskCode.EXPOSURE_UNKNOWN, "held_units")
    exposure: dict[CheckKind, Fraction] = {}
    for kind, book, key in (
        (CheckKind.ISSUER_CONCENTRATION, x.issuer_exposure, a.issuer_id),
        (CheckKind.SECTOR_CONCENTRATION, x.sector_exposure, a.sector_id),
    ):
        if LimitMetric(kind.value) in metrics:
            if (e := book.get(key)) is None:
                block(RiskCode.EXPOSURE_UNKNOWN, f"{kind.value}:{key}")
            elif e.currency == ccy:
                exposure[kind] = Fraction(e.amount.value)
    # Completeness: every held instrument needs a value (none defaults to 0). The
    # traded one is valued at units x the fresh mark; a listed value must agree.
    for iid, units in x.held_units.items():
        if units.value and iid != a.instrument and iid not in x.position_values:
            block(RiskCode.EXPOSURE_UNKNOWN, f"position_value:{iid.uuid}")
    traded = Fraction(held.value) * price if held is not None else Fraction(0)
    tol = Fraction(x.value_tolerance.amount.value) if x.value_tolerance else 0
    listed = x.position_values.get(a.instrument)
    if (
        listed is not None
        and price
        and listed.currency == ccy
        and (abs(Fraction(listed.amount.value) - traded) > tol)
    ):
        block(RiskCode.EXPOSURE_UNKNOWN, "position value differs from units x mark")
    if held is not None:
        exposure[CheckKind.POSITION_CONCENTRATION] = traded
    shock_ids = sorted(
        {a.instrument, *x.position_values, *(i for i, u in x.held_units.items() if u)},
        key=lambda i: i.uuid,
    )
    if LimitMetric.STRESS_LOSS in metrics:
        for iid in shock_ids:
            if iid not in x.stress_shocks:
                block(RiskCode.STRESS_SHOCK_MISSING, str(iid.uuid))
    exit_price = min([a.stop, *a.gap_exits]) if a.stop is not None else None
    if buy and LimitMetric.PLANNED_TRADE_LOSS in metrics:
        if exit_price is None:
            block(RiskCode.LOSS_INPUT_INVALID, "no planned exit")
        elif price and price <= Fraction(exit_price.value):
            block(RiskCode.LOSS_INPUT_INVALID, "non-positive per-unit loss")
    if x.pending_deposits:
        warnings.append("pending_deposits_not_spendable")
    if buy and not a.gap_exits:
        warnings.append("stop_fill_assumed_no_gap_scenario")
    if reasons or held is None or mark is None:
        return RiskEvaluation(
            Outcome.BLOCK, tuple(reasons), (), a.quantity, None, None,
            tuple(warnings), decision.policy_hash,
        )  # fmt: skip

    # Every input is present: build the affine checks in the scope currency.
    sign = 1 if buy else -1
    out = sum((_amount(m, ccy) for m in x.pending_withdrawals), Fraction(0))
    fixed, unit = _amount(a.fixed_cost, ccy), _amount(a.unit_cost, ccy)
    spend = _Affine(fixed, price + unit)  # cash out for a buy
    mark_prov = f"mark:{mark.source}@{format_instant(mark.observed_at)}"
    checks: list[_Check] = []

    def add(kind: CheckKind, lim: Limit | None, value: _Affine, *extra: str) -> None:
        prov: tuple[str, ...] = (mark_prov, *extra)
        thr, den = _Affine(allocation, Fraction(0)), None
        pre = _Affine(Fraction(0), -value.k)  # amount: value(q) <= value(0)
        if lim is not None:
            prov = (f"limit:{lim.metric.value}/{lim.scope}/{lim.window}", *prov)
            thr = _Affine(Fraction(lim.value.value), Fraction(0))
        if lim is not None and lim.unit == "ratio":
            den = _Affine(dens[lim.denominator] - out - fixed, -unit)  # post-trade
            r = Fraction(lim.value.value)
            thr, prov = _Affine(r * den.c, r * den.k), prov + den_prov[lim.denominator]
            before = dens[lim.denominator] - out  # pre-trade: no costs
            r0 = Fraction(value.c, before) if before > 0 else None
            pre = (  # post ratio <= pre ratio, i.e. r0 * den(q) - value(q) >= 0
                _Affine(Fraction(-1), Fraction(0)) if r0 is None
                else _Affine(r0 * den.c - value.c, r0 * den.k - value.k)
            )  # fmt: skip
        at_least = kind is CheckKind.CASH_RESERVE
        checks.append(_Check(kind, lim, value, thr, den, at_least, prov, pre))

    if buy:
        used = _amount(x.allocation_used, ccy)
        add(CheckKind.ALLOCATION, None, _Affine(used + spend.c, spend.k))
    for lim in limits:
        kind = CheckKind(lim.metric.value)
        if kind is CheckKind.CASH_RESERVE and buy:
            cash = _amount(x.available_cash, ccy) - out
            add(kind, lim, _Affine(cash - spend.c, -spend.k), f"withdrawals:{out}")
        elif kind in exposure:
            add(kind, lim, _Affine(exposure[kind], sign * price))
        elif kind is CheckKind.PLANNED_TRADE_LOSS and buy and exit_price is not None:
            loss = _Affine(fixed, price - Fraction(exit_price.value) + unit)
            gap = ("gap_scenario",) if exit_price in a.gap_exits else ()
            add(
                kind,
                lim,
                loss,
                "stop_not_guaranteed",
                f"exit:{exit_price.to_wire()}",
                *gap,
            )
        elif kind is CheckKind.STRESS_LOSS:
            s = {i: Fraction(x.stress_shocks[i].value) for i in shock_ids}
            values = {**x.position_values}
            values.pop(a.instrument, None)
            pnl = traded * s[a.instrument] + sum(
                (_amount(v, ccy) * s[i] for i, v in values.items()), Fraction(0)
            )
            shocks = (f"shock:{i.uuid}={x.stress_shocks[i].to_wire()}" for i in s)
            add(kind, lim, _Affine(-pnl, -sign * price * s[a.instrument]), *shocks)

    q = Fraction(a.quantity.value)
    lo, hi = Fraction(0), _INF if buy else Fraction(held.value)
    if not buy and q > hi:
        block(RiskCode.SHORT_NOT_PERMITTED, f"held {held.to_wire()}")
    results: list[LimitCheck] = []
    for c in checks:
        # A reducing sell may also pass a check whose post-trade measure (ratio,
        # costs included) is not above the pre-trade one.
        h, g = c.headroom(), c.pre if reducing and not c.at_least else None
        region = _union(_region(h), None if g is None else _region(g))
        lo, hi = (lo, Fraction(-1)) if region is None else (
            max(lo, region[0]), min(hi, region[1]))  # fmt: skip
        reduction = h.at(q) < 0 and g is not None and g.at(q) >= 0
        passed = reduction or h.at(q) >= 0
        if not passed:
            withdrawal = c.at_least and out > 0 and h.c < 0
            code = RiskCode.WITHDRAWAL_RESERVE_BREACH if withdrawal else _CODE[c.kind]
            block(code, c.kind.value)
        value, threshold = c.value.at(q), c.threshold.at(q)
        den = None if c.den is None else c.den.at(q)
        ratio = None if not den or den < 0 else Fraction(value, den)
        results.append(
            LimitCheck(
                c.kind, str(c.limit.scope if c.limit else scope),
                c.limit.unit if c.limit else "amount",
                c.limit.denominator if c.limit and den is not None else None,
                None if den is None else _money(den, ccy, up=False),
                _money(value, ccy, up=not c.at_least),
                _money(threshold, ccy, up=c.at_least),
                None if ratio is None else _ratio(ratio, up=not c.at_least),
                _money(h.at(q), ccy, up=False), passed,
                "reduction_not_worsening" if reduction else "post_trade", c.provenance,
            )
        )  # fmt: skip
    lots = max(hi, Fraction(0)) // Fraction(a.lot.value)  # buys: allocation bounds hi
    if lots * Fraction(a.lot.value) < lo:
        lots = 0
    with localcontext(DOMAIN_CONTEXT):
        feasible = Quantity(Decimal(lots) * a.lot.value)
    alternative = None
    if reasons and {r.code for r in reasons} <= LIMIT_CODES and feasible.value > 0:
        alternative = PositiveQuantity(feasible.value)
    outcome = Outcome.BLOCK if reasons else Outcome.WARN if warnings else Outcome.PASS
    return RiskEvaluation(
        outcome, tuple(reasons), tuple(results), a.quantity, feasible, alternative,
        tuple(warnings), decision.policy_hash,
    )  # fmt: skip
