"""Source observations, balanced postings and economic event constructors (T012).

Spec §4 "Monetary and unit ledgers" and T008 review C-12/C-13, F-01..F-03:
- A `SourceObservation` is an immutable imported record. It is not a journal entry.
- A `JournalEvent` carries two posting sets that are never added together: money
  postings balance per currency (F-01) and unit postings balance per instrument
  against an explicit offsetting unit account (F-02). A split has only unit postings
  and a deposit only money postings.
- Cost convention, spec §5 "Cost and P&L reference convention" and §4: acquisition
  fees are capitalised into cost; disposition fees reduce proceeds; a partial sale
  takes cost proportional to the units sold (average cost, `LOT_POLICY`; spec §5 names
  no lot-selection method). The raw fee is kept on the event, outside the postings.
  This is an economic performance convention, not a tax-lot rule (R046).
- `event_id` fingerprints source identity plus economic content; `key` is the
  idempotency key the journal (`qw_domain.journal`) enforces.

LIMITATION: `sell` and `split` trust the `Holding` snapshot they are given. Build
them through `Journal.sell`/`Journal.split`, which derive it from the journal.
Stdlib only.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import Literal

from qw_domain.corporate_actions import SourceRef, Split
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    Money,
    PositiveQuantity,
    Price,
    Quantity,
    safe_repr,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant

LOT_POLICY = "economic_average_cost/1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)
_SCALE = 12  # MoneyAmount and Quantity scale (review §3.2)


class JournalError(ValueError):
    """A rejected event or operation; `code` names the reason for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class BookAccount(StrEnum):
    """Monetary ledger accounts suggested by spec §4 (C-12 enum)."""

    CASH = "cash"
    SECURITY_COST = "security_cost"
    EXTERNAL_CAPITAL = "external_capital"
    REALIZED_PL = "realized_pl"
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER_CLEARING = "transfer_clearing"


class UnitAccount(StrEnum):
    """Unit ledger accounts (C-12). Only `position` holds units of the account."""

    POSITION = "position"
    UNIT_CLEARING = "unit_clearing"
    CORPORATE_ACTION_CLEARING = "corporate_action_clearing"


class EventKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    BUY = "buy"
    SELL = "sell"
    FEE = "fee"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    SPLIT = "split"
    REVERSAL = "reversal"


def _check_account(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise JournalError("account_id", f"{safe_repr(value)} is malformed")
    return value


def _digest(content: object) -> str:
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """An imported source record exactly as received (string payload, e.g. a CSV
    row). The fingerprint covers account, source identity, effective time and
    payload; `observed_at` (receipt time) is excluded so a re-import matches."""

    account_id: str
    source: SourceRef
    observed_at: datetime
    effective_at: datetime
    payload: Mapping[str, str]
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _check_account(self.account_id)
        if type(self.source) is not SourceRef:
            raise TypeError("source must be a SourceRef")
        items = dict(self.payload)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in items.items()):
            raise TypeError("payload keys and values must be strings")
        set_ = object.__setattr__
        set_(self, "observed_at", ensure_aware_utc(self.observed_at))
        set_(self, "effective_at", ensure_aware_utc(self.effective_at))
        set_(self, "payload", MappingProxyType(items))
        content = [
            self.account_id,
            self.source.source_id,
            self.source.record_id,
            format_instant(self.effective_at),
            items,
        ]
        set_(self, "fingerprint", _digest(content))

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.account_id, self.source.source_id, self.source.record_id)


@dataclass(frozen=True, slots=True)
class MoneyPosting:
    """One money line. `instrument_id` names the sub-ledger (security cost, realised
    P&L or income of an instrument); security cost requires it."""

    account: BookAccount
    amount: Money
    instrument_id: InstrumentId | None = None

    def __post_init__(self) -> None:
        if type(self.account) is not BookAccount:
            raise TypeError("account must be a BookAccount")
        if type(self.amount) is not Money:
            raise TypeError(f"money posting needs Money: {type(self.amount).__name__}")
        if self.instrument_id is not None and type(self.instrument_id) is not (
            InstrumentId
        ):
            raise TypeError("instrument_id must be an InstrumentId")
        if self.account is BookAccount.SECURITY_COST and self.instrument_id is None:
            raise JournalError(
                "posting_instrument", "security cost needs an instrument"
            )
        if self.amount.amount.value.is_zero():
            raise JournalError("posting_zero", f"zero {self.account} posting")

    def wire(self) -> list[str]:
        iid = "" if self.instrument_id is None else self.instrument_id.to_wire()
        return [self.account, self.amount.currency, self.amount.amount.to_wire(), iid]


