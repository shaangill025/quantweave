"""Strategy improvement path: bounded parameter/ranking candidates inside a frozen
search space, full trial accounting, prospective review and an adoption-candidate
artifact that grants nothing (T047 increment 1).

All data is SYNTHETIC; expected values are hand-written.
"""

import ast
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.research_data import EvidenceClass
from qw_domain.shadow import ShadowBook, open_run, score
from qw_domain.strategy_registry import (
    AdoptionState,
    EvidenceState,
    StrategyManifest,
    StrategyRegistry,
    status,
)
from qw_domain.trial_ledger import Candidate, Outcome, Role, Split, TrialLedger
from test_backtest import PX, protocol, scripted
from test_improvement import ASSESSOR, EXP, GOOD, PRODUCER, card, proto, scheduled
from test_research_data import iid, rights
from test_shadow import GRID, LATE, LIVE, dec, series
from test_strategy_registry import AT, consent, manifest
from test_trial_ledger import DS, PROMO, T0, TENANT, TRAIN, ask, plan, trial

D = Decimal
PROSPECTIVE = EvidenceClass.PROSPECTIVE
H = timedelta(hours=1)
BASE = manifest()  # SYNTHETIC swing momentum 0.1.0: lookback 126, band 0.05, weekly
DIMS = (si.Dimension("lookback", 60, 200),
        si.Dimension("review", allowed=("weekly", "monthly")))  # fmt: skip
SPACE = si.SearchSpace("space-synth-1", BASE.strategy_id, "0.1.0",
                       BASE.material_hash, DIMS, 3, 2)  # fmt: skip
EVAL = protocol()
PLAN = plan(protocol_hash=EVAL.content_hash)
DERIVED = si.derive(BASE, SPACE, {"lookback": 63}, "cand-1")
STRATS = StrategyRegistry().register(BASE).register(DERIVED)


def iproto(**kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "baseline_version": "0.1.0", "baseline_hash": BASE.material_hash,
        "plan_digest": im.plan_digest(PLAN),
        "mutation_space_hash": SPACE.content_hash,
    } | kw  # fmt: skip
    return proto(**args)


def study(p: im.Protocol | None = None) -> si.StrategyStudy:
    return si.StrategyStudy(p or iproto(), SPACE, EVAL, BASE)


def cand(derived: StrategyManifest = DERIVED, **kw: object) -> im.Candidate:
    args: dict[str, object] = {
        "candidate_id": derived.version, "experiment_id": EXP, "parent_id": "0.1.0",
        "producer": PRODUCER, "runtime_versions": {"engine": "v1"},
        "created_at": T0 + timedelta(minutes=5),
    } | kw  # fmt: skip
    return si.strategy_candidate(BASE, SPACE, derived, **args)  # type: ignore[arg-type]


CAND = cand()


def reg() -> im.ExperimentRegistry:
    return im.ExperimentRegistry(TENANT).register(iproto()).register_candidate(CAND)


def dev_ledger() -> TrialLedger:
    """Plan at T0; trial-1 (TRAIN) for cand-1 granted at T0+1h, recorded at T0+2h."""
    st, lg = study(), TrialLedger(TENANT).register_plan(PLAN, DS)
    lg, a = si.request_research_access(
        st, lg, CAND, "acc-1", "trial-1", Split.TRAIN, TRAIN, "res-synth", T0 + H
    )
    assert a.granted
    lg, out = si.develop(st, reg(), STRATS, lg, "cand-1", "acc-1", "trial-1",
                         T0 + 2 * H, DS, rights(), PX, lambda cfg: scripted(["1"] * 5),
                         {"px-synth-1": PX})  # fmt: skip
    assert out.trial.outcome is Outcome.COMPLETED, out.reason
    return lg


def research(lg: TrialLedger, n: int, trial_id: str | None = None) -> TrialLedger:
    """A granted TRAIN access acc-n for cand-1 at T0 + n hours."""
    return si.request_research_access(study(), lg, CAND, f"acc-{n}",
                                      trial_id or f"trial-{n}", Split.TRAIN, TRAIN,
                                      "res-synth", T0 + n * H)[0]  # fmt: skip


