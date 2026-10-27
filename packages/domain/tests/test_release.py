"""Release signing, operator install, user adoption and rollback (T051 increment 1).
All data and keys are SYNTHETIC; the HMAC signer below is a test-only stand-in.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain import improvement as im
from qw_domain import release as rl
from qw_domain.strategy_registry import StrategyConsent, StrategyRegistry
from test_improvement import ART, GOOD, assess, cand, promo_ledger, proto, registry
from test_strategy_registry import manifest
from test_trial_ledger import T0, TENANT

SHARED = im.Scope.INSTALLATION
REL, INST, ADOPT = (T0 + timedelta(days=d) for d in (2, 3, 4))
ART2 = "e" * 64
M1 = manifest(implementation_hash=ART, version="0.2.0")
M2 = manifest(implementation_hash=ART2, version="0.3.0")
M3 = manifest(implementation_hash="f" * 64, version="0.4.0")
SID = M1.strategy_id
COMP = f"strategy:{SID}"
NO: StrategyRegistry = StrategyRegistry()  # no adoptions yet
USER_A, USER_B = "tenant-synth-a", "tenant-synth-b"
SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3]
     / "docs/spec/contracts/schemas/promotion_receipt.schema.json").read_text()
)  # fmt: skip


@dataclass(frozen=True)
class HmacTestKey:  # SYNTHETIC, test-only: symmetric, so never a production scheme
    key_id: str
    secret: bytes
    algorithm: str = "hmac-sha256-test"

    def sign(self, message: bytes) -> str:
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def verify(self, message: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(message), signature)


KEY = HmacTestKey("maint-key-synth-1", b"SYNTHETIC-test-secret")
FORGER = HmacTestKey("maint-key-synth-1", b"optimizer-forged-secret")
TRUST = rl.TrustedKey(KEY.key_id, KEY.algorithm, "maint-synth", KEY, T0)
KEYS = {KEY.key_id: TRUST}


def approval(at: datetime = REL, who: str = "maint-synth",
             step: str = "stepup-maint-1") -> rl.MaintainerApproval:  # fmt: skip
    return rl.MaintainerApproval(who, step, at)


def accepted(p: im.Protocol | None = None) -> im.ExperimentRegistry:
    """A shared strategy experiment with cand-1 assessed accept-for-review and
    children cand-2, cand-3 whose accept verdicts are recorded directly (T044 N3)."""
    reg = registry(p or proto(scope=SHARED))
    v = assess(reg, promo_ledger(GOOD))
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    reg = reg.record_verdict(v)
    for n, art in ((2, ART2), (3, M3.implementation_hash)):
        c = cand(f"cand-{n}", parent_id="cand-1", artifact_hash=art)
        reg = reg.register_candidate(c).record_verdict(replace(v,
            verdict_id=f"verdict-{n}", candidate_id=c.candidate_id,
            candidate_hash=c.content_hash))  # fmt: skip
    return reg


def signed(
    rid: str = "rel-1", compat: tuple[int, int, int] = (1, 2, 1)
) -> rl.SignedRelease:
    n = int(rid[-1])
    cid, m = f"cand-{n}", (M1, M2, M3)[n - 1]
    a = approval(step=f"stepup-maint-{rid}")
    man = rl.prepare_release(accepted(), cid, rid, rl.Compatibility(*compat), a, m)
    return rl.sign_release(man, KEY)


def deployment(edition: rl.Edition = rl.Edition.SELF_HOSTED,
               test_keys: bool = True) -> rl.DeploymentLedger:  # fmt: skip
    d = rl.Deployment("deploy-synth", edition, frozenset({"operator-synth"}),
                      frozenset({USER_A, USER_B}), 1, test_keys)  # fmt: skip
    return rl.DeploymentLedger(d, KEYS)


def strategies() -> StrategyRegistry:
    return StrategyRegistry().register(M1).register(M2)


def consent(step: str, at: datetime = ADOPT) -> StrategyConsent:
    return StrategyConsent("user-synth", step, at, "I adopt this version")


def installed(*rels: rl.SignedRelease) -> rl.DeploymentLedger:
    lg = deployment()
    for n, sr in enumerate(rels):
        lg = lg.install(f"ev-{n}", sr, "operator-synth", f"stepup-op-{n}",
                        INST + timedelta(minutes=n), strategies=NO)  # fmt: skip
    return lg


def valid(r: rl.PromotionReceipt) -> dict[str, object]:
    wire = r.to_wire()
    Draft202012Validator(SCHEMA).validate(wire)
    return wire


def test_approval_classes_map_to_receipt_authority_scopes() -> None:
    S, A, E = rl.AuthorityScope, im.ApprovalClass, rl.Edition
    assert {(a, e): rl.authority_scope(a, e) for a in A for e in E} == {
        (A.MAINTAINER_RELEASE, E.HOSTED): S.HOSTED_MAINTAINER,
        (A.MAINTAINER_RELEASE, E.SELF_HOSTED): S.HOSTED_MAINTAINER,
        (A.OPERATOR_INSTALL, E.HOSTED): S.HOSTED_MAINTAINER,
        (A.OPERATOR_INSTALL, E.SELF_HOSTED): S.SELF_HOSTED_OPERATOR,
        (A.USER_STRATEGY_ADOPTION, E.HOSTED): S.USER_STRATEGY_ADOPTION,
        (A.USER_STRATEGY_ADOPTION, E.SELF_HOSTED): S.USER_STRATEGY_ADOPTION,
    }
    kinds = SCHEMA["properties"]["change_kind"]["enum"]
    assert set(rl.CHANGE_KIND) == set(im.Target)
    assert set(rl.CHANGE_KIND.values()) == set(kinds)


def test_only_the_latest_accepted_shared_verdict_is_releasable() -> None:
    compat, reg = rl.Compatibility(1, 1, 1), accepted()
    man = rl.prepare_release(reg, "cand-1", "rel-1", compat, approval(), M1)
    assert (man.verdict_id, man.rollback_version_id) == ("verdict-1", "strat-v0")
    assert man.candidate_hash == reg.candidate("cand-1").content_hash
    assert man.user_adoption_required and man.component == COMP

    def refused(code: str, r: im.ExperimentRegistry = reg, **kw: object) -> None:
        args: dict[str, object] = {"reg": r, "candidate_id": "cand-1",
            "release_id": "rel-1", "compatibility": compat,
            "approval": approval(), "strategy": M1} | kw  # fmt: skip
        with pytest.raises(rl.ReleaseError, match=code):
            rl.prepare_release(**args)  # type: ignore[arg-type]

    v = reg.verdicts("cand-1")[0]
    later = replace(v, verdict_id="v-rej", decision=im.Decision.REJECT,
                    reasons=("goal:net_return",))  # fmt: skip
    refused("not_accepted", reg.record_verdict(later))
    refused("not_accepted", registry(proto(scope=SHARED)))  # no verdict
    refused("tenant_scope", accepted(proto()))  # R039: private fork is not shared
    for actor in ("optimizer-synth", "actor-synth"):  # producer and assessor
        refused("self_approval", approval=approval(who=actor))
    refused("time_order", approval=approval(at=v.assessed_at - timedelta(seconds=1)))
    refused("strategy_binding", strategy=None)
    refused("strategy_binding", strategy=M2)  # implementation is not cand-1
    with pytest.raises(rl.ReleaseError, match="compatibility"):
        rl.Compatibility(1, 1, 2)  # writes a schema it cannot run on


def _forged(target: im.Target) -> im.ExperimentRegistry:
    p = proto(scope=SHARED, target=target, sandbox_profile_id="sandbox-synth")
    c = cand(target=target)
    reg = im.ExperimentRegistry(TENANT).register(p).register_candidate(c)
    v = assess(registry(proto(scope=SHARED)), promo_ledger(GOOD))
    return reg.record_verdict(replace(v, candidate_hash=c.content_hash,
                                      protocol_hash=p.content_hash))  # fmt: skip


def test_sandboxed_and_tenant_only_targets_cannot_be_signed() -> None:
    compat = rl.Compatibility(1, 1, 1)
    for target in im.SANDBOX_TARGETS - rl.SIGNABLE_SANDBOX:  # R076: T007 unqualified
        with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
            rl.prepare_release(_forged(target), "cand-1", "r", compat, approval())
    assert {im.Target.RECURSIVE_OPTIMIZER} == rl.SIGNABLE_SANDBOX  # T050 inc. 7
    with pytest.raises(rl.ReleaseError, match="maintainer_release_not_required"):
        rl.prepare_release(_forged(im.Target.PERSONAL_ADAPTATION), "cand-1", "r",
                           compat, approval())  # fmt: skip
    man = rl.prepare_release(_forged(im.Target.RESEARCH), "cand-1", "r", compat,
                             approval())  # fmt: skip
    assert man.component == "research:research" and not man.user_adoption_required


def test_verification_binds_key_principal_manifest_and_signature() -> None:
    sr, at = signed(), INST
    assert rl.verify_release(sr, KEYS, at, True) == []
    assert rl.verify_release(sr, KEYS, at, False) == ["test_key_refused"]
    assert rl.verify_release(sr, {}, at, True) == ["untrusted_key"]
    forged = rl.sign_release(sr.manifest, FORGER)  # same key id, other secret
    assert rl.verify_release(forged, KEYS, at, True) == ["bad_signature"]
    for change in ({"compatibility": rl.Compatibility(1, 9, 9)},
                   {"approval": approval(who="optimizer-synth")},
                   {"required_approvals": ("operator_install",)}):  # fmt: skip
        tampered = replace(sr, manifest=replace(sr.manifest, **change))
        why = rl.verify_release(tampered, KEYS, at, True)
        assert {"manifest_hash", "bad_signature"} <= set(why)
    other = {KEY.key_id: replace(TRUST, principal_id="someone-else")}
    assert rl.verify_release(sr, other, at, True) == ["key_principal"]
    revoked = {KEY.key_id: replace(TRUST, revoked_at=at)}
    assert rl.verify_release(sr, revoked, at, True) == ["key_not_valid"]
    late = {KEY.key_id: replace(TRUST, valid_from=REL + timedelta(seconds=1))}
    assert rl.verify_release(sr, late, at, True) == ["key_not_valid"]


def test_maintainer_signature_and_install_never_adopt() -> None:
    lg = installed(signed())
    s, rcpt = rl.adopt_release(lg, strategies(), USER_A, "rel-1", {},
                               consent("stepup-user-a"), "prom-adopt-a")  # fmt: skip
    before = rl.strategy_state(lg, s, USER_A, SID, ADOPT)
    assert (before.state, before.version) == (rl.TenantState.ACTIVE, "0.2.0")
    lg2 = lg.install("ev-9", signed("rel-2"), "operator-synth", "stepup-op-9",
                     ADOPT + timedelta(hours=1), strategies=s)  # fmt: skip
    after = rl.strategy_state(lg2, s, USER_A, SID, ADOPT + timedelta(hours=2))
    assert after == before  # AT034: semantics unchanged until the user adopts
    assert rl.strategy_state(lg2, s, USER_B, SID, ADOPT + timedelta(hours=2)).state \
        is rl.TenantState.NOT_ADOPTED  # fmt: skip
    assert [r.version for r in s.adoptions[(USER_A, SID)]] == ["0.2.0"]
    w = valid(rcpt)
    assert w["authority_scope"] == "user_strategy_adoption"
    assert w["user_adoption_required"] is False and w["rollback_version_id"] == (
        "strat-v0"
    )


def test_install_is_a_separate_operator_act() -> None:
    sr, lg = signed(), deployment()

    def refused(code: str, ledger: rl.DeploymentLedger = lg, **kw: object) -> None:
        args: dict[str, object] = {"event_id": "ev-1", "sr": sr,
            "operator_id": "operator-synth", "step_up": "stepup-op-1",
            "at": INST, "strategies": NO} | kw  # fmt: skip
        with pytest.raises(rl.ReleaseError, match=code):
            ledger.install(**args)  # type: ignore[arg-type]

    refused("not_operator", operator_id=USER_A)  # AT039: a user cannot deploy
    refused("step_up_reused", step_up="stepup-maint-rel-1")
    refused("time_order", at=REL - timedelta(seconds=1))
    refused("unverified.*test_key_refused", deployment(test_keys=False))
    refused("schema_incompatible", sr=signed(compat=(2, 2, 2)))
    lg = lg.install("ev-1", sr, "operator-synth", "stepup-op-1", INST, strategies=NO)
    refused("release_conflict", lg, step_up="stepup-op-2")
    assert lg.schema == 1 and lg.head(COMP) == "rel-1"
    newer_first = installed(signed("rel-2", (1, 2, 2)), signed(compat=(1, 2, 1)))
    assert newer_first.schema == 2  # an older writer never downgrades state
    w = valid(lg.install_receipt("rel-1", "prom-install-1"))
    assert (w["authority_scope"], w["approved_by"]) == (
        "self_hosted_operator", "operator-synth")  # fmt: skip
    assert w["signature_reference"] == sr.signature_reference
    hosted = deployment(rl.Edition.HOSTED).install(
        "e", sr, "operator-synth", "s", INST, strategies=NO
    )
    assert valid(hosted.install_receipt("rel-1", "p"))["authority_scope"] == (
        "hosted_maintainer"
    )
    w = valid(rl.release_receipt(sr, "prom-release-1"))
    assert (w["authority_scope"], w["approved_by"]) == (
        "hosted_maintainer", "maint-synth")  # fmt: skip
    assert w["user_adoption_required"] is True


def test_adoption_is_explicit_per_tenant_and_binds_the_installed_version() -> None:
    lg, s = installed(signed()), strategies()

    def refused(code: str, **kw: object) -> None:
        args: dict[str, object] = {"ledger": lg, "strategies": s,
            "tenant_id": USER_A, "release_id": "rel-1", "config": {},
            "consent": consent("stepup-user-a"),
            "receipt_id": "prom-a"} | kw  # fmt: skip
        with pytest.raises((rl.ReleaseError, ValueError), match=code):
            rl.adopt_release(**args)  # type: ignore[arg-type]

    refused("tenant_not_in_deployment", tenant_id="tenant-synth-z")
    refused("not_installed", release_id="rel-2")
    for step in ("stepup-op-0", "stepup-maint-rel-1"):  # reused authority
        refused("step_up_reused", consent=consent(step))
    refused("time_order", consent=consent("stepup-u", INST - timedelta(seconds=1)))
    refused("strategy_binding", strategies=StrategyRegistry().register(
        replace(M1, implementation_hash="0" * 64)))  # fmt: skip
    s2, _ = rl.adopt_release(lg, s, USER_A, "rel-1", {}, consent("stepup-user-a"), "p")
    refused("step_up_reused", strategies=s2)  # one step-up, one adoption
    st = rl.strategy_state(lg, s2, USER_A, SID, ADOPT)
    assert st.adoption_hash == s2.adoptions[(USER_A, SID)][-1].receipt_hash
    assert rl.strategy_state(lg, s2, USER_B, SID, ADOPT).version is None


def test_compatible_rollback_is_a_new_event_and_substitutes_nothing() -> None:
    lg = installed(signed(), signed("rel-2", (1, 2, 2)))
    s, _ = rl.adopt_release(lg, strategies(), USER_A, "rel-1", {},
                            consent("stepup-user-a"), "p1")  # fmt: skip
    s, _ = rl.adopt_release(lg, s, USER_B, "rel-2", {}, consent("stepup-user-b"), "p2")
    at = ADOPT + timedelta(days=1)
    back = lg.rollback("ev-rb", COMP, "operator-synth", "stepup-op-rb", at,
                       strategies=s)  # fmt: skip
    assert back.entries[: len(lg.entries)] == lg.entries  # history untouched
    rb = back.entries[-1]
    assert isinstance(rb, rl.Rollback) and (rb.from_release_id, rb.to_release_id) == (
        "rel-2", "rel-1")  # fmt: skip
    assert back.head(COMP) == "rel-1" and back.schema == 2  # state never downgraded
    a, b = (rl.strategy_state(back, s, u, SID, at) for u in (USER_A, USER_B))
    assert (a.state, a.version) == (rl.TenantState.ACTIVE, "0.2.0")
    assert (b.state, b.version, b.reasons) == (rl.TenantState.PAUSED, "0.3.0",
                                               ("withdrawn",))  # fmt: skip
    s, r = rl.adopt_release(back, s, USER_B, "rel-1", {},
                            consent("stepup-user-b2", at), "p3")  # fmt: skip
    assert valid(r)["rollback_version_id"] == "0.3.0"
    assert rl.strategy_state(back, s, USER_B, SID, at).state is rl.TenantState.ACTIVE
    with pytest.raises(rl.ReleaseError, match=r"not_runnable.*withdrawn"):
        rl.adopt_release(back, s, USER_A, "rel-2", {},
                         consent("stepup-user-a2", at), "p4")  # fmt: skip


def test_unsupported_rollback_pauses_without_rewriting_records() -> None:
    lg = installed(signed(compat=(1, 1, 1)), signed("rel-2", (1, 2, 2)))
    s, _ = rl.adopt_release(lg, strategies(), USER_B, "rel-2", {},
                            consent("stepup-user-b"), "p")  # fmt: skip
    at = ADOPT + timedelta(days=1)
    adoptions = dict(s.adoptions)
    paused = lg.rollback("ev-rb", COMP, "operator-synth", "stepup-op-rb", at,
                         strategies=s)  # fmt: skip
    assert paused.entries[: len(lg.entries)] == lg.entries
    p = paused.entries[-1]
    assert isinstance(p, rl.Pause) and p.release_id == "rel-2"
    assert p.reasons == ("rel-1:schema_incompatible:2",)
    assert paused.head(COMP) == "rel-2" and paused.schema == 2  # no downgrade
    b = rl.strategy_state(paused, s, USER_B, SID, at)
    assert (b.state, b.version) == (rl.TenantState.PAUSED, "0.3.0")
    assert b.reasons == p.reasons and dict(s.adoptions) == adoptions
    assert [r.version for r in adoptions[(USER_B, SID)]] == ["0.3.0"]
    with pytest.raises(rl.ReleaseError, match="rollback_target"):
        lg.rollback("e", COMP, "operator-synth", "s", at, strategies=s,
                    to_release_id="rel-9")  # fmt: skip
    one = installed(signed())
    nothing = one.rollback("e", COMP, "operator-synth", "s", at, strategies=s).entries[
        -1
    ]
    assert isinstance(nothing, rl.Pause) and nothing.reasons == ("no_prior_release",)
    revoked = {KEY.key_id: replace(TRUST, revoked_at=at)}
    lg3 = rl.DeploymentLedger(lg.deployment, revoked, lg.entries)  # replays
    p3 = lg3.rollback("e", COMP, "operator-synth", "s", at, strategies=s).entries[-1]
    assert isinstance(p3, rl.Pause) and "rel-1:key_not_valid" in p3.reasons


def test_replay_refuses_forged_or_reordered_histories() -> None:
    sr, lg = signed(), installed(signed(), signed("rel-2", (1, 2, 2)))
    d, ev = lg.deployment, lg.entries
    first, second = ev[0], ev[1]
    assert isinstance(first, rl.Install) and isinstance(second, rl.Install)
    bad_sig = replace(first, release=rl.sign_release(sr.manifest, FORGER))
    reuse = replace(second, step_up_receipt_id="stepup-op-0")
    rb = rl.Rollback("r", COMP, "rel-1", "rel-2", "operator-synth", "x", ADOPT)
    pz = rl.Pause("z", COMP, "rel-1", ("why",), "operator-synth", "y", ADOPT)
    cases = {"unverified": (bad_sig,), "time_order": (ev[1], ev[0]),
             "step_up_reused": (first, reuse),
             "rollback": (*ev, rb), "pause": (*ev, pz),
             "not_operator": (replace(first, operator_id=USER_A),)}  # fmt: skip
    for code, entries in cases.items():
        with pytest.raises(rl.ReleaseError, match=code):
            rl.DeploymentLedger(d, KEYS, entries)


def test_review_s1_a_test_algorithm_cannot_pose_as_a_production_key() -> None:
    prod = {KEY.key_id: replace(TRUST, test_only=False)}
    assert not rl.PRODUCTION_ALGORITHMS  # until T009 reviews Ed25519
    assert rl.verify_release(signed(), prod, INST, False) == [
        "algorithm_not_production"
    ]


def test_review_s2_components_are_namespaced_by_target() -> None:
    reg = accepted()
    strat = replace(M1, strategy_id="research")
    a = rl.prepare_release(reg, "cand-1", "rel-s", rl.Compatibility(1, 2, 1),
                           approval(step="st-m1"), strat)  # fmt: skip
    b = rl.prepare_release(
        _forged(im.Target.RESEARCH),
        "cand-1",
        "rel-r",
        rl.Compatibility(1, 2, 1),
        approval(step="st-m2"),
    )
    assert (a.component, b.component) == ("strategy:research", "research:research")
    lg = deployment()
    for n, m in enumerate((a, b)):
        lg = lg.install(f"e{n}", rl.sign_release(m, KEY), "operator-synth", f"o{n}",
                        INST + timedelta(days=n), strategies=NO)  # fmt: skip
    one = lg.rollback(
        "rb",
        "research:research",
        "operator-synth",
        "o3",
        INST + timedelta(days=2),
        strategies=NO,
    ).entries[-1]
    assert isinstance(one, rl.Pause) and one.reasons == ("no_prior_release",)


def test_review_s3_each_step_up_authorises_one_act_only() -> None:
    lg = installed(signed())
    s, _ = rl.adopt_release(lg, strategies(), USER_A, "rel-1", {},
                            consent("stepup-user-a"), "p")  # fmt: skip
    later = ADOPT + timedelta(hours=1)
    with pytest.raises(rl.ReleaseError, match="step_up_reused"):
        lg.install("ev-9", signed("rel-2"), "operator-synth", "stepup-user-a", later,
                   strategies=s)  # fmt: skip
    lg = lg.install("ev-9", signed("rel-2"), "operator-synth", "o9", later,
                    strategies=s)  # fmt: skip
    with pytest.raises(rl.ReleaseError, match="step_up_reused"):
        lg.rollback("rb", COMP, "operator-synth", "stepup-user-a", later,
                    strategies=s)  # fmt: skip


def test_review_nits_duties_targets_receipts_keys_and_read_time() -> None:
    d = replace(deployment().deployment, operators=frozenset({"maint-synth"}))
    with pytest.raises(rl.ReleaseError, match="separation_of_duties"):  # N1
        rl.DeploymentLedger(d, KEYS).install("e", signed(), "maint-synth", "x", INST,
                                             strategies=NO)  # fmt: skip
    lg = installed(signed(compat=(1, 1, 1)), signed("rel-2", (1, 2, 2)))
    with pytest.raises(rl.ReleaseError, match=r"rollback_target.*schema"):  # N2
        lg.rollback("rb", COMP, "operator-synth", "o", ADOPT, strategies=NO,
                    to_release_id="rel-1")  # fmt: skip
    same = rl.DeploymentLedger(
        lg.deployment, KEYS, tuple(replace(e, at=INST) for e in lg.entries)
    )
    assert same.install_receipt("rel-1", "r").rollback_version_id == "strat-v0"
    lg = installed(signed(), signed("rel-2", (1, 2, 2)))  # N3: skips withdrawn
    lg = lg.rollback("rb", COMP, "operator-synth", "o", ADOPT, strategies=NO)
    lg = lg.install("e3", signed("rel-3", (1, 2, 2)), "operator-synth", "o3", ADOPT,
                    strategies=NO)  # fmt: skip
    assert lg.install_receipt("rel-3", "r").rollback_version_id == "rel-1"
    with pytest.raises(TypeError):  # N4
        lg.keys[KEY.key_id] = replace(TRUST, revoked_at=T0)  # type: ignore[index]

    class Boom:
        def verify(self, message: bytes, signature: str) -> bool:
            raise RuntimeError("SYNTHETIC verifier failure")

    boom = {KEY.key_id: replace(TRUST, verifier=Boom())}
    assert rl.verify_release(signed(), boom, INST, True) == ["bad_signature"]
    with pytest.raises(rl.ReleaseError, match="time_order"):  # N8
        rl.strategy_state(lg, NO, USER_A, SID, ADOPT - timedelta(seconds=1))


def _hand_built(target: im.Target, **kw: object) -> rl.SignedRelease:
    """A manifest assembled without `prepare_release` and signed with the trusted
    key: the bypass the T050 cross-model review found (SYNTHETIC)."""
    base = signed().manifest
    fields: dict[str, object] = {
        "release_id": f"rel-hand-{target.value}", "target": target,
        "component": f"{target.value}:{target.value}", "strategy_version": None,
        "strategy_material_hash": None,
        "approval": approval(step=f"stepup-hand-{target.value}"),
    } | kw  # fmt: skip
    return rl.sign_release(replace(base, **fields), KEY)  # type: ignore[arg-type]


def test_followup_install_rechecks_the_sandbox_gate() -> None:
    lg = installed(signed())
    for target in sorted(im.SANDBOX_TARGETS):  # R076: T007 still unqualified
        sr = _hand_built(target)
        assert rl.verify_release(sr, KEYS, INST, True) == ["sandbox_unqualified"]
        assert rl.verify_release(sr, {}, INST, True) == ["sandbox_unqualified",
                                                         "untrusted_key"]  # fmt: skip
        with pytest.raises(rl.ReleaseError, match=r"^sandbox_unqualified"):
            lg.install("ev-h", sr, "operator-synth", "stepup-op-h",
                       INST + timedelta(hours=1), strategies=NO)  # fmt: skip
        assert lg.head(sr.manifest.component) is None and len(lg.entries) == 1
        assert lg.schema == 1 and "stepup-op-h" not in lg.step_ups
        ev = rl.Install("ev-h", sr, "operator-synth", "stepup-op-h",
                        INST + timedelta(hours=1))  # fmt: skip
        with pytest.raises(rl.ReleaseError, match=r"^sandbox_unqualified"):
            rl.DeploymentLedger(lg.deployment, KEYS, (*lg.entries, ev))  # replay


def test_followup_install_rechecks_prepare_invariants_in_the_manifest() -> None:
    S, R = im.Target.STRATEGY, im.Target.RESEARCH
    m = signed().manifest
    named = {"strategy_version": m.strategy_version,
             "strategy_material_hash": m.strategy_material_hash}  # fmt: skip
    cases = {
        "maintainer_release_not_required": _hand_built(
            R, required_approvals=("operator_install",)
        ),
        "strategy_binding": _hand_built(S, component=COMP),  # no strategy version
        "component": _hand_built(R, component="strategy:research"),
    }
    more = (("strategy_binding", _hand_built(S, component=COMP,
                                             strategy_version="0.2.0")),
            ("component", _hand_built(S, component=f"research:{SID}", **named)),
            ("component", _hand_built(S, component="strategy:", **named)),
            ("component", _hand_built(R, component="research:other")))  # fmt: skip
    for code, sr in more:
        assert rl.verify_release(sr, KEYS, INST, True) == [code]
    lg = deployment()
    for code, sr in cases.items():
        assert rl.verify_release(sr, KEYS, INST, True) == [code]
        with pytest.raises(rl.ReleaseError, match=rf"^{code}"):
            lg.install("ev-h", sr, "operator-synth", "stepup-op-h", INST,
                       strategies=NO)  # fmt: skip
        assert lg.entries == ()
    odd = _hand_built(R, **named)  # a research manifest carrying strategy fields
    assert rl.verify_release(odd, KEYS, INST, True) == ["strategy_binding"]
    # Legitimate targets assembled the same way still install (no over-refusal).
    ok = _hand_built(R)
    assert rl.verify_release(ok, KEYS, INST, True) == []
    lg = lg.install("ev-ok", ok, "operator-synth", "stepup-op-ok", INST,
                    strategies=NO)  # fmt: skip
    assert lg.head("research:research") == ok.manifest.release_id


def test_review_manifest_types_and_two_rule_order() -> None:
    lg, at = deployment(), INST

    def install_code(sr: rl.SignedRelease) -> str:
        with pytest.raises(rl.ReleaseError) as e:
            lg.install("ev-h", sr, "operator-synth", "stepup-op-h", at, strategies=NO)
        return e.value.code

    base = _hand_built(im.Target.RESEARCH)
    bare = replace(base.manifest, target="research")  # type: ignore[arg-type]
    untyped = rl.SignedRelease(bare, "0" * 64, KEY.key_id, KEY.algorithm, "sig")
    assert rl.verify_release(untyped, KEYS, at, True) == ["target_type"]
    assert install_code(untyped) == "target_type"
    for approvals in ("maintainer_release", ["maintainer_release"],
                      ("maintainer_release", 1)):  # fmt: skip
        sr = _hand_built(im.Target.RESEARCH, required_approvals=approvals)
        assert rl.verify_release(sr, KEYS, at, True) == ["approvals_type"]
        assert install_code(sr) == "approvals_type"
    both = _hand_built(im.Target.APPLICATION_CODE,
                       required_approvals=("operator_install",))  # fmt: skip
    assert rl.verify_release(both, KEYS, at, True) == [
        "sandbox_unqualified", "maintainer_release_not_required"]  # fmt: skip
    assert install_code(both) == "sandbox_unqualified"
    assert lg.entries == ()


def test_decision_23_solo_maintainer_is_an_explicit_recorded_opt_in() -> None:
    one = frozenset({"maint-synth"})
    for edition, ops in (
        (rl.Edition.HOSTED, one),
        (rl.Edition.SELF_HOSTED, one | {"operator-synth"}),
        (rl.Edition.SELF_HOSTED, set()),
    ):
        with pytest.raises(rl.ReleaseError, match="solo_maintainer_refused"):
            rl.SoloMaintainer(edition, frozenset(ops))  # never hosted or multi-operator
    solo = rl.SoloMaintainer(rl.Edition.SELF_HOSTED, one)
    base = rl.Deployment("deploy-solo", rl.Edition.SELF_HOSTED, one,
                         frozenset({USER_A}), 1, True)  # fmt: skip

    def put(d: rl.Deployment, step: str = "stepup-op-0") -> rl.DeploymentLedger:
        return rl.DeploymentLedger(d, KEYS).install(
            "ev-0", signed(), "maint-synth", step, INST, strategies=NO
        )

    with pytest.raises(rl.ReleaseError, match="separation_of_duties"):
        put(base)  # without the opt-in the separation is kept
    two = replace(base, operators=one | {"operator-synth"}, solo=solo)
    with pytest.raises(rl.ReleaseError, match="solo_maintainer_refused"):
        rl.DeploymentLedger(two, KEYS)  # the opt-in must be the deployment's facts
    with pytest.raises(rl.ReleaseError, match="solo_maintainer_refused"):
        rl.DeploymentLedger(replace(base, solo=one), KEYS)  # type: ignore[arg-type]
    lg = put(replace(base, solo=solo))
    i = lg.entries[-1]
    assert isinstance(i, rl.Install) and i.self_approved  # recorded as self-approved
    with pytest.raises(rl.ReleaseError, match="step_up_reused"):
        put(replace(base, solo=solo), "stepup-maint-rel-1")  # still two step-ups
    n = installed(signed()).entries[0]
    assert isinstance(n, rl.Install) and not n.self_approved
    for d, forged in (
        (lg.deployment, replace(i, self_approved=False)),
        (n_dep := installed().deployment, replace(n, self_approved=True)),
    ):
        with pytest.raises(rl.ReleaseError, match="self_approved_mismatch"):
            rl.DeploymentLedger(d, KEYS, (forged,))
    assert n_dep.solo is None


def test_decision_33_verdicts_are_marked_caller_supplied() -> None:
    sr = signed()
    assert sr.verdict_source is rl.VerdictSource.CALLER_SUPPLIED
    lg = installed(sr)
    s, _ = rl.adopt_release(lg, strategies(), USER_A, "rel-1", {},
                            consent("stepup-user-a"), "p")  # fmt: skip
    a, b = (rl.strategy_state(lg, s, u, SID, ADOPT) for u in (USER_A, USER_B))
    assert (a.state, a.verdict_source) == (
        rl.TenantState.ACTIVE,
        rl.VerdictSource.CALLER_SUPPLIED,
    )
    assert (b.state, b.verdict_source) == (rl.TenantState.NOT_ADOPTED, None)
