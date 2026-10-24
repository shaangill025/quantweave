"""T050 increment 3: strict cycle-receipt and recursive-lineage wire forms, and the
experiment schema's `next_cycle_run_id`. SYNTHETIC data only (the increment 1 and 2
fixtures); the recursion path is GATE-LIFTED (`lifted()` and `gate_lifted_install`),
so nothing here is release proof."""

import json
from dataclasses import replace
from datetime import timedelta, timezone
from typing import Any

import pytest
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl
from qw_domain.research_data import digest
from test_improvement import ASSESSOR, PRODUCER, book
from test_optimizer_improvement import (
    EXP,
    RUN,
    V0,
    cycle,
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
    lifted,
    v0_cycle,
)
from test_optimizer_measure import V1R, derived, measure
from test_release import deployment
from test_research_improvement import BASE, DEV, PLAN
from test_research_improvement import protocol as research_protocol

E = oi.OptimizerError


def recursion() -> tuple[im.ExperimentRegistry, rl.DeploymentLedger,
                         oi.CycleReceipt, oi.CycleReceipt, im.Candidate]:  # fmt: skip
    """GATE-LIFTED SYNTHETIC: V0 -> receipt-derived V1 -> measured -> hand-built
    accept and signed release -> V1's own cycle -> the next research candidate."""
    reg, sc = derived()
    reg, lg = lifted(reg, sc, scores=measure(sc.candidate))
    r0 = v0_cycle()
    r1 = cycle(sc.config, "cycle-v1-1", reg, lg, (r0, sc), RUN)
    assert isinstance(r1, oi.CycleReceipt), r1
    rp = research_protocol(experiment_id="exp-research-2",
        budget_reservation_id="bud-research-2",
        producer=replace(PRODUCER, version="opt-v1"))  # fmt: skip
    return reg, lg, r0, r1, oi.cycle_candidate("cand-research-1", r1, rp, BASE, RUN)


def lineage_receipt() -> tuple[im.ExperimentRegistry, rl.DeploymentLedger,
                               im.Candidate, oi.LineageReceipt]:  # fmt: skip
    reg, lg, r0, r1, nxt = recursion()
    lin = oi.lineage("lineage-synth-1", reg, EXP, lg, r0, r1, BASE, DEV, nxt,
                     accept_synthetic_evidence=True)  # fmt: skip
    return reg, lg, nxt, lin


def optimizer_record(reg: im.ExperimentRegistry) -> im.ExperimentRecord:
    return im.record(reg, EXP, "cand-opt-1", PLAN, book())


def refused(code: str, w: object, cls: Any = oi.CycleReceipt) -> None:
    with pytest.raises(E, match=code):
        cls.from_wire(w)


def rehash(w: dict[str, Any]) -> dict[str, Any]:
    """A forger who recomputes the lineage hash (receipts are unauthenticated)."""
    body = {k: v for k, v in w.items() if k != "lineage_hash"}
    return {**body, "lineage_hash": digest(["qw-optimizer-lineage-v1", body])}


@pytest.mark.usefixtures("gate_lifted_install")
def test_cycle_receipt_wire_round_trips_exactly() -> None:
    _, _, r0, r1, _ = recursion()
    for r in (r0, r1):
        w: dict[str, Any] = r.wire()
        assert json.loads(json.dumps(w)) == w  # JSON values only
        assert oi.CycleReceipt.from_wire(json.loads(json.dumps(w))) == r
        assert oi.CycleReceipt.from_wire(w).wire() == w
        assert w["receipt_hash"] == r.content_hash and w["at"].endswith("Z")
    assert r0.wire()["declared_cost"] == "0.5"  # a decimal string, never a number
    assert r1.wire()["authority"] == "release:rel-opt-1"
    assert r0.content_hash != r1.content_hash


