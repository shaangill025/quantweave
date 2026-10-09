"""The first-release option package catalogue (T035; R017, R089; spec 09 "Mode and
catalogue", "Payoff convention"; config/options_catalogue.json).

- Eight opening structures: long call, long put, covered call, cash-secured put and
  the four directional verticals. `build_package` validates the legs and refuses with
  a typed `Unavailable`: one underlying, known and verified terms (T034
  `contract_terms`), one currency, the right and signed quantity per leg, and for a
  vertical the same expiry, deliverable, multiplier and settlement, equal quantities,
  distinct strikes in the structure's order, and a long at least as exercisable as
  the short. A covered call carries its shares as a `StockLeg` with a stated
  acquisition basis: exactly N units per short contract, physical settlement.
- Coverage, when an `AccountCover` is given, is T034 `check_cover` on the option
  legs (shares for a covered call, the K*M cash reserve for a cash-secured put,
  paid premium, assignment funding, broker permissions). Any blocking reason refuses
  the package (`coverage_refused`). Without a cover the package is analysis only,
  and the pricing gate (`option_pricing`) blocks live use.
- Payoff convention (spec 09): expiry P&L = signed intrinsic over the option legs +
  stock value at S - stock basis + premiums in journal sign (+ received, - paid) -
  fees. It is exact (`Fraction`) for the whole package, never per leg. P&L is
  piecewise linear in S >= 0 with kinks at the effective strikes K' (T034), so the
  bounds come from S = 0, every kink and the slope beyond the last kink; breakevens
  are its exact zeros. `max_profit` is None when unbounded; `max_loss` is a positive
  amount. Money rounds conservatively: profit down, loss up. These are idealized
  expiry bounds, not pathwise guarantees (spec 09).
- Credibility: a long leg must pay a non-zero premium and a short leg must not pay
  (`premium_sign`, `premium_zero`); max loss <= 0 is refused (`non_arbitrage`), and
  so is max profit <= 0 (`no_profit_possible`) except for a covered call, whose
  sunk stock basis may exceed strike + premium.
Analysis only: nothing here submits, amends, cancels or exercises anything.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from qw_domain.decimals import Money, PositiveQuantity
from qw_domain.identity import InstrumentId
from qw_domain.option_risk import (
    AccountCover,
    ContractTerms,
    CoverCheck,
    Leg,
    _can_cover,
    _money,
    check_cover,
    contract_terms,
)
from qw_domain.options import OptionRight, Settlement
from qw_domain.valuation import Unavailable

PAYOFF_METHOD = "expiry_piecewise_linear_exact/1"


class PackageKind(StrEnum):  # values are config/options_catalogue.json `allowed`
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    BULL_CALL_DEBIT = "bull_call_debit_vertical"
    BEAR_PUT_DEBIT = "bear_put_debit_vertical"
    BULL_PUT_CREDIT = "bull_put_credit_vertical"
    BEAR_CALL_CREDIT = "bear_call_credit_vertical"


class Direction(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class Side(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


_C, _P, _K = OptionRight.CALL, OptionRight.PUT, PackageKind
_UP, _DOWN, _DR, _CR = Direction.BULLISH, Direction.BEARISH, Side.DEBIT, Side.CREDIT
# kind -> (right, direction, side, leg signs in strike order: lower strike first)
_SHAPES: dict[PackageKind, tuple[OptionRight, Direction, Side, tuple[int, ...]]] = {
    _K.LONG_CALL: (_C, _UP, _DR, (1,)),
    _K.LONG_PUT: (_P, _DOWN, _DR, (1,)),
    _K.COVERED_CALL: (_C, _UP, _CR, (-1,)),
    _K.CASH_SECURED_PUT: (_P, _UP, _CR, (-1,)),
    _K.BULL_CALL_DEBIT: (_C, _UP, _DR, (1, -1)),
    _K.BEAR_PUT_DEBIT: (_P, _DOWN, _DR, (-1, 1)),
    _K.BULL_PUT_CREDIT: (_P, _UP, _CR, (1, -1)),
    _K.BEAR_CALL_CREDIT: (_C, _DOWN, _CR, (-1, 1)),
}


@dataclass(frozen=True, slots=True)
class StockLeg:
    """Shares held with a covered call, at their stated acquisition basis (total)."""

    instrument_id: InstrumentId
    quantity: PositiveQuantity
    basis: Money


@dataclass(frozen=True, slots=True)
class OptionPackage:
    kind: PackageKind
    direction: Direction
    side: Side
    legs: tuple[Leg, ...]  # in strike order
    terms: tuple[ContractTerms, ...]
    stock: StockLeg | None
    fees: Money
    cover: CoverCheck | None  # None: coverage not checked (analysis only)
    max_profit: Money | None  # None: unbounded
    max_loss: Money
    breakevens: tuple[Fraction, ...]  # exact terminal prices where P&L is 0
    method: str = PAYOFF_METHOD

    @property
    def currency(self) -> str:
        return self.fees.currency

    def payoff(self, spot: Decimal | Fraction) -> Fraction:
        """Exact whole-package expiry P&L at terminal underlying price `spot` >= 0."""
        s = Fraction(spot)
        if s < 0:
            raise ValueError("terminal price must be >= 0")
        total = -Fraction(self.fees.amount.value)
        for leg, t in zip(self.legs, self.terms, strict=True):
            moneyness = t.units * s + t.cash - t.strike_cash
            if leg.contract.right is _P:
                moneyness = -moneyness
            q = Fraction(leg.quantity.value)
            total += q * max(moneyness, Fraction()) + Fraction(leg.premium.amount.value)
        if self.stock is not None:
            stock = self.stock
            total += Fraction(stock.quantity.value) * s
            total -= Fraction(stock.basis.amount.value)
        return total


def _bounds(
    pkg: OptionPackage,
) -> tuple[Fraction | None, Fraction | None, tuple[Fraction, ...]]:
    """(max P&L or None if unbounded, min P&L or None if unbounded, breakevens)."""
    xs = sorted({Fraction(), *(t.effective_strike for t in pkg.terms)})
    ys = [pkg.payoff(x) for x in xs]
    slope = pkg.payoff(xs[-1] + 1) - ys[-1]
    roots = {x for x, y in zip(xs, ys, strict=True) if y == 0}
    for x0, x1, y0, y1 in zip(xs, xs[1:], ys, ys[1:], strict=False):
        if y0 * y1 < 0:
            roots.add(x0 + Fraction(y0 * (x1 - x0), y0 - y1))
    if ys[-1] * slope < 0:
        roots.add(xs[-1] - Fraction(ys[-1], slope))
    top = None if slope > 0 else max(ys)
    bottom = None if slope < 0 else min(ys)
    return top, bottom, tuple(sorted(roots))


def _refuse(code: str, reason: str) -> Unavailable:
    return Unavailable(code, reason)


def build_package(
    kind: PackageKind,
    legs: Sequence[Leg],
    *,
    fees: Money,
    stock: StockLeg | None = None,
    cover: AccountCover | None = None,
) -> OptionPackage | Unavailable:
    right, direction, side, signs = _SHAPES[kind]
    if len(legs) != len(signs):
        return _refuse("leg_count", f"{kind} takes {len(signs)} option legs")
    if len({leg.contract.contract_id for leg in legs}) != len(legs):
        return _refuse("duplicate_contract", "each leg is a distinct contract")
    terms: list[ContractTerms] = []
    for leg in legs:
        t = contract_terms(leg.contract)
        if isinstance(t, Unavailable):
            return t
        terms.append(t)
    currency = terms[0].currency
    if {t.currency for t in terms} | {leg.premium.currency for leg in legs} | {
        fees.currency
    } != {currency}:
        return _refuse("currency_mismatch", "premium, fee or strike currency differs")
    if fees.amount.value < 0:
        return _refuse("fee_negative", "fees must be >= 0")
    if len({t.underlying_id for t in terms}) != 1:
        return _refuse("underlying_mismatch", "legs on different underlyings")
    if any(leg.contract.right is not right for leg in legs):
        return _refuse("right_mismatch", f"{kind} uses {right} contracts")
    order = sorted(range(len(legs)), key=lambda i: terms[i].effective_strike)
    legs = [legs[i] for i in order]
    terms = [terms[i] for i in order]
    quantities = [Fraction(leg.quantity.value) for leg in legs]
    if any(q * sign <= 0 for q, sign in zip(quantities, signs, strict=True)):
        if len(legs) == 2 and quantities[0] * quantities[1] < 0:
            return _refuse("strike_order", f"{kind}: long strike on the wrong side")
        return _refuse("quantity_sign", f"{kind} needs signs {signs} by strike")
    for q, leg in zip(quantities, legs, strict=True):
        if (
            q * Fraction(leg.premium.amount.value) > 0
        ):  # journal sign: + received, - paid
            return _refuse("premium_sign", "a long leg pays, a short leg receives")
        if q > 0 and leg.premium.amount.value == 0:
            return _refuse("premium_zero", "a long option is never free")
    if len(legs) == 2:
        (lo, hi), (tl, th) = legs, terms
        if lo.contract.expiry != hi.contract.expiry:
            return _refuse("expiry_mismatch", "vertical legs expire differently")
        if (tl.units, tl.cash, lo.contract.multiplier, lo.contract.settlement) != (
            th.units,
            th.cash,
            hi.contract.multiplier,
            hi.contract.settlement,
        ):
            return _refuse("terms_mismatch", "deliverable, multiplier or settlement")
        if tl.strike_cash == th.strike_cash:
            return _refuse("strike_order", "vertical strikes must differ")
        if quantities[0] != -quantities[1]:
            return _refuse("quantity_mismatch", "vertical legs need equal quantities")
        long, short = (lo, hi) if quantities[0] > 0 else (hi, lo)
        if not _can_cover(long.contract, short.contract):
            return _refuse("style_cannot_cover", "a European long, an American short")
    if kind is _K.COVERED_CALL:
        if stock is None:
            return _refuse("stock_leg_required", "a covered call holds its shares")
        if stock.instrument_id != terms[0].underlying_id:
            return _refuse("underlying_mismatch", "shares of another instrument")
        if Fraction(stock.quantity.value) != terms[0].units * -quantities[0]:
            return _refuse("stock_quantity_mismatch", "N units per short contract")
        if legs[0].contract.settlement is not Settlement.PHYSICAL:
            return _refuse("cash_settled_call", "shares cannot cover a cash call")
        if stock.basis.currency != currency:
            return _refuse("currency_mismatch", "stock basis currency differs")
        if stock.basis.amount.value <= 0:
            return _refuse("stock_basis_invalid", "stated basis must be > 0")
    elif stock is not None:
        return _refuse("stock_leg_unexpected", f"{kind} has no stock leg")
    pkg = OptionPackage(
        kind=kind,
        direction=direction,
        side=side,
        legs=tuple(legs),
        terms=tuple(terms),
        stock=stock,
        fees=fees,
        cover=None,
        max_profit=None,
        max_loss=Money.of(0, currency),
        breakevens=(),
    )
    top, bottom, roots = _bounds(pkg)
    if bottom is None:  # unreachable for the catalogue shapes; never a zero
        return _refuse("unbounded_loss", "expiry loss is unbounded")
    if bottom >= 0:
        return _refuse("non_arbitrage", "max loss <= 0: premiums are not credible")
    # A covered call on shares bought above strike + premium is reported as is:
    # the stock basis is sunk.
    if top is not None and top <= 0 and kind is not _K.COVERED_CALL:
        return _refuse("no_profit_possible", "max profit <= 0 after premium and fees")
    checked = None
    if cover is not None:
        result = check_cover(legs, cover)
        if isinstance(result, Unavailable):
            return result
        if result.blocked:
            return _refuse("coverage_refused", ", ".join(result.reasons))
        checked = result
    profit = None if top is None else _money(top, currency, up=False)
    loss = _money(-bottom, currency)
    return replace(
        pkg, cover=checked, max_profit=profit, max_loss=loss, breakevens=roots
    )
