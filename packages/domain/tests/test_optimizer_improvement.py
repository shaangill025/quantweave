"""Recursive optimizer improvement (T050 increment 1). All data, keys and proposers
are SYNTHETIC and deterministic; expected values are hand-computed. Records marked
GATE-LIFTED stand in for what T044 `assess`, T051 `prepare_release` and T051
`install` refuse for an optimizer target (T007 sandbox unqualified): `lifted()`
hand-builds the verdict and release, and the `gate_lifted_install` fixture lifts the
install gate. The real path is tested separately and stops at `sandbox_unqualified`.
"""

import ast
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl
from qw_domain import research_improvement as ri
from qw_domain.decimals import UsdBudget as Usd
from test_improvement import ASSESSOR, PRODUCER, book, proto
from test_release import INST, KEY, NO, REL, approval, deployment
from test_research_improvement import BASE, DEV, FIX, FIXTURE
from test_research_improvement import protocol as research_protocol
from test_trial_ledger import T0, TENANT, ledger

LAST = ri.ValueRule.LAST_NUMBER
OP, VAL, DATE = oi.Operator, oi.Operator.VALUE_RULE, oi.Operator.DATE_RULE
FIRST, BEST = oi.Selection.FIRST_IMPROVING, oi.Selection.BEST_OF_BUDGET
V0 = oi.OptimizerConfig("opt-v0", 1, (VAL, DATE), FIRST)
V1_PAYLOAD = {"proposal_budget": "2", "operators": f"{DATE},{VAL}",
              "selection": "first_improving"}  # fmt: skip
V1 = oi.OptimizerConfig.from_payload("opt-v1", V1_PAYLOAD)
EXP = "exp-opt-1"
GOOD = {t.metric: Decimal(2 if t.metric == "dev_gain" else 0)
        for t in oi.scorecard("card-opt").thresholds}  # fmt: skip
LIMITS = oi.Limits(2, 2, Usd("20.00"), Usd("0.50"))
C0, SC_AT, V_AT, RUN = (T0 + timedelta(minutes=m) for m in (10, 20, 1440, 5760))


@dataclass(frozen=True)
class Proposer:  # SYNTHETIC deterministic stand-in for a gateway-backed generator
    payload: dict[str, str]

    def propose(self, current: oi.OptimizerConfig, cycle: oi.CycleReceipt) -> (
        dict[str, str]
    ):  # fmt: skip
        return self.payload


def optimizer_protocol(**kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "experiment_id": EXP, "target": oi.OPT, "scope": im.Scope.INSTALLATION,
        "baseline_version": "opt-v0", "baseline_hash": V0.content_hash,
        "scorecard": oi.scorecard("card-opt"), "sandbox_profile_id": "sandbox-synth",
        "budget_reservation_id": "bud-opt-1",
        "hypothesis": "SYNTHETIC: trying date rules first finds date defects sooner",
    } | kw  # fmt: skip
    return proto(**args)


def registry(p: im.Protocol | None = None) -> im.ExperimentRegistry:
    return im.ExperimentRegistry(TENANT).register(p or optimizer_protocol())


def cycle(cfg: oi.OptimizerConfig, cid: str, reg: im.ExperimentRegistry,
          lg: rl.DeploymentLedger | None = None, history: oi.History = (),
          at: datetime = C0, limits: oi.Limits = LIMITS, synthetic: bool = True) -> (
    oi.CycleReceipt | oi.Blocked
):  # fmt: skip
    return oi.run_cycle(cid, cfg, reg, EXP, lg or deployment(), BASE, DEV, limits,
                        history, at, accept_synthetic_evidence=synthetic)  # fmt: skip


def verify(reg: im.ExperimentRegistry, lg: rl.DeploymentLedger, r0: oi.CycleReceipt,
           r1: oi.CycleReceipt, nxt: im.Candidate | None) -> list[str]:  # fmt: skip
    v = oi.verify_recursion(reg, EXP, lg, r0, r1, BASE, DEV, nxt,
                            accept_synthetic_evidence=True)  # fmt: skip
    assert v.provisional and "receipts_unauthenticated" in v.limitations
    return list(v.reasons)


def v0_cycle() -> oi.CycleReceipt:
    r = cycle(V0, "cycle-v0-1", registry())
    assert isinstance(r, oi.CycleReceipt)
    return r


