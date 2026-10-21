"""Experiment protocols, candidate lineage, scheduling and the independent assessor
(T044 increment 1). All data is SYNTHETIC; expected values are hand-written.
"""

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from qw_domain.backtest import ClaimMetric, DeclaredClaim
from qw_domain.backtest import Direction as ClaimDirection
from qw_domain.budget import BudgetBook, BudgetCaps, Category, Status
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.feedback import (
    Access,
    CaseOrigin,
    EvaluationCase,
    ImprovementEvidence,
    Purpose,
    Split,
    Use,
)
from qw_domain.research_data import digest
from test_backtest import protocol as bt_protocol
from test_feedback import SUBJ
from test_trial_ledger import (
    DEV,
    PROMO,
    T0,
    TENANT,
    ask,
    evaluation,
    ledger,
    plan,
    trial,  # fmt: skip
)

H = im.Threshold
D = im.Direction
PRODUCER = im.Party("optimizer-synth", "route-a", "family-a", "opt-v0")
ASSESSOR = im.Party("actor-synth", "route-b", "family-b", "assessor-v1")
CAPS = BudgetCaps.from_config(
    {"pilot_monthly_usd": "100.00", "improvement_monthly_usd_max": "20.00"},
    authorization_ref="owner-auth-synth-1",
)
EXP = "exp-synth-1"
BASE_HASH, ART = "c" * 64, "d" * 64
AT = T0 + timedelta(hours=1)


def card(target: im.Target = im.Target.STRATEGY, *extra: im.Threshold) -> im.Scorecard:
    hard = tuple(H(m, D.AT_MOST, Decimal(0), True) for m in im.HARD_CONTROLS[target])
    goal = H("net_return", D.AT_LEAST, Decimal("0.01"), False)
    return im.Scorecard("card-synth", target, (*hard, goal, *extra))


def proto(**kw: object) -> im.Protocol:
    target = kw.get("target", im.Target.STRATEGY)
    assert isinstance(target, im.Target)
    args: dict[str, object] = {
        "experiment_id": EXP, "scope": im.Scope.TENANT, "scope_id": TENANT,
        "target": target, "hypothesis": "SYNTHETIC: a bounded variant beats baseline",
        "baseline_version": "strat-v0", "baseline_hash": BASE_HASH,
        "producer": PRODUCER, "assessor": ASSESSOR, "require_distinct_family": True,
        "scorecard": card(target), "plan_id": "plan-synth-1",
        "plan_digest": im.plan_digest(plan()),
        "evidence_purpose": Purpose.PERSONAL_ADAPTATION, "risk_policy_hash": "f" * 64,
        "approvals": im.MIN_APPROVALS[target], "budget_reservation_id": "bud-exp-1",
        "max_cost": Usd("5.00"), "provider_account_ref": "provider-acct-synth",
        "max_output_tokens": 1000, "max_duration": timedelta(hours=2),
        "sandbox_profile_id": None, "authorized_by": "owner-synth",
        "registered_at": T0,
    } | kw  # fmt: skip
    return im.Protocol(**args)  # type: ignore[arg-type]


def cand(cid: str = "cand-1", **kw: object) -> im.Candidate:
    args: dict[str, object] = {
        "candidate_id": cid, "experiment_id": EXP, "target": im.Target.STRATEGY,
        "parent_id": "strat-v0", "producer": PRODUCER, "artifact_hash": ART,
        "payload": {"lookback_months": "11"}, "runtime_versions": {"engine": "v1"},
        "evidence_uses": (), "created_at": T0 + timedelta(minutes=5),
    } | kw  # fmt: skip
    return im.Candidate(**args)  # type: ignore[arg-type]


def ev(cid: str, use: object) -> im.Candidate:  # a distinct artifact per id
    return cand(cid, artifact_hash=cid[-1] * 64, evidence_uses=(use,))


def registry(p: im.Protocol | None = None) -> im.ExperimentRegistry:
    reg = im.ExperimentRegistry(TENANT).register(p or proto())
    return reg.register_candidate(cand())


def book() -> BudgetBook:
    return BudgetBook.open(TENANT, AT, CAPS)


def scheduled(reg: im.ExperimentRegistry) -> BudgetBook:
    b, out = im.schedule(reg, (EXP,), book(), AT)
    assert isinstance(out[0], im.Scheduled)
    return b


def promo_ledger(metrics: dict[str, Decimal], c: im.Candidate | None = None,
                 actor: str = "actor-synth") -> tl.TrialLedger:  # fmt: skip
    key = (c or cand()).trial_key
    lg, a = ask(ledger(), 1, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR, None, key)
    lg = tl.TrialLedger(lg.tenant_id, (*lg.entries[:-1], replace(a, actor_id=actor)))
    return lg.record(trial(1, tl.Split.PROMOTION, family=EXP, metrics=metrics,
                           material_hash=key.material_hash,
                           config_hash=key.config_hash))  # fmt: skip


# Decision 35: a strategy promotion also needs each baseline's excess mean and se;
# 0.02 - 2.58 * 0.002 > 0 clears the widened bound for small trial counts.
EXCESS = {
    f"baseline.{b}.{k}": Decimal(v)
    for b in ("bl-hold", "bl-cash")
    for k, v in (("excess_mean", "0.02"), ("excess_std_error", "0.002"))
}
GOOD = {"net_return": Decimal("0.02"), "lookahead_violations": Decimal(0),
        "survivorship_violations": Decimal(0),
        "unrecorded_trials": Decimal(0), **EXCESS}  # fmt: skip
EVAL = evaluation()  # the T033 protocol the fixture plan freezes (PROTO_HASH)


def assessed(lg: tl.TrialLedger, who: im.Party, at: datetime, vid: str,
             **kw: object) -> im.Assessment:  # fmt: skip
    """An assessment carrying the evaluation protocol frozen with the plan."""
    return im.Assessment(lg, who, at, vid, evaluation=EVAL, **kw)  # type: ignore[arg-type]


def assess(reg: im.ExperimentRegistry, lg: tl.TrialLedger, **kw: object) -> im.Verdict:
    args: dict[str, object] = {"registry": reg, "experiment_id": EXP,
        "candidate_id": "cand-1", "ledger": lg, "book": scheduled(reg),
        "assessor": ASSESSOR, "at": AT + timedelta(days=1),
        "verdict_id": "verdict-1", "evaluation": EVAL} | kw  # fmt: skip
    return im.assess(**args)  # type: ignore[arg-type]


