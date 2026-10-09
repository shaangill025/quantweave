"""Option exposure, collateral and legging (T034; R017, R043, R044, R068, R089; F-14).
SYNTHETIC contracts. Oracles: numerical_oracles.json NUM08-NUM16 and hand values."""

import json
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.option_risk import (
    AccountCover,
    CoverCheck,
    Leg,
    Structure,
    VerticalBounds,
    check_cover,
    check_legging,
    contract_terms,
    expiry_intrinsic,
    vertical_bounds,
)
from qw_domain.options import (
    CashDeliverable,
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
ALL = frozenset(Structure)


def opt(
    right: OptionRight, strike: str, n: int = 1, mult: str = "100"
) -> OptionContract:
    return OptionContract(
        contract_id=InstrumentId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        underlying_id=UND,
        expiry=SessionRef(CalendarId("XNYS"), date(2026, 11, 20)),
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


C, P = OptionRight.CALL, OptionRight.PUT
# SYNTHETIC adjusted contract: K=50, M=100 delivers 150 units plus USD 12.50.
ADJ = replace(
    opt(C, "50", 9),
    deliverables=(
        UnitDeliverable(UND, PositiveQuantity("150")),
        CashDeliverable(Money.of("12.50", "USD")),
    ),
    adjusted=True,
    terms_version=2,
)


def usd(x: str | int) -> Money:
    return Money.of(x, "USD")


def leg(c: OptionContract, q: int, premium: str = "0") -> Leg:
    return Leg(c, Quantity(q), usd(premium))


def cover(
    cash: str, units: str = "0", permitted: frozenset[Structure] | None = ALL
) -> AccountCover:
    return AccountCover("USD", usd(cash), {UND: Quantity(units)}, permitted)


def checked(legs: list[Leg], acct: AccountCover) -> CoverCheck:
    r = check_cover(legs, acct)
    assert isinstance(r, CoverCheck), r
    return r


# (oracle, legs as (right, strike, contracts), net premium; NUM14 adds the
# 1200 share gain from 48 to 60 on 100 units).
PAYOFFS = [
    ("NUM08", [(C, "50", 1)], "60", -300),
    ("NUM09", [(P, "50", 1)], "40", -300),
    ("NUM10", [(C, "50", 1), (C, "55", -1)], "60", -200),
    ("NUM11", [(P, "50", 1), (P, "45", -1)], "40", -200),
    ("NUM12", [(P, "45", 1), (P, "50", -1)], "40", 200),
    ("NUM13", [(C, "55", 1), (C, "50", -1)], "60", 200),
    ("NUM14", [(C, "50", -1)], "60", 1200 + 200),
    ("NUM15", [(P, "50", -1)], "40", 200),
]


@pytest.mark.parametrize(("oracle", "legs", "spot", "premium"), PAYOFFS)
def test_payoff_oracles(
    oracle: str, legs: list[tuple[OptionRight, str, int]], spot: str, premium: int
) -> None:
    total = usd(premium)
    for i, (right, strike, q) in enumerate(legs):
        v = expiry_intrinsic(opt(right, strike, i + 1), Price(spot))
        assert isinstance(v, Money)
        total = total + v if q > 0 else total - v
    assert total == usd(ORACLES[oracle]["expected"]["expiry_pnl"])


def test_covered_call_and_cash_secured_put_oracles() -> None:
    exp14, exp15 = ORACLES["NUM14"]["expected"], ORACLES["NUM15"]["expected"]
    cc = checked([leg(opt(C, "50"), -1, "200")], cover("0", "100"))
    assert cc.units_encumbered == ((UND, Fraction(exp14["shares_required"])),)
    csp = checked([leg(opt(P, "50"), -1, "200")], cover("5000"))
    assert csp.cash_required == usd(exp15["gross_assignment_cash"])
    assert not csp.blocked and not cc.blocked


@pytest.mark.parametrize(
    ("oracle", "long", "short", "premiums"),
    [
        ("NUM10", (C, "50"), (C, "55"), ("-300", "100")),
        ("NUM11", (P, "50"), (P, "45"), ("-300", "100")),
        ("NUM12", (P, "45"), (P, "50"), ("-100", "300")),
        ("NUM13", (C, "55"), (C, "50"), ("-100", "300")),
    ],
)
def test_vertical_bounds_and_reserve(
    oracle: str,
    long: tuple[OptionRight, str],
    short: tuple[OptionRight, str],
    premiums: tuple[str, str],
) -> None:
    lg, sh = (
        leg(opt(*long), 1, premiums[0]),
        leg(opt(short[0], short[1], 2), -1, premiums[1]),
    )
    b = vertical_bounds(lg, sh, usd(0))
    exp = ORACLES[oracle]["expected"]
    assert b == VerticalBounds(usd(exp["expiry_max_loss"]), usd(exp["expiry_max_gain"]))
    r = checked([lg, sh], cover("10000"))
    debit = oracle in ("NUM10", "NUM11")
    # debit: the premium paid; credit: the full width (unsettled credit never funds).
    assert (
        r.cash_required == usd(300 if debit else 600)
        and Structure.SPREAD in r.structures
    )
    fees = vertical_bounds(lg, sh, usd(2))
    assert isinstance(fees, VerticalBounds) and fees.max_loss == usd(
        exp["expiry_max_loss"]
    ) + usd(2)
    assert vertical_bounds(lg, leg(opt(*short), -2), usd(0)) == Unavailable(
        "not_a_vertical", "legs differ in terms or quantity"
    )


def test_vertical_bounds_follow_structure_not_premium_sign() -> None:
    low, high = opt(C, "50"), opt(C, "55", 2)
    # Bear call (a credit structure) paid at a net debit of 400; width 500.
    bear = vertical_bounds(leg(high, 1, "-450"), leg(low, -1, "50"), usd(0))
    assert bear == VerticalBounds(usd(900), usd(-400))
    # Bull call (a debit structure) at a net credit of 400.
    bull = vertical_bounds(leg(low, 1, "-100"), leg(high, -1, "500"), usd(0))
    assert bull == VerticalBounds(usd(-400), usd(900))
    zero = vertical_bounds(leg(low, 1, "-100"), leg(high, -1, "100"), usd(3))
    assert zero == VerticalBounds(usd(3), usd(497))
    # Puts: long K > short K is the debit structure.
    bear_put = vertical_bounds(leg(opt(P, "55"), 1), leg(opt(P, "50", 2), -1), usd(0))
    assert bear_put == VerticalBounds(usd(0), usd(500))


def test_vertical_bounds_refuse_bad_inputs() -> None:
    lg, sh = leg(opt(C, "50"), 1, "-300"), leg(opt(C, "55", 2), -1, "100")
    for fee, code in (
        (Money.of("1", "CAD"), "currency_mismatch"),
        (usd(-1), "fee_negative"),
    ):
        r = vertical_bounds(lg, sh, fee)
        assert isinstance(r, Unavailable) and r.code == code
    cad = Leg(opt(C, "55", 2), Quantity(-1), Money.of("100", "CAD"))
    r = vertical_bounds(lg, cad, usd(0))
    assert isinstance(r, Unavailable) and r.code == "currency_mismatch"


def test_european_long_does_not_cover_american_short() -> None:
    eu_long = replace(opt(P, "45", 2), style=ExerciseStyle.EUROPEAN)
    am_short = leg(opt(P, "50"), -1)
    r = checked([leg(eu_long, 1), am_short], cover("10000"))
    assert r.cash_required == usd(5000) and Structure.SPREAD not in r.structures
    nb = vertical_bounds(leg(eu_long, 1), am_short, usd(0))
    assert isinstance(nb, Unavailable) and nb.code == "not_a_vertical"
    am_long = opt(P, "45", 3)
    eu_short = leg(replace(opt(P, "50"), style=ExerciseStyle.EUROPEAN), -1)
    r = checked([leg(am_long, 1), eu_short], cover("10000"))
    assert r.cash_required == usd(500) and Structure.SPREAD in r.structures


def test_negative_or_cash_only_deliverable_fails_closed() -> None:
    neg = replace(
        ADJ,
        deliverables=(
            UnitDeliverable(UND, PositiveQuantity("150")),
            CashDeliverable(Money.of("-12.50", "USD")),
        ),
    )
    r = contract_terms(neg)
    assert isinstance(r, Unavailable) and r.code == "cash_deliverable_negative"
    cash_only = replace(ADJ, deliverables=(CashDeliverable(Money.of("1", "USD")),))
    r = contract_terms(cash_only)
    assert isinstance(r, Unavailable) and r.code == "unit_deliverable_missing"


def test_adjusted_and_unknown_terms() -> None:
    t = contract_terms(ADJ)
    assert not isinstance(t, Unavailable)
    assert (t.units, t.cash, t.strike_cash, t.effective_strike) == (
        Fraction(150),
        Fraction(25, 2),
        Fraction(5000),
        Fraction(133, 4),
    )
    assert expiry_intrinsic(ADJ, Price("40")) == usd("1012.5")  # 6000 + 12.50 - 5000
    assert expiry_intrinsic(replace(ADJ, right=P), Price("30")) == usd("487.5")
    # Covered: 150 units per contract plus the USD 12.50 cash component.
    cc = checked([leg(ADJ, -1)], cover("12.5", "150"))
    assert not cc.blocked and cc.units_encumbered == ((UND, Fraction(150)),)
    assert checked([leg(ADJ, -1)], cover("12.5", "149")).reasons == (
        "uncovered_short_call",
    )
    short_cash = checked([leg(ADJ, -1)], cover("12.49", "150"))
    assert short_cash.reasons == ("insufficient_cash",)  # the USD 12.50 is delivered
    # An adjusted put reserves the gross K*M; cash it would receive is not netted.
    assert checked(
        [leg(replace(ADJ, right=P), -1)], cover("5000")
    ).cash_required == usd(5000)
    # NUM16 and missing terms: represented, blocked with a typed reason.
    unknown = replace(ADJ, multiplier=None, deliverables=(), terms_verified=False)
    assert unknown.sizing_allowed is ORACLES["NUM16"]["expected"]["sizing_allowed"]
    for bad in (unknown, replace(ADJ, terms_verified=False)):
        r = check_cover([leg(bad, -1)], cover("99999", "999"))
        assert isinstance(r, Unavailable) and r.code == "terms_unverified"
    u = contract_terms(unknown)
    assert isinstance(u, Unavailable) and "multiplier_unknown" in u.reason
    assert "deliverables_unknown" in u.reason


def test_shares_cover_one_call_and_combined_cash() -> None:
    a, b = leg(opt(C, "50"), -1), leg(opt(C, "55", 2), -1)
    assert checked([a, b], cover("0", "100")).reasons == ("uncovered_short_call",)
    assert not checked([a, b], cover("0", "200")).blocked
    puts = [leg(opt(P, "50"), -1), leg(opt(P, "40", 2), -1)]
    assert checked(puts, cover("8999.99")).reasons == ("insufficient_cash",)
    assert not checked(puts, cover("9000")).blocked
    cash_settled = replace(opt(C, "50"), settlement=Settlement.CASH)
    assert checked([leg(cash_settled, -1)], cover("0", "100")).reasons == (
        "uncovered_short_call",
    )


def test_permissions_and_currency_block() -> None:
    put = [leg(opt(P, "50"), -1)]
    assert checked(put, cover("5000", permitted=None)).reasons == (
        "broker_permission_unknown",
    )
    only_long = frozenset({Structure.LONG})
    assert checked(put, cover("5000", permitted=only_long)).reasons == (
        "broker_permission_missing:cash_secured_put",
    )
    cad = AccountCover("CAD", Money.of("99999", "CAD"), {}, ALL)
    r = check_cover(put, cad)
    assert isinstance(r, Unavailable) and r.code == "currency_mismatch"
    with pytest.raises(ValueError, match="account currency"):
        AccountCover("USD", Money.of("1", "CAD"), {}, ALL)
    with pytest.raises(TypeError):
        Leg(opt(P, "50"), 1.0, usd(0))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="whole"):
        leg(opt(P, "50"), 0)


def test_legging_checks_every_intermediate_state() -> None:
    long45, short50 = leg(opt(P, "45"), 1, "-100"), leg(opt(P, "50", 2), -1, "300")
    acct = cover("1000")
    assert not checked([long45, short50], acct).blocked  # final state needs 600
    good = check_legging([long45, short50], acct)
    assert not isinstance(good, Unavailable) and not good.blocked and good.worst == 1
    bad = check_legging([short50, long45], acct)  # short first: a 5000 put alone
    assert not isinstance(bad, Unavailable) and bad.blocked and bad.worst == 0
    assert bad.states[0].reasons == ("insufficient_cash",)
    # Bear call credit: short call first is uncovered without units.
    calls = [leg(opt(C, "50"), -1, "300"), leg(opt(C, "55", 2), 1, "-100")]
    r = check_legging(calls, cover("10000"))
    assert (
        not isinstance(r, Unavailable)
        and r.blocked
        and r.states[0].reasons == ("uncovered_short_call",)
    )
    # Closing the long leg of an open credit spread first leaves a naked short.
    existing = [long45, short50]
    r = check_legging(
        [leg(opt(P, "45"), -1, "50"), leg(opt(P, "50", 2), 1, "-60")], acct, existing
    )
    assert not isinstance(r, Unavailable) and r.blocked and r.worst == 0
    r = check_legging(
        [leg(opt(P, "50", 2), 1, "-60"), leg(opt(P, "45"), -1, "50")], acct, existing
    )
    assert not isinstance(r, Unavailable) and not r.blocked


@settings(derandomize=True, database=None, max_examples=150, deadline=None)
@given(
    st.integers(min_value=1, max_value=50), st.sampled_from(["put", "spread", "call"])
)
def test_collateral_monotone_in_quantity(n: int, kind: str) -> None:
    def need(q: int) -> Money:
        legs = {
            "put": [leg(opt(P, "50"), -q)],
            "spread": [leg(opt(C, "50"), -q, "300"), leg(opt(C, "55", 2), q, "-100")],
            "call": [leg(opt(C, "50"), -q), leg(opt(C, "40", 2), q, f"-{q}")],
        }[kind]
        return checked(legs, cover("0", "0")).cash_required

    assert need(n) <= need(n + 1)
    assert need(n) > usd(0)
