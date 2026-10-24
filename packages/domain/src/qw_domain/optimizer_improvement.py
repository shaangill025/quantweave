"""Recursive optimizer improvement (T050 increment 1; spec §11 optimizer row and
"Recursive lineage acceptance"; R028, R030, R033, R037, R038, R076, R086, R092, R099).
`OptimizerConfig` is a closed search policy hashed on its canonical behaviour;
`run_cycle` interprets it on a development fixture, for the protocol baseline or the
installed head that is this experiment's assessed, evidenced self-change of it;
`propose_self_change` goes through a `SelfProposer` port; T044 assesses (blocked
until T007); `verify_recursion` re-executes both cycles and checks the lineage.
Increment 2 (owner decisions 86, 89): `ReceiptProposer`, the free deterministic
proposer that derives V1 from V0's cycle receipt alone, and `measure`, the
assessor's run of V0 and V1 on the protected promotion fixture (`promotion_gain`).
LIMITATIONS: in memory; times, actors, receipts and costs are caller-asserted, so
verification is necessary, not sufficient; costs are not reserved in the T037 book.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from qw_domain import improvement as im
from qw_domain import research_improvement as ri
from qw_domain.budget import BudgetBook
from qw_domain.decimals import UsdBudget, safe_repr
from qw_domain.instants import ensure_aware_utc
from qw_domain.release import DeploymentLedger, Install
from qw_domain.research_data import digest
from qw_domain.sources import ID_PATTERN
from qw_domain.trial_ledger import SplitPlan, TrialLedger


class OptimizerError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


_E = OptimizerError
OPT = im.Target.RECURSIVE_OPTIMIZER
COMPONENT = f"{OPT.value}:{OPT.value}"  # T051 prepare_release component naming
OWNER_IMPROVEMENT_CAP = Decimal("20")  # USD per month (spec §11, personal pilot)
MAX_PROPOSALS = 8
LIMITATIONS = ("in_memory", "caller_asserted_times", "declared_costs_not_reserved",
               "receipts_unauthenticated")  # fmt: skip
SYNTHETIC = frozenset({"synthetic_only", "gate_lifted_synthetic"})
OPT_HARD = ("replayed_output", "payload_invalid")


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise _E(what, f"{safe_repr(value)} is malformed")
    return value


class Operator(StrEnum):  # T046 research knobs a cycle may mutate
    DATE_RULE = "extract.publication_date"
    VALUE_RULE = "extract.value"


_FIELDS: Mapping[Operator, tuple[str, type[StrEnum]]] = MappingProxyType({
    Operator.DATE_RULE: ("date_rule", ri.DateRule),
    Operator.VALUE_RULE: ("value_rule", ri.ValueRule),
})  # fmt: skip


class Selection(StrEnum):
    FIRST_IMPROVING = "first_improving"  # stop at the first proposal beating the base
    BEST_OF_BUDGET = "best_of_budget"  # spend the budget, keep the best improver


KNOBS = frozenset({"proposal_budget", "operators", "selection"})
_WIDENING = ("test", "threshold", "risk", "verif", "gate", "release", "sign", "key",
             "assess", "scorecard", "approv", "cap", "limit", "depth", "holdout",
             "answer", "polic", "sandbox", "budget")  # fmt: skip


@dataclass(frozen=True, slots=True)
class OptimizerConfig:
    version_id: str
    proposal_budget: int
    operators: tuple[Operator, ...]
    selection: Selection

    def __post_init__(self) -> None:
        _id(self.version_id, "version_id")
        b = self.proposal_budget
        if type(b) is not int or not 1 <= b <= MAX_PROPOSALS:
            raise _E("value", "proposal_budget")
        ops = tuple(self.operators)
        object.__setattr__(self, "operators", ops)
        if not ops or len(set(ops)) != len(ops):
            raise _E("value", "operators")
        if not all(type(o) is Operator for o in ops):
            raise TypeError("operators must be Operator values")
        if type(self.selection) is not Selection:
            raise TypeError("selection must be a Selection")

    def to_payload(self) -> dict[str, str]:
        return {"proposal_budget": str(self.proposal_budget),
                "operators": ",".join(o.value for o in self.operators),
                "selection": self.selection.value}  # fmt: skip

    @classmethod
    def from_payload(cls, version_id: str, payload: Mapping[str, str]) -> (
        "OptimizerConfig"
    ):  # fmt: skip
        """Strict, canonical parse; every knob is required."""
        for k in payload:
            if k not in KNOBS:
                n = k.lower().replace("-", "_") if isinstance(k, str) else ""
                code = "widens_authority" if any(w in n for w in _WIDENING) else (
                    "not_adaptable")  # fmt: skip
                raise _E(code, safe_repr(k))
        if set(payload) != KNOBS or not all(isinstance(v, str) for v in
                                            payload.values()):  # fmt: skip
            raise _E("value", "all three knobs as strings")
        n = payload["proposal_budget"]
        if not (n.isascii() and n.isdigit() and n == str(int(n))):
            raise _E("value", "proposal_budget")
        try:
            ops = tuple(Operator(o) for o in payload["operators"].split(","))
            sel = Selection(payload["selection"])
        except ValueError:
            raise _E("value", "operators or selection") from None
        return cls(version_id, int(n), ops, sel)

    @property
    def canonical(self) -> tuple[int, tuple[Operator, ...], Selection]:
        """Idle parts dropped: operators past the budget, budget past the reachable
        moves, the rule for one proposal, operator order in a full best-of search."""
        ops, n = list[Operator](), 0
        for o in self.operators:
            if n < self.proposal_budget:
                ops, n = [*ops, o], n + len(_FIELDS[o][1]) - 1
        b = min(self.proposal_budget, n)
        sel = Selection.BEST_OF_BUDGET if b == 1 else self.selection
        if sel is Selection.BEST_OF_BUDGET and b == n:
            ops = sorted(ops)
        return b, tuple(ops), sel

    @property
    def content_hash(self) -> str:  # behaviour only: the version id is a label
        b, ops, sel = self.canonical
        return digest(["qw-optimizer-v2", b, [o.value for o in ops], sel.value])


def diff(a: OptimizerConfig, b: OptimizerConfig) -> tuple[str, ...]:
    pa, pb = a.to_payload(), b.to_payload()
    return tuple(sorted(k for k in pa if pa[k] != pb[k]))


def scorecard(scorecard_id: str, max_cost: UsdBudget, limits: "Limits") -> (
    im.Scorecard
):  # fmt: skip
    """Hard controls at zero; goals: an out-of-sample `promotion_gain` of at least 1
    and a declared cycle cost within `max_cost`, which is positive and never above
    the owner's monthly improvement cap in `limits`."""
    if not isinstance(max_cost, UsdBudget) or not isinstance(limits, Limits):
        raise TypeError("max_cost is a UsdBudget and limits are Limits")
    if not 0 < max_cost.value <= limits.monthly_cap.value:
        raise _E("cost_exceeds_limits", str(max_cost.value))
    t, d, z = im.Threshold, im.Direction, Decimal(0)
    hard = (*im.HARD_CONTROLS[OPT], *OPT_HARD)
    return im.Scorecard(scorecard_id, OPT, (
        *(t(m, d.AT_MOST, z, True) for m in hard),
        t("promotion_gain", d.AT_LEAST, Decimal(1), False),  # assessor-measured
        t("cost_usd", d.AT_MOST, max_cost.value, False),
    ))  # fmt: skip


