"""Retention classes, legal holds, deletion jobs, backup expiry and restore-time
tombstones (T058 increment 1; spec §4 "Corrections and retention", §12 "Privacy and
lifecycle", §15 "Recovery and rollback"; data dictionary `deletion_tombstone`; R031,
R052, R075, R077, R086, R087, R088). Domain, stdlib only, in memory.
- Classes are the data dictionary `retention_class` values plus the spec §12 classes
  it lacks (provider payloads, logs, backups). A `Policy` gives each class a `Rule`;
  a class without one is unconfigured. The spec sets no period or obligation for
  any class (counsel and provider terms do), so `spec_baseline` leaves every class
  unconfigured and nothing is deleted or expired under it: fail closed, and each gap
  is reported as a reason code. Configured rules name an authority and a basis.
- `owner_defaults` (increment 2, owner decision 9 of 2026-10-15: minimal defaults,
  legal hold, held as configuration) gives every class a rule: backups 30 days,
  audit events (ids only) 7 years; the implementer's minimal choices, confirmed by
  owner decision 20 of 2026-10-21 (authority `owner-decision-20`), with `reference`
  and `release` (retained, no age expiry) confirmed by decision 34; and decision
  21: the financial journal retained, tombstones 7 years. All stay adjustable
  configuration (`retention_config`) within the fixed rules below; counsel and
  provider terms may add obligations (spec §12). `Retention` applies it unless a
  policy is passed.
- Fixed rules, whatever the policy (decision 21): the financial journal is
  append-only, never deleted on request and never expired by age until the owner
  sets its period; the tombstone ledger (`privacy`) is retained, never deleted on
  request, and its period must exceed the backup period, so a tombstone outlives
  every backup copy it guards. Decision 30: the period may be lengthened but never
  set below `TOMBSTONE_RETENTION` (2557 days, `tombstone_floor`). No job compacts
  the ledger yet: an expired tombstone is still kept (`tombstone_ledger`).
- `plan` is a pure function of the trigger (a `DeletionRequest` or an `ExpirySweep`),
  the items, the policy and the holds as of the trigger time. Free text (the
  acknowledgement) never affects it. `Retention.submit` stores one plan per job id:
  a retry returns it unchanged, so trigger times are never reset.
- Legal holds (decision 22): the tenant owner or the installation operator places
  one; a release needs both, as two distinct principals, each with its own step-up
  receipt never used before (`Approval`). The caller's auth layer verifies that a
  principal holds the role and that the receipt is valid; the domain enforces the
  combination. One person holding both roles cannot release a hold alone, unless
  (decision 31) the store was built with a `SoloRelease`: only a self-hosted install
  with exactly one configured operator can make one. A release by that operator
  holding both roles then needs two distinct, unused step-up receipts at least 24
  hours apart, both after the hold was placed and not after the release, and is
  recorded `solo`. The step-up times are asserted by the caller, like the receipt
  ids: the auth layer must check each against its receipt; the domain checks order
  and spacing only.
- A delete intent under a legal hold is planned as `held`, not dropped. `execute`
  re-checks holds and the current policy (the stricter of plan and current wins) at
  its own time; a suspended intent stays pending until a later run deletes it. An
  item already tombstoned keeps its first tombstone. Tombstones carry a pseudonym (an
  unkeyed hash of tenant, class and record id; ids are case-sensitive, as in the
  stores; guessable ids make it a pseudonym, not confidentiality), the residual
  backups that may still hold a copy and their expiry.
- The ledger is a hash chain, append-only in time. The chain detects accidental
  edits; authenticity depends on the `LedgerCheckpoint` (length and head) kept
  outside the backup failure domain. `restore` requires that anchor, refuses a
  truncated or mismatched ledger and one that does not extend the snapshot's copy,
  and drops every tombstoned item before anything is served.
- `reproducibility` marks derived items whose inputs were deleted (transitively) as
  not reproducible, and unknown inputs as `lineage_missing` (T052 approach, not code).
LIMITATIONS: no persistence, routes, real backups or store-specific deletion; the
caller deletes rows from each store; the anchor is unkeyed (a keyed or signed anchor
waits for a reviewed key scheme: an owner decision).
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.release import Edition
from qw_domain.sources import ID_PATTERN

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
GENESIS = "0" * 64

OWNER_DECISIONS = (  # still open after decisions 9 (2026-10-15), 20-22, 30, 31, 34
    "the financial journal's own retention period (decision 21: retained, "
    "append-only; only a period the owner sets later may expire it)",
    "mapping of spec §12 class names onto the data dictionary classes",
    "a keyed HMAC for record hashes and a keyed or signed ledger anchor",
)


class RetentionError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise RetentionError(what, f"{safe_repr(value)} is malformed")
    return value


def _utc(value: object, what: str = "time") -> datetime:
    try:
        return ensure_aware_utc(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise RetentionError(what, "an aware datetime") from None


def _sha(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class RetentionClass(StrEnum):
    SECURITY = "security"
    REFERENCE = "reference"
    FINANCIAL = "financial"
    POLICY = "policy"
    AUDIT = "audit"
    RESEARCH = "research"  # includes recommendation history (proposal_version)
    MARKET = "market"
    EVIDENCE = "evidence"  # licensed evidence
    RELEASE = "release"
    IMPROVEMENT = "improvement"  # experiments
    SIMULATION = "simulation"
    OPERATIONS = "operations"
    PRIVACY = "privacy"  # deletion tombstones, migration exports
    PROVIDER_PAYLOAD = "provider_payload"  # spec §12 transient provider payloads
    LOG = "log"
    BACKUP = "backup"


class OnRequest(StrEnum):
    UNCONFIGURED = "unconfigured"
    DELETE = "delete"
    RETAIN = "retain"  # a configured legal/operational obligation


@dataclass(frozen=True, slots=True)
class Rule:
    cls: RetentionClass
    on_request: OnRequest = OnRequest.UNCONFIGURED
    expire_after: timedelta | None = None  # None: no expiry configured, not "forever"
    authority_id: str | None = None
    basis: str | None = None  # reference to the counsel/provider-terms basis

    def __post_init__(self) -> None:
        if type(self.cls) is not RetentionClass:
            raise TypeError("cls must be RetentionClass")
        if type(self.on_request) is not OnRequest:
            raise TypeError("on_request must be OnRequest")
        configured = (self.on_request is not OnRequest.UNCONFIGURED
                      or self.expire_after is not None)  # fmt: skip
        if (self.cls is RetentionClass.PRIVACY and configured
                and self.on_request is not OnRequest.RETAIN):  # fmt: skip
            raise RetentionError("tombstone_ledger_protected", "owner decision 21")
        if (self.cls is RetentionClass.FINANCIAL
                and self.on_request is OnRequest.DELETE):  # fmt: skip
            raise RetentionError("financial_append_only", "owner decision 21")
        if self.cls is RetentionClass.FINANCIAL and self.expire_after is not None:
            raise RetentionError("financial_no_expiry", "explicit workflow only")
        span = self.expire_after
        if span is not None and (type(span) is not timedelta or span <= timedelta(0)):
            raise RetentionError("period", "a positive timedelta")
        if configured:
            if self.authority_id is None or self.basis is None:
                raise RetentionError("authority", "a configured rule names both")
            _id(self.authority_id, "authority")
            _id(self.basis, "authority")


@dataclass(frozen=True, slots=True)
class Policy:
    policy_id: str
    version: int
    rules: Mapping[RetentionClass, Rule]

    def __post_init__(self) -> None:
        _id(self.policy_id, "policy_id")
        if type(self.version) is not int or self.version < 1:
            raise RetentionError("version", "a positive int")
        for cls, r in self.rules.items():
            if type(r) is not Rule or r.cls is not cls:
                raise RetentionError("rule_class", safe_repr(cls))
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        tomb = self.rule(RetentionClass.PRIVACY).expire_after
        kept = self.rule(RetentionClass.BACKUP).expire_after
        if tomb is not None and tomb < TOMBSTONE_RETENTION:
            raise RetentionError("tombstone_floor", "owner decision 30: >= 2557 days")
        if tomb is not None and (kept is None or tomb <= kept):
            raise RetentionError("tombstone_outlives_backups", "owner decision 21")

    def rule(self, cls: RetentionClass) -> Rule:
        return self.rules.get(cls) or Rule(cls)


def spec_baseline(policy_id: str) -> Policy:
    """Every class unconfigured: the spec sets no retention period (§12)."""
    return Policy(policy_id, 1, {c: Rule(c) for c in RetentionClass})


DEFAULT_POLICY_ID = "retention-defaults"
AUDIT_RETENTION = timedelta(days=7 * 365 + 2)  # 7 years: at most 2 leap days in 7
BACKUP_RETENTION = timedelta(days=30)  # a deleted record's backup copy: 30 days
TOMBSTONE_RETENTION = AUDIT_RETENTION  # owner decision 21: 7 years, as audit
_OWNER = "owner-decision-9"
_BASIS = "owner-decisions-2026-10-15-9"
_D20 = ("owner-decision-20", "owner-decisions-2026-10-21-20")
_D21 = ("owner-decision-21", "owner-decisions-2026-10-21-21")
_D34 = ("owner-decision-34", "owner-decisions-2026-10-21-34")
_D, _R = OnRequest.DELETE, OnRequest.RETAIN
# (on request, age expiry) for the classes the owner gave no number for: the
# minimal choice that invents no legal period. Deletable classes drop personal
# content on request; no age expiry where counsel, rights or provider terms decide.
_IMPLEMENTER: Mapping[RetentionClass, tuple[OnRequest, timedelta | None]] = {
    RetentionClass.SECURITY: (_D, None),
    RetentionClass.REFERENCE: (_R, None),  # shared reference data, not personal
    RetentionClass.FINANCIAL: (_R, None),  # append-only journal (decision 21)
    RetentionClass.POLICY: (_D, None),
    RetentionClass.RESEARCH: (_D, None),
    RetentionClass.MARKET: (_D, None),  # age limits come from rights/provider terms
    RetentionClass.EVIDENCE: (_D, None),  # likewise
    RetentionClass.RELEASE: (_R, None),  # release and promotion evidence (§15)
    RetentionClass.IMPROVEMENT: (_D, None),
    RetentionClass.SIMULATION: (_D, None),
    RetentionClass.OPERATIONS: (_D, None),  # purge windows: retention_config
    RetentionClass.PROVIDER_PAYLOAD: (_D, timedelta(hours=48)),  # transient (§12)
    RetentionClass.LOG: (_D, BACKUP_RETENTION),
}
_BY_34 = frozenset({RetentionClass.REFERENCE, RetentionClass.RELEASE})


def _source(c: RetentionClass) -> tuple[str, str]:
    if c in _BY_34:  # not named in decision 20's question; confirmed by 34
        return _D34
    return _D21 if c is RetentionClass.FINANCIAL else _D20


def owner_defaults() -> Policy:
    """Owner decisions 9, 20 and 21: minimal defaults; tombstones kept 7 years."""
    rules = {c: Rule(c, on, after, *_source(c))
             for c, (on, after) in _IMPLEMENTER.items()}  # fmt: skip
    rules[RetentionClass.AUDIT] = Rule(RetentionClass.AUDIT, _R, AUDIT_RETENTION,
                                       _OWNER, _BASIS)  # fmt: skip
    rules[RetentionClass.BACKUP] = Rule(RetentionClass.BACKUP, _R, BACKUP_RETENTION,
                                        _OWNER, _BASIS)  # fmt: skip
    rules[RetentionClass.PRIVACY] = Rule(RetentionClass.PRIVACY, _R,
                                         TOMBSTONE_RETENTION, *_D21)  # fmt: skip
    return Policy(DEFAULT_POLICY_ID, 1, rules)


def record_hash(tenant_id: str, cls: RetentionClass, record_id: str) -> str:
    return _sha(["qw-record-1", tenant_id, cls.value, record_id])


@dataclass(frozen=True, slots=True)
class Item:
    tenant_id: str
    record_id: str
    cls: RetentionClass
    created_at: datetime

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant_id")
        _id(self.record_id, "record_id")
        if type(self.cls) is not RetentionClass:
            raise TypeError("cls must be RetentionClass")
        object.__setattr__(self, "created_at", _utc(self.created_at))

    @property
    def record_hash(self) -> str:
        return record_hash(self.tenant_id, self.cls, self.record_id)


class HoldRole(StrEnum):  # owner decision 22: who may place and release holds
    TENANT_OWNER = "tenant_owner"
    OPERATOR = "operator"  # the installation operator


@dataclass(frozen=True, slots=True)
class Approval:
    """One party to a hold release: a role, its principal and a step-up receipt.
    `stepped_up_at` (caller-asserted, from the receipt) is required for a solo
    release only."""

    role: HoldRole
    principal_id: str
    step_up_receipt_id: str
    stepped_up_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.role) is not HoldRole:
            raise RetentionError("hold_role", safe_repr(self.role))
        _id(self.principal_id, "principal_id")
        _id(self.step_up_receipt_id, "step_up")
        if self.stepped_up_at is not None:
            object.__setattr__(self, "stepped_up_at",
                               _utc(self.stepped_up_at, "step_up_time"))  # fmt: skip


SOLO_DELAY = timedelta(hours=24)  # owner decision 31


@dataclass(frozen=True, slots=True)
class SoloRelease:
    """Owner decision 31: the explicit opt-in to solo hold release. `edition` and
    `operators` are the deployment's facts; only a self-hosted install with exactly
    one configured operator can enable it."""

    edition: Edition
    operators: frozenset[str]

    def __post_init__(self) -> None:
        ops = self.operators
        if (self.edition is not Edition.SELF_HOSTED
                or not isinstance(ops, frozenset) or len(ops) != 1):  # fmt: skip
            raise RetentionError("solo_release_refused", "self-hosted, one operator")
        _id(next(iter(ops)), "operator_id")

    @property
    def operator_id(self) -> str:
        return next(iter(self.operators))


@dataclass(frozen=True, slots=True)
class LegalHold:
    hold_id: str
    tenant_id: str
    classes: frozenset[RetentionClass]  # explicit scope, never implicitly everything
    record_ids: frozenset[str]  # empty: every record of those classes
    reason: str  # a code, not free text
    authority_id: str
    placed_at: datetime
    placed_by: HoldRole  # either role may place a hold; no step-up needed
    principal_id: str

    def covers(self, it: Item) -> bool:
        ids = self.record_ids
        return (it.tenant_id == self.tenant_id and it.cls in self.classes
                and (not ids or it.record_id in ids))  # fmt: skip


@dataclass(frozen=True, slots=True)
class HoldRelease:
    hold_id: str
    tenant_id: str
    authority_id: str
    reason: str
    released_at: datetime
    approvals: tuple[Approval, ...]  # the tenant owner and the operator
    solo: bool = False  # decision 31: one person held both roles; must match


def _event(tenant: str, auth: str, reason: str, at: datetime) -> datetime:
    _id(tenant, "tenant_id")
    _id(auth, "authority_id")
    if not isinstance(reason, str) or _CODE.fullmatch(reason) is None:
        raise RetentionError("reason", "a reason code")
    return _utc(at)


@dataclass
class Holds:
    """Append-only hold events; a release is recorded, never a removal."""

    _events: list[LegalHold | HoldRelease] = field(default_factory=list)
    solo: SoloRelease | None = None  # decision 31: None, two people release

    def __post_init__(self) -> None:
        if self.solo is not None and type(self.solo) is not SoloRelease:
            raise RetentionError("solo_release_refused", "a SoloRelease or None")

    def _find(self, tenant_id: str, hold_id: str) -> LegalHold | None:
        key = (tenant_id, hold_id)
        return next((e for e in self._events if isinstance(e, LegalHold)
                     and (e.tenant_id, e.hold_id) == key), None)  # fmt: skip

    def _released(self, h: LegalHold) -> HoldRelease | None:
        key = (h.tenant_id, h.hold_id)
        return next((e for e in self._events if isinstance(e, HoldRelease)
                     and (e.tenant_id, e.hold_id) == key), None)  # fmt: skip

    def place(self, h: LegalHold) -> LegalHold:
        _id(h.hold_id, "hold_id")
        at = _event(h.tenant_id, h.authority_id, h.reason, h.placed_at)
        if type(h.placed_by) is not HoldRole:
            raise RetentionError("hold_role", safe_repr(h.placed_by))
        _id(h.principal_id, "principal_id")
        if not h.classes or not all(type(c) is RetentionClass for c in h.classes):
            raise RetentionError("hold_scope", "name the classes held")
        for r in h.record_ids:
            _id(r, "hold_scope")
        h = replace(h, classes=frozenset(h.classes), record_ids=frozenset(h.record_ids),
                    placed_at=at)  # fmt: skip
        old = self._find(h.tenant_id, h.hold_id)
        if old is not None:
            if old == h:
                return old
            raise RetentionError("conflicting_duplicate", safe_repr(h.hold_id))
        self._events.append(h)
        return h

    def release(self, r: HoldRelease) -> HoldRelease:
        at = _event(r.tenant_id, r.authority_id, r.reason, r.released_at)
        h = self._find(r.tenant_id, r.hold_id)
        if h is None:
            raise RetentionError("not_found", f"{safe_repr(r.hold_id)} in this tenant")
        if self._released(h) is not None:
            raise RetentionError("released", safe_repr(r.hold_id))
        if at < h.placed_at:
            raise RetentionError("time_order", format_instant(at))
        solo = self._approve(r.approvals)
        if r.solo is not solo:
            raise RetentionError("solo_mismatch", f"a solo release: {solo}")
        if solo:
            self._delay(r.approvals, h.placed_at, at)
        r = replace(r, released_at=at)
        self._events.append(r)
        return r

    def _approve(self, approvals: object) -> bool:
        """Owner decision 22: both roles, two principals, two unused receipts.
        Ids compare case-insensitively, so a case variant is not a second one.
        True: a solo release (decision 31), allowed only to the one operator."""
        if (type(approvals) is not tuple or len(approvals) != 2
                or not all(type(a) is Approval for a in approvals)
                or {a.role for a in approvals} != set(HoldRole)):  # fmt: skip
            raise RetentionError("release_approvals", "tenant owner and operator")
        who = {a.principal_id.casefold() for a in approvals}
        solo = len(who) == 1
        if solo:
            if self.solo is None:
                raise RetentionError("distinct_principals", "two people release")
            if who != {self.solo.operator_id.casefold()}:
                raise RetentionError("solo_principal", "the configured operator only")
        used = {a.step_up_receipt_id.casefold() for e in self._events
                if isinstance(e, HoldRelease) for a in e.approvals}  # fmt: skip
        mine = {a.step_up_receipt_id.casefold() for a in approvals}
        if len(mine) != 2 or mine & used:
            raise RetentionError("step_up_reused", "one fresh receipt per party")
        return solo

    @staticmethod
    def _delay(approvals: tuple[Approval, ...], placed: datetime,
               at: datetime) -> None:  # fmt: skip
        times = sorted(a.stepped_up_at for a in approvals
                       if a.stepped_up_at is not None)  # fmt: skip
        if len(times) != 2:
            raise RetentionError("step_up_time", "a solo release times each step-up")
        if times[0] < placed or times[1] > at:
            raise RetentionError("time_order", "step-ups between placing and release")
        if times[1] - times[0] < SOLO_DELAY:
            raise RetentionError("solo_delay", "step-ups at least 24 hours apart")

    def active(self, it: Item, at: datetime) -> LegalHold | None:
        live = []
        for e in self._events:
            if isinstance(e, LegalHold) and e.placed_at <= at and e.covers(it):
                rel = self._released(e)
                if rel is None or at < rel.released_at:
                    live.append(e)
        return min(live, key=lambda h: h.hold_id, default=None)

    def history(self, tenant_id: str) -> tuple[LegalHold | HoldRelease, ...]:
        return tuple(e for e in self._events if e.tenant_id == tenant_id)


@dataclass(frozen=True, slots=True)
class DeletionRequest:  # openapi.yaml DeleteRequest plus tenant, id and time
    request_id: str
    tenant_id: str
    scope_id: str
    classes: frozenset[RetentionClass]
    requested_at: datetime
    reauthentication_receipt_id: str
    acknowledgement: str  # recorded; never read by the planner
    record_ids: frozenset[str] = frozenset()  # empty: every record of the classes

    def __post_init__(self) -> None:
        _id(self.request_id, "request_id")
        _id(self.tenant_id, "tenant_id")
        _id(self.scope_id, "scope_id")
        _id(self.reauthentication_receipt_id, "reauthentication")
        if not self.classes or not all(type(c) is RetentionClass for c in self.classes):
            raise RetentionError("classes", "at least one RetentionClass")
        if not isinstance(self.acknowledgement, str) or not self.acknowledgement:
            raise RetentionError("acknowledgement", "required")
        for r in self.record_ids:
            _id(r, "record_ids")
        object.__setattr__(self, "classes", frozenset(self.classes))
        object.__setattr__(self, "record_ids", frozenset(self.record_ids))
        object.__setattr__(self, "requested_at", _utc(self.requested_at))


@dataclass(frozen=True, slots=True)
class ExpirySweep:
    sweep_id: str
    tenant_id: str
    triggered_at: datetime

    def __post_init__(self) -> None:
        _id(self.sweep_id, "sweep_id")
        _id(self.tenant_id, "tenant_id")
        object.__setattr__(self, "triggered_at", _utc(self.triggered_at))


type Trigger = DeletionRequest | ExpirySweep


def _job(trig: Trigger) -> tuple[str, datetime]:
    if isinstance(trig, DeletionRequest):
        return trig.request_id, trig.requested_at
    return trig.sweep_id, trig.triggered_at


class Action(StrEnum):
    DELETE = "delete"
    HELD = "held"  # a delete intent suspended by a legal hold; stays pending
    RETAIN = "retain"


@dataclass(frozen=True, slots=True)
class Decision:
    record_id: str
    record_hash: str
    cls: RetentionClass
    created_at: datetime
    action: Action
    reason: str  # requested | expired | legal_hold:<id> | retention_* | ...


@dataclass(frozen=True, slots=True)
class Plan:
    job_id: str
    tenant_id: str
    trigger_at: datetime
    policy_id: str
    policy_version: int
    decisions: tuple[Decision, ...]  # sorted by record hash
    not_found: tuple[str, ...]
    digest: str


def _decide(it: Item, trig: Trigger, rule: Rule, holds: Holds,
            at: datetime) -> str | None:  # fmt: skip
    """The reason for this item, or None when the trigger does not concern it."""
    if isinstance(trig, DeletionRequest):
        if it.cls not in trig.classes or (
                trig.record_ids and it.record_id not in trig.record_ids):  # fmt: skip
            return None
    elif rule.expire_after is not None and it.created_at + rule.expire_after > at:
        return None  # not expired yet
    if it.cls is RetentionClass.PRIVACY:
        return "tombstone_ledger"
    if isinstance(trig, ExpirySweep):
        if it.cls is RetentionClass.FINANCIAL:
            return "explicit_workflow_only"
        why = "expired" if rule.expire_after else "retention_period_unconfigured"
    else:
        why = {OnRequest.UNCONFIGURED: "retention_unconfigured",
               OnRequest.RETAIN: "retention_obligation",
               OnRequest.DELETE: "requested"}[rule.on_request]  # fmt: skip
    held = holds.active(it, at) if why in _INTENT else None
    return why if held is None else f"legal_hold:{held.hold_id}"


_INTENT = ("requested", "expired")


def _allows(trig: Trigger, rule: Rule, d: "Decision", at: datetime) -> bool:
    """Whether the current rule still permits this delete intent at `at`."""
    if isinstance(trig, DeletionRequest):
        return rule.on_request is OnRequest.DELETE
    return rule.expire_after is not None and d.created_at + rule.expire_after <= at


def plan(trig: Trigger, items: Iterable[Item], policy: Policy, holds: Holds) -> Plan:
    job_id, at = _job(trig)
    decisions, seen, keys = [], set(), set()
    for it in items:
        if type(it) is not Item:
            raise TypeError("items must be Item")
        if it.tenant_id != trig.tenant_id:
            raise RetentionError("other_tenant", safe_repr(it.record_id))
        if it.record_hash in keys:
            raise RetentionError("duplicate_item", safe_repr(it.record_id))
        keys.add(it.record_hash)
        seen.add(it.record_id)
        reason = _decide(it, trig, policy.rule(it.cls), holds, at)
        if reason is not None:
            act = (Action.DELETE if reason in _INTENT else Action.HELD
                   if reason.startswith("legal_hold:") else Action.RETAIN)  # fmt: skip
            decisions.append(Decision(it.record_id, it.record_hash, it.cls,
                                      it.created_at, act, reason))  # fmt: skip
    decisions.sort(key=lambda d: d.record_hash)
    asked = trig.record_ids if isinstance(trig, DeletionRequest) else frozenset()
    missing = tuple(sorted(asked - seen))
    body = [job_id, trig.tenant_id, format_instant(at), policy.policy_id,
            policy.version, [asdict(d) for d in decisions], missing]  # fmt: skip
    return Plan(job_id, trig.tenant_id, at, policy.policy_id, policy.version,
                tuple(decisions), missing, _sha(body))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Backup:
    backup_id: str
    cutoff_at: datetime  # the restore point: state as of this instant
    completed_at: datetime
    expires_at: datetime | None  # None: backup retention unconfigured

    def __post_init__(self) -> None:
        _id(self.backup_id, "backup_id")
        cut, done = _utc(self.cutoff_at), _utc(self.completed_at)
        if done < cut:
            raise RetentionError("time_order", "completed before its cutoff")
        end = None if self.expires_at is None else _utc(self.expires_at)
        object.__setattr__(self, "cutoff_at", cut)
        object.__setattr__(self, "completed_at", done)
        object.__setattr__(self, "expires_at", end)


def backup(backup_id: str, cutoff_at: datetime, completed_at: datetime,
           policy: Policy | None = None) -> Backup:  # fmt: skip
    policy = owner_defaults() if policy is None else policy
    period = policy.rule(RetentionClass.BACKUP).expire_after
    end = None if period is None else _utc(completed_at) + period
    return Backup(backup_id, cutoff_at, completed_at, end)


@dataclass(frozen=True, slots=True)
class Tombstone:
    seq: int
    tenant_id: str
    record_hash: str
    cls: RetentionClass
    job_id: str
    requested_at: datetime  # the job's trigger time, never reset by a retry
    deleted_at: datetime  # first execution that deleted it
    residual_backups: tuple[str, ...]  # unexpired backups that may hold a copy
    backup_expiry_at: datetime | None  # None: no residual copy, or unknown expiry
    prev_hash: str
    entry_hash: str


def _entry_hash(t: Tombstone) -> str:
    body = {k: v for k, v in asdict(t).items() if k != "entry_hash"}
    return _sha(["qw-tombstone-1", body])


@dataclass(frozen=True, slots=True)
class LedgerCheckpoint:  # stored outside the backup failure domain
    length: int
    head: str  # entry hash of the last entry, or GENESIS


def verify(entries: Sequence[Tombstone]) -> None:
    prev = GENESIS
    for n, t in enumerate(entries):
        if type(t) is not Tombstone or t.seq != n or t.prev_hash != prev or (
                t.entry_hash != _entry_hash(t)):  # fmt: skip
            raise RetentionError("ledger_tampered", f"entry {n}")
        prev = t.entry_hash


@dataclass
class TombstoneLedger:
    _entries: list[Tombstone] = field(default_factory=list)

    @property
    def entries(self) -> tuple[Tombstone, ...]:
        return tuple(self._entries)

    def checkpoint(self) -> LedgerCheckpoint:
        head = self._entries[-1].entry_hash if self._entries else GENESIS
        return LedgerCheckpoint(len(self._entries), head)

    def find(self, tenant_id: str, rhash: str) -> Tombstone | None:
        key = (tenant_id, rhash)
        return next((t for t in self._entries
                     if (t.tenant_id, t.record_hash) == key), None)  # fmt: skip

    def append(self, d: Decision, p: Plan, at: datetime,
               backups: Sequence[Backup]) -> Tombstone:  # fmt: skip
        if self._entries and at < self._entries[-1].deleted_at:
            raise RetentionError("time_order", "before the latest tombstone")
        residual = sorted(
            (b for b in backups if d.created_at <= b.cutoff_at < at
             and (b.expires_at is None or b.expires_at > at)),
            key=lambda b: b.backup_id)  # fmt: skip
        ends = [b.expires_at for b in residual]
        expiry = max(e for e in ends if e) if ends and None not in ends else None
        prev = self._entries[-1].entry_hash if self._entries else GENESIS
        kept = tuple(b.backup_id for b in residual)
        n = len(self._entries)
        draft = Tombstone(n, p.tenant_id, d.record_hash, d.cls, p.job_id,
                          p.trigger_at, at, kept, expiry, prev, "")  # fmt: skip
        t = replace(draft, entry_hash=_entry_hash(draft))
        self._entries.append(t)
        return t


@dataclass(frozen=True, slots=True)
class Execution:
    job_id: str
    deleted: tuple[Tombstone, ...]  # rows the caller removes from each store
    suspended: tuple[tuple[str, str], ...]  # (record hash, legal_hold:<id> or
    # policy_changed): pending intents a later run deletes
    complete: bool


@dataclass
class Retention:
    """One installation's holds, deletion jobs and tombstone ledger. `policy` is
    the configured policy used when a call passes none (owner decision 9)."""

    holds: Holds = field(default_factory=Holds)
    ledger: TombstoneLedger = field(default_factory=TombstoneLedger)
    policy: Policy = field(default_factory=owner_defaults)
    _jobs: dict[str, tuple[Trigger, Plan]] = field(default_factory=dict)

    def submit(self, trig: Trigger, items: Iterable[Item],
               policy: Policy | None = None) -> Plan:  # fmt: skip
        policy = self.policy if policy is None else policy
        job_id, _ = _job(trig)
        old = self._jobs.get(job_id)
        if old is not None:
            same = (old[1].policy_id, old[1].policy_version) == (
                policy.policy_id, policy.version)  # fmt: skip
            if old[0] == trig and same:
                return old[1]  # retry: the stored plan and trigger time stand
            raise RetentionError("conflicting_duplicate", safe_repr(job_id))
        p = plan(trig, items, policy, self.holds)
        self._jobs[job_id] = (trig, p)
        return p

    def execute(self, job_id: str, at: datetime, policy: Policy | None = None,
                backups: Sequence[Backup] = ()) -> Execution:  # fmt: skip
        """Idempotent: a rerun returns the same tombstones and deletes pending
        intents that holds and the current policy now permit."""
        policy = self.policy if policy is None else policy
        if job_id not in self._jobs:
            raise RetentionError("not_found", safe_repr(job_id))
        trig, p = self._jobs[job_id]
        at = _utc(at)
        if at < p.trigger_at:
            raise RetentionError("time_order", format_instant(at))
        done, todo, suspended = [], [], []
        for d in p.decisions:
            if d.action is Action.RETAIN:
                continue
            old = self.ledger.find(p.tenant_id, d.record_hash)
            held = self.holds.active(Item(p.tenant_id, d.record_id, d.cls,
                                          d.created_at), at)  # fmt: skip
            if old is not None:
                done.append(old)
            elif held is not None:
                suspended.append((d.record_hash, f"legal_hold:{held.hold_id}"))
            elif not _allows(trig, policy.rule(d.cls), d, at):
                suspended.append((d.record_hash, "policy_changed"))
            else:
                todo.append(d)
        last = self.ledger.entries[-1:]
        if todo and last and at < last[0].deleted_at:  # checked before any append
            raise RetentionError("time_order", "before the latest tombstone")
        done += [self.ledger.append(d, p, at, backups) for d in todo]
        done.sort(key=lambda t: t.record_hash)
        return Execution(job_id, tuple(done), tuple(suspended), not suspended)


@dataclass(frozen=True, slots=True)
class Snapshot:
    backup: Backup
    items: tuple[Item, ...]
    ledger: tuple[Tombstone, ...]  # the ledger copy inside the backup

    def __post_init__(self) -> None:
        ok = (type(self.backup) is Backup and type(self.items) is tuple
              and type(self.ledger) is tuple
              and all(type(i) is Item for i in self.items)
              and all(type(t) is Tombstone for t in self.ledger))  # fmt: skip
        if not ok:
            raise RetentionError("snapshot", "a Backup and tuples of Item/Tombstone")


@dataclass(frozen=True, slots=True)
class Restored:
    items: tuple[Item, ...]  # servable: every tombstone applied
    suppressed: tuple[str, ...]  # record hashes of tombstoned rows in the backup
    ledger: tuple[Tombstone, ...]  # the current ledger, unchanged
    restore_point: datetime
    restored_at: datetime
    data_loss: timedelta | None  # incident time minus restore point, when known


def restore(snap: Snapshot, current: Sequence[Tombstone], anchor: LedgerCheckpoint,
            restored_at: datetime,
            incident_at: datetime | None = None) -> Restored:  # fmt: skip
    at = _utc(restored_at)
    if at < snap.backup.completed_at:
        raise RetentionError("time_order", "restore before backup completion")
    verify(snap.ledger)
    verify(current)
    if type(anchor) is not LedgerCheckpoint or len(current) > anchor.length:
        raise RetentionError("ledger_mismatch", "not the anchored ledger")
    if len(current) < anchor.length:
        raise RetentionError("ledger_truncated", f"{len(current)} < {anchor.length}")
    head = current[-1].entry_hash if current else GENESIS
    if head != anchor.head:
        raise RetentionError("ledger_mismatch", "head differs from the anchor")
    if tuple(current[: len(snap.ledger)]) != tuple(snap.ledger):
        raise RetentionError("ledger_diverged", "current ledger must extend the copy")
    dead = {(t.tenant_id, t.record_hash) for t in current}
    kept = tuple(i for i in snap.items if (i.tenant_id, i.record_hash) not in dead)
    gone = tuple(i.record_hash for i in snap.items
                 if (i.tenant_id, i.record_hash) in dead)  # fmt: skip
    loss = None
    if incident_at is not None:
        loss = _utc(incident_at) - snap.backup.cutoff_at
    return Restored(kept, gone, tuple(current), snap.backup.cutoff_at, at, loss)


class Repro(StrEnum):
    REPRODUCIBLE = "reproducible"
    NOT_REPRODUCIBLE = "not_reproducible"  # an input was deleted
    LINEAGE_MISSING = "lineage_missing"  # an input is unknown: fail closed


@dataclass(frozen=True, slots=True)
class Derived:
    tenant_id: str
    node_id: str
    records: tuple[str, ...]  # input record hashes
    parents: tuple[str, ...]  # input derived node ids


@dataclass(frozen=True, slots=True)
class Status:
    node_id: str
    status: Repro
    deleted_inputs: tuple[str, ...]  # root record hashes
    missing: tuple[str, ...]


def reproducibility(tenant_id: str, nodes: Iterable[Derived], live: Iterable[str],
                    tombstones: Sequence[Tombstone]) -> dict[str, Status]:  # fmt: skip
    mine = {n.node_id: n for n in nodes if n.tenant_id == tenant_id}
    alive = set(live)
    dead = {t.record_hash for t in tombstones if t.tenant_id == tenant_id}
    out: dict[str, Status] = {}

    def visit(nid: str, path: frozenset[str]) -> tuple[set[str], set[str]]:
        if nid in path:
            raise RetentionError("lineage_cycle", safe_repr(nid))
        if nid in out:
            return set(out[nid].deleted_inputs), set(out[nid].missing)
        n = mine[nid]
        gone = {r for r in n.records if r in dead}
        miss = {r for r in n.records if r not in dead and r not in alive}
        for par in n.parents:
            if par not in mine:
                miss.add(par)
                continue
            g, m = visit(par, path | {nid})
            gone |= g
            miss |= m
        s = (Repro.NOT_REPRODUCIBLE if gone else
             Repro.LINEAGE_MISSING if miss else Repro.REPRODUCIBLE)  # fmt: skip
        out[nid] = Status(nid, s, tuple(sorted(gone)), tuple(sorted(miss)))
        return gone, miss

    for nid in sorted(mine):
        visit(nid, frozenset())
    return out
