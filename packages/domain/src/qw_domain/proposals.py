"""Proposal versions, lifecycle and final checks (T026).

Spec §7 "Proposal objects and lifecycle", "Timing and invalidation"; §3 "Atomic
final decision"; T008 F-05, F-10, F-15. Conventions:
- A `ProposalVersion` is immutable. It binds the adopted policy hash, per-account
  journal revisions, input observation times with their freshness bounds, and a
  digest of the risk evaluation. A revision appends a version and supersedes the
  old one. History is an append-only event list; state is derived from it.
- `step` moves along the pipeline. Only `publish` enters `active`, only `dismiss`
  enters `dismissed`, and `settle` records expiry or invalidation from caller time.
- Every decision settles first. At or after `expires_at` the version expires. It is
  invalidated when a bound revision, the policy hash, an input's freshness or the
  risk evaluation (recomputed here from `Current`) no longer holds, or a pause
  covers it. A late decision is refused, never applied.
- The proposal mode comes from the adopted policy (`ai_mode`), never the caller.
- Planning commitments live in `qw_domain.commitments`; `publish` takes an
  admission callback, so this module does not depend on them.
Stdlib only.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from qw_domain.decimals import DOMAIN_CONTEXT, Money, Price
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.policy import PolicyHistory
from qw_domain.risk import (
    Outcome,
    ProposedAction,
    RiskEvaluation,
    RiskInputs,
    Side,
    evaluate,
)
from qw_domain.risk_pauses import PauseBook


class ProposalState(StrEnum):
    CANDIDATE = "candidate"
    VERIFYING = "verifying"
    AWAITING_REVIEW = "awaiting_review"
    READY_FOR_FINAL_CHECK = "ready_for_final_check"
    ACTIVE = "active"
    RESEARCH_ONLY = "research_only"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class ExecutionState(StrEnum):
    """Orthogonal to the proposal state; only `planned` is set here."""

    NONE = "none"
    PLANNED = "planned"
    USER_REPORTED = "user_reported"
    BROKER_CONFIRMED = "broker_confirmed"
    RECONCILED = "reconciled"


class Mode(StrEnum):
    RULES_ONLY = "rules_only"
    AI_ENABLED = "ai_enabled"


class Code(StrEnum):
    EXPIRED = "EXPIRED"
    POLICY_CHANGED = "POLICY_CHANGED"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"
    INPUT_STALE = "INPUT_STALE"
    RISK_PAUSED = "RISK_PAUSED"
    RISK_CHANGED = "RISK_CHANGED"
    RISK_BLOCKED = "RISK_BLOCKED"
    TENANT_MISMATCH = "TENANT_MISMATCH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NOT_AI_MODE = "NOT_AI_MODE"
    NOT_READY = "NOT_READY"
    NOT_ACTIVE = "NOT_ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVISION_LIMIT = "REVISION_LIMIT"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    ALTERNATIVE_SELECTED = "ALTERNATIVE_SELECTED"
    CAPITAL_CONFLICT = "CAPITAL_CONFLICT"  # commitment admission
    CASH_UNCONFIRMED = "CASH_UNCONFIRMED"  # commitment admission


P = ProposalState
_CLOSE = frozenset({P.EXPIRED, P.INVALIDATED, P.SUPERSEDED})
_END: frozenset[ProposalState] = frozenset()
# fmt: off
TRANSITIONS: MappingProxyType[ProposalState, frozenset[ProposalState]] = (
    MappingProxyType({
        P.CANDIDATE: _CLOSE | {P.VERIFYING, P.RESEARCH_ONLY, P.REJECTED},
        P.VERIFYING: _CLOSE | {
            P.AWAITING_REVIEW, P.READY_FOR_FINAL_CHECK, P.RESEARCH_ONLY, P.REJECTED},
        P.AWAITING_REVIEW: _CLOSE | {
            P.READY_FOR_FINAL_CHECK, P.RESEARCH_ONLY, P.REJECTED},
        P.READY_FOR_FINAL_CHECK: _CLOSE | {P.ACTIVE, P.REJECTED},
        P.ACTIVE: _CLOSE | {P.DISMISSED},
        P.RESEARCH_ONLY: _END, P.REJECTED: _END, P.EXPIRED: _END,
        P.INVALIDATED: _END, P.DISMISSED: _END, P.SUPERSEDED: _END,
    })
)
# fmt: on
TERMINAL = frozenset(s for s, dst in TRANSITIONS.items() if not dst)
_STEPS = (TRANSITIONS[P.CANDIDATE] | TRANSITIONS[P.VERIFYING]) - _CLOSE
MAX_REVIEW_REVISIONS = 1  # spec §7: one evaluation plus at most one revised submission


class ProposalError(ValueError):
    """Misuse: an illegal transition, a malformed version or mismatched inputs."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def check_transition(src: ProposalState, dst: ProposalState) -> None:
    if dst not in TRANSITIONS[src]:
        raise ProposalError("illegal", f"{src} -> {dst}")