def _cost_bound(p: im.Protocol) -> Decimal:
    """The protocol's cycle-cost goal; positive and within the owner's USD20 cap."""
    bs = [t.bound for t in p.scorecard.thresholds if t.metric == "cost_usd"
          and t.direction is im.Direction.AT_MOST]  # fmt: skip
    if len(bs) != 1 or not 0 < bs[0] <= OWNER_IMPROVEMENT_CAP:
        raise _E("cost_bound_missing", p.experiment_id)
    return bs[0]


def check_protocol(p: im.Protocol) -> None:
    if p.target is not OPT:
        raise _E("not_optimizer", p.experiment_id)
    if p.scope is not im.Scope.INSTALLATION:  # R039: optimizer changes are shared
        raise _E("tenant_scope", p.experiment_id)
    held = {t.metric for t in p.scorecard.thresholds if t.hard and t.bound == 0
            and t.direction is im.Direction.AT_MOST}  # fmt: skip
    if not held.issuperset(OPT_HARD):
        raise _E("control_missing", p.experiment_id)
    ts = p.scorecard.thresholds
    if any(t.metric == "dev_gain" for t in ts):  # producer-visible: never a goal
        raise _E("dev_gain_goal", p.experiment_id)
    gain = [t for t in ts if t.metric == "promotion_gain"]
    if len(gain) != 1 or gain[0].hard or gain[0].bound < 1 or (
        gain[0].direction is not im.Direction.AT_LEAST
    ):  # fmt: skip
        raise _E("gain_goal_missing", p.experiment_id)
    _cost_bound(p)


