"""Evaluator improvement: candidate evaluator policies assessed against a fixed,
frozen evaluation control (T048 increment 1; spec §11 decision-evaluator row;
R028, R029, R030, R033, R039, R084, R092, R099). Domain only, stdlib only.
- `Policy` is the closed set of evaluator knobs: which claim kinds are material
  (never fewer than the T039 floor), the relative conflict tolerance, the minimum
  number of alternatives, the revision bound, and per-review cost and per-pass
  output-token and timeout limits, each inside a cap. Evaluator code, prompts,
  isolation, protocol thresholds, risk policy and answer keys are not knobs: such
  a payload key is refused and named (`answer_key`, `widens_authority`).
- The control is a versioned set of labelled cases (thesis, evidence, recorded
  evaluator-model replies). `freeze` produces a `Manifest` of case hashes and a
  salted commitment to the labels, never the labels themselves; its hash is the
  T032 plan's `protocol_hash`, so the T044 protocol freezes it. A modified, added,
  removed or reordered case, or a changed key, is refused before any key read.
- `AnswerKey.read` returns the labels only to the protocol's assessor on its first
  granted promotion (holdout) access for that candidate, once. Case ids or labels
  in a candidate payload count as `answer_key_mutations`.
- `measure` replays every case through the real T039 `evaluate` under the baseline
  and the candidate policy (deterministic clock, replayed replies, gateway budget
  book) and reports false approvals, over-rejections, latency and cost. Its
  numbers reach a verdict only via the assessor's T032 trial and T044 `assess`.
  Hard rule (§11 "reduce harmful approvals"): false approvals never increase.
- Nothing here grants, installs or adopts: a verdict needs the approval classes
  T044 froze for the evaluator target (maintainer release and operator install).
LIMITATIONS: in memory; times and actor ids are caller-asserted; the answer key is
sealed only by convention inside the process (storage isolation is persistence's);
installation, rollback and the live evaluator reading an installed policy are
increment 2.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType

from qw_domain import evaluator as ev
from qw_domain import improvement as im
from qw_domain import researcher as rs
from qw_domain.budget import BudgetBook, BudgetCaps
from qw_domain.decimal_math import div
from qw_domain.decimals import DOMAIN_CONTEXT, UsdBudget, safe_repr
from qw_domain.evidence import ClaimKind, EvidenceStore
from qw_domain.gateway import (
    GatewayContext,
    ModelRequest,
    ModelRun,
    ProviderResponse,
    Route,
    UsageReport,
)
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import ResearchError, digest
from qw_domain.sources import ID_PATTERN
from qw_domain.trial_ledger import Role, Split, TrialLedger


class PolicyError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class ControlError(PolicyError):
    pass


REVIEW_TTL = timedelta(hours=4)
MAX_REVIEW_COST = Decimal("0.10")  # USD per evaluation; orchestrator decision
TOKENS, TIMEOUT = (64, 2000), (1, 30)  # per pass; inside the route bounds
KNOBS = frozenset({"material_kinds", "conflict_tolerance", "min_alternatives",
                   "max_revisions", "review_max_cost_usd", "pass_output_tokens",
                   "pass_timeout_seconds"})  # fmt: skip
_KEY = ("answer", "key", "label", "expect", "case", "holdout", "oracle", "truth",
        "control")  # fmt: skip
_WIDENING = ("risk", "polic", "threshold", "scorecard", "approv", "assessor",
             "family", "isolat", "budget", "order", "broker", "limit", "gate", "code",
             "prompt", "instruct", "route", "model", "verif", "evidence", "scope",
             "tenant")  # fmt: skip
DEFAULT_COST = UsdBudget("0.05")
EVALUATOR_HARD = ("false_approval_increase", "new_abstentions", "policy_widened",
                  "payload_invalid", "artifact_mismatch")  # fmt: skip
MAX_LATENCY_INCREASE = Decimal(60)  # seconds over the control; orchestrator decision
MAX_COST_INCREASE = Decimal("0.01")  # USD over the control; orchestrator decision
# Goals every evaluator scorecard holds, with the loosest bound allowed.
GOALS: Mapping[str, tuple[im.Direction, Decimal]] = MappingProxyType({
    "improved": (im.Direction.AT_LEAST, Decimal(1)),
    "over_rejection_increase": (im.Direction.AT_MOST, Decimal(0)),
    "latency_increase_seconds": (im.Direction.AT_MOST, MAX_LATENCY_INCREASE),
    "cost_increase_usd": (im.Direction.AT_MOST, MAX_COST_INCREASE),
})  # fmt: skip
_DEC = re.compile(r"[0-9]{1,3}(\.[0-9]{1,6})?", re.ASCII)


def _canon(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _dec(s: str, what: str) -> Decimal:
    if _DEC.fullmatch(s) is None or _canon(Decimal(s)) != s:
        raise PolicyError("value", what)
    return Decimal(s)


def _int(s: str, what: str) -> int:
    if not (len(s) <= 4 and s.isascii() and s.isdigit() and str(int(s)) == s):
        raise PolicyError("value", what)
    return int(s)


def _classify(key: str) -> str:
    k = key.lower()
    if any(w in k for w in _KEY):
        return "answer_key"
    return "widens_authority" if any(w in k for w in _WIDENING) else "not_adaptable"


@dataclass(frozen=True, slots=True)
class Policy:
    material_kinds: frozenset[ClaimKind] = ev.MATERIAL_KINDS
    conflict_tolerance: Decimal = Decimal(0)
    min_alternatives: int = 1
    max_revisions: int = ev.MAX_REVISIONS
    max_cost: UsdBudget = DEFAULT_COST
    output_tokens: int = 1000
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        kinds = self.material_kinds
        if not isinstance(kinds, frozenset) or not all(
            type(k) is ClaimKind for k in kinds
        ) or not kinds >= ev.MATERIAL_KINDS:  # fmt: skip
            raise PolicyError("value", "material_kinds")
        tol = self.conflict_tolerance
        if type(tol) is not Decimal or not tol.is_finite() or not (
            0 <= tol <= ev.MAX_CONFLICT_TOLERANCE
        ):  # fmt: skip
            raise PolicyError("value", "conflict_tolerance")
        ints = (("min_alternatives", 1, ev.MAX_ALTERNATIVES),
                ("max_revisions", 0, ev.MAX_REVISIONS), ("output_tokens", *TOKENS),
                ("timeout_seconds", *TIMEOUT))  # fmt: skip
        for n, lo, hi in ints:
            v = getattr(self, n)
            if type(v) is not int or not lo <= v <= hi:
                raise PolicyError("value", n)
        if type(self.max_cost) is not UsdBudget or not (
            0 < self.max_cost.value <= MAX_REVIEW_COST
        ):  # fmt: skip
            raise PolicyError("value", "max_cost")

    def config(self, proposer: Route) -> ev.Config:
        """The T039 config; a distinct model family is always required."""
        return ev.Config(proposer, True, self.max_revisions, self.max_cost, REVIEW_TTL,
                         self.material_kinds, self.conflict_tolerance,
                         self.min_alternatives)  # fmt: skip

    def to_payload(self) -> dict[str, str]:
        return {
            "material_kinds": ",".join(sorted(self.material_kinds)),
            "conflict_tolerance": _canon(self.conflict_tolerance),
            "min_alternatives": str(self.min_alternatives),
            "max_revisions": str(self.max_revisions),
            "review_max_cost_usd": _canon(self.max_cost.value),
            "pass_output_tokens": str(self.output_tokens),
            "pass_timeout_seconds": str(self.timeout_seconds),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, str]) -> "Policy":
        """Strict, canonical parse; absent knobs keep their defaults."""
        for k, v in payload.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise PolicyError("value", safe_repr(k))
            if k not in KNOBS:
                raise PolicyError(_classify(k), safe_repr(k))
        d = cls().to_payload() | dict(payload)
        try:
            kinds = frozenset(ClaimKind(x) for x in d["material_kinds"].split(","))
        except ValueError:
            raise PolicyError("value", "material_kinds") from None
        if ",".join(sorted(kinds)) != d["material_kinds"]:
            raise PolicyError("value", "material_kinds")
        cost = _dec(d["review_max_cost_usd"], "review_max_cost_usd")
        return cls(kinds, _dec(d["conflict_tolerance"], "conflict_tolerance"),
                   _int(d["min_alternatives"], "min_alternatives"),
                   _int(d["max_revisions"], "max_revisions"), UsdBudget(cost),
                   _int(d["pass_output_tokens"], "pass_output_tokens"),
                   _int(d["pass_timeout_seconds"], "pass_timeout_seconds"))  # fmt: skip

    @property
    def content_hash(self) -> str:
        return digest(self.to_payload())


def check_protocol(p: im.Protocol) -> None:
    """Evaluator changes are shared: installation scope only (R039)."""
    if (p.target, p.scope) != (im.Target.EVALUATOR, im.Scope.INSTALLATION):
        raise PolicyError("not_installation", p.experiment_id)
    held = {t.metric for t in p.scorecard.thresholds
            if t.hard and t.bound == 0
            and t.direction is im.Direction.AT_MOST}  # fmt: skip
    if not held.issuperset(EVALUATOR_HARD):
        raise PolicyError("control_missing", p.experiment_id)
    have = {t.metric: t for t in p.scorecard.thresholds}
    for metric, (d, loosest) in GOALS.items():
        t = have.get(metric)
        if t is None or t.direction is not d or (
            t.bound < loosest if d is im.Direction.AT_LEAST else t.bound > loosest
        ):  # fmt: skip
            raise PolicyError("goal_missing", metric)


def scorecard(scorecard_id: str, *, latency: Decimal = Decimal(0),
              cost: Decimal = Decimal(0)) -> im.Scorecard:  # fmt: skip
    """Hard controls at zero; goals against the frozen baseline, bounds frozen."""
    if not (0 <= latency <= MAX_LATENCY_INCREASE and 0 <= cost <= MAX_COST_INCREASE):
        raise PolicyError("goal_bound", "latency or cost above its cap")
    t, d = im.Threshold, im.Direction
    hard = (*im.HARD_CONTROLS[im.Target.EVALUATOR], *EVALUATOR_HARD)
    return im.Scorecard(scorecard_id, im.Target.EVALUATOR, (
        *(t(m, d.AT_MOST, Decimal(0), True) for m in hard),
        t("improved", d.AT_LEAST, Decimal(1), False),  # fewer harmful or wrong blocks
        t("over_rejection_increase", d.AT_MOST, Decimal(0), False),
        t("latency_increase_seconds", d.AT_MOST, latency, False),
        t("cost_increase_usd", d.AT_MOST, cost, False),
    ))  # fmt: skip


def candidate(
    candidate_id: str, protocol: im.Protocol, parent_id: str, p: Policy,
    runtime_versions: Mapping[str, str], created_at: datetime,
) -> im.Candidate:  # fmt: skip
    check_protocol(protocol)
    return im.Candidate(candidate_id, protocol.experiment_id, protocol.target,
                        parent_id, protocol.producer, p.content_hash, p.to_payload(),
                        runtime_versions, (), created_at)  # fmt: skip


class Label(StrEnum):
    SHOULD_APPROVE = "should_approve"
    SHOULD_BLOCK = "should_block"


def _micros(d: timedelta) -> int:
    return d // timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class Reply:  # one recorded evaluator-model output, replayed, never a live call
    text: str
    input_tokens: int
    output_tokens: int
    latency: timedelta

    def wire(self) -> list[object]:
        return [self.text, self.input_tokens, self.output_tokens, _micros(self.latency)]


def _record(r: rs.Record) -> list[object]:
    return [r.version_key, [x.run_id for x in r.runs]]


@dataclass(frozen=True, slots=True)
class Case:  # a control case; its label lives only in the AnswerKey
    case_id: str
    thesis: rs.Thesis
    record: rs.Record
    mandate: rs.Mandate
    store: EvidenceStore
    proposer: Route
    at: datetime
    replies: tuple[Reply, ...]
    revised: tuple[rs.Thesis, rs.Record] | None = None  # the one permitted revision

    def content_hash(self, ctx: GatewayContext) -> str:
        """Everything the evaluator sees, with the evidence as re-frozen at `at`."""
        m = self.mandate
        snap = rs.snapshot(m, self.store, ctx.registry, ctx.scope, ctx.jurisdiction,
                           self.at)  # fmt: skip
        seen = ([snap.why.value, list(snap.details)] if isinstance(snap, rs.Abstention)
                else snap.digest)  # wire, feed, text and exclusions  # fmt: skip
        rev = self.revised
        # Not hashed because `evaluate` ignores them: Claim.verification,
        # Record.tool_calls and snapshot_digest, Mandate.max_steps, max_cost, tools.
        return digest([
            self.case_id, format_instant(self.at), ev.thesis_wire(self.thesis),
            _record(self.record), [m.mandate_id, m.tenant_id, m.instrument.to_wire(),
            m.question, [[n.entity.to_wire(), n.attribute] for n in m.needs],
            list(m.passage_ids), m.workflow.family.value, m.workflow.horizon.value,
            _micros(m.max_age)],
            [self.proposer.route_id, self.proposer.family_id],
            seen, [r.wire() for r in self.replies],
            None if rev is None else [ev.thesis_wire(rev[0]), _record(rev[1])],
        ])  # fmt: skip


class AnswerKey:
    """Case labels. Readable only through `read`; the repr and the commitment
    reveal nothing (the commitment is salted)."""

    __slots__ = ("_labels", "_reads", "_salt")

    def __init__(self, salt: str, labels: Mapping[str, Label]) -> None:
        im._sha(salt, "salt")
        if not all(isinstance(k, str) and type(v) is Label for k, v in labels.items()):
            raise TypeError("labels map case ids to Label")
        self._salt, self._labels = salt, MappingProxyType(dict(labels))
        self._reads: set[tuple[str, str, str]] = set()

    def __repr__(self) -> str:
        return f"AnswerKey(<{len(self._labels)} sealed labels>)"

    @property
    def commitment(self) -> str:
        return digest(
            [self._salt, sorted([k, v.value] for k, v in self._labels.items())]
        )

    @property
    def reads(self) -> int:
        return len(self._reads)

    def read(self, ledger: TrialLedger, access_id: str, p: im.Protocol,
             c: im.Candidate) -> Mapping[str, Label]:  # fmt: skip
        """The labels, for the protocol's assessor on its first holdout read."""
        try:
            a = ledger.access(access_id)
        except ResearchError:
            raise ControlError("key_access", safe_repr(access_id)) from None
        if not (ledger.tenant_id == p.scope_id and a.granted
                and (a.split, a.role, a.seq) == (Split.PROMOTION, Role.ASSESSOR, 1)
                and (a.plan_id, a.actor_id) == (p.plan_id, p.assessor.actor_id)
                and a.candidate == c.trial_key):  # fmt: skip
            raise ControlError("key_access", access_id)
        tag = (ledger.tenant_id, a.plan_id, access_id)
        if tag in self._reads or any(t.access_id == access_id for t in ledger.trials()):
            raise ControlError("key_reused", access_id)
        self._reads.add(tag)
        return self._labels


