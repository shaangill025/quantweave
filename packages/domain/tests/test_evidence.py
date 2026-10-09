"""Evidence store and claim graph (T036 increment 1).

Every source, passage, claim, tenant and feed is SYNTHETIC; feeds are qualified
through the real T021 registry with SYNTHETIC evidence. No network. Expectations are
hand-written; the Hypothesis property checks the store against an independent oracle
computed from the generated scenario, never from the store.
"""

import hashlib
from dataclasses import replace
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
    Invalid,
    Reading,
    SourceKind,
    Status,
)
from qw_domain.filings import Fact, Period, PublicationBasis, parse_unit
from qw_domain.identity import InstrumentId, Resolution
from qw_domain.ingest import IngestDenied, IngestRights
from qw_domain.instants import InstantError
from qw_domain.rights import EntitlementSource, Registry, Use, UseScope

T0 = datetime(2026, 1, 5, tzinfo=UTC)
E1 = InstrumentId(UUID("00000000-0000-4000-8000-00000000e001"))
E2 = InstrumentId(UUID("00000000-0000-4000-8000-00000000e002"))
FY25 = Period(date(2025, 1, 1), date(2025, 12, 31))
REV = "us-gaap:Revenues"
REG = qualified_registry(("feed-a", "feed-b"))
RELEASE = "SYNTHETIC release: Revenues were 100 USD for FY2025."
WIRE = "SYNTHETIC   RELEASE:  revenues were 100 usd for fy2025."  # same, re-typeset
OTHER = "SYNTHETIC filing note: total revenue 100 USD in fiscal 2025."


def h(hours: int) -> datetime:
    return T0 + timedelta(hours=hours)


def rights(
    feed: str = "feed-a",
    at: datetime = T0,
    keep: timedelta = KEEP,
    registry: Registry = REG,
    tenant: str = TENANT,
) -> IngestRights:
    return IngestRights(registry, tenant, feed, at, UseScope.PERSONAL, REGION, keep)


def fact(value: str = "100", **kw: object) -> Reading:
    base = Reading(E1, REV, FY25, "USD", Decimal(value))
    return replace(base, **kw)  # type: ignore[arg-type]


def sourced(
    store: EvidenceStore,
    sid: str,
    content: str = RELEASE,
    reading: Reading | None = None,
    **kw: object,
) -> str:
    """Add a source and one whole-content passage; returns the passage id."""
    store.add_source(rights(**kw), sid, SourceKind.PRIMARY, content, T0)  # type: ignore[arg-type]
    pid = f"p-{sid}"
    at = kw.get("at", T0)
    store.add_passage(TENANT, pid, sid, 0, content, reading or fact(), at)  # type: ignore[arg-type]
    return pid


def claimed(store: EvidenceStore, value: str = "100", cid: str = "c-1") -> None:
    store.state_claim(TENANT, cid, ClaimKind.REPORTED_FACT, fact(value), T0)


def status(store: EvidenceStore, at: datetime, cid: str = "c-1") -> Status:
    return store.support(TENANT, cid, at, REG).status


def test_supported_claim_with_stable_citation_and_hashes() -> None:
    store = EvidenceStore()
    src = store.add_source(rights(), "s-1", SourceKind.PRIMARY, RELEASE, T0)
    assert src.content_hash == hashlib.sha256(RELEASE.encode()).hexdigest()
    span = "Revenues were 100 USD"
    p = store.add_passage(TENANT, "p-1", "s-1", 19, span, fact(), T0)
    assert p.text_hash == hashlib.sha256(span.encode()).hexdigest()
    assert (p.start, p.end) == (19, 40)
    claimed(store)
    cit = store.cite(TENANT, "c-1", "p-1", h(1))
    assert store.cite(TENANT, "c-1", "p-1", h(2)) == cit  # idempotent, same id
    other = EvidenceStore()
    other.add_source(rights(), "s-1", SourceKind.PRIMARY, RELEASE, T0)
    other.add_passage(TENANT, "p-1", "s-1", 19, span, fact(), T0)
    claimed(other)
    assert other.cite(TENANT, "c-1", "p-1", h(5)).citation_id == cit.citation_id
    rep = store.support(TENANT, "c-1", h(1), REG)
    assert rep.status is Status.SUPPORTED and rep.valid == (cit.citation_id,)
    assert (rep.independent_sources, rep.corroborated) == (1, False)
    assert status(store, T0) is Status.UNSUPPORTED  # citation not yet linked


