"""Capability read model (T021 increment 3): every feature is disabled unless each of
its gates passes, and the rights gate is the T021 registry evaluated for one tenant
at one time. Every provider, feed, tenant and evidence id is SYNTHETIC; expected
reason lists are written out by hand."""

from datetime import UTC, datetime, timedelta

import pytest
from ingest_rights_helper import PERMIT, REGION, UNTIL, T, qualified_registry
from qw_domain.capabilities import FEATURES, Reason, evaluate
from qw_domain.release import Edition
from qw_domain.rights import (
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsProfile,
    RightState,
    Use,
    UseScope,
)
from qw_domain.sources import ID_PATTERN

A, B = "tenant-synth-a", "tenant-synth-b"
AT = datetime(2026, 10, 14, 12, tzinfo=UTC)
SELF = Edition.SELF_HOSTED
IDS = sorted(f.id for f in FEATURES)
RIGHTS = "provider_rights_unqualified"
MODEL, SANDBOX = "model_route_unqualified", "sandbox_unqualified"
STRATEGY, CLEARANCE = "strategy_unqualified", "commercial_clearance_pending"
EVIDENCE = "evaluation_evidence_unqualified"  # QUAL20
INDEPENDENCE = "evaluator_independence_unqualified"  # §1, §10
AI = [EVIDENCE, MODEL]
IMPROVE = [
    "experiment_budget_unqualified",
    "human_promotion_unqualified",
    "independent_assessment_unqualified",
    MODEL,
    SANDBOX,
]  # QUAL21
EMPTY_SELF_HOSTED = {
    "ai_evaluator": [EVIDENCE, INDEPENDENCE, MODEL, RIGHTS],
    "ai_research": [*AI, RIGHTS],
    "broker_connections": ["connection_unqualified"],
    "code_improvement": IMPROVE,
    "governed_chat": [*AI, RIGHTS],
    "intraday_signals": [RIGHTS, STRATEGY],
    "optimizer_recursion": IMPROVE,
    "options": ["opt_in_required", "option_feed_unqualified", RIGHTS, STRATEGY],
    "provider_data_display": [RIGHTS],
    "strategy_proposals": [RIGHTS, STRATEGY],
}


def display_only(tenant: str = A, until: datetime = UNTIL) -> Registry:
    """A SYNTHETIC feed qualified for display only; its rights and the tenant's
    entitlement grant every use, personal scope and REGION."""
    auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", T)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", T)
    grant = Grant(RightState.GRANTED, PERMIT, until)
    uses: dict[Use, Grant] = {u: grant for u in Use if u is not Use.RETENTION}
    rights = RightsProfile(uses, {UseScope.PERSONAL: grant}, {REGION: grant})
    feed = (
        FeedHistory.new("feed-synth", "provider-synth")
        .revise("synthetic_feed", frozenset({Use.DISPLAY}), rights, T)
        .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), T)
        .transition(FeedStatus.QUALIFIED, "operator-synth", (work, PERMIT), T)
    )
    ent = EntitlementHistory.new(tenant, "feed-synth").revise(
        EntitlementSource.INSTALLATION_LICENSE, True, rights, None, T
    )
    return (
        Registry()
        .with_provider(Provider("provider-synth", "SYNTHETIC provider"))
        .with_feed(feed)
        .with_entitlement(ent)
    )


def view(
    registry: Registry,
    tenant: str = A,
    at: datetime = AT,
    edition: Edition = SELF,
    region: str | None = REGION,
) -> dict[str, list[str]]:
    states = evaluate(registry, tenant, at, edition, region)
    assert [s.feature_id for s in states] == IDS  # every feature, sorted by id
    for s in states:
        assert s.enabled == (not s.reason_codes)
        assert list(s.reason_codes) == sorted(set(s.reason_codes))
    return {s.feature_id: list(s.reason_codes) for s in states}


def enabled(got: dict[str, list[str]]) -> list[str]:
    return [k for k, v in got.items() if not v]


def test_feature_ids_are_contract_ids_and_none_is_enabled_by_a_constant() -> None:
    assert len(IDS) == len(set(IDS)) == 10
    for f in FEATURES:
        assert ID_PATTERN.fullmatch(f.id) and len(f.id) <= 128
        assert f.uses or f.unrecorded, f.id  # a gateless feature would be constant
        assert Use.RETENTION not in f.uses  # retention needs a duration


def test_the_empty_registry_disables_everything() -> None:
    assert view(Registry()) == EMPTY_SELF_HOSTED


def test_a_display_only_qualification_enables_display_only() -> None:
    got = view(display_only())
    assert enabled(got) == ["provider_data_display"]
    # every other feature keeps exactly its empty-registry reasons: model
    # processing and financial recommendation are not qualified on this feed
    assert {k: v for k, v in got.items() if v} == {
        k: v for k, v in EMPTY_SELF_HOSTED.items() if k != "provider_data_display"
    }


