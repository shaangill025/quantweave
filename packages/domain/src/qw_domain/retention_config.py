"""Retention configuration (T058 increment 2; owner decision 9 of 2026-10-15:
"Retention defaults are minimal, with legal hold, held as configuration").

`defaults()` is the configured default: `retention.owner_defaults()` for the data
classes plus the worker purge windows (idempotency records 48 h, the owner's value;
outbox 7 days and inbox 30 days, the existing T025 windows). An operator overrides
them with a JSON document (`parse`). Validation fails closed, raising
`RetentionError`:
- unknown classes, windows or fields, duplicate keys, floats and malformed JSON;
- durations that are not a positive whole number of hours or days ("48h", "30d");
- a purge window below its floor (idempotency 24 h, A-05; outbox 1 day; inbox
  7 days, the redelivery bound of migration 0006);
- a rule that shortens a declared `Obligation` (counsel or provider terms): a
  shorter age expiry, any expiry when the obligation is indefinite, or deletion on
  request. The fixed rules of `retention.Rule` and `Policy` (owner decision 21 of
  2026-10-21) still apply: the financial journal is never deletable on request and
  never expires by age (`financial_append_only`, `financial_no_expiry`); the
  tombstone period (7 years) may only be lengthened (decision 30,
  `tombstone_floor`), only as `retain` and only above the backup period
  (`tombstone_outlives_backups`).
- `solo_hold_release` (owner decision 31) must be a JSON boolean (default false).
  It is the operator's explicit opt-in, held here because the operator holds this
  file, as decision 23 has the operator configure "solo maintainer". It does not
  enable anything by itself: `solo_release` takes the edition and the configured
  operators from the deployment record (never from this file) and refuses a hosted
  or multi-operator install (`solo_release_refused`); the caller builds
  `retention.Holds(solo=...)` from its result. Like purge holds, the flag is not
  yet a persisted, audited event; each solo release is recorded as `solo`.
A `PurgeHold` (an installation-wide legal hold on a purge window) suspends that
purge entirely: `purge_window` returns None and the worker skips it. Tenant-scoped
`retention.LegalHold`s cover the data classes; purge windows have no tenant scope.
LIMITATION: holds placed in this file are not persisted events; a persisted,
audited hold store comes with the T058 persistence increment. The operator places
a purge hold by writing it here (decision 22 lets the operator place one), but
removing it from the file is a release by one party: decision 22's two-party,
step-up release cannot be enforced for file-held purge holds until that store.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.release import Edition
from qw_domain.retention import (
    DEFAULT_POLICY_ID,
    OnRequest,
    Policy,
    RetentionClass,
    RetentionError,
    Rule,
    SoloRelease,
    owner_defaults,
)
from qw_domain.sources import ID_PATTERN


class Window(StrEnum):
    IDEMPOTENCY = "idempotency"  # HTTP idempotency records
    OUTBOX = "outbox"  # published outbox events
    INBOX = "inbox"  # processed inbox rows


WINDOW_FLOORS: Mapping[Window, timedelta] = MappingProxyType({
    Window.IDEMPOTENCY: timedelta(hours=24),
    Window.OUTBOX: timedelta(days=1),
    Window.INBOX: timedelta(days=7),
})  # fmt: skip
DEFAULT_WINDOWS: Mapping[Window, timedelta] = MappingProxyType({
    Window.IDEMPOTENCY: timedelta(hours=48),
    Window.OUTBOX: timedelta(days=7),
    Window.INBOX: timedelta(days=30),
})  # fmt: skip

_SPAN = re.compile(r"([1-9][0-9]{0,6})([hd])", re.ASCII)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_MAX_TEXT = 65_536


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise RetentionError(what, f"{safe_repr(value)} is malformed")
    return value


@dataclass(frozen=True, slots=True)
class Obligation:
    """A minimum retention set outside the product (counsel, provider terms).
    `min_retain` None: indefinite, so the class may not expire by age."""

    cls: RetentionClass
    min_retain: timedelta | None
    authority_id: str
    basis: str

    def __post_init__(self) -> None:
        if type(self.cls) is not RetentionClass:
            raise RetentionError("unknown_class", safe_repr(self.cls))
        span = self.min_retain
        if span is not None and (type(span) is not timedelta or span <= timedelta(0)):
            raise RetentionError("duration", "a positive timedelta")
        _id(self.authority_id, "authority")
        _id(self.basis, "authority")

    def check(self, rule: Rule) -> None:
        after, least = rule.expire_after, self.min_retain
        if rule.on_request is OnRequest.DELETE or (after is not None and (
                least is None or after < least)):  # fmt: skip
            raise RetentionError("obligation", f"{self.cls.value}: {self.basis}")


@dataclass(frozen=True, slots=True)
class PurgeHold:
    hold_id: str
    windows: frozenset[Window]
    reason: str  # a code, not free text
    authority_id: str

    def __post_init__(self) -> None:
        _id(self.hold_id, "hold_id")
        _id(self.authority_id, "authority")
        if not isinstance(self.reason, str) or _CODE.fullmatch(self.reason) is None:
            raise RetentionError("reason", "a reason code")
        if not self.windows or not all(type(w) is Window for w in self.windows):
            raise RetentionError("hold_scope", "name the windows held")
        object.__setattr__(self, "windows", frozenset(self.windows))


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    policy: Policy
    windows: Mapping[Window, timedelta]
    obligations: tuple[Obligation, ...] = ()
    purge_holds: tuple[PurgeHold, ...] = ()
    solo_hold_release: bool = False  # owner decision 31: the operator's opt-in

    def __post_init__(self) -> None:
        if type(self.policy) is not Policy:
            raise RetentionError("config", "a Policy")
        if type(self.solo_hold_release) is not bool:
            raise RetentionError("solo_hold_release", "a boolean")
        if set(self.windows) != set(Window):
            raise RetentionError("unknown_window", "every window, and only those")
        for w, span in self.windows.items():
            if type(span) is not timedelta or span < WINDOW_FLOORS[w]:
                raise RetentionError("window_floor", f"{w.value} >= {WINDOW_FLOORS[w]}")
        for o in self.obligations:
            if type(o) is not Obligation:
                raise RetentionError("config", "obligations must be Obligation")
            o.check(self.policy.rule(o.cls))
        ids = [h.hold_id for h in self.purge_holds if type(h) is PurgeHold]
        if len(ids) != len(self.purge_holds):
            raise RetentionError("config", "purge_holds must be PurgeHold")
        if len(set(ids)) != len(ids):
            raise RetentionError("duplicate_hold", "hold ids are unique")
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))
        object.__setattr__(self, "obligations", tuple(self.obligations))
        object.__setattr__(self, "purge_holds", tuple(self.purge_holds))

    def held(self) -> frozenset[Window]:
        return frozenset(w for h in self.purge_holds for w in h.windows)

    def purge_window(self, w: Window) -> timedelta | None:
        """The configured window, or None while a purge hold suspends it."""
        return None if w in self.held() else self.windows[w]

    def solo_release(self, edition: Edition,
                     operators: frozenset[str]) -> SoloRelease | None:  # fmt: skip
        """None unless opted in; then the deployment must be self-hosted with one
        configured operator, or this fails closed."""
        return SoloRelease(edition, operators) if self.solo_hold_release else None


def defaults() -> RetentionConfig:
    return RetentionConfig(owner_defaults(), DEFAULT_WINDOWS)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise RetentionError("duplicate_key", safe_repr(k))
        out[k] = v
    return out


def _no_number(text: str) -> object:
    raise RetentionError("config", f"no floats or constants: {safe_repr(text)}")


def _map(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RetentionError("config", "an object")
    return raw


def _obj(raw: object, allowed: set[str],
         required: frozenset[str] = frozenset()) -> dict[str, object]:  # fmt: skip
    raw = _map(raw)
    if unknown := set(raw) - allowed:
        raise RetentionError("unknown_field", safe_repr(sorted(unknown)))
    if missing := required - set(raw):
        raise RetentionError("missing_field", safe_repr(sorted(missing)))
    return raw


def _list(raw: object) -> list[object]:
    if not isinstance(raw, list):
        raise RetentionError("config", "a list")
    return raw


def _span(raw: object) -> timedelta:
    m = _SPAN.fullmatch(raw) if isinstance(raw, str) else None
    if m is None:
        raise RetentionError("duration", f"{safe_repr(raw)}: e.g. '48h' or '30d'")
    n = int(m.group(1))
    return timedelta(hours=n) if m.group(2) == "h" else timedelta(days=n)


def _opt_span(raw: object) -> timedelta | None:
    return None if raw is None else _span(raw)


def _enum[E: StrEnum](kind: type[E], raw: object, code: str) -> E:
    if isinstance(raw, str) and raw in kind._value2member_map_:
        return kind(raw)
    raise RetentionError(code, safe_repr(raw))


def _cls(raw: object) -> RetentionClass:
    return _enum(RetentionClass, raw, "unknown_class")


def _window(raw: object) -> Window:
    return _enum(Window, raw, "unknown_window")


def _rule(cls: RetentionClass, raw: object) -> Rule:
    e = _obj(raw, {"on_request", "expire_after", "authority_id", "basis"},
             frozenset({"on_request"}))  # fmt: skip
    on = _enum(OnRequest, e["on_request"], "on_request")
    auth, basis = e.get("authority_id"), e.get("basis")
    if auth is not None:
        _id(auth, "authority")
    if basis is not None:
        _id(basis, "authority")
    return Rule(cls, on, _opt_span(e.get("expire_after")),
                auth if isinstance(auth, str) else None,
                basis if isinstance(basis, str) else None)  # fmt: skip


def _obligation(raw: object) -> Obligation:
    keys = {"cls", "min_retain", "authority_id", "basis"}
    e = _obj(raw, keys, frozenset(keys))
    return Obligation(_cls(e["cls"]), _opt_span(e["min_retain"]),
                      _id(e["authority_id"], "authority"),
                      _id(e["basis"], "authority"))  # fmt: skip


def _hold(raw: object) -> PurgeHold:
    keys = {"hold_id", "windows", "reason", "authority_id"}
    e = _obj(raw, keys, frozenset(keys))
    reason = e["reason"]
    if not isinstance(reason, str):
        raise RetentionError("reason", "a reason code")
    return PurgeHold(_id(e["hold_id"], "hold_id"),
                     frozenset(_window(w) for w in _list(e["windows"])), reason,
                     _id(e["authority_id"], "authority"))  # fmt: skip


def parse(text: str) -> RetentionConfig:
    """An operator's JSON overrides on top of `defaults()`. Required: `policy_id`
    (not the defaults' id) and `version`; optional: `classes`, `windows`,
    `obligations`, `purge_holds`, `solo_hold_release`."""
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        raise RetentionError("config", f"a JSON text of at most {_MAX_TEXT} chars")
    try:
        raw = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_number,
                         parse_constant=_no_number)  # fmt: skip
    except (json.JSONDecodeError, RecursionError):
        raise RetentionError("config", "malformed JSON") from None
    top = _obj(raw, {"policy_id", "version", "classes", "windows", "obligations",
                     "purge_holds", "solo_hold_release"},
               frozenset({"policy_id", "version"}))  # fmt: skip
    if top["policy_id"] == DEFAULT_POLICY_ID:
        raise RetentionError("reserved_policy", "name the operator's own policy")
    base = defaults()
    rules = dict(base.policy.rules)
    for name, entry in _map(top.get("classes", {})).items():
        cls = _cls(name)
        rules[cls] = _rule(cls, entry)
    windows = dict(base.windows)
    for name, span in _map(top.get("windows", {})).items():
        windows[_window(name)] = _span(span)
    version = top["version"]
    if type(version) is not int:
        raise RetentionError("version", "a positive int")
    policy = Policy(_id(top["policy_id"], "policy_id"), version, rules)
    solo = top.get("solo_hold_release", False)
    if type(solo) is not bool:
        raise RetentionError("solo_hold_release", "a boolean")
    return RetentionConfig(
        policy, windows,
        tuple(_obligation(o) for o in _list(top.get("obligations", []))),
        tuple(_hold(h) for h in _list(top.get("purge_holds", []))),
        solo,
    )  # fmt: skip