def test_cycle_receipt_wire_refusals() -> None:
    r0 = v0_cycle()
    w: dict[str, Any] = r0.wire()
    p0 = w["proposals"][0]
    plus_one = r0.at.astimezone(timezone(timedelta(hours=1))).isoformat()
    cfg = w["config"]
    cases: list[tuple[str, object]] = [
        ("wire_type", [w]),
        ("wire_keys", {**w, "extra": "x"}),
        ("wire_keys", {k: v for k, v in w.items() if k != "at"}),
        ("wire_keys", {**w, "config": {**cfg, "gate": "off"}}),
        ("wire_type", {**w, "declared_cost": 0.5}),  # a JSON float
        ("wire_type", {**w, "declared_cost": 1}),
        ("wire_decimal", {**w, "declared_cost": "1e1"}),
        ("wire_decimal", {**w, "declared_cost": "0.50"}),
        ("negative_cost", {**w, "declared_cost": "-1"}),
        ("wire_instant", {**w, "at": w["at"].removesuffix("Z")}),  # naive
        ("wire_instant", {**w, "at": plus_one}),  # the same instant, not UTC
        ("wire_type", {**w, "proposals": [{**p0, "score": 0.0}]}),
        ("wire_type", {**w, "proposals": [{**p0, "score": True}]}),
        ("wire_type", {**w, "proposals": [{**p0, "score": -1}]}),
        ("wire_type", {**w, "proposals": "none"}),
        ("wire_value", {**w, "proposals": [{**p0, "value": "regex_any"}]}),
        ("wire_value", {**w, "proposals": [{**p0, "knob": "assessor.rule"}]}),
        ("wire_hash", {**w, "base_hash": w["base_hash"].upper()}),
        ("wire_hash", {**w, "selected": "f" * 63}),
        ("wire_value", {**w, "authority": "owner:opt-v0"}),
        ("authority", {**w, "authority": "release:"}),
        ("cycle_id", {**w, "cycle_id": "bad id"}),
        ("runner_config_mismatch", {**w, "runner_hash": "e" * 64}),
        ("runner_config_mismatch", {**w, "config": {**cfg, "proposal_budget": "2"}}),
        ("^value: proposal_budget", {**w, "config": {**cfg, "proposal_budget": "01"}}),
        ("^value: proposal_budget", {**w, "config": {**cfg, "proposal_budget": "9"}}),
        (
            "^value: operators or selection",
            {**w, "config": {**cfg, "selection": "zzz"}},
        ),
        ("^value: operators", {**w, "config": {**cfg, "operators": "extract.value,"}}),
        ("^wire_type: proposal_budget", {**w, "config": {**cfg, "proposal_budget": 1}}),
        ("hash_mismatch: outputs_hash", {**w, "proposals": [{**p0, "score": 2}]}),
        ("hash_mismatch: outputs_hash", {**w, "selected": "f" * 64}),
        ("hash_mismatch: receipt_hash", {**w, "authority": "release:rel-x"}),
        ("hash_mismatch: receipt_hash", {**w, "fixture_hash": "e" * 64}),
        ("hash_mismatch: receipt_hash", {**w, "receipt_hash": "e" * 64}),
    ]
    for code, bad in cases:
        refused(code, bad)
    assert w == r0.wire()  # nothing above mutated the original
    odd = replace(r0, config=replace(V0, version_id="opt-vx"))  # not the runner
    with pytest.raises(E, match="runner_config_mismatch"):
        odd.wire()


@pytest.mark.usefixtures("gate_lifted_install")
def test_lineage_records_the_spec_fields_and_round_trips() -> None:
    reg, _, _, receipt = lineage_receipt()
    _, _, r0, r1, nxt = recursion()
    lin = receipt.claim
    assert (lin.optimizer_v0_hash, lin.candidate_v1_hash, lin.installed_v1_hash) == (
        V0.content_hash, V1R.content_hash, V1R.content_hash)  # fmt: skip
    assert lin.diff == ("operators", "proposal_budget")  # V0 budget 1 -> V1 budget 3
    assert lin.assessor_manifest_hash == reg.protocol(EXP).content_hash
    assert (lin.release_id, lin.approval_principal_id, lin.approval_receipt_id) == (
        "rel-opt-1", "maint-synth", "stepup-maint-1")  # fmt: skip
    assert lin.signature_reference.startswith("sig.")
    assert (lin.next_cycle, lin.v0_cycle, lin.assessor) == (r1, r0, ASSESSOR)
    assert lin.next_candidate == oi.NextCandidate(
        "cand-research-1", "exp-research-2", nxt.artifact_hash, "opt-v1",
        V1R.content_hash, "cycle-v1-1")  # fmt: skip
    assert lin.verdict_limitations == ("gate_lifted_synthetic",)
    assert (lin.install_operator_id, lin.install_self_approved) == ("operator-synth",
                                                                    False)  # fmt: skip
    w = receipt.wire()
    assert w["limitations"] == list(oi.LIMITATIONS)  # unauthenticated: provisional
    assert json.loads(json.dumps(w)) == w
    assert oi.UnverifiedLineage.from_wire(json.loads(json.dumps(w))) == lin
    assert oi.UnverifiedLineage.from_wire(w).wire() == w == lin.wire()
    assert receipt.content_hash == lin.content_hash == w["lineage_hash"]
    assert lin.refusals() == ()


