"""Evaluation statistics for per-period net returns (T033 increment 1). Spec §14
"Report net returns, ... drawdown, ... sample size/effective dependence and
uncertainty", "dependence-aware uncertainty when justified"; §8 "use chronological
splits and dependence-aware uncertainty estimates".
- `mean`, sample `volatility` (divisor n - 1), a per-period Sharpe-like ratio
  (mean / volatility, no risk-free rate, not annualized) and `max_drawdown` of the
  compounded series (non-positive, NUM20 sign convention).
- The confidence interval for the mean is Newey-West with the Bartlett kernel:
  gamma_k = (1/n) sum_{t>k} (r_t - m)(r_{t-k} - m),
  var(mean) = (gamma_0 + 2 sum_{k=1..L} (1 - k/(L+1)) gamma_k) / n,
  interval mean +/- z * sqrt(var(mean)). `L` and `z` come from the protocol frozen
  before evaluation; nothing here picks them after seeing the data.
- Too few observations is a stated limitation, never an invented interval.
LIMITATION: the normal approximation is weak for small samples; callers report `n`
and the lag with every interval. Stdlib only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from qw_domain.decimal_math import CTX, div, sqrt

# Standard normal 0.975 quantile (two-sided 95%); checked against norm_cdf in tests.
Z95 = Decimal("1.959963984540054")


class StatsError(ValueError):
    pass


def _values(xs: Sequence[Decimal], least: int) -> list[Decimal]:
    values = list(xs)
    for x in values:
        if type(x) is not Decimal or not x.is_finite():
            raise TypeError("returns must be finite Decimals")
    if len(values) < least:
        raise StatsError(f"at least {least} observations are required")
    return values


def mean(xs: Sequence[Decimal]) -> Decimal:
    values = _values(xs, 1)
    return div(CTX.plus(sum(values, Decimal(0))), Decimal(len(values)))


def sample_volatility(xs: Sequence[Decimal]) -> Decimal | None:
    values = _values(xs, 1)
    if len(values) < 2:
        return None
    m = mean(values)
    ss = sum((CTX.power(CTX.subtract(x, m), 2) for x in values), Decimal(0))
    return sqrt(div(ss, Decimal(len(values) - 1)))


def max_drawdown(xs: Sequence[Decimal]) -> Decimal:
    equity = peak = Decimal(1)
    worst = Decimal(0)
    for r in _values(xs, 1):
        equity = CTX.multiply(equity, CTX.add(1, r))
        peak = max(peak, equity)
        worst = min(worst, CTX.subtract(div(equity, peak), 1))
    return worst


@dataclass(frozen=True, slots=True)
class HacInterval:
    method: str
    lags: int
    n: int
    mean: Decimal
    variance_of_mean: Decimal
    std_error: Decimal
    z: Decimal
    lower: Decimal
    upper: Decimal

    def to_wire(self) -> dict[str, object]:
        return {"ci_method": self.method, "ci_lags": self.lags, "ci_n": self.n,
                "ci_z": str(self.z), "ci_lower": str(self.lower),
                "ci_upper": str(self.upper)}  # fmt: skip


def newey_west_interval(xs: Sequence[Decimal], lags: int, z: Decimal) -> HacInterval:
    values = _values(xs, 2)
    n = len(values)
    if type(lags) is not int or not 0 <= lags < n:
        raise StatsError("lags must be an int with 0 <= lags < n")
    if type(z) is not Decimal or not z > 0:
        raise StatsError("z must be a positive Decimal")
    m = mean(values)
    d = [CTX.subtract(x, m) for x in values]

    def gamma(k: int) -> Decimal:
        s = sum((CTX.multiply(d[t], d[t - k]) for t in range(k, n)), Decimal(0))
        return div(s, Decimal(n))

    long_run = gamma(0)
    for k in range(1, lags + 1):
        weight = CTX.subtract(1, div(Decimal(k), Decimal(lags + 1)))
        long_run = CTX.add(long_run, CTX.multiply(2 * weight, gamma(k)))
    var = div(long_run, Decimal(n))
    if var < 0:  # Bartlett weights keep it non-negative; guard rounding only
        raise StatsError("negative long-run variance")
    se = sqrt(var)
    half = CTX.multiply(z, se)
    lower, upper = CTX.subtract(m, half), CTX.add(m, half)
    return HacInterval("newey_west_bartlett", lags, n, m, var, se, z, lower, upper)


@dataclass(frozen=True, slots=True)
class Summary:
    n: int
    mean: Decimal
    volatility: Decimal | None
    sharpe_like: Decimal | None
    max_drawdown: Decimal
    interval: HacInterval | None
    limitations: tuple[str, ...]


def summarize(xs: Sequence[Decimal], lags: int, z: Decimal) -> Summary:
    values = _values(xs, 1)
    vol = sample_volatility(values)
    m = mean(values)
    ratio = div(m, vol) if vol else None
    interval: HacInterval | None = None
    limitations: tuple[str, ...] = ()
    if len(values) > max(lags, 1):
        interval = newey_west_interval(values, lags, z)
    else:
        limitations = ("insufficient_sample_for_interval",)
    return Summary(len(values), m, vol, ratio, max_drawdown(values), interval,
                   limitations)  # fmt: skip
