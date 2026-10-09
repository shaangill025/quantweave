"""Benchmark cash-flow simulation (T018; spec §5 "Benchmark simulation", §14, R064).

`simulate_benchmark` invests the portfolio's opening value and external flows in a
benchmark selected before the period (`BENCHMARK_METHOD`):
- Execution: each net flow of a date trades at that date's level (`Execution`, one
  modelled convention); flows of one date are netted first, and a date whose flows net
  to zero still needs a level. A missing level on the start date, a flow date or the
  end date is `Unavailable` (`benchmark_level_missing`). No level is carried forward
  or interpolated; the spec defines no fill policy, so none is applied.
- The policy pins the series version (`benchmark_version_mismatch` otherwise) and
  declares reinvestment: `total_return` for an index (distributions are in the
  level), `none` for a proxy (price levels; distributions are not modelled).
- Flows must fall in [start, end) (`flow_outside_period`): the opening value is the
  value at the start of the start date, and the ending value is valued at the end
  date's level after the period, so an end-date flow would be outside the period. The
  caller values the portfolio on the same convention when comparing.
- A total-return index (theoretical) buys fractional units at no cost. An investable
  proxy buys whole multiples of `unit_increment` with `fee_per_trade` per trade;
  residual cash stays uninvested at zero return. A withdrawal sells the fewest units
  that cover it and the fee.
- `unit_increment` must be a power of ten (1, 0.01, 10, ...).
- Units that would go negative are `Unavailable` (`negative_units`).
- The policy must be selected before the period: before the start date begins in any
  time zone (00:00 at UTC+14), else `benchmark_selected_late`.
- Flows, opening value, policy and series share the reporting currency; converting a
  foreign-currency benchmark is not implemented.
- TWR uses `returns.link_subperiods` on exact simulated values (fees are internal
  performance effects); MWR uses `money_weighted_return` on the opening value and
  flows (investor perspective) and the ending value. The ending value and units are
  floored at 1e-12, as in `valuation`.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from math import ceil, floor

from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    CurrencyMismatchError,
    Money,
    Price,
    Quantity,
)
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.returns import (
    DatedFlow,
    ExternalFlow,
    MoneyWeightedReturn,
    PeriodReturn,
    link_subperiods,
    money_weighted_return,
)
from qw_domain.valuation import Unavailable

BENCHMARK_METHOD = "benchmark_flow_simulation/1"
_EARLIEST_OFFSET = timedelta(hours=14)


class Execution(StrEnum):
    LEVEL_ON_FLOW_DATE = "level_on_flow_date"


class Reinvestment(StrEnum):
    TOTAL_RETURN = "total_return"  # distributions are in the index level
    NONE = "none"


class BenchmarkKind(StrEnum):
    TOTAL_RETURN_INDEX = "total_return_index"  # theoretical
    INVESTABLE_PROXY = "investable_proxy"


@dataclass(frozen=True, slots=True)
class BenchmarkPolicy:
    id: str
    version: str
    currency: str
    kind: BenchmarkKind
    series_id: str
    selected_at: datetime
    fee_per_trade: Money | None = None  # proxy only
    unit_increment: Quantity | None = None  # proxy only, a power of ten
    execution: Execution = Execution.LEVEL_ON_FLOW_DATE
    series_version: str = field(kw_only=True)
    reinvestment: Reinvestment = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected_at", ensure_aware_utc(self.selected_at))
        object.__setattr__(self, "execution", Execution(self.execution))
        if type(self.kind) is not BenchmarkKind:
            raise TypeError("kind must be a BenchmarkKind")
        if type(self.reinvestment) is not Reinvestment:
            raise TypeError("reinvestment must be a Reinvestment")
        proxy = self.kind is BenchmarkKind.INVESTABLE_PROXY
        if self.reinvestment is not (
            Reinvestment.NONE if proxy else Reinvestment.TOTAL_RETURN
        ):
            raise ValueError(
                f"{self.kind} with reinvestment {self.reinvestment} is not modelled"
            )
        if not proxy and (self.fee_per_trade, self.unit_increment) != (None, None):
            raise ValueError("a theoretical index has no fee or unit increment")
        if not proxy:
            return
        fee, inc = self.fee_per_trade, self.unit_increment
        if (
            type(fee) is not Money
            or fee.currency != self.currency
            or fee.amount.value < 0
        ):
            raise ValueError("a proxy needs a non-negative fee in the policy currency")
        if type(inc) is not Quantity or not (
            inc.value > 0 and inc.value == Decimal(1).scaleb(inc.value.adjusted())
        ):
            raise ValueError("a proxy needs a power-of-ten unit increment")


@dataclass(frozen=True, slots=True)
class BenchmarkLevel:
    """Level (index value or proxy price) for a reporting-calendar date."""

    on: date
    level: Price
    observed_at: datetime

    def __post_init__(self) -> None:
        require_date(self.on, "level date")
        if type(self.level) is not Price:
            raise TypeError("level must be a Price")
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    id: str
    version: str
    currency: str
    levels: tuple[BenchmarkLevel, ...]

    def __post_init__(self) -> None:
        if any(type(lv) is not BenchmarkLevel for lv in self.levels):
            raise TypeError("levels must be BenchmarkLevel")
        if len({lv.on for lv in self.levels}) != len(self.levels):
            raise ValueError("duplicate level date")

    def level(self, on: date) -> BenchmarkLevel | None:
        return next((lv for lv in self.levels if lv.on == on), None)


@dataclass(frozen=True, slots=True)
class BenchmarkSimulation:
    policy_id: str
    policy_version: str
    series_id: str
    series_version: str
    start: date
    end: date
    ending_value: Money
    units: Quantity
    cash: Money
    fees: Money
    twr: PeriodReturn | Unavailable
    mwr: MoneyWeightedReturn | Unavailable
    levels_used: tuple[BenchmarkLevel, ...]
    method: str = BENCHMARK_METHOD


def _floor12(value: Fraction) -> Decimal:
    scaled = value * 10**12
    with localcontext(DOMAIN_CONTEXT):
        return Decimal(scaled.numerator // scaled.denominator).scaleb(-12)


def simulate_benchmark(
    policy: BenchmarkPolicy,
    series: BenchmarkSeries,
    opening: Money,
    start: date,
    end: date,
    flows: Sequence[ExternalFlow],
) -> BenchmarkSimulation | Unavailable:
    """Invest `opening` at `start` and each flow (positive into the portfolio) in the
    benchmark; see the module conventions."""
    fl = tuple(flows)
    if type(policy) is not BenchmarkPolicy or type(series) is not BenchmarkSeries:
        raise TypeError("simulate_benchmark needs a BenchmarkPolicy and a series")
    if any(type(f) is not ExternalFlow for f in fl) or type(opening) is not Money:
        raise TypeError("opening must be Money and flows ExternalFlow")
    if series.id != policy.series_id:
        raise ValueError("series is not the policy's benchmark series")
    currencies = {opening.currency, series.currency, *(f.amount.currency for f in fl)}
    if currencies != {policy.currency}:
        raise CurrencyMismatchError(sorted(currencies | {policy.currency}))
    require_date(start, "start")
    if require_date(end, "end") <= start:
        return Unavailable("incomplete_period", "the period needs a positive length")
    if any(not start <= f.on < end for f in fl):
        return Unavailable("flow_outside_period", "flows must fall in [start, end)")
    if series.version != policy.series_version:
        return Unavailable("benchmark_version_mismatch", "series version not pinned")
    if policy.selected_at >= datetime.combine(start, time(), UTC) - _EARLIEST_OFFSET:
        return Unavailable("benchmark_selected_late", "selected after the period began")
    net: defaultdict[date, Fraction] = defaultdict(Fraction)
    net[start] += Fraction(opening.amount.value)
    for f in fl:
        net[f.on] += Fraction(f.amount.amount.value)
    used = []
    for on in sorted({*net, end}):
        lv = series.level(on)
        if lv is None:
            return Unavailable("benchmark_level_missing", f"no benchmark level on {on}")
        used.append(lv)
    units, cash, fees = Fraction(), Fraction(), Fraction()
    points: list[tuple[datetime, Fraction | None, Fraction]] = []
    for lv in used:
        price, flow = Fraction(lv.level.value), net.get(lv.on, Fraction())
        points.append((lv.observed_at, units * price + cash, flow))
        if lv.on == end:
            break
        if policy.kind is BenchmarkKind.TOTAL_RETURN_INDEX:
            units += Fraction(flow, price)
        else:
            assert policy.fee_per_trade is not None and policy.unit_increment
            fee = Fraction(policy.fee_per_trade.amount.value)
            step = Fraction(policy.unit_increment.value)
            cash += flow
            n = 0
            if flow > 0:
                n = max(floor(Fraction(cash - fee, step * price)), 0)
            elif cash < 0:
                n = -ceil(Fraction(fee - cash, step * price))
            if n:
                units, cash, fees = (
                    units + n * step,
                    cash - n * step * price - fee,
                    fees + fee,
                )
        if units < 0:
            return Unavailable("negative_units", f"withdrawal exceeds value on {lv.on}")
    value = points[-1][1]
    assert value is not None
    rc = policy.currency
    ending = Money.of(_floor12(value), rc)
    mwr_flows = [
        DatedFlow(on, Money.of(0, rc) - Money.of(_floor12(amount), rc))
        for on, amount in sorted(net.items())
        if amount
    ]
    return BenchmarkSimulation(
        policy.id,
        policy.version,
        series.id,
        series.version,
        start,
        end,
        ending,
        Quantity(_floor12(units)),
        Money.of(_floor12(cash), rc),
        Money.of(_floor12(fees), rc),
        link_subperiods(points),
        money_weighted_return([*mwr_flows, DatedFlow(end, ending)]),
        tuple(used),
    )
