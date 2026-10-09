"""Versioned exchange calendars (T011; R050, R080, R091). SYNTHETIC calendar data.

Expected UTC instants are hand-computed: New York is UTC-5 (EST) before 2026-03-08 and
from 2026-11-01, UTC-4 (EDT) in between.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import (
    CalendarCoverageError,
    CalendarError,
    CalendarId,
    Halt,
    MarketState,
    load_calendar,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError

PROFILE = settings(derandomize=True, database=None, max_examples=200)
FIXTURE = (Path(__file__).parent / "fixtures" / "xnys_synthetic.json").read_text()
CAL = load_calendar(FIXTURE)
D = date
A = InstrumentId(UUID("00000000-0000-4000-8000-00000000000a"))
B = InstrumentId(UUID("00000000-0000-4000-8000-00000000000b"))


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def test_fixture_is_labelled_synthetic() -> None:
    assert "SYNTHETIC" in FIXTURE and "NOT authoritative" in FIXTURE
    assert CAL.calendar_id == CalendarId("XNYS")


@pytest.mark.parametrize(
    ("day", "open_at", "close_at"),
    [
        (D(2026, 3, 6), utc(2026, 3, 6, 14, 30), utc(2026, 3, 6, 21)),  # EST
        (D(2026, 3, 9), utc(2026, 3, 9, 13, 30), utc(2026, 3, 9, 20)),  # EDT
        (D(2026, 10, 30), utc(2026, 10, 30, 13, 30), utc(2026, 10, 30, 20)),  # EDT
        (D(2026, 11, 2), utc(2026, 11, 2, 14, 30), utc(2026, 11, 2, 21)),  # EST
    ],
)
def test_dst_transitions_shift_utc_session(
    day: date, open_at: datetime, close_at: datetime
) -> None:
    session = CAL.session_for(day)
    assert session is not None
    assert (session.open_at, session.close_at) == (open_at, close_at)
    assert session.open_at.tzinfo is UTC and not session.early_close
    assert session.calendar_id.code + session.version == "XNYSsynthetic-2026.1"


def test_half_day_closes_at_13_local() -> None:
    session = CAL.session_for(D(2026, 11, 27))
    assert session is not None and session.early_close
    assert session.close_at == utc(2026, 11, 27, 18)  # 13:00 EST
    assert CAL.is_open(utc(2026, 11, 27, 17, 59, 59))
    assert not CAL.is_open(utc(2026, 11, 27, 18))
    assert CAL.next_close(utc(2026, 11, 27, 15)) == utc(2026, 11, 27, 18)


def test_holiday_and_weekend_have_no_session() -> None:
    assert CAL.session_for(D(2026, 11, 26)) is None  # SYNTHETIC holiday entry
    assert CAL.session_for(D(2026, 11, 28)) is None  # Saturday
    assert not CAL.is_open(utc(2026, 11, 26, 16))
    assert CAL.next_open(utc(2026, 11, 25, 22)) == utc(2026, 11, 27, 14, 30)


def test_boundaries_open_inclusive_close_exclusive() -> None:
    open_at, close_at = utc(2026, 10, 8, 13, 30), utc(2026, 10, 8, 20)
    assert not CAL.is_open(open_at - timedelta(microseconds=1))
    assert CAL.is_open(open_at)
    assert CAL.is_open(close_at - timedelta(microseconds=1))
    assert not CAL.is_open(close_at)
    assert CAL.next_open(open_at) == utc(2026, 10, 9, 13, 30)  # strictly after
    assert CAL.next_open(open_at - timedelta(microseconds=1)) == open_at
    assert CAL.next_close(open_at) == close_at
    assert CAL.next_close(close_at) == utc(2026, 10, 9, 20)
    local = datetime(2026, 10, 8, 9, 30, tzinfo=CAL.tz)  # aware non-UTC input
    assert CAL.is_open(local) and CAL.next_close(local).tzinfo is UTC
    with pytest.raises(InstantError):
        CAL.is_open(datetime(2026, 10, 8, 14))  # noqa: DTZ001


def test_halts_market_wide_and_per_instrument() -> None:
    market = Halt(utc(2026, 10, 8, 15), utc(2026, 10, 8, 15, 15), None, "MWCB L1")
    single = Halt(utc(2026, 10, 8, 16), None, A, "news pending")
    halts = (market, single)
    assert CAL.state_at(utc(2026, 10, 8, 14, 59), B, halts) is MarketState.OPEN
    assert CAL.state_at(utc(2026, 10, 8, 15), B, halts) is MarketState.HALTED
    assert CAL.state_at(utc(2026, 10, 8, 15, 15), B, halts) is MarketState.OPEN
    assert CAL.state_at(utc(2026, 10, 8, 16), A, halts) is MarketState.HALTED
    assert CAL.state_at(utc(2026, 10, 8, 16), B, halts) is MarketState.OPEN
    assert CAL.state_at(utc(2026, 10, 8, 16), None, halts) is MarketState.OPEN
    assert not CAL.is_open(utc(2026, 10, 8, 19), A, halts)  # open-ended halt
    assert CAL.state_at(utc(2026, 10, 8, 21), A, halts) is MarketState.CLOSED
    with pytest.raises(CalendarError):
        Halt(utc(2026, 10, 8, 15), utc(2026, 10, 8, 15), None, "empty")
    with pytest.raises(InstantError):
        Halt(datetime(2026, 10, 8, 15), None, None, "naive")  # noqa: DTZ001


def test_version_selected_by_session_date() -> None:
    nov, dec = CAL.session_for(D(2026, 11, 30)), CAL.session_for(D(2026, 12, 24))
    assert nov is not None and nov.version == "synthetic-2026.1"
    assert dec is not None and dec.version == "synthetic-2026.2" and dec.early_close
    assert CAL.session_for(D(2026, 12, 31)) is None  # ad-hoc closure in 2026.2
    for outside in (D(2025, 12, 31), D(2028, 1, 3)):
        with pytest.raises(CalendarCoverageError):
            CAL.session_for(outside)


def test_trading_day_arithmetic() -> None:
    assert CAL.add_trading_days(D(2026, 11, 25), 1) == D(2026, 11, 27)
    assert CAL.add_trading_days(D(2026, 12, 30), 1) == D(2027, 1, 4)
    assert CAL.add_trading_days(D(2027, 1, 4), -1) == D(2026, 12, 30)
    assert CAL.add_trading_days(D(2026, 11, 27), 0) == D(2026, 11, 27)
    with pytest.raises(CalendarError):
        CAL.add_trading_days(D(2026, 11, 26), 0)
    assert CAL.trading_days_between(D(2026, 11, 23), D(2026, 11, 30)) == 4
    with pytest.raises(CalendarCoverageError):
        CAL.add_trading_days(D(2027, 12, 30), 5)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"effective_from": "2026-12-01"', '"effective_from": "2026-11-30"'),
        ('"close": "16:00"', '"close": "09:00"'),
        ('"2026-11-27": "13:00"', '"2026-11-27": "17:00"'),
        ('"tz": "America/New_York"', '"tz": "Mars/Olympus"'),
        ('"weekend": ["sat", "sun"]', '"weekend": [6, 7]'),
        ('"weekend": ["sat", "sun"]', '"weekend": [5.0]'),
        ('"weekend": ["sat", "sun"]', '"weekend": ["sab"]'),
        ('"calendar_id": "XNYS"', '"calendar_id": "xnys"'),
        ('"holidays": ["2026-07-03",', '"holidays": ["2026-02-30",'),
        ('"adhoc_closures": []', '"adhoc_closures": ["2028-01-04"]'),
    ],
)
def test_loader_rejects_bad_data(old: str, new: str) -> None:
    assert old in FIXTURE
    with pytest.raises(CalendarError):
        load_calendar(FIXTURE.replace(old, new, 1))


def _days(start: date, end: date) -> int:
    """Independent oracle: dates in [start, end) that have a session."""
    days = (start + timedelta(i) for i in range((end - start).days))
    return sum(CAL.session_for(d) is not None for d in days)


@PROFILE
@given(st.integers(0, 600), st.integers(-25, 25))
def test_property_trading_day_arithmetic_matches_session_for(
    offset: int, n: int
) -> None:
    start, one = D(2026, 1, 1) + timedelta(offset), timedelta(1)
    assert CAL.is_trading_day(start) == (CAL.session_for(start) is not None)
    try:
        end = CAL.add_trading_days(start, n)
    except CalendarCoverageError:
        return
    except CalendarError:
        assert n == 0 and CAL.session_for(start) is None
        return
    lo, hi = sorted((start, end))
    assert CAL.session_for(end) is not None
    assert CAL.trading_days_between(lo, hi) == _days(lo, hi)
    if n >= 0:
        assert _days(lo + one, hi + one) == n  # (start, end]
    else:
        assert _days(lo, hi) == -n  # [end, start)
