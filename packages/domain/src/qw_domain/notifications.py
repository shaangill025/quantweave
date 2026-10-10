"""Notifications: in-app feed, minimised external messages, delivery records and
current-state resolution (T056; R020, R047, R067, R069, R083, R085; spec §13
"Notifications", §2 Journey C, §7 "Timing and invalidation", ONB19).

- The in-app record is canonical and always kept (an email link must resolve);
  `in_app=False` only unlists non-safety items.
- External payloads carry fixed generic text and the link `/notifications/<id>`:
  no amounts, quantities, prices, instruments, accounts or object ids. The id is an
  HMAC under a server key, not a capability: reads are authorised by tenant/user.
- Opening resolves the object as of the open time; a closed, superseded or missing
  version is not actionable, and opening never changes the object.
- Delivery (queued, attempted, delivered = provider accepted, failed, unknown) never
  sets `seen_at`; only the owner's explicit open does. Only an explicit rejection
  is retried; an exception or unrecognised result may follow a send, so it is
  `unknown` (final). Each attempt re-checks consent, verification and push
  qualification; a refusal closes it (`failed`, `withdrawn_<reason>`).
- One record per (tenant, user, category, object, version): a repeat returns the
  original. External sends per (user, category, object) coalesce inside
  `dedupe_window`; quiet hours defer them. Safety categories bypass both (D1).
- Push needs a qualified subscription, email a verified single-line address.
In-memory; persistence and routes are remaining work. Stdlib only.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qw_domain.decimals import safe_repr
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import TRANSITIONS as JOB_TRANSITIONS
from qw_domain.jobs import JobState
from qw_domain.monitoring import Alert
from qw_domain.proposals import Current as ProposalCurrent
from qw_domain.proposals import ExecutionState, Proposal, ProposalState, resolve
from qw_domain.risk_pauses import Pause
from qw_domain.sources import ID_PATTERN


class NotificationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise NotificationError("malformed", f"{what} {safe_repr(value)}")
    return value


class Category(StrEnum):
    PROPOSAL = "proposal"
    MONITORING_ALERT = "monitoring_alert"
    RISK_PAUSE = "risk_pause"
    JOB_OUTCOME = "job_outcome"


SAFETY = frozenset({Category.RISK_PAUSE})  # always listed in-app


class Channel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    BROWSER_PUSH = "browser_push"


EXTERNAL = (Channel.EMAIL, Channel.BROWSER_PUSH)


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    ATTEMPTED = "attempted"
    DELIVERED = "delivered"  # the provider accepted it: not receipt, not reading
    FAILED = "failed"
    UNKNOWN = "unknown"


_TITLES = {
    Category.PROPOSAL: "A decision item changed",
    Category.MONITORING_ALERT: "A watchlist condition was met",
    Category.RISK_PAUSE: "A risk pause needs your attention",
    Category.JOB_OUTCOME: "A background task finished",
}
_BODY = "Open the app to see the current status. This message is not a confirmation."


@dataclass(frozen=True, slots=True)
class Signal:
    """A source event: what changed and when it was triggered (never reset)."""

    tenant_id: str
    category: Category
    object_id: str
    version: str  # the object's state version at trigger time
    trigger_at: datetime

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant_id")
        _id(self.object_id, "object_id")
        if not isinstance(self.version, str) or not 0 < len(self.version) <= 160:
            raise NotificationError("malformed", "version")
        object.__setattr__(self, "trigger_at", ensure_aware_utc(self.trigger_at))


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:32]


def _bool(obj: object, *names: str) -> None:
    for name in names:
        if type(getattr(obj, name)) is not bool:
            raise NotificationError("malformed", f"{name} must be a bool")


def from_proposal(p: Proposal) -> Signal:
    v, last = p.latest, p.events[-1]
    version = f"{v.version}:{p.state(v.version)}"
    return Signal(
        v.action.tenant_id, Category.PROPOSAL, v.proposal_id, version, last.at
    )


def from_alert(a: Alert) -> Signal:
    r = a.rule
    obj = "rule-" + _digest(r.watchlist_id, str(r.instrument_id.uuid), r.rule_id)
    return Signal(r.tenant_id, Category.MONITORING_ALERT, obj, a.trigger_id, a.at)


def from_pause(p: Pause, tenant_id: str) -> Signal:
    return Signal(tenant_id, Category.RISK_PAUSE, p.pause_id, "paused", p.triggered_at)


def from_job(tenant_id: str, job_id: str, state: JobState, at: datetime) -> Signal:
    """A finished job: a terminal state, or dead_letter (waits for an operator)."""
    if JOB_TRANSITIONS[state] and state is not JobState.DEAD_LETTER:
        raise NotificationError("job_not_terminal", state)
    return Signal(tenant_id, Category.JOB_OUTCOME, job_id, state.value, at)


_WINDOW = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])")


@dataclass(frozen=True, slots=True)
class Preferences:
    """Contract `NotificationPreferences` plus per-(category, channel) mutes.
    External channels are opt-in. Detail exposure is not offered yet."""

    in_app: bool = True
    email: bool = False
    browser_push: bool = False
    quiet_hours_timezone: str = "UTC"
    quiet_hours: tuple[str, ...] = ()  # local "HH:MM-HH:MM", end exclusive
    redact_financial_values: bool = True
    muted: frozenset[tuple[Category, Channel]] = frozenset()

    def __post_init__(self) -> None:
        _bool(self, "in_app", "email", "browser_push", "redact_financial_values")
        if type(self.muted) is not frozenset or not all(
            type(m) is tuple and len(m) == 2 and type(m[0]) is Category
            and type(m[1]) is Channel for m in self.muted
        ):  # fmt: skip
            raise NotificationError("malformed", "muted (Category, Channel) pairs")
        if self.redact_financial_values is not True:
            raise NotificationError("detail_exposure_unsupported")
        if any((c, Channel.IN_APP) in self.muted for c in SAFETY):
            raise NotificationError("safety_in_app", "cannot be muted")
        try:
            ZoneInfo(self.quiet_hours_timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise NotificationError("quiet_hours", "unknown timezone") from None
        if type(self.quiet_hours) is not tuple:
            raise NotificationError("quiet_hours", "a tuple of windows")
        quiet: set[int] = set()  # minutes of the local day
        for w in self.quiet_hours:
            m = _WINDOW.fullmatch(w) if isinstance(w, str) else None
            if m is None or m[1] + m[2] == m[3] + m[4]:
                raise NotificationError("quiet_hours", safe_repr(w))
            a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
            quiet |= set(range(a, b) if a < b else [*range(a, 1440), *range(b)])
        if len(quiet) == 1440:
            raise NotificationError("quiet_hours", "covers the whole day")

    def enabled(self, channel: Channel) -> bool:
        return {Channel.IN_APP: self.in_app, Channel.EMAIL: self.email,
                Channel.BROWSER_PUSH: self.browser_push}[channel]  # fmt: skip

    def _window_end(self, at: datetime) -> datetime | None:
        tz = ZoneInfo(self.quiet_hours_timezone)
        local = at.astimezone(tz)
        now, day = local.time().replace(tzinfo=None), local.date()
        ends: list[datetime] = []
        for w in self.quiet_hours:
            a, b = (time(int(x[:2]), int(x[3:])) for x in w.split("-"))
            if a < b and a <= now < b:
                ends.append(datetime.combine(day, b, tz))
            elif a > b and (now >= a or now < b):
                end_day = day + timedelta(days=1) if now >= a else day
                ends.append(datetime.combine(end_day, b, tz))
        return ensure_aware_utc(max(ends)) if ends else None

    def quiet_until(self, at: datetime) -> datetime | None:
        """End of the quiet period containing `at` (UTC), following windows that
        start where another ends; None outside quiet hours."""
        end, nxt = None, self._window_end(ensure_aware_utc(at))
        for _ in range(len(self.quiet_hours) + 1):
            if nxt is None or (end is not None and nxt <= end):
                break
            end, nxt = nxt, self._window_end(nxt)
        return end


_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[!-~]*)?", re.ASCII)
# Conservative single-line address: no whitespace, controls, quotes or separators.
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+"
)


@dataclass(frozen=True, slots=True)
class PushSubscription:
    endpoint_origin: str
    permission: str  # browser permission: granted, denied or default
    environment_supports: bool
    expires_at: datetime | None
    revoked: bool

    def __post_init__(self) -> None:
        url = self.endpoint_origin
        if not isinstance(url, str) or len(url) > 2048 or not _URL.fullmatch(url):
            raise NotificationError("malformed", "endpoint: a single-line URL")
        if self.permission not in ("granted", "denied", "default"):
            raise NotificationError("malformed", "permission")
        _bool(self, "environment_supports", "revoked")
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", ensure_aware_utc(self.expires_at))


def push_qualification(sub: PushSubscription | None, at: datetime) -> tuple[str, ...]:
    """Why browser push cannot be used now; empty when qualified."""
    if sub is None:
        return ("no_subscription",)
    checks = {
        "permission_not_granted": sub.permission != "granted",
        "unsupported_environment": not sub.environment_supports,
        "endpoint_not_https": not sub.endpoint_origin.startswith("https://"),
        "subscription_expired": sub.expires_at is not None
        and ensure_aware_utc(at) >= sub.expires_at,
        "revoked": sub.revoked,
    }
    return tuple(reason for reason, failed in checks.items() if failed)


@dataclass(frozen=True, slots=True)
class Recipient:
    tenant_id: str
    user_id: str
    preferences: Preferences
    email_address: str | None
    email_verified: bool
    push: PushSubscription | None

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant_id")
        _id(self.user_id, "user_id")
        _bool(self, "email_verified")
        if type(self.preferences) is not Preferences:
            raise NotificationError("malformed", "preferences")
        if not (self.push is None or type(self.push) is PushSubscription):
            raise NotificationError("malformed", "push")
        a = self.email_address
        if a is not None and (
            not isinstance(a, str) or len(a) > 254 or not _EMAIL.fullmatch(a)
        ):
            raise NotificationError("malformed", "email_address")


@dataclass(frozen=True, slots=True)
class Subject:
    category: Category
    object_id: str
    version: str


@dataclass(frozen=True, slots=True)
class Notification:
    id: str  # opaque
    tenant_id: str
    user_id: str
    subject: Subject
    trigger_at: datetime  # source event time, never reset
    created_at: datetime  # generated
    listed: bool  # shown in the in-app feed
    seen_at: datetime | None = None  # explicit open only


@dataclass(frozen=True, slots=True)
class ExternalMessage:
    notification_id: str
    channel: Channel
    title: str
    body: str
    link: str


def render(n: Notification, channel: Channel) -> ExternalMessage:
    """The minimised external payload: generic text and an opaque link only."""
    if channel is Channel.IN_APP:
        raise NotificationError("in_app_not_external")
    title = _TITLES[n.subject.category]
    return ExternalMessage(n.id, channel, title, _BODY, f"/notifications/{n.id}")


class SendOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


_OUTCOMES: dict[object, tuple[DeliveryStatus, str]] = {
    SendOutcome.ACCEPTED: (DeliveryStatus.DELIVERED, ""),
    SendOutcome.REJECTED: (DeliveryStatus.FAILED, "rejected"),  # the only retry
    SendOutcome.UNKNOWN: (DeliveryStatus.UNKNOWN, "unknown"),
}
_UNRECOGNISED = (DeliveryStatus.UNKNOWN, "unrecognised_result")


class Transport(Protocol):
    """Provider-neutral sender (an SMTP adapter, a self-hosted push sender)."""

    def send(self, message: ExternalMessage, recipient: Recipient) -> SendOutcome: ...


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    status: DeliveryStatus
    at: datetime
    code: str = ""


@dataclass(frozen=True, slots=True)
class Delivery:
    notification_id: str
    channel: Channel
    not_before: datetime
    events: tuple[DeliveryEvent, ...]

    @property
    def status(self) -> DeliveryStatus:
        return self.events[-1].status

    @property
    def attempts(self) -> int:
        return sum(e.status is DeliveryStatus.ATTEMPTED for e in self.events)

    @property
    def accepted_at(self) -> datetime | None:
        return next(
            (e.at for e in self.events if e.status is DeliveryStatus.DELIVERED), None
        )


@dataclass(frozen=True, slots=True)
class CurrentView:
    """The referenced object's state as of the time it was resolved."""

    state: str
    actionable: bool
    current_version: str


