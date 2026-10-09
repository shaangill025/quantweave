"""Source observations, the balanced decimal journal and materialized positions (T012).

Spec §4 "Monetary and unit ledgers" and T008 review C-12/C-13, F-01..F-03, F-05:
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
- Idempotency (spec §4 "Idempotency and conflict resolution"): one source record maps
  to at most one event. Same record and content is a no-op; same record with other
  content is a conflict and never overwrites.
- Corrections (spec §4 "Corrections and retention", F-05): the original is kept; a
  reversal negates it and a superseding event reposts. Every entry keeps its
  `recorded_at` knowledge time, so any earlier knowledge state can be replayed.

Persistence, the import pipeline, FX, transfers, options and other corporate actions
are later work. Stdlib only.
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
    def key(self) -> tuple[str, str, str]:
        """Idempotency key: the source record, or the reversed event for a reversal."""
        if self.reverses is not None:
            return (self.account_id, "reversal", self.reverses)
        return (self.account_id, self.source.source_id, self.source.record_id)

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
    h: Header,
    instrument_id: InstrumentId,
    qty: PositiveQuantity,
    price: Price,
    trade_fee: Money,
    held: "Holding",
) -> JournalEvent:
    """Net proceeds = qty * price - fee; allocated cost is proportional (§5). Remaining
    cost rounds up at scale 12 so unrealised gain is never overstated. Stocks are
    long-only: selling more than is held, or with unknown cost, is rejected."""
    gross = _trade_value(qty, price, trade_fee)
    if held.cost is None:
        raise JournalError("cost_unknown", "sale of units with unknown cost")
    if held.quantity.value < qty.value:
        raise JournalError("short_sale_unsupported", "sale exceeds held units")
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


def split(h: Header, action: Split, held: PositiveQuantity) -> JournalEvent:
    """Unit-only event: the position changes by new - held units against
    corporate-action clearing. Cost is untouched, so total cost is preserved and cost
    per unit scales by the inverse ratio (F-02). A fractional result that is not
    exactly representable (cash in lieu with unknown terms) is rejected."""
    if type(action) is not Split or type(held) is not PositiveQuantity:
        raise TypeError("split needs a Split action and a PositiveQuantity")
    new = _exact(Fraction(held.value) * action.ratio.fraction())
    with localcontext(DOMAIN_CONTEXT):
        delta = Quantity(new - held.value)
    clearing = UnitAccount.CORPORATE_ACTION_CLEARING
    return _event(EventKind.SPLIT, h, *_units(action.instrument_id, delta, clearing))


# ---- Append-only journal with idempotency, conflicts and knowledge time


class Outcome(StrEnum):
    POSTED = "posted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Entry:
    event: JournalEvent
    recorded_at: datetime  # knowledge time


@dataclass(frozen=True, slots=True)
class Conflict:
    """A rejected record whose key is already held with other content."""

    key: tuple[str, str, str]
    existing: str
    rejected: JournalEvent | SourceObservation


class ObservationLog:
    """Immutable source observations keyed by (account, source, record)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], SourceObservation] = {}
        self.conflicts: list[Conflict] = []

    def observe(self, obs: SourceObservation) -> Outcome:
        if type(obs) is not SourceObservation:
            raise TypeError("observe takes a SourceObservation")
        prior = self._by_key.get(obs.key)
        if prior is None:
            self._by_key[obs.key] = obs
            return Outcome.POSTED
        if prior.fingerprint == obs.fingerprint:
            return Outcome.DUPLICATE
        self.conflicts.append(Conflict(obs.key, prior.fingerprint, obs))
        return Outcome.CONFLICT

    def get(self, key: tuple[str, str, str]) -> SourceObservation | None:
        return self._by_key.get(key)