@dataclass(frozen=True, slots=True)
class UnitPosting:
    account: UnitAccount
    instrument_id: InstrumentId
    quantity: Quantity

    def __post_init__(self) -> None:
        if type(self.account) is not UnitAccount:
            raise TypeError("account must be a UnitAccount")
        if type(self.instrument_id) is not InstrumentId:
            raise TypeError("instrument_id must be an InstrumentId")
        if type(self.quantity) is not Quantity:
            raise TypeError(
                f"unit posting needs Quantity: {type(self.quantity).__name__}"
            )
        if self.quantity.value.is_zero():
            raise JournalError("posting_zero", f"zero {self.account} posting")

    def wire(self) -> list[str]:
        return [self.account, self.instrument_id.to_wire(), self.quantity.to_wire()]


def money_balances(postings: Iterable[MoneyPosting]) -> dict[str, Decimal]:
    """Sum per currency; currencies are never netted together."""
    out: dict[str, Decimal] = {}
    with localcontext(DOMAIN_CONTEXT):
        for p in postings:
            cur = p.amount.currency
            out[cur] = out.get(cur, Decimal(0)) + p.amount.amount.value
    return out


def unit_balances(postings: Iterable[UnitPosting]) -> dict[InstrumentId, Decimal]:
    out: dict[InstrumentId, Decimal] = {}
    with localcontext(DOMAIN_CONTEXT):
        for p in postings:
            out[p.instrument_id] = (
                out.get(p.instrument_id, Decimal(0)) + p.quantity.value
            )
    return out


@dataclass(frozen=True, slots=True)
class JournalEvent:
    """A posted economic event. `event_id` is the fingerprint of source identity plus
    economic content, so equal content gives an equal id."""

    kind: EventKind
    account_id: str
    effective_at: datetime
    source: SourceRef
    money: tuple[MoneyPosting, ...] = ()
    units: tuple[UnitPosting, ...] = ()
    fee: Money | None = None
    reverses: str | None = None
    supersedes: str | None = None
    lot_policy: str = LOT_POLICY
    event_id: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not EventKind:
            raise TypeError("kind must be an EventKind")
        _check_account(self.account_id)
        if type(self.source) is not SourceRef:
            raise TypeError("source must be a SourceRef")
        object.__setattr__(self, "effective_at", ensure_aware_utc(self.effective_at))
        for name, kind in (("money", MoneyPosting), ("units", UnitPosting)):
            lines = getattr(self, name)
            if type(lines) is not tuple or any(type(p) is not kind for p in lines):
                raise TypeError(f"{name} must be a tuple of {kind.__name__}")
            if len(lines) == 1:
                raise JournalError("posting_count", f"{name} needs 0 or >= 2 lines")
        if not self.money and not self.units:
            raise JournalError("event_empty", "an event needs postings")
        if self.fee is not None and type(self.fee) is not Money:
            raise TypeError("fee must be Money")
        if (self.kind is EventKind.REVERSAL) != (self.reverses is not None):
            raise JournalError("reversal_link", "reversal kind and link must agree")
        unbalanced = {
            k: v for k, v in money_balances(self.money).items() if not v.is_zero()
        }
        if unbalanced:
            raise JournalError("money_unbalanced", f"per-currency sums {unbalanced}")
        bad_units = [k.to_wire() for k, v in unit_balances(self.units).items() if v]
        if bad_units:
            raise JournalError("units_unbalanced", f"instruments {bad_units}")
        content = {
            "kind": self.kind.value,
            "account": self.account_id,
            "effective_at": format_instant(self.effective_at),
            "source": [self.source.source_id, self.source.record_id],
            "money": sorted(p.wire() for p in self.money),
            "units": sorted(p.wire() for p in self.units),
            "fee": None if self.fee is None else self.fee.to_wire(),
            "reverses": self.reverses,
            "supersedes": self.supersedes,
            "lot_policy": self.lot_policy,
        }
        object.__setattr__(self, "event_id", _digest(content))

    @property
    def key(self) -> tuple[str, ...]:
        """Idempotency key. The leading tag separates namespaces, so no source id can
        forge the key of a reversal."""
        if self.reverses is not None:
            return ("reversal", self.account_id, self.reverses)
        return ("record", self.account_id, self.source.source_id, self.source.record_id)

    def reversal(self, source: SourceRef) -> "JournalEvent":
        """The negating event, effective when the original was."""
        if self.kind is EventKind.REVERSAL:
            raise JournalError("reverse_reversal", "a reversal is not reversed")
        return JournalEvent(
            EventKind.REVERSAL,
            self.account_id,
            self.effective_at,
            source,
            tuple(
                MoneyPosting(p.account, _neg(p.amount), p.instrument_id)
                for p in self.money
            ),
            tuple(
                UnitPosting(p.account, p.instrument_id, -p.quantity) for p in self.units
            ),
            reverses=self.event_id,
        )


