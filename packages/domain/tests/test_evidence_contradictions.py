"""Contradictions between supported claims and their resolution (T036 increment 2).

SYNTHETIC sources, claims, tenants and feeds; feeds qualified through the real T021
registry with SYNTHETIC evidence. No network. The property's oracle is computed from
the generated scenario, never from the store.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ingest_rights_helper import KEEP, REGION, TENANT, qualified_registry
from qw_domain.evidence import (
    ClaimKind,
    EvidenceError,
    EvidenceStore,
    Reading,
    ResolutionState,
    SourceKind,
    Status,
)
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights
from qw_domain.rights import EntitlementHistory, EntitlementSource, UseScope

T0 = datetime(2026, 1, 5, tzinfo=UTC)
E1 = InstrumentId(UUID("00000000-0000-4000-8000-00000000e001"))
FY25 = Period(date(2025, 1, 1), date(2025, 12, 31))
OTHER_TENANT = "tenant-synth-b"
_BASE = qualified_registry(("feed-a",))
_ENT = _BASE.entitlements[(TENANT, "feed-a")].revisions[0]
REG = _BASE.with_entitlement(
    EntitlementHistory.new(OTHER_TENANT, "feed-a").revise(
        EntitlementSource.INSTALLATION_LICENSE,
        True,
        _ENT.rights,
        None,
        _ENT.recorded_at,
    )
)


def h(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


def reading(value: str, unit: str = "USD") -> Reading:
    return Reading(E1, "us-gaap:Revenues", FY25, unit, Decimal(value))


def supported_claim(
    store: EvidenceStore,
    cid: str,
    value: str,
    at: datetime = T0,
    tenant: str = TENANT,
    unit: str = "USD",
) -> str:
    """State a claim and cite its own SYNTHETIC source; returns the source id."""
    sid = f"s-{cid}-{value}"
    text = f"SYNTHETIC note {cid}: revenue {value} {unit} for FY2025."
    ing = IngestRights(REG, tenant, "feed-a", at, UseScope.PERSONAL, REGION, KEEP)
    store.add_source(ing, sid, SourceKind.PRIMARY, text, at)
    store.add_passage(tenant, f"p-{sid}", sid, 0, text, reading(value, unit), at)
    store.state_claim(tenant, cid, ClaimKind.REPORTED_FACT, reading(value, unit), at)
    store.cite(tenant, cid, f"p-{sid}", at)
    return sid


def status(
    store: EvidenceStore, cid: str, at: datetime, tenant: str = TENANT
) -> Status:
    return store.support(tenant, cid, at, REG).status


def test_disagreeing_supported_claims_are_contested_until_resolved() -> None:
    store = EvidenceStore()
    supported_claim(store, "c-1", "100")
    supported_claim(store, "c-3", "100")  # agrees with c-1
    supported_claim(store, "c-4", "90", unit="CAD")  # another key
    store.state_claim(TENANT, "c-5", ClaimKind.REPORTED_FACT, reading("70"), T0)
    assert status(store, "c-1", T0) is Status.SUPPORTED  # unsupported c-5 ignored
    supported_claim(store, "c-2", "90", h(1))
    (con,) = store.contradictions(TENANT, h(1), REG)
    assert con.members == frozenset({("c-1", 1), ("c-2", 1), ("c-3", 1)})
    assert con.values == frozenset({Decimal(100), Decimal(90)})
    assert (con.state, con.resolution) == (ResolutionState.UNRESOLVED, None)
    assert [status(store, c, h(1)) for c in ("c-1", "c-2", "c-3", "c-4")] == [
        Status.CONTESTED,
        Status.CONTESTED,
        Status.CONTESTED,
        Status.SUPPORTED,
    ]
    assert status(store, "c-1", T0) is Status.SUPPORTED  # as of before c-2
    rec = store.resolve(TENANT, con.members, "c-1", "analyst-synth", h(2), REG)
    assert (rec.preferred, rec.actor_id, rec.at) == ("c-1", "analyst-synth", h(2))
    assert status(store, "c-1", h(1)) is Status.CONTESTED  # before the resolution
    assert [status(store, c, h(2)) for c in ("c-1", "c-2", "c-3")] == [
        Status.SUPPORTED,
        Status.CONTESTED,  # the resolution rejected its value
        Status.SUPPORTED,
    ]
    (after,) = store.contradictions(TENANT, h(2), REG)
    assert (after.state, after.resolution) == (ResolutionState.RESOLVED, rec)


def test_resolution_is_refused_unless_current_attributed_and_ordered() -> None:
    store = EvidenceStore()
    supported_claim(store, "c-1", "100")
    supported_claim(store, "c-2", "90")
    (con,) = store.contradictions(TENANT, T0, REG)
    stale = frozenset({("c-1", 1)})
    for members, preferred, actor, code in (
        (stale, "c-1", "analyst-synth", "stale_contradiction"),
        (con.members, "c-9", "analyst-synth", "preferred"),
        (con.members, "c-1", " ", "actor"),
    ):
        with pytest.raises(EvidenceError, match=code):
            store.resolve(TENANT, members, preferred, actor, h(1), REG)
    store.resolve(TENANT, con.members, "c-2", "analyst-synth", h(2), REG)
    with pytest.raises(EvidenceError, match="time_order"):
        store.resolve(TENANT, con.members, "c-1", "analyst-synth", h(1), REG)
    store.resolve(TENANT, con.members, "c-1", "analyst-synth", h(3), REG)
    assert [r.preferred for r in store.resolutions(TENANT)] == ["c-2", "c-1"]
    assert status(store, "c-2", h(2)) is Status.SUPPORTED  # as of the first record
    assert status(store, "c-2", h(3)) is Status.CONTESTED  # latest record wins


def test_new_member_revision_or_lost_support_reopens() -> None:
    store = EvidenceStore()
    supported_claim(store, "c-1", "100")
    s2 = supported_claim(store, "c-2", "90")
    (con,) = store.contradictions(TENANT, T0, REG)
    store.resolve(TENANT, con.members, "c-1", "analyst-synth", h(1), REG)
    supported_claim(store, "c-3", "80", h(2))  # new member
    (again,) = store.contradictions(TENANT, h(2), REG)
    assert again.state is ResolutionState.UNRESOLVED
    assert status(store, "c-1", h(2)) is Status.CONTESTED
    store.resolve(TENANT, again.members, "c-1", "analyst-synth", h(3), REG)
    supported_claim(store, "c-3", "85", h(4))  # a cited revision of c-3
    (third,) = store.contradictions(TENANT, h(4), REG)
    assert ("c-3", 2) in third.members and third.state is ResolutionState.UNRESOLVED
    store.withdraw(TENANT, s2, h(5))  # c-2 loses support: members shrink
    (fourth,) = store.contradictions(TENANT, h(5), REG)
    assert fourth.members == frozenset({("c-1", 1), ("c-3", 2)})
    assert fourth.state is ResolutionState.UNRESOLVED
    assert status(store, "c-2", h(5)) is Status.UNSUPPORTED


def test_contradictions_are_tenant_isolated() -> None:
    store = EvidenceStore()
    supported_claim(store, "c-1", "100")
    supported_claim(store, "c-2", "90", tenant=OTHER_TENANT)  # same key elsewhere
    assert store.contradictions(TENANT, h(1), REG) == ()
    assert status(store, "c-1", h(1)) is Status.SUPPORTED
    supported_claim(store, "c-2", "90", h(1))
    (con,) = store.contradictions(TENANT, h(1), REG)
    with pytest.raises(EvidenceError, match="stale_contradiction"):
        store.resolve(OTHER_TENANT, con.members, "c-1", "analyst-synth", h(2), REG)
    assert store.resolutions(OTHER_TENANT) == ()
    assert status(store, "c-2", h(2), OTHER_TENANT) is Status.SUPPORTED


# Property: claim i states VALUES[v_i] and is supported from hour 1 if cited, until
# its source is withdrawn; claim 0 may be revised (uncited) at hour 6. A resolution
# is attempted at hour 4. The key is contested at q iff the supported claims hold two
# or more distinct values and no resolution recorded by q matches their member set.
VALUES = ("100", "250", "90")


@settings(max_examples=300, deadline=None)
@given(
    claims=st.lists(
        st.tuples(
            st.sampled_from((0, 1, 2, 1)),  # value index
            st.sampled_from((True, True, True, False)),  # cited
            st.sampled_from((None, None, None, 3, 5, 8)),  # withdrawn at hour
        ),
        min_size=2,
        max_size=4,
    ),
    revise=st.sampled_from((False, False, True)),
    attempt=st.sampled_from((True, True, False)),
    pick=st.integers(0, 3),
    query=st.integers(0, 10) | st.integers(4, 7),
)
def test_contested_iff_disagreeing_supported_claims_without_matching_resolution(
    claims: list[tuple[int, bool, int | None]],
    revise: bool,
    attempt: bool,
    pick: int,
    query: int,
) -> None:
    def supported(i: int, hour: int) -> bool:
        _, cited, w = claims[i]
        gone = (w is not None and w <= hour) or (i == 0 and revise and hour >= 6)
        return cited and hour >= 1 and not gone

    def members(hour: int) -> frozenset[tuple[str, int]]:
        return frozenset((f"c{i}", 1) for i in range(len(claims)) if supported(i, hour))

    def disagree(hour: int) -> bool:
        return len({claims[i][0] for i in range(len(claims)) if supported(i, hour)}) > 1

    store, sources = EvidenceStore(), {}
    for i, (v, cited, _) in enumerate(claims):
        text = f"SYNTHETIC note {i}: revenue {VALUES[v]} USD."
        ing = IngestRights(REG, TENANT, "feed-a", T0, UseScope.PERSONAL, REGION, KEEP)
        store.add_source(ing, f"s{i}", SourceKind.PRIMARY, text, T0)
        store.add_passage(TENANT, f"p{i}", f"s{i}", 0, text, reading(VALUES[v]), T0)
        store.state_claim(
            TENANT, f"c{i}", ClaimKind.REPORTED_FACT, reading(VALUES[v]), T0
        )
        sources[f"c{i}"] = cited
    events: list[tuple[int, str, int]] = [(1, "cite", i) for i in range(len(claims))]
    events += [(w, "withdraw", i) for i, (_, _, w) in enumerate(claims) if w]
    events += [(4, "zresolve", 0)] if attempt else []
    events += [(6, "revise", 0)] if revise else []
    resolved: frozenset[tuple[str, int]] | None = None
    preferred = ""
    for hour, kind, i in sorted(events):
        if kind == "cite" and sources[f"c{i}"]:
            store.cite(TENANT, f"c{i}", f"p{i}", h(hour))
        elif kind == "withdraw":
            store.withdraw(TENANT, f"s{i}", h(hour))
        elif kind == "revise":
            store.state_claim(TENANT, "c0", ClaimKind.REPORTED_FACT, reading("7"), h(6))
        elif kind == "zresolve":
            now = members(4)
            preferred = sorted(now)[pick % len(now)][0] if now else "c0"
            if disagree(4):
                store.resolve(TENANT, now, preferred, "analyst-synth", h(4), REG)
                resolved = now
            else:
                with pytest.raises(EvidenceError, match="stale_contradiction"):
                    store.resolve(TENANT, now, preferred, "analyst-synth", h(4), REG)
    matched = resolved is not None and query >= 4 and resolved == members(query)
    contested = disagree(query) and not matched
    cons = store.contradictions(TENANT, h(query), REG)
    states = {c.state for c in cons}
    assert (ResolutionState.UNRESOLVED in states) == contested
    assert len(cons) == int(disagree(query))
    pref_value = claims[int(preferred[1:])][0] if matched else None
    for i, (v, _, _) in enumerate(claims):
        want = Status.UNSUPPORTED
        if supported(i, query):
            lost = disagree(query) and (not matched or v != pref_value)
            want = Status.CONTESTED if lost else Status.SUPPORTED
        assert status(store, f"c{i}", h(query)) is want
