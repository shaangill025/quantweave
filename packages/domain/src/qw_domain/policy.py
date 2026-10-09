"""Draft policy inference, versioned policies, adoption receipts and the sizing gate.

T019; spec §2 contradiction rules, §7 "Policy as a versioned contract", T008 C-20.
- Every inferred field names the responses that support it. Risk limits come only
  from the user's own ONB14 entries: nothing (no template, no loss-capacity answer)
  fills them, so a draft without entries has no limits.
- Contradictions are visible `Conflict`s, never averaged. A blocking one requires the
  answers to change; the others must be acknowledged at adoption.
- A semantic change appends a policy version; an adopted version is never edited.
- Adoption is a typed `AdoptionReceipt` (C-20 `policy_adoption`). Acknowledgement text
  is recorded for the UI and never authorises. Signing (key id, algorithm, signature)
  belongs to the adapter that holds the key; it signs `receipt_hash`.
- `check_sizing` fails closed with typed reasons. SYNTHETIC policies never authorise.
Stdlib only.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from qw_domain.decimals import Ratio
from qw_domain.instants import ensure_aware_utc, format_instant, require_date
from qw_domain.onboarding import (
    NOT_SURE,
    Allocation,
    Limit,
    LimitMetric,
    Responses,
    SizingScope,
)
from qw_domain.sources import ID_PATTERN

# Required before live sizing: config/risk_policy_draft.json `limits`.
REQUIRED_METRICS = frozenset(LimitMetric) - {
    LimitMetric.POSITION_CONCENTRATION,
    LimitMetric.DRAWDOWN,
}
NEAR_TERM = timedelta(days=365)  # ONB05 "<1y" bucket


class PolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ConflictCode(StrEnum):
    PRESERVATION_VS_INTRADAY = "preservation_near_term_need_vs_intraday"
    PRESERVATION_NEED_UNKNOWN = "preservation_need_unknown_vs_intraday"
    MONITORING_EXCLUDES_INTRADAY = "insufficient_monitoring_for_intraday"
    OPTIONS_OFF_EXISTING_SHORT = "options_off_existing_short_option"
    OPTION_PERMISSION_UNKNOWN = "account_option_permission_unknown"
    OPTION_PERMISSION_DENIED = "account_option_permission_denied"  # exclusion only
    PROFIT_GOAL_INFEASIBLE = "profit_goal_infeasible"
    PROFIT_GOAL_UNASSESSED = "profit_goal_feasibility_unassessed"


_PRESERVE = ConflictCode.PRESERVATION_VS_INTRADAY
_MONITOR = ConflictCode.MONITORING_EXCLUDES_INTRADAY
_DETAIL = {
    _PRESERVE: "capital preservation with a near-term essential withdrawal conflicts"
    " with aggressive intraday trading; change the answers to resolve",
    _MONITOR: "same-day strategies need regular-session availability",
    ConflictCode.PRESERVATION_NEED_UNKNOWN: "preservation with intraday trading while"
    " the withdrawal need or loss capacity is not_sure; intraday stays excluded",
    ConflictCode.OPTIONS_OFF_EXISTING_SHORT: "options are off but a short option is"
    " held: no new option ideas; risk and expiry warnings continue",
    ConflictCode.OPTION_PERMISSION_UNKNOWN: "account option permission is unknown",
    ConflictCode.PROFIT_GOAL_INFEASIBLE: "target return exceeds the reviewed"
    " feasibility method; constraints are kept, not optimised away",
    ConflictCode.PROFIT_GOAL_UNASSESSED: "no reviewed feasibility method configured",
}


@dataclass(frozen=True, slots=True)
class Conflict:
    code: ConflictCode
    sources: tuple[str, ...]  # response ids and account facts
    blocking: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Exclusion:
    subject: str
    reason: ConflictCode


@dataclass(frozen=True, slots=True)
class InferredField:
    name: str
    value: str | tuple[str, ...]
    sources: tuple[str, ...]


class OptionPermission(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """Reconciled account state the questionnaire cannot answer."""

    account_id: str
    option_permission: OptionPermission
    has_short_option: bool


@dataclass(frozen=True, slots=True)
class FeasibilityBound:
    """A reviewed, versioned research choice; this module sets no number itself."""

    method_id: str
    max_supported_return: Ratio


@dataclass(frozen=True, slots=True)
class DraftPolicy:
    tenant_id: str
    synthetic: bool
    response_schema_version: str
    fields: tuple[InferredField, ...]
    limits: tuple[Limit, ...]
    allocations: tuple[Allocation, ...]
    conflicts: tuple[Conflict, ...]
    exclusions: tuple[Exclusion, ...]
    retained_warnings: tuple[str, ...]

    @property
    def label(self) -> str:
        return "SYNTHETIC" if self.synthetic else "LIVE"

    @property
    def content_hash(self) -> str:
        return _hash(
            {
                "tenant_id": self.tenant_id,
                "label": self.label,
                "schema": self.response_schema_version,
                "fields": [[f.name, f.value, f.sources] for f in self.fields],
                "limits": _canonical_limits(self.limits),
                "allocations": sorted(
                    [str(a.scope), a.amount.to_wire()] for a in self.allocations
                ),
                "conflicts": [[c.code, c.sources, c.blocking] for c in self.conflicts],
                "exclusions": [[e.subject, e.reason] for e in self.exclusions],
                "warnings": self.retained_warnings,
            }
        )


def _canonical_limits(limits: tuple[Limit, ...]) -> list[dict[str, str | None]]:
    return sorted(
        (x.to_wire() for x in limits), key=lambda w: json.dumps(w, sort_keys=True)
    )


def _hash(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def build_draft(
    tenant_id: str,
    responses: Responses,
    facts: Sequence[AccountFacts],
    *,
    as_of: date,
    feasibility: FeasibilityBound | None = None,
    synthetic: bool = False,
) -> DraftPolicy:
    as_of = require_date(as_of, "as_of")
    r = responses
    objectives = r.selected("ONB04")
    instruments = r.selected("ONB11")
    horizons = r.selected("ONB12")
    conflicts: list[Conflict] = []
    exclusions: list[Exclusion] = []
    warnings: list[str] = []

    def flag(code: ConflictCode, *sources: str, exclude: str = "") -> None:
        blocking = code is ConflictCode.PRESERVATION_VS_INTRADAY
        conflicts.append(Conflict(code, sources, blocking, _DETAIL[code]))
        if exclude:
            exclusions.append(Exclusion(exclude, code))

    need = r.dated_need("ONB06.need")
    near = r.choice("ONB05") == "lt_1y" or (
        need is not None and need.on - as_of <= NEAR_TERM
    )
    unsure = [q for q in ("ONB06", "ONB07") if r.choice(q) == NOT_SURE]
    if (
        "capital_preservation" in objectives
        and r.choice("ONB06") in ("known_dated_need", NOT_SURE)
        and r.choice("ONB07") in ("essential_spending_affected", NOT_SURE)
        and (near or "ONB06" in unsure)
        and ("same_day" in horizons or "active_trading" in objectives)
    ):
        if not unsure:
            flag(_PRESERVE, "ONB04", "ONB05", "ONB06", "ONB06.need", "ONB07", "ONB12")
        else:  # fail closed: unknown answers could complete the blocking rule
            unknown = ConflictCode.PRESERVATION_NEED_UNKNOWN
            flag(unknown, *unsure, "ONB04", "ONB12", exclude="horizon:intraday")
    if "same_day" in horizons and r.choice("ONB10") != "regular_session":
        flag(_MONITOR, "ONB10", "ONB12", exclude="horizon:intraday")
    known = {f.account_id: f for f in facts}
    accounts = sorted(known.keys() | {a.scope.account_id for a in r.allocations()})
    for acct in accounts:  # missing account state counts as unknown permission
        fact = known.get(acct, AccountFacts(acct, OptionPermission.UNKNOWN, False))
        if "options" not in instruments and fact.has_short_option:
            flag(ConflictCode.OPTIONS_OFF_EXISTING_SHORT, "ONB11", f"account:{acct}")
            warnings.append(f"option_risk_and_expiry:{acct}")
        if "options" not in instruments:
            continue
        if fact.option_permission is OptionPermission.UNKNOWN:
            code = ConflictCode.OPTION_PERMISSION_UNKNOWN
            flag(code, "ONB11", f"account:{acct}", exclude=f"option_proposals:{acct}")
        elif fact.option_permission is OptionPermission.DENIED:
            denied = ConflictCode.OPTION_PERMISSION_DENIED
            exclusions.append(Exclusion(f"option_proposals:{acct}", denied))
    if warnings:
        exclusions.append(
            Exclusion("new_option_ideas", ConflictCode.OPTIONS_OFF_EXISTING_SHORT)
        )
    if (target := r.ratio("ONB04.target_return")) is not None:
        if feasibility is None:
            flag(ConflictCode.PROFIT_GOAL_UNASSESSED, "ONB04.target_return")
        elif target > feasibility.max_supported_return:
            flag(
                ConflictCode.PROFIT_GOAL_INFEASIBLE,
                "ONB04.target_return",
                feasibility.method_id,
            )

    excluded = {e.subject for e in exclusions}
    kept = tuple(
        "intraday" if h == "same_day" else h
        for h in horizons
        if not (h == "same_day" and "horizon:intraday" in excluded)
    )
    fields = [
        InferredField("scope", r.choice("ONB01") or "", ("ONB01",)),
        InferredField("reporting_currency", r.choice("ONB03") or "", ("ONB03",)),
        InferredField("objectives", objectives, ("ONB04",)),
        InferredField("horizon", r.choice("ONB05") or "", ("ONB05",)),
        InferredField("loss_capacity", r.choice("ONB07") or "", ("ONB07",)),
        InferredField("instruments", instruments, ("ONB11",)),
        InferredField("horizons", kept, ("ONB12", "ONB10")),
        InferredField("ai_mode", r.choice("ONB16") or "", ("ONB16",)),
    ]
    return DraftPolicy(
        tenant_id=tenant_id,
        synthetic=synthetic or r.choice("ONB01") == "hypothetical_only",
        response_schema_version=r.schema_version,
        fields=tuple(f for f in fields if f.value),
        limits=r.limits(),  # the user's own entries, verbatim; never a template
        allocations=r.allocations(),
        conflicts=tuple(conflicts),
        exclusions=tuple(exclusions),
        retained_warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    policy_id: str
    version: int
    draft: DraftPolicy
    content_hash: str


@dataclass(frozen=True, slots=True)
class AdoptionConsent:
    """The explicit adoption act. Accepting planning recommendations is not one."""

    principal_id: str
    step_up_receipt_id: str
    signed_at: datetime
    acknowledged: frozenset[ConflictCode]
    acknowledgement_text: str  # recorded for UX; never authorises


@dataclass(frozen=True, slots=True)
class AdoptionReceipt:
    kind: str
    tenant_id: str
    policy_id: str
    version: int
    policy_hash: str
    label: str
    principal_id: str
    step_up_receipt_id: str
    signed_at: datetime
    limits: tuple[Limit, ...]
    acknowledged: tuple[ConflictCode, ...]
    exclusions: tuple[Exclusion, ...]
    acknowledgement_text: str

    def to_wire(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "policy_id": self.policy_id,
            "version": self.version,
            "policy_hash": self.policy_hash,
            "label": self.label,
            "principal_id": self.principal_id,
            "step_up_receipt_id": self.step_up_receipt_id,
            "signed_at": format_instant(self.signed_at),
            "limits": _canonical_limits(self.limits),
            "acknowledged": [c.value for c in self.acknowledged],
            "exclusions": [[e.subject, e.reason.value] for e in self.exclusions],
            "acknowledgement_text": self.acknowledgement_text,
        }

    @property
    def receipt_hash(self) -> str:
        return _hash(self.to_wire())


@dataclass(frozen=True, slots=True)
class PolicyHistory:
    """Append-only versions and receipts; status is derived, never stored."""

    policy_id: str
    tenant_id: str
    versions: tuple[PolicyVersion, ...] = ()
    receipts: tuple[AdoptionReceipt, ...] = field(default=())

    @classmethod
    def new(cls, policy_id: str, tenant_id: str) -> "PolicyHistory":
        for value in (policy_id, tenant_id):
            if ID_PATTERN.fullmatch(value) is None:
                raise PolicyError("id", f"{value!r} is malformed")
        return cls(policy_id, tenant_id)

    def propose(self, draft: DraftPolicy) -> "PolicyHistory":
        """Append a version when the semantics changed; otherwise return self."""
        if draft.tenant_id != self.tenant_id:
            raise PolicyError("tenant", "draft belongs to another tenant")
        digest = draft.content_hash
        if self.versions and self.versions[-1].content_hash == digest:
            return self
        n = len(self.versions) + 1
        version = PolicyVersion(self.policy_id, n, draft, digest)
        return PolicyHistory(
            self.policy_id, self.tenant_id, (*self.versions, version), self.receipts
        )

    @property
    def adopted(self) -> tuple[PolicyVersion, AdoptionReceipt] | None:
        if not self.receipts:
            return None
        receipt = self.receipts[-1]
        return self.versions[receipt.version - 1], receipt

    def status(self, version: int) -> str:
        current = self.adopted
        if current is not None and current[0].version == version:
            return "adopted"
        if current is not None and version < current[0].version:
            return "superseded"
        return "awaiting_adoption"

    def adopt(self, version: int, consent: AdoptionConsent) -> "PolicyHistory":
        if not self.versions or version != self.versions[-1].version:
            raise PolicyError("not_latest", f"version {version} is not the latest")
        if self.status(version) == "adopted":
            raise PolicyError("already_adopted", f"version {version}")
        if type(consent) is not AdoptionConsent or not all(
            isinstance(v, str) and ID_PATTERN.fullmatch(v)
            for v in (consent.principal_id, consent.step_up_receipt_id)
        ):
            raise PolicyError("consent", "a principal and step-up receipt are required")
        text = consent.acknowledgement_text
        if not isinstance(text, str):
            raise PolicyError("consent", "acknowledgement text must be a string")
        try:
            text.encode()
        except UnicodeEncodeError:
            raise PolicyError("consent", "acknowledgement text is not UTF-8") from None
        target = self.versions[-1]
        draft = target.draft
        if blocking := [c.code.value for c in draft.conflicts if c.blocking]:
            raise PolicyError("conflict_requires_resolution", ", ".join(blocking))
        open_codes = {c.code for c in draft.conflicts if not c.blocking}
        if consent.acknowledged - open_codes:
            raise PolicyError(
                "unknown_acknowledgement", "acknowledged an absent conflict"
            )
        if missing := open_codes - consent.acknowledged:
            raise PolicyError("unacknowledged_conflict", ", ".join(sorted(missing)))
        if not draft.limits:
            raise PolicyError("limits_required", "no user-entered numerical limits")
        if gaps := _limit_gaps(draft):
            raise PolicyError("missing_required_limits", "; ".join(gaps))
        receipt = AdoptionReceipt(
            kind="policy_adoption",
            tenant_id=self.tenant_id,
            policy_id=self.policy_id,
            version=target.version,
            policy_hash=target.content_hash,
            label=draft.label,
            principal_id=consent.principal_id,
            step_up_receipt_id=consent.step_up_receipt_id,
            signed_at=ensure_aware_utc(consent.signed_at),
            limits=draft.limits,
            acknowledged=tuple(sorted(consent.acknowledged)),
            exclusions=draft.exclusions,
            acknowledgement_text=consent.acknowledgement_text,
        )
        return PolicyHistory(
            self.policy_id, self.tenant_id, self.versions, (*self.receipts, receipt)
        )


def _missing_for(
    draft: DraftPolicy, scope: SizingScope, denominator: str | None = None
) -> list[str]:
    have = {
        x.metric
        for x in draft.limits
        if x.scope.covers(scope) and denominator in (None, x.denominator)
    }
    return sorted(m.value for m in REQUIRED_METRICS - have)


def _limit_gaps(draft: DraftPolicy) -> list[str]:
    return [
        f"{a.scope}: {', '.join(missing)}"
        for a in draft.allocations
        if (missing := _missing_for(draft, a.scope))
    ]


class SizingBlock(StrEnum):
    NO_ADOPTED_POLICY = "no_adopted_policy"
    SYNTHETIC_POLICY = "synthetic_policy"
    TENANT_MISMATCH = "tenant_mismatch"
    RECEIPT_MISMATCH = "receipt_mismatch"
    ACCOUNT_MISSING = "account_missing"
    CURRENCY_MISSING = "currency_missing"
    DENOMINATOR_MISSING = "denominator_missing"
    SCOPE_NOT_ALLOCATED = "scope_not_allocated"
    LIMIT_MISSING = "limit_missing"


@dataclass(frozen=True, slots=True)
class SizingRequest:
    tenant_id: str
    account_id: str | None
    currency: str | None
    sleeve_id: str | None
    denominator: str | None


@dataclass(frozen=True, slots=True)
class SizingReason:
    code: SizingBlock
    detail: str


@dataclass(frozen=True, slots=True)
class SizingDecision:
    allowed: bool
    reasons: tuple[SizingReason, ...]
    policy_hash: str | None


def check_sizing(
    history: PolicyHistory | None, request: SizingRequest
) -> SizingDecision:
    """Allowed only under an adopted, live policy whose receipt matches its version
    and whose required limits cover the requested account/currency/sleeve.
    The request's denominator must match a covering limit for every required metric,
    so mixed-denominator policies block sizing (fail closed; review interpretation)."""
    reasons: list[SizingReason] = []

    def block(code: SizingBlock, detail: str = "") -> None:
        reasons.append(SizingReason(code, detail))

    current = history.adopted if history is not None else None
    if history is None or current is None:
        block(SizingBlock.NO_ADOPTED_POLICY)
        return SizingDecision(False, tuple(reasons), None)
    version, receipt = current
    if request.tenant_id != history.tenant_id:  # C-07: reveal nothing else
        mismatch = SizingReason(SizingBlock.TENANT_MISMATCH, "")
        return SizingDecision(False, (mismatch,), None)
    if version.draft.synthetic or receipt.label != "LIVE":
        block(SizingBlock.SYNTHETIC_POLICY, "SYNTHETIC policies never authorise")
    if (
        receipt.policy_hash != version.content_hash
        or version.draft.content_hash != version.content_hash
        or receipt.limits != version.draft.limits
    ):
        block(SizingBlock.RECEIPT_MISMATCH)
    if not request.denominator:
        block(SizingBlock.DENOMINATOR_MISSING)
    if not request.account_id:
        block(SizingBlock.ACCOUNT_MISSING)
    if not request.currency:
        block(SizingBlock.CURRENCY_MISSING)
    if request.account_id and request.currency:
        scope = SizingScope(request.account_id, request.currency, request.sleeve_id)
        if scope not in {a.scope for a in version.draft.allocations}:
            block(SizingBlock.SCOPE_NOT_ALLOCATED, str(scope))
        # Each required metric needs a covering limit on the requested denominator.
        for metric in _missing_for(version.draft, scope, request.denominator or None):
            block(SizingBlock.LIMIT_MISSING, metric)
    return SizingDecision(not reasons, tuple(reasons), version.content_hash)
