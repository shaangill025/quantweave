"""Time-weighted and money-weighted returns, FX attribution (T018; spec §5, F-07, F-08).

Rounding: returns are `Ratio`s rounded half-even at 1e-18 (`Rounding.DISPLAY`), once,
from an exact `Fraction` (TWR, FX interaction) or from the solver's 50-digit result.

TWR (`TWR_METHOD`) links exact sub-periods at external flows: sub-period return = value
before the next flow / (value before this flow + this flow) - 1. A missing valuation,
a non-positive start, a negative end value or two points at one instant (ambiguous
order) is `Unavailable`; an end value of exactly 0 is -100%. The flow at the last
point is after the period and is ignored.

MWR (`MWR_METHOD`) solves sum(CF_i * y^d_i) = 0, d_i = days from the first flow and
y = (1 + p)^(-1/T) the daily discount factor, p the period return over the T days to
the last flow (ACT/365F: annualized r = y^-365 - 1). Integer powers only, so long
series stay fast. Flows are investor-perspective; same-day flows are summed.
- The day-0 net flow must be an investment (< 0), else `no_equity`; no sign change is
  `no_sign_change`; a zero horizon or one flow is `incomplete_flows`. A zero terminal
  value after contributions only is a total loss: -100%, the limit as y -> infinity.
- Uniqueness (`uniqueness_rule`): one sign change in the net flows (Descartes), or
  one sign change in total across the partial sums from the start and from the end,
  plus one if the flows sum to 0 (Laguerre's bound for roots in (0, 1) and (1, inf);
  Norstrom's criterion is its first half, which alone only covers r > 0), with a
  positive last flow. The unique root is found by bisection over the whole domain.
  Otherwise the result is `ambiguous`: candidate roots from a 5% geometric grid (a pair
  of roots closer than one grid step can be missed) and no single return.
- The domain for 1 + p, default [1e-6, 1e6] (`growth_domain`), is an implementation
  choice, not from the spec: wide enough for any plausible period, and bounded so the
  bisection has a bracket. A root outside it is `Unavailable`.
- Bisection stops at bracket width 1e-30 in y; at `MAX_ITERATIONS` it warns
  `not_converged`. `residual` is |NPV| at the unrounded solver root, not at the
  18-place `Ratio`. Only Decimal arithmetic (precision 50).
- The annualized rate is reported for horizons of 365 days or more, or on request.

FX (`FX_METHOD`): 1 + r_reporting = (1 + r_local)(1 + r_fx), with interaction
r_local * r_fx, so local + currency + interaction = reporting exactly. This holds for
an unleveraged asset held unchanged. `linked_fx_attribution` (`FX_LINKED_METHOD`)
applies it to pathwise sub-periods of one local currency, with flows only at their
boundaries: the products of (1 + r) over sub-periods factor the same way, so the
linked local and currency returns are chained and the interaction is their product.
Allocating currency effects across several currencies or accounts is a convention the
spec does not define; it is not implemented.

Modified Dietz (`DIETZ_METHOD`, approximate, never exact TWR): (V_end - V_start -
sum CF_i) / (V_start + sum w_i CF_i), CF positive into the portfolio, w_i = (D - d_i)
/ D for a flow d_i calendar days after the start of a D-day period (flows at the start
of their day: weight 1 on the start date, 0 on the end date). V_start and V_end must
be valued on the same convention as the flows: V_start at the start of the start date
(before its flows), V_end at the end of the end date (after its flows); the caller
supplies them and they are not checked. A non-positive denominator or a zero-day
period is `Unavailable`.

Dates (`DatedFlow.on`, `ExternalFlow.on`) are calendar dates in the account's
reporting calendar; the caller converts event instants with the account's declared
time zone. The spec does not define this basis.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import accumulate, pairwise
from math import prod
from typing import Literal

from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    CurrencyMismatchError,
    DecimalValueError,
    Money,
    Ratio,
    Rounding,
    quantize,
)
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.valuation import Unavailable

TWR_METHOD = "chain_linked_twr/1"
MWR_METHOD = "mwr_bisection_act365f/1"
FX_METHOD = "fx_multiplicative/1"
FX_LINKED_METHOD = "fx_multiplicative_linked/1"
DIETZ_METHOD = "modified_dietz_approx/1"
GROWTH_DOMAIN = (Decimal("0.000001"), Decimal(1000000))  # 1 + period return
TOLERANCE = Decimal("1e-30")
MAX_ITERATIONS = 200
_GRID_STEP = Decimal("1.05")
_HALF = Decimal("0.5")
_QUANTUM = Decimal("1e-18")


def _ratio(value: Fraction) -> Ratio:
    with localcontext(DOMAIN_CONTEXT):
        return Ratio(Decimal(round(value * 10**18)).scaleb(-18))


def _same_currency(amounts: Sequence[Money]) -> None:
    if len({m.currency for m in amounts}) > 1:
        raise CurrencyMismatchError(sorted({m.currency for m in amounts}))


@dataclass(frozen=True, slots=True)
class FlowValuation:
    """The exact value immediately before an external flow at `at` (None: missing)
    and the flow, positive into the portfolio."""

    at: datetime
    value_before: Money | None
    flow: Money

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_aware_utc(self.at))
        if type(self.flow) is not Money or not (
            self.value_before is None or type(self.value_before) is Money
        ):
            raise TypeError("value_before and flow must be Money")
        if self.value_before is not None:
            _same_currency([self.value_before, self.flow])


@dataclass(frozen=True, slots=True)
class PeriodReturn:
    value: Ratio
    subperiods: tuple[Ratio, ...]
    start_at: datetime
    end_at: datetime
    exact: bool
    method: str


def time_weighted_return(points: Sequence[FlowValuation]) -> PeriodReturn | Unavailable:
    pts = tuple(points)
    if any(type(p) is not FlowValuation for p in pts):
        raise TypeError("points must be FlowValuation")
    _same_currency([p.flow for p in pts])
    return link_subperiods(
        [
            (
                p.at,
                None
                if p.value_before is None
                else Fraction(p.value_before.amount.value),
                Fraction(p.flow.amount.value),
            )
            for p in pts
        ]
    )


def link_subperiods(
    points: Sequence[tuple[datetime, Fraction | None, Fraction]],
) -> PeriodReturn | Unavailable:
    """TWR over exact (instant, value before the flow or None, flow) points.
    Instants must be aware (normalized to UTC); values and flows exact `Fraction`s."""
    points = [(ensure_aware_utc(at), v, f) for at, v, f in points]
    if any(
        type(f) is not Fraction or not (v is None or type(v) is Fraction)
        for _, v, f in points
    ):
        raise TypeError("link_subperiods takes Fraction values and flows")
    if len(points) < 2:
        return Unavailable("insufficient_valuations", "TWR needs a start and an end")
    if any(b[0] <= a[0] for a, b in pairwise(points)):
        return Unavailable("ambiguous_event_order", "points must strictly increase")
    total, subs = Fraction(1), []
    for (at, before, flow), (end_at, end, _) in pairwise(points):
        if before is None or end is None:
            return Unavailable("missing_valuation_interval", f"no value at {at}")
        if end < 0:
            return Unavailable("negative_end_value", f"value below zero at {end_at}")
        if before + flow <= 0:
            return Unavailable("nonpositive_denominator", f"start value at {at}")
        growth = Fraction(end, before + flow)
        subs.append(_ratio(growth - 1))
        total *= growth
    return PeriodReturn(
        _ratio(total - 1), tuple(subs), points[0][0], points[-1][0], True, TWR_METHOD
    )


@dataclass(frozen=True, slots=True)
class ExternalFlow:
    """Portfolio-perspective external flow: contributions positive, withdrawals
    negative (spec §5 Modified Dietz sign). `on` is a reporting-calendar date."""

    on: date
    amount: Money

    def __post_init__(self) -> None:
        require_date(self.on, "flow date")
        if type(self.amount) is not Money:
            raise TypeError("flow amount must be Money")


@dataclass(frozen=True, slots=True)
class ApproximateReturn:
    value: Ratio
    start: date
    end: date
    weights: tuple[Ratio, ...]  # per flow, rounded for display; the value is exact
    approximate: bool = True
    label: str = "approximate (Modified Dietz), not exact TWR"
    weighting: str = "calendar_day_start_of_day"
    method: str = DIETZ_METHOD


def modified_dietz(
    start_value: Money,
    end_value: Money,
    start: date,
    end: date,
    flows: Sequence[ExternalFlow],
) -> ApproximateReturn | Unavailable:
    fl = tuple(flows)
    if any(type(f) is not ExternalFlow for f in fl):
        raise TypeError("flows must be ExternalFlow")
    if type(start_value) is not Money or type(end_value) is not Money:
        raise TypeError("start and end values must be Money")
    _same_currency([start_value, end_value, *(f.amount for f in fl)])
    require_date(start, "start")
    days = (require_date(end, "end") - start).days
    if any(not start <= f.on <= end for f in fl):
        raise ValueError("flows must fall within the period")
    if days <= 0:
        return Unavailable(
            "incomplete_period", "Modified Dietz needs a positive period"
        )
    weights = [Fraction((end - f.on).days, days) for f in fl]
    cfs = [Fraction(f.amount.amount.value) for f in fl]
    v0, v1 = Fraction(start_value.amount.value), Fraction(end_value.amount.value)
    denominator = v0 + sum(
        (w * c for w, c in zip(weights, cfs, strict=True)), Fraction()
    )
    if denominator <= 0:
        return Unavailable("nonpositive_denominator", f"denominator {denominator}")
    value = Fraction(v1 - v0 - sum(cfs, Fraction()), denominator)
    return ApproximateReturn(_ratio(value), start, end, tuple(map(_ratio, weights)))


@dataclass(frozen=True, slots=True)
class DatedFlow:
    """Investor-perspective flow: contributions negative, withdrawals positive; the
    terminal portfolio value is included once, as a positive last flow. `on` is a
    calendar date in the account's reporting calendar (module docstring)."""

    on: date
    amount: Money

    def __post_init__(self) -> None:
        require_date(self.on, "flow date")
        if type(self.amount) is not Money:
            raise TypeError("flow amount must be Money")


