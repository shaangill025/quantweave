"""Improvement experiments: frozen protocols, candidate lineage, budget scheduling
and the independent assessor (T044 increment 1; spec §11, §14, §15; R028, R029,
R030, R032, R033, R035, R037, R084, R092, R099). Domain only, stdlib only.
- Targets are the six of `experiment.schema.json`. Each has hard controls taken from
  the §11 negative-control column; a scorecard must hold each as a hard `at_most 0`
  threshold (it cannot drop or loosen one) plus at least one goal threshold.
- A `Protocol` is registered before any candidate and frozen into `content_hash`:
  scorecard, test access (T032 plan id and `plan_digest`), evidence purpose, risk
  policy hash, required runtime approval classes, budget terms, assessor identity.
  Registering the id again with any change is `protocol_frozen`, naming the fields.
- A `Candidate` is data: an artifact hash, a payload of strings and the logged T043
  `select` uses it was built from. Nothing here reads the payload, and keys naming
  protected controls are refused (`protected_field`). Lineage is append-only: the
  parent is the baseline or a registered candidate, and an artifact identical to its
  parent is refused (relabelling is not a change).
- `schedule` reserves each queued experiment's worst-case cost in the T037 book under
  the improvement category before any work, in queue order. A cap refusal defers it
  and every later one (`queued_behind`): nothing is skipped or overrun. A retry finds
  the existing reservation and never resets it; a closed one is not re-reserved.
- `assess` is run by the protocol's assessor, which must differ from the producer in
  actor and route (and family when configured). It reads only the protocol and the
  T032 trial ledger: the promotion trial its own access ran on the first holdout read.
  Numbers a candidate or producer claims must equal the recorded ones. Hard failures
  cannot be offset by goals. The verdict is accept-for-review, reject, insufficient
  evidence or blocked; it never promotes, installs or adopts anything.
- Code and optimizer targets need the T007 sandbox, which is not qualified: they are
  representable, but scheduling and assessment fail closed (`sandbox_unqualified`).
LIMITATIONS: no persistence; times and actor ids are caller-asserted; evidence uses
are checked against the store only when a candidate is first registered;
`record_verdict` checks hashes and assessor identity but cannot re-run `assess`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from qw_domain.budget import BudgetBook, Category, Reservation, Status
from qw_domain.decimals import UsdBudget, safe_repr
from qw_domain.feedback import Access as EvidenceAccess
from qw_domain.feedback import ImprovementEvidence, Purpose, Use
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import EvidenceClass, ResearchError, digest
from qw_domain.sources import ID_PATTERN
from qw_domain.trial_ledger import Candidate as TrialKey
from qw_domain.trial_ledger import Outcome, Role, SplitPlan, TrialLedger
from qw_domain.trial_ledger import Split as TrialSplit


class ImprovementError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ImprovementError(what, f"{safe_repr(value)} is malformed")
    return value


def _sha(value: object, what: str) -> str:
    ok = isinstance(value, str) and len(value) == 64
    if not ok or any(c not in "0123456789abcdef" for c in str(value)):
        raise ImprovementError(what, f"{safe_repr(value)} is not a sha256 hex digest")
    return str(value)


class Target(StrEnum):  # experiment.schema.json `target`
    PERSONAL_ADAPTATION = "personal_adaptation"
    RESEARCH = "research"
    STRATEGY = "strategy"
    EVALUATOR = "evaluator"
    APPLICATION_CODE = "application_code"
    RECURSIVE_OPTIMIZER = "recursive_optimizer"


class Scope(StrEnum):
    TENANT = "tenant"
    INSTALLATION = "installation"


class ApprovalClass(StrEnum):  # R099: typed runtime approvals, granted elsewhere
    MAINTAINER_RELEASE = "maintainer_release"
    OPERATOR_INSTALL = "operator_install"
    USER_STRATEGY_ADOPTION = "user_strategy_adoption"


SANDBOX_TARGETS = frozenset({Target.APPLICATION_CODE, Target.RECURSIVE_OPTIMIZER})
# §11 negative controls as counted violations; each must be held at zero.
HARD_CONTROLS: Mapping[Target, tuple[str, ...]] = MappingProxyType({
    Target.PERSONAL_ADAPTATION: ("risk_broadened", "private_export"),
    Target.RESEARCH: ("unsupported_material_facts",),
    Target.STRATEGY: ("lookahead_violations", "survivorship_violations",
                      "unrecorded_trials"),
    Target.EVALUATOR: ("answer_key_mutations", "unverified_claims_accepted"),
    Target.APPLICATION_CODE: ("secret_access_attempts", "host_writes",
                              "unauthorized_dependencies",
                              "protected_control_mutations"),
    Target.RECURSIVE_OPTIMIZER: ("identical_relabels", "threshold_changes"),
})  # fmt: skip
_CODE = frozenset({ApprovalClass.MAINTAINER_RELEASE, ApprovalClass.OPERATOR_INSTALL})
# Minimum runtime approvals a protocol must declare (interpretation of §11/R099).
MIN_APPROVALS: Mapping[Target, frozenset[ApprovalClass]] = MappingProxyType({
    Target.PERSONAL_ADAPTATION: frozenset({ApprovalClass.OPERATOR_INSTALL,
                                           ApprovalClass.USER_STRATEGY_ADOPTION}),
    Target.RESEARCH: _CODE, Target.EVALUATOR: _CODE,
    Target.STRATEGY: frozenset(ApprovalClass),
    Target.APPLICATION_CODE: _CODE, Target.RECURSIVE_OPTIMIZER: _CODE,
})  # fmt: skip


class Direction(StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


@dataclass(frozen=True, slots=True)
class Threshold:
    metric: str
    direction: Direction
    bound: Decimal
    hard: bool  # a hard constraint: never offset by better goal metrics

    def __post_init__(self) -> None:
        _id(self.metric, "metric")
        if type(self.bound) is not Decimal or not self.bound.is_finite():
            raise TypeError("bound must be a finite Decimal")
        if not isinstance(self.direction, Direction) or type(self.hard) is not bool:
            raise TypeError("typed direction and hard flag are required")

    def passes(self, value: Decimal) -> bool:
        if self.direction is Direction.AT_LEAST:
            return value >= self.bound
        return value <= self.bound

    def wire(self) -> list[object]:
        return [self.metric, self.direction.value, str(self.bound), self.hard]


@dataclass(frozen=True, slots=True)
class Scorecard:
    scorecard_id: str
    target: Target
    thresholds: tuple[Threshold, ...]

    def __post_init__(self) -> None:
        _id(self.scorecard_id, "scorecard_id")
        ts = tuple(self.thresholds)
        object.__setattr__(self, "thresholds", ts)
        if not all(type(t) is Threshold for t in ts):
            raise TypeError("Threshold records are required")
        if len({t.metric for t in ts}) != len(ts):
            raise ImprovementError("duplicate_metric", self.scorecard_id)
        zero = {t.metric for t in ts if t.hard and t.direction is Direction.AT_MOST
                and t.bound == 0}  # fmt: skip
        if missing := [m for m in HARD_CONTROLS[Target(self.target)] if m not in zero]:
            raise ImprovementError("hard_control_missing", ",".join(missing))
        if not any(not t.hard for t in ts):
            raise ImprovementError("goal_required", self.scorecard_id)

    def wire(self) -> list[object]:
        return [
            self.scorecard_id,
            self.target.value,
            sorted(t.wire() for t in self.thresholds),
        ]


@dataclass(frozen=True, slots=True)
class Party:  # who produced or assesses: actor, model route, family, version
    actor_id: str
    route_id: str
    family_id: str
    version: str

    def __post_init__(self) -> None:
        for f in fields(self):
            _id(getattr(self, f.name), f.name)


def independence(producer: Party, assessor: Party, distinct_family: bool) -> list[str]:
    out = [f"same_{n}" for n in ("actor_id", "route_id")
           if getattr(producer, n) == getattr(assessor, n)]  # fmt: skip
    if distinct_family and producer.family_id == assessor.family_id:
        out.append("same_family_id")
    return out


def plan_digest(p: SplitPlan) -> str:
    """The test access a protocol freezes: the T032 plan's terms."""
    return digest([p.plan_id, p.dataset_hash, p.protocol_hash,
                   [w.to_wire() for w in (p.train, p.development, p.promotion)],
                   str(p.embargo.total_seconds()),
                   sorted(i.to_wire() for i in p.holdout_instruments),
                   p.max_promotion_accesses, p.approved_by])  # fmt: skip


