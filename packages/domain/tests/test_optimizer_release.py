"""Optimizer releases and rollback (T050 increment 7; R076, R092, R099; owner
decisions 23, 88 and 96). SYNTHETIC data and test-only HMAC keys only. The accept
verdict is hand-built (T044 `assess` blocks an optimizer target until T007), so a
release can be prepared and signed here; installing it is refused. Tests marked
GATE-LIFTED install it only through the `gate_lifted_install` fixture, which drops
the install gate for the optimizer in this process, to exercise decisions 88 and
96's rollback. Production keeps the gate.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest
import test_release as tr
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl
from test_improvement import ASSESSOR, PRODUCER
from test_optimizer_improvement import (
    EXP,
    GOOD,
    LIMITS,
    RUN,
    V0,
    V1,
    V_AT,
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
    self_change,
)
from test_release import FORGER, INST, KEY, KEYS, NO, approval, deployment, valid
from test_research_improvement import BASE, DEV

COMP, OPERATOR = oi.COMPONENT, "operator-synth"
RB = INST + timedelta(hours=1)


def accepted() -> im.ExperimentRegistry:
    """SYNTHETIC: V1's candidate with a hand-built accept verdict (T044 cannot
    produce one for an optimizer target before T007; `assess_measured` is gated)."""
    reg, sc = self_change()
    c, p = sc.candidate, reg.protocol(EXP)
    return reg.record_verdict(im.Verdict(
        "verdict-opt-1", EXP, c.candidate_id, c.content_hash, p.content_hash,
        ASSESSOR, im.Decision.ACCEPT_FOR_REVIEW, (), MappingProxyType(GOOD),
        ("trial-lifted-1",), (), ("gate_lifted_synthetic",), V_AT))  # fmt: skip


def prepared(
    rid: str = "rel-opt-1", who: str = "maint-synth",
    reg: im.ExperimentRegistry | None = None,
) -> rl.ReleaseManifest:  # fmt: skip
    a = approval(who=who, step=f"stepup-maint-{rid}")
    return rl.prepare_release(reg or accepted(), "cand-opt-1", rid,
                              rl.Compatibility(1, 1, 1), a)  # fmt: skip


def signed(rid: str = "rel-opt-1") -> rl.SignedRelease:
    return rl.sign_release(prepared(rid), KEY)


def test_an_optimizer_release_is_prepared_and_signed_but_never_installed() -> None:
    reg = accepted()
    m = prepared(reg=reg)
    c = reg.candidate("cand-opt-1")
    assert (m.target, m.component, m.rollback_version_id) == (oi.OPT, COMP, "opt-v0")
    assert (m.candidate_hash, m.artifact_hash) == (c.content_hash, c.artifact_hash)
    assert m.artifact_hash == V1.content_hash and m.strategy_version is None
    assert not m.user_adoption_required
    assert "maintainer_release" in m.required_approvals  # R099: a typed approval
    sr = rl.sign_release(m, KEY)
    assert rl.verify_release(sr, KEYS, INST, True, installing=False) == []
    assert rl.verify_release(sr, KEYS, INST, True) == ["sandbox_unqualified"]
    assert rl.manifest_refusals(m) == ["sandbox_unqualified"]
    code = tr._hand_built(im.Target.APPLICATION_CODE)  # review finding 4
    assert rl.verify_release(code, KEYS, INST, True, installing=False) == [
        "sandbox_unqualified"]  # fmt: skip
    assert valid(rl.release_receipt(sr, "rcpt-opt-1"))["change_kind"] == "optimizer"
    lg = deployment()
    with pytest.raises(rl.ReleaseError, match=r"^sandbox_unqualified"):
        lg.install("ev-0", sr, OPERATOR, "stepup-op-0", INST, strategies=NO)
    assert lg.entries == () and lg.head(COMP) is None
    ev = rl.Install("ev-0", sr, OPERATOR, "stepup-op-0", INST)
    with pytest.raises(rl.ReleaseError, match=r"^sandbox_unqualified"):
        rl.DeploymentLedger(lg.deployment, KEYS, (ev,))  # a stored row replays shut
    with pytest.raises(rl.ReleaseError, match="nothing_installed"):
        lg.rollback("ev-rb", COMP, OPERATOR, "stepup-op-rb", RB, strategies=NO)
    # V0 keeps the authority: nothing is installed
    assert oi.runner_authority(V0, reg, EXP, lg, RUN) == "baseline:opt-v0"


def test_optimizer_release_approval_is_separated_and_signature_bound() -> None:
    reg = accepted()
    for actor in (PRODUCER.actor_id, ASSESSOR.actor_id):  # R030: no self-approval
        with pytest.raises(rl.ReleaseError, match="self_approval"):
            prepared(who=actor, reg=reg)
    with pytest.raises(rl.ReleaseError, match="time_order"):
        rl.prepare_release(reg, "cand-opt-1", "rel-opt-1", rl.Compatibility(1, 1, 1),
                           approval(at=V_AT - timedelta(seconds=1)))  # fmt: skip
    rejected = replace(reg.verdicts("cand-opt-1")[-1], verdict_id="verdict-opt-2",
                       decision=im.Decision.REJECT, reasons=("goal:x",))  # fmt: skip
    with pytest.raises(rl.ReleaseError, match="not_accepted"):
        prepared(reg=reg.record_verdict(rejected))  # only the latest verdict
    sr = signed()
    forged = rl.sign_release(sr.manifest, FORGER)  # same key id, other secret
    assert rl.verify_release(forged, KEYS, INST, True, installing=False) == [
        "bad_signature"]  # fmt: skip
    widened = replace(sr, manifest=replace(sr.manifest,
                      compatibility=rl.Compatibility(1, 9, 9)))  # fmt: skip
    assert {"manifest_hash", "bad_signature"} <= set(
        rl.verify_release(widened, KEYS, INST, True, installing=False))  # fmt: skip
    assert rl.verify_release(sr, {}, INST, True, installing=False) == [
        "untrusted_key"]  # fmt: skip
    assert rl.verify_release(sr, KEYS, INST, False, installing=False) == [
        "test_key_refused"
    ]  # production refuses test keys (decision 33)


@pytest.mark.usefixtures("gate_lifted_install")
def test_gate_lifted_first_optimizer_rollback_returns_to_frozen_v0() -> None:
    """GATE-LIFTED SYNTHETIC: decision 88, rollback of the first installed V1."""
    reg, sr = accepted(), signed()
    lg = deployment().install("ev-0", sr, OPERATOR, "stepup-op-0", INST,
                              strategies=NO)  # fmt: skip
    syn = {"accept_synthetic_evidence": True}
    assert oi.runner_authority(V1, reg, EXP, lg, RUN, **syn) == "release:rel-opt-1"
    sup = oi.runner_authority(V0, reg, EXP, lg, RUN, **syn)
    assert isinstance(sup, oi.Blocked) and "superseded" in sup.reasons
    back = lg.rollback("ev-rb", COMP, OPERATOR, "stepup-op-rb", RB, strategies=NO)
    assert back.entries[:1] == lg.entries  # nothing earlier is rewritten
    rb = back.entries[-1]
    assert rb == rl.Rollback(
        "ev-rb", COMP, "rel-opt-1", None, OPERATOR, "stepup-op-rb", RB
    )  # recorded, never a Pause
    assert back.head(COMP) is None and back.schema == 1
    assert "withdrawn" in back.runnable("rel-opt-1", RB)
    again = rl.DeploymentLedger(back.deployment, back.keys, back.entries)  # replay
    assert again.head(COMP) is None and again.entries == back.entries
    assert oi.runner_authority(V0, reg, EXP, back, RUN, **syn) == "baseline:opt-v0"
    v1 = oi.runner_authority(V1, reg, EXP, back, RUN, **syn)
    assert isinstance(v1, oi.Blocked) and "not_installed" in v1.reasons
    r = oi.run_cycle("cycle-v0-2", V0, reg, EXP, back, BASE, DEV, LIMITS, (), RUN,
                     accept_synthetic_evidence=True)  # fmt: skip
    assert isinstance(r, oi.CycleReceipt) and r.authority == "baseline:opt-v0"
    with pytest.raises(rl.ReleaseError, match="nothing_installed"):
        back.rollback("ev-rb2", COMP, OPERATOR, "stepup-op-rb2", RB, strategies=NO)
    with pytest.raises(rl.ReleaseError, match="release_conflict"):
        back.install("ev-1", sr, OPERATOR, "stepup-op-1", RB, strategies=NO)
    with pytest.raises(rl.ReleaseError, match="step_up_reused"):
        lg.rollback("ev-rb", COMP, OPERATOR, "stepup-op-0", RB, strategies=NO)


@pytest.mark.usefixtures("gate_lifted_install")
def test_gate_lifted_every_optimizer_rollback_without_an_earlier_returns_to_v0() -> (
    None
):  # fmt: skip
    """GATE-LIFTED SYNTHETIC: decision 96, V1 -> V0 -> V1' -> V0; a usable earlier
    release is still preferred; forgeries and signer installs fail."""
    reg, one, two = accepted(), signed(), signed("rel-opt-2")
    lg = deployment()
    with pytest.raises(rl.ReleaseError, match=r"^unverified: .*bad_signature"):
        lg.install("ev-0", rl.sign_release(one.manifest, FORGER), OPERATOR,
                   "stepup-op-0", INST, strategies=NO)  # fmt: skip
    both = rl.DeploymentLedger(replace(lg.deployment, operators=frozenset(
        {OPERATOR, "maint-synth"})), KEYS)  # fmt: skip
    with pytest.raises(rl.ReleaseError, match="separation_of_duties"):  # decision 23
        both.install("ev-0", one, "maint-synth", "stepup-op-0", INST, strategies=NO)
    syn = {"accept_synthetic_evidence": True}
    at = [RB + timedelta(hours=h) for h in range(3)]
    lg = lg.install("ev-0", one, OPERATOR, "stepup-op-0", INST, strategies=NO)
    lg = lg.rollback("ev-rb-1", COMP, OPERATOR, "stepup-rb-1", at[0], strategies=NO)
    assert oi.runner_authority(V0, reg, EXP, lg, RUN, **syn) == "baseline:opt-v0"
    lg = lg.install("ev-1", two, OPERATOR, "stepup-op-1", at[1], strategies=NO)
    assert oi.runner_authority(V1, reg, EXP, lg, RUN, **syn) == "release:rel-opt-2"
    lg = lg.rollback("ev-rb-2", COMP, OPERATOR, "stepup-rb-2", at[2], strategies=NO)
    assert lg.entries[-1] == rl.Rollback("ev-rb-2", COMP, "rel-opt-2", None,
                                         OPERATOR, "stepup-rb-2", at[2])  # fmt: skip
    assert lg.head(COMP) is None and not any(
        isinstance(e, rl.Pause) for e in lg.entries)  # fmt: skip
    assert oi.runner_authority(V0, reg, EXP, lg, RUN, **syn) == "baseline:opt-v0"
    v1 = oi.runner_authority(V1, reg, EXP, lg, RUN, **syn)
    assert isinstance(v1, oi.Blocked) and "not_installed" in v1.reasons
    assert rl.DeploymentLedger(lg.deployment, KEYS, lg.entries).head(COMP) is None
    # with a usable earlier release, T051's rule still applies, not V0
    lg = deployment().install("ev-0", one, OPERATOR, "stepup-op-0", INST,
                              strategies=NO)  # fmt: skip
    lg = lg.install("ev-1", two, OPERATOR, "stepup-op-1", INST, strategies=NO)
    to_v0 = rl.Rollback("ev-rb", COMP, "rel-opt-2", None, OPERATOR, "stepup-op-rb", RB)
    with pytest.raises(
        rl.ReleaseError, match="rollback_target: earlier_release_usable"
    ):
        rl.DeploymentLedger(lg.deployment, KEYS, (*lg.entries, to_v0))
    back = lg.rollback("ev-rb", COMP, OPERATOR, "stepup-op-rb", RB, strategies=NO)
    assert back.entries[-1] == replace(to_v0, to_release_id="rel-opt-1")
    assert back.head(COMP) == "rel-opt-1"  # the earlier release, not V0


def test_a_strategy_head_never_rolls_back_to_a_baseline() -> None:
    lg = tr.installed(tr.signed())
    paused = lg.rollback("ev-rb", tr.COMP, OPERATOR, "stepup-op-rb", RB, strategies=NO)
    assert isinstance(paused.entries[-1], rl.Pause)  # T051: unsupported pauses
    forged = rl.Rollback("ev-rb", tr.COMP, "rel-1", None, OPERATOR, "stepup-op-rb", RB)
    with pytest.raises(rl.ReleaseError, match="rollback_target: no_frozen_baseline"):
        rl.DeploymentLedger(lg.deployment, KEYS, (*lg.entries, forged))
    with pytest.raises(rl.ReleaseError, match=r"^rollback\b"):
        rl.DeploymentLedger(lg.deployment, KEYS, (*lg.entries,
                            replace(forged, from_release_id="rel-2")))  # fmt: skip
