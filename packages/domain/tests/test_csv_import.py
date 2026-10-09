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
from qw_domain import csv_import as ci
from qw_domain import sources as src
from qw_domain.corporate_actions import SourceRef
from qw_domain.csv_import import Reason, RowStatus, commit, preview
from qw_domain.decimals import Money, Quantity
from qw_domain.identity import InstrumentId, Listing, Mic, SecurityMaster, Ticker
from qw_domain.journal import Journal, ObservationLog, materialize
from qw_domain.postings import EventKind, Header, deposit

FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
FIXTURE = (FIXTURES / "canonical_transactions.csv").read_bytes()
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
ACCT = "acct-syn-1"  # the reference SYN-ACCOUNT maps here through the src.SourceMap
T0 = datetime(2026, 2, 1, tzinfo=UTC)
HEADER = ",".join(ci.CANONICAL_FIELDS)
TX = ci.ImportRequest("tenant-1", ci.FileKind.TRANSACTIONS, ci.ColumnMapping.identity())


def world() -> tuple[Journal, src.SourceMap, SecurityMaster]:
    smap = src.SourceMap()
    smap.add_account(src.UnderlyingAccount(ACCT, "tenant-1", "SYN", "cash", "USD"))
    smap.add_source(src.Source("SYN-BROKER", "tenant-1", src.SourceKind.BROKER_EXPORT))
    start = datetime(2025, 1, 1, tzinfo=UTC)
    smap.remap("SYN-BROKER", "SYN-ACCOUNT", [src.MappedInterval(ACCT, start)], start)
    master = SecurityMaster()
    master.add_listing(Listing(SYN, Mic("XNYS"), Ticker("SYNTH"), date(2025, 1, 1)))
    return Journal(), smap, master


def run(
    data: bytes, journal: Journal | None = None, request: ci.ImportRequest = TX
) -> tuple[ci.ImportPreview, Journal, src.SourceMap]:
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
            row = dict(zip(ci.CANONICAL_FIELDS, cells, strict=True)) | changes
            cells = [row[f] for f in ci.CANONICAL_FIELDS]
        out.append(",".join(cells))
    return "\n".join(out).encode()


ROW_OF = {"D1": 1, "B1": 2, "S1": 3}


def outcome(p: ci.ImportPreview, record: str) -> tuple[RowStatus, Reason | None]:
    (row,) = [r for r in p.rows if r.record_id == record]
    return row.status, row.reason


# ---- Fixture: NUM01 through the importer, idempotent re-import, conflicts


def test_fixture_imports_and_reproduces_num01() -> None:
    p, journal, _ = run(FIXTURE)
    assert p.state == "validated" and p.count(RowStatus.ACCEPTED) == 3 == len(p.rows)
    assert journal.events() == ()  # a preview is not a mutation
    result = commit(p, journal, T0, "tenant-1")
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
    assert len(p.file_hash) == 64 and p.mapping_version_id == "canonical-identity-1"


def test_reimport_is_noop_and_changed_row_conflicts() -> None:
    journal, log = world()[0], ObservationLog()
    commit(run(FIXTURE, journal)[0], journal, T0, "tenant-1", log)
    before = (journal.events(), journal.revision(ACCT))
    again = run(FIXTURE, journal)[0]
    assert again.count(RowStatus.DUPLICATE) == 3 and again.state == "validated"
    result = commit(again, journal, T0 + timedelta(days=1), "tenant-1", log)
    assert (result.outcome, result.posted) == ("committed", 0)
    assert (journal.events(), journal.revision(ACCT)) == before
    changed = run(edit("D1", gross_cash="2000"), journal)[0]
    assert outcome(changed, "D1") == (RowStatus.CONFLICT, None)
    assert outcome(changed, "B1") == (RowStatus.DUPLICATE, None)
    assert changed.state == "needs_resolution"
    refused = commit(changed, journal, T0 + timedelta(days=2), "tenant-1")
    assert refused.outcome == "rejected"
    assert journal.events() == before[0]
    assert len(log.conflicts) == 0  # the conflicting row never reached the log


def test_duplicate_written_differently_adds_no_observation_conflict() -> None:
    journal, log = world()[0], ObservationLog()
    commit(run(FIXTURE, journal)[0], journal, T0, "tenant-1", log)
    again = run(edit("B1", price="50.0", quantity="10.00"), journal)[0]
    assert again.count(RowStatus.DUPLICATE) == 3
    assert commit(again, journal, T0, "tenant-1", log).outcome == "committed"
    assert log.conflicts == [] and len(journal.events()) == 3