@dataclass(frozen=True, slots=True)
class Protocol:
    experiment_id: str
    scope: Scope
    scope_id: str
    target: Target
    hypothesis: str
    baseline_version: str
    baseline_hash: str
    producer: Party
    assessor: Party
    require_distinct_family: bool
    scorecard: Scorecard
    plan_id: str
    plan_digest: str
    evidence_purpose: Purpose
    risk_policy_hash: str
    approvals: frozenset[ApprovalClass]
    budget_reservation_id: str
    max_cost: UsdBudget
    provider_account_ref: str
    max_output_tokens: int
    max_duration: timedelta
    sandbox_profile_id: str | None
    authorized_by: str
    registered_at: datetime

    def __post_init__(self) -> None:
        ids = ("experiment_id", "scope_id", "baseline_version", "plan_id",
               "budget_reservation_id", "provider_account_ref",
               "authorized_by")  # fmt: skip
        for n in ids:
            _id(getattr(self, n), n)
        for n in ("baseline_hash", "plan_digest", "risk_policy_hash"):
            _sha(getattr(self, n), n)
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ImprovementError("hypothesis", "a stated hypothesis is required")
        types = (("scope", Scope), ("target", Target), ("evidence_purpose", Purpose),
                 ("producer", Party), ("assessor", Party), ("scorecard", Scorecard),
                 ("max_cost", UsdBudget))  # fmt: skip
        for n, t in types:
            if not isinstance(getattr(self, n), t):
                raise TypeError(f"{n} must be {t.__name__}")
        if type(self.require_distinct_family) is not bool:
            raise TypeError("require_distinct_family must be a bool")
        if self.scorecard.target is not self.target:
            raise ImprovementError("scorecard_target", self.scorecard.scorecard_id)
        if why := independence(self.producer, self.assessor,
                               self.require_distinct_family):  # fmt: skip
            raise ImprovementError("assessor_not_independent", ",".join(why))
        ap = frozenset(self.approvals)
        object.__setattr__(self, "approvals", ap)
        if not all(type(a) is ApprovalClass for a in ap) or not (
            MIN_APPROVALS[self.target] <= ap
        ):  # fmt: skip
            raise ImprovementError("approvals", "the target's minimum is required")
        if self.max_cost.value <= 0 or type(self.max_output_tokens) is not int or (
            self.max_output_tokens < 1
        ):  # fmt: skip
            raise ImprovementError("budget", "positive cost and token limits")
        if type(self.max_duration) is not timedelta or self.max_duration <= timedelta():
            raise ImprovementError("max_duration", "a positive timedelta")
        if self.target in SANDBOX_TARGETS and self.sandbox_profile_id is None:
            raise ImprovementError("sandbox_profile", "code targets name a profile")
        if self.sandbox_profile_id is not None:
            _id(self.sandbox_profile_id, "sandbox_profile_id")
        object.__setattr__(self, "registered_at", ensure_aware_utc(self.registered_at))

    def wire(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Party):
                v = [v.actor_id, v.route_id, v.family_id, v.version]
            elif isinstance(v, Scorecard):
                v = v.wire()
            elif isinstance(v, frozenset):
                v = sorted(v)
            elif isinstance(v, datetime):
                v = format_instant(v)
            elif isinstance(v, timedelta):
                v = str(v.total_seconds())
            elif isinstance(v, UsdBudget):
                v = v.to_wire()
            out[f.name] = v
        return out

    @property
    def content_hash(self) -> str:
        return digest(self.wire())


