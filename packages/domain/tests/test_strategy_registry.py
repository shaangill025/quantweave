"""Strategy manifests, bounded configuration, qualification axes, adoption (T027).

SYNTHETIC manifests, tenants and evidence ids only. No strategy here is qualified for
real use and no result claims an edge. Hashes are compared for change or equality,
never against a copy of the implementation.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.instants import InstantError
from qw_domain.rights import Use
from qw_domain.strategy_registry import (
    TRANSITIONS,
    AdoptionState,
    AssetClass,
    Axis,
    AxisStep,
    DataNeed,
    Family,
    FamilyPolicy,
    Horizon,
    Parameter,
    ParamKind,
    QualEvidence,
    StrategyConsent,
    StrategyError,
    StrategyManifest,
    StrategyRegistry,
    StrategyStatus,
    status,
)
from qw_domain.strategy_registry import EligibilityState as U
from qw_domain.strategy_registry import EvidenceState as V
from qw_domain.strategy_registry import OperationalState as O

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
T = AT - timedelta(days=2)
TENANT = "tenant-synth-a"
ACTOR = "quant-reviewer-synth"
EV = (QualEvidence("synthetic-run-1", AT - timedelta(days=5), synthetic=False),)
SYN = (QualEvidence("synthetic-only-1", AT - timedelta(days=5), synthetic=True),)
D = Decimal
PARAMS = (
    Parameter("lookback", ParamKind.INTEGER, 126, minimum=20, maximum=252),
    Parameter("band", ParamKind.DECIMAL, D("0.05"), D("0.01"), D("0.2")),
    Parameter("review", ParamKind.CHOICE, "weekly", allowed=("weekly", "monthly")),
    Parameter("long_only", ParamKind.BOOLEAN, True),
)


def manifest(family: Family = Family.SWING_MOMENTUM, **kw: Any) -> StrategyManifest:
    model = "model-config-synth-1" if family is Family.AI_THESIS else None
    horizon = {
        Family.SWING_MOMENTUM: Horizon.SWING_POSITION,
        Family.OPENING_RANGE_BREAKOUT: Horizon.INTRADAY,
    }.get(family, Horizon.LONG_TERM)
    data = (DataNeed("feed-synth-bars", frozenset({Use.DERIVED_DATA})),)
    assets = frozenset({AssetClass.STOCKS, AssetClass.ETFS})
    base = StrategyManifest(
        f"str-synth-{family.value}", "0.1.0", family, "a" * 64, data, assets,
        horizon, True, PARAMS, "protocol-synth-1", model, "SYNTHETIC",
    )  # fmt: skip
    return replace(base, **kw)


def family_policy(family: Family, limited: bool = False, **kw: Any) -> FamilyPolicy:
    when = AT - timedelta(days=30)
    base = FamilyPolicy(family, f"fp-{family.value}", 1, "maint-synth", when, limited)
    return replace(base, **kw)


def consent(at: datetime = AT - timedelta(hours=1)) -> StrategyConsent:
    return StrategyConsent("user-1", "stepup-7", at, "I adopt this strategy")


def step(
    reg: StrategyRegistry,
    m: StrategyManifest,
    axis: Axis,
    to: Any,
    t: datetime = T,
    evidence: tuple[QualEvidence, ...] = EV,
    cfg: dict[str, Any] | None = None,
) -> StrategyRegistry:
    config = None if axis is Axis.OPERATIONAL else (cfg or {})
    tenant = TENANT if axis is Axis.ELIGIBILITY else None
    sid, v = m.strategy_id, m.version
    return reg.qualify(
        axis, sid, v, to, ACTOR, evidence, t, config=config, tenant=tenant
    )


def qualified(
    m: StrategyManifest,
    cfg: dict[str, Any] | None = None,
    *,
    reg: StrategyRegistry | None = None,
    limited_ok: bool = False,
) -> StrategyRegistry:
    reg = (reg or StrategyRegistry()).register(m)
    if m.family not in reg.family_policies:
        reg = reg.with_family_policy(family_policy(m.family, limited_ok))
    t = AT - timedelta(days=4)
    reg = step(reg, m, Axis.OPERATIONAL, O.PASSED, t)
    for state in (V.HISTORICAL, V.PROSPECTIVE_LIMITED, V.QUALIFIED_FOR_DECLARED_CLAIM):
        reg = step(reg, m, Axis.EVIDENCE, state, t, cfg=cfg)
    reg = step(reg, m, Axis.ELIGIBILITY, U.ELIGIBLE, t, cfg=cfg)
    return reg.adopt(TENANT, m.strategy_id, m.version, cfg or {}, consent())


def card(
    reg: StrategyRegistry, m: StrategyManifest, cfg: dict[str, Any] | None = None
) -> StrategyStatus:
    return status(reg, TENANT, m.strategy_id, m.version, cfg or {}, AT)


QUALIFIED = (
    O.PASSED,
    V.QUALIFIED_FOR_DECLARED_CLAIM,
    U.ELIGIBLE,
    AdoptionState.CURRENT,
)
FRESH = (O.NOT_TESTED, V.NONE, U.NOT_EVALUATED, AdoptionState.NONE)


def axes(s: StrategyStatus) -> tuple[Any, ...]:
    return (s.operational, s.evidence, s.eligibility, s.adoption)


# --- 1. manifests and bounded configuration (R061, AT061; R072) -------------------


def test_manifest_version_is_immutable() -> None:
    m = manifest()
    reg = StrategyRegistry().register(m)
    assert reg.register(m) is reg  # identical re-registration is a no-op
    with pytest.raises(StrategyError, match="immutable"):
        reg.register(replace(m, implementation_hash="b" * 64))


def test_config_bounds_and_defaults() -> None:
    m = manifest()
    resolved = m.resolve({"lookback": 200})
    assert resolved["lookback"] == 200 and resolved["band"] == D("0.05")
    bad: list[dict[str, Any]] = [
        {"lookback": 19},
        {"lookback": 253},
        {"lookback": True},  # bool is not an integer
        {"lookback": 126.0},
        {"band": 0.05},  # float refused
        {"band": D("0.009")},
        {"band": D("NaN")},
        {"review": "daily"},
        {"long_only": 1},
        {"unknown": 1},
    ]
    for cfg in bad:
        with pytest.raises(StrategyError, match="config"):
            m.resolve(cfg)
        with pytest.raises(StrategyError, match="config"):
            card(qualified(m), m, cfg)
    # Equal values hash equally regardless of representation; changes do not.
    assert m.config_hash({"band": D("0.050")}) == m.config_hash({})
    assert m.config_hash({"band": D("0.06")}) != m.config_hash({})


def test_manifest_rejects_unbounded_or_inconsistent_declarations() -> None:
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "bounded"),
        ({"minimum": 1}, "bounded"),
        ({"minimum": 5, "maximum": 1}, "bounded"),
        ({"minimum": 2, "maximum": 10}, "default"),
        ({"minimum": D(0), "maximum": D(9)}, "bounded"),  # bound type must match
    ]
    for kw, code in cases:
        with pytest.raises(StrategyError, match=code):
            Parameter("x", ParamKind.INTEGER, 1, **kw)
    with pytest.raises(StrategyError, match="bounded"):
        Parameter("x", ParamKind.CHOICE, "a")
    with pytest.raises(StrategyError, match="parameter"):
        manifest(parameters=(PARAMS[0], PARAMS[0]))
    with pytest.raises(StrategyError, match="horizon"):
        manifest(Family.OPENING_RANGE_BREAKOUT, horizon=Horizon.LONG_TERM)
    with pytest.raises(StrategyError, match="model_ref"):
        manifest(Family.AI_THESIS, model_ref=None)
    with pytest.raises(StrategyError, match="implementation_hash"):
        manifest(implementation_hash="not-a-hash")
    with pytest.raises(StrategyError, match="data"):
        DataNeed("feed-synth-bars", frozenset({Use.RETENTION}))  # no duration


# --- 2. three axes, research-only by default (R059, R065; AT059, AT065, AT084) ----


def test_catalogue_inclusion_starts_unqualified_and_unadopted() -> None:
    m = manifest()
    assert axes(card(StrategyRegistry().register(m), m)) == FRESH
    assert axes(card(qualified(m), m)) == QUALIFIED


def test_axes_are_independent_and_bound_to_the_configuration() -> None:
    m = manifest()
    reg = qualified(m)
    assert card(step(reg, m, Axis.OPERATIONAL, O.SUSPENDED), m).operational is (
        O.SUSPENDED
    )
    down = card(step(reg, m, Axis.EVIDENCE, V.HISTORICAL), m)
    assert (down.operational, down.evidence) == (O.PASSED, V.HISTORICAL)
    assert card(step(reg, m, Axis.ELIGIBILITY, U.INELIGIBLE), m).eligibility is (
        U.INELIGIBLE
    )
    # Another configuration inherits no evidence, eligibility or adoption (spec §7).
    other = card(reg, m, {"lookback": 60})
    assert axes(other) == (O.PASSED, V.NONE, U.NOT_EVALUATED, AdoptionState.STALE)
    # Eligibility is per tenant.
    s = status(reg, "tenant-synth-b", m.strategy_id, m.version, {}, AT)
    assert (s.eligibility, s.adoption) == (U.NOT_EVALUATED, AdoptionState.NONE)


def test_transitions_need_actor_evidence_and_legal_steps() -> None:
    m = manifest()
    reg = StrategyRegistry().register(m).with_family_policy(family_policy(m.family))
    ops, ref = Axis.OPERATIONAL, (m.strategy_id, m.version)
    late = (QualEvidence("syn-late", AT, synthetic=False),)  # after the step
    for evidence in ((), late):
        with pytest.raises(StrategyError, match="evidence"):
            step(reg, m, ops, O.PASSED, evidence=evidence)
    with pytest.raises(StrategyError, match="actor"):
        reg.qualify(ops, *ref, O.PASSED, "", EV, T)
    with pytest.raises(StrategyError, match="axis"):  # a state from another axis
        step(reg, m, ops, V.LIMITED)
    with pytest.raises(StrategyError, match="axis"):  # eligibility needs a tenant
        reg.qualify(Axis.ELIGIBILITY, *ref, U.ELIGIBLE, ACTOR, EV, T, config={})
    failed = step(reg, m, ops, O.FAILED)
    with pytest.raises(StrategyError, match="illegal_transition"):
        step(failed, m, ops, O.PASSED)
    with pytest.raises(StrategyError, match="illegal_transition"):  # no skipping
        step(reg, m, Axis.EVIDENCE, V.QUALIFIED_FOR_DECLARED_CLAIM)
    with pytest.raises(StrategyError, match="time_order"):
        step(step(reg, m, ops, O.PASSED), m, ops, O.PASSED, T - timedelta(1))
    future = card(step(reg, m, ops, O.PASSED, AT + timedelta(1)), m)
    assert future.operational is O.NOT_TESTED  # a later step is not yet known


def test_evidence_needs_a_predeclared_family_policy() -> None:
    m = manifest()
    reg = StrategyRegistry().register(m)
    with pytest.raises(StrategyError, match="predeclared"):
        step(reg, m, Axis.EVIDENCE, V.HISTORICAL)
    late = reg.with_family_policy(family_policy(m.family, True, approved_at=AT))
    with pytest.raises(StrategyError, match="predeclared"):  # approved after the step
        step(late, m, Axis.EVIDENCE, V.HISTORICAL)
    with pytest.raises(StrategyError, match="limited_not_permitted"):
        step(qualified(m), m, Axis.EVIDENCE, V.LIMITED)
    s = card(step(qualified(m, limited_ok=True), m, Axis.EVIDENCE, V.LIMITED), m)
    assert (s.evidence, s.limited_evidence_permitted) == (V.LIMITED, True)
    # A newer policy permitting limited evidence does not reach back to older steps.
    newer = family_policy(m.family, True, version=2, approved_at=AT - timedelta(1))
    reg = step(qualified(m), m, Axis.EVIDENCE, V.HISTORICAL).with_family_policy(newer)
    assert not card(reg, m).limited_evidence_permitted
    with pytest.raises(StrategyError, match="family_policy"):  # append in order
        reg.with_family_policy(replace(newer, version=1))
    with pytest.raises(StrategyError, match="family_policy"):
        family_policy(m.family, permits_limited_evidence=1)
    with pytest.raises(StrategyError, match="family_policy"):
        reg.with_family_policy("fp-forged")  # type: ignore[arg-type]


def test_inputs_are_frozen_and_parameter_order_is_not_material() -> None:
    params, allowed = list(PARAMS), ["weekly", "monthly"]
    kw: dict[str, Any] = {"allowed": allowed}
    choice = Parameter("review", ParamKind.CHOICE, "weekly", **kw)
    m = manifest(parameters=params)
    before = StrategyRegistry().register(m).manifests[(m.strategy_id, m.version)]
    params.pop()
    allowed.append("daily")
    assert before.material_hash == manifest().material_hash
    assert choice.check("daily") is not None
    assert manifest(parameters=PARAMS[::-1]).material_hash == m.material_hash
    with pytest.raises(StrategyError, match="allowed"):
        Parameter("r", ParamKind.CHOICE, "w", **(kw | {"allowed": "w"}))
    with pytest.raises(StrategyError, match="data"):
        DataNeed("feed-synth-bars", frozenset({Use.RETENTION}), timedelta(0))


def test_forged_histories_are_refused() -> None:
    m = manifest()
    reg = qualified(m)
    ev_key = (Axis.EVIDENCE.value, m.material_hash, m.config_hash({}))
    late = family_policy(m.family, version=2, approved_at=AT)
    forged = AxisStep(V.HISTORICAL, ACTOR, EV, T, late.content_hash)  # policy after
    fps = {m.family: (*reg.family_policies[m.family], late)}
    ops = reg.qualifications[(Axis.OPERATIONAL.value, m.material_hash)]
    (r,) = reg.adoptions[(TENANT, m.strategy_id)]
    for bad in (
        {"family_policies": fps, "qualifications": {ev_key: (forged,)}},
        {"qualifications": {(Axis.OPERATIONAL.value, "f" * 64): ops}},
        {"adoptions": {(TENANT, m.strategy_id): (replace(r, manifest_hash="f" * 64),)}},
        {"adoptions": {(TENANT, m.strategy_id): (r, replace(r, signed_at=T))}},
        {"adoptions": {("tenant-synth-b", m.strategy_id): (r,)}},
        {"family_policies": {Family.INCOME: reg.family_policies[m.family]}},
        {"manifests": {("other", "1"): m}},
    ):
        with pytest.raises(StrategyError, match="integrity"):
            replace(reg, **bad)
    with pytest.raises(StrategyError, match="time_order"):
        reg.adopt(TENANT, m.strategy_id, m.version, {}, consent(AT - timedelta(3)))


def test_synthetic_only_evidence_stays_limited() -> None:
    m = manifest()
    reg = qualified(m, limited_ok=True)
    with pytest.raises(StrategyError, match="synthetic"):
        step(reg, m, Axis.EVIDENCE, V.PROSPECTIVE_LIMITED, evidence=SYN)
    s = card(step(reg, m, Axis.EVIDENCE, V.LIMITED, evidence=SYN), m)
    assert s.evidence is V.LIMITED and s.evidence_synthetic_only
    mixed = card(step(reg, m, Axis.EVIDENCE, V.HISTORICAL, evidence=SYN + EV), m)
    assert not mixed.evidence_synthetic_only


# --- 3. adoption and material change (R034, R099, R024; AT034, AT099, AT024) ------


def test_adoption_receipt_binds_hashes_and_step_up() -> None:
    m = manifest()
    sid, v = m.strategy_id, m.version
    reg = qualified(m, {"lookback": 60})
    (receipt,) = reg.adoptions[(TENANT, sid)]
    assert receipt.kind == "user_strategy_adoption"
    assert receipt.manifest_hash == m.material_hash
    assert receipt.config_hash == m.config_hash({"lookback": 60})
    assert (receipt.principal_id, receipt.step_up_receipt_id) == ("user-1", "stepup-7")
    assert card(reg, m, {"lookback": 60}).adoption_hash == receipt.receipt_hash
    for bad in (
        replace(consent(), step_up_receipt_id=""),
        replace(consent(), principal_id="bad id"),
    ):
        with pytest.raises(StrategyError, match="consent"):
            reg.adopt(TENANT, sid, v, {}, bad)
    with pytest.raises(StrategyError, match="config"):
        reg.adopt(TENANT, sid, v, {"lookback": 1}, consent())
    # An adoption signed after the read time is not yet known.
    later = reg.adopt(TENANT, sid, v, {}, consent(AT + timedelta(1)))
    assert card(later, m, {"lookback": 60}).adoption is AdoptionState.CURRENT


MATERIAL: list[dict[str, Any]] = [
    {"implementation_hash": "c" * 64},
    {"parameters": (replace(PARAMS[0], maximum=300), *PARAMS[1:])},
    {"data": (DataNeed("feed-synth-other", frozenset({Use.DERIVED_DATA})),)},
    {"horizon": Horizon.SWING_POSITION},
    {"research_protocol_id": "protocol-synth-2"},
    {"asset_classes": frozenset({AssetClass.STOCKS, AssetClass.OPTIONS})},
    {"regular_session_only": False},
]


@pytest.mark.parametrize("change", MATERIAL)
def test_material_change_needs_requalification_and_readoption(
    change: dict[str, Any],
) -> None:
    m = manifest(Family.QUALITY_VALUE)
    v2 = replace(m, version="0.2.0", **change)
    assert v2.material_hash != m.material_hash
    reg = qualified(m).register(v2)
    stale = (O.NOT_TESTED, V.NONE, U.NOT_EVALUATED, AdoptionState.STALE)
    assert axes(card(reg, v2)) == stale
    assert axes(card(reg, m)) == QUALIFIED  # a new version leaves v1 as adopted
    assert axes(card(qualified(v2, reg=reg), v2)) == QUALIFIED  # after re-adoption


def test_model_change_does_not_inherit_ai_evidence() -> None:
    m = manifest(Family.AI_THESIS)
    v2 = replace(m, version="0.2.0", model_ref="model-config-synth-2")
    assert card(qualified(m).register(v2), v2).evidence is V.NONE


def test_display_only_change_keeps_adoption_and_qualification() -> None:
    m = manifest()
    v2 = replace(m, version="0.1.1", title="Renamed SYNTHETIC strategy")
    assert v2.material_hash == m.material_hash
    assert v2.content_hash != m.content_hash
    assert axes(card(qualified(m).register(v2), v2)) == QUALIFIED


def test_naive_datetimes_rejected() -> None:
    m = manifest()
    reg = qualified(m)
    naive = datetime(2026, 10, 9, 15)  # noqa: DTZ001 - the naive input under test
    for build in (
        lambda: status(reg, TENANT, m.strategy_id, m.version, {}, naive),
        lambda: step(reg, m, Axis.OPERATIONAL, O.PASSED, naive),
        lambda: reg.adopt(TENANT, m.strategy_id, m.version, {}, consent(naive)),
        lambda: QualEvidence("syn", naive, synthetic=False),
        lambda: family_policy(m.family, approved_at=naive),
    ):
        with pytest.raises(InstantError):
            build()


# --- 4. property: status follows exactly the legal transitions --------------------


@settings(max_examples=80, deadline=None)
@given(
    moves=st.lists(
        st.tuples(
            st.sampled_from([Axis.OPERATIONAL, Axis.EVIDENCE]),
            st.sampled_from([*O, *V]),
            st.booleans(),
        ),
        max_size=12,
    ),
    limited=st.booleans(),
)
def test_status_tracks_only_legal_evidenced_steps(
    moves: list[tuple[Axis, Any, bool]], limited: bool
) -> None:
    m = manifest()
    fp = family_policy(m.family, limited)
    reg = StrategyRegistry().register(m).with_family_policy(fp)
    expect: dict[Axis, Any] = {Axis.OPERATIONAL: O.NOT_TESTED, Axis.EVIDENCE: V.NONE}
    synthetic_only = False
    for axis, to, synthetic in moves:
        legal = isinstance(to, O if axis is Axis.OPERATIONAL else V) and (
            to in TRANSITIONS[expect[axis]]
        )
        too_strong = (
            axis is Axis.EVIDENCE and synthetic and to not in (V.NONE, V.LIMITED)
        ) or (to is V.LIMITED and not limited)
        try:
            reg = step(reg, m, axis, to, evidence=SYN if synthetic else EV)
        except StrategyError:
            assert not legal or too_strong
            continue
        assert legal and not too_strong
        expect[axis] = to
        if axis is Axis.EVIDENCE:
            synthetic_only = synthetic
    s = card(reg, m)
    assert (s.operational, s.evidence) == (
        expect[Axis.OPERATIONAL],
        expect[Axis.EVIDENCE],
    )
    assert s.evidence_synthetic_only == synthetic_only
    assert s.evidence is V.NONE or not synthetic_only or s.evidence is V.LIMITED
