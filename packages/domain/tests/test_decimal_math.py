"""Decimal exp/ln/sqrt/normal CDF (T034). Oracles: standard normal table values
(independently computed with IEEE-double erf; checked to 1e-15)."""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import decimal_math as dm

PROFILE = settings(derandomize=True, database=None, max_examples=200, deadline=None)
D = Decimal


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        ("0", "0.5"),
        ("1", "0.8413447460685429"),
        ("-1", "0.15865525393145707"),
        ("1.96", "0.9750021048517796"),
        ("2", "0.9772498680518208"),
        ("-3", "0.0013498980316301035"),
        ("5", "0.9999997133484282"),
    ],
)
def test_norm_cdf_matches_table(x: str, expected: str) -> None:
    assert abs(dm.norm_cdf(D(x)) - D(expected)) < D("1e-15")


def test_norm_cdf_tails_and_known_constants() -> None:
    assert dm.norm_cdf(D(15)) == 1 and dm.norm_cdf(D(-15)) == 0
    # Phi(-14) is about 7.8e-45: computed with guard digits, not cancelled to zero.
    assert D("7.7e-45") < dm.norm_cdf(D(-14)) < D("7.9e-45")
    assert abs(dm.norm_pdf(D(0)) - D("0.3989422804014326779399460599343818684759")) < (
        dm.TOLERANCE
    )
    assert abs(dm.exp(D(1)) - D("2.718281828459045235360287471352662497757")) < (
        dm.TOLERANCE
    )
    assert abs(dm.ln(dm.exp(D(3))) - 3) < dm.TOLERANCE
    assert abs(dm.CTX.power(dm.sqrt(D(2)), 2) - 2) < dm.TOLERANCE
    assert dm.exp(D(-3_000_000)) == 0  # documented underflow to zero


@PROFILE
@given(st.decimals(min_value=-20, max_value=20, places=6))
def test_norm_cdf_symmetric_and_monotone(x: Decimal) -> None:
    lo, hi = dm.norm_cdf(x), dm.norm_cdf(x + D("0.001"))
    assert D(0) <= lo <= hi <= D(1)
    assert abs(lo + dm.norm_cdf(-x) - 1) < dm.TOLERANCE


def test_rejects_float_and_bad_domain() -> None:
    with pytest.raises(TypeError):
        dm.norm_cdf(0.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dm.exp(D("NaN"))
    with pytest.raises(ValueError, match="non-positive"):
        dm.ln(D(0))
    with pytest.raises(ValueError, match="negative"):
        dm.sqrt(D(-1))


# Owner decision 35: the inverse normal for the Bonferroni-widened z. Oracles are
# standard normal quantiles (published tables; scipy.stats.norm.ppf agrees to 1e-15).
@pytest.mark.parametrize(
    ("p", "expected"),
    [
        ("0.5", "0"),
        ("0.9", "1.2815515655446004"),
        ("0.95", "1.6448536269514722"),
        ("0.975", "1.959963984540054"),
        ("0.99", "2.3263478740408408"),
        ("0.995", "2.5758293035489004"),
        ("0.999", "3.090232306167813"),
        ("0.9999", "3.719016485455681"),  # first written ...709 (a recall slip);
        # the round trip below fixes Phi at it to 1e-30
        ("0.025", "-1.959963984540054"),
        ("0.001", "-3.090232306167813"),
    ],
)
def test_norm_ppf_matches_known_quantiles(p: str, expected: str) -> None:
    assert abs(dm.norm_ppf(D(p)) - D(expected)) < D("1e-14")


def test_norm_ppf_inverts_norm_cdf_over_a_grid() -> None:
    grid = [D("1e-12"), D("1e-6"), D("0.001"), D("0.02425"), D("0.0243"),
            *(D(k) / 20 for k in range(1, 20)), D("0.97575"), D("0.999999"),
            1 - D("1e-12")]  # fmt: skip
    for p in grid:  # both region boundaries of the starting approximation
        x = dm.norm_ppf(p)
        assert abs(dm.norm_cdf(x) - p) < D("1e-30"), p
        assert abs(x + dm.norm_ppf(1 - p)) < D("1e-28"), p  # symmetric
    z = D("1.959963984540054")
    assert abs(dm.norm_ppf(dm.norm_cdf(z)) - z) < D("1e-30")  # two-sided


@given(st.decimals(min_value=D("0.0001"), max_value=D("0.9999"), places=6))
@PROFILE
def test_norm_ppf_round_trips_and_is_monotone(p: Decimal) -> None:
    x = dm.norm_ppf(p)
    assert abs(dm.norm_cdf(x) - p) < D("1e-30")
    assert dm.norm_ppf(p + D("0.00001")) > x


def test_norm_ppf_refuses_outside_the_open_unit_interval() -> None:
    for p in (D(0), D(1), D("-0.1"), D("1.1"), D("1e-60"), 1 - D("1e-60")):
        with pytest.raises(ValueError, match="norm_ppf"):
            dm.norm_ppf(p)
    for bad in (0.5, D("NaN")):
        with pytest.raises(TypeError):
            dm.norm_ppf(bad)  # type: ignore[arg-type]
