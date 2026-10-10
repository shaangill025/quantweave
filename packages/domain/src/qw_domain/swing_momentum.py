"""Long-only swing/position momentum reference STR-TREND-001 (T030 increment 1).

Spec §8 STR-TREND-001 and shared contract, §5 position sizing; R059, R062, R072,
R084. Research candidates only: action eligibility stays false until qualification
(`strategy_gate`). Conventions (`METHOD`):
- Input is the declared feed's finalized regular-session daily bar (one bar per
  session, `[open, close)`) of each of the latest N calendar sessions closed at t, as
  `BarBook.visible` shows it at t: the latest version known at t, a retrospective bar
  only from its own (recovery) knowledge time. A session with no such bar is
  `missing_session` (an unavailable oldest prefix is `insufficient_history`); two
  visible records with different closes are `bar_conflict`. Nothing is padded or
  carried forward.
- Split adjustment: splits are the latest versions known at t (`CorporateActionLog`).
  `SeriesBasis.adjusted_for` names the split versions the feed's prices already
  reflect; any other known split effective inside the window divides the closes
  before its effective date by its ratio. A basis naming a split unknown at t, of
  another version, or effective after the signal session is
  `split_adjustment_mismatch`. Stock dividends, mergers and spin-offs inside the
  window are not adjusted here and block (`corporate_action_unsupported`). A split
  known at t effective after the signal session and on or before the entry session
  blocks the signal and sizing (`split_before_entry`): no unit conversion.
- Signal at the last closed session s: momentum = C_s / C_(s-m) - 1, trend mean =
  mean of the last w closes, volatility = mean absolute close-to-close change over
  the last v changes (price units of session s). Candidate: momentum > 0 and C_s >
  trend mean (strict). Candidates rank by momentum, ties by instrument id.
- Timing: the signal is known at its latest input's knowledge time; the earliest
  action is the first regular-session open after t, never the signal close.
  `check_fill` refuses an earlier fill; `reference_entry` is that session's open.
- Exits (deterministic, next-session): `invalidated:<x>` (caller-supplied policy,
  thesis or risk invalidation), `trend_break` (C_s < trend mean), `stop_breach`
  (C_s <= the entry-units stop converted through later known splits) and
  `time_exit` (sessions held >= max_holding_sessions). Missing data without an
  invalidation is unknown (`exit=None`), never "hold".
- Sizing: stop = estimate - k x volatility, floored at 1e-12; per-unit planned loss
  = estimate - stop + 2 x unit cost; quantity = the lot floor of min((budget -
  2 x fixed) / per-unit loss, (cash - fixed) / (estimate + unit cost)). The budget is
  the adopted policy's planned-loss amount and cash is net of commitments: both are
  caller inputs; missing, stale or mismatched inputs block. Precondition: the
  estimate mark is quoted in the bars' currency (bars carry none to check). The
  candidate is input to `risk.evaluate`, which applies every policy limit; the stop
  is a scenario, not a guaranteed exit.
- Parameters are unqualified research defaults from the catalogue, read only from a
  configuration whose hash matches the adopted one. Exact arithmetic (`Fraction`).
LIMITATIONS: price return only (cash dividends are not adjusted); no automatic
detection of unexplained price discontinuities; universe screening (liquidity,
OTC/penny, R060) and diversification limits are the caller's and `risk`'s.
Stdlib only.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from functools import cache
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from qw_domain.bars import Bar, BarBook, SessionLabel
from qw_domain.calendars import CalendarCoverageError, ExchangeCalendar, Session
from qw_domain.corporate_actions import (
    CorporateActionLog,
    Merger,
    SpinOff,
    Split,
    StockDividend,
)
from qw_domain.decimals import Money, MoneyAmount, PositiveQuantity, Price, safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.quality_value import param_fraction, resolve_config, to_decimal
from qw_domain.rights import Use
from qw_domain.risk import ProposedAction, Side
from qw_domain.sources import ID_PATTERN
from qw_domain.strategy_registry import (
    AssetClass,
    DataNeed,
    Family,
    Horizon,
    Parameter,
    ParamKind,
    ParamValue,
    StrategyError,
    StrategyManifest,
)
from qw_domain.valuation import Mark, MarkKind

STRATEGY_ID, VERSION = "STR-TREND-001", "0.1.0-research"
METHOD = "momentum_trend_next_session/1"
_DAY = timedelta(days=1)
_SCALE = 10**12


class Status(StrEnum):
    CANDIDATE = "research_candidate"
    NOT_ELIGIBLE = "not_eligible"  # computed, fails the momentum/trend filter
    NO_SIGNAL = "no_signal"  # inputs missing, conflicting or mismatched


@dataclass(frozen=True, slots=True)
class SeriesBasis:
    """The feed whose bars are read and the split versions its prices reflect."""

    feed_id: str
    adjusted_for: Mapping[str, int]  # split event id -> version

    def __post_init__(self) -> None:
        if ID_PATTERN.fullmatch(self.feed_id) is None:
            raise StrategyError("basis", safe_repr(self.feed_id))
        object.__setattr__(
            self, "adjusted_for", MappingProxyType(dict(self.adjusted_for))
        )


@dataclass(frozen=True, slots=True)
class TrendInputs:
    as_of: datetime
    calendar: ExchangeCalendar
    book: BarBook
    bases: Mapping[InstrumentId, SeriesBasis]
    actions: CorporateActionLog
    universe: tuple[InstrumentId, ...]  # point-in-time membership at as_of
    config: Mapping[str, object]
    config_hash: str  # of the adopted configuration

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "as_of", ensure_aware_utc(self.as_of))
        put(self, "universe", tuple(dict.fromkeys(self.universe)))
        put(self, "bases", MappingProxyType(dict(self.bases)))
        put(self, "config", MappingProxyType(dict(self.config)))


@dataclass(frozen=True, slots=True)
class Signal:
    instrument_id: InstrumentId
    status: Status
    reasons: tuple[str, ...]  # why no signal or not eligible
    signal_session: date | None
    close: Decimal | None  # split-adjusted, units of the signal session
    momentum: Decimal | None
    trend_mean: Decimal | None
    volatility: Decimal | None
    known_at: datetime | None
    earliest_action_at: datetime | None  # the next regular-session open after t
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class TrendResult:
    method: str
    blocked: tuple[str, ...]
    signals: tuple[Signal, ...]  # candidates by rank, then others by instrument id
    research_only: ClassVar[bool] = True  # never an order or an actionable proposal

    @property
    def candidates(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.status is Status.CANDIDATE)


@dataclass(frozen=True, slots=True)
class _Series:
    sessions: tuple[date, ...]
    closes: tuple[Fraction, ...]
    known_at: datetime


def _closed_sessions(cal: ExchangeCalendar, t: datetime, n: int) -> list[Session]:
    """The latest n sessions closed at t, oldest first."""
    out: list[Session] = []
    day = t.astimezone(cal.tz).date()
    while len(out) < n:  # ends with CalendarCoverageError before the first version
        s = cal.session_for(day)
        if s is not None and s.close_at <= t:
            out.append(s)
        day -= _DAY
    return out[::-1]


def _daily(
    book: BarBook, i: InstrumentId, s: Session, feed: str, at: datetime
) -> tuple[Bar, ...]:
    return tuple(
        b
        for b in book.visible(i, s.open_at, s.close_at, at)
        if b.feed_id == feed and b.slot.label is SessionLabel.REGULAR
    )


def _known_splits(ins: TrendInputs, i: InstrumentId) -> dict[str, Split]:
    return {
        e.event_id: e
        for e in ins.actions.events(ins.as_of)
        if isinstance(e, Split) and e.instrument_id == i
    }


def _series(ins: TrendInputs, i: InstrumentId, n: int) -> _Series | tuple[str, ...]:
    basis = ins.bases.get(i)
    if basis is None:
        return ("series_basis_unknown",)
    try:
        sessions = _closed_sessions(ins.calendar, ins.as_of, n)
    except CalendarCoverageError:
        return ("calendar_uncovered",)
    raw: list[Fraction | None] = []
    known: list[datetime] = []
    for s in sessions:
        bars = _daily(ins.book, i, s, basis.feed_id, ins.as_of)
        if len({b.ohlc.close.value for b in bars}) > 1:
            return (f"bar_conflict:{s.session_date.isoformat()}",)
        raw.append(Fraction(bars[0].ohlc.close.value) if bars else None)
        known += [b.knowledge_at for b in bars]
    missing = [k for k, c in enumerate(raw) if c is None]
    if missing and missing == list(range(len(missing))):
        return (f"insufficient_history:{n - len(missing)}/{n}",)
    if missing:
        return (f"missing_session:{sessions[missing[-1]].session_date.isoformat()}",)
    days = tuple(s.session_date for s in sessions)
    first, last = days[0], days[-1]
    for e in ins.actions.events(ins.as_of):
        if (
            isinstance(e, StockDividend | Merger | SpinOff)
            and e.instrument_id == i
            and first < e.effective <= last
        ):
            return (f"corporate_action_unsupported:{e.event_id}",)
    splits = _known_splits(ins, i)
    for eid, version in sorted(basis.adjusted_for.items()):
        sp = splits.get(eid)
        if sp is None or sp.version != version or sp.effective > last:
            return (f"split_adjustment_mismatch:{eid}",)
    apply = [
        sp
        for sp in splits.values()
        if sp.event_id not in basis.adjusted_for and first < sp.effective <= last
    ]
    closes = []
    for day, c in zip(days, raw, strict=True):
        assert c is not None  # missing sessions returned above
        for sp in apply:
            if day < sp.effective:
                c = Fraction(c, sp.ratio.fraction())
        closes.append(c)
    known += [sp.known_at for sp in apply]
    return _Series(days, tuple(closes), max(known))


def _params(ins: TrendInputs) -> tuple[dict[str, ParamValue] | None, tuple[str, ...]]:
    return resolve_config(trend_manifest(), ins.config, ins.config_hash)


def _mean(xs: list[Fraction]) -> Fraction:
    return Fraction(sum(xs, Fraction(0)), len(xs))


def _next_open(ins: TrendInputs) -> datetime | None:
    try:
        return ins.calendar.next_open(ins.as_of)
    except CalendarCoverageError:
        return None


def _split_before_entry(
    ins: TrendInputs, i: InstrumentId, signal: date, entry_open: datetime
) -> tuple[str, ...]:
    """A split known at t effective in (signal session, entry session] would leave
    the signal, stop and estimate in different units: fail closed."""
    entry = entry_open.astimezone(ins.calendar.tz).date()
    return tuple(
        f"split_before_entry:{eid}"
        for eid, sp in sorted(_known_splits(ins, i).items())
        if signal < sp.effective <= entry
    )


def _signal(ins: TrendInputs, i: InstrumentId, p: Mapping[str, ParamValue]) -> Signal:
    m, w, v = (int(p[k]) for k in ("momentum_sessions", "trend_sessions",
                                   "volatility_sessions"))  # fmt: skip
    ser = _series(ins, i, max(w, m + 1, v + 1))
    nxt = _next_open(ins)
    if nxt is None and not isinstance(ser, tuple):
        ser = ("calendar_uncovered",)
    if not isinstance(ser, tuple) and nxt is not None:
        ser = _split_before_entry(ins, i, ser.sessions[-1], nxt) or ser
    if isinstance(ser, tuple):
        return Signal(i, Status.NO_SIGNAL, ser, None, None, None, None, None, None,
                      None)  # fmt: skip
    c = list(ser.closes)
    mom = Fraction(c[-1], c[-1 - m]) - 1
    mean = _mean(c[-w:])
    vol = _mean([abs(b - a) for a, b in pairwise(c[-v - 1 :])])
    reasons = tuple(
        why
        for bad, why in ((mom <= 0, "momentum_non_positive"),
                         (c[-1] <= mean, "close_not_above_trend"))
        if bad
    )  # fmt: skip
    status = Status.NOT_ELIGIBLE if reasons else Status.CANDIDATE
    return Signal(
        i, status, reasons, ser.sessions[-1], to_decimal(c[-1]), to_decimal(mom),
        to_decimal(mean), to_decimal(vol), ser.known_at, nxt,
    )  # fmt: skip


def evaluate(ins: TrendInputs) -> TrendResult:
    """Deterministic momentum/trend research candidates at `ins.as_of`."""
    params, blocked = _params(ins)
    if params is None:
        return TrendResult(METHOD, blocked, ())
    sigs = [_signal(ins, i, params) for i in ins.universe]
    cands = sorted(
        (s for s in sigs if s.status is Status.CANDIDATE),
        key=lambda s: (-(s.momentum or Decimal(0)), s.instrument_id.to_wire()),
    )
    rest = sorted(
        (s for s in sigs if s.status is not Status.CANDIDATE),
        key=lambda s: s.instrument_id.to_wire(),
    )
    ranked = [replace(s, rank=k) for k, s in enumerate(cands, 1)]
    return TrendResult(METHOD, (), (*ranked, *rest))


def check_fill(sig: Signal, fill_at: datetime) -> str | None:
    """None when a fill at `fill_at` respects next-session timing."""
    if sig.status is not Status.CANDIDATE or sig.earliest_action_at is None:
        return "not_a_candidate"
    if ensure_aware_utc(fill_at) < sig.earliest_action_at:
        return "same_close_fill"
    return None


def reference_entry(
    sig: Signal, cal: ExchangeCalendar, book: BarBook, feed_id: str, at: datetime
) -> Price | str:
    """The evaluation fill price: the entry session's open, once its bar is known."""
    if sig.status is not Status.CANDIDATE or sig.earliest_action_at is None:
        return "not_a_candidate"
    s = cal.session_for(sig.earliest_action_at.astimezone(cal.tz).date())
    assert s is not None  # earliest_action_at is a session open
    bars = _daily(book, sig.instrument_id, s, feed_id, ensure_aware_utc(at))
    if not bars:
        return "entry_bar_not_known"
    if len({b.ohlc.open.value for b in bars}) > 1:
        return "bar_conflict"
    return bars[0].ohlc.open


