"""Pre-expiry scenario grids on catalogue packages (T035; R017, R089; spec 09
"Pre-expiry analysis"). SYNTHETIC contracts and marks. Oracles:
numerical_oracles.json NUM08-NUM15 at the expiry slice; Hull's S=42, K=40, r=10%,
vol 20%, T=0.5 (call 4.76 per unit); put-call parity: a 40/45 bull call plus a
45/40 bear put is worth 500 e^(-0.05) = 475.614712250357 (double precision);
hand-computed covered-call expiry values."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from qw_domain.calendars import CalendarId, Session, SessionRef
from qw_domain.decimals import (
    Money,
    Multiplier,
    PositiveQuantity,
    Price,
    Quantity,
    Ratio,
)
from qw_domain.identity import InstrumentId
from qw_domain.option_greeks import DayCount, ModelInputs
from qw_domain.option_packages import (
    OptionPackage,
    PackageKind,
    StockLeg,
    build_package,
)
from qw_domain.option_risk import AccountCover, Leg, Structure
from qw_domain.option_scenarios import (
    ScenarioGap,
    ScenarioGrid,
    ScenarioPoint,
    Shock,
    grid,
    scenario_grid,
)
from qw_domain.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.valuation import Mark, MarkKind, Unavailable

ORACLES = {
    o["id"]: o
    for o in json.loads(
        (
            Path(__file__).parents[3]
            / "docs/spec/tests/fixtures/numerical_oracles.json"
        ).read_text()
    )["oracles"]
}
D, K = Decimal, PackageKind
C, P = OptionRight.CALL, OptionRight.PUT
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
CLOSE = datetime(2027, 4, 9, 20, 0, tzinfo=UTC)
NOW = CLOSE - timedelta(hours=4380)  # T = 0.5 (ACT/365F)
EXPIRY = CLOSE - NOW  # the elapsed time that reaches the expiry slice
OPEN = CLOSE - timedelta(minutes=390)
SESSION = Session(CalendarId("XNYS"), "SYNTHETIC", CLOSE.date(), OPEN, CLOSE, False)
ZERO, NOW_ONLY, DAY = Ratio(0), [timedelta(0)], timedelta(days=1)


def opt(right: OptionRight, strike: str | int, n: int) -> OptionContract:
    return OptionContract(
        contract_id=InstrumentId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        underlying_id=UND,
        expiry=SessionRef(CalendarId("XNYS"), CLOSE.date()),
        strike=Price(str(strike)),
        strike_currency="USD",
        right=right,
        style=ExerciseStyle.EUROPEAN,
        settlement=Settlement.PHYSICAL,
        multiplier=Multiplier("100"),
        deliverables=(UnitDeliverable(UND, PositiveQuantity("100")),),
        adjusted=False,
        terms_verified=True,
        terms_version=1,
    )


def usd(x: int | str) -> Money:
    return Money.of(x, "USD")


def leg(c: OptionContract, q: int, premium: int) -> Leg:
    return Leg(c, Quantity(q), usd(premium))


ACCOUNT = AccountCover("USD", usd(10**6), {UND: Quantity(500)}, frozenset(Structure))


def package(
    kind: PackageKind, legs: list[Leg], stock: StockLeg | None = None
) -> OptionPackage:
    pkg = build_package(kind, legs, fees=usd(0), stock=stock, cover=ACCOUNT)
    assert isinstance(pkg, OptionPackage), pkg
    return pkg


def base(spot: str = "42", rate: str = "0.1", at: datetime = NOW) -> ModelInputs:
    return ModelInputs(
        spot=Mark(UND, Price(spot), "USD", MarkKind.MID, at, "SYNTHETIC"),
        vol=Ratio("0.2"),
        rate=Ratio(rate),
        dividend_yield=ZERO,
        valuation_at=at,
        max_age=timedelta(minutes=15),
        expiry_session=SESSION,
        day_count=DayCount.ACT_365F,
    )


C40, C45, P40, P45 = opt(C, 40, 1), opt(C, 45, 2), opt(P, 40, 3), opt(P, 45, 4)
C50, C55, P45B, P50 = opt(C, 50, 5), opt(C, 55, 6), opt(P, 45, 7), opt(P, 50, 8)
VOLS = {c.contract_id: Ratio("0.2") for c in (C40, C45, P40, P45, C50, C55, P45B, P50)}
LONG_CALL = package(K.LONG_CALL, [leg(C40, 1, -476)])
SPOTS = [Ratio(x) for x in ("-0.5", "-0.1", "0", "0.1", "0.5")]


def run(
    pkg: OptionPackage, shocks: tuple[Shock, ...], inputs: ModelInputs | None = None
) -> ScenarioGrid:
    g = scenario_grid(pkg, inputs or base(), VOLS, shocks)
    assert isinstance(g, ScenarioGrid), g
    return g


def points(g: ScenarioGrid) -> list[ScenarioPoint]:
    out = [p for p in g.points if isinstance(p, ScenarioPoint)]
    assert len(out) == len(g.points)
    return out


def test_base_matches_hull_and_outputs_are_separate() -> None:
    g = run(LONG_CALL, grid(SPOTS, [Ratio("-0.1"), ZERO, Ratio("0.1")], NOW_ONLY))
    assert round(g.base.premium_value) == 476 and abs(g.base.pnl) < 1
    assert g.complete and g.model_applicable and g.base.change == 0
    ps = points(g)
    losses = [-p.pnl for p in [g.base, *ps]]
    assert g.worst_loss == max(losses) and D(0) < g.worst_loss < 476
    assert g.collateral == usd(476)  # the paid premium (T034 cover check)
    by = {(p.shock.spot.value, p.shock.vol.value): p for p in ps}
    for lo, hi in pairwise(SPOTS):  # monotone in spot and vol
        assert by[(hi.value, D(0))].premium_value > by[(lo.value, D(0))].premium_value
        assert (
            by[(lo.value, D("0.1"))].premium_value > by[(lo.value, D(0))].premium_value
        )
        assert by[(lo.value, D(0))].change < by[(hi.value, D(0))].change


def test_bull_call_plus_bear_put_is_the_discounted_width_everywhere() -> None:
    shocks = grid(SPOTS, [Ratio("-0.1"), Ratio("0.3")], NOW_ONLY)
    calls = run(
        package(K.BULL_CALL_DEBIT, [leg(C40, 1, -300), leg(C45, -1, 100)]), shocks
    )
    puts = run(package(K.BEAR_PUT_DEBIT, [leg(P45, 1, -250), leg(P40, -1, 50)]), shocks)
    for c, p in zip(points(calls), points(puts), strict=True):
        assert abs(c.premium_value + p.premium_value - D("475.614712250357")) < D(
            "1e-9"
        )


# oracle -> package legs (M=100, strikes 45/50/55); the expiry slice is reached
# from a base spot of 50 by a +20% or -20% shock. NUM12/13 state only the net
# credit (2.00); the split assumed here is short +3.00, long -1.00.
ORACLE_LEGS = {
    "NUM08": [leg(C50, 1, -300)],
    "NUM09": [leg(P50, 1, -300)],
    "NUM10": [leg(C50, 1, -350), leg(C55, -1, 150)],
    "NUM11": [leg(P50, 1, -350), leg(P45B, -1, 150)],
    "NUM12": [leg(P50, -1, 300), leg(P45B, 1, -100)],
    "NUM13": [leg(C50, -1, 300), leg(C55, 1, -100)],
    "NUM14": [leg(C50, -1, 200)],
    "NUM15": [leg(P50, -1, 200)],
}
# NUM08-NUM15 cover the eight kinds in this order.
KINDS = dict(
    zip(
        ORACLE_LEGS,
        [*PackageKind][:2] + [*PackageKind][4:] + [*PackageKind][2:4],
        strict=True,
    )
)
SHARES = StockLeg(UND, PositiveQuantity(100), usd(4800))  # NUM14: 100 @ 48


@pytest.mark.parametrize("oid", sorted(ORACLE_LEGS))
def test_expiry_slice_matches_the_numerical_oracles(oid: str) -> None:
    stock = SHARES if oid == "NUM14" else None
    pkg = package(KINDS[oid], ORACLE_LEGS[oid], stock)
    spot = D(ORACLES[oid]["inputs"]["terminal_spot"])
    shock = Ratio(str((spot - 50) / 50))
    (p,) = points(run(pkg, grid([shock], [ZERO], [EXPIRY]), base("50")))
    assert p.at_expiry and p.spot.value == spot
    assert p.pnl == D(ORACLES[oid]["expected"]["expiry_pnl"])
    assert Fraction(p.pnl) == pkg.payoff(spot)


def test_covered_call_delta_and_continuity_into_expiry() -> None:
    covered = package(K.COVERED_CALL, [leg(C50, -1, 200)], SHARES)
    late = EXPIRY - timedelta(minutes=1)
    g = run(
        covered, grid([Ratio("-0.2"), Ratio("0.2")], [ZERO], [late, EXPIRY]), base("50")
    )
    low_late, low_exp, high_late, high_exp = points(g)
    assert (low_exp.pnl, low_exp.delta_units) == (D(-600), D(100))  # 4000-4800+200
    assert (high_exp.pnl, high_exp.delta_units) == (D(400), D(0))  # 5000-4800+200
    assert abs(low_late.pnl - low_exp.pnl) < D("0.01")
    assert abs(high_late.pnl - high_exp.pnl) < D("0.01")
    assert not low_late.at_expiry and D(99) < low_late.delta_units <= 100
    assert g.worst_loss is not None and abs(g.worst_loss - 600) < D("0.01")
    assert g.collateral == usd(0)  # the shares cover the call


def test_gaps_are_kept_and_bad_bases_refused() -> None:
    shocks = grid([ZERO], [Ratio("-0.2"), ZERO], [timedelta(0), EXPIRY])
    g = run(LONG_CALL, shocks)
    gaps = [p for p in g.points if isinstance(p, ScenarioGap)]
    assert [x.reason.code for x in gaps] == ["vol_not_positive"]  # not at expiry
    assert not g.complete and len(g.points) == 4
    assert g.worst_loss is None  # incomplete: a gap may hide the worst point
    refusals = {
        "vol_missing": scenario_grid(LONG_CALL, base(), {}, shocks),
        "spot_stale": scenario_grid(
            LONG_CALL,
            replace(base(), valuation_at=NOW + timedelta(hours=1)),
            VOLS,
            shocks,
        ),
        "contract_expired": scenario_grid(LONG_CALL, base(at=CLOSE), VOLS, shocks),
        "expiry_session_mismatch": scenario_grid(
            LONG_CALL,
            replace(
                base(),
                expiry_session=replace(SESSION, session_date=OPEN.date() - DAY),
            ),
            VOLS,
            shocks,
        ),
    }
    for code, r in refusals.items():
        assert isinstance(r, Unavailable) and r.code == code, (code, r)
    american = package(
        K.LONG_CALL, [leg(replace(C40, style=ExerciseStyle.AMERICAN), 1, -476)]
    )
    assert not run(american, grid([ZERO], [ZERO], NOW_ONLY)).model_applicable
    with pytest.raises(ValueError, match="spot shock"):
        Shock(Ratio("-1"), ZERO, timedelta(0))
    with pytest.raises(ValueError, match="elapsed"):
        Shock(ZERO, ZERO, -timedelta(seconds=1))


SHAPES = {  # kind -> legs as (right, strike: 0 lower or 1 higher, signed contracts)
    K.LONG_CALL: [(C, 0, 1)],
    K.LONG_PUT: [(P, 0, 1)],
    K.COVERED_CALL: [(C, 0, -1)],
    K.CASH_SECURED_PUT: [(P, 0, -1)],
    K.BULL_CALL_DEBIT: [(C, 0, 1), (C, 1, -1)],
    K.BEAR_PUT_DEBIT: [(P, 1, 1), (P, 0, -1)],
    K.BULL_PUT_CREDIT: [(P, 1, -1), (P, 0, 1)],
    K.BEAR_CALL_CREDIT: [(C, 0, -1), (C, 1, 1)],
}


@settings(max_examples=120, derandomize=True, deadline=None)
@given(
    kind=st.sampled_from(list(PackageKind)),
    strikes=st.tuples(st.integers(30, 60), st.integers(1, 10)),
    premiums=st.tuples(st.integers(1, 1000), st.integers(1, 1000)),
    spots=st.lists(st.integers(-90, 300), min_size=1, max_size=4),
    vol=st.integers(10, 80),
)
def test_expiry_slice_is_the_payoff_and_worst_loss_is_bounded(
    kind: PackageKind,
    strikes: tuple[int, int],
    premiums: tuple[int, int],
    spots: list[int],
    vol: int,
) -> None:
    k = (strikes[0], strikes[0] + strikes[1])
    shape = SHAPES[kind]
    legs = [
        leg(opt(r, k[hi], 20 + i), q, premiums[i] * -q)
        for i, (r, hi, q) in enumerate(shape)
    ]
    stock = (
        StockLeg(UND, PositiveQuantity(100), usd(4000))
        if kind is K.COVERED_CALL
        else None
    )
    pkg = build_package(kind, legs, fees=usd(0), stock=stock)
    assume(isinstance(pkg, OptionPackage))
    assert isinstance(pkg, OptionPackage)
    shocks = grid(
        [Ratio(D(s).scaleb(-2)) for s in spots],
        [ZERO],
        [timedelta(0), EXPIRY // 2, EXPIRY],
    )
    vols = {c.contract.contract_id: Ratio(D(vol).scaleb(-2)) for c in pkg.legs}
    g = scenario_grid(pkg, base("45", "0.05"), vols, shocks)
    assert isinstance(g, ScenarioGrid) and g.complete and g.worst_loss is not None
    expiry = [p for p in points(g) if p.at_expiry]
    assert len(expiry) == len(spots)
    for p in expiry:
        assert Fraction(p.pnl) == pkg.payoff(p.spot.value)
    assert max(D(0), -g.base.pnl, *(-p.pnl for p in expiry)) <= g.worst_loss
    assert g.worst_loss <= pkg.max_loss.amount.value + D("1e-20")  # r >= 0, q = 0


def test_worst_loss_is_never_negative_and_needs_shocks() -> None:
    cheap = package(K.LONG_CALL, [leg(C40, 1, -100)])  # worth ~476 at the base
    g = run(cheap, grid([Ratio("0.1"), Ratio("0.5")], [ZERO], NOW_ONLY))
    assert all(p.pnl > 0 for p in points(g)) and g.base.pnl > 0
    assert g.worst_loss == 0 and g.complete
    empty = scenario_grid(cheap, base(), VOLS, ())
    assert isinstance(empty, Unavailable) and empty.code == "no_shocks"