def rec(lg: TrialLedger, n: int, out: Outcome = Outcome.COMPLETED,
        **kw: object) -> TrialLedger:  # fmt: skip
    """Record trial-n of cand-1's key (any family/outcome) on the plan."""
    k = CAND.trial_key
    args: dict[str, object] = {"family": EXP, "metrics": {},
        "material_hash": k.material_hash, "config_hash": k.config_hash,
        "protocol_hash": EVAL.content_hash} | kw  # fmt: skip
    return lg.record(trial(n, Split.TRAIN, out, **args))


def promoted(metrics: dict[str, Decimal], lg: TrialLedger | None = None) -> TrialLedger:
    """The assessor's single holdout read (acc-9 at T0+9h, trial-9 at T0+9h30)."""
    k = CAND.trial_key
    lg, _ = ask(lg or dev_ledger(), 9, Split.PROMOTION, PROMO, Role.ASSESSOR, None, k)
    return lg.record(trial(9, Split.PROMOTION, family=EXP, metrics=metrics,
                           material_hash=k.material_hash, config_hash=k.config_hash,
                           protocol_hash=EVAL.content_hash))  # fmt: skip


def prospective(lg: TrialLedger, n: int = 3, **kw: object) -> si.Prospective:
    args: dict[str, object] = {
        "registry": STRATS, "ledger": lg, "run_id": "shadow-synth-1",
        "strategy_id": BASE.strategy_id, "version": "cand-1", "config": {},
        "trial_id": "trial-1", "protocol": EVAL, "instrument": iid(1),
        "min_scored": 2, "max_capture_delay": timedelta(minutes=5), "grid": GRID,
        "registered_at": T0 + 10 * H,
    } | kw  # fmt: skip
    b = ShadowBook(open_run(**args))  # type: ignore[arg-type]
    for d in (dec(1, 0, "1"), dec(2, 1, "0"), dec(3, 2, "1"))[:n]:
        b = b.capture(d)
    return si.Prospective(b, LIVE, {"px-synth-1": LIVE}, b.run.declaration_hash)


def judge(lg: TrialLedger, pro: si.Prospective | None,
          r: im.ExperimentRegistry | None = None) -> si.StrategyAssessment:  # fmt: skip
    r = r or reg()
    return si.assess_strategy(study(), r, STRATS, lg, scheduled(r), ASSESSOR, LATE,
                              "verdict-1", "cand-1", pro)  # fmt: skip


def test_search_space_is_a_bounded_subset_of_the_registered_version() -> None:
    for name in ("max_risk", "policy_weight", "holdout_days", "thresholds"):
        with pytest.raises(im.ImprovementError, match="protected_dimension"):
            si.Dimension(name, 1, 2)
    with pytest.raises(TypeError):
        si.Dimension("band", 0.01, 0.1)  # type: ignore[arg-type]
    with pytest.raises(im.ImprovementError, match="dimension"):
        si.Dimension("lookback", 200, 60)
    with pytest.raises(im.ImprovementError, match="max_trials"):
        replace(SPACE, max_trials=0)
    bad = {
        "dimension_bounds": (si.Dimension("lookback", 10, 200),),  # manifest min 20
        "unknown_parameter": (si.Dimension("leverage", 1, 2),),
    }
    for code, dims in bad.items():
        with pytest.raises(im.ImprovementError, match=code):
            replace(SPACE, dimensions=dims).check_base(BASE)
    with pytest.raises(im.ImprovementError, match="dimension_bounds"):
        replace(SPACE, dimensions=(si.Dimension("review", allowed=("daily",)),)
                ).check_base(BASE)  # fmt: skip
    other = replace(BASE, implementation_hash="9" * 64)
    with pytest.raises(im.ImprovementError, match="space_mismatch"):
        SPACE.check_base(other)


