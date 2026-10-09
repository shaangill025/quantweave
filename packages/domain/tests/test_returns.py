"""TWR, MWR and FX attribution (T018 increment 1; F-07, F-08, R064, R090).

SYNTHETIC inputs only. Oracles: NUM03 (TWR), NUM04 (FX), NUM05 (ambiguous MWR), read
from numerical_oracles.json; other expectations are hand-computed in the comments or
built from known growth factors with exact `Fraction` arithmetic in the test.
"""

import json
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import returns
from qw_domain.decimals import DOMAIN_CONTEXT, CurrencyMismatchError, Money, Ratio
from qw_domain.instants import InstantError
from qw_domain.returns import (
    DatedFlow,
    FlowValuation,
    FxAttribution,
    MoneyWeightedReturn,
    PeriodReturn,
    fx_attribution,
    money_weighted_return,
    time_weighted_return,
)
from qw_domain.valuation import Unavailable

PROFILE = settings(derandomize=True, database=None, max_examples=60, deadline=None)
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
T0 = datetime(2026, 1, 2, 21, tzinfo=UTC)
D0 = date(2025, 1, 1)


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def pt(
    day: int, value: str | Decimal | None, flow: str | Decimal = "0"
) -> FlowValuation:
    return FlowValuation(
        T0 + timedelta(days=day), None if value is None else usd(value), usd(flow)
    )


def code(out: object) -> str:
    assert isinstance(out, Unavailable)
    return out.code


# ---- TWR


def test_num03_chain_linked_twr() -> None:
    inp, exp = ORACLES["NUM03"]["inputs"], ORACLES["NUM03"]["expected"]
    out = time_weighted_return(
        [
            pt(0, inp["start"]),
            pt(10, inp["before_flow"], inp["contribution"]),
            pt(20, inp["end"]),
        ]
    )
    assert isinstance(out, PeriodReturn)
    assert out.value == Ratio(exp["return"])  # 21%, not the 131% value change
    assert out.subperiods == (Ratio("0.1"), Ratio("0.1"))
    assert out.exact and (out.start_at, out.end_at) == (T0, T0 + timedelta(days=20))


@pytest.mark.parametrize(
    ("points", "reason"),
    [
        ([pt(0, "100")], "insufficient_valuations"),
        ([pt(0, "100"), pt(5, None), pt(9, "120")], "missing_valuation_interval"),
        ([pt(0, "100", "-100"), pt(5, "0")], "nonpositive_denominator"),
        ([pt(0, "-5"), pt(5, "10")], "nonpositive_denominator"),
        ([pt(0, "100"), pt(0, "100", "5"), pt(3, "110")], "ambiguous_event_order"),
        ([pt(5, "100"), pt(0, "110")], "ambiguous_event_order"),
        ([pt(0, "100"), pt(5, "-50")], "negative_end_value"),
    ],
)
def test_twr_unavailable(points: list[FlowValuation], reason: str) -> None:
    assert code(time_weighted_return(points)) == reason


def test_twr_total_loss_is_minus_one_not_below() -> None:
    out = time_weighted_return([pt(0, "100"), pt(5, "0")])
    assert isinstance(out, PeriodReturn) and out.value == Ratio("-1")


def test_twr_rejections() -> None:
    naive = datetime(2026, 1, 2)  # noqa: DTZ001 - the naive input under test
    with pytest.raises(InstantError):
        FlowValuation(naive, usd(1), usd(0))
    with pytest.raises(TypeError):
        FlowValuation(T0, Decimal(1), usd(0))  # type: ignore[arg-type]
    with pytest.raises(CurrencyMismatchError):
        time_weighted_return(
            [
                pt(0, "1"),
                FlowValuation(
                    T0 + T0.resolution, Money.of(1, "CAD"), Money.of(0, "CAD")
                ),
            ]
        )
    with pytest.raises(CurrencyMismatchError):
        FlowValuation(T0, usd(1), Money.of(0, "CAD"))


growth = st.integers(50, 200).map(lambda n: Decimal(n).scaleb(-2))


