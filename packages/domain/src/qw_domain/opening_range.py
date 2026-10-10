"""Regular-session opening-range breakout reference STR-ORB-001 (T031 increment 1).

Spec §8 STR-ORB-001, §6 "Do not backfill the current opening range with unavailable
future data or a different feed without reinitialization"; R050, R066, R067, R072,
R091. Research candidates only: action eligibility stays false until qualification
(`strategy_gate`). Long-only, at most one entry per security and session.
- The range is `[open, open + opening_range_minutes)` of the actual session from the
  versioned calendar (no session: `no_session`). It is frozen at `frozen_at = end +
  completion_buffer_seconds` from the 1-minute bars of the configured feed known
  then; before that it is `range_not_complete`. Every minute needs a live bar
  (`range_interval_missing`; a bar known after the freeze is missing), a
  retrospective-only minute is `range_interval_unconfirmed`, a sequence gap touching
  the range is `range_gap`, a continuity watermark before the range end is
  `range_trailing_loss`, and a correction known after the freeze is
  `range_corrected` (reinitialization, never a silent refreeze). high == low is
  `range_zero`.
- Bars of any other feed inside the range or the signal window are `feed_switch`
  (AT066 reinitialization). A halt of the instrument or the market overlapping
  [open, as_of] (halts are half-open) is `halted`.
- Breakout: the first later 1-minute bar, known at `as_of`, of the same feed whose
  close is at least range_high + breakout_ticks x tick_size. Minutes before it must be
  covered like the range (`signal_interval_missing`); scanning stops at a
  not-yet-known minute, or at the continuity watermark, where a closed minute past
  it is `signal_not_confirmed`. The signal is triggered at the bar's knowledge time.
- Deadlines come from the session's actual close (early closes included): exit =
  close - exit_minutes_before_close (flat before close), entry = exit -
  min_action_window_minutes, review = min(trigger + review_window_seconds, entry),
  with review_window_seconds at most 60 (AT067). A trigger at or after the entry
  deadline is `entry_window_closed`; `as_of` at or after the review deadline is
  `review_expired`. Deadlines are never extended and a later breakout bar never opens
  a second entry.
- Range and signal are research observations. A proposal candidate also needs a
  real-time feed (`feed_not_realtime` otherwise: delayed data is research-only) and
  the feed qualified for financial recommendation at `as_of` (`feed_unqualified`).
- Stop/target: the catalogue declares a same-session time exit and no price target
  (`target` is None). `adverse_exit_scenario` is range_low, a sizing scenario for the
  risk engine, not a guaranteed stop fill.
LIMITATIONS: T022 `coverage` states are not used yet, so a quiet minute (no eligible
trades) has no bar and blocks (fails closed); a concurrent second feed blocks even when
harmless (one feed per range); gaps, the continuity watermark and halts are
caller-supplied facts known at `as_of` (calendars record no halt knowledge time);
no indicative-feed class exists in `FeedLatency`; no volume, spread or slippage
criterion yet; candidates are not yet bound into `proposals.ProposalVersion`.
Stdlib only.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import cache
from pathlib import Path

from qw_domain.bars import Bar, BarBook, Gap
from qw_domain.calendars import CalendarCoverageError, ExchangeCalendar, Halt
from qw_domain.decimals import Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.rights import Registry, Use, UseScope, check_use
from qw_domain.strategy_registry import (
    AssetClass,
    DataNeed,
    Family,
    Horizon,
    Parameter,
    ParamKind,
    StrategyError,
    StrategyManifest,
)
from qw_domain.streams import FeedLatency

STRATEGY_ID, VERSION = "STR-ORB-001", "0.1.0-research"
METHOD = "opening_range_breakout/1"
_MIN = timedelta(minutes=1)
_REALTIME = frozenset(
    {FeedLatency.REALTIME_CONSOLIDATED, FeedLatency.REALTIME_EXCHANGE_LIMITED}
)


class OrbCode(StrEnum):  # declaration order is the report order
    CONFIG_INVALID = "config_invalid"
    CONFIG_MISMATCH = "config_mismatch"
    NO_SESSION = "no_session"
    RANGE_NOT_COMPLETE = "range_not_complete"
    FEED_SWITCH = "feed_switch"
    RANGE_INTERVAL_MISSING = "range_interval_missing"
    RANGE_INTERVAL_UNCONFIRMED = "range_interval_unconfirmed"
    RANGE_GAP = "range_gap"
    RANGE_TRAILING_LOSS = "range_trailing_loss"
    RANGE_CORRECTED = "range_corrected"
    RANGE_ZERO = "range_zero"
    HALTED = "halted"
    SIGNAL_INTERVAL_MISSING = "signal_interval_missing"
    SIGNAL_NOT_CONFIRMED = "signal_not_confirmed"
    ENTRY_WINDOW_CLOSED = "entry_window_closed"
    REVIEW_EXPIRED = "review_expired"
    FEED_NOT_REALTIME = "feed_not_realtime"
    FEED_UNQUALIFIED = "feed_unqualified"


_ORDER = {c: i for i, c in enumerate(OrbCode)}


@dataclass(frozen=True, slots=True)
class OrbReason:
    code: OrbCode
    subject: str


@dataclass(frozen=True, slots=True)
class OrbInputs:
    instrument_id: InstrumentId
    session_date: date
    feed_id: str  # the configured, qualified intraday feed
    calendar: ExchangeCalendar
    book: BarBook
    gaps: tuple[Gap, ...]  # sequence gaps of (feed, instrument) known at as_of
    continuity_through: datetime | None  # stream continuous through this instant
    halts: tuple[Halt, ...]  # halts known at as_of
    tick_size: Price
    rights: Registry
    tenant_id: str
    scope: UseScope
    jurisdiction: str
    config: Mapping[str, object]
    config_hash: str  # the adopted configuration hash
    as_of: datetime  # server-clock decision time

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        if self.continuity_through is not None:
            at = ensure_aware_utc(self.continuity_through)
            object.__setattr__(self, "continuity_through", at)
        if type(self.tick_size) is not Price or self.tick_size.value <= 0:
            raise ValueError("tick_size must be a positive Price")


@dataclass(frozen=True, slots=True)
class OpeningRange:
    start: datetime
    end: datetime
    frozen_at: datetime
    high: Decimal
    low: Decimal
    feed_id: str
    calendar_version: str
    bar_versions: tuple[int, ...]  # the frozen version of each minute


@dataclass(frozen=True, slots=True)
class Breakout:
    bar_start: datetime
    close: Decimal
    threshold: Decimal
    known_at: datetime


@dataclass(frozen=True, slots=True)
class OrbCandidate:
    """A long proposal candidate; the proposal pipeline and risk engine size it."""

    instrument_id: InstrumentId
    session_date: date
    range: OpeningRange
    trigger_at: datetime
    entry_reference: Decimal  # breakout close: a reference, never an assumed fill
    adverse_exit_scenario: Decimal  # range low
    risk_per_unit: Decimal
    target: Decimal | None  # None: time exit only
    entry_deadline: datetime
    exit_deadline: datetime
    review_deadline: datetime
    config_hash: str
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class OrbResult:
    method: str
    range: OpeningRange | None  # research observation
    signal: Breakout | None  # research observation
    candidate: OrbCandidate | None
    reasons: tuple[OrbReason, ...]


def check_review(c: OrbCandidate, at: datetime) -> tuple[OrbReason, ...]:
    """A review or approval at `at` is too late at or after the review deadline."""
    if ensure_aware_utc(at) >= c.review_deadline:
        return (OrbReason(OrbCode.REVIEW_EXPIRED, format_instant(c.review_deadline)),)
    return ()


def _pick(
    book: BarBook, ins: OrbInputs, start: datetime, at: datetime, why: list[OrbReason]
) -> tuple[Bar | None, bool]:
    """The live bar of the configured feed for [start, start+1min) known at `at`,
    and whether only a retrospective one exists. Other feeds are a switch."""
    vis = book.visible(ins.instrument_id, start, start + _MIN, ins.as_of)
    if any(b.feed_id != ins.feed_id for b in vis):
        why.append(OrbReason(OrbCode.FEED_SWITCH, format_instant(start)))
    mine = [
        b
        for b in book.visible(ins.instrument_id, start, start + _MIN, at)
        if b.feed_id == ins.feed_id
    ]
    live = [b for b in mine if not b.retrospective]
    return (live[0] if live else None), bool(mine) and not live


def evaluate(ins: OrbInputs) -> OrbResult:
    """Deterministic ORB evaluation of one instrument and session at `ins.as_of`."""
    why: list[OrbReason] = []

    def done(
        rng: OpeningRange | None = None,
        sig: Breakout | None = None,
        cand: OrbCandidate | None = None,
    ) -> OrbResult:
        why.sort(key=lambda r: (_ORDER[r.code], r.subject))
        return OrbResult(METHOD, rng, sig, None if why else cand, tuple(why))

    m, at = orb_manifest(ins.feed_id), ins.as_of
    try:
        p = m.resolve(ins.config)
        digest = m.config_hash(ins.config)
    except StrategyError as exc:
        why.append(OrbReason(OrbCode.CONFIG_INVALID, str(exc)))
        return done()
    if digest != ins.config_hash:
        why.append(OrbReason(OrbCode.CONFIG_MISMATCH, ins.config_hash))
        return done()
    span, ticks = int(p["opening_range_minutes"]), int(p["breakout_ticks"])
    buffer = timedelta(seconds=int(p["completion_buffer_seconds"]))
    review = timedelta(seconds=int(p["review_window_seconds"]))
    try:
        session = ins.calendar.session_for(ins.session_date)
    except CalendarCoverageError:
        session = None
    if session is None:
        why.append(OrbReason(OrbCode.NO_SESSION, ins.session_date.isoformat()))
        return done()
    start, end = session.open_at, session.open_at + span * _MIN
    frozen_at = end + buffer
    exit_at = session.close_at - int(p["exit_minutes_before_close"]) * _MIN
    entry_at = exit_at - int(p["min_action_window_minutes"]) * _MIN
    if at < frozen_at:
        why.append(OrbReason(OrbCode.RANGE_NOT_COMPLETE, format_instant(frozen_at)))
        return done()

    # The frozen range.
    used: list[Bar] = []
    for k in range(span):
        s = start + k * _MIN
        b, retro_only = _pick(ins.book, ins, s, frozen_at, why)
        if b is None:
            code = (
                OrbCode.RANGE_INTERVAL_UNCONFIRMED
                if retro_only
                else OrbCode.RANGE_INTERVAL_MISSING
            )
            why.append(OrbReason(code, format_instant(s)))
            continue
        now = [x for x in ins.book.visible(ins.instrument_id, s, s + _MIN, at)
               if x.key == b.key]  # fmt: skip
        if now and now[0].version != b.version:
            why.append(OrbReason(OrbCode.RANGE_CORRECTED, format_instant(s)))
        used.append(b)
    for g in ins.gaps:
        if g.touches(start, end):
            why.append(OrbReason(OrbCode.RANGE_GAP, format_instant(g.start)))
    watermark = ins.continuity_through
    if watermark is None or watermark < end:
        seen = "none" if watermark is None else format_instant(watermark)
        why.append(OrbReason(OrbCode.RANGE_TRAILING_LOSS, seen))
    for h in ins.halts:
        scoped = h.instrument_id is None or h.instrument_id == ins.instrument_id
        if scoped and h.start <= at and (h.end is None or h.end > start):
            why.append(OrbReason(OrbCode.HALTED, format_instant(h.start)))
    if why or watermark is None:
        return done()
    high = max(b.ohlc.high.value for b in used)
    low = min(b.ohlc.low.value for b in used)
    if high == low:
        why.append(OrbReason(OrbCode.RANGE_ZERO, str(high)))
        return done()
    rng = OpeningRange(
        start, end, frozen_at, high, low, ins.feed_id, session.version,
        tuple(b.version for b in used),
    )  # fmt: skip

    # The first breakout known at `as_of`.
    threshold = high + ticks * ins.tick_size.value
    sig: Breakout | None = None
    s = end
    while sig is None and s + _MIN <= min(at, watermark, session.close_at):
        b, retro_only = _pick(ins.book, ins, s, at, why)
        if why:  # a feed switch
            return done(rng)
        gap = any(g.touches(s, s + _MIN) for g in ins.gaps)
        if b is None or gap:
            if not (retro_only or gap) and s + _MIN + buffer > at:
                break  # not yet known: within the late-data buffer
            why.append(OrbReason(OrbCode.SIGNAL_INTERVAL_MISSING, format_instant(s)))
            return done(rng)
        used.append(b)
        if b.ohlc.close.value >= threshold:
            sig = Breakout(s, b.ohlc.close.value, threshold, b.knowledge_at)
        s += _MIN
    if sig is None and watermark < s + _MIN <= min(at, session.close_at):
        seen = format_instant(watermark)  # a closed minute past the watermark
        why.append(OrbReason(OrbCode.SIGNAL_NOT_CONFIRMED, seen))
    if sig is None:
        return done(rng)

    # Proposal-only checks: deadlines, feed latency and qualification.
    review_at = min(sig.known_at + review, entry_at)
    if sig.known_at >= entry_at:
        why.append(OrbReason(OrbCode.ENTRY_WINDOW_CLOSED, format_instant(entry_at)))
    elif at >= review_at:
        why.append(OrbReason(OrbCode.REVIEW_EXPIRED, format_instant(review_at)))
    for lat in sorted({b.latency for b in used} - _REALTIME):
        why.append(OrbReason(OrbCode.FEED_NOT_REALTIME, lat.value))
    use = check_use(ins.rights, ins.tenant_id, ins.feed_id,
                    Use.FINANCIAL_RECOMMENDATION, at, scope=ins.scope,
                    jurisdiction=ins.jurisdiction)  # fmt: skip
    for r in use.reasons:
        why.append(OrbReason(OrbCode.FEED_UNQUALIFIED, f"{r.code.value}:{r.subject}"))
    cand = OrbCandidate(
        ins.instrument_id, ins.session_date, rng, sig.known_at, sig.close, low,
        sig.close - low, None, entry_at, exit_at, review_at, digest, m.material_hash,
    )  # fmt: skip
    return done(rng, sig, cand)


@cache
def orb_manifest(feed_id: str = "feed-config-check") -> StrategyManifest:
    """STR-ORB-001 for `strategy_registry`, regular-session only. The catalogue binds
    opening_range_minutes = 15 and signal_bar_minutes = 1; the other defaults are
    unqualified research choices, not tuned or profitable settings."""
    params = (
        Parameter("opening_range_minutes", ParamKind.INTEGER, 15, 5, 60),
        Parameter("signal_bar_minutes", ParamKind.INTEGER, 1, 1, 1),
        Parameter("completion_buffer_seconds", ParamKind.INTEGER, 30, 5, 300),
        Parameter("breakout_ticks", ParamKind.INTEGER, 1, 1, 100),
        Parameter("exit_minutes_before_close", ParamKind.INTEGER, 10, 1, 60),
        Parameter("min_action_window_minutes", ParamKind.INTEGER, 30, 5, 120),
        Parameter("review_window_seconds", ParamKind.INTEGER, 60, 1, 60),
    )
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return StrategyManifest(
        STRATEGY_ID, VERSION, Family.OPENING_RANGE_BREAKOUT, code,
        (DataNeed(feed_id, frozenset({Use.DERIVED_DATA})),),
        frozenset({AssetClass.STOCKS, AssetClass.ETFS}), Horizon.INTRADAY, True,
        params, "protocol-orb-reference-1", None, "Opening-range breakout reference",
    )  # fmt: skip