def test_candidate_changes_only_declared_defaults_and_gets_a_new_material_hash() -> (
    None
):
    assert si.version_diff(BASE, DERIVED) == {"lookback": ["126", "63"]}
    assert DERIVED.material_hash != BASE.material_hash
    assert DERIVED.implementation_hash == BASE.implementation_hash
    assert (CAND.artifact_hash, CAND.payload_hash) == (
        DERIVED.material_hash, DERIVED.config_hash({}))  # fmt: skip
    outside: list[dict[str, object]] = [
        {"band": D("0.1")}, {"lookback": 250}, {"review": "daily"}, {"lookback": 63.0}
    ]  # fmt: skip
    for values in outside:
        with pytest.raises(im.ImprovementError, match="outside_search_space"):
            si.derive(BASE, SPACE, values, "cand-x")  # type: ignore[arg-type]
    with pytest.raises(im.ImprovementError, match="no_change"):
        si.derive(BASE, SPACE, {"lookback": 126}, "cand-x")
    wider = replace(DERIVED.parameters[0], maximum=300)
    for forged in (
        replace(DERIVED, implementation_hash="9" * 64),
        replace(DERIVED, regular_session_only=False),
        replace(DERIVED, parameters=(wider, *DERIVED.parameters[1:])),
    ):
        with pytest.raises(im.ImprovementError, match="outside_search_space"):
            cand(forged)
    with pytest.raises(im.ImprovementError, match="version_mismatch"):
        cand(candidate_id="cand-2")
    # relabelling the same defaults under a new id is not a new candidate
    same = si.derive(BASE, SPACE, {"lookback": 63}, "cand-2")
    with pytest.raises(im.ImprovementError, match="identical_artifact"):
        reg().register_candidate(cand(same))


def test_study_requires_the_search_space_frozen_in_the_protocol() -> None:
    for p, code in ((iproto(mutation_space_hash=None), "search_space_not_frozen"),
                    (iproto(mutation_space_hash="0" * 64), "search_space_not_frozen"),
                    (iproto(baseline_hash="0" * 64), "baseline_mismatch"),
                    (iproto(plan_id="plan-other"), "plan_mismatch")):  # fmt: skip
        with pytest.raises(im.ImprovementError, match=code):
            study(p)
    r = im.ExperimentRegistry(TENANT).register(iproto())
    wider = replace(SPACE, max_trials=50).content_hash
    with pytest.raises(im.ImprovementError, match=r"protocol_frozen.*mutation_space"):
        r.register(iproto(mutation_space_hash=wider))
    with pytest.raises(im.ImprovementError, match="target"):
        study(
            iproto(
                target=im.Target.RESEARCH,
                scorecard=card(im.Target.RESEARCH),
                approvals=im.MIN_APPROVALS[im.Target.STRATEGY],
            )
        )


def test_every_trial_is_counted_and_the_trial_budget_is_enforced() -> None:
    st, lg = study(), dev_ledger()
    lg = research(research(lg, 2), 3)
    with pytest.raises(im.ImprovementError, match="trial_budget_exhausted"):
        research(lg, 4)
    with pytest.raises(im.ImprovementError, match="assessor_only"):
        si.request_research_access(st, lg, CAND, "acc-5", "trial-5", Split.PROMOTION,
                                   PROMO, "res-synth", T0 + 4 * H)  # fmt: skip
    acct = si.account(st, reg(), lg)
    assert (acct.research_trials, acct.unrecorded, acct.unregistered) == (3, 2, ())
    assert acct.violations == ("hard:unrecorded_trials",)
    for n, out in ((2, Outcome.FAILED), (3, Outcome.ABANDONED)):
        lg = rec(lg, n, out, recorded_at=T0 + 3 * H + n * timedelta(minutes=10))
    acct = si.account(st, reg(), lg)
    assert (acct.research_trials, acct.unrecorded, acct.violations) == (3, 0, ())
    outcomes = [r["outcome"] for r in acct.report.rows]
    assert outcomes == ["completed", "failed", "abandoned"]  # failures retained
    # a variant tried outside the registry is counted and flagged
    ghost = Candidate(EXP, BASE.material_hash, "0" * 64)
    lg, _ = lg.request_access("acc-6", PLAN.plan_id, "trial-6", ghost, Split.TRAIN,
                              TRAIN, None, "res-synth", Role.RESEARCHER,
                              T0 + 6 * H)  # fmt: skip
    acct = si.account(st, reg(), lg)
    assert acct.research_trials == 4 and acct.unregistered == ("trial-6",)
    assert set(acct.violations) == {"hard:unrecorded_trials", "trial_budget_exceeded",
                                    "unregistered_variant_trials"}  # fmt: skip


