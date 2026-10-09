"""Option contract identity and OCC symbols (T011; R017, R089, F-14). SYNTHETIC."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price
from qw_domain.identity import IdentityError, InstrumentId
from qw_domain.options import (
    CashDeliverable,
    ExerciseStyle,
    OccSymbol,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)

PROFILE = settings(derandomize=True, database=None, max_examples=300)
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
CID = InstrumentId(UUID("00000000-0000-4000-8000-0000000000c1"))
EXPIRY = SessionRef(CalendarId("XNYS"), date(2026, 11, 20))
STANDARD = OptionContract(
    contract_id=CID,
    underlying_id=UND,
    expiry=EXPIRY,
    strike=Price("150"),
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


def test_adjusted_contract_with_unverified_terms_blocks_sizing() -> None:
    assert STANDARD.sizing_block_reasons() == () and STANDARD.sizing_allowed
    # SYNTHETIC adjusted contract after a 3-for-2 split: 150 shares plus cash.
    adjusted = replace(
        STANDARD,
        deliverables=(
            UnitDeliverable(UND, PositiveQuantity("150")),
            CashDeliverable(Money.of("12.50", "USD")),
        ),
        adjusted=True,
        terms_verified=False,
        terms_version=2,
    )
    assert adjusted.sizing_block_reasons() == ("terms_unverified",)
    assert not adjusted.sizing_allowed
    verified = replace(adjusted, terms_verified=True)
    assert verified.sizing_allowed  # explicit, verified adjusted terms are usable


def test_unknown_terms_are_represented_but_blocked() -> None:
    unknown = replace(
        STANDARD,
        multiplier=None,
        deliverables=(),
        style=ExerciseStyle.UNKNOWN,
        settlement=Settlement.UNKNOWN,
        adjusted=True,
        terms_verified=False,
    )
    assert unknown.multiplier is None  # never defaulted to 100
    assert unknown.sizing_block_reasons() == (
        "terms_unverified",
        "multiplier_unknown",
        "deliverables_unknown",
        "exercise_style_unknown",
        "settlement_unknown",
    )


INCOMPLETE: list[dict[str, Any]] = [
    {"multiplier": None}, {"deliverables": ()}, {"style": ExerciseStyle.UNKNOWN},
    {"settlement": Settlement.UNKNOWN},
]  # fmt: skip
INVALID: list[dict[str, Any]] = [
    {"strike_currency": "usd"}, {"terms_version": 0}, {"strike": Price("0")},
    {"deliverables": (UnitDeliverable(CID, PositiveQuantity("1")),)},  # itself
]  # fmt: skip


def test_verified_terms_must_be_complete_and_valid() -> None:
    for change in INCOMPLETE:
        with pytest.raises(ValueError, match="verified"):
            replace(STANDARD, **change)
    for change in INVALID:
        with pytest.raises(ValueError):
            replace(STANDARD, **change)


@pytest.mark.parametrize(
    ("text", "root", "expiry", "right", "strike"),
    [
        ("AAPL  261120C00150000", "AAPL", date(2026, 11, 20), OptionRight.CALL, "150"),
        ("SPY   270115P00412500", "SPY", date(2027, 1, 15), OptionRight.PUT, "412.5"),
        ("BRKB1 261218C00000500", "BRKB1", date(2026, 12, 18), OptionRight.CALL, "0.5"),
        ("ABCDEF261120P99999999", "ABCDEF", date(2026, 11, 20), OptionRight.PUT,
         "99999.999"),
    ],
)  # fmt: skip
def test_occ_parse_and_format(
    text: str, root: str, expiry: date, right: OptionRight, strike: str
) -> None:
    symbol = OccSymbol.parse(text)
    assert symbol == OccSymbol(root, expiry, right, Price(strike))
    assert symbol.format() == text


@pytest.mark.parametrize(
    "bad",
    [
        "AAPL 261120C00150000", "AAPL  261120X00150000", "AAPL  261131C00150000",
        "AAPL  261120C0015000", "AAPL  261120C00000000", "aapl  261120C00150000",
        "AAPL  261120C00150000\n", "      261120C00150000", "AA PL 261120C00150000",
        "AAPL  2611\uff120C00150000",
    ],
)  # fmt: skip
def test_occ_rejects_malformed(bad: str) -> None:
    with pytest.raises(IdentityError):
        OccSymbol.parse(bad)


def test_occ_rejects_unrepresentable_strike() -> None:
    for strike in ("0.0005", "100000"):
        with pytest.raises(IdentityError):
            OccSymbol("AAPL", date(2026, 11, 20), OptionRight.CALL, Price(strike))
    with pytest.raises(IdentityError):
        OccSymbol("AAPL", date(2100, 1, 1), OptionRight.CALL, Price("1"))


def test_symbol_does_not_determine_deliverable() -> None:
    # An adjusted root ("BRKB1") parses, but the symbol carries no multiplier or
    # deliverable: those come only from explicit contract terms.
    symbol = OccSymbol.parse("BRKB1 261218C00000500")
    assert not hasattr(symbol, "multiplier") and not hasattr(symbol, "deliverables")


roots = st.text("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=1, max_size=6)
strikes = st.integers(1, 99_999_999).map(lambda m: Price(Decimal(m).scaleb(-3)))


@PROFILE
@given(
    roots,
    st.dates(date(2000, 1, 1), date(2099, 12, 31)),
    st.sampled_from(OptionRight),
    strikes,
)
def test_property_occ_round_trip(
    root: str, expiry: date, right: OptionRight, strike: Price
) -> None:
    symbol = OccSymbol(root, expiry, right, strike)
    text = symbol.format()
    assert len(text) == 21
    assert OccSymbol.parse(text) == symbol
    assert OccSymbol.parse(text).format() == text