def test_all_six_targets_have_hard_controls_and_minimum_approvals() -> None:
    assert {t.value for t in im.Target} == {"personal_adaptation", "research",
        "strategy", "evaluator", "application_code", "recursive_optimizer"}  # fmt: skip
    for t in im.Target:
        assert im.HARD_CONTROLS[t] and im.MIN_APPROVALS[t]
        proto(target=t, sandbox_profile_id="sandbox-synth")  # each is representable
    sandboxed = {im.Target.APPLICATION_CODE, im.Target.RECURSIVE_OPTIMIZER}
    assert sandboxed == im.SANDBOX_TARGETS
    assert im.MIN_APPROVALS[im.Target.STRATEGY] == frozenset(im.ApprovalClass)
    assert im.MIN_APPROVALS[im.Target.PERSONAL_ADAPTATION] == {
        im.ApprovalClass.OPERATOR_INSTALL,
        im.ApprovalClass.USER_STRATEGY_ADOPTION,
    }


def test_scorecard_cannot_drop_or_loosen_a_hard_control() -> None:
    hard = im.HARD_CONTROLS[im.Target.STRATEGY]
    goal = H("net_return", D.AT_LEAST, Decimal("0.01"), False)
    with pytest.raises(im.ImprovementError, match="hard_control_missing"):
        im.Scorecard("c", im.Target.STRATEGY, (goal,))
    loose = tuple(H(m, D.AT_MOST, Decimal(1), True) for m in hard)
    with pytest.raises(im.ImprovementError, match="hard_control_missing"):
        im.Scorecard("c", im.Target.STRATEGY, (*loose, goal))
    zero = tuple(H(m, D.AT_MOST, Decimal(0), True) for m in hard)
    with pytest.raises(im.ImprovementError, match="goal_required"):
        im.Scorecard("c", im.Target.STRATEGY, zero)
    with pytest.raises(TypeError):
        H("net_return", D.AT_LEAST, 0.01, False)  # type: ignore[arg-type]


def test_protocol_requires_independent_assessor_and_human_approvals() -> None:
    for same in (replace(ASSESSOR, actor_id=PRODUCER.actor_id),
                 replace(ASSESSOR, route_id=PRODUCER.route_id),
                 replace(ASSESSOR, family_id=PRODUCER.family_id)):  # fmt: skip
        with pytest.raises(im.ImprovementError, match="assessor_not_independent"):
            proto(assessor=same)
    proto(assessor=replace(ASSESSOR, family_id="family-a"),
          require_distinct_family=False)  # configured: family may match  # fmt: skip
    with pytest.raises(im.ImprovementError, match="approvals"):
        proto(approvals=frozenset({im.ApprovalClass.MAINTAINER_RELEASE}))
    with pytest.raises(im.ImprovementError, match="sandbox_profile"):
        proto(target=im.Target.APPLICATION_CODE)


def test_frozen_protocol_refuses_every_later_change() -> None:
    reg = im.ExperimentRegistry(TENANT).register(proto())
    assert reg.register(proto()) is reg  # identical retry
    tighter = card(im.Target.STRATEGY)
    changes: dict[str, object] = {
        "scorecard": replace(tighter, thresholds=(*tighter.thresholds[:-1],
            H("net_return", D.AT_LEAST, Decimal("-1"), False))),
        "plan_id": "plan-other", "plan_digest": "0" * 64,
        "risk_policy_hash": "1" * 64,
        "assessor": replace(ASSESSOR, version="assessor-v2"),
        "max_cost": Usd("20.00"),
    }  # fmt: skip
    for name, value in changes.items():
        with pytest.raises(im.ImprovementError, match=f"protocol_frozen.*{name}"):
            reg.register(proto(**{name: value}))
    assert reg.protocol(EXP).content_hash == proto().content_hash
    rs = im.Target.RESEARCH
    reg = im.ExperimentRegistry(TENANT).register(proto(target=rs))
    with pytest.raises(im.ImprovementError, match=r"protocol_frozen.*approvals"):
        reg.register(proto(target=rs, approvals=frozenset(im.ApprovalClass)))


def test_candidate_is_data_and_cannot_carry_protected_fields() -> None:
    reg = im.ExperimentRegistry(TENANT).register(proto())
    for key in ("thresholds", "Scorecard", "approval-records", "risk_policy_hash",
                "test_access", "plan_id", "assessor", "signing_key"):  # fmt: skip
        with pytest.raises(im.ImprovementError, match="protected_field"):
            reg.register_candidate(cand(payload={key: "0"}))
    before = reg.protocol(EXP).content_hash
    reg = reg.register_candidate(cand())
    assert reg.protocol(EXP).content_hash == before


def test_lineage_is_append_only_and_bound() -> None:
    reg = registry()
    child = cand("cand-2", parent_id="cand-1", artifact_hash="e" * 64)
    reg = reg.register_candidate(child)
    assert reg.lineage("cand-2") == ("cand-2", "cand-1", "strat-v0")
    assert reg.register_candidate(child) is reg  # identical retry
    bad: dict[str, dict[str, object]] = {
        "duplicate": {"candidate_id": "cand-2", "artifact_hash": "a" * 64},
        "unknown_parent": {"parent_id": "cand-404"},
        "identical_artifact": {"artifact_hash": ART, "parent_id": "cand-1"},
        "producer_mismatch": {"producer": replace(PRODUCER, actor_id="other")},
        "target_mismatch": {"target": im.Target.RESEARCH},
        "before_protocol": {"created_at": T0 - timedelta(minutes=1)},
    }
    for code, kw in bad.items():
        kw = {"candidate_id": "cand-x", "artifact_hash": "9" * 64} | kw
        with pytest.raises(im.ImprovementError, match=code):
            reg.register_candidate(cand(**kw))  # type: ignore[arg-type]
    with pytest.raises(im.ImprovementError, match="identical_artifact"):
        reg.register_candidate(cand("cand-y", artifact_hash=BASE_HASH))
    replayed = im.ExperimentRegistry(TENANT, reg.entries)
    assert replayed.lineage("cand-2") == reg.lineage("cand-2")
    with pytest.raises(im.ImprovementError, match="unknown_parent"):
        im.ExperimentRegistry(TENANT, (reg.entries[0], reg.entries[2]))


