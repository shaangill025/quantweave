"""Provider, feed, rights and entitlement registry and the use gate (T021).

SYNTHETIC providers, feeds, tenants and evidence ids only. No real provider is named
or approved here. Hashes are compared for change or equality, never against a copy of
the implementation.
"""

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.instants import InstantError
from qw_domain.rights import (
    FAIL_CLOSED,
    DenyCode,
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsError,
    RightsProfile,
    RightState,
    Transition,
    Use,
    UseDecision,
    UseScope,
    check_use,
)

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
LATER = AT + timedelta(days=365)
TENANT = "tenant-synth-a"
OTHER = "tenant-synth-b"
FEED = "feed-synth-eod"
PROVIDER = "provider-synth"
REGION = "CA-ON"
ACTOR = "operator-synth"
REV_AT = AT - timedelta(days=2)
PERMIT = Evidence(
    EvidenceKind.PERMISSION,
    "synthetic-permission-1",
    AT - timedelta(1),
    "counsel-synth",
)
AUTH = Evidence(EvidenceKind.AUTH, "synthetic-auth-1", AT - timedelta(hours=2))
WORKLOAD = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload-1", AT - timedelta(1))
QUAL = (WORKLOAD, PERMIT)
DOCS = Evidence(EvidenceKind.DOCS, "synthetic-docs-1", AT - timedelta(days=3))
KEEP = timedelta(days=30)


ALL = frozenset(Use)
NAME = "synthetic_eod"
LICENCE = EntitlementSource.INSTALLATION_LICENSE
CODE = {"unknown": DenyCode.UNKNOWN, "denied": DenyCode.DENIED}


def granted(use: Use | None = None, expires: datetime = LATER) -> Grant:
    keep = KEEP if use is Use.RETENTION else None
    return Grant(RightState.GRANTED, PERMIT, expires, retention=keep)


def bad(state: str, use: Use) -> Grant:
    if state == "unknown":
        return Grant(RightState.UNKNOWN)
    if state == "denied":
        return Grant(RightState.DENIED, PERMIT)
    return granted(use, expires=AT)  # expires exactly at the check time


def rights(**uses: Grant) -> RightsProfile:
    table = {u: granted(u) for u in Use} | {Use(k): g for k, g in uses.items()}
    return RightsProfile(table, {UseScope.PERSONAL: granted()}, {REGION: granted()})


def registered(
    profile: RightsProfile | None = None, uses: frozenset[Use] = ALL
) -> FeedHistory:
    return FeedHistory.new(FEED, PROVIDER).revise(
        NAME, uses, profile or rights(), REV_AT
    )


def feed(profile: RightsProfile | None = None, *, qualify: bool = True) -> FeedHistory:
    h = registered(profile).transition(
        FeedStatus.CONNECTED, ACTOR, (AUTH,), AT - timedelta(hours=1)
    )
    return h.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT) if qualify else h


def entitlement(
    profile: RightsProfile | None = None,
    source: EntitlementSource = LICENCE,
    consent: str | None = "synthetic-consent-1",
) -> EntitlementHistory:
    return EntitlementHistory.new(TENANT, FEED).revise(
        source, True, profile or rights(), consent, REV_AT
    )


def registry(
    f: FeedHistory | None = None, e: EntitlementHistory | None = None
) -> Registry:
    reg = Registry().with_provider(Provider(PROVIDER, "SYNTHETIC provider"))
    return reg.with_feed(f or feed()).with_entitlement(e or entitlement())


def decide(
    reg: Registry, use: Use, at: datetime = AT, *, tenant: str = TENANT, **kw: object
) -> UseDecision:
    args: dict[str, object] = {"scope": UseScope.PERSONAL, "jurisdiction": REGION}
    if use is Use.RETENTION:
        args["retain_for"] = KEEP
    decision = check_use(reg, tenant, FEED, use, at, **(args | kw))  # type: ignore[arg-type]
    assert decision.allowed is (not decision.reasons)
    return decision


