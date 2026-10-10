"""Experiment trial ledger and train/development/promotion access separation
(T032 increment 1). Spec §14 (preregistered chronological windows and a protected
final promotion evaluation, embargo, all trials registered, "additional final-test
queries count as evaluation access"), §11 (independent assessor; all assessment
access recorded), R029, R065, R084.
- A `SplitPlan` holds one frozen dataset manifest (its hash, evidence class and
  observed range are read from it, never redeclared; `register_plan` requires the
  dataset itself and refuses any other manifest) and an approver, with three
  embargoed chronological windows inside that range, optional holdout instruments
  and a declared promotion access limit.
- Every access request names its `Candidate` (family, material and config hash) and
  is recorded, granted or refused. Train/development stay
  inside their window and may not name a holdout instrument; promotion access is for
  the protected assessor role only and refused at the limit. `open_view` re-checks
  research rights and serves data only inside the access window, hiding holdout (and
  unresolved) instruments from train/development.
- Every trial, including failed and abandoned ones, is appended and must match the
  candidate its access was granted for and the plan's evaluation protocol hash.
  `trial_count` (for later multiple-testing adjustment) also counts granted
  accesses never bound to a trial. Only a completed promotion trial on the first
  holdout access yields a T027 `EvidenceRecord`; later ones are `holdout_reused`.
  Construction replays every entry (forged grants fail).
LIMITATIONS: no persistence; roles are caller-asserted; times must come from the
server clock; no multiple-testing adjustment is computed yet.
Stdlib only.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.filings import AsOfView
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.research_data import (
    DatasetManifest,
    EvidenceClass,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    Window,
    check_id,
    require_research_use,
)
from qw_domain.strategy_gate import EvidenceRecord

_SHA256 = re.compile(r"[a-f0-9]{64}", re.ASCII)


class Split(StrEnum):
    TRAIN = "train"
    DEVELOPMENT = "development"
    PROMOTION = "promotion"


class Role(StrEnum):
    RESEARCHER = "researcher"
    ASSESSOR = "protected_assessor"


class Outcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


def _sha(value: object, what: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ResearchError(what, f"{safe_repr(value)} is not a sha256 hex digest")


@dataclass(frozen=True, slots=True)
class Candidate:
    """What is evaluated: hypothesis family plus strategy material/config hashes."""

    family: str
    material_hash: str
    config_hash: str

    def __post_init__(self) -> None:
        check_id(self.family, "family")
        _sha(self.material_hash, "material_hash")
        _sha(self.config_hash, "config_hash")


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """Hash, evidence class and range come from `dataset`; a plan cannot relabel."""

    plan_id: str
    dataset: DatasetManifest
    train: Window
    development: Window
    promotion: Window
    embargo: timedelta
    holdout_instruments: frozenset[InstrumentId]
    max_promotion_accesses: int
    approved_by: str
    registered_at: datetime
    protocol_hash: str  # the evaluation protocol frozen with the plan (T033)

    def __post_init__(self) -> None:
        check_id(self.plan_id, "plan")
        _sha(self.protocol_hash, "protocol_hash")
        check_id(self.approved_by, "approver")
        if type(self.dataset) is not DatasetManifest:
            raise ResearchError("dataset", "a DatasetManifest is required")
        object.__setattr__(self, "registered_at", ensure_aware_utc(self.registered_at))
        object.__setattr__(
            self, "holdout_instruments", frozenset(self.holdout_instruments)
        )
        windows = (self.train, self.development, self.promotion)
        if not all(type(w) is Window for w in windows):
            raise ResearchError("window", "Window records are required")
        if type(self.embargo) is not timedelta or self.embargo < timedelta(0):
            raise ResearchError("embargo", "a non-negative timedelta")
        if not all(a.end + self.embargo <= b.start for a, b in pairwise(windows)):
            raise ResearchError(
                "chronology", "train < development < promotion, embargoed"
            )
        if not all(self.dataset.observed.covers(w) for w in windows):
            raise ResearchError(
                "observed", "split windows lie inside the dataset range"
            )
        n = self.max_promotion_accesses
        if type(n) is not int or n < 1:
            raise ResearchError("max_promotion_accesses", "a positive int")
        if not all(type(i) is InstrumentId for i in self.holdout_instruments):
            raise ResearchError("holdout_instruments", "InstrumentId values only")

    @property
    def dataset_hash(self) -> str:
        return self.dataset.content_hash

    def window(self, split: Split) -> Window:
        return {Split.TRAIN: self.train, Split.DEVELOPMENT: self.development,
                Split.PROMOTION: self.promotion}[split]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Access:
    access_id: str
    plan_id: str
    trial_id: str
    candidate: Candidate
    split: Split
    window: Window
    instruments: frozenset[InstrumentId] | None  # None: the whole permitted universe
    actor_id: str
    role: Role
    at: datetime
    granted: bool
    reasons: tuple[str, ...]
    seq: int  # promotion: n-th granted holdout access; otherwise 0

    def __post_init__(self) -> None:
        for name in ("access_id", "plan_id", "trial_id", "actor_id"):
            check_id(getattr(self, name), name)
        object.__setattr__(self, "at", ensure_aware_utc(self.at))
        if not isinstance(self.split, Split) or not isinstance(self.role, Role):
            raise ResearchError("access", "typed split and role are required")
        if type(self.window) is not Window or type(self.candidate) is not Candidate:
            raise ResearchError("access", "a Window and a Candidate are required")


@dataclass(frozen=True, slots=True)
class Trial:
    trial_id: str
    plan_id: str
    family: str  # hypothesis family for multiple-testing accounting
    hypothesis: str
    material_hash: str
    config_hash: str
    dataset_hash: str
    protocol_hash: str
    split: Split
    outcome: Outcome
    metrics: Mapping[str, Decimal]
    access_id: str | None  # None only for a trial that never reached data
    recorded_at: datetime

    def __post_init__(self) -> None:
        for name in ("trial_id", "plan_id", "family"):
            check_id(getattr(self, name), name)
        for name in ("material_hash", "config_hash", "dataset_hash", "protocol_hash"):
            _sha(getattr(self, name), name)
        if self.access_id is not None:
            check_id(self.access_id, "access")
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ResearchError("hypothesis", "a stated hypothesis is required")
        if not isinstance(self.split, Split) or not isinstance(self.outcome, Outcome):
            raise ResearchError("trial", "typed split and outcome are required")
        metrics = dict(self.metrics)
        for k, v in metrics.items():
            check_id(k, "metrics")
            if type(v) is not Decimal or not v.is_finite():
                raise ResearchError("metrics", f"{k}: a finite Decimal is required")
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(self, "recorded_at", ensure_aware_utc(self.recorded_at))

    def metrics_wire(self) -> dict[str, str]:
        return {k: str(v) for k, v in sorted(self.metrics.items())}


type Entry = SplitPlan | Access | Trial


@dataclass(slots=True)
class _State:
    plans: dict[str, SplitPlan] = field(default_factory=dict)
    accesses: dict[str, Access] = field(default_factory=dict)
    trials: dict[str, Trial] = field(default_factory=dict)
    bound: set[str] = field(default_factory=set)  # access ids used by a trial
    last: datetime | None = None


def _decide(
    s: _State, p: SplitPlan, split: Split, window: Window,
    instruments: frozenset[InstrumentId] | None, role: Role,
) -> tuple[tuple[str, ...], int]:  # fmt: skip
    reasons: list[str] = []
    if not p.window(split).covers(window):
        reasons.append("outside_split_window")
    seq = 0
    if split is Split.PROMOTION:
        if role is not Role.ASSESSOR:
            reasons.append("assessor_only")
        mine = [a for a in s.accesses.values() if a.plan_id == p.plan_id]
        used = sum(1 for a in mine if a.granted and a.split is split)
        if used >= p.max_promotion_accesses:
            reasons.append("holdout_exhausted")
        seq = 0 if reasons else used + 1
    elif instruments is not None and instruments & p.holdout_instruments:
        reasons.append("holdout_instrument")
    return tuple(reasons), seq


def _apply(s: _State, e: Entry) -> None:
    when = e.registered_at if isinstance(e, SplitPlan) else (
        e.at if isinstance(e, Access) else e.recorded_at)  # fmt: skip
    if s.last is not None and when < s.last:
        raise ResearchError("time_order", "ledger entries are appended in time order")
    if isinstance(e, SplitPlan):
        if e.plan_id in s.plans:
            raise ResearchError("duplicate", e.plan_id)
        s.plans[e.plan_id] = e
    elif isinstance(e, Access):
        p = s.plans.get(e.plan_id)
        if p is None:
            raise ResearchError("unknown_plan", e.plan_id)
        if e.access_id in s.accesses:
            raise ResearchError("duplicate", e.access_id)
        reasons, seq = _decide(s, p, e.split, e.window, e.instruments, e.role)
        if (e.granted, e.reasons, e.seq) != (not reasons, reasons, seq):
            raise ResearchError("integrity", f"access {e.access_id} does not replay")
        s.accesses[e.access_id] = e
    else:
        if type(e) is not Trial:
            raise ResearchError("entry", type(e).__name__)
        if e.trial_id in s.trials:
            raise ResearchError("duplicate", e.trial_id)
        p = s.plans.get(e.plan_id)
        if p is None:
            raise ResearchError("unknown_plan", e.plan_id)
        if e.dataset_hash != p.dataset_hash:
            raise ResearchError("dataset_mismatch", e.trial_id)
        if e.protocol_hash != p.protocol_hash:
            raise ResearchError("protocol_changed", e.trial_id)
        if e.access_id is None:
            if e.outcome is Outcome.COMPLETED:
                raise ResearchError("access_required", e.trial_id)
        else:
            a = s.accesses.get(e.access_id)
            if (
                a is None or not a.granted or e.access_id in s.bound
                or (a.trial_id, a.plan_id, a.split) != (e.trial_id, e.plan_id, e.split)
            ):  # fmt: skip
                raise ResearchError("access_mismatch", e.trial_id)
            if a.candidate != Candidate(e.family, e.material_hash, e.config_hash):
                raise ResearchError("candidate_mismatch", e.trial_id)
            s.bound.add(e.access_id)
        s.trials[e.trial_id] = e
    s.last = when


@dataclass(frozen=True, slots=True)
class TrialLedger:
    """Append-only, per tenant. Use the methods; construction replays all entries."""

    tenant_id: str
    entries: tuple[Entry, ...] = ()
    _state: _State = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        check_id(self.tenant_id, "tenant")
        object.__setattr__(self, "entries", tuple(self.entries))
        state = _State()
        for e in self.entries:
            _apply(state, e)
        object.__setattr__(self, "_state", state)

    def _append(self, e: Entry) -> "TrialLedger":
        return TrialLedger(self.tenant_id, (*self.entries, e))

    def register_plan(self, plan: SplitPlan, dataset: ResearchDataset) -> "TrialLedger":
        """Register `plan` for `dataset`, whose hash was re-derived from its content
        on construction; a manifest that differs in any field is refused."""
        if type(dataset) is not ResearchDataset or dataset.manifest != plan.dataset:
            raise ResearchError("dataset_mismatch", plan.plan_id)
        return self._append(plan)

    def request_access(
        self, access_id: str, plan_id: str, trial_id: str, candidate: Candidate,
        split: Split, window: Window, instruments: frozenset[InstrumentId] | None,
        actor_id: str, role: Role, at: datetime,
    ) -> tuple["TrialLedger", Access]:  # fmt: skip
        """Decide and record the access request; refusals are recorded too."""
        p = self.plan(plan_id)
        if instruments is not None:
            instruments = frozenset(instruments)
        reasons, seq = _decide(self._state, p, split, window, instruments, role)
        a = Access(access_id, plan_id, trial_id, candidate, split, window, instruments,
                   actor_id, role, at, not reasons, reasons, seq)  # fmt: skip
        return self._append(a), a

    def record(self, trial: Trial) -> "TrialLedger":
        return self._append(trial)

    def plan(self, plan_id: str) -> SplitPlan:
        if (p := self._state.plans.get(plan_id)) is None:
            raise ResearchError("unknown_plan", safe_repr(plan_id))
        return p

    def access(self, access_id: str) -> Access:
        if (a := self._state.accesses.get(access_id)) is None:
            raise ResearchError("unknown_access", safe_repr(access_id))
        return a

    def accesses(self) -> tuple[Access, ...]:
        return tuple(self._state.accesses.values())

    def trials(self, family: str | None = None) -> tuple[Trial, ...]:
        return tuple(t for t in self._state.trials.values()
                     if family is None or t.family == family)  # fmt: skip

    def unrecorded_accesses(self, family: str) -> tuple[Access, ...]:
        """Granted accesses in `family` that no recorded trial is bound to."""
        return tuple(a for a in self._state.accesses.values()
                     if a.granted and a.candidate.family == family
                     and a.access_id not in self._state.bound)  # fmt: skip

    def trial_count(self, family: str) -> int:
        """Distinct trial ids in `family` that were recorded (any outcome) or saw
        data through a granted access, recorded or not; refused accesses saw no data.
        This is the count a multiple-testing adjustment must use."""
        seen = {a.trial_id for a in self.unrecorded_accesses(family)}
        return len(seen | {t.trial_id for t in self.trials(family)})

    def evidence_record(self, trial_id: str) -> EvidenceRecord:
        """The T027 evidence-axis record for a qualifying promotion trial."""
        t = self._state.trials.get(trial_id)
        if t is None:
            raise ResearchError("unknown_trial", safe_repr(trial_id))
        if t.split is not Split.PROMOTION:
            raise ResearchError("not_promotion", trial_id)
        if t.outcome is not Outcome.COMPLETED or t.access_id is None:
            raise ResearchError("not_completed", trial_id)
        if self._state.accesses[t.access_id].seq != 1:
            raise ResearchError("holdout_reused", trial_id)
        klass = self.plan(t.plan_id).dataset.evidence_class
        synthetic = klass is EvidenceClass.SYNTHETIC
        return EvidenceRecord(t.trial_id, t.material_hash, t.config_hash, None,
                              synthetic, t.recorded_at)  # fmt: skip


@dataclass(frozen=True, slots=True)
class SplitView:
    """The dataset as one granted access may see it."""

    dataset: ResearchDataset
    plan: SplitPlan
    access: Access

    def _allowed(self, iid: InstrumentId | None) -> bool:
        a, holdout = self.access, self.plan.holdout_instruments
        if a.instruments is not None and iid not in a.instruments:
            return False
        if a.split is Split.PROMOTION or not holdout:
            return True
        return iid is not None and iid not in holdout

    def _time(self, t: datetime) -> datetime:
        t = ensure_aware_utc(t)
        if not self.access.window.contains(t):
            raise ResearchError("outside_access_window", self.access.access_id)
        return t

    def facts_at(self, t: datetime) -> AsOfView:
        view = self.dataset.facts_at(self._time(t))
        book = self.dataset.facts.facts
        hidden = {f.key for f in book if not self._allowed(f.instrument.instrument_id)}
        facts = {k: f for k, f in view.facts.items() if k not in hidden}
        return AsOfView(facts, frozenset(view.conflicted - hidden))

    def members(self, on: date, known_by: datetime) -> frozenset[InstrumentId]:
        known_by = self._time(known_by)
        if on > known_by.date():
            raise ResearchError("future_date", "membership on a date not yet reached")
        raw = self.dataset.members(on, known_by)
        return frozenset(i for i in raw if self._allowed(i))


def open_view(
    ledger: TrialLedger,
    dataset: ResearchDataset,
    access_id: str,
    rights: ResearchRights,
) -> SplitView:
    a = ledger.access(access_id)
    if not a.granted:
        raise ResearchError("access_refused", access_id)
    p = ledger.plan(a.plan_id)
    if dataset.manifest != p.dataset:  # hash, evidence class, range and terms
        raise ResearchError("dataset_mismatch", access_id)
    if not ledger.tenant_id == dataset.manifest.tenant_id == rights.tenant_id:
        raise ResearchError("tenant_mismatch", access_id)
    require_research_use(rights, dataset.manifest.sources)
    return SplitView(dataset, p, a)