_PROTECTED = frozenset({f.name for f in fields(Protocol)} | {
    "threshold", "thresholds", "approval", "approval_record", "approval_records",
    "test_access", "holdout", "risk_constraints", "answer_key", "answer_keys",
    "signing_key", "assessor_config", "release_receipt",
})  # fmt: skip


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    experiment_id: str
    target: Target
    parent_id: str  # the baseline version or a registered candidate
    producer: Party
    artifact_hash: str  # diff or artifact content
    payload: Mapping[str, str]
    runtime_versions: Mapping[str, str]
    evidence_uses: tuple[Use, ...]  # logged T043 selections it was built from
    created_at: datetime

    def __post_init__(self) -> None:
        for n in ("candidate_id", "experiment_id", "parent_id"):
            _id(getattr(self, n), n)
        _sha(self.artifact_hash, "artifact_hash")
        if not isinstance(self.target, Target) or type(self.producer) is not Party:
            raise TypeError("typed target and producer are required")
        for n in ("payload", "runtime_versions"):
            m = dict(getattr(self, n))
            if not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in m.items()):  # fmt: skip
                raise TypeError(f"{n} holds strings only")
            object.__setattr__(self, n, MappingProxyType(dict(sorted(m.items()))))
        for k in self.payload:
            if k.lower().replace("-", "_") in _PROTECTED:
                raise ImprovementError("protected_field", safe_repr(k))
        object.__setattr__(self, "evidence_uses", tuple(self.evidence_uses))
        if not all(type(u) is Use for u in self.evidence_uses):
            raise TypeError("evidence_uses holds T043 Use records")
        object.__setattr__(self, "created_at", ensure_aware_utc(self.created_at))

    @property
    def payload_hash(self) -> str:
        return digest(dict(self.payload))

    @property
    def trial_key(self) -> TrialKey:
        """The T032 ledger candidate: the experiment is the hypothesis family."""
        return TrialKey(self.experiment_id, self.artifact_hash, self.payload_hash)

    @property
    def content_hash(self) -> str:
        uses = [[u.experiment_id, u.access, u.purpose, list(u.record_ids),
                 format_instant(u.at)] for u in self.evidence_uses]  # fmt: skip
        return digest([self.candidate_id, self.experiment_id, self.target,
                       self.parent_id, self.producer.actor_id, self.producer.version,
                       self.artifact_hash, dict(self.payload),
                       dict(self.runtime_versions), uses,
                       format_instant(self.created_at)])  # fmt: skip