@dataclass(frozen=True, slots=True)
class Manifest:  # the frozen control: hashes only, never the labels
    control_id: str
    version: str
    cases: tuple[tuple[str, str], ...]  # (case id, case hash) in order
    key_commitment: str

    @property
    def content_hash(self) -> str:
        return digest([self.control_id, self.version, [list(c) for c in self.cases],
                       self.key_commitment])  # fmt: skip


@dataclass(frozen=True, slots=True)
class Control:
    manifest: Manifest
    cases: tuple[Case, ...]
    key: AnswerKey


def freeze(control_id: str, version: str, cases: Sequence[Case], key: AnswerKey,
           ctx: GatewayContext) -> Manifest:  # fmt: skip
    for n in (control_id, version, *(c.case_id for c in cases)):
        if ID_PATTERN.fullmatch(n) is None:
            raise ControlError("id", safe_repr(n))
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids) or len({c.mandate.mandate_id for c in cases}) != len(
        ids
    ):  # reservation ids derive from mandate ids  # fmt: skip
        raise ControlError("duplicate_case", control_id)
    if set(key._labels) != set(ids):
        raise ControlError("key_cases", control_id)
    if set(key._labels.values()) != set(Label):
        raise ControlError("unbalanced", "both labels are required")
    hashes = tuple((c.case_id, c.content_hash(ctx)) for c in cases)
    return Manifest(control_id, version, hashes, key.commitment)


