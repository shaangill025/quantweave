"""Normalized bars on a session grid, sequence gaps and versioned corrections
(T022 increment 1; R050, R066, R083, R091; spec §6 "Normalization and continuity").

- Bars are left-closed, right-open `[start, end)` on a session grid derived from the
  versioned exchange calendar (T011): regular bins are anchored at the open, post
  bins at the close and pre bins end at the open; no bin crosses a session edge (the
  last one is truncated). Pre/post exist only when `ExtendedHours` is given and are
  labelled; anything else outside the session is rejected, never binned.
- Live trades are aggregated per (feed, instrument): only condition-eligible trades
  known by the build's receipt time, in closed intervals. Duplicates and sequence
  conflicts are rejected with a reason; a sequence gap is reported and its intervals
  are not built (a partial bar is never presented as complete). Volume is the
  feed's own volume: an exchange-limited feed's volume is not consolidated volume.
- A bar is known from `knowledge_at` (at least its end). Corrections are appended as
  new versions in `BarBook`; readers see the latest version known at their time.
- Live trades pass the T021 ingestion gate (`ingest.require_ingest`) at the build
  time. `retrospective` marks bars reconstructed after the fact (recovery is
  increment 2); a retrospective bar is a separate record, never a version of a live
  one.
LIMITATIONS: no coverage accounting, recovery or EOD/delayed history ingestion yet
(increment 2); no persistence. Stdlib only.
"""

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise

from qw_domain.calendars import ExchangeCalendar
from qw_domain.decimals import PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights, require_ingest
from qw_domain.instants import ensure_aware_utc, require_date
from qw_domain.streams import FeedLatency

_MIN = timedelta(minutes=1)
_ZERO = timedelta(0)


class BarError(ValueError):
    pass


class SessionLabel(StrEnum):
    PRE = "pre"
    REGULAR = "regular"
    POST = "post"


@dataclass(frozen=True, slots=True)
class ExtendedHours:
    pre: timedelta
    post: timedelta

    def __post_init__(self) -> None:
        for d in (self.pre, self.post):
            if type(d) is not timedelta or d < _ZERO:
                raise BarError("extended hours must be non-negative timedeltas")


@dataclass(frozen=True, slots=True)
class Slot:
    label: SessionLabel
    session_date: date
    start: datetime
    end: datetime


def session_grid(
    calendar: ExchangeCalendar,
    day: date,
    interval: timedelta | None,
    extended: ExtendedHours | None = None,
) -> tuple[Slot, ...]:
    """The bar intervals of `day`; `interval=None` gives one bar per segment."""
    require_date(day, "day")
    if interval is not None and (
        type(interval) is not timedelta or interval < _MIN or interval % _MIN
    ):
        raise BarError("interval must be a whole number of minutes")
    session = calendar.session_for(day)
    if session is None:
        return ()
    o, c = session.open_at, session.close_at
    segs = [(SessionLabel.REGULAR, o, c)]
    if extended is not None:
        segs += [(SessionLabel.PRE, o - extended.pre, o)]
        segs += [(SessionLabel.POST, c, c + extended.post)]
    slots: list[Slot] = []
    for label, a, b in segs:
        step = b - a if interval is None else interval
        if label is SessionLabel.PRE:  # bins end at the open
            t = b
            while t > a:
                slots.append(Slot(label, day, max(a, t - step), t))
                t -= step
        else:
            t = a
            while t < b:
                slots.append(Slot(label, day, t, min(b, t + step)))
                t += step
    return tuple(sorted(slots, key=lambda s: s.start))


@dataclass(frozen=True, slots=True)
class Trade:
    instrument_id: InstrumentId
    feed_id: str
    sequence: int  # per (feed, instrument) stream
    price: Price
    size: PositiveQuantity
    event_at: datetime
    received_at: datetime  # server receipt time
    eligible: bool  # trade conditions eligible for bar construction

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_at", ensure_aware_utc(self.event_at))
        object.__setattr__(self, "received_at", ensure_aware_utc(self.received_at))
        if type(self.price) is not Price or self.price.value <= 0:
            raise BarError("trade price must be a positive Price")
        if type(self.size) is not PositiveQuantity:
            raise BarError("trade size must be a PositiveQuantity")
        if type(self.sequence) is not int or self.sequence < 0:
            raise BarError("sequence must be a non-negative int")
        if type(self.eligible) is not bool:
            raise BarError("eligible must be a bool")


@dataclass(frozen=True, slots=True)
class Ohlc:
    open: Price
    high: Price
    low: Price
    close: Price

    def __post_init__(self) -> None:
        px = (self.open, self.high, self.low, self.close)
        if not all(type(p) is Price for p in px):
            raise BarError("prices must be Price")
        o, h, lo, c = (p.value for p in px)
        if not (Decimal(0) < lo <= min(o, c) and max(o, c) <= h):
            raise BarError("inconsistent OHLC")