def test_candidate_evidence_must_be_a_logged_development_selection() -> None:
    store = ImprovementEvidence()
    store.record(EvaluationCase(
        tenant_id=TENANT, record_id="case-1", subject=SUBJ, occurred_at=T0,
        recorded_at=T0, inputs={"attribute": "revenue"}, expected="ok",
        split=Split.FINAL_HOLDOUT, origin=CaseOrigin.SYNTHETIC))  # fmt: skip
    at = T0 + timedelta(minutes=1)
    dev = store.select(TENANT, EXP, Purpose.PERSONAL_ADAPTATION, Access.DEVELOPMENT, at)
    assert dev.records == ()  # the holdout case never reaches development
    final = store.select(TENANT, EXP, Purpose.PERSONAL_ADAPTATION,
                         Access.FINAL_ASSESSMENT, at)  # fmt: skip
    reg = im.ExperimentRegistry(TENANT).register(proto())
    reg = reg.register_candidate(cand(evidence_uses=(dev.use,)), store)
    with pytest.raises(im.ImprovementError, match="final_access_by_producer"):
        reg.register_candidate(ev("c2", final.use), store)
    other = replace(dev.use, experiment_id="exp-other")
    with pytest.raises(im.ImprovementError, match="evidence_experiment"):
        reg.register_candidate(ev("c3", other), store)
    forged = replace(dev.use, record_ids=("case-1",))
    with pytest.raises(im.ImprovementError, match="evidence_not_logged"):
        reg.register_candidate(ev("c4", forged), store)
    with pytest.raises(im.ImprovementError, match="evidence_not_logged"):
        reg.register_candidate(ev("c5", dev.use))


def test_scheduling_reserves_first_and_defers_on_exhaustion() -> None:
    reg = im.ExperimentRegistry(TENANT)
    for n, cost in ((1, "12.00"), (2, "9.00"), (3, "1.00")):
        reg = reg.register(proto(experiment_id=f"exp-{n}", max_cost=Usd(cost),
                                 budget_reservation_id=f"bud-{n}"))  # fmt: skip
    b, out = im.schedule(reg, ("exp-1", "exp-2", "exp-3"), book(), AT)
    assert out == (im.Scheduled("exp-1", "bud-1"),
                   im.Deferred("exp-2", "improvement_cap"),
                   im.Deferred("exp-3", "queued_behind", "exp-2"))  # fmt: skip
    assert b.summary(Category.IMPROVEMENT).reserved == Usd("12.00")
    first = b.get("bud-1")
    later = AT + timedelta(hours=1)
    b2, again = im.schedule(reg, ("exp-1",), b, later)  # retry never resets
    assert again == (im.Scheduled("exp-1", "bud-1"),) and b2.get("bud-1") == first
    # essential services keep the rest of the inference cap (R032)
    _, refusal = b.reserve("mon-1", Category.MONITORING, Usd("80.00"),
                            "provider-acct-synth", 10, AT, later)  # fmt: skip
    assert refusal is None
    b4 = b.release("bud-1")
    _, closed = im.schedule(reg, ("exp-1",), b4, later)
    assert closed == (im.Deferred("exp-1", "reservation_closed"),)
    assert b4.get("bud-1").status is Status.RELEASED
    unauth = BudgetBook.open(TENANT, AT, replace(CAPS, authorization_ref=None))
    _, out = im.schedule(reg, ("exp-3",), unauth, AT)
    assert out == (im.Deferred("exp-3", "spending_not_authorized"),)


def test_code_and_optimizer_targets_fail_closed_without_a_sandbox() -> None:
    for t in im.SANDBOX_TARGETS:
        p = proto(target=t, sandbox_profile_id="sandbox-synth",
                  baseline_version="opt-v0")  # fmt: skip
        reg = im.ExperimentRegistry(TENANT).register(p)
        reg = reg.register_candidate(cand(target=t, parent_id="opt-v0"))
        b, out = im.schedule(reg, (EXP,), book(), AT)
        assert out == (im.Deferred(EXP, "sandbox_unqualified"),)
        assert b == book()  # nothing reserved
        v = im.assess(reg, EXP, "cand-1", promo_ledger(GOOD), b, ASSESSOR,
                      AT, "verdict-1")  # fmt: skip
        assert v.decision is im.Decision.BLOCKED
        assert v.reasons == ("sandbox_unqualified",) and v.scores == {}


def test_assessor_accepts_only_for_human_review_from_recorded_trials() -> None:
    reg = registry()
    v = assess(reg, promo_ledger(GOOD))
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW and v.reasons == ()
    assert v.next_state == "awaiting_human_review" and v.requires_human_promotion
    assert v.scores["net_return"] == Decimal("0.02") and v.trial_ids == ("trial-1",)
    assert "synthetic_only" in v.limitations
    reg2 = reg.record_verdict(v)
    assert reg2.verdicts("cand-1") == (v,)
    with pytest.raises(im.ImprovementError, match="assessor_mismatch"):
        reg.record_verdict(replace(v, assessor=PRODUCER))


def test_higher_return_with_a_hard_violation_is_rejected() -> None:
    bad = GOOD | {"net_return": Decimal("0.50"), "lookahead_violations": Decimal(1)}
    v = assess(registry(), promo_ledger(bad))
    assert v.decision is im.Decision.REJECT
    assert v.reasons == ("hard:lookahead_violations",)
    low = assess(registry(), promo_ledger(GOOD | {"net_return": Decimal("0.005")}))
    assert low.decision is im.Decision.REJECT and low.reasons == ("goal:net_return",)


def test_candidate_supplied_numbers_never_replace_recorded_ones() -> None:
    claimed = GOOD | {"net_return": Decimal("0.09")}
    v = assess(registry(), promo_ledger(GOOD), claimed=claimed)
    assert v.decision is im.Decision.REJECT and v.reasons == ("metrics_mismatch",)
    assert v.scores["net_return"] == Decimal("0.02")


def test_missing_metric_or_reused_holdout_is_insufficient_never_zero() -> None:
    partial = {k: v for k, v in GOOD.items() if k != "unrecorded_trials"}
    v = assess(registry(), promo_ledger(partial))
    assert v.decision is im.Decision.INSUFFICIENT
    assert v.reasons == ("metric_missing:unrecorded_trials",)
    none = assess(registry(), ledger())
    assert none.reasons == ("no_qualifying_trial",)
    lg = ledger(plan(max_promotion_accesses=2))
    key = cand().trial_key
    lg, _ = ask(lg, 1, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR,
                candidate=tl.Candidate("fam-other", "1" * 64, "2" * 64))  # fmt: skip
    lg, _ = ask(lg, 2, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR, candidate=key)
    lg = lg.record(trial(2, tl.Split.PROMOTION, family=EXP, metrics=GOOD,
                         material_hash=key.material_hash,
                         config_hash=key.config_hash))  # fmt: skip
    p = proto(plan_digest=im.plan_digest(plan(max_promotion_accesses=2)))
    v = assess(registry(p), lg)
    assert v.decision is im.Decision.INSUFFICIENT
    assert v.reasons == ("no_qualifying_trial", "holdout_reused")