@PROFILE
@given(
    st.lists(st.tuples(growth, st.integers(-40, 300)), min_size=1, max_size=5),
    st.integers(1, 10**6),
)
def test_twr_is_independent_of_flow_size(
    steps: list[tuple[Decimal, int]], start: int
) -> None:
    """Values built from known growth factors; each flow moves the post-flow value
    to a positive whole amount (-40%..+300%); the linked return must not change."""
    points, post, expected = [pt(0, Decimal(start))], Decimal(start), Fraction(1)
    with localcontext(DOMAIN_CONTEXT):
        for i, (g, pct) in enumerate(steps, start=1):
            pre = post * g
            post = max(Decimal(1), (pre * (100 + pct)).scaleb(-2).to_integral_value())
            points.append(pt(i, pre, post - pre))
            expected *= Fraction(g)
    out = time_weighted_return(points)
    assert isinstance(out, PeriodReturn)
    assert out.value.value == Decimal(round((expected - 1) * 10**18)).scaleb(-18)


# ---- MWR


def flows(*items: tuple[int, str]) -> list[DatedFlow]:
    return [DatedFlow(D0 + timedelta(days=d), usd(a)) for d, a in items]


def test_num05_two_roots_reported_as_ambiguous() -> None:
    exp = ORACLES["NUM05"]["expected"]
    cfs = ORACLES["NUM05"]["inputs"]["annual_cashflows"]
    # 2025-01-01, 2026-01-01, 2027-01-01: 365 days apart, so ACT/365F gives 1 and 2.
    out = money_weighted_return(flows(*zip((0, 365, 730), cfs, strict=True)))
    assert isinstance(out, MoneyWeightedReturn)
    assert out.status == exp["status"] == "ambiguous"
    assert out.period_return is None and out.annualized is None
    roots = [r.annualized for r in out.roots]
    assert len(roots) == len(exp["roots"])
    for got, want in zip(roots, exp["roots"], strict=True):
        assert got is not None and abs(got.value - Decimal(want)) < Decimal("1e-15")


def test_unique_annual_root() -> None:
    out = money_weighted_return(flows((0, "-1000"), (365, "1100")))
    assert isinstance(out, MoneyWeightedReturn)
    assert out.status == "unique" and out.horizon_days == 365
    assert out.period_return is not None and out.annualized is not None
    assert abs(out.period_return.value - Decimal("0.1")) < Decimal("1e-18")
    assert abs(out.annualized.value - Decimal("0.1")) < Decimal("1e-18")
    assert out.day_count == "ACT/365F" and out.iterations <= 200


def test_short_window_is_not_annualized_by_default() -> None:
    out = money_weighted_return(flows((0, "-1000"), (30, "1010")))
    assert isinstance(out, MoneyWeightedReturn)
    assert out.period_return is not None
    assert abs(out.period_return.value - Decimal("0.01")) < Decimal("1e-18")
    assert out.annualized is None
    asked = money_weighted_return(flows((0, "-1000"), (30, "1010")), annualize=True)
    assert isinstance(asked, MoneyWeightedReturn) and asked.annualized is not None
    assert "annualized_short_window" in asked.warnings
    # 1.01 ** (365 / 30) - 1 = 0.128637... (hand: e^(12.1667 * 0.00995033) - 1)
    assert abs(asked.annualized.value - Decimal("0.1286")) < Decimal("0.0001")


@pytest.mark.parametrize(
    ("items", "reason"),
    [
        ([(0, "-100")], "incomplete_flows"),
        ([(0, "-100"), (10, "-5")], "no_sign_change"),
        ([(0, "100"), (10, "-50"), (20, "60")], "no_equity"),
        ([(0, "0"), (10, "5")], "no_equity"),
        ([(0, "-1"), (1, "10000000")], "root_outside_domain"),
        ([(0, "-100"), (0, "100")], "incomplete_flows"),  # zero horizon
    ],
)
def test_mwr_unavailable(items: list[tuple[int, str]], reason: str) -> None:
    assert code(money_weighted_return(flows(*items))) == reason