# Called with the notification and the read time; returns None when not found.
Resolver = Callable[["Notification", datetime], CurrentView | None]


def proposal_resolver(p: Proposal, cur: ProposalCurrent) -> Resolver:
    """Resolve a proposal subject ("<version>:<state>") against the record as of the
    read time (`cur` with `as_of` replaced by it): a superseded version shows
    `superseded`. Only the notified, active, unplanned latest version is
    actionable. Tenant, category and proposal id must match."""

    def view(note: "Notification", at: datetime) -> CurrentView:
        s, a = note.subject, p.latest.action
        if (note.tenant_id, s.category, s.object_id) != (
            a.tenant_id, Category.PROPOSAL, p.latest.proposal_id,
        ):  # fmt: skip
            raise NotificationError("subject_mismatch")
        head, latest = s.version.split(":")[0], p.latest.version
        n = int(head) if head.isascii() and head.isdigit() else 0
        if not 1 <= n <= latest:
            raise NotificationError("subject_mismatch", "unknown version")
        now = resolve(p, replace(cur, as_of=at))
        current = f"{latest}:{now}"
        if n != latest:
            return CurrentView(p.state(n), False, current)
        planned = p.execution(latest) is not ExecutionState.NONE
        return CurrentView(now, now is ProposalState.ACTIVE and not planned, current)

    return view