@dataclass(frozen=True, slots=True)
class Limits:  # owner configuration; never part of an optimizer artifact
    max_depth: int  # optimizer generations above the bootstrapped baseline
    patience: int  # a runner stops after this many cycles in a row select nothing
    monthly_cap: UsdBudget
    cost_per_proposal: UsdBudget  # declared, worst case; nothing is spent here

    def __post_init__(self) -> None:
        ints = (self.max_depth, self.patience)
        if (
            not all(type(x) is int and x >= 1 for x in ints)
            or not isinstance(self.cost_per_proposal, UsdBudget)
            or self.cost_per_proposal.value <= 0
        ):
            raise _E("limits", "positive depth, patience and cost per proposal")
        if not isinstance(self.monthly_cap, UsdBudget):
            raise TypeError("costs are UsdBudget")
        if not 0 < self.monthly_cap.value <= OWNER_IMPROVEMENT_CAP:
            raise _E("cap_exceeds_owner_budget", str(self.monthly_cap.value))


@dataclass(frozen=True, slots=True)
class Blocked:
    step: str
    reasons: tuple[str, ...]
    limitations: tuple[str, ...] = LIMITATIONS


@dataclass(frozen=True, slots=True)
class Proposal:
    knob: Operator
    value: str
    config_hash: str
    score: int


@dataclass(frozen=True, slots=True)
class CycleReceipt:
    cycle_id: str
    runner_version: str
    runner_hash: str
    config: OptimizerConfig  # the policy actually executed
    authority: str  # "baseline:<version>" or "release:<release id>"
    base_hash: str
    fixture_hash: str
    proposals: tuple[Proposal, ...]
    selected: str | None  # research config hash; a cycle may find nothing
    declared_cost: UsdBudget
    at: datetime

    def __post_init__(self) -> None:
        _cost(self.declared_cost)

    @property
    def outputs_hash(self) -> str:
        return digest([[[p.knob, p.value, p.config_hash, p.score]
                        for p in self.proposals], self.selected])  # fmt: skip


@dataclass(frozen=True, slots=True)
class SelfChange:
    candidate: im.Candidate
    config: OptimizerConfig
    diff: tuple[str, ...]
    declared_cost: UsdBudget
    at: datetime

    def __post_init__(self) -> None:
        _cost(self.declared_cost)


@dataclass(frozen=True, slots=True)
class Verification:
    reasons: tuple[str, ...]  # empty: the necessary checks pass
    provisional: bool = True  # receipts are self-asserted; never sufficient alone
    limitations: tuple[str, ...] = LIMITATIONS


def _cost(c: object) -> Decimal:
    if not isinstance(c, UsdBudget) or c.value < 0:
        raise _E("negative_cost", safe_repr(c))
    return c.value


type History = Sequence[CycleReceipt | SelfChange]


class SelfProposer(Protocol):  # a model or rule behind the gateway; not configured
    def propose(self, current: OptimizerConfig, cycle: CycleReceipt) -> (
        Mapping[str, str]
    ): ...  # fmt: skip


def _score(cfg: ri.ResearchConfig, dev: ri.Fixture) -> int:
    dates = sum(ri.extract_date(cfg.date_rule, d.text) == d.published_on
                for d in dev.docs)  # fmt: skip
    values = sum((ri.extract_value(cfg.value_rule, d.text) or ("", None))[1]
                 == d.value for d in dev.docs)  # fmt: skip
    return dates + values


def _variant(base: ri.ResearchConfig, op: Operator, value: str) -> ri.ResearchConfig:
    name, kind = _FIELDS[op]
    return replace(base, **{name: kind(value)})  # type: ignore[arg-type]


def _execute(
    cfg: OptimizerConfig, base: ri.ResearchConfig, dev: ri.Fixture
) -> tuple[tuple[Proposal, ...], str | None]:
    if dev.split is not ri.Split.DEVELOPMENT:
        raise _E("not_development", dev.fixture_id)
    return _search(cfg, base, dev)


