"""AI-mode routing of rule, research and chat candidates through one proposal graph
(T040 increment 1; spec §7, §10 live workflow, §13, §15; R015, R019, R023, R056,
R067, R074, R085). Domain only: no adapter, network, provider SDK or persistence.
- Mode is the adopted policy's `ai_mode` (ONB16) via `proposals.policy_mode`;
  nothing here takes a mode argument. It changes only by adopting a new policy
  version, an explicit step-up consent (`mode_changes`); a provider failure,
  budget refusal or chat message never changes it. A version binds the policy
  hash, so after a mode change an in-flight candidate fails `POLICY_CHANGED` and
  is invalidated, never relabelled into the other mode's path (spec §10).
- `submit` is the one entry for every origin: verifying, then a deterministic
  re-check of every claim against evidence frozen at the decision time (facts
  match passages; calculations are recomputed by registered formula). AI mode
  sends rule, research and chat candidates alike to `awaiting_review`; rules mode
  sends rule candidates to `ready_for_final_check` (rules_verified, never
  AI-reviewed) and AI or chat candidates to `research_only` (`NOT_AI_MODE`).
- A rules-origin `Candidate` carries its `Fact`/`Calc` inputs; its thesis must be
  `rule_thesis` of them and its record the run-free `rule_record`, so a model
  thesis cannot be relabelled as a rule (the evaluator also refuses a rules
  thesis holding anything but facts and formula calculations).
- `apply` hands over a T039 evaluation. An abstention leaves the candidate
  pending with a `REVIEW_UNAVAILABLE` event. A review must bind the version
  (proposal id, version, thesis hash); approval only reaches the T026 final check.
- Deadline: the earlier of expiry and the original trigger plus the review window,
  at most 60 seconds for intraday actions (spec §15). It comes from the immutable
  version's trigger, so a retry or revision cannot move it. A review not applied
  before it expires the candidate with `REVIEW_DEADLINE_MISSED`; `tally` counts
  misses in the denominator (R067).
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from qw_domain import evaluator as ev
from qw_domain import researcher as rs
from qw_domain.evidence import ClaimKind, EvidenceStore
from qw_domain.gateway import GatewayContext
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.policy import PolicyHistory
from qw_domain.proposals import (
    Code,
    Current,
    Mode,
    Proposal,
    ProposalState,
    ProposalVersion,
    Reason,
    revise,
    settle,
    step,
)
from qw_domain.strategy_registry import Family, Horizon

P = ProposalState
INTRADAY_REVIEW_LIMIT = timedelta(seconds=60)  # spec §15, provisional


class RoutingError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class Origin(StrEnum):
    RULES = "rules"
    AI_RESEARCH = "ai_research"
    CHAT = "chat"


class Label(StrEnum):  # which verification a candidate has had (R019, R023)
    RULES_VERIFIED = "rules_verified"  # deterministic checks only
    REVIEW_PENDING = "review_pending"
    AI_REVIEWED = "ai_reviewed"
    REVIEW_MISSED = "review_missed"
    NOT_ACTIONABLE = "not_actionable"


@dataclass(frozen=True, slots=True)
class ModeChange:
    policy_version: int
    previous: Mode | None
    mode: Mode
    principal_id: str
    step_up_receipt_id: str
    signed_at: datetime
    receipt_hash: str


def mode_changes(h: PolicyHistory) -> tuple[ModeChange, ...]:
    """Each adoption that set or changed the mode, with its consent."""
    out: list[ModeChange] = []
    prev: Mode | None = None
    for rc in h.receipts:
        fields = h.versions[rc.version - 1].draft.fields
        value = next((f.value for f in fields if f.name == "ai_mode"), "")
        if value not in set(Mode):
            raise RoutingError("mode", f"policy version {rc.version} names none")
        if (mode := Mode(str(value))) is not prev:
            out.append(ModeChange(rc.version, prev, mode, rc.principal_id,
                rc.step_up_receipt_id, rc.signed_at, rc.receipt_hash))  # fmt: skip
        prev = mode
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Fact:  # a rule input reported by one passage
    claim_id: str
    passage_id: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class Calc:  # a rule calculation: registered formula over input passages, in order
    claim_id: str
    formula: str
    inputs: tuple[str, ...]
    value: Decimal


def rule_record(m: rs.Mandate) -> rs.Record:
    """The audit record of a rule thesis: no model run produced it."""
    return rs.Record(m.mandate_id, m.workflow.material_hash, (), None, (), ())


def rule_thesis(
    m: rs.Mandate, summary: str, facts: tuple[Fact, ...], calcs: tuple[Calc, ...],
    invalidation: tuple[str, ...],
) -> rs.Thesis:  # fmt: skip
    """A rule output as structured claims: inputs as reported facts, each
    calculation with its formula and typed input records (R023, R085)."""
    fact, calc = ClaimKind.REPORTED_FACT, ClaimKind.CALCULATION
    claims = [rs.Claim(f.claim_id, fact, f"rule input {f.passage_id}",
                       (f.passage_id,), f.value, "") for f in facts]  # fmt: skip
    claims += [rs.Claim(c.claim_id, calc, f"{c.formula}({','.join(c.inputs)})",
                        c.inputs, c.value, "", c.formula) for c in calcs]  # fmt: skip
    return rs.Thesis(m.mandate_id, m.instrument, summary, tuple(claims), (),
                     ("no action",), invalidation, "rule output")  # fmt: skip


@dataclass(frozen=True, slots=True)
class Candidate:
    origin: Origin
    thesis: rs.Thesis
    mandate: rs.Mandate
    record: rs.Record
    review_window: timedelta  # from the original trigger (policy deadline)
    facts: tuple[Fact, ...] = ()  # rules origin: the thesis is rebuilt from these
    calcs: tuple[Calc, ...] = ()

    def __post_init__(self) -> None:
        m, t = self.mandate, self.thesis
        rules = m.workflow.family is not Family.AI_THESIS
        if rules is not (self.origin is Origin.RULES):
            raise RoutingError("origin", "rules origin iff a non-ai_thesis workflow")
        if rules and (
            not self.facts
            or self.record != rule_record(m)
            or t != (rule_thesis(m, t.summary, self.facts, self.calcs, t.invalidation))
        ):
            raise RoutingError("rule_thesis", "not the rule_thesis of its inputs")
        if not rules and (not self.record.runs or self.facts or self.calcs):
            raise RoutingError("origin", "a model origin carries its model runs")
        if {self.thesis.mandate_id, self.record.mandate_id} != {
            self.mandate.mandate_id
        } or self.thesis.instrument != self.mandate.instrument:  # fmt: skip
            raise RoutingError("mandate", "thesis, record and mandate differ")
        if self.review_window <= timedelta(0):
            raise RoutingError("review_window", "positive")


def deadline(v: ProposalVersion, c: Candidate) -> datetime:
    limit = c.review_window
    if v.action.horizon == Horizon.INTRADAY.value:
        limit = min(limit, INTRADAY_REVIEW_LIMIT)
    return min(v.expires_at, v.trigger_at + limit)


@dataclass(frozen=True, slots=True)
class Routed:
    proposal: Proposal
    candidate: Candidate
    reasons: tuple[Reason, ...] = ()  # why the last call did not advance
    review: ev.Review | None = None  # the latest review applied


def _bind(v: ProposalVersion, c: Candidate) -> None:
    a, m = v.action, c.mandate
    got = (a.tenant_id, a.instrument, v.proposal_id, a.strategy_id)
    if got != (m.tenant_id, m.instrument, m.mandate_id, m.workflow.strategy_id):
        raise RoutingError("binding", "tenant, instrument, id or strategy differ")


def _unsupported(c: Candidate, store: EvidenceStore, ctx: GatewayContext,
                 at: datetime) -> tuple[str, ...]:  # fmt: skip
    m = c.mandate
    snap = rs.snapshot(m, store, ctx.registry, ctx.scope, ctx.jurisdiction, at)
    if isinstance(snap, rs.Abstention):
        return snap.details
    by_id = {i.passage_id: i for i in snap.items}
    bad = {x.claim_id: w for x in c.thesis.claims
           if (w := rs._problem(x, m, by_id, set(by_id)))}  # fmt: skip
    out = [f"{k}:{w}" for k, w in bad.items()]
    facts = [x for x in c.thesis.claims if x.kind is ClaimKind.REPORTED_FACT]
    if all(x.claim_id in bad for x in facts):
        out.append("no_supported_fact")
    return tuple(out)


def _note(r: Routed, dst: ProposalState, at: datetime, code: Code, why: str) -> Routed:
    """Record a coded event: a same-state note or a deadline expiry."""
    p = r.proposal._record(dst, at, code, why)
    return replace(r, proposal=p, reasons=(Reason(code, why),))


def _route(p: Proposal, c: Candidate, store: EvidenceStore, ctx: GatewayContext,
           cur: Current) -> Routed:  # fmt: skip
    d = step(p, P.VERIFYING, cur)
    if not d.accepted:
        return Routed(d.proposal, c, d.reasons)
    if bad := _unsupported(c, store, ctx, cur.as_of):
        why = Reason(Code.MATERIAL_CLAIM_UNSUPPORTED, ",".join(bad))
        d = step(d.proposal, P.REJECTED, cur, why.code, why.detail)
        return Routed(d.proposal, c, (why,))
    code: Code | None = None
    if d.proposal.latest.mode is Mode.AI_ENABLED:
        dst = P.AWAITING_REVIEW  # every origin, rules included (R023)
    elif c.origin is Origin.RULES:
        dst = P.READY_FOR_FINAL_CHECK
    else:
        dst, code = P.RESEARCH_ONLY, Code.NOT_AI_MODE
    d = step(d.proposal, dst, cur, code)
    return Routed(d.proposal, c, d.reasons or ((Reason(code),) if code else ()))


def submit(v: ProposalVersion, c: Candidate, store: EvidenceStore,
           ctx: GatewayContext, cur: Current) -> Routed:  # fmt: skip
    """Enter the proposal graph; the mode is the adopted policy's."""
    _bind(v, c)
    return _route(Proposal.new(v, cur), c, store, ctx, cur)


