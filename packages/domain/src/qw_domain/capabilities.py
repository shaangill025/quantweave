"""Capability read model for `GET /capabilities` (T021 increment 3; spec §1 "Free
baseline and qualified capability profiles", §6 capability registry, §16 status
vocabulary; planning/qualification_register QUAL01-QUAL24).

Feature presence, operational qualification and eligibility are separate (§1), so a
feature is enabled only when every gate passes, and each failing gate is a typed
reason. Nothing is enabled by a constant: every feature has at least one gate.
- Rights gate: one feed must allow every use the feature needs for this tenant at
  `at` (`rights.check_use`: qualified feed, active entitlement, use, scope and
  jurisdiction grants). A self-hosted installation is evaluated in personal scope
  and the operator's configured jurisdiction. Hosted tenants' residence is not
  stored, so on a hosted deployment no rights gate can pass (`jurisdiction_unknown`).
- Unrecorded gates: qualifications with no runtime record yet (model routes T006,
  model-assisted evidence QUAL20, evaluator independence, sandbox T007, independent
  assessment, human promotion and experiment budget QUAL21, read-only connections,
  strategy qualification, the options feed and the options opt-in, which the
  adopted policy holds but this read does not resolve).
  They never pass until a store records them.
- Hosted investment output also needs commercial clearance (§16; QUAL18, QUAL19).
The feature list is project-proposed: the spec names capabilities but no canonical
feature-id list (docs/receipts/T021.md, increment 3). Stdlib only.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from qw_domain.instants import ensure_aware_utc
from qw_domain.release import Edition
from qw_domain.rights import Registry, Use, UseScope, check_use


class Reason(StrEnum):
    RIGHTS = "provider_rights_unqualified"  # T004/T021 rights registry
    JURISDICTION = "jurisdiction_unknown"
    MODEL_ROUTE = "model_route_unqualified"  # T006; QUAL11-QUAL14
    EVIDENCE = "evaluation_evidence_unqualified"  # QUAL20 model-assisted evidence
    INDEPENDENCE = "evaluator_independence_unqualified"  # §1, §10 separate evaluator
    SANDBOX = "sandbox_unqualified"  # T007; QUAL17
    ASSESSMENT = "independent_assessment_unqualified"  # §11; QUAL21
    PROMOTION = "human_promotion_unqualified"  # §11 "human promotes"; QUAL21
    BUDGET = "experiment_budget_unqualified"  # QUAL21 USD20 experiment budget
    CONNECTION = "connection_unqualified"  # T016; QUAL05-QUAL07
    STRATEGY = "strategy_unqualified"  # T027-T033; QUAL15, QUAL16
    OPTION_FEED = "option_feed_unqualified"  # QUAL08
    OPT_IN = "opt_in_required"  # §9 options are opt-in
    CLEARANCE = "commercial_clearance_pending"  # §16; QUAL18, QUAL19


@dataclass(frozen=True, slots=True)
class Feature:
    id: str
    uses: frozenset[Use]  # all on one qualified feed
    unrecorded: frozenset[Reason]
    investment_output: bool


@dataclass(frozen=True, slots=True)
class FeatureState:
    feature_id: str
    enabled: bool
    reason_codes: tuple[str, ...]


_R, _U = Reason, Use
_SHOW, _MODEL = frozenset({_U.DISPLAY}), frozenset({_U.MODEL_PROCESSING})
_ADVICE = frozenset({_U.DISPLAY, _U.FINANCIAL_RECOMMENDATION})
_NONE: frozenset[Use] = frozenset()
_AI = frozenset({_R.MODEL_ROUTE, _R.EVIDENCE})
_IMPROVE = frozenset(
    {_R.MODEL_ROUTE, _R.SANDBOX, _R.ASSESSMENT, _R.PROMOTION, _R.BUDGET}
)
FEATURES: tuple[Feature, ...] = (
    Feature("provider_data_display", _SHOW, frozenset(), False),
    Feature("strategy_proposals", _ADVICE, frozenset({_R.STRATEGY}), True),
    Feature("intraday_signals", _ADVICE, frozenset({_R.STRATEGY}), True),
    Feature("options", _ADVICE,
            frozenset({_R.OPT_IN, _R.OPTION_FEED, _R.STRATEGY}), True),
    Feature("ai_research", _MODEL, _AI, True),
    Feature("ai_evaluator", _MODEL, _AI | {_R.INDEPENDENCE}, True),
    Feature("governed_chat", _MODEL, _AI, True),
    Feature("broker_connections", _NONE, frozenset({_R.CONNECTION}), False),
    Feature("code_improvement", _NONE, _IMPROVE, False),
    Feature("optimizer_recursion", _NONE, _IMPROVE, False),
)  # fmt: skip


def _rights_pass(
    registry: Registry, tenant_id: str, uses: frozenset[Use], at: datetime, region: str
) -> bool:
    def allowed(feed_id: str, use: Use) -> bool:
        decision = check_use(registry, tenant_id, feed_id, use, at,
                             scope=UseScope.PERSONAL, jurisdiction=region)  # fmt: skip
        return decision.allowed

    return any(all(allowed(f, u) for u in uses) for f in sorted(registry.feeds))


def evaluate(
    registry: Registry,
    tenant_id: str,
    at: datetime,
    edition: Edition,
    jurisdiction: str | None,
) -> tuple[FeatureState, ...]:
    """Every feature for `tenant_id` at `at` (the time of use, from the server
    clock), sorted by id. `jurisdiction` is a self-hosted operator's; a hosted
    deployment must pass None."""
    if not isinstance(edition, Edition):  # a plain "hosted" string is not one
        raise ValueError("deployment must be an edition")
    at = ensure_aware_utc(at)
    if edition is Edition.HOSTED and jurisdiction is not None:
        raise ValueError("a hosted deployment has no installation jurisdiction")
    states = []
    for f in FEATURES:
        reasons = set(f.unrecorded)
        if f.uses:
            if jurisdiction is None:
                reasons |= {_R.JURISDICTION, _R.RIGHTS}
            elif not _rights_pass(registry, tenant_id, f.uses, at, jurisdiction):
                reasons.add(_R.RIGHTS)
        if f.investment_output and edition is Edition.HOSTED:
            reasons.add(_R.CLEARANCE)
        codes = tuple(sorted(r.value for r in reasons))
        states.append(FeatureState(f.id, not codes, codes))
    return tuple(sorted(states, key=lambda s: s.feature_id))
