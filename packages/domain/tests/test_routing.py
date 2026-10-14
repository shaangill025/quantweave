"""AI-mode routing of rule, research and chat candidates; mode; deadlines (T040).

Everything is SYNTHETIC: tenant, policy answers, feeds, closes, passages, routes,
rates and the models. Models are deterministic scripted fakes behind the T037
gateway's `ProviderAdapter` interface; nothing leaves the process. Oracles are
hand-computed: closes 100, 105, 110 give momentum 110 / 100 - 1 = 0.1 and a mean
of (100 + 105 + 110) / 3 = 105.
"""

import ast
import inspect
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ingest_rights_helper import KEEP, REGION, TENANT
from qw_domain import evaluator as ev
from qw_domain import researcher as rs
from qw_domain import routing as rt
from qw_domain.budget import BudgetBook, BudgetCaps
from qw_domain.decimals import Quantity, Ratio
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.evidence import EvidenceStore, Reading, SourceKind
from qw_domain.filings import Period
from qw_domain.gateway import GatewayContext, Qualification, Route
from qw_domain.ingest import IngestRights
from qw_domain.onboarding import load_catalogue
from qw_domain.policy import (
    AccountFacts,
    AdoptionConsent,
    OptionPermission,
    PolicyHistory,
    build_draft,
)
from qw_domain.proposals import (
    Binding,
    Code,
    Current,
    Mode,
    ProposalVersion,
    cash_requirement,
    policy_mode,
    publish,
)
from qw_domain.proposals import ProposalState as S
from qw_domain.rights import UseScope
from qw_domain.risk import evaluate as risk
from qw_domain.risk_pauses import PauseBook
from qw_domain.swing_momentum import trend_manifest
from test_evaluator import EVAL_ROUTE, LATER, blind, cfg, compare, researched
from test_proposals import ANSWERS, BUY, INPUTS, MARK, OBS, OTHER, REVS, SPEC
from test_researcher import AT, E1, REG, Fake, mandate, store_with

T = timedelta
TREND = trend_manifest("feed-a")
CTX = GatewayContext(REG, UseScope.PERSONAL, REGION, None, T(hours=1))
PAUSES = PauseBook(TENANT)
TEN_MIN = T(minutes=10)
CATALOGUE = load_catalogue(json.loads(
    (SPEC / "config/onboarding_questions.json").read_text()))  # fmt: skip


def history(mode: str = "rules_only", h: PolicyHistory | None = None) -> PolicyHistory:
    """Adopt a SYNTHETIC policy with `mode`; on `h`, as its next version."""
    extra = {"ONB16": mode} | ({} if mode == "rules_only" else {"ONB17": ["prov-a"]})
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, False),)
    draft = build_draft(TENANT, CATALOGUE.validate({**ANSWERS, **extra}), facts,
                        as_of=date(2026, 10, 9))  # fmt: skip
    h = (h or PolicyHistory.new("pol-1", TENANT)).propose(draft)
    n = h.versions[-1].version
    return h.adopt(n, AdoptionConsent("user-1", f"stepup-{n}", AT, frozenset(), ""))


H, H_AI = history(), history("ai_enabled")
INS = replace(INPUTS, marks={E1: replace(MARK, instrument_id=E1)},
              held_units={E1: Quantity(0)},
              stress_shocks={E1: Ratio("-0.20"), OTHER: Ratio("-0.05")})  # fmt: skip


def cur(at: datetime = AT, h: PolicyHistory = H) -> Current:
    return Current(at, h, INS, REVS, PAUSES)


def ver(pid: str, strategy: str, h: PolicyHistory = H, **kw: Any) -> ProposalVersion:
    a = replace(BUY, tenant_id=TENANT, instrument=E1, strategy_id=strategy)
    times: dict[str, Any] = {"trigger_at": AT - T(seconds=5), "created_at": AT,
        "received_at": AT - T(seconds=4), "expires_at": AT + T(minutes=30)}  # fmt: skip
    return ProposalVersion(pid, 1, a, Binding.of(risk(h, a, INS, PAUSES), REVS, OBS),
        cash_requirement(a, MARK.price), **(times | kw), mode=policy_mode(h),
        alternatives_group_id=None)  # fmt: skip