def test_a_full_qualification_clears_only_the_rights_reason() -> None:
    got = view(qualified_registry(("feed-synth",), None, A))
    assert enabled(got) == ["provider_data_display"]
    assert got["ai_research"] == got["governed_chat"] == AI
    assert got["ai_evaluator"] == [EVIDENCE, INDEPENDENCE, MODEL]
    assert got["strategy_proposals"] == [STRATEGY]
    assert got["options"] == ["opt_in_required", "option_feed_unqualified", STRATEGY]
    assert got["broker_connections"] == ["connection_unqualified"]


def test_one_missing_use_keeps_the_features_that_need_it_disabled() -> None:
    reg = qualified_registry(("feed-synth",), Use.FINANCIAL_RECOMMENDATION, A)
    got = view(reg)
    assert got["strategy_proposals"] == [RIGHTS, STRATEGY]  # needs display AND it
    assert got["ai_research"] == AI
    assert enabled(got) == ["provider_data_display"]


def test_another_tenants_entitlement_enables_nothing() -> None:
    assert view(display_only(), tenant=B) == EMPTY_SELF_HOSTED


@pytest.mark.parametrize(
    "case",
    ["expired", "before_known", "other_region", "no_region", "suspended"],
)
def test_rights_are_evaluated_at_the_time_and_place_of_use(case: str) -> None:
    reg, at = display_only(), AT
    region: str | None = REGION
    if case == "expired":
        reg = display_only(until=AT)  # a grant is unusable at its expiry
    elif case == "before_known":
        at = T - timedelta(seconds=1)
    elif case == "other_region":
        region = "US"
    elif case == "no_region":
        region = None
    else:
        feed = reg.feeds["feed-synth"]
        stop = Evidence(EvidenceKind.WORKLOAD, "synthetic-outage", AT)
        suspended = feed.transition(FeedStatus.SUSPENDED, "operator-synth",
                                    (stop,), AT)  # fmt: skip
        reg = reg.with_feed(suspended)
    got = view(reg, at=at, region=region)
    expected = dict(EMPTY_SELF_HOSTED)
    if case == "no_region":
        expected = {k: sorted([*v, "jurisdiction_unknown"]) if RIGHTS in v else v
                    for k, v in expected.items()}  # fmt: skip
    assert got == expected


def test_hosted_has_no_tenant_residence_and_needs_commercial_clearance() -> None:
    got = view(qualified_registry(("feed-synth",), None, A), edition=Edition.HOSTED,
               region=None)  # fmt: skip
    assert enabled(got) == []
    assert got["provider_data_display"] == ["jurisdiction_unknown", RIGHTS]
    assert got["strategy_proposals"] == [
        CLEARANCE, "jurisdiction_unknown", RIGHTS, STRATEGY]  # fmt: skip
    assert got["broker_connections"] == ["connection_unqualified"]
    assert got["code_improvement"] == IMPROVE
    with pytest.raises(ValueError, match="jurisdiction"):
        evaluate(Registry(), A, AT, Edition.HOSTED, REGION)


def test_reasons_are_the_typed_vocabulary_and_time_must_be_aware() -> None:
    codes = {c for v in view(Registry(), edition=Edition.HOSTED, region=None).values()
             for c in v}  # fmt: skip
    assert codes <= {r.value for r in Reason}
    with pytest.raises(ValueError):
        evaluate(Registry(), A, datetime(2026, 10, 14), SELF, REGION)  # noqa: DTZ001


def test_uses_split_across_two_feeds_do_not_combine() -> None:
    """Display qualified on one feed and financial recommendation on another: no
    single feed covers both, so strategy_proposals keeps the rights reason."""
    show = display_only().feeds["feed-synth"]
    advice = qualified_registry(("feed-advice",), None, A)
    feed = advice.feeds["feed-advice"]
    rev = feed.current
    narrowed = (
        FeedHistory.new("feed-advice", "provider-synth")
        .revise(rev.feed, frozenset({Use.FINANCIAL_RECOMMENDATION}), rev.rights, T)
        .transition(FeedStatus.CONNECTED, "operator-synth",
                    feed.transitions[0].evidence, T)
        .transition(FeedStatus.QUALIFIED, "operator-synth",
                    feed.transitions[1].evidence, T)
    )  # fmt: skip
    reg = display_only().with_feed(show).with_feed(narrowed)
    reg = reg.with_entitlement(advice.entitlements[(A, "feed-advice")])
    got = view(reg)
    assert enabled(got) == ["provider_data_display"]
    assert got["strategy_proposals"] == [RIGHTS, STRATEGY]
    assert got["intraday_signals"] == [RIGHTS, STRATEGY]


@pytest.mark.parametrize("edition", ["self_hosted", "hosted", None])
def test_a_deployment_that_is_not_an_edition_is_refused(edition: object) -> None:
    with pytest.raises(ValueError, match="edition"):
        evaluate(Registry(), A, AT, edition, None)  # type: ignore[arg-type]