@dataclass(frozen=True, slots=True)
class MwrRoot:
    period: Ratio
    annualized: Ratio | None


@dataclass(frozen=True, slots=True)
class MoneyWeightedReturn:
    status: Literal["unique", "ambiguous"]
    roots: tuple[MwrRoot, ...]  # the root, or the candidates when ambiguous
    start: date
    end: date
    horizon_days: int
    sign_changes: int  # Descartes count on the net dated flows
    uniqueness_rule: str  # descartes, partial_sums, total_loss or none
    iterations: int
    residual: Decimal  # largest |NPV| at an unrounded solver root
    warnings: tuple[str, ...]
    growth_domain: tuple[Decimal, Decimal]
    day_count: str = "ACT/365F"
    tolerance: Decimal = TOLERANCE
    method: str = MWR_METHOD

    @property
    def period_return(self) -> Ratio | None:
        return self.roots[0].period if self.status == "unique" else None

    @property
    def annualized(self) -> Ratio | None:
        return self.roots[0].annualized if self.status == "unique" else None


def _changes(values: Sequence[Decimal]) -> int:
    signs = [v < 0 for v in values if v]
    return sum(a != b for a, b in pairwise(signs))


def _npv(terms: Sequence[tuple[int, Decimal]], y: Decimal) -> Decimal:
    """sum(a * y^d) over (day, amount) in day order, by integer powers only."""
    total, power, prev = Decimal(0), Decimal(1), 0
    with localcontext(DOMAIN_CONTEXT) as ctx:
        for d, a in terms:
            power *= ctx.power(y, d - prev)
            total += a * power
            prev = d
    return total


