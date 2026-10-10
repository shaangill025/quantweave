"""Normalized bars, session boundaries, sequence gaps and corrections (T022 inc. 1;
R050, R066, R091). SYNTHETIC trades, feeds and calendar (fixtures/xnys_synthetic.json).

OHLCV values are hand-computed. Session times: 2026-10-08 is EDT (UTC-4), regular
13:30Z-20:00Z; 2026-11-27 is an EST early close at 13:00 local = 18:00Z; 2026-07-03 is
a holiday in the fixture.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from ingest_rights_helper import qualified_rights
from qw_domain.bars import (
    Bar,
    BarBook,
    BarBuild,
    BarError,
    ExtendedHours,
    Gap,
    Marker,
    SessionLabel,
    Trade,
    build_bars,
    session_grid,
)
from qw_domain.calendars import load_calendar
from qw_domain.decimals import PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestDenied
from qw_domain.rights import Registry
from qw_domain.streams import FeedLatency

FIXTURE = Path(__file__).parent / "fixtures" / "xnys_synthetic.json"
CAL = load_calendar(FIXTURE.read_text())
DAY, EARLY, HOLIDAY = date(2026, 10, 8), date(2026, 11, 27), date(2026, 7, 3)
X = InstrumentId(UUID(int=1))
IEX, HIST = "feed-synth-iex", "feed-synth-delayed"
RT, DELAYED = FeedLatency.REALTIME_EXCHANGE_LIMITED, FeedLatency.DELAYED
MIN = timedelta(minutes=1)
EXT = ExtendedHours(pre=timedelta(hours=1), post=timedelta(hours=1))


def utc(h: int, m: int, s: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=UTC)


def trade(
    seq: int,
    at: datetime,
    px: str = "1",
    size: str = "1",
    lag: int = 1,
    ok: bool = True,
) -> Trade:
    late = at + timedelta(seconds=lag)
    return Trade(X, IEX, seq, Price(px), PositiveQuantity(size), at, late, ok)


TRADES = [
    trade(1, utc(13, 30, 10), "100.50", "10"),
    trade(2, utc(13, 30, 40), "101.25", "5"),
    trade(3, utc(13, 30, 45), "250", "1", ok=False),  # ineligible condition
    trade(4, utc(13, 30, 50), "99.75", "20"),
    trade(5, utc(13, 31, 5), "100.00", "1"),
]


def build(
    trades: list[Trade],
    as_of: datetime,
    day: date = DAY,
    extended: ExtendedHours | None = None,
    markers: list[Marker] | None = None,
) -> BarBuild:
    rights = qualified_rights(IEX, as_of)
    return build_bars(trades, CAL, day, X, MIN, RT, rights, extended, markers)


def reasons(result: BarBuild) -> list[str]:
    return sorted(r for _, r in result.rejected)


def ohlcv(b: Bar) -> tuple[str, ...]:
    px = (b.ohlc.open, b.ohlc.high, b.ohlc.low, b.ohlc.close)
    return (*(p.to_wire() for p in px), b.volume.to_wire(), str(b.trade_count))


def test_trades_aggregate_to_hand_computed_minute_bars() -> None:
    result = build(TRADES, utc(13, 35))
    first, second = result.bars
    assert (first.start, first.end) == (utc(13, 30), utc(13, 31))
    assert ohlcv(first) == ("100.5", "101.25", "99.75", "99.75", "35", "3")
    assert ohlcv(second) == ("100", "100", "100", "100", "1", "1")
    assert (first.slot.label, first.slot.session_date) == (SessionLabel.REGULAR, DAY)
    assert (first.feed_id, first.latency, first.retrospective) == (IEX, RT, False)
    assert first.knowledge_at == utc(13, 31)  # final only once the interval closed
    assert reasons(result) == ["ineligible_condition"] and result.gaps == ()


def test_late_or_open_interval_data_is_not_known_yet() -> None:
    late = trade(6, utc(13, 31, 30), "102", "2", lag=600)
    result = build([*TRADES, late], utc(13, 31, 30))
    assert len(result.bars) == 1  # the 13:31 interval is still open
    assert reasons(result) == ["ineligible_condition", "interval_open", "not_yet_known"]


def test_session_boundaries_without_and_with_extended_hours() -> None:
    edge = [trade(1, utc(13, 29, 59), "99", "3"), trade(2, utc(20, 0), "98", "4")]
    result = build(edge, utc(21, 0))
    assert result.bars == () and reasons(result) == ["outside_session"] * 2
    labels = [
        (b.slot.label, b.start, b.end) for b in build(edge, utc(21, 0), DAY, EXT).bars
    ]
    assert labels == [
        (SessionLabel.PRE, utc(13, 29), utc(13, 30)),
        (SessionLabel.POST, utc(20, 0), utc(20, 1)),
    ]


def test_early_close_and_holiday_come_from_the_calendar() -> None:
    after_close = [trade(1, utc(18, 0, day=EARLY))]
    assert reasons(build(after_close, utc(23, 0, day=EARLY), EARLY)) == [
        "outside_session"
    ]
    grid = session_grid(CAL, EARLY, MIN)
    assert (grid[0].start, grid[-1].end) == (
        utc(14, 30, 0, EARLY),
        utc(18, 0, 0, EARLY),
    )
    assert session_grid(CAL, HOLIDAY, MIN) == ()
    on_holiday = [trade(1, utc(15, 0, day=HOLIDAY))]
    assert reasons(build(on_holiday, utc(23, 0, day=HOLIDAY), HOLIDAY)) == [
        "outside_session"
    ]


def test_bins_never_cross_the_close() -> None:
    grid = session_grid(CAL, DAY, timedelta(minutes=7))
    assert len(grid) == 56  # 390 minutes: 55 full bins plus a 5-minute remainder
    assert (grid[-1].start, grid[-1].end) == (utc(19, 55), utc(20, 0))
    assert grid[0].start == utc(13, 30)
    daily = session_grid(CAL, DAY, None)
    assert [(s.start, s.end) for s in daily] == [(utc(13, 30), utc(20, 0))]
    with pytest.raises(BarError):
        session_grid(CAL, DAY, timedelta(seconds=30))  # no sub-minute bars


def test_sequence_gap_is_reported_and_its_bins_are_not_built() -> None:
    trades = [
        trade(1, utc(13, 30, 5)),
        trade(2, utc(13, 31, 5)),
        trade(5, utc(13, 32, 30)),  # 3 and 4 never arrived
        trade(6, utc(13, 33, 30)),
    ]
    result = build(trades, utc(13, 40))
    assert result.gaps == (Gap(utc(13, 31, 5), utc(13, 32, 30), "sequence_gap"),)
    assert [b.start for b in result.bars] == [utc(13, 30), utc(13, 33)]
    assert reasons(result) == ["in_gap", "in_gap"]


def test_duplicate_and_conflicting_sequences() -> None:
    a = trade(1, utc(13, 30, 5), "10")
    clash = trade(1, utc(13, 30, 6), "10.5")
    result = build([a, a, clash], utc(13, 35))
    assert ohlcv(result.bars[0]) == ("10", "10", "10", "10", "1", "1")
    assert reasons(result) == ["duplicate", "sequence_conflict"]
    redelivered = replace(a, received_at=a.received_at + timedelta(seconds=5))
    again = build([redelivered, a], utc(13, 35))
    assert [(t, r) for t, r in again.rejected] == [(redelivered, "duplicate")]
    assert again.bars[0].knowledge_at == utc(13, 31)  # first receipt kept


def test_corrections_are_versions_visible_by_knowledge_time() -> None:
    first = build(TRADES, utc(13, 32)).bars[0]
    late = trade(6, utc(13, 30, 5), "98", "5", lag=350)  # first trade, seen 13:35:55
    corrected = build([*TRADES, late], utc(13, 36)).bars[0]
    book = BarBook().record(first).record(corrected)
    assert book.record(corrected) == book  # identical content: no new version
    (seen,) = book.visible(X, utc(13, 30), utc(13, 31), utc(13, 34))
    assert ohlcv(seen) == ("100.5", "101.25", "99.75", "99.75", "35", "3")
    (latest,) = book.visible(X, utc(13, 30), utc(13, 31), utc(13, 36))
    assert latest.version == 2 and len(book.bars) == 2  # the first version is kept
    assert ohlcv(latest) == ("98", "101.25", "98", "99.75", "40", "4")  # event order
    with pytest.raises(BarError):
        BarBook().record(corrected).record(first)  # older knowledge cannot correct


def test_live_trades_need_ingestion_rights_and_matching_feed() -> None:
    no_rights = qualified_rights(IEX, utc(14, 0), registry=Registry())
    with pytest.raises(IngestDenied):
        build_bars(TRADES, CAL, DAY, X, MIN, RT, no_rights)
    with pytest.raises(BarError):  # trades of another feed
        build_bars(TRADES, CAL, DAY, X, MIN, RT, qualified_rights(HIST, utc(14, 0)))