def check(reg: Registry, use: Use, at: datetime = AT, **kw: object) -> set[DenyCode]:
    return {r.code for r in decide(reg, use, at, **kw).reasons}  # type: ignore[arg-type]


@pytest.mark.parametrize("use", list(Use))
def test_fully_granted_qualified_entitled_use_is_allowed(use: Use) -> None:
    decision = decide(registry(), use)
    assert decision.allowed and decision.reasons == ()
    assert decision.feed_hash is not None and decision.entitlement_hash is not None


@pytest.mark.parametrize("use", list(Use))
def test_fail_closed_registry_denies_every_use_and_cannot_qualify(use: Use) -> None:
    assert FAIL_CLOSED.grant(use).state is RightState.UNKNOWN
    with pytest.raises(RightsError) as err:
        feed(FAIL_CLOSED)
    assert err.value.code == "use_not_granted"
    codes = check(
        registry(feed(FAIL_CLOSED, qualify=False), entitlement(FAIL_CLOSED)), use
    )
    assert {DenyCode.NOT_QUALIFIED, DenyCode.UNKNOWN} <= codes


@pytest.mark.parametrize("state", ("unknown", "denied", "expired"))
@pytest.mark.parametrize("use", list(Use))
def test_entitlement_layer_blocks_each_use(use: Use, state: str) -> None:
    reg = registry(e=entitlement(rights(**{use.value: bad(state, use)})))
    assert CODE.get(state, DenyCode.EXPIRED) in check(reg, use)
    other = next(u for u in (Use.DISPLAY, Use.DERIVED_DATA) if u is not use)
    assert check(reg, other) == set()  # only the affected use is blocked


@pytest.mark.parametrize("use", list(Use))
def test_feed_grant_expiry_blocks_after_qualification(use: Use) -> None:
    assert check(registry(), use, LATER - timedelta(microseconds=1)) == set()
    assert DenyCode.EXPIRED in check(registry(), use, LATER)


@pytest.mark.parametrize("state", ("unknown", "denied"))
def test_feed_layer_unknown_or_denied_required_use_cannot_qualify(state: str) -> None:
    profile = rights(display=bad(state, Use.DISPLAY))
    with pytest.raises(RightsError, match="use_not_granted"):
        feed(profile)
    codes = check(registry(feed(profile, qualify=False)), Use.DISPLAY)
    assert {DenyCode.NOT_QUALIFIED, CODE[state]} <= codes


def test_evidence_and_status_are_taken_as_known_at_the_check_time() -> None:
    assert DenyCode.UNKNOWN in check(registry(), Use.DISPLAY, AT - timedelta(days=2))
    late = feed(qualify=False)
    late = late.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT + timedelta(1))
    assert check(registry(late), Use.DISPLAY) == {DenyCode.NOT_QUALIFIED}


def test_unknown_with_docs_and_a_review_date_is_still_denied() -> None:
    pending = Grant(RightState.UNKNOWN, DOCS, LATER)  # docs checked, permission pending
    reg = registry(e=entitlement(rights(display=pending)))
    assert check(reg, Use.DISPLAY) == {DenyCode.UNKNOWN}


def test_connected_is_not_qualified() -> None:
    connected = feed(qualify=False)
    assert connected.status is FeedStatus.CONNECTED
    assert check(registry(connected), Use.DISPLAY) == {DenyCode.NOT_QUALIFIED}


def test_registered_suspended_and_retired_block() -> None:
    assert check(registry(registered()), Use.DISPLAY) == {DenyCode.NOT_QUALIFIED}
    suspended = feed().transition(
        FeedStatus.SUSPENDED, ACTOR, (DOCS,), AT + timedelta(1)
    )
    assert check(registry(suspended), Use.DISPLAY, LATER - timedelta(1)) == {
        DenyCode.NOT_QUALIFIED
    }
    retired = suspended.transition(
        FeedStatus.RETIRED, ACTOR, (DOCS,), AT + timedelta(2)
    )
    assert check(registry(retired), Use.DISPLAY, AT + timedelta(3)) == {
        DenyCode.RETIRED
    }
    with pytest.raises(RightsError, match="illegal_transition"):
        retired.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT + timedelta(4))


