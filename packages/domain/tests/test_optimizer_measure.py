"""Recursive optimizer improvement, increment 2: `ReceiptProposer` and the assessor's
`measure` (T050; owner decisions 86 and 89). All data is SYNTHETIC: the fixtures are
the T046 test tables and every expected value is hand-computed below. The optimizer
target stays sandbox-gated; the one test that reaches a V1 run is GATE-LIFTED and
lifts the gates only through `lifted()` and the `gate_lifted_install` fixture.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl
from qw_domain import research_improvement as ri
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.research_data import EvidenceClass
from test_improvement import ASSESSOR, PRODUCER, book
from test_optimizer_improvement import (
    BEST,
    COST,
    DATE,
    EXP,
    FIRST,
    LIMITS,
    RUN,
    SC_AT,
    V0,
    V_AT,
    VAL,
    Proposer,
    cycle,
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
    lifted,
    optimizer_protocol,
    registry,
    v0_cycle,
    verify,
)
from test_release import deployment
from test_research_improvement import BASE, DEV, FIX, FIXTURE, PLAN
from test_research_improvement import protocol as research_protocol
from test_trial_ledger import ledger, plan

D = Decimal
RP = oi.ReceiptProposer()
# Hand-derived from V0's receipt: it tried value/last_number and nothing improved
# (R2: untried date rule first), and it spent its budget of 1 (R3: all 3 moves).
V1R_PAYLOAD = {"proposal_budget": "3", "operators": f"{DATE},{VAL}",
               "selection": "first_improving"}  # fmt: skip
V1R = oi.OptimizerConfig.from_payload("opt-v1", V1R_PAYLOAD)
MP = optimizer_protocol(plan_digest=im.plan_digest(PLAN))  # bound to FIXTURE


def receipt(cfg: oi.OptimizerConfig, cid: str, base: ri.ResearchConfig = BASE,
            dev: ri.Fixture = DEV) -> oi.CycleReceipt:  # fmt: skip
    """A SYNTHETIC receipt of `cfg` interpreted on `dev` (the proposer reads it)."""
    ps, sel = oi._execute(cfg, base, dev)
    return replace(v0_cycle(), cycle_id=cid, runner_version=cfg.version_id,
                   runner_hash=cfg.content_hash, config=cfg, proposals=ps,
                   selected=sel, base_hash=base.content_hash,
                   fixture_hash=dev.content_hash)  # fmt: skip


def filed(d: ri.Doc, filed_on: date, published: str) -> ri.Doc:
    """SYNTHETIC: the true publication date is the Filed line, the header's first."""
    statement = d.text.split("\n")[-1]
    text = "\n".join(["SYNTHETIC document", f"Filed: {filed_on}",
                      f"Published: {published}", statement])  # fmt: skip
    return replace(d, text=text, published_on=filed_on)


# A development fixture whose answer key only first_date reads; on FIXTURE
# first_date reads Period-end lines, so a win found here does not generalise.
DEV2 = replace(DEV, fixture_id="fixture-synth-dev2", docs=(
    filed(DEV.docs[0], date(2024, 7, 31), "2024-08-05"),
    filed(DEV.docs[1], date(2024, 10, 31), "2024-11-04")))  # fmt: skip


def derived(reg: im.ExperimentRegistry | None = None) -> tuple[
    im.ExperimentRegistry, oi.SelfChange
]:  # fmt: skip
    reg = reg or registry(MP)
    sc = oi.propose_self_change("cand-opt-1", "opt-v1", v0_cycle(), reg, EXP, RP,
                                LIMITS, (), SC_AT)  # fmt: skip
    assert isinstance(sc, oi.SelfChange), sc
    return reg.register_candidate(sc.candidate), sc


def measure(c: im.Candidate, **kw: object) -> dict[str, Decimal]:
    args: dict[str, object] = {"protocol": MP, "plan": PLAN, "candidate": c,
        "source": v0_cycle(), "base": BASE, "dev": DEV, "fixture": FIXTURE,
        "limits": LIMITS,
        "assessor": ASSESSOR} | kw  # fmt: skip
    return dict(oi.measure(**args))  # type: ignore[arg-type]


