"""Option package catalogue: builders, whole-package expiry payoff, bounds and
breakevens (T035; R017, R089). SYNTHETIC contracts. Oracles: numerical_oracles.json
NUM08-NUM15 and hand-computed payoffs at and around the strikes (M=100)."""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.option_packages import (
    Direction,
    OptionPackage,
    PackageKind,
    Side,
    StockLeg,
    build_package,
)
from qw_domain.option_risk import (
    AccountCover,
    Leg,
    Structure,
    VerticalBounds,
    vertical_bounds,
)
from qw_domain.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.valuation import Unavailable

ORACLES = {
    o["id"]: o
    for o in json.loads(
        (
            Path(__file__).parents[3]
            / "docs/spec/tests/fixtures/numerical_oracles.json"
        ).read_text()
    )["oracles"]
}
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
OTHER = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a2"))
C, P, K = OptionRight.CALL, OptionRight.PUT, PackageKind
EXPIRY = SessionRef(CalendarId("XNYS"), date(2026, 11, 20))


def opt(
    right: OptionRight, strike: str, n: int = 1, mult: str = "100"
) -> OptionContract:
    return OptionContract(
        contract_id=InstrumentId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        underlying_id=UND,
        expiry=EXPIRY,
        strike=Price(strike),
        strike_currency="USD",
        right=right,
        style=ExerciseStyle.AMERICAN,
        settlement=Settlement.PHYSICAL,
        multiplier=Multiplier(mult),
        deliverables=(UnitDeliverable(UND, PositiveQuantity(mult)),),
        adjusted=False,
        terms_verified=True,
        terms_version=1,
    )


def usd(x: str | int | Decimal) -> Money:
    return Money.of(x, "USD")


def leg(c: OptionContract, q: int, premium: str | int | Decimal) -> Leg:
    return Leg(c, Quantity(q), usd(premium))


def account(cash: str = "100000", units: str = "0") -> AccountCover:
    return AccountCover("USD", usd(cash), {UND: Quantity(units)}, frozenset(Structure))


SHARES = StockLeg(UND, PositiveQuantity("100"), usd("4800"))  # 100 @ 48
# kind -> legs; the vertical premiums net to a debit or credit of 2 per unit.
CASES: dict[PackageKind, list[Leg]] = {
    K.LONG_CALL: [leg(opt(C, "50"), 1, -300)],
    K.LONG_PUT: [leg(opt(P, "50"), 1, -300)],
    K.COVERED_CALL: [leg(opt(C, "50"), -1, 200)],
    K.CASH_SECURED_PUT: [leg(opt(P, "50"), -1, 200)],
    K.BULL_CALL_DEBIT: [leg(opt(C, "50"), 1, -350), leg(opt(C, "55", 2), -1, 150)],
    K.BEAR_PUT_DEBIT: [leg(opt(P, "50"), 1, -350), leg(opt(P, "45", 2), -1, 150)],
    K.BULL_PUT_CREDIT: [leg(opt(P, "50"), -1, 350), leg(opt(P, "45", 2), 1, -150)],
    K.BEAR_CALL_CREDIT: [leg(opt(C, "50"), -1, 350), leg(opt(C, "55", 2), 1, -150)],
}
# kind -> (direction/side, max profit (None: unbounded), max loss, breakeven,
# {terminal price: expiry P&L}), all hand-computed.
HAND = {
    K.LONG_CALL: ("+D", None, 300, "53", {40: -300, 50: -300, 53: 0, 60: 700}),
    K.LONG_PUT: ("-D", 4700, 300, "47", {0: 4700, 40: 700, 47: 0, 50: -300}),
    K.COVERED_CALL: ("+C", 400, 4600, "46", {0: -4600, 46: 0, 48: 200, 60: 400}),
    K.CASH_SECURED_PUT: ("+C", 200, 4800, "48", {0: -4800, 40: -800, 48: 0, 70: 200}),
    K.BULL_CALL_DEBIT: ("+D", 300, 200, "52", {45: -200, 50: -200, 52: 0, 60: 300}),
    K.BEAR_PUT_DEBIT: ("-D", 300, 200, "48", {40: 300, 47: 100, 48: 0, 50: -200}),
    K.BULL_PUT_CREDIT: ("+C", 200, 300, "48", {45: -300, 47: -100, 48: 0, 50: 200}),
    K.BEAR_CALL_CREDIT: ("-C", 200, 300, "52", {50: 200, 52: 0, 54: -200, 60: -300}),
}
SIDES = {
    "+": Direction.BULLISH,
    "-": Direction.BEARISH,
    "D": Side.DEBIT,
    "C": Side.CREDIT,
}


