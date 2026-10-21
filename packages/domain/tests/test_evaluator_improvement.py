"""Evaluator improvement path (T048 increment 1). Everything is SYNTHETIC: the
control cases, their labels, the recorded evaluator-model replies (replayed, never
live), routes, rates, approvals. Expected metrics are hand-computed from the case
table below (rate card USD3 / USD15 per million tokens, 100 in / 50 out per pass
= USD0.00105; blind replies take 2 s, compare replies 3 s).
"""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from ingest_rights_helper import KEEP, REGION, TENANT
from qw_domain import evaluator as ev
from qw_domain import evaluator_improvement as ei
from qw_domain import improvement as im
from qw_domain import researcher as rs
from qw_domain import trial_ledger as tl
from qw_domain.budget import BudgetBook, BudgetCaps
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.evidence import ClaimKind, EvidenceStore, Reading, SourceKind
from qw_domain.feedback import Purpose
from qw_domain.gateway import GatewayContext
from qw_domain.ingest import IngestRights
from qw_domain.rights import UseScope
from qw_domain.strategy_registry import Horizon
from test_evaluator import (
    EVAL_ROUTE,
    LATER,
    blind,
    compare,
    researched,
    store_restated,
    verdicts,
)
from test_improvement import ASSESSOR, CAPS, PRODUCER, proto
from test_researcher import AT, E1, FY25, REG, REV, mandate, route, store_with
from test_trial_ledger import DS, PROMO, T0, plan

K = ClaimKind
SCOPE = "installation-synth"
EXP = "exp-synth-1"
S = timedelta(seconds=1)
BASE = ei.Policy()
HELPFUL = ei.Policy(material_kinds=ev.MATERIAL_KINDS | {K.ASSUMPTION},
                    conflict_tolerance=Decimal("0.005"))  # fmt: skip
LAX = ei.Policy(conflict_tolerance=Decimal("0.01"))
STRICT = ei.Policy(material_kinds=ev.MATERIAL_KINDS | {K.INTERPRETATION})
NO_REVISION = ei.Policy(max_revisions=0)
RIG = ei.Rig((EVAL_ROUTE,), GatewayContext(REG, UseScope.PERSONAL, REGION, None,
             timedelta(hours=1)), BudgetCaps(Usd("100"), Usd("20"), "own"))  # fmt: skip


def store_reading(sid: str, value: str) -> EvidenceStore:
    """store_with() plus a later primary passage `p-<sid>` reporting `value`."""
    s, pub = store_with(), AT - timedelta(hours=2)
    r = IngestRights(REG, TENANT, "feed-a", pub, UseScope.PERSONAL, REGION, KEEP)
    text = f"SYNTHETIC amendment: Revenues were {value} USD for FY2025."
    s.add_source(r, sid, SourceKind.PRIMARY, text, pub)
    s.add_passage(TENANT, f"p-{sid}", sid, 0, text,
                  Reading(E1, REV, FY25, "USD", Decimal(value)), pub)  # fmt: skip
    return s


def replies(*compares: dict[str, object]) -> tuple[ei.Reply, ...]:
    out: list[ei.Reply] = []
    for c in compares:
        out += [ei.Reply(json.dumps(blind()), 100, 50, 2 * S),
                ei.Reply(json.dumps(c), 100, 50, 3 * S)]  # fmt: skip
    return tuple(out)


def case(cid: str, store: EvidenceStore, *compares: dict[str, object],
         extra: tuple[str, ...] = (), revised: bool = False) -> ei.Case:  # fmt: skip
    m = mandate(mandate_id=f"mandate-{cid}", passage_ids=("p-fact", "p-wire", *extra))
    res, again = researched(store, m), researched(store, m)
    assert isinstance(res.outcome, rs.Thesis) and isinstance(again.outcome, rs.Thesis)
    rev = (again.outcome, again.record) if revised else None
    return ei.Case(cid, res.outcome, res.record, m, store, route(), LATER,
                   replies(*compares), rev)  # fmt: skip


def contested(claim: str, result: str = "contested") -> dict[str, object]:
    return compare(verdicts=verdicts(**{claim: result}))


