"""Experiment protocols, candidate lineage, scheduling and the independent assessor
(T044 increment 1). All data is SYNTHETIC; expected values are hand-written.
"""

import ast
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from qw_domain.budget import BudgetBook, BudgetCaps, Category, Status
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.feedback import (
    Access,
    CaseOrigin,
    EvaluationCase,
    ImprovementEvidence,
    Purpose,
    Split,
)
from test_feedback import SUBJ
from test_trial_ledger import DEV, PROMO, T0, TENANT, ask, ledger, plan, trial

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


GOOD = {"net_return": Decimal("0.02"), "lookahead_violations": Decimal(0),
        "survivorship_violations": Decimal(0),
        "unrecorded_trials": Decimal(0)}  # fmt: skip


def assess(reg: im.ExperimentRegistry, lg: tl.TrialLedger, **kw: object) -> im.Verdict:
    args: dict[str, object] = {"registry": reg, "experiment_id": EXP,
        "candidate_id": "cand-1", "ledger": lg, "book": scheduled(reg),
        "assessor": ASSESSOR, "at": AT + timedelta(days=1),
        "verdict_id": "verdict-1"} | kw  # fmt: skip
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


SU = ("evidence:synthetic_only", "unavailable:parent_optimizer_version")


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
    run = im.Assessment(promo_ledger(GOOD), ASSESSOR, AT + timedelta(days=1), "v-1")
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
    assert thin.state is im.RecordState.FAILED
    assert "insufficient:no_qualifying_trial" in thin.reasons
    blocked = im.record(reg, EXP, "cand-1", pl, book(), None, run)
    assert blocked.state is im.RecordState.PROPOSED
    assert "blocked:budget_not_reserved" in blocked.reasons
    for t in im.SANDBOX_TARGETS:  # T007: representable, never past proposed
        p = proto(target=t, sandbox_profile_id="sbx-synth")
        r = im.ExperimentRegistry(TENANT).register(p).register_candidate(
            cand(target=t))  # fmt: skip
        got = im.record(r, EXP, "cand-1", pl, book(), im.Deferred(
            EXP, "sandbox_unqualified"), run)  # fmt: skip
        assert got.state is im.RecordState.PROPOSED and got.decision is (
            im.Decision.BLOCKED)  # fmt: skip
        assert "blocked:sandbox_unqualified" in got.reasons
        optimizer = t is im.Target.RECURSIVE_OPTIMIZER  # its lineage: the parent
        assert got.parent_optimizer_version == ("strat-v0" if optimizer else None)
        sandbox: list[dict[str, object]] = [{"state": im.RecordState.AUTHORIZED},
                                            {"reasons": ()}]  # fmt: skip
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
