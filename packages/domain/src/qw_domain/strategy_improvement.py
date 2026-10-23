"""Strategy/ranking improvement path (T047 increment 1; spec §11 strategy row, §14,
§8; R028, R029, R033, R034, R059, R065, R084, R099). Domain only, stdlib only.
- A `SearchSpace` declares which parameters of one registered strategy version a
  candidate may change, inside tighter bounds than the manifest's, plus a trial
  budget and the minimum number of prospective shadow slots. Its hash is frozen in
  the T044 protocol (`mutation_space_hash`) with the scorecard, splits and holdout.
  Dimensions naming risk, policy, threshold, approval, holdout or budget controls
  are refused: those stay outside candidate authority.
- A candidate is a derived manifest version whose only material change is new
  defaults for declared dimensions (no code: that is T049). Its artifact hash is
  the derived material hash and its payload the resolved defaults, so the T032
  ledger key equals the one `shadow.open_run` derives, and an equal set of
  defaults under a new id is the T044 `identical_artifact` relabel.
- `SearchSpace` and `Dimension` have a strict stored form (`to_wire`/`from_wire`)
  for the study input store (owner decisions 72 and 74); `content_hash` is
  unchanged. `candidate_manifest` re-derives a candidate's manifest from the base,
  the space and its payload and requires the candidate's bound hashes (decision 78).
- `request_research_access` refuses reused trial ids and goes no further than the
  trial budget; `account` counts every trial and unrecorded access on the plan or
  in the family (failed, abandoned, other families, unregistered variants).
- `holdout_run` (owner decision 39) is the assessor's own promotion run (no
  promotion of the candidate; nothing here promotes): it refuses
  before reading anything (study, registered assessor, frozen plan, the candidate's
  manifest version, the freeze time, any promotion entry not made by the registered
  assessor, any earlier granted promotion read, any unrecorded trial: decision
  50), then records the one assessor read and the walk-forward on the caller's
  `HoldoutData` as a promotion trial (`run_promotion_trial`, recorded at `at`; a
  failed evaluation still spends the read). A completed trial also carries the
  hard controls the run derives (decisions 46 and 49, `DERIVED_BASIS`):
  look-ahead 0, unrecorded trials 0, and survivorship 0 only for the plan's single
  preregistered holdout instrument (else missing). The caller then derives the
  verdict with `assessed_at == at`.
- `assess_strategy` runs the T044 assessor (holdout read once by its own actor),
  then rejects on ledger-derived violations (unrecorded trials, budget overrun,
  unregistered variants, other families), then needs a prospective shadow window
  bound to a ledger trial of the candidate, registered after its research trials
  and scored now from the captured book (no hindsight); otherwise the result is
  at most insufficient evidence, i.e. research only. Record `recordable_verdict`.
- `adoption_candidate` re-checks the assessment and the latest recorded verdict,
  then bundles the diff, protocol, every trial, verdict and prospective report
  with the required approval classes. It grants, installs and adopts nothing.
LIMITATIONS: no persistence; times and actors are caller-asserted; the strategy
callable built from the configuration is trusted to honour it; one instrument per
prospective run; significance is never claimed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from qw_domain import improvement as im
from qw_domain.backtest import (
    EvaluationProtocol,
    PriceSeries,
    Strategy,
    TrialOutcome,
    TrialReport,
    ledger_report,
    run_promotion_trial,
    run_trial,
)
from qw_domain.budget import BudgetBook
from qw_domain.decimals import safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    EvidenceClass,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    Window,
    digest,
)
from qw_domain.shadow import ShadowBook, ShadowReport, ShadowRun, score
from qw_domain.strategy_registry import (
    EvidenceState,
    Parameter,
    ParamKind,
    ParamValue,
    StrategyManifest,
    StrategyRegistry,
)
from qw_domain.trial_ledger import Access, Role, Split, TrialLedger
from qw_domain.trial_ledger import Candidate as TrialKey

Error = im.ImprovementError
type Diff = dict[str, list[str]]  # parameter -> [base default, candidate default]
_PROTECTED = ("risk", "policy", "threshold", "approval", "holdout", "budget",
              "limit", "scorecard", "assessor", "consent")  # fmt: skip


def _bound(value: object) -> bool:
    return type(value) is int or (type(value) is Decimal and value.is_finite())


@dataclass(frozen=True, slots=True)
class Dimension:
    """One parameter a candidate may set: numeric bounds, or allowed choices."""

    name: str
    minimum: int | Decimal | None = None
    maximum: int | Decimal | None = None
    allowed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        im._id(self.name, "dimension")
        flat = "".join(ch for ch in self.name.lower() if ch.isalnum())
        if any(word in flat for word in _PROTECTED):  # normalised containment
            raise Error("protected_dimension", self.name)
        lo, hi = self.minimum, self.maximum
        if (lo is None) != (hi is None):
            raise Error("dimension", f"{self.name}: both bounds or neither")
        if lo is not None and not (_bound(lo) and _bound(hi)):
            raise TypeError("dimension bounds are ints or finite Decimals")
        if lo is not None and lo > hi:  # type: ignore[operator]
            raise Error("dimension", f"{self.name}: minimum above maximum")
        allowed = tuple(self.allowed)
        object.__setattr__(self, "allowed", allowed)
        if not all(type(a) is str for a in allowed) or (lo is not None and allowed):
            raise Error("dimension", f"{self.name}: string choices without bounds")

    def admits(self, value: object) -> bool:
        if self.allowed:
            return value in self.allowed
        if self.minimum is None:
            return True
        return self.minimum <= value <= self.maximum  # type: ignore[operator]

    def wire(self) -> list[object]:
        b = [None if x is None else str(x) for x in (self.minimum, self.maximum)]
        return [self.name, *b, list(self.allowed)]

    def to_wire(self) -> dict[str, object]:
        """The stored form: bounds as the decimal strings the hashed `wire` holds,
        so one hash has one wire (5 and Decimal(5) store alike, and compare equal)."""
        _, lo, hi, allowed = self.wire()
        return {"name": self.name, "minimum": lo, "maximum": hi,
                "allowed": allowed}  # fmt: skip

    @classmethod
    def from_wire(cls, w: object) -> "Dimension":
        """Canonical input only: exact keys, both bounds null or both decimal
        strings `str` gives back unchanged (no float, bool, `+`, spaces,
        underscores or exponent respelling); bounds load as Decimal."""
        keys = {"name", "minimum", "maximum", "allowed"}
        if type(w) is not dict or set(w) != keys:
            raise Error("dimension_wire", "an object with exactly the dimension keys")
        allowed, bounds = w["allowed"], (w["minimum"], w["maximum"])
        if type(allowed) is not list or not all(type(a) is str for a in allowed):
            raise Error("dimension_wire", "allowed is a list of strings")
        if bounds != (None, None) and not all(type(b) is str for b in bounds):
            raise Error("dimension_wire", "bounds are both null or both strings")
        try:
            lo, hi = (None, None) if bounds == (None, None) else map(Decimal, bounds)
            d = cls(w["name"], lo, hi, tuple(allowed))
        except (ValueError, ArithmeticError, TypeError) as exc:
            if isinstance(exc, Error):
                raise
            raise Error("dimension_wire", type(exc).__name__) from None
        if d.to_wire() != w:
            raise Error("dimension_wire", f"{d.name}: not the canonical wire form")
        return d


@dataclass(frozen=True, slots=True)
class SearchSpace:
    space_id: str
    strategy_id: str
    base_version: str
    base_material_hash: str
    dimensions: tuple[Dimension, ...]
    max_trials: int  # research (non-promotion) trials, recorded or not
    min_prospective_scored: int  # floor on a shadow run's declared minimum

    def __post_init__(self) -> None:
        for n in ("space_id", "strategy_id", "base_version"):
            im._id(getattr(self, n), n)
        im._sha(self.base_material_hash, "base_material_hash")
        dims = tuple(self.dimensions)
        object.__setattr__(self, "dimensions", dims)
        if not dims or not all(type(d) is Dimension for d in dims):
            raise Error("dimensions", "at least one Dimension")
        if len({d.name for d in dims}) != len(dims):
            raise Error("dimensions", "one Dimension per parameter")
        for n in ("max_trials", "min_prospective_scored"):
            v = getattr(self, n)
            if type(v) is not int or v < 1:
                raise Error(n, "a positive int")

    @property
    def content_hash(self) -> str:
        return digest([self.space_id, self.strategy_id, self.base_version,
                       self.base_material_hash, sorted(d.wire() for d in
                       self.dimensions), self.max_trials,
                       self.min_prospective_scored])  # fmt: skip

    def to_wire(self) -> dict[str, object]:
        """The stored form, dimensions in name order: `content_hash` ignores their
        order, so one content has one wire."""
        dims = sorted(self.dimensions, key=lambda d: d.name)
        return {"space_id": self.space_id, "strategy_id": self.strategy_id,
                "base_version": self.base_version,
                "base_material_hash": self.base_material_hash,
                "dimensions": [d.to_wire() for d in dims],
                "max_trials": self.max_trials,
                "min_prospective_scored": self.min_prospective_scored}  # fmt: skip

    @classmethod
    def from_wire(cls, w: object) -> "SearchSpace":
        """Canonical input only: exact keys, JSON ints (never bools or floats) for
        the counts, dimensions in name order; it must re-serialize to `w`."""
        keys = {"space_id", "strategy_id", "base_version", "base_material_hash",
                "dimensions", "max_trials", "min_prospective_scored"}  # fmt: skip
        if type(w) is not dict or set(w) != keys:
            raise Error("space_wire", "an object with exactly the search space keys")
        if type(w["dimensions"]) is not list:
            raise Error("space_wire", "dimensions must be a list")
        s = cls(w["space_id"], w["strategy_id"], w["base_version"],
                w["base_material_hash"],
                tuple(Dimension.from_wire(d) for d in w["dimensions"]),
                w["max_trials"], w["min_prospective_scored"])  # fmt: skip
        if s.to_wire() != w:
            raise Error("space_wire", f"{s.space_id}: not the canonical wire form")
        return s

    def check_base(self, base: StrategyManifest) -> None:
        if (base.strategy_id, base.version, base.material_hash) != (
            self.strategy_id, self.base_version, self.base_material_hash
        ):  # fmt: skip
            raise Error("space_mismatch", f"{base.strategy_id}@{base.version}")
        params = {p.name: p for p in base.parameters}
        for d in self.dimensions:
            p = params.get(d.name)
            if p is None:
                raise Error("unknown_parameter", d.name)
            numeric = p.kind in (ParamKind.INTEGER, ParamKind.DECIMAL)
            bounds = [b for b in (d.minimum, d.maximum) if b is not None]
            shaped = numeric == bool(bounds) and (
                p.kind is ParamKind.CHOICE
            ) == bool(d.allowed)  # fmt: skip
            # Bounds compare by value (a stored space loads them as Decimal): inside
            # the parameter's bounds, and whole numbers for an integer parameter.
            if not shaped or not all(
                p.minimum <= b <= p.maximum  # type: ignore[operator]
                and (p.kind is not ParamKind.INTEGER or b == int(b))
                for b in bounds
            ) or any(p.check(a) is not None for a in d.allowed):  # fmt: skip
                raise Error("dimension_bounds", d.name)


def _default(m: StrategyManifest, name: str) -> str:
    return next(str(p.to_wire()[2]) for p in m.parameters if p.name == name)


def derive(
    base: StrategyManifest, space: SearchSpace, values: Mapping[str, ParamValue],
    version: str,
) -> StrategyManifest:  # fmt: skip
    """The candidate version: `base` with new defaults for declared dimensions."""
    space.check_base(base)
    dims = {d.name: d for d in space.dimensions}
    params = {p.name: p for p in base.parameters}
    for k, v in values.items():
        d = dims.get(k)
        if d is None or params[k].check(v) is not None or not d.admits(v):
            raise Error("outside_search_space", f"{safe_repr(k)}={safe_repr(v)}")
    new = tuple(replace(p, default=values.get(p.name, p.default))
                for p in base.parameters)  # fmt: skip
    out = replace(base, version=version, parameters=new)
    if out.material_hash == base.material_hash:
        raise Error("no_change", version)
    return out


def version_diff(base: StrategyManifest, derived: StrategyManifest) -> Diff:
    """Changed defaults as [base, candidate] wire values; anything else is refused."""
    names = [p.name for p in base.parameters]
    if [p.name for p in derived.parameters] != names:
        raise Error("outside_search_space", "parameter declarations changed")
    diff = {n: [_default(base, n), _default(derived, n)] for n in names}
    return {n: v for n, v in diff.items() if v[0] != v[1]}


def check_derived(
    base: StrategyManifest, space: SearchSpace, derived: StrategyManifest
) -> None:
    """`derived` differs from `base` only in declared, in-bounds defaults."""
    diff = version_diff(base, derived)
    if derived.title != base.title:
        raise Error("outside_search_space", "display title changed")
    values = {p.name: p.default for p in derived.parameters if p.name in diff}
    if derive(base, space, values, derived.version).material_hash != (
        derived.material_hash
    ):  # fmt: skip
        raise Error("outside_search_space", "a material change beyond defaults")


def strategy_candidate(
    base: StrategyManifest, space: SearchSpace, derived: StrategyManifest, *,
    candidate_id: str, experiment_id: str, parent_id: str, producer: im.Party,
    runtime_versions: Mapping[str, str], created_at: datetime,
) -> im.Candidate:  # fmt: skip
    check_derived(base, space, derived)
    if derived.version != candidate_id:
        raise Error("version_mismatch", candidate_id)
    payload = {p.name: str(p.to_wire()[2]) for p in derived.parameters}
    c = im.Candidate(candidate_id, experiment_id, im.Target.STRATEGY, parent_id,
                     producer, derived.material_hash, payload, runtime_versions, (),
                     created_at)  # fmt: skip
    if c.payload_hash != derived.config_hash({}):
        raise Error("payload_mismatch", candidate_id)
    return c


def _parse(p: Parameter, text: str) -> ParamValue:
    """A payload string as `p`'s type; only its canonical rendering is accepted."""
    value: ParamValue
    try:
        if p.kind is ParamKind.INTEGER:
            value = int(text)
        elif p.kind is ParamKind.DECIMAL:
            value = Decimal(text)
        elif p.kind is ParamKind.BOOLEAN:
            value = {"true": True, "false": False}[text]
        else:
            value = text
    except (ValueError, ArithmeticError, KeyError):
        raise Error("payload_mismatch", f"{p.name}={safe_repr(text)}") from None
    if p.check(value) is not None:
        raise Error("outside_search_space", f"{p.name}={safe_repr(text)}")
    if replace(p, default=value).to_wire()[2] != text:
        raise Error("payload_mismatch", f"{p.name}={safe_repr(text)} not canonical")
    return value