@pytest.mark.parametrize(
    ("stated", "code"),
    [
        (fact(entity=E2), "entity_mismatch"),
        (fact(attribute="us-gaap:NetIncomeLoss"), "attribute_mismatch"),
        (fact(period=Period(date(2024, 1, 1), date(2024, 12, 31))), "period_mismatch"),
        (fact(period=Period(None, date(2025, 12, 31))), "period_mismatch"),
        (fact(unit="CAD"), "unit_mismatch"),
        (fact("100000"), "value_mismatch"),
    ],
)
def test_bad_citation_is_refused_never_stored(stated: Reading, code: str) -> None:
    store = EvidenceStore()
    pid = sourced(store, "s-1")
    store.state_claim(TENANT, "c-1", ClaimKind.REPORTED_FACT, stated, T0)
    with pytest.raises(EvidenceError) as err:
        store.cite(TENANT, "c-1", pid, h(1))
    assert err.value.code == code
    rep = store.support(TENANT, "c-1", h(2), REG)
    assert rep.status is Status.UNSUPPORTED and rep.valid == () and rep.invalid == {}


def test_equal_decimal_values_match() -> None:
    store = EvidenceStore()
    pid = sourced(store, "s-1")
    claimed(store, "100.00")
    store.cite(TENANT, "c-1", pid, h(1))
    assert status(store, h(1)) is Status.SUPPORTED


@pytest.mark.parametrize(
    ("start", "quote", "code"),
    [(0, "Revenues", "span_mismatch"), (60, "x", "span_mismatch"), (0, "", "span")],
)
def test_passage_must_be_an_exact_span(start: int, quote: str, code: str) -> None:
    store = EvidenceStore()
    store.add_source(rights(), "s-1", SourceKind.PRIMARY, RELEASE, T0)
    with pytest.raises(EvidenceError) as err:
        store.add_passage(TENANT, "p-1", "s-1", start, quote, fact(), T0)
    assert err.value.code == code


def test_syndicated_copies_are_one_source_not_corroboration() -> None:
    store = EvidenceStore()
    claimed(store)
    a = sourced(store, "s-a", RELEASE)
    b = sourced(store, "s-b", WIRE, feed="feed-b")
    for pid in (a, b):
        store.cite(TENANT, "c-1", pid, h(1))
    rep = store.support(TENANT, "c-1", h(1), REG)
    assert len(rep.valid) == 2
    assert (rep.independent_sources, rep.corroborated) == (1, False)
    store.add_source(
        rights("feed-b", h(2)), "s-c", SourceKind.PRIMARY, OTHER, h(2), "s-a"
    )  # declared syndication of s-a with different wording
    store.add_passage(TENANT, "p-s-c", "s-c", 0, OTHER, fact(), h(2))
    store.cite(TENANT, "c-1", "p-s-c", h(2))
    assert store.support(TENANT, "c-1", h(2), REG).independent_sources == 1
    d = sourced(store, "s-d", OTHER, at=h(3))
    store.cite(TENANT, "c-1", d, h(3))
    rep = store.support(TENANT, "c-1", h(3), REG)
    assert (rep.independent_sources, rep.corroborated) == (2, True)


def test_withdrawal_propagates_to_syndicated_copies_but_history_is_kept() -> None:
    store = EvidenceStore()
    claimed(store)
    a = sourced(store, "s-a", RELEASE)
    b = sourced(store, "s-b", WIRE, feed="feed-b")
    store.cite(TENANT, "c-1", a, h(1))
    store.cite(TENANT, "c-1", b, h(1))
    store.withdraw(TENANT, "s-a", h(10))
    assert status(store, h(9)) is Status.SUPPORTED  # history unchanged
    rep = store.support(TENANT, "c-1", h(10), REG)
    assert rep.status is Status.UNSUPPORTED
    assert set(rep.invalid.values()) == {Invalid.WITHDRAWN}
    assert store.claim_at(TENANT, "c-1", h(10)) is not None  # not deleted
    with pytest.raises(EvidenceError, match="evidence_unavailable"):
        store.cite(TENANT, "c-1", b, h(11))


