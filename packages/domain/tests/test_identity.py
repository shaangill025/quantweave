"""Instrument identity, listings and resolution (T011; R004, C-27). SYNTHETIC data."""

from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.identity import (
    IdentityError,
    InstrumentId,
    Listing,
    Mic,
    ProviderId,
    ProviderLink,
    Resolution,
    SecurityMaster,
    Ticker,
)

PROFILE = settings(derandomize=True, database=None, max_examples=200)
XNYS, XNAS = Mic("XNYS"), Mic("XNAS")
A = InstrumentId(UUID("00000000-0000-4000-8000-00000000000a"))
B = InstrumentId(UUID("00000000-0000-4000-8000-00000000000b"))
C = InstrumentId(UUID("00000000-0000-4000-8000-00000000000c"))
D = date


def test_instrument_id_is_not_a_ticker() -> None:
    assert InstrumentId.from_wire(A.to_wire()) == A
    assert A.to_wire() == "00000000-0000-4000-8000-00000000000a"
    for bad in ("AAPL", "00000000-0000-4000-8000-00000000000A", " " + A.to_wire()):
        with pytest.raises(IdentityError):
            InstrumentId.from_wire(bad)
    assert Ticker("AAPL") != A  # type: ignore[comparison-overlap]


@pytest.mark.parametrize(
    ("kind", "bad"),
    [(Mic, b) for b in ("XNY", "xnys", "XNYSX", "XN-S", "")]
    + [(Ticker, b) for b in ("", "aapl", "A B", ".A", "A" * 17, "\uff21\uff30")],
)
def test_mic_and_ticker_reject_malformed(kind: type[Mic | Ticker], bad: str) -> None:
    with pytest.raises(IdentityError):
        kind(bad)


def test_listing_interval_must_be_non_empty() -> None:
    with pytest.raises(IdentityError):
        Listing(A, XNYS, Ticker("X"), D(2026, 1, 2), D(2026, 1, 2))


def test_ticker_collision_across_mics_is_ambiguous_without_mic() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNYS, Ticker("ABC"), D(2020, 1, 1)))
    m.add_listing(Listing(B, XNAS, Ticker("ABC"), D(2021, 1, 1)))
    assert m.resolve(Ticker("ABC"), XNYS, D(2026, 5, 1)) == Resolution.found(A)
    assert m.resolve(Ticker("ABC"), XNAS, D(2026, 5, 1)) == Resolution.found(B)
    ambiguous = m.resolve(Ticker("ABC"), None, D(2026, 5, 1))
    assert ambiguous.status == "ambiguous" and ambiguous.candidates == (A, B)
    assert ambiguous.instrument_id is None
    # Before B listed, the MIC-less lookup is unique.
    assert m.resolve(Ticker("ABC"), None, D(2020, 6, 1)) == Resolution.found(A)


def test_ticker_reused_after_delisting_resolves_by_date() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNYS, Ticker("OLD"), D(2010, 1, 4), D(2019, 3, 1)))
    m.add_listing(Listing(C, XNYS, Ticker("OLD"), D(2024, 6, 3)))
    assert m.resolve(Ticker("OLD"), XNYS, D(2019, 2, 28)) == Resolution.found(A)
    gap = m.resolve(Ticker("OLD"), XNYS, D(2019, 3, 1))  # end date is exclusive
    assert gap.status == "unknown" and gap.instrument_id is None
    assert m.resolve(Ticker("OLD"), XNYS, D(2022, 1, 1)).status == "unknown"
    assert m.resolve(Ticker("OLD"), XNYS, D(2024, 6, 3)) == Resolution.found(C)


def test_rename_old_ticker_until_end_new_from_start() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNAS, Ticker("FB"), D(2012, 5, 18)))
    m.rename(A, XNAS, Ticker("META"), D(2022, 6, 9))
    assert m.resolve(Ticker("FB"), XNAS, D(2022, 6, 8)) == Resolution.found(A)
    assert m.resolve(Ticker("FB"), XNAS, D(2022, 6, 9)).status == "unknown"
    assert m.resolve(Ticker("META"), XNAS, D(2022, 6, 8)).status == "unknown"
    assert m.resolve(Ticker("META"), XNAS, D(2022, 6, 9)) == Resolution.found(A)
    assert m.ticker_as_of(A, XNAS, D(2022, 6, 8)) == Ticker("FB")
    assert m.ticker_as_of(A, XNAS, D(2026, 1, 1)) == Ticker("META")
    assert m.ticker_as_of(A, XNYS, D(2026, 1, 1)) is None