def test_mwr_rejections() -> None:
    with pytest.raises(TypeError):
        DatedFlow(datetime(2025, 1, 1, tzinfo=UTC), usd(1))
    with pytest.raises(TypeError):
        DatedFlow(D0, 0.5)  # type: ignore[arg-type]
    with pytest.raises(CurrencyMismatchError):
        money_weighted_return(
            [DatedFlow(D0, usd(-1)), DatedFlow(D0 + timedelta(1), Money.of(2, "CAD"))]
        )
    with pytest.raises(ValueError, match="order"):
        money_weighted_return(flows((10, "-1"), (0, "2")))


def test_withdrawal_with_one_cumulative_sign_change_is_unique() -> None:
    # Net flows change sign 3 times (Descartes), cumulative sums once (Norstrom) and
    # the sums from the end never: one root. 0.18257822031780 is from an independent
    # float bisection run outside the repository (scratchpad), checked to 1e-12.
    items = ((0, "-1000"), (90, "200"), (180, "-500"), (365, "1500"))
    out = money_weighted_return(flows(*items))
    assert isinstance(out, MoneyWeightedReturn) and out.status == "unique"
    assert out.sign_changes == 3 and out.annualized is not None
    assert abs(out.annualized.value - Decimal("0.18257822031780")) < Decimal("1e-12")


def test_two_roots_of_opposite_sign_stay_ambiguous() -> None:
    # -100, +200, -50 yearly: cumulative sums change sign once (Norstrom applies to
    # r > 0 only) but the sums from the end change too. Roots r = +-1/sqrt(2).
    out = money_weighted_return(flows((0, "-100"), (365, "200"), (730, "-50")))
    assert isinstance(out, MoneyWeightedReturn) and out.status == "ambiguous"
    half_sqrt2 = Decimal("0.70710678118654752440")
    got = [r.annualized.value for r in out.roots if r.annualized is not None]
    assert len(got) == 2
    assert abs(got[0] + half_sqrt2) < Decimal("1e-15")
    assert abs(got[1] - half_sqrt2) < Decimal("1e-15")


def test_partial_sums_from_the_end_are_checked() -> None:
    # -100, +300, -150, +10 yearly: cumulative sums change sign once and the last
    # flow is positive, but sums from the end change twice: three roots (scratchpad
    # float scan: r = 1.38986, -0.46860, -0.92126).
    items = ((0, "-100"), (365, "300"), (730, "-150"), (1095, "10"))
    out = money_weighted_return(flows(*items))
    assert isinstance(out, MoneyWeightedReturn) and out.status == "ambiguous"
    want = (Decimal("-0.92126"), Decimal("-0.46860"), Decimal("1.38986"))
    got = [r.annualized.value for r in out.roots if r.annualized is not None]
    assert len(got) == 3
    assert all(abs(g - w) < Decimal("0.00001") for g, w in zip(got, want, strict=True))


def test_total_loss_is_minus_one() -> None:
    out = money_weighted_return(flows((0, "-1000"), (100, "-500"), (365, "0")))
    assert isinstance(out, MoneyWeightedReturn) and out.status == "unique"
    assert out.period_return == Ratio("-1") and out.annualized == Ratio("-1")


def test_growth_domain_is_a_parameter() -> None:
    items = ((0, "-1"), (1, "10000000"))
    assert code(money_weighted_return(flows(*items))) == "root_outside_domain"
    wide = (Decimal("0.5"), Decimal("1e8"))
    out = money_weighted_return(flows(*items), growth_domain=wide)
    assert isinstance(out, MoneyWeightedReturn) and out.growth_domain == wide
    assert out.period_return is not None
    assert abs(out.period_return.value - 9999999) < Decimal("1e-9")
    with pytest.raises(ValueError, match="domain"):
        money_weighted_return(flows(*items), growth_domain=(Decimal(2), Decimal(3)))


def test_non_convergence_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(returns, "MAX_ITERATIONS", 3)
    out = money_weighted_return(flows((0, "-1000"), (365, "1100")))
    assert isinstance(out, MoneyWeightedReturn) and "not_converged" in out.warnings