def _wire(x: object) -> object:
    if isinstance(x, datetime):
        return format_instant(x)
    if isinstance(x, timedelta):
        return x // timedelta(microseconds=1)
    if isinstance(x, UUID):
        return str(x)
    to_wire = getattr(x, "to_wire", None)
    if callable(to_wire):
        return to_wire()
    raise TypeError(f"no canonical form for {type(x).__name__}")


def _hash(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=_wire)
    return hashlib.sha256(text.encode()).hexdigest()


def risk_digest(ev: RiskEvaluation) -> str:
    """Outcome, reasons, requested size and policy. Limit values and the feasible
    size are left out: a price tick that changes neither outcome nor reasons is
    not material, and commitments reserve at the current price instead."""
    reasons = [[r.code, r.detail] for r in ev.reasons]
    return _hash([ev.outcome, reasons, ev.requested, ev.policy_hash, ev.method])


def policy_mode(history: PolicyHistory | None) -> Mode:
    """The adopted policy's AI mode (ONB16)."""
    adopted = history.adopted if history is not None else None
    if adopted is None:
        raise ProposalError("policy_unbound", "no adopted policy")
    value = next((f.value for f in adopted[0].draft.fields if f.name == "ai_mode"), "")
    if value not in set(Mode):
        raise ProposalError("mode", "the adopted policy names no AI mode")
    return Mode(str(value))


@dataclass(frozen=True, slots=True)
class Observation:
    key: str  # e.g. "mark:<instrument>", "fx:USD", "account:<id>"
    observed_at: datetime
    max_age: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))
        if self.max_age <= timedelta(0):
            raise ProposalError("observation", "max_age must be positive")

    def stale(self, as_of: datetime) -> bool:
        return self.observed_at > as_of or as_of - self.observed_at > self.max_age


@dataclass(frozen=True, slots=True)
class Binding:
    policy_hash: str
    account_revisions: tuple[tuple[str, int], ...]
    observations: tuple[Observation, ...]
    risk_digest: str
    risk_outcome: Outcome

    @classmethod
    def of(
        cls,
        ev: RiskEvaluation,
        revisions: Mapping[str, int],
        obs: tuple[Observation, ...],
    ) -> "Binding":
        """Bind what the evaluation saw: the policy, account revisions, input times."""
        if ev.policy_hash is None:
            raise ProposalError("policy_unbound", "no adopted policy to bind")
        if not revisions or min(revisions.values()) < 1:
            raise ProposalError("revision", "bind at least one revision >= 1")
        revs = tuple(sorted(revisions.items()))
        return cls(ev.policy_hash, revs, obs, risk_digest(ev), ev.outcome)


def cash_requirement(action: ProposedAction, price: Price) -> Money:
    """Estimated cash effect of a buy: fixed + (price + unit cost) x quantity,
    rounded up to the money scale. A sale needs none; its proceeds are not cash."""
    if action.side is not Side.BUY:
        return Money.of(0, action.currency)
    with localcontext(DOMAIN_CONTEXT):
        unit = price.value + action.unit_cost.amount.value
        exact = action.fixed_cost.amount.value + unit * action.quantity.value
        amount = exact.quantize(Decimal(1).scaleb(-12), rounding=ROUND_CEILING)
    return Money.of(amount, action.currency)