def self_change(payload: dict[str, str] = V1_PAYLOAD, version: str = "opt-v1",
                reg: im.ExperimentRegistry | None = None) -> (
    tuple[im.ExperimentRegistry, oi.SelfChange]
):  # fmt: skip
    reg = reg or registry()
    sc = oi.propose_self_change("cand-opt-1", version, v0_cycle(), reg, EXP,
                                Proposer(payload), LIMITS, (), SC_AT)  # fmt: skip
    assert isinstance(sc, oi.SelfChange)
    return reg.register_candidate(sc.candidate), sc


def lifted(reg: im.ExperimentRegistry, sc: oi.SelfChange, *, verdict: bool = True,
           assessor: im.Party = ASSESSOR, operator: str = "operator-synth",
           approver: str = "maint-synth", trials: tuple[str, ...] = ("trial-lifted-1",),
           scores: dict[str, Decimal] | None = None,
           verdict_at: datetime = V_AT) -> (
    tuple[im.ExperimentRegistry, rl.DeploymentLedger]
):  # fmt: skip
    """GATE-LIFTED SYNTHETIC: the accept verdict and signed release that T044 and
    T051 refuse to produce for an optimizer target until T007 qualifies."""
    c, p = sc.candidate, reg.protocol(EXP)
    scores = GOOD if scores is None else scores
    v = im.Verdict("verdict-opt-1", EXP, c.candidate_id, c.content_hash,
                   p.content_hash, assessor, im.Decision.ACCEPT_FOR_REVIEW, (),
                   MappingProxyType(scores), trials, (), ("gate_lifted_synthetic",),
                   verdict_at)  # fmt: skip
    if verdict:
        reg = reg.record_verdict(v)
    man = rl.ReleaseManifest("rel-opt-1", oi.COMPONENT, oi.OPT, EXP, c.candidate_id,
        c.content_hash, c.artifact_hash, p.content_hash, v.verdict_id,
        tuple(sorted(p.approvals)), rl.Compatibility(1, 1, 1), None, None,
        p.baseline_version, approval(who=approver))  # fmt: skip
    d = deployment()
    keys = {k: replace(t, principal_id=approver) for k, t in d.keys.items()}
    d = rl.DeploymentLedger(replace(d.deployment, operators=frozenset({operator})),
                            keys)  # fmt: skip
    return reg, d.install("ev-opt-0", rl.sign_release(man, KEY), operator,
                          "stepup-op-0", INST, strategies=NO)  # fmt: skip


@pytest.fixture
def gate_lifted_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """GATE-LIFTED SYNTHETIC: drops T051's `sandbox_unqualified` target gate for the
    optimizer only, so the hand-built release of `lifted()` installs and stays
    runnable. Production keeps the gate (`test_real_gates_...` installs nothing)."""
    real = rl._target_gates

    def gates(target: im.Target, approvals: Collection[str]) -> list[str]:
        out = real(target, approvals)
        return [g for g in out if (target, g) != (oi.OPT, "sandbox_unqualified")]

    monkeypatch.setattr(rl, "_target_gates", gates)


def chain(payload: dict[str, str] = V1_PAYLOAD) -> tuple[
    im.ExperimentRegistry, rl.DeploymentLedger, oi.CycleReceipt, oi.CycleReceipt,
    im.Candidate,
]:  # fmt: skip
    reg, sc = self_change(payload)
    reg, lg = lifted(reg, sc)
    r1 = cycle(sc.config, "cycle-v1-1", reg, lg, (v0_cycle(), sc), RUN)
    assert isinstance(r1, oi.CycleReceipt), r1
    rp = research_protocol(experiment_id="exp-research-2",
        budget_reservation_id="bud-research-2",
        producer=replace(PRODUCER, version=r1.runner_version))  # fmt: skip
    nxt = oi.cycle_candidate("cand-research-1", r1, rp, BASE, RUN) if (
        r1.selected) else None  # fmt: skip
    return reg, lg, v0_cycle(), r1, nxt  # type: ignore[return-value]


