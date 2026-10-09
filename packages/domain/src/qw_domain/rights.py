"""Provider, feed, rights and tenant entitlement registry; fail-closed use gate.

T021; spec §6 capability registry and "Rights-aware evidence and exports", §16
provider rights and status vocabulary, ADR-004, data dictionary `entitlement`.
- A `RightsProfile` holds a tri-state `Grant` per use, per commercial/personal scope
  and per jurisdiction. Absent is unknown; unknown is denied. A grant needs permission
  evidence (documentation is not permission, R094) and an expiry; evidence recorded
  after the caller's time is not yet known.
- Providers and feeds are installation-scoped, entitlements tenant-scoped. Feed and
  entitlement revisions are append-only with a content hash.
- Feed status (§16): registered → connected → qualified ⇄ suspended → retired; each
  step names an actor and dated evidence. Connected needs auth evidence and never
  implies qualified. Qualified needs workload evidence and every required use, a scope
  and a jurisdiction granted, and binds the revision: any later revision revokes it.
- `check_use` allows a use only for a qualified feed, an active entitlement, and
  grants of use, scope and jurisdiction in both profiles. Pooling across tenants also
  needs an installation licence (never a user's own credential) and consent.
- Revisions carry a server-set knowledge time; `check_use` reads the feed revision,
  status and entitlement revision known at `at`, which must be the time of use.
- LIMITATIONS: no persistence yet; no provider is qualified (T004 has not run);
  evidence `ref_id`s are not resolved against an evidence store.
Stdlib only.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.sources import ID_PATTERN

_REGION = re.compile(r"[A-Z]{2}(-[A-Z0-9]{1,3})?", re.ASCII)


class RightsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _check_id(value: object, what: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise RightsError(what, f"{safe_repr(value)} is malformed")


class Use(StrEnum):
    DISPLAY = "display"
    MODEL_PROCESSING = "model_processing"
    RETENTION = "retention"
    DERIVED_DATA = "derived_data"
    REDISTRIBUTION = "redistribution"
    USER_EXPORT = "user_export"
    CROSS_TENANT_POOLING = "cross_tenant_pooling"
    FINANCIAL_RECOMMENDATION = "financial_recommendation"


class UseScope(StrEnum):
    PERSONAL = "personal"
    COMMERCIAL = "commercial"


class RightState(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):  # §16 status vocabulary
    DOCS = "docs_checked"
    PERMISSION = "permission_granted"
    AUTH = "auth_tested"
    FIELD = "field_tested"
    WORKLOAD = "workload_tested"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: EvidenceKind
    ref_id: str
    recorded_at: datetime
    actor_id: str | None = None  # who attests it; required for permission (R094)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", ensure_aware_utc(self.recorded_at))
        if not isinstance(self.kind, EvidenceKind):
            raise RightsError("evidence", f"kind {safe_repr(self.kind)}")
        _check_id(self.ref_id, "evidence")
        if self.actor_id is not None or self.kind is EvidenceKind.PERMISSION:
            _check_id(self.actor_id, "actor")

    def to_wire(self) -> list[str | None]:
        when = format_instant(self.recorded_at)
        return [self.kind.value, self.ref_id, when, self.actor_id]


@dataclass(frozen=True, slots=True)
class Grant:
    state: RightState
    evidence: Evidence | None = None
    expires_at: datetime | None = None  # review date; a grant never runs open-ended
    retention: timedelta | None = None  # maximum retention, for Use.RETENTION only

    def __post_init__(self) -> None:
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", ensure_aware_utc(self.expires_at))
        if not isinstance(self.state, RightState):
            raise RightsError("state", safe_repr(self.state))
        if self.evidence is not None and type(self.evidence) is not Evidence:
            raise RightsError("evidence", "an Evidence record is required")
        if self.state is RightState.GRANTED:
            if (
                self.evidence is None
                or self.evidence.kind is not EvidenceKind.PERMISSION
            ):
                raise RightsError("permission_evidence", "a grant needs permission")
            if self.expires_at is None:
                raise RightsError("review_date", "a grant needs an expiry")
        if self.retention is not None and (
            type(self.retention) is not timedelta or self.retention <= timedelta(0)
        ):
            raise RightsError("retention", "duration must be a positive timedelta")

    def usable(self, at: datetime) -> bool:
        return (
            self.state is RightState.GRANTED
            and self.evidence is not None
            and self.expires_at is not None
            and self.evidence.recorded_at <= at < self.expires_at
        )

    def to_wire(self) -> list[object]:
        keep = self.retention
        return [
            self.state.value,
            None if self.evidence is None else self.evidence.to_wire(),
            None if self.expires_at is None else format_instant(self.expires_at),
            None if keep is None else str(keep // timedelta(microseconds=1)),
        ]


UNKNOWN = Grant(RightState.UNKNOWN)


@dataclass(frozen=True, slots=True)
class RightsProfile:
    uses: Mapping[Use, Grant]
    scopes: Mapping[UseScope, Grant]
    jurisdictions: Mapping[str, Grant]  # ISO 3166 country or subdivision code

    def __post_init__(self) -> None:
        for name, kind in (("uses", Use), ("scopes", UseScope)):
            table = dict(getattr(self, name))
            if not all(isinstance(k, kind) for k in table):
                raise RightsError(name, "keys must be enum members")
            object.__setattr__(self, name, MappingProxyType(table))
        regions = dict(self.jurisdictions)
        for code in regions:
            if not isinstance(code, str) or _REGION.fullmatch(code) is None:
                raise RightsError("jurisdiction", f"{safe_repr(code)} is malformed")
        object.__setattr__(self, "jurisdictions", MappingProxyType(regions))
        grants = [*self.uses.items(), *self.scopes.items(), *regions.items()]
        if not all(type(g) is Grant for _, g in grants):
            raise RightsError("grant", "values must be Grant records")
        for key, g in grants:
            needs = key is Use.RETENTION and g.state is RightState.GRANTED
            if needs != (g.retention is not None):
                raise RightsError(
                    "retention", f"{key}: duration only on granted retention"
                )

    def grant(self, use: Use) -> Grant:
        return self.uses.get(use, UNKNOWN)

    def to_wire(self) -> dict[str, object]:
        return {
            "uses": {k.value: g.to_wire() for k, g in sorted(self.uses.items())},
            "scopes": {k.value: g.to_wire() for k, g in sorted(self.scopes.items())},
            "jurisdictions": {
                k: g.to_wire() for k, g in sorted(self.jurisdictions.items())
            },
        }


FAIL_CLOSED = RightsProfile({}, {}, {})


def _check_rights(rights: object) -> None:
    if type(rights) is not RightsProfile:
        raise RightsError("rights", "a RightsProfile is required")


def _hash(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Provider:
    provider_id: str
    name: str

    def __post_init__(self) -> None:
        _check_id(self.provider_id, "provider")


class FeedStatus(StrEnum):
    REGISTERED = "registered"
    CONNECTED = "connected"
    QUALIFIED = "qualified"
    SUSPENDED = "suspended"
    RETIRED = "retired"


_S = FeedStatus
TRANSITIONS: Mapping[FeedStatus, frozenset[FeedStatus]] = MappingProxyType(
    {
        _S.REGISTERED: frozenset({_S.CONNECTED, _S.RETIRED}),
        _S.CONNECTED: frozenset({_S.QUALIFIED, _S.SUSPENDED, _S.RETIRED}),
        _S.QUALIFIED: frozenset({_S.QUALIFIED, _S.SUSPENDED, _S.RETIRED}),
        _S.SUSPENDED: frozenset({_S.QUALIFIED, _S.RETIRED}),
        _S.RETIRED: frozenset(),
    }
)
_E = EvidenceKind
_NEEDS = {_S.CONNECTED: {_E.AUTH}, _S.QUALIFIED: {_E.WORKLOAD, _E.PERMISSION}}


@dataclass(frozen=True, slots=True)
class FeedRevision:
    feed_id: str
    provider_id: str
    revision: int
    feed: str
    required_uses: frozenset[Use]  # the uses this deployment qualifies
    rights: RightsProfile  # what the provider permits this installation
    content_hash: str
    recorded_at: datetime  # knowledge time, set by the server; not hashed


@dataclass(frozen=True, slots=True)
class Transition:
    status: FeedStatus
    actor_id: str
    evidence: tuple[Evidence, ...]
    at: datetime
    revision: int
    revision_hash: str


@dataclass(frozen=True, slots=True)
class FeedHistory:
    feed_id: str
    provider_id: str
    revisions: tuple[FeedRevision, ...] = ()
    transitions: tuple[Transition, ...] = field(default=())

    @classmethod
    def new(cls, feed_id: str, provider_id: str) -> "FeedHistory":
        _check_id(feed_id, "feed")
        _check_id(provider_id, "provider")
        return cls(feed_id, provider_id)

    @property
    def current(self) -> FeedRevision:
        if not self.revisions:
            raise RightsError("no_revision", f"feed {self.feed_id} has no rights")
        return self.revisions[-1]

    @property
    def status(self) -> FeedStatus:
        return self.transitions[-1].status if self.transitions else _S.REGISTERED

    def status_at(self, at: datetime) -> Transition | None:
        known = [t for t in self.transitions if t.at <= at]
        return known[-1] if known else None

    def revision_at(self, at: datetime) -> FeedRevision | None:
        known = [r for r in self.revisions if r.recorded_at <= at]
        return known[-1] if known else None

    def revise(
        self,
        feed: str,
        required_uses: frozenset[Use],
        rights: RightsProfile,
        recorded_at: datetime,
    ) -> "FeedHistory":
        recorded_at = ensure_aware_utc(recorded_at)
        last = [
            *(r.recorded_at for r in self.revisions[-1:]),
            *(t.at for t in self.transitions[-1:]),
        ]
        if any(recorded_at < x for x in last):
            raise RightsError("time_order", "revisions are recorded in time order")
        _check_id(feed, "feed")
        uses = required_uses
        if (
            not uses
            or not all(isinstance(u, Use) for u in uses)
            or type(uses) is not frozenset
        ):
            raise RightsError("required_uses", "a non-empty frozenset of Use")
        _check_rights(rights)
        body = {
            "feed_id": self.feed_id,
            "provider_id": self.provider_id,
            "feed": feed,
            "required_uses": sorted(u.value for u in required_uses),
            "rights": rights.to_wire(),
        }
        digest = _hash(body)
        if self.revisions and self.revisions[-1].content_hash == digest:
            return self
        n, pid = len(self.revisions) + 1, self.provider_id
        rev = FeedRevision(
            self.feed_id, pid, n, feed, required_uses, rights, digest, recorded_at
        )
        return FeedHistory(
            self.feed_id, self.provider_id, (*self.revisions, rev), self.transitions
        )

    def transition(
        self,
        to: FeedStatus,
        actor_id: str,
        evidence: tuple[Evidence, ...],
        at: datetime,
    ) -> "FeedHistory":
        at = ensure_aware_utc(at)
        current = self.current
        if to not in TRANSITIONS[self.status]:
            raise RightsError("illegal_transition", f"{self.status} -> {to}")
        _check_id(actor_id, "actor")
        if at < current.recorded_at or (
            self.transitions and at < self.transitions[-1].at
        ):
            raise RightsError("time_order", "transitions are recorded in time order")
        known = tuple(
            e for e in evidence if type(e) is Evidence and e.recorded_at <= at
        )
        need = _NEEDS.get(to, set())  # needed kinds must postdate the revision
        fresh = {e.kind for e in known if e.recorded_at >= current.recorded_at}
        if not known or not need <= fresh:
            raise RightsError("evidence_required", f"{to} needs {sorted(need)}")
        if to is _S.QUALIFIED:
            r = current.rights
            if missing := [
                u for u in sorted(current.required_uses) if not r.grant(u).usable(at)
            ]:
                raise RightsError("use_not_granted", ", ".join(missing))
            for name, table in (("scope", r.scopes), ("jurisdiction", r.jurisdictions)):
                if not any(g.usable(at) for g in table.values()):
                    raise RightsError("use_not_granted", f"no {name} granted")
        step = Transition(
            to, actor_id, known, at, current.revision, current.content_hash
        )
        return FeedHistory(
            self.feed_id, self.provider_id, self.revisions, (*self.transitions, step)
        )


class EntitlementSource(StrEnum):
    USER_CREDENTIAL = "user_credential"  # BYO key: never a sublicence to pool
    INSTALLATION_LICENSE = "installation_license"


@dataclass(frozen=True, slots=True)
class EntitlementRevision:
    tenant_id: str
    feed_id: str
    revision: int
    source: EntitlementSource
    active: bool
    rights: RightsProfile  # what this tenant's plan or account permits
    pooling_consent_ref: str | None
    content_hash: str
    recorded_at: datetime  # knowledge time, set by the server; not hashed


@dataclass(frozen=True, slots=True)
class EntitlementHistory:
    tenant_id: str
    feed_id: str
    revisions: tuple[EntitlementRevision, ...] = ()

    @classmethod
    def new(cls, tenant_id: str, feed_id: str) -> "EntitlementHistory":
        _check_id(tenant_id, "tenant")
        _check_id(feed_id, "feed")
        return cls(tenant_id, feed_id)

    @property
    def current(self) -> EntitlementRevision | None:
        return self.revisions[-1] if self.revisions else None

    def revision_at(self, at: datetime) -> EntitlementRevision | None:
        known = [r for r in self.revisions if r.recorded_at <= at]
        return known[-1] if known else None

    def revise(
        self,
        source: EntitlementSource,
        active: bool,
        rights: RightsProfile,
        pooling_consent_ref: str | None,
        recorded_at: datetime,
    ) -> "EntitlementHistory":
        recorded_at = ensure_aware_utc(recorded_at)
        if self.revisions and recorded_at < self.revisions[-1].recorded_at:
            raise RightsError("time_order", "revisions are recorded in time order")
        if not isinstance(source, EntitlementSource) or type(active) is not bool:
            raise RightsError("entitlement", "source and active flag are required")
        _check_rights(rights)
        if pooling_consent_ref is not None:
            _check_id(pooling_consent_ref, "consent")
        body = {
            "tenant_id": self.tenant_id,
            "feed_id": self.feed_id,
            "source": source.value,
            "active": active,
            "rights": rights.to_wire(),
            "pooling_consent_ref": pooling_consent_ref,
        }
        digest = _hash(body)
        if self.revisions and self.revisions[-1].content_hash == digest:
            return self
        n, ref = len(self.revisions) + 1, pooling_consent_ref
        rev = EntitlementRevision(
            self.tenant_id,
            self.feed_id,
            n,
            source,
            active,
            rights,
            ref,
            digest,
            recorded_at,
        )
        return EntitlementHistory(self.tenant_id, self.feed_id, (*self.revisions, rev))


def _extends(old: tuple[object, ...], new: tuple[object, ...]) -> bool:
    return new[: len(old)] == old


def _replay(history: object) -> None:
    """Rebuild a history through `revise`/`transition` and require equality, so
    hashes, numbering, transition rules and evidence are re-derived, not trusted."""
    try:
        if type(history) is FeedHistory:
            feed = FeedHistory.new(history.feed_id, history.provider_id)
            pending = list(history.revisions)
            for t in (*history.transitions, None):
                while pending and (t is None or len(feed.revisions) < t.revision):
                    x = pending.pop(0)
                    feed = feed.revise(x.feed, x.required_uses, x.rights, x.recorded_at)
                if t is not None:
                    feed = feed.transition(t.status, t.actor_id, t.evidence, t.at)
            rebuilt: object = feed
        elif type(history) is EntitlementHistory:
            ent = EntitlementHistory.new(history.tenant_id, history.feed_id)
            for y in history.revisions:
                ref, when = y.pooling_consent_ref, y.recorded_at
                ent = ent.revise(y.source, y.active, y.rights, ref, when)
            rebuilt = ent
        else:
            raise RightsError("type", type(history).__name__)
    except (ValueError, TypeError, AttributeError) as exc:  # InstantError included
        raise RightsError("integrity", f"history does not replay: {exc}") from None
    if rebuilt != history:
        raise RightsError("integrity", "history differs from its replay")


@dataclass(frozen=True, slots=True)
class Registry:
    """Immutable; every history is replayed on construction (forged ones refused)."""

    providers: Mapping[str, Provider] = field(default_factory=dict)
    feeds: Mapping[str, FeedHistory] = field(default_factory=dict)
    entitlements: Mapping[tuple[str, str], EntitlementHistory] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        for name in ("providers", "feeds", "entitlements"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for pid, p in self.providers.items():
            if type(p) is not Provider or p.provider_id != pid:
                raise RightsError("integrity", f"provider {safe_repr(pid)}")
        for fid, f in self.feeds.items():
            _replay(f)
            if f.provider_id not in self.providers:
                raise RightsError("provider_unregistered", f.provider_id)
            if f.feed_id != fid:
                raise RightsError("integrity", f"feed key {safe_repr(fid)}")
        for key, e in self.entitlements.items():
            _replay(e)
            if e.feed_id not in self.feeds:
                raise RightsError("feed_unregistered", e.feed_id)
            if (e.tenant_id, e.feed_id) != key:
                raise RightsError("integrity", f"entitlement key {safe_repr(key)}")

    def with_provider(self, provider: Provider) -> "Registry":
        known = self.providers.get(provider.provider_id)
        if known is not None and known != provider:
            raise RightsError("append_only", "a provider record is immutable")
        providers = {**self.providers, provider.provider_id: provider}
        return Registry(providers, self.feeds, self.entitlements)

    def with_feed(self, history: FeedHistory) -> "Registry":
        old = self.feeds.get(history.feed_id)
        if old is not None and not (
            old.provider_id == history.provider_id
            and _extends(old.revisions, history.revisions)
            and _extends(old.transitions, history.transitions)
        ):
            raise RightsError("append_only", "feed history may only be extended")
        feeds = {**self.feeds, history.feed_id: history}
        return Registry(self.providers, feeds, self.entitlements)

    def with_entitlement(self, history: EntitlementHistory) -> "Registry":
        key = (history.tenant_id, history.feed_id)
        old = self.entitlements.get(key)
        if old is not None and not _extends(old.revisions, history.revisions):
            raise RightsError("append_only", "entitlement history may only be extended")
        return Registry(self.providers, self.feeds, {**self.entitlements, key: history})


class DenyCode(StrEnum):
    UNKNOWN = "right_unknown"
    DENIED = "right_denied"
    EXPIRED = "right_expired"
    NOT_QUALIFIED = "not_qualified"
    NOT_ENTITLED = "not_entitled"
    RETIRED = "feed_retired"
    POOLING_NOT_GRANTED = "cross_tenant_pooling_not_granted"
    RETENTION_EXCEEDED = "retention_exceeded"


@dataclass(frozen=True, slots=True)
class DenyReason:
    code: DenyCode
    subject: str


@dataclass(frozen=True, slots=True)
class UseDecision:
    allowed: bool
    reasons: tuple[DenyReason, ...]
    feed_hash: str | None
    entitlement_hash: str | None


def _grant_reason(grant: Grant, at: datetime) -> DenyCode | None:
    if grant.usable(at):
        return None
    if grant.state is RightState.DENIED:
        return DenyCode.DENIED
    if grant.state is RightState.UNKNOWN or grant.expires_at is None:
        return DenyCode.UNKNOWN
    if at >= grant.expires_at:
        return DenyCode.EXPIRED
    return DenyCode.UNKNOWN  # permission recorded after `at` was not yet known


def check_use(
    registry: Registry,
    tenant_id: str,
    feed_id: str,
    use: Use,
    at: datetime,
    *,
    scope: UseScope,
    jurisdiction: str,
    retain_for: timedelta | None = None,
) -> UseDecision:
    """Allow `use` of `feed_id` content for `tenant_id` at `at`, or deny with every
    applicable reason. `retain_for` is required for, and only for, Use.RETENTION.
    PRECONDITION: `at` is the time of use. Revisions, transitions and evidence are
    taken as known at `at`, so the persistence/API layer must set it from the server
    clock; a caller-chosen past `at` would replay older permissions."""
    at = ensure_aware_utc(at)
    if retain_for is not None and use is not Use.RETENTION:
        raise RightsError("retain_for", "only a retention check takes a duration")
    reasons: list[DenyReason] = []

    def deny(code: DenyCode, subject: str) -> None:
        reasons.append(DenyReason(code, subject))

    feed = registry.feeds.get(feed_id)
    rev = feed.revision_at(at) if feed is not None else None
    if feed is None or rev is None:
        deny(DenyCode.NOT_QUALIFIED, "no_feed_revision_known")
        return UseDecision(False, tuple(reasons), None, None)
    step = feed.status_at(at)
    status = step.status if step is not None else _S.REGISTERED
    if status is _S.RETIRED:
        deny(DenyCode.RETIRED, feed_id)
    elif step is None or status is not _S.QUALIFIED:
        deny(DenyCode.NOT_QUALIFIED, f"status:{status.value}")
    elif (step.revision, step.revision_hash) != (rev.revision, rev.content_hash):
        deny(DenyCode.NOT_QUALIFIED, "rights_revised_requalification_required")
    elif use not in rev.required_uses:
        deny(DenyCode.NOT_QUALIFIED, f"use_not_qualified:{use.value}")
    history = registry.entitlements.get((tenant_id, feed_id))
    ent = history.revision_at(at) if history is not None else None
    if ent is None or not ent.active:
        deny(DenyCode.NOT_ENTITLED, tenant_id)
        ent = None
    layers = [("feed", rev.rights)] + ([("entitlement", ent.rights)] if ent else [])
    for layer, rights in layers:
        region = rights.jurisdictions.get(jurisdiction, UNKNOWN)
        checks = (
            ("use", use, rights.grant(use)),
            ("scope", scope, rights.scopes.get(scope, UNKNOWN)),
        )
        for kind, key, grant in (*checks, ("jurisdiction", jurisdiction, region)):
            if (code := _grant_reason(grant, at)) is not None:
                deny(code, f"{layer}:{kind}:{key}")
        keep = rights.grant(use).retention
        if retain_for is not None and keep is not None and retain_for > keep:
            deny(DenyCode.RETENTION_EXCEEDED, f"{layer}:duration")
    if use is Use.RETENTION and (retain_for is None or retain_for <= timedelta(0)):
        deny(DenyCode.RETENTION_EXCEEDED, "duration_missing")
    if use is Use.CROSS_TENANT_POOLING and (
        ent is None
        or ent.source is not EntitlementSource.INSTALLATION_LICENSE
        or ent.pooling_consent_ref is None
        or not all(r.grant(use).usable(at) for _, r in layers)
    ):
        deny(DenyCode.POOLING_NOT_GRANTED, tenant_id)
    ent_hash = None if ent is None else ent.content_hash
    return UseDecision(not reasons, tuple(reasons), rev.content_hash, ent_hash)