def test_development_backtest_is_bound_to_the_candidate_version() -> None:
    lg = dev_ledger()
    t = lg.trials(EXP)[0]
    assert (t.material_hash, t.config_hash) == (DERIVED.material_hash,
                                                DERIVED.config_hash({}))  # fmt: skip
    # weights 1, 1, 1 on opens 100, 110, 99, 99: one entry cost 0.002, gross 0
    assert (t.metrics["periods"], t.metrics["total_cost"]) == (D(3), D("0.002"))
    assert t.metrics["gross_mean"] == 0
    st = study()
    lg2, _ = lg.request_access("acc-7", PLAN.plan_id, "trial-7", Candidate(
        EXP, BASE.material_hash, BASE.config_hash({})), Split.TRAIN, TRAIN, None,
        "res-synth", Role.RESEARCHER, T0 + 7 * H)  # fmt: skip
    with pytest.raises(im.ImprovementError, match="candidate_mismatch"):
        si.develop(st, reg(), STRATS, lg2, "cand-1", "acc-7", "trial-7", T0 + 8 * H,
                   DS, rights(), PX, lambda cfg: scripted(["1"] * 5),
                   {"px-synth-1": PX})  # fmt: skip
    seen: list[object] = []

    def build(cfg: object) -> object:
        seen.append(cfg)
        return scripted(["1"] * 5)

    si.develop(st, reg(), STRATS, si.request_research_access(
        st, lg, CAND, "acc-8", "trial-8", Split.TRAIN, TRAIN, "res-synth",
        T0 + 8 * H)[0], "cand-1", "acc-8", "trial-8", T0 + 8 * H, DS, rights(), PX,
        build, {"px-synth-1": PX})  # type: ignore[arg-type]  # fmt: skip
    assert seen == [
        {"lookback": 63, "band": D("0.05"), "review": "weekly", "long_only": True}
    ]  # the candidate's defaults, nothing else


def test_overfit_higher_pnl_candidate_with_violations_is_rejected() -> None:
    """AT029 / §11 strategy negative control (SYNTHETIC)."""
    rich = GOOD | {"net_return": D("0.50"), "lookahead_violations": D(1)}
    lg = promoted(rich)
    a = judge(lg, prospective(lg))
    assert a.decision is im.Decision.REJECT and "hard:lookahead_violations" in a.reasons
    assert a.research_only and a.evidence_ceiling is EvidenceState.NONE
    with pytest.raises(im.ImprovementError, match="not_eligible"):
        r = reg().record_verdict(si.recordable_verdict(a))
        si.adoption_candidate(study(), a, r, STRATS, lg, "adopt-art-1")
    # clean recorded numbers, but variants were tried and never recorded
    lg = dev_ledger()
    lg = promoted(GOOD | {"net_return": D("0.50")}, research(lg, 2))
    a = judge(lg, prospective(lg))
    assert a.verdict.decision is im.Decision.ACCEPT_FOR_REVIEW  # T044 alone passes
    assert a.decision is im.Decision.REJECT
    rec = si.recordable_verdict(a)  # what must be recorded: T047's decision
    assert (rec.decision, rec.reasons) == (a.decision, a.reasons)
    assert a.reasons == ("hard:unrecorded_trials",)


