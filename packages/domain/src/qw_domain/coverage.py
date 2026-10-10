"""Coverage accounting, retrospective recovery and rights-gated EOD/delayed history
(T022 increment 2; R014, R066, R083, R091; spec §6 "Normalization and continuity").

- Coverage per (instrument, interval) closed by `as_of`: `covered` (streamed in real
  time; no bar means no eligible trades), `delayed` (only delayed/EOD data for a gap
  or unstreamed interval), `retrospective` (recovered after the fact) or `missing`,
  each with the reason real-time coverage is absent (a gap reason, or
  `not_streamed`). A missing interval is never filled silently.
- Recovery fills missing intervals from ingested history that covers them exactly,
  as separate retrospective bars whose `knowledge_at` is the recovery time, so they
  are invisible as of any earlier time and never known in real time at the bar time.
- History is ingested only through the T021 gate (`ingest.require_ingest`), on the
  session grid, and is known from its receipt time.
Stdlib only.
"""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from qw_domain.bars import (
    Bar,
    BarBook,
    BarError,
    ExtendedHours,
    Gap,
    Ohlc,
    SessionLabel,
    Slot,
    session_grid,
)
from qw_domain.calendars import ExchangeCalendar
from qw_domain.decimals import Quantity
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights, require_ingest
from qw_domain.instants import ensure_aware_utc
from qw_domain.streams import FeedLatency


class CoverageState(StrEnum):
    COVERED = "covered"  # streamed in real time (no bar: no eligible trades)
    DELAYED = "delayed"  # only delayed/EOD data for an unstreamed or gap interval
    RETROSPECTIVE = "retrospective"  # recovered after the fact
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Coverage:
    instrument_id: InstrumentId
    session: SessionLabel
    start: datetime
    end: datetime
    state: CoverageState
    reason: str | None  # why real-time coverage is absent
    bar: Bar | None


def coverage(
    grid: Iterable[Slot],
    book: BarBook,
    instrument_id: InstrumentId,
    as_of: datetime,
    gaps: Iterable[Gap],
    streamed: Iterable[tuple[datetime, datetime]],
) -> tuple[Coverage, ...]:
    """Coverage of every interval closed by `as_of`. `streamed` are the windows in
    which the instrument was subscribed; `gaps` are sequence gaps and outages."""
    as_of = ensure_aware_utc(as_of)
    gap_list, windows = tuple(gaps), tuple(streamed)
    out: list[Coverage] = []
    for s in grid:
        if s.end > as_of:
            continue
        bars = book.visible(instrument_id, s.start, s.end, as_of)
        live = [b for b in bars if b.latency.real_time and not b.retrospective]
        retro = [b for b in bars if b.retrospective]
        late = [b for b in bars if not b.latency.real_time and not b.retrospective]
        why = next((g.reason for g in gap_list if g.touches(s.start, s.end)), None)
        if why is None and not any(a <= s.start and s.end <= b for a, b in windows):
            why = "not_streamed"
        if why is None:
            state, bar = CoverageState.COVERED, (live[0] if live else None)
        elif retro:
            state, bar = CoverageState.RETROSPECTIVE, retro[0]
        elif late:
            state, bar = CoverageState.DELAYED, late[0]
        else:
            state, bar = CoverageState.MISSING, None
        out.append(Coverage(instrument_id, s.label, s.start, s.end, state, why, bar))
    return tuple(out)


def recover(
    missing: Iterable[Coverage], history: Iterable[Bar], recovered_at: datetime
) -> tuple[Bar, ...]:
    """Retrospective bars for missing intervals that `history` covers exactly."""
    recovered_at = ensure_aware_utc(recovered_at)
    index: dict[tuple[InstrumentId, datetime, datetime], Bar] = {}
    for h in sorted(history, key=lambda b: b.feed_id, reverse=True):
        if h.retrospective:
            raise BarError("recover from ingested history, not recovered bars")
        index[(h.instrument_id, h.start, h.end)] = h
    out: list[Bar] = []
    for c in missing:
        if c.state is not CoverageState.MISSING:
            raise BarError("only missing intervals are recovered")
        src = index.get((c.instrument_id, c.start, c.end))
        if src is None:
            continue
        if c.end > recovered_at or src.knowledge_at > recovered_at:
            raise BarError("recovery cannot precede the interval or its history")
        out.append(
            replace(src, retrospective=True, knowledge_at=recovered_at, version=1)
        )
    return tuple(out)


@dataclass(frozen=True, slots=True)
class ProviderBar:
    instrument_id: InstrumentId
    start: datetime
    end: datetime
    ohlc: Ohlc
    volume: Quantity


def ingest_history(
    rows: Iterable[ProviderBar],
    calendar: ExchangeCalendar,
    day: date,
    interval: timedelta | None,
    latency: FeedLatency,
    rights: IngestRights,
    extended: ExtendedHours | None = None,
) -> tuple[Bar, ...]:
    """Delayed/EOD provider bars for `day`, known from `rights.received_at`."""
    if type(rights) is not IngestRights:
        raise TypeError("history ingestion needs IngestRights")
    if not isinstance(latency, FeedLatency) or latency.real_time:
        raise BarError("history is delayed or end-of-day, never real time")
    require_ingest(rights)
    at = rights.received_at
    grid = {
        (s.start, s.end): s for s in session_grid(calendar, day, interval, extended)
    }
    out: list[Bar] = []
    seen: set[tuple[InstrumentId, datetime, datetime]] = set()
    for r in rows:
        start, end = ensure_aware_utc(r.start), ensure_aware_utc(r.end)
        if (r.instrument_id, start, end) in seen:  # recovery must not depend on order
            raise BarError(f"repeated history bar {start}..{end}")
        seen.add((r.instrument_id, start, end))
        slot = grid.get((start, end))
        if slot is None:
            raise BarError(f"bar {start}..{end} is not on the session grid")
        args = (r.instrument_id, rights.feed_id, latency, slot, r.ohlc, r.volume)
        out.append(Bar(*args, None, at))  # Bar refuses receipt before the end
    return tuple(out)