def test_commit_is_bound_to_tenant_and_journal_and_never_raises() -> None:
    p, journal, _ = run(FIXTURE)
    other = world()[0]
    for target, tenant, reason in [
        (other, "tenant-1", "journal_mismatch"),
        (journal.copy(), "tenant-1", "journal_mismatch"),
        (journal, "tenant-2", "tenant_mismatch"),
    ]:
        refused = commit(p, target, T0, tenant)
        assert (refused.outcome, refused.reason) == ("rejected", reason)
    h = Header("acct-other", T0, SourceRef("SYN-MANUAL", "X1"))
    journal.post(deposit(h, Money.of(5, "USD")), T0)  # another account: no conflict
    early = commit(p, journal, T0 - timedelta(days=1), "tenant-1")
    assert (early.outcome, early.reason) == ("rejected", "recorded_at_order")
    assert other.events() == () and len(journal.events()) == 1
    assert commit(p, journal, T0, "tenant-1").posted == 3


def test_commit_against_stale_revision_conflicts() -> None:
    p, journal, _ = run(FIXTURE)
    h = Header(ACCT, T0, SourceRef("SYN-MANUAL", "X1"))
    journal.post(deposit(h, Money.of(5, "USD")), T0)
    result = commit(p, journal, T0, "tenant-1")
    assert result.outcome == "conflict" and result.posted == 0
    assert len(journal.events()) == 1


def test_bom_and_crlf_files_match_plain() -> None:
    plain = materialize(_committed(FIXTURE).events())
    for data in (b"\xef\xbb\xbf" + FIXTURE, FIXTURE.replace(b"\n", b"\r\n")):
        assert materialize(_committed(data).events()) == plain


def test_back_dated_rows_update_later_holdings() -> None:
    journal = _committed(FIXTURE)  # 6 units left after S1 on Jan 6
    rows = [
        "SYN-ACCOUNT,SYN-BROKER,B0,2026-01-03T15:00:00Z,buy,SYNTH,5,10,-50,0,USD",
        "SYN-ACCOUNT,SYN-BROKER,S2,2026-01-07T15:00:00Z,sell,SYNTH,11,10,110,0,USD",
    ]
    p = run("\n".join([HEADER, *rows]).encode(), journal)[0]
    assert p.state == "validated", p.rows
    assert commit(p, journal, T0, "tenant-1").posted == 2
    assert materialize(journal.events()).holding(ACCT, SYN).quantity == Quantity(0)


def _committed(data: bytes) -> Journal:
    p, journal, _ = run(data)
    assert p.state == "validated", p.rows
    assert commit(p, journal, T0, "tenant-1").outcome == "committed"
    return journal


# ---- Row quarantine: one adversarial file per reason


