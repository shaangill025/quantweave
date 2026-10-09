"""SEC-shaped filings and reported facts: rights gate, exact values, units,
publication time, restatements and point-in-time queries (T023 increment 1).

All payloads are SYNTHETIC, shaped like SEC submissions/companyfacts JSON: the issuer
(CIK 9990001), accessions and feed are invented, and the feed is qualified through
the real T021 registry with SYNTHETIC evidence. No network. Expected values are
hand-written in each test, never computed by the code under test.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.filings import (
    Fact,
    FactBook,
    FilingsError,
    KnowledgeBasis,
    Period,
    PublicationBasis,
    UnitKind,
    parse_companyfacts,
    parse_submissions,
    parse_unit,
)
from qw_domain.identity import (
    InstrumentId,
    ProviderId,
    ProviderLink,
    Resolution,
    SecurityMaster,
)
from qw_domain.ingest import IngestDenied, IngestRights
from qw_domain.instants import InstantError
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

T = datetime(2025, 1, 2, tzinfo=UTC)  # feed qualified; grants expire 2028
FEED, TENANT, REGION, KEEP = "feed-synth-sec", "tenant-synth-a", "CA-ON", timedelta(365)
PERMIT = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", T, "counsel-synth")
CIK10 = "0009990001"
RECEIVED = datetime(2026, 10, 9, 12, tzinfo=UTC)
PUB = KnowledgeBasis.PUBLICATION
A1, A2, A3 = (f"{CIK10}-26-00000{i}" for i in (1, 2, 3))


def _profile(missing: Use | None = None) -> RightsProfile:
    def grant(use: Use | None = None) -> Grant:
        keep = KEEP if use is Use.RETENTION else None
        return Grant(RightState.GRANTED, PERMIT, datetime(2028, 1, 1, tzinfo=UTC), keep)

    uses = {u: grant(u) for u in Use if u is not missing}
    return RightsProfile(uses, {UseScope.PERSONAL: grant()}, {REGION: grant()})


def rights(
    received: datetime = RECEIVED,
    missing: Use | None = None,
    registry: Registry | None = None,
) -> IngestRights:
    if registry is None:
        auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", T)
        work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", T)
        feed = (
            FeedHistory.new(FEED, "provider-synth")
            .revise("synthetic_sec", frozenset(Use), _profile(), T)
            .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), T)
            .transition(FeedStatus.QUALIFIED, "operator-synth", (work, PERMIT), T)
        )
        ent = EntitlementHistory.new(TENANT, FEED).revise(
            EntitlementSource.INSTALLATION_LICENSE, True, _profile(missing), None, T
        )
        registry = (
            Registry()
            .with_provider(Provider("provider-synth", "SYNTHETIC provider"))
            .with_feed(feed)
            .with_entitlement(ent)
        )
    scope = UseScope.PERSONAL
    return IngestRights(registry, TENANT, FEED, received, scope, REGION, KEEP)


def row(val: str, accn: str, filed: str, form: str = "10-K", end: str = "31") -> str:
    return (
        f'{{"start": "2025-01-01", "end": "2025-12-{end}", "val": {val}, '
        f'"accn": "{accn}", "fy": 2025, "fp": "FY", "form": "{form}", '
        f'"filed": "{filed}"}}'
    )


def payload(units: dict[str, list[str]]) -> str:
    body = ", ".join(f'"{u}": [{", ".join(rows)}]' for u, rows in units.items())
    return (
        '{"cik": 9990001, "entityName": "SYNTHETIC Issuer", '
        f'"facts": {{"us-gaap": {{"Revenues": {{"units": {{{body}}}}}}}}}}}'
    )


def usd(*rows: str) -> str:
    return payload({"USD": list(rows)})


def submissions(
    accepted: str = "2026-02-01T21:05:00.000Z", filed: str = "2026-02-01"
) -> str:
    return (
        f'{{"cik": "{CIK10}", "name": "SYNTHETIC Issuer", "filings": {{"recent": {{'
        f'"accessionNumber": ["{A1}"], "filingDate": ["{filed}"], '
        f'"acceptanceDateTime": ["{accepted}"], "form": ["10-K"]}}}}}}'
    )


def test_values_stay_exact_decimals_never_float() -> None:
    vals = ["9007199254740993", "0.123456789012", "1.5E+3", "0.1", "0.2"]
    text = usd(*(row(v, A1, "2026-02-01", end=f"2{i}") for i, v in enumerate(vals)))
    values = [f.value for f in parse_companyfacts(text, rights()).facts]
    assert all(type(v) is Decimal for v in values)
    assert values[0] == Decimal("9007199254740993")  # 2**53 + 1: a float drops the 3
    assert values[1:3] == [Decimal("0.123456789012"), Decimal(1500)]
    assert values[3] + values[4] == Decimal("0.3")  # 0.1 + 0.2 != 0.3 in binary


@pytest.mark.parametrize(
    "val",
    [
        "0.1234567890123",
        pytest.param("1." + "0" * 59 + "1", id="61-significant-digits"),  # > prec 50
        "1E+26",
        pytest.param("1" + "0" * 4400, id="int-4401-digits"),  # json.loads digit cap
        "NaN",
        "Infinity",
        '"100"',
        "true",
        "null",
    ],
)
def test_unrepresentable_or_non_numeric_values_refuse_payload(val: str) -> None:
    with pytest.raises(FilingsError, match=r"value|json"):
        parse_companyfacts(usd(row(val, A1, "2026-02-01")), rights())


def test_zero_forms_are_accepted_as_zero() -> None:
    text = usd(row("0E-30", A1, "2026-02-01"), row("-0", A1, "2026-02-01", end="30"))
    assert [f.value for f in parse_companyfacts(text, rights()).facts] == [0, 0]


def test_malformed_payloads_are_refused() -> None:
    good = usd(row("1", A1, "2026-02-01"))
    with pytest.raises(FilingsError, match="duplicate_key"):
        parse_companyfacts(
            good.replace('"cik": 9990001,', '"cik": 1, "cik": 2,'), rights()
        )
    with pytest.raises(FilingsError, match="period"):
        parse_companyfacts(good.replace("2025-01-01", "2026-01-01"), rights())
    with pytest.raises(TypeError):
        parse_companyfacts({"cik": 9990001}, rights())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "kind", "currency"),
    [
        ("USD", UnitKind.CURRENCY, "USD"),
        ("CAD", UnitKind.CURRENCY, "CAD"),
        ("shares", UnitKind.SHARES, None),
        ("USD/shares", UnitKind.PER_SHARE, "USD"),
        ("pure", UnitKind.PURE, None),
    ],
)
def test_known_units(raw: str, kind: UnitKind, currency: str | None) -> None:
    unit = parse_unit(raw)
    assert (unit.kind, unit.currency, unit.raw) == (kind, currency, raw)


@pytest.mark.parametrize(
    "raw", ["usd", "EUR", "shares/USD", "USD/USD", "Vote", "USD-per-share", "", "USD "]
)
def test_unknown_or_ambiguous_units_are_refused_not_coerced(raw: str) -> None:
    with pytest.raises(FilingsError, match="unit_unknown"):
        parse_unit(raw)
    text = payload(
        {"USD": [row("10", A1, "2026-02-01")], raw: [row("7", A1, "2026-02-01")]}
    )
    got = parse_companyfacts(text, rights())
    assert [f.unit.raw for f in got.facts] == ["USD"]  # the known series still loads
    assert [(r.concept, r.unit, r.code) for r in got.refused] == [
        ("us-gaap:Revenues", raw, "unit_unknown")
    ]


def test_publication_time_from_acceptance_or_conservative_filed_date() -> None:
    filings = parse_submissions(submissions(), rights())
    assert [(f.accession, f.form, f.filing_date) for f in filings] == [
        (A1, "10-K", date(2026, 2, 1))
    ]
    text = usd(row("5", A1, "2026-02-01"), row("6", A2, "2026-02-01", end="30"))
    facts = parse_companyfacts(text, rights(), filings=filings).facts
    accepted, bound = sorted(facts, key=lambda f: f.accession)
    assert accepted.published_at == datetime(2026, 2, 1, 21, 5, tzinfo=UTC)
    assert accepted.publication_basis is PublicationBasis.ACCEPTANCE
    # Filed date only: D+1 05:00Z, at or after the end of D in New York (EST or EDT).
    assert bound.published_at == datetime(2026, 2, 2, 5, tzinfo=UTC)
    assert bound.publication_basis is PublicationBasis.FILED_DATE_BOUND
    assert (bound.received_at, bound.feed_id, bound.form) == (RECEIVED, FEED, "10-K")
    assert (bound.fy, bound.fp, bound.period.start) == (2025, "FY", date(2025, 1, 1))


@pytest.mark.parametrize(
    ("form", "filed"), [("10-Q", "2026-02-01"), ("10-K", "2026-02-02")]
)
def test_companyfacts_contradicting_submissions_is_refused(
    form: str, filed: str
) -> None:
    filings = parse_submissions(submissions(), rights())
    with pytest.raises(FilingsError, match="filing_mismatch"):
        parse_companyfacts(usd(row("5", A1, filed, form)), rights(), filings=filings)


@pytest.mark.parametrize(
    ("accepted", "filed", "ok"),
    [
        ("2020-02-01T21:05:00Z", "2026-02-01", False),  # years early: a PIT leak
        ("2026-01-27T23:59:59Z", "2026-02-01", False),  # D-4d 00:00Z minus 1 second
        ("2026-01-28T00:00:00Z", "2026-02-01", True),  # D-4d 00:00Z, the earliest
        # Friday 2026-02-13 17:45 ET, after hours before Presidents' Day (Monday):
        # EDGAR filing date is Tuesday 2026-02-17.
        ("2026-02-13T22:45:00Z", "2026-02-17", True),
        ("2026-02-02T05:00:00Z", "2026-02-01", True),  # D+1 05:00Z, the latest
        ("2026-02-02T05:00:01Z", "2026-02-01", False),
        ("2026-02-05T12:00:00Z", "2026-02-01", False),
    ],
)
def test_acceptance_must_fit_the_filing_date(
    accepted: str, filed: str, ok: bool
) -> None:
    text = submissions(accepted, filed)
    if ok:
        (filing,) = parse_submissions(text, rights())
        assert filing.accepted_at == datetime.fromisoformat(accepted)
    else:
        with pytest.raises(FilingsError, match="acceptance_filed_mismatch"):
            parse_submissions(text, rights())


def test_acceptance_after_receipt_is_refused() -> None:
    with pytest.raises(FilingsError, match="published_after_receipt"):
        parse_submissions(
            submissions(), rights(datetime(2026, 2, 1, 21, 4, tzinfo=UTC))
        )


def test_cik_maps_to_instrument_only_where_known() -> None:
    iid = InstrumentId.from_wire("0f0e0d0c-0b0a-4908-8706-050403020100")
    master = SecurityMaster()
    master.add_provider_link(
        ProviderLink(ProviderId("sec_cik", CIK10), iid, date(2020, 1, 1))
    )
    text = usd(row("5", A1, "2026-02-01"))
    fact = parse_companyfacts(text, rights(), master=master).facts[0]
    assert (fact.cik, fact.instrument) == (CIK10, Resolution.found(iid))
    assert parse_companyfacts(text, rights()).facts[0].instrument == Resolution(
        "unknown"
    )


def restated() -> FactBook:
    """FY2025 revenue: 1000 (A1 filed 2026-02-01), restated 990 (A2, 10-K/A filed
    2026-05-01), then 985 in the next annual report (A3 filed 2027-02-01). Each is
    received two days after its filed date."""
    book = FactBook()
    for val, accn, filed, form in (
        ("1000", A1, "2026-02-01", "10-K"),
        ("990", A2, "2026-05-01", "10-K/A"),
        ("985", A3, "2027-02-01", "10-K"),
    ):
        got = datetime.fromisoformat(filed).replace(tzinfo=UTC) + timedelta(days=2)
        book = book.add(
            parse_companyfacts(usd(row(val, accn, filed, form)), rights(got)).facts
        )
    return book


def test_restatements_are_versions_and_as_of_picks_latest_known() -> None:
    book = restated()
    key = book.facts[0].key
    for at, want in (
        (datetime(2026, 2, 2, 4, 59, 59, tzinfo=UTC), None),
        (datetime(2026, 2, 2, 5, tzinfo=UTC), Decimal(1000)),  # exactly at publication
        (datetime(2026, 5, 2, 4, 59, tzinfo=UTC), Decimal(1000)),
        (datetime(2026, 5, 2, 5, tzinfo=UTC), Decimal(990)),
        (datetime(2027, 2, 2, 5, tzinfo=UTC), Decimal(985)),
    ):
        got = book.as_of(at, PUB).facts.get(key)
        assert (None if got is None else got.value) == want
    assert [f.value for f in book.history(key)] == [
        Decimal(1000),
        Decimal(990),
        Decimal(985),
    ]
    assert book.add(book.facts) == book  # re-ingesting the same versions is idempotent


def test_receipt_basis_hides_facts_received_after_the_query() -> None:
    book = restated()
    key = book.facts[0].key
    at = datetime(2026, 5, 2, 12, tzinfo=UTC)  # 990 published, received 2026-05-03
    assert book.as_of(at, PUB).facts[key].value == Decimal(990)
    assert book.as_of(at, KnowledgeBasis.RECEIPT).facts[key].value == Decimal(1000)


def test_one_accession_with_two_values_or_tied_versions_are_not_guessed() -> None:
    clash = usd(row("5", A1, "2026-02-01"), row("6", A1, "2026-02-01"))
    with pytest.raises(FilingsError, match="fact_conflict"):
        parse_companyfacts(clash, rights())
    tie = usd(row("5", A1, "2026-02-01"), row("6", A2, "2026-02-01"))
    facts = parse_companyfacts(tie, rights()).facts
    view = FactBook().add(facts).as_of(RECEIVED, PUB)
    assert view.facts == {} and view.conflicted == frozenset({facts[0].key})


@pytest.mark.parametrize("missing", [Use.RETENTION, Use.DERIVED_DATA, None])
def test_ingestion_fails_closed_without_rights(missing: Use | None) -> None:
    denied = rights(missing=missing) if missing else rights(registry=Registry())
    with pytest.raises(IngestDenied) as err:
        parse_companyfacts(usd(row("5", A1, "2026-02-01")), denied)
    assert err.value.reasons
    with pytest.raises(IngestDenied):
        parse_submissions(submissions(), denied)


def test_as_of_rejects_naive_time() -> None:
    with pytest.raises(InstantError):
        FactBook().as_of(datetime(2026, 1, 1), PUB)  # noqa: DTZ001


T0 = datetime(2026, 1, 1, tzinfo=UTC)
BASE = Fact(
    CIK10, Resolution("unknown"), "us-gaap", "Revenues", parse_unit("USD"),
    Period(date(2025, 1, 1), date(2025, 12, 31)), 2025, "FY", Decimal(1), A1, "10-K",
    date(2026, 2, 1), T0, PublicationBasis.ACCEPTANCE, T0, FEED, "synthetic-hash",
)  # fmt: skip
hours = st.integers(0, 12)  # a small range, so tied versions are frequent


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 2), hours, hours, st.integers(-9, 9)), max_size=12
    ),
    hours,
    hours,
    st.sampled_from(list(KnowledgeBasis)),
)
def test_as_of_never_sees_the_future_and_is_monotone(
    rows: list[tuple[int, int, int, int]], q1: int, q2: int, basis: KnowledgeBasis
) -> None:
    facts = [
        replace(
            BASE,
            concept=f"Concept{k}",
            value=Decimal(v),
            accession=f"{CIK10}-26-{i:06d}",
            published_at=T0 + timedelta(hours=p),
            received_at=T0 + timedelta(hours=p + r),
        )
        for i, (k, p, r, v) in enumerate(rows)
    ]
    book = FactBook().add(facts)
    t1, t2 = sorted((T0 + timedelta(hours=2 * q1), T0 + timedelta(hours=2 * q2)))
    v1, v2 = book.as_of(t1, basis), book.as_of(t2, basis)

    def known(f: Fact) -> datetime:
        return f.published_at if basis is PUB else max(f.published_at, f.received_at)

    for view, t in ((v1, t1), (v2, t2)):
        for key in {g.key for g in facts if known(g) <= t}:
            seen = [g for g in facts if g.key == key and known(g) <= t]
            last = max(known(g) for g in seen)
            tied = {g.value for g in seen if known(g) == last}
            # Latest known version; tied different values are conflicted, not picked.
            assert (key in view.conflicted) is (len(tied) > 1)
            if len(tied) == 1:
                assert known(view.facts[key]) == last <= t
                assert view.facts[key].value in tied
        assert view.facts.keys().isdisjoint(view.conflicted)
    assert set(v1.facts) | v1.conflicted <= set(v2.facts) | v2.conflicted
    for key, f in v1.facts.items():
        assert key not in v2.facts or known(v2.facts[key]) >= known(f)