def build(
    kind: PackageKind, legs: list[Leg], fees: str = "0", **kw: object
) -> OptionPackage:
    stock = SHARES if kind is K.COVERED_CALL else None
    args: dict[str, object] = {"stock": stock, "cover": account(units="100")} | kw
    pkg = build_package(kind, legs, fees=usd(fees), **args)  # type: ignore[arg-type]
    assert isinstance(pkg, OptionPackage), pkg
    return pkg


def refused(kind: PackageKind, legs: list[Leg], **kw: object) -> Unavailable:
    args: dict[str, object] = {"fees": usd("0"), "stock": None, "cover": None} | kw
    r = build_package(kind, legs, **args)  # type: ignore[arg-type]
    assert isinstance(r, Unavailable), r
    return r


def test_catalogue_matches_the_spec_options_config() -> None:
    config = Path(__file__).parents[3] / "docs/spec/config/options_catalogue.json"
    assert {k.value for k in PackageKind} == set(
        json.loads(config.read_text())["allowed"]
    )


@pytest.mark.parametrize("kind", list(PackageKind))
def test_hand_computed_payoffs_bounds_and_breakevens(kind: PackageKind) -> None:
    shape, profit, loss, even, table = HAND[kind]
    pkg = build(kind, CASES[kind])
    assert (pkg.direction, pkg.side) == (SIDES[shape[0]], SIDES[shape[1]])
    assert pkg.max_profit == (None if profit is None else usd(profit))
    assert pkg.max_loss == usd(loss) and pkg.breakevens == (Fraction(even),)
    for spot, pnl in table.items():
        assert pkg.payoff(Decimal(spot)) == pnl, (kind, spot)
    assert pkg.cover is not None and not pkg.cover.blocked


# oracle -> (kind, legs as (right, strike input, contracts)). The oracles state
# only the net premium, and the expiry payoff depends only on the net. Assumed
# split: the net is on the first leg, except that a credit vertical's long leg
# pays 1.00 per unit and its short leg receives net + 1.00 (a long never trades
# at 0).
ORACLE_LEGS = {
    "NUM08": (K.LONG_CALL, [(C, "strike", 1)]),
    "NUM09": (K.LONG_PUT, [(P, "strike", 1)]),
    "NUM10": (K.BULL_CALL_DEBIT, [(C, "low_strike", 1), (C, "high_strike", -1)]),
    "NUM11": (K.BEAR_PUT_DEBIT, [(P, "high_strike", 1), (P, "low_strike", -1)]),
    "NUM12": (K.BULL_PUT_CREDIT, [(P, "short_strike", -1), (P, "long_strike", 1)]),
    "NUM13": (K.BEAR_CALL_CREDIT, [(C, "short_strike", -1), (C, "long_strike", 1)]),
    "NUM14": (K.COVERED_CALL, [(C, "strike", -1)]),
    "NUM15": (K.CASH_SECURED_PUT, [(P, "strike", -1)]),
}


@pytest.mark.parametrize("oid", sorted(ORACLE_LEGS))
def test_numerical_oracles(oid: str) -> None:
    kind, spec = ORACLE_LEGS[oid]
    i, expected = ORACLES[oid]["inputs"], ORACLES[oid]["expected"]
    m = Decimal(i["multiplier"])
    net = Decimal(i.get("premium") or i.get("net_debit") or i["net_credit"]) * m
    legs = [
        leg(opt(r, i[key], n + 1, i["multiplier"]), q, -net * q if n == 0 else 0)
        for n, (r, key, q) in enumerate(spec)
    ]
    if "net_credit" in i:  # the assumed split above; the net is unchanged
        legs = [
            replace(x, premium=x.premium + usd(-m * x.quantity.value)) for x in legs
        ]
    stock = None
    if "share_cost" in i:
        basis = usd(Decimal(i["share_cost"]) * m)
        stock = StockLeg(UND, PositiveQuantity(i["multiplier"]), basis)
    pkg = build(kind, legs, stock=stock)
    spot = Decimal(i["terminal_spot"])
    assert pkg.payoff(spot) == Decimal(expected["expiry_pnl"])
    if "expiry_max_loss" in expected:
        assert pkg.max_loss == usd(expected["expiry_max_loss"])
        assert pkg.max_profit == usd(expected["expiry_max_gain"])
    if "premium_at_risk" in expected:
        assert pkg.max_loss == usd(expected["premium_at_risk"])