def candidate_manifest(
    base: StrategyManifest, space: SearchSpace, candidate: im.Candidate
) -> StrategyManifest:
    """Re-derive the candidate's manifest (version = candidate id) from the base,
    the space and the payload, and require the candidate's bound hashes (owner
    decision 78: re-derived, never stored). Only defaults that differ from the
    base reach `derive`, which refuses any undeclared key."""
    if candidate.target is not im.Target.STRATEGY:
        raise Error("target", candidate.candidate_id)
    params = {p.name: p for p in base.parameters}
    if set(candidate.payload) != set(params):
        raise Error("payload_mismatch", candidate.candidate_id)
    values = {n: _parse(params[n], t) for n, t in candidate.payload.items()
              if t != _default(base, n)}  # fmt: skip
    m = derive(base, space, values, candidate.candidate_id)
    if (m.material_hash, m.config_hash({})) != (
        candidate.artifact_hash, candidate.payload_hash
    ):  # fmt: skip
        raise Error("candidate_mismatch", candidate.candidate_id)
    return m


@dataclass(frozen=True, slots=True)
class StrategyStudy:
    """A T044 strategy protocol with its frozen search space and backtest protocol."""

    protocol: im.Protocol
    space: SearchSpace
    evaluation: EvaluationProtocol
    base: StrategyManifest

    def __post_init__(self) -> None:
        p, s = self.protocol, self.space
        if p.target is not im.Target.STRATEGY:
            raise Error("target", p.experiment_id)
        if p.mutation_space_hash != s.content_hash:
            raise Error("search_space_not_frozen", p.experiment_id)
        s.check_base(self.base)
        if (p.baseline_version, p.baseline_hash) != (
            s.base_version, s.base_material_hash
        ):  # fmt: skip
            raise Error("baseline_mismatch", p.experiment_id)
        if self.evaluation.plan_id != p.plan_id:
            raise Error("plan_mismatch", p.experiment_id)

    @property
    def family(self) -> str:  # the T032 hypothesis family of every trial
        return self.protocol.experiment_id

    def check_plan(self, ledger: TrialLedger) -> None:
        plan = ledger.plan(self.protocol.plan_id)
        if plan.protocol_hash != self.evaluation.content_hash or (
            im.plan_digest(plan) != self.protocol.plan_digest
        ):  # fmt: skip
            raise Error("test_access_changed", plan.plan_id)