def _search(
    cfg: OptimizerConfig, base: ri.ResearchConfig, dev: ri.Fixture
) -> tuple[tuple[Proposal, ...], str | None]:
    """The fixed interpreter; callers choose the split (`_execute`, `measure`)."""
    floor, out, best = _score(base, dev), [], None
    moves = [(op, v.value) for op in cfg.operators for v in _FIELDS[op][1]
             if v is not getattr(base, _FIELDS[op][0])]  # fmt: skip
    for op, value in moves[: cfg.proposal_budget]:
        c = _variant(base, op, value)
        p = Proposal(op, value, c.content_hash, _score(c, dev))
        out.append(p)
        if p.score > floor and (best is None or (p.score, best.config_hash) > (
            best.score, p.config_hash
        )):  # fmt: skip
            # ties keep the lower hash, so operator order cannot change the pick
            best = p
            if cfg.selection is Selection.FIRST_IMPROVING:
                break
    return tuple(out), None if best is None else best.config_hash


def _candidates(
    reg: im.ExperimentRegistry, h: str, experiment_id: str | None = None
) -> list[im.Candidate]:
    return [e for e in reg.entries if isinstance(e, im.Candidate)
            and e.target is OPT and e.artifact_hash == h
            and experiment_id in (None, e.experiment_id)]  # fmt: skip


def generation(reg: im.ExperimentRegistry, h: str) -> int:
    """Optimizer self-changes between `h` and a bootstrapped baseline."""
    n, seen = 0, {h}
    while cs := _candidates(reg, h):
        bases = {reg.protocol(c.experiment_id).baseline_hash for c in cs}
        if len(bases) != 1:
            raise _E("lineage_ambiguous", h)
        h = bases.pop()
        if h in seen:
            raise _E("lineage_cycle", h)
        n, seen = n + 1, seen | {h}
    return n


def _spent(history: History, at: datetime) -> Decimal:
    def month(t: datetime) -> tuple[int, int]:  # the UTC calendar month
        u = ensure_aware_utc(t)
        return u.year, u.month

    return sum((_cost(x.declared_cost) for x in history
                if month(x.at) == month(at)), Decimal(0))  # fmt: skip


def _fold(x: object) -> str:
    return x.casefold() if isinstance(x, str) else ""


def _is_optimizer(a: im.Party, producer: im.Party, *versions: str | None) -> bool:
    """`a` shares an actor, route or family with the producer (case-insensitive),
    or carries an optimizer version."""
    same = any(_fold(getattr(a, n)) == _fold(getattr(producer, n))
               for n in ("actor_id", "route_id", "family_id"))  # fmt: skip
    return same or _fold(a.version) in {_fold(v) for v in (producer.version, *versions)}


def _assessment(
    reg: im.ExperimentRegistry, p: im.Protocol, h: str,
    release: Install | None, *, accept_synthetic: bool,
) -> list[str]:  # fmt: skip
    """Why `h` is not this experiment's assessed, evidenced, released self-change
    of the protocol baseline."""
    check_protocol(p)
    cs = _candidates(reg, h, p.experiment_id)
    if len(cs) != 1:
        return ["not_proposed" if not cs else "ambiguous_candidate"]
    c, out = cs[0], []
    rv = c.runtime_versions
    if (c.parent_id, rv.get("optimizer")) != (p.baseline_version, p.baseline_hash):
        out.append("not_from_baseline")
    vs = reg.verdicts(c.candidate_id)
    if not vs:
        return [*out, "assessment_skipped"]
    v = vs[-1]
    if _is_optimizer(v.assessor, p.producer, rv.get("candidate_version")):
        out.append("self_certification")
    if v.decision is not im.Decision.ACCEPT_FOR_REVIEW:
        out += [f"not_accepted:{v.decision.value}", *v.reasons]
    elif not v.trial_ids or any(t.metric not in v.scores
                                for t in p.scorecard.thresholds):  # fmt: skip
        out.append("verdict_without_evidence")
    elif not all(t.passes(v.scores[t.metric]) for t in p.scorecard.thresholds):
        out.append("verdict_inconsistent")  # an accept its own scores contradict
    elif not accept_synthetic and SYNTHETIC & set(v.limitations):
        out.append("synthetic_evidence")
    if release is not None:
        m = release.release.manifest
        if (m.experiment_id, m.candidate_id, m.candidate_hash, m.verdict_id) != (
            c.experiment_id, c.candidate_id, c.content_hash, v.verdict_id
        ):  # fmt: skip
            out.append("release_binding")
        actors = {_fold(p.producer.actor_id), _fold(v.assessor.actor_id)}
        if _fold(m.approval.principal_id) in actors:
            out.append("self_approval")
        if _fold(release.operator_id) == _fold(p.producer.actor_id):
            out.append("self_install")
        if min(m.approval.approved_at, release.at) < v.assessed_at:
            out.append("before_verdict")
    return out