def closes(s: EvidenceStore, social: bool = False) -> EvidenceStore:
    for k, value in enumerate(("100", "105", "110")):
        at, day = AT - T(minutes=50 - 10 * k), date(2026, 10, 6 + k)
        text = f"SYNTHETIC bar: close {value} USD on {day}"
        kind = SourceKind.SOCIAL if social and k == 0 else SourceKind.PRIMARY
        r = IngestRights(REG, TENANT, "feed-a", at, UseScope.PERSONAL, REGION, KEEP)
        s.add_source(r, f"c{k}", kind, text, at)
        reading = Reading(E1, "close", Period(day, day), "USD", Decimal(value))
        s.add_passage(TENANT, f"p-c{k}", f"c{k}", 0, text, reading, at)
    return s


def rule_mandate(*extra: str) -> rs.Mandate:
    return replace(mandate(), mandate_id="rule-trend-1", workflow=TREND,
                   needs=(rs.Need(E1, "close"),),
                   passage_ids=("p-c0", "p-c1", "p-c2", *extra),
                   tools=frozenset({rs.Tool.READ}))  # fmt: skip


FACTS = tuple(rt.Fact(f"f-c{k}", f"p-c{k}", Decimal(v))
              for k, v in enumerate(("100", "105", "110")))  # fmt: skip
MOM = rt.Calc("c-mom", "ratio_minus_one/1", ("p-c2", "p-c0"), Decimal("0.1"))
MEAN = rt.Calc("c-mean", "mean/1", ("p-c0", "p-c1", "p-c2"), Decimal("105"))


def rule_candidate(calcs: tuple[rt.Calc, ...] = (MOM, MEAN), window: T = TEN_MIN,
                   m: rs.Mandate | None = None) -> rt.Candidate:  # fmt: skip
    m = m or rule_mandate()
    t = rt.rule_thesis(m, "SYNTHETIC: close above trend, momentum positive", FACTS,
                       calcs, ("close below trend mean",))  # fmt: skip
    return rt.Candidate(rt.Origin.RULES, t, m, rt.rule_record(m), window,
                        facts=FACTS, calcs=calcs)  # fmt: skip


def ai_candidate(origin: rt.Origin = rt.Origin.AI_RESEARCH) -> rt.Candidate:
    r = researched()
    assert isinstance(r.outcome, rs.Thesis)
    return rt.Candidate(origin, r.outcome, mandate(), r.record, TEN_MIN)


def review(c: rt.Candidate, *replies: dict[str, object] | str,
           chain: tuple[Route, ...] = (EVAL_ROUTE,)) -> ev.Evaluation:  # fmt: skip
    ticks = iter(LATER + T(seconds=i) for i in range(1000))
    eng = rs.Engine(
        chain, CTX, Fake(*replies), lambda: next(ticks), 1000, T(seconds=20)
    )
    book = BudgetBook.open(TENANT, AT, BudgetCaps(Usd("100"), Usd("20"), "own"))
    return ev.evaluate(c.thesis, c.record, c.mandate, closes(store_with()), eng, cfg(),
                       book, LATER)  # fmt: skip


def approve(**results: str) -> tuple[dict[str, object], dict[str, object]]:
    ids = ["f-c0", "f-c1", "f-c2", "c-mom", "c-mean"]
    vs = [{"claim_id": k, "result": results.get(k, "supported"), "reason": "SYNTH"}
          for k in ids]  # fmt: skip
    return blind(), compare(verdicts=vs)


def submit(c: rt.Candidate, h: PolicyHistory, store: EvidenceStore | None = None
           ) -> rt.Routed:  # fmt: skip
    v = ver(c.mandate.mandate_id, c.mandate.workflow.strategy_id, h)
    return rt.submit(v, c, store or closes(store_with()), CTX, cur(h=h))


def test_rules_mode_rule_candidate_is_rules_verified_never_ai_reviewed() -> None:
    r = submit(rule_candidate(), H)
    assert r.reasons == () and r.review is None
    assert [e.state for e in r.proposal.events] == [
        S.CANDIDATE, S.VERIFYING, S.READY_FOR_FINAL_CHECK]  # fmt: skip
    assert rt.label(r) is rt.Label.RULES_VERIFIED
    d = publish(r.proposal, 1, cur(AT + T(seconds=10)))
    assert d.accepted
    assert rt.label(replace(r, proposal=d.proposal)) is rt.Label.RULES_VERIFIED


