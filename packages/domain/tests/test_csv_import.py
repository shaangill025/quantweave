"""Canonical CSV import preview and commit (T014; spec §4 import workflow, §12; F-25).

SYNTHETIC inputs only: docs/spec/tests/fixtures/canonical_transactions.csv and files
built in this module. NUM01 expectations are read from numerical_oracles.json; the
shuffle property compares against the unshuffled import, not against a model.
"""

import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.corporate_actions import SourceRef
from qw_domain.csv_import import (
    CANONICAL_FIELDS,
    ColumnMapping,
    FileKind,
    ImportPreview,
    ImportRequest,
    Limits,
    MappingError,
    Reason,
    RowStatus,
    commit,
    preview,
    spreadsheet_safe,
)
from qw_domain.decimals import Money, Quantity
from qw_domain.identity import InstrumentId, Listing, Mic, SecurityMaster, Ticker
from qw_domain.journal import Journal, ObservationLog, materialize
from qw_domain.postings import EventKind, Header, deposit
from qw_domain.sources import (
    MappedInterval,
    Source,
    SourceKind,
    SourceMap,
    UnderlyingAccount,
    consolidate,
)

FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
FIXTURE = (FIXTURES / "canonical_transactions.csv").read_bytes()
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
ACCT = "acct-syn-1"  # the reference SYN-ACCOUNT maps here through the SourceMap
T0 = datetime(2026, 2, 1, tzinfo=UTC)
HEADER = ",".join(CANONICAL_FIELDS)
TX = ImportRequest("tenant-1", FileKind.TRANSACTIONS, ColumnMapping.identity())


def world() -> tuple[Journal, SourceMap, SecurityMaster]:
    smap = SourceMap()
    smap.add_account(UnderlyingAccount(ACCT, "tenant-1", "SYN", "cash", "USD"))
    smap.add_source(Source("SYN-BROKER", "tenant-1", SourceKind.BROKER_EXPORT))
    start = datetime(2025, 1, 1, tzinfo=UTC)
    smap.remap("SYN-BROKER", "SYN-ACCOUNT", [MappedInterval(ACCT, start)], start)
    master = SecurityMaster()
    master.add_listing(Listing(SYN, Mic("XNYS"), Ticker("SYNTH"), date(2025, 1, 1)))
    return Journal(), smap, master


def run(
    data: bytes, journal: Journal | None = None, request: ImportRequest = TX
) -> tuple[ImportPreview, Journal, SourceMap]:
    fresh, smap, master = world()
    j = fresh if journal is None else journal
    return preview(data, request, j, smap, master, T0), j, smap


def fixture_lines() -> list[str]:
    return FIXTURE.decode().splitlines()


def edit(record: str, **changes: str) -> bytes:
    """The fixture with fields of one record replaced (by canonical name)."""
    out = []
    for line in fixture_lines():
        cells = line.split(",")
        if cells[2] == record:
            row = dict(zip(CANONICAL_FIELDS, cells, strict=True)) | changes
            cells = [row[f] for f in CANONICAL_FIELDS]
        out.append(",".join(cells))
    return "\n".join(out).encode()


ROW_OF = {"D1": 1, "B1": 2, "S1": 3}


def outcome(p: ImportPreview, record: str) -> tuple[RowStatus, Reason | None]:
    (row,) = [r for r in p.rows if r.record_id == record]
    return row.status, row.reason


# ---- Fixture: NUM01 through the importer, idempotent re-import, conflicts


