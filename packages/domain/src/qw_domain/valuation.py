"""Position and account valuation with unrealized P&L (T018; spec §5, F-03, F-06).

Conventions (`VALUATION_METHOD`):
- Local value = quantity x mark x explicit multiplier, in the mark currency. There is
  no default multiplier. `FxQuote.rate` is units of `quote` (reporting currency) per
  one unit of `base` (local); reporting value = local value x rate.
- Products are exact (`Fraction`) and rounded once, toward minus infinity, at the
  `MoneyAmount` scale (1e-12): assets round down (the `Rounding.PROCEEDS` direction),
  liabilities away from zero (`Rounding.COST`). The reporting value is rounded from the
  exact local product, never from the rounded local value.
- Unrealized P&L = marked value - remaining economic cost (the journal's average-cost
  convention, `postings.LOT_POLICY`), in the cost currency. In the reporting currency it
  needs the cost at historical rates (`cost_reporting`); translating cost at today's
  rate would hide the currency effect, so without it the result is unavailable.
- Realized P&L is read from the journal (`Positions.realized`) in its own currency.
- A missing, non-positive, stale or future mark or FX rate, a missing multiplier, or an
  unsupported short makes the value `Unavailable`, never zero. NAV is unavailable if any
  part is; `covered_value` totals the available parts and is labelled partial.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from qw_domain.decimals import DOMAIN_CONTEXT, FxRate, Money, Multiplier, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.journal import Positions
from qw_domain.postings import Holding

VALUATION_METHOD = "simple_position_value/1"
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


@dataclass(frozen=True, slots=True)
class Unavailable:
    """A typed unavailable result (spec §5): a stable `code` and a human reason."""

    code: str
    reason: str


def _currency(code: object) -> str:
    if not isinstance(code, str) or _CURRENCY.fullmatch(code) is None:
        raise ValueError(f"currency must be an ISO 4217 code: {code!r:.20}")
    return code


def _floor_money(value: Fraction, currency: str) -> Money:
    scaled = value * 10**12
    with localcontext(DOMAIN_CONTEXT):
        amount = Decimal(scaled.numerator // scaled.denominator).scaleb(-12)
    return Money.of(amount, currency)


class MarkKind(StrEnum):
    BID = "bid"
    ASK = "ask"
    MID = "mid"
    LAST = "last"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class Mark:
    """A per-unit price in `currency` with its type, event time and source."""

    instrument_id: InstrumentId
    price: Price
    currency: str
    kind: MarkKind
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        if (
            type(self.instrument_id) is not InstrumentId
            or type(self.price) is not Price
        ):
            raise TypeError("mark needs an InstrumentId and a Price")
        if type(self.kind) is not MarkKind or not isinstance(self.source, str):
            raise TypeError("mark needs a MarkKind and a source")
        _currency(self.currency)
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class FxQuote:
    """`rate` units of `quote` per one unit of `base`. A reference rate, not a fill."""

    base: str
    quote: str
    rate: FxRate
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        if type(self.rate) is not FxRate:
            raise TypeError("rate must be FxRate")
        if _currency(self.base) == _currency(self.quote):
            raise ValueError("FX currency pair needs two currencies")
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class ValuationBasis:
    """Reporting currency, the valuation instant and the maximum input age."""

    reporting_currency: str
    as_of: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        _currency(self.reporting_currency)
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        if type(self.max_age) is not timedelta or self.max_age < timedelta(0):
            raise ValueError("max_age must be a non-negative timedelta")

    def stale(self, observed_at: datetime, what: str) -> Unavailable | None:
        if observed_at > self.as_of:
            return Unavailable(f"{what}_after_as_of", f"{what} observed after as_of")
        if self.as_of - observed_at > self.max_age:
            return Unavailable(f"{what}_stale", f"{what} older than {self.max_age}")
        return None


@dataclass(frozen=True, slots=True)
class PositionValuation:
    instrument_id: InstrumentId
    mark: Mark
    fx: FxQuote | None
    multiplier: Multiplier
    local: Money
    reporting: Money
    unrealized_local: Money | Unavailable
    unrealized_reporting: Money | Unavailable
    method: str = VALUATION_METHOD

    @property
    def mark_observed_at(self) -> datetime:
        return self.mark.observed_at

    @property
    def fx_observed_at(self) -> datetime | None:
        return None if self.fx is None else self.fx.observed_at


def _unrealized(value: Money, cost: Money | None) -> Money | Unavailable:
    if cost is None:
        return Unavailable("cost_unknown", "no known economic cost; P&L unavailable")
    if cost.currency != value.currency:
        return Unavailable("cost_currency_mismatch", f"cost in {cost.currency}")
    return value - cost


def _rate(
    local: str, fx: FxQuote | None, basis: ValuationBasis
) -> Fraction | Unavailable:
    if local == basis.reporting_currency:
        if fx is not None:
            raise ValueError("FX pair given for a reporting-currency amount")
        return Fraction(1)
    if fx is None:
        return Unavailable("fx_missing", f"no {local}/{basis.reporting_currency} rate")
    if (fx.base, fx.quote) != (local, basis.reporting_currency):
        raise ValueError(f"FX pair {fx.base}/{fx.quote} does not convert {local}")
    return basis.stale(fx.observed_at, "fx") or Fraction(fx.rate.value)


def value_position(
    holding: Holding,
    mark: Mark | None,
    multiplier: Multiplier,
    basis: ValuationBasis,
    *,
    fx: FxQuote | None = None,
    cost_reporting: Money | None = None,
) -> PositionValuation | Unavailable:
    """Value one holding; see the module conventions."""
    if type(holding) is not Holding or type(multiplier) is not Multiplier:
        raise TypeError("value_position needs a Holding and a Multiplier")
    if holding.status == "unsupported_short":
        return Unavailable("unsupported_short", "short stock is outside coverage")
    if mark is None:
        return Unavailable("mark_missing", "no qualified mark")
    if mark.instrument_id != holding.instrument_id:
        raise ValueError("mark is for another instrument")
    if mark.price.value <= 0:
        return Unavailable("mark_not_positive", "zero or negative mark")
    if (stale := basis.stale(mark.observed_at, "mark")) is not None:
        return stale
    rate = _rate(mark.currency, fx, basis)
    if isinstance(rate, Unavailable):
        return rate
    exact = Fraction(holding.quantity.value) * Fraction(mark.price.value)
    exact *= Fraction(multiplier.value)
    local = _floor_money(exact, mark.currency)
    reporting = _floor_money(exact * rate, basis.reporting_currency)
    unrealized = _unrealized(local, holding.cost)
    if mark.currency == basis.reporting_currency:
        unrealized_reporting = unrealized
    elif cost_reporting is None:
        unrealized_reporting = Unavailable(
            "historical_fx_cost_unknown", "cost at historical FX rates is unknown"
        )
    elif type(cost_reporting) is not Money:
        raise TypeError("cost_reporting must be Money")
    else:
        unrealized_reporting = _unrealized(reporting, cost_reporting)
    return PositionValuation(
        holding.instrument_id,
        mark,
        None if mark.currency == basis.reporting_currency else fx,
        multiplier,
        local,
        reporting,
        unrealized,
        unrealized_reporting,
    )


@dataclass(frozen=True, slots=True)
class AccountValuation:
    account_id: str
    basis: ValuationBasis
    positions: tuple[tuple[InstrumentId, PositionValuation | Unavailable], ...]
    cash: tuple[tuple[str, Money | Unavailable], ...]  # by local currency
    nav: Money | Unavailable
    covered_value: Money
    coverage: Literal["complete", "partial"]
    realized: tuple[Money, ...]  # journal realized P&L per currency, untranslated
    method: str = VALUATION_METHOD


def value_account(
    positions: Positions,
    account_id: str,
    marks: Mapping[InstrumentId, Mark],
    multipliers: Mapping[InstrumentId, Multiplier],
    fx: Mapping[str, FxQuote],
    basis: ValuationBasis,
) -> AccountValuation:
    """Value one account's holdings and cash in the reporting currency. `fx` is keyed
    by local currency. Flat holdings are skipped; nothing is netted across accounts.
    An account with no holding, cash or realized rows is `account_unknown`, not 0."""
    rc = basis.reporting_currency
    covered, first_gap = Money.of(0, rc), None
    held: list[tuple[InstrumentId, PositionValuation | Unavailable]] = []
    for (acct, iid), h in sorted(positions.holdings.items()):
        if acct != account_id or not h.quantity.value:
            continue
        mult, m = multipliers.get(iid), marks.get(iid)
        pv: PositionValuation | Unavailable
        if mult is None:
            pv = Unavailable("multiplier_missing", "no explicit contract multiplier")
        else:
            quote = fx.get(m.currency) if m is not None and m.currency != rc else None
            pv = value_position(h, m, mult, basis, fx=quote)
        held.append((iid, pv))
    cash: list[tuple[str, Money | Unavailable]] = []
    for (acct, cur), amount in sorted(positions.cash.items()):
        if acct != account_id:
            continue
        rate = _rate(cur, fx.get(cur) if cur != rc else None, basis)
        if isinstance(rate, Unavailable):
            cash.append((cur, rate))
        else:
            cash.append((cur, _floor_money(Fraction(amount.amount.value) * rate, rc)))
    rows = (*positions.holdings, *positions.cash, *positions.realized)
    if not any(acct == account_id for acct, _ in rows):
        first_gap = Unavailable("account_unknown", "no journal rows for the account")
    for part in [pv for _, pv in held] + [c for _, c in cash]:
        if isinstance(part, PositionValuation):
            covered += part.reporting
        elif isinstance(part, Money):
            covered += part
        elif first_gap is None:
            first_gap = part
    realized = tuple(
        m for (a, _), m in sorted(positions.realized.items()) if a == account_id
    )
    return AccountValuation(
        account_id,
        basis,
        tuple(held),
        tuple(cash),
        first_gap or covered,
        covered,
        "complete" if first_gap is None else "partial",
        realized,
    )