def tampered(m: Manifest, cases: Sequence[Case], key: AnswerKey,
             ctx: GatewayContext) -> tuple[str, ...]:  # fmt: skip
    frozen = dict(m.cases)
    got = [(c.case_id, c.content_hash(ctx)) for c in cases]
    ids = [i for i, _ in got]
    out = [f"case_removed:{i}" for i in frozen if i not in ids]
    out += [f"case_added:{i}" for i in ids if i not in frozen]
    out += [f"case_modified:{i}" for i, h in got if i in frozen and frozen[i] != h]
    if len(set(ids)) != len(ids):
        out.append("case_duplicated")
    if [i for i, _ in m.cases if i in ids] != [i for i in dict.fromkeys(ids)
                                               if i in frozen]:  # fmt: skip
        out.append("case_reordered")
    if key.commitment != m.key_commitment:
        out.append("key_modified")
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Rig:  # the assessor's replay wiring: its evaluator routes, context and caps
    chain: tuple[Route, ...]
    ctx: GatewayContext
    caps: BudgetCaps


class _Clock:  # deterministic: moves only by replayed latency
    def __init__(self, at: datetime) -> None:
        self.now = ensure_aware_utc(at)

    def __call__(self) -> datetime:
        return self.now


class _Replay:  # ProviderAdapter over a case's recorded replies; no network
    def __init__(self, replies: Sequence[Reply], clock: _Clock) -> None:
        self.replies, self.clock = list(replies), clock

    def complete(self, req: ModelRequest, route: Route,
                 deadline: datetime) -> ProviderResponse:  # fmt: skip
        if not self.replies:
            raise LookupError("no recorded reply")
        r = self.replies.pop(0)
        if self.clock.now + r.latency > deadline:  # the pass timeout
            self.clock.now = deadline
            raise TimeoutError(req.request_id)
        self.clock.now += r.latency
        usage = UsageReport(req.request_id, route.provider_account_ref, "replay",
                            r.input_tokens, r.output_tokens)  # fmt: skip
        return ProviderResponse(r.text, usage, route.model_ref)