def test_optimizer_artifact_is_closed_and_hashed_on_behaviour() -> None:
    assert V1.to_payload() == V1_PAYLOAD and oi.diff(V0, V1) == (
        "operators", "proposal_budget")  # fmt: skip
    assert replace(V1, version_id="opt-v9").content_hash == V1.content_hash
    assert V1.content_hash != V0.content_hash
    # behaviour-preserving relabels share the hash (N3): with one proposal the
    # rule and the later operators are idle; past 3 reachable moves the budget is
    # idle, and a full best-of search does not depend on operator order.
    same = (replace(V0, operators=(VAL,), selection=BEST),
            replace(V0, operators=(VAL,), version_id="opt-v0b"))  # fmt: skip
    assert {c.content_hash for c in same} == {V0.content_hash}
    full = replace(V1, proposal_budget=3, selection=BEST)
    for c in (replace(full, proposal_budget=8), replace(full, operators=(VAL, DATE))):
        assert c.content_hash == full.content_hash
    assert replace(V1, operators=(VAL, DATE)).content_hash != V1.content_hash
    widening = ("acceptance_threshold", "risk_limit", "test_selection",
                "verification_rule", "release_gate", "signing_key", "max_depth",
                "assessor", "monthly_cap", "Scorecard")  # fmt: skip
    for k in widening:
        with pytest.raises(oi.OptimizerError, match="widens_authority"):
            oi.OptimizerConfig.from_payload("opt-x", V1_PAYLOAD | {k: "0"})
    with pytest.raises(oi.OptimizerError, match="not_adaptable"):
        oi.OptimizerConfig.from_payload("opt-x", V1_PAYLOAD | {"temperature": "1"})
    bad = ({"proposal_budget": "9"}, {"proposal_budget": "02"},
           {"proposal_budget": "0"}, {"operators": f"{DATE},{DATE}"},
           {"operators": "retrieval.top_k"}, {"selection": "random"})  # fmt: skip
    for b in bad:
        with pytest.raises(oi.OptimizerError, match="value"):
            oi.OptimizerConfig.from_payload("opt-x", V1_PAYLOAD | b)
    with pytest.raises(oi.OptimizerError, match="value"):
        oi.OptimizerConfig.from_payload("opt-x", {"selection": "first_improving"})
    with pytest.raises(oi.OptimizerError, match="cap_exceeds_owner_budget"):
        oi.Limits(2, 2, Usd("20.01"), Usd("0.50"))


@pytest.mark.usefixtures("gate_lifted_install")
def test_cycles_are_deterministic_and_hand_computed() -> None:
    # DEV: two docs whose Updated line differs from Published; values are first.
    # Base score = dates 0 + values 2 = 2. V0 tries only last_number: 0 + 0.
    r0 = v0_cycle()
    last = replace(BASE, value_rule=ri.ValueRule.LAST_NUMBER)
    assert r0.proposals == (oi.Proposal(VAL, "last_number", last.content_hash, 0),)
    assert r0.selected is None and r0.authority == "baseline:opt-v0"
    assert r0.declared_cost == Usd("0.50") and r0.runner_hash == V0.content_hash
    # V1 tries published first: dates 2 + values 2 = 4 > 2, selected, stop.
    _, _, _, r1, nxt = chain()
    assert r1.proposals == (oi.Proposal(DATE, "published", FIX.content_hash, 4),)
    assert r1.selected == FIX.content_hash and r1.declared_cost == Usd("1.00")
    # best-of-budget spends the budget: first_date reads Period-end (0 + 2 = 2).
    best = replace(V1, proposal_budget=3, selection=BEST)
    out = oi._execute(best, BASE, DEV)
    assert [(p.value, p.score) for p in out[0]] == [
        ("published", 4), ("first_date", 2), ("last_number", 0)]  # fmt: skip
    assert out[1] == FIX.content_hash
    # a tie (published 2 + 0, first_number 0 + 2) keeps the lower hash either way
    tie = replace(BASE, date_rule=ri.DateRule.FIRST_DATE, value_rule=LAST)
    picks = {oi._execute(replace(best, operators=o), tie, DEV)[1] for o in ((DATE, VAL),
             (VAL, DATE))}  # fmt: skip
    assert picks == {
        min(
            replace(tie, date_rule=ri.DateRule.PUBLISHED).content_hash,
            replace(tie, value_rule=ri.ValueRule.FIRST_NUMBER).content_hash,
        )
    }
    with pytest.raises(oi.OptimizerError, match="not_development"):
        oi._execute(V1, BASE, FIXTURE)
    with pytest.raises(oi.OptimizerError, match="duplicate"):
        cycle(V0, "cycle-v0-1", registry(), history=(r0,))
    assert nxt.artifact_hash == FIX.content_hash and nxt.producer.version == "opt-v1"
    assert dict(nxt.runtime_versions) == {"optimizer": V1.content_hash,
                                          "optimizer_cycle": "cycle-v1-1"}  # fmt: skip


