"""Option position lifecycle, expiry expectations and early-assignment flags (T034;
spec 09 "Lifecycle and collateral state"). Analysis and journal events only.

States change only on observed or user-reported events, never on an expectation:
random assignment is not inferred. A multi-contract position can be closed,
exercised, assigned or (after expiry) expire worthless in parts, from any state
while contracts remain open. Exercised or assigned contracts stay encumbered until
the broker reconciliation event; collateral is released only on close, worthless
expiry or reconciliation. A conflict moves a live position to `exception`; only an
`exception_resolved` event naming an actor and a reference leaves it, back to the
prior state with quantities unchanged, so nothing is released while in exception.
Spec 09 defines no conflict after a terminal state, so none is accepted. An event id
applies at most once: an identical replay is a no-op, a different payload raises.
Times never go back. Nothing here can submit, exercise or cancel a broker position.
Marks must be fresh (`check_mark`): at or before `as_of` and within `max_age`.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from fractions import Fraction

from qw_domain.decimals import Money, Price, Quantity
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.option_risk import check_mark, contract_terms, expiry_intrinsic
from qw_domain.options import ExerciseStyle, OptionContract, OptionRight
from qw_domain.valuation import Mark, Unavailable


class State(StrEnum):
    OPEN = "open"
    PARTIALLY_CLOSED = "partially_closed"
    EXPIRATION_PENDING = "expiration_pending"
    EXERCISE_REPORTED = "exercise_reported"
    ASSIGNMENT_REPORTED = "assignment_reported"
    SETTLEMENT_PENDING = "settlement_pending"
    RECONCILED = "reconciled"
    EXCEPTION = "exception"
    CLOSED = "closed"
    EXPIRED = "expired"


class Event(StrEnum):
    CLOSE_REPORTED = "close_reported"
    EXPIRY_REACHED = "expiry_reached"
    EXERCISE_REPORTED = "exercise_reported"  # long side
    ASSIGNMENT_REPORTED = "assignment_reported"  # short side
    EXPIRED_WORTHLESS_REPORTED = "expired_worthless_reported"
    SETTLEMENT_REPORTED = "settlement_reported"
    BROKER_RECONCILED = "broker_reconciled"
    CONFLICT_REPORTED = "conflict_reported"
    EXCEPTION_RESOLVED = "exception_resolved"


_TERMINAL = {State.CLOSED, State.EXPIRED, State.RECONCILED}
_REPORTED = {State.EXERCISE_REPORTED, State.ASSIGNMENT_REPORTED}
_COUNTED = {Event.CLOSE_REPORTED, Event.EXERCISE_REPORTED, Event.ASSIGNMENT_REPORTED}


class LifecycleError(ValueError):
    """A transition the state machine does not allow."""


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    event_id: str
    kind: Event
    observed_at: datetime
    source: str  # e.g. broker_import, user_report
    quantity: Quantity | None = None  # contracts; required for counted events
    actor: str | None = None  # required to resolve an exception
    reference: str | None = None  # evidence for the resolution

    def __post_init__(self) -> None:
        ensure_aware_utc(self.observed_at)
        if not self.event_id or not self.source:
            raise ValueError("event id and source are required")
        if self.quantity is not None and type(self.quantity) is not Quantity:
            raise TypeError("quantity must be Quantity")
        has_qty = self.quantity is not None and self.quantity.value > 0
        if (self.kind in _COUNTED) != has_qty:
            raise ValueError(f"{self.kind} quantity must be given iff counted")
        if self.kind is Event.EXCEPTION_RESOLVED and not (
            self.actor and self.reference
        ):
            raise ValueError("a resolution needs an actor and a reference")


@dataclass(frozen=True, slots=True)
class Position:
    contract: OptionContract
    long: bool
    open_quantity: Fraction  # contracts still live
    pending_quantity: Fraction = Fraction()  # exercised/assigned, not reconciled
    state: State = State.OPEN
    expiry_reached: bool = False
    prior_state: State | None = None  # the state an exception returns to
    applied: tuple[LifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        for q in (self.open_quantity, self.pending_quantity):
            if type(q) is not Fraction or q < 0 or q.denominator != 1:
                raise ValueError("quantities must be whole non-negative Fractions")

    @property
    def encumbered_quantity(self) -> Fraction:
        """Contracts whose collateral or premium exposure is still held."""
        return self.open_quantity + self.pending_quantity

    def _rest(self, open_qty: Fraction, done: State) -> State:
        """The state once nothing is pending: `done` when no contract is open."""
        if self.pending_quantity:
            return self.state
        if not open_qty:
            return done
        return (
            State.EXPIRATION_PENDING if self.expiry_reached else State.PARTIALLY_CLOSED
        )

    def apply(self, e: LifecycleEvent) -> "Position":
        for a in self.applied:
            if a.event_id == e.event_id:
                if a != e:
                    raise LifecycleError("event id reused with a different payload")
                return self
        if self.applied and e.observed_at < self.applied[-1].observed_at:
            raise LifecycleError("event observed before the last applied event")
        if not _GUARDS[e.kind](self):
            raise LifecycleError(f"{e.kind} not allowed from {self.state}")
        if e.kind is Event.EXERCISE_REPORTED and not self.long:
            raise LifecycleError("a short position is assigned, not exercised")
        if e.kind is Event.ASSIGNMENT_REPORTED and self.long:
            raise LifecycleError("a long position is exercised, not assigned")
        q = Fraction(e.quantity.value) if e.quantity is not None else Fraction()
        if q > self.open_quantity:
            raise LifecycleError("reported quantity exceeds the open quantity")
        nxt = replace(self, applied=(*self.applied, e))
        left = self.open_quantity - q
        match e.kind:
            case Event.CLOSE_REPORTED:
                return replace(
                    nxt, open_quantity=left, state=self._rest(left, State.CLOSED)
                )
            case Event.EXERCISE_REPORTED | Event.ASSIGNMENT_REPORTED:
                return replace(
                    nxt,
                    open_quantity=left,
                    pending_quantity=self.pending_quantity + q,
                    state=State(e.kind.value),
                )
            case Event.EXPIRED_WORTHLESS_REPORTED:
                done = self._rest(Fraction(), State.EXPIRED)
                return replace(nxt, open_quantity=Fraction(), state=done)
            case Event.BROKER_RECONCILED:
                idle = replace(nxt, pending_quantity=Fraction())
                return replace(idle, state=idle._rest(left, State.RECONCILED))
            case Event.EXPIRY_REACHED:
                state = (
                    self.state if self.pending_quantity else State.EXPIRATION_PENDING
                )
                return replace(nxt, expiry_reached=True, state=state)
            case Event.SETTLEMENT_REPORTED:
                return replace(nxt, state=State.SETTLEMENT_PENDING)
            case Event.CONFLICT_REPORTED:
                return replace(nxt, state=State.EXCEPTION, prior_state=self.state)
            case Event.EXCEPTION_RESOLVED:
                assert self.prior_state is not None
                return replace(nxt, state=self.prior_state, prior_state=None)


def _normal(p: Position) -> bool:
    return p.state is not State.EXCEPTION


# Transition guards: which positions accept each event.
_GUARDS: dict[Event, Callable[[Position], bool]] = {
    Event.CLOSE_REPORTED: lambda p: _normal(p) and p.open_quantity > 0,
    Event.EXERCISE_REPORTED: lambda p: _normal(p) and p.open_quantity > 0,
    Event.ASSIGNMENT_REPORTED: lambda p: _normal(p) and p.open_quantity > 0,
    Event.EXPIRY_REACHED: lambda p: (
        _normal(p) and p.open_quantity > 0 and not p.expiry_reached
    ),
    Event.EXPIRED_WORTHLESS_REPORTED: lambda p: (
        _normal(p) and p.open_quantity > 0 and p.expiry_reached
    ),
    Event.SETTLEMENT_REPORTED: lambda p: p.state in _REPORTED,
    Event.BROKER_RECONCILED: lambda p: p.state is State.SETTLEMENT_PENDING,
    Event.CONFLICT_REPORTED: lambda p: _normal(p) and p.state not in _TERMINAL,
    Event.EXCEPTION_RESOLVED: lambda p: p.state is State.EXCEPTION,
}


class Expectation(StrEnum):
    WORTHLESS = "expect_worthless"
    EXERCISE = "expect_exercise"
    ASSIGNMENT = "expect_assignment"
    UNCERTAIN = "uncertain"  # in the money below the auto-exercise threshold


@dataclass(frozen=True, slots=True)
class ExpiryView:
    expectation: Expectation
    units: Fraction  # underlying units to the position holder if it happens
    cash: Fraction  # strike-currency cash to the holder (+ in, - out)


def expiry_expectation(
    contract: OptionContract,
    contracts: Quantity,  # + long, - short
    settlement: Mark,  # the underlying's settlement price
    auto_exercise_min: Price | None,  # caller's rule: in-the-money per unit
    as_of: datetime,
    max_age: timedelta,
) -> ExpiryView | Unavailable:
    """In the money by at least the threshold (>=) counts as exercise/assignment."""
    terms = contract_terms(contract)
    if isinstance(terms, Unavailable):
        return terms
    if auto_exercise_min is None:
        return Unavailable("auto_exercise_rule_missing", "no auto-exercise rule")
    if auto_exercise_min.value <= 0:
        return Unavailable("auto_exercise_rule_invalid", "threshold must be > 0")
    und, cur = contract.underlying_id, terms.currency
    if stale := check_mark(settlement, und, cur, as_of, max_age, "settlement"):
        return stale
    n = Fraction(contracts.value)
    call = contract.right is OptionRight.CALL
    itm = Fraction(settlement.price.value) - terms.effective_strike
    itm = itm if call else -itm
    if itm <= 0:
        return ExpiryView(Expectation.WORTHLESS, Fraction(), Fraction())
    side = 1 if call else -1  # a call holder receives the deliverable
    units = side * terms.units * n
    cash = side * (terms.cash - terms.strike_cash) * n
    if itm < Fraction(auto_exercise_min.value):
        return ExpiryView(Expectation.UNCERTAIN, units, cash)
    kind = Expectation.EXERCISE if n > 0 else Expectation.ASSIGNMENT
    return ExpiryView(kind, units, cash)


@dataclass(frozen=True, slots=True)
class ExDividend:
    """The underlying's next declared ex-dividend date and cash per unit."""

    ex_date: date
    per_unit: Money

    def __post_init__(self) -> None:
        require_date(self.ex_date, "ex_date")


