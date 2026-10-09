"""Research datasets: rights refusal, frozen manifests, survivorship-free universe
membership and point-in-time facts (T032 increment 1).

All data is SYNTHETIC: invented instruments, CIK 9990002, accessions and feeds; the
feeds are qualified through the real T021 registry with SYNTHETIC evidence. Expected
values are hand-written or come from an independent oracle in the test.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ingest_rights_helper import qualified_rights
from qw_domain.filings import Fact, FactBook, KnowledgeBasis, Period, Unit, UnitKind
from qw_domain.filings import PublicationBasis as PB
from qw_domain.identity import InstrumentId, Resolution
from qw_domain.research_data import (
    Change,
    DatasetRefused,
    EvidenceClass,
    MembershipEvent,
    PriceMode,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    Universe,
    Window,
    freeze_dataset,
)
from qw_domain.rights import DenyCode, Registry, Use

FEED = "feed-synth-research"
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
AS_OF = datetime(2026, 1, 1, tzinfo=UTC)
OBSERVED = Window(datetime(2020, 1, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC))
USD = Unit(UnitKind.CURRENCY, "USD", "USD")
KEY_PERIOD = Period(date(2023, 1, 1), date(2023, 12, 31))


def iid(n: int) -> InstrumentId:
    return InstrumentId(UUID(int=n))


def at(y: int, m: int = 1, d: int = 1) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def rights(
    missing: Use | None = None, registry: Registry | None = None
) -> ResearchRights:
    ing = qualified_rights(FEED, NOW, missing, registry)
    return ResearchRights(ing.registry, ing.tenant_id, NOW, ing.scope,
                          ing.jurisdiction, ing.retain_for)  # fmt: skip


def fact(value: str, accn: str, published: datetime, feed: str = FEED) -> Fact:
    return Fact("0009990002", Resolution.found(iid(1)), "us-gaap", "Revenues", USD,
                KEY_PERIOD, 2023, "FY", Decimal(value), accn, "10-K", published.date(),
                published, PB.ACCEPTANCE, published, feed, "f" * 64)  # fmt: skip


def event(n: int, change: Change, eff: date, known: datetime) -> MembershipEvent:
    return MembershipEvent(iid(n), change, eff, known, FEED)


def freeze(
    facts: tuple[Fact, ...] = (), events: tuple[MembershipEvent, ...] = (), **kw: object
) -> ResearchDataset:
    args: dict[str, object] = {
        "observed": OBSERVED, "as_of": AS_OF, "basis": KnowledgeBasis.PUBLICATION,
        "price_mode": PriceMode.RAW_TRADABLE, "evidence_class": EvidenceClass.SYNTHETIC,
    } | kw  # fmt: skip
    r = args.pop("rights", rights())
    return freeze_dataset("ds-synth-1", r, frozenset({FEED}), facts,  # type: ignore[arg-type]
                          Universe("univ-synth", events), **args)  # type: ignore[arg-type]  # fmt: skip


# --- survivorship -------------------------------------------------------------

LISTED, DELISTED = date(2020, 3, 2), date(2023, 6, 30)
EVENTS = (
    event(1, Change.ADD, LISTED, at(2020, 3, 2)),
    event(1, Change.REMOVE, DELISTED, at(2023, 7, 3)),  # announced after the fact
    event(2, Change.ADD, date(2022, 1, 3), at(2022, 1, 3)),
)


def test_delisted_name_stays_in_the_historical_universe() -> None:
    ds = freeze(events=EVENTS)
    assert ds.members(date(2021, 6, 1), at(2021, 6, 1)) == {iid(1)}
    assert ds.members(date(2023, 1, 2), at(2023, 1, 2)) == {iid(1), iid(2)}
    # today's survivor list would contain only iid(2); history keeps iid(1)
    assert ds.members(date(2025, 6, 1), at(2025, 6, 1)) == {iid(2)}
    # nobody is a member before listing, whatever is known later
    assert ds.members(date(2020, 3, 1), at(2025, 1, 1)) == frozenset()
    assert iid(2) not in ds.members(date(2021, 12, 31), at(2025, 1, 1))


def test_membership_change_known_only_from_its_knowledge_time() -> None:
    ds = freeze(events=EVENTS)
    day = date(2023, 7, 1)  # after the effective delisting, before it was known
    assert iid(1) in ds.members(day, at(2023, 7, 1))
    assert iid(1) not in ds.members(day, at(2023, 7, 3))


def test_membership_sequence_must_alternate_from_a_listing() -> None:
    with pytest.raises(ResearchError, match="membership_sequence"):
        Universe("u", (event(1, Change.REMOVE, LISTED, AS_OF),))
    twice = (event(1, Change.ADD, LISTED, AS_OF), event(1, Change.ADD, DELISTED, AS_OF))
    with pytest.raises(ResearchError, match="membership_sequence"):
        Universe("u", twice)


# --- point-in-time facts --------------------------------------------------------

ORIGINAL = fact("100", "0009990002-24-000001", at(2024, 2, 15))
RESTATED = fact("90", "0009990002-25-000001", at(2025, 3, 1))


def test_future_filing_is_not_visible_and_queries_past_freeze_are_refused() -> None:
    ds = freeze(facts=(ORIGINAL,))
    assert ds.facts_at(at(2024, 2, 14)).facts == {}
    assert [f.value for f in ds.facts_at(at(2024, 2, 15)).facts.values()] == [
        Decimal("100")
    ]
    with pytest.raises(ResearchError, match="after_freeze"):
        ds.facts_at(AS_OF + timedelta(microseconds=1))


def test_restatement_is_invisible_before_its_knowledge_time() -> None:
    ds = freeze(facts=(ORIGINAL, RESTATED))
    assert [f.value for f in ds.facts_at(at(2025, 2, 28)).facts.values()] == [
        Decimal("100")
    ]
    assert [f.value for f in ds.facts_at(at(2025, 3, 1)).facts.values()] == [
        Decimal("90")
    ]


def test_records_known_after_as_of_are_left_out_of_the_frozen_dataset() -> None:
    late = fact("80", "0009990002-26-000001", at(2026, 2, 1))
    ds = freeze(facts=(ORIGINAL, late), events=(*EVENTS, event(3, Change.ADD,
                date(2025, 1, 2), at(2026, 2, 1))))  # fmt: skip
    assert {f.accession for f in ds.facts.facts} == {ORIGINAL.accession}
    assert all(e.instrument_id != iid(3) for e in ds.universe.events)


# --- rights and integrity -------------------------------------------------------


@pytest.mark.parametrize("missing", [Use.RETENTION, Use.DERIVED_DATA])
def test_missing_research_right_refuses_the_dataset(missing: Use) -> None:
    with pytest.raises(DatasetRefused) as exc:
        freeze(rights=rights(missing))
    assert [(f, r.code) for f, r in exc.value.reasons] == [(FEED, DenyCode.UNKNOWN)]


def test_unknown_feed_refuses_the_dataset() -> None:
    with pytest.raises(DatasetRefused) as exc:
        freeze(rights=rights(registry=Registry()))
    assert {r.code for _, r in exc.value.reasons} == {DenyCode.NOT_QUALIFIED}


def test_undeclared_source_and_future_as_of_are_refused() -> None:
    with pytest.raises(ResearchError, match="undeclared_source"):
        freeze(facts=(fact("1", "0009990002-24-000009", at(2024, 1, 2), "feed-x"),))
    with pytest.raises(ResearchError, match="as_of"):
        freeze(as_of=NOW + timedelta(seconds=1))
    with pytest.raises(ResearchError, match="as_of"):
        freeze(observed=Window(at(2020), AS_OF + timedelta(days=1)))


def test_manifest_hash_binds_terms_and_content() -> None:
    ds = freeze(facts=(ORIGINAL,), events=EVENTS)
    assert len(ds.manifest.content_hash) == 64
    assert ds.manifest.sources.keys() == {FEED}
    other = freeze(
        facts=(ORIGINAL,), events=EVENTS, evidence_class=EvidenceClass.HISTORICAL
    )
    assert other.manifest.content_hash != ds.manifest.content_hash
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset(ds.manifest, FactBook((ORIGINAL, RESTATED)), ds.universe)
    forged = replace(ds.manifest, evidence_class=EvidenceClass.PROSPECTIVE)
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset(forged, ds.facts, ds.universe)


# --- properties -----------------------------------------------------------------

DAY0 = date(2020, 1, 1)
days = st.integers(0, 2000)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.tuples(days, days, st.one_of(st.none(), st.tuples(days, days))),
             min_size=1, max_size=6),
    days, days,
)  # fmt: skip
def test_universe_membership_matches_listing_oracle(
    names: list[tuple[int, int, tuple[int, int] | None]], on_n: int, known_n: int
) -> None:
    """One listing per instrument, optional delisting after it; membership on `on`
    known by `known` is listed-and-known and not delisted-and-known."""
    events: list[MembershipEvent] = []
    oracle: set[InstrumentId] = set()
    on, known = DAY0 + timedelta(on_n), at(2020) + timedelta(known_n)
    for n, (list_n, list_known, delist) in enumerate(names, start=1):
        listed, list_k = DAY0 + timedelta(list_n), at(2020) + timedelta(list_known)
        events.append(event(n, Change.ADD, listed, list_k))
        gone = False
        if delist is not None:
            d_eff = listed + timedelta(1 + delist[0])
            d_k = at(2020) + timedelta(delist[1])
            events.append(event(n, Change.REMOVE, d_eff, d_k))
            gone = d_eff <= on and d_k <= known
        if listed <= on and list_k <= known and not gone:
            oracle.add(iid(n))
    assert Universe("u", tuple(events)).members(on, known) == oracle


@settings(max_examples=150, deadline=None)
@given(st.lists(st.tuples(days, st.integers(-5, 5)), min_size=1, max_size=8), days)
def test_point_in_time_query_never_returns_later_knowledge(
    versions: list[tuple[int, int]], query_n: int
) -> None:
    facts = tuple(
        fact(str(v), f"0009990002-24-{i:06d}", at(2020) + timedelta(pub_n))
        for i, (pub_n, v) in enumerate(versions)
    )
    ds = freeze(facts=facts)
    t = at(2020) + timedelta(query_n)
    known = [f for f in facts if f.published_at <= t]
    view = ds.facts_at(t)
    assert all(f.published_at <= t for f in view.facts.values())
    if not known:
        assert view.facts == {} and view.conflicted == frozenset()
        return
    latest = max(f.published_at for f in known)
    values = {f.value for f in known if f.published_at == latest}
    if len(values) == 1:
        assert [f.value for f in view.facts.values()] == list(values)
    else:
        assert view.facts == {} and len(view.conflicted) == 1
