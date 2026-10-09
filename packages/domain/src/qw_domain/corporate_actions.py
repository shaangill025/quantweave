"""Corporate-action events and their identity effects (T011; R004, R017, R089, F-14).

Events are immutable and versioned: a correction is a new version of the same
`event_id` with a later `known_at` (aware UTC, when we learned it), and earlier versions
are kept unchanged in a `CorporateActionLog`. Ratios are exact rationals. Unknown merger
or spin-off terms are declared (`status="terms_unknown"`), never guessed.

Effects here are pure: `apply_to_master` returns a new `SecurityMaster` (a symbol
change adds the new listing interval; nothing else changes identity) and
`adjust_option` returns a new, adjusted and unverified `OptionContract` version.
Each event id is absorbed at most once (`applied_actions`); a correction is absorbed
by `replay_option` from the base terms, never stacked on the superseded version.
Ledger postings, unit adjustments and quarantine of unknown actions belong to T012.

Recorded limitation: ordinary cash dividends never adjust option terms, and whether a
dividend is `special` is supplied by the source. A mislabelled extraordinary dividend
therefore leaves a verified contract unchanged; detecting it is T012 quarantine work.

Deliverable arithmetic is per original contract: units are multiplied exactly, whole
units stay deliverable and a fractional remainder becomes `CashInLieu` with an unknown
amount. The exchange may instead adjust strike and contract count; the verified terms
replace these computed ones (F-14).
"""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Literal, Self

from qw_domain.decimals import DOMAIN_CONTEXT, Money, PositiveQuantity, safe_repr
from qw_domain.identity import InstrumentId, Mic, SecurityMaster, Ticker
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.options import (
    CashDeliverable,
    CashInLieu,
    Deliverable,
    OptionContract,
    UnitDeliverable,
    UnknownDeliverable,
    component_key,
)


class CorporateActionError(ValueError):
    """A malformed corporate action or a rejected correction or effect."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)  # ledger_event ids
_RECORD = re.compile(r"[\x20-\x7e]{1,256}", re.ASCII)
_DECIMAL = re.compile(r"[0-9]{1,18}(\.[0-9]{1,18})?", re.ASCII)
_MAX_TERM = 10**18


def _check_id(value: object, what: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise CorporateActionError(f"{what} {safe_repr(value)} is malformed")


@dataclass(frozen=True, slots=True)
class PositiveRatio:
    """An exact ratio `numerator:denominator` of positive ints, kept in lowest terms.
    For a split it is new units per old unit: 2:1 forward, 1:10 reverse. Terms are
    bounded by 10**18 after reduction, which keeps them printable and storable."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise TypeError("ratio terms must be ints")
        if self.numerator < 1 or self.denominator < 1:
            raise CorporateActionError(f"ratio {self.to_wire()} must be positive")
        exact = Fraction(self.numerator, self.denominator)
        if max(exact.numerator, exact.denominator) > _MAX_TERM:
            raise CorporateActionError("ratio terms exceed 10**18")
        object.__setattr__(self, "numerator", exact.numerator)
        object.__setattr__(self, "denominator", exact.denominator)

    @classmethod
    def from_decimal(cls, value: str | Decimal) -> Self:
        """From a decimal string such as "1.5" or a finite positive `Decimal`."""
        if isinstance(value, str):
            if _DECIMAL.fullmatch(value) is None:
                raise CorporateActionError(f"ratio {safe_repr(value)} is malformed")
            value = Decimal(value)
        if type(value) is not Decimal:
            raise TypeError(f"ratio must be str or Decimal: {type(value).__name__}")
        if not value.is_finite() or value <= 0:
            raise CorporateActionError(f"ratio {safe_repr(value)} must be positive")
        try:  # e.g. the int digit limit for huge exponents
            return cls(*value.as_integer_ratio())
        except (ValueError, OverflowError) as exc:
            if isinstance(exc, CorporateActionError):
                raise
            raise CorporateActionError(f"ratio {safe_repr(value)}: {exc}") from None

    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def inverse(self) -> Self:
        return type(self)(self.denominator, self.numerator)

    def to_wire(self) -> str:
        return f"{self.numerator}:{self.denominator}"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where the event came from: a source id and that source's record id."""

    source_id: str
    record_id: str

    def __post_init__(self) -> None:
        _check_id(self.source_id, "source id")
        if not isinstance(self.record_id, str) or not _RECORD.fullmatch(self.record_id):
            raise CorporateActionError(f"record id {safe_repr(self.record_id)}")


