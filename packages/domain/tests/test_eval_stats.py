"""Evaluation statistics (T033 increment 1): mean, sample volatility, Sharpe-like
ratio, drawdown and the Newey-West (Bartlett) confidence interval for the mean.

All series are SYNTHETIC. Expected values are hand-computed in the comments or come
from docs/spec/tests/fixtures/numerical_oracles.json (NUM20).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from qw_domain.decimal_math import norm_cdf
from qw_domain.eval_stats import (
    Z95,
    StatsError,
    max_drawdown,
    mean,
    newey_west_interval,
    sample_volatility,
    summarize,
)

D = Decimal
ORACLES = Path(__file__).parents[3] / "docs/spec/tests/fixtures/numerical_oracles.json"
# SYNTHETIC net returns; mean 0.01, deviations 0.01, -0.02, 0.02, -0.01, 0.00.
R = [D("0.02"), D("-0.01"), D("0.03"), D("0.00"), D("0.01")]
TOL = D("1e-30")


def test_moments_match_hand_computation() -> None:
    assert mean(R) == D("0.01")
    # sum of squared deviations 0.0010 / (n - 1 = 4) = 0.00025
    vol = sample_volatility(R)
    assert vol is not None and abs(vol * vol - D("0.00025")) < TOL
    s = summarize(R, lags=1, z=Z95)
    # Sharpe-like = 0.01 / sqrt(0.00025) = sqrt(0.4) per period, not annualized
    assert s.sharpe_like is not None and abs(s.sharpe_like**2 - D("0.4")) < TOL
    assert sample_volatility([D("0.01")]) is None  # one observation: undefined


def test_drawdown_hand_and_numerical_oracle_num20() -> None:
    # equity 1.02 -> 1.0098 (peak 1.02): 1.0098 / 1.02 - 1 = -0.01
    assert max_drawdown(R) == D("-0.01")
    num20 = next(
        o for o in json.loads(ORACLES.read_text())["oracles"] if o["id"] == "NUM20"
    )
    i = num20["inputs"]
    unit_return = D(i["current_unit_value"]) / D(i["initial_unit_value"]) - 1
    assert max_drawdown([unit_return]) == D(num20["expected"]["drawdown"])
    assert max_drawdown([D("0.05"), D("0.01")]) == 0  # never below the peak


def test_newey_west_interval_matches_hand_computation() -> None:
    # gamma0 = (1+4+4+1+0)e-4 / 5 = 2e-4
    # gamma1 = (-2 -4 -2 + 0)e-4 / 5 = -1.6e-4
    # long-run variance = gamma0 + 2 * (1 - 1/2) * gamma1 = 4e-5
    # variance of the mean = 4e-5 / 5 = 8e-6 (exact in Decimal)
    ci = newey_west_interval(R, lags=1, z=Z95)
    assert (ci.method, ci.lags, ci.n) == ("newey_west_bartlett", 1, 5)
    assert ci.variance_of_mean == D("0.000008")
    assert abs(ci.std_error**2 - D("0.000008")) < TOL
    half = ci.upper - ci.mean
    assert abs((half / Z95) ** 2 - D("0.000008")) < TOL
    assert ci.mean - ci.lower == half and ci.mean == D("0.01")
    # With lags = 0 it is the iid variance gamma0 / n = 4e-5 (biased, divisor n).
    assert newey_west_interval(R, lags=0, z=Z95).variance_of_mean == D("0.00004")
    # Positive autocorrelation widens the interval relative to lags = 0.
    trend = [D("0.01"), D("0.02"), D("0.03"), D("0.04")]
    assert (
        newey_west_interval(trend, 1, Z95).variance_of_mean
        > newey_west_interval(trend, 0, Z95).variance_of_mean
    )


def test_z95_is_the_normal_975_quantile_independently() -> None:
    assert abs(norm_cdf(Z95) - D("0.975")) < D("1e-15")


def test_insufficient_samples_are_reported_not_invented() -> None:
    with pytest.raises(StatsError, match="lags"):
        newey_west_interval(R, lags=5, z=Z95)
    with pytest.raises(StatsError, match="observations"):
        newey_west_interval([D("0.01")], lags=0, z=Z95)
    with pytest.raises(StatsError, match="observations"):
        mean([])
    s = summarize([D("0.01")], lags=1, z=Z95)
    assert s.interval is None and s.sharpe_like is None
    assert s.limitations == ("insufficient_sample_for_interval",)
    flat = summarize([D("0.01")] * 3, lags=1, z=Z95)
    assert flat.sharpe_like is None  # zero volatility: no ratio
    with pytest.raises(TypeError):
        mean([0.01])  # type: ignore[list-item]