def test_without_a_completed_prospective_window_it_is_research_only() -> None:
    lg = promoted(GOOD)
    cases = {
        "prospective_review_missing": None,
        "prospective_below_declared_minimum": prospective(lg, n=1),
        "prospective_minimum_below_study": prospective(lg, min_scored=1),
    }
    for reason, pro in cases.items():
        a = judge(lg, pro)
        assert a.verdict.decision is im.Decision.ACCEPT_FOR_REVIEW
        assert a.decision is im.Decision.INSUFFICIENT and reason in a.reasons
        assert a.research_only and a.evidence_ceiling is EvidenceState.NONE
    p = prospective(lg)
    for change, reason in (
        ({"registered_at": T0}, "prospective_before_freeze"),  # before the candidate
        ({"protocol": protocol(fill_lag_bars=2)}, "prospective_protocol_changed"),
    ):  # hand-built runs that bypass open_run
        run = replace(p.book.run, **change)
        b = ShadowBook(run, p.book.entries if "registered_at" in change else ())
        a = judge(lg, si.Prospective(b, LIVE, p.baseline_series, run.declaration_hash))
        assert reason in a.reasons and a.decision is im.Decision.INSUFFICIENT
    outsider = replace(ASSESSOR, actor_id="actor-other")
    r = reg()
    a = si.assess_strategy(study(), r, STRATS, lg, scheduled(r), outsider, LATE, "v-2",
                           "cand-1", None)  # fmt: skip
    assert a.decision is im.Decision.BLOCKED and a.research_only
    short = judge(lg, prospective(lg, n=1))  # a report exists, below its minimum
    with pytest.raises(im.ImprovementError, match="not_eligible"):
        r2 = r.record_verdict(si.recordable_verdict(short))
        si.adoption_candidate(study(), short, r2, STRATS, lg, "adopt-art-1")
    with pytest.raises(im.ImprovementError, match="study_mismatch"):
        si.assess_strategy(
            study(iproto(risk_policy_hash="1" * 64)),
            r,
            STRATS,
            lg,
            scheduled(r),
            ASSESSOR,
            LATE,
            "v-3",
            "cand-1",
            p,
        )
    a = judge(lg, replace(p, declared_hash="0" * 64))
    assert "prospective_invalid:declaration_mismatch" in a.reasons
    other = si.derive(BASE, SPACE, {"review": "monthly"}, "cand-2")
    strats = STRATS.register(other)
    k = cand(other, parent_id="0.1.0").trial_key
    lg2, _ = lg.request_access("acc-10", PLAN.plan_id, "trial-10", k, Split.TRAIN,
                               TRAIN, None, "res-synth", Role.RESEARCHER,
                               T0 + 10 * H)  # fmt: skip
    lg2 = lg2.record(
        trial(
            10,
            Split.TRAIN,
            family=EXP,
            material_hash=k.material_hash,
            config_hash=k.config_hash,
            protocol_hash=EVAL.content_hash,
        )
    )
    r = reg().register_candidate(cand(other))
    a = judge(
        lg2,
        prospective(
            lg2,
            registry=strats,
            version="cand-2",
            trial_id="trial-10",
            registered_at=T0 + 11 * H,
        ),
        r,
    )
    assert "prospective_not_candidate" in a.reasons and a.research_only