def test_deleting_one_copy_leaves_an_independent_copy_and_limits_replay() -> None:
    store = EvidenceStore()
    claimed(store)
    a = sourced(store, "s-a", RELEASE)
    b = sourced(store, "s-b", WIRE, feed="feed-b")
    store.cite(TENANT, "c-1", a, h(1))
    store.delete(TENANT, "s-a", h(5))
    rep = store.support(TENANT, "c-1", h(5), REG)
    assert rep.status is Status.UNSUPPORTED and rep.replay_limited
    assert set(rep.invalid.values()) == {Invalid.DELETED}
    early = store.support(TENANT, "c-1", h(2), REG)
    assert early.status is Status.SUPPORTED and early.replay_limited
    assert store.passage_text(TENANT, a) is None
    assert store.passage_text(TENANT, b) == WIRE
    store.cite(TENANT, "c-1", b, h(6))
    assert status(store, h(6)) is Status.SUPPORTED


def test_retention_expiry_makes_claim_unsupported_at_that_time() -> None:
    store = EvidenceStore()
    claimed(store)
    pid = sourced(store, "s-1", keep=timedelta(days=30))
    store.cite(TENANT, "c-1", pid, h(1))
    end = T0 + timedelta(days=30)
    assert status(store, end - timedelta(microseconds=1)) is Status.SUPPORTED
    rep = store.support(TENANT, "c-1", end, REG)
    assert rep.status is Status.UNSUPPORTED
    assert set(rep.invalid.values()) == {Invalid.RETENTION_EXPIRED}
    store.purge_expired(TENANT, end)
    assert store.passage_text(TENANT, pid) is None


def test_rights_revocation_propagates() -> None:
    store = EvidenceStore()
    claimed(store)
    pid = sourced(store, "s-1")
    store.cite(TENANT, "c-1", pid, h(1))
    ent = REG.entitlements[(TENANT, "feed-a")]
    cut = ent.revise(
        EntitlementSource.INSTALLATION_LICENSE,
        False,
        ent.revisions[0].rights,
        None,
        h(8),
    )
    revoked = REG.with_entitlement(cut)
    assert store.support(TENANT, "c-1", h(7), revoked).status is Status.SUPPORTED
    rep = store.support(TENANT, "c-1", h(8), revoked)
    assert rep.status is Status.UNSUPPORTED
    assert set(rep.invalid.values()) == {Invalid.RIGHTS_REVOKED}


def test_ingest_is_rights_gated() -> None:
    reg = qualified_registry(("feed-a",), missing=Use.RETENTION)
    with pytest.raises(IngestDenied):
        EvidenceStore().add_source(
            rights(registry=reg), "s-1", SourceKind.PRIMARY, RELEASE, T0
        )


def test_revisions_are_append_only_with_as_of_queries() -> None:
    store = EvidenceStore()
    claimed(store)
    old = sourced(store, "s-1")
    store.cite(TENANT, "c-1", old, h(1))
    store.state_claim(TENANT, "c-1", ClaimKind.REPORTED_FACT, fact("120"), h(10))
    rev1, rev2 = (
        store.claim_at(TENANT, "c-1", h(9)),
        store.claim_at(TENANT, "c-1", h(10)),
    )
    assert rev1 is not None and rev2 is not None
    assert (rev1.revision, rev1.stated.value, rev2.revision) == (1, Decimal(100), 2)
    assert status(store, h(9)) is Status.SUPPORTED
    rep = store.support(TENANT, "c-1", h(10), REG)
    assert rep.status is Status.UNSUPPORTED and rep.revision == 2
    assert set(rep.invalid.values()) == {Invalid.REVISION_SUPERSEDED}
    new = sourced(store, "s-2", OTHER.replace("100", "120"), fact("120"), at=h(11))
    store.cite(TENANT, "c-1", new, h(11))
    assert status(store, h(11)) is Status.SUPPORTED
    with pytest.raises(EvidenceError, match="time_order"):
        store.state_claim(TENANT, "c-1", ClaimKind.REPORTED_FACT, fact("1"), h(3))
    with pytest.raises(EvidenceError, match="kind"):
        store.state_claim(TENANT, "c-1", ClaimKind.FORECAST, fact("1"), h(12))