@dataclass(frozen=True, slots=True)
class Bar:
    instrument_id: InstrumentId
    feed_id: str
    latency: FeedLatency
    slot: Slot  # session label, session date and [start, end)
    ohlc: Ohlc
    volume: Quantity  # the feed's own volume
    trade_count: int | None  # None for provider bars
    knowledge_at: datetime  # when this content became known
    retrospective: bool = False  # reconstructed after the fact
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "knowledge_at", ensure_aware_utc(self.knowledge_at))
        if type(self.ohlc) is not Ohlc or type(self.volume) is not Quantity:
            raise BarError("a bar needs Ohlc and a Quantity volume")
        if self.volume.value < 0 or self.knowledge_at < self.slot.end:
            raise BarError("negative volume, or known before the interval ended")

    @property
    def start(self) -> datetime:
        return self.slot.start

    @property
    def end(self) -> datetime:
        return self.slot.end

    @property
    def key(self) -> tuple[InstrumentId, str, datetime, datetime, bool]:
        return (
            self.instrument_id,
            self.feed_id,
            self.start,
            self.end,
            self.retrospective,
        )

    @property
    def content(self) -> tuple[object, ...]:
        return (
            self.key,
            self.latency,
            self.slot,
            self.ohlc,
            self.volume,
            self.trade_count,
        )


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    end: datetime  # inclusive: data in [start, end] may be missing
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_aware_utc(self.start))
        object.__setattr__(self, "end", ensure_aware_utc(self.end))
        if self.end < self.start or not self.reason:
            raise BarError("a gap needs start <= end and a reason")

    def touches(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start <= self.end


@dataclass(frozen=True, slots=True)
class BarBuild:
    bars: tuple[Bar, ...]
    rejected: tuple[tuple[Trade, str], ...]
    gaps: tuple[Gap, ...]


def _locate(grid: Sequence[Slot], at: datetime) -> Slot | None:
    i = bisect_right([s.start for s in grid], at) - 1
    return grid[i] if i >= 0 and at < grid[i].end else None


def build_bars(
    trades: Iterable[Trade],
    calendar: ExchangeCalendar,
    day: date,
    instrument_id: InstrumentId,
    interval: timedelta,
    latency: FeedLatency,
    rights: IngestRights,
    extended: ExtendedHours | None = None,
) -> BarBuild:
    """Bars of one instrument on `rights.feed_id`, as known at `rights.received_at`."""
    if type(rights) is not IngestRights:
        raise TypeError("bar construction needs IngestRights")
    items = list(trades)
    if any(
        t.instrument_id != instrument_id or t.feed_id != rights.feed_id for t in items
    ):
        raise BarError("every trade must be of this instrument and feed")
    require_ingest(rights)
    as_of, grid = rights.received_at, session_grid(calendar, day, interval, extended)
    rejected: list[tuple[Trade, str]] = []
    first: dict[int, Trade] = {}
    for t in sorted(items, key=lambda t: t.received_at):  # first known wins
        prior = first.get(t.sequence)
        if t.received_at > as_of:
            rejected.append((t, "not_yet_known"))
        elif t.received_at < t.event_at:
            rejected.append((t, "receipt_before_event"))
        elif prior is not None:
            same = replace(prior, received_at=t.received_at) == t  # a redelivery
            rejected.append((t, "duplicate" if same else "sequence_conflict"))
        else:
            first[t.sequence] = t
    gaps = tuple(
        Gap(min(a.event_at, b.event_at), max(a.event_at, b.event_at), "sequence_gap")
        for a, b in pairwise(sorted(first.values(), key=lambda t: t.sequence))
        if b.sequence > a.sequence + 1
    )
    bins: dict[Slot, list[Trade]] = {}
    for t in first.values():
        slot = _locate(grid, t.event_at)
        if not t.eligible:
            rejected.append((t, "ineligible_condition"))
        elif slot is None:
            rejected.append((t, "outside_session"))
        elif slot.end > as_of:
            rejected.append((t, "interval_open"))
        elif any(g.touches(slot.start, slot.end) for g in gaps):
            rejected.append((t, "in_gap"))
        else:
            bins.setdefault(slot, []).append(t)
    bars = []
    for slot, group in sorted(bins.items(), key=lambda kv: kv[0].start):
        group.sort(key=lambda t: (t.event_at, t.sequence))
        px = [t.price.value for t in group]
        volume = sum((t.size.value for t in group), Decimal(0))
        known = max([slot.end, *(t.received_at for t in group)])
        ohlc = Ohlc(Price(px[0]), Price(max(px)), Price(min(px)), Price(px[-1]))
        args = (instrument_id, rights.feed_id, latency, slot, ohlc, Quantity(volume))
        bars.append(Bar(*args, len(group), known))
    return BarBuild(tuple(bars), tuple(rejected), gaps)


@dataclass(frozen=True, slots=True)
class BarBook:
    """Append-only bar versions; a correction never overwrites what was known."""

    bars: tuple[Bar, ...] = ()

    def record(self, bar: Bar) -> "BarBook":
        if type(bar) is not Bar:
            raise TypeError("a Bar is required")
        prior = [b for b in self.bars if b.key == bar.key]
        if prior and prior[-1].content == bar.content:
            return self
        if prior and bar.knowledge_at < prior[-1].knowledge_at:
            raise BarError("a correction cannot be known before the version it fixes")
        return BarBook((*self.bars, replace(bar, version=len(prior) + 1)))

    def visible(
        self, instrument_id: InstrumentId, start: datetime, end: datetime, at: datetime
    ) -> tuple[Bar, ...]:
        """Latest version per source known at `at`, ordered by feed id."""
        latest: dict[object, Bar] = {}
        for b in self.bars:
            if (b.instrument_id, b.start, b.end) == (instrument_id, start, end) and (
                b.knowledge_at <= at
            ):
                latest[b.key] = b
        return tuple(
            sorted(latest.values(), key=lambda b: (b.feed_id, b.retrospective))
        )