class Decision(StrEnum):
    ACCEPT_FOR_REVIEW = "accept_for_review"
    REJECT = "reject"
    INSUFFICIENT = "insufficient_evidence"
    BLOCKED = "blocked"


NEXT_STATE: Mapping[Decision, str] = MappingProxyType({  # spec §11 candidate states
    Decision.ACCEPT_FOR_REVIEW: "awaiting_human_review",
    Decision.REJECT: "rejected", Decision.INSUFFICIENT: "insufficient_evidence",
    Decision.BLOCKED: "drafted",
})  # fmt: skip


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict_id: str
    experiment_id: str
    candidate_id: str
    candidate_hash: str
    protocol_hash: str
    assessor: Party
    decision: Decision
    reasons: tuple[str, ...]
    scores: Mapping[str, Decimal]  # from recorded trials only
    trial_ids: tuple[str, ...]
    failed_trial_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    assessed_at: datetime

    def __post_init__(self) -> None:
        if (self.decision is Decision.ACCEPT_FOR_REVIEW) == bool(self.reasons):
            raise ImprovementError("verdict_inconsistent", self.verdict_id)

    @property
    def next_state(self) -> str:
        return NEXT_STATE[self.decision]

    @property
    def requires_human_promotion(self) -> bool:
        return True  # experiment.schema.json const; no path here promotes


type Entry = Protocol | Candidate | Verdict


@dataclass(slots=True)
class _State:
    protocols: dict[str, Protocol] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)
    verdicts: list[Verdict] = field(default_factory=list)