def test_fixture_imports_and_reproduces_num01() -> None:
    p, journal, _ = run(FIXTURE)
    assert p.state == "validated" and p.accepted_rows == 3 == p.row_count
    assert journal.events() == ()  # a preview is not a mutation
    result = commit(p, journal, T0)
    assert (result.outcome, result.posted) == ("committed", 3)
    exp = ORACLES["NUM01"]["expected"]
    pos = materialize(journal.events())
    held = pos.holding(ACCT, SYN)
    assert pos.cash == {(ACCT, "USD"): Money.of(exp["cash"], "USD")}
    assert held.quantity == Quantity(exp["remaining_qty"])
    assert held.cost == Money.of(exp["remaining_cost"], "USD")
    assert pos.realized == {(ACCT, "USD"): Money.of(exp["realized"], "USD")}
    sources = {e.source for e in journal.events()}
    assert sources == {SourceRef("SYN-BROKER", r) for r in ("D1", "B1", "S1")}
    wire = p.to_wire()
    assert wire["file_hash"] == p.file_hash and len(p.file_hash) == 64
    assert wire["mapping_version_id"] == "canonical-identity-1"
    assert (wire["state"], wire["source_kind"], wire["accepted_rows"]) == (
        "validated",
        "canonical",
        3,
    )


def test_reimport_is_noop_and_changed_row_conflicts() -> None:
    journal, log = world()[0], ObservationLog()
    commit(run(FIXTURE, journal)[0], journal, T0, log)
    before = (journal.events(), journal.revision(ACCT))
    again = run(FIXTURE, journal)[0]
    assert again.duplicate_rows == 3 and again.state == "validated"
    result = commit(again, journal, T0 + timedelta(days=1), log)
    assert (result.outcome, result.posted) == ("committed", 0)
    assert (journal.events(), journal.revision(ACCT)) == before
    changed = run(edit("D1", gross_cash="2000"), journal)[0]
    assert outcome(changed, "D1") == (RowStatus.CONFLICT, None)
    assert outcome(changed, "B1") == (RowStatus.DUPLICATE, None)
    issues = changed.to_wire()["issues"]
    assert issues == [
        {
            "code": "conflict",
            "message": "row 1: record differs",
            "affected_ids": [],
            "blocking": True,
        }
    ]
    assert changed.state == "needs_resolution"
    assert commit(changed, journal, T0 + timedelta(days=2)).outcome == "rejected"
    assert journal.events() == before[0]
    assert len(log.conflicts) == 0  # the conflicting row never reached the log


def test_commit_against_stale_revision_conflicts() -> None:
    p, journal, _ = run(FIXTURE)
    h = Header(ACCT, T0, SourceRef("SYN-MANUAL", "X1"))
    journal.post(deposit(h, Money.of(5, "USD")), T0)
    result = commit(p, journal, T0)
    assert result.outcome == "conflict" and result.posted == 0
    assert len(journal.events()) == 1


def test_bom_and_crlf_files_match_plain() -> None:
    plain = materialize(_committed(FIXTURE).events())
    for data in (b"\xef\xbb\xbf" + FIXTURE, FIXTURE.replace(b"\n", b"\r\n")):
        assert materialize(_committed(data).events()) == plain


def _committed(data: bytes) -> Journal:
    p, journal, _ = run(data)
    assert p.state == "validated", p.rows
    assert commit(p, journal, T0).outcome == "committed"
    return journal


# ---- Row quarantine: one adversarial file per reason