type _Row = tuple[str, Split, str, str, str, str, bool]  # id, split, family, plan,
# material, config, recorded


def _scope(ledger: TrialLedger, family: str, plan_id: str) -> list[_Row]:
    """Every trial and unrecorded granted access on the plan or in the family."""
    bound = {t.access_id for t in ledger.trials()}
    rows = [(t.trial_id, t.split, t.family, t.plan_id, t.material_hash,
             t.config_hash, True) for t in ledger.trials()]  # fmt: skip
    rows += [(a.trial_id, a.split, a.candidate.family, a.plan_id,
              a.candidate.material_hash, a.candidate.config_hash, False)
             for a in ledger.accesses()
             if a.granted and a.access_id not in bound]  # fmt: skip
    return [r for r in rows if family == r[2] or plan_id == r[3]]


def _research(rows: list[_Row]) -> set[str]:
    return {r[0] for r in rows if r[1] is not Split.PROMOTION}


def request_research_access(
    study: StrategyStudy, ledger: TrialLedger, candidate: im.Candidate,
    access_id: str, trial_id: str, split: Split, window: Window, actor_id: str,
    at: datetime,
) -> tuple[TrialLedger, Access]:  # fmt: skip
    """A train/development access for `candidate`, refused past the trial budget."""
    if split is Split.PROMOTION:
        raise Error("assessor_only", "the holdout is read by the assessor")
    if candidate.experiment_id != study.family:
        raise Error("candidate_experiment", candidate.candidate_id)
    study.check_plan(ledger)
    used = {t.trial_id for t in ledger.trials()} | {
        a.trial_id for a in ledger.accesses()
    }  # fmt: skip
    if trial_id in used:
        raise Error("trial_id_reused", trial_id)
    seen = _research(_scope(ledger, study.family, study.protocol.plan_id))
    if len(seen) >= study.space.max_trials:
        raise Error("trial_budget_exhausted", f"{len(seen)} trials")
    return ledger.request_access(access_id, study.protocol.plan_id, trial_id,
                                 candidate.trial_key, split, window, None, actor_id,
                                 Role.RESEARCHER, at)  # fmt: skip