def _check_candidate(s: _State, c: Candidate) -> None:
    p = s.protocols.get(c.experiment_id)
    if p is None:
        raise ImprovementError("unknown_experiment", c.experiment_id)
    if c.candidate_id in s.candidates or any(
        c.candidate_id == q.baseline_version for q in s.protocols.values()
    ):  # fmt: skip
        raise ImprovementError("duplicate", c.candidate_id)
    if c.target is not p.target:
        raise ImprovementError("target_mismatch", c.candidate_id)
    if c.producer != p.producer:
        raise ImprovementError("producer_mismatch", c.candidate_id)
    if c.created_at < p.registered_at:
        raise ImprovementError("before_protocol", c.candidate_id)
    mine = [x for x in s.candidates.values() if x.experiment_id == c.experiment_id]
    if c.parent_id != p.baseline_version and c.parent_id not in {
        x.candidate_id for x in mine
    }:  # fmt: skip
        raise ImprovementError("unknown_parent", c.parent_id)
    # no relabel of the baseline, an ancestor or a sibling in this experiment
    if c.artifact_hash in {p.baseline_hash, *(x.artifact_hash for x in mine)}:
        raise ImprovementError("identical_artifact", c.candidate_id)
    for u in c.evidence_uses:
        if (u.tenant_id, u.experiment_id) != (p.scope_id, p.experiment_id) or (
            u.purpose is not p.evidence_purpose
        ):  # fmt: skip
            raise ImprovementError("evidence_experiment", c.candidate_id)
        if u.access is not EvidenceAccess.DEVELOPMENT:
            raise ImprovementError("final_access_by_producer", c.candidate_id)


def _apply(s: _State, e: Entry, scope_id: str) -> None:
    if isinstance(e, Protocol):
        if e.scope_id != scope_id:
            raise ImprovementError("scope_mismatch", e.experiment_id)
        if e.experiment_id in s.protocols or e.baseline_version in s.candidates:
            raise ImprovementError("duplicate", e.experiment_id)
        if any(q.budget_reservation_id == e.budget_reservation_id
               for q in s.protocols.values()):  # fmt: skip
            raise ImprovementError("duplicate_reservation_id", e.experiment_id)
        s.protocols[e.experiment_id] = e
    elif isinstance(e, Candidate):
        _check_candidate(s, e)
        s.candidates[e.candidate_id] = e
    elif type(e) is Verdict:
        c = s.candidates.get(e.candidate_id)
        p = s.protocols.get(e.experiment_id)
        if c is None or p is None or c.experiment_id != p.experiment_id:
            raise ImprovementError("unknown_candidate", e.candidate_id)
        if (e.candidate_hash, e.protocol_hash) != (c.content_hash, p.content_hash):
            raise ImprovementError("hash_mismatch", e.verdict_id)
        if e.assessor != p.assessor:
            raise ImprovementError("assessor_mismatch", e.verdict_id)
        if any(v.verdict_id == e.verdict_id for v in s.verdicts):
            raise ImprovementError("duplicate", e.verdict_id)
        s.verdicts.append(e)
    else:
        raise ImprovementError("entry", type(e).__name__)