@dataclass(frozen=True, slots=True)
class _Action:
    """Common header. `effective` is the ex/effective local date."""

    event_id: str
    version: int
    instrument_id: InstrumentId
    effective: date
    source: SourceRef
    known_at: datetime

    def __post_init__(self) -> None:
        _check_id(self.event_id, "event id")
        if type(self.version) is not int or self.version < 1:
            raise CorporateActionError("version must be an integer >= 1")
        if type(self.instrument_id) is not InstrumentId:
            raise TypeError("instrument_id must be an InstrumentId")
        if type(self.source) is not SourceRef:
            raise TypeError("source must be a SourceRef")
        require_date(self.effective, "effective date")
        object.__setattr__(self, "known_at", ensure_aware_utc(self.known_at))
        self._validate()

    def _validate(self) -> None:
        return


@dataclass(frozen=True, slots=True)
class Split(_Action):
    ratio: PositiveRatio

    def _validate(self) -> None:
        if type(self.ratio) is not PositiveRatio or self.ratio.numerator == (
            self.ratio.denominator
        ):
            raise CorporateActionError("a split needs a ratio other than 1:1")

    @property
    def is_reverse(self) -> bool:
        return self.ratio.numerator < self.ratio.denominator


@dataclass(frozen=True, slots=True)
class StockDividend(_Action):
    """`rate` additional units per unit held: 1:20 is a 5% stock dividend."""

    rate: PositiveRatio

    def _validate(self) -> None:
        if type(self.rate) is not PositiveRatio:
            raise TypeError("rate must be a PositiveRatio")


@dataclass(frozen=True, slots=True)
class SymbolChange(_Action):
    mic: Mic
    old_ticker: Ticker
    new_ticker: Ticker

    def _validate(self) -> None:
        if self.old_ticker == self.new_ticker:
            raise CorporateActionError("a symbol change needs a different ticker")


def _positive_money(value: object, what: str) -> None:
    if type(value) is not Money or value.amount.value <= 0:
        raise CorporateActionError(f"{what} must be positive Money")


@dataclass(frozen=True, slots=True)
class CashDividend(_Action):
    """Per-share cash; `effective` is the ex-date. Dates are checked where supplied:
    announced <= ex <= record <= pay. A `special` distribution may go ex after pay
    (due-bill) and is not held to ex <= record; it adjusts option deliverables."""

    amount_per_share: Money
    record_date: date | None = None
    pay_date: date | None = None
    announced: date | None = None
    special: bool = False

    def _validate(self) -> None:
        _positive_money(self.amount_per_share, "dividend amount")
        if type(self.special) is not bool:
            raise TypeError("special must be a bool")
        record, pay, announced = self.record_date, self.pay_date, self.announced
        for value, what in ((record, "record"), (pay, "pay"), (announced, "announced")):
            if value is not None:
                require_date(value, f"{what} date")
        order = [(record, pay)]
        if announced is not None:
            order += [(announced, self.effective), (announced, record)]
        if not self.special:
            order += [(self.effective, record), (self.effective, pay)]
        if any(a is not None and b is not None and b < a for a, b in order):
            raise CorporateActionError(f"dividend dates out of order: {self!r}")

    @property
    def ex_date(self) -> date:
        return self.effective

    @property
    def currency(self) -> str:
        return self.amount_per_share.currency


type TermsStatus = Literal["terms_known", "terms_unknown"]


def _check_terms(status: object, known: bool, any_term: bool) -> None:
    if status not in ("terms_known", "terms_unknown"):
        raise CorporateActionError(f"terms status {safe_repr(status)}")
    if (status == "terms_known") != known or (status == "terms_unknown" and any_term):
        raise CorporateActionError(f"terms do not match status {status}")


@dataclass(frozen=True, slots=True)
class Merger(_Action):
    """The target (`instrument_id`) converts into `units_per_share` of `acquirer_id`
    and/or `cash_per_share`."""

    status: TermsStatus
    acquirer_id: InstrumentId | None = None
    units_per_share: PositiveRatio | None = None
    cash_per_share: Money | None = None

    def _validate(self) -> None:
        units, acquirer, cash = (
            self.units_per_share,
            self.acquirer_id,
            self.cash_per_share,
        )
        if cash is not None:
            _positive_money(cash, "merger cash")
        if (units is None) != (acquirer is None) or acquirer == self.instrument_id:
            raise CorporateActionError("merger units need a distinct acquirer")
        terms = units is not None or cash is not None
        _check_terms(self.status, terms, terms or acquirer is not None)