def _version(
    study: StrategyStudy, strategies: StrategyRegistry, c: im.Candidate
) -> StrategyManifest:
    m = strategies.manifest(study.base.strategy_id, c.candidate_id)
    check_derived(study.base, study.space, m)
    if (m.material_hash, m.config_hash({})) != (c.artifact_hash, c.payload_hash):
        raise Error("candidate_mismatch", c.candidate_id)
    return m


def develop(
    study: StrategyStudy, registry: im.ExperimentRegistry,
    strategies: StrategyRegistry, ledger: TrialLedger, candidate_id: str,
    access_id: str, trial_id: str, recorded_at: datetime, dataset: ResearchDataset,
    rights: ResearchRights, series: PriceSeries,
    build: Callable[[Mapping[str, ParamValue]], Strategy],
    baseline_series: Mapping[str, PriceSeries],
) -> tuple[TrialLedger, TrialOutcome]:  # fmt: skip
    """Walk-forward backtest of the candidate's defaults on its granted access; the
    trial is recorded whatever happens (T033 `run_trial`)."""
    c = registry.candidate(candidate_id)
    if c.experiment_id != study.family or (
        registry.protocol(study.family) != study.protocol
    ):  # fmt: skip
        raise Error("study_mismatch", candidate_id)
    m = _version(study, strategies, c)
    if ledger.access(access_id).candidate != c.trial_key:
        raise Error("candidate_mismatch", access_id)
    study.check_plan(ledger)
    return run_trial(ledger, trial_id, study.protocol.hypothesis, recorded_at,
                     dataset, rights, access_id, study.evaluation, series,
                     build(MappingProxyType(m.resolve({}))),
                     baseline_series)  # fmt: skip