@pytest.mark.usefixtures("gate_lifted_install")
def test_a_parsed_lineage_is_served_only_after_it_is_re_derived() -> None:
    reg, lg, nxt, receipt = lineage_receipt()
    rec, w = optimizer_record(reg), receipt.wire()
    nc: dict[str, Any] = w["next_candidate"]  # type: ignore[assignment]
    u = oi.UnverifiedLineage.from_wire(w)
    with pytest.raises(TypeError):
        oi.next_cycle_run_id(rec, u)  # type: ignore[arg-type]
    with pytest.raises(E, match="lineage_unverified"):
        oi.LineageReceipt(u, object())  # only lineage() or verify_lineage() seal
    with pytest.raises(E, match="lineage_unverified"):
        oi.LineageReceipt(receipt, oi._SEAL)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        receipt.claim = u  # type: ignore[misc]
    with pytest.raises(TypeError):
        oi.verify_lineage(receipt, reg, lg, BASE, DEV, nxt)  # type: ignore[arg-type]
    with pytest.raises(E, match="lineage_unverified: synthetic_evidence"):
        oi.verify_lineage(u, reg, lg, BASE, DEV, nxt)  # no opt-in
    ok = oi.verify_lineage(u, reg, lg, BASE, DEV, nxt, accept_synthetic_evidence=True)
    assert ok == receipt and oi.next_cycle_run_id(rec, ok) == "cycle-v1-1"
    forged_cycle = replace(receipt.claim.next_cycle, cycle_id="cycle-forged").wire()
    forgeries: list[tuple[str, dict[str, Any]]] = [  # each locally consistent
        ("lineage_mismatch: approval_receipt_id", {"approval_receipt_id": "step-f"}),
        ("lineage_mismatch: lineage_hash,signature_reference",
         {"signature_reference": "sig.forged.00"}),
        ("lineage_mismatch: install_operator_id", {"install_operator_id": "op-f"}),
        ("lineage_mismatch: lineage_hash,verdict_limitations",
         {"verdict_limitations": ["reviewed"]}),  # the gate-lifted mark dropped
        ("lineage_unverified: runner_authority",
         {"release_id": "rel-f", "next_cycle": replace(
             receipt.claim.next_cycle, authority="release:rel-f").wire()}),
        ("lineage_unverified: next_lineage",
         {"next_cycle": forged_cycle, "next_candidate": {
             **nc, "optimizer_cycle": "cycle-forged"}}),
    ]  # fmt: skip
    for code, change in forgeries:
        fake = oi.UnverifiedLineage.from_wire(rehash({**w, **change}))
        with pytest.raises(TypeError):
            oi.next_cycle_run_id(rec, fake)  # type: ignore[arg-type]
        with pytest.raises(E, match=code):
            oi.verify_lineage(fake, reg, lg, BASE, DEV, nxt,
                              accept_synthetic_evidence=True)  # fmt: skip


@pytest.mark.usefixtures("gate_lifted_install")
def test_lineage_is_built_only_from_verified_recursion() -> None:
    reg, lg, r0, r1, nxt = recursion()
    with pytest.raises(E, match="lineage_unverified: synthetic_evidence"):
        oi.lineage("lin-1", reg, EXP, lg, r0, r1, BASE, DEV, nxt)  # no opt-in
    with pytest.raises(E, match=r"lineage_unverified: .*not_installed"):
        oi.lineage("lin-1", reg, EXP, deployment(), r0, r1, BASE, DEV, nxt,
                   accept_synthetic_evidence=True)  # fmt: skip
    with pytest.raises(E, match="lineage_incomplete: next_lineage"):
        oi.lineage("lin-1", reg, EXP, lg, r0, r1, BASE, DEV, None,
                   accept_synthetic_evidence=True)  # fmt: skip
    replay = replace(r1, proposals=r0.proposals, selected=r0.selected)
    assert replay.selected is None  # V0's cycle selected nothing
    with pytest.raises(E, match=r"lineage_incomplete: .*replayed_output"):
        oi.lineage("lin-1", reg, EXP, lg, r0, replay, BASE, DEV, nxt,
                   accept_synthetic_evidence=True)  # fmt: skip
    empty = replace(r1, proposals=(), selected=None)  # V1 selected nothing
    for n in (nxt, None):
        with pytest.raises(E, match="lineage_incomplete"):
            oi.lineage("lin-1", reg, EXP, lg, r0, empty, BASE, DEV, n,
                       accept_synthetic_evidence=True)  # fmt: skip