@dataclass(frozen=True, slots=True)
class Submitted:
    notification: Notification
    created: bool
    deliveries: tuple[Delivery, ...]
    reasons: tuple[str, ...]  # channel:reason for every channel not used


@dataclass(frozen=True, slots=True)
class Opened:
    notification: Notification
    view: CurrentView
    event_age: timedelta  # open time minus trigger time

    @property
    def changed(self) -> bool:
        return self.view.current_version != self.notification.subject.version


@dataclass(frozen=True, slots=True)
class FeedEntry:
    id: str
    category: Category
    state: str
    actionable: bool
    seen: bool


_UNAVAILABLE = CurrentView("unavailable", False, "")


@dataclass
class Center:
    """One tenant's notification records, deliveries and decision reasons."""

    tenant_id: str
    dedupe_window: timedelta
    max_attempts: int
    id_key: bytes = field(repr=False)  # server secret for record ids (>= 16 bytes)
    _items: dict[str, Notification] = field(default_factory=dict)
    _deliveries: dict[tuple[str, Channel], Delivery] = field(default_factory=dict)
    _last_external: dict[tuple[str, Category, str], datetime] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant_id")
        if self.dedupe_window < timedelta(0) or self.max_attempts < 1:
            raise NotificationError("config", "window >= 0 and max_attempts >= 1")
        if type(self.id_key) is not bytes or len(self.id_key) < 16:
            raise NotificationError("config", "id_key: at least 16 bytes")

    def submit(self, s: Signal, r: Recipient, at: datetime) -> Submitted:
        """Decide everything first, then store the record and its deliveries."""
        at = ensure_aware_utc(at)
        if s.tenant_id != self.tenant_id or r.tenant_id != self.tenant_id:
            raise NotificationError("tenant", "signal or recipient of another tenant")
        if at < s.trigger_at:
            raise NotificationError("time_order", "generated before its trigger")
        user, cat = r.user_id, s.category
        body = json.dumps([self.tenant_id, user, cat, s.object_id, s.version])
        mac = hmac.new(self.id_key, body.encode(), hashlib.sha256).hexdigest()
        nid = "ntf-" + mac[:32]
        if (old := self._items.get(nid)) is not None:
            return Submitted(old, False, (), ())
        prefs, reasons = r.preferences, list[str]()
        listed = cat in SAFETY or (
            prefs.in_app and (cat, Channel.IN_APP) not in prefs.muted
        )
        if not listed:
            reasons.append("in_app:off")
        subject = Subject(cat, s.object_id, s.version)
        n = Notification(nid, self.tenant_id, user, subject, s.trigger_at, at, listed)
        key = (user, cat, s.object_id)
        last = None if cat in SAFETY else self._last_external.get(key)
        start = at if cat in SAFETY else prefs.quiet_until(at) or at
        out = []
        for ch in EXTERNAL:
            why = _refusal(ch, cat, r, at)
            if not why and last is not None and at < last + self.dedupe_window:
                why = "coalesced"
            if why:
                reasons.append(f"{ch}:{why}")
                continue
            out.append(
                Delivery(nid, ch, start, (DeliveryEvent(DeliveryStatus.QUEUED, at),))
            )
        self._items[nid] = n
        for d in out:
            self._deliveries[(nid, d.channel)] = d
            self._last_external[key] = at
        return Submitted(n, True, tuple(out), tuple(reasons))

    def attempt(
        self, nid: str, ch: Channel, tx: Transport, r: Recipient, at: datetime
    ) -> Delivery:
        """One send attempt. Consent, verification and push qualification are
        re-checked at `at`; a refusal closes the delivery without sending. Only an
        explicit rejection is retryable; an exception or an unrecognised result may
        follow a send, so it is `unknown` (final). Errors are codes, never text."""
        at = ensure_aware_utc(at)
        d, n = self._deliveries.get((nid, ch)), self._items.get(nid)
        if d is None or n is None or (n.tenant_id, n.user_id) != (
            r.tenant_id, r.user_id
        ):  # fmt: skip
            raise NotificationError("no_delivery")
        if at < d.events[-1].at:
            raise NotificationError("time_order", "attempt times never go back")
        if d.status not in (DeliveryStatus.QUEUED, DeliveryStatus.FAILED) or (
            d.events[-1].code.startswith("withdrawn_")
        ):
            raise NotificationError("not_retryable", d.status)
        if d.attempts >= self.max_attempts:
            raise NotificationError("attempts_exhausted")
        if at < d.not_before:
            raise NotificationError("quiet_hours", "deferred until the window ends")
        if why := _refusal(ch, n.subject.category, r, at):
            event = DeliveryEvent(DeliveryStatus.FAILED, at, f"withdrawn_{why}")
            d = self._deliveries[(nid, ch)] = replace(d, events=(*d.events, event))
            return d
        events = [*d.events, DeliveryEvent(DeliveryStatus.ATTEMPTED, at)]
        try:
            outcome: object = tx.send(render(n, ch), r)
            end, code = _OUTCOMES.get(outcome, _UNRECOGNISED)
        except Exception:
            end, code = DeliveryStatus.UNKNOWN, "transport_error"
        d = replace(d, events=(*events, DeliveryEvent(end, at, code)))
        self._deliveries[(nid, ch)] = d
        return d

    def get(self, user_id: str, nid: str) -> Notification:
        n = self._items.get(nid)
        if n is None or n.user_id != user_id:
            raise NotificationError("not_found")
        return n

    def deliveries(self, nid: str) -> tuple[Delivery, ...]:
        return tuple(d for (i, _), d in self._deliveries.items() if i == nid)

    def open(self, user_id: str, nid: str, resolver: Resolver, at: datetime) -> Opened:
        """Explicit open by the owner: resolve state as of `at`, record first seen.
        Resolver errors propagate and record nothing."""
        at, n = ensure_aware_utc(at), self.get(user_id, nid)
        if at < n.created_at:
            raise NotificationError("time_order", "opened before it was generated")
        view = resolver(n, at) or _UNAVAILABLE
        if n.seen_at is None:
            n = self._items[nid] = replace(n, seen_at=at)
        return Opened(n, view, at - n.trigger_at)

    def feed(
        self, user_id: str, resolver: Resolver, at: datetime
    ) -> tuple[FeedEntry, ...]:
        """The user's listed items, newest first, with state as of `at`. Not a read.
        An item whose resolution fails is listed as `unavailable`."""
        at = ensure_aware_utc(at)
        mine = [n for n in self._items.values() if n.user_id == user_id and n.listed]
        out = []
        for n in sorted(mine, key=lambda n: n.created_at, reverse=True):
            try:
                v = resolver(n, at) or _UNAVAILABLE
            except Exception:
                v = _UNAVAILABLE
            seen = n.seen_at is not None
            out.append(FeedEntry(n.id, n.subject.category, v.state, v.actionable, seen))
        return tuple(out)


def _refusal(ch: Channel, cat: Category, r: Recipient, at: datetime) -> str:
    """Why `ch` may not be used for `r` now (consent, verification, push), or ""."""
    if not r.preferences.enabled(ch):
        return "off"
    if (cat, ch) in r.preferences.muted:
        return "muted"
    if ch is Channel.EMAIL and not (r.email_verified and r.email_address):
        return "unverified"
    if ch is Channel.BROWSER_PUSH and (why := push_qualification(r.push, at)):
        return why[0]
    return ""
