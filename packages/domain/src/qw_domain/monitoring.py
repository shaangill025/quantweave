"""Deterministic watchlist events and the alert lifecycle (T024; R009, R014, R020,
R047; spec §2 Journey C, §6 "Freshness is multidimensional", §13 notifications).

- Metrics of the contract `Condition`: `price_cross` (gt/lt: prior close at or short
  of the level, latest close beyond it), `pct_move` (close to close) and `volume`.
  Review dates come from the entry. Only closed coverage known at `at` is read
  (anything later is `look_ahead`); comparisons are exact Decimal.
- Input not real-time covered, older than `max_age`, from another feed, incomplete,
  without a bar (`Coverage` names no feed, so silence is not attributable to the
  rule's feed) or with no configured threshold evaluates to `stale_source` with the
  reason: it never fires and is never skipped silently.
- `AlertBook` (per tenant): one alert per (rule, trigger instance); none during the
  cooldown after the rule's last alert or while snoozed. A suppressed instance is
  consumed, so it never alerts late. Changing an active snooze needs `extend=True`.
  Every decision is logged. Alerts are factual events; delivery belongs to T056.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum

from qw_domain.bars import Bar
from qw_domain.coverage import Coverage, CoverageState
from qw_domain.decimals import DOMAIN_CONTEXT, DecimalValueError, Quantity, safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.sources import ID_PATTERN


class MonitoringError(ValueError):
    pass


class Metric(StrEnum):
    PRICE_CROSS = "price_cross"
    PCT_MOVE = "pct_move"
    VOLUME = "volume"


_RANGE = frozenset({"gt", "gte", "lt", "lte", "between"})
_OPS = {
    Metric.PRICE_CROSS: frozenset({"gt", "lt"}),
    Metric.PCT_MOVE: _RANGE,
    Metric.VOLUME: _RANGE,
}


def _id(value: object, what: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise MonitoringError(f"{what} {safe_repr(value)} is malformed")


@dataclass(frozen=True, slots=True)
class Condition:
    metric: str
    operator: str
    values: tuple[str, ...]
    data_spec_id: str  # the feed (or filings feed) the rule reads
    description: str

    def __post_init__(self) -> None:
        if self.metric not in set(Metric):
            raise MonitoringError(f"unsupported metric {safe_repr(self.metric)}")
        if self.operator not in _OPS[Metric(self.metric)]:
            raise MonitoringError(f"operator {safe_repr(self.operator)} not allowed")
        if type(self.values) is not tuple or not all(
            isinstance(v, str) for v in self.values
        ):
            raise MonitoringError("values must be a tuple of strings")
        _id(self.data_spec_id, "data_spec_id")
        if not isinstance(self.description, str):
            raise MonitoringError("description must be a string")
        n = 2 if self.operator == "between" else 1
        if len(self.thresholds) != n or (
            n == 2 and self.thresholds[0] > self.thresholds[1]
        ):
            raise MonitoringError(f"{self.operator} needs {n} ordered thresholds")

    @property
    def thresholds(self) -> tuple[Decimal, ...]:
        try:
            return tuple(Quantity(v).value for v in self.values)
        except DecimalValueError as exc:
            raise MonitoringError(f"threshold is not a decimal: {exc}") from None

    @property
    def rule_id(self) -> str:
        """Content address: editing a condition makes a new rule."""
        body = [self.metric, self.operator, list(self.values), self.data_spec_id]
        return hashlib.sha256(json.dumps(body).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RuleKey:
    tenant_id: str
    watchlist_id: str
    instrument_id: InstrumentId
    rule_id: str  # Condition.rule_id, or "review_date"


class Outcome(StrEnum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    STALE_SOURCE = "stale_source"


@dataclass(frozen=True, slots=True)
class Evaluation:
    rule: RuleKey
    at: datetime
    outcome: Outcome
    trigger_id: str | None  # the trigger instance; set exactly when triggered
    reason: str  # stale reason, or the observation that decided the outcome

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_aware_utc(self.at))
        if not isinstance(self.outcome, Outcome):
            raise MonitoringError("outcome must be an Outcome")
        if (self.trigger_id is not None) != (self.outcome is Outcome.TRIGGERED):
            raise MonitoringError("a trigger instance exists only when triggered")


def _compare(op: str, x: Decimal, th: Sequence[Decimal]) -> bool:
    if op == "between":
        return th[0] <= x <= th[1]
    return {"gt": x > th[0], "gte": x >= th[0], "lt": x < th[0], "lte": x <= th[0]}[op]


def _freshness(
    at: datetime, observed: datetime, max_age: timedelta | None
) -> str | None:
    if max_age is None:
        return "freshness_unconfigured"  # unqualified thresholds block (feed policy)
    return "source_age_exceeded" if at - observed > max_age else None


def evaluate_market(
    rule: RuleKey,
    cond: Condition,
    recent: Sequence[Coverage],
    at: datetime,
    max_age: timedelta | None,
) -> Evaluation:
    """`recent`: the instrument's T022 coverage records as of `at`."""
    at = ensure_aware_utc(at)

    def stale(reason: str) -> Evaluation:
        return Evaluation(rule, at, Outcome.STALE_SOURCE, None, reason)

    metric = Metric(cond.metric)
    closed = sorted(
        (c for c in recent if c.instrument_id == rule.instrument_id),
        key=lambda c: c.end,
    )
    for r in closed:
        if r.end > at or (r.bar is not None and r.bar.knowledge_at > at):
            raise MonitoringError("look_ahead: input not known at evaluation time")
    if max_age is None:
        return stale("freshness_unconfigured")
    need = 1 if metric is Metric.VOLUME else 2
    if len(closed) < need:
        return stale("insufficient_history")
    use = closed[-need:]
    if need == 2 and use[0].end != use[1].start:
        return stale("non_contiguous")
    bars: list[Bar] = []
    for u in use:
        if u.state is not CoverageState.COVERED:
            return stale(f"coverage_{u.state.value}:{u.reason}")
        if u.bar is None:  # Coverage names no feed: silence is not attributable
            return stale("feed_unverifiable")
        if u.bar.feed_id != cond.data_spec_id:
            return stale("feed_mismatch")
        bars.append(u.bar)
    if (why := _freshness(at, use[-1].end, max_age)) is not None:
        return stale(why)
    last, th = use[-1], cond.thresholds
    first, bar = bars[0], bars[-1]
    if metric is Metric.VOLUME:
        x = bar.volume.value
        hit, seen = _compare(cond.operator, x, th), f"volume {x}"
    else:
        p, c = first.ohlc.close.value, bar.ohlc.close.value
        seen = f"close {p} -> {c}"
        if metric is Metric.PRICE_CROSS:
            above = cond.operator == "gt"
            hit = p <= th[0] < c if above else p >= th[0] > c
        else:  # (c - p) / p * 100 op t  <=>  (c - p) * 100 op t * p, as p > 0
            with localcontext(DOMAIN_CONTEXT):
                scaled = [t * p for t in th]
                hit = _compare(cond.operator, (c - p) * 100, scaled)
    if not hit:
        return Evaluation(rule, at, Outcome.NOT_TRIGGERED, None, seen)
    return Evaluation(rule, at, Outcome.TRIGGERED, last.start.isoformat(), seen)