@dataclass(frozen=True, slots=True)
class ProposalVersion:
    proposal_id: str
    version: int
    action: ProposedAction
    binding: Binding
    requirement: Money  # estimated cash effect at binding time
    trigger_at: datetime
    received_at: datetime
    created_at: datetime
    expires_at: datetime
    mode: Mode
    alternatives_group_id: str | None

    def __post_init__(self) -> None:
        for name in ("trigger_at", "received_at", "created_at", "expires_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))
        t = (self.trigger_at, self.received_at, self.created_at, self.expires_at)
        if not (t[0] <= t[1] <= t[2] < t[3]):  # F-15
            raise ProposalError("time_order", "trigger <= received <= created < expiry")
        if self.version < 1 or not self.proposal_id:
            raise ProposalError("version", "versions start at 1 under a proposal id")
        if self.alternatives_group_id == "":
            raise ProposalError("group", "an alternatives group id is non-empty")
        req = self.requirement
        if req.currency != self.action.currency or req.amount.value < 0:
            raise ProposalError("requirement", "non-negative, in the action currency")
        if self.action.side is not Side.BUY and req.amount.value != 0:
            raise ProposalError("requirement", "a sale reserves no cash")

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.action.tenant_id, self.action.account_id, self.action.currency

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self))

    @property
    def canonical(self) -> object:
        """The hashed content as plain JSON values (the stored form)."""
        return json.loads(json.dumps(asdict(self), default=_wire))


@dataclass(frozen=True, slots=True)
class Current:
    """Decision-time records supplied by the caller. The risk evaluation is
    recomputed from `history` and `inputs`, never passed in separately."""

    as_of: datetime
    history: PolicyHistory | None
    inputs: RiskInputs
    account_revisions: Mapping[str, int]
    pauses: PauseBook

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))

    def evaluate(
        self, action: ProposedAction, inputs: RiskInputs | None = None
    ) -> RiskEvaluation:
        at_now = replace(inputs or self.inputs, as_of=self.as_of)
        return evaluate(self.history, action, at_now, self.pauses)


@dataclass(frozen=True, slots=True)
class Reason:
    code: Code
    detail: str = ""


def final_check(v: ProposalVersion, cur: Current) -> tuple[Reason, ...]:
    """Why the bound snapshot no longer holds; empty when it still holds."""
    a, b, now = v.action, v.binding, cur.as_of
    if cur.pauses.tenant_id != a.tenant_id:
        return (Reason(Code.TENANT_MISMATCH),)
    ev = cur.evaluate(a)
    out = [Reason(Code.INPUT_STALE, o.key) for o in b.observations if o.stale(now)]
    if ev.policy_hash is None or ev.policy_hash != b.policy_hash:
        out.append(Reason(Code.POLICY_CHANGED))
    for account, revision in b.account_revisions:
        if cur.account_revisions.get(account) != revision:
            out.append(Reason(Code.ACCOUNT_CHANGED, account))
    if a.side is Side.BUY or a.covers_short_call:  # the reduction rule of risk.py
        on = cur.pauses.blocking(a.tenant_id, a.account_id, a.sleeve_id, a.strategy_id)
        out += [
            Reason(Code.RISK_PAUSED, f"{p.scope.kind}:{p.scope.scope_id}") for p in on
        ]
    if risk_digest(ev) != b.risk_digest:
        out.append(Reason(Code.RISK_CHANGED, ev.outcome))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class Event:
    version: int
    state: ProposalState
    execution: ExecutionState
    at: datetime
    code: Code | None = None
    detail: str = ""


def _check_mode(v: ProposalVersion, cur: Current) -> None:
    if v.mode is not policy_mode(cur.history):
        raise ProposalError("mode", "the mode comes from the adopted policy")


@dataclass(frozen=True, slots=True)
class Proposal:
    """One proposal id: append-only versions and events."""

    versions: tuple[ProposalVersion, ...]
    events: tuple[Event, ...]

    @classmethod
    def new(cls, v: ProposalVersion, cur: Current) -> "Proposal":
        if v.version != 1:
            raise ProposalError("version", "a new proposal starts at version 1")
        _check_mode(v, cur)
        return cls((v,), (Event(1, P.CANDIDATE, ExecutionState.NONE, cur.as_of),))

    @property
    def latest(self) -> ProposalVersion:
        return self.versions[-1]

    @property
    def live(self) -> bool:
        return self.state(self.latest.version) not in TERMINAL

    def _last(self, n: int) -> Event:
        return next(e for e in reversed(self.events) if e.version == n)

    def state(self, n: int) -> ProposalState:
        return self._last(n).state

    def execution(self, n: int) -> ExecutionState:
        return self._last(n).execution

    # fmt: off
    def _record(self, dst: ProposalState, at: datetime, code: Code | None = None,
                why: str = "", execution: ExecutionState | None = None) -> "Proposal":
        last = self._last(self.latest.version)
        if at < self.events[-1].at:
            raise ProposalError("time_regressed", "decision times never go back")
        if dst is not last.state:
            check_transition(last.state, dst)
        event = Event(last.version, dst, execution or last.execution, at, code, why)
        return replace(self, events=(*self.events, event))
    # fmt: on


