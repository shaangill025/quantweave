"""Option lifecycle, expiry expectations and early-assignment flags (T034; R017,
R089; spec 09). SYNTHETIC contracts; hand-computed oracles. Analysis only."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from uuid import UUID

import pytest
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError
from qw_domain.option_lifecycle import (
    Event,
    ExDividend,
    Expectation,
    ExpiryView,
    LifecycleError,
    LifecycleEvent,
    Position,
    State,
    early_assignment_flags,
    expiry_expectation,
)
from qw_domain.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.valuation import Mark, MarkKind, Unavailable

UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
CALL = OptionContract(
    contract_id=InstrumentId(UUID("00000000-0000-4000-8000-0000000000c1")),
    underlying_id=UND,
    expiry=SessionRef(CalendarId("XNYS"), date(2026, 11, 20)),
    strike=Price("50"),
    strike_currency="USD",
    right=OptionRight.CALL,
    style=ExerciseStyle.AMERICAN,
    settlement=Settlement.PHYSICAL,
    multiplier=Multiplier("100"),
    deliverables=(UnitDeliverable(UND, PositiveQuantity("100")),),
    adjusted=False,
    terms_verified=True,
    terms_version=1,
)
PUT = replace(CALL, right=OptionRight.PUT)
T0 = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
_seq = iter(range(1, 1000))


def ev(kind: Event, qty: int | None = None, minutes: int = 0) -> LifecycleEvent:
    q = None if qty is None else Quantity(qty)
    at = T0 + timedelta(minutes=minutes)
    return LifecycleEvent(f"e{next(_seq)}", kind, at, "user_report", q)


def test_short_put_early_assignment_then_expiry() -> None:
    p = Position(PUT, long=False, open_quantity=Fraction(2))
    p = p.apply(ev(Event.ASSIGNMENT_REPORTED, 1, 1))
    assert p.state is State.ASSIGNMENT_REPORTED and p.encumbered_quantity == 2
    p = p.apply(ev(Event.SETTLEMENT_REPORTED, minutes=2))
    assert p.state is State.SETTLEMENT_PENDING and p.encumbered_quantity == 2
    p = p.apply(ev(Event.BROKER_RECONCILED, minutes=3))
    assert (p.state, p.open_quantity, p.encumbered_quantity) == (
        State.PARTIALLY_CLOSED,
        Fraction(1),
        Fraction(1),
    )
    p = p.apply(ev(Event.EXPIRY_REACHED, minutes=4))
    assert p.state is State.EXPIRATION_PENDING and p.encumbered_quantity == 1
    p = p.apply(ev(Event.EXPIRED_WORTHLESS_REPORTED, minutes=5))
    assert p.state is State.EXPIRED and p.encumbered_quantity == 0  # released


def test_close_releases_and_replays_are_idempotent() -> None:
    p = Position(CALL, long=True, open_quantity=Fraction(3))
    first = ev(Event.CLOSE_REPORTED, 2, 1)
    p = p.apply(first)
    assert p.state is State.PARTIALLY_CLOSED and p.encumbered_quantity == 1
    assert p.apply(first) is p  # same event id: no double close
    p = p.apply(ev(Event.CLOSE_REPORTED, 1, 2))
    assert p.state is State.CLOSED and p.encumbered_quantity == 0
    conflict = Position(CALL, True, Fraction(1)).apply(ev(Event.CONFLICT_REPORTED))
    assert conflict.state is State.EXCEPTION


def test_invalid_transitions_and_inputs() -> None:
    short = Position(PUT, long=False, open_quantity=Fraction(1))
    long = Position(CALL, long=True, open_quantity=Fraction(1))
    bad = [
        (short, ev(Event.EXERCISE_REPORTED, 1)),
        (long, ev(Event.ASSIGNMENT_REPORTED, 1)),
        (long, ev(Event.EXPIRED_WORTHLESS_REPORTED)),  # needs expiry reached
        (long, ev(Event.BROKER_RECONCILED)),
        (long, ev(Event.CLOSE_REPORTED, 2)),  # more than open
    ]
    for pos, e in bad:
        with pytest.raises(LifecycleError):
            pos.apply(e)
    later = long.apply(ev(Event.EXPIRY_REACHED, minutes=10))
    with pytest.raises(LifecycleError, match="before"):
        later.apply(ev(Event.EXERCISE_REPORTED, 1, minutes=5))
    with pytest.raises(InstantError):
        LifecycleEvent("x", Event.EXPIRY_REACHED, datetime(2026, 1, 1), "s")  # noqa: DTZ001
    with pytest.raises(ValueError, match="iff counted"):
        LifecycleEvent("x", Event.CLOSE_REPORTED, T0, "s")
    with pytest.raises(ValueError, match="whole"):
        Position(CALL, True, Fraction(1, 2))


AGE = timedelta(minutes=15)


def mark(price: str, inst: InstrumentId = UND, at: datetime = T0) -> Mark:
    return Mark(inst, Price(price), "USD", MarkKind.MID, at, "SYNTHETIC")


def test_expiry_expectation() -> None:
    rule = Price("0.01")
    q1, qm1 = Quantity(1), Quantity(-1)

    def view(c: OptionContract, q: Quantity, s: str, r: Price | None = rule) -> object:
        return expiry_expectation(c, q, mark(s), r, T0, AGE)

    worthless = ExpiryView(Expectation.WORTHLESS, Fraction(), Fraction())
    assert view(CALL, q1, "50") == worthless  # at the money
    assert view(CALL, q1, "60") == ExpiryView(
        Expectation.EXERCISE, Fraction(100), Fraction(-5000)
    )
    # Short put assigned at 40: receives 100 units, pays 5000.
    assert view(PUT, qm1, "40") == ExpiryView(
        Expectation.ASSIGNMENT, Fraction(100), Fraction(-5000)
    )
    near = view(CALL, q1, "50.005")
    assert isinstance(near, ExpiryView) and near.expectation is Expectation.UNCERTAIN
    at_rule = view(CALL, q1, "50.01")  # exactly the threshold counts as exercised
    assert (
        isinstance(at_rule, ExpiryView) and at_rule.expectation is Expectation.EXERCISE
    )
    for r, code in (
        (None, "auto_exercise_rule_missing"),
        (Price("0"), "auto_exercise_rule_invalid"),
    ):
        got = view(CALL, q1, "60", r)
        assert isinstance(got, Unavailable) and got.code == code
    unknown = replace(CALL, multiplier=None, terms_verified=False)
    got = view(unknown, q1, "60")
    assert isinstance(got, Unavailable) and "multiplier_unknown" in got.reason
    stale = expiry_expectation(CALL, q1, mark("60", at=T0 - AGE * 2), rule, T0, AGE)
    assert isinstance(stale, Unavailable) and stale.code == "settlement_stale"


def usd(x: str) -> Money:
    return Money.of(x, "USD")


def flags(
    c: OptionContract, q: int, spot: str, premium: str, div: ExDividend | None
) -> object:
    # Option marks are quoted per unit; per contract is the quote times M (100).
    opt_mark = mark(premium, c.contract_id)
    return early_assignment_flags(c, Quantity(q), mark(spot), opt_mark, div, T0, AGE)


def test_early_assignment_flags() -> None:
    before = ExDividend(date(2026, 11, 13), usd("0.50"))
    # ITM by 2 (200 per contract), mark 2.30 (230): extrinsic 30; dividend 50.
    assert flags(CALL, -1, "52", "2.30", before) == (
        "short_in_the_money",
        "dividend_exceeds_extrinsic",
    )
    after = ExDividend(date(2026, 11, 23), usd("0.50"))  # after expiry
    past = ExDividend(date(2026, 10, 30), usd("0.50"))  # already ex before `at`
    for div in (None, after, past):
        assert flags(CALL, -1, "52", "2.30", div) == ("short_in_the_money",)
    # A deep in-the-money put only gets the no-extrinsic flag.
    assert flags(PUT, -1, "45", "5", None) == (
        "short_in_the_money",
        "no_extrinsic_value",
    )
    assert flags(CALL, -1, "40", "0.01", before) == ()
    assert flags(CALL, 1, "60", "0.01", None) == ()
    assert flags(replace(CALL, style=ExerciseStyle.EUROPEAN), -1, "60", "1", None) == ()
    cad_div = ExDividend(date(2026, 11, 13), Money.of("0.5", "CAD"))
    r = flags(CALL, -1, "52", "2.30", cad_div)
    assert isinstance(r, Unavailable) and r.code == "currency_mismatch"
    old = mark("2.30", CALL.contract_id, T0 - AGE * 2)
    r = early_assignment_flags(CALL, Quantity(-1), mark("52"), old, None, T0, AGE)
    assert isinstance(r, Unavailable) and r.code == "option_mark_stale"
    r = early_assignment_flags(
        CALL, Quantity(-1), mark("52"), mark("2.3"), None, T0, AGE
    )
    assert isinstance(r, Unavailable) and r.code == "mark_instrument_mismatch"


def test_event_quantity_type_and_terminal_states() -> None:
    with pytest.raises(TypeError):
        LifecycleEvent("x", Event.CLOSE_REPORTED, T0, "s", 1.0)  # type: ignore[arg-type]
    closed = Position(CALL, True, Fraction(1)).apply(ev(Event.CLOSE_REPORTED, 1))
    for kind, qty in (
        (Event.EXPIRY_REACHED, None),
        (Event.CLOSE_REPORTED, 1),
        (Event.CONFLICT_REPORTED, None),  # spec 09 gives no conflict after a close
    ):
        with pytest.raises(LifecycleError):
            closed.apply(ev(kind, qty, minutes=1))


def run(p: Position, *steps: tuple[Event, int | None]) -> Position:
    for i, (kind, qty) in enumerate(steps, start=len(p.applied) + 1):
        p = p.apply(ev(kind, qty, minutes=i))
    return p


A, S, R = Event.ASSIGNMENT_REPORTED, Event.SETTLEMENT_REPORTED, Event.BROKER_RECONCILED


def test_multi_contract_sequences() -> None:
    three = Position(PUT, long=False, open_quantity=Fraction(3))
    p = run(three, (A, 1), (A, 1))  # two assignments in a row
    assert (p.open_quantity, p.pending_quantity) == (Fraction(1), Fraction(2))
    p = run(three, (A, 1), (Event.CLOSE_REPORTED, 2))  # assign 1, close 2
    assert p.open_quantity == 0 and p.encumbered_quantity == 1
    assert run(p, (S, None), (R, None)).state is State.RECONCILED
    p = run(three, (A, 1), (S, None), (A, 1))  # assign, settle, assign again
    assert p.state is State.ASSIGNMENT_REPORTED and p.pending_quantity == 2
    p = run(three, (Event.EXPIRY_REACHED, None), (A, 1))
    p = run(p, (Event.EXPIRED_WORTHLESS_REPORTED, None))  # the rest expire
    assert p.open_quantity == 0 and p.encumbered_quantity == 1  # 1 still settling
    assert run(p, (S, None), (R, None)).state is State.RECONCILED
    with pytest.raises(LifecycleError, match="exceeds"):
        run(three, (A, 2), (A, 2))


def test_exception_resolution_and_replay() -> None:
    p = run(Position(PUT, False, Fraction(2)), (A, 1), (Event.CONFLICT_REPORTED, None))
    assert p.state is State.EXCEPTION and p.encumbered_quantity == 2
    for kind, qty in ((S, None), (R, None), (Event.CLOSE_REPORTED, 1)):
        with pytest.raises(LifecycleError):
            p.apply(ev(kind, qty, minutes=9))  # only a resolution leaves exception
    with pytest.raises(ValueError, match="actor"):
        LifecycleEvent("r", Event.EXCEPTION_RESOLVED, T0, "user_report")
    fix = LifecycleEvent(
        "r",
        Event.EXCEPTION_RESOLVED,
        T0 + timedelta(minutes=9),
        "user_report",
        actor="principal-1",
        reference="broker-statement-2026-11-02",
    )
    back = p.apply(fix)
    assert back.state is State.ASSIGNMENT_REPORTED and back.encumbered_quantity == 2
    first = ev(Event.CLOSE_REPORTED, 1, minutes=1)
    q = Position(CALL, True, Fraction(2)).apply(first)
    assert q.apply(first) is q
    with pytest.raises(LifecycleError, match="payload"):
        q.apply(replace(first, quantity=Quantity(2)))