def runner_authority(
    cfg: OptimizerConfig, reg: im.ExperimentRegistry, experiment_id: str,
    deployment: DeploymentLedger, at: datetime, *,
    accept_synthetic_evidence: bool = False,
) -> str | Blocked:  # fmt: skip
    """Who may run `cfg` now: the protocol baseline while nothing is installed,
    else only the installed head that is this experiment's accepted self-change.
    `accept_synthetic_evidence` is for tests only."""
    p, h = reg.protocol(experiment_id), cfg.content_hash
    check_protocol(p)
    head = deployment.head(COMPONENT)
    if head is None:
        if h == p.baseline_hash:
            return f"baseline:{p.baseline_version}"
        why = _assessment(reg, p, h, None, accept_synthetic=accept_synthetic_evidence)
        return Blocked("authority", ("not_installed", *why))
    i, at = deployment.install_of(head), ensure_aware_utc(at)
    why = list(deployment.runnable(head, at))
    if at < i.at:
        why.append("not_yet_installed")
    if i.release.manifest.artifact_hash != h:
        why.append("superseded" if h == p.baseline_hash else "not_installed")
    else:
        why += _assessment(reg, p, h, i, accept_synthetic=accept_synthetic_evidence)
    return Blocked("authority", tuple(why)) if why else f"release:{head}"


def run_cycle(
    cycle_id: str, cfg: OptimizerConfig, reg: im.ExperimentRegistry,
    experiment_id: str, deployment: DeploymentLedger, base: ri.ResearchConfig,
    dev: ri.Fixture, limits: Limits, history: History, at: datetime, *,
    accept_synthetic_evidence: bool = False,
) -> CycleReceipt | Blocked:  # fmt: skip
    _id(cycle_id, "cycle_id")
    at = ensure_aware_utc(at)
    if any(isinstance(x, CycleReceipt) and x.cycle_id == cycle_id for x in history):
        raise _E("duplicate", cycle_id)
    syn = accept_synthetic_evidence
    auth = runner_authority(cfg, reg, experiment_id, deployment, at,
                            accept_synthetic_evidence=syn)  # fmt: skip
    if isinstance(auth, Blocked):
        return replace(auth, step="cycle")
    h, why = cfg.content_hash, []
    mine = [x for x in history if isinstance(x, CycleReceipt) and x.runner_hash == h]
    last = mine[-limits.patience :]
    if len(last) == limits.patience and all(x.selected is None for x in last):
        why.append("stop_no_progress")
    cost = UsdBudget(limits.cost_per_proposal.value * cfg.proposal_budget)
    if _spent(history, at) + cost.value > limits.monthly_cap.value:
        why.append("improvement_budget_exhausted")
    if why:
        return Blocked("cycle", tuple(why))
    proposals, selected = _execute(cfg, base, dev)
    return CycleReceipt(cycle_id, cfg.version_id, h, cfg, auth, base.content_hash,
                        dev.content_hash, proposals, selected, cost, at)  # fmt: skip


def propose_self_change(
    candidate_id: str, version_id: str, cycle: CycleReceipt,
    reg: im.ExperimentRegistry, experiment_id: str, proposer: SelfProposer | None,
    limits: Limits, history: History, at: datetime,
) -> SelfChange | Blocked:  # fmt: skip
    """The runner of `cycle` proposes its successor in the protocol whose frozen
    baseline it is. Refusals of the payload raise; gates block."""
    at = ensure_aware_utc(at)
    p, cur = reg.protocol(experiment_id), cycle.config
    check_protocol(p)
    if cycle.runner_hash != cur.content_hash or cur.content_hash != p.baseline_hash:
        return Blocked("self_change", ("runner_not_baseline",))
    if p.producer.version != cur.version_id:
        return Blocked("self_change", ("producer_not_runner",))
    if at < cycle.at:
        raise _E("time_order", candidate_id)
    if proposer is None:
        return Blocked("self_change", ("generator_not_configured",))
    new = OptimizerConfig.from_payload(version_id, proposer.propose(cur, cycle))
    if new.content_hash == cur.content_hash or _candidates(reg, new.content_hash):
        raise _E("identical_relabel", version_id)
    why = []
    if generation(reg, cur.content_hash) + 1 > limits.max_depth:
        why.append("depth_limit")
    if _spent(history, at) + limits.cost_per_proposal.value > limits.monthly_cap.value:
        why.append("improvement_budget_exhausted")
    if why:
        return Blocked("self_change", tuple(why))
    c = im.Candidate(candidate_id, experiment_id, OPT, p.baseline_version,
                     p.producer, new.content_hash, new.to_payload(),
                     {"optimizer": cur.content_hash, "candidate_version": version_id,
                      "source_cycle": cycle.cycle_id,
                      "source_outputs": cycle.outputs_hash}, (), at)  # fmt: skip
    return SelfChange(c, new, diff(cur, new), limits.cost_per_proposal, at)