@dataclass(frozen=True, slots=True)
class TrialAccounting:
    report: TrialReport  # every trial in the family, any outcome
    trial_count: int  # distinct trials on the plan or in the family, any outcome
    research_trials: int
    max_trials: int
    unrecorded: int  # granted accesses with no recorded trial
    unregistered: tuple[str, ...]  # trials of variants never registered
    foreign: tuple[str, ...]  # trials on the plan under another family (or plan)

    @property
    def violations(self) -> tuple[str, ...]:
        checks = (("hard:unrecorded_trials", self.unrecorded > 0),
                  ("trial_budget_exceeded", self.research_trials > self.max_trials),
                  ("unregistered_variant_trials", bool(self.unregistered)),
                  ("foreign_family_trials", bool(self.foreign)))  # fmt: skip
        return tuple(name for name, bad in checks if bad)


def account(
    study: StrategyStudy, registry: im.ExperimentRegistry, ledger: TrialLedger
) -> TrialAccounting:
    f, plan_id = study.family, study.protocol.plan_id
    rows = _scope(ledger, f, plan_id)
    known = {(c.artifact_hash, c.payload_hash) for c in registry.entries
             if isinstance(c, im.Candidate) and c.experiment_id == f}  # fmt: skip
    known.add((study.base.material_hash, study.base.config_hash({})))
    foreign = {r[0] for r in rows if r[2:4] != (f, plan_id)}
    stray = {r[0] for r in rows if r[4:6] not in known} - foreign
    return TrialAccounting(ledger_report(ledger, f), len({r[0] for r in rows}),
                           len(_research(rows)), study.space.max_trials,
                           sum(1 for r in rows if not r[6]), tuple(sorted(stray)),
                           tuple(sorted(foreign)))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Prospective:
    """The captured shadow book and the bars to score it from."""

    book: ShadowBook
    series: PriceSeries
    baseline_series: Mapping[str, PriceSeries]
    declared_hash: str  # the run's declaration hash recorded at registration


@dataclass(frozen=True, slots=True)
class StrategyAssessment:
    verdict: im.Verdict  # the T044 assessor's verdict, recorded by the caller
    decision: im.Decision
    reasons: tuple[str, ...]
    accounting: TrialAccounting
    prospective: ShadowReport | None
    evidence_ceiling: EvidenceState
    limitations: tuple[str, ...]
    significance_claimed: bool = False

    @property
    def adoption_eligible(self) -> bool:
        return self.decision is im.Decision.ACCEPT_FOR_REVIEW

    @property
    def research_only(self) -> bool:
        return not self.adoption_eligible