def test_self_change_is_proposed_not_stubbed_and_bounded() -> None:
    reg, r0 = registry(), v0_cycle()
    args = ("cand-opt-1", "opt-v1", r0, reg, EXP)
    blocked = oi.propose_self_change(*args, None, LIMITS, (), SC_AT)
    assert blocked == oi.Blocked("self_change", ("generator_not_configured",))
    with pytest.raises(oi.OptimizerError, match="widens_authority"):
        oi.propose_self_change(*args, Proposer(V1_PAYLOAD | {
            "acceptance_threshold": "0"}), LIMITS, (), SC_AT)  # fmt: skip
    with pytest.raises(oi.OptimizerError, match="identical_relabel"):
        oi.propose_self_change(*args, Proposer(V0.to_payload()), LIMITS, (), SC_AT)
    with pytest.raises(oi.OptimizerError, match="time_order"):
        oi.propose_self_change(*args, Proposer(V1_PAYLOAD), LIMITS, (), T0)
    spent = (replace(r0, declared_cost=Usd("19.80")),)
    over = oi.propose_self_change(*args, Proposer(V1_PAYLOAD), LIMITS, spent, SC_AT)
    assert over == oi.Blocked("self_change", ("improvement_budget_exhausted",))
    reg, sc = self_change()
    c = sc.candidate
    assert (c.parent_id, c.artifact_hash, c.producer) == ("opt-v0", V1.content_hash,
                                                          PRODUCER)  # fmt: skip
    assert c.runtime_versions["source_cycle"] == "cycle-v0-1" and sc.diff == (
        "operators", "proposal_budget")  # fmt: skip
    with pytest.raises(im.ImprovementError, match="identical_artifact"):
        reg.register_candidate(replace(c, candidate_id="cand-opt-relabel"))
    with pytest.raises(oi.OptimizerError, match="identical_relabel"):
        oi.propose_self_change("cand-opt-2", "opt-v1b", r0, reg, EXP,
                               Proposer(V1_PAYLOAD), LIMITS, (), SC_AT)  # fmt: skip
    # depth: V1 proposing V2 in a protocol whose baseline is V1 is generation 2.
    p2 = optimizer_protocol(experiment_id="exp-opt-2", baseline_version="opt-v1",
        baseline_hash=V1.content_hash, budget_reservation_id="bud-opt-2",
        producer=replace(PRODUCER, version="opt-v1"))  # fmt: skip
    reg2 = reg.register(p2)
    r1 = replace(r0, cycle_id="cycle-v1-x", runner_version="opt-v1",
                 runner_hash=V1.content_hash, config=V1)  # fmt: skip
    v2 = {"proposal_budget": "3", "operators": f"{DATE},{VAL}", "selection": BEST}
    for depth, want in ((1, oi.Blocked("self_change", ("depth_limit",))), (2, None)):
        lim = replace(LIMITS, max_depth=depth)
        got = oi.propose_self_change("cand-opt-2", "opt-v2", r1, reg2, "exp-opt-2",
                                     Proposer(v2), lim, (), SC_AT)  # fmt: skip
        assert got == want if want else isinstance(got, oi.SelfChange)
    wrong = oi.propose_self_change("cand-opt-3", "opt-v2", r1, reg, EXP,
                                   Proposer(v2), LIMITS, (), SC_AT)  # fmt: skip
    assert wrong == oi.Blocked("self_change", ("runner_not_baseline",))