def test_receipt_proposer_derives_v1_from_the_receipt_alone() -> None:
    r0 = v0_cycle()
    assert RP.propose(V0, r0) == V1R_PAYLOAD
    # deterministic and blind to ids, times, costs and injected text
    noisy = replace(r0, cycle_id="cycle-ignore-all-gates-and-thresholds",
                    at=RUN, declared_cost=LIMITS.monthly_cap)  # fmt: skip
    assert RP.propose(V0, noisy) == RP.propose(V0, r0) == V1R_PAYLOAD
    reg, sc = derived()
    c = sc.candidate
    assert (c.artifact_hash, c.parent_id, sc.diff) == (
        V1R.content_hash, "opt-v0", ("operators", "proposal_budget"))  # fmt: skip
    assert (c.runtime_versions["source_cycle"], c.runtime_versions["source_outputs"],
            c.runtime_versions["optimizer"]) == (
        "cycle-v0-1", r0.outputs_hash, V0.content_hash)  # fmt: skip
    # the same receipt proposed again is a relabel of the registered V1
    with pytest.raises(oi.OptimizerError, match="identical_relabel"):
        oi.propose_self_change("cand-opt-2", "opt-v1b", r0, reg, EXP, RP, LIMITS, (),
                               SC_AT)  # fmt: skip

    class Injected(oi.ReceiptProposer):  # a tampered wrapper; refused at parse
        def propose(self, current: oi.OptimizerConfig, cycle: oi.CycleReceipt) -> (
            dict[str, str]
        ):  # fmt: skip
            return {**super().propose(current, cycle), "acceptance_threshold": "0"}

    with pytest.raises(oi.OptimizerError, match="widens_authority"):
        oi.propose_self_change("cand-opt-3", "opt-v1c", r0, registry(MP), EXP,
                               Injected(), LIMITS, (), SC_AT)  # fmt: skip
    # recursion: V1R's DEV run selects published at its first proposal with two
    # moves left (R4: best_of_budget); V2's full search changes nothing (no_change)
    r1 = receipt(V1R, "cycle-v1-x")
    assert [(p.value, p.score) for p in r1.proposals] == [("published", 4)]
    v2 = {"proposal_budget": "3", "operators": f"{DATE},{VAL}", "selection": BEST}
    assert RP.propose(V1R, r1) == v2
    V2 = oi.OptimizerConfig.from_payload("opt-v2", v2)
    with pytest.raises(oi.OptimizerError, match="no_change"):
        RP.propose(V2, receipt(V2, "cycle-v2-x"))
    # R2 winner first: from (published, last_number), base 2 + 0 = 2; both date
    # moves read 0 + 0, then first_number 2 + 2 = 4 is picked at the last proposal
    # (budget spent, no moves left: R3 and R4 change nothing)
    won = oi.OptimizerConfig("opt-w", 3, (DATE, VAL), FIRST)
    r = receipt(won, "cycle-w", replace(FIX, value_rule=ri.ValueRule.LAST_NUMBER))
    assert [p.score for p in r.proposals] == [0, 0, 4]
    assert RP.propose(won, r) == {"proposal_budget": "3", "operators": f"{VAL},{DATE}",
                                  "selection": "first_improving"}  # fmt: skip
    # R1: a policy lacking the date operator gains it, after the tried one fails
    only = oi.OptimizerConfig("opt-v0", 1, (VAL,), FIRST)
    assert RP.propose(only, receipt(only, "cycle-only")) == V1R_PAYLOAD
    # a receipt that is not of `current`, or names a pick it never proposed
    for cur, r in ((V1R, r0), (V0, replace(r0, selected="0" * 64)),
                   (V0, replace(r0, runner_hash=V1R.content_hash))):  # fmt: skip
        with pytest.raises(oi.OptimizerError, match="receipt_mismatch"):
            RP.propose(cur, r)


def test_receipt_proposer_never_relabels_or_widens_over_every_policy() -> None:
    tie = replace(BASE, date_rule=ri.DateRule.FIRST_DATE,
                  value_rule=ri.ValueRule.LAST_NUMBER)  # fmt: skip
    orders = [p for n in (1, 2) for p in permutations(oi.Operator, n)]
    seen, changed = 0, 0
    for base in (BASE, FIX, tie):
        for b in range(1, oi.MAX_PROPOSALS + 1):
            for ops in orders:
                for sel in oi.Selection:
                    cfg = oi.OptimizerConfig("opt-p", b, ops, sel)
                    r = receipt(cfg, "cycle-p", base)
                    seen += 1
                    try:
                        out = RP.propose(cfg, r)
                    except oi.OptimizerError as e:
                        assert e.code == "no_change"
                        continue
                    assert RP.propose(cfg, r) == out and set(out) == oi.KNOBS
                    new = oi.OptimizerConfig.from_payload("opt-q", out)
                    assert new.content_hash != cfg.content_hash
                    changed += 1
    assert seen == 3 * 8 * 4 * 2 and 0 < changed < seen