# SYNTHETIC control: (case, label). Baseline outcome noted for the hand computation.
CASES = (
    case("ok", store_with(), compare()),  # approve
    case("round", store_reading("round", "100.3"), compare(),
         extra=("p-round",)),  # 0.3 % rounding: baseline over-rejects
    case("restated", store_restated(), compare(), extra=("p-restated",)),  # blocks
    case("near", store_reading("near", "99.2"), compare(),
         extra=("p-near",)),  # 0.8 % restatement: material, blocks
    case("assume", store_with(), contested("c-assume", "unsupported")),  # baseline
    # approves an unsupported material assumption: a harmful approval
    case("soft", store_with(), contested("c-read")),  # disclosed, approves
    case("revise", store_with(), contested("c-rev"), compare(),
         revised=True),  # revise, then the revision approves
)  # fmt: skip
A, B = ei.Label.SHOULD_APPROVE, ei.Label.SHOULD_BLOCK
LABELS = {"ok": A, "round": A, "restated": B, "near": B, "assume": B, "soft": A,
          "revise": A}  # fmt: skip
SALT = "5" * 64


def control(cases: tuple[ei.Case, ...] = CASES, labels: dict[str, ei.Label] = LABELS,
            manifest: ei.Manifest | None = None) -> ei.Control:  # fmt: skip
    key = ei.AnswerKey(SALT, labels)
    frozen = manifest or ei.freeze("control-synth", "v1", CASES,
                                   ei.AnswerKey(SALT, LABELS), RIG.ctx)  # fmt: skip
    return ei.Control(frozen, cases, key)


PLAN = plan(protocol_hash=control().manifest.content_hash)


def protocol(base: ei.Policy = BASE, **kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "target": im.Target.EVALUATOR, "scope": im.Scope.INSTALLATION,
        "scope_id": SCOPE, "scorecard": ei.scorecard("card-ev"),
        "baseline_version": "evalpol-v0", "baseline_hash": base.content_hash,
        "plan_digest": im.plan_digest(PLAN),
        "evidence_purpose": Purpose.SHARED_IMPROVEMENT,
        "hypothesis": "SYNTHETIC: fewer harmful approvals, no new over-rejection",
    } | kw  # fmt: skip
    return proto(**args)


def cand(p: ei.Policy, pr: im.Protocol | None = None) -> im.Candidate:
    return ei.candidate("cand-1", pr or protocol(), "evalpol-v0", p,
                        {"evaluator": "t039"}, T0 + timedelta(minutes=5))  # fmt: skip


def access(c: im.Candidate, role: tl.Role = tl.Role.ASSESSOR,
           actor: str = ASSESSOR.actor_id) -> tuple[tl.TrialLedger, str]:  # fmt: skip
    lg = tl.TrialLedger(SCOPE).register_plan(PLAN, DS)
    lg, a = lg.request_access("acc-1", PLAN.plan_id, "trial-1", c.trial_key,
                              tl.Split.PROMOTION, PROMO, None, actor, role,
                              T0 + timedelta(hours=1))  # fmt: skip
    return lg, a.access_id


def measured(c: im.Candidate, pr: im.Protocol | None = None, base: ei.Policy = BASE,
             ctl: ei.Control | None = None) -> dict[str, Decimal]:  # fmt: skip
    lg, aid = access(c)
    return dict(ei.measure(pr or protocol(base), c, base, ctl or control(), RIG, lg,
                           aid))  # fmt: skip


def record(lg: tl.TrialLedger, c: im.Candidate, pr: im.Protocol,
           metrics: object, aid: str) -> tl.TrialLedger:  # fmt: skip
    key = c.trial_key
    return lg.record(tl.Trial("trial-1", PLAN.plan_id, key.family, pr.hypothesis,
        key.material_hash, key.config_hash, PLAN.dataset_hash, PLAN.protocol_hash,
        tl.Split.PROMOTION, tl.Outcome.COMPLETED, metrics, aid,  # type: ignore[arg-type]
        T0 + timedelta(hours=1, minutes=30)))  # fmt: skip


Assessed = tuple[im.ExperimentRegistry, im.Verdict]


def verdict(p: ei.Policy, pr: im.Protocol | None = None, base: ei.Policy = BASE,
            c: im.Candidate | None = None) -> Assessed:  # fmt: skip
    pr = pr or protocol(base)
    c = c or cand(p, pr)
    reg = im.ExperimentRegistry(SCOPE).register(pr).register_candidate(c)
    lg, aid = access(c)
    lg = record(lg, c, pr, ei.measure(pr, c, base, control(), RIG, lg, aid), aid)
    book, out = im.schedule(reg, (EXP,), BudgetBook.open(SCOPE, T0, CAPS), T0)
    assert isinstance(out[0], im.Scheduled)
    v = im.assess(reg, EXP, "cand-1", lg, book, ASSESSOR, T0 + timedelta(days=1),
                  "verdict-1")  # fmt: skip
    return reg.record_verdict(v), v