def sweep(r: Routed, cur: Current) -> Routed:
    """Record a missed review deadline, else any expiry or invalidation."""
    p, v = r.proposal, r.proposal.latest
    dl = deadline(v, r.candidate)
    if p.state(v.version) is P.AWAITING_REVIEW and cur.as_of >= dl:
        return _note(r, P.EXPIRED, cur.as_of, Code.REVIEW_DEADLINE_MISSED,
                     format_instant(dl))  # fmt: skip
    p, closed = settle(p, cur)
    return replace(r, proposal=p, reasons=closed)


_DECIDED = {
    ev.Decision.RESEARCH_ONLY: (P.RESEARCH_ONLY, Code.REVIEW_RESEARCH_ONLY),
    ev.Decision.REJECT: (P.REJECTED, Code.REVIEW_REJECTED),
}


def apply(r: Routed, e: ev.Evaluation, cur: Current) -> Routed:
    """Hand a T039 evaluation to the candidate awaiting it."""
    r = sweep(r, cur)
    p, v = r.proposal, r.proposal.latest
    if r.reasons:
        return r
    if (state := p.state(v.version)) is not P.AWAITING_REVIEW:
        return replace(r, reasons=(Reason(Code.NOT_READY, state),))
    out, at = e.outcome, cur.as_of
    if isinstance(out, rs.Abstention):  # stays pending, visibly
        return _note(r, P.AWAITING_REVIEW, at, Code.REVIEW_UNAVAILABLE, out.why)
    digest = rs._sha(rs._json(ev.thesis_wire(r.candidate.thesis)))
    if (out.proposal_id, out.proposal_version, out.proposal_hash) != (
        v.proposal_id, v.version, digest,
    ):  # fmt: skip
        raise RoutingError("review_binding", "another proposal, version or thesis")
    if ensure_aware_utc(out.completed_at) > at:
        raise RoutingError("review_time", "completed after the decision time")
    r = replace(r, review=out)  # completed_at <= as_of < deadline (swept above)
    why = ",".join(out.material)
    if out.decision is ev.Decision.REVISE:
        return _note(r, P.AWAITING_REVIEW, at, Code.REVIEW_REVISE, why)
    if out.decision is ev.Decision.APPROVE:
        d = step(p, P.READY_FOR_FINAL_CHECK, cur)
        return replace(r, proposal=d.proposal, reasons=d.reasons)
    dst, code = _DECIDED[out.decision]
    d = step(p, dst, cur, code, why)
    return replace(r, proposal=d.proposal, reasons=d.reasons or (Reason(code),))


