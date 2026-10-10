"""Preselected baselines and cash-flow-matched scorecards (T042 increment 1; R064,
R090, R042, R043). SYNTHETIC journals, marks, levels and virtual books only, not
market data or real exports. Expected numbers are hand-computed in the comments; the
actual-side MWR is from an independent float bisection run outside the repository
(scratchpad), checked to 1e-12.

Fixture: period 2026-03-02 .. 2026-04-01 (30 days, reporting calendar UTC). The real
account deposits 1000 and buys 5 SYN at 100 before the period (opening NAV 1000),
deposits 500 on 2026-03-12 (day 10) and ends with SYN at 120: 600 + 1000 cash = 1600.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from decimal import Decimal as D
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from qw_domain.benchmark import (
    BenchmarkKind,
    BenchmarkLevel,
    BenchmarkPolicy,
    BenchmarkSeries,
    Reinvestment,
)
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import (
    CurrencyMismatchError,
    Money,
    Multiplier,
    PositiveQuantity,
    Price,
    Quantity,
    Ratio,
)
from qw_domain.identity import InstrumentId
from qw_domain.journal import Journal
from qw_domain.postings import Header as H
from qw_domain.postings import JournalEvent, buy, deposit, fee, interest, withdrawal
from qw_domain.returns import ApproximateReturn, ExternalFlow, MoneyWeightedReturn
from qw_domain.scorecard import (
    ActualInput,
    EvaluationPlan,
    MarketInputs,
    PlanError,
    Role,
    Scorecard,
    Side,
    build_scorecard,
    value_virtual,
)
from qw_domain.simulation import EntryKind, VirtualEntry, VirtualPortfolio
from qw_domain.sources import HoldingLine, SourceSnapshot
from qw_domain.valuation import Mark, MarkKind, Unavailable

SYN = InstrumentId(UUID("00000000-0000-4000-8000-0000000004a2"))
ACCT, OTHER, VACCT = "SYN-ACCT", "SYN-OTHER", "SYN-VIRTUAL"
START, END = date(2026, 3, 2), date(2026, 4, 1)
OPEN_AT = datetime(2026, 3, 2, 5, tzinfo=UTC)
END_AT = datetime(2026, 4, 1, 21, tzinfo=UTC)
DAY10 = datetime(2026, 3, 12, 15, tzinfo=UTC)
FROZEN = datetime(2026, 2, 27, 12, tzinfo=UTC)
UTCZ = ZoneInfo("UTC")
ONE = {SYN: Multiplier(1)}


def usd(v: str | int) -> Money:
    return Money.of(v, "USD")


def hdr(at: datetime, rec: str, acct: str = ACCT) -> H:
    return H(acct, at, SourceRef("SYN-BROKER", rec))


def policy(
    pid: str = "bm", selected: datetime = FROZEN - timedelta(days=7)
) -> BenchmarkPolicy:
    tri, tr = BenchmarkKind.TOTAL_RETURN_INDEX, Reinvestment.TOTAL_RETURN
    return BenchmarkPolicy(pid, "1", "USD", tri, "SYN-TR", selected,
                           series_version="2026.1", reinvestment=tr)  # fmt: skip


def plan(**kw: object) -> EvaluationPlan:
    args: dict[str, object] = dict(
        plan_id="plan-synth", version=1, currency="USD", start=START, end=END,
        tz=UTCZ, baselines=(policy(),), frozen_at=FROZEN,
    )  # fmt: skip
    args.update(kw)
    return EvaluationPlan(**args)  # type: ignore[arg-type]


def series(levels: dict[int, str] | None = None) -> dict[str, BenchmarkSeries]:
    # Index 100 on day 0, 110 on day 10, 121 on day 30.
    lv = levels if levels is not None else {0: "100", 10: "110", 30: "121"}
    days = {START + timedelta(d): v for d, v in lv.items()}
    rows = tuple(
        BenchmarkLevel(on, Price(v), datetime.combine(on, time(21), UTC))
        for on, v in days.items()
    )
    return {"SYN-TR": BenchmarkSeries("SYN-TR", "2026.1", "USD", rows)}


def history(*extra: JournalEvent, trade: bool = True) -> tuple[JournalEvent, ...]:
    j = Journal()
    pre = datetime(2026, 2, 25, 15, tzinfo=UTC)
    events = [deposit(hdr(pre, "dep-0"), usd(1000))]
    if trade:
        h = hdr(pre + timedelta(days=1), "buy-0")
        events.append(buy(h, SYN, PositiveQuantity(5), Price(100), usd(0)))
    events.append(deposit(hdr(DAY10, "dep-1"), usd(500)))
    for ev in sorted((*events, *extra), key=lambda e: e.effective_at):
        j.post(ev, ev.effective_at)
    return j.events()


def marks(at: datetime, price: str | None) -> dict[InstrumentId, Mark]:
    if price is None:
        return {}
    return {SYN: Mark(SYN, Price(price), "USD", MarkKind.LAST, at, "SYN-FEED")}


def mkt(at: datetime, price: str | None, age: timedelta = timedelta(0)) -> MarketInputs:
    return MarketInputs(at, marks(at - age, price), ONE, timedelta(hours=1))


def act(*extra: JournalEvent, trade: bool = True) -> ActualInput:
    return ActualInput(ACCT, history(*extra, trade=trade))


def vbook(*, flow: str = "500", flow_at: datetime = DAY10) -> VirtualPortfolio:
    # Virtual: capital 1000 at the opening, buys 8 SYN at 100 (cash -800, cost 800),
    # receives the matched 500 on day 10. End: 8 x 120 + 700 cash = 1660.
    p = VirtualPortfolio.open(VACCT, usd(1000), OPEN_AT)
    t, c = OPEN_AT + timedelta(hours=1), Decimal(flow)
    p = p.record(VirtualEntry("fill-1", EntryKind.FILL, t, SYN, VACCT,
                              cash=D(-800), units=D(8), cost=D(800)))  # fmt: skip
    cap = VirtualEntry("cap-2", EntryKind.CAPITAL, flow_at, None, VACCT, cash=c,
                       capital=c)  # fmt: skip
    return p.record(cap)


def card(**kw: object) -> Scorecard:
    args: dict[str, object] = dict(
        plan=plan(), actual=act(), opening=mkt(OPEN_AT, "100"),
        ending=mkt(END_AT, "120"), series=series(), virtual=vbook(),
    )  # fmt: skip
    args.update(kw)
    return build_scorecard(**args)  # type: ignore[arg-type]


def side(x: object) -> Side:
    assert isinstance(x, Side), x
    return x


def code(x: object) -> str:
    assert isinstance(x, Unavailable), x
    return x.code


def ratio(x: object) -> Decimal:
    assert isinstance(x, ApproximateReturn), x
    return x.value.value


# ---- baselines are preselected and version-pinned


def test_plan_must_be_frozen_before_the_period_begins_anywhere() -> None:
    begins = datetime.combine(START, time(), UTC) - timedelta(hours=14)
    assert plan(frozen_at=begins - timedelta(seconds=1)).version == 1
    with pytest.raises(PlanError) as e:
        plan(frozen_at=begins)
    assert e.value.code == "baseline_selected_late"


@pytest.mark.parametrize(
    ("kw", "err"),
    [
        ({"baselines": (policy(selected=FROZEN + timedelta(seconds=1)),)},
         "baseline_selected_after_freeze"),
        ({"baselines": (policy(), policy())}, "plan_invalid"),
        ({"baselines": ()}, "plan_invalid"),
        ({"end": START}, "plan_invalid"),
    ],
)  # fmt: skip
def test_plan_refusals(kw: dict[str, object], err: str) -> None:
    with pytest.raises(PlanError) as e:
        plan(**kw)
    assert e.value.code == err


def test_plan_currency_must_match_its_baselines() -> None:
    with pytest.raises(CurrencyMismatchError):
        plan(currency="CAD")


def test_revision_before_the_period_starts_is_a_new_version() -> None:
    p, at = plan(), FROZEN + timedelta(hours=1)
    p2 = p.revise((policy(), policy("bm2", selected=at)), at)
    assert (p2.version, p2.frozen_at) == (2, at)
    assert [b.id for b in p2.baselines] == ["bm", "bm2"]
    assert p.version == 1 and len(p.baselines) == 1  # the original is unchanged


@pytest.mark.parametrize(
    ("at", "err"),
    [(OPEN_AT, "baseline_change_after_start"),
     (FROZEN - timedelta(seconds=1), "revision_out_of_order")],
)  # fmt: skip
def test_revision_refusals(at: datetime, err: str) -> None:
    with pytest.raises(PlanError) as e:
        plan().revise((policy("bm2", selected=FROZEN - timedelta(days=9)),), at)
    assert e.value.code == err


def test_plan_digest_exposes_a_baseline_swap_at_the_same_version() -> None:
    swapped = replace(plan(), baselines=(policy("sneaky", selected=FROZEN),))
    assert swapped.version == 1 and swapped.digest != plan().digest
    assert plan().digest == plan().digest and len(plan().digest) == 64
    c = card(plan=swapped)
    assert (c.plan_digest, c.plan_frozen_at) == (swapped.digest, FROZEN)
    assert (c.plan_id, c.plan_version) == ("plan-synth", 1)
    assert [k for k, _ in card().baselines] == ["bm@1:SYN-TR@2026.1"]


# ---- deposits are flows, not returns


def test_deposit_only_account_shows_zero_return() -> None:
    # 1000 opening cash, +500 deposit, 1500 at the end: gain 0, Dietz 0, MWR 0.
    # The naive 1500/1000 - 1 = 50% must not appear.
    a = side(card(actual=act(trade=False), virtual=None).actual)
    assert (a.opening, a.ending, a.contributions) == (usd(1000), usd(1500), usd(500))
    assert a.investment_gain == usd(0)
    assert ratio(a.approx_return) == 0
    assert isinstance(a.mwr, MoneyWeightedReturn) and a.mwr.status == "unique"
    assert a.mwr.roots[0].period.value == 0


def test_actual_side_hand_computed() -> None:
    # gain 1600 - 1000 - 500 = 100; Dietz w = 20/30, 100 / (1000 + 500 x 2/3) = 0.075.
    a = side(card().actual)
    assert a.role is Role.ACTUAL and a.ref == "account:SYN-ACCT"
    assert (a.ending, a.investment_gain, a.withdrawals) == (usd(1600), usd(100), usd(0))
    assert ratio(a.approx_return) == Decimal("0.075")
    assert isinstance(a.approx_return, ApproximateReturn)
    assert a.approx_return.approximate is True
    assert isinstance(a.mwr, MoneyWeightedReturn)
    # Float bisection of -1000 y^0 - 500 y^10 + 1600 y^30 = 0 (scratchpad).
    err = abs(a.mwr.roots[0].period.value - Decimal("0.0752282492000"))
    assert err < Decimal("1e-12")


def test_baseline_simulates_the_same_flows() -> None:
    # 1000 at 100 = 10 units; 500 at 110 = 50/11 units; 160/11 x 121 = 1760.
    # gain 260; exact TWR 1.1 x 1.1 - 1 = 0.21; Dietz 260 / (4000/3) = 0.195.
    c = card()
    b = side(c.baselines[0][1])
    assert b.role is Role.BASELINE
    assert (b.opening, b.ending, b.investment_gain) == (usd(1000), usd(1760), usd(260))
    assert ratio(b.approx_return) == Decimal("0.195")
    assert b.exact_twr is not None and not isinstance(b.exact_twr, Unavailable)
    assert b.exact_twr.value == Ratio("0.21")
    assert c.flows == (ExternalFlow(date(2026, 3, 12), usd(500)),)


def test_simulated_side_and_comparisons_never_mix_actual_with_simulated() -> None:
    # Virtual: 1660 - 1000 - 500 = 160; Dietz 160 / (4000/3) = 0.12.
    c = card()
    s = side(c.simulated)
    assert s.role is Role.SIMULATED and s.ref == "virtual:SYN-VIRTUAL"
    assert (s.ending, s.investment_gain) == (usd(1660), usd(160))
    assert ratio(s.approx_return) == Decimal("0.12")
    # actual - baseline = 100 - 260; simulated - baseline = 160 - 260.
    assert c.gain_vs_baselines == (
        ("bm@1:SYN-TR@2026.1", Role.ACTUAL, usd(-160)),
        ("bm@1:SYN-TR@2026.1", Role.SIMULATED, usd(-100)),
    )
    assert c.scope == "account:SYN-ACCT" and c.full_financial_situation is False
    assert c.attribution == ("not_defined_by_spec:active_return_allocation",)
    assert "no significance or edge claim" in c.interpretation


def test_reversed_deposit_nets_out_and_other_accounts_are_ignored() -> None:
    j = Journal()
    for ev in history():
        j.post(ev, ev.effective_at)
    extra = deposit(hdr(DAY10 + timedelta(days=2), "dep-x"), usd(300))
    j.post(extra, extra.effective_at)
    rev = SourceRef("SYN-BROKER", "rev-x")
    j.reverse(extra.event_id, rev, DAY10 + timedelta(days=3))
    other = deposit(hdr(DAY10, "dep-o", OTHER), usd(9999))
    j.post(other, DAY10 + timedelta(days=4))
    c = card(actual=ActualInput(ACCT, j.events()))
    assert c.flows == (ExternalFlow(date(2026, 3, 12), usd(500)),)
    assert side(c.actual).investment_gain == usd(100)


def at_day(d: int) -> datetime:
    return OPEN_AT + timedelta(days=d, hours=10)


@pytest.mark.parametrize(
    ("extra", "flows", "gain", "fees"),
    [
        # 200 withdrawn on day 20: NAV 1400; gain 1400 - 1000 - 300 = 100.
        ((withdrawal(hdr(at_day(20), "wd-1"), usd(200)),),
         {10: 500, 20: -200}, 100, 0),
        # Review (a): +100 day 5, -100 day 7 are both flows; gain stays 100.
        ((deposit(hdr(at_day(5), "d5"), usd(100)),
          withdrawal(hdr(at_day(7), "w7"), usd(100))),
         {5: 100, 7: -100, 10: 500}, 100, 0),
        # Review (b): +100 and a fee 100 the same day: NAV 1600, flows 600; gain 0.
        ((deposit(hdr(at_day(5), "d5"), usd(100)), fee(hdr(at_day(5), "f5"), usd(100))),
         {5: 100, 10: 500}, 0, 100),
    ],
)  # fmt: skip
def test_flows_and_gain_come_from_the_same_history(
    extra: tuple[JournalEvent, ...], flows: dict[int, int], gain: int, fees: int
) -> None:
    a = side(card(actual=act(*extra), virtual=None).actual)
    assert a.investment_gain == usd(gain) and a.fees == usd(fees)
    want = tuple(ExternalFlow(START + timedelta(d), usd(v)) for d, v in flows.items())
    assert card(actual=act(*extra), virtual=None).flows == want


def test_flow_date_moves_with_the_recorded_event() -> None:
    # Review (c): the 500 recorded on day 11 (not 10): Dietz 100 / (1000 + 500 x
    # 19/30) = 3000/39500. The day-11 level 110 is supplied for the baseline.
    late = deposit(hdr(at_day(11), "dep-1"), usd(500))
    ev = (*history()[:2], late)
    c = card(actual=ActualInput(ACCT, ev), virtual=None,
             series=series({0: "100", 11: "110", 30: "121"}))  # fmt: skip
    assert c.flows == (ExternalFlow(date(2026, 3, 13), usd(500)),)
    assert ratio(side(c.actual).approx_return) == Decimal("0.075949367088607595")


def test_income_and_standalone_fees_are_internal_and_shown() -> None:
    # Interest 10 and a fee 3 on day 15: NAV 1607; gain 1607 - 1000 - 500 = 107.
    t = at_day(15)
    extra = (interest(hdr(t, "int-1"), usd(10)), fee(hdr(t, "fee-1"), usd(3)))
    a = side(card(actual=act(*extra), virtual=None).actual)
    assert (a.income, a.fees, a.investment_gain) == (usd(10), usd(3), usd(107))


def test_flow_dates_use_the_plan_time_zone() -> None:
    # 2026-03-12T02:00Z is 2026-03-11 22:00 in New York (EDT).
    ev = (
        *history()[:2],
        deposit(hdr(datetime(2026, 3, 12, 2, tzinfo=UTC), "d"), usd(500)),
    )
    c = card(plan=plan(tz=ZoneInfo("America/New_York")), actual=ActualInput(ACCT, ev),
             virtual=None, series=series({0: "100", 9: "110", 30: "121"}))  # fmt: skip
    assert c.flows == (ExternalFlow(date(2026, 3, 11), usd(500)),)


# ---- no reconstruction, no fabricated flows, no zero for missing data


def test_snapshot_history_is_refused_and_nothing_downstream_is_fabricated() -> None:
    line = HoldingLine(SYN, "USD", Quantity(5))
    snap = SourceSnapshot("SYN-MIRROR", "ref-1", DAY10, (line,), (usd(1500),), True)
    c = card(actual=ActualInput(ACCT, (*history(), snap)))
    assert code(c.actual) == "snapshot_not_history"
    assert c.flows == () and c.gain_vs_baselines == ()
    assert code(c.simulated) == "actual_unavailable"
    assert code(c.baselines[0][1]) == "actual_unavailable"


def test_records_of_the_wrong_kind_are_type_errors() -> None:
    ve = VirtualEntry("v", EntryKind.CAPITAL, DAY10, None, VACCT, cash=D(1),
                      capital=D(1))  # fmt: skip
    with pytest.raises(TypeError):
        card(actual=ActualInput(ACCT, (*history(), ve)))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        value_virtual(Journal(), mkt(END_AT, None))  # type: ignore[arg-type]
    with pytest.raises(ValueError):  # N7: one id cannot be both real and virtual
        card(virtual=VirtualPortfolio.open(ACCT, usd(1000), OPEN_AT))


def test_missing_or_stale_marks_are_unavailable_not_zero() -> None:
    c = card(ending=mkt(END_AT, None))
    assert (code(c.actual), code(c.simulated)) == ("mark_missing", "actual_unavailable")
    stale = card(ending=mkt(END_AT, "120", timedelta(hours=2)))
    assert code(stale.actual) == "mark_stale"
    gap = stale.baselines[0][1]
    assert code(gap) == "actual_unavailable" and "mark_stale" in gap.reason  # type: ignore[union-attr]
    assert code(value_virtual(vbook(), mkt(END_AT, None))) == "mark_missing"


def test_missing_level_on_flow_date_is_not_interpolated() -> None:
    c = card(series=series({0: "100", 9: "105", 11: "115", 30: "121"}))
    assert code(c.baselines[0][1]) == "benchmark_level_missing"
    assert c.gain_vs_baselines == ()
    assert side(c.actual).investment_gain == usd(100)  # other sides still reported


def test_missing_series_and_version_mismatch() -> None:
    assert code(card(series={}).baselines[0][1]) == "benchmark_series_missing"
    bad = {"SYN-TR": replace(series()["SYN-TR"], version="2026.2")}
    assert code(card(series=bad).baselines[0][1]) == "benchmark_version_mismatch"


def test_unmatched_virtual_flows_or_opening_are_refused() -> None:
    assert code(card(virtual=vbook(flow="400")).simulated) == "cash_flows_not_matched"
    moved = vbook(flow_at=DAY10 + timedelta(days=1))
    assert code(card(virtual=moved).simulated) == "cash_flows_not_matched"
    p = VirtualPortfolio.open(VACCT, usd(900), OPEN_AT)
    c = card(virtual=p)
    assert code(c.simulated) == "opening_not_matched"
    assert side(c.actual).investment_gain == usd(100)


def test_flow_on_the_end_date_and_off_period_valuations() -> None:
    late = deposit(hdr(END_AT - timedelta(hours=2), "dep-end"), usd(10))
    assert code(card(actual=act(late)).actual) == "flow_on_end_date"
    early = mkt(OPEN_AT - timedelta(days=1), "100")
    assert code(card(opening=early).actual) == "valuation_date_mismatch"
    with pytest.raises(CurrencyMismatchError):
        card(plan=replace(plan(), currency="USD"), virtual=VirtualPortfolio.open(
            VACCT, Money.of(1000, "CAD"), OPEN_AT))  # fmt: skip