@pytest.mark.parametrize(
    ("record", "changes", "reason"),
    [
        ("B1", {"quantity": "1e1"}, Reason.MALFORMED_DECIMAL),
        ("B1", {"price": "50.0"}, None),  # control: trailing zero is fine
        ("B1", {"fee": "-1"}, Reason.FIELD_INVALID),
        ("B1", {"quantity": "10.0000000000001"}, Reason.SCALE_OVERFLOW),
        ("B1", {"price": "0.333333333333", "quantity": "10.5"}, Reason.SCALE_OVERFLOW),
        ("B1", {"effective_at": "2026-01-05T15:00:00"}, Reason.MALFORMED_TIMESTAMP),
        ("B1", {"effective_at": "2026-02-30T15:00:00Z"}, Reason.MALFORMED_TIMESTAMP),
        ("B1", {"kind": "transfer_in"}, Reason.UNKNOWN_KIND),
        ("B1", {"currency": ""}, Reason.CURRENCY_INVALID),
        ("B1", {"currency": "usd"}, Reason.CURRENCY_INVALID),
        ("B1", {"gross_cash": "-499"}, Reason.AMOUNT_MISMATCH),
        ("B1", {"symbol": "NOPE"}, Reason.UNRESOLVED_SYMBOL),
        ("B1", {"symbol": ""}, Reason.FIELD_INVALID),
        ("D1", {"symbol": "SYNTH"}, Reason.FIELD_INVALID),
        ("D1", {"gross_cash": "-1000"}, Reason.FIELD_INVALID),
        ("B1", {"account_ref": "OTHER"}, Reason.UNMAPPED_ACCOUNT),
        ("B1", {"source_ref": "SYN-UNKNOWN"}, Reason.UNMAPPED_ACCOUNT),
        ("B1", {"source_ref": "bad source"}, Reason.INVALID_REFERENCE),
        ("S1", {"quantity": "11", "gross_cash": "660"}, Reason.INSUFFICIENT_UNITS),
        ("B1", {"symbol": "=HYPERLINK(1)"}, Reason.FORMULA_INJECTION),
        ("B1", {"record_id": "@SUM(A1)"}, Reason.FORMULA_INJECTION),
        ("D1", {"account_ref": "+SYN"}, Reason.FORMULA_INJECTION),
        ("D1", {"source_ref": "-SYN"}, Reason.FORMULA_INJECTION),
        ("D1", {"kind": '"\tdeposit"'}, Reason.FORMULA_INJECTION),
        ("D1", {"record_id": '"\rD1"'}, Reason.FORMULA_INJECTION),
        ("D1", {"symbol": "X" * 300}, Reason.FIELD_TOO_LONG),
        ("D1", {"currency": "USD,extra"}, Reason.COLUMN_COUNT),
        ("D1", {"kind": "position"}, Reason.MIXED_FILE_KIND),
    ],
)
def test_row_quarantine(record: str, changes: dict[str, str], reason: Reason) -> None:
    p, journal, _ = run(edit(record, **changes))
    row = p.rows[ROW_OF[record] - 1]
    assert row.row_number == ROW_OF[record]
    if reason is None:
        assert p.state == "validated"
        return
    assert (row.status, row.reason) == (RowStatus.QUARANTINED, reason), row
    assert row.event is None and row.message
    assert p.state == "needs_resolution" and p.rejected_rows >= 1
    assert commit(p, journal, T0).outcome == "rejected" and journal.events() == ()


def test_sell_without_prior_cost_is_quarantined() -> None:
    data = "\n".join(line for line in fixture_lines() if ",B1," not in line).encode()
    p, _, _ = run(data)
    assert outcome(p, "S1") == (RowStatus.QUARANTINED, Reason.UNKNOWN_COST)
    assert outcome(p, "D1") == (RowStatus.ACCEPTED, None)


def test_mixed_currency_fee_column() -> None:
    mapping = ColumnMapping(
        "with-fee-ccy-1", {f: f for f in (*CANONICAL_FIELDS, "fee_currency")}
    )
    request = ImportRequest("tenant-1", FileKind.TRANSACTIONS, mapping)
    lines = fixture_lines()
    rows = [lines[0] + ",fee_currency", lines[1] + ",", lines[2] + ",CAD"]
    p = run("\n".join([*rows, lines[3] + ",USD"]).encode(), request=request)[0]
    assert outcome(p, "B1") == (RowStatus.QUARANTINED, Reason.MIXED_CURRENCY)
    assert outcome(p, "D1") == (RowStatus.ACCEPTED, None)
    assert outcome(p, "S1") == (RowStatus.QUARANTINED, Reason.UNKNOWN_COST)