def test_fees_widen_loss_and_shift_breakeven() -> None:
    pkg = build(K.LONG_CALL, CASES[K.LONG_CALL], fees="1.30")
    assert pkg.max_loss == usd("301.30") and pkg.breakevens == (Fraction("53.013"),)
    assert pkg.payoff(Decimal("53.013")) == 0


BCD = CASES[K.BULL_CALL_DEBIT]
LATER = SessionRef(CalendarId("XNYS"), date(2026, 12, 18))
TEN = (UnitDeliverable(UND, PositiveQuantity("10")),)


def short55(**changes: Any) -> list[Leg]:
    return [BCD[0], replace(BCD[1], contract=replace(BCD[1].contract, **changes))]


def long50(**changes: Any) -> list[Leg]:
    return [replace(BCD[0], contract=replace(BCD[0].contract, **changes)), BCD[1]]


ELSEWHERE = {
    "underlying_id": OTHER,
    "deliverables": (UnitDeliverable(OTHER, TEN[0].quantity),),
}
INVALID = [
    (K.LONG_CALL, [leg(opt(C, "50"), 1, 300)], "premium_sign"),
    (K.CASH_SECURED_PUT, [leg(opt(P, "50"), -1, -200)], "premium_sign"),
    (K.LONG_CALL, [leg(opt(C, "50"), 1, 0)], "premium_zero"),
    (
        K.BULL_PUT_CREDIT,
        [leg(opt(P, "50"), -1, 350), leg(opt(P, "45", 2), 1, 0)],
        "premium_zero",
    ),
    (K.BULL_CALL_DEBIT, [BCD[0], leg(opt(C, "55", 2), -1, 400)], "non_arbitrage"),
    (K.BULL_CALL_DEBIT, [leg(opt(C, "50"), 1, -650), BCD[1]], "no_profit_possible"),
    (K.LONG_CALL, [leg(opt(P, "50"), 1, -300)], "right_mismatch"),
    (K.LONG_CALL, [leg(opt(C, "50"), -1, 300)], "quantity_sign"),
    (K.LONG_PUT, CASES[K.BULL_PUT_CREDIT], "leg_count"),
    (K.BULL_CALL_DEBIT, BCD[:1], "leg_count"),
    (K.BULL_CALL_DEBIT, short55(expiry=LATER), "expiry_mismatch"),
    (K.BULL_CALL_DEBIT, short55(**ELSEWHERE), "underlying_mismatch"),
    (K.BULL_CALL_DEBIT, short55(strike=Price("50")), "strike_order"),
    (K.BULL_CALL_DEBIT, short55(strike=Price("45")), "strike_order"),
    (K.BEAR_CALL_CREDIT, BCD, "strike_order"),
    (K.BULL_PUT_CREDIT, CASES[K.BEAR_PUT_DEBIT], "strike_order"),
    (K.BULL_CALL_DEBIT, [BCD[0], leg(opt(C, "55", 2), 1, -150)], "quantity_sign"),
    (K.BEAR_PUT_DEBIT, CASES[K.BULL_CALL_DEBIT], "right_mismatch"),
    (
        K.BULL_CALL_DEBIT,
        short55(multiplier=Multiplier("10"), deliverables=TEN),
        "terms_mismatch",
    ),
    (K.BULL_CALL_DEBIT, short55(settlement=Settlement.CASH), "terms_mismatch"),
    (K.BULL_CALL_DEBIT, [BCD[0], leg(opt(C, "55", 2), -2, 300)], "quantity_mismatch"),
    (K.BULL_CALL_DEBIT, long50(style=ExerciseStyle.EUROPEAN), "style_cannot_cover"),
    (K.BULL_CALL_DEBIT, [BCD[0], leg(opt(C, "55"), -1, 150)], "duplicate_contract"),
    (
        K.LONG_CALL,
        [leg(replace(opt(C, "50"), multiplier=None, terms_verified=False), 1, -300)],
        "terms_unverified",
    ),
    (
        K.LONG_CALL,
        [Leg(opt(C, "50"), Quantity(1), Money.of("-300", "CAD"))],
        "currency_mismatch",
    ),
]