def test_policy_is_a_closed_typed_knob_set_and_widening_is_named() -> None:
    assert set(BASE.to_payload()) == ei.KNOBS
    assert ei.Policy.from_payload(HELPFUL.to_payload()) == HELPFUL
    assert (
        HELPFUL.to_payload()["material_kinds"] == "assumption,calculation,reported_fact"
    )
    assert HELPFUL.content_hash != BASE.content_hash
    named = {"answer_key_case_near": "answer_key", "expected_labels": "answer_key",
             "holdout_cases": "answer_key", "risk_limits": "widens_authority",
             "scorecard_bounds": "widens_authority", "require_distinct_family":
             "widens_authority", "prompt_template": "widens_authority",
             "evaluator_code": "widens_authority",
             "theme": "not_adaptable"}  # fmt: skip
    for k, code in named.items():
        with pytest.raises(ei.PolicyError, match=code):
            ei.Policy.from_payload({k: "1"})
    bad = {"material_kinds": ("reported_fact", "assumption,reported_fact", "x",
                              "reported_fact,calculation"),
           "conflict_tolerance": ("0.011", "-0", "0.0050", "1e-3", ".005"),
           "min_alternatives": ("0", "4", "01"), "max_revisions": ("2", "-1"),
           "review_max_cost_usd": ("0", "0.11", "0.050"),
           "pass_output_tokens": ("63", "2001"),
           "pass_timeout_seconds": ("0", "31", " 5")}  # fmt: skip
    for k, values in bad.items():
        for v in values:
            with pytest.raises(ei.PolicyError, match="value"):
                ei.Policy.from_payload({k: v})
    # the evaluator itself refuses a policy below its floor (spec §7 and T039)
    cfg = BASE.config(route())
    for kw in ({"material_kinds": frozenset({K.REPORTED_FACT})},
               {"conflict_tolerance": Decimal("0.02")}, {"min_alternatives": 0},
               {"conflict_tolerance": 0}):  # fmt: skip
        with pytest.raises((ev.EvaluatorError, TypeError)):
            replace(cfg, **kw)


def test_evaluator_policy_knobs_change_the_computed_outcome() -> None:
    cfg = BASE.config(route())
    assert (cfg.material_kinds, cfg.conflict_tolerance, cfg.min_alternatives) == (
        ev.MATERIAL_KINDS, Decimal(0), 1)  # T039 defaults unchanged  # fmt: skip
    rnd = CASES[1]
    for p, decision, material in (
        (BASE, ev.Decision.REVISE, ("conflict:c-rev:p-round",)),
        (HELPFUL, ev.Decision.APPROVE, ()),
        (ei.Policy(min_alternatives=2, conflict_tolerance=Decimal("0.005")),
         ev.Decision.REVISE, ("alternatives_short",)),
    ):  # fmt: skip
        out = ei.replay(p, rnd, RIG)
        assert isinstance(out.outcome, ev.Review), out.outcome
        assert (out.outcome.decision, out.outcome.material) == (decision, material)
    # a 2 s pass timeout: the 3 s compare reply times out; no review, reservation open
    out = ei.replay(ei.Policy(timeout_seconds=2), CASES[0], RIG)
    assert isinstance(out.outcome, rs.Abstention)
    assert out.outcome.why is rs.Why.MODEL_FAILED
    assert [r.finished_at - r.dispatched_at for r in out.runs] == [2 * S, 2 * S]
    assert out.book.summary().reserved.value > 0