def test_injection_cell_is_never_echoed_raw() -> None:
    p = run(edit("B1", symbol="=cmd|' /C calc'!A0"))[0]
    row = next(r for r in p.rows if r.record_id == "B1")
    assert row.reason is Reason.FORMULA_INJECTION
    assert not row.message.startswith(("=", "+", "-", "@"))
    assert spreadsheet_safe("=1+2") == "'=1+2" and spreadsheet_safe("-5") == "'-5"
    assert spreadsheet_safe("\t=x") == "'\t=x" and spreadsheet_safe("SYNTH") == "SYNTH"


def test_unknown_columns_are_kept_as_unknown_fields() -> None:
    lines = fixture_lines()
    data = "\n".join([lines[0] + ",note"] + [x + ",n" for x in lines[1:]]).encode()
    p = run(data)[0]
    assert p.state == "validated"
    assert [i.code for i in p.issues] == ["unknown_columns"]
    assert all(r.unknown_fields == {"note": "n"} for r in p.rows)


# ---- File rejection and limits


@pytest.mark.parametrize(
    ("data", "limits", "code"),
    [
        (FIXTURE, Limits(max_bytes=100), "file_too_large"),
        (FIXTURE, Limits(max_rows=2), "too_many_rows"),
        (FIXTURE, Limits(max_columns=10), "too_many_columns"),
        (FIXTURE.replace(b"B1", b"B\x001"), Limits(), "nul_byte"),
        (FIXTURE.replace(b"SYNTH", b"SYN\xff"), Limits(), "encoding"),
        (FIXTURE.replace(b"kind", b"symbol"), Limits(), "duplicate_header"),
        (FIXTURE.replace(b",fee,", b",fees,"), Limits(), "missing_column"),
        (b"", Limits(), "empty_file"),
        (FIXTURE.replace(b"D1", b'"D"1'), Limits(), "csv_malformed"),
        (FIXTURE.replace(b"B1,", b'"B\n1",'), Limits(), None),
        (HEADER.replace("fee", "f" * 300).encode(), Limits(), "field_too_long"),
    ],
)
def test_file_rejected(data: bytes, limits: Limits, code: str | None) -> None:
    request = ImportRequest(TX.tenant_id, TX.file_kind, TX.mapping, limits=limits)
    p, journal, _ = run(data, request=request)
    if code is None:  # control: a quoted newline is a valid cell, not a file error
        assert p.state == "needs_resolution" and p.row_count == 3
        return
    assert p.state == "rejected" and p.rows == () and p.issues[0].code == code
    assert commit(p, journal, T0).outcome == "rejected"


def test_format_version_is_explicit() -> None:
    request = ImportRequest("tenant-1", FileKind.TRANSACTIONS, TX.mapping, "csv/9")
    assert run(FIXTURE, request=request)[0].issues[0].code == "format_version"


# ---- Column mapping


def test_user_mapping_from_source_headers() -> None:
    renamed = {f: f"Col {f.title()}" for f in CANONICAL_FIELDS}
    header = ",".join(renamed[f] for f in CANONICAL_FIELDS)
    data = "\n".join([header, *fixture_lines()[1:]]).encode()
    mapping = ColumnMapping("broker-x-2", {v: k for k, v in renamed.items()})
    request = ImportRequest("tenant-1", FileKind.TRANSACTIONS, mapping)
    p = run(data, request=request)[0]
    assert p.state == "validated" and p.mapping_version_id == "broker-x-2"
    assert p.file_hash != run(FIXTURE)[0].file_hash


@pytest.mark.parametrize(
    ("version", "columns", "code"),
    [
        ("v1", {"a": "kind", "b": "kind"}, "mapping_duplicate"),
        ("v1", {"a": "colour"}, "mapping_unknown_field"),
        ("v1", {"kind": "kind"}, "mapping_missing"),
        ("bad version", {f: f for f in CANONICAL_FIELDS}, "mapping_version"),
    ],
)
def test_mapping_validation(version: str, columns: dict[str, str], code: str) -> None:
    with pytest.raises(MappingError) as err:
        ColumnMapping(version, columns)
    assert err.value.code == code