def test_ai_mode_routes_rule_candidates_through_the_evaluator() -> None:
    c = rule_candidate()
    r = submit(c, H_AI)
    assert r.proposal.state(1) is S.AWAITING_REVIEW
    assert rt.label(r) is rt.Label.REVIEW_PENDING
    early = publish(r.proposal, 1, cur(AT + T(seconds=1), H_AI))
    assert not early.accepted and early.reasons[0].code is Code.NOT_READY
    e = review(c, *approve())
    assert e.isolation == "rules_origin"  # no proposer model to be distinct from
    rv = e.outcome
    assert isinstance(rv, ev.Review) and rv.decision is ev.Decision.APPROVE
    assert rv.researcher_run_id == "rules:rule-trend-1" and rv.material == ()
    assert rv.model_family_distinct is False
    done = rt.apply(r, e, cur(LATER + T(seconds=10), H_AI))
    assert done.proposal.state(1) is S.READY_FOR_FINAL_CHECK
    assert rt.label(done) is rt.Label.AI_REVIEWED
    assert publish(done.proposal, 1, cur(LATER + T(seconds=11), H_AI)).accepted
    # labels come from the bound review, never from the state alone
    assert rt.label(replace(done, review=None)) is rt.Label.REVIEW_PENDING
    contested = review(c, *approve(**{"c-mom": "contested"})).outcome
    assert isinstance(contested, ev.Review)  # a disputed calculation is material
    assert contested.decision is ev.Decision.REVISE
    assert "verdict:c-mom:contested" in contested.material


@pytest.mark.parametrize(("calc", "why"), [
    (replace(MOM, value=Decimal("0.2")), "calc_mismatch"),
    (replace(MOM, inputs=("p-c0", "p-c2")), "calc_mismatch"),  # order matters
    (replace(MOM, formula="ratio_minus_one/2"), "formula_unknown"),
    (replace(MOM, inputs=("p-c2",)), "calc_inputs"),
    (replace(MOM, inputs=("p-c2", "p-gone")), "not_read"),
    (replace(MOM, inputs=("p-c2", "p-fact")), "input_mismatch"),  # revenue 100
    (replace(MOM, inputs=("p-c2", "p-c2"), value=Decimal(0)), "repeated_input"),
])  # fmt: skip
def test_calculation_claims_are_recomputed_before_review(
    calc: rt.Calc, why: str
) -> None:
    c = rule_candidate((calc,), m=rule_mandate("p-fact"))
    for h in (H, H_AI):  # deterministic checks come first in both modes
        r = submit(c, h)
        assert r.proposal.state(1) is S.REJECTED
        assert r.reasons[0].code is Code.MATERIAL_CLAIM_UNSUPPORTED
        assert r.reasons[0].detail.split(",") == [f"c-mom:{why}"]
        assert r.proposal.events[-1].code is Code.MATERIAL_CLAIM_UNSUPPORTED


def test_rule_origin_cannot_wrap_a_model_thesis() -> None:
    ai = ai_candidate()
    m = replace(mandate(), workflow=TREND)  # an AI thesis relabelled as a rule
    with pytest.raises(rt.RoutingError, match="rule_thesis"):
        rt.Candidate(rt.Origin.RULES, ai.thesis, m, rt.rule_record(m), TEN_MIN)
    c = rule_candidate()
    with pytest.raises(rt.RoutingError, match="rule_thesis"):  # a dropped claim
        replace(c, thesis=replace(c.thesis, claims=c.thesis.claims[:-1]))
    with pytest.raises(rt.RoutingError, match="rule_thesis"):  # a model run
        replace(c, record=ai.record)
    eng = rs.Engine((EVAL_ROUTE,), CTX, Fake(), lambda: LATER, 1000, T(seconds=20))
    book = BudgetBook.open(TENANT, AT, BudgetCaps(Usd("100"), Usd("20"), "own"))
    with pytest.raises(ev.EvaluatorError, match="rules_thesis"):
        ev.evaluate(ai.thesis, rt.rule_record(m), m, closes(store_with()), eng, cfg(),
                    book, LATER)  # fmt: skip