def test_assessment_blocks_on_identity_budget_or_test_access_drift() -> None:
    reg, lg = registry(), promo_ledger(GOOD)
    v = assess(reg, lg, assessor=replace(ASSESSOR, version="assessor-v9"))
    assert v.reasons == ("assessor_not_registered",)
    assert assess(reg, lg, book=book()).reasons == ("budget_not_reserved",)
    assert assess(reg, promo_ledger(GOOD, actor="optimizer-synth")).reasons == (
        "no_qualifying_trial", "not_assessor_run")  # fmt: skip
    drift = ledger(plan(max_promotion_accesses=3))
    assert assess(reg, drift).reasons == ("test_access_changed",)
    key = cand().trial_key
    dev_only, _ = ask(ledger(), 1, tl.Split.DEVELOPMENT, DEV, candidate=key)
    assert assess(reg, dev_only).decision is im.Decision.INSUFFICIENT


def test_module_has_no_promotion_or_approval_path() -> None:
    src = Path(im.__file__).read_text()
    names = {n.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef | ast.ClassDef)}  # fmt: skip
    for word in ("promote", "approve", "install", "adopt", "sign"):
        assert not any(word in n.lower() for n in names), word
    imports = {n.module for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ImportFrom)}  # fmt: skip
    assert not imports & {"qw_domain.gateway", "qw_domain.proposals"}


def test_review_round1_time_budget_lineage_and_verdict_rules() -> None:
    late = proto(registered_at=T0 + timedelta(hours=10))  # holdout read at T0+1h
    reg = im.ExperimentRegistry(TENANT).register(late)
    reg = reg.register_candidate(cand(created_at=T0 + timedelta(hours=11)))
    v = assess(reg, promo_ledger(GOOD), at=T0 + timedelta(hours=12))
    assert v.reasons == ("no_qualifying_trial", "trial_before_freeze")
    early = assess(registry(), promo_ledger(GOOD), at=T0 - timedelta(days=5))
    assert early.reasons == ("no_qualifying_trial", "time_order")
    reg = registry()
    with pytest.raises(im.ImprovementError, match="duplicate_reservation_id"):
        reg.register(proto(experiment_id="exp-2"))
    end = AT + timedelta(hours=1)
    small, _ = book().reserve("bud-exp-1", Category.IMPROVEMENT, Usd("0.01"),
                              "provider-acct-synth", 1000, AT, end)  # fmt: skip
    _, out = im.schedule(reg, (EXP,), small, AT)
    assert out == (im.Deferred(EXP, "reservation_terms_mismatch"),)
    mon, _ = book().reserve("bud-exp-1", Category.MONITORING, Usd("5.00"),
                            "provider-acct-synth", 1000, AT, end)  # fmt: skip
    v = assess(reg, promo_ledger(GOOD), book=mon)
    assert v.reasons == ("reservation_terms_mismatch",)
    reg = reg.register(
        proto(
            experiment_id="exp-b",
            budget_reservation_id="bud-b",
            baseline_version="strat-b0",
            baseline_hash="b" * 64,
        )
    )
    with pytest.raises(im.ImprovementError, match="unknown_parent"):
        reg.register_candidate(
            cand(
                "cand-b",
                experiment_id="exp-b",
                parent_id="cand-1",
                artifact_hash="e" * 64,
            )
        )
    with pytest.raises(im.ImprovementError, match="duplicate"):
        reg.register_candidate(cand("strat-b0", artifact_hash="7" * 64))
    reg = reg.register_candidate(
        cand("cand-2", parent_id="cand-1", artifact_hash="e" * 64)
    )
    for parent, art in (("cand-2", BASE_HASH), ("cand-2", ART), ("strat-v0", ART)):
        with pytest.raises(im.ImprovementError, match="identical_artifact"):
            reg.register_candidate(
                cand(
                    "cand-3",
                    parent_id=parent,
                    artifact_hash=art,
                    payload={"lookback_months": "12"},
                )
            )
    for kw in ({"max_cost": Usd("0")}, {"max_output_tokens": 0}):
        with pytest.raises(im.ImprovementError, match="budget"):
            proto(**kw)
    neg = assess(registry(), promo_ledger(GOOD | {"lookahead_violations": Decimal(-3)}))
    assert neg.decision is im.Decision.INSUFFICIENT
    assert neg.reasons == ("metric_invalid:lookahead_violations",)
    ok = assess(registry(), promo_ledger(GOOD))
    for bad in ({"reasons": ("hard:x",)}, {"decision": im.Decision.REJECT}):
        with pytest.raises(im.ImprovementError, match="verdict_inconsistent"):
            replace(ok, **bad)
    with pytest.raises(TypeError):
        assess(registry(), promo_ledger(GOOD), claimed={"net_return": 0.02})
    with pytest.raises(im.ImprovementError, match="duplicate"):
        registry().record_verdict(ok).record_verdict(ok)


SU = ("evidence:synthetic_only", "none:parent_optimizer_version")