def test_use_outside_the_qualified_set_is_not_qualified() -> None:
    narrow = registered(uses=frozenset({Use.DISPLAY}))
    narrow = narrow.transition(
        FeedStatus.CONNECTED, ACTOR, (AUTH,), AT - timedelta(hours=1)
    )
    narrow = narrow.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT)
    assert check(registry(narrow), Use.DISPLAY) == set()
    assert check(registry(narrow), Use.MODEL_PROCESSING) == {DenyCode.NOT_QUALIFIED}


def test_rights_revision_revokes_qualification_until_requalified() -> None:
    unknown = rights(display=Grant(RightState.UNKNOWN))
    revised = feed().revise(NAME, ALL, unknown, AT + timedelta(hours=1))
    assert revised.status is FeedStatus.QUALIFIED  # the gate checks the revision
    assert check(registry(revised), Use.DERIVED_DATA) == set()  # rev 2 not yet known
    after = AT + timedelta(hours=5)
    assert check(registry(revised), Use.DERIVED_DATA, after) == {DenyCode.NOT_QUALIFIED}
    again = revised.revise(NAME, ALL, rights(), AT + timedelta(hours=2))  # = rev 1
    assert again.current.revision == 3
    assert again.current.content_hash == again.revisions[0].content_hash
    assert check(registry(again), Use.DERIVED_DATA, after) == {DenyCode.NOT_QUALIFIED}
    with pytest.raises(RightsError, match="evidence_required"):  # stale evidence
        again.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT + timedelta(1))
    fresh = tuple(replace(e, recorded_at=AT + timedelta(hours=3)) for e in QUAL)
    for one in fresh:  # both workload and permission must postdate the revision
        with pytest.raises(RightsError, match="evidence_required"):
            again.transition(FeedStatus.QUALIFIED, ACTOR, (one, *QUAL), after)
    requalified = again.transition(FeedStatus.QUALIFIED, ACTOR, fresh, after)
    assert check(registry(requalified), Use.DERIVED_DATA, after) == set()
    with pytest.raises(RightsError, match="time_order"):
        again.revise(NAME, ALL, unknown, AT)


def test_not_entitled_unregistered_or_inactive() -> None:
    other = decide(registry(), Use.DISPLAY, tenant=OTHER)
    assert {r.code for r in other.reasons} == {DenyCode.NOT_ENTITLED}
    assert other.entitlement_hash is None
    inactive = entitlement().revise(LICENCE, False, rights(), None, AT + timedelta(1))
    assert check(registry(e=inactive), Use.DISPLAY) == set()  # deactivated later
    later = AT + timedelta(2)
    assert check(registry(e=inactive), Use.DISPLAY, later) == {DenyCode.NOT_ENTITLED}
    future = EntitlementHistory.new(TENANT, FEED)
    future = future.revise(LICENCE, True, rights(), None, AT + timedelta(1))
    assert check(registry(e=future), Use.DISPLAY) == {DenyCode.NOT_ENTITLED}
    assert check(registry(e=future), Use.DISPLAY, later) == set()
    assert check(Registry(), Use.DISPLAY) == {DenyCode.NOT_QUALIFIED}


def test_cross_tenant_pooling_denied_by_default() -> None:
    pool = Use.CROSS_TENANT_POOLING
    no_rights = check(registry(e=entitlement(FAIL_CLOSED)), pool)
    assert {DenyCode.UNKNOWN, DenyCode.POOLING_NOT_GRANTED} <= no_rights
    assert check(registry(e=entitlement(consent=None)), pool) == {
        DenyCode.POOLING_NOT_GRANTED
    }
    byo = entitlement(source=EntitlementSource.USER_CREDENTIAL)
    assert check(registry(e=byo), pool) == {DenyCode.POOLING_NOT_GRANTED}
    assert check(registry(e=byo), Use.DISPLAY) == set()  # BYO serves its own tenant
    assert check(registry(), pool) == set()  # explicit grant, licence and consent