def test_social_inputs_block_rule_claims() -> None:
    r = submit(rule_candidate(), H, closes(store_with(), social=True))
    assert r.proposal.state(1) is S.REJECTED  # a social close is no evidence
    assert set(r.reasons[0].detail.split(",")) == {
        "f-c0:lead", "c-mom:social_input", "c-mean:social_input"}  # fmt: skip


def test_ai_and_chat_origin_need_ai_mode_and_review() -> None:
    for origin in (rt.Origin.AI_RESEARCH, rt.Origin.CHAT):
        c = ai_candidate(origin)
        r = submit(c, H)
        assert r.proposal.state(1) is S.RESEARCH_ONLY
        assert r.reasons[0].code is Code.NOT_AI_MODE
        assert rt.label(r) is rt.Label.NOT_ACTIONABLE
        assert submit(c, H_AI).proposal.state(1) is S.AWAITING_REVIEW
    with pytest.raises(rt.RoutingError, match="origin"):
        replace(ai_candidate(), origin=rt.Origin.RULES)
    with pytest.raises(rt.RoutingError, match="origin"):
        replace(rule_candidate(), origin=rt.Origin.CHAT)
    with pytest.raises(rt.RoutingError, match="binding"):
        rt.submit(ver("other-id", TREND.strategy_id), rule_candidate(),
                  closes(store_with()), CTX, cur())  # fmt: skip


def test_outage_keeps_mode_and_pending_and_the_miss_stays_visible() -> None:
    c = rule_candidate(window=T(minutes=2))
    r = submit(c, H_AI)
    deadline = rt.deadline(r.proposal.latest, c)
    assert deadline == AT - T(seconds=5) + T(minutes=2)  # from the original trigger
    down = review(c, chain=(replace(EVAL_ROUTE,
                  qualification=Qualification.UNQUALIFIED),))  # fmt: skip
    assert isinstance(down.outcome, rs.Abstention)
    out = rt.apply(r, down, cur(LATER + T(seconds=5), H_AI))
    assert out.proposal.state(1) is S.AWAITING_REVIEW
    last = out.proposal.events[-1]
    assert (last.code, last.detail) == (Code.REVIEW_UNAVAILABLE, "gateway_refused")
    assert policy_mode(H_AI) is Mode.AI_ENABLED  # no silent switch to rules
    assert rt.sweep(out, cur(deadline - T(seconds=1), H_AI)).proposal == out.proposal
    missed = rt.sweep(out, cur(deadline, H_AI))
    assert missed.proposal.state(1) is S.EXPIRED
    assert missed.proposal.events[-1].code is Code.REVIEW_DEADLINE_MISSED
    assert rt.label(missed) is rt.Label.REVIEW_MISSED
    counts = rt.tally([missed, submit(rule_candidate(), H)])
    assert counts[rt.Label.REVIEW_MISSED] == 1 and sum(counts.values()) == 2
    # a complete review that arrives after the deadline is a miss, not a pass
    short = rule_candidate(window=T(seconds=30))  # deadline AT + 25 s
    e = review(short, *approve())  # completed at about AT + 61 s
    assert isinstance(e.outcome, ev.Review)
    with pytest.raises(rt.RoutingError, match="review_time"):
        rt.apply(submit(short, H_AI), e, cur(AT + T(seconds=20), H_AI))
    late = rt.apply(submit(short, H_AI), e, cur(LATER + T(seconds=30), H_AI))
    assert late.proposal.state(1) is S.EXPIRED
    assert rt.label(late) is rt.Label.REVIEW_MISSED


def test_intraday_deadline_is_at_most_sixty_seconds_from_trigger() -> None:
    c, v = rule_candidate(), ver("rule-trend-1", TREND.strategy_id, H_AI)
    intraday = replace(v, action=replace(v.action, horizon="intraday"))
    assert rt.deadline(intraday, c) == v.trigger_at + T(seconds=60)
    soon = replace(intraday, expires_at=v.trigger_at + T(seconds=30))
    assert rt.deadline(soon, c) == v.trigger_at + T(seconds=30)
    assert rt.deadline(v, c) == v.trigger_at + TEN_MIN