@dataclass(frozen=True, slots=True)
class Held:
    instrument_id: InstrumentId
    entry_session: date
    stop: Price | None  # in the units of the entry session
    invalidations: tuple[str, ...]  # policy/thesis/risk invalidations known at t


@dataclass(frozen=True, slots=True)
class ExitDecision:
    instrument_id: InstrumentId
    exit: bool | None  # None: unknown, never an assumed hold
    reasons: tuple[str, ...]
    earliest_action_at: datetime | None


def exit_decision(h: Held, ins: TrendInputs) -> ExitDecision:
    params, blocked = _params(ins)
    if params is None:
        return ExitDecision(h.instrument_id, None, blocked, None)
    reasons = [f"invalidated:{x}" for x in h.invalidations]
    ser, nxt = _series(ins, h.instrument_id, int(params["trend_sessions"])), None
    if not isinstance(ser, tuple):
        nxt = _next_open(ins)
    if isinstance(ser, tuple) or nxt is None:
        why = ser if isinstance(ser, tuple) else ("calendar_uncovered",)
        if reasons:
            return ExitDecision(h.instrument_id, True, tuple(reasons), _next_open(ins))
        return ExitDecision(h.instrument_id, None, why, None)
    last, day = ser.closes[-1], ser.sessions[-1]
    if last < _mean(list(ser.closes)):
        reasons.append("trend_break")
    if h.stop is not None:
        stop = Fraction(h.stop.value)
        for sp in _known_splits(ins, h.instrument_id).values():
            if h.entry_session < sp.effective <= day:
                stop = Fraction(stop, sp.ratio.fraction())
        if last <= stop:
            reasons.append("stop_breach")
    held = 0
    if h.entry_session <= day:
        held = ins.calendar.trading_days_between(h.entry_session, day) + 1
    if held >= int(params["max_holding_sessions"]):
        reasons.append("time_exit")
    return ExitDecision(h.instrument_id, bool(reasons), tuple(reasons), nxt)


