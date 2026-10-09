"""Versioned exchange calendars, sessions and halts (T011; R050, R080, R091, C-08).

Inputs and outputs are aware datetimes normalized to UTC. Local session meaning is kept
through `(calendar_id, version, session_date)` on every `Session`, never inferred from
a UTC instant alone. A session is the half-open interval `[open_at, close_at)`: the
market is open at the opening instant and closed at the closing instant. Halts are
half-open intraday intervals, market-wide (`instrument_id is None`) or per instrument;
an open-ended halt has `end is None`. A session time that falls in a DST gap (does
not exist) or fold (occurs twice) on some date raises `CalendarError` from
`session_for` for that date: the zone is a calendar property, so the check cannot run
inside `CalendarVersion`. Dates outside every version raise
`CalendarCoverageError`; a calendar never guesses beyond its data. Auctions and
pre/post-market sessions are not modelled yet.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo

from qw_domain.decimals import safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, require_date


class CalendarError(ValueError):
    """Malformed calendar data or an invalid calendar query."""


class CalendarCoverageError(CalendarError):
    """The date is outside every version of the calendar."""


_CALENDAR_ID = re.compile(r"[A-Z0-9]{4}(-[A-Z0-9]{1,16})?", re.ASCII)
_DAY = timedelta(days=1)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True, order=True)
class CalendarId:
    """Calendar identity: the exchange MIC, optionally with a session-kind suffix."""

    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _CALENDAR_ID.fullmatch(self.code) is None:
            raise CalendarError(f"calendar id {safe_repr(self.code)} is malformed")


@dataclass(frozen=True, slots=True)
class SessionRef:
    """A local session date with the calendar that gives it meaning."""

    calendar_id: CalendarId
    session_date: date


@dataclass(frozen=True, slots=True)
class Session:
    calendar_id: CalendarId
    version: str
    session_date: date
    open_at: datetime
    close_at: datetime
    early_close: bool


@dataclass(frozen=True)
class CalendarVersion:
    """Session rules for local dates in [effective_from, effective_to)."""

    version: str
    effective_from: date
    effective_to: date
    open: time
    close: time
    weekend: frozenset[int]
    holidays: frozenset[date] = frozenset()
    early_closes: Mapping[date, time] = field(default_factory=dict)
    adhoc_closures: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        require_date(self.effective_from, "effective_from")
        require_date(self.effective_to, "effective_to")
        if not self.version or self.effective_to <= self.effective_from:
            raise CalendarError(f"version {safe_repr(self.version)} has empty range")
        if not self.open < self.close:
            raise CalendarError(f"{self.version}: open must precede close")
        dates = {*self.holidays, *self.early_closes, *self.adhoc_closures}
        if any(not self.covers(d) for d in dates):
            raise CalendarError(f"{self.version}: special date outside its range")
        if any(not self.open < t < self.close for t in self.early_closes.values()):
            raise CalendarError(f"{self.version}: early close outside regular hours")
        if any(type(d) is not int or not 0 <= d <= 6 for d in self.weekend):
            raise CalendarError(f"{self.version}: weekend days must be ints 0..6")
        if self.holidays & self.early_closes.keys():
            raise CalendarError(f"{self.version}: a holiday cannot have an early close")
        if any(
            d.weekday() in self.weekend for d in (*self.holidays, *self.early_closes)
        ):
            raise CalendarError(f"{self.version}: special date on a weekend day")

    def covers(self, day: date) -> bool:
        return self.effective_from <= day < self.effective_to


class MarketState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class Halt:
    start: datetime
    end: datetime | None
    instrument_id: InstrumentId | None  # None: market-wide
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_aware_utc(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", ensure_aware_utc(self.end))
            if self.end <= self.start:
                raise CalendarError("halt end must be after its start")

    def applies(self, at: datetime, instrument_id: InstrumentId | None) -> bool:
        scoped = self.instrument_id is None or self.instrument_id == instrument_id
        return scoped and self.start <= at and (self.end is None or at < self.end)


type Halts = Iterable[Halt]


class ExchangeCalendar:
    """All versions of one calendar; versions may not overlap and share one zone."""

    def __init__(
        self, calendar_id: CalendarId, tz: ZoneInfo, versions: Iterable[CalendarVersion]
    ) -> None:
        self.calendar_id, self.tz = calendar_id, tz
        self.versions = tuple(sorted(versions, key=lambda v: v.effective_from))
        pairs = zip(self.versions, self.versions[1:], strict=False)
        if not self.versions or any(
            b.effective_from < a.effective_to for a, b in pairs
        ):
            raise CalendarError("a calendar needs non-overlapping versions")
        if len({v.version for v in self.versions}) != len(self.versions):
            raise CalendarError("version names must be unique within a calendar")

    def version_for(self, day: date) -> CalendarVersion:
        for version in self.versions:
            if version.covers(day):
                return version
        raise CalendarCoverageError(f"{self.calendar_id.code} has no data for {day}")

    def session_for(self, day: date) -> Session | None:
        v = self.version_for(day)
        if day.weekday() in v.weekend or day in v.holidays or day in v.adhoc_closures:
            return None
        close = v.early_closes.get(day)

        def at(t: time) -> datetime:
            local = datetime.combine(day, t, tzinfo=self.tz)
            # PEP 495: in a gap or fold the two folds give different offsets.
            if local.utcoffset() != local.replace(fold=1).utcoffset():
                raise CalendarError(f"{day} {t}: session time in a DST gap or fold")
            return local.astimezone(UTC)

        return Session(
            self.calendar_id,
            v.version,
            day,
            at(v.open),
            at(v.close if close is None else close),
            close is not None,
        )

    def is_trading_day(self, day: date) -> bool:
        return self.session_for(day) is not None

    def state_at(
        self, at: datetime, instrument: InstrumentId | None = None, halts: Halts = ()
    ) -> MarketState:
        """OPEN, CLOSED, or HALTED by a market-wide or `instrument` halt."""
        at = ensure_aware_utc(at)
        session = self.session_for(at.astimezone(self.tz).date())
        if session is None or not session.open_at <= at < session.close_at:
            return MarketState.CLOSED
        if any(h.applies(at, instrument) for h in halts):
            return MarketState.HALTED
        return MarketState.OPEN

    def is_open(
        self, at: datetime, instrument: InstrumentId | None = None, halts: Halts = ()
    ) -> bool:
        return self.state_at(at, instrument, halts) is MarketState.OPEN

    def _sessions_from(self, at: datetime) -> Iterable[Session]:
        day = ensure_aware_utc(at).astimezone(self.tz).date()
        while True:  # ends with CalendarCoverageError past the last version
            session = self.session_for(day)
            if session is not None:
                yield session
            day += _DAY

    def next_open(self, at: datetime) -> datetime:
        """The first session open strictly after `at` (halts are not considered)."""
        at = ensure_aware_utc(at)
        return next(s.open_at for s in self._sessions_from(at) if s.open_at > at)

    def next_close(self, at: datetime) -> datetime:
        """The first session close strictly after `at`."""
        at = ensure_aware_utc(at)
        return next(s.close_at for s in self._sessions_from(at) if s.close_at > at)

    def add_trading_days(self, day: date, n: int) -> date:
        """The n-th trading day after (n > 0) or before (n < 0) `day`; for n == 0,
        `day` itself, which must be a trading day."""
        if type(n) is not int:
            raise TypeError(f"n must be an int, not {type(n).__name__}")
        if n == 0:
            if not self.is_trading_day(day):
                raise CalendarError(f"{day} is not a trading day")
            return day
        step, remaining = (_DAY, n) if n > 0 else (-_DAY, -n)
        while remaining:
            day += step
            remaining -= self.is_trading_day(day)
        return day

    def trading_days_between(self, start: date, end: date) -> int:
        """Number of trading days in [start, end); end before start is an error."""
        if end < start:
            raise CalendarError(f"end {end} is before start {start}")
        days = (start + timedelta(i) for i in range((end - start).days))
        return sum(self.is_trading_day(d) for d in days)


def _reject_number(text: str) -> object:
    raise CalendarError(f"calendar data must not contain JSON numbers: {text}")


def _weekend(names: list[str]) -> frozenset[int]:
    if len(set(names)) != len(names):
        raise CalendarError(f"duplicate weekend entries: {safe_repr(names)}")
    return frozenset(_WEEKDAYS.index(d) for d in names)


def load_calendar(text: str) -> ExchangeCalendar:
    """Build a calendar from JSON (dates/times as strings, weekend as day names)."""
    try:
        raw = json.loads(text, parse_float=_reject_number, parse_int=_reject_number)
        versions = [
            CalendarVersion(
                version=v["version"],
                effective_from=date.fromisoformat(v["effective_from"]),
                effective_to=date.fromisoformat(v["effective_to"]),
                open=time.fromisoformat(v["open"]),
                close=time.fromisoformat(v["close"]),
                weekend=_weekend(v["weekend"]),
                holidays=frozenset(map(date.fromisoformat, v["holidays"])),
                early_closes={
                    date.fromisoformat(d): time.fromisoformat(t)
                    for d, t in v["early_closes"].items()
                },
                adhoc_closures=frozenset(map(date.fromisoformat, v["adhoc_closures"])),
            )
            for v in raw["versions"]
        ]
        return ExchangeCalendar(
            CalendarId(raw["calendar_id"]), ZoneInfo(raw["tz"]), versions
        )
    except CalendarError:
        raise
    except (KeyError, TypeError, ValueError) as exc:  # includes ZoneInfoNotFoundError
        raise CalendarError(f"calendar data rejected: {safe_repr(exc)}") from None
