"""Proposal versions, lifecycle and final checks (T026).

SYNTHETIC inputs only: made-up ids, prices and balances. Base case: buy 80 at an
ask of 50 with no costs (estimated cash effect 80 x 50 = 4000) under a deliberately
generous adopted policy (reserve 1, ratio limits 1, allocation 100000).
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.onboarding import load_catalogue
from qw_domain.policy import (
    AccountFacts,
    AdoptionConsent,
    OptionPermission,
    PolicyHistory,
    build_draft,
)
from qw_domain.proposals import (
    TRANSITIONS,
    Binding,
    Code,
    Current,
    ExecutionState,
    Mode,
    Observation,
    Proposal,
    ProposalError,
    ProposalState,
    ProposalVersion,
    Reason,
    cash_requirement,
    dismiss,
    mark_planned,
    policy_mode,
    publish,
    resolve,
    revise,
    risk_digest,
    step,
)
from qw_domain.risk import ProposedAction, RiskInputs, Side, evaluate
from qw_domain.risk_pauses import Pause, PauseBook, PauseScope, PauseTrigger, ScopeKind
from qw_domain.valuation import Mark, MarkKind

SPEC = Path(__file__).resolve().parents[3] / "docs/spec"
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
T = timedelta
TENANT = "tenant-synth"
IID, OTHER = InstrumentId(UUID(int=1)), InstrumentId(UUID(int=2))
S = ProposalState


def cad(x: str) -> Money:
    return Money.of(x, "CAD")


def lim(metric: str, value: str, unit: str = "ratio") -> dict[str, str]:
    return {
        "metric": metric, "value": value, "unit": unit, "denominator": "account_nav",
        "window": "per_trade", "account_id": "acct-1", "currency": "CAD",
    }  # fmt: skip


ANSWERS: dict[str, Any] = {
    "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": ["growth"],
    "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
    "ONB10": "weekly", "ONB11": ["stocks", "etfs"], "ONB12": ["long_term"],
    "ONB13": [{"account_id": "acct-1", "currency": "CAD", "amount": "100000"}],
    "ONB14": [
        lim("cash_reserve", "1", "amount"), lim("issuer_concentration", "1"),
        lim("sector_concentration", "1"), lim("planned_trade_loss", "100000", "amount"),
        lim("stress_loss", "1"), lim("loss_pause", "0.10"),
    ],
    "ONB16": "rules_only",
}  # fmt: skip


def adopt(**changes: Any) -> PolicyHistory:
    raw = (SPEC / "config/onboarding_questions.json").read_text()
    responses = load_catalogue(json.loads(raw)).validate({**ANSWERS, **changes})
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, False),)
    draft = build_draft(TENANT, responses, facts, as_of=date(2026, 10, 9))
    consent = AdoptionConsent("user-1", "stepup-1", AT, frozenset(), "")
    return PolicyHistory.new("pol-1", TENANT).propose(draft).adopt(1, consent)


H = adopt()
H_AI = adopt(ONB16="ai_enabled", ONB17=["provider-a"])
MARK = Mark(IID, Price("50"), "CAD", MarkKind.ASK, AT - T(seconds=1), "feed")
INPUTS = RiskInputs(
    as_of=AT, market_max_age=T(minutes=15), fx_max_age=T(hours=1),
    account_max_age=T(seconds=300), account_observed_at=AT - T(seconds=60),
    reconciled=True, available_cash=cad("5000"), allocation_used=cad("0"),
    marks={IID: MARK}, fx={}, denominators={"account_nav": cad("20000")},
    held_units={IID: Quantity(0)}, position_values={OTHER: cad("14000")},
    issuer_exposure={"issuer-x": cad("500")}, sector_exposure={"sector-y": cad("2000")},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05")},
)  # fmt: skip
BUY = ProposedAction(
    tenant_id=TENANT, account_id="acct-1", currency="CAD", sleeve_id=None,
    strategy_id="strat-1", denominator="account_nav", instrument=IID,
    issuer_id="issuer-x", sector_id="sector-y", horizon="long_term", side=Side.BUY,
    quantity=PositiveQuantity(80), lot=PositiveQuantity(1), stop=Price(48),
    fixed_cost=cad("0"), unit_cost=cad("0"), leveraged_or_inverse=False,
)  # fmt: skip
NO_PAUSES = PauseBook(TENANT)
REVS = {"acct-1": 7}
OBS = (
    Observation("mark:1", MARK.observed_at, T(minutes=15)),
    Observation("fx:USD", AT - T(minutes=50), T(hours=1)),
    Observation("account:acct-1", AT - T(seconds=60), T(seconds=300)),
)


def cur(at: datetime = AT, history: PolicyHistory | None = H, **kw: Any) -> Current:
    base: dict[str, Any] = {
        "inputs": INPUTS, "account_revisions": REVS, "pauses": NO_PAUSES,
    }  # fmt: skip
    return Current(as_of=at, history=history, **{**base, **kw})


def ver(
    pid: str = "prop-a", action: ProposedAction = BUY, n: int = 1,
    history: PolicyHistory = H, **kw: Any,
) -> ProposalVersion:  # fmt: skip
    ev = evaluate(history, action, INPUTS, NO_PAUSES)
    fields: dict[str, Any] = {
        "trigger_at": AT - T(seconds=5), "received_at": AT - T(seconds=4),
        "created_at": AT, "expires_at": AT + T(minutes=30),
        "mode": policy_mode(history), "alternatives_group_id": None,
    }  # fmt: skip
    fields.update(kw)
    req = cash_requirement(action, MARK.price)
    return ProposalVersion(pid, n, action, Binding.of(ev, REVS, OBS), req, **fields)


def ready(v: ProposalVersion, history: PolicyHistory = H) -> Proposal:
    p = Proposal.new(v, cur(history=history))
    path = [S.VERIFYING, S.READY_FOR_FINAL_CHECK]
    if v.mode is Mode.AI_ENABLED:
        path.insert(1, S.AWAITING_REVIEW)
    for dst in path:
        d = step(p, dst, cur(history=history))
        assert d.accepted, d.reasons
        p = d.proposal
    return p


def active(v: ProposalVersion) -> Proposal:
    d = publish(ready(v), v.version, cur())
    assert d.accepted, d.reasons
    return d.proposal


def codes(reasons: Any) -> set[Code]:
    return {r.code for r in reasons}


def test_states_match_the_contract_and_the_table_is_closed() -> None:
    schema = json.loads((SPEC / "contracts/schemas/proposal.schema.json").read_text())
    props = schema["properties"]
    assert {s.value for s in S} == set(props["state"]["enum"])
    assert {e.value for e in ExecutionState} == set(props["execution_state"]["enum"])
    terminal = {S.RESEARCH_ONLY, S.REJECTED, S.EXPIRED, S.INVALIDATED, S.DISMISSED}
    assert {s for s, dst in TRANSITIONS.items() if not dst} == terminal | {S.SUPERSEDED}
    assert TRANSITIONS[S.ACTIVE] == {
        S.DISMISSED,
        S.SUPERSEDED,
        S.INVALIDATED,
        S.EXPIRED,
    }
    for dst in TRANSITIONS.values():  # every live state can expire or be invalidated
        assert not dst or {S.EXPIRED, S.INVALIDATED} <= dst
    assert S.ACTIVE not in TRANSITIONS[S.AWAITING_REVIEW]  # final check is mandatory


def test_version_hash_binding_and_time_order() -> None:
    v = ver()
    assert v.content_hash == ver().content_hash and len(v.content_hash) == 64
    other = ver(action=replace(BUY, quantity=PositiveQuantity(81)))
    assert v.content_hash != other.content_hash
    assert v.content_hash != ver(expires_at=AT + T(minutes=31)).content_hash
    assert v.requirement == cad("4000")  # 80 x 50, no costs
    assert v.binding.risk_digest == risk_digest(evaluate(H, BUY, INPUTS, NO_PAUSES))
    adopted = H.adopted
    assert adopted is not None and v.binding.policy_hash == adopted[0].content_hash
    assert v.binding.account_revisions == (("acct-1", 7),)
    with pytest.raises(ProposalError, match="time_order"):  # F-15
        ver(received_at=AT - T(seconds=6))
    with pytest.raises(ProposalError, match="time_order"):
        ver(expires_at=AT)
    with pytest.raises(ValueError, match="naive"):
        ver(created_at=AT.replace(tzinfo=None))
    with pytest.raises(ProposalError, match="requirement"):  # sales create no cash
        replace(ver(action=replace(BUY, side=Side.SELL)), requirement=cad("-3000"))


def test_mode_comes_from_the_adopted_policy() -> None:
    assert policy_mode(H) is Mode.RULES_ONLY and policy_mode(H_AI) is Mode.AI_ENABLED
    with pytest.raises(ProposalError, match="mode"):
        Proposal.new(ver(mode=Mode.AI_ENABLED), cur())
    with pytest.raises(ProposalError, match="policy_unbound"):
        policy_mode(None)


def test_rules_mode_publishes_and_plans_once() -> None:
    v = ver()
    seen: list[ProposalVersion] = []

    def refuse(x: ProposalVersion, c: Current) -> tuple[Reason, ...]:
        seen.append(x)
        return (Reason(Code.CAPITAL_CONFLICT, "stub"),)

    held = publish(ready(v), 1, cur(), refuse)
    assert not held.accepted and seen == [v]
    assert held.proposal.state(1) is S.READY_FOR_FINAL_CHECK  # re-evaluate later
    p = active(v)
    d = mark_planned(p, 1, cur(AT + T(minutes=1)))
    assert d.accepted and d.proposal.execution(1) is ExecutionState.PLANNED
    assert [e.state for e in d.proposal.events] == [
        S.CANDIDATE, S.VERIFYING, S.READY_FOR_FINAL_CHECK, S.ACTIVE, S.ACTIVE
    ]  # fmt: skip
    again = mark_planned(d.proposal, 1, cur(AT + T(minutes=2)))
    assert not again.accepted and codes(again.reasons) == {Code.ALREADY_DECIDED}


def test_ai_mode_requires_review_and_rules_mode_has_none() -> None:
    v = ver(history=H_AI)
    c = cur(history=H_AI)
    p = step(Proposal.new(v, c), S.VERIFYING, c).proposal
    skip = step(p, S.READY_FOR_FINAL_CHECK, c)
    assert not skip.accepted and codes(skip.reasons) == {Code.REVIEW_REQUIRED}
    assert step(p, S.AWAITING_REVIEW, c).accepted
    r = ver()
    q = step(Proposal.new(r, cur()), S.VERIFYING, cur()).proposal
    assert codes(step(q, S.AWAITING_REVIEW, cur()).reasons) == {Code.NOT_AI_MODE}
    with pytest.raises(ProposalError, match="illegal"):
        step(q, S.ACTIVE, cur())  # publication only through publish()


PAUSE = Pause(
    "pause-1", PauseScope(ScopeKind.ACCOUNT, "acct-1"), PauseTrigger.USER, AT,
    "user-flag-1", False,
)  # fmt: skip
CHANGES: list[tuple[dict[str, Any], Reason]] = [
    ({"history": adopt(ONB13=[{**ANSWERS["ONB13"][0], "amount": "90000"}])},
     Reason(Code.POLICY_CHANGED)),
    ({"history": None}, Reason(Code.POLICY_CHANGED)),
    ({"account_revisions": {"acct-1": 8}}, Reason(Code.ACCOUNT_CHANGED, "acct-1")),
    ({"account_revisions": {}}, Reason(Code.ACCOUNT_CHANGED, "acct-1")),
    ({"at": AT + T(seconds=241)}, Reason(Code.INPUT_STALE, "account:acct-1")),
    ({"at": AT + T(minutes=11)}, Reason(Code.INPUT_STALE, "fx:USD")),  # 61 min
    ({"at": AT + T(minutes=15)}, Reason(Code.INPUT_STALE, "mark:1")),  # 15 min 1 s
    ({"pauses": NO_PAUSES.pause(PAUSE)}, Reason(Code.RISK_PAUSED, "account:acct-1")),
    ({"inputs": replace(INPUTS, reconciled=False)}, Reason(Code.RISK_CHANGED, "block")),
    ({"pauses": PauseBook("tenant-other")}, Reason(Code.TENANT_MISMATCH)),
]  # fmt: skip


@pytest.mark.parametrize(("change", "reason"), CHANGES)
def test_final_check_invalidates_and_never_plans(
    change: dict[str, Any], reason: Reason
) -> None:
    v = ver()
    p = active(v)
    d = mark_planned(p, 1, cur(**change))
    assert not d.accepted and reason in d.reasons
    assert d.proposal.state(1) is S.INVALIDATED
    assert d.proposal.execution(1) is ExecutionState.NONE
    assert d.proposal.versions[0].binding == v.binding  # F-05: not rewritten
    fresh = publish(ready(v), 1, cur(**change))
    assert not fresh.accepted and fresh.proposal.state(1) is S.INVALIDATED


def test_late_review_and_late_decisions_cannot_activate() -> None:
    v, c = ver(history=H_AI), cur(history=H_AI)
    p = step(Proposal.new(v, c), S.VERIFYING, c).proposal
    p = step(p, S.AWAITING_REVIEW, c).proposal
    late = cur(AT + T(minutes=30), history=H_AI)  # == expires_at: expired
    assert resolve(p, late) is S.EXPIRED and p.state(1) is S.AWAITING_REVIEW
    d = step(p, S.READY_FOR_FINAL_CHECK, late)
    assert not d.accepted and codes(d.reasons) == {Code.EXPIRED}
    assert d.proposal.state(1) is S.EXPIRED
    again = publish(d.proposal, 1, late)
    assert not again.accepted and again.proposal.state(1) is S.EXPIRED
    q = active(ver())
    gone = mark_planned(q, 1, cur(AT + T(minutes=30)))
    assert not gone.accepted and Code.EXPIRED in codes(gone.reasons)
    with pytest.raises(ProposalError, match="time_regressed"):
        mark_planned(q, 1, cur(AT - T(seconds=1)))


def test_revision_supersedes_keeps_scope_and_is_bounded() -> None:
    v1, c = ver(history=H_AI), cur(history=H_AI)
    p = step(Proposal.new(v1, c), S.VERIFYING, c).proposal
    p = step(p, S.AWAITING_REVIEW, c).proposal
    smaller = replace(BUY, quantity=PositiveQuantity(70))
    v2 = ver(n=2, history=H_AI, action=smaller)
    with pytest.raises(ProposalError, match="mode"):  # no downgrade past review
        revise(p, replace(v2, mode=Mode.RULES_ONLY), c)
    with pytest.raises(ProposalError, match="scope"):
        revise(p, ver(n=2, history=H_AI, action=replace(BUY, account_id="acct-9")), c)
    with pytest.raises(ProposalError, match="version"):
        revise(p, replace(v2, version=5), c)
    p = revise(p, v2, c).proposal
    assert p.state(1) is S.SUPERSEDED and p.state(2) is S.CANDIDATE
    assert p.versions[0] == v1  # history preserved
    stale = mark_planned(p, 1, c)
    assert not stale.accepted and codes(stale.reasons) == {Code.SUPERSEDED}
    third = revise(p, ver(n=3, history=H_AI), c)  # AT074: at most one
    assert not third.accepted and codes(third.reasons) == {Code.REVISION_LIMIT}


def test_revision_refuses_closed_and_planned_versions() -> None:
    p = Proposal.new(ver(), cur())
    late = revise(p, ver(n=2), cur(AT + T(hours=2)))
    assert not late.accepted and codes(late.reasons) == {Code.EXPIRED}
    planned = mark_planned(active(ver()), 1, cur()).proposal
    d = revise(planned, ver(n=2), cur())
    assert not d.accepted and codes(d.reasons) == {Code.ALREADY_DECIDED}
    live = revise(active(ver()), ver(n=2), cur())  # active, unplanned: allowed
    assert live.accepted and live.proposal.state(1) is S.SUPERSEDED


def test_alternatives_close_siblings_and_refuse_a_second_choice() -> None:
    a = active(ver("prop-a", alternatives_group_id="grp-1"))
    b = active(ver("prop-b", alternatives_group_id="grp-1"))
    d = mark_planned(a, 1, cur(), {"prop-b": b})
    assert d.accepted and d.siblings["prop-b"].state(1) is S.INVALIDATED
    assert d.siblings["prop-b"].events[-1].code is Code.ALTERNATIVE_SELECTED
    c = active(ver("prop-c", alternatives_group_id="grp-1"))  # late sibling
    late = mark_planned(c, 1, cur(), {"prop-a": d.proposal})
    assert not late.accepted and codes(late.reasons) == {Code.ALTERNATIVE_SELECTED}
    assert d.proposal.execution(1) is ExecutionState.PLANNED  # never undone
    with pytest.raises(ProposalError, match="sibling_key"):
        mark_planned(a, 1, cur(), {"prop-x": b})
    with pytest.raises(ProposalError, match="sibling_group"):
        mark_planned(a, 1, cur(), {"prop-d": active(ver("prop-d"))})
    away = replace(BUY, account_id="acct-2")
    e = Proposal.new(ver("prop-e", action=away, alternatives_group_id="grp-1"), cur())
    with pytest.raises(ProposalError, match="group_scope"):
        mark_planned(a, 1, cur(), {"prop-e": e})


def test_dismiss_is_final() -> None:
    d = dismiss(active(ver()), 1, cur())
    assert d.accepted and d.proposal.state(1) is S.DISMISSED
    after = mark_planned(d.proposal, 1, cur())
    assert not after.accepted and codes(after.reasons) == {Code.NOT_ACTIVE}


OPS = st.lists(
    st.sampled_from([*S, "publish", "plan", "dismiss", "revise", "late", "policy"]),
    max_size=12,
)
OTHER_POLICY = adopt(ONB13=[{**ANSWERS["ONB13"][0], "amount": "90000"}])


@settings(max_examples=80, deadline=None, derandomize=True, database=None)
@given(OPS, st.sampled_from([H, H_AI]))
def test_no_recorded_transition_is_outside_the_table(
    ops: list[Any], history: PolicyHistory
) -> None:
    v = ver(history=history)
    at = AT
    p = Proposal.new(v, cur(history=history))
    for op in ops:
        at += T(seconds=1)
        c = cur(at, history=history)
        n = p.latest.version
        if op in set(S) or op in ("late", "policy"):
            dst = S(op) if op in set(S) else S.VERIFYING
            if op == "late":
                at += T(hours=1)
            c = cur(at, history=OTHER_POLICY if op == "policy" else history)
            try:
                p = step(p, dst, c).proposal
            except ProposalError as exc:  # only an illegal move may raise
                assert exc.code == "illegal"
                assert dst not in TRANSITIONS[p.state(n)] or dst in {
                    S.CANDIDATE, S.ACTIVE, S.DISMISSED, S.EXPIRED, S.INVALIDATED,
                    S.SUPERSEDED,
                }  # fmt: skip
        elif op == "publish":
            p = publish(p, n, c).proposal
        elif op == "plan":
            p = mark_planned(p, n, c).proposal
        elif op == "dismiss":
            p = dismiss(p, n, c).proposal
        else:
            p = revise(p, ver(n=n + 1, history=history), c).proposal
    for k in range(1, len(p.versions) + 1):
        states = [e.state for e in p.events if e.version == k]
        assert states[0] is S.CANDIDATE
        for src, dst in pairwise(states):
            assert dst in TRANSITIONS[src] or (src is dst is S.ACTIVE)
    assert [e.at for e in p.events] == sorted(e.at for e in p.events)