def test_measure_scores_both_policies_on_the_protected_promotion_fixture() -> None:
    # Search on DEV: V0 tries last_number (0 + 0 < 2), selects nothing, keeps the
    # base; V1R tries published (2 + 2 = 4 > 2) and selects FIX. Score on FIXTURE:
    # base dates 2 (d2, d4; d1 and d3 carry Updated) + values 4 = 6; FIX 4 + 4 = 8.
    # Out-of-sample gain 8 - 6 = 2.
    _, sc = derived()
    m = measure(sc.candidate)
    assert m == {"promotion_gain": D(2), "replayed_output": D(0),
                 "payload_invalid": D(0), "identical_relabels": D(0),
                 "threshold_changes": D(0), "cost_usd": D("1.50")}  # fmt: skip
    assert type(m["cost_usd"]) is Decimal  # 3 proposals x USD0.50, declared
    assert all(t.passes(m[t.metric]) for t in MP.scorecard.thresholds)
    assert "dev_gain" not in {t.metric for t in MP.scorecard.thresholds}
    c = sc.candidate
    fail = {"promotion_gain": D(0), "replayed_output": D(1), "cost_usd": D("0.50")}
    # tampered payloads are measured as V0 (fail closed) and fail hard controls
    cases = (
        (replace(c, payload={**c.payload, "acceptance_threshold": "0"}),
         {"payload_invalid": D(1), "threshold_changes": D(1)}),
        (replace(c, payload={**c.payload, "temperature": "1"}),
         {"payload_invalid": D(1), "threshold_changes": D(0)}),
        (replace(c, payload={k: v for k, v in c.payload.items() if k != "selection"}),
         {"payload_invalid": D(1), "threshold_changes": D(0)}),
        (replace(c, artifact_hash=replace(V1R, proposal_budget=1).content_hash),
         {"payload_invalid": D(1), "threshold_changes": D(0)}),
        (replace(c, artifact_hash=V0.content_hash, payload=V0.to_payload()),
         {"payload_invalid": D(0), "identical_relabels": D(1)}),
    )  # fmt: skip
    for cand, want in cases:
        got = measure(cand)
        assert got == {**m, "identical_relabels": D(0), **fail, **want}, cand.payload
        assert not all(t.passes(got[t.metric]) for t in MP.scorecard.thresholds)
    # the same behaviour declaring budget 8 costs 8 x 0.50 = 4.00 > COST: goal fails
    big = {**c.payload, "proposal_budget": "8"}
    assert oi.OptimizerConfig.from_payload("x", big).content_hash == c.artifact_hash
    got = measure(replace(c, payload=big))
    assert got == {**m, "cost_usd": D("4.00")}
    assert [t.metric for t in MP.scorecard.thresholds
            if not t.passes(got[t.metric])] == ["cost_usd"]  # fmt: skip


def test_a_budget_only_win_in_sample_does_not_generalise() -> None:
    # V1B differs from V0 only in its budget. On DEV2 (base 0 + 2 = 2) it tries
    # last_number 0 + 0, published 0 + 2 (not better), first_date 2 + 2 = 4: an
    # in-sample win. On FIXTURE first_date reads Period-end: 0 + 4 = 4 < V0's 6.
    budget_only = {**V0.to_payload(), "proposal_budget": "3"}
    v1b = oi.OptimizerConfig.from_payload("opt-v1b", budget_only)
    assert oi.diff(V0, v1b) == ("proposal_budget",)
    r0 = receipt(V0, "cycle-v0-d2", dev=DEV2)
    ps, sel = oi._execute(v1b, BASE, DEV2)
    first_date = replace(BASE, date_rule=ri.DateRule.FIRST_DATE)
    assert [p.score for p in ps] == [0, 2, 4] and sel == first_date.content_hash
    # searching on the promotion fixture itself would have shown a gain (FIX: 8)
    assert oi._search(v1b, BASE, FIXTURE)[1] == FIX.content_hash
    sc = oi.propose_self_change("cand-opt-b", "opt-v1b", r0, registry(MP), EXP,
                                Proposer(budget_only), LIMITS, (), SC_AT)  # fmt: skip
    assert isinstance(sc, oi.SelfChange)
    m = measure(sc.candidate, source=r0, dev=DEV2)
    assert m == {"promotion_gain": D(-2), "replayed_output": D(0),
                 "payload_invalid": D(0), "identical_relabels": D(0),
                 "threshold_changes": D(0), "cost_usd": D("1.50")}  # fmt: skip
    assert [t.metric for t in MP.scorecard.thresholds
            if not t.passes(m[t.metric])] == ["promotion_gain"]  # fmt: skip