def _closing(v: ProposalVersion, cur: Current) -> tuple[Reason, ...]:
    if cur.as_of >= v.expires_at:
        return (Reason(Code.EXPIRED, format_instant(v.expires_at)),)
    return final_check(v, cur)


def resolve(p: Proposal, cur: Current) -> ProposalState:
    """Read-time status of the latest version, without recording anything."""
    state = p.state(p.latest.version)
    reasons = () if state in TERMINAL else _closing(p.latest, cur)
    if not reasons:
        return state
    return P.EXPIRED if reasons[0].code is Code.EXPIRED else P.INVALIDATED


def settle(p: Proposal, cur: Current) -> tuple[Proposal, tuple[Reason, ...]]:
    """Record expiry or invalidation of the latest version. Reasons are returned
    only when the state changed."""
    if cur.as_of < p.events[-1].at:
        raise ProposalError("time_regressed", "decision times never go back")
    state = p.state(p.latest.version)
    reasons = () if state in TERMINAL else _closing(p.latest, cur)
    if not reasons:
        return p, ()
    dst = P.EXPIRED if reasons[0].code is Code.EXPIRED else P.INVALIDATED
    return p._record(dst, cur.as_of, reasons[0].code, reasons[0].detail), reasons


def expire(p: Proposal, as_of: datetime) -> Proposal:
    """Record expiry of a live latest version at or after `expires_at`, exactly as
    `settle` would; it needs no inputs, so an expiry sweep can run without them."""
    as_of, v = ensure_aware_utc(as_of), p.latest
    if p.state(v.version) in TERMINAL or as_of < v.expires_at:
        return p
    return p._record(P.EXPIRED, as_of, Code.EXPIRED, format_instant(v.expires_at))


@dataclass(frozen=True, slots=True)
class Decision:
    accepted: bool
    proposal: Proposal
    reasons: tuple[Reason, ...] = ()
    siblings: Mapping[str, Proposal] = field(default_factory=dict)


def _no(p: Proposal, *why: Reason) -> Decision:
    return Decision(False, p, why)


def step(p: Proposal, dst: ProposalState, cur: Current) -> Decision:
    """A pipeline move: verifying, review, final check, research-only, rejected."""
    if dst not in _STEPS:
        raise ProposalError("illegal", f"{dst} is not a pipeline step")
    p, closed = settle(p, cur)
    if closed:
        return _no(p, *closed)
    src, ai = p.state(p.latest.version), p.latest.mode is Mode.AI_ENABLED
    check_transition(src, dst)
    if src is P.VERIFYING and dst is P.READY_FOR_FINAL_CHECK and ai:
        return _no(p, Reason(Code.REVIEW_REQUIRED))
    if dst is P.AWAITING_REVIEW and not ai:
        return _no(p, Reason(Code.NOT_AI_MODE))
    return Decision(True, p._record(dst, cur.as_of))


def revise(p: Proposal, v: ProposalVersion, cur: Current) -> Decision:
    """Append the next version and supersede the current one. The scope (tenant,
    account, currency) is kept and the mode is the policy's. A closed or planned
    version is not revised: a plan is bound to the content the user accepted.
    Before activation at most `MAX_REVIEW_REVISIONS` revisions (AT074)."""
    old = p.latest
    if v.proposal_id != old.proposal_id or v.version != old.version + 1:
        raise ProposalError("version", "the next version of the same proposal")
    if v.scope != old.scope:
        raise ProposalError("scope", "a revision keeps tenant, account and currency")
    _check_mode(v, cur)
    p, closed = settle(p, cur)
    if closed:
        return _no(p, *closed)
    if (state := p.state(old.version)) in TERMINAL:
        return _no(p, Reason(Code.NOT_ACTIVE, state))
    if (done := p.execution(old.version)) is not ExecutionState.NONE:
        return _no(p, Reason(Code.ALREADY_DECIDED, done))
    early = sum(
        1 for n in range(1, old.version)
        if [e.state for e in p.events if e.version == n][-2] is not P.ACTIVE
    )  # fmt: skip
    if state is not P.ACTIVE and early >= MAX_REVIEW_REVISIONS:
        return _no(p, Reason(Code.REVISION_LIMIT))
    superseded = p._record(P.SUPERSEDED, cur.as_of, Code.SUPERSEDED)
    first = Event(v.version, P.CANDIDATE, ExecutionState.NONE, cur.as_of)
    return Decision(True, Proposal((*p.versions, v), (*superseded.events, first)))