def test_record_derives_honest_states_and_reasons() -> None:
    reg, pl = registry(proto(sandbox_profile_id="sbx-synth")), plan()
    b = scheduled(reg)
    with pytest.raises(im.ImprovementError, match="contract_field_unavailable"):
        im.record(registry(), EXP, "cand-1", pl, b)  # no sandbox profile: refused
    first = im.record(reg, EXP, "cand-1", pl, book())
    assert (first.state, first.reasons) == (im.RecordState.PROPOSED, SU)
    assert first.requires_human_promotion and first.verdict_id is None
    assert (first.candidate_version, first.candidate_hash) == ("cand-1", ART)
    assert first.protocol_id == reg.protocol(EXP).content_hash
    assert first.data_snapshot_ids == (pl.dataset.dataset_id,)
    auth = im.record(reg, EXP, "cand-1", pl, b, im.Scheduled(EXP, "bud-exp-1"))
    assert auth.state is im.RecordState.AUTHORIZED and auth.may_follow(first)
    with pytest.raises(im.ImprovementError, match="reservation_unverified"):
        im.record(reg, EXP, "cand-1", pl, book(), im.Scheduled(EXP, "bud-exp-1"))
    later = im.record(reg, EXP, "cand-1", pl, b, im.Deferred(EXP, "queued_behind"))
    assert later.reasons == ("deferred:queued_behind", *SU)
    run = assessed(promo_ledger(GOOD), ASSESSOR, AT + timedelta(days=1), "v-1")
    done = im.record(reg, EXP, "cand-1", pl, b, im.Scheduled(EXP, "bud-exp-1"), run)
    assert (done.state, done.decision) == (
        im.RecordState.AWAITING_REVIEW, im.Decision.ACCEPT_FOR_REVIEW)  # fmt: skip
    assert done.reasons == (SU[0], "none:evaluation_receipt_ids", SU[1])
    assert done.parent_optimizer_version is None  # not the producer's version
    assert done.may_follow(auth) and not first.may_follow(done)
    assert not done.may_follow(done)  # nothing follows a decision
    assert not replace(auth, candidate_version="cand-9").may_follow(first)
    assert not first.may_follow(auth)  # never back
    bad = replace(run, ledger=promo_ledger(GOOD | {"lookahead_violations": Decimal(1)}))
    rej = im.record(reg, EXP, "cand-1", pl, b, None, bad)
    assert rej.state is im.RecordState.REJECTED
    assert "rejected:hard:lookahead_violations" in rej.reasons
    thin = im.record(reg, EXP, "cand-1", pl, b, None,
                     replace(run, ledger=ledger()))  # fmt: skip
    assert thin.state is im.RecordState.INSUFFICIENT_EVIDENCE  # 0.6.0: not failed
    assert "insufficient:no_qualifying_trial" in thin.reasons
    blocked = im.record(reg, EXP, "cand-1", pl, book(), None, run)
    assert (blocked.state, blocked.decision) == (im.RecordState.BLOCKED,
                                                 im.Decision.BLOCKED)  # fmt: skip
    assert "blocked:budget_not_reserved" in blocked.reasons
    for t in im.SANDBOX_TARGETS:  # T007: representable, always blocked
        p = proto(target=t, sandbox_profile_id="sbx-synth")
        r = im.ExperimentRegistry(TENANT).register(p).register_candidate(
            cand(target=t))  # fmt: skip
        got = im.record(r, EXP, "cand-1", pl, book(), im.Deferred(
            EXP, "sandbox_unqualified"), run)  # fmt: skip
        assert got.state is im.RecordState.BLOCKED and got.decision is (
            im.Decision.BLOCKED)  # fmt: skip
        bare = im.record(r, EXP, "cand-1", pl, book())  # no verdict: still blocked
        assert (bare.state, bare.decision) == (im.RecordState.BLOCKED, None)
        assert "blocked:sandbox_unqualified" in got.reasons
        optimizer = t is im.Target.RECURSIVE_OPTIMIZER  # its lineage: the parent
        if optimizer:  # a set parent named as absent is contradictory
            for x in ("none:parent_optimizer_version",
                      "unavailable:parent_optimizer_version"):  # fmt: skip
                with pytest.raises(im.ImprovementError, match="record_inconsistent"):
                    replace(got, reasons=tuple(sorted({*got.reasons, x})))
        assert got.parent_optimizer_version == ("strat-v0" if optimizer else None)
        sandbox: list[dict[str, object]] = [{"state": im.RecordState.AUTHORIZED},
                                            {"reasons": ()}]  # fmt: skip
        if not optimizer:  # an optimizer proposed record is a readable 0.5.0 one
            sandbox.append({"state": im.RecordState.PROPOSED})
        for forged in sandbox:
            with pytest.raises(im.ImprovementError, match="record_inconsistent"):
                replace(got, **forged)  # type: ignore[arg-type]
    decided: list[dict[str, object]] = [
        {"state": im.RecordState.REJECTED}, {"reasons": ("evidence:synthetic_only",)},
        {"decision": None}, {"reasons": ("b", "a")},
        {"parent_optimizer_version": "opt-v0"}]  # fmt: skip
    for forged in decided:
        with pytest.raises(im.ImprovementError):
            replace(done, **forged)  # type: ignore[arg-type]
    many = [f"evidence:r{i:02}" for i in range(65 - len(done.reasons))]
    for extra in (many, ["evidence:" + "x" * 151]):  # served length stays <= 300
        with pytest.raises(im.ImprovementError, match="reasons"):
            replace(done, reasons=tuple(sorted([*done.reasons, *extra])))
    ok = replace(done, reasons=tuple(sorted([*done.reasons, "evidence:" + "x" * 150])))
    assert max(len(f"{r}/{'e' * 128}") for r in ok.reasons) <= 300
    with pytest.raises(im.ImprovementError, match="hypothesis"):
        replace(done, hypothesis="h" * 4001)
    with pytest.raises(im.ImprovementError, match="test_access_changed"):
        im.record(reg, EXP, "cand-1", plan(max_promotion_accesses=2), b)


# --- project contract 0.6.0 (owner decisions 1-3 and Q7, 2026-10-20) ---
St = im.RecordState
# The doc's transition table, hand-written; same-state proposed and authorized
# re-records (a changed deferral reason) stay allowed, as in 0.5.0.
ALLOWED = {
    St.PROPOSED: {St.PROPOSED, St.AUTHORIZED, St.BLOCKED},
    St.AUTHORIZED: {St.AUTHORIZED, St.BLOCKED, St.AWAITING_REVIEW, St.REJECTED,
                    St.INSUFFICIENT_EVIDENCE},
    St.BLOCKED: {St.PROPOSED, St.AUTHORIZED, St.BLOCKED, St.AWAITING_REVIEW,
                 St.REJECTED, St.INSUFFICIENT_EVIDENCE},
    St.INSUFFICIENT_EVIDENCE: {St.INSUFFICIENT_EVIDENCE, St.BLOCKED,
                               St.AWAITING_REVIEW, St.REJECTED},
    St.AWAITING_REVIEW: set(), St.REJECTED: set(),
}  # fmt: skip


def states() -> dict[im.RecordState, im.ExperimentRecord]:
    reg, pl = registry(proto(sandbox_profile_id="sbx-synth")), plan()
    b = scheduled(reg)
    run = assessed(promo_ledger(GOOD), ASSESSOR, AT + timedelta(days=1), "v-1")
    bad = promo_ledger(GOOD | {"lookahead_violations": Decimal(1)})
    got = [
        im.record(reg, EXP, "cand-1", pl, book()),
        im.record(reg, EXP, "cand-1", pl, b, im.Scheduled(EXP, "bud-exp-1")),
        im.record(reg, EXP, "cand-1", pl, book(), None, run),
        im.record(reg, EXP, "cand-1", pl, b, None, run),
        im.record(reg, EXP, "cand-1", pl, b, None, replace(run, ledger=bad)),
        im.record(reg, EXP, "cand-1", pl, b, None, replace(run, ledger=ledger())),
    ]
    return {r.state: r for r in got}  # fmt: skip