def _neg(m: Money) -> Money:
    return Money(-m.amount, m.currency)


def _mul(a: Decimal, b: Decimal) -> Decimal:
    with localcontext(DOMAIN_CONTEXT):
        return a * b


# ---- Event constructors (spec §4 "Supported economic events"; §5 cost convention)


@dataclass(frozen=True, slots=True)
class Header:
    account_id: str
    effective_at: datetime
    source: SourceRef


type HoldingStatus = Literal["long", "flat", "unsupported_short"]


@dataclass(frozen=True, slots=True)
class Holding:
    """Units of one instrument in one account and their economic cost. `cost` is None
    when no cost is known (never zero). A negative quantity is kept and flagged as
    unsupported exposure rather than hidden (spec §4 outside-coverage holdings)."""

    account_id: str
    instrument_id: InstrumentId
    quantity: Quantity
    cost: Money | None

    @property
    def status(self) -> HoldingStatus:
        v = self.quantity.value
        return "long" if v > 0 else "unsupported_short" if v < 0 else "flat"


def _held_by(h: Header, held: Holding) -> InstrumentId:
    """The held instrument, after checking the snapshot belongs to the account."""
    if type(held) is not Holding:
        raise TypeError("held must be a Holding")
    if held.account_id != h.account_id:
        raise JournalError("holding_mismatch", "holding is for another account")
    return held.instrument_id


def _event(
    kind: EventKind, h: Header, *lines: object, fee: Money | None = None
) -> JournalEvent:
    money = tuple(x for x in lines if isinstance(x, MoneyPosting))
    units = tuple(x for x in lines if isinstance(x, UnitPosting))
    return JournalEvent(kind, h.account_id, h.effective_at, h.source, money, units, fee)


def _money_event(
    kind: EventKind,
    h: Header,
    amount: Money,
    credit: BookAccount,
    *,
    inflow: bool,
    instrument_id: InstrumentId | None = None,
) -> JournalEvent:
    if type(amount) is not Money or amount.amount.value <= 0:
        raise JournalError("amount_not_positive", f"{kind} needs a positive amount")
    cash = amount if inflow else _neg(amount)
    return _event(
        kind,
        h,
        MoneyPosting(BookAccount.CASH, cash),
        MoneyPosting(credit, _neg(cash), instrument_id),
    )


def deposit(h: Header, amount: Money) -> JournalEvent:
    return _money_event(
        EventKind.DEPOSIT, h, amount, BookAccount.EXTERNAL_CAPITAL, inflow=True
    )


def withdrawal(h: Header, amount: Money) -> JournalEvent:
    return _money_event(
        EventKind.WITHDRAWAL, h, amount, BookAccount.EXTERNAL_CAPITAL, inflow=False
    )


def fee(h: Header, amount: Money) -> JournalEvent:
    return _money_event(EventKind.FEE, h, amount, BookAccount.EXPENSE, inflow=False)


def interest(h: Header, amount: Money) -> JournalEvent:
    return _money_event(EventKind.INTEREST, h, amount, BookAccount.INCOME, inflow=True)


def dividend(h: Header, instrument_id: InstrumentId, amount: Money) -> JournalEvent:
    """Cash dividend received (payment, not entitlement; spec §4)."""
    return _money_event(
        EventKind.DIVIDEND,
        h,
        amount,
        BookAccount.INCOME,
        inflow=True,
        instrument_id=instrument_id,
    )


def _units(
    iid: InstrumentId, qty: Quantity, offset: UnitAccount
) -> tuple[UnitPosting, ...]:
    return (UnitPosting(UnitAccount.POSITION, iid, qty), UnitPosting(offset, iid, -qty))