@pytest.mark.usefixtures("gate_lifted_install")
def test_lineage_refuses_self_promotion_and_lineage_breaks() -> None:
    lin = lineage_receipt()[3].claim
    r0, r1, h0 = lin.v0_cycle, lin.next_cycle, lin.optimizer_v0_hash
    early = replace(r1, at=r0.at - timedelta(minutes=1))
    v0_next = replace(r0, cycle_id="cycle-v0-2", authority="release:rel-opt-1")
    n = lin.next_candidate
    assert n is not None
    relabel = {"candidate_v1_hash": h0, "installed_v1_hash": h0, "next_cycle": v0_next}
    same = replace(V0, selection=oi.Selection.BEST_OF_BUDGET)  # V0's hash, new payload
    assert same.content_hash == h0 and oi.diff(V0, same) == ("selection",)
    relabel2 = relabel | {"next_cycle": replace(v0_next, config=same)}
    cases: list[tuple[str, dict[str, Any]]] = [
        ("self_certification", {"assessor": PRODUCER}),
        ("self_certification", {"assessor": replace(ASSESSOR, version="OPT-V1")}),
        ("self_certification", {"assessor": replace(ASSESSOR, family_id="family-a")}),
        ("self_approval", {"approval_principal_id": "optimizer-synth"}),
        ("self_approval", {"approval_principal_id": "Optimizer-Synth"}),
        ("self_approval", {"approval_principal_id": "Actor-Synth"}),  # the assessor
        ("self_install", {"install_operator_id": "optimizer-synth"}),
        ("self_install", {"install_operator_id": "OPTIMIZER-SYNTH"}),
        ("self_approved_mismatch", {"install_self_approved": True}),
        ("self_approved_mismatch", {"install_operator_id": "maint-synth"}),
        ("next_runner_not_v1", {"next_cycle": replace(r1, authority="baseline:x")}),
        ("next_runner_not_v1", {"release_id": "rel-opt-2"}),
        ("next_runner_not_v1", {"next_cycle": early}),
        ("next_runner_not_v1", {"next_cycle": v0_next}),  # V0 ran the next cycle
        ("next_runner_not_v1", {"next_cycle": replace(r1, runner_hash=h0)}),
        ("next_runner_not_v1", {"next_cycle": replace(r1, config=V0)}),
        ("lineage_break", {"installed_v1_hash": "e" * 64}),
        ("lineage_break", {"candidate_v1_hash": "e" * 64}),
        ("lineage_break", {"optimizer_v0_hash": "e" * 64}),
        ("lineage_break", {"producer": replace(PRODUCER, version="opt-v9")}),
        ("lineage_break", {"v0_cycle": replace(r0, runner_hash="e" * 64)}),
        ("diff_mismatch", {"diff": ("selection",)}),
        ("identical_relabel", relabel),
        ("identical_relabel", relabel2),  # same behaviour, relabelled payload
        ("lineage_incomplete", {"next_candidate": None}),
        ("next_lineage", {"next_candidate": replace(n, optimizer=h0)}),
        ("next_lineage", {"next_candidate": replace(n, optimizer_cycle="cycle-v0-1")}),
        ("next_lineage", {"next_candidate": replace(n, producer_version="opt-v0")}),
        ("next_lineage", {"next_candidate": replace(n, artifact_hash="e" * 64)}),
        (
            "lineage_incomplete",
            {"next_cycle": replace(r1, selected=None, proposals=())},
        ),
        (
            "lineage_incomplete",
            {
                "next_cycle": replace(r1, selected=None, proposals=()),
                "next_candidate": None,
            },
        ),
    ]
    for code, kw in cases:
        with pytest.raises(E, match=rf"lineage_refused: .*{code}"):
            replace(lin, **kw)
        if all(
            not isinstance(v, oi.CycleReceipt)  # else the cycle wire refuses
            or v.runner_hash == v.config.content_hash
            for v in kw.values()
        ):
            bad = {**lin.wire(), **{k: _wired(v) for k, v in kw.items()}}
            with pytest.raises(E, match=code):
                oi.UnverifiedLineage.from_wire(rehash(bad))  # forged hash: refused
    with pytest.raises(TypeError):
        replace(lin, next_candidate={"optimizer": h0})  # type: ignore[arg-type]
    solo = replace(lin, install_operator_id="maint-synth", install_self_approved=True)
    assert solo.refusals() == ()  # decision 23: recorded solo opt-in, not refused


