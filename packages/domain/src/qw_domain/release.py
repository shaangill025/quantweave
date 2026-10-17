"""Release signing, operator install, user strategy adoption and compatible rollback
(T051 increment 1; spec §11 "Promotion, adoption and recovery", §15, Journey D;
R030, R034, R039, R051, R076, R087, R092, R096, R099). Domain only, stdlib only.
- Three typed acts, never implied by one another (R099): a maintainer signs a release
  manifest; an operator installs a verified release into one deployment; a tenant
  adopts a strategy version with step-up consent (`StrategyRegistry.adopt`, T027).
  Each act has its own step-up receipt, and each yields a `promotion_receipt`.
- Only the latest recorded T044 verdict, if it is accept-for-review, of an
  installation-scope protocol that requires maintainer release can be released.
  Tenant-scope candidates (R039) and code/optimizer targets (T007 sandbox not
  qualified, R076) are refused. Producer and assessor actors cannot sign (R030).
  Verification and install re-check the gates the manifest can show (target,
  required approvals, strategy binding, component), so a hand-built manifest signed
  with a trusted key cannot skip them.
- The signature covers the canonical manifest: candidate, artifact, protocol and
  strategy hashes, verdict id, required approvals, schema compatibility, rollback
  version and the maintainer's approval. Verification checks the key id against the
  operator's trusted key set, its principal, validity and revocation, the manifest
  hash and the signature. Test-only keys are refused unless the deployment allows them.
- A deployment's state schema only moves forward. A release runs while the schema is
  inside its range. Rollback appends an event; if no installed, verified, compatible
  prior release exists, a `Pause` with a structured reason is appended instead.
  Nothing is rewritten or downgraded, and no tenant is moved to another version.
LIMITATIONS: `Signer`/`Verifier` are protocols; only a test HMAC signer exists (in the
tests). Production needs an asymmetric scheme (Ed25519) from a reviewed dependency,
so operators hold public keys only (R096). No persistence; times, actor ids and
step-up receipts are caller-asserted. Verdicts are trusted as recorded (T044 N3).
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from qw_domain.improvement import (
    SANDBOX_TARGETS,
    ApprovalClass,
    Decision,
    ExperimentRegistry,
    Scope,
    Target,
)
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import digest
from qw_domain.sources import ID_PATTERN
from qw_domain.strategy_registry import StrategyConsent, StrategyManifest
from qw_domain.strategy_registry import StrategyRegistry as Strategies


class ReleaseError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ReleaseError(what, "malformed id")
    return value


class AuthorityScope(StrEnum):  # promotion_receipt.schema.json `authority_scope`
    HOSTED_MAINTAINER = "hosted_maintainer"
    SELF_HOSTED_OPERATOR = "self_hosted_operator"
    USER_STRATEGY_ADOPTION = "user_strategy_adoption"


class Edition(StrEnum):
    HOSTED = "hosted"
    SELF_HOSTED = "self_hosted"


def authority_scope(cls: ApprovalClass, edition: Edition) -> AuthorityScope:
    """T044 approval class -> schema scope. Maintainers sign shared releases for both
    editions; the hosted edition's installs are made by its maintainers."""
    if cls is ApprovalClass.USER_STRATEGY_ADOPTION:
        return AuthorityScope.USER_STRATEGY_ADOPTION
    if cls is ApprovalClass.OPERATOR_INSTALL and edition is Edition.SELF_HOSTED:
        return AuthorityScope.SELF_HOSTED_OPERATOR
    return AuthorityScope.HOSTED_MAINTAINER


CHANGE_KIND: Mapping[Target, str] = {  # promotion_receipt `change_kind`
    Target.STRATEGY: "strategy", Target.APPLICATION_CODE: "application",
    Target.RECURSIVE_OPTIMIZER: "optimizer", Target.RESEARCH: "configuration",
    Target.EVALUATOR: "configuration", Target.PERSONAL_ADAPTATION: "configuration",
}  # fmt: skip