def test_control_is_frozen_and_any_tamper_is_rejected_before_the_key_is_read() -> None:
    ctl = control()
    assert ctl.manifest.content_hash == PLAN.protocol_hash  # frozen into T044 protocol
    assert [i for i, _ in ctl.manifest.cases] == list(LABELS)
    assert "should_" not in json.dumps(ctl.manifest.cases) and SALT not in repr(ctl.key)
    c = cand(HELPFUL)
    changed = replace(CASES[2], replies=replies(compare("research_only")))
    tampered = {
        "case_modified:restated": control((*CASES[:2], changed, *CASES[3:])),
        "case_removed:soft": control(CASES[:5] + CASES[6:]),
        "case_added:extra": control((*CASES, replace(CASES[0], case_id="extra"))),
        "case_reordered": control((CASES[1], CASES[0], *CASES[2:])),
        "key_modified": control(labels=LABELS | {"near": A}),
    }  # fmt: skip
    for code, ctl_bad in tampered.items():
        lg, aid = access(c)
        with pytest.raises(ei.ControlError, match=code):
            ei.measure(protocol(), c, BASE, ctl_bad, RIG, lg, aid)
        assert ctl_bad.key.reads == 0  # refused before any answer-key read
    # a re-frozen control (new manifest) is not the one the protocol froze
    other = ei.freeze("control-synth", "v2", CASES, ei.AnswerKey(SALT, LABELS), RIG.ctx)
    with pytest.raises(ei.ControlError, match="control_not_frozen"):
        measured(c, ctl=control(manifest=other))
    with pytest.raises(ei.ControlError, match="unbalanced"):
        ei.freeze("control-synth", "v1", CASES, ei.AnswerKey(SALT, dict.fromkeys(
            LABELS, A)), RIG.ctx)  # fmt: skip
    with pytest.raises(ei.ControlError, match="key_cases"):
        ei.freeze("control-synth", "v1", CASES[:6], ei.AnswerKey(SALT, LABELS), RIG.ctx)
    with pytest.raises(ei.ControlError, match="baseline_hash"):
        measured(c, pr=protocol(), base=HELPFUL)


def test_answer_key_is_read_once_by_the_registered_assessor_only() -> None:
    c = cand(HELPFUL)
    for role, actor in ((tl.Role.RESEARCHER, PRODUCER.actor_id),
                        (tl.Role.ASSESSOR, "someone-synth")):  # fmt: skip
        lg, aid = access(c, role, actor)
        with pytest.raises(ei.ControlError, match="key_access"):
            ei.measure(protocol(), c, BASE, control(), RIG, lg, aid)
    ctl = control()
    lg, aid = access(c)
    ei.measure(protocol(), c, BASE, ctl, RIG, lg, aid)
    assert ctl.key.reads == 1
    with pytest.raises(ei.ControlError, match="key_reused"):
        ei.measure(protocol(), c, BASE, ctl, RIG, lg, aid)
    other = replace(c, artifact_hash="9" * 64)  # another candidate's holdout read
    with pytest.raises(ei.ControlError, match="key_access"):
        ei.measure(protocol(), other, BASE, control(), RIG, lg, aid)


def test_helpful_candidate_metrics_and_acceptance_for_review() -> None:
    _, v = verdict(HELPFUL)
    # Baseline: approves `assume` (harmful) and over-rejects `round`; 8 evaluations
    # (revise runs twice) x 5 s = 40 s; 16 passes x USD0.00105 = USD0.0168.
    # Helpful: blocks `assume`, approves `round`; same calls, same latency and cost.
    assert dict(v.scores) == {
        "answer_key_mutations": 0, "unverified_claims_accepted": 0,
        "new_abstentions": 0, "artifact_mismatch": 0,
        "policy_widened": 0, "payload_invalid": 0, "false_approval_increase": -1,
        "improved": 1, "over_rejection_increase": -1, "latency_increase_seconds": 0,
        "cost_increase_usd": 0, "false_approvals": 0, "over_rejections": 0,
        "false_approval_rate": 0, "over_rejection_rate": 0, "latency_seconds": 40,
        "cost_usd": Decimal("0.0168"), "baseline_false_approvals": 1,
        "baseline_over_rejections": 1, "baseline_false_approval_rate":
        Decimal("0.3333"), "baseline_over_rejection_rate": Decimal("0.25"),
        "baseline_latency_seconds": 40, "baseline_cost_usd": Decimal("0.0168"),
    }  # fmt: skip
    assert all(type(x) is Decimal for x in v.scores.values())
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert v.limitations == ("synthetic_only", "no_multiple_testing_adjustment")  # 36
    assert v.requires_human_promotion