@dataclass(frozen=True, slots=True)
class SpinOff(_Action):
    """Holders keep `instrument_id` and receive `units_per_share` of `spun_off_id`."""

    status: TermsStatus
    spun_off_id: InstrumentId | None = None
    units_per_share: PositiveRatio | None = None

    def _validate(self) -> None:
        new, units = self.spun_off_id, self.units_per_share
        if new == self.instrument_id:
            raise CorporateActionError("a spin-off creates a different instrument")
        both = new is not None and units is not None
        _check_terms(self.status, both, new is not None or units is not None)


type CorporateAction = (
    Split | StockDividend | SymbolChange | CashDividend | Merger | SpinOff
)


class CorporateActionLog:
    """Append-only versions per event id. Version n+1 must keep the event kind and
    instrument and be known strictly later than version n; nothing recorded is ever
    replaced. A correction may change the effective date and terms (both stay visible
    in `history`). It may not move the event to another instrument: effects already
    derived for the first instrument would silently lose their cause, so a wrong
    instrument is corrected by a new event id."""

    def __init__(self) -> None:
        self._versions: dict[str, tuple[CorporateAction, ...]] = {}

    def record(self, event: CorporateAction) -> None:
        history = self._versions.get(event.event_id, ())
        if event.version != len(history) + 1:
            raise CorporateActionError(f"expected version {len(history) + 1}")
        if history and type(event) is not type(history[-1]):
            raise CorporateActionError("a correction cannot change the event kind")
        if history and event.instrument_id != history[-1].instrument_id:
            raise CorporateActionError("a correction cannot change the instrument")
        if history and event.known_at <= history[-1].known_at:
            raise CorporateActionError("a correction must be known later")
        self._versions[event.event_id] = (*history, event)

    def history(self, event_id: str) -> tuple[CorporateAction, ...]:
        return self._versions.get(event_id, ())

    def current(
        self, event_id: str, known_as_of: datetime | None = None
    ) -> CorporateAction | None:
        """The latest version known at `known_as_of` (default: now in the log)."""
        cutoff = None if known_as_of is None else ensure_aware_utc(known_as_of)
        known = [
            e for e in self.history(event_id) if cutoff is None or e.known_at <= cutoff
        ]
        return known[-1] if known else None

    def events(
        self, known_as_of: datetime | None = None
    ) -> tuple[CorporateAction, ...]:
        found = (self.current(i, known_as_of) for i in self._versions)
        return tuple(e for e in found if e is not None)


def apply_to_master(master: SecurityMaster, event: CorporateAction) -> SecurityMaster:
    """A new master with the event's identity effect; the input is not changed.
    Only a symbol change alters identity data. Listings of instruments created by a
    merger or spin-off are added explicitly with their own source."""
    out = master.copy()
    if isinstance(event, SymbolChange):
        day_before = event.effective - timedelta(days=1)
        current = master.ticker_as_of(event.instrument_id, event.mic, day_before)
        if current != event.old_ticker:
            raise CorporateActionError(
                f"{event.old_ticker.symbol} not listed for the instrument {day_before}"
            )
        out.rename(event.instrument_id, event.mic, event.new_ticker, event.effective)
    return out


def _units(iid: InstrumentId, exact: Fraction, event_id: str) -> list[Deliverable]:
    whole = exact.numerator // exact.denominator
    out: list[Deliverable] = []
    if whole:
        out.append(UnitDeliverable(iid, PositiveQuantity(whole)))
    if exact != whole:
        out.append(CashInLieu(iid, exact - whole, event_id))
    return out


def _cash(per_share: Money, held: PositiveQuantity) -> CashDeliverable:
    with localcontext(DOMAIN_CONTEXT):
        return CashDeliverable(
            Money.of(per_share.amount.value * held.value, per_share.currency)
        )