def test_scope_jurisdiction_and_retention_duration() -> None:
    reg = registry()
    assert DenyCode.UNKNOWN in check(reg, Use.DISPLAY, scope=UseScope.COMMERCIAL)
    assert DenyCode.UNKNOWN in check(reg, Use.DISPLAY, jurisdiction="US-NY")
    over = KEEP + timedelta(seconds=1)
    assert check(reg, Use.RETENTION, retain_for=over) == {DenyCode.RETENTION_EXCEEDED}
    assert check(reg, Use.RETENTION, retain_for=None) == {DenyCode.RETENTION_EXCEEDED}
    with pytest.raises(RightsError, match="retain_for"):
        check(reg, Use.DISPLAY, retain_for=KEEP)


def test_revision_changes_hash_and_is_append_only() -> None:
    one = registered()
    assert one.revise(NAME, ALL, rights(), AT) is one
    other_ref = replace(PERMIT, ref_id="synthetic-permission-2")
    two = one.revise(
        NAME, ALL, rights(display=Grant(RightState.GRANTED, other_ref, LATER)), AT
    )
    assert [r.revision for r in two.revisions] == [1, 2]
    assert two.revisions[0] == one.revisions[0]
    assert two.current.content_hash != one.current.content_hash
    shorter = one.revise(
        NAME, ALL, rights(display=granted(expires=LATER - timedelta(1))), AT
    )
    assert shorter.current.content_hash != one.current.content_hash
    e1 = entitlement()
    e2 = e1.revise(LICENCE, True, rights(), None, AT)
    assert e2.current is not None and e1.current is not None
    assert (
        e2.current.revision == 2 and e2.current.content_hash != e1.current.content_hash
    )
    assert e1.revise(LICENCE, True, rights(), "synthetic-consent-1", AT) is e1
    with pytest.raises(RightsError, match="append_only"):
        registry(two).with_feed(shorter)
    with pytest.raises(RightsError, match="append_only"):
        registry(e=e2).with_entitlement(e1.revise(LICENCE, False, rights(), None, AT))


def test_transitions_require_evidence_and_an_actor() -> None:
    with pytest.raises(RightsError, match="evidence_required"):
        registered().transition(FeedStatus.CONNECTED, ACTOR, (DOCS,), AT)
    with pytest.raises(RightsError, match="illegal_transition"):
        registered().transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT)
    connected = feed(qualify=False)
    for evidence in ((), (DOCS,), (DOCS, PERMIT, AUTH), (WORKLOAD,)):
        with pytest.raises(RightsError, match="evidence_required"):
            connected.transition(FeedStatus.QUALIFIED, ACTOR, evidence, AT)
    with pytest.raises(RightsError, match="actor"):
        connected.transition(FeedStatus.QUALIFIED, " ", QUAL, AT)
    with pytest.raises(RightsError, match="evidence_required"):
        feed().transition(FeedStatus.SUSPENDED, ACTOR, (), AT)
    with pytest.raises(RightsError, match="time_order"):
        connected.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT - timedelta(1))
    late = replace(WORKLOAD, recorded_at=AT + timedelta(1))
    with pytest.raises(RightsError, match="evidence_required"):
        connected.transition(FeedStatus.QUALIFIED, ACTOR, (late,), AT)
    with pytest.raises(RightsError, match="no_revision"):
        FeedHistory.new(FEED, PROVIDER).transition(
            FeedStatus.CONNECTED, ACTOR, (AUTH,), AT
        )


