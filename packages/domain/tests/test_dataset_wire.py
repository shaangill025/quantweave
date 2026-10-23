"""Research dataset wire forms and the decision 76 content hash (T032).

Strict, canonical `to_wire`/`from_wire` for `Fact` (with its `Resolution`), `Unit`,
`Period`, `FactBook`, `MembershipEvent`, `Universe` and `ResearchDataset`; the
dataset content hash binds every fact field (owner decision 76).

All data is SYNTHETIC (the facts, events and rights of test_research_data). Expected
wire values are hand-written.
"""

import json
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from qw_domain.filings import (
    Fact,
    FactBook,
    FilingsError,
    Period,
    Unit,
    UnitKind,
    parse_unit,
)
from qw_domain.filings import PublicationBasis as PB
from qw_domain.identity import IdentityError, Resolution
from qw_domain.research_data import (
    Change,
    MembershipEvent,
    ResearchDataset,
    ResearchError,
    Universe,
    canonical,
)
from test_research_data import (
    EVENTS,
    FEED,
    KEY_PERIOD,
    ORIGINAL,
    RESTATED,
    at,
    event,
    freeze,
    iid,
)

type Wire = Any


def via_json(w: object) -> Wire:
    return json.loads(json.dumps(w))


# --- round trips ----------------------------------------------------------------

ORIGINAL_WIRE = {
    "cik": "0009990002",
    "instrument": {"status": "found",
                   "instrument_id": "00000000-0000-0000-0000-000000000001",
                   "candidates": ["00000000-0000-0000-0000-000000000001"]},
    "taxonomy": "us-gaap", "concept": "Revenues", "unit": "USD",
    "period": ["2023-01-01", "2023-12-31"], "fy": 2023, "fp": "FY", "value": "100",
    "accession": "0009990002-24-000001", "form": "10-K", "filed": "2024-02-15",
    "published_at": "2024-02-15T00:00:00Z", "publication_basis": "acceptance_time",
    "received_at": "2024-02-15T00:00:00Z", "feed_id": FEED, "feed_hash": "f" * 64,
    "accepted_at": None,
}  # fmt: skip

VARIANTS = {
    "original": ORIGINAL,
    "accepted": replace(ORIGINAL, accepted_at=at(2024, 2, 14)),
    "ambiguous": replace(ORIGINAL, instrument=Resolution.of({iid(1), iid(2)})),
    "unknown": replace(ORIGINAL, instrument=Resolution("unknown")),
    "instant": replace(ORIGINAL, period=Period(None, date(2023, 12, 31))),
    "no_fy_fp": replace(ORIGINAL, fy=None, fp=None),
    "negative": replace(ORIGINAL, value=Decimal("-12.345")),
    "per_share": replace(ORIGINAL, unit=parse_unit("USD/shares")),
    "shares": replace(ORIGINAL, unit=parse_unit("shares")),
    "pure": replace(ORIGINAL, unit=parse_unit("pure")),
    "filed_bound": replace(ORIGINAL, publication_basis=PB.FILED_DATE_BOUND),
}


def test_fact_wire_is_the_hand_written_form() -> None:
    assert ORIGINAL.to_wire() == ORIGINAL_WIRE
    assert Fact.from_wire(via_json(ORIGINAL_WIRE)) == ORIGINAL


@pytest.mark.parametrize("name", sorted(VARIANTS))
def test_fact_round_trips_through_json(name: str) -> None:
    f = VARIANTS[name]
    back = Fact.from_wire(via_json(f.to_wire()))
    assert back == f and back.to_wire() == f.to_wire()


@pytest.mark.parametrize(
    ("value", "text"),
    [("100.00", "100"), ("1.5E+3", "1500"), ("-0", "0"), ("0E-30", "0"),
     ("0.000000000001", "0.000000000001"), ("-7.50", "-7.5")],
)  # fmt: skip
def test_fact_value_has_one_canonical_decimal_string(value: str, text: str) -> None:
    f = replace(ORIGINAL, value=Decimal(value))
    assert f.to_wire()["value"] == text
    assert Fact.from_wire(via_json(f.to_wire())) == f  # equal as decimals


def test_unit_period_and_resolution_round_trip() -> None:
    for raw in ("USD", "CAD", "USD/shares", "shares", "pure", "EUR"):
        u = Unit.from_wire(raw)
        assert u.to_wire() == raw and Unit.from_wire(u.to_wire()) == u
    assert parse_unit("USD/shares") == Unit(UnitKind.PER_SHARE, "USD", "USD/shares")
    instant, duration = Period(None, date(2023, 12, 31)), KEY_PERIOD
    for p in (instant, duration):
        assert Period.from_wire(via_json(p.to_wire())) == p
    assert Period(None, date(2023, 12, 31)).to_wire() == [None, "2023-12-31"]
    ambiguous = VARIANTS["ambiguous"].instrument
    for r in (ambiguous, Resolution("unknown"), ORIGINAL.instrument):
        assert Resolution.from_wire(via_json(r.to_wire())) == r