def _replacement(d: UnitDeliverable, event: CorporateAction) -> list[Deliverable]:
    held, eid = Fraction(d.quantity.value), event.event_id
    match event:
        case Split(ratio=ratio):
            return _units(d.instrument_id, held * ratio.fraction(), eid)
        case StockDividend(rate=rate):
            return _units(d.instrument_id, held * (1 + rate.fraction()), eid)
        case CashDividend(special=True):
            return [d, _cash(event.amount_per_share, d.quantity)]
        case Merger(status="terms_unknown") | SpinOff(status="terms_unknown"):
            keep: list[Deliverable] = [d] if isinstance(event, SpinOff) else []
            return [*keep, UnknownDeliverable(eid)]
        case Merger(acquirer_id=acquirer, units_per_share=units, cash_per_share=cash):
            out: list[Deliverable] = []
            if acquirer is not None and units is not None:
                out += _units(acquirer, held * units.fraction(), eid)
            return out + ([] if cash is None else [_cash(cash, d.quantity)])
        case SpinOff(spun_off_id=InstrumentId() as new, units_per_share=units) if (
            units is not None
        ):
            return [d, *_units(new, held * units.fraction(), eid)]
    return [d]


def _merged(items: list[Deliverable]) -> tuple[Deliverable, ...]:
    """Sum units of one instrument and cash of one currency, keeping first order."""
    out: dict[tuple[object, ...], Deliverable] = {}
    for d in items:
        key = component_key(d)
        prior = out.get(key)
        if isinstance(prior, UnitDeliverable) and isinstance(d, UnitDeliverable):
            d = UnitDeliverable(d.instrument_id, prior.quantity + d.quantity)
        elif isinstance(prior, CashDeliverable) and isinstance(d, CashDeliverable):
            d = CashDeliverable(prior.amount + d.amount)
        out[key] = d
    return tuple(out.values())


def _affects(contract: OptionContract, event: CorporateAction) -> bool:
    if isinstance(event, SymbolChange) or (
        isinstance(event, CashDividend) and not event.special
    ):
        return False  # identity and ordinary dividends leave terms unchanged (see top)
    held = {
        d.instrument_id for d in contract.deliverables if isinstance(d, UnitDeliverable)
    }
    return event.instrument_id in held | {contract.underlying_id}


def adjust_option(contract: OptionContract, event: CorporateAction) -> OptionContract:
    """The next terms version after `event`, adjusted and unverified, so it stays
    blocked from live sizing until verified (F-14); `contract` itself if unaffected.
    An event id already in `applied_actions` is rejected, whatever its version."""
    if any(event.event_id == applied for applied, _ in contract.applied_actions):
        raise CorporateActionError(
            f"{event.event_id} already applied; replay corrections from base terms"
        )
    if not _affects(contract, event):
        return contract
    items: list[Deliverable] = []
    for d in contract.deliverables:
        if isinstance(d, UnitDeliverable) and d.instrument_id == event.instrument_id:
            items += _replacement(d, event)
        else:
            items.append(d)
    return replace(
        contract,
        deliverables=_merged(items),
        adjusted=True,
        terms_verified=False,
        terms_version=contract.terms_version + 1,
        applied_actions=(*contract.applied_actions, (event.event_id, event.version)),
    )


# Same-day precedence (a convention, not an exchange rule; verified terms decide):
# splits, stock dividends, cash, spin-offs, mergers, then symbol changes. Flooring
# makes a split and a stock dividend non-commutative, so the event id breaks ties
# only within one kind.
_PRECEDENCE = (Split, StockDividend, CashDividend, SpinOff, Merger, SymbolChange)


def replay_option(
    base: OptionContract,
    log: CorporateActionLog,
    known_as_of: datetime | None = None,
    effective_as_of: date | None = None,
) -> OptionContract:
    """Rebuild terms from `base` (terms before any logged action) with the current
    version of each logged event known at `known_as_of`, ordered by effective date,
    same-day precedence by kind and event id. This is how a correction replaces the
    version it supersedes.

    Caller contract: only actions effective during the contract's life apply. Events
    effective after `effective_as_of` (announced but not yet effective) or after the
    expiry session date are skipped; `base` must be the terms in force before the
    earliest logged action."""
    if base.applied_actions:
        raise CorporateActionError("replay starts from base terms with no actions")
    last = base.expiry.session_date
    if effective_as_of is not None:
        last = min(last, require_date(effective_as_of, "effective_as_of"))
    events = [e for e in log.events(known_as_of) if e.effective <= last]
    out = base
    for event in sorted(
        events, key=lambda e: (e.effective, _PRECEDENCE.index(type(e)), e.event_id)
    ):
        out = adjust_option(out, event)
    return out
