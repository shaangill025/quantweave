"""Valuation, unrealized/realized P&L and NAV (T018 increment 1; F-03, F-06).

SYNTHETIC inputs only. Expected numbers come from numerical_oracles.json (NUM01, read
from the file) or are hand-computed in the comments. The P&L identity property checks
the journal and the valuation against each other, not against a copy of either.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import (
    FxRate,
    Money,
    Multiplier,
    PositiveQuantity,
    Price,
    Quantity,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError
from qw_domain.journal import Journal, materialize
from qw_domain.postings import Header, Holding, buy, deposit
from qw_domain.valuation import (
    FxQuote,
    Mark,
    MarkKind,
    PositionValuation,
    Unavailable,
    ValuationBasis,
    value_account,
    value_position,
)

PROFILE = settings(derandomize=True, database=None, max_examples=100, deadline=None)
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
SYN_CAD = InstrumentId(UUID("00000000-0000-4000-8000-00000000005c"))
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
AS_OF = T0 + timedelta(days=3)
DAY = timedelta(days=1)
ACCT = "SYN-ACCOUNT"
ONE = Multiplier(1)


def hdr(record: str, day: int = 0) -> Header:
    return Header(ACCT, T0 + timedelta(days=day), SourceRef("SYN-BROKER", record))


def mark(
    price: str, iid: InstrumentId = SYN, cur: str = "USD", at: datetime = AS_OF
) -> Mark:
    return Mark(iid, Price(price), cur, MarkKind.LAST, at, "SYN-FEED")


def cad_usd(rate: str = "0.731", at: datetime = AS_OF) -> FxQuote:
    return FxQuote("CAD", "USD", FxRate(rate), at, "SYN-FX")


def held(qty: str, cost: Money | None, iid: InstrumentId = SYN) -> Holding:
    return Holding(ACCT, iid, Quantity(qty), cost)


def unavailable(out: object) -> str:
    assert isinstance(out, Unavailable)
    return out.code


B = ValuationBasis("USD", AS_OF, DAY)


# ---- NUM01 through the journal: cash, cost, realized, unrealized, NAV


def num01_journal() -> Journal:
    inp = ORACLES["NUM01"]["inputs"]
    j = Journal()
    usd = Money.of
    j.post(deposit(hdr("D1"), usd(inp["deposit"], "USD")), T0)
    qty, px = PositiveQuantity(inp["buy_qty"]), Price(inp["buy_price"])
    j.post(buy(hdr("B1", 1), SYN, qty, px, usd(inp["buy_fee"], "USD")), T0)
    sell_qty, sell_px = PositiveQuantity(inp["sell_qty"]), Price(inp["sell_price"])
    j.sell(hdr("S1", 2), SYN, sell_qty, sell_px, usd(inp["sell_fee"], "USD"), T0)
    return j


def test_num01_nav_realized_unrealized_from_oracle() -> None:
    exp = ORACLES["NUM01"]["expected"]
    pos = materialize(num01_journal().events())
    m = mark(ORACLES["NUM01"]["inputs"]["mark"])
    out = value_account(pos, ACCT, {SYN: m}, {SYN: ONE}, {}, B)
    assert out.nav == Money.of(exp["nav"], "USD")
    assert out.coverage == "complete" and out.covered_value == out.nav
    assert out.realized == (Money.of(exp["realized"], "USD"),)
    (pv,) = (v for _, v in out.positions)
    assert isinstance(pv, PositionValuation)
    assert pv.unrealized_local == Money.of(exp["unrealized"], "USD")
    assert pv.unrealized_reporting == Money.of(exp["unrealized"], "USD")
    assert pv.mark_observed_at == AS_OF and pv.fx_observed_at is None
    gain = exp["realized"], exp["unrealized"]
    assert Decimal(gain[0]) + Decimal(gain[1]) == Decimal(exp["total_gain"])


def test_missing_mark_blocks_nav_and_labels_partial_coverage() -> None:
    pos = materialize(num01_journal().events())
    out = value_account(pos, ACCT, {}, {SYN: ONE}, {}, B)
    assert unavailable(out.nav) == "mark_missing"
    assert out.coverage == "partial"
    assert out.covered_value == Money.of("738", "USD")  # cash only, never 0 for SYN
    assert unavailable(dict(out.positions)[SYN]) == "mark_missing"


def test_missing_multiplier_blocks_and_has_no_default() -> None:
    pos = materialize(num01_journal().events())
    out = value_account(pos, ACCT, {SYN: mark("60")}, {}, {}, B)
    assert unavailable(out.nav) == "multiplier_missing"


def test_foreign_cash_without_fx_blocks_nav() -> None:
    j = num01_journal()
    j.post(deposit(hdr("D2"), Money.of("100", "CAD")), T0)
    pos = materialize(j.events())
    marks, mults = {SYN: mark("60")}, {SYN: ONE}
    out = value_account(pos, ACCT, marks, mults, {}, B)
    assert unavailable(out.nav) == "fx_missing"
    out = value_account(pos, ACCT, marks, mults, {"CAD": cad_usd()}, B)
    assert out.nav == Money.of("1171.1", "USD")  # 1098 + 100 * 0.731


def test_unknown_account_is_unavailable_not_zero() -> None:
    pos = materialize(num01_journal().events())
    out = value_account(pos, "SYN-OTHER", {SYN: mark("60")}, {SYN: ONE}, {}, B)
    assert unavailable(out.nav) == "account_unknown"
    assert out.coverage == "partial" and out.positions == () and out.cash == ()


def test_staleness_boundary_age_equal_to_max_age_is_fresh() -> None:
    # max_age is the oldest acceptable age: age == max_age passes, one tick more fails.
    h = held("1", None)
    assert isinstance(
        value_position(h, mark("1", at=AS_OF - DAY), ONE, B), PositionValuation
    )
    late = mark("1", at=AS_OF - DAY - AS_OF.resolution)
    assert unavailable(value_position(h, late, ONE, B)) == "mark_stale"


# ---- Single positions: FX, multipliers, rounding, staleness


def test_foreign_position_fx_and_historical_cost_convention() -> None:
    h = held("10", Money.of("240", "CAD"), SYN_CAD)
    m = mark("25.5", SYN_CAD, "CAD")
    out = value_position(h, m, ONE, B, fx=cad_usd())
    assert isinstance(out, PositionValuation)
    assert out.local == Money.of("255", "CAD")
    assert out.reporting == Money.of("186.405", "USD")  # 255 * 0.731
    assert out.unrealized_local == Money.of("15", "CAD")
    # Cost is never translated at today's rate: that would hide the currency effect.
    assert unavailable(out.unrealized_reporting) == "historical_fx_cost_unknown"
    cost_usd = Money.of("180", "USD")  # cost at the acquisition-date rate (SYNTHETIC)
    out = value_position(h, m, ONE, B, fx=cad_usd(), cost_reporting=cost_usd)
    assert isinstance(out, PositionValuation)
    assert out.unrealized_reporting == Money.of("6.405", "USD")
    assert out.fx_observed_at == AS_OF


def test_option_multiplier_is_explicit() -> None:
    h = held("2", Money.of("640", "USD"))
    out = value_position(h, mark("3.5"), Multiplier(100), B)
    assert isinstance(out, PositionValuation)
    assert out.local == Money.of("700", "USD") and out.unrealized_local == Money.of(
        "60", "USD"
    )


def test_reporting_value_rounds_down_at_scale_12() -> None:
    h = held("1", None, SYN_CAD)
    fx = cad_usd("0.123456789012345678")
    out = value_position(h, mark("1", SYN_CAD, "CAD"), ONE, B, fx=fx)
    assert isinstance(out, PositionValuation)
    assert out.reporting == Money.of("0.123456789012", "USD")  # floor, not ...013
    assert unavailable(out.unrealized_local) == "cost_unknown"  # unknown, not zero


@pytest.mark.parametrize(
    ("h", "m", "fx", "code"),
    [
        (held("5", None), None, None, "mark_missing"),
        (held("-5", None), mark("10"), None, "unsupported_short"),
        (held("5", None), mark("0"), None, "mark_not_positive"),
        (held("5", None), mark("10", at=AS_OF - 2 * DAY), None, "mark_stale"),
        (held("5", None), mark("10", at=AS_OF + DAY), None, "mark_after_as_of"),
        (held("5", None, SYN_CAD), mark("10", SYN_CAD, "CAD"), None, "fx_missing"),
        (
            held("5", None, SYN_CAD),
            mark("10", SYN_CAD, "CAD"),
            cad_usd(at=AS_OF - 2 * DAY),
            "fx_stale",
        ),
        (
            held("5", None, SYN_CAD),
            mark("10", SYN_CAD, "CAD"),
            cad_usd(at=AS_OF + DAY),
            "fx_after_as_of",
        ),
    ],
)
def test_blocked_inputs_are_unavailable(
    h: Holding, m: Mark | None, fx: FxQuote | None, code: str
) -> None:
    assert unavailable(value_position(h, m, ONE, B, fx=fx)) == code


def test_cost_in_another_currency_makes_unrealized_unavailable() -> None:
    out = value_position(held("1", Money.of("5", "CAD")), mark("6"), ONE, B)
    assert isinstance(out, PositionValuation)
    assert unavailable(out.unrealized_local) == "cost_currency_mismatch"


def test_input_rejections() -> None:
    naive = datetime(2026, 1, 5, 15)  # noqa: DTZ001 - the naive input under test
    with pytest.raises(InstantError):
        Mark(SYN, Price("1"), "USD", MarkKind.LAST, naive, "SYN-FEED")
    with pytest.raises(TypeError):
        Price(0.1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Mark(SYN, "1", "USD", MarkKind.LAST, AS_OF, "SYN-FEED")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="currency"):
        Mark(SYN, Price("1"), "usd", MarkKind.LAST, AS_OF, "SYN-FEED")
    with pytest.raises(ValueError, match="instrument"):
        value_position(held("1", None, SYN_CAD), mark("1"), ONE, B)
    with pytest.raises(ValueError, match="pair"):
        bad = FxQuote("EUR", "USD", FxRate("1.1"), AS_OF, "SYN-FX")
        value_position(
            held("1", None, SYN_CAD), mark("1", SYN_CAD, "CAD"), ONE, B, fx=bad
        )
    with pytest.raises(InstantError):
        ValuationBasis("USD", naive, DAY)
    with pytest.raises(ValueError, match="max_age"):
        ValuationBasis("USD", AS_OF, -DAY)


# ---- Property: realized + unrealized == NAV - contributions (F-03, single currency)

trades = st.lists(
    st.tuples(st.booleans(), st.integers(1, 50), st.integers(1, 20000)),
    min_size=1,
    max_size=8,
)


@PROFILE
@given(trades, st.integers(1, 20000))
def test_pnl_identity_against_journal(
    ops: list[tuple[bool, int, int]], px: int
) -> None:
    j = Journal()
    j.post(deposit(hdr("D"), Money.of("1000000", "USD")), T0)
    units = 0
    for i, (is_buy, qty, cents) in enumerate(ops):
        price, f = Price(Decimal(cents).scaleb(-2)), Money.of("0.37", "USD")
        if is_buy:
            q = PositiveQuantity(qty)
            j.post(buy(hdr(f"B{i}", 1), SYN, q, price, f), T0)
            units += qty
        elif units:
            q = PositiveQuantity(min(qty, units))
            j.sell(hdr(f"S{i}", 1), SYN, q, price, f, T0)
            units -= min(qty, units)
    pos = materialize(j.events())
    marks = {SYN: mark(str(Decimal(px).scaleb(-2)))}
    out = value_account(pos, ACCT, marks, {SYN: ONE}, {}, B)
    assert isinstance(out.nav, Money)
    realized = sum((r.amount.value for r in out.realized), Decimal(0))
    unrealized = Decimal(0)
    for _, pv in out.positions:
        assert isinstance(pv, PositionValuation)
        assert isinstance(pv.unrealized_local, Money)
        unrealized += pv.unrealized_local.amount.value
    assert realized + unrealized == out.nav.amount.value - Decimal("1000000")