def _open(
    p: Proposal, n: int, cur: Current, need: ProposalState
) -> tuple[Proposal, Decision | None]:
    """Decision prelude: the latest version, settled, in the needed state."""
    if n != p.latest.version:
        return p, _no(p, Reason(Code.SUPERSEDED, str(p.latest.version)))
    p, closed = settle(p, cur)
    if closed:
        return p, _no(p, *closed)
    if (state := p.state(n)) is not need:
        code = Code.NOT_READY if need is P.READY_FOR_FINAL_CHECK else Code.NOT_ACTIVE
        return p, _no(p, Reason(code, state))
    return p, None


# Admission of a version that passed its final check: refusal reasons, or ().
Admit = Callable[[ProposalVersion, Current], tuple[Reason, ...]]


def publish(p: Proposal, n: int, cur: Current, admit: Admit | None = None) -> Decision:
    """Final check, then `admit` (the commitment check); `active` only if both
    pass. An admission refusal leaves the version ready for re-evaluation."""
    p, refused = _open(p, n, cur, P.READY_FOR_FINAL_CHECK)
    if refused is not None:
        return refused
    if p.latest.binding.risk_outcome is Outcome.BLOCK:
        rejected = p._record(P.REJECTED, cur.as_of, Code.RISK_BLOCKED)
        return _no(rejected, Reason(Code.RISK_BLOCKED))
    if reasons := admit(p.latest, cur) if admit is not None else ():
        return _no(p, *reasons)
    return Decision(True, p._record(P.ACTIVE, cur.as_of))


def mark_planned(
    p: Proposal, n: int, cur: Current, siblings: Mapping[str, Proposal] | None = None
) -> Decision:
    """The user's accept: re-check the binding, then mark planned. `siblings`
    (keyed by proposal id) are the other members of its alternatives group, all
    in the same tenant, account and currency. If one was ever planned this is
    refused; otherwise the active ones are invalidated (`ALTERNATIVE_SELECTED`)."""
    given = dict(siblings or {})
    group, scope = p.latest.alternatives_group_id, p.latest.scope
    for key, s in given.items():
        if s.latest.proposal_id != key or key == p.latest.proposal_id:
            raise ProposalError("sibling_key", key)
        if group is None or s.latest.alternatives_group_id != group:
            raise ProposalError("sibling_group", key)
        if s.latest.scope != scope:
            raise ProposalError("group_scope", "a group stays in one account/currency")
    p, refused = _open(p, n, cur, P.ACTIVE)
    if refused is not None:
        return refused
    if (done := p.execution(n)) is not ExecutionState.NONE:
        return _no(p, Reason(Code.ALREADY_DECIDED, done))
    chosen = sorted(
        k for k, s in given.items()
        if any(e.execution is not ExecutionState.NONE for e in s.events)
    )  # fmt: skip
    if chosen:
        return _no(p, Reason(Code.ALTERNATIVE_SELECTED, ",".join(chosen)))
    closed: dict[str, Proposal] = {}
    for key, s in sorted(given.items()):
        s, _ = settle(s, cur)
        if s.state(s.latest.version) is P.ACTIVE:
            s = s._record(P.INVALIDATED, cur.as_of, Code.ALTERNATIVE_SELECTED)
        closed[key] = s
    planned = p._record(P.ACTIVE, cur.as_of, execution=ExecutionState.PLANNED)
    return Decision(True, planned, siblings=closed)


def dismiss(p: Proposal, n: int, cur: Current) -> Decision:
    """Dismiss an active version without rewriting history."""
    p, refused = _open(p, n, cur, P.ACTIVE)
    if refused is not None:
        return refused
    return Decision(True, p._record(P.DISMISSED, cur.as_of))