def _wired(v: object) -> object:
    if isinstance(v, oi.CycleReceipt):
        return v.wire()
    if isinstance(v, im.Party):
        return [v.actor_id, v.route_id, v.family_id, v.version]
    if isinstance(v, oi.NextCandidate):
        return {k: getattr(v, k) for k in v.__dataclass_fields__}
    return list(v) if isinstance(v, tuple) else v


@pytest.mark.usefixtures("gate_lifted_install")
def test_lineage_wire_refusals() -> None:
    w: dict[str, Any] = lineage_receipt()[3].wire()
    nc = w["next_candidate"]
    v0_forged = {**w["v0_cycle"], "authority": "baseline:opt-vx"}
    cases: list[tuple[str, object]] = [
        ("wire_type", [w]),
        ("wire_keys", {**w, "approved": "yes"}),
        ("wire_keys", {k: v for k, v in w.items() if k != "diff"}),
        ("wire_value", {**w, "limitations": []}),  # provisional cannot be dropped
        ("wire_type", {**w, "producer": w["producer"][:3]}),
        ("wire_value", {**w, "producer": [*w["producer"][:3], "bad id"]}),
        ("wire_type", {**w, "diff": "operators"}),
        ("wire_type", {**w, "verdict_limitations": [1]}),
        ("wire_value: verdict_limitations", {**w, "verdict_limitations": []}),
        ("wire_type", {**w, "install_self_approved": "false"}),
        ("wire_type", {**w, "install_self_approved": 0}),
        ("wire_type", {**w, "release_id": 7}),
        ("wire_keys", {**w, "next_candidate": {**nc, "extra": "x"}}),
        ("wire_type", {**w, "next_candidate": {**nc, "optimizer": None}}),
        ("wire_hash", {**w, "installed_v1_hash": "E" * 64}),
        ("wire_type", {**w, "next_cycle": {**w["next_cycle"], "declared_cost": 1.5}}),
        ("hash_mismatch: receipt_hash", {**w, "v0_cycle": v0_forged}),
        ("hash_mismatch: lineage_hash", {**w, "approval_receipt_id": "stepup-x"}),
        ("hash_mismatch: lineage_hash", {**w, "lineage_hash": "e" * 64}),
    ]
    for code, bad in cases:
        refused(code, bad, oi.UnverifiedLineage)


@pytest.mark.usefixtures("gate_lifted_install")
def test_next_cycle_run_id_comes_only_from_bound_lineage() -> None:
    reg, _, _, lin = lineage_receipt()
    rec = optimizer_record(reg)
    assert (rec.target, rec.state) == (oi.OPT, im.RecordState.BLOCKED)  # gates hold
    assert oi.next_cycle_run_id(rec, None) is None  # never invented
    assert (
        oi.next_cycle_run_id(rec, lin) == "cycle-v1-1" == lin.claim.next_cycle.cycle_id
    )
    kws: tuple[dict[str, Any], ...] = (
        {"experiment_id": "exp-opt-2"}, {"protocol_id": "e" * 64},
        {"candidate_version": "cand-opt-2"}, {"candidate_hash": "e" * 64},
        {"parent_optimizer_version": "opt-v9"})  # fmt: skip
    for kw in kws:
        with pytest.raises(E, match="lineage_binding"):
            oi.next_cycle_run_id(replace(rec, **kw), lin)
    why = tuple(sorted({*rec.reasons, "none:parent_optimizer_version"}))
    other = replace(rec, target=im.Target.APPLICATION_CODE,
                    parent_optimizer_version=None, reasons=why)  # fmt: skip
    with pytest.raises(E, match="lineage_binding"):
        oi.next_cycle_run_id(other, lin)
    # N8: a parent optimizer version exists only on the optimizer target
    # (ExperimentRecord.__post_init__), so the target term cannot differ alone
    with pytest.raises(im.ImprovementError, match="record_inconsistent"):
        replace(rec, target=im.Target.APPLICATION_CODE, reasons=why)
    with pytest.raises(TypeError):
        oi.next_cycle_run_id(rec, lin.wire())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        oi.next_cycle_run_id(rec, lin.claim)  # type: ignore[arg-type]