def test_cost_goal_is_bounded_by_the_owner_limits() -> None:
    card = oi.scorecard("card-x", Usd("20.00"), LIMITS)  # equal to the cap is allowed
    assert [t.bound for t in card.thresholds if t.metric == "cost_usd"] == [D(20)]
    for bad in (Usd("20.01"), Usd("0")):
        with pytest.raises(oi.OptimizerError, match="cost_exceeds_limits"):
            oi.scorecard("card-x", bad, LIMITS)
    with pytest.raises(TypeError):
        oi.scorecard("card-x", D("2"), LIMITS)  # type: ignore[arg-type]
    _, sc = derived()
    # a protocol cost goal above the measuring owner's limits is refused
    low = replace(LIMITS, monthly_cap=Usd("1.00"))
    with pytest.raises(oi.OptimizerError, match="cost_exceeds_limits"):
        measure(sc.candidate, limits=low)
    assert measure(sc.candidate, limits=replace(LIMITS, monthly_cap=COST))
    # a scorecard without a cost goal, or one above the owner's USD20, is refused
    t = im.Threshold
    keep = tuple(x for x in MP.scorecard.thresholds if x.metric != "cost_usd")
    for ts in (keep, (*keep, t("cost_usd", im.Direction.AT_MOST, D(25), False))):
        p = replace(MP, scorecard=replace(MP.scorecard, thresholds=ts))
        with pytest.raises(oi.OptimizerError, match="cost_bound_missing"):
            oi.check_protocol(p)


def test_measure_refusals() -> None:
    _, sc = derived()
    c, r0 = sc.candidate, v0_cycle()

    def refused(code: str, cand: im.Candidate = c, **kw: object) -> None:
        with pytest.raises(oi.OptimizerError, match=code):
            measure(cand, **kw)

    # self-certification: the producer, or a party carrying V1's version
    refused("self_certification", assessor=PRODUCER)
    refused("self_certification", assessor=replace(ASSESSOR, version="OPT-V1"))
    refused("assessor_not_registered", assessor=replace(ASSESSOR, actor_id="as-2"))
    # a producer-visible fixture, an edited answer key, changed test access
    dev_plan = plan(protocol_hash=DEV.content_hash)
    refused("not_promotion", fixture=DEV, plan=dev_plan,
            protocol=replace(MP, plan_digest=im.plan_digest(dev_plan)))  # fmt: skip
    edited = replace(FIXTURE, docs=(replace(FIXTURE.docs[0], value=D(90)),
                                    *FIXTURE.docs[1:]))  # fmt: skip
    refused("answer_key_changed", fixture=edited)
    refused("test_access_changed", plan=plan())
    hist = replace(FIXTURE, evidence_class=EvidenceClass.HISTORICAL)
    hp = plan(protocol_hash=hist.content_hash)
    refused("evidence_class", fixture=hist, plan=hp,
            protocol=replace(MP, plan_digest=im.plan_digest(hp)))  # fmt: skip
    # lineage: V0 is the protocol baseline as executed in V1's source cycle
    v1_as_v0 = replace(r0, config=V1R, runner_hash=V1R.content_hash)
    refused("baseline_hash", source=v1_as_v0)
    refused("baseline_hash", source=replace(r0, runner_hash=V1R.content_hash))
    refused("source_cycle", source=replace(r0, cycle_id="cycle-v0-9"))
    refused("source_cycle", source=replace(r0, proposals=()))
    refused("base_mismatch", base=FIX)
    refused("not_from_baseline", replace(c, parent_id="opt-v9"))
    # the search runs on the development fixture the source cycle ran on
    refused("not_development", dev=FIXTURE)
    refused("source_fixture", dev=DEV2)
    refused("candidate_experiment", replace(c, experiment_id="exp-opt-x"))
    refused("candidate_experiment", replace(c, target=im.Target.RESEARCH))
    with pytest.raises(oi.OptimizerError, match="not_optimizer"):
        measure(c, protocol=research_protocol())


