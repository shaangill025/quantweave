"""Balanced postings, observations and event constructors (T012 PR A; F-01..F-03).

SYNTHETIC inputs. Expected numbers come from numerical_oracles.json (NUM01, NUM02),
read from the file, or are hand-computed / exact `Fraction` oracles in the test.
"""

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import DecimalValueError, Money, PositiveQuantity, Price
from qw_domain.decimals import Quantity as Q
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError
from qw_domain.postings import (
    BookAccount,
    EventKind,
    Header,
    Holding,
    JournalError,
    JournalEvent,
    MoneyPosting,
    SourceObservation,
    UnitAccount,
    UnitPosting,
    buy,
    deposit,
    dividend,
    fee,
    interest,
    sell,
    split,
    withdrawal,
)

PROFILE = settings(derandomize=True, database=None, max_examples=200)
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
SYN2 = InstrumentId(UUID("00000000-0000-4000-8000-00000000005b"))
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
ACCT = "SYN-ACCOUNT"


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def hdr(record: str, account: str = ACCT) -> Header:
    return Header(account, T0, SourceRef("SYN-BROKER", record))


def held(
    qty: str | Decimal, cost: str | Decimal | None, iid: InstrumentId = SYN
) -> Holding:
    return Holding(ACCT, iid, Q(qty), None if cost is None else usd(cost))


def split_action(ratio: PositiveRatio, iid: InstrumentId = SYN) -> Split:
    return Split("ca-1", 1, iid, T0.date(), SourceRef("SYN-CA", "1"), T0, ratio)


def amounts(e: JournalEvent) -> dict[BookAccount, Decimal]:
    return {p.account: p.amount.amount.value for p in e.money}


def d(text: str) -> Decimal:
    return Decimal(text)


# ---- Oracles NUM01 and NUM02 on postings


def test_num01_postings_match_oracle() -> None:
    inp, exp = ORACLES["NUM01"]["inputs"], ORACLES["NUM01"]["expected"]
    b = buy(
        hdr("B1"),
        SYN,
        PositiveQuantity(inp["buy_qty"]),
        Price(inp["buy_price"]),
        usd(inp["buy_fee"]),
    )
    cost = d(inp["buy_qty"]) * d(inp["buy_price"]) + d(inp["buy_fee"])
    assert amounts(b) == {BookAccount.CASH: -cost, BookAccount.SECURITY_COST: cost}
    assert b.fee == usd(inp["buy_fee"])  # raw fee kept beside the postings
    s = sell(
        hdr("S1"),
        PositiveQuantity(inp["sell_qty"]),
        Price(inp["sell_price"]),
        usd(inp["sell_fee"]),
        held(inp["buy_qty"], cost),
    )
    net = d(inp["sell_qty"]) * d(inp["sell_price"]) - d(inp["sell_fee"])
    assert amounts(s) == {
        BookAccount.CASH: net,
        BookAccount.SECURITY_COST: d(exp["remaining_cost"]) - cost,
        BookAccount.REALIZED_PL: -d(exp["realized"]),
    }
    assert d(inp["deposit"]) - cost + net == d(exp["cash"])
    assert {u.quantity for u in s.units} == {-Q(inp["sell_qty"]), Q(inp["sell_qty"])}


def test_num02_split_postings_match_oracle() -> None:
    inp, exp = ORACLES["NUM02"]["inputs"], ORACLES["NUM02"]["expected"]
    ratio = PositiveRatio.from_decimal(inp["ratio"])
    e = split(
        hdr("SPL"), split_action(ratio), held(inp["quantity"], inp["economic_cost"])
    )
    assert e.money == ()  # total cost untouched
    pos = [u.quantity for u in e.units if u.account is UnitAccount.POSITION]
    assert pos == [Q(exp["quantity"]) - Q(inp["quantity"])]
    assert d(exp["cost_per_unit"]) * d(exp["quantity"]) == d(inp["economic_cost"])
    assert d(exp["new_price"]) * d(exp["quantity"]) == d(exp["value"])


def test_non_terminating_allocation_rounds_remaining_cost_up() -> None:
    """Cost 100 over 3 units, sold 1, 1, 1. Oracle: exact Fraction, ceil at 1e-12."""
    h = held("3", "100")
    remaining_seen = []
    for i in range(3):
        assert h.cost is not None
        e = sell(hdr(f"S{i}"), PositiveQuantity(1), Price(10), usd(1), h)
        allocated = -amounts(e)[BookAccount.SECURITY_COST]
        exact = Fraction(h.cost.amount.value) * (Fraction(h.quantity.value) - 1)
        exact /= Fraction(h.quantity.value)
        oracle = Fraction(math.ceil(exact * 10**12), 10**12)
        remaining = h.cost.amount.value - allocated
        assert Fraction(remaining) == oracle
        assert allocated + remaining == h.cost.amount.value
        assert amounts(e)[BookAccount.REALIZED_PL] == -(d("9") - allocated)
        remaining_seen.append(remaining)
        h = held(str(h.quantity.value - 1), remaining)
    assert remaining_seen == [d("66.666666666667"), d("33.333333333334"), d("0")]