@pytest.mark.parametrize(("kind", "legs", "code"), INVALID)
def test_invalid_legs_are_refused(
    kind: PackageKind, legs: list[Leg], code: str
) -> None:
    assert refused(kind, legs).code == code


def test_stock_leg_rules_and_fees() -> None:
    cc = CASES[K.COVERED_CALL]
    assert refused(K.COVERED_CALL, cc).code == "stock_leg_required"
    half = StockLeg(UND, PositiveQuantity("50"), usd("2400"))
    assert refused(K.COVERED_CALL, cc, stock=half).code == "stock_quantity_mismatch"
    other = StockLeg(OTHER, PositiveQuantity("100"), usd("4800"))
    assert refused(K.COVERED_CALL, cc, stock=other).code == "underlying_mismatch"
    free = StockLeg(UND, PositiveQuantity("100"), usd("0"))
    assert refused(K.COVERED_CALL, cc, stock=free).code == "stock_basis_invalid"
    one = refused(K.LONG_CALL, [leg(opt(C, "50"), 1, 0)], fees=usd(1))
    assert one.code == "premium_zero"  # a fee does not make a free option credible


def test_covered_call_on_shares_held_above_strike_plus_premium_is_reported() -> None:
    underwater = StockLeg(UND, PositiveQuantity("100"), usd("6000"))  # 100 @ 60
    pkg = build(K.COVERED_CALL, CASES[K.COVERED_CALL], stock=underwater)
    assert pkg.max_profit == usd(-800) and pkg.max_loss == usd(5800)  # 5000+200-6000
    assert pkg.breakevens == () and pkg.payoff(Decimal(70)) == -800
    cash_call = [leg(replace(opt(C, "50"), settlement=Settlement.CASH), -1, 200)]
    assert refused(K.COVERED_CALL, cash_call, stock=SHARES).code == "cash_settled_call"
    lc = CASES[K.LONG_CALL]
    assert refused(K.LONG_CALL, lc, stock=SHARES).code == "stock_leg_unexpected"
    assert refused(K.LONG_CALL, lc, fees=usd("-1")).code == "fee_negative"
    cad = Money.of("1", "CAD")
    assert refused(K.LONG_CALL, lc, fees=cad).code == "currency_mismatch"


def test_coverage_is_refused_without_shares_or_cash() -> None:
    cc, csp = CASES[K.COVERED_CALL], CASES[K.CASH_SECURED_PUT]
    no_shares = refused(K.COVERED_CALL, cc, stock=SHARES, cover=account(units="99"))
    assert no_shares.code == "coverage_refused"
    assert "uncovered_short_call" in no_shares.reason
    short_cash = refused(K.CASH_SECURED_PUT, csp, cover=account(cash="4999.99"))
    assert short_cash.code == "coverage_refused"
    assert "insufficient_cash" in short_cash.reason
    assert build(K.CASH_SECURED_PUT, csp, cover=account(cash="5000")).cover is not None
    unknown = replace(account(), permitted=None)
    r = refused(K.LONG_CALL, CASES[K.LONG_CALL], cover=unknown)
    assert "broker_permission_unknown" in r.reason
    research = build(K.BULL_PUT_CREDIT, CASES[K.BULL_PUT_CREDIT], cover=None)
    assert research.cover is None  # analysis only; the pricing gate blocks live use


@pytest.mark.parametrize(
    "kind", [K.BULL_CALL_DEBIT, K.BEAR_PUT_DEBIT, K.BULL_PUT_CREDIT, K.BEAR_CALL_CREDIT]
)
def test_verticals_agree_with_t034_vertical_bounds(kind: PackageKind) -> None:
    pkg, legs = build(kind, CASES[kind], fees="2.50"), CASES[kind]
    long, short = sorted(legs, key=lambda x: x.quantity.value, reverse=True)
    t034 = vertical_bounds(long, short, usd("2.50"))
    assert isinstance(t034, VerticalBounds)
    assert (pkg.max_loss, pkg.max_profit) == (t034.max_loss, t034.max_gain)