def test_mode_change_is_explicit_and_invalidates_in_flight_candidates() -> None:
    h2 = history("ai_enabled", H)
    assert [(x.policy_version, x.previous, x.mode, x.step_up_receipt_id)
            for x in rt.mode_changes(h2)] == [
        (1, None, Mode.RULES_ONLY, "stepup-1"),
        (2, Mode.RULES_ONLY, Mode.AI_ENABLED, "stepup-2")]  # fmt: skip
    assert "mode" not in inspect.signature(rt.submit).parameters
    ready = submit(rule_candidate(), H)  # rules-verified under the old policy
    d = publish(ready.proposal, 1, cur(AT + T(seconds=10), h2))
    assert not d.accepted and d.reasons[0].code is Code.POLICY_CHANGED
    assert d.proposal.state(1) is S.INVALIDATED  # never relabelled as AI-reviewed
    pending = submit(rule_candidate(), H_AI)
    back = history("rules_only", H_AI)
    assert rt.mode_changes(back)[-1].mode is Mode.RULES_ONLY
    out = rt.apply(pending, review(rule_candidate(), *approve()),
                   cur(LATER + T(seconds=10), back))  # fmt: skip
    assert out.proposal.state(1) is S.INVALIDATED  # not moved to the rules path
    assert out.reasons[0].code is Code.POLICY_CHANGED
    with pytest.raises(Exception, match="mode"):  # the caller cannot pick a mode
        rt.submit(replace(ver("rule-trend-1", TREND.strategy_id), mode=Mode.AI_ENABLED),
                  rule_candidate(), closes(store_with()), CTX, cur())  # fmt: skip


def test_review_binds_the_version_and_revision_keeps_the_trigger() -> None:
    c = rule_candidate()
    r = submit(c, H_AI)
    e = review(c, *approve(**{"c-mom": "contested"}))
    assert isinstance(e.outcome, ev.Review)
    forged = replace(e, outcome=replace(e.outcome, proposal_hash="0" * 64))
    at = cur(LATER + T(seconds=10), H_AI)
    with pytest.raises(rt.RoutingError, match="review_binding"):
        rt.apply(r, forged, at)
    asked = rt.apply(r, e, at)
    assert asked.proposal.events[-1].code is Code.REVIEW_REVISE
    assert asked.proposal.state(1) is S.AWAITING_REVIEW
    v2 = replace(r.proposal.latest, version=2, created_at=LATER + T(seconds=20))
    now, store = cur(LATER + T(seconds=20), H_AI), closes(store_with())
    with pytest.raises(rt.RoutingError, match="trigger_reset"):
        rt.resubmit(asked, replace(v2, trigger_at=AT - T(seconds=4)), c, store, CTX,
                    now)  # fmt: skip
    with pytest.raises(rt.RoutingError, match="not_revise"):
        rt.resubmit(r, v2, c, store, CTX, now)
    again = rt.resubmit(asked, v2, c, store, CTX, now)
    assert again.proposal.state(2) is S.AWAITING_REVIEW
    assert rt.deadline(again.proposal.latest, c) == rt.deadline(r.proposal.latest, c)


def assert_no_order_path(module: str) -> set[str]:
    """Every qw_domain module `module` reaches; none mutates commitments or orders."""
    src, seen, todo = Path(rt.__file__).parent, set(), [module]
    while todo:
        tree = ast.parse((src / f"{todo.pop().split('.')[-1]}.py").read_text())
        for n in ast.walk(tree):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            if isinstance(n, ast.ImportFrom) and n.module:
                pkg = n.module == "qw_domain"  # from qw_domain import x as y
                names += [f"qw_domain.{a.name}" for a in n.names] if pkg else [n.module]
            for mod in names:
                if mod.startswith("qw_domain.") and mod not in seen:
                    seen.add(mod)
                    todo.append(mod)
    banned = {"commitments", "reconciliation"}  # journal: read-only, via valuation
    assert not {m.split(".")[-1] for m in seen} & banned
    assert not [m for m in seen if "broker" in m or "order" in m]
    return seen


def test_no_broker_or_order_path() -> None:
    seen = assert_no_order_path("qw_domain.routing")
    assert {"qw_domain.evaluator", "qw_domain.proposals"} <= seen