# Algorithms accepted from non-test keys. Empty until T009 reviews an asymmetric
# scheme (Ed25519): until then every key is test-only and production refuses it.
PRODUCTION_ALGORITHMS: frozenset[str] = frozenset()


class Signer(Protocol):
    @property
    def key_id(self) -> str: ...
    @property
    def algorithm(self) -> str: ...
    def sign(self, message: bytes) -> str: ...


class Verifier(Protocol):
    def verify(self, message: bytes, signature: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class TrustedKey:
    """A maintainer release key in the operator's trusted set."""

    key_id: str
    algorithm: str
    principal_id: str
    verifier: Verifier
    valid_from: datetime
    revoked_at: datetime | None = None
    test_only: bool = True


@dataclass(frozen=True, slots=True)
class Compatibility:
    min_schema: int  # state schema versions this release can run on (inclusive)
    max_schema: int
    writes_schema: int  # the state schema after install (expand-only migration)

    def __post_init__(self) -> None:
        v = (self.min_schema, self.writes_schema, self.max_schema)
        if not all(type(x) is int for x in v) or not 1 <= v[0] <= v[1] <= v[2]:
            raise ReleaseError("compatibility", "1 <= min <= writes <= max")

    def runs_on(self, schema: int) -> bool:
        return self.min_schema <= schema <= self.max_schema


@dataclass(frozen=True, slots=True)
class MaintainerApproval:
    principal_id: str
    step_up_receipt_id: str
    approved_at: datetime


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    release_id: str
    component: str  # "<target>:<strategy id or target>": one namespace per kind
    target: Target
    experiment_id: str
    candidate_id: str
    candidate_hash: str
    artifact_hash: str
    protocol_hash: str
    verdict_id: str
    required_approvals: tuple[str, ...]
    compatibility: Compatibility
    strategy_version: str | None
    strategy_material_hash: str | None
    rollback_version_id: str
    approval: MaintainerApproval

    def wire(self) -> dict[str, object]:
        c, a = self.compatibility, self.approval
        return {
            "release_id": self.release_id, "component": self.component,
            "target": self.target.value, "experiment_id": self.experiment_id,
            "candidate_id": self.candidate_id, "candidate_hash": self.candidate_hash,
            "artifact_hash": self.artifact_hash, "protocol_hash": self.protocol_hash,
            "verdict_id": self.verdict_id,
            "required_approvals": list(self.required_approvals),
            "compatibility": [c.min_schema, c.max_schema, c.writes_schema],
            "strategy": [self.strategy_version, self.strategy_material_hash],
            "rollback_version_id": self.rollback_version_id,
            "approval": [a.principal_id, a.step_up_receipt_id,
                         format_instant(a.approved_at)],
        }  # fmt: skip

    @property
    def manifest_hash(self) -> str:
        return digest(self.wire())

    @property
    def strategy_id(self) -> str | None:
        return self.component.split(":", 1)[1] if self.strategy_version else None

    @property
    def user_adoption_required(self) -> bool:
        return ApprovalClass.USER_STRATEGY_ADOPTION.value in self.required_approvals


def _target_gates(target: Target, approvals: Collection[str]) -> list[str]:
    """Target gates shared by `prepare_release` and `verify_release`, so a signed
    manifest that skipped preparation cannot pass a gate preparation enforces."""
    out = []
    if target in SANDBOX_TARGETS:  # R076: T007 sandbox not qualified
        out.append("sandbox_unqualified")
    if ApprovalClass.MAINTAINER_RELEASE not in approvals:
        out.append("maintainer_release_not_required")
    return out


def manifest_refusals(m: ReleaseManifest) -> list[str]:
    """`prepare_release` invariants that the manifest alone can show. Self-approval,
    scope and verdict order need the registry and are checked only when preparing.
    A mistyped target or approval list is refused alone: nothing else can be read."""
    if not isinstance(m.target, Target):
        return ["target_type"]
    approvals = m.required_approvals
    if type(approvals) is not tuple or not all(isinstance(a, str) for a in approvals):
        return ["approvals_type"]  # a bare str would pass a substring `in` test
    out = _target_gates(m.target, approvals)
    strategy = m.target is Target.STRATEGY
    absent = (m.strategy_version is None, m.strategy_material_hash is None)
    if absent != (not strategy, not strategy):  # strategy releases name both
        out.append("strategy_binding")
    kind, _, sid = m.component.partition(":")
    if kind != m.target.value or not sid or (not strategy and sid != kind):
        out.append("component")
    return out


def prepare_release(
    reg: ExperimentRegistry, candidate_id: str, release_id: str,
    compatibility: Compatibility, approval: MaintainerApproval,
    strategy: StrategyManifest | None = None,
) -> ReleaseManifest:  # fmt: skip
    """The manifest a maintainer may sign for one assessed candidate."""
    _id(release_id, "release_id")
    for x in (approval.principal_id, approval.step_up_receipt_id):
        _id(x, "approval")
    at = ensure_aware_utc(approval.approved_at)
    c = reg.candidate(candidate_id)
    p = reg.protocol(c.experiment_id)
    if p.scope is not Scope.INSTALLATION:
        raise ReleaseError("tenant_scope", "private learning is not a shared release")
    if gates := _target_gates(p.target, p.approvals):
        raise ReleaseError(gates[0], p.target.value)
    verdicts = reg.verdicts(candidate_id)
    if not verdicts or verdicts[-1].decision is not Decision.ACCEPT_FOR_REVIEW:
        raise ReleaseError("not_accepted", candidate_id)
    v = verdicts[-1]
    actors = {p.producer.actor_id, p.assessor.actor_id, v.assessor.actor_id}
    if approval.principal_id in actors:
        raise ReleaseError("self_approval", approval.principal_id)
    if at < v.assessed_at:
        raise ReleaseError("time_order", "approval precedes the verdict")
    if (strategy is None) != (p.target is not Target.STRATEGY):
        raise ReleaseError("strategy_binding", "strategy releases name a manifest")
    if strategy is not None and strategy.implementation_hash != c.artifact_hash:
        raise ReleaseError("strategy_binding", "implementation is not the candidate")
    sid = strategy.strategy_id if strategy else p.target.value
    return ReleaseManifest(
        release_id, f"{p.target.value}:{sid}", p.target,
        p.experiment_id, candidate_id, c.content_hash, c.artifact_hash,
        p.content_hash, v.verdict_id, tuple(sorted(p.approvals)), compatibility,
        strategy.version if strategy else None,
        strategy.material_hash if strategy else None, p.baseline_version,
        MaintainerApproval(approval.principal_id, approval.step_up_receipt_id, at),
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class SignedRelease:
    manifest: ReleaseManifest
    manifest_hash: str
    key_id: str
    algorithm: str
    signature: str

    @property
    def signature_reference(self) -> str:
        return f"sig.{self.key_id[:80]}.{digest(self.signature)[:32]}"


def _message(m: ReleaseManifest) -> bytes:
    return digest(["qw-release-v1", m.wire()]).encode()


def sign_release(m: ReleaseManifest, signer: Signer) -> SignedRelease:
    _id(signer.key_id, "key_id")
    return SignedRelease(m, m.manifest_hash, signer.key_id, signer.algorithm,
                         signer.sign(_message(m)))  # fmt: skip


def verify_release(
    sr: SignedRelease, keys: Mapping[str, TrustedKey], at: datetime,
    allow_test_keys: bool,
) -> list[str]:  # fmt: skip
    """Reasons the release cannot be trusted at `at`; empty when it verifies. A
    validly signed manifest is still refused if it breaks a preparation gate."""
    at, m = ensure_aware_utc(at), sr.manifest
    out = manifest_refusals(m)
    if out[:1] in (["target_type"], ["approvals_type"]):
        return out  # the manifest cannot be hashed or read further
    k = keys.get(sr.key_id)
    if k is None or k.key_id != sr.key_id or k.algorithm != sr.algorithm:
        return [*out, "untrusted_key"]
    if k.principal_id != m.approval.principal_id:
        out.append("key_principal")
    if m.approval.approved_at < k.valid_from or (
        k.revoked_at is not None and k.revoked_at <= at
    ):  # fmt: skip
        out.append("key_not_valid")
    if k.test_only and not allow_test_keys:
        out.append("test_key_refused")
    if not k.test_only and k.algorithm not in PRODUCTION_ALGORITHMS:
        out.append("algorithm_not_production")
    if sr.manifest_hash != m.manifest_hash:
        out.append("manifest_hash")
    try:
        ok = k.verifier.verify(_message(m), sr.signature) is True
    except Exception:  # a failing verifier never verifies
        ok = False
    if not ok:
        out.append("bad_signature")
    return out


@dataclass(frozen=True, slots=True)
class PromotionReceipt:
    id: str
    change_kind: str
    candidate_id: str
    candidate_hash: str
    assessment_receipt_ids: tuple[str, ...]
    approved_by: str
    approved_at: datetime
    authority_scope: AuthorityScope
    rollback_version_id: str
    signature_reference: str
    effective_at: datetime
    user_adoption_required: bool
    compatibility_result: str = "passed"  # issued only after compatibility passed

    def to_wire(self) -> dict[str, object]:
        return {
            "id": self.id, "change_kind": self.change_kind,
            "candidate_id": self.candidate_id, "candidate_hash": self.candidate_hash,
            "assessment_receipt_ids": list(self.assessment_receipt_ids),
            "approved_by": self.approved_by,
            "approved_at": format_instant(self.approved_at),
            "authority_scope": self.authority_scope.value,
            "rollback_version_id": self.rollback_version_id,
            "signature_reference": self.signature_reference,
            "effective_at": format_instant(self.effective_at),
            "compatibility_result": self.compatibility_result,
            "user_adoption_required": self.user_adoption_required,
        }  # fmt: skip


def _receipt(
    rid: str, sr: SignedRelease, by: str, at: datetime, scope: AuthorityScope,
    rollback: str, sig_ref: str, adoption_required: bool,
) -> PromotionReceipt:  # fmt: skip
    m = sr.manifest
    return PromotionReceipt(
        _id(rid, "receipt_id"), CHANGE_KIND[m.target], m.candidate_id,
        m.candidate_hash, (m.verdict_id,), by, at, scope, rollback, sig_ref, at,
        adoption_required,
    )  # fmt: skip


def release_receipt(sr: SignedRelease, receipt_id: str) -> PromotionReceipt:
    a = sr.manifest.approval
    return _receipt(receipt_id, sr, a.principal_id, a.approved_at,
                    AuthorityScope.HOSTED_MAINTAINER,
                    sr.manifest.rollback_version_id, sr.signature_reference,
                    sr.manifest.user_adoption_required)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Deployment:
    deployment_id: str
    edition: Edition
    operators: frozenset[str]
    tenants: frozenset[str]
    initial_schema: int
    allows_test_keys: bool = False


@dataclass(frozen=True, slots=True)
class Install:
    event_id: str
    release: SignedRelease
    operator_id: str
    step_up_receipt_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Rollback:
    event_id: str
    component: str
    from_release_id: str
    to_release_id: str
    operator_id: str
    step_up_receipt_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Pause:  # an unsupported rollback: the head release is suspended, not replaced
    event_id: str
    component: str
    release_id: str
    reasons: tuple[str, ...]
    operator_id: str
    step_up_receipt_id: str
    at: datetime


type Event = Install | Rollback | Pause


@dataclass(slots=True)
class _State:
    schema: int
    installs: dict[str, Install] = field(default_factory=dict)
    head: dict[str, str] = field(default_factory=dict)  # component -> release id
    withdrawn: set[str] = field(default_factory=set)
    paused: dict[str, Pause] = field(default_factory=dict)  # release id -> pause
    last_at: datetime | None = None
    step_ups: set[str] = field(default_factory=set)


class DeploymentLedger:
    """Append-only events of one deployment; construction replays them all."""

    def __init__(
        self, deployment: Deployment, keys: Mapping[str, TrustedKey],
        entries: tuple[Event, ...] = (),
    ) -> None:  # fmt: skip
        d = deployment
        _id(d.deployment_id, "deployment_id")
        if not isinstance(d.edition, Edition) or type(d.initial_schema) is not int:
            raise TypeError("typed edition and schema are required")
        self.deployment = d
        self.keys: Mapping[str, TrustedKey] = MappingProxyType(dict(keys))
        self.entries = tuple(entries)
        self._s = _State(d.initial_schema)
        for e in self.entries:
            self._apply(e)

    def _act(
        self, operator_id: str, step_up: str, at: datetime,
        used: frozenset[str] = frozenset(),
    ) -> datetime:  # fmt: skip
        if operator_id not in self.deployment.operators:
            raise ReleaseError("not_operator", operator_id)
        if _id(step_up, "step_up") in self._s.step_ups | used:
            raise ReleaseError("step_up_reused", step_up)
        at = ensure_aware_utc(at)
        if self._s.last_at is not None and at < self._s.last_at:
            raise ReleaseError("time_order", "events are appended in time order")
        return at

    def _apply(self, e: Event) -> None:
        s = self._s
        at = self._act(e.operator_id, e.step_up_receipt_id, e.at)
        if isinstance(e, Install):
            m = e.release.manifest
            if why := manifest_refusals(m):  # typed, before any state change
                raise ReleaseError(why[0], ",".join(why))
            if m.release_id in s.installs:
                raise ReleaseError("release_conflict", m.release_id)
            if m.approval.step_up_receipt_id in s.step_ups | {e.step_up_receipt_id}:
                raise ReleaseError("step_up_reused", e.event_id)
            if at < m.approval.approved_at:
                raise ReleaseError("time_order", "install precedes the approval")
            if e.operator_id == m.approval.principal_id:  # orchestrator decision
                raise ReleaseError("separation_of_duties", e.operator_id)
            if why := verify_release(e.release, self.keys, at,
                                     self.deployment.allows_test_keys):  # fmt: skip
                raise ReleaseError("unverified", ",".join(why))
            if not m.compatibility.runs_on(s.schema):
                raise ReleaseError("schema_incompatible", f"state schema {s.schema}")
            s.schema = max(s.schema, m.compatibility.writes_schema)
            s.installs[m.release_id] = e
            s.step_ups.add(m.approval.step_up_receipt_id)
            s.head[m.component] = m.release_id
        elif isinstance(e, Rollback):
            src, dst = s.installs.get(e.from_release_id), s.installs.get(
                e.to_release_id
            )  # fmt: skip
            if src is None or dst is None or s.head.get(e.component) != (
                e.from_release_id
            ) or dst.release.manifest.component != e.component:  # fmt: skip
                raise ReleaseError("rollback", e.event_id)
            if why := self._unusable(e.to_release_id, at):
                raise ReleaseError("rollback_target", ",".join(why))
            s.withdrawn.add(e.from_release_id)
            s.head[e.component] = e.to_release_id
        elif isinstance(e, Pause):
            if s.head.get(e.component) != e.release_id or not e.reasons:
                raise ReleaseError("pause", e.event_id)
            s.paused[e.release_id] = e
        else:
            raise ReleaseError("event", type(e).__name__)
        s.step_ups.add(e.step_up_receipt_id)
        s.last_at = at

    def _unusable(self, release_id: str, at: datetime) -> list[str]:
        s = self._s
        i = s.installs.get(release_id)
        if i is None:
            return ["not_installed"]
        out = []
        if release_id in s.withdrawn:
            out.append("withdrawn")
        if release_id in s.paused:
            out.extend(s.paused[release_id].reasons)
        if not i.release.manifest.compatibility.runs_on(s.schema):
            out.append(f"schema_incompatible:{s.schema}")
        out += verify_release(i.release, self.keys, at,
                              self.deployment.allows_test_keys)  # fmt: skip
        return out

    def _append(self, e: Event, strategies: Strategies) -> "DeploymentLedger":
        """Every act's step-up is unique, adoption step-ups included."""
        self._act(
            e.operator_id, e.step_up_receipt_id, e.at, _adoption_steps(strategies)
        )
        return DeploymentLedger(self.deployment, self.keys, (*self.entries, e))

    @property
    def schema(self) -> int:
        return self._s.schema

    @property
    def step_ups(self) -> frozenset[str]:
        return frozenset(self._s.step_ups)

    def head(self, component: str) -> str | None:
        return self._s.head.get(component)

    def install_of(self, release_id: str) -> Install:
        if (i := self._s.installs.get(release_id)) is None:
            raise ReleaseError("not_installed", release_id)
        return i

    def runnable(self, release_id: str, at: datetime) -> list[str]:
        """Reasons the release cannot run at `at`; empty when it can."""
        return self._unusable(release_id, ensure_aware_utc(at))

    def install(
        self, event_id: str, sr: SignedRelease, operator_id: str, step_up: str,
        at: datetime, *, strategies: Strategies,
    ) -> "DeploymentLedger":  # fmt: skip
        """Operator install. It never adopts anything for any tenant."""
        _id(event_id, "event_id")
        return self._append(Install(event_id, sr, operator_id, step_up, at), strategies)

    def install_receipt(self, release_id: str, receipt_id: str) -> PromotionReceipt:
        i = self.install_of(release_id)
        m = i.release.manifest
        prior = [e.release.manifest.release_id
                 for e in self.entries[: self.entries.index(i)]
                 if isinstance(e, Install) and e.release.manifest.component
                 == m.component
                 and e.release.manifest.release_id not in self._s.withdrawn
                 ]  # fmt: skip
        scope = authority_scope(ApprovalClass.OPERATOR_INSTALL, self.deployment.edition)
        return _receipt(receipt_id, i.release, i.operator_id, i.at, scope,
                        prior[-1] if prior else m.rollback_version_id,
                        i.release.signature_reference,
                        m.user_adoption_required)  # fmt: skip

    def rollback(
        self, event_id: str, component: str, operator_id: str, step_up: str,
        at: datetime, *, strategies: Strategies, to_release_id: str | None = None,
    ) -> "DeploymentLedger":  # fmt: skip
        """Withdraw the component's head for an earlier installed, verified and
        schema-compatible release. Without one, append a `Pause` with the reasons;
        the head is suspended and no record is changed. An explicit target that
        cannot run is refused instead."""
        _id(event_id, "event_id")
        at = ensure_aware_utc(at)
        head = self._s.head.get(component)
        if head is None:
            raise ReleaseError("nothing_installed", component)
        earlier = [e.release.manifest.release_id for e in self.entries
                   if isinstance(e, Install) and e.release.manifest.component
                   == component and e.release.manifest.release_id != head]  # fmt: skip
        if to_release_id is not None and (
            bad := ["not_earlier"] if to_release_id not in earlier
            else self._unusable(to_release_id, at)
        ):  # fmt: skip
            raise ReleaseError("rollback_target", ",".join(bad))
        options = [to_release_id] if to_release_id else earlier[::-1]
        why: list[str] = [] if options else ["no_prior_release"]
        for rid in options:
            if not (bad := self._unusable(rid, at)):
                return self._append(Rollback(event_id, component, head, rid,
                                             operator_id, step_up, at),
                                    strategies)  # fmt: skip
            why += [f"{rid}:{r}" for r in bad]
        return self._append(Pause(event_id, component, head, tuple(why),
                                  operator_id, step_up, at), strategies)  # fmt: skip


class TenantState(StrEnum):
    NOT_ADOPTED = "not_adopted"
    ACTIVE = "active"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class TenantStrategy:
    """What proposals may bind: the exact adopted version and receipt, if runnable."""

    state: TenantState
    version: str | None
    adoption_hash: str | None
    reasons: tuple[str, ...]


def _adoption_steps(strategies: Strategies) -> frozenset[str]:
    return frozenset(r.step_up_receipt_id for rs in strategies.adoptions.values()
                     for r in rs)  # fmt: skip


def _bound_release(
    ledger: DeploymentLedger, strategy_id: str, version: str, material: str
) -> str | None:
    for e in reversed(ledger.entries):
        if isinstance(e, Install):
            m = e.release.manifest
            if (m.strategy_id, m.strategy_version, m.strategy_material_hash) == (
                strategy_id, version, material
            ):  # fmt: skip
                return m.release_id
    return None


def _tenant(ledger: DeploymentLedger, tenant_id: str) -> None:
    if tenant_id not in ledger.deployment.tenants:
        raise ReleaseError("tenant_not_in_deployment", tenant_id)


def strategy_state(
    ledger: DeploymentLedger, strategies: Strategies, tenant_id: str,
    strategy_id: str, at: datetime,
) -> TenantStrategy:  # fmt: skip
    """The tenant's latest adoption known at `at` and whether its release can run.
    A maintainer signature or install never changes it; nothing is substituted."""
    _tenant(ledger, tenant_id)
    at = ensure_aware_utc(at)
    if ledger.entries and at < ledger.entries[-1].at:
        raise ReleaseError("time_order", "the ledger is read as of its last event")
    known = [r for r in strategies.adoptions.get((tenant_id, strategy_id), ())
             if r.signed_at <= at]  # fmt: skip
    if not known:
        return TenantStrategy(TenantState.NOT_ADOPTED, None, None, ())
    r = known[-1]
    rid = _bound_release(ledger, strategy_id, r.version, r.manifest_hash)
    why = ["adopted_version_not_installed"] if rid is None else ledger.runnable(rid, at)
    state = TenantState.PAUSED if why else TenantState.ACTIVE
    return TenantStrategy(state, r.version, r.receipt_hash, tuple(why))


def adopt_release(
    ledger: DeploymentLedger, strategies: Strategies, tenant_id: str,
    release_id: str, config: Mapping[str, object], consent: StrategyConsent,
    receipt_id: str,
) -> tuple[Strategies, PromotionReceipt]:  # fmt: skip
    """The tenant's explicit adoption of an installed, runnable strategy release."""
    _tenant(ledger, tenant_id)
    i = ledger.install_of(release_id)
    m, at = i.release.manifest, ensure_aware_utc(consent.signed_at)
    sid = m.strategy_id
    if sid is None or m.strategy_version is None or m.strategy_material_hash is None:
        raise ReleaseError("not_a_strategy_release", release_id)
    if why := ledger.runnable(release_id, at):
        raise ReleaseError("not_runnable", ",".join(why))
    if at < i.at:
        raise ReleaseError("time_order", "adoption precedes the install")
    used = _adoption_steps(strategies)
    if consent.step_up_receipt_id in ledger.step_ups | used | {
        m.approval.step_up_receipt_id
    }:  # fmt: skip
        raise ReleaseError("step_up_reused", consent.step_up_receipt_id)
    if strategies.manifest(sid, m.strategy_version).material_hash != (
        m.strategy_material_hash
    ):  # fmt: skip
        raise ReleaseError("strategy_binding", release_id)
    prior = strategies.adoptions.get((tenant_id, sid), ())
    out = strategies.adopt(tenant_id, sid, m.strategy_version, config, consent)
    r = out.adoptions[(tenant_id, sid)][-1]
    return out, _receipt(
        receipt_id, i.release, r.principal_id, at,
        AuthorityScope.USER_STRATEGY_ADOPTION,
        prior[-1].version if prior else m.rollback_version_id,
        f"adoption.{r.receipt_hash}", False,
    )  # fmt: skip