def _bisect(
    terms: Sequence[tuple[int, Decimal]], lo: Decimal, hi: Decimal
) -> tuple[Decimal, int, bool]:
    """A root in (lo, hi), given NPV(lo) and NPV(hi) of strictly opposite signs;
    the flag is False if `MAX_ITERATIONS` ran out first."""
    negative_lo = _npv(terms, lo) < 0
    with localcontext(DOMAIN_CONTEXT):
        for i in range(1, MAX_ITERATIONS + 1):
            mid = (lo + hi) * _HALF
            value = _npv(terms, mid)
            if value == 0 or hi - lo <= TOLERANCE:
                return mid, i, True
            if (value < 0) == negative_lo:
                lo = mid
            else:
                hi = mid
        return (lo + hi) * _HALF, MAX_ITERATIONS, False


def _root(y: Decimal, horizon: int, annualize: bool) -> tuple[MwrRoot, str | None]:
    with localcontext(DOMAIN_CONTEXT) as ctx:
        period = _ratio(Fraction(1, Fraction(ctx.power(y, horizon))) - 1)
        annual = ctx.power(y, -365) - 1
    if not annualize:
        return MwrRoot(period, None), None
    try:
        rate = quantize(Ratio, annual, quantum=_QUANTUM, rounding=Rounding.DISPLAY)
    except DecimalValueError:
        return MwrRoot(period, None), "annualized_out_of_range"
    return MwrRoot(period, rate), None