def _run_checks(
    study: StrategyStudy, c: im.Candidate, ledger: TrialLedger, r: ShadowRun,
    series: EvidenceClass, holdout_synthetic: bool,
) -> list[str]:  # fmt: skip
    """Why the shadow run is not a valid prospective window for `c` (none: valid)."""
    if (r.tenant_id, r.strategy_id, r.version, r.candidate) != (
        ledger.tenant_id, study.base.strategy_id, c.candidate_id, c.trial_key
    ):  # fmt: skip
        return ["prospective_not_candidate"]
    found = [t for t in ledger.trials() if t.trial_id == r.trial_id]
    if not found:
        return ["prospective_trial_unknown"]
    if TrialKey(found[0].family, found[0].material_hash, found[0].config_hash) != (
        c.trial_key
    ):  # fmt: skip
        return ["prospective_trial_not_candidate"]
    mine = [t.recorded_at for t in ledger.trials(c.experiment_id)
            if t.split is not Split.PROMOTION and (t.material_hash, t.config_hash)
            == (c.artifact_hash, c.payload_hash)]  # fmt: skip
    why = []
    if r.registered_at < max([found[0].recorded_at, *mine]):
        why.append("prospective_before_trial")  # the run follows every research trial
    if series is EvidenceClass.SYNTHETIC and not holdout_synthetic:
        why.append("prospective_synthetic_series")
    if r.protocol_hash != study.evaluation.content_hash:
        why.append("prospective_protocol_changed")
    if r.registered_at < max(study.protocol.registered_at, c.created_at):
        why.append("prospective_before_freeze")
    if r.min_scored < study.space.min_prospective_scored:
        why.append("prospective_minimum_below_study")
    return why


def _prospective(
    study: StrategyStudy, c: im.Candidate, ledger: TrialLedger,
    pro: Prospective | None, at: datetime, *, holdout_synthetic: bool,
) -> tuple[ShadowReport | None, list[str]]:  # fmt: skip
    if pro is None:
        return None, ["prospective_review_missing"]
    why = _run_checks(study, c, ledger, pro.book.run, pro.series.evidence_class,
                      holdout_synthetic)  # fmt: skip
    try:  # scored by the assessor now, from the captured book: no hindsight
        report = score(pro.book, pro.series, at, pro.baseline_series,
                       pro.declared_hash)  # fmt: skip
    except ResearchError as exc:
        return None, [*why, f"prospective_invalid:{exc.code}"]
    if not report.claim_permitted:
        why.append("prospective_below_declared_minimum")
    return report, why


@dataclass(frozen=True, slots=True)
class HoldoutData:
    """The holdout price data the assessor evaluates on (owner decision 39), read
    from the protected store (owner decision 41, `holdout_series_store`)."""

    series: PriceSeries
    baseline_series: Mapping[str, PriceSeries]


def holdout_read(
    study: StrategyStudy, registry: im.ExperimentRegistry,
    strategies: StrategyRegistry, ledger: TrialLedger, candidate_id: str,
    assessor: im.Party, at: datetime, access_id: str, trial_id: str,
) -> tuple[TrialLedger, Access]:  # fmt: skip
    """The one promotion read `holdout_run` takes, requested on `ledger` but not
    opened: every refusal comes first, so a refused caller's ledger is unchanged.
    Deterministic, so a store can record the read before any data is opened (owner
    decision 42) and `holdout_run` re-derives the same one."""
    p, c = study.protocol, registry.candidate(candidate_id)
    if c.experiment_id != study.family or registry.protocol(study.family) != p:
        raise Error("study_mismatch", candidate_id)
    if assessor != p.assessor:
        raise Error("assessor_not_registered", assessor.actor_id)
    study.check_plan(ledger)
    _version(study, strategies, c)
    at = ensure_aware_utc(at)
    if at < max(p.registered_at, c.created_at):
        raise Error("before_freeze", format_instant(at))
    mine = [a for a in ledger.accesses()
            if a.plan_id == p.plan_id and a.split is Split.PROMOTION]  # fmt: skip
    trials = [t for t in ledger.trials()
              if t.plan_id == p.plan_id and t.split is Split.PROMOTION]  # fmt: skip
    if any(
        (a.role, a.actor_id) != (Role.ASSESSOR, assessor.actor_id) for a in mine
    ) or any(t.access_id is None for t in trials):  # fmt: skip
        raise Error("promotion_entry_not_assessor", p.plan_id)
    if any(a.granted for a in mine):
        raise Error("holdout_used", p.plan_id)
    if (n := account(study, registry, ledger).unrecorded) > 0:  # decision 50
        raise Error("unrecorded_trials", str(n))
    plan = ledger.plan(p.plan_id)
    lg, a = ledger.request_access(access_id, p.plan_id, trial_id, c.trial_key,
                                  Split.PROMOTION, plan.promotion, None,
                                  assessor.actor_id, Role.ASSESSOR, at)  # fmt: skip
    if not a.granted:
        raise Error("promotion_refused", ",".join(a.reasons))
    return lg, a