# ---- Snapshot files feed sources.consolidate; never mixed with transactions


def test_snapshot_file_produces_source_snapshots() -> None:
    rows = [
        HEADER,
        "SYN-ACCOUNT,SYN-BROKER,P1,2026-01-31T21:00:00Z,position,SYNTH,50,,,,USD",
        "SYN-ACCOUNT,SYN-BROKER,C1,2026-01-31T21:00:00Z,cash,,,,738,,USD",
    ]
    request = ImportRequest(
        "tenant-1", FileKind.SNAPSHOT, TX.mapping, snapshot_complete=True
    )
    p, journal, smap = run("\n".join(rows).encode(), request=request)
    assert p.state == "validated" and journal.events() == ()
    (snap,) = p.snapshots
    view = consolidate(smap, "tenant-1", p.snapshots, T0, timedelta(days=2))
    acct = view.accounts[ACCT]
    assert snap.complete and acct.status == "reconciled"
    assert acct.units == {(SYN, "USD"): Quantity(50)}
    assert acct.cash == {"USD": Money.of(738, "USD")}
    assert commit(p, journal, T0).outcome == "rejected"  # snapshots never post
    mixed = run("\n".join([*rows, fixture_lines()[1]]).encode(), request=request)[0]
    assert outcome(mixed, "D1") == (RowStatus.QUARANTINED, Reason.MIXED_FILE_KIND)
    assert mixed.snapshots == ()
    dup = run("\n".join([*rows, rows[1].replace("P1", "P2")]).encode(), request=request)
    assert outcome(dup[0], "P2") == (RowStatus.QUARANTINED, Reason.DUPLICATE_LINE)
    again = run("\n".join([*rows, rows[1]]).encode(), request=request)[0]
    assert again.duplicate_rows == 1 and len(again.snapshots) == 1
    changed = run(
        "\n".join([*rows, rows[1].replace(",50,", ",40,")]).encode(), request=request
    )
    assert [r.status for r in changed[0].rows][-1] is RowStatus.CONFLICT


# ---- Property: row order does not change the result


@st.composite
def transaction_rows(draw: st.DrawFn) -> list[str]:
    """A deposit, buys R0..Rn at distinct times, and a sale of R0 later that day."""
    rows = ["SYN-ACCOUNT,SYN-BROKER,D0,2026-01-01T00:00:00Z,deposit,,,,100000,0,USD"]
    for i in range(draw(st.integers(1, 12))):
        day, qty, price = (
            draw(st.integers(lo, hi)) for lo, hi in ((1, 20), (1, 30), (1, 99))
        )
        fee = draw(st.sampled_from(["0", "1", "0.5"]))
        at = f"2026-01-{day:02d}T{i:02d}:00:00Z"
        rows.append(
            f"SYN-ACCOUNT,SYN-BROKER,R{i},{at},buy,SYNTH,{qty},{price},"
            f"-{qty * price},{fee},USD"
        )
    cells = rows[1].split(",")
    cells[2], cells[4], cells[8] = "S0", "sell", cells[8].removeprefix("-")
    cells[3] = cells[3].replace("T00:00:00Z", "T23:59:59Z")
    return [*rows, ",".join(cells)]


@settings(derandomize=True, database=None, max_examples=60, deadline=None)
@given(transaction_rows(), st.randoms(use_true_random=False))
def test_shuffled_rows_give_identical_positions(
    rows: list[str], rnd: random.Random
) -> None:
    shuffled = rows[:]
    rnd.shuffle(shuffled)
    first = _committed("\n".join([HEADER, *rows]).encode())
    second = _committed("\n".join([HEADER, *shuffled]).encode())
    assert materialize(first.events()) == materialize(second.events())
    assert first.events() == second.events()
    assert [e.kind for e in first.events()].count(EventKind.SELL) == 1