# ---- Rejections (F-01, F-02, C-06)


def cash(v: str, cur: str = "USD") -> MoneyPosting:
    return MoneyPosting(BookAccount.CASH, Money.of(v, cur))


def ext(v: str, cur: str = "USD") -> MoneyPosting:
    return MoneyPosting(BookAccount.EXTERNAL_CAPITAL, Money.of(v, cur))


def ev(
    money: tuple[MoneyPosting, ...] = (), units: tuple[UnitPosting, ...] = ()
) -> JournalEvent:
    return JournalEvent(EventKind.DEPOSIT, "A", T0, SourceRef("S", "1"), money, units)


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda: ev((cash("100"), ext("-99"))), "money_unbalanced"),
        (lambda: ev((cash("100", "USD"), ext("-100", "CAD"))), "money_unbalanced"),
        (lambda: ev((cash("100"),)), "posting_count"),
        (lambda: ev(), "event_empty"),
        (lambda: cash("0"), "posting_zero"),
        (lambda: UnitPosting(UnitAccount.POSITION, SYN, Q("0")), "posting_zero"),
        (lambda: MoneyPosting(BookAccount.SECURITY_COST, usd(1)), "posting_instrument"),
        (
            lambda: ev(
                units=(
                    UnitPosting(UnitAccount.POSITION, SYN, Q("10")),
                    UnitPosting(UnitAccount.UNIT_CLEARING, SYN2, Q("-10")),
                )
            ),
            "units_unbalanced",
        ),
        (lambda: deposit(hdr("x"), usd(0)), "amount_not_positive"),
        (lambda: withdrawal(hdr("x"), usd(-5)), "amount_not_positive"),
        (
            lambda: buy(hdr("x"), SYN, PositiveQuantity(1), Price(1), usd(-1)),
            "fee_negative",
        ),
        (
            lambda: sell(
                hdr("x"), PositiveQuantity(6), Price(1), usd(0), held("5", "5")
            ),
            "short_sale_unsupported",
        ),
        (
            lambda: sell(
                hdr("x"), PositiveQuantity(1), Price(1), usd(0), held("5", None)
            ),
            "cost_unknown",
        ),
        (
            lambda: sell(
                hdr("x"),
                PositiveQuantity(1),
                Price(1),
                Money.of(0, "CAD"),
                held("5", "5"),
            ),
            "currency_mismatch",
        ),
        (
            lambda: sell(
                hdr("x", "OTHER"), PositiveQuantity(1), Price(1), usd(0), held("5", "5")
            ),
            "holding_mismatch",
        ),
        (
            lambda: split(
                hdr("x"), split_action(PositiveRatio(2, 1), SYN2), held("5", "5")
            ),
            "holding_mismatch",
        ),
        (
            lambda: split(hdr("x"), split_action(PositiveRatio(2, 1)), held("0", None)),
            "split_not_long",
        ),
        (
            lambda: split(
                hdr("x"), split_action(PositiveRatio(1, 3)), held("100", "1")
            ),
            "split_fraction",
        ),
    ],
)
def test_rejected_events(build: Callable[[], object], code: str) -> None:
    with pytest.raises(JournalError) as err:
        build()
    assert err.value.code == code