def test_states_follow_the_transition_table_with_new_verdict_ids() -> None:
    recs = states()
    assert set(recs) == set(ALLOWED)
    for prev, a in recs.items():
        for nxt, b in recs.items():
            fresh = replace(b, verdict_id=f"v-{nxt.value}") if b.verdict_id else b
            assert fresh.may_follow(a) is (nxt in ALLOWED[prev]), (prev, nxt)
    thin = recs[St.INSUFFICIENT_EVIDENCE]
    assert not thin.may_follow(thin)  # the same verdict id is not a re-assessment
    assert replace(thin, verdict_id="v-2").may_follow(thin)
    blocked = recs[St.BLOCKED]
    assert not replace(recs[St.AWAITING_REVIEW]).may_follow(blocked)  # same v-1
    assert recs[St.BLOCKED].decision is im.Decision.BLOCKED
    assert "none:evaluation_receipt_ids" not in blocked.reasons
    for forged in ({"decision": im.Decision.INSUFFICIENT},  # a blocked verdict's state
                   {"reasons": ("evidence:synthetic_only",
                                "none:parent_optimizer_version")},
                   {"state": im.RecordState.AUTHORIZED}):  # fmt: skip
        with pytest.raises(im.ImprovementError, match="record_inconsistent"):
            replace(blocked, **forged)
    with pytest.raises(im.ImprovementError, match="record_inconsistent"):
        replace(thin, state=St.FAILED)  # 0.6.0 reasons with the 0.5.0 state


def test_insufficient_evidence_is_reassessed_only_while_the_holdout_is_unused() -> None:
    reg, pl = registry(proto(sandbox_profile_id="sbx-synth")), plan()
    b = scheduled(reg)
    at = AT + timedelta(days=1)
    thin = im.record(reg, EXP, "cand-1", pl, b, None,
                     assessed(ledger(), ASSESSOR, at, "v-1"))  # fmt: skip
    assert thin.reasons == (SU[0], "insufficient:no_qualifying_trial",
                            "none:evaluation_receipt_ids", SU[1])  # fmt: skip
    again = im.record(
        reg,
        EXP,
        "cand-1",
        pl,
        b,
        None,
        assessed(promo_ledger(GOOD), ASSESSOR, at, "v-2"),
    )
    assert again.state is St.AWAITING_REVIEW and again.may_follow(thin)
    assert again.budget_reservation_id == thin.budget_reservation_id
    # The first holdout read is consumed by a trial missing a metric: a second read
    # with full metrics is holdout reuse, so it is never scored or accepted.
    two = plan(max_promotion_accesses=2)
    reg2 = registry(proto(sandbox_profile_id="sbx-synth",
                          plan_digest=im.plan_digest(two)))  # fmt: skip
    key = cand().trial_key
    lg, _ = ask(ledger(two), 1, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR,
                candidate=key)  # fmt: skip
    partial = {k: v for k, v in GOOD.items() if k != "unrecorded_trials"}
    lg = lg.record(trial(1, tl.Split.PROMOTION, family=EXP, metrics=partial,
                         material_hash=key.material_hash,
                         config_hash=key.config_hash))  # fmt: skip
    b2 = scheduled(reg2)
    first = im.record(reg2, EXP, "cand-1", two, b2, None,
                      assessed(lg, ASSESSOR, at, "v-1"))  # fmt: skip
    assert "insufficient:metric_missing:unrecorded_trials" in first.reasons
    lg, _ = ask(lg, 2, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR, candidate=key)
    lg = lg.record(trial(2, tl.Split.PROMOTION, family=EXP, metrics=GOOD,
                         material_hash=key.material_hash,
                         config_hash=key.config_hash))  # fmt: skip
    second = im.record(reg2, EXP, "cand-1", two, b2, None,
                       assessed(lg, ASSESSOR, at, "v-2"))  # fmt: skip
    assert second.state is St.INSUFFICIENT_EVIDENCE and second.may_follow(first)
    assert second.reasons == first.reasons  # the second read's metrics are unused


def test_reassessment_never_resets_the_budget_or_changes_the_plan() -> None:
    reg, pl = registry(proto(sandbox_profile_id="sbx-synth")), plan()
    b = scheduled(reg)
    at = AT + timedelta(days=1)
    thin = im.record(reg, EXP, "cand-1", pl, b, None,
                     assessed(ledger(), ASSESSOR, at, "v-1"))  # fmt: skip
    released = b.release("bud-exp-1")
    late = im.record(
        reg,
        EXP,
        "cand-1",
        pl,
        released,
        None,
        assessed(promo_ledger(GOOD), ASSESSOR, at, "v-2"),
    )
    assert (late.state, late.decision) == (St.BLOCKED, im.Decision.BLOCKED)
    assert "blocked:budget_not_reserved" in late.reasons and late.may_follow(thin)
    _, out = im.schedule(reg, (EXP,), released, at)  # never re-reserved
    assert out == (im.Deferred(EXP, "reservation_closed"),)
    two = plan(max_promotion_accesses=2)  # more data needs a new plan ...
    with pytest.raises(im.ImprovementError, match="test_access_changed"):
        im.record(reg, EXP, "cand-1", two, b)
    with pytest.raises(im.ImprovementError, match="protocol_frozen"):
        reg.register(
            proto(sandbox_profile_id="sbx-synth", plan_digest=im.plan_digest(two))
        )  # ... a new experiment
    moved = replace(thin, protocol_id="e" * 64, verdict_id="v-3")
    assert not moved.may_follow(thin)


def test_records_stored_under_0_5_0_stay_readable() -> None:
    recs = states()
    old = (SU[0], "unavailable:parent_optimizer_version")
    legacy = [
        replace(recs[St.INSUFFICIENT_EVIDENCE], state=St.FAILED, reasons=(
            SU[0], "insufficient:no_qualifying_trial", "none:evaluation_receipt_ids",
            "unavailable:parent_optimizer_version")),
        replace(recs[St.PROPOSED], reasons=old),
        replace(recs[St.BLOCKED], state=St.PROPOSED, reasons=(
            "blocked:budget_not_reserved", *old)),
    ]  # fmt: skip
    failed, proposed, was_blocked = legacy
    assert not any(
        replace(recs[s], verdict_id="v-9").may_follow(failed)
        for s in ALLOWED
        if recs[s].verdict_id
    )  # failed stays final
    assert recs[St.AUTHORIZED].may_follow(proposed)
    for new in recs.values():  # never a 0.5.0 record after a 0.6.0 one
        assert not proposed.may_follow(new) and not failed.may_follow(new)
    assert replace(recs[St.BLOCKED], verdict_id="v-2").may_follow(was_blocked)
    for forged in ({"state": St.BLOCKED}, {"state": St.FAILED, "decision": None,
                                           "verdict_id": None}):  # fmt: skip
        with pytest.raises(im.ImprovementError, match="record_inconsistent"):
            replace(proposed, **forged)
    with pytest.raises(im.ImprovementError, match="record_inconsistent"):
        replace(
            failed,
            reasons=tuple(
                sorted(
                    {*failed.reasons} - {"unavailable:parent_optimizer_version"}
                    | {SU[1]}
                )
            ),
        )  # mixed forms