def _reach(ops: Sequence[Operator]) -> int:  # moves a policy can make from any base
    return sum(len(_FIELDS[o][1]) - 1 for o in ops)


@dataclass(frozen=True, slots=True)
class ReceiptProposer:
    """The free, deterministic proposer (owner decision 89): V1 is derived from V0's
    cycle receipt alone (its executed policy, proposals and selection), within the
    closed knobs. No ids, times, costs or fixtures are read. The rules, from the
    receipt's own facts:
    R1 widen: operators V0 lacks are appended, in `Operator` order.
    R2 order: if a proposal was selected, its operator goes first; otherwise the
       operators V0 tried go after the ones it did not (stable).
    R3 budget: if V0 spent its whole budget, the budget becomes every move the
       widened operators can make (at most `MAX_PROPOSALS`).
    R4 selection: if V0 stopped at its first improver with moves and budget left,
       it becomes `best_of_budget`.
    A result with V0's behaviour raises `no_change` (never a relabel); the payload
    holds only the three knobs, so it cannot widen authority."""

    def propose(self, current: OptimizerConfig, cycle: CycleReceipt) -> (
        Mapping[str, str]
    ):  # fmt: skip
        cur, ps = cycle.config, cycle.proposals
        if not cycle.runner_hash == cur.content_hash == current.content_hash:
            raise _E("receipt_mismatch", cycle.cycle_id)
        won = [p for p in ps if p.config_hash == cycle.selected]
        if cycle.selected is not None and not won:
            raise _E("receipt_mismatch", cycle.cycle_id)
        ops = [*cur.operators, *(o for o in Operator if o not in cur.operators)]
        if won:
            ops = [won[0].knob, *(o for o in ops if o is not won[0].knob)]
        else:
            tried = {p.knob for p in ps}
            ops = [o for o in ops if o not in tried] + [o for o in ops if o in tried]
        budget, sel = cur.proposal_budget, cur.selection
        if len(ps) == cur.proposal_budget:
            budget = max(budget, min(MAX_PROPOSALS, _reach(ops)))
        left = len(ps) < min(cur.proposal_budget, _reach(cur.operators))
        if won and sel is Selection.FIRST_IMPROVING and left:
            sel = Selection.BEST_OF_BUDGET
        new = OptimizerConfig(cur.version_id, budget, tuple(ops), sel)
        if new.content_hash == cur.content_hash:
            raise _E("no_change", cycle.cycle_id)
        return new.to_payload()


def assess_self_change(
    reg: im.ExperimentRegistry, sc: SelfChange, ledger: TrialLedger,
    book: BudgetBook, assessor: im.Party, at: datetime, verdict_id: str,
) -> im.Verdict:  # fmt: skip
    """T044's independent assessor; an optimizer identity can never assess."""
    p = reg.protocol(sc.candidate.experiment_id)
    if _is_optimizer(assessor, p.producer, sc.config.version_id):
        raise _E("self_certification", assessor.actor_id)
    return im.assess(reg, p.experiment_id, sc.candidate.candidate_id, ledger, book,
                     assessor, at, verdict_id)  # fmt: skip


def _picked(r: tuple[tuple[Proposal, ...], str | None], base: ri.ResearchConfig) -> (
    ri.ResearchConfig
):  # fmt: skip
    pick = [p for p in r[0] if p.config_hash == r[1]]
    return _variant(base, pick[0].knob, pick[0].value) if pick else base