@dataclass(frozen=True, slots=True)
class ExperimentRegistry:
    """Append-only, per scope. Construction replays every entry."""

    scope_id: str
    entries: tuple[Entry, ...] = ()
    _state: _State = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _id(self.scope_id, "scope_id")
        object.__setattr__(self, "entries", tuple(self.entries))
        state = _State()
        for e in self.entries:
            _apply(state, e, self.scope_id)
        object.__setattr__(self, "_state", state)

    def _append(self, e: Entry) -> "ExperimentRegistry":
        return ExperimentRegistry(self.scope_id, (*self.entries, e))

    def register(self, p: Protocol) -> "ExperimentRegistry":
        old = self._state.protocols.get(p.experiment_id)
        if old is not None:
            if old == p:
                return self
            a, b = old.wire(), p.wire()
            changed = sorted(k for k in a if a[k] != b[k])
            raise ImprovementError("protocol_frozen", ",".join(changed))
        return self._append(p)

    def register_candidate(
        self, c: Candidate, evidence: ImprovementEvidence | None = None
    ) -> "ExperimentRegistry":
        """Append `c`; each evidence use must be one `evidence.select` logged."""
        if self._state.candidates.get(c.candidate_id) == c:
            return self
        _check_candidate(self._state, c)
        logged = () if evidence is None else evidence.uses(self.scope_id)
        if any(u not in logged for u in c.evidence_uses):
            raise ImprovementError("evidence_not_logged", c.candidate_id)
        return self._append(c)

    def record_verdict(self, v: Verdict) -> "ExperimentRegistry":
        return self._append(v)

    def protocol(self, experiment_id: str) -> Protocol:
        if (p := self._state.protocols.get(experiment_id)) is None:
            raise ImprovementError("unknown_experiment", safe_repr(experiment_id))
        return p

    def candidate(self, candidate_id: str) -> Candidate:
        if (c := self._state.candidates.get(candidate_id)) is None:
            raise ImprovementError("unknown_candidate", safe_repr(candidate_id))
        return c

    def lineage(self, candidate_id: str) -> tuple[str, ...]:
        """The candidate, its ancestors, and the baseline version they started from."""
        out, c = [candidate_id], self.candidate(candidate_id)
        base = self.protocol(c.experiment_id).baseline_version
        while c.parent_id != base:
            c = self.candidate(c.parent_id)
            out.append(c.candidate_id)
        return (*out, base)

    def verdicts(self, candidate_id: str) -> tuple[Verdict, ...]:
        return tuple(v for v in self._state.verdicts if v.candidate_id == candidate_id)


@dataclass(frozen=True, slots=True)
class Scheduled:
    experiment_id: str
    reservation_id: str


@dataclass(frozen=True, slots=True)
class Deferred:
    experiment_id: str
    reason: str
    detail: str = ""


_CAPS = frozenset({"improvement_cap", "inference_cap"})


def _bound(p: Protocol, r: Reservation) -> bool:
    """The reservation carries exactly the protocol's frozen budget terms."""
    return (r.category, r.reserved, r.provider_account_ref, r.max_output_tokens) == (
        Category.IMPROVEMENT,
        p.max_cost,
        p.provider_account_ref,
        p.max_output_tokens,
    )


def schedule(
    registry: ExperimentRegistry, queue: Sequence[str], book: BudgetBook, at: datetime
) -> tuple[BudgetBook, tuple[Scheduled | Deferred, ...]]:
    """Reserve queued experiments in order against the improvement budget."""
    if book.scope_id != registry.scope_id:
        raise ImprovementError("scope_mismatch", book.scope_id)
    at = ensure_aware_utc(at)
    out: list[Scheduled | Deferred] = []
    blocker: str | None = None
    for eid in queue:
        p = registry.protocol(eid)
        rid = p.budget_reservation_id
        old = book.find(rid)
        if p.target in SANDBOX_TARGETS:
            out.append(Deferred(eid, "sandbox_unqualified"))
        elif old is not None and not _bound(p, old):
            out.append(Deferred(eid, "reservation_terms_mismatch"))
        elif old is not None:
            ok = old.status is Status.RESERVED
            out.append(Scheduled(eid, rid) if ok else
                       Deferred(eid, "reservation_closed"))  # fmt: skip
        elif blocker is not None:
            out.append(Deferred(eid, "queued_behind", blocker))
        else:
            book, refusal = book.reserve(rid, Category.IMPROVEMENT, p.max_cost,
                p.provider_account_ref, p.max_output_tokens, at,
                at + p.max_duration)  # fmt: skip
            if refusal is None:
                out.append(Scheduled(eid, rid))
            else:
                out.append(Deferred(eid, refusal.code))
                blocker = eid if refusal.code in _CAPS else None
    return book, tuple(out)


