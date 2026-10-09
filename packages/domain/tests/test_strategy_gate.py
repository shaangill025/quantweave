"""Research-only and actionable strategy gates (T027 increment 2).

SYNTHETIC manifests, tenants, feeds, policies and evidence ids only. No strategy here
is qualified for real use and no result claims an edge. Each world is built from
explicit flags, and the property's oracle is computed from those flags, never from
the gate.
"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from qw_domain import rights as rt
from qw_domain.instants import InstantError
from qw_domain.onboarding import Catalogue, load_catalogue
from qw_domain.policy import (
    REQUIRED_METRICS,
    AccountFacts,
    AdoptionConsent,
    OptionPermission,
    PolicyHistory,
    build_draft,
)
from qw_domain.rights import Use, UseScope
from qw_domain.strategy_gate import (
    Capability,
    EvidenceRecord,
    GateCode,
    GateDecision,
    GateInputs,
    actionable,
    research_eligible,
)
from qw_domain.strategy_registry import (
    AssetClass,
    Axis,
    DataNeed,
    Family,
    FamilyPolicy,
    Horizon,
    Parameter,
    ParamKind,
    QualEvidence,
    StrategyConsent,
    StrategyManifest,
    StrategyRegistry,
)
from qw_domain.strategy_registry import EligibilityState as U
from qw_domain.strategy_registry import EvidenceState as V
from qw_domain.strategy_registry import OperationalState as O

CONFIG = Path(__file__).resolve().parents[3] / "docs/spec/config"
CAT: Catalogue = load_catalogue(
    json.loads((CONFIG / "onboarding_questions.json").read_text())
)
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
T = AT - timedelta(days=4)  # every qualification step
KNOWN = AT - timedelta(days=5)  # every evidence record
TENANT, OTHER = "tenant-synth-a", "tenant-synth-b"
FEED, REGION = "feed-synth-eod", "CA-ON"
LATER, LAG = AT + timedelta(days=365), timedelta(days=10)
PERMIT = rt.Evidence(
    rt.EvidenceKind.PERMISSION, "synth-permit", AT - timedelta(9), "law"
)
LADDER = (V.NONE, V.HISTORICAL, V.PROSPECTIVE_LIMITED, V.QUALIFIED_FOR_DECLARED_CLAIM)
LIMIT_ROWS = [
    {"metric": m, "value": "0.05", "unit": "ratio", "window": "rolling_30d",
     "denominator": "account_nav", "account_id": "acct-1", "currency": "CAD"}
    for m in sorted(REQUIRED_METRICS)
]  # fmt: skip
G = GateCode


def manifest(family: Family = Family.SWING_MOMENTUM, **kw: Any) -> StrategyManifest:
    horizon = {
        Family.SWING_MOMENTUM: Horizon.SWING_POSITION,
        Family.OPENING_RANGE_BREAKOUT: Horizon.INTRADAY,
    }.get(family, Horizon.LONG_TERM)
    model = "model-config-synth-1" if family is Family.AI_THESIS else None
    params = (Parameter("lookback", ParamKind.INTEGER, 126, minimum=20, maximum=252),
              Parameter("band", ParamKind.DECIMAL, Decimal("0"), Decimal("-1"),
                        Decimal("1")))  # fmt: skip
    data = (DataNeed(FEED, frozenset({Use.DERIVED_DATA})),)
    assets = frozenset({AssetClass.STOCKS, AssetClass.ETFS})
    base = StrategyManifest(
        f"str-synth-{family.value}", "0.1.0", family, "a" * 64, data, assets,
        horizon, True, params, "protocol-synth-1", model, "SYNTHETIC",
    )  # fmt: skip
    return replace(base, **kw)


def rights_registry(
    drop: frozenset[Use] = frozenset(), tenants: tuple[str, ...] = (TENANT,)
) -> rt.Registry:
    grant = rt.Grant(rt.RightState.GRANTED, PERMIT, LATER)
    keep = replace(grant, retention=timedelta(days=30))
    table = {u: keep if u is Use.RETENTION else grant for u in Use}
    profile = rt.RightsProfile(table, {UseScope.PERSONAL: grant}, {REGION: grant})
    uses = frozenset(Use) - drop  # a feed is qualified only for these uses
    auth = rt.Evidence(rt.EvidenceKind.AUTH, "synth-auth", AT - timedelta(8))
    work = rt.Evidence(rt.EvidenceKind.WORKLOAD, "synth-work", AT - timedelta(8))
    feed = (
        rt.FeedHistory.new(FEED, "provider-synth")
        .revise("synthetic_eod", uses, profile, AT - timedelta(10))
        .transition(rt.FeedStatus.CONNECTED, "op-synth", (auth,), AT - timedelta(8))
        .transition(
            rt.FeedStatus.QUALIFIED, "op-synth", (work, PERMIT), AT - timedelta(7)
        )
    )
    reg = rt.Registry().with_provider(rt.Provider("provider-synth", "SYNTHETIC"))
    reg = reg.with_feed(feed)
    for tenant in tenants:
        ent = rt.EntitlementHistory.new(tenant, FEED)
        lic = rt.EntitlementSource.INSTALLATION_LICENSE
        reg = reg.with_entitlement(ent.revise(lic, True, profile, None, AT - LAG))
    return reg


def investment_policy(
    tenant: str = TENANT,
    instruments: tuple[str, ...] = ("stocks", "etfs"),
    horizons: tuple[str, ...] = ("long_term", "swing_position"),
    ai_mode: str = "rules_only",
    signed_at: datetime = AT - timedelta(days=1),
    options: OptionPermission = OptionPermission.GRANTED,
    **answers: Any,
) -> PolicyHistory:
    raw: dict[str, Any] = {
        "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": ["growth"],
        "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
        "ONB10": "regular_session", "ONB11": list(instruments),
        "ONB12": ["same_day" if h == "intraday" else h for h in horizons],
        "ONB13": [{"account_id": "acct-1", "currency": "CAD", "amount": "5000"}],
        "ONB14": LIMIT_ROWS, "ONB16": ai_mode,
    } | answers  # fmt: skip
    facts = (AccountFacts("acct-1", options, has_short_option=False),)
    draft = build_draft(tenant, CAT.validate(raw), facts, as_of=date(2026, 10, 1))
    acks = frozenset(c.code for c in draft.conflicts)  # non-blocking ones only
    act = AdoptionConsent("user-1", "stepup-1", signed_at, acks, "I adopt")
    return PolicyHistory.new("pol-1", tenant).propose(draft).adopt(1, act)


@dataclass(frozen=True)
class World:
    reg: StrategyRegistry
    m: StrategyManifest
    inputs: GateInputs


def world(
    family: Family = Family.SWING_MOMENTUM,
    *,
    ops: O = O.PASSED,
    ev: V = V.QUALIFIED_FOR_DECLARED_CLAIM,
    synthetic: bool = False,
    permits: bool = False,
    superseded: bool = False,
    elig: U = U.ELIGIBLE,
    adoption: str = "current",
    record_ok: bool = True,
    fin_rec: bool = True,
    capability: bool = True,
    m: StrategyManifest | None = None,
    reg: StrategyRegistry | None = None,
    policy: PolicyHistory | None = None,
) -> World:
    m = m or manifest(family)
    sid, ver = m.strategy_id, m.version
    r = (reg or StrategyRegistry()).register(m)
    first = FamilyPolicy(
        m.family, "fp-synth", 1, "maint-synth", AT - timedelta(30),
        permits or ev is V.LIMITED,
    )  # fmt: skip
    if m.family not in r.family_policies:
        r = r.with_family_policy(first)

    def q(axis: Axis, to: Any, ref: str, syn: bool = False) -> StrategyRegistry:
        evidence = (QualEvidence(ref, KNOWN, synthetic=syn),)
        cfg: dict[str, Any] | None = None if axis is Axis.OPERATIONAL else {}
        tenant = TENANT if axis is Axis.ELIGIBILITY else None
        return r.qualify(
            axis, sid, ver, to, "quant-synth", evidence, T, config=cfg, tenant=tenant
        )

    ops_path = {O.PASSED: [O.PASSED], O.SUSPENDED: [O.PASSED, O.SUSPENDED],
                O.FAILED: [O.FAILED], O.NOT_TESTED: []}[ops]  # fmt: skip
    for o in ops_path:
        r = q(Axis.OPERATIONAL, o, f"ev-ops-{ver}")
    path = [ev] if ev is V.LIMITED else [*LADDER[1 : LADDER.index(ev) + 1]]
    for v in path if ev is not V.NONE else []:
        r = q(Axis.EVIDENCE, v, f"ev-inv-{ver}", synthetic)
    if superseded:
        later = AT - timedelta(1)
        second = replace(first, version=2, approved_at=later)
        r = r.with_family_policy(replace(second, permits_limited_evidence=permits))
    if elig is not U.NOT_EVALUATED:
        r = q(Axis.ELIGIBILITY, elig, f"ev-elig-{ver}")
    if adoption != "none":
        cfg = {} if adoption == "current" else {"lookback": 100}
        act = StrategyConsent("user-1", "stepup-7", AT - timedelta(hours=1), "I adopt")
        r = r.adopt(TENANT, sid, ver, cfg, act)
    mh, ch = m.material_hash, m.config_hash({})
    records = [
        EvidenceRecord(f"ev-ops-{ver}", mh, None, None, False, KNOWN),
        EvidenceRecord(f"ev-inv-{ver}", mh, ch, None, synthetic, KNOWN),
        EvidenceRecord(f"ev-elig-{ver}", mh, ch, TENANT, False, KNOWN),
    ]
    index = {e.ref_id: e for e in records if record_ok or e is not records[1]}
    caps = frozenset(Capability) if capability else frozenset()
    drop = frozenset() if fin_rec else frozenset({Use.FINANCIAL_RECOMMENDATION})
    inputs = GateInputs(
        rights_registry(drop), UseScope.PERSONAL, REGION,
        policy or investment_policy(), index, caps,
    )  # fmt: skip
    return World(r, m, inputs)


def decide(
    w: World, *, at: datetime = AT, tenant: str = TENANT, research: bool = False,
    cfg: dict[str, Any] | None = None, version: str | None = None, **inputs: Any,
) -> GateDecision:  # fmt: skip
    gate = research_eligible if research else actionable
    ins = replace(w.inputs, **inputs)
    d = gate(w.reg, tenant, w.m.strategy_id, version or w.m.version, cfg or {}, at, ins)
    assert d.allowed is (not d.reasons)
    return d


def codes(d: GateDecision) -> set[GateCode]:
    return {r.code for r in d.reasons}


# --- 1. the qualified, adopted, permitted case ------------------------------------


def test_fully_qualified_world_is_actionable() -> None:
    w = world()
    d = decide(w)
    assert d.allowed and d.reasons == ()
    assert d.manifest_hash == w.m.material_hash
    assert d.policy_hash is not None and d.adoption_hash is not None
    assert decide(w, research=True).allowed


# --- 2. each refusal reason, one precondition at a time ---------------------------

REFUSALS: list[tuple[dict[str, Any], GateCode]] = [
    ({"ops": O.NOT_TESTED}, G.OPERATIONAL_NOT_PASSED),
    ({"ops": O.SUSPENDED}, G.OPERATIONAL_NOT_PASSED),
    ({"ops": O.FAILED}, G.OPERATIONAL_NOT_PASSED),
    ({"ev": V.NONE}, G.EVIDENCE_INSUFFICIENT),
    ({"ev": V.HISTORICAL}, G.LIMITED_EVIDENCE_NOT_PERMITTED),
    ({"ev": V.PROSPECTIVE_LIMITED}, G.LIMITED_EVIDENCE_NOT_PERMITTED),
    ({"ev": V.LIMITED, "synthetic": True, "permits": True}, G.EVIDENCE_SYNTHETIC_ONLY),
    ({"superseded": True}, G.FAMILY_POLICY_SUPERSEDED),
    ({"elig": U.INELIGIBLE}, G.USER_NOT_ELIGIBLE),
    ({"elig": U.NOT_EVALUATED}, G.USER_NOT_ELIGIBLE),
    ({"adoption": "none"}, G.STRATEGY_NOT_ADOPTED),
    ({"adoption": "stale"}, G.ADOPTION_STALE),
    ({"record_ok": False}, G.EVIDENCE_UNVERIFIED),
    ({"fin_rec": False}, G.FEED_UNQUALIFIED),
    ({"capability": False}, G.CAPABILITY_MISSING),
]


@pytest.mark.parametrize(("flags", "code"), REFUSALS)
def test_single_failing_precondition_names_its_reason(
    flags: dict[str, Any], code: GateCode
) -> None:
    assert codes(decide(world(**flags))) == {code}


def test_limited_evidence_is_actionable_only_under_a_permitting_policy() -> None:
    assert decide(world(ev=V.HISTORICAL, permits=True)).allowed
    assert decide(world(ev=V.LIMITED, permits=True)).allowed
    # A permitting policy approved after the evidence step does not rescue it: the
    # evidence must be reassessed under the policy in force.
    w = world(ev=V.HISTORICAL, superseded=True, permits=True)
    assert codes(decide(w)) == {G.FAMILY_POLICY_SUPERSEDED}


@pytest.mark.parametrize("permits", [False, True])
def test_synthetic_only_evidence_is_never_actionable(permits: bool) -> None:
    w = world(ev=V.LIMITED, synthetic=True, permits=permits)
    assert G.EVIDENCE_SYNTHETIC_ONLY in codes(decide(w))
    assert decide(w, research=True).allowed


@pytest.mark.parametrize(
    ("policy", "code"),
    [
        (None, G.POLICY_NOT_ADOPTED),
        (investment_policy(OTHER), G.POLICY_NOT_ADOPTED),
        (investment_policy(signed_at=AT + timedelta(1)), G.POLICY_NOT_ADOPTED),
        (investment_policy(ONB01="hypothetical_only"), G.POLICY_SYNTHETIC),
        (investment_policy(instruments=("stocks",)), G.ASSET_CLASS_NOT_PERMITTED),
        (investment_policy(horizons=("long_term",)), G.HORIZON_NOT_PERMITTED),
    ],
)
def test_investment_policy_refusals(
    policy: PolicyHistory | None, code: GateCode
) -> None:
    w = world()
    assert codes(decide(w, policy=policy)) == {code}
    assert decide(w, policy=policy, research=True).allowed  # research needs no policy


def test_policy_receipt_must_match_its_version() -> None:
    p = investment_policy()
    forged = replace(p, receipts=(replace(p.receipts[0], policy_hash="f" * 64),))
    assert codes(decide(world(), policy=forged)) == {G.POLICY_NOT_ADOPTED}


def test_ai_thesis_needs_ai_mode_and_model_processing_rights() -> None:
    w = world(Family.AI_THESIS)
    assert codes(decide(w)) == {G.AI_MODE_NOT_PERMITTED}
    assert codes(decide(w, research=True)) == {G.AI_MODE_NOT_PERMITTED}
    on = investment_policy(ai_mode="ai_enabled")
    assert decide(w, policy=on).allowed
    no_model = rights_registry(frozenset({Use.MODEL_PROCESSING}))
    assert codes(decide(w, policy=on, rights=no_model)) == {G.FEED_UNQUALIFIED}
    assert codes(decide(w, policy=on, rights=no_model, research=True)) == {
        G.FEED_UNQUALIFIED
    }
    assert decide(world(), rights=no_model).allowed  # only ai_thesis needs it


def test_orb_is_regular_session_intraday_only() -> None:
    intraday = investment_policy(horizons=("long_term", "intraday"))
    w = world(Family.OPENING_RANGE_BREAKOUT, policy=intraday)
    assert decide(w).allowed
    ext = manifest(Family.OPENING_RANGE_BREAKOUT, regular_session_only=False)
    w2 = world(m=ext, policy=intraday)
    assert codes(decide(w2)) == {G.SESSION_NOT_PERMITTED}
    # The draft drops intraday without regular-session monitoring (ONB10).
    weekly = investment_policy(horizons=("long_term", "intraday"), ONB10="weekly")
    assert codes(decide(w, policy=weekly)) == {G.HORIZON_NOT_PERMITTED}


def test_options_need_policy_permission_and_option_terms() -> None:
    m = manifest(asset_classes=frozenset({AssetClass.STOCKS, AssetClass.OPTIONS}))
    w = world(m=m, policy=investment_policy(instruments=("stocks", "options")))
    assert decide(w).allowed
    family_only = frozenset({Capability.EXCHANGE_CALENDAR})
    assert codes(decide(w, capabilities=family_only)) == {G.CAPABILITY_MISSING}
    assert codes(decide(w, policy=investment_policy())) == {G.ASSET_CLASS_NOT_PERMITTED}


@pytest.mark.parametrize("perm", list(OptionPermission))
def test_option_strategies_need_an_account_permitted_for_options(
    perm: OptionPermission,
) -> None:
    m = manifest(asset_classes=frozenset({AssetClass.STOCKS, AssetClass.OPTIONS}))
    p = investment_policy(instruments=("stocks", "options"), options=perm)
    d = decide(world(m=m, policy=p))
    if perm is OptionPermission.GRANTED:
        assert d.allowed
    else:  # the draft excludes option proposals for the only account
        assert [(r.code, r.subject) for r in d.reasons] == [
            (G.ASSET_CLASS_NOT_PERMITTED, "options")
        ]


def test_rights_are_checked_for_declared_uses_in_research_too() -> None:
    w = world()
    d = decide(w, research=True, jurisdiction="US-NY")  # no grant for this region
    assert codes(d) == {G.JURISDICTION_NOT_PERMITTED}
    assert d.reasons[0].code is G.JURISDICTION_NOT_PERMITTED  # first in §7 order
    assert {r.subject for r in d.reasons} >= {f"{FEED}:derived_data:right_unknown"}


def test_evidence_record_must_bind_the_same_key_and_facts() -> None:
    w = world()
    good = dict(w.inputs.evidence)
    other_cfg = w.m.config_hash({"lookback": 100})
    for bad in (
        replace(good["ev-inv-0.1.0"], config_hash=other_cfg),
        replace(good["ev-inv-0.1.0"], material_hash="0" * 64),
        replace(good["ev-inv-0.1.0"], synthetic=True),
        replace(good["ev-inv-0.1.0"], recorded_at=KNOWN - timedelta(1)),
        replace(good["ev-elig-0.1.0"], tenant_id=OTHER),
    ):
        d = decide(w, evidence=good | {bad.ref_id: bad})
        assert codes(d) == {G.EVIDENCE_UNVERIFIED}, bad


# --- 3. research-only is strictly weaker ------------------------------------------


def test_research_eligible_does_not_imply_actionable() -> None:
    w = world(ev=V.NONE, elig=U.NOT_EVALUATED, adoption="none", fin_rec=False,
              ops=O.NOT_TESTED, capability=False)  # fmt: skip
    assert decide(w, research=True, policy=None).allowed
    expected = {G.OPERATIONAL_NOT_PASSED, G.EVIDENCE_INSUFFICIENT,
                G.USER_NOT_ELIGIBLE, G.STRATEGY_NOT_ADOPTED, G.FEED_UNQUALIFIED,
                G.CAPABILITY_MISSING, G.POLICY_NOT_ADOPTED}  # fmt: skip
    d = decide(w, policy=None)
    assert codes(d) == expected
    order = list(GateCode)
    assert [order.index(r.code) for r in d.reasons] == sorted(
        order.index(r.code) for r in d.reasons
    )  # reasons come in gate order (spec §7)


@pytest.mark.parametrize("ops", [O.FAILED, O.SUSPENDED])
def test_failed_or_suspended_code_is_not_research_eligible(ops: O) -> None:
    assert codes(decide(world(ops=ops), research=True)) == {G.OPERATIONAL_NOT_PASSED}


# --- 4. material change, tenants and decision time --------------------------------


def test_material_change_requires_requalification_and_readoption() -> None:
    w = world()
    v2 = replace(w.m, version="0.2.0", implementation_hash="b" * 64)
    w2 = world(m=v2, reg=w.reg, adoption="none")
    assert codes(decide(w2, version="0.2.0")) == {G.ADOPTION_STALE}
    assert decide(w, version="0.1.0").allowed  # the adopted version is unchanged
    act = StrategyConsent("user-1", "stepup-8", AT - timedelta(minutes=5), "I adopt")
    readopted = replace(w2, reg=w2.reg.adopt(TENANT, "str-synth-swing_momentum",
                                             "0.2.0", {}, act))  # fmt: skip
    assert decide(readopted, version="0.2.0").allowed
    both = dict(w.inputs.evidence) | dict(w2.inputs.evidence)
    d = decide(readopted, version="0.1.0", evidence=both)
    assert codes(d) == {G.ADOPTION_STALE}


def test_another_tenant_inherits_no_eligibility_or_adoption() -> None:
    w = world()
    rights = rights_registry(tenants=(TENANT, OTHER))
    d = decide(w, tenant=OTHER, rights=rights, policy=investment_policy(OTHER))
    assert codes(d) == {G.USER_NOT_ELIGIBLE, G.STRATEGY_NOT_ADOPTED}
    assert G.SOURCE_RIGHTS_DENIED in codes(decide(w, tenant=OTHER, policy=None))


def test_facts_after_the_decision_time_do_not_count() -> None:
    w = world()
    before_adoption = AT - timedelta(hours=2)
    assert codes(decide(w, at=before_adoption)) == {G.STRATEGY_NOT_ADOPTED}
    before_steps = T - timedelta(hours=1)
    d = decide(w, at=before_steps)
    assert {G.OPERATIONAL_NOT_PASSED, G.EVIDENCE_INSUFFICIENT,
            G.USER_NOT_ELIGIBLE} <= codes(d)  # fmt: skip


def test_family_policy_approved_after_decision_time_is_not_trusted() -> None:
    w = world(ev=V.HISTORICAL)
    later = FamilyPolicy(Family.SWING_MOMENTUM, "fp-synth", 2, "maint-synth",
                         AT + timedelta(hours=1), True)  # fmt: skip
    reg = w.reg.with_family_policy(later)
    w2 = replace(w, reg=reg)
    assert codes(decide(w2)) == {G.LIMITED_EVIDENCE_NOT_PERMITTED}
    assert G.FAMILY_POLICY_SUPERSEDED in codes(decide(w2, at=AT + timedelta(hours=2)))


def test_unknown_strategy_invalid_config_and_naive_time_fail_closed() -> None:
    w = world()
    assert codes(decide(w, version="9.9.9")) == {G.STRATEGY_UNKNOWN}
    assert codes(decide(w, cfg={"lookback": 1})) == {G.CONFIG_INVALID}
    assert codes(decide(w, cfg={"band": 0.5})) == {G.CONFIG_INVALID}
    with pytest.raises(InstantError):
        decide(w, at=AT.replace(tzinfo=None))


def test_negative_zero_hashes_as_zero() -> None:
    h = {
        x: manifest().config_hash({"band": Decimal(x)})
        for x in ["0", "-0", "-0.0", ".1", "-.1"]
    }
    assert h["0"] == h["-0"] == h["-0.0"] and h[".1"] != h["-.1"]


# --- 5. property: actionable iff every precondition holds -------------------------


# Each flag: its passing value and every value it may take. A drawn set of flags is
# varied; the rest pass, so examples cover both allowed and refused worlds.
FLAGS: dict[str, tuple[Any, st.SearchStrategy[Any]]] = {
    "ops": (O.PASSED, st.sampled_from(list(O))),
    "ev": (V.QUALIFIED_FOR_DECLARED_CLAIM, st.sampled_from(list(V))),
    "synthetic": (False, st.booleans()),
    "permits": (False, st.booleans()),
    "superseded": (False, st.booleans()),
    "elig": (U.ELIGIBLE, st.sampled_from(list(U))),
    "adoption": ("current", st.sampled_from(["none", "current", "stale"])),
    "record_ok": (True, st.booleans()),
    "fin_rec": (True, st.booleans()),
    "capability": (True, st.booleans()),
    "instruments": ({"stocks", "etfs"}, st.sets(st.sampled_from(["stocks", "etfs",
                                                "options"]), min_size=1)),
    "horizons": ({"long_term", "swing_position"},
                 st.sets(st.sampled_from(["long_term", "swing_position"]), min_size=1)),
}  # fmt: skip


@settings(max_examples=80, deadline=None)
@given(data=st.data())
def test_actionable_iff_every_precondition_holds(data: st.DataObject) -> None:
    varied = data.draw(st.sets(st.sampled_from(sorted(FLAGS)), max_size=3))
    f = {k: data.draw(gen) if k in varied else ok for k, (ok, gen) in FLAGS.items()}
    ev: V = f["ev"]
    f["synthetic"] = f["synthetic"] and ev is V.LIMITED  # synthetic caps at limited
    f["record_ok"] = f["record_ok"] or ev is V.NONE  # no evidence step to verify
    instruments, horizons = f.pop("instruments"), f.pop("horizons")
    policy = investment_policy(instruments=tuple(sorted(instruments)),
                               horizons=tuple(sorted(horizons)))  # fmt: skip
    w = world(**f, policy=policy)
    current_permits = f["permits"] or (ev is V.LIMITED and not f["superseded"])
    oracle = all(
        (
            f["ops"] is O.PASSED,
            ev is not V.NONE,
            not f["synthetic"],
            ev is V.QUALIFIED_FOR_DECLARED_CLAIM or current_permits,
            not f["superseded"],
            f["elig"] is U.ELIGIBLE,
            f["adoption"] == "current",
            f["record_ok"],
            f["fin_rec"],
            f["capability"],
            {"stocks", "etfs"} <= instruments,
            "swing_position" in horizons,
        )
    )
    event(f"allowed={oracle}")
    d = decide(w)
    assert d.allowed is oracle, codes(d)
    if d.allowed:  # actionable implies research eligibility
        assert decide(w, research=True).allowed