def test_regressions_and_tampering_candidates_are_rejected() -> None:
    _, v = verdict(LAX)  # 1 % tolerance approves the 0.8 % restatement in `near`
    assert (v.scores["false_approvals"], v.scores["over_rejections"]) == (
        Decimal(2),
        Decimal(0),
    )
    assert v.decision is im.Decision.REJECT
    assert v.reasons == ("hard:false_approval_increase",)  # not offset by `improved`
    _, v = verdict(STRICT)  # interpretation made material: `soft` over-rejected
    assert v.scores["over_rejections"] == 2 and v.scores["false_approvals"] == 1
    assert v.reasons == ("goal:improved", "goal:over_rejection_increase")
    _, v = verdict(NO_REVISION)  # `revise` rejected at once: cheaper, worse
    assert (v.scores["latency_increase_seconds"], v.scores["cost_increase_usd"]) == (
        Decimal(-5), Decimal("-0.0021"))  # fmt: skip
    assert v.reasons == ("goal:improved", "goal:over_rejection_increase")
    # against a no-revision baseline, allowing a revision costs 2 more passes
    pr = protocol(NO_REVISION)
    _, v = verdict(BASE, pr, NO_REVISION)
    assert v.reasons == ("goal:latency_increase_seconds", "goal:cost_increase_usd")
    loose = ei.scorecard("card-ev", latency=Decimal(5), cost=Decimal("0.0021"))
    _, v = verdict(BASE, protocol(NO_REVISION, scorecard=loose), NO_REVISION)
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    # payloads built around the policy type still meet the independent measurement
    good = HELPFUL.to_payload()
    for extra, metric in (({"answer_labels": "x"}, "answer_key_mutations"),
                          ({"min_alternatives": "two"}, "payload_invalid"),
                          ({"min_alternatives": "case near"}, "answer_key_mutations"),
                          ({"material_kinds": "near"}, "answer_key_mutations"),
                          ({"note": "should_block"}, "answer_key_mutations"),
                          ({"risk_override": "1"}, "policy_widened"),
                          ({"pass_timeout_seconds": "99"},
                           "payload_invalid")):  # fmt: skip
        m = measured(replace(cand(HELPFUL), payload=good | extra))
        assert m[metric] == 1, (extra, m)
    c = replace(cand(HELPFUL), payload=good | {"answer_labels": "x"})
    _, v = verdict(HELPFUL, c=c)
    assert v.decision is im.Decision.REJECT
    assert v.reasons == ("hard:answer_key_mutations",)


def test_protocol_must_be_installation_scoped_and_hold_the_controls() -> None:
    ei.check_protocol(protocol())
    with pytest.raises(ei.PolicyError, match="not_installation"):
        ei.check_protocol(protocol(scope=im.Scope.TENANT))
    with pytest.raises(ei.PolicyError, match="not_installation"):
        ei.check_protocol(proto())  # a strategy protocol
    hard = tuple(im.Threshold(m, im.Direction.AT_MOST, Decimal(0), True)
                 for m in im.HARD_CONTROLS[im.Target.EVALUATOR])  # fmt: skip
    goal = im.Threshold("improved", im.Direction.AT_LEAST, Decimal(1), False)
    weak = im.Scorecard("card-weak", im.Target.EVALUATOR, (*hard, goal))
    with pytest.raises(ei.PolicyError, match="control_missing"):
        ei.check_protocol(protocol(scorecard=weak))
    assert im.MIN_APPROVALS[im.Target.EVALUATOR] == {
        im.ApprovalClass.MAINTAINER_RELEASE,
        im.ApprovalClass.OPERATOR_INSTALL,
    }


def test_nothing_here_grants_installs_or_adopts() -> None:
    names = [n for n in dir(ei) if callable(getattr(ei, n))]
    verbs = ("approve", "install", "adopt", "promote", "sign", "grant")
    assert not [n for n in names if any(v in n.lower() for v in verbs)]


