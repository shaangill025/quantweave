"""Corporate-action events and identity effects (T011; R004, R017, R089, F-14).

SYNTHETIC events. Expected quantities, cash and dates are hand-computed.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.corporate_actions import (
    CashDividend,
    CorporateAction,
    CorporateActionError,
    CorporateActionLog,
    Merger,
    PositiveRatio,
    SourceRef,
    SpinOff,
    Split,
    StockDividend,
    SymbolChange,
    adjust_option,
    apply_to_master,
)
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price
from qw_domain.identity import InstrumentId, Listing, Mic, Resolution, SecurityMaster
from qw_domain.identity import Ticker as T
from qw_domain.instants import InstantError
from qw_domain.options import (
    CashDeliverable,
    CashInLieu,
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
    UnknownDeliverable,
)

PROFILE = settings(derandomize=True, database=None, max_examples=300)
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
ACQ = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a2"))
NEW = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a3"))
CID = InstrumentId(UUID("00000000-0000-4000-8000-0000000000c1"))
XNAS = Mic("XNAS")
SRC = SourceRef("synthetic-feed", "rec-1")
KNOWN = datetime(2026, 10, 1, 12, tzinfo=UTC)
D = date


def head(**kw: Any) -> dict[str, Any]:
    base = {"event_id": "ca-1", "version": 1, "instrument_id": UND,
            "effective": D(2026, 11, 2), "source": SRC, "known_at": KNOWN}  # fmt: skip
    return base | kw


def contract(units: str = "100") -> OptionContract:
    return OptionContract(
        contract_id=CID,
        underlying_id=UND,
        expiry=SessionRef(CalendarId("XNYS"), D(2026, 12, 18)),
        strike=Price("150"),
        strike_currency="USD",
        right=OptionRight.CALL,
        style=ExerciseStyle.AMERICAN,
        settlement=Settlement.PHYSICAL,
        multiplier=Multiplier("100"),
        deliverables=(UnitDeliverable(UND, PositiveQuantity(units)),),
        adjusted=False,
        terms_verified=True,
        terms_version=1,
    )


def units(iid: InstrumentId, qty: str) -> UnitDeliverable:
    return UnitDeliverable(iid, PositiveQuantity(qty))


def test_ratio_is_exact_from_ints_or_decimal_strings() -> None:
    assert PositiveRatio(4, 2) == PositiveRatio(2, 1)  # stored in lowest terms
    assert PositiveRatio.from_decimal("1.5") == PositiveRatio(3, 2)
    assert PositiveRatio.from_decimal("0.1") == PositiveRatio(1, 10)
    assert PositiveRatio.from_decimal(Decimal("2.50")) == PositiveRatio(5, 2)
    assert PositiveRatio(3, 2).inverse() == PositiveRatio(2, 3)
    assert PositiveRatio(1, 10).to_wire() == "1:10"
    for n, d in ((0, 1), (1, 0), (-2, 1), (True, 1), (2.0, 1), ("2", 1)):
        with pytest.raises((TypeError, CorporateActionError)):
            PositiveRatio(n, d)  # type: ignore[arg-type]
    for text in ("0", "-1", "1e3", "abc", " 2", "NaN", "Infinity", "1.", "\uff12"):
        with pytest.raises(CorporateActionError):
            PositiveRatio.from_decimal(text)
    for bad in (1.5, Decimal("NaN"), Decimal("-1"), Decimal(0)):
        with pytest.raises((TypeError, CorporateActionError)):
            PositiveRatio.from_decimal(bad)  # type: ignore[arg-type]


def test_split_and_reverse_split() -> None:
    split = Split(**head(), ratio=PositiveRatio(2, 1))
    assert (
        not split.is_reverse and Split(**head(), ratio=PositiveRatio(1, 10)).is_reverse
    )
    with pytest.raises(CorporateActionError, match="1:1"):
        Split(**head(), ratio=PositiveRatio(3, 3))


def test_event_header_is_validated() -> None:
    est = timezone(timedelta(hours=-5))
    split = Split(**head(known_at=datetime(2026, 10, 1, 7, tzinfo=est)),
                  ratio=PositiveRatio(2, 1))  # fmt: skip
    assert split.known_at == KNOWN and split.known_at.tzinfo is UTC
    with pytest.raises(InstantError):
        Split(**head(known_at=datetime(2026, 10, 1)), ratio=PositiveRatio(2, 1))  # noqa: DTZ001
    bad: list[dict[str, Any]] = [
        {"effective": datetime(2026, 11, 2, tzinfo=UTC)}, {"version": 0},
        {"version": True}, {"event_id": ""}, {"event_id": "-x"},
        {"source": ("feed", "rec")},
    ]  # fmt: skip
    for change in bad:
        with pytest.raises((TypeError, CorporateActionError)):
            Split(**head(**change), ratio=PositiveRatio(2, 1))
    for source_id, record_id in (("", "r"), ("feed", ""), ("feed", "a\nb")):
        with pytest.raises(CorporateActionError):
            SourceRef(source_id, record_id)


def test_cash_dividend_dates_and_amount() -> None:
    usd = Money.of("0.24", "USD")
    # T+1 settlement: ex == record; pay later.
    div = CashDividend(**head(effective=D(2026, 11, 6)), amount_per_share=usd,
                       record_date=D(2026, 11, 6), pay_date=D(2026, 11, 20),
                       announced=D(2026, 10, 22))  # fmt: skip
    assert div.ex_date == D(2026, 11, 6) and div.currency == "USD"
    CashDividend(
        **head(effective=D(2026, 11, 5)),
        amount_per_share=usd,
        record_date=D(2026, 11, 6),
        pay_date=D(2026, 11, 20),
    )  # T+2 era
    CashDividend(**head(), amount_per_share=usd)  # dates not supplied stay None
    bad: list[dict[str, Any]] = [
        {"record_date": D(2026, 11, 1)},  # ex after record
        {
            "record_date": D(2026, 11, 2),
            "pay_date": D(2026, 11, 1),
        },  # pay before record
        {"pay_date": D(2026, 11, 1)},  # pay before ex
        {"announced": D(2026, 11, 3)},  # announced after ex
        {"amount_per_share": Money.of("0", "USD")},
        {"amount_per_share": Money.of("-1", "USD")},
        {"record_date": datetime(2026, 11, 2, tzinfo=UTC)},
        {"special": 1},
    ]
    for change in bad:
        with pytest.raises((TypeError, CorporateActionError)):
            CashDividend(**(head() | {"amount_per_share": usd} | change))
    # Due-bill special distribution: ex-date is the business day after pay.
    special = CashDividend(
        **head(effective=D(2026, 11, 2)),
        special=True,
        amount_per_share=Money.of("12", "USD"),
        record_date=D(2026, 10, 15),
        pay_date=D(2026, 10, 30),
    )
    assert special.ex_date > special.pay_date  # type: ignore[operator]


def test_merger_and_spinoff_terms_known_or_unknown() -> None:
    Merger(**head(), status="terms_unknown")
    Merger(
        **head(),
        status="terms_known",
        acquirer_id=ACQ,
        units_per_share=PositiveRatio(1, 2),
        cash_per_share=Money.of("10", "USD"),
    )
    Merger(**head(), status="terms_known", cash_per_share=Money.of("10", "USD"))
    SpinOff(**head(), status="terms_unknown")
    SpinOff(**head(), status="terms_known", spun_off_id=NEW,
            units_per_share=PositiveRatio(1, 3))  # fmt: skip
    bad: list[tuple[type[Merger | SpinOff], dict[str, Any]]] = [
        (Merger, {"status": "pending"}),
        (Merger, {"status": "terms_known"}),  # no terms
        (Merger, {"status": "terms_known", "units_per_share": PositiveRatio(1, 2)}),
        (Merger, {"status": "terms_known", "acquirer_id": UND,
                  "units_per_share": PositiveRatio(1, 2)}),
        (Merger, {"status": "terms_unknown", "acquirer_id": ACQ}),
        (Merger, {"status": "terms_known", "cash_per_share": Money.of("0", "USD")}),
        (SpinOff, {"status": "terms_known", "spun_off_id": NEW}),
        (SpinOff, {"status": "terms_known", "spun_off_id": UND,
                   "units_per_share": PositiveRatio(1, 3)}),
        (SpinOff, {"status": "terms_unknown", "units_per_share": PositiveRatio(1, 3)}),
    ]  # fmt: skip
    for kind, change in bad:
        with pytest.raises(CorporateActionError):
            kind(**head(**change))


def test_symbol_change_rejects_same_ticker() -> None:
    with pytest.raises(CorporateActionError):
        SymbolChange(**head(), mic=XNAS, old_ticker=T("FB"), new_ticker=T("FB"))


def test_corrections_supersede_without_mutation() -> None:
    log = CorporateActionLog()
    v1 = Split(**head(), ratio=PositiveRatio(2, 1))
    v2 = Split(**head(version=2, known_at=KNOWN + timedelta(days=2),
                      source=SourceRef("synthetic-feed", "rec-2")),
               ratio=PositiveRatio(3, 2))  # fmt: skip
    log.record(v1)
    log.record(v2)
    assert log.history("ca-1") == (v1, v2)
    assert v1.ratio == PositiveRatio(2, 1)  # the superseded version is unchanged
    assert log.current("ca-1") == v2
    assert log.current("ca-1", KNOWN + timedelta(days=1)) == v1  # point in time
    assert log.current("ca-1", KNOWN - timedelta(seconds=1)) is None
    assert log.current("other") is None
    assert log.events(KNOWN + timedelta(days=1)) == (v1,)
    rejected: list[CorporateAction] = [
        replace(v2, version=4, known_at=KNOWN + timedelta(days=3)),  # gap
        replace(v2, version=3),  # known_at not after v2
        Merger(**head(version=3, known_at=KNOWN + timedelta(days=3)),
               status="terms_unknown"),  # kind changed
        replace(v1, event_id="ca-2", version=2),  # first version must be 1
    ]  # fmt: skip
    for event in rejected:
        with pytest.raises(CorporateActionError):
            log.record(event)
    assert log.history("ca-1") == (v1, v2) and log.history("ca-2") == ()


def test_symbol_change_creates_listing_interval_and_split_keeps_identity() -> None:
    master = SecurityMaster()
    master.add_listing(Listing(UND, XNAS, T("FB"), D(2012, 5, 18)))
    change = SymbolChange(**head(effective=D(2022, 6, 9)), mic=XNAS,
                          old_ticker=T("FB"), new_ticker=T("META"))  # fmt: skip
    after = apply_to_master(master, change)
    assert after.resolve(T("FB"), XNAS, D(2022, 6, 8)) == Resolution.found(UND)
    assert after.resolve(T("FB"), XNAS, D(2022, 6, 9)).status == "unknown"
    assert after.resolve(T("META"), XNAS, D(2022, 6, 9)) == Resolution.found(UND)
    assert master.resolve(T("META"), XNAS, D(2022, 6, 9)).status == "unknown"  # pure
    wrong = replace(change, old_ticker=T("XYZ"))
    with pytest.raises(CorporateActionError, match="not listed"):
        apply_to_master(master, wrong)
    split = Split(**head(effective=D(2022, 7, 1)), ratio=PositiveRatio(20, 1))
    assert apply_to_master(after, split).listings() == after.listings()


def _adjusted(c: OptionContract, deliverables: tuple[object, ...]) -> None:
    assert c.deliverables == deliverables
    assert c.adjusted and not c.terms_verified and c.terms_version == 2
    assert not c.sizing_allowed and "terms_unverified" in c.sizing_block_reasons()
    assert (c.strike, c.multiplier) == (Price("150"), Multiplier("100"))


@pytest.mark.parametrize(
    ("qty", "ratio", "expected"),
    [
        ("100", PositiveRatio(2, 1), (units(UND, "200"),)),
        ("100", PositiveRatio(3, 2), (units(UND, "150"),)),
        ("75", PositiveRatio(3, 2),
         (units(UND, "112"), CashInLieu(UND, Fraction(1, 2), "ca-1"))),  # 112.5
        ("100", PositiveRatio(1, 10), (units(UND, "10"),)),
        ("100", PositiveRatio(1, 3),
         (units(UND, "33"), CashInLieu(UND, Fraction(1, 3), "ca-1"))),  # 33 1/3
        ("1", PositiveRatio(1, 4), (CashInLieu(UND, Fraction(1, 4), "ca-1"),)),
    ],
)  # fmt: skip
def test_split_adjusts_deliverable(
    qty: str, ratio: PositiveRatio, expected: tuple[object, ...]
) -> None:
    adjusted = adjust_option(contract(qty), Split(**head(), ratio=ratio))
    _adjusted(adjusted, expected)
    if any(isinstance(d, CashInLieu) for d in expected):
        assert "cash_in_lieu_unknown" in adjusted.sizing_block_reasons()
        with pytest.raises(ValueError, match="verified"):
            replace(adjusted, terms_verified=True)  # F-14: cannot be verified as-is


def test_other_actions_adjust_deliverables() -> None:
    c = contract()
    usd = Money.of("10", "USD")
    stock = StockDividend(**head(), rate=PositiveRatio(1, 20))  # 5%
    _adjusted(adjust_option(c, stock), (units(UND, "105"),))
    special = CashDividend(**head(), amount_per_share=Money.of("2.50", "USD"),
                           special=True)  # fmt: skip
    _adjusted(adjust_option(c, special),
              (units(UND, "100"), CashDeliverable(Money.of("250", "USD"))))  # fmt: skip
    merger = Merger(
        **head(),
        status="terms_known",
        acquirer_id=ACQ,
        units_per_share=PositiveRatio(1, 2),
        cash_per_share=usd,
    )
    _adjusted(adjust_option(c, merger),
              (units(ACQ, "50"), CashDeliverable(Money.of("1000", "USD"))))  # fmt: skip
    spin = SpinOff(**head(), status="terms_known", spun_off_id=NEW,
                   units_per_share=PositiveRatio(1, 3))  # fmt: skip
    _adjusted(adjust_option(c, spin), (units(UND, "100"), units(NEW, "33"),
              CashInLieu(NEW, Fraction(1, 3), "ca-1")))  # fmt: skip
    unknown = UnknownDeliverable("ca-1")
    for event, expected in (
        (Merger(**head(), status="terms_unknown"), (unknown,)),  # target converts
        (SpinOff(**head(), status="terms_unknown"), (units(UND, "100"), unknown)),
    ):
        out = adjust_option(c, event)
        _adjusted(out, expected)
        assert "deliverable_terms_unknown" in out.sizing_block_reasons()
    # A cash merger term adds to an existing cash component of the same currency.
    with_cash = replace(c, deliverables=(*c.deliverables, CashDeliverable(usd)))
    out = adjust_option(with_cash, replace(merger, units_per_share=None,
                                           acquirer_id=None))  # fmt: skip
    assert out.deliverables == (CashDeliverable(Money.of("1010", "USD")),)


def test_unaffected_contracts_are_returned_unchanged() -> None:
    c = contract()
    ordinary = CashDividend(**head(), amount_per_share=Money.of("0.24", "USD"))
    rename = SymbolChange(**head(), mic=XNAS, old_ticker=T("AB"), new_ticker=T("CD"))
    other = Split(**head(instrument_id=ACQ), ratio=PositiveRatio(2, 1))
    for event in (ordinary, rename, other):
        assert adjust_option(c, event) is c


def test_underlying_action_with_unknown_deliverables_stays_blocked() -> None:
    c = replace(contract(), deliverables=(), terms_verified=False)
    out = adjust_option(c, Split(**head(), ratio=PositiveRatio(2, 1)))
    assert out.deliverables == () and out.adjusted and out.terms_version == 2
    assert "deliverables_unknown" in out.sizing_block_reasons()


ratios = st.tuples(st.integers(1, 50), st.integers(1, 50)).filter(
    lambda nd: nd[0] != nd[1]
)


@PROFILE
@given(ratios, st.integers(1, 10_000))
def test_property_split_then_inverse_restores_deliverable(
    nd: tuple[int, int], k: int
) -> None:
    ratio = PositiveRatio(*nd)
    # k * old units split into k * new whole units; no cash in lieu either way.
    start = contract(str(k * ratio.denominator))
    split = Split(**head(), ratio=ratio)
    inverse = Split(**head(event_id="ca-2"), ratio=ratio.inverse())
    once = adjust_option(start, split)
    assert once.deliverables == (units(UND, str(k * ratio.numerator)),)
    back = adjust_option(once, inverse)
    assert back.deliverables == start.deliverables
    assert back.terms_version == 3 and not back.terms_verified


@PROFILE
@given(ratios, st.integers(1, 10**8))
def test_property_split_conserves_entitlement(nd: tuple[int, int], cents: int) -> None:
    qty = Decimal(cents).scaleb(-2)
    out = adjust_option(contract(str(qty)), Split(**head(), ratio=PositiveRatio(*nd)))
    whole = sum(
        (Fraction(d.quantity.value) for d in out.deliverables
         if isinstance(d, UnitDeliverable)), Fraction(0))  # fmt: skip
    rest = [d.units for d in out.deliverables if isinstance(d, CashInLieu)]
    assert whole.denominator == 1 and all(0 < r < 1 for r in rest)
    assert whole + sum(rest, Fraction(0)) == Fraction(qty) * nd[0] / nd[1]