@pytest.mark.parametrize(
    ("text", "value", "ok"),
    [
        (RELEASE, "999", False),  # the stated value is not in the quote
        (RELEASE, "2025", False),  # FY2025 is not a number token
        ("SYNTHETIC: revenue of 1,000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 1000 USD.", "1000.00", True),
        ("SYNTHETIC: revenue of 1000 USD.", "100", False),  # not inside 1000
        ("SYNTHETIC: revenue of 1,00 USD.", "100", False),  # malformed grouping
        ("SYNTHETIC: revenue of 1,0000 USD.", "1000", False),
        ("SYNTHETIC: part 100A7 USD.", "100", False),  # glued to letters
        ("SYNTHETIC: a loss of -12.5 USD.", "-12.5", True),
        ("SYNTHETIC: revenue of 1 000 USD.", "1", False),  # space
        ("SYNTHETIC: revenue of 1 000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 1\u00a0000 USD.", "1", False),  # nbsp
        ("SYNTHETIC: revenue of 1\u00a0000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 1\u2009000 USD.", "1", False),  # thin
        ("SYNTHETIC: revenue of 1\u2009000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 1\u202f000 USD.", "1", False),  # narrow-nbsp
        ("SYNTHETIC: revenue of 1\u202f000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 1'000 USD.", "1", False),  # apostrophe
        ("SYNTHETIC: revenue of 1'000 USD.", "1000", True),
        ("SYNTHETIC: revenue of 12 345.67 USD.", "12", False),
        ("SYNTHETIC: revenue of 12 345.67 USD.", "345.67", False),
        ("SYNTHETIC: revenue of 12 345.67 USD.", "12345.67", True),
        ("SYNTHETIC: revenue of 1 00 USD.", "1", False),  # invalid group: no token
        ("SYNTHETIC: revenue of 1 00 USD.", "100", False),
        ("SYNTHETIC: fiscal 2025 100 USD.", "100", False),  # adjacent groups
        ("SYNTHETIC: revenue of 1,000 000 USD.", "1000", False),  # mixed separators
        ("SYNTHETIC: revenue of 1,000 000 USD.", "1000000", False),
    ],
)
def test_reading_value_must_be_a_number_in_the_quote(
    text: str, value: str, ok: bool
) -> None:
    store = EvidenceStore()
    store.add_source(rights(), "s-1", SourceKind.PRIMARY, text, T0)
    if ok:
        store.add_passage(TENANT, "p-1", "s-1", 0, text, fact(value), T0)
        return
    with pytest.raises(EvidenceError, match="reading_not_in_quote"):
        store.add_passage(TENANT, "p-1", "s-1", 0, text, fact(value), T0)
    claimed(store, value)  # the probe: a claim of the wrong value stays unsupported
    assert status(store, h(1)) is Status.UNSUPPORTED


def test_source_is_not_evidence_before_publication() -> None:
    store = EvidenceStore()
    claimed(store)
    late = T0 + timedelta(days=30)
    store.add_source(rights(), "s-1", SourceKind.PRIMARY, RELEASE, late)
    store.add_passage(TENANT, "p-1", "s-1", 0, RELEASE, fact(), T0)
    with pytest.raises(EvidenceError, match="not_yet_published"):
        store.cite(TENANT, "c-1", "p-1", h(1))
    store.cite(TENANT, "c-1", "p-1", late)
    assert status(store, late - timedelta(microseconds=1)) is Status.UNSUPPORTED
    assert status(store, late) is Status.SUPPORTED


def test_reingesting_identical_content_is_idempotent() -> None:
    store = EvidenceStore()
    first = store.add_source(rights(), "s-1", SourceKind.PRIMARY, RELEASE, T0)
    again = store.add_source(rights(at=h(3)), "s-1", SourceKind.PRIMARY, RELEASE, T0)
    assert again is first and again.received_at == T0  # receipt time never reset
    with pytest.raises(EvidenceError, match="exists"):
        store.add_source(rights(at=h(3)), "s-1", SourceKind.PRIMARY, OTHER, T0)
    with pytest.raises(EvidenceError, match="exists"):
        store.add_source(rights("feed-b", h(3)), "s-1", SourceKind.PRIMARY, RELEASE, T0)