def test_real_gates_stop_the_path_before_v1_runs() -> None:
    reg, sc = self_change()
    with pytest.raises(oi.OptimizerError, match="self_certification"):
        oi.assess_self_change(reg, sc, ledger(), book(), PRODUCER, V_AT, "verdict-x")
    v = oi.assess_self_change(reg, sc, ledger(), book(), ASSESSOR, V_AT, "verdict-1")
    assert (v.decision, v.reasons, dict(v.scores)) == (
        im.Decision.BLOCKED, ("sandbox_unqualified",), {})  # fmt: skip
    reg = reg.record_verdict(v)
    with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
        rl.prepare_release(reg, "cand-opt-1", "rel-opt-1", rl.Compatibility(1, 1, 1),
                           approval())  # fmt: skip
    want = ("not_installed", "not_accepted:blocked", "sandbox_unqualified")
    assert oi.runner_authority(V1, reg, EXP, deployment(), RUN) == oi.Blocked(
        "authority", want)  # fmt: skip
    assert cycle(V1, "cycle-v1-1", reg, at=RUN) == oi.Blocked("cycle", want)
    forged = replace(v0_cycle(), cycle_id="cycle-v1-1", runner_version="opt-v1",
                     runner_hash=V1.content_hash, config=V1, at=RUN)  # fmt: skip
    why = verify(reg, deployment(), v0_cycle(), forged, None)
    assert {"not_installed", "sandbox_unqualified", "receipt_not_reproduced",
            "replayed_output"} <= set(why)  # fmt: skip
    with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
        lifted(*self_change())  # without the lift, T051 refuses to install it
    fresh, _ = self_change()  # nothing assessed at all
    assert oi.runner_authority(V1, fresh, EXP, deployment(), RUN) == oi.Blocked(
        "authority", ("not_installed", "assessment_skipped"))  # fmt: skip


@pytest.mark.usefixtures("gate_lifted_install")
def test_gate_lifted_chain_passes_necessary_recursion_checks_only() -> None:
    reg, lg, r0, r1, nxt = chain()
    assert r1.authority == "release:rel-opt-1" and r1.runner_hash == V1.content_hash
    assert verify(reg, lg, r0, r1, nxt) == []  # necessary, not sufficient
    v = oi.verify_recursion(reg, EXP, lg, r0, r1, BASE, DEV, nxt)  # no opt-in
    assert v.reasons == ("synthetic_evidence",) and v.provisional
    assert cycle(V1, "cycle-v1-2", reg, lg, at=RUN, synthetic=False) == oi.Blocked(
        "cycle", ("synthetic_evidence",))  # fmt: skip
    assert cycle(V0, "cycle-v0-2", reg, lg, at=RUN) == oi.Blocked(
        "cycle", ("superseded",))  # fmt: skip
    other = replace(V1, proposal_budget=1)
    assert cycle(other, "cycle-x", reg, lg, at=RUN) == oi.Blocked(
        "cycle", ("not_installed",))  # fmt: skip


def gate(g: im.ExperimentRegistry, d: rl.DeploymentLedger) -> tuple[str, ...]:
    r = cycle(V1, "cycle-v1-g", g, d, at=RUN)
    assert isinstance(r, oi.Blocked) and r.step == "cycle"
    return r.reasons


def lift(**kw: object) -> tuple[str, ...]:
    g, sc = self_change()
    return gate(*lifted(g, sc, **kw))  # type: ignore[arg-type]