def test_eligible_candidate_yields_an_adoption_artifact_that_grants_nothing() -> None:
    lg = promoted(GOOD)
    a = judge(lg, prospective(lg))
    assert a.decision is im.Decision.ACCEPT_FOR_REVIEW and a.reasons == ()
    assert a.adoption_eligible and a.evidence_ceiling is EvidenceState.LIMITED
    assert not a.significance_claimed and "synthetic_only" in a.limitations
    # SYNTHETIC bars labelled prospective: the synthetic holdout still caps the ceiling
    fwd = series({0: "100", 1: "110", 2: "99", 3: "99", 4: "108.9"}, PROSPECTIVE)
    p = prospective(lg)
    a2 = judge(lg, replace(p, series=fwd, baseline_series={"px-synth-1": fwd}))
    assert a2.prospective is not None
    assert a2.prospective.evidence_ceiling is EvidenceState.PROSPECTIVE_LIMITED
    assert a2.evidence_ceiling is EvidenceState.LIMITED
    with pytest.raises(im.ImprovementError, match="verdict_not_recorded"):
        si.adoption_candidate(study(), a, reg(), STRATS, lg, "adopt-art-1")
    r = reg().record_verdict(si.recordable_verdict(a))
    art = si.adoption_candidate(study(), a, r, STRATS, lg, "adopt-art-1")
    assert art["diff"] == {"lookback": ["126", "63"]}
    assert art["candidate_version"] == "cand-1" and art["base_version"] == "0.1.0"
    assert art["protocol_hash"] == iproto().content_hash
    assert art["search_space_hash"] == SPACE.content_hash
    assert art["risk_policy_hash"] == "f" * 64
    assert art["required_approvals"] == ["maintainer_release", "operator_install",
                                         "user_strategy_adoption"]  # fmt: skip
    assert art["granted_approvals"] == [] and art["installs"] is False
    assert art["adopts"] is False and art["next_state"] == "awaiting_human_review"
    trials = art["trials"]
    assert isinstance(trials, dict) and trials["trial_count"] == 2
    assert [row["trial_id"] for row in trials["rows"]] == ["trial-1", "trial-9"]
    verdict = art["verdict"]
    assert isinstance(verdict, dict) and verdict["scores"]["net_return"] == "0.02"
    pro = art["prospective"]
    assert isinstance(pro, dict) and pro["counts"]["scored"] == 3
    assert art["significance_claimed"] is False and art["evidence_ceiling"] == "limited"
    assert art["content_hash"] == si.adoption_candidate(
        study(), a, r, STRATS, lg, "adopt-art-1")["content_hash"]  # fmt: skip
    # a trial recorded after the verdict invalidates the bundle
    later = research(lg, 11)
    with pytest.raises(im.ImprovementError, match="trials_changed"):
        si.adoption_candidate(study(), a, r, STRATS, later, "adopt-art-1")
    # R034: an existing user's adoption of the base does not carry to the candidate
    users = STRATS.adopt(TENANT, BASE.strategy_id, "0.1.0", {}, consent())
    assert status(users, TENANT, BASE.strategy_id, "0.1.0", {}, AT).adoption is (
        AdoptionState.CURRENT)  # fmt: skip
    assert status(users, TENANT, BASE.strategy_id, "cand-1", {}, AT).adoption is (
        AdoptionState.STALE)  # fmt: skip


def test_module_never_adopts_installs_or_records_approvals() -> None:
    src = Path(si.__file__).read_text()
    tree = ast.parse(src)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"adopt", "qualify", "record_verdict", "register",
                        "register_candidate", "reserve"}  # fmt: skip
    defs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not any(
        w in d
        for d in defs
        for w in ("install", "approve", "promote", "sign", "order", "submit")
    )
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               for a in n.names}  # fmt: skip
    assert "StrategyConsent" not in imports and "ApprovalClass" not in imports


def test_review_round_1_trials_on_the_plan_in_another_family_are_counted() -> None:
    """S1: the plan's trials count whatever family label they carry."""
    st, lg, k = study(), dev_ledger(), CAND.trial_key
    other = Candidate("other-family", k.material_hash, k.config_hash)
    for i in (2, 3):
        lg, _ = lg.request_access(f"acc-{i}", PLAN.plan_id, f"trial-{i}", other,
                                  Split.TRAIN, TRAIN, None, "res-synth",
                                  Role.RESEARCHER, T0 + i * H)  # fmt: skip
    acct = si.account(st, reg(), lg)
    assert (acct.research_trials, acct.unrecorded, acct.trial_count) == (3, 2, 3)
    assert set(acct.violations) == {"hard:unrecorded_trials", "foreign_family_trials"}
    with pytest.raises(im.ImprovementError, match="trial_budget_exhausted"):
        research(lg, 4)
    lg = rec(lg, 2, family="other-family", recorded_at=T0 + 3 * H)
    lg = promoted(GOOD, lg)
    a = judge(lg, prospective(lg))
    assert a.decision is im.Decision.REJECT and "foreign_family_trials" in a.reasons
    # N2: a trial id already on the ledger is not reused
    with pytest.raises(im.ImprovementError, match="trial_id_reused"):
        research(dev_ledger(), 5, "trial-1")