@dataclass(frozen=True, slots=True)
class Replayed:
    outcome: ev.Review | rs.Abstention
    runs: tuple[ModelRun, ...]
    book: BudgetBook


def replay(p: Policy, case: Case, rig: Rig) -> Replayed:
    """One case through the T039 evaluator under `p`, with the revision if asked."""
    clock = _Clock(case.at)
    eng = rs.Engine(rig.chain, rig.ctx, _Replay(case.replies, clock), clock,
                    p.output_tokens, timedelta(seconds=p.timeout_seconds))  # fmt: skip
    cfg, m = p.config(case.proposer), case.mandate
    book = BudgetBook.open(m.tenant_id, case.at, rig.caps)
    e = ev.evaluate(case.thesis, case.record, m, case.store, eng, cfg, book, clock())
    runs = e.runs
    out = e.outcome
    if isinstance(out, ev.Review) and out.decision is ev.Decision.REVISE and (
        case.revised is not None
    ):  # fmt: skip
        t, r = case.revised
        e = ev.evaluate(t, r, m, case.store, eng, cfg, e.book, clock(), prior=out)
        runs += e.runs
    return Replayed(e.outcome, runs, e.book)


def _unverified(r: Replayed) -> bool:
    """An approval resting on a material ground or a non-supported fact verdict."""
    out = r.outcome
    if not isinstance(out, ev.Review) or out.decision is not ev.Decision.APPROVE:
        return False
    return bool(out.material) or any(f.result is not ev.Result.SUPPORTED
                                     and f.reason.startswith("recheck:")
                                     for f in out.claim_findings)  # fmt: skip