@pytest.mark.usefixtures("gate_lifted_install")
def test_recursion_negative_controls() -> None:
    reg, lg, r0, r1, nxt = chain()

    def why(r: oi.CycleReceipt = r1, n: im.Candidate | None = nxt,
            g: im.ExperimentRegistry = reg, d: rl.DeploymentLedger = lg,
            r_0: oi.CycleReceipt = r0) -> list[str]:  # fmt: skip
        return verify(g, d, r_0, r, n)

    assert why(replace(r1, config=V0)) == ["identical_relabel",  # V0's policy ran
        "receipt_not_reproduced", "runner_config_mismatch"]  # fmt: skip
    replay = replace(r1, proposals=r0.proposals, selected=None)
    assert why(replay) == ["receipt_not_reproduced", "replayed_output"]
    assert why(replace(r1, at=REL)) == ["ran_before_install"]
    assert cycle(V1, "cycle-v1-0", reg, lg, at=REL) == oi.Blocked(
        "cycle", ("not_yet_installed",))  # fmt: skip
    assert why(replace(r1, authority="baseline:opt-v0")) == ["runner_authority"]
    assert why(n=None) == why(n=replace(nxt, producer=PRODUCER)) == ["next_lineage"]
    rt = dict(nxt.runtime_versions) | {"optimizer_cycle": "cycle-other"}
    assert why(n=replace(nxt, runtime_versions=rt)) == ["next_lineage"]
    assert why(r_0=replace(r0, cycle_id="cycle-v0-9")) == ["source_cycle"]
    assert why(r_0=replace(r0, proposals=())) == [
        "receipt_not_reproduced", "source_cycle"]  # fmt: skip
    assert "v0_not_baseline" in why(r_0=replace(r0, config=V1,
        runner_hash=V1.content_hash, runner_version="opt-v1"))  # fmt: skip
    assert why(replace(r1, fixture_hash="0" * 64)) == ["receipt_not_reproduced"]
    assert why(replace(r1, runner_version="opt-v9")) == [
        "next_lineage", "runner_config_mismatch"]  # fmt: skip
    keys = {k: replace(t, revoked_at=RUN - timedelta(hours=1))
            for k, t in lg.keys.items()}  # fmt: skip
    assert why(d=rl.DeploymentLedger(lg.deployment, keys, lg.entries)) == [
        "key_not_valid"]  # fmt: skip
    g3, sc = self_change()  # skipped assessment: installed without any verdict
    g3, d3 = lifted(g3, sc, verdict=False)
    assert gate(g3, d3) == ("assessment_skipped",)
    assert why(g=g3, d=d3) == ["assessment_skipped"]
    # B2: no evidence; approver is the producer or assessor; acts before the verdict
    no_gain = {k: v for k, v in GOOD.items() if k != "dev_gain"}
    kws: tuple[dict[str, object], ...] = ({"trials": ()}, {"scores": no_gain})
    for kw in kws:
        assert lift(**kw) == ("verdict_without_evidence",)  # D3, D4
    violating = {k: Decimal(99) for k in GOOD}  # N-A: accepts what it violates
    assert lift(scores=violating) == ("verdict_inconsistent",)
    assert lift(approver=PRODUCER.actor_id) == lift(approver=ASSESSOR.actor_id) == (
        "self_approval",)  # fmt: skip
    assert lift(verdict_at=INST + timedelta(hours=1)) == ("before_verdict",)
    assert lift(operator=PRODUCER.actor_id) == ("self_install",)  # V0 cannot install
    # self-certification (N1: case variants and a shared family count too)
    with pytest.raises(im.ImprovementError, match="assessor_not_independent"):
        optimizer_protocol(assessor=PRODUCER)
    for a in (replace(ASSESSOR, version="opt-v1"), replace(ASSESSOR, version="OPT-V1"),
              replace(ASSESSOR, actor_id="Optimizer-Synth"),
              replace(ASSESSOR, family_id=PRODUCER.family_id)):  # fmt: skip
        p = optimizer_protocol(assessor=a, require_distinct_family=False)
        g, s4 = self_change(reg=registry(p))
        with pytest.raises(oi.OptimizerError, match="self_certification"):
            oi.assess_self_change(g, s4, ledger(), book(), a, V_AT, "v-n1")
        assert gate(*lifted(g, s4, assessor=a)) == ("self_certification",)
    # a later verdict supersedes the one the release names
    v = reg.verdicts("cand-opt-1")[-1]
    later = replace(v, verdict_id="verdict-opt-2", decision=im.Decision.REJECT,
                    reasons=("hard:threshold_changes",))  # fmt: skip
    assert gate(reg.record_verdict(later), lg) == (
        "not_accepted:reject", "hard:threshold_changes", "release_binding")  # fmt: skip
    # B1: a V1 candidate of another optimizer experiment grants nothing in EXP
    px = optimizer_protocol(experiment_id="exp-opt-x", baseline_version="opt-x0",
        baseline_hash=replace(V1, proposal_budget=1).content_hash,
        budget_reservation_id="bud-opt-x")  # fmt: skip
    cx = im.Candidate("cand-x", "exp-opt-x", oi.OPT, "opt-x0", PRODUCER,
                      V1.content_hash, V1.to_payload(), {}, (), SC_AT)  # fmt: skip
    gx = registry().register(px).register_candidate(cx).record_verdict(replace(v,
        experiment_id="exp-opt-x", candidate_id="cand-x",
        candidate_hash=cx.content_hash, protocol_hash=px.content_hash))  # fmt: skip
    assert gate(gx, lg) == ("not_proposed",)
    forged = replace(sc, candidate=replace(sc.candidate, candidate_id="cand-opt-9",
                     runtime_versions={}))  # fmt: skip  # not from the V0 run
    g1 = registry().register_candidate(forged.candidate)
    assert gate(*lifted(g1, forged)) == ("not_from_baseline",)
    # the research candidate names the runner and its base
    with pytest.raises(oi.OptimizerError, match="producer_not_runner"):
        oi.cycle_candidate("cand-r", r1, research_protocol(), BASE, RUN)
    rp = research_protocol(producer=replace(PRODUCER, version="opt-v1"))
    with pytest.raises(oi.OptimizerError, match="base_mismatch"):
        oi.cycle_candidate("cand-r", r1, rp, FIX, RUN)
    with pytest.raises(oi.OptimizerError, match="nothing_selected"):
        oi.cycle_candidate("cand-r", replace(r1, selected=None), rp, BASE, RUN)
    # stop rule, budget and money (B3)
    nothing = (r0, replace(r0, cycle_id="cycle-v0-0"))
    assert cycle(V0, "cycle-v0-3", registry(), history=nothing) == oi.Blocked(
        "cycle", ("stop_no_progress",))  # fmt: skip
    rich = (replace(r0, declared_cost=Usd("19.60")),)
    assert cycle(V1, "cycle-v1-2", reg, lg, rich, RUN) == oi.Blocked(
        "cycle", ("improvement_budget_exhausted",))  # fmt: skip
    with pytest.raises(oi.OptimizerError, match="limits"):
        oi.Limits(2, 2, Usd("20.00"), Usd("0"))
    for neg in (lambda: replace(r0, declared_cost=Usd("-1000")),
                lambda: replace(sc, declared_cost=Usd("-1"))):  # fmt: skip
        with pytest.raises(oi.OptimizerError, match="negative_cost"):
            neg()
    bad = replace(r0)
    object.__setattr__(bad, "declared_cost", Usd("-1000"))
    with pytest.raises(oi.OptimizerError, match="negative_cost"):
        cycle(V0, "c-neg", registry(), history=(bad,))
    src = Path(oi.__file__).read_text()
    names = {n.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef)}  # fmt: skip
    assert not {n for n in names if any(w in n for w in (
        "promote", "approve", "install", "adopt", "sign", "release"))}  # fmt: skip