def test_factbook_universe_and_event_round_trip() -> None:
    book = FactBook((ORIGINAL, RESTATED))
    assert FactBook.from_wire(via_json(book.to_wire())) == book
    for e in EVENTS:
        assert MembershipEvent.from_wire(via_json(e.to_wire())) == e
    u = Universe("univ-synth", EVENTS)
    assert u.to_wire() == {"universe_id": "univ-synth",
                           "events": [e.to_wire() for e in EVENTS]}  # fmt: skip
    assert Universe.from_wire(via_json(u.to_wire())) == u


def test_dataset_round_trips_and_keeps_its_hash() -> None:
    ds = freeze(facts=(ORIGINAL, RESTATED, VARIANTS["accepted"]), events=EVENTS)
    w = ds.to_wire()
    assert set(w) == {"manifest", "facts", "universe"}
    back = ResearchDataset.from_wire(via_json(w))
    assert back == ds and back.to_wire() == w
    assert back.manifest.content_hash == ds.manifest.content_hash


# --- refusals ---------------------------------------------------------------------


def edit(**kw: object) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda w: w | kw


def drop(key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda w: {k: v for k, v in w.items() if k != key}


def res(status: str, iid: str | None, cands: list[str]) -> dict[str, object]:
    return {"status": status, "instrument_id": iid, "candidates": cands}


UPPER = "00000000-0000-0000-0000-00000000000A"
BAD_FACTS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "value_float": edit(value=100.0),
    "value_int": edit(value=100),
    "value_bool": edit(value=True),
    "value_exponent": edit(value="1E+2"),
    "value_trailing_zero": edit(value="100.0"),
    "value_negative_zero": edit(value="-0"),
    "value_plus": edit(value="+100"),
    "value_nan": edit(value="NaN"),
    "value_space": edit(value=" 100"),
    "value_out_of_bounds": edit(value="1" + "0" * 26),
    "fy_bool": edit(fy=True),
    "fy_float": edit(fy=2023.0),
    "fy_string": edit(fy="2023"),
    "fy_range": edit(fy=1800),
    "fp_bad": edit(fp="Q5"),
    "extra_key": edit(note="x"),
    "missing_key": drop("accepted_at"),
    "missing_instrument": drop("instrument"),
    "offset_not_z": edit(published_at="2024-02-15T00:00:00+00:00"),
    "naive": edit(received_at="2024-02-15T00:00:00"),
    "accepted_empty": edit(accepted_at=""),
    "filed_basic_format": edit(filed="20240215"),
    "period_reversed": edit(period=["2023-12-31", "2023-01-01"]),
    "period_three": edit(period=["2023-01-01", "2023-06-30", "2023-12-31"]),
    "period_tuple_like": edit(period={"start": None, "end": "2023-12-31"}),
    "unit_unknown": edit(unit="USD/kg"),
    "unit_not_text": edit(unit=["USD"]),
    "basis_unknown": edit(publication_basis="filed_date"),
    "feed_hash_upper": edit(feed_hash="F" * 64),
    "feed_bad": edit(feed_id="bad feed"),
    "cik_int": edit(cik=9990002),
    "accession_bad": edit(accession="x"),
    "instrument_mismatch": edit(instrument=res("found", None, [])),
    "instrument_upper": edit(instrument=res("found", UPPER, [UPPER])),
    "instrument_status": edit(instrument=res("maybe", None, [])),
    "instrument_extra": edit(instrument=ORIGINAL_WIRE["instrument"] | {"x": 1}),  # type: ignore[operator]
    "not_an_object": lambda w: [w],
}


@pytest.mark.parametrize("name", sorted(BAD_FACTS))
def test_fact_wire_refuses(name: str) -> None:
    bad = BAD_FACTS[name](via_json(ORIGINAL_WIRE))
    with pytest.raises(FilingsError):
        Fact.from_wire(bad)


def test_unit_period_and_resolution_wires_refuse() -> None:
    for raw in ("usd", "USD/", "USD/share", "", 1, None):
        with pytest.raises(FilingsError, match="unit_wire"):
            Unit.from_wire(raw)
    with pytest.raises(FilingsError, match="unit"):  # kind and raw disagree
        Unit(UnitKind.CURRENCY, "USD", "CAD")
    with pytest.raises(FilingsError, match="unit"):
        Unit("shares", None, "shares")  # type: ignore[arg-type]
    for p in ([None], ["2023-1-1", "2023-12-31"], [None, 20231231], ("x", "y")):
        with pytest.raises(FilingsError, match="period"):
            Period.from_wire(p)
    with pytest.raises(IdentityError):
        Resolution.from_wire({"status": "unknown", "instrument_id": None})


