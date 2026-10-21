"""Improvement experiments: frozen protocols, candidate lineage, budget scheduling
and the independent assessor (T044 increment 1; spec §11, §14, §15; R028, R029,
R030, R032, R033, R035, R037, R084, R092, R099). Domain only, stdlib only.
- Targets are the six of `experiment.schema.json`. Each has hard controls taken from
  the §11 negative-control column; a scorecard must hold each as a hard `at_most 0`
  threshold (it cannot drop or loosen one) plus at least one goal threshold.
- A `Protocol` is registered before any candidate and frozen into `content_hash`:
  scorecard, test access (T032 plan id and `plan_digest`), evidence purpose, risk
  policy hash, required runtime approval classes, budget terms, assessor identity
  and, for strategies, the permitted mutations (`mutation_space_hash`, T047).
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
- `record` derives the `experiment` schema view that experiment_store persists
  (project contract 0.6.0): proposed, authorized, then from an `assess` it runs
  itself awaiting_review, rejected, insufficient_evidence or blocked; code and
  optimizer stay blocked. What the schema cannot carry is a `<kind>:<detail>`
  reason (for example `blocked:sandbox_unqualified`). insufficient_evidence may be
  re-assessed on the same frozen protocol under a new verdict id: `assess` admits
  only the plan's first holdout read in the ledger it is given and never
  re-reserves the budget, and a new plan is a new protocol, so a new experiment
  (owner decision Q7). experiment_store persists protocols, lineage, holdout reads
  and verdicts (`derive`), so the single holdout read holds across ledgers there
  (0028). 0.5.0 records
  (`failed` for insufficient evidence; blocked verdicts and code targets as
  proposed or authorized) stay readable and are never derived.
LIMITATIONS: the budget book is not persisted; times and actor ids are
caller-asserted; evidence uses
are checked against the store only when a candidate is first registered;
`record_verdict` checks hashes and assessor identity but cannot re-run `assess`.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from qw_domain.backtest import EvaluationProtocol
from qw_domain.budget import BudgetBook, Category, Reservation, Status
from qw_domain.decimal_math import CTX, norm_cdf, norm_ppf
from qw_domain.decimals import UsdBudget, safe_repr
from qw_domain.feedback import Access as EvidenceAccess
from qw_domain.feedback import ImprovementEvidence, Purpose, Use
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
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
    mutation_space_hash: str | None = None  # permitted mutations (T047 search space)
    retest_of: str | None = None  # owner decision 19: the experiment this re-tests

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
        if self.mutation_space_hash is not None:
            _sha(self.mutation_space_hash, "mutation_space_hash")
        if self.retest_of is not None:
            _id(self.retest_of, "retest_of")
            if self.retest_of == self.experiment_id:
                raise ImprovementError(
                    "retest_of", "an experiment never re-tests itself"
                )
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
            elif f.name == "retest_of" and v is None:
                continue  # absent: protocols without a link keep their hashes
            out[f.name] = v
        return out

    @property
    def content_hash(self) -> str:
        return digest(self.wire())

    @classmethod
    def from_wire(cls, w: Mapping[str, Any]) -> "Protocol":
        """The inverse of `wire` (a stored copy); it must give `w` back exactly."""
        sid, target, ts = w["scorecard"]
        card = Scorecard(sid, Target(target), tuple(Threshold(
            m, Direction(d), Decimal(b), h) for m, d, b, h in ts))  # fmt: skip
        secs = Decimal(w["max_duration"]) * 1_000_000
        p = cls(**(dict(w) | {
            "scope": Scope(w["scope"]), "target": Target(w["target"]),
            "producer": Party(*w["producer"]), "assessor": Party(*w["assessor"]),
            "scorecard": card, "evidence_purpose": Purpose(w["evidence_purpose"]),
            "approvals": frozenset(ApprovalClass(a) for a in w["approvals"]),
            "max_cost": UsdBudget.from_wire(w["max_cost"]),
            "max_duration": timedelta(microseconds=int(secs)),
            "registered_at": parse_instant(w["registered_at"]),
        }))  # fmt: skip
        if p.wire() != dict(w):
            raise ImprovementError("protocol_wire", p.experiment_id)
        return p


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

    def wire(self) -> dict[str, object]:
        uses = [[u.tenant_id, u.experiment_id, u.purpose.value, u.access.value,
                 u.recipient, list(u.record_ids), list(u.consent_ids),
                 format_instant(u.at)] for u in self.evidence_uses]  # fmt: skip
        pr = self.producer
        return {"candidate_id": self.candidate_id, "experiment_id": self.experiment_id,
                "target": self.target.value, "parent_id": self.parent_id,
                "producer": [pr.actor_id, pr.route_id, pr.family_id, pr.version],
                "artifact_hash": self.artifact_hash, "payload": dict(self.payload),
                "runtime_versions": dict(self.runtime_versions),
                "evidence_uses": uses,
                "created_at": format_instant(self.created_at)}  # fmt: skip

    @classmethod
    def from_wire(cls, w: Mapping[str, Any]) -> "Candidate":
        """The inverse of `wire` (a stored copy); it must give `w` back exactly."""
        uses = tuple(Use(t, e, Purpose(p), EvidenceAccess(a), r, tuple(ids),
                         tuple(cs), parse_instant(at))
                     for t, e, p, a, r, ids, cs, at in w["evidence_uses"])  # fmt: skip
        c = cls(w["candidate_id"], w["experiment_id"], Target(w["target"]),
                w["parent_id"], Party(*w["producer"]), w["artifact_hash"],
                w["payload"], w["runtime_versions"], uses,
                parse_instant(w["created_at"]))  # fmt: skip
        if c.wire() != dict(w):
            raise ImprovementError("candidate_wire", c.candidate_id)
        return c

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
    # decision 35: z', the chain's trial count and each widened excess lower bound
    adjustment: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "adjustment", MappingProxyType(dict(self.adjustment)))
        if any(type(v) is not Decimal for v in self.adjustment.values()):
            raise TypeError("adjustment values must be Decimals")
        if (self.decision is Decision.ACCEPT_FOR_REVIEW) == bool(self.reasons):
            raise ImprovementError("verdict_inconsistent", self.verdict_id)

    @property
    def next_state(self) -> str:
        return NEXT_STATE[self.decision]

    def wire(self) -> dict[str, object]:
        a = self.assessor
        return {"verdict_id": self.verdict_id, "experiment_id": self.experiment_id,
                "candidate_id": self.candidate_id,
                "candidate_hash": self.candidate_hash,
                "protocol_hash": self.protocol_hash,
                "assessor": [a.actor_id, a.route_id, a.family_id, a.version],
                "decision": self.decision.value, "reasons": list(self.reasons),
                "scores": {k: str(v) for k, v in sorted(self.scores.items())},
                "trial_ids": list(self.trial_ids),
                "failed_trial_ids": list(self.failed_trial_ids),
                "limitations": list(self.limitations),
                "assessed_at": format_instant(self.assessed_at),
                **({"adjustment": {k: str(v) for k, v in
                                   sorted(self.adjustment.items())}}
                   if self.adjustment else {})}  # fmt: skip

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
            changed = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
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
    verdict_id: str, claimed: Mapping[str, Decimal] | None = None, *,
    evaluation: EvaluationProtocol | None = None, chain_trials: int | None = None,
) -> Verdict:  # fmt: skip
    """Score one candidate against its frozen protocol from recorded trials. A
    strategy promotion must also clear the Bonferroni-widened excess bound
    (`_widened`, owner decisions 35 to 37); `chain_trials` is the store's count of
    the whole retest chain (else this ledger's count of the experiment's family)."""
    _id(verdict_id, "verdict_id")
    if chain_trials is not None and (type(chain_trials) is not int or chain_trials < 0):
        raise ImprovementError("chain_trials", "a non-negative int")
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
    adjust: dict[str, Decimal] = {}

    def verdict(decision: Decision, *reasons: str) -> Verdict:
        return Verdict(verdict_id, experiment_id, candidate_id, c.content_hash,
                       p.content_hash, assessor, decision, reasons,
                       MappingProxyType(scores), tuple(used), tuple(failed),
                       tuple(limits), at, adjust)  # fmt: skip

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
    if p.target is not Target.STRATEGY:  # decision 36: counted, not adjusted
        limits.append("no_multiple_testing_adjustment")
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
    widened: list[str] = []
    if p.target is Target.STRATEGY:
        k = chain_trials
        if k is None and p.retest_of is None:  # no link: this family is the chain
            k = ledger.trial_count(experiment_id)
        why = _widened(plan, evaluation, k, scores, adjust)
        if why:
            return verdict(Decision.INSUFFICIENT, *why)
        widened = [f"bonferroni:{n[: -len(_LOWER)]}" for n, b in adjust.items()
                   if n.endswith(_LOWER) and b < 0]  # fmt: skip
    reasons = [f"{'hard' if t.hard else 'goal'}:{t.metric}"
               for t in sorted(cards, key=lambda t: not t.hard)
               if not t.passes(scores[t.metric])] + widened  # fmt: skip
    if claimed is not None and any(scores.get(k) != v for k, v in claimed.items()):
        reasons.append("metrics_mismatch")
    if reasons:
        return verdict(Decision.REJECT, *reasons)
    return verdict(Decision.ACCEPT_FOR_REVIEW)


def chain_trials(ledger: TrialLedger, chain: Sequence[str],
                 stored: Mapping[str, int]) -> int:  # fmt: skip
    """Owner decision 19: the trials of every experiment in a retest chain (`chain`,
    this experiment first, each one's hypothesis family), the count a
    multiple-testing adjustment must use; per experiment the larger of the ledger's
    `trial_count` and the `stored` count of its earlier receipts. No adjustment is
    applied here: none is named by the spec."""
    if not chain or len(set(chain)) != len(chain) or any(
        type(n) is not int or n < 0 for n in stored.values()
    ):  # fmt: skip
        raise ImprovementError("retest_chain", "distinct ids and counts >= 0")
    return sum(max(ledger.trial_count(e), stored.get(e, 0)) for e in chain)


_LOWER = ".excess_lower_widened"


def _widened(plan: SplitPlan, ev: EvaluationProtocol | None, k: int | None,
             scores: Mapping[str, Decimal],
             adjust: dict[str, Decimal]) -> list[str]:  # fmt: skip
    """Decisions 35 to 37: the excess lower bound at z' = Phi^-1(1 - alpha / (2 k)),
    alpha = 2 (1 - Phi(z)), for the claim's baseline, else every declared one.
    `ev` must be the evaluation protocol the frozen plan names by hash (so z is
    not the caller's), and the trial was recorded under it (the ledger refuses a
    trial whose protocol hash is not its plan's). Fills `adjust`; returns why
    the evidence is insufficient (missing or unbound inputs), else []."""
    if ev is None:
        return ["evaluation_protocol_missing"]
    if type(ev) is not EvaluationProtocol or ev.content_hash != plan.protocol_hash:
        return ["evaluation_protocol_unbound"]  # (the hash covers its plan id)
    if k is None or k < 1:  # a retest needs the store's chain count, and the
        return ["chain_trials_unknown"]  # scored trial is always one of them
    if "ci_z" in scores and scores["ci_z"] != ev.z:
        return ["z_mismatch"]
    named = ev.claim.baseline_id
    ids = [named] if named is not None else [b.baseline_id for b in ev.baselines]
    keys = [
        f"baseline.{i}.{m}" for i in ids for m in ("excess_mean", "excess_std_error")
    ]
    if missing := [f"metric_missing:{n}" for n in keys if n not in scores]:
        return missing
    alpha = CTX.multiply(2, CTX.subtract(1, norm_cdf(ev.z)))
    try:
        z = norm_ppf(CTX.subtract(1, CTX.divide(alpha, Decimal(2 * k))))
    except ValueError:
        return ["widened_z_unavailable"]
    adjust.update({"chain_trials": Decimal(k), "z_widened": z})
    for i in ids:
        mean, se = scores[f"baseline.{i}.excess_mean"], scores[
            f"baseline.{i}.excess_std_error"]  # fmt: skip
        adjust[f"baseline.{i}{_LOWER}"] = CTX.subtract(mean, CTX.multiply(z, se))
    return []


class RecordState(StrEnum):  # the experiment.schema.json states derivable here
    PROPOSED = "proposed"  # registered; no verified reservation
    AUTHORIZED = "authorized"  # its frozen budget terms are reserved
    BLOCKED = "blocked"  # a closed gate (T007) or a blocked assessment
    AWAITING_REVIEW = "awaiting_review"  # accept-for-review; a human decides
    REJECTED = "rejected"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # never a pass; re-assessable
    FAILED = "failed"  # 0.5.0 records only (insufficient evidence); never derived


_S = RecordState
_DECIDED = MappingProxyType({
    Decision.ACCEPT_FOR_REVIEW: _S.AWAITING_REVIEW, Decision.REJECT: _S.REJECTED,
    Decision.INSUFFICIENT: _S.INSUFFICIENT_EVIDENCE, Decision.BLOCKED: _S.BLOCKED,
})  # fmt: skip
_DECIDED_050 = MappingProxyType({
    Decision.ACCEPT_FOR_REVIEW: _S.AWAITING_REVIEW, Decision.REJECT: _S.REJECTED,
    Decision.INSUFFICIENT: _S.FAILED,
})  # fmt: skip
_UNDECIDED = frozenset({_S.PROPOSED, _S.AUTHORIZED})
# Allowed revisions (0.6.0); awaiting_review, rejected and failed are final here.
_NEXT = MappingProxyType({
    _S.PROPOSED: frozenset({_S.PROPOSED, _S.AUTHORIZED, _S.BLOCKED}),
    _S.AUTHORIZED: frozenset({_S.AUTHORIZED, _S.BLOCKED, _S.AWAITING_REVIEW,
                             _S.REJECTED, _S.INSUFFICIENT_EVIDENCE}),
    _S.BLOCKED: frozenset({_S.PROPOSED, _S.AUTHORIZED, _S.BLOCKED, _S.AWAITING_REVIEW,
                          _S.REJECTED, _S.INSUFFICIENT_EVIDENCE}),
    _S.INSUFFICIENT_EVIDENCE: frozenset({_S.INSUFFICIENT_EVIDENCE, _S.BLOCKED,
                                        _S.AWAITING_REVIEW, _S.REJECTED}),
})  # fmt: skip
_MOVING = frozenset({"state", "verdict_id", "decision", "reasons"})
# <kind>:<detail>; served as <kind>:<id>/<detail>, at most 300 with a 128-char id
_REASON = re.compile(r"[a-z]{1,12}:[A-Za-z0-9_.:-]{1,150}")
_NO_PARENT = "none:parent_optimizer_version"  # 0.6.0: known not to apply
_NO_PARENT_050 = "unavailable:parent_optimizer_version"


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """One experiment in the `experiment` schema's terms, derived by `record`.
    `reasons` are `<kind>:<detail>` codes the schema cannot carry (blocked,
    deferred, insufficient, rejected, evidence, none); a reader must show them.
    It is consistent under the 0.6.0 rules, or is a 0.5.0 record (stored before
    0027; read only, `record` never derives one)."""

    experiment_id: str
    target: Target
    scope: Scope
    scope_id: str
    state: RecordState
    baseline_version: str
    candidate_version: str
    hypothesis: str
    protocol_id: str  # the frozen protocol's content hash
    protected_assessor_version: str
    data_snapshot_ids: tuple[str, ...]  # the T032 plan's dataset
    budget_reservation_id: str
    sandbox_profile_id: str
    candidate_hash: str  # the candidate's artifact hash
    parent_optimizer_version: str | None  # optimizer target only: its lineage
    verdict_id: str | None
    decision: Decision | None
    reasons: tuple[str, ...]
    # Owner decision 19: the frozen protocol's link, copied by `derive` (contract
    # 0.11.0); out of the wire when absent, so stored revision hashes are unchanged.
    retest_of: str | None = None

    def __post_init__(self) -> None:
        if self.retest_of is not None:
            _id(self.retest_of, "retest_of")
            if self.retest_of == self.experiment_id:
                raise ImprovementError(
                    "retest_of", "an experiment never re-tests itself"
                )
        for n in ("experiment_id", "scope_id", "baseline_version", "candidate_version",
                  "protected_assessor_version", "budget_reservation_id",
                  "sandbox_profile_id"):  # fmt: skip
            _id(getattr(self, n), n)
        if self.parent_optimizer_version is not None:
            _id(self.parent_optimizer_version, "parent_optimizer_version")
        _sha(self.protocol_id, "protocol_id")
        _sha(self.candidate_hash, "candidate_hash")
        types = (("target", Target), ("scope", Scope), ("state", RecordState))
        if not all(type(getattr(self, n)) is t for n, t in types):
            raise TypeError("typed target, scope and state are required")
        h = self.hypothesis
        if not isinstance(h, str) or not h.strip() or len(h) > 4000 or "\x00" in h:
            raise ImprovementError("hypothesis", "1 to 4000 characters, no NUL")
        object.__setattr__(self, "data_snapshot_ids", tuple(self.data_snapshot_ids))
        for d in self.data_snapshot_ids:
            _id(d, "data_snapshot_id")
        lim = tuple(self.reasons)
        object.__setattr__(self, "reasons", lim)
        if len(lim) > 64 or lim != tuple(sorted(set(lim))) or not all(
            isinstance(x, str) and _REASON.fullmatch(x) for x in lim
        ):  # fmt: skip
            raise ImprovementError("reasons", "at most 64 sorted kind:detail codes")
        if len(self.data_snapshot_ids) > 64:
            raise ImprovementError("data_snapshot_ids", "at most 64")
        if (self.verdict_id is None) != (self.decision is None):
            raise ImprovementError("verdict", "a verdict id goes with its decision")
        if self.verdict_id is not None:
            _id(self.verdict_id, "verdict_id")
            if type(self.decision) is not Decision:
                raise TypeError("decision must be a Decision")
        optimizer = self.target is Target.RECURSIVE_OPTIMIZER
        if optimizer != (self.parent_optimizer_version is not None) or not (
            self._consistent(lim) or self._consistent_050(lim)
        ):  # fmt: skip
            raise ImprovementError("record_inconsistent", self.experiment_id)

    def _consistent(self, lim: tuple[str, ...]) -> bool:
        """0.6.0: the decision fixes the state; code and optimizer stay blocked."""
        sandbox, d = self.target in SANDBOX_TARGETS, self.decision
        if d is None:
            ok = self.state in _UNDECIDED or (sandbox and self.state is _S.BLOCKED)
        elif d is Decision.BLOCKED:
            ok = self.state is _S.BLOCKED and any(x.startswith("blocked:") for x in lim)
        else:
            ok = self.state is _DECIDED[d] and "none:evaluation_receipt_ids" in lim
        optimizer = self.target is Target.RECURSIVE_OPTIMIZER
        ok = ok and _NO_PARENT_050 not in lim and optimizer != (_NO_PARENT in lim)
        return ok and (not sandbox or (
            self.state is _S.BLOCKED and "blocked:sandbox_unqualified" in lim
        ))  # fmt: skip

    def _consistent_050(self, lim: tuple[str, ...]) -> bool:
        """The 0.5.0 rules (PR #126), verbatim, so rows stored then stay readable."""
        if self.decision is not None and self.decision in _DECIDED_050:
            ok = self.state is _DECIDED_050[self.decision] and (
                "none:evaluation_receipt_ids" in lim
            )  # fmt: skip
        else:  # unassessed or blocked: nothing past authorization
            ok = self.state in _UNDECIDED and (self.decision is None or any(
                x.startswith("blocked:") for x in lim))  # fmt: skip
        optimizer = self.target is Target.RECURSIVE_OPTIMIZER
        ok = ok and _NO_PARENT not in lim and optimizer != (_NO_PARENT_050 in lim)
        if self.target in SANDBOX_TARGETS:  # T007: never past proposed
            ok = ok and self.state is _S.PROPOSED and (
                "blocked:sandbox_unqualified" in lim
            )  # fmt: skip
        return ok

    @property
    def requires_human_promotion(self) -> bool:
        return True  # experiment.schema.json const; nothing here promotes

    @property
    def is_050(self) -> bool:
        """A record stored under 0.5.0 (the two rule sets admit disjoint records)."""
        return self._consistent_050(self.reasons)

    def may_follow(self, prev: "ExperimentRecord") -> bool:
        """A later revision: the same experiment, protocol and candidate, a state
        `_NEXT` allows after `prev` (nothing after awaiting_review, rejected or
        failed), and a verdict, if any, under a new verdict id: a re-assessment
        is a new verdict and the earlier one stays in the chain. A 0.5.0 record
        follows only a 0.5.0 one, by the 0.5.0 rank (never back to proposed,
        nothing after a decision)."""
        same = all(getattr(self, f.name) == getattr(prev, f.name)
                   for f in fields(self) if f.name not in _MOVING)  # fmt: skip
        if self.is_050:
            return (
                same
                and prev.is_050
                and prev.state in _UNDECIDED
                and not (prev.state is _S.AUTHORIZED and self.state is _S.PROPOSED)
            )
        return same and self.state in _NEXT.get(prev.state, ()) and (
            self.verdict_id is None or self.verdict_id != prev.verdict_id
        )  # fmt: skip

    def wire(self) -> dict[str, object]:
        """Plain values: enums as their values, tuples as lists; `retest_of` only
        when set."""
        out: dict[str, object] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "retest_of" and v is None:
                continue
            out[f.name] = list(v) if isinstance(v, tuple) else (
                v.value if isinstance(v, StrEnum) else v)  # fmt: skip
        return out


@dataclass(frozen=True, slots=True)
class Assessment:  # what `assess` needs beyond the registry, plan and book
    ledger: TrialLedger
    assessor: Party
    at: datetime
    verdict_id: str
    claimed: Mapping[str, Decimal] | None = None
    evaluation: EvaluationProtocol | None = None  # T033, bound by the plan's hash


def record(
    registry: ExperimentRegistry, experiment_id: str, candidate_id: str,
    plan: SplitPlan, book: BudgetBook, outcome: Scheduled | Deferred | None = None,
    assessment: Assessment | None = None,
) -> ExperimentRecord:  # fmt: skip
    """The experiment's record (`derive` without the verdict)."""
    return derive(registry, experiment_id, candidate_id, plan, book, outcome,
                  assessment)[0]  # fmt: skip


def derive(
    registry: ExperimentRegistry, experiment_id: str, candidate_id: str,
    plan: SplitPlan, book: BudgetBook, outcome: Scheduled | Deferred | None = None,
    assessment: Assessment | None = None, *, chain_trials: int | None = None,
) -> tuple[ExperimentRecord, Verdict | None]:  # fmt: skip
    """The record and the verdict it carries. A verdict comes only from running
    `assess` here on the given registry, ledger and book."""
    p, c = registry.protocol(experiment_id), registry.candidate(candidate_id)
    if c.experiment_id != experiment_id:
        raise ImprovementError("candidate_experiment", candidate_id)
    if plan.plan_id != p.plan_id or plan_digest(plan) != p.plan_digest:
        raise ImprovementError("test_access_changed", p.plan_id)
    if assessment is not None and p.target is Target.STRATEGY:
        # review M1: refuse before any verdict, so no holdout read is spent on an
        # assessment that cannot apply decision 35 (assess alone: insufficient)
        ev = assessment.evaluation
        if ev is None:
            raise ImprovementError("evaluation_protocol_missing", experiment_id)
        if type(ev) is not EvaluationProtocol or ev.content_hash != plan.protocol_hash:
            raise ImprovementError("evaluation_protocol_unbound", experiment_id)
    if p.sandbox_profile_id is None:  # the schema requires one; never invented
        raise ImprovementError("contract_field_unavailable", "sandbox_profile_id")
    if outcome is not None and outcome.experiment_id != experiment_id:
        raise ImprovementError("outcome_experiment", outcome.experiment_id)
    lim: set[str] = set()
    if p.target in SANDBOX_TARGETS:
        lim.add("blocked:sandbox_unqualified")
    optimizer = c.parent_id if p.target is Target.RECURSIVE_OPTIMIZER else None
    if optimizer is None:  # 0.6.0: null and named; never the producer's version
        lim.add(_NO_PARENT)
    if plan.dataset.evidence_class is EvidenceClass.SYNTHETIC:
        lim.add("evidence:synthetic_only")
    reserved = False
    if isinstance(outcome, Scheduled):
        r = book.find(p.budget_reservation_id)
        if book.scope_id != p.scope_id or r is None or not _bound(p, r):
            raise ImprovementError("reservation_unverified", experiment_id)
        reserved = True
    elif isinstance(outcome, Deferred):
        lim.add(f"deferred:{outcome.reason}")
    v = None if assessment is None else assess(
        registry, experiment_id, candidate_id, assessment.ledger, book,
        assessment.assessor, assessment.at, assessment.verdict_id,
        assessment.claimed, evaluation=assessment.evaluation,
        chain_trials=chain_trials)  # fmt: skip
    state = RecordState.AUTHORIZED if reserved else RecordState.PROPOSED
    if v is not None:
        kind = {Decision.BLOCKED: "blocked", Decision.INSUFFICIENT: "insufficient",
                Decision.REJECT: "rejected"}.get(v.decision)  # fmt: skip
        lim.update(f"{kind}:{r}" for r in v.reasons if kind)
        lim.update(f"evidence:{x}" for x in v.limitations)
        state = _DECIDED[v.decision]  # blocked, never failed, for missing budget
        if v.decision is not Decision.BLOCKED:
            lim.add("none:evaluation_receipt_ids")  # T044 has no receipt store yet
    if p.target in SANDBOX_TARGETS:  # T007 not qualified: blocked, never proposed
        state = RecordState.BLOCKED
    return ExperimentRecord(
        experiment_id, p.target, p.scope, p.scope_id, state, p.baseline_version,
        c.candidate_id, p.hypothesis, p.content_hash, p.assessor.version,
        (plan.dataset.dataset_id,), p.budget_reservation_id, p.sandbox_profile_id,
        c.artifact_hash, optimizer, None if v is None else v.verdict_id,
        None if v is None else v.decision, tuple(sorted(lim)),
        p.retest_of), v  # fmt: skip