@pytest.mark.usefixtures("gate_lifted_install")
def test_review_round2_strict_default_ambiguity_and_utc_month() -> None:
    from datetime import UTC, timezone

    # D1: without an install the gate and verification stay strict by default
    reg, _, r0, r1, nxt = chain()
    assert oi.runner_authority(V1, reg, EXP, deployment(), RUN) == oi.Blocked(
        "authority", ("not_installed", "synthetic_evidence"))  # fmt: skip
    v = oi.verify_recursion(reg, EXP, deployment(), r0, r1, BASE, DEV, nxt)
    assert {"not_installed", "synthetic_evidence"} <= set(v.reasons)
    # D6: two EXP candidates for one artifact (a corrupted store; T044 refuses it)
    g, sc = self_change()
    dup = replace(sc.candidate, candidate_id="cand-opt-dup")
    object.__setattr__(g, "entries", (*g.entries, dup))
    assert oi.runner_authority(V1, g, EXP, deployment(), RUN) == oi.Blocked(
        "authority", ("not_installed", "ambiguous_candidate"))  # fmt: skip
    # D17: one artifact proposed from two different baselines
    px = optimizer_protocol(experiment_id="exp-opt-x", baseline_version="opt-x0",
        baseline_hash=replace(V1, proposal_budget=1).content_hash,
        budget_reservation_id="bud-opt-x")  # fmt: skip
    cx = im.Candidate("cand-x", "exp-opt-x", oi.OPT, "opt-x0", PRODUCER,
                      V1.content_hash, V1.to_payload(), {}, (), SC_AT)  # fmt: skip
    g, _ = self_change()
    with pytest.raises(oi.OptimizerError, match="lineage_ambiguous"):
        oi.generation(g.register(px).register_candidate(cx), V1.content_hash)
    assert oi.generation(g, V1.content_hash) == 1
    # N-B: 2026-10-31T23:30-05:00 is November in UTC and counts against November
    est = timezone(-timedelta(hours=5))
    late = replace(r0, cycle_id="cycle-v0-oct", declared_cost=Usd("19.90"),
                   at=datetime(2026, 10, 31, 23, 30, tzinfo=est))  # fmt: skip
    nov = datetime(2026, 11, 1, 5, tzinfo=UTC)
    assert cycle(V0, "cycle-v0-nov", registry(), history=(late,), at=nov) == (
        oi.Blocked("cycle", ("improvement_budget_exhausted",)))  # fmt: skip
    dec = datetime(2026, 12, 1, 5, tzinfo=UTC)
    assert isinstance(cycle(V0, "cycle-v0-dec", registry(), history=(late,), at=dec),
                      oi.CycleReceipt)  # fmt: skip