def test_factbook_refuses_duplicates_and_non_lists() -> None:
    w = via_json(FactBook((ORIGINAL,)).to_wire())
    with pytest.raises(FilingsError, match="factbook_wire"):
        FactBook.from_wire(w + w)  # the same version twice
    with pytest.raises(FilingsError, match="factbook_wire"):
        FactBook.from_wire({"facts": w})
    changed = w[0] | {"value": "99"}  # same version, other value
    with pytest.raises(FilingsError, match="fact_conflict"):
        FactBook.from_wire([w[0], changed])


BAD_EVENTS: dict[str, object] = {
    "short": ["00000000-0000-0000-0000-000000000001", "add", "2020-03-02",
              "2020-03-02T00:00:00Z"],
    "offset": ["00000000-0000-0000-0000-000000000001", "add", "2020-03-02",
               "2020-03-02T00:00:00+00:00", FEED],
    "basic_date": ["00000000-0000-0000-0000-000000000001", "add", "20200302",
                   "2020-03-02T00:00:00Z", FEED],
    "change": ["00000000-0000-0000-0000-000000000001", "list", "2020-03-02",
               "2020-03-02T00:00:00Z", FEED],
    "instrument": ["1", "add", "2020-03-02", "2020-03-02T00:00:00Z", FEED],
    "feed": ["00000000-0000-0000-0000-000000000001", "add", "2020-03-02",
             "2020-03-02T00:00:00Z", "bad feed"],
    "number": ["00000000-0000-0000-0000-000000000001", "add", "2020-03-02",
               "2020-03-02T00:00:00Z", 1],
    "object": {"instrument_id": "00000000-0000-0000-0000-000000000001"},
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(BAD_EVENTS))
def test_membership_event_wire_refuses(name: str) -> None:
    assert EVENTS[0].to_wire()[1:] == ["add", "2020-03-02", "2020-03-02T00:00:00Z",
                                       FEED]  # fmt: skip
    with pytest.raises(ResearchError):
        MembershipEvent.from_wire(BAD_EVENTS[name])


def test_universe_wire_refuses() -> None:
    w = via_json(Universe("univ-synth", EVENTS).to_wire())
    for bad in (w | {"x": 1}, {"universe_id": "u"}, w | {"events": {}},
                w | {"universe_id": 1}, [w]):  # fmt: skip
        with pytest.raises(ResearchError, match="universe_wire"):
            Universe.from_wire(bad)
    twice = w | {"events": [w["events"][0], w["events"][0]]}
    with pytest.raises(ResearchError, match="membership_sequence"):
        Universe.from_wire(twice)


def dataset_wire() -> Wire:
    return via_json(freeze(facts=(ORIGINAL, RESTATED), events=EVENTS).to_wire())


def test_dataset_wire_refuses_shape_and_strictness() -> None:
    w = dataset_wire()
    for bad in (w | {"x": 1}, {k: w[k] for k in ("manifest", "facts")}, [w]):
        with pytest.raises(ResearchError, match="dataset_wire"):
            ResearchDataset.from_wire(bad)
    float_value = w | {"facts": [w["facts"][0] | {"value": 100.0}, w["facts"][1]]}
    with pytest.raises(ResearchError, match="dataset_wire"):
        ResearchDataset.from_wire(float_value)
    with pytest.raises(ResearchError, match="dataset_wire"):
        ResearchDataset.from_wire(w | {"facts": w["facts"] + w["facts"][:1]})
    ev = w["universe"]["events"]
    offset = [[*ev[0][:3], "2020-03-02T00:00:00+00:00", FEED], *ev[1:]]
    with pytest.raises(ResearchError, match="event_wire"):
        ResearchDataset.from_wire(w | {"universe": w["universe"] | {"events": offset}})
    with pytest.raises(ResearchError, match="manifest_wire"):
        ResearchDataset.from_wire(w | {"manifest": w["manifest"] | {"x": 1}})


def test_dataset_wire_rederives_the_hash_and_refuses_a_mismatch() -> None:
    w = dataset_wire()
    edited = w | {"facts": [w["facts"][0] | {"value": "101"}, w["facts"][1]]}
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset.from_wire(edited)
    forged = w | {"manifest": w["manifest"] | {"content_hash": "0" * 64}}
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset.from_wire(forged)
    dropped = w | {"universe": w["universe"] | {"events": w["universe"]["events"][:1]}}
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset.from_wire(dropped)


