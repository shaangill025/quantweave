"""Benchmark cash-flow simulation (T018 increment 2; spec §5, §14, R064).

SYNTHETIC levels and flows only, not market data. Expectations are hand-traced in the
comments; the MWR value is from an independent float bisection run outside the
repository (scratchpad), checked to 1e-12.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.benchmark import (
    BenchmarkKind,
    BenchmarkLevel,
    BenchmarkPolicy,
    BenchmarkSeries,
    BenchmarkSimulation,
    Execution,
    Reinvestment,
    simulate_benchmark,
)
from qw_domain.decimals import CurrencyMismatchError, Money, Price, Quantity, Ratio
from qw_domain.instants import InstantError
from qw_domain.returns import ExternalFlow, MoneyWeightedReturn, PeriodReturn
from qw_domain.valuation import Unavailable

PROFILE = settings(derandomize=True, database=None, max_examples=60, deadline=None)
D0 = date(2026, 1, 5)
SELECTED = datetime(2025, 12, 31, 12, tzinfo=UTC)
TRI, PROXY = BenchmarkKind.TOTAL_RETURN_INDEX, BenchmarkKind.INVESTABLE_PROXY


def policy(
    kind: BenchmarkKind = TRI, selected: datetime = SELECTED, **kw: object
) -> BenchmarkPolicy:
    args: dict[str, object] = {
        "series_version": "2026.1",
        "reinvestment": Reinvestment.TOTAL_RETURN if kind is TRI else Reinvestment.NONE,
    }
    args.update(kw)
    return BenchmarkPolicy("bm", "1", "USD", kind, "SYN-TR", selected, **args)  # type: ignore[arg-type]


INDEX = policy()


def usd(v: str | int) -> Money:
    return Money.of(v, "USD")


def series(*levels: tuple[int, str], sid: str = "SYN-TR") -> BenchmarkSeries:
    rows = [
        BenchmarkLevel(
            D0 + timedelta(days=d),
            Price(v),
            datetime.combine(D0 + timedelta(days=d), time(21), UTC),
        )
        for d, v in levels
    ]
    return BenchmarkSeries(sid, "2026.1", "USD", tuple(rows))


def sim(
    s: BenchmarkSeries,
    opening: str,
    days: int,
    *items: tuple[int, str],
    policy: BenchmarkPolicy = INDEX,
) -> BenchmarkSimulation | Unavailable:
    fl = [ExternalFlow(D0 + timedelta(days=d), usd(a)) for d, a in items]
    return simulate_benchmark(
        policy, s, usd(opening), D0, D0 + timedelta(days=days), fl
    )


def code(out: object) -> str:
    assert isinstance(out, Unavailable)
    return out.code


def test_no_flows_equals_the_benchmark_return() -> None:
    out = sim(series((0, "100"), (30, "112.5")), "1000", 30)
    assert isinstance(out, BenchmarkSimulation)
    assert out.ending_value == usd("1125") and out.units == Quantity("10")
    assert isinstance(out.twr, PeriodReturn) and out.twr.value == Ratio("0.125")
    assert isinstance(out.mwr, MoneyWeightedReturn) and out.mwr.status == "unique"
    assert out.mwr.period_return is not None and out.mwr.annualized is None
    assert abs(out.mwr.period_return.value - Decimal("0.125")) < Decimal("1e-15")
    assert (out.series_id, out.series_version) == ("SYN-TR", "2026.1")


def test_flows_are_invested_at_the_flow_date_level() -> None:
    # 1000 at 100 -> 10 units; +550 at 110 (value before 1100) -> 15 units; -300 at 90
    # (value before 1350) -> 15 - 10/3 = 35/3 units; at 99: 35/3 x 99 = 1155.
    # TWR = 1.1 x (1350/1650) x (1155/1050) - 1 = 0.99 - 1 = -0.01, the index return.
    s = series((0, "100"), (28, "110"), (56, "90"), (85, "99"))
    out = sim(s, "1000", 85, (28, "550"), (56, "-300"))
    assert isinstance(out, BenchmarkSimulation)
    assert out.ending_value == usd("1155")
    assert isinstance(out.twr, PeriodReturn) and out.twr.value == Ratio("-0.01")
    assert out.twr.subperiods == (Ratio("0.1"), Ratio("-0.181818181818181818"),
                                  Ratio("0.1"))  # fmt: skip
    assert out.units == Quantity("11.666666666666")  # 35/3 floored at 1e-12
    # MWR on -1000, -550, +300, +1155 at days 0, 28, 56, 85 (independent oracle).
    assert isinstance(out.mwr, MoneyWeightedReturn) and out.mwr.status == "unique"
    assert out.mwr.period_return is not None
    want = Decimal("-0.074889079455126")
    assert abs(out.mwr.period_return.value - want) < Decimal("1e-12")
    assert [lv.on for lv in out.levels_used] == [D0 + timedelta(days=d)
                                                for d in (0, 28, 56, 85)]  # fmt: skip


def test_same_day_flows_are_netted() -> None:
    s = series((0, "100"), (10, "100"), (20, "120"))
    a = sim(s, "1000", 20, (10, "500"), (10, "-200"))
    b = sim(s, "1000", 20, (10, "300"))
    assert isinstance(a, BenchmarkSimulation) and isinstance(b, BenchmarkSimulation)
    assert a.ending_value == b.ending_value == usd("1560")  # 13 units x 120


@pytest.mark.parametrize(
    ("levels", "items", "reason"),
    [
        (((0, "100"), (30, "110")), ((10, "50"),), "benchmark_level_missing"),
        (((0, "100"), (10, "100")), (), "benchmark_level_missing"),  # no end level
        (((10, "100"), (30, "110")), (), "benchmark_level_missing"),  # no start
        (((0, "100"), (10, "100"), (30, "1")), ((10, "-1001"),), "negative_units"),
    ],
)
def test_unavailable(
    levels: tuple[tuple[int, str], ...], items: tuple[tuple[int, str], ...], reason: str
) -> None:
    assert code(sim(series(*levels), "1000", 30, *items)) == reason


def test_no_carry_forward_from_a_nearby_level() -> None:
    # A level the day before the flow is not used: the gap blocks the result.
    out = sim(series((0, "100"), (9, "100"), (30, "110")), "1000", 30, (10, "50"))
    assert code(out) == "benchmark_level_missing"


def test_policy_selected_late_is_unavailable() -> None:
    # 2026-01-05 begins at UTC+14 on 2026-01-04 10:00:00Z.
    s = series((0, "100"), (30, "110"))
    late = policy(selected=datetime(2026, 1, 4, 10, tzinfo=UTC))
    assert code(sim(s, "1000", 30, policy=late)) == "benchmark_selected_late"
    ok = policy(selected=datetime(2026, 1, 4, 9, 59, 59, tzinfo=UTC))
    assert isinstance(sim(s, "1000", 30, policy=ok), BenchmarkSimulation)


def test_series_version_is_pinned() -> None:
    out = sim(series((0, "100"), (30, "110")), "1000", 30,
              policy=policy(series_version="2025.9"))  # fmt: skip
    assert code(out) == "benchmark_version_mismatch"


def test_flows_outside_the_period_are_unavailable() -> None:
    s = series((0, "1"), (5, "1"))
    assert code(sim(s, "1", 5, (5, "1"))) == "flow_outside_period"  # end date
    early = [ExternalFlow(D0 - timedelta(days=1), usd("1"))]
    out = simulate_benchmark(INDEX, s, usd("1"), D0, D0 + timedelta(days=5), early)
    assert code(out) == "flow_outside_period"


def test_net_zero_flow_date_still_needs_a_level() -> None:
    out = sim(series((0, "100"), (30, "110")), "1000", 30, (10, "50"), (10, "-50"))
    assert code(out) == "benchmark_level_missing"


def test_investable_proxy_with_fees_and_whole_units() -> None:
    proxy = policy(PROXY, fee_per_trade=usd("1"), unit_increment=Quantity("1"))
    # Day 0: 1000 at 100, fee 1 -> floor(999/100) = 9 units, cash 99.
    # Day 10 at 105: value before 945 + 99 = 1044; withdraw 150 -> cash -51; sell
    # ceil((51 + 1)/105) = 1 unit -> 8 units, cash 53. Day 20 at 110: 880 + 53 = 933.
    s = series((0, "100"), (10, "105"), (20, "110"))
    out = sim(s, "1000", 20, (10, "-150"), policy=proxy)
    assert isinstance(out, BenchmarkSimulation)
    assert out.units == Quantity("8") and out.cash == usd("53")
    assert out.fees == usd("2") and out.ending_value == usd("933")
    want = Fraction(1044, 1000) * Fraction(933, 1044 - 150) - 1
    assert isinstance(out.twr, PeriodReturn)
    assert out.twr.value.value == Decimal(round(want * 10**18)).scaleb(-18)


def test_rejections() -> None:
    with pytest.raises(ValueError, match="fee"):
        policy(fee_per_trade=usd("1"))
    with pytest.raises(ValueError, match="increment"):
        policy(PROXY, fee_per_trade=usd("1"))
    with pytest.raises(ValueError, match="increment"):
        policy(PROXY, fee_per_trade=usd("1"), unit_increment=Quantity("0.5"))
    with pytest.raises(ValueError, match="next_close"):
        policy(execution="next_close")
    assert policy().execution is Execution.LEVEL_ON_FLOW_DATE
    with pytest.raises(ValueError, match="reinvestment"):  # proxy reinvesting
        policy(PROXY, fee_per_trade=usd("1"), unit_increment=Quantity("1"),
               reinvestment=Reinvestment.TOTAL_RETURN)  # fmt: skip
    with pytest.raises(ValueError, match="reinvestment"):
        policy(reinvestment=Reinvestment.NONE)
    with pytest.raises(TypeError):
        policy(reinvestment="total_return")
    with pytest.raises(InstantError):
        policy(selected=datetime(2025, 12, 1))  # noqa: DTZ001
    with pytest.raises(InstantError):
        BenchmarkLevel(D0, Price("1"), datetime(2026, 1, 5, 21))  # noqa: DTZ001
    with pytest.raises(TypeError):
        BenchmarkLevel(D0, Decimal("1.5"), SELECTED)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Price(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate"):
        series((0, "1"), (0, "2"))
    with pytest.raises(ValueError, match="series"):
        sim(series((0, "1"), (5, "1"), sid="OTHER"), "1", 5)
    with pytest.raises(CurrencyMismatchError):
        simulate_benchmark(INDEX, series((0, "1"), (5, "1")), Money.of("1", "CAD"),
                           D0, D0 + timedelta(days=5), [])  # fmt: skip
    assert code(sim(series((0, "1")), "1", 0)) == "incomplete_period"


levels = st.integers(1, 10**6).map(lambda n: Price(Decimal(n).scaleb(-2)))


@PROFILE
@given(
    st.lists(st.tuples(levels, st.integers(0, 10**6)), min_size=2, max_size=6),
    st.integers(1, 10**6),
)
def test_index_twr_is_flow_independent(
    path: list[tuple[Price, int]], opening: int
) -> None:
    # Contributions only, so units never go negative: the simulated TWR is the index
    # return and the end value is sum(flow_i x L_end / L_i), floored at 1e-12.
    s = BenchmarkSeries("SYN-TR", "2026.1", "USD", tuple(
        BenchmarkLevel(D0 + timedelta(days=i), p,
                       datetime.combine(D0 + timedelta(days=i), time(21), UTC))
        for i, (p, _) in enumerate(path)
    ))  # fmt: skip
    end = len(path) - 1
    items = [(i, str(c)) for i, (_, c) in enumerate(path[1:-1], 1) if c]
    out = sim(s, str(opening), end, *items)
    assert isinstance(out, BenchmarkSimulation) and isinstance(out.twr, PeriodReturn)
    first, last = Fraction(path[0][0].value), Fraction(path[-1][0].value)
    want = Fraction(last, first) - 1
    assert out.twr.value.value == Decimal(round(want * 10**18)).scaleb(-18)
    value = opening * Fraction(last, first) + sum(
        (Fraction(c) * Fraction(last, Fraction(path[i][0].value)) for i, c in items),
        Fraction(0),
    )
    assert 0 <= value - Fraction(out.ending_value.amount.value) < Fraction(1, 10**12)