def test_long_ambiguous_series_is_fast() -> None:
    # 801 daily flows whose partial sums alternate in sign (Norstrom cannot apply):
    # the 5% grid scan runs on all of them.
    items = [(0, "-1000")] + [(d, "1500" if d % 2 else "-1500") for d in range(1, 800)]
    start = time.perf_counter()
    out = money_weighted_return(flows(*items, (800, "1000")))
    assert time.perf_counter() - start < 15
    assert isinstance(out, MoneyWeightedReturn) and out.status == "ambiguous"


@PROFILE
@given(st.integers(1, 10**9), st.integers(1, 10**9), st.integers(1, 3650))
def test_two_flow_root_matches_exact_ratio(a: int, b: int, days: int) -> None:
    out = money_weighted_return(flows((0, str(-a)), (days, str(b))))
    if not Fraction(1, 10**6) <= Fraction(b, a) <= 10**6:
        assert code(out) == "root_outside_domain"
        return
    assert isinstance(out, MoneyWeightedReturn) and out.period_return is not None
    exact = Fraction(b, a) - 1
    assert abs(Fraction(out.period_return.value) - exact) <= Fraction(1, 10**15)


@PROFILE
@given(
    st.lists(
        st.tuples(st.integers(1, 400), st.integers(1, 1000)), min_size=1, max_size=6
    ),
    st.integers(1, 10000),
)
def test_root_zeroes_npv(contribs: list[tuple[int, int]], pct: int) -> None:
    """One sign change (contributions, then a terminal value): a unique root whose
    NPV, evaluated here from the defining equation, is ~0 relative to its terms.
    Terminal = 1%..100x of contributions, after a gap at least as long as the
    contribution span, keeps 1 + p within [1e-4, 6e5]: the root is always in the
    default domain, so no input may come back unavailable."""
    day, items = 0, []
    for gap, amount in contribs:
        items.append((day, str(-amount)))
        day += gap
    day = 2 * day
    terminal = Decimal(sum(a for _, a in contribs) * pct).scaleb(-2)
    out = money_weighted_return(flows(*items, (day, str(terminal))))
    assert isinstance(out, MoneyWeightedReturn)
    assert out.status == "unique" and out.period_return is not None
    with localcontext(DOMAIN_CONTEXT):
        g = 1 + out.period_return.value
        terms = [Decimal(a) * g ** -(Decimal(d) / day) for d, a in items]
        terms.append(Decimal(terminal) / g)
        # Bounded by the discounted magnitudes: the reported Ratio has 18 places, so
        # near -100% the relative precision of 1 + p is what limits the residual.
        bound = sum((abs(t) for t in terms), Decimal(0)) * Decimal("1e-12")
        assert abs(sum(terms, Decimal(0))) <= bound


# ---- FX attribution


def test_num04_fx_interaction() -> None:
    inp, exp = ORACLES["NUM04"]["inputs"], ORACLES["NUM04"]["expected"]
    out = fx_attribution(Ratio(inp["local_return"]), Ratio(inp["fx_return"]))
    assert isinstance(out, FxAttribution)
    assert out.reporting == Ratio(exp["base_return"])  # 15.5%, not 15%
    assert out.interaction == Ratio(exp["interaction"])
    assert (out.local, out.currency) == (Ratio("0.10"), Ratio("0.05"))


def test_fx_attribution_bounds() -> None:
    assert code(fx_attribution(Ratio("-1"), Ratio("0.1"))) == "return_below_minus_one"
    with pytest.raises(TypeError):
        fx_attribution(Decimal("0.1"), Ratio("0"))  # type: ignore[arg-type]


ratios = st.integers(-99 * 10**16, 5 * 10**18).map(
    lambda n: Ratio(Decimal(n).scaleb(-18))
)


@PROFILE
@given(ratios, ratios)
def test_fx_effects_add_up(local: Ratio, fx: Ratio) -> None:
    out = fx_attribution(local, fx)
    assert isinstance(out, FxAttribution)
    exact = (1 + Fraction(local.value)) * (1 + Fraction(fx.value)) - 1
    assert abs(Fraction(out.reporting.value) - exact) <= Fraction(1, 2 * 10**18)