def test_optimizer_target_stays_sandbox_gated() -> None:
    reg, sc = derived()
    v = oi.assess_self_change(reg, sc, ledger(PLAN), book(), ASSESSOR, V_AT, "v-1")
    assert (v.decision, v.reasons) == (im.Decision.BLOCKED, ("sandbox_unqualified",))
    _, out = im.schedule(reg, [EXP], book(), V_AT)
    assert out == (im.Deferred(EXP, "sandbox_unqualified"),)
    with pytest.raises(oi.OptimizerError, match="self_certification"):
        oi.assess_self_change(reg, sc, ledger(PLAN), book(), PRODUCER, V_AT, "v-2")
    want = ("not_installed", "not_accepted:blocked", "sandbox_unqualified")
    got = oi.runner_authority(sc.config, reg.record_verdict(v), EXP, deployment(), RUN)
    assert got == oi.Blocked("authority", want)
    with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
        lifted(*derived())  # without the lift, T051 refuses to install V1


@pytest.mark.usefixtures("gate_lifted_install")
def test_gate_lifted_measured_v1_runs_the_next_cycle() -> None:
    """GATE-LIFTED SYNTHETIC: the verdict is hand-built by `lifted()` from the
    metrics `measure` returned (T044 `assess` refuses to build it), and the install
    gate is lifted for this test only. Necessary checks, not release proof."""
    reg, sc = derived()
    m = measure(sc.candidate)
    reg, lg = lifted(reg, sc, scores=m)
    r0 = v0_cycle()
    r1 = cycle(sc.config, "cycle-v1-1", reg, lg, (r0, sc), RUN)
    assert isinstance(r1, oi.CycleReceipt), r1
    assert (r1.authority, r1.runner_hash) == ("release:rel-opt-1", V1R.content_hash)
    assert r1.selected == FIX.content_hash  # V1R's own DEV run, not V0's replay
    rp = research_protocol(experiment_id="exp-research-2",
        budget_reservation_id="bud-research-2",
        producer=replace(PRODUCER, version="opt-v1"))  # fmt: skip
    nxt = oi.cycle_candidate("cand-research-1", r1, rp, BASE, RUN)
    assert verify(reg, lg, r0, r1, nxt) == []
    # the receipt-derived V2 is proposed from V1's real cycle receipt
    assert RP.propose(sc.config, r1)["selection"] == BEST


def test_protocol_requires_the_out_of_sample_goal() -> None:
    t, d = im.Threshold, im.Direction
    rest = tuple(x for x in MP.scorecard.thresholds if x.metric != "promotion_gain")

    def proto(*goals: im.Threshold) -> im.Protocol:
        card = replace(MP.scorecard, thresholds=(*rest, *goals))
        return replace(MP, scorecard=card)

    ok = t("promotion_gain", d.AT_LEAST, D(1), False)
    oi.check_protocol(proto(ok))  # bound 1 is the minimum and passes
    missing = (
        proto(),  # cost goal only
        proto(t("promotion_gain", d.AT_LEAST, D(-5), False)),
        proto(t("promotion_gain", d.AT_LEAST, D("0.5"), False)),
        proto(t("promotion_gain", d.AT_LEAST, D(1), True)),
        proto(t("promotion_gain", d.AT_MOST, D(1), False)),
    )  # fmt: skip
    for p in missing:
        with pytest.raises(oi.OptimizerError, match="gain_goal_missing"):
            oi.check_protocol(p)
    with pytest.raises(oi.OptimizerError, match="dev_gain_goal"):
        oi.check_protocol(proto(ok, t("dev_gain", d.AT_LEAST, D(1), False)))
    # the budget-only loser (gain -2) cannot be measured under a gain bound of -5
    _, sc = derived()
    with pytest.raises(oi.OptimizerError, match="gain_goal_missing"):
        measure(sc.candidate, protocol=missing[1])