def _trade_value(qty: PositiveQuantity, price: Price, trade_fee: Money) -> Decimal:
    if type(qty) is not PositiveQuantity or type(price) is not Price:
        raise TypeError("quantity must be PositiveQuantity and price Price")
    if type(trade_fee) is not Money or trade_fee.amount.value < 0:
        raise JournalError("fee_negative", "fee must be Money >= 0")
    return _mul(qty.value, price.value)


def buy(
    h: Header,
    instrument_id: InstrumentId,
    qty: PositiveQuantity,
    price: Price,
    trade_fee: Money,
) -> JournalEvent:
    """Cost = qty * price + fee (§5). The fee's currency is the trade currency."""
    with localcontext(DOMAIN_CONTEXT):
        gross = _trade_value(qty, price, trade_fee)
        cost = Money.of(gross + trade_fee.amount.value, trade_fee.currency)
    return _event(
        EventKind.BUY,
        h,
        MoneyPosting(BookAccount.CASH, _neg(cost)),
        MoneyPosting(BookAccount.SECURITY_COST, cost, instrument_id),
        *_units(instrument_id, Quantity(qty.value), UnitAccount.UNIT_CLEARING),
        fee=trade_fee,
    )


def _ceil_scaled(value: Fraction) -> Decimal:
    """Round up to the class scale (Rounding.COST direction), exactly."""
    scaled = value * 10**_SCALE
    return Decimal(-(-scaled.numerator // scaled.denominator)).scaleb(-_SCALE)


def sell(
    h: Header, qty: PositiveQuantity, price: Price, trade_fee: Money, held: Holding
) -> JournalEvent:
    """Net proceeds = qty * price - fee; allocated cost is proportional (§5). Remaining
    cost rounds up at scale 12 so unrealised gain is never overstated. Stocks are
    long-only: selling more than is held, or with unknown cost, is rejected."""
    instrument_id = _held_by(h, held)
    gross = _trade_value(qty, price, trade_fee)
    if held.quantity.value < qty.value:
        raise JournalError("short_sale_unsupported", "sale exceeds held units")
    if held.cost is None:
        raise JournalError("cost_unknown", "sale of units with unknown cost")
    cur = held.cost.currency
    if trade_fee.currency != cur:
        raise JournalError("currency_mismatch", f"{trade_fee.currency} vs cost {cur}")
    total, units = Fraction(held.cost.amount.value), Fraction(held.quantity.value)
    per_unit = total * Fraction(units.denominator, units.numerator)
    remaining = _ceil_scaled(per_unit * (units - Fraction(qty.value)))
    with localcontext(DOMAIN_CONTEXT):
        allocated = held.cost.amount.value - remaining
        net = gross - trade_fee.amount.value
        realized = net - allocated
    lines = [
        (BookAccount.CASH, net, None),
        (BookAccount.SECURITY_COST, -allocated, instrument_id),
        (BookAccount.REALIZED_PL, -realized, instrument_id),
    ]
    return _event(
        EventKind.SELL,
        h,
        *(MoneyPosting(a, Money.of(v, cur), i) for a, v, i in lines if v),
        *_units(instrument_id, -Quantity(qty.value), UnitAccount.UNIT_CLEARING),
        fee=trade_fee,
    )


def _exact(value: Fraction) -> Decimal:
    """`value` as a Decimal at scale <= 12, or reject: units are never rounded."""
    for k in range(_SCALE + 1):
        if (10**k) % value.denominator == 0:
            return Decimal(value.numerator * (10**k // value.denominator)).scaleb(-k)
    raise JournalError("split_fraction", f"{value} units not representable; quarantine")


def split(h: Header, action: Split, held: Holding) -> JournalEvent:
    """Unit-only event: the position changes by new - held units against
    corporate-action clearing. Cost is untouched, so total cost is preserved and cost
    per unit scales by the inverse ratio (F-02). A fractional result that is not
    exactly representable (cash in lieu with unknown terms) is rejected."""
    if type(action) is not Split:
        raise TypeError("split needs a Split action")
    if _held_by(h, held) != action.instrument_id:
        raise JournalError("holding_mismatch", "split is for another instrument")
    if held.quantity.value <= 0:
        raise JournalError("split_not_long", "a split applies to a long holding")
    new = _exact(Fraction(held.quantity.value) * action.ratio.fraction())
    with localcontext(DOMAIN_CONTEXT):
        delta = Quantity(new - held.quantity.value)
    clearing = UnitAccount.CORPORATE_ACTION_CLEARING
    return _event(EventKind.SPLIT, h, *_units(action.instrument_id, delta, clearing))
