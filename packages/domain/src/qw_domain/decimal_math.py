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