def test_stored_wire_forms_round_trip_with_unchanged_hashes() -> None:
    p = proto(max_duration=timedelta(seconds=90, milliseconds=500),
              mutation_space_hash="e" * 64)  # fmt: skip
    w = json.loads(json.dumps(p.wire()))  # as stored in jsonb
    assert im.Protocol.from_wire(w).content_hash == p.content_hash
    with pytest.raises(im.ImprovementError, match="protocol_wire"):  # not canonical
        im.Protocol.from_wire(w | {"max_duration": "90.50"})
    with pytest.raises(im.ImprovementError, match="approvals"):  # still validated
        im.Protocol.from_wire(w | {"approvals": []})
    use = Use(TENANT, EXP, Purpose.PERSONAL_ADAPTATION, Access.DEVELOPMENT,
              None, ("rec-1",), ("consent-1",), T0)  # fmt: skip
    c = cand(evidence_uses=(use,))
    back = im.Candidate.from_wire(json.loads(json.dumps(c.wire())))
    assert back.content_hash == c.content_hash and back.evidence_uses == (use,)
    lg = promo_ledger(GOOD)
    assert lg.checkpoint(3) == digest([tl.entry_wire(e) for e in lg.entries])
    assert lg.checkpoint(1) == ledger().checkpoint(1) != lg.checkpoint(2)


GOLDEN_PROTOCOL = "c8e22cf9a7f6eb41061d00db947f6707d294e65a9849fa6604bfad751582b1b3"


def test_decision_19_retest_of_is_a_frozen_link_to_another_experiment() -> None:
    # pinned on base b5499fa, whose fixture plan froze the protocol hash "e" * 64
    old = proto(plan_digest=im.plan_digest(plan(protocol_hash="e" * 64)))
    assert old.content_hash == GOLDEN_PROTOCOL
    assert "retest_of" not in proto().wire()  # absent: earlier hashes unchanged
    p = proto(retest_of="exp-synth-0")
    assert p.wire()["retest_of"] == "exp-synth-0" and p.content_hash != GOLDEN_PROTOCOL
    back = im.Protocol.from_wire(json.loads(json.dumps(p.wire())))
    assert (back.retest_of, back.content_hash) == ("exp-synth-0", p.content_hash)
    for bad in (EXP, "", "bad id"):  # never itself, always an id
        with pytest.raises(im.ImprovementError, match="retest_of"):
            proto(retest_of=bad)
    reg = im.ExperimentRegistry(TENANT).register(p)
    with pytest.raises(im.ImprovementError, match="protocol_frozen"):
        reg.register(proto())  # the link is part of the frozen protocol


def test_decision_19_chain_trials_count_every_experiment_of_the_chain() -> None:
    def dev(n: int, family: str) -> tl.Trial:  # failed: counted, never discarded
        return trial(n, tl.Split.DEVELOPMENT, tl.Outcome.FAILED, family=family,
                     trial_id=f"trial-{n}", access_id=None)  # fmt: skip

    lg = ledger()
    for n, family in ((1, "exp-2"), (2, "exp-2"), (3, "exp-1")):
        lg = lg.record(dev(n, family))
    chain = ("exp-2", "exp-1", "exp-0")  # exp-2 re-tests exp-1, which re-tests exp-0
    # per experiment, the larger of the ledger's and the stored count:
    # exp-2 max(2, 0) + exp-1 max(1, 3) + exp-0 max(0, 2) = 2 + 3 + 2 = 7
    assert im.chain_trials(lg, chain, {"exp-1": 3, "exp-0": 2}) == 7
    assert im.chain_trials(lg, chain, {"exp-1": 0}) == 2 + 1 + 0
    assert im.chain_trials(lg, chain[:1], {}) == 2  # no link: its own family only
    for bad_chain, stored in ((("exp-2", "exp-2"), {}), ((), {}),
                              (chain, {"exp-0": -1})):  # fmt: skip
        with pytest.raises(im.ImprovementError, match="retest_chain"):
            im.chain_trials(lg, bad_chain, stored)


# Owner decisions 35 to 37: a strategy promotion must clear "excess over baseline,
# CI lower bound >= 0" recomputed at z' = Phi^-1(1 - alpha / (2 k)), alpha =
# 2 (1 - Phi(z)) from the plan's frozen z, k = chain_trials; for the claim's
# baseline, else every declared baseline. Other targets only flag the count.
Z_995 = Decimal("2.5758293035489004")  # Phi^-1(0.995), a published quantile


def promo_on(sp: tl.SplitPlan, metrics: dict[str, Decimal]) -> tl.TrialLedger:
    key = cand().trial_key
    lg, _ = ask(ledger(sp), 1, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR, None, key)
    return lg.record(trial(1, tl.Split.PROMOTION, family=EXP, metrics=metrics,
                           material_hash=key.material_hash,
                           config_hash=key.config_hash,
                           protocol_hash=sp.protocol_hash))  # fmt: skip


def failed_dev(lg: tl.TrialLedger, n: int) -> tl.TrialLedger:
    for i in range(2, n + 2):  # recorded after the promotion trial, in EXP
        lg = lg.record(trial(i, tl.Split.DEVELOPMENT, tl.Outcome.FAILED, family=EXP,
                             trial_id=f"trial-d{i}", access_id=None))  # fmt: skip
    return lg