def holdout_run(
    study: StrategyStudy, registry: im.ExperimentRegistry,
    strategies: StrategyRegistry, ledger: TrialLedger, candidate_id: str,
    dataset: ResearchDataset, rights: ResearchRights, holdout: HoldoutData,
    build: Callable[[Mapping[str, ParamValue]], Strategy], assessor: im.Party,
    at: datetime, access_id: str, trial_id: str,
) -> tuple[TrialLedger, TrialOutcome]:  # fmt: skip
    """The assessor's promotion run of `candidate_id` on the holdout: `holdout_read`
    (refusals before any read; the caller's ledger is unchanged), then the trial."""
    lg, _ = holdout_read(study, registry, strategies, ledger, candidate_id, assessor,
                         at, access_id, trial_id)  # fmt: skip
    p, c = study.protocol, registry.candidate(candidate_id)
    m = _version(study, strategies, c)
    plan = ledger.plan(p.plan_id)
    return run_promotion_trial(lg, trial_id, p.hypothesis, ensure_aware_utc(at),
                               dataset, rights, access_id, study.evaluation,
                               holdout.series, build(MappingProxyType(m.resolve({}))),
                               holdout.baseline_series,
                               derived=_derived(plan.holdout_instruments,
                                                holdout))  # fmt: skip


_ONE = Decimal(1)
# Owner decisions 46, 49 and 50: the hard controls the assessor run derives, each
# recorded with a marker metric (value 1) naming its basis, stated here in full.
DERIVED_BASIS: Mapping[str, str] = MappingProxyType({
    "basis.lookahead_violations.walk_forward_refusal": (
        "walk-forward refusal; strategy code is manifest-registered and trusted; "
        "data captured outside the context is not detected"),
    "basis.unrecorded_trials.account_before_read": (
        "account() on the ledger before the read: no granted access without a "
        "recorded trial on the plan or in the family (else refused)"),
    "basis.survivorship_violations.single_preregistered_instrument": (
        "the plan preregisters exactly one holdout instrument and the holdout "
        "series is on it: no universe selection"),
})  # fmt: skip


def _derived(preregistered: frozenset[InstrumentId],
             holdout: HoldoutData) -> dict[str, Decimal]:  # fmt: skip
    """Recorded only on a completed promotion trial, so `lookahead_violations` is 0
    only when the walk-forward guard refused nothing (decision 49); the unrecorded
    count was 0 before the read (decision 50); survivorship is left missing unless
    the evaluated instrument is the plan's single preregistered one (decision 46)."""
    m = {"lookahead_violations": Decimal(0), "unrecorded_trials": Decimal(0)}
    if preregistered == {holdout.series.instrument}:
        m["survivorship_violations"] = Decimal(0)
    return m | {k: _ONE for k in DERIVED_BASIS if k.split(".")[1] in m}


def assess_strategy(
    study: StrategyStudy, registry: im.ExperimentRegistry,
    strategies: StrategyRegistry, ledger: TrialLedger, book: BudgetBook,
    assessor: im.Party, at: datetime, verdict_id: str, candidate_id: str,
    prospective: Prospective | None, claimed: Mapping[str, Decimal] | None = None,
) -> StrategyAssessment:  # fmt: skip
    f = study.family
    if registry.protocol(f) != study.protocol:
        raise Error("study_mismatch", f)
    v = im.assess(registry, f, candidate_id, ledger, book, assessor, at, verdict_id,
                  claimed, evaluation=study.evaluation)  # fmt: skip
    c = registry.candidate(candidate_id)
    acct = account(study, registry, ledger)
    limits = list(v.limitations)  # the Bonferroni widening is in v (decision 35)

    def out(d: im.Decision, reasons: list[str], report: ShadowReport | None = None,
            ceiling: EvidenceState = EvidenceState.NONE,
            ) -> StrategyAssessment:  # fmt: skip
        extra = [] if report is None else list(report.limitations)
        lim = tuple(dict.fromkeys([*limits, *extra]))
        return StrategyAssessment(v, d, tuple(dict.fromkeys(reasons)), acct, report,
                                  ceiling, lim)  # fmt: skip

    if v.decision is im.Decision.BLOCKED:
        return out(v.decision, list(v.reasons))
    _version(study, strategies, c)
    if v.decision is im.Decision.REJECT or acct.violations:
        return out(im.Decision.REJECT, [*v.reasons, *acct.violations])
    synthetic = "synthetic_only" in v.limitations
    report, why = _prospective(study, c, ledger, prospective, at,
                               holdout_synthetic=synthetic)  # fmt: skip
    if why or v.decision is im.Decision.INSUFFICIENT:
        return out(im.Decision.INSUFFICIENT, [*v.reasons, *why], report)
    assert report is not None
    ceiling = report.evidence_ceiling
    if synthetic:
        ceiling = EvidenceState.LIMITED
    return out(v.decision, [], report, ceiling)