def test_review_round_1_the_prospective_run_is_checked_against_the_ledger() -> None:
    """S2: a hand-built run naming an unknown or earlier trial does not qualify."""
    base = {"material_hash": BASE.material_hash, "config_hash": BASE.config_hash({})}
    lg = rec(promoted(GOOD), 13, Outcome.FAILED, access_id=None,
             recorded_at=T0 + 9 * H + timedelta(minutes=45), **base)  # fmt: skip
    p = prospective(lg)
    for change, reason in (
        ({"trial_id": "trial-nope"}, "prospective_trial_unknown"),
        ({"registered_at": T0 + timedelta(minutes=10)}, "prospective_before_trial"),
        ({"trial_id": "trial-13"}, "prospective_trial_not_candidate"),  # baseline's
    ):
        run = replace(p.book.run, **change)
        b = ShadowBook(run, p.book.entries)
        a = judge(lg, si.Prospective(b, LIVE, p.baseline_series, run.declaration_hash))
        assert reason in a.reasons and a.decision is im.Decision.INSUFFICIENT
    # a research trial of the candidate recorded after the run was registered
    later = rec(research(lg, 11, "trial-12"), 12, access_id="acc-11")
    a = judge(later, prospective(later))
    assert "prospective_before_trial" in a.reasons and a.research_only
    # synthetic shadow bars are refused against a non-synthetic holdout
    c = CAND
    _, why = si._prospective(study(), c, lg, p, LATE, holdout_synthetic=False)
    assert "prospective_synthetic_series" in why
    assert si._prospective(study(), c, lg, p, LATE, holdout_synthetic=True)[1] == []


def test_review_round_1_adoption_refuses_caller_built_assessments() -> None:
    """S3/N4/N1/N3: the artifact re-checks the assessment and the recorded verdict."""
    lg = promoted(GOOD)
    good = judge(lg, prospective(lg))
    short = judge(lg, prospective(lg, n=1))
    t044_reject = replace(good.verdict, decision=im.Decision.REJECT, reasons=("x",))
    acc, lim = im.Decision.ACCEPT_FOR_REVIEW, EvidenceState.LIMITED
    pro = good.prospective
    assert pro is not None
    base_key = Candidate(EXP, BASE.material_hash, BASE.config_hash({}))
    other_run = replace(pro.run, candidate=base_key)
    p = prospective(lg)  # S4: re-scored report of a run on no ledger trial, too early
    nope = replace(p.book.run, trial_id="trial-nope", registered_at=T0 + H / 6)
    rescored = score(ShadowBook(nope, p.book.entries), LIVE, LATE, p.baseline_series,
                     nope.declaration_hash)  # fmt: skip
    assert rescored.claim_permitted and rescored.evidence_ceiling is lim
    forgeries = (
        replace(short, decision=acc, reasons=(), evidence_ceiling=lim),
        replace(good, verdict=t044_reject),
        replace(good, reasons=("x",)),
        replace(good, evidence_ceiling=EvidenceState.HISTORICAL),
        replace(good, prospective=replace(pro, run=other_run)),
        replace(good, prospective=rescored),
    )
    for forged in forgeries:  # each passes every other check
        r = reg().record_verdict(si.recordable_verdict(replace(forged, reasons=())))
        with pytest.raises(im.ImprovementError, match="assessment_inconsistent"):
            si.adoption_candidate(study(), forged, r, STRATS, lg, "art-f")
    # a later non-accept verdict for the candidate supersedes the accept
    rec = si.recordable_verdict(good)
    later = replace(rec, verdict_id="verdict-2", decision=im.Decision.REJECT,
                    reasons=("owner_review",))  # fmt: skip
    r = reg().record_verdict(rec).record_verdict(later)
    with pytest.raises(im.ImprovementError, match="verdict_not_recorded"):
        si.adoption_candidate(study(), good, r, STRATS, lg, "art-f")
    for name in ("maxRisk", "riskcap", "max.risk", "a.holdout"):  # N1 normalised
        with pytest.raises(im.ImprovementError, match="protected_dimension"):
            si.Dimension(name, 1, 2)
    with pytest.raises(im.ImprovementError, match="outside_search_space"):  # N3
        cand(replace(DERIVED, title="Approved: risk-free alpha"))
