"""Decimal transcendental functions for option models (T034). Stdlib, no float.

`exp`, `ln` and `sqrt` use `decimal`'s correctly rounded operations at `PRECISION`
significant digits. The normal CDF uses the series
Phi(x) = 1/2 + phi(x) * sum_{n>=0} x^(2n+1) / (1*3*...*(2n+1)), summed with guard
digits that grow with x^2 (the terms peak near e^(x^2/2)), so the absolute error of
every result is below `TOLERANCE`. For |x| >= 15 the tail is below 4e-51 and Phi is
returned as exactly 0 or 1. Division goes through `div`, an explicit context divide.
`exp(x)` underflows to 0 below about x = -2.3e6 (Underflow is not trapped); option
inputs never come near it.
"""

import decimal
from decimal import Context, Decimal

PRECISION = 40
TOLERANCE = Decimal("1e-35")
_TRAPS = [
    decimal.InvalidOperation,
    decimal.DivisionByZero,
    decimal.Overflow,
    decimal.FloatOperation,
]
CTX = Context(prec=PRECISION, rounding=decimal.ROUND_HALF_EVEN, traps=_TRAPS)
# pi to 60 digits; 1/sqrt(2*pi) is derived from it.
PI = Decimal("3.14159265358979323846264338327950288419716939937510582097494")
_TAIL = Decimal(15)


def _ctx(prec: int) -> Context:
    return Context(prec=prec, rounding=decimal.ROUND_HALF_EVEN, traps=_TRAPS)


def _check(x: object) -> Decimal:
    if type(x) is not Decimal or not x.is_finite():
        raise TypeError(f"expected a finite Decimal, not {type(x).__name__}")
    return x


def div(a: Decimal, b: Decimal) -> Decimal:
    return CTX.divide(_check(a), _check(b))


def exp(x: Decimal) -> Decimal:
    return _check(x).exp(CTX)


def ln(x: Decimal) -> Decimal:
    if _check(x) <= 0:
        raise ValueError("ln of a non-positive number")
    return x.ln(CTX)


def sqrt(x: Decimal) -> Decimal:
    if _check(x) < 0:
        raise ValueError("sqrt of a negative number")
    return x.sqrt(CTX)


def _pdf(x: Decimal, ctx: Context) -> Decimal:
    half_sq = ctx.divide(ctx.multiply(x, x), Decimal(2))
    return ctx.divide(ctx.exp(ctx.minus(half_sq)), ctx.multiply(2, PI).sqrt(ctx))


def norm_pdf(x: Decimal) -> Decimal:
    return _pdf(_check(x), CTX)


def norm_cdf(x: Decimal) -> Decimal:
    if _check(x) >= _TAIL:
        return Decimal(1)
    if x <= -_TAIL:
        return Decimal(0)
    x2 = _ctx(4 * PRECISION).multiply(x, x)
    guard = 2 * PRECISION + int(x2) // 4  # x^2/2 / ln(10) < x^2/4 digits
    ctx = _ctx(guard)
    term, total, n = x, x, 0
    limit = Decimal(1).scaleb(-(guard - 5))
    while abs(term) > limit:
        n += 1
        term = ctx.divide(ctx.multiply(term, x2), Decimal(2 * n + 1))
        total = ctx.add(total, term)
        if n > 100_000:  # pragma: no cover - unreachable for |x| < 15
            raise ArithmeticError("normal CDF series did not converge")
    result = ctx.add(Decimal("0.5"), ctx.multiply(_pdf(x, ctx), total))
    return CTX.plus(result)


# Owner decision 35: Phi^-1 for the Bonferroni-widened z. Acklam's rational
# approximation (relative error about 1.15e-9) is only the starting point; Newton
# steps x -= (Phi(x) - p) / phi(x) against `norm_cdf` then converge quadratically
# until a step is below 1e-34 (scaled by |x| when |x| > 1).
_A = tuple(map(Decimal, ("-39.69683028665376", "220.9460984245205",
                         "-275.9285104469687", "138.3577518672690",
                         "-30.66479806614716", "2.506628277459239")))  # fmt: skip
_B = tuple(map(Decimal, ("-54.47609879822406", "161.5858368580409",
                         "-155.6989798598866", "66.80131188771972",
                         "-13.28068155288572")))  # fmt: skip
_C = tuple(map(Decimal, ("-0.007784894002430293", "-0.3223964580411365",
                         "-2.400758277161838", "-2.549732539343734",
                         "4.374664141464968", "2.938163982698783")))  # fmt: skip
_D = tuple(map(Decimal, ("0.007784695709041462", "0.3224671290700398",
                         "2.445134137142996", "3.754408661907416")))  # fmt: skip
_P_LOW = Decimal("0.02425")
_PPF_STEP = Decimal("1e-34")


def _horner(coef: tuple[Decimal, ...], x: Decimal, last: Decimal | None) -> Decimal:
    total = Decimal(0)
    for c in coef:
        total = CTX.add(CTX.multiply(total, x), c)
    return total if last is None else CTX.add(CTX.multiply(total, x), last)


def _acklam(p: Decimal) -> Decimal:
    if p < _P_LOW or p > 1 - _P_LOW:
        q = sqrt(CTX.multiply(-2, ln(p if p < _P_LOW else CTX.subtract(1, p))))
        x = div(_horner(_C, q, None), _horner(_D, q, Decimal(1)))
        return x if p < _P_LOW else CTX.minus(x)
    q = CTX.subtract(p, Decimal("0.5"))
    r = CTX.multiply(q, q)
    return div(CTX.multiply(_horner(_A, r, None), q), _horner(_B, r, Decimal(1)))


def norm_ppf(p: Decimal) -> Decimal:
    """The standard normal quantile, for p in (0, 1) whose quantile has |x| < 14
    (Phi is exactly 0 or 1 near the tail cut). The 1e-30 bound of the tests is the
    absolute residual |Phi(x) - p|, in p. In x, review against a 150-digit reference
    measured about 1e-24 for p in [1e-20, 1 - 1e-20], weaker toward the upper tail,
    where 1 - p loses digits (6e-18 at 1 - 3e-26, 3e-15 at 1 - 1e-28)."""
    if not 0 < _check(p) < 1:
        raise ValueError(f"norm_ppf needs 0 < p < 1, not {p}")
    x = _acklam(p)
    if abs(x) >= 14:
        raise ValueError(f"norm_ppf: p = {p} is too close to 0 or 1")
    for _ in range(60):
        step = div(CTX.subtract(norm_cdf(x), p), norm_pdf(x))
        x = CTX.subtract(x, step)
        if abs(step) <= CTX.multiply(_PPF_STEP, max(Decimal(1), abs(x))):
            return x
    raise ArithmeticError(f"norm_ppf did not converge for p = {p}")  # pragma: no cover