def money_weighted_return(
    flows: Sequence[DatedFlow],
    *,
    annualize: bool = False,
    growth_domain: tuple[Decimal, Decimal] = GROWTH_DOMAIN,
) -> MoneyWeightedReturn | Unavailable:
    low, high = growth_domain
    if not (type(low) is Decimal and type(high) is Decimal and 0 < low < 1 < high):
        raise ValueError("growth domain must be Decimals with 0 < low < 1 < high")
    fl = tuple(flows)
    if any(type(f) is not DatedFlow for f in fl):
        raise TypeError("flows must be DatedFlow")
    _same_currency([f.amount for f in fl])
    if any(b.on < a.on for a, b in pairwise(fl)):
        raise ValueError("flows must be in date order")
    horizon = (fl[-1].on - fl[0].on).days if fl else 0
    if horizon <= 0:
        return Unavailable("incomplete_flows", "MWR needs flows on two dates")
    by_day: dict[int, Decimal] = {}
    with localcontext(DOMAIN_CONTEXT):
        for f in fl:
            d = (f.on - fl[0].on).days
            by_day[d] = by_day.get(d, Decimal(0)) + f.amount.amount.value
    if by_day[0] >= 0:
        return Unavailable("no_equity", "no positive starting investment")
    terms = [(d, a) for d, a in sorted(by_day.items()) if a]
    amounts = [a for _, a in terms]
    annualize_root = annualize or horizon >= 365
    warnings = ["annualized_short_window"] if annualize and horizon < 365 else []
    common = (fl[0].on, fl[-1].on, horizon)
    if fl[-1].amount.amount.value == 0 and all(a < 0 for a in amounts):
        minus_one = Ratio(-1)
        root = MwrRoot(minus_one, minus_one if annualize_root else None)
        return MoneyWeightedReturn(
            "unique", (root,), *common, 0, "total_loss", 0, Decimal(0),
            tuple(warnings), growth_domain,
        )  # fmt: skip
    descartes = _changes(amounts)
    if descartes == 0:
        return Unavailable("no_sign_change", "no IRR without a sign change")
    with localcontext(DOMAIN_CONTEXT):
        partial_sums = _changes(list(accumulate(amounts)))
        partial_sums += _changes(list(accumulate(reversed(amounts))))
        partial_sums += sum(amounts) == 0
    rule = "descartes" if descartes == 1 else ""
    if not rule and partial_sums == 1 and amounts[-1] > 0:
        rule = "partial_sums"
    with localcontext(DOMAIN_CONTEXT) as ctx:
        e = ctx.divide(Decimal(-1), Decimal(horizon))
        lo, hi = ctx.power(high, e), ctx.power(low, e)
        grid, step = [lo], ctx.power(_GRID_STEP, -e)
        while rule == "" and grid[-1] * step < hi:
            grid.append(grid[-1] * step)
        grid.append(hi)
    values = [(y, _npv(terms, y)) for y in grid]
    found = [(y, 0, True) for y, v in values if v == 0]
    for (y0, v0), (y1, v1) in pairwise(values):
        if v0 and v1 and (v0 < 0) != (v1 < 0):
            found.append(_bisect(terms, y0, y1))
    if not found:
        code = "root_outside_domain" if rule else "no_root_in_domain"
        return Unavailable(code, f"no root for 1 + period return in [{low}, {high}]")
    roots = []
    for y, _, _ in sorted(found, reverse=True):  # ascending return
        root, warning = _root(y, horizon, annualize_root)
        roots.append(root)
        warnings += [warning] if warning else []
    if not all(ok for _, _, ok in found):
        warnings.append("not_converged")
    if not rule and len(found) == 1:
        warnings.append("uniqueness_not_established")
    return MoneyWeightedReturn(
        "unique" if rule else "ambiguous",
        tuple(roots),
        *common,
        descartes,
        rule or "none",
        max(i for _, i, _ in found),
        max(abs(_npv(terms, y)) for y, _, _ in found),
        tuple(dict.fromkeys(warnings)),
        growth_domain,
    )


@dataclass(frozen=True, slots=True)
class FxAttribution:
    local: Ratio
    currency: Ratio
    interaction: Ratio
    reporting: Ratio
    method: str = FX_METHOD
    subperiods: tuple["FxAttribution", ...] = ()


def fx_attribution(local: Ratio, fx: Ratio) -> FxAttribution | Unavailable:
    if type(local) is not Ratio or type(fx) is not Ratio:
        raise TypeError("fx_attribution takes Ratio returns")
    if local.value <= -1 or fx.value <= -1:
        return Unavailable("return_below_minus_one", "a return of -100% or less")
    interaction = _ratio(Fraction(local.value) * Fraction(fx.value))
    with localcontext(DOMAIN_CONTEXT):
        reporting = Ratio(local.value + fx.value + interaction.value)
    return FxAttribution(local, fx, interaction, reporting)


def linked_fx_attribution(
    subperiods: Sequence[tuple[Ratio, Ratio]],
) -> FxAttribution | Unavailable:
    """Chain (local, fx) sub-period returns of one local currency; see the module."""
    parts = []
    for local, fx in subperiods:
        part = fx_attribution(local, fx)
        if isinstance(part, Unavailable):
            return part
        parts.append(part)
    if not parts:
        return Unavailable("insufficient_valuations", "no sub-periods")
    growth_local = prod((1 + Fraction(p.local.value) for p in parts), start=Fraction(1))
    growth_fx = prod((1 + Fraction(p.currency.value) for p in parts), start=Fraction(1))
    total = fx_attribution(_ratio(growth_local - 1), _ratio(growth_fx - 1))
    assert not isinstance(total, Unavailable)  # products of positive factors
    return FxAttribution(
        total.local,
        total.currency,
        total.interaction,
        total.reporting,
        FX_LINKED_METHOD,
        tuple(parts),
    )