def evaluate_review(
    rule: RuleKey, review_at: datetime | None, at: datetime
) -> Evaluation:
    at = ensure_aware_utc(at)
    if review_at is None or at < ensure_aware_utc(review_at):
        return Evaluation(rule, at, Outcome.NOT_TRIGGERED, None, "review_not_due")
    due = ensure_aware_utc(review_at).isoformat()
    return Evaluation(rule, at, Outcome.TRIGGERED, due, "review_date_reached")


class Decision(StrEnum):
    ALERTED = "alerted"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    SNOOZED = "snoozed"
    STALE_SOURCE = "stale_source"
    NOT_TRIGGERED = "not_triggered"


@dataclass(frozen=True, slots=True)
class Alert:
    rule: RuleKey
    trigger_id: str
    at: datetime
    reason: str


@dataclass(frozen=True, slots=True)
class Record:
    evaluation: Evaluation
    decision: Decision
    alert: Alert | None


@dataclass(frozen=True, slots=True)
class Snooze:
    rule: RuleKey
    at: datetime  # when requested
    until: datetime
    extended: bool  # replaced an active snooze


class AlertBook:
    def __init__(self, tenant_id: str, cooldown: timedelta) -> None:
        _id(tenant_id, "tenant")
        if type(cooldown) is not timedelta or cooldown < timedelta(0):
            raise MonitoringError("cooldown must be a non-negative timedelta")
        self.tenant_id, self.cooldown = tenant_id, cooldown
        self.log: list[Record] = []
        self.snoozes: list[Snooze] = []
        self._seen: set[tuple[RuleKey, str]] = set()
        self._last_alert: dict[RuleKey, datetime] = {}
        self._until: dict[RuleKey, datetime] = {}
        self._clock: datetime | None = None

    @property
    def alerts(self) -> tuple[Alert, ...]:
        return tuple(r.alert for r in self.log if r.alert is not None)

    def _tick(self, rule: RuleKey, at: datetime) -> datetime:
        if rule.tenant_id != self.tenant_id:
            raise MonitoringError("rule belongs to another tenant")
        at = ensure_aware_utc(at)
        if self._clock is not None and at < self._clock:
            raise MonitoringError("time_order: evaluations are offered in time order")
        self._clock = at
        return at

    def snoozed_until(self, rule: RuleKey) -> datetime | None:
        return self._until.get(rule)

    def snooze(
        self, rule: RuleKey, until: datetime, at: datetime, extend: bool = False
    ) -> None:
        at = self._tick(rule, at)
        until = ensure_aware_utc(until)
        if until <= at:
            raise MonitoringError("snooze must end in the future")
        active = rule in self._until and at < self._until[rule]
        if active and not extend:
            raise MonitoringError("snooze_active: changing it needs extend=True")
        self._until[rule] = until
        self.snoozes.append(Snooze(rule, at, until, active))

    def offer(self, ev: Evaluation) -> Record:
        at, rule = self._tick(ev.rule, ev.at), ev.rule
        alert = None
        if ev.outcome is Outcome.STALE_SOURCE:
            decision = Decision.STALE_SOURCE  # recorded, never alerted, not consumed
        elif ev.trigger_id is None:
            decision = Decision.NOT_TRIGGERED
        elif (rule, ev.trigger_id) in self._seen:
            decision = Decision.DUPLICATE
        else:
            self._seen.add((rule, ev.trigger_id))
            last = self._last_alert.get(rule)
            if rule in self._until and at < self._until[rule]:
                decision = Decision.SNOOZED
            elif last is not None and at < last + self.cooldown:
                decision = Decision.COOLDOWN
            else:
                decision = Decision.ALERTED
                alert = Alert(rule, ev.trigger_id, at, ev.reason)
                self._last_alert[rule] = at
        record = Record(ev, decision, alert)
        self.log.append(record)
        return record