def test_rename_rejects_without_open_listing_or_onto_taken_ticker() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNYS, Ticker("AAA"), D(2020, 1, 1)))
    m.add_listing(Listing(B, XNYS, Ticker("BBB"), D(2020, 1, 1)))
    with pytest.raises(IdentityError, match="overlap"):
        m.rename(A, XNYS, Ticker("BBB"), D(2025, 1, 1))
    assert m.ticker_as_of(A, XNYS, D(2026, 1, 1)) == Ticker("AAA")  # unchanged
    with pytest.raises(IdentityError, match="no listing"):
        m.rename(C, XNYS, Ticker("CCC"), D(2025, 1, 1))
    with pytest.raises(IdentityError, match="no listing"):
        m.rename(A, XNYS, Ticker("AAB"), D(2020, 1, 1))  # same day as start


def test_overlapping_validity_for_same_mic_ticker_is_rejected() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNYS, Ticker("X"), D(2020, 1, 1), D(2023, 1, 1)))
    with pytest.raises(IdentityError, match="overlap"):
        m.add_listing(Listing(B, XNYS, Ticker("X"), D(2022, 12, 31)))
    m.add_listing(Listing(B, XNYS, Ticker("X"), D(2023, 1, 1)))  # adjacent is fine
    with pytest.raises(IdentityError, match="overlap"):  # one ticker per MIC at once
        m.add_listing(Listing(A, XNYS, Ticker("Y"), D(2022, 1, 1)))


def test_provider_ids_resolve_and_never_guess() -> None:
    m = SecurityMaster()
    alpaca = ProviderId("alpaca", "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415")
    m.add_provider_link(ProviderLink(alpaca, A, D(2020, 1, 1)))
    assert m.resolve_provider(alpaca, D(2026, 1, 1)) == Resolution.found(A)
    assert m.resolve_provider(alpaca, D(2019, 1, 1)).status == "unknown"
    other = ProviderId("snaptrade", alpaca.external_id)  # namespace is part of key
    assert m.resolve_provider(other, D(2026, 1, 1)).status == "unknown"
    with pytest.raises(IdentityError, match="overlap"):
        m.add_provider_link(ProviderLink(alpaca, B, D(2025, 1, 1)))
    for ns, ext in (("", "x"), ("Alpaca", "x"), ("alpaca", ""), ("alpaca", "x" * 129)):
        with pytest.raises(IdentityError):
            ProviderId(ns, ext)


intervals = st.lists(
    st.tuples(
        st.sampled_from([A, B, C]),
        st.sampled_from(["X", "Y"]),
        st.integers(0, 60),
        st.one_of(st.none(), st.integers(1, 30)),
    ),
    max_size=25,
)


@PROFILE
@given(intervals)
def test_property_accepted_listings_never_overlap(
    items: list[tuple[InstrumentId, str, int, int | None]],
) -> None:
    m = SecurityMaster()
    base = D(2026, 1, 1)
    for inst, tick, start, length in items:
        end = None if length is None else base + timedelta(start + length)
        with suppress(IdentityError):
            m.add_listing(
                Listing(inst, XNYS, Ticker(tick), base + timedelta(start), end)
            )
    accepted = m.listings()
    for day in (base + timedelta(i) for i in range(95)):  # independent day scan
        live = [x for x in accepted if x.valid_from <= day < (x.valid_to or D.max)]
        assert len(live) == len({x.ticker for x in live})
        assert len(live) == len({x.instrument_id for x in live})
        for x in live:
            assert m.resolve(x.ticker, XNYS, day) == Resolution.found(x.instrument_id)


def test_validity_dates_reject_datetime() -> None:
    noon = datetime(2026, 1, 2, 12, tzinfo=UTC)
    alpaca = ProviderId("alpaca", "x")
    for bad in (
        lambda: Listing(A, XNYS, Ticker("X"), noon),
        lambda: Listing(A, XNYS, Ticker("X"), D(2026, 1, 1), noon),
        lambda: ProviderLink(alpaca, A, noon),
        lambda: ProviderLink(alpaca, A, D(2026, 1, 1), noon),
    ):
        with pytest.raises(TypeError):
            bad()


def test_rename_to_same_ticker_is_rejected() -> None:
    m = SecurityMaster()
    m.add_listing(Listing(A, XNYS, Ticker("AAA"), D(2020, 1, 1)))
    with pytest.raises(IdentityError, match="same ticker"):
        m.rename(A, XNYS, Ticker("AAA"), D(2025, 1, 1))
    assert m.listings() == (Listing(A, XNYS, Ticker("AAA"), D(2020, 1, 1)),)


def test_resolution_state_is_consistent() -> None:
    assert Resolution.of(set()) == Resolution("unknown")
    for status, iid, cands in (
        ("found", None, ()), ("found", A, ()), ("found", A, (A, B)),
        ("ambiguous", None, (A,)), ("ambiguous", A, (A, B)),
        ("ambiguous", None, (B, A)), ("ambiguous", None, (A, A)),
        ("unknown", A, ()), ("unknown", None, (A,)), ("other", None, ()),
    ):  # fmt: skip
        with pytest.raises(IdentityError):
            Resolution(status, iid, cands)  # type: ignore[arg-type]