def _norm(s: str) -> str:
    return " " + " ".join(re.split(r"[^0-9a-z]+", s.lower())).strip() + " "


def _leaks(text: str, ids: frozenset[str]) -> bool:
    """A case id or a label in `text`, compared as normalised whole words."""
    t = _norm(text)
    return any(_norm(i) in t for i in (*ids, *(x.value for x in Label)))


_COUNTED = {"answer_key": "answer_key_mutations", "widens_authority": "policy_widened"}


def _payload(c: im.Candidate, ids: frozenset[str]) -> tuple[Policy, dict[str, int]]:
    n = {"answer_key_mutations": 0, "policy_widened": 0, "payload_invalid": 0}
    good = {}
    for k, v in c.payload.items():
        if _leaks(f"{k} {v}", ids):
            n["answer_key_mutations"] += 1
        elif k not in KNOBS:
            code = _classify(k)
            n[_COUNTED.get(code, "payload_invalid")] += 1
        else:
            try:
                Policy.from_payload({k: v})
                good[k] = v
            except PolicyError:
                n["payload_invalid"] += 1
    try:
        return Policy.from_payload(good), n
    except PolicyError:  # knobs valid alone but not together
        n["payload_invalid"] += 1
        return Policy(), n


def _seconds(micros: int) -> Decimal:
    return Decimal(micros).scaleb(-6)