def test_registry_replays_histories_and_refuses_forgeries() -> None:
    h = registered()
    step = Transition(FeedStatus.QUALIFIED, ACTOR, (), AT, 1, h.current.content_hash)
    rev = h.current
    for forged in (
        replace(h, transitions=(step,)),  # qualified with no evidence or connection
        replace(h, revisions=(replace(rev, content_hash="0" * 64),)),
        replace(h, revisions=(replace(rev, revision=2),)),
        replace(feed(), transitions=feed().transitions[1:]),
    ):
        with pytest.raises(RightsError, match="integrity"):
            registry(forged)
    e = entitlement()
    assert e.current is not None
    with pytest.raises(RightsError, match="integrity"):
        registry(e=replace(e, revisions=(replace(e.current, active=False),)))
    providers = {PROVIDER: Provider(PROVIDER, "SYNTHETIC provider")}
    reg = Registry(providers)
    providers.clear()
    assert PROVIDER in reg.providers
    with pytest.raises(TypeError):
        reg.providers["x"] = Provider("x", "x")  # type: ignore[index]
    with pytest.raises(RightsError, match="actor"):  # permission must be attributable
        Evidence(EvidenceKind.PERMISSION, "synthetic-permission-1", AT)


def test_grant_and_registry_validation() -> None:
    for evidence in (DOCS, None):  # documentation is not permission
        with pytest.raises(RightsError, match="permission_evidence"):
            Grant(RightState.GRANTED, evidence, LATER)
    with pytest.raises(RightsError, match="review_date"):
        Grant(RightState.GRANTED, PERMIT, None)
    for use, keep in ((Use.RETENTION, None), (Use.DISPLAY, KEEP)):
        with pytest.raises(RightsError, match="retention"):
            RightsProfile({use: Grant(RightState.GRANTED, PERMIT, LATER, keep)}, {}, {})
    with pytest.raises(RightsError, match="jurisdiction"):
        RightsProfile({}, {}, {"ontario": granted()})
    with pytest.raises(RightsError, match="provider"):
        Registry().with_feed(feed())
    with pytest.raises(RightsError, match="feed"):
        Registry().with_entitlement(entitlement())


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 10, 9, 15)  # noqa: DTZ001
    with pytest.raises(InstantError):
        check(registry(), Use.DISPLAY, naive)
    with pytest.raises(InstantError):
        Evidence(EvidenceKind.PERMISSION, "synthetic-permission-1", naive)
    with pytest.raises(InstantError):
        Grant(RightState.GRANTED, PERMIT, naive)
    with pytest.raises(InstantError):
        feed(qualify=False).transition(FeedStatus.QUALIFIED, ACTOR, QUAL, naive)


STATES = st.sampled_from(list(RightState))
USE_STATES = st.fixed_dictionaries({u: STATES for u in Use})


def _grant(state: RightState, use: Use | None) -> Grant:
    if state is RightState.GRANTED:
        return granted(use)
    return Grant(state, PERMIT if state is RightState.DENIED else None)


@settings(max_examples=300, deadline=None)
@given(
    USE_STATES,
    USE_STATES,
    st.frozensets(st.sampled_from(list(Use)), min_size=1),
    STATES,
    STATES,
)
def test_no_combination_with_a_required_use_unknown_is_allowed(
    feed_states: dict[Use, RightState],
    ent_states: dict[Use, RightState],
    required: frozenset[Use],
    scope_state: RightState,
    region_state: RightState,
) -> None:
    def profile(states: dict[Use, RightState]) -> RightsProfile:
        uses = {u: _grant(s, u) for u, s in states.items()}
        scopes = {UseScope.PERSONAL: _grant(scope_state, None)}
        return RightsProfile(uses, scopes, {REGION: _grant(region_state, None)})

    h = registered(profile(feed_states), required)
    h = h.transition(FeedStatus.CONNECTED, ACTOR, (AUTH,), AT - timedelta(hours=1))
    with contextlib.suppress(RightsError):
        h = h.transition(FeedStatus.QUALIFIED, ACTOR, QUAL, AT)
    reg = registry(h, entitlement(profile(ent_states)))
    some_required_unknown = any(feed_states[u] is RightState.UNKNOWN for u in required)
    for use in Use:
        allowed = not check(reg, use)
        assert not (allowed and some_required_unknown)
        if allowed:
            assert use in required
            assert feed_states[use] is ent_states[use] is RightState.GRANTED
            assert scope_state is region_state is RightState.GRANTED