@dataclass(frozen=True, slots=True)
class SizingInputs:
    estimate: Mark | None  # fresh ask or last price for the entry estimate
    market_max_age: timedelta
    risk_budget: Money | None  # adopted planned-loss amount for one trade
    available_cash: Money | None  # broker-available, net of commitments (T017)
    lot: PositiveQuantity  # a power of ten
    fixed_cost: Money
    unit_cost: Money


@dataclass(frozen=True, slots=True)
class TrendCandidate:
    """A proposal candidate, not an order."""

    instrument_id: InstrumentId
    signal_session: date
    earliest_action_at: datetime
    quantity: PositiveQuantity
    lot: PositiveQuantity
    estimate: Price
    estimate_observed_at: datetime
    stop: Price  # scenario exit, not a guaranteed fill
    fixed_cost: Money
    unit_cost: Money
    cash_required: Money  # fixed + q x (estimate + unit cost), rounded up
    planned_loss: Money  # 2 x fixed + q x per-unit loss, rounded up

    def to_action(
        self, *, tenant_id: str, account_id: str, denominator: str, issuer_id: str,
        sector_id: str, leveraged_or_inverse: bool | None,
        sleeve_id: str | None = None,
    ) -> ProposedAction:  # fmt: skip
        return ProposedAction(
            tenant_id, account_id, self.cash_required.currency, sleeve_id,
            STRATEGY_ID, denominator, self.instrument_id, issuer_id, sector_id,
            Horizon.SWING_POSITION.value, Side.BUY, self.quantity, self.lot,
            self.stop, self.fixed_cost, self.unit_cost, leveraged_or_inverse,
        )  # fmt: skip


