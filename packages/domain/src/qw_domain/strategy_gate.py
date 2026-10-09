"""Research-only eligibility and the actionable gate for one strategy configuration.

T027; spec §7 eligibility ordering and typed reasons, §8 three-axis qualification and
"Limited-evidence action is permitted only where predeclared family policy explicitly
allows it", §11/§16 user strategy adoption, R050 regular-session intraday.
- Both gates fail closed and return every applicable reason, sorted in the §7 order
  (jurisdiction, feature permission, instrument/catalogue, qualification, adoption,
  source rights). A missing input is a reason, never a default allow.
- `research_eligible` needs a valid configuration, code that is not failed or
  suspended, rights for the manifest's declared uses (plus model processing and the AI
  mode for ai_thesis). `actionable` adds every other check, so it implies research
  eligibility and never the reverse.
- Evidence below `qualified_for_declared_claim` is limited evidence: actionable only
  when the family policy in force at `at` permits it and is the policy the evidence
  was assessed under. A policy approved after `at` is not yet known. Synthetic-only
  evidence is never actionable.
- Every fact is read as known at `at`: axis steps, adoption receipts, investment policy
  receipts, family policies, rights and evidence records.
- LIMITATIONS: evidence records are a caller-supplied index (no evidence store yet,
  T036); capabilities are caller-asserted facts from their own modules; per-proposal
  checks (session open, freshness, risk, pauses, option contract terms) belong to the
  proposal gate; release approval before adoption (R034) is not modelled here.
Stdlib only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from qw_domain.instants import ensure_aware_utc
from qw_domain.policy import PolicyHistory, PolicyVersion
from qw_domain.rights import DenyCode, Registry, Use, UseScope, check_use
from qw_domain.strategy_registry import (
    AdoptionState,
    AssetClass,
    Axis,
    AxisStep,
    Family,
    Horizon,
    StrategyError,
    StrategyManifest,
    StrategyRegistry,
    qual_key,
    status,
)
from qw_domain.strategy_registry import EligibilityState as _U
from qw_domain.strategy_registry import EvidenceState as _V
from qw_domain.strategy_registry import OperationalState as _O


class GateCode(StrEnum):  # declaration order is the §7 evaluation order
    STRATEGY_UNKNOWN = "strategy_unknown"
    CONFIG_INVALID = "config_invalid"
    JURISDICTION_NOT_PERMITTED = "jurisdiction_not_permitted"
    POLICY_NOT_ADOPTED = "policy_not_adopted"
    POLICY_SYNTHETIC = "policy_synthetic"
    AI_MODE_NOT_PERMITTED = "ai_mode_not_permitted"
    ASSET_CLASS_NOT_PERMITTED = "asset_class_not_permitted"
    HORIZON_NOT_PERMITTED = "horizon_not_permitted"
    SESSION_NOT_PERMITTED = "session_not_permitted"
    CAPABILITY_MISSING = "capability_missing"
    OPERATIONAL_NOT_PASSED = "operational_not_passed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_SYNTHETIC_ONLY = "evidence_synthetic_only"
    FAMILY_POLICY_SUPERSEDED = "family_policy_superseded"
    LIMITED_EVIDENCE_NOT_PERMITTED = "limited_evidence_not_permitted"
    USER_NOT_ELIGIBLE = "user_not_eligible"
    EVIDENCE_UNVERIFIED = "evidence_unverified"
    STRATEGY_NOT_ADOPTED = "strategy_not_adopted"
    ADOPTION_STALE = "adoption_stale"
    FEED_UNQUALIFIED = "feed_unqualified"
    SOURCE_RIGHTS_DENIED = "source_rights_denied"


_ORDER = {code: i for i, code in enumerate(GateCode)}


class Capability(StrEnum):
    ADOPTED_TARGETS = "adopted_targets"
    POINT_IN_TIME_FUNDAMENTALS = "point_in_time_fundamentals"
    EXCHANGE_CALENDAR = "exchange_calendar"
    INDEPENDENT_EVALUATOR = "independent_evaluator"
    OPTION_TERMS = "option_terms"  # T034 contract terms and lifecycle


_C = Capability
FAMILY_CAPABILITIES: Mapping[Family, frozenset[Capability]] = {
    Family.ALLOCATION: frozenset({_C.ADOPTED_TARGETS}),
    Family.QUALITY_VALUE: frozenset({_C.POINT_IN_TIME_FUNDAMENTALS}),
    Family.INCOME: frozenset({_C.POINT_IN_TIME_FUNDAMENTALS}),
    Family.SWING_MOMENTUM: frozenset({_C.EXCHANGE_CALENDAR}),
    Family.OPENING_RANGE_BREAKOUT: frozenset({_C.EXCHANGE_CALENDAR}),
    Family.AI_THESIS: frozenset({_C.INDEPENDENT_EVALUATOR}),
}


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """A stored evidence record as the gate needs it: what it is bound to."""

    ref_id: str
    material_hash: str
    config_hash: str | None  # None for operational evidence
    tenant_id: str | None  # set only for eligibility evidence
    synthetic: bool
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class GateInputs:
    rights: Registry
    scope: UseScope
    jurisdiction: str
    policy: PolicyHistory | None  # the tenant's adopted investment policy history
    evidence: Mapping[str, EvidenceRecord]
    capabilities: frozenset[Capability]  # available at `at` for this tenant


@dataclass(frozen=True, slots=True)
class GateReason:
    code: GateCode
    subject: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reasons: tuple[GateReason, ...]
    manifest_hash: str | None
    config_hash: str | None
    adoption_hash: str | None
    policy_hash: str | None


def _policy_at(
    history: PolicyHistory | None, tenant_id: str, at: datetime
) -> tuple[PolicyVersion, str] | str:
    """The investment policy adopted at `at`, with its receipt hash, or why not."""
    if history is None or history.tenant_id != tenant_id:
        return "none"  # another tenant's policy reveals nothing
    known = [r for r in history.receipts if r.signed_at <= at]
    if not known:
        return "none"
    r = max(reversed(known), key=lambda x: x.signed_at)  # latest signed, as status
    if not 1 <= r.version <= len(history.versions):
        return "receipt_mismatch"
    v = history.versions[r.version - 1]
    if (
        r.tenant_id != tenant_id
        or r.policy_hash != v.content_hash
        or v.draft.content_hash != v.content_hash
    ):
        return "receipt_mismatch"
    return v, r.receipt_hash


def _field(v: PolicyVersion, name: str) -> tuple[str, ...]:
    for f in v.draft.fields:
        if f.name == name:
            return (f.value,) if isinstance(f.value, str) else f.value
    return ()


def _verify(
    step: AxisStep | None, key: tuple[str, ...], index: Mapping[str, EvidenceRecord]
) -> list[str]:
    """Refs of `step` whose stored record is missing or bound to something else."""
    bad: list[str] = []
    for e in step.evidence if step else ():
        r = index.get(e.ref_id)
        bound = (
            r is not None
            and r.ref_id == e.ref_id
            and tuple(p for p in (r.material_hash, r.config_hash, r.tenant_id) if p)
            == key[1:]
            and r.synthetic is e.synthetic
            and ensure_aware_utc(r.recorded_at) == e.recorded_at
        )
        if not bound:
            bad.append(f"{key[0]}:{e.ref_id}")
    return bad


def _evaluate(
    reg: StrategyRegistry,
    tenant_id: str,
    strategy_id: str,
    version: str,
    config: Mapping[str, object],
    at: datetime,
    ins: GateInputs,
    act: bool,
) -> GateDecision:
    at = ensure_aware_utc(at)
    reasons: list[GateReason] = []

    def deny(code: GateCode, subject: str = "") -> None:
        reasons.append(GateReason(code, subject))

    def done(*hashes: str | None) -> GateDecision:
        reasons.sort(key=lambda r: _ORDER[r.code])
        return GateDecision(not reasons, tuple(reasons), *hashes)

    try:
        m: StrategyManifest = reg.manifest(strategy_id, version)
    except StrategyError:
        deny(GateCode.STRATEGY_UNKNOWN, f"{strategy_id}@{version}")
        return done(None, None, None, None)
    try:
        cfg = m.config_hash(config)
    except StrategyError as exc:
        deny(GateCode.CONFIG_INVALID, str(exc))
        return done(m.material_hash, None, None, None)
    s = status(reg, tenant_id, strategy_id, version, config, at)
    ai = m.family is Family.AI_THESIS

    # Feature permission and instrument/catalogue eligibility (investment policy).
    adopted = _policy_at(ins.policy, tenant_id, at) if act or ai else None
    policy_hash = None
    if isinstance(adopted, str):
        deny(GateCode.POLICY_NOT_ADOPTED, adopted)
    elif adopted is not None:
        pv, policy_hash = adopted
        if pv.draft.synthetic:
            deny(GateCode.POLICY_SYNTHETIC, pv.policy_id)
        if ai and _field(pv, "ai_mode") != ("ai_enabled",):
            deny(GateCode.AI_MODE_NOT_PERMITTED, "ai_mode")
        if act:
            allowed = set(_field(pv, "instruments"))
            excluded = {e.subject for e in pv.draft.exclusions}
            # policy.build_draft excludes `option_proposals:<account>` when that
            # account's option permission is unknown or denied.
            accounts = {a.scope.account_id for a in pv.draft.allocations}
            permitted = {a for a in accounts if f"option_proposals:{a}" not in excluded}
            no_option_account = not permitted or "new_option_ideas" in excluded
            for a in sorted(m.asset_classes):
                if a.value not in allowed or (
                    a is AssetClass.OPTIONS and no_option_account
                ):
                    deny(GateCode.ASSET_CLASS_NOT_PERMITTED, a.value)
            if m.horizon.value not in _field(pv, "horizons"):
                deny(GateCode.HORIZON_NOT_PERMITTED, m.horizon.value)
    if act:
        if m.horizon is Horizon.INTRADAY and not m.regular_session_only:
            deny(GateCode.SESSION_NOT_PERMITTED, "intraday is regular-session only")
        needed = set(FAMILY_CAPABILITIES[m.family])
        if AssetClass.OPTIONS in m.asset_classes:
            needed.add(Capability.OPTION_TERMS)
        for c in sorted(needed - ins.capabilities):
            deny(GateCode.CAPABILITY_MISSING, c.value)

    # Strategy/version qualification on all three axes.
    if s.operational is not _O.PASSED and (act or s.operational is not _O.NOT_TESTED):
        deny(GateCode.OPERATIONAL_NOT_PASSED, s.operational.value)
    ev_key = qual_key(Axis.EVIDENCE, m, cfg)
    if act:
        ev = reg.step_at(ev_key, at)
        if ev is None or s.evidence is _V.NONE:
            deny(GateCode.EVIDENCE_INSUFFICIENT, s.evidence.value)
        else:
            current = reg.family_policy_at(m.family, at)
            if s.evidence_synthetic_only:
                deny(GateCode.EVIDENCE_SYNTHETIC_ONLY, s.evidence.value)
            if current is None or current.content_hash != ev.family_policy_hash:
                deny(GateCode.FAMILY_POLICY_SUPERSEDED, m.family.value)
            elif s.evidence is not _V.QUALIFIED_FOR_DECLARED_CLAIM and not (
                current.permits_limited_evidence
            ):
                deny(GateCode.LIMITED_EVIDENCE_NOT_PERMITTED, s.evidence.value)
        if s.eligibility is not _U.ELIGIBLE:
            deny(GateCode.USER_NOT_ELIGIBLE, s.eligibility.value)
    keys = [qual_key(Axis.OPERATIONAL, m)]
    if act:
        keys += [ev_key, qual_key(Axis.ELIGIBILITY, m, cfg, tenant_id)]
    for key in keys:
        for ref in _verify(reg.step_at(key, at), key, ins.evidence):
            deny(GateCode.EVIDENCE_UNVERIFIED, ref)

    # User adoption bound to this material and configuration hash.
    if act and s.adoption is AdoptionState.NONE:
        deny(GateCode.STRATEGY_NOT_ADOPTED, strategy_id)
    elif act and s.adoption is AdoptionState.STALE:
        deny(GateCode.ADOPTION_STALE, "material or configuration changed")

    # Source rights for every data need and intended use.
    extra = {Use.MODEL_PROCESSING} if ai else set()
    if act:
        extra.add(Use.FINANCIAL_RECOMMENDATION)
    for need in m.data:
        for use in sorted(need.uses | extra):
            keep = need.retain_for if use is Use.RETENTION else None
            d = check_use(ins.rights, tenant_id, need.feed_id, use, at,
                          scope=ins.scope, jurisdiction=ins.jurisdiction,
                          retain_for=keep)  # fmt: skip
            for r in d.reasons:
                code = (
                    GateCode.FEED_UNQUALIFIED
                    if r.code in (DenyCode.NOT_QUALIFIED, DenyCode.RETIRED)
                    else GateCode.JURISDICTION_NOT_PERMITTED
                    if ":jurisdiction:" in r.subject
                    else GateCode.SOURCE_RIGHTS_DENIED
                )
                deny(code, f"{need.feed_id}:{use.value}:{r.code.value}")
    adoption = s.adoption_hash if act else None
    return done(m.material_hash, cfg, adoption, policy_hash)


def research_eligible(
    reg: StrategyRegistry,
    tenant_id: str,
    strategy_id: str,
    version: str,
    config: Mapping[str, object],
    at: datetime,
    inputs: GateInputs,
) -> GateDecision:
    """May this configuration run for research-only output for `tenant_id` at `at`?
    Not a user-eligibility or action decision (§8)."""
    return _evaluate(reg, tenant_id, strategy_id, version, config, at, inputs, False)


def actionable(
    reg: StrategyRegistry,
    tenant_id: str,
    strategy_id: str,
    version: str,
    config: Mapping[str, object],
    at: datetime,
    inputs: GateInputs,
) -> GateDecision:
    """May this configuration produce actionable proposals for `tenant_id` at `at`?
    `at` is the server-clock decision time. Proposal-level gates still follow."""
    return _evaluate(reg, tenant_id, strategy_id, version, config, at, inputs, True)