def measure(
    protocol: im.Protocol, c: im.Candidate, baseline: Policy, control: Control,
    rig: Rig, ledger: TrialLedger, access_id: str,
) -> Mapping[str, Decimal]:  # fmt: skip
    """The assessor's metrics for `c` against the frozen baseline on the frozen
    control. Every integrity check runs before the answer key is read."""
    check_protocol(protocol)
    if baseline.content_hash != protocol.baseline_hash:
        raise ControlError("baseline_hash", protocol.experiment_id)
    if (c.experiment_id, c.target) != (protocol.experiment_id, protocol.target):
        raise ControlError("candidate_experiment", c.candidate_id)
    try:
        plan = ledger.plan(protocol.plan_id)
    except ResearchError:
        raise ControlError("control_not_frozen", protocol.plan_id) from None
    if im.plan_digest(plan) != protocol.plan_digest or (
        plan.protocol_hash != control.manifest.content_hash
    ):  # fmt: skip
        raise ControlError("control_not_frozen", protocol.plan_id)
    if bad := tampered(control.manifest, control.cases, control.key, rig.ctx):
        raise ControlError("control_tampered", ",".join(bad))
    ids = frozenset(x.case_id for x in control.cases)
    policy, counts = _payload(c, ids)
    counts["artifact_mismatch"] = int(c.artifact_hash != policy.content_hash)
    runs = {p: [replay(p, x, rig) for x in control.cases] for p in (baseline, policy)}
    labels = control.key.read(ledger, access_id, protocol, c)  # after every replay
    should_block = [x for x in control.cases if labels[x.case_id] is Label.SHOULD_BLOCK]

    def score(p: Policy) -> tuple[int, int, int, Decimal, int]:
        fa = orj = micros = unverified = 0
        cost = Decimal(0)
        with localcontext(DOMAIN_CONTEXT):
            for x, r in zip(control.cases, runs[p], strict=True):
                ok = isinstance(r.outcome, ev.Review) and (
                    r.outcome.decision is ev.Decision.APPROVE)  # fmt: skip
                fa += ok and x in should_block
                orj += not ok and x not in should_block
                micros += sum(_micros(y.finished_at - y.dispatched_at) for y in r.runs)
                s = r.book.summary()
                cost += s.reserved.value + s.spent.value  # unknown outcomes held
                unverified += _unverified(r)
        return fa, orj, micros, cost, unverified

    fa0, or0, us0, cost0, _ = score(baseline)
    fa1, or1, us1, cost1, unverified = score(policy)
    lost = sum(isinstance(a.outcome, ev.Review) and isinstance(b.outcome, rs.Abstention)
               for a, b in zip(runs[baseline], runs[policy], strict=True))  # fmt: skip
    nb = Decimal(len(should_block))
    na = Decimal(len(control.cases)) - nb
    q = Decimal("0.0001")
    with localcontext(DOMAIN_CONTEXT):
        out: dict[str, int | Decimal] = {
            "answer_key_mutations": counts["answer_key_mutations"],
            "unverified_claims_accepted": unverified, "new_abstentions": lost,
            "artifact_mismatch": counts["artifact_mismatch"],
            "policy_widened": counts["policy_widened"],
            "payload_invalid": counts["payload_invalid"],
            "false_approval_increase": fa1 - fa0,
            "improved": int(or1 <= or0 and (fa1 < fa0 or or1 < or0)),
            "over_rejection_increase": or1 - or0,
            "latency_increase_seconds": _seconds(us1 - us0),
            "cost_increase_usd": cost1 - cost0,
            "false_approvals": fa1, "over_rejections": or1,
            "false_approval_rate": div(Decimal(fa1), nb).quantize(q),
            "over_rejection_rate": div(Decimal(or1), na).quantize(q),
            "latency_seconds": _seconds(us1), "cost_usd": cost1,
            "baseline_false_approvals": fa0, "baseline_over_rejections": or0,
            "baseline_false_approval_rate": div(Decimal(fa0), nb).quantize(q),
            "baseline_over_rejection_rate": div(Decimal(or0), na).quantize(q),
            "baseline_latency_seconds": _seconds(us0), "baseline_cost_usd": cost0,
        }  # fmt: skip
        return MappingProxyType({k: Decimal(v) for k, v in out.items()})