# kind -> legs as (right, strike: 0 lower or 1 higher, signed contracts)
SHAPES = {
    K.LONG_CALL: [(C, 0, 1)],
    K.LONG_PUT: [(P, 0, 1)],
    K.COVERED_CALL: [(C, 0, -1)],
    K.CASH_SECURED_PUT: [(P, 0, -1)],
    K.BULL_CALL_DEBIT: [(C, 0, 1), (C, 1, -1)],
    K.BEAR_PUT_DEBIT: [(P, 1, 1), (P, 0, -1)],
    K.BULL_PUT_CREDIT: [(P, 1, -1), (P, 0, 1)],
    K.BEAR_CALL_CREDIT: [(C, 0, -1), (C, 1, 1)],
}


def closed_form(
    kind: PackageKind, k: tuple[int, int], debit: int, n: int, basis: int
) -> tuple[int, int | None, Fraction]:
    """Textbook (max loss, max profit, breakeven) from the strikes, the net debit
    D = paid - received + fees (negative for a net credit) and n contracts of 100;
    independent of the package code."""
    lo, hi, w, per = k[0], k[1], 100 * n * (k[1] - k[0]), Fraction(debit, 100 * n)
    table: dict[PackageKind, tuple[int, int | None, Fraction | int, int]] = {
        # (loss = a + D, profit = b - D, breakeven = c + sign * D / 100n)
        K.LONG_CALL: (0, None, lo, 1),
        K.LONG_PUT: (0, 100 * n * lo, lo, -1),
        K.COVERED_CALL: (basis, 100 * n * lo - basis, Fraction(basis, 100 * n), 1),
        K.CASH_SECURED_PUT: (100 * n * lo, 0, lo, 1),
        K.BULL_CALL_DEBIT: (0, w, lo, 1),
        K.BEAR_PUT_DEBIT: (0, w, hi, -1),
        K.BULL_PUT_CREDIT: (w, 0, hi, 1),
        K.BEAR_CALL_CREDIT: (w, 0, lo, -1),
    }
    a, b, c, sign = table[kind]
    return a + debit, None if b is None else b - debit, Fraction(c) + sign * per


CENTS = st.integers(min_value=0, max_value=2000)


@settings(max_examples=300, derandomize=True, deadline=None)
@given(
    kind=st.sampled_from(list(PackageKind)),
    strikes=st.tuples(st.integers(10, 90), st.integers(1, 20)),
    premiums=st.tuples(CENTS, CENTS),
    fee=st.integers(0, 500),
    n=st.integers(1, 5),
    spot=st.integers(0, 20000).map(lambda c: Decimal(c).scaleb(-2)),
)
def test_payoff_bounds_and_breakevens_match_closed_forms(
    kind: PackageKind,
    strikes: tuple[int, int],
    premiums: tuple[int, int],
    fee: int,
    n: int,
    spot: Decimal,
) -> None:
    k = (strikes[0], strikes[0] + strikes[1])
    shape = SHAPES[kind]
    legs = [
        leg(opt(r, str(k[hi]), i + 1), q * n, premiums[i] * n * -q)
        for i, (r, hi, q) in enumerate(shape)
    ]
    debit = fee + sum(premiums[i] * n * q for i, (_, _, q) in enumerate(shape))
    loss, profit, even = closed_form(kind, k, debit, n, 4000 * n)
    stock = StockLeg(UND, PositiveQuantity(100 * n), usd(4000 * n))
    holds = {"stock": stock if kind is K.COVERED_CALL else None, "cover": None}
    pkg = build_package(kind, legs, fees=usd(fee), **holds)  # type: ignore[arg-type]
    free_long = any(premiums[i] == 0 for i, (_, _, q) in enumerate(shape) if q > 0)
    underwater = kind is K.COVERED_CALL and profit is not None and profit <= 0
    if (
        free_long
        or loss <= 0
        or (profit is not None and profit <= 0 and not underwater)
    ):
        assert isinstance(pkg, Unavailable)
        codes = (
            ("premium_zero",) if free_long else ("non_arbitrage", "no_profit_possible")
        )
        assert pkg.code in codes
        return
    assert isinstance(pkg, OptionPackage), pkg
    assert pkg.max_loss == usd(loss)
    assert pkg.max_profit == (None if profit is None else usd(profit))
    if underwater:  # flat at max profit <= 0 above the strike
        assert pkg.breakevens == (() if profit else (Fraction(k[0]),))
    else:
        assert pkg.breakevens == (even,) and pkg.payoff(even) == 0
    assert -loss <= pkg.payoff(spot)
    assert profit is None or pkg.payoff(spot) <= profit