@pytest.mark.parametrize(
    ("record", "changes", "reason"),
    [
        ("B1", {"quantity": "1e1"}, Reason.MALFORMED_DECIMAL),
        ("B1", {"price": "50.0"}, None),  # control: trailing zero is fine
        ("B1", {"fee": "-1"}, Reason.FIELD_INVALID),
        ("B1", {"price": "0", "gross_cash": "0"}, Reason.FIELD_INVALID),
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
    assert commit(p, journal, T0, "tenant-1").outcome == "rejected"
    assert journal.events() == ()


def test_sell_without_prior_cost_is_quarantined() -> None:
    data = "\n".join(line for line in fixture_lines() if ",B1," not in line).encode()
    p, _, _ = run(data)
    assert outcome(p, "S1") == (RowStatus.QUARANTINED, Reason.UNKNOWN_COST)
    assert outcome(p, "D1") == (RowStatus.ACCEPTED, None)


def test_mixed_currency_fee_column() -> None:
    mapping = ci.ColumnMapping(
        "with-fee-ccy-1", {f: f for f in (*ci.CANONICAL_FIELDS, "fee_currency")}
    )
    request = ci.ImportRequest("tenant-1", ci.FileKind.TRANSACTIONS, mapping)
    lines = fixture_lines()
    rows = [lines[0] + ",fee_currency", lines[1] + ",", lines[2] + ",CAD"]
    p = run("\n".join([*rows, lines[3] + ",USD"]).encode(), request=request)[0]
    assert outcome(p, "B1") == (RowStatus.QUARANTINED, Reason.MIXED_CURRENCY)
    assert outcome(p, "D1") == (RowStatus.ACCEPTED, None)
    assert outcome(p, "S1") == (RowStatus.QUARANTINED, Reason.UNKNOWN_COST)


def test_sale_of_a_holding_with_mixed_cost_currencies_is_quarantined() -> None:
    cad = "SYN-ACCOUNT,SYN-BROKER,B2,2026-01-05T16:00:00Z,buy,SYNTH,1,5,-5,0,CAD"
    p = run("\n".join([*fixture_lines(), cad]).encode())[0]
    assert outcome(p, "B2") == (RowStatus.ACCEPTED, None)
    assert outcome(p, "S1") == (RowStatus.QUARANTINED, Reason.MIXED_CURRENCY)


def test_injection_cell_is_never_echoed_raw() -> None:
    row = run(edit("B1", symbol="=cmd|' /C calc'!A0"))[0].rows[1]
    assert row.reason is Reason.FORMULA_INJECTION and "=" not in row.message
    assert ci.spreadsheet_safe("=1+2") == "'=1+2" and ci.spreadsheet_safe("-5") == "'-5"
    assert (
        ci.spreadsheet_safe("\t=x") == "'\t=x"
        and ci.spreadsheet_safe("SYNTH") == "SYNTH"
    )


def test_unknown_columns_are_kept_raw_and_never_screened() -> None:
    lines = fixture_lines()
    rows = [lines[0] + ",note", *(x + ",-5" for x in lines[1:])]
    p = run("\n".join(rows).encode())[0]
    assert p.state == "validated"  # an unmapped negative number quarantines nothing
    assert [i.code for i in p.issues] == ["unknown_columns"]
    assert all(r.unknown_fields == {"note": "-5"} for r in p.rows)
    bad = edit("B1", record_id="@" + "X" * 100).decode().splitlines()
    bad = [bad[0] + ",note", *(x + ",=HYPERLINK(1)" for x in bad[1:])]
    row = run("\n".join(bad).encode())[0].rows[1]
    assert row.reason is Reason.FORMULA_INJECTION
    assert row.record_id == "'@" + "X" * 63  # truncated and neutralized
    assert row.unknown_fields == {"note": "'=HYPERLINK(1)"}


# ---- File rejection and limits


@pytest.mark.parametrize(
    ("data", "limits", "code"),
    [
        (FIXTURE, ci.Limits(max_bytes=100), "file_too_large"),
        (FIXTURE, ci.Limits(max_rows=2), "too_many_rows"),
        (FIXTURE, ci.Limits(max_columns=10), "too_many_columns"),
        (FIXTURE.replace(b"B1", b"B\x001"), ci.Limits(), "nul_byte"),
        (FIXTURE.replace(b"SYNTH", b"SYN\xff"), ci.Limits(), "encoding"),
        (FIXTURE.replace(b"kind", b"symbol"), ci.Limits(), "duplicate_header"),
        (FIXTURE.replace(b",fee,", b",fees,"), ci.Limits(), "missing_column"),
        (b"", ci.Limits(), "empty_file"),
        (FIXTURE.replace(b"D1", b'"D"1'), ci.Limits(), "csv_malformed"),
        (FIXTURE.replace(b"B1,", b'"B\n1",'), ci.Limits(), None),
        (HEADER.replace("fee", "f" * 300).encode(), ci.Limits(), "field_too_long"),
    ],
)
def test_file_rejected(data: bytes, limits: ci.Limits, code: str | None) -> None:
    request = ci.ImportRequest(TX.tenant_id, TX.file_kind, TX.mapping, limits=limits)
    p, journal, _ = run(data, request=request)
    if code is None:  # control: a quoted newline is a valid cell, not a file error
        assert p.state == "needs_resolution" and len(p.rows) == 3
        return
    assert p.state == "rejected" and p.rows == () and p.issues[0].code == code
    assert commit(p, journal, T0, "tenant-1").outcome == "rejected"


def test_format_version_is_explicit() -> None:
    request = ci.ImportRequest(
        "tenant-1", ci.FileKind.TRANSACTIONS, TX.mapping, "csv/9"
    )
    assert run(FIXTURE, request=request)[0].issues[0].code == "format_version"


# ---- Column mapping


def test_user_mapping_from_source_headers() -> None:
    renamed = {f: f"Col {f.title()}" for f in ci.CANONICAL_FIELDS}
    header = ",".join(renamed[f] for f in ci.CANONICAL_FIELDS)
    data = "\n".join([header, *fixture_lines()[1:]]).encode()
    mapping = ci.ColumnMapping("broker-x-2", {v: k for k, v in renamed.items()})
    request = ci.ImportRequest("tenant-1", ci.FileKind.TRANSACTIONS, mapping)
    p = run(data, request=request)[0]
    assert p.state == "validated" and p.mapping_version_id == "broker-x-2"
    assert p.file_hash != run(FIXTURE)[0].file_hash


@pytest.mark.parametrize(
    ("version", "columns", "code"),
    [
        ("v1", {"a": "kind", "b": "kind"}, "mapping_duplicate"),
        ("v1", {"a": "colour"}, "mapping_unknown_field"),
        ("v1", {"kind": "kind"}, "mapping_missing"),
        ("bad version", {f: f for f in ci.CANONICAL_FIELDS}, "mapping_version"),
    ],
)
def test_mapping_validation(version: str, columns: dict[str, str], code: str) -> None:
    with pytest.raises(ci.MappingError) as err:
        ci.ColumnMapping(version, columns)
    assert err.value.code == code


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