def assess(
    registry: ExperimentRegistry, experiment_id: str, candidate_id: str,
    ledger: TrialLedger, book: BudgetBook, assessor: Party, at: datetime,
    verdict_id: str, claimed: Mapping[str, Decimal] | None = None,
) -> Verdict:  # fmt: skip
    """Score one candidate against its frozen protocol from recorded trials."""
    _id(verdict_id, "verdict_id")
    at = ensure_aware_utc(at)
    if claimed is not None and any(type(v) is not Decimal for v in claimed.values()):
        raise TypeError("claimed metrics must be Decimals")
    p, c = registry.protocol(experiment_id), registry.candidate(candidate_id)
    if c.experiment_id != experiment_id:
        raise ImprovementError("candidate_experiment", candidate_id)
    scores: dict[str, Decimal] = {}
    used: list[str] = []
    failed: list[str] = []
    limits: list[str] = []

    def verdict(decision: Decision, *reasons: str) -> Verdict:
        return Verdict(verdict_id, experiment_id, candidate_id, c.content_hash,
                       p.content_hash, assessor, decision, reasons,
                       MappingProxyType(scores), tuple(used), tuple(failed),
                       tuple(limits), at)  # fmt: skip

    if p.target in SANDBOX_TARGETS:  # T007 not qualified: no in-process substitute
        return verdict(Decision.BLOCKED, "sandbox_unqualified")
    blocked = []
    if assessor != p.assessor:
        blocked.append("assessor_not_registered")
    r = book.find(p.budget_reservation_id)
    if book.scope_id != p.scope_id or r is None or r.status is not Status.RESERVED:
        blocked.append("budget_not_reserved")
    elif not _bound(p, r):
        blocked.append("reservation_terms_mismatch")
    try:
        plan = ledger.plan(p.plan_id)
    except ResearchError:
        plan = None
    if ledger.tenant_id != p.scope_id or plan is None or (
        plan_digest(plan) != p.plan_digest
    ):  # fmt: skip
        blocked.append("test_access_changed")
    if blocked or plan is None:
        return verdict(Decision.BLOCKED, *blocked)
    if plan.dataset.evidence_class is EvidenceClass.SYNTHETIC:
        limits.append("synthetic_only")
    key, notes, best = c.trial_key, set(), None
    for t in ledger.trials(key.family):
        if t.split is not TrialSplit.PROMOTION or t.plan_id != p.plan_id or (
            (t.material_hash, t.config_hash) != (key.material_hash, key.config_hash)
        ):  # fmt: skip
            continue
        a = ledger.access(t.access_id) if t.access_id else None
        if t.outcome is not Outcome.COMPLETED:
            failed.append(t.trial_id)  # retained and reported, never discarded
        elif (
            a is None or a.role is not Role.ASSESSOR or a.actor_id != assessor.actor_id
        ):
            notes.add("not_assessor_run")
        elif a.at < max(p.registered_at, c.created_at):
            notes.add("trial_before_freeze")  # holdout read before freeze/candidate
        elif not a.at <= t.recorded_at <= at:
            notes.add("time_order")
        elif a.seq != 1:
            notes.add("holdout_reused")
        else:
            best = t
    if best is None:
        return verdict(Decision.INSUFFICIENT, "no_qualifying_trial", *sorted(notes))
    used.append(best.trial_id)
    scores.update(best.metrics)
    cards = p.scorecard.thresholds
    missing = [f"metric_missing:{t.metric}" for t in cards if t.metric not in scores]
    if missing:
        return verdict(Decision.INSUFFICIENT, *missing)
    bad = [f"metric_invalid:{m}" for m in HARD_CONTROLS[p.target] if scores[m] < 0]
    if bad:  # violation counts are never negative
        return verdict(Decision.INSUFFICIENT, *bad)
    reasons = [f"{'hard' if t.hard else 'goal'}:{t.metric}"
               for t in sorted(cards, key=lambda t: not t.hard)
               if not t.passes(scores[t.metric])]  # fmt: skip
    if claimed is not None and any(scores.get(k) != v for k, v in claimed.items()):
        reasons.append("metrics_mismatch")
    if reasons:
        return verdict(Decision.REJECT, *reasons)
    return verdict(Decision.ACCEPT_FOR_REVIEW)
