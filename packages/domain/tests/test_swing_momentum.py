"""Swing/momentum reference STR-TREND-001 (T030 increment 1).

SYNTHETIC daily bars, splits, calendar (fixtures/xnys_synthetic.json) and accounts
only; no market data. Expected numbers are hand-computed in the comments. The
property's oracle is the same run on inputs pre-filtered to what was known at t.
Sessions 2026-10-05..09 (Mon-Fri, EDT): close 20:00Z, bars known 20:30Z.
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import rights as rt
from qw_domain.bars import Bar, BarBook, Ohlc, session_grid
from qw_domain.calendars import load_calendar
from qw_domain.corporate_actions import (
    CorporateActionLog,
    PositiveRatio,
    SourceRef,
    Split,
    StockDividend,
)
from qw_domain.decimals import Money, MoneyAmount, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.risk import Side
from qw_domain.strategy_gate import GateCode, GateInputs, actionable
from qw_domain.strategy_registry import (
    Family,
    Horizon,
    StrategyError,
    StrategyRegistry,
)
from qw_domain.streams import FeedLatency
from qw_domain.swing_momentum import (
    Held,
    SeriesBasis,
    SizingInputs,
    Status,
    TrendInputs,
    check_fill,
    evaluate,
    exit_decision,
    reference_entry,
    size_entry,
    trend_manifest,
)
from qw_domain.valuation import Mark, MarkKind

FIXTURES = Path(__file__).parent / "fixtures"
CAL = load_calendar((FIXTURES / "xnys_synthetic.json").read_text())
CATALOGUE = Path(__file__).parents[3] / "docs/spec/config/strategy_catalogue.json"
X, Y = InstrumentId(UUID(int=1)), InstrumentId(UUID(int=2))
FEED = "feed-synth-eod"
M = trend_manifest()
SMALL: dict[str, object] = {
    "momentum_sessions": 3, "trend_sessions": 5, "volatility_sessions": 2
}  # fmt: skip
D = [date(2026, 10, d) for d in (5, 6, 7, 8, 9)]
T = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)
NEXT_OPEN = datetime(2026, 10, 12, 13, 30, tzinfo=UTC)
SRC = SourceRef("src-synth-ca", "rec-1")


def bar(
    day: date, close: str, *, known: datetime | None = None, open_: str | None = None,
    retro: bool = False, inst: InstrumentId = X,
) -> Bar:  # fmt: skip
    slot = session_grid(CAL, day, None)[0]
    o, c = Price(open_ or close), Price(close)
    hi, lo = max(o.value, c.value), min(o.value, c.value)
    ohlc = Ohlc(o, Price(hi), Price(lo), c)
    when = known or slot.end + timedelta(minutes=30)
    return Bar(inst, FEED, FeedLatency.END_OF_DAY, slot, ohlc, Quantity("0"), None,
               when, retro)  # fmt: skip


def book(*bars: Bar) -> BarBook:
    b = BarBook()
    for x in sorted(bars, key=lambda x: x.knowledge_at):
        b = b.record(x)
    return b


def closes(
    values: list[str], days: list[date] = D, inst: InstrumentId = X
) -> list[Bar]:
    return [bar(d, v, inst=inst) for d, v in zip(days, values, strict=True)]


def split(eid: str, eff: date, known: datetime, n: int = 2, v: int = 1) -> Split:
    return Split(eid, v, X, eff, SRC, known, PositiveRatio(n, 1))


def ins(
    bars: list[Bar], *, t: datetime = T, cfg: dict[str, object] | None = None,
    log: CorporateActionLog | None = None, adjusted: dict[str, int] | None = None,
    universe: tuple[InstrumentId, ...] = (X,),
) -> TrendInputs:  # fmt: skip
    c = SMALL if cfg is None else cfg
    bases = {i: SeriesBasis(FEED, adjusted or {}) for i in universe}
    return TrendInputs(t, CAL, book(*bars), bases, log or CorporateActionLog(),
                       universe, c, M.config_hash(c))  # fmt: skip


BASE = ["10", "11", "12", "11", "13"]


def test_hand_computed_small_series() -> None:
    r = evaluate(ins(closes(BASE)))
    (s,) = r.signals
    assert r.blocked == () and s.status is Status.CANDIDATE and s.rank == 1
    assert s.signal_session == D[4] and s.close == Decimal(13)
    assert s.momentum == Decimal("0.181818181818181818")  # 13/11 - 1 = 2/11
    assert s.trend_mean == Decimal("11.4")  # (10+11+12+11+13)/5 = 57/5
    assert s.volatility == Decimal("1.5")  # (|11-12| + |13-11|)/2
    assert s.known_at == datetime(2026, 10, 9, 20, 30, tzinfo=UTC)
    assert s.earliest_action_at == NEXT_OPEN  # Friday close -> Monday open
    assert r.candidates == (s,)


def test_default_126_momentum_and_200_trend() -> None:
    last, t = date(2026, 11, 20), datetime(2026, 11, 20, 22, tzinfo=UTC)
    days = [CAL.add_trading_days(last, -k) for k in range(199, -1, -1)]
    bars = closes([str(100 + k) for k in range(200)], days)
    (s,) = evaluate(ins(bars, cfg={}, t=t)).signals
    # close 299, close 126 sessions earlier 173: 299/173 - 1 = 126/173
    assert s.momentum == Decimal("0.728323699421965318")
    assert s.trend_mean == Decimal("199.5")  # mean of 100..299
    assert s.status is Status.CANDIDATE
    (short,) = evaluate(ins(bars[1:], cfg={}, t=t)).signals
    assert short.status is Status.NO_SIGNAL
    assert short.reasons == ("insufficient_history:199/200",)


def test_catalogue_defaults_and_manifest() -> None:
    entry = next(
        s for s in json.loads(CATALOGUE.read_text())["strategies"]
        if s["id"] == "STR-TREND-001"
    )  # fmt: skip
    p = {x.name: x.default for x in M.parameters}
    assert p["momentum_sessions"] == entry["parameters"]["momentum_sessions"] == 126
    assert p["trend_sessions"] == entry["parameters"]["trend_sessions"] == 200
    assert (M.strategy_id, M.version) == (entry["id"], entry["version"])
    assert (M.family, M.horizon) == (Family.SWING_MOMENTUM, Horizon.SWING_POSITION)
    assert M.regular_session_only


def test_config_is_bound_to_the_adopted_hash() -> None:
    i = ins(closes(BASE))
    assert evaluate(replace(i, config_hash="0" * 64)).blocked == ("config_mismatch",)
    bad = replace(i, config={"trend_sessions": 1})
    assert evaluate(bad).blocked == ("config_invalid:config",)
    assert evaluate(bad).signals == ()
    with pytest.raises(StrategyError):
        SeriesBasis("bad feed id", {})


def test_gate_refuses_the_unqualified_manifest() -> None:
    m = trend_manifest("feed-synth-eod")
    reg = StrategyRegistry().register(m)
    gate = GateInputs(
        rt.Registry(), rt.UseScope.PERSONAL, "CA-ON", None, {}, frozenset()
    )
    d = actionable(reg, "tenant-synth-a", m.strategy_id, m.version, {}, T, gate)
    assert not d.allowed
    assert GateCode.OPERATIONAL_NOT_PASSED in {r.code for r in d.reasons}


def test_no_look_ahead_from_later_bars_corrections_or_actions() -> None:
    base = evaluate(ins(closes(BASE)))
    later = T + timedelta(minutes=1)
    noisy = [
        *closes(BASE),
        bar(date(2026, 10, 12), "1"),  # the next session
        bar(D[4], "2", known=later),  # a correction known after t
        bar(D[2], "50", known=later, retro=True),  # recovered after t
    ]
    log = CorporateActionLog()
    log.record(split("split-late", D[3], later))
    assert evaluate(ins(noisy, log=log)) == base


def test_split_applied_only_from_its_known_time() -> None:
    raw = closes(["20", "22", "24", "11", "13"])  # 2:1 effective D[3]
    log = CorporateActionLog()
    log.record(split("split-x", D[3], T - timedelta(days=1)))
    (s,) = evaluate(ins(raw, log=log)).signals
    assert (s.momentum, s.trend_mean) == (Decimal("0.181818181818181818"),
                                          Decimal("11.4"))  # fmt: skip
    late = CorporateActionLog()
    late.record(split("split-x", D[3], T + timedelta(hours=1)))  # announced after t
    (r,) = evaluate(ins(raw, log=late)).signals
    # raw: 13/22 - 1 < 0; mean (20+22+24+11+13)/5 = 18 > 13
    assert r.status is Status.NOT_ELIGIBLE
    assert r.reasons == ("momentum_non_positive", "close_not_above_trend")
    # A provider series already adjusted for the known split is not adjusted twice.
    (a,) = evaluate(ins(closes(BASE), log=log, adjusted={"split-x": 1})).signals
    assert a.momentum == Decimal("0.181818181818181818")


def test_split_adjustment_mismatch_refuses() -> None:
    log = CorporateActionLog()
    log.record(split("split-x", D[3], T + timedelta(hours=1)))
    (s,) = evaluate(ins(closes(BASE), log=log, adjusted={"split-x": 1})).signals
    assert s.status is Status.NO_SIGNAL  # basis applies a split unknown at t
    assert s.reasons == ("split_adjustment_mismatch:split-x",)
    known = CorporateActionLog()
    known.record(split("split-x", D[3], T - timedelta(days=2)))
    known.record(split("split-x", D[3], T - timedelta(days=1), n=3, v=2))
    (v,) = evaluate(ins(closes(BASE), log=known, adjusted={"split-x": 1})).signals
    assert v.reasons == ("split_adjustment_mismatch:split-x",)  # corrected ratio


def test_unsupported_action_in_window_blocks() -> None:
    log = CorporateActionLog()
    log.record(StockDividend("sd-1", 1, X, D[2], SRC, T - timedelta(days=9),
                             PositiveRatio(1, 20)))  # fmt: skip
    (s,) = evaluate(ins(closes(BASE), log=log)).signals
    assert s.reasons == ("corporate_action_unsupported:sd-1",)


def test_insufficient_history_missing_and_conflicting_sessions() -> None:
    (s,) = evaluate(ins(closes(BASE[1:], D[1:]))).signals
    assert (s.status, s.reasons) == (Status.NO_SIGNAL, ("insufficient_history:4/5",))
    hole = [b for b in closes(BASE) if b.slot.session_date != D[2]]
    (h,) = evaluate(ins(hole)).signals
    assert h.reasons == ("missing_session:2026-10-07",)  # never padded
    late = [*closes(BASE[:4], D[:4]), bar(D[4], "13", known=T + timedelta(hours=1))]
    (lt,) = evaluate(ins(late)).signals
    assert lt.reasons == ("missing_session:2026-10-09",)  # no stale substitute
    retro = bar(D[2], "12.5", retro=True, known=T - timedelta(minutes=5))
    (c,) = evaluate(ins([*closes(BASE), retro])).signals
    assert c.reasons == ("bar_conflict:2026-10-07",)
    (u,) = evaluate(replace(ins(closes(BASE)), bases={})).signals
    assert u.reasons == ("series_basis_unknown",)


def test_no_opportunity_records_reasons_and_no_candidates() -> None:
    bars = [*closes(BASE), *closes(["13", "12", "11", "12", "10"], inst=Y)]
    r = evaluate(ins(bars, universe=(Y, X)))
    assert [s.instrument_id for s in r.signals] == [X, Y]  # candidates first
    down = evaluate(ins(closes(["13", "12", "11", "12", "10"], inst=Y),
                        universe=(Y,)))  # fmt: skip
    assert down.candidates == ()
    assert down.signals[0].reasons == ("momentum_non_positive", "close_not_above_trend")


def test_ranking_by_momentum() -> None:
    bars = [*closes(BASE), *closes(["10", "10", "10", "10", "12"], inst=Y)]
    r = evaluate(ins(bars, universe=(X, Y)))
    # X: 2/11 = 0.1818; Y: 12/10 - 1 = 0.2
    assert [(s.instrument_id, s.rank) for s in r.candidates] == [(Y, 1), (X, 2)]


def test_next_session_fill_never_at_the_signal_close() -> None:
    i = ins([*closes(BASE)])
    (s,) = evaluate(i).signals
    close_at = session_grid(CAL, D[4], None)[0].end
    assert check_fill(s, close_at) == "same_close_fill"
    assert check_fill(s, T) == "same_close_fill"  # after hours, before the open
    assert check_fill(s, NEXT_OPEN) is None
    entry = bar(date(2026, 10, 12), "14", open_="13.5")
    at = datetime(2026, 10, 12, 21, tzinfo=UTC)
    assert reference_entry(s, i.calendar, book(*closes(BASE)), FEED, at) == (
        "entry_bar_not_known"
    )
    px = reference_entry(s, i.calendar, book(*closes(BASE), entry), FEED, at)
    assert px == Price("13.5")  # the entry-session open, not the signal close 13
    early = reference_entry(s, i.calendar, book(entry), FEED, T)
    assert early == "entry_bar_not_known"


def test_holiday_skips_to_the_next_session() -> None:
    days = [date(2026, 6, d) for d in (26, 29, 30)] + [date(2026, 7, 1),
                                                         date(2026, 7, 2)]  # fmt: skip
    t = datetime(2026, 7, 2, 22, tzinfo=UTC)
    (s,) = evaluate(ins(closes(BASE, days), t=t)).signals
    assert s.earliest_action_at == datetime(2026, 7, 6, 13, 30, tzinfo=UTC)  # 3rd off


def test_split_between_signal_and_entry_blocks() -> None:
    days = [date(2026, 6, d) for d in (26, 29, 30)] + [date(2026, 7, 1),
                                                         date(2026, 7, 2)]  # fmt: skip
    t = datetime(2026, 7, 2, 22, tzinfo=UTC)
    bars = closes(BASE, days)
    for eff in (date(2026, 7, 3), date(2026, 7, 6)):  # holiday gap; entry session
        log = CorporateActionLog()
        log.record(split("split-e", eff, t - timedelta(days=3)))
        (s,) = evaluate(ins(bars, t=t, log=log)).signals
        assert s.status is Status.NO_SIGNAL
        assert s.reasons == ("split_before_entry:split-e",)
    after = CorporateActionLog()  # effective after the entry session: not blocking
    after.record(split("split-e", date(2026, 7, 7), t - timedelta(days=3)))
    assert evaluate(ins(bars, t=t, log=after)).signals[0].rank == 1
    # A signal computed without the split is refused by sizing once it is known.
    (clean,) = evaluate(ins(bars, t=t)).signals
    late = CorporateActionLog()
    late.record(split("split-e", date(2026, 7, 6), t - timedelta(days=3)))
    est = Mark(X, Price("13"), "USD", MarkKind.ASK, t, "src-synth-mark")
    got = size_entry(clean, ins(bars, t=t, log=late), sizing(estimate=est))
    assert got == ("split_before_entry:split-e",)


def test_exits() -> None:
    i = ins(closes(BASE))
    hold = Held(X, D[0], Price("9"), ())
    e = exit_decision(hold, i)
    assert (e.exit, e.reasons, e.earliest_action_at) == (False, (), NEXT_OPEN)
    brk = exit_decision(hold, ins(closes(["10", "11", "12", "13", "11"])))
    assert brk.reasons == ("trend_break",)  # 11 < 57/5
    stop = exit_decision(replace(hold, stop=Price("13")), i)
    assert stop.reasons == ("stop_breach",)  # close 13 <= stop 13
    timed = exit_decision(
        hold,
        replace(
            i,
            config={**SMALL, "max_holding_sessions": 5},
            config_hash=M.config_hash({**SMALL, "max_holding_sessions": 5}),
        ),
    )
    assert timed.reasons == ("time_exit",)  # D[0]..D[4] = 5 sessions held
    inv = exit_decision(replace(hold, invalidations=("thesis",)), ins([]))
    assert (inv.exit, inv.reasons) == (True, ("invalidated:thesis",))
    unknown = exit_decision(hold, ins([]))
    assert unknown.exit is None and unknown.reasons == ("insufficient_history:0/5",)


def test_stop_is_split_adjusted_after_entry() -> None:
    log = CorporateActionLog()
    log.record(split("split-x", D[3], T - timedelta(days=1)))
    raw = closes(["20", "22", "24", "11", "13"])
    # stop 25 set before the 2:1 split becomes 12.5; adjusted close 13 > 12.5
    i = ins(raw, log=log)
    assert exit_decision(Held(X, D[0], Price("25"), ()), i).exit is False
    assert exit_decision(Held(X, D[0], Price("26"), ()), i).reasons == ("stop_breach",)


def usd(x: str) -> Money:
    return Money(MoneyAmount(x), "USD")


def sizing(**kw: object) -> SizingInputs:
    base: dict[str, object] = {
        "estimate": Mark(X, Price("13"), "USD", MarkKind.ASK, T, "src-synth-mark"),
        "market_max_age": timedelta(hours=1),
        "risk_budget": usd("100"), "available_cash": usd("300"),
        "lot": PositiveQuantity("1"), "fixed_cost": usd("1"), "unit_cost": usd("0.01"),
    }  # fmt: skip
    base.update(kw)
    return SizingInputs(**base)  # type: ignore[arg-type]


def test_volatility_sizing_hand_computed() -> None:
    i = ins(closes(BASE))
    (s,) = evaluate(i).signals
    c = size_entry(s, i, sizing())
    # stop 13 - 2 x 1.5 = 10; loss/unit 3 + 2 x 0.01 = 3.02; risk (100-2)/3.02 = 32.4
    # cash (300-1)/13.01 = 22.98 -> 22; cash 1 + 22 x 13.01 = 287.22
    assert not isinstance(c, tuple)
    assert (c.quantity, c.stop, c.cash_required) == (
        PositiveQuantity("22"), Price("10"), usd("287.22"))  # fmt: skip
    assert c.earliest_action_at == NEXT_OPEN
    rich = size_entry(s, i, sizing(available_cash=usd("1000")))
    assert not isinstance(rich, tuple)
    assert (rich.quantity, rich.cash_required) == (PositiveQuantity("32"),
                                                   usd("417.32"))  # fmt: skip
    a = rich.to_action(tenant_id="tenant-synth-a", account_id="acct-synth-1",
                       denominator="account_value", issuer_id="iss-x",
                       sector_id="sec-x", leveraged_or_inverse=False)  # fmt: skip
    assert (a.side, a.stop, a.horizon, a.strategy_id) == (
        Side.BUY, Price("10"), "swing_position", "STR-TREND-001")  # fmt: skip


def test_sizing_blocks_on_missing_or_stale_inputs() -> None:
    i = ins(closes(BASE))
    (s,) = evaluate(i).signals
    assert size_entry(s, i, sizing(risk_budget=None)) == ("risk_budget_missing",)
    assert size_entry(s, i, sizing(available_cash=None)) == ("cash_unknown",)
    old = Mark(X, Price("13"), "USD", MarkKind.ASK, T - timedelta(hours=2), "src-m")
    assert size_entry(s, i, sizing(estimate=old)) == ("estimate_stale",)
    fut = replace(old, observed_at=T + timedelta(seconds=1))
    assert size_entry(s, i, sizing(estimate=fut)) == ("estimate_not_yet_observed",)
    bid = replace(old, observed_at=T, kind=MarkKind.BID)
    assert size_entry(s, i, sizing(estimate=bid)) == ("estimate_unsuitable",)
    assert size_entry(s, i, sizing(available_cash=usd("10"))) == ("below_lot",)
    eur = Money(MoneyAmount("100"), "EUR")
    assert size_entry(s, i, sizing(risk_budget=eur)) == ("currency_mismatch",)
    tight = sizing(estimate=Mark(X, Price("2"), "USD", MarkKind.ASK, T, "src-m"))
    assert size_entry(s, i, tight) == ("stop_non_positive",)  # 2 - 3 <= 0
    down = evaluate(ins(closes(["13", "12", "11", "12", "10"]))).signals[0]
    assert size_entry(down, i, sizing()) == ("not_a_candidate",)


@settings(max_examples=120, deadline=None)
@given(
    st.lists(st.integers(1, 60), min_size=5, max_size=5),
    st.lists(st.tuples(st.integers(0, 6), st.integers(1, 60), st.integers(1, 72),
                       st.booleans()), max_size=6),
    st.lists(st.tuples(st.integers(1, 4), st.integers(-72, 72), st.integers(2, 4)),
             max_size=3),
)  # fmt: skip
def test_signal_at_t_ignores_everything_known_after_t(
    values: list[int],
    noise: list[tuple[int, int, int, bool]],
    splits: list[tuple[int, int, int]],
) -> None:
    days = [*D, date(2026, 10, 12), date(2026, 10, 13)]
    base = closes([str(v) for v in values])
    extra = [
        bar(days[d], str(c), known=max(T, session_grid(CAL, days[d], None)[0].end)
            + timedelta(hours=h), retro=r)
        for d, c, h, r in noise
    ]  # fmt: skip
    full, known = CorporateActionLog(), CorporateActionLog()
    for k, (d, hours, n) in enumerate(splits):
        sp = split(f"split-{k}", D[d], T + timedelta(hours=hours), n=n)
        full.record(sp)
        if sp.known_at <= T:
            known.record(sp)
    got = evaluate(ins([*base, *extra], log=full))
    assert got == evaluate(ins(base, log=known))
    for s in got.signals:
        assert s.known_at is None or s.known_at <= T
        assert s.earliest_action_at is None or s.earliest_action_at > T