def test_review_round1_goals_abstentions_hashes_and_key_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S1: the regression goals are mandatory and their bounds capped
    timeout_all = ei.Policy(timeout_seconds=1)  # every case abstains: nothing approved
    loosest = ei.scorecard("card-ev", latency=ei.MAX_LATENCY_INCREASE,
                           cost=ei.MAX_COST_INCREASE)  # fmt: skip
    _, v = verdict(timeout_all, protocol(scorecard=loosest))
    assert v.decision is im.Decision.REJECT and v.scores["new_abstentions"] == 7
    assert v.reasons == ("hard:new_abstentions", "goal:improved",
                         "goal:over_rejection_increase",
                         "goal:cost_increase_usd")  # held reservations  # fmt: skip
    for kw in ({"latency": ei.MAX_LATENCY_INCREASE + 1},
               {"cost": ei.MAX_COST_INCREASE + Decimal("0.000001")}):  # fmt: skip
        with pytest.raises(ei.PolicyError, match="goal_bound"):
            ei.scorecard("card-ev", **kw)
    hard = tuple(im.Threshold(m, im.Direction.AT_MOST, Decimal(0), True)
                 for m in (*im.HARD_CONTROLS[im.Target.EVALUATOR],
                           *ei.EVALUATOR_HARD))  # fmt: skip
    improved = im.Threshold("improved", im.Direction.AT_LEAST, Decimal(1), False)
    loose_or = im.Threshold("over_rejection_increase", im.Direction.AT_MOST,
                            Decimal(100), False)  # fmt: skip
    for goals in ((improved,), (improved, loose_or)):
        card = im.Scorecard("card-weak", im.Target.EVALUATOR, (*hard, *goals))
        with pytest.raises(ei.PolicyError, match="goal_missing"):
            ei.check_protocol(protocol(scorecard=card))
    # `improved` needs over-rejections not to rise (STRICT-like trade is not one)
    both = ev.MATERIAL_KINDS | {K.ASSUMPTION, K.INTERPRETATION}
    trade = ei.Policy(material_kinds=both)
    m = measured(cand(trade))  # blocks `assume` but over-rejects `soft`
    assert (m["false_approval_increase"], m["over_rejection_increase"]) == (
        Decimal(-1), Decimal(1))  # fmt: skip
    assert m["improved"] == 0

    # S2: feed and workflow horizon are part of the frozen case
    def fed(feed: str) -> ei.Case:
        st, pub = store_with(), AT - timedelta(hours=2)
        r = IngestRights(REG, TENANT, feed, pub, UseScope.PERSONAL, REGION, KEEP)
        st.add_source(
            r, "x", SourceKind.INDEPENDENT, "SYNTHETIC x: Revenues were 100 USD.", pub
        )
        st.add_passage(TENANT, "p-x", "x", 0, "SYNTHETIC x: Revenues were 100 USD.",
                       Reading(E1, REV, FY25, "USD", Decimal(100)), pub)  # fmt: skip
        m = replace(CASES[0].mandate, passage_ids=("p-fact", "p-wire", "p-x"))
        return replace(CASES[0], store=st, mandate=m)

    assert fed("feed-a").content_hash(RIG.ctx) != fed("feed-s").content_hash(RIG.ctx)
    wf = replace(CASES[0].mandate.workflow, horizon=Horizon.SWING_POSITION)
    moved = replace(CASES[0], mandate=replace(CASES[0].mandate, workflow=wf))
    assert moved.content_hash(RIG.ctx) != CASES[0].content_hash(RIG.ctx)
    # N1: a replay failure cannot burn the key; a recorded trial bars a re-read
    c = cand(HELPFUL)

    def boom(*_: object) -> None:
        raise ev.EvaluatorError("synthetic_failure")

    ctl = control()
    with monkeypatch.context() as mp:
        mp.setattr(ei, "replay", boom)
        lg, aid = access(c)
        with pytest.raises(ev.EvaluatorError):
            ei.measure(protocol(), c, BASE, ctl, RIG, lg, aid)
    assert ctl.key.reads == 0
    lg = record(lg, c, protocol(), ei.measure(protocol(), c, BASE, ctl, RIG, lg, aid),
                aid)  # fmt: skip
    with pytest.raises(ei.ControlError, match="key_reused"):
        ei.measure(protocol(), c, BASE, control(), RIG, lg, aid)  # a fresh key
    # N2: ids and payload text are normalised alike
    ids = frozenset({"Case-Near"})
    assert ei._leaks("x case_near y", ids) and ei._leaks("CASE NEAR", ids)
    assert not ei._leaks("nearby", ids) and not ei._leaks("case", ids)
    # N3: the artifact hash must be the payload policy's hash
    assert measured(replace(cand(HELPFUL), artifact_hash="9" * 64))[
        "artifact_mismatch"] == 1  # fmt: skip
    assert measured(cand(HELPFUL))["artifact_mismatch"] == 0
    # N6: repeated alternatives count once
    dup = replace(CASES[0], replies=replies(compare(alternatives=["null", " Null"])))
    out = ei.replay(ei.Policy(min_alternatives=2), dup, RIG).outcome
    assert isinstance(out, ev.Review) and out.material == ("alternatives_short",)