def _scaled(x: Fraction, *, up: bool) -> Decimal:
    n = x * _SCALE
    whole = -(-n.numerator // n.denominator) if up else n.numerator // n.denominator
    return Decimal(whole).scaleb(-12)


def size_entry(
    sig: Signal, ins: TrendInputs, s: SizingInputs
) -> TrendCandidate | tuple[str, ...]:
    """Volatility-stop sizing of a candidate; a tuple of reasons when blocked."""
    if (
        sig.status is not Status.CANDIDATE
        or sig.volatility is None
        or sig.signal_session is None
        or sig.earliest_action_at is None
    ):
        return ("not_a_candidate",)
    params, blocked = _params(ins)
    if params is None:
        return blocked
    gap = _split_before_entry(
        ins, sig.instrument_id, sig.signal_session, sig.earliest_action_at
    )
    if gap:
        return gap
    mark, budget, cash = s.estimate, s.risk_budget, s.available_cash
    if mark is None:
        return ("estimate_missing",)
    if budget is None:
        return ("risk_budget_missing",)
    if cash is None:
        return ("cash_unknown",)
    if any(x.currency != mark.currency for x in (budget, cash, s.fixed_cost,
                                                 s.unit_cost)):  # fmt: skip
        return ("currency_mismatch",)
    if mark.instrument_id != sig.instrument_id or mark.kind not in (
        MarkKind.ASK, MarkKind.LAST,
    ):  # fmt: skip
        return ("estimate_unsuitable",)
    age = ins.as_of - mark.observed_at
    if age < timedelta(0):
        return ("estimate_not_yet_observed",)
    if age > s.market_max_age:
        return ("estimate_stale",)
    px, unit = Fraction(mark.price.value), Fraction(s.unit_cost.amount.value)
    fixed, lot = Fraction(s.fixed_cost.amount.value), Fraction(s.lot.value)
    k = param_fraction(params, "stop_volatility_multiple")
    stop = Fraction(_scaled(px - k * Fraction(sig.volatility), up=False))
    if stop <= 0:
        return ("stop_non_positive",)
    per_unit = px - stop + 2 * unit
    q_risk = Fraction(Fraction(budget.amount.value) - 2 * fixed, per_unit)
    q_cash = Fraction(Fraction(cash.amount.value) - fixed, px + unit)
    q = (min(q_risk, q_cash) // lot) * lot
    if q <= 0:
        return ("below_lot",)
    ccy = mark.currency
    return TrendCandidate(
        sig.instrument_id, sig.signal_session, sig.earliest_action_at,
        PositiveQuantity(Decimal(int(q // lot)) * s.lot.value), s.lot, mark.price,
        mark.observed_at, Price(_scaled(stop, up=False)), s.fixed_cost, s.unit_cost,
        Money(MoneyAmount(_scaled(fixed + q * (px + unit), up=True)), ccy),
        Money(MoneyAmount(_scaled(2 * fixed + q * per_unit, up=True)), ccy),
    )  # fmt: skip


@cache
def trend_manifest(feed_id: str = "feed-config-check") -> StrategyManifest:
    """STR-TREND-001 for `strategy_registry`; its code hash is this module's source.
    Session counts are the catalogue's research seeds; the rest are unqualified
    research choices (the spec gives no values)."""
    i, d = ParamKind.INTEGER, ParamKind.DECIMAL
    params = (
        Parameter("momentum_sessions", i, 126, 2, 504),
        Parameter("trend_sessions", i, 200, 2, 756),
        Parameter("volatility_sessions", i, 20, 2, 252),
        Parameter("stop_volatility_multiple", d, Decimal(2), Decimal("0.5"),
                  Decimal(10)),
        Parameter("max_holding_sessions", i, 126, 1, 756),
    )  # fmt: skip
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return StrategyManifest(
        STRATEGY_ID, VERSION, Family.SWING_MOMENTUM, code,
        (DataNeed(feed_id, frozenset({Use.DERIVED_DATA})),),
        frozenset({AssetClass.STOCKS, AssetClass.ETFS}), Horizon.SWING_POSITION,
        True, params, "protocol-trend-reference-1", None,
        "Momentum/trend swing reference",
    )  # fmt: skip
