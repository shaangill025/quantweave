"""Opening-range breakout reference STR-ORB-001 (T031 increment 1; R050, R066, R091).

SYNTHETIC bars, feeds, halts and calendar (fixtures/xnys_synthetic.json); no market
data. Hand values: 2026-10-08 is EDT, regular 13:30Z-20:00Z, so the default range is
[13:30Z, 13:45Z), complete at 13:45:30Z (30 s buffer); exit deadline 20:00 - 10 min =
19:50Z; entry deadline 19:50 - 30 min = 19:20Z. 2026-11-27 closes early at 13:00 EST =
18:00Z (open 14:30Z): exit 17:50Z, entry 17:20Z. 2026-11-26 is a fixture holiday.
Range bars are flat at 100.00 except 13:33 (high 101.20) and 13:40 (low 99.40), so
range_high = 101.20 and range_low = 99.40. With tick 0.01 and one breakout tick the
threshold is 101.21; the 13:46 bar closes at 101.50, the first close at or above it.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ingest_rights_helper import REGION, TENANT, qualified_registry
from qw_domain.bars import Bar, BarBook, Gap, Ohlc, SessionLabel, Slot
from qw_domain.calendars import Halt, load_calendar
from qw_domain.decimals import Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.opening_range import (
    OrbCode,
    OrbInputs,
    OrbResult,
    check_review,
    evaluate,
    orb_manifest,
)
from qw_domain.rights import Use, UseScope
from qw_domain.strategy_registry import Family, Horizon
from qw_domain.streams import FeedLatency
from test_strategy_gate import FEED as GATE_FEED
from test_strategy_gate import G, codes, decide, investment_policy, world

FIXTURE = Path(__file__).parent / "fixtures" / "xnys_synthetic.json"
CAL = load_calendar(FIXTURE.read_text())
DAY, EARLY, HOLIDAY = date(2026, 10, 8), date(2026, 11, 27), date(2026, 11, 26)
X, Y = InstrumentId(UUID(int=1)), InstrumentId(UUID(int=2))
SIP, IEX = "feed-synth-sip", "feed-synth-iex"
RT = FeedLatency.REALTIME_CONSOLIDATED
MIN = timedelta(minutes=1)
LAG = timedelta(seconds=2)
REG = qualified_registry((SIP, IEX))
M = orb_manifest(SIP)
C = OrbCode


def utc(h: int, m: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=UTC)


def bar(
    start: datetime,
    hi: str = "100.00",
    lo: str = "100.00",
    close: str = "100.00",
    feed: str = SIP,
    latency: FeedLatency = RT,
    known: datetime | None = None,
    retro: bool = False,
    inst: InstrumentId = X,
) -> Bar:
    slot = Slot(SessionLabel.REGULAR, start.date(), start, start + MIN)
    ohlc = Ohlc(Price(lo), Price(hi), Price(lo), Price(close))  # open = low
    at = known or start + MIN + LAG
    return Bar(inst, feed, latency, slot, ohlc, Quantity("10"), None, at, retro)


def range_bars(open_at: datetime, **kw: Any) -> list[Bar]:
    out = []
    for i in range(15):
        hi, lo = {3: ("101.20", "100.00"), 10: ("100.00", "99.40")}.get(
            i, ("100.00", "100.00")
        )
        out.append(bar(open_at + i * MIN, hi, lo, **kw))
    return out


OPEN = utc(13, 30)
BREAKOUT = bar(utc(13, 46), "101.60", "100.90", "101.50")
BASE = [*range_bars(OPEN), bar(utc(13, 45), "101.20", "100.50", "101.20"), BREAKOUT]
DECIDE = utc(13, 47, 30)  # breakout known 13:47:02; review deadline 13:48:02


def book(bars: list[Bar]) -> BarBook:
    b = BarBook()
    for x in bars:
        b = b.record(x)
    return b


def inputs(bars: list[Bar] = BASE, **kw: Any) -> OrbInputs:
    base = OrbInputs(
        instrument_id=X, session_date=DAY, feed_id=SIP, calendar=CAL,
        book=book(bars), gaps=(), continuity_through=utc(19, 0), halts=(),
        tick_size=Price("0.01"), rights=REG, tenant_id=TENANT,
        scope=UseScope.PERSONAL, jurisdiction=REGION, config={},
        config_hash=M.config_hash({}), as_of=DECIDE,
    )  # fmt: skip
    return replace(base, **kw)


def reason_codes(r: OrbResult) -> set[OrbCode]:
    return {x.code for x in r.reasons}


# --- breakout with hand-computed range, stop and deadlines ---------------------------


def test_breakout_candidate_hand_values() -> None:
    r = evaluate(inputs())
    assert r.reasons == ()
    assert r.range is not None and r.signal is not None and r.candidate is not None
    rg, c = r.range, r.candidate
    assert (rg.start, rg.end, rg.frozen_at) == (OPEN, utc(13, 45), utc(13, 45, 30))
    assert (rg.high, rg.low) == (Decimal("101.20"), Decimal("99.40"))
    assert (rg.feed_id, rg.calendar_version) == (SIP, "synthetic-2026.1")
    assert c.trigger_at == utc(13, 47, 2)
    assert c.entry_reference == Decimal("101.50")
    assert c.adverse_exit_scenario == Decimal("99.40")  # range low: not a stop fill
    assert c.risk_per_unit == Decimal("2.10")
    assert c.target is None  # the catalogue declares a time exit, no price target
    assert c.review_deadline == utc(13, 48, 2)  # known + 60 s
    assert (c.entry_deadline, c.exit_deadline) == (utc(19, 20), utc(19, 50))
    assert c.config_hash == M.config_hash({})
    assert c.manifest_hash == M.material_hash


def test_close_at_range_high_is_not_a_breakout() -> None:
    # 13:45 closes at 101.20 < 101.21; seen alone it gives no signal and no reason.
    r = evaluate(inputs(BASE[:-1], as_of=utc(13, 46, 30)))
    assert (r.reasons, r.signal, r.candidate) == ((), None, None)
    assert r.range is not None


def test_breakout_ticks_parameter_raises_the_threshold() -> None:
    cfg = {"breakout_ticks": 50}  # 101.20 + 0.50 = 101.70 > 101.50
    r = evaluate(inputs(config=cfg, config_hash=M.config_hash(cfg)))
    assert r.signal is None and r.candidate is None


def test_zero_range_blocks() -> None:
    flat = [bar(OPEN + i * MIN) for i in range(15)] + BASE[15:]
    assert reason_codes(evaluate(inputs(flat))) == {C.RANGE_ZERO}


# --- partial ranges -------------------------------------------------------------------


def test_before_completion_buffer_is_not_complete() -> None:
    r = evaluate(inputs(as_of=utc(13, 45, 29)))
    assert reason_codes(r) == {C.RANGE_NOT_COMPLETE} and r.range is None
    assert evaluate(inputs(BASE[:16], as_of=utc(13, 45, 30))).range is not None


def test_missing_interval_makes_range_partial() -> None:
    bars = [b for b in BASE if b.start != utc(13, 38)]
    r = evaluate(inputs(bars))
    assert reason_codes(r) == {C.RANGE_INTERVAL_MISSING}
    assert r.range is None and r.signal is None and r.candidate is None


def test_bar_known_after_the_buffer_is_missing_at_freeze() -> None:
    late = bar(utc(13, 38), known=utc(13, 45, 31))
    bars = [late if b.start == utc(13, 38) else b for b in BASE]
    assert reason_codes(evaluate(inputs(bars))) == {C.RANGE_INTERVAL_MISSING}


def test_sequence_gap_makes_range_partial() -> None:
    gap = Gap(utc(13, 36, 10), utc(13, 36, 40), "sequence_gap")
    assert reason_codes(evaluate(inputs(gaps=(gap,)))) == {C.RANGE_GAP}


def test_unconfirmed_retrospective_interval_makes_range_partial() -> None:
    retro = bar(utc(13, 38), retro=True)
    bars = [retro if b.start == utc(13, 38) else b for b in BASE]
    assert reason_codes(evaluate(inputs(bars))) == {C.RANGE_INTERVAL_UNCONFIRMED}


def test_trailing_loss_makes_range_partial() -> None:
    r = evaluate(inputs(continuity_through=utc(13, 44, 30)))
    assert reason_codes(r) == {C.RANGE_TRAILING_LOSS}
    assert reason_codes(evaluate(inputs(continuity_through=None))) == {
        C.RANGE_TRAILING_LOSS
    }


def test_correction_before_freeze_is_used_after_freeze_blocks() -> None:
    fixed = replace(BASE[3], ohlc=Ohlc(*(Price(p) for p in ("100", "101.30", "100",
                                                         "100"))))  # fmt: skip
    early = evaluate(inputs([*BASE, replace(fixed, knowledge_at=utc(13, 45, 10))]))
    assert early.reasons == () and early.range is not None
    assert early.range.high == Decimal("101.30")
    late = evaluate(inputs([*BASE, replace(fixed, knowledge_at=utc(13, 46))]))
    assert reason_codes(late) == {C.RANGE_CORRECTED}


# --- provider switch, halts, sessions -------------------------------------------------


def test_second_feed_inside_range_blocks() -> None:
    r = evaluate(inputs([*BASE, bar(utc(13, 40), feed=IEX)]))
    assert reason_codes(r) == {C.FEED_SWITCH} and r.range is None


def test_feed_change_during_range_blocks() -> None:
    bars = [replace(b, feed_id=IEX) if b.start >= utc(13, 40) else b for b in BASE]
    assert C.FEED_SWITCH in reason_codes(evaluate(inputs(bars)))


def test_breakout_on_another_feed_blocks() -> None:
    bars = [*BASE[:-1], replace(BREAKOUT, feed_id=IEX)]
    r = evaluate(inputs(bars))
    assert C.FEED_SWITCH in reason_codes(r) and r.candidate is None


def test_halt_during_range_or_before_entry_blocks() -> None:
    during = Halt(utc(13, 35), utc(13, 37), X, "SYNTHETIC LULD")
    before_entry = Halt(utc(13, 46, 30), utc(13, 46, 50), None, "SYNTHETIC market")
    for h in (during, before_entry):
        r = evaluate(inputs(halts=(h,)))
        assert reason_codes(r) == {C.HALTED} and r.candidate is None
    open_ended = Halt(utc(13, 20), None, X, "SYNTHETIC pending news")
    assert reason_codes(evaluate(inputs(halts=(open_ended,)))) == {C.HALTED}
    other = Halt(utc(13, 35), utc(13, 37), Y, "SYNTHETIC other name")
    ended = Halt(utc(13, 0), utc(13, 30), X, "SYNTHETIC ended at the open")
    assert evaluate(inputs(halts=(other, ended))).candidate is not None


def test_half_day_deadlines_come_from_the_early_close() -> None:
    o = utc(14, 30, day=EARLY)
    bars = [*range_bars(o), bar(o + 15 * MIN, "101.60", "100.90", "101.50")]
    r = evaluate(inputs(bars, session_date=EARLY, as_of=utc(14, 46, 30, day=EARLY),
                        continuity_through=utc(15, 0, day=EARLY)))  # fmt: skip
    assert r.candidate is not None
    c = r.candidate
    assert c.exit_deadline == utc(17, 50, day=EARLY)
    assert c.entry_deadline == utc(17, 20, day=EARLY)
    assert c.review_deadline == utc(14, 47, 2, day=EARLY)


def test_holiday_has_no_candidate() -> None:
    r = evaluate(inputs([], session_date=HOLIDAY, as_of=utc(16, 0, day=HOLIDAY)))
    assert reason_codes(r) == {C.NO_SESSION} and r.range is None


def test_breakout_after_entry_deadline_blocks() -> None:
    n = int((utc(19, 21) - OPEN) / MIN)  # flat bars through 19:20, breakout 19:20
    bars = [*range_bars(OPEN), *(bar(OPEN + i * MIN) for i in range(15, n - 1))]
    bars.append(bar(utc(19, 20), "101.60", "100.90", "101.50"))
    r = evaluate(inputs(bars, as_of=utc(19, 21, 10), continuity_through=utc(19, 30)))
    assert reason_codes(r) == {C.ENTRY_WINDOW_CLOSED} and r.candidate is None


def test_halt_boundaries_are_half_open() -> None:
    at_decision = Halt(DECIDE, DECIDE + MIN, X, "SYNTHETIC starts at as_of")
    assert reason_codes(evaluate(inputs(halts=(at_decision,)))) == {C.HALTED}
    ends_at_open = Halt(utc(13, 25), OPEN, None, "SYNTHETIC ends at the open")
    assert evaluate(inputs(halts=(ends_at_open,))).candidate is not None
    after = Halt(DECIDE + timedelta(seconds=1), None, X, "SYNTHETIC not yet known")
    assert evaluate(inputs(halts=(after,))).candidate is not None


def test_watermark_before_the_breakout_minute_end_is_not_confirmed() -> None:
    # Range end 13:45 <= watermark 13:46:30 < breakout minute end 13:47.
    r = evaluate(inputs(continuity_through=utc(13, 46, 30)))
    assert reason_codes(r) == {C.SIGNAL_NOT_CONFIRMED}
    assert r.range is not None and r.signal is None and r.candidate is None


@pytest.mark.parametrize(
    ("day", "open_h", "exit_h"),
    [(date(2026, 3, 6), 14, 20), (date(2026, 3, 9), 13, 19),
     (date(2026, 11, 2), 14, 20)],
)  # fmt: skip
def test_dst_deadlines_follow_the_local_session(
    day: date, open_h: int, exit_h: int
) -> None:
    # EST: 09:30-16:00 = 14:30Z-21:00Z; EDT (from 2026-03-08 to 2026-11-01):
    # 13:30Z-20:00Z. Exit = close - 10 min, entry = exit - 30 min.
    o = utc(open_h, 30, day=day)
    bars = [*range_bars(o), bar(o + 15 * MIN, "101.60", "100.90", "101.50")]
    ins = inputs(bars, session_date=day, as_of=o + 16 * MIN + timedelta(seconds=30),
                 continuity_through=o + 120 * MIN)  # fmt: skip
    c = evaluate(ins).candidate
    assert c is not None and c.range.start == o
    assert c.exit_deadline == utc(exit_h, 50, day=day)
    assert c.entry_deadline == utc(exit_h, 20, day=day)


def test_missing_bar_before_the_breakout_blocks() -> None:
    bars = [b for b in BASE if b.start != utc(13, 45)]
    assert reason_codes(evaluate(inputs(bars))) == {C.SIGNAL_INTERVAL_MISSING}


# --- review deadline ------------------------------------------------------------------


def test_expired_review_blocks_and_is_never_extended() -> None:
    r = evaluate(inputs(as_of=utc(13, 48, 2)))
    assert reason_codes(r) == {C.REVIEW_EXPIRED} and r.candidate is None
    assert r.signal is not None  # the research observation stays visible
    # A later breakout bar does not open a second entry for the session.
    again = bar(utc(13, 47), "101.80", "101.40", "101.70")
    r2 = evaluate(inputs([*BASE, again], as_of=utc(13, 48, 30)))
    assert reason_codes(r2) == {C.REVIEW_EXPIRED}
    c = evaluate(inputs()).candidate
    assert c is not None
    assert check_review(c, utc(13, 48, 1)) == ()
    assert [x.code for x in check_review(c, utc(13, 48, 2))] == [C.REVIEW_EXPIRED]


def test_review_window_never_exceeds_sixty_seconds() -> None:
    cfg = {"review_window_seconds": 15}
    h = M.config_hash(cfg)
    r = evaluate(inputs(config=cfg, config_hash=h, as_of=utc(13, 47, 10)))
    assert r.candidate is not None
    assert r.candidate.review_deadline == utc(13, 47, 17)
    late = evaluate(inputs(config=cfg, config_hash=h, as_of=utc(13, 47, 17)))
    assert reason_codes(late) == {C.REVIEW_EXPIRED}
    bad = {"review_window_seconds": 61}
    assert C.CONFIG_INVALID in reason_codes(evaluate(inputs(config=bad)))


def test_config_must_match_the_adopted_hash() -> None:
    cfg = {"completion_buffer_seconds": 60}
    assert reason_codes(evaluate(inputs(config=cfg))) == {C.CONFIG_MISMATCH}


# --- feed qualification ---------------------------------------------------------------


def test_delayed_feed_is_research_only() -> None:
    bars = [replace(b, latency=FeedLatency.DELAYED) for b in BASE]
    r = evaluate(inputs(bars))
    assert reason_codes(r) == {C.FEED_NOT_REALTIME} and r.candidate is None
    assert r.range is not None and r.signal is not None


def test_feed_not_qualified_for_recommendations_blocks() -> None:
    reg = qualified_registry((SIP,), missing=Use.FINANCIAL_RECOMMENDATION)
    r = evaluate(inputs(rights=reg))
    assert reason_codes(r) == {C.FEED_UNQUALIFIED} and r.candidate is None


# --- manifest and the T027 gate -------------------------------------------------------


def test_manifest_binds_the_catalogue_and_the_gate_needs_regular_session() -> None:
    assert (M.strategy_id, M.version) == ("STR-ORB-001", "0.1.0-research")
    assert (M.family, M.horizon) == (Family.OPENING_RANGE_BREAKOUT, Horizon.INTRADAY)
    p = M.resolve({})
    assert (p["opening_range_minutes"], p["signal_bar_minutes"]) == (15, 1)
    assert M.regular_session_only is True
    intraday = investment_policy(horizons=("long_term", "intraday"))
    m = orb_manifest(GATE_FEED)
    assert decide(world(m=m, policy=intraday)).allowed
    ext = replace(m, regular_session_only=False)
    assert codes(decide(world(m=ext, policy=intraday))) == {G.SESSION_NOT_PERMITTED}


# --- property: no candidate unless every precondition holds ---------------------------


@settings(max_examples=150, deadline=None)
@given(
    missing=st.booleans(), retro=st.booleans(), gap=st.booleans(),
    other_feed=st.booleans(), delayed=st.booleans(), halt=st.booleans(),
    loss=st.booleans(), unqualified=st.booleans(), holiday=st.booleans(),
    offset=st.integers(min_value=-40, max_value=200),
)  # fmt: skip
def test_candidate_only_when_every_precondition_holds(
    missing: bool, retro: bool, gap: bool, other_feed: bool, delayed: bool,
    halt: bool, loss: bool, unqualified: bool, holiday: bool, offset: int,
) -> None:  # fmt: skip
    as_of = utc(13, 47, 2) + timedelta(seconds=offset)  # breakout known 13:47:02
    bars = list(BASE)
    if missing:
        bars = [b for b in bars if b.start != utc(13, 41)]
    if retro:
        bars = [replace(b, retrospective=True) if b.start == utc(13, 42) else b
                for b in bars]  # fmt: skip
    if delayed:
        bars = [replace(b, latency=FeedLatency.DELAYED) for b in bars]
    if other_feed:
        bars.append(bar(utc(13, 31), feed=IEX))
    gaps = (Gap(utc(13, 32, 5), utc(13, 32, 9), "sequence_gap"),) if gap else ()
    halts = (Halt(utc(13, 39), utc(13, 40), X, "SYNTHETIC"),) if halt else ()
    reg = qualified_registry((SIP,), Use.FINANCIAL_RECOMMENDATION if unqualified
                             else None)  # fmt: skip
    ins = inputs(bars, gaps=gaps, halts=halts, rights=reg, as_of=as_of,
                 continuity_through=utc(13, 44) if loss else utc(19, 0))  # fmt: skip
    if holiday:
        ins = replace(ins, session_date=HOLIDAY)
    r = evaluate(ins)
    # Oracle from the toggles: known breakout (offset >= 0), review open (< 60 s).
    ok = not (missing or retro or gap or other_feed or delayed or halt or loss
              or unqualified or holiday) and 0 <= offset < 60  # fmt: skip
    assert (r.candidate is not None) is ok
    assert (r.candidate is None) is (bool(r.reasons) or r.signal is None)