def early_assignment_flags(
    contract: OptionContract,
    contracts: Quantity,
    spot: Mark,  # the underlying
    option_mark: Mark,  # the contract, quoted per unit; per contract is price * M
    dividend: ExDividend | None,
    at: datetime,
    max_age: timedelta,
) -> tuple[str, ...] | Unavailable:
    """Risk flags for a short American contract; an assignment is never inferred.
    A dividend counts only if it goes ex after `at` (UTC date) and on or before the
    expiry session. Deep in-the-money puts get `no_extrinsic_value` only; no
    interest-rate test is modelled."""
    if contracts.value >= 0 or contract.style is not ExerciseStyle.AMERICAN:
        return ()
    terms = contract_terms(contract)
    if isinstance(terms, Unavailable):
        return terms
    assert contract.multiplier is not None  # implied by contract_terms
    cur = terms.currency
    if dividend is not None and dividend.per_unit.currency != cur:
        return Unavailable("currency_mismatch", "dividend not in strike currency")
    for m, inst, what in (
        (spot, contract.underlying_id, "spot"),
        (option_mark, contract.contract_id, "option_mark"),
    ):
        if stale := check_mark(m, inst, cur, at, max_age, what):
            return stale
    option_price = Fraction(option_mark.price.value) * Fraction(
        contract.multiplier.value
    )
    intrinsic = expiry_intrinsic(contract, spot.price)
    if isinstance(intrinsic, Unavailable):
        return intrinsic
    if intrinsic.amount.value <= 0:
        return ()
    extrinsic = option_price - Fraction(intrinsic.amount.value)
    flags = ["short_in_the_money"]
    if extrinsic <= 0:
        flags.append("no_extrinsic_value")
    if (
        contract.right is OptionRight.CALL
        and dividend is not None
        and ensure_aware_utc(at).date() < dividend.ex_date
        and dividend.ex_date <= contract.expiry.session_date
        and Fraction(dividend.per_unit.amount.value) * terms.units >= extrinsic
    ):
        flags.append("dividend_exceeds_extrinsic")
    return tuple(flags)
