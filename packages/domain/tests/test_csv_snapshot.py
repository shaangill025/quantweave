"""Canonical CSV holdings-snapshot files and the preview wire form (T014 PR B).
SYNTHETIC rows only; expected figures are the literal values written into the rows.
The wire form is checked against the specification's JSON Schema."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain.csv_import import (
    CANONICAL_FIELDS,
    ColumnMapping,
    FileKind,
    ImportRequest,
    Reason,
    RowStatus,
    commit,
    preview,
)
from qw_domain.csv_snapshot import SnapshotPreview, preview_snapshot
from qw_domain.decimals import Money, Quantity
from qw_domain.identity import InstrumentId, Listing, Mic, SecurityMaster, Ticker
from qw_domain.import_wire import preview_to_wire
from qw_domain.journal import Journal
from qw_domain.sources import (
    MappedInterval,
    Source,
    SourceKind,
    SourceMap,
    UnderlyingAccount,
    consolidate,
)
from referencing import Registry, Resource

SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
ACCT, T0 = "acct-syn-1", datetime(2026, 2, 1, tzinfo=UTC)
HEADER = ",".join(CANONICAL_FIELDS)
DEPOSIT = "SYN-ACCOUNT,SYN-BROKER,D1,2026-01-02T15:00:00Z,deposit,,,,1000,0,USD"


def world() -> tuple[Journal, SourceMap, SecurityMaster]:
    smap = SourceMap()
    smap.add_account(UnderlyingAccount(ACCT, "tenant-1", "SYN", "cash", "USD"))
    smap.add_source(Source("SYN-BROKER", "tenant-1", SourceKind.BROKER_EXPORT))
    start = datetime(2025, 1, 1, tzinfo=UTC)
    smap.remap("SYN-BROKER", "SYN-ACCOUNT", [MappedInterval(ACCT, start)], start)
    master = SecurityMaster()
    master.add_listing(Listing(SYN, Mic("XNYS"), Ticker("SYNTH"), date(2025, 1, 1)))
    return Journal(), smap, master


SNAP = ImportRequest("tenant-1", FileKind.SNAPSHOT, ColumnMapping.identity())
ROWS = [
    HEADER,
    "SYN-ACCOUNT,SYN-BROKER,P1,2026-01-31T21:00:00Z,position,SYNTH,50,,,,USD",
    "SYN-ACCOUNT,SYN-BROKER,C1,2026-01-31T21:00:00Z,cash,,,,738,,USD",
]


def snap(*extra: str) -> SnapshotPreview:
    _, smap, master = world()
    return preview_snapshot(
        "\n".join([*ROWS, *extra]).encode(), SNAP, smap, master, T0, True
    )


def test_snapshot_file_feeds_consolidate_and_never_posts() -> None:
    journal, smap, master = world()
    result = preview_snapshot("\n".join(ROWS).encode(), SNAP, smap, master, T0, True)
    assert result.preview.state == "validated" and result.preview.journal_id is None
    (snapshot,) = result.snapshots
    view = consolidate(smap, "tenant-1", result.snapshots, T0, timedelta(days=2))
    acct = view.accounts[ACCT]
    assert snapshot.complete and acct.status == "reconciled"
    assert acct.units == {(SYN, "USD"): Quantity(50)}
    assert acct.cash == {"USD": Money.of(738, "USD")}
    assert commit(result.preview, journal, T0, "tenant-1").outcome == "rejected"
    assert journal.events() == ()


@pytest.mark.parametrize(
    ("extra", "status", "reason"),
    [
        (DEPOSIT, RowStatus.QUARANTINED, Reason.MIXED_FILE_KIND),
        (ROWS[1].replace("P1", "P2"), RowStatus.QUARANTINED, Reason.DUPLICATE_LINE),
        (ROWS[1].replace(",50,", ",40,"), RowStatus.CONFLICT, None),
        (
            ROWS[2].replace("C1", "C3").replace(",,,,738", ",,1,,738"),
            RowStatus.QUARANTINED,
            Reason.FIELD_INVALID,
        ),
        (
            ROWS[2].replace("C1", "C2").replace("738", "7.5e2"),
            RowStatus.QUARANTINED,
            Reason.MALFORMED_DECIMAL,
        ),
    ],
)
def test_snapshot_rows_block_the_file(
    extra: str, status: RowStatus, reason: Reason | None
) -> None:
    result = snap(extra)
    last = result.preview.rows[-1]
    assert last.status is status and result.snapshots == ()
    assert reason is None or last.reason is reason
    assert result.preview.state == "needs_resolution"


def test_repeated_identical_record_is_a_duplicate() -> None:
    result = snap(ROWS[1])
    assert result.preview.count(RowStatus.DUPLICATE) == 1 and len(result.snapshots) == 1


def test_snapshot_preview_takes_only_snapshot_requests() -> None:
    _, smap, master = world()
    tx = ImportRequest("tenant-1", FileKind.TRANSACTIONS, ColumnMapping.identity())
    with pytest.raises(ValueError, match="csv_import"):
        preview_snapshot(b"", tx, smap, master, T0)


SCHEMAS = Path(__file__).resolve().parents[3] / "docs/spec/contracts/schemas"


def test_preview_wire_form_matches_the_contract() -> None:
    common = json.loads((SCHEMAS / "common.schema.json").read_text())
    registry = Registry().with_resource(
        "common.schema.json", Resource.from_contents(common)
    )
    schema = json.loads((SCHEMAS / "import_preview.schema.json").read_text())
    validator = Draft202012Validator(schema, registry=registry)
    journal, smap, master = world()
    tx = ImportRequest("tenant-1", FileKind.TRANSACTIONS, ColumnMapping.identity())
    files = [b"x", "\n".join([HEADER, DEPOSIT]).encode()]
    files.append(files[1] + b"\n" + DEPOSIT.replace("1000", "9").encode())
    files.append(
        files[1] + b"\n" + DEPOSIT.replace("D1", "D2X").replace("USD", "usd").encode()
    )
    wires = [preview_to_wire(preview(f, tx, journal, smap, master, T0)) for f in files]
    wires.append(preview_to_wire(snap().preview))
    for wire in wires:
        assert list(validator.iter_errors(wire)) == []
    states = [w["state"] for w in wires]
    assert states == [
        "rejected",
        "validated",
        "needs_resolution",
        "needs_resolution",
        "validated",
    ]
    assert [i["code"] for i in wires[2]["issues"]] == ["conflict"]  # type: ignore[attr-defined]
    assert [i["code"] for i in wires[3]["issues"]] == ["currency_invalid"]  # type: ignore[attr-defined]
    assert (wires[1]["accepted_rows"], wires[1]["target_account_id"]) == (1, ACCT)