def test_type_and_scale_rejections() -> None:
    with pytest.raises(TypeError):  # ten shares are not fifty dollars
        MoneyPosting(BookAccount.CASH, Q("10"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        UnitPosting(UnitAccount.POSITION, SYN, usd(10))  # type: ignore[arg-type]
    with pytest.raises(DecimalValueError) as err:
        usd(Decimal("0.0000000000001"))
    assert err.value.code == "decimal_scale_exceeded"
    with pytest.raises(DecimalValueError):  # 12 + 12 fractional digits: never rounded
        buy(hdr("x"), SYN, PositiveQuantity("0.000000000001"), Price("0.5"), usd(0))
    naive = Header("A", datetime(2026, 1, 2), SourceRef("S", "1"))  # noqa: DTZ001
    with pytest.raises(InstantError):
        deposit(naive, usd(1))


def test_cash_only_events_and_reverse_split() -> None:
    assert amounts(interest(hdr("I"), usd("0.25")))[BookAccount.CASH] == d("0.25")
    assert amounts(fee(hdr("F"), usd("1.35"))) == {
        BookAccount.CASH: d("-1.35"),
        BookAccount.EXPENSE: d("1.35"),
    }
    div = dividend(hdr("V"), SYN, usd("3.10"))
    assert MoneyPosting(BookAccount.INCOME, usd("-3.10"), SYN) in div.money
    out = split(hdr("R"), split_action(PositiveRatio(1, 3)), held("99", "1"))
    assert {u.quantity for u in out.units} == {Q(-66), Q(66)}


# ---- Fingerprints and idempotency keys


def test_fingerprints_and_keys() -> None:
    obs = SourceObservation("A", SourceRef("S", "1"), T0, T0, {"x": "1"})
    later = SourceObservation(
        "A", SourceRef("S", "1"), T0 + timedelta(1), T0, {"x": "1"}
    )
    other = SourceObservation("A", SourceRef("S", "1"), T0, T0, {"x": "2"})
    assert later.fingerprint == obs.fingerprint != other.fingerprint
    with pytest.raises(TypeError):
        obs.payload["x"] = "5"  # type: ignore[index]
    a, b = deposit(hdr("D"), usd(10)), deposit(hdr("D"), usd(10))
    assert a.event_id == b.event_id != deposit(hdr("D"), usd(11)).event_id
    # A source named "reversal" cannot forge the key of a reversal.
    rev = a.reversal(SourceRef("SYN-CORR", "1"))
    forged = deposit(Header(ACCT, T0, SourceRef("reversal", a.event_id)), usd(1))
    assert rev.key != forged.key and len(rev.key) != len(forged.key)
    with pytest.raises(JournalError, match="reverse_reversal"):
        rev.reversal(SourceRef("S", "x"))


# ---- Properties


qtys = st.decimals(min_value=d("0.001"), max_value=d("500"), places=3)
money_values = st.decimals(min_value=d("0.01"), max_value=d("10000"), places=2)
RATIOS = [PositiveRatio(*r) for r in ((2, 1), (3, 2), (1, 2), (1, 10), (3, 1))]


@st.composite
def events(draw: st.DrawFn) -> JournalEvent:
    h = hdr(draw(st.sampled_from(["a", "b"])), draw(st.sampled_from([ACCT, "B"])))
    cur, a = draw(st.sampled_from(["USD", "CAD"])), draw(money_values)
    m, q, p = Money.of(a, cur), draw(qtys), Price(draw(money_values))
    h_ = Holding(h.account_id, SYN, Q(q), Money.of(draw(money_values), cur))
    kind = draw(st.sampled_from(range(8)))
    makers: list[Callable[[], JournalEvent]] = [
        lambda: deposit(h, m),
        lambda: withdrawal(h, m),
        lambda: fee(h, m),
        lambda: interest(h, m),
        lambda: dividend(h, SYN, m),
        lambda: buy(h, SYN, PositiveQuantity(q), p, m),
        lambda: sell(
            h, PositiveQuantity(draw(st.decimals(d("0.001"), q, places=3))), p, m, h_
        ),
        lambda: split(h, split_action(draw(st.sampled_from(RATIOS))), h_),
    ]
    return makers[kind]()


def commodity_totals(evs: list[JournalEvent]) -> dict[str, Decimal]:
    """Independent re-sum per currency and per instrument."""
    tot: dict[str, Decimal] = {}
    for e in evs:
        for m in e.money:
            k = m.amount.currency
            tot[k] = tot.get(k, d("0")) + m.amount.amount.value
        for u in e.units:
            k = u.instrument_id.to_wire()
            tot[k] = tot.get(k, d("0")) + u.quantity.value
    return tot


@PROFILE
@given(st.lists(events(), min_size=1, max_size=12))
def test_every_event_and_any_journal_balance(evs: list[JournalEvent]) -> None:
    for e in evs:
        assert set(commodity_totals([e]).values()) <= {d("0")}
    assert set(commodity_totals(evs).values()) <= {d("0")}


@PROFILE
@given(qtys, money_values, st.sampled_from(RATIOS))
def test_split_preserves_cost_and_value(
    q: Decimal, price: Decimal, r: PositiveRatio
) -> None:
    e = split(hdr("S"), split_action(r), held(q, "1"))
    assert e.money == ()
    new_q = q + sum(
        u.quantity.value for u in e.units if u.account is UnitAccount.POSITION
    )
    new_price = Fraction(price) * r.fraction().denominator / r.fraction().numerator
    assert Fraction(new_q) * new_price == Fraction(q) * Fraction(price)