def test_social_input_is_a_lead_not_support() -> None:
    store = EvidenceStore()
    claimed(store)
    store.add_source(rights(), "s-x", SourceKind.SOCIAL, RELEASE, T0)
    store.add_passage(TENANT, "p-x", "s-x", 0, RELEASE, fact(), T0)
    with pytest.raises(EvidenceError, match="social_lead"):
        store.cite(TENANT, "c-1", "p-x", h(1))


def test_knowledge_time_is_enforced() -> None:
    store = EvidenceStore()
    pid = sourced(store, "s-1", at=h(5))
    store.state_claim(TENANT, "c-1", ClaimKind.REPORTED_FACT, fact(), h(6))
    with pytest.raises(EvidenceError, match="time_order"):
        store.cite(TENANT, "c-1", pid, h(4))  # the store clock never runs back
    with pytest.raises(EvidenceError, match="not_known"):
        store.support(TENANT, "c-1", h(5), REG)
    with pytest.raises(InstantError):
        store.cite(TENANT, "c-1", pid, datetime(2026, 1, 9))  # noqa: DTZ001
    with pytest.raises(TypeError):
        Reading(E1, REV, FY25, "USD", 100)  # type: ignore[arg-type]


def test_tenant_isolation() -> None:
    store = EvidenceStore()
    pid = sourced(store, "s-1")
    claimed(store)
    reg = qualified_registry(("feed-a",))  # entitles TENANT only
    with pytest.raises(IngestDenied):
        store.add_source(
            rights(registry=reg, tenant="tenant-synth-b"),
            "s-1",
            SourceKind.PRIMARY,
            RELEASE,
            T0,
        )
    other = "tenant-synth-b"
    for call in (
        lambda: store.cite(other, "c-1", pid, h(1)),
        lambda: store.support(other, "c-1", h(1), REG),
        lambda: store.add_passage(other, "p-2", "s-1", 0, RELEASE, fact(), h(1)),
        lambda: store.withdraw(other, "s-1", h(1)),
        lambda: store.passage_text(other, pid),
    ):
        with pytest.raises(EvidenceError, match="not_found"):
            call()
    store.state_claim(other, "c-1", ClaimKind.REPORTED_FACT, fact(), h(1))
    with pytest.raises(EvidenceError, match="not_found"):
        store.cite(other, "c-1", pid, h(2))  # a passage of TENANT


def test_reported_fact_from_filings_is_a_primary_source() -> None:
    received = h(2)
    base = Fact(
        cik="0009990001",
        instrument=Resolution.found(E1),
        taxonomy="us-gaap",
        concept="Revenues",
        unit=parse_unit("USD"),
        period=FY25,
        fy=2025,
        fp="FY",
        value=Decimal("100"),
        accession="0009990001-26-000001",
        form="10-K",
        filed=date(2026, 1, 5),
        published_at=h(5),  # filed-date bound: after our receipt
        publication_basis=PublicationBasis.ACCEPTANCE,
        received_at=received,
        feed_id="feed-a",
        feed_hash="0" * 64,
    )
    store = EvidenceStore()
    p = store.add_fact(rights(at=received), base)
    assert p.reading == fact()
    store.state_claim(TENANT, "c-1", ClaimKind.REPORTED_FACT, fact(), h(3))
    with pytest.raises(EvidenceError, match="not_yet_published"):
        store.cite(TENANT, "c-1", p.passage_id, h(3))
    store.cite(TENANT, "c-1", p.passage_id, h(5))
    assert status(store, h(5)) is Status.SUPPORTED
    assert store.add_fact(rights(at=received), base) == p  # re-ingest is a no-op
    unknown = replace(base, instrument=Resolution("unknown"), accession="x")
    with pytest.raises(EvidenceError, match="entity_unresolved"):
        store.add_fact(rights(at=received), unknown)
    with pytest.raises(EvidenceError, match="fact_rights"):
        store.add_fact(rights("feed-b", received), base)