def test_decision_35_the_widened_bound_turns_a_pass_into_a_rejection() -> None:
    # bl-hold: excess mean 0.01, se 0.004. Unadjusted (z = 1.959963984540054):
    # 0.01 - 0.0078398559 = 0.0021601441 >= 0, a pass. With k = 5 trials:
    # alpha = 0.05, alpha / (2 k) = 0.005, z' = Phi^-1(0.995) = 2.5758293035489:
    # 0.01 - 0.0103033172 = -0.0003033172 < 0, a rejection. bl-cash: 0.02 and 0.002
    # clear both (0.02 - 0.0051516586 = 0.0148483414).
    m = GOOD | {"baseline.bl-hold.excess_mean": Decimal("0.01"),
                "baseline.bl-hold.excess_std_error": Decimal("0.004")}  # fmt: skip
    one = assess(registry(), promo_ledger(m))  # k = 1: z' = z, the plain bound
    assert one.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert abs(one.adjustment["z_widened"] - EVAL.z) < Decimal("1e-14")
    assert abs(one.adjustment["baseline.bl-hold.excess_lower_widened"]
               - Decimal("0.0021601441")) < Decimal("1e-10")  # fmt: skip
    for v in (
        assess(registry(), promo_ledger(m), chain_trials=5),
        assess(registry(), failed_dev(promo_ledger(m), 4)),
    ):  # 1 + 4 trials
        assert (v.decision, v.reasons) == (
            im.Decision.REJECT,
            ("bonferroni:baseline.bl-hold",),
        )
        a = v.adjustment
        assert a["chain_trials"] == 5 and abs(a["z_widened"] - Z_995) < Decimal(
            "1e-12")  # fmt: skip
        assert abs(a["baseline.bl-hold.excess_lower_widened"]
                   - Decimal("-0.0003033172")) < Decimal("1e-10")  # fmt: skip
        assert abs(a["baseline.bl-cash.excess_lower_widened"]
                   - Decimal("0.0148483414")) < Decimal("1e-10")  # fmt: skip
        assert v.wire()["adjustment"]["chain_trials"] == "5"  # type: ignore[index]
        with pytest.raises(TypeError, match="Decimals"):  # never a float or an int
            replace(v, adjustment={"z_widened": 2})  # type: ignore[dict-item]
    # a retest's own ledger cannot know the chain: the store must supply it
    linked = registry(proto(retest_of="exp-synth-0"))
    v = assess(linked, promo_ledger(GOOD))
    assert (v.decision, v.reasons) == (
        im.Decision.INSUFFICIENT,
        ("chain_trials_unknown",),
    )
    assert assess(linked, promo_ledger(GOOD), chain_trials=3).decision is (
        im.Decision.ACCEPT_FOR_REVIEW)  # fmt: skip
    for bad in (-1, Decimal(2)):
        with pytest.raises(im.ImprovementError, match="chain_trials"):
            assess(registry(), promo_ledger(GOOD), chain_trials=bad)
    v = assess(registry(), promo_ledger(GOOD), chain_trials=0)  # cannot be: the
    assert v.reasons == ("chain_trials_unknown",)  # scored trial is one of them


def test_decision_35_widened_z_at_two_and_twenty_trials() -> None:
    # z' = Phi^-1(1 - 0.05 / (2 k)) from z = 1.959963984540054 (statistics.NormalDist
    # agrees to 1e-14): k = 2: 2.241402727604945, k = 20: 3.023341439739147
    for k, z in ((2, "2.241402727604945"), (20, "3.023341439739147")):
        v = assess(registry(), promo_ledger(GOOD), chain_trials=k)
        assert abs(v.adjustment["z_widened"] - Decimal(z)) < Decimal("1e-12"), k


def test_review_m1_a_strategy_record_needs_its_bound_protocol_first() -> None:
    # derive refuses before any verdict exists, so the store spends no holdout read
    reg, at = registry(), AT + timedelta(days=1)
    for ev, why in (
        (None, "evaluation_protocol_missing"),
        (evaluation("plan-other"), "evaluation_protocol_unbound"),
    ):
        run = im.Assessment(promo_ledger(GOOD), ASSESSOR, at, "v-1", evaluation=ev)
        with pytest.raises(im.ImprovementError, match=why):
            im.derive(reg, EXP, "cand-1", plan(), scheduled(reg), None, run)
    r = im.Target.RESEARCH  # other targets have no evaluation protocol to bind
    other = im.ExperimentRegistry(TENANT).register(proto(
        target=r, sandbox_profile_id="sbx-synth")).register_candidate(
        cand(target=r))  # fmt: skip
    good = {"net_return": Decimal("0.02"), "unsupported_material_facts": Decimal(0)}
    run = im.Assessment(promo_ledger(good), ASSESSOR, at, "v-1")
    assert im.derive(other, EXP, "cand-1", plan(), scheduled(other), None, run)[1]


def test_decision_37_the_claims_baseline_else_every_declared_one() -> None:
    m = GOOD | {"baseline.bl-cash.excess_mean": Decimal("-0.01")}  # cash fails
    v = assess(registry(), promo_ledger(m))  # NET_MEAN claim names no baseline
    assert v.reasons == ("bonferroni:baseline.bl-cash",)
    claim = DeclaredClaim(ClaimMetric.EXCESS_MEAN, ClaimDirection.POSITIVE, "bl-hold")
    named = replace(EVAL, claim=claim)
    sp = plan(protocol_hash=named.content_hash)
    reg = registry(proto(plan_digest=im.plan_digest(sp)))
    v = assess(reg, promo_on(sp, m), evaluation=named)  # only bl-hold is tested
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert set(v.adjustment) == {
        "chain_trials",
        "z_widened",
        "baseline.bl-hold.excess_lower_widened",
    }


def test_decision_35_missing_or_unbound_inputs_are_insufficient() -> None:
    lg = promo_ledger(GOOD)
    no_se = {k: v for k, v in GOOD.items() if k != "baseline.bl-cash.excess_std_error"}
    cases: list[tuple[dict[str, object], tl.TrialLedger, str]] = [
        ({"evaluation": None}, lg, "evaluation_protocol_missing"),
        ({"evaluation": replace(EVAL, z=Decimal("2.5"))}, lg,
         "evaluation_protocol_unbound"),  # not the hash the plan froze
        ({"evaluation": evaluation("plan-other")}, lg, "evaluation_protocol_unbound"),
        ({}, promo_ledger(no_se), "metric_missing:baseline.bl-cash.excess_std_error"),
        ({}, promo_ledger(GOOD | {"ci_z": Decimal("2.5")}), "z_mismatch"),
    ]  # fmt: skip
    for kw, ledger_, why in cases:
        v = assess(registry(), ledger_, **kw)
        assert (v.decision, v.reasons) == (im.Decision.INSUFFICIENT, (why,)), why
        assert dict(v.adjustment) == {} and "adjustment" not in v.wire()


def test_decision_36_count_based_targets_flag_the_missing_adjustment() -> None:
    r = im.Target.RESEARCH
    good = {"net_return": Decimal("0.02"), "unsupported_material_facts": Decimal(0)}
    reg = im.ExperimentRegistry(TENANT).register(proto(target=r)).register_candidate(
        cand(target=r))  # fmt: skip
    v = assess(reg, promo_ledger(good), evaluation=None)  # no z, no excess CI
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert "no_multiple_testing_adjustment" in v.limitations
    assert dict(v.adjustment) == {} and "adjustment" not in v.wire()
    s = assess(registry(), promo_ledger(GOOD))  # the strategy rule was applied
    assert "no_multiple_testing_adjustment" not in s.limitations


def test_the_fixture_plan_freezes_test_backtests_protocol() -> None:
    assert EVAL.content_hash == bt_protocol().content_hash  # one SYNTHETIC protocol