def resubmit(r: Routed, v: ProposalVersion, c: Candidate, store: EvidenceStore,
             ctx: GatewayContext, cur: Current) -> Routed:  # fmt: skip
    """The one permitted revision after a `revise` review (T026 bounds it). The
    trigger, origin and review window are kept, so the deadline cannot move."""
    if r.review is None or r.review.decision is not ev.Decision.REVISE:
        raise RoutingError("not_revise", "no revise review to answer")
    old = (r.proposal.latest.trigger_at, r.candidate.origin, r.candidate.review_window)
    if (v.trigger_at, c.origin, c.review_window) != old:
        raise RoutingError("trigger_reset", "trigger, origin and window are kept")
    _bind(v, c)
    d = revise(r.proposal, v, cur)
    if not d.accepted:
        return replace(r, proposal=d.proposal, reasons=d.reasons)
    return _route(d.proposal, c, store, ctx, cur)


def label(r: Routed) -> Label:
    p, v, rv = r.proposal, r.proposal.latest, r.review
    state, ai = p.state(v.version), v.mode is Mode.AI_ENABLED
    if p.events[-1].code is Code.REVIEW_DEADLINE_MISSED:
        return Label.REVIEW_MISSED
    if state in (P.READY_FOR_FINAL_CHECK, P.ACTIVE):
        if not ai:
            return Label.RULES_VERIFIED
        bound = (v.proposal_id, v.version, ev.Decision.APPROVE)
        got = rv and (rv.proposal_id, rv.proposal_version, rv.decision)
        ok = got == bound
        return Label.AI_REVIEWED if ok else Label.REVIEW_PENDING
    pending = state in (P.CANDIDATE, P.VERIFYING, P.AWAITING_REVIEW)
    return Label.REVIEW_PENDING if pending and ai else Label.NOT_ACTIONABLE


def tally(routed: Iterable[Routed]) -> dict[Label, int]:
    """Every candidate counts, misses included (R067)."""
    got = Counter(label(r) for r in routed)
    return {k: got.get(k, 0) for k in Label}