def test_dataset_refuses_a_fact_that_cannot_be_stored() -> None:
    ds = freeze(facts=(ORIGINAL,))
    loose = replace(ORIGINAL, feed_hash="synthetic-hash")  # T023 allows it in memory
    with pytest.raises(ResearchError, match="fact_wire"):
        ResearchDataset(ds.manifest, FactBook((loose,)), ds.universe)
    with pytest.raises(ResearchError, match="fact_wire"):
        freeze(facts=(loose,))


# --- decision 76: the hash binds every fact field ---------------------------------

FIELD_CHANGES: dict[str, Any] = {
    "cik": "0009990003",
    "instrument": Resolution.found(iid(2)),
    "taxonomy": "dei",
    "concept": "NetIncomeLoss",
    "unit": parse_unit("CAD"),
    "period": Period(date(2023, 1, 1), date(2023, 6, 30)),
    "fy": 2022,
    "fp": "Q4",
    "value": Decimal("101"),
    "accession": "0009990002-24-000002",
    "form": "10-K/A",
    "filed": date(2024, 2, 14),
    "published_at": at(2024, 2, 16),
    "publication_basis": PB.FILED_DATE_BOUND,
    "received_at": at(2024, 2, 16),
    "feed_id": "feed-synth-other",
    "feed_hash": "e" * 64,
    "accepted_at": at(2024, 2, 15),
}


def test_every_fact_field_has_a_hash_case() -> None:
    assert set(FIELD_CHANGES) == {f.name for f in fields(Fact)}


@pytest.mark.parametrize("name", list(FIELD_CHANGES))
def test_changing_any_single_fact_field_changes_the_content_hash(name: str) -> None:
    ds = freeze(facts=(ORIGINAL,), events=EVENTS)
    changed = replace(ORIGINAL, **{name: FIELD_CHANGES[name]})
    assert changed != ORIGINAL
    assert ResearchDataset(ds.manifest, FactBook((ORIGINAL,)), ds.universe) == ds
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset(ds.manifest, FactBook((changed,)), ds.universe)
    w = ds.to_wire()
    w["facts"] = [changed.to_wire()]
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset.from_wire(via_json(w))


def test_moving_a_fact_to_another_instrument_changes_the_hash() -> None:
    """Decision 76: the holdout split hides facts by instrument, so a fact moved
    from a holdout name to a development name must not keep the hash."""
    moved = replace(ORIGINAL, instrument=Resolution.found(iid(2)))
    a = freeze(facts=(ORIGINAL,), events=EVENTS)
    b = freeze(facts=(moved,), events=EVENTS)
    assert a.manifest.content_hash != b.manifest.content_hash
    unresolved = freeze(facts=(replace(ORIGINAL, instrument=Resolution("unknown")),),
                        events=EVENTS)  # fmt: skip
    assert unresolved.manifest.content_hash not in {
        a.manifest.content_hash, b.manifest.content_hash}  # fmt: skip
    w = via_json(a.to_wire())
    w["facts"][0]["instrument"] = moved.instrument.to_wire()
    with pytest.raises(ResearchError, match="integrity"):
        ResearchDataset.from_wire(via_json(w))


def test_hash_ignores_record_order_but_not_events() -> None:
    a = freeze(facts=(ORIGINAL, RESTATED), events=EVENTS)
    b = freeze(facts=(RESTATED, ORIGINAL), events=tuple(reversed(EVENTS)))
    assert a.manifest.content_hash == b.manifest.content_hash
    third = event(3, Change.ADD, date(2024, 1, 2), at(2024, 1, 2))
    c = freeze(facts=(ORIGINAL, RESTATED), events=(*EVENTS, third))
    assert c.manifest.content_hash != a.manifest.content_hash


def test_dataset_has_one_stored_form_and_refuses_an_unsorted_wire() -> None:
    """Facts sorted by the canonical JSON of their wire, events by their wire, so
    one dataset has exactly one stored form (decision 74 compares bytes)."""
    third = event(3, Change.ADD, date(2024, 1, 2), at(2024, 1, 2))
    a = freeze(facts=(RESTATED, ORIGINAL), events=(third, *reversed(EVENTS)))
    b = freeze(facts=(ORIGINAL, RESTATED), events=(*EVENTS, third))
    assert canonical(a.to_wire()) == canonical(b.to_wire()) and a == b
    w: Wire = a.to_wire()
    assert w["facts"] == sorted(w["facts"], key=canonical) and len(w["facts"]) == 2
    events = w["universe"]["events"]
    assert events == sorted(events) and len(events) == 4
    w = via_json(w)
    swapped = w | {"facts": w["facts"][::-1]}
    with pytest.raises(ResearchError, match="dataset_wire"):
        ResearchDataset.from_wire(swapped)
    rev = w | {"universe": w["universe"] | {"events": w["universe"]["events"][::-1]}}
    with pytest.raises(ResearchError, match="dataset_wire"):
        ResearchDataset.from_wire(rev)
    assert ResearchDataset.from_wire(w) == a