# Property: SUPPORTED at q iff some citation known at q, bound to the revision current
# at q, whose source is unexpired, undeleted and has no withdrawn copy (any source of
# the same content group) at q. Contents 0 and 1 state 100, content 2 states 250.
CONTENTS = (RELEASE, OTHER, "SYNTHETIC: Revenues were 250 USD for FY2025.")
VALUES = ("100", "100", "250")
src_st = st.tuples(
    st.integers(0, 2),  # content group
    st.booleans(),  # feed-b re-typesets the text (same group)
    st.integers(0, 30),  # received hour
    st.integers(10, 120),  # retention hours
    st.sampled_from((None, None, 5, 20, 40)),  # withdrawn this long after receipt
    st.sampled_from((None, None, 0, 15, 40)),  # deleted this long after receipt
)


@settings(max_examples=200, deadline=None)
@given(
    sources=st.lists(src_st, min_size=1, max_size=5),
    revised=st.sampled_from((None, 10, 25, 50)),
    cites=st.lists(
        st.tuples(st.integers(0, 4), st.integers(0, 40)), min_size=1, max_size=6
    ),
    lag=st.integers(0, 90),
)
def test_supported_iff_valid_unwithdrawn_unexpired_citation(
    sources: list[tuple[int, bool, int, int, int | None, int | None]],
    revised: int | None,
    cites: list[tuple[int, int]],
    lag: int,
) -> None:
    query = sources[0][2] + lag

    def rev_at(hour: int) -> int:
        return 2 if revised is not None and hour >= revised else 1

    def group_withdrawn(group: int, hour: int) -> bool:
        return any(
            g == group and w is not None and r + w <= hour
            for g, _, r, _, w, _ in sources
        )

    def usable(i: int, hour: int) -> bool:
        g, _, r, keep, _, d = sources[i]
        return (
            r <= hour < r + keep
            and not (d is not None and r + d <= hour)
            and not group_withdrawn(g, hour)
        )

    events: list[tuple[int, int, object]] = [(0, 1, "claim")]
    for i, (_, _, r, _, w, d) in enumerate(sources):
        events.append((r, 0, ("source", i)))
        events += [(r + w, 2, ("withdraw", i))] if w is not None else []
        events += [(r + d, 2, ("delete", i))] if d is not None else []
    events += [(revised, 1, "revise")] if revised is not None else []
    for i, lag in cites:  # cite `lag` hours after the source's receipt
        events.append(
            (sources[i % len(sources)][2] + lag, 3, ("cite", i % len(sources)))
        )
    store, accepted = EvidenceStore(), []
    for hour, _, ev in sorted(events, key=lambda e: (e[0], e[1], repr(e[2]))):
        if ev == "claim":
            claimed(store)
        elif ev == "revise":
            store.state_claim(
                TENANT, "c-1", ClaimKind.REPORTED_FACT, fact("250"), h(hour)
            )
        elif isinstance(ev, tuple) and ev[0] == "source":
            i = ev[1]
            g, b, r, keep, _, _ = sources[i]
            text = CONTENTS[g].upper() if b else CONTENTS[g]
            feed = "feed-b" if b else "feed-a"
            kw = {"keep": timedelta(hours=keep), "at": h(r), "feed": feed}
            sourced(store, f"s{i}", text, fact(VALUES[g]), **kw)
        elif isinstance(ev, tuple) and ev[0] in ("withdraw", "delete"):
            getattr(store, ev[0])(TENANT, f"s{ev[1]}", h(hour))
        elif isinstance(ev, tuple):
            i = ev[1]
            matches = VALUES[sources[i][0]] == ("250" if rev_at(hour) == 2 else "100")
            if usable(i, hour) and matches:
                store.cite(TENANT, "c-1", f"p-s{i}", h(hour))
                accepted.append((i, hour))
            else:
                with pytest.raises(EvidenceError):
                    store.cite(TENANT, "c-1", f"p-s{i}", h(hour))
    valid = {
        sources[i][0]
        for i, t in accepted
        if t <= query and rev_at(t) == rev_at(query) and usable(i, query)
    }
    rep = store.support(TENANT, "c-1", h(query), REG)
    assert (rep.status is Status.SUPPORTED) == bool(valid)
    assert rep.independent_sources == len(valid)
