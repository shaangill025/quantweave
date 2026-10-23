"""The PriceSeries and PriceBar wire form (backtest.py) the protected
holdout_series store keeps (owner decision 41): decimal strings, UTC instants,
canonical only. SYNTHETIC bars."""

import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from qw_domain.backtest import PriceBar, PriceSeries
from qw_domain.research_data import EvidenceClass, ResearchError, digest
from test_research_data import iid

D = Decimal
PLUS2 = timezone(timedelta(hours=2))
DAY0 = datetime(2024, 1, 2, 16, 30, tzinfo=PLUS2)  # 14:30 UTC


def series(opens: tuple[str, ...] = ("100.25", "101", "99.875")) -> PriceSeries:
    out = []
    for k, o in enumerate(opens):
        start = DAY0 + timedelta(days=k)
        end = start + timedelta(hours=6, minutes=30, microseconds=5 * k)
        out.append(PriceBar(start, end, end + timedelta(minutes=1), D(o), D(o) + 1))
    return PriceSeries(
        "px-synth-h", "tenant-synth", iid(3), EvidenceClass.SYNTHETIC, tuple(out)
    )


def test_round_trip_keeps_decimals_and_utc() -> None:
    s = series()
    w = s.to_wire()
    assert w["bars"][0] == [
        "2024-01-02T14:30:00Z",
        "2024-01-02T21:00:00Z",
        "2024-01-02T21:01:00Z",
        "100.25",
        "101.25",
    ]
    assert w["bars"][1][1] == "2024-01-03T21:00:00.000005Z"
    assert w["instrument"] == iid(3).to_wire() and w["evidence_class"] == "synthetic"
    back = PriceSeries.from_wire(json.loads(json.dumps(w)))  # through JSON text
    assert back == s and back.bars[2].open == D("99.875")
    assert type(back.bars[0].close) is Decimal
    assert all(b.start.tzinfo is UTC for b in back.bars)
    assert back.to_wire() == w
    assert digest(w) == s.content_hash  # the wire is what content_hash covers


def test_empty_series_round_trips() -> None:
    s = PriceSeries("px-0", "tenant-synth", iid(1), EvidenceClass.HISTORICAL, ())
    assert PriceSeries.from_wire(s.to_wire()) == s


def _bad(path: tuple[Any, ...], value: object) -> dict[str, Any]:
    w: Any = json.loads(json.dumps(series().to_wire()))
    target = w
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return dict(w)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("extra",), "x"),  # unknown key
        (("series_id",), KeyError),  # missing key
        (("series_id",), 7),
        (("series_id",), "bad id with spaces"),
        (("instrument",), "not-a-uuid"),
        (("evidence_class",), "real"),
        (("bars",), "none"),
        (("bars", 0), ["2024-01-02T14:30:00Z"]),  # too short
        (("bars", 0, 3), 100.25),  # a JSON number, never a float
        (("bars", 0, 3), "NaN"),
        (("bars", 0, 3), "1.0025E+2"),  # not the canonical decimal string
        (("bars", 0, 3), "-1"),
        (("bars", 0, 3), "0"),
        (("bars", 0, 0), "2024-01-02T16:30:00+02:00"),  # an offset, not UTC Z
        (("bars", 0, 0), "2024-01-02T14:30:00"),  # no offset
        (("bars", 0, 0), 1704205800),  # an epoch number, never an instant
        (("bars", 0, 0), "2024-01-02T22:30:00Z"),  # start after end
        (
            ("bars", 1),
            [
                "2024-01-02T14:30:00Z",
                "2024-01-02T21:00:00Z",
                "2024-01-02T21:01:00Z",
                "1",
                "1",
            ],
        ),  # overlaps bar 0
    ],
)
def test_malformed_or_non_canonical_wire_is_refused(
    path: tuple[Any, ...], value: object
) -> None:
    with pytest.raises(ResearchError):
        PriceSeries.from_wire(_bad(path, value))


@pytest.mark.parametrize("wire", [None, [], "x", {"bars": []}])
def test_non_object_wire_is_refused(wire: object) -> None:
    with pytest.raises(ResearchError):
        PriceSeries.from_wire(wire)


@pytest.mark.parametrize(
    "wire", [None, ["2024-01-02T14:30:00Z"] * 5, ("a", "b", "c", "1", "1")]
)
def test_bar_wire_is_refused_unless_five_valid_strings(wire: object) -> None:
    with pytest.raises(ResearchError):
        PriceBar.from_wire(wire)


def test_bar_wire_round_trips() -> None:
    b = series().bars[1]
    assert PriceBar.from_wire(b.to_wire()) == b