class Journal:
    """Append-only financial journal. `recorded_at` is non-decreasing."""

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._by_key: dict[tuple[str, str, str], JournalEvent] = {}
        self._by_id: dict[str, Entry] = {}
        self.conflicts: list[Conflict] = []

    def post(self, event: JournalEvent, recorded_at: datetime) -> Outcome:
        if type(event) is not JournalEvent:
            raise TypeError("post takes a JournalEvent")
        recorded_at = ensure_aware_utc(recorded_at)
        prior = self._by_key.get(event.key)
        if prior is not None:
            if prior.event_id == event.event_id:
                return Outcome.DUPLICATE
            self.conflicts.append(Conflict(event.key, prior.event_id, event))
            return Outcome.CONFLICT
        if self._entries and recorded_at < self._entries[-1].recorded_at:
            raise JournalError("recorded_at_order", "knowledge time moved backwards")
        for link in (event.reverses, event.supersedes):
            target = None if link is None else self._by_id.get(link)
            if link is not None and (
                target is None or target.event.account_id != event.account_id
            ):
                raise JournalError("link_unknown", f"no event {link} in this account")
        if event.supersedes is not None and self.reversal_of(event.supersedes) is None:
            raise JournalError("supersede_unreversed", "reverse the original first")
        entry = Entry(event, recorded_at)
        self._entries.append(entry)
        self._by_key[event.key] = event
        self._by_id[event.event_id] = entry
        return Outcome.POSTED

    def reverse(
        self, event_id: str, source: SourceRef, recorded_at: datetime
    ) -> Outcome:
        entry = self._by_id.get(event_id)
        if entry is None:
            raise JournalError("link_unknown", f"no event {event_id}")
        return self.post(entry.event.reversal(source), recorded_at)

    def correct(self, replacement: JournalEvent, recorded_at: datetime) -> Outcome:
        """Reverse `replacement.supersedes` (sourced to the correction record) and
        post the replacement. The original entry is kept unchanged."""
        if replacement.supersedes is None:
            raise JournalError("supersede_missing", "replacement names no original")
        outcome = self.reverse(replacement.supersedes, replacement.source, recorded_at)
        if outcome is Outcome.CONFLICT:
            return outcome
        return self.post(replacement, recorded_at)

    def reversal_of(
        self, event_id: str, known_as_of: datetime | None = None
    ) -> JournalEvent | None:
        """The reversal of `event_id`, if one was known by `known_as_of`."""
        entry = self._by_id.get(event_id)
        if entry is None:
            return None
        rev = self._by_key.get((entry.event.account_id, "reversal", event_id))
        if rev is None or known_as_of is None:
            return rev
        known = self._by_id[rev.event_id].recorded_at <= ensure_aware_utc(known_as_of)
        return rev if known else None

    def entries(self, known_as_of: datetime | None = None) -> tuple[Entry, ...]:
        if known_as_of is None:
            return tuple(self._entries)
        t = ensure_aware_utc(known_as_of)
        return tuple(e for e in self._entries if e.recorded_at <= t)

    def events(self, known_as_of: datetime | None = None) -> tuple[JournalEvent, ...]:
        return tuple(e.event for e in self.entries(known_as_of))


# ---- Materialized positions: a pure fold over postings


type HoldingStatus = Literal["long", "flat", "unsupported_short"]


@dataclass(frozen=True, slots=True)
class Holding:
    """Units held and their economic cost. `cost` is None when no cost was ever
    posted (unknown, never zero). A negative quantity is kept and flagged as
    unsupported exposure rather than hidden (spec §4 outside-coverage holdings)."""

    quantity: Quantity
    cost: Money | None

    @property
    def status(self) -> HoldingStatus:
        v = self.quantity.value
        return "long" if v > 0 else "unsupported_short" if v < 0 else "flat"


type AccountKey = tuple[str, str]  # (account id, currency)


@dataclass(frozen=True, slots=True)
class Positions:
    holdings: dict[tuple[str, InstrumentId], Holding]
    cash: dict[AccountKey, Money]
    realized: dict[AccountKey, Money]  # realised net P&L, gain positive

    def holding(self, account_id: str, instrument_id: InstrumentId) -> Holding:
        return self.holdings.get(
            (account_id, instrument_id), Holding(Quantity(0), None)
        )


def materialize(
    events: Iterable[JournalEvent], effective_as_of: datetime | None = None
) -> Positions:
    """Fold events (optionally only those effective by `effective_as_of`). The fold
    reads postings only, so a reversal undoes its original exactly."""
    t = None if effective_as_of is None else ensure_aware_utc(effective_as_of)
    qty: dict[tuple[str, InstrumentId], Quantity] = {}
    cost: dict[tuple[str, InstrumentId], Money] = {}
    cash: dict[AccountKey, Money] = {}
    realized: dict[AccountKey, Money] = {}
    for ev in events:
        if t is not None and ev.effective_at > t:
            continue
        acct = ev.account_id
        for u in ev.units:
            if u.account is UnitAccount.POSITION:
                k = (acct, u.instrument_id)
                qty[k] = qty.get(k, Quantity(0)) + u.quantity
        for p in ev.money:
            cur = p.amount.currency
            if p.account is BookAccount.CASH:
                cash[(acct, cur)] = cash.get((acct, cur), Money.of(0, cur)) + p.amount
            elif p.account is BookAccount.REALIZED_PL:
                prior = realized.get((acct, cur), Money.of(0, cur))
                realized[(acct, cur)] = prior - p.amount
            elif p.account is BookAccount.SECURITY_COST and p.instrument_id is not None:
                k = (acct, p.instrument_id)
                cost[k] = cost.get(k, Money.of(0, cur)) + p.amount
    # A net-zero cost (e.g. a buy and its reversal) is no known cost, never zero.
    cost = {k: v for k, v in cost.items() if v.amount.value}
    holdings = {
        k: Holding(qty.get(k, Quantity(0)), cost.get(k))
        for k in qty.keys() | cost.keys()
        if qty.get(k, Quantity(0)).value or k in cost
    }
    return Positions(
        holdings,
        {k: v for k, v in cash.items() if v.amount.value},
        {k: v for k, v in realized.items() if v.amount.value},
    )