def measure(
    protocol: im.Protocol, plan: SplitPlan, candidate: im.Candidate,
    source: CycleReceipt, base: ri.ResearchConfig, dev: ri.Fixture,
    fixture: ri.Fixture, limits: Limits, assessor: im.Party,
) -> Mapping[str, Decimal]:  # fmt: skip
    """The assessor's measurement of V1 (`candidate`) against V0 (the protocol
    baseline, as executed in `source`, the cycle V1 was proposed from). Each policy
    searches (runs its cycle) from `source`'s research base on `dev`, the
    producer-visible development fixture `source` ran on; the config each selects
    (the base when nothing is selected) is then scored on `fixture`, the protected
    promotion fixture whose hash the T032 plan freezes. `promotion_gain` is V1's
    out-of-sample score minus V0's, so a win that holds only on development data
    does not count. `replayed_output` is 1 when V1's search equals V0's; the slots
    cannot be swapped (`not_development`, `not_promotion`). `identical_relabels`
    is 1 when the artifact is V0's; `threshold_changes` counts payload keys that
    reach a protected control; `payload_invalid` is 1 when the payload is not the
    artifact's full canonical knob set (V1 is then measured as V0: fail closed);
    `cost_usd` is V1's declared worst-case cycle cost under the owner's limits, and
    the protocol's cost goal may not exceed them (`cost_exceeds_limits`). The
    numbers reach a verdict only through T044 `assess`, which stays
    `sandbox_unqualified` here."""
    p, rv = protocol, candidate.runtime_versions
    check_protocol(p)
    if (candidate.experiment_id, candidate.target) != (p.experiment_id, p.target):
        raise _E("candidate_experiment", candidate.candidate_id)
    if _is_optimizer(assessor, p.producer, rv.get("candidate_version")):
        raise _E("self_certification", assessor.actor_id)
    if assessor != p.assessor:
        raise _E("assessor_not_registered", assessor.actor_id)
    if im.plan_digest(plan) != p.plan_digest:
        raise _E("test_access_changed", plan.plan_id)
    if fixture.content_hash != plan.protocol_hash:
        raise _E("answer_key_changed", fixture.fixture_id)
    if fixture.split is not ri.Split.PROMOTION:
        raise _E("not_promotion", fixture.fixture_id)
    if fixture.evidence_class is not plan.dataset.evidence_class:
        raise _E("evidence_class", fixture.evidence_class.value)
    if dev.split is not ri.Split.DEVELOPMENT:
        raise _E("not_development", dev.fixture_id)
    if _cost_bound(p) > limits.monthly_cap.value:
        raise _E("cost_exceeds_limits", p.experiment_id)
    v0 = source.config
    if not source.runner_hash == v0.content_hash == p.baseline_hash:
        raise _E("baseline_hash", p.experiment_id)
    if candidate.parent_id != v0.version_id:
        raise _E("not_from_baseline", candidate.candidate_id)
    if (rv.get("source_cycle"), rv.get("source_outputs")) != (
        source.cycle_id, source.outputs_hash
    ):  # fmt: skip
        raise _E("source_cycle", candidate.candidate_id)
    if base.content_hash != source.base_hash:
        raise _E("base_mismatch", source.cycle_id)
    if dev.content_hash != source.fixture_hash:
        raise _E("source_fixture", dev.fixture_id)
    knobs = {k: v for k, v in candidate.payload.items() if k in KNOBS}
    stray = [k for k in candidate.payload if k not in KNOBS]
    widening = [k for k in stray if any(w in k.lower().replace("-", "_")
                                        for w in _WIDENING)]  # fmt: skip
    try:
        v1 = OptimizerConfig.from_payload(v0.version_id, knobs)
        bound = v1.content_hash == candidate.artifact_hash and not stray
    except OptimizerError:
        v1, bound = v0, False
    if not bound:
        v1 = v0  # an unrunnable or unbound payload proves nothing: measured as V0
    r0, r1 = _execute(v0, base, dev), _execute(v1, base, dev)  # the searches
    gain = _score(_picked(r1, base), fixture) - _score(_picked(r0, base), fixture)
    return MappingProxyType({
        "promotion_gain": Decimal(gain), "replayed_output": Decimal(int(r1 == r0)),
        "payload_invalid": Decimal(int(not bound)),
        "identical_relabels": Decimal(int(candidate.artifact_hash == v0.content_hash)),
        "threshold_changes": Decimal(len(widening)),
        "cost_usd": limits.cost_per_proposal.value * v1.proposal_budget,
    })  # fmt: skip


