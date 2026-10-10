"""Coverage accounting, retrospective recovery, history ingestion, trailing-loss
markers and scheduled discovery jobs (T022 increment 2; R014, R066, R083, R091).

SYNTHETIC trades, feeds, calendar and instruments; shared helpers come from
test_bars (2026-10-08 is EDT: regular session 13:30Z-20:00Z).
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ingest_rights_helper import (
    KEEP,
    REGION,
    TENANT,
    qualified_registry,
    qualified_rights,
)
from qw_domain.bars import Bar, BarBook, BarError, Gap, Marker, Ohlc, session_grid
from qw_domain.coverage import (
    CoverageState,
    ProviderBar,
    coverage,
    ingest_history,
    recover,
)
from qw_domain.decimals import Price, Quantity
from qw_domain.ingest import IngestDenied
from qw_domain.jobs import Priority
from qw_domain.rights import Registry, Use, UseScope
from qw_domain.streams import (
    FeedLatency,
    StreamError,
    StreamFeed,
    StreamRights,
    discovery_jobs,
    plan_streams,
)
from test_bars import CAL, DAY, DELAYED, HIST, IEX, MIN, RT, X, build, trade, utc
from test_streams import demand, inst

S = CoverageState
EOD = FeedLatency.END_OF_DAY
OHLC = Ohlc(Price("11"), Price("12.5"), Price("10.5"), Price("12"))
GRID = session_grid(CAL, DAY, MIN)


def history(start: datetime, received: datetime) -> tuple[Bar, ...]:
    row = ProviderBar(X, start, start + MIN, OHLC, Quantity("400"))
    return ingest_history(
        [row], CAL, DAY, MIN, DELAYED, qualified_rights(HIST, received)
    )


def book_of(*bars: Bar) -> BarBook:
    book = BarBook()
    for b in bars:
        book = book.record(b)
    return book


def test_coverage_states_and_recovery_knowledge_time() -> None:
    trades = [
        trade(1, utc(13, 30, 5)),
        trade(2, utc(13, 31, 55)),
        trade(5, utc(13, 33, 5)),
    ]
    built = build(trades, utc(13, 40))  # sequence gap 13:31:55-13:33:05
    delayed = history(utc(13, 36), utc(13, 55))
    book = book_of(*built.bars, *delayed)
    streamed = ((utc(13, 30), utc(13, 36)),)
    gaps = (*built.gaps, Gap(utc(13, 34), utc(13, 34, 30), "connection_lost"))
    cov = coverage(GRID, book, X, utc(14, 0), gaps, streamed)
    assert [(c.start.minute, c.state, c.reason) for c in cov[:8]] == [
        (30, S.COVERED, None),
        (31, S.MISSING, "sequence_gap"),
        (32, S.MISSING, "sequence_gap"),
        (33, S.MISSING, "sequence_gap"),
        (34, S.MISSING, "connection_lost"),
        (35, S.COVERED, None),  # streamed, no eligible trades: no bar
        (36, S.DELAYED, "not_streamed"),
        (37, S.MISSING, "not_streamed"),
    ]
    assert cov[5].bar is None and cov[6].bar == delayed[0]
    assert len(cov) == 30  # only intervals closed by 14:00

    source = history(utc(13, 32), utc(14, 5))
    missing = [c for c in cov if c.state is S.MISSING]
    (fixed,) = recover(missing, source, utc(14, 10))
    assert fixed.retrospective and fixed.knowledge_at == utc(14, 10)
    assert (fixed.start, fixed.latency, fixed.ohlc) == (utc(13, 32), DELAYED, OHLC)
    book = book.record(fixed)
    before = coverage(GRID, book, X, utc(14, 9), gaps, streamed)
    after = coverage(GRID, book, X, utc(14, 10), gaps, streamed)
    assert before[2].state is S.MISSING  # not known before the recovery time
    assert (after[2].state, after[2].reason, after[2].bar) == (
        S.RETROSPECTIVE,
        "sequence_gap",
        fixed,
    )
    assert after[1].state is S.MISSING  # no history for 13:31: stays missing
    with pytest.raises(BarError):
        recover(missing, source, utc(14, 4))  # before the history was received
    with pytest.raises(BarError):
        recover(cov[:1], source, utc(14, 10))  # a covered interval is not recovered
    with pytest.raises(BarError):
        recover(missing, (fixed,), utc(14, 20))  # never recover from recovered bars


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(st.integers(0, 120), st.integers(0, 120), st.integers(0, 240))
def test_property_recovered_bar_never_visible_before_recovery(
    received: int, delay: int, query: int
) -> None:
    lost = Gap(utc(13, 32), utc(13, 32, 30), "connection_lost")
    cov = coverage(
        GRID, BarBook(), X, utc(13, 33), (lost,), ((utc(13, 30), utc(16, 0)),)
    )
    source = history(utc(13, 32), utc(13, 33) + timedelta(minutes=received))
    at = source[0].knowledge_at + timedelta(minutes=delay)
    (fixed,) = recover([c for c in cov if c.state is S.MISSING], source, at)
    book = book_of(fixed)
    asked = utc(13, 33) + timedelta(minutes=query)
    seen = book.visible(X, utc(13, 32), utc(13, 33), asked)
    state = coverage(GRID, book, X, asked, (lost,), ((utc(13, 30), utc(16, 0)),))[2]
    assert (seen == (fixed,)) is (asked >= at)
    assert (state.state is S.RETROSPECTIVE) is (asked >= at)
    assert fixed.knowledge_at == at >= fixed.end


def test_history_ingestion_is_rights_gated_and_validated() -> None:
    row = ProviderBar(X, utc(13, 40), utc(13, 41), OHLC, Quantity("5"))
    for denied in (
        qualified_rights(HIST, utc(15, 0), registry=Registry()),
        qualified_rights(HIST, utc(15, 0), missing=Use.RETENTION),
    ):
        with pytest.raises(IngestDenied):
            ingest_history([row], CAL, DAY, MIN, DELAYED, denied)
    ok = qualified_rights(HIST, utc(15, 0))
    (bar,) = ingest_history([row], CAL, DAY, MIN, DELAYED, ok)
    assert bar.knowledge_at == utc(15, 0) and bar.trade_count is None
    for bad in (
        replace(row, start=utc(13, 40, 30), end=utc(13, 41, 30)),  # misaligned
        replace(row, start=utc(20, 0), end=utc(20, 1)),  # outside the session
    ):
        with pytest.raises(BarError):
            ingest_history([bad], CAL, DAY, MIN, DELAYED, ok)
    with pytest.raises(BarError):  # history never claims real-time latency
        ingest_history([row], CAL, DAY, MIN, RT, ok)
    with pytest.raises(BarError):  # the same interval twice from one feed
        ingest_history(
            [row, replace(row, volume=Quantity("6"))], CAL, DAY, MIN, DELAYED, ok
        )
    early = qualified_rights(HIST, utc(13, 40, 30))
    with pytest.raises(BarError):  # a bar cannot be received before it ends
        ingest_history([row], CAL, DAY, MIN, DELAYED, early)
    daily = ProviderBar(X, utc(13, 30), utc(20, 0), OHLC, Quantity("9"))
    eod = qualified_rights(HIST, utc(23, 0))
    (day_bar,) = ingest_history([daily], CAL, DAY, None, EOD, eod)
    assert (day_bar.start, day_bar.end, day_bar.latency) == (
        utc(13, 30),
        utc(20, 0),
        EOD,
    )


def marker(at: datetime, last: int, lag: int = 1) -> Marker:
    return Marker(IEX, X, at, last, at + timedelta(seconds=lag))


def test_trailing_loss_needs_an_end_of_interval_marker() -> None:
    trades = [
        trade(1, utc(13, 30, 5)),
        trade(2, utc(13, 30, 20)),
        trade(3, utc(13, 31, 10)),
    ]
    assert len(build(trades, utc(13, 33, 30)).bars) == 2  # no markers: undetectable
    marks = [marker(utc(13, 31), 2), marker(utc(13, 32), 4)]  # seq 4 never arrived
    built = build(trades, utc(13, 33, 30), markers=marks)
    assert [b.start for b in built.bars] == [utc(13, 30)]
    assert built.gaps == (
        Gap(utc(13, 31), utc(13, 32) - timedelta(microseconds=1), "trailing_loss"),
        Gap(utc(13, 32), utc(13, 33, 30), "unconfirmed"),
    )
    streamed = ((utc(13, 30), utc(13, 40)),)
    cov = coverage(GRID, book_of(*built.bars), X, utc(13, 33, 30), built.gaps, streamed)
    assert [(c.state, c.reason) for c in cov] == [
        (S.COVERED, None),
        (S.MISSING, "trailing_loss"),
        (S.MISSING, "unconfirmed"),  # after the last marker: not yet confirmed
    ]


def test_trailing_loss_does_not_assume_sequence_follows_event_time() -> None:
    # Lost seq 3 happened at 13:30:40, before seq 2 (13:31:20): the gap must start
    # at the last confirmed boundary (here the session open), not at seq 2.
    trades = [trade(1, utc(13, 30, 5)), trade(2, utc(13, 31, 20))]
    built = build(trades, utc(13, 32, 30), markers=[marker(utc(13, 32), 3)])
    assert built.bars == ()
    assert built.gaps[0] == Gap(
        utc(13, 30), utc(13, 32) - timedelta(microseconds=1), "trailing_loss"
    )
    inner = build(
        [trades[0], trade(3, utc(13, 31, 20))],
        utc(13, 32, 30),
        markers=[marker(utc(13, 32), 3)],
    )
    assert (
        Gap(utc(13, 30), utc(13, 32) - timedelta(microseconds=1), "sequence_gap")
        in inner.gaps
    )


def test_markers_sharing_a_time_do_not_depend_on_input_order() -> None:
    trades = [trade(1, utc(13, 30, 5)), trade(2, utc(13, 30, 20))]  # 3 and 4 lost
    marks = [marker(utc(13, 32), 2), marker(utc(13, 32), 4)]
    one = build(trades, utc(13, 32, 30), markers=marks)
    two = build(trades, utc(13, 32, 30), markers=marks[::-1])
    assert one.gaps == two.gaps and one.bars == two.bars == ()
    end = utc(13, 32) - timedelta(microseconds=1)
    assert one.gaps[0] == Gap(utc(13, 30), end, "trailing_loss")


def test_markers_unknown_at_build_time_confirm_nothing() -> None:
    trades = [trade(1, utc(13, 30, 5))]
    late = [marker(utc(13, 31), 1, lag=600)]
    for marks in ([], late):
        built = build(trades, utc(13, 33), markers=marks)
        assert built.bars == ()
        assert built.gaps == (Gap(utc(13, 30), utc(13, 33), "unconfirmed"),)
    ok = build(trades, utc(13, 33), markers=[marker(utc(13, 31), 1)])
    assert len(ok.bars) == 1  # confirmed complete by the marker
    with pytest.raises(BarError):  # a marker of another feed
        build(trades, utc(13, 33), markers=[replace(late[0], feed_id=HIST)])


def test_scheduled_discovery_jobs_batch_the_uncovered_set() -> None:
    feeds = [StreamFeed(IEX, RT, 2, frozenset(inst(i) for i in range(1, 10)))]
    reg = qualified_registry((IEX,))
    rights = StreamRights(reg, TENANT, UseScope.PERSONAL, REGION, KEEP)
    plan = plan_streams([demand(i) for i in range(1, 8)], feeds, rights, utc(13, 0))
    session = CAL.session_for(DAY)
    assert session is not None
    jobs = discovery_jobs(plan, session, batch_size=2)
    batches = [j.payload["instruments"] for j in jobs]
    wire = [inst(i).to_wire() for i in range(3, 8)]  # 1 and 2 are streamed
    assert batches == [wire[0:2], wire[2:4], wire[4:5]]
    assert {(j.kind, j.priority, j.deadline_at) for j in jobs} == {
        ("scheduled_discovery", Priority.SCHEDULED_RESEARCH, utc(20, 0))
    }
    assert len({j.input_revision for j in jobs}) == 3
    again = plan_streams(
        [demand(i) for i in range(7, 0, -1)], feeds, rights, utc(14, 0)
    )
    assert discovery_jobs(again, session, 2) == jobs  # same key: idempotent enqueue
    with pytest.raises(StreamError):
        discovery_jobs(plan, session, 0)