def recordable_verdict(a: StrategyAssessment) -> im.Verdict:
    """The verdict to record: T044's, carrying T047's decision and reasons. Record
    this one, never `a.verdict`, whose accept may predate a T047 rejection."""
    return replace(a.verdict, decision=a.decision, reasons=a.reasons)


def check_assessment(
    study: StrategyStudy, a: StrategyAssessment, c: im.Candidate, ledger: TrialLedger
) -> None:
    """Re-check an accept-for-review assessment that may have been built by hand,
    including its prospective run against the ledger."""
    v, rep = a.verdict, a.prospective
    synthetic = "synthetic_only" in v.limitations
    ok = (
        v.decision is a.decision is im.Decision.ACCEPT_FOR_REVIEW and a.reasons == ()
        and rep is not None and rep.claim_permitted and not a.significance_claimed
        and not _run_checks(study, c, ledger, rep.run, rep.evidence_class, synthetic)
        and a.evidence_ceiling is (EvidenceState.LIMITED if synthetic
                                   else rep.evidence_ceiling)
    )  # fmt: skip
    if not ok:
        raise Error("assessment_inconsistent", v.verdict_id)


def adoption_candidate(
    study: StrategyStudy, assessment: StrategyAssessment,
    registry: im.ExperimentRegistry, strategies: StrategyRegistry,
    ledger: TrialLedger, artifact_id: str,
) -> dict[str, object]:  # fmt: skip
    """The evidence bundle a human reviews before release, install and adoption.
    It records the approval classes required and grants none of them."""
    im._id(artifact_id, "artifact_id")
    a, v, p = assessment, assessment.verdict, study.protocol
    if not a.adoption_eligible:
        raise Error("not_eligible", a.decision.value)
    c = registry.candidate(v.candidate_id)
    check_assessment(study, a, c, ledger)
    assert a.prospective is not None  # checked above
    recorded = registry.verdicts(v.candidate_id)  # the latest must be this one
    if not recorded or recorded[-1] != recordable_verdict(a) or (
        v.protocol_hash != p.content_hash
    ):  # fmt: skip
        raise Error("verdict_not_recorded", v.verdict_id)
    m = _version(study, strategies, c)
    if account(study, registry, ledger) != a.accounting:
        raise Error("trials_changed", v.verdict_id)
    acct = a.accounting
    body: dict[str, object] = {
        "artifact_kind": "strategy_adoption_candidate", "artifact_id": artifact_id,
        "experiment_id": p.experiment_id, "candidate_id": c.candidate_id,
        "lineage": list(registry.lineage(c.candidate_id)),
        "strategy_id": m.strategy_id, "base_version": study.base.version,
        "candidate_version": m.version,
        "base_material_hash": study.base.material_hash,
        "candidate_material_hash": m.material_hash,
        "candidate_config_hash": m.config_hash({}),
        "diff": version_diff(study.base, m), "protocol_hash": p.content_hash,
        "search_space_hash": study.space.content_hash,
        "evaluation_protocol_hash": study.evaluation.content_hash,
        "risk_policy_hash": p.risk_policy_hash, "scorecard": p.scorecard.wire(),
        "trials": {"rows": list(acct.report.rows),
                   "trial_count": acct.trial_count,
                   "unrecorded": acct.unrecorded,
                   "research_trials": acct.research_trials,
                   "max_trials": acct.max_trials},
        "verdict": {"verdict_id": v.verdict_id, "decision": v.decision.value,
                    "assessor": [v.assessor.actor_id, v.assessor.version],
                    "scores": {k: str(x) for k, x in sorted(v.scores.items())},
                    "trial_ids": list(v.trial_ids),
                    "failed_trial_ids": list(v.failed_trial_ids),
                    "assessed_at": format_instant(v.assessed_at)},
        "prospective": a.prospective.to_wire(), "decision": a.decision.value,
        "evidence_ceiling": a.evidence_ceiling.value,
        "significance_claimed": False, "limitations": list(a.limitations),
        "required_approvals": sorted(x.value for x in p.approvals),
        "granted_approvals": [], "next_state": v.next_state,
        "installs": False, "adopts": False,
    }  # fmt: skip
    return {**body, "content_hash": digest(body)}