def cycle_candidate(
    candidate_id: str, cycle: CycleReceipt, protocol: im.Protocol,
    base: ri.ResearchConfig, at: datetime,
) -> im.Candidate:  # fmt: skip
    """The research candidate a cycle selected; its lineage names the runner."""
    if protocol.producer.version != cycle.runner_version:
        raise _E("producer_not_runner", protocol.experiment_id)
    if base.content_hash not in {cycle.base_hash} & {protocol.baseline_hash}:
        raise _E("base_mismatch", protocol.experiment_id)
    pick = [p for p in cycle.proposals if p.config_hash == cycle.selected]
    if not pick:
        raise _E("nothing_selected", cycle.cycle_id)
    cfg = _variant(base, pick[0].knob, pick[0].value)
    return ri.candidate(candidate_id, protocol, protocol.baseline_version, cfg,
                        {"optimizer": cycle.runner_hash,
                         "optimizer_cycle": cycle.cycle_id}, at)  # fmt: skip


def verify_recursion(
    reg: im.ExperimentRegistry, experiment_id: str, deployment: DeploymentLedger,
    v0_cycle: CycleReceipt, v1_cycle: CycleReceipt, base: ri.ResearchConfig,
    dev: ri.Fixture, next_candidate: im.Candidate | None, *,
    accept_synthetic_evidence: bool = False,
) -> Verification:  # fmt: skip
    """Necessary checks for V0 -> assessed V1 -> V1 ran the next cycle; both cycles
    are re-executed from their recorded inputs. Receipt ids, times, authority and
    costs are self-asserted, so passing is provisional and never sufficient alone."""
    p, out = reg.protocol(experiment_id), []
    v0, v1, h1 = v0_cycle.config, v1_cycle.config, v1_cycle.runner_hash
    if v0.content_hash != p.baseline_hash:
        out.append("v0_not_baseline")
    for r in (v0_cycle, v1_cycle):
        if (r.runner_hash, r.runner_version) != (r.config.content_hash,
                                                 r.config.version_id):  # fmt: skip
            out.append("runner_config_mismatch")
        if (r.base_hash, r.fixture_hash) != (base.content_hash, dev.content_hash) or (
            (r.proposals, r.selected) != _execute(r.config, base, dev)
        ):  # fmt: skip
            out.append("receipt_not_reproduced")
    if v1.content_hash == v0.content_hash or not diff(v0, v1):
        out.append("identical_relabel")
    if v1_cycle.outputs_hash == v0_cycle.outputs_hash or (
        _execute(v0, base, dev) == (v1_cycle.proposals, v1_cycle.selected)
    ):  # fmt: skip
        out.append("replayed_output")  # V1's run is what V0 would have produced
    cs = _candidates(reg, h1, experiment_id)
    if not cs:
        return Verification(tuple(sorted({*out, "not_proposed"})))
    c, rv = cs[0], cs[0].runtime_versions
    if (rv.get("source_cycle"), rv.get("source_outputs")) != (
        v0_cycle.cycle_id, v0_cycle.outputs_hash
    ) or c.created_at < v0_cycle.at:  # fmt: skip
        out.append("source_cycle")
    installs = [e for e in deployment.entries if isinstance(e, Install)
                and e.release.manifest.artifact_hash == h1]  # fmt: skip
    if not installs:
        syn = accept_synthetic_evidence
        out += ["not_installed", *_assessment(reg, p, h1, None, accept_synthetic=syn)]
    else:
        i = installs[-1]
        rid = i.release.manifest.release_id
        out += _assessment(reg, p, h1, i, accept_synthetic=accept_synthetic_evidence)
        out += deployment.runnable(rid, v1_cycle.at)
        if i.at > v1_cycle.at:
            out.append("ran_before_install")
        if v1_cycle.authority != f"release:{rid}":
            out.append("runner_authority")
    n = next_candidate
    if v1_cycle.selected is not None and (n is None or (
        n.runtime_versions.get("optimizer"), n.runtime_versions.get("optimizer_cycle"),
        n.producer.version, n.artifact_hash) != (h1, v1_cycle.cycle_id,
        v1_cycle.runner_version, v1_cycle.selected)):  # fmt: skip
        out.append("next_lineage")
    return Verification(tuple(sorted(set(out))))
