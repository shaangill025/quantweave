"""Canonical CSV holdings-snapshot files (T014; spec §4 "Canonical boundaries").

A snapshot file uses the canonical columns of `qw_domain.csv_import` with kinds
`position` (symbol, quantity) and `cash` (gross_cash is the balance). Rows group into
one `SourceSnapshot` per (source, account reference, effective time) for
`sources.consolidate`. Snapshots are observations, never journal events, so there is
nothing to commit to a journal. A transaction row, a malformed row, a repeated item
within one snapshot, or a repeated record id with other content blocks the file:
`snapshots` is empty unless every row is accepted or a duplicate.
Stdlib only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from qw_domain.csv_import import (
    Context,
    FileKind,
    ImportPreview,
    ImportRequest,
    Reason,
    RowError,
    RowOutcome,
    RowStatus,
    build_preview,
    parse_file,
    require_empty,
    require_zero_fee,
    resolve_instrument,
    row_outcome,
    text,
    wire_decimal,
)
from qw_domain.decimals import Money, MoneyAmount, Quantity
from qw_domain.identity import InstrumentId, SecurityMaster
from qw_domain.postings import Header, SourceObservation
from qw_domain.sources import HoldingLine, SourceMap, SourceSnapshot

type Line = HoldingLine | Money
type _Group = tuple[str, str, datetime]  # (source, account reference, as of)


@dataclass(frozen=True, slots=True)
class SnapshotPreview:
    preview: ImportPreview
    snapshots: tuple[SourceSnapshot, ...]


def _line(ctx: Context, h: Header, row: Mapping[str, str]) -> Line:
    kind, cur = text(row, "kind"), text(row, "currency")
    require_empty(row, kind, "price")
    require_zero_fee(row, kind)
    if kind == "cash":
        require_empty(row, kind, "symbol", "quantity")
        return Money(wire_decimal(MoneyAmount, row, "gross_cash"), cur)
    require_empty(row, kind, "gross_cash")
    iid = resolve_instrument(ctx, row, h.effective_at)
    return HoldingLine(iid, cur, wire_decimal(Quantity, row, "quantity"))


def _item(line: Line) -> tuple[InstrumentId | None, str]:
    if isinstance(line, Money):
        return (None, line.currency)
    return (line.instrument_id, line.currency)


def preview_snapshot(
    data: bytes,
    request: ImportRequest,
    source_map: SourceMap,
    master: SecurityMaster,
    at: datetime,
    complete: bool = False,
) -> SnapshotPreview:
    """`complete`: the file lists whole accounts, so an omitted item reports 0."""
    if request.file_kind is not FileKind.SNAPSHOT:
        raise ValueError("transaction files are previewed by qw_domain.csv_import")
    ctx = Context(request, source_map, master, at)
    parsed = parse_file(data, ctx)
    seen: dict[tuple[str, str], str] = {}
    groups: dict[_Group, list[Line]] = {}
    rows: list[RowOutcome] = []
    for p in parsed.ready():
        assert p.header is not None
        h = p.header
        obs = SourceObservation(h.account_id, h.source, at, h.effective_at, p.row)
        ref = (h.source.source_id, h.source.record_id)
        if ref in seen:
            same = seen[ref] == obs.fingerprint
            status = RowStatus.DUPLICATE if same else RowStatus.CONFLICT
            rows.append(row_outcome(p, status, obs=obs))
            continue
        try:
            line = _line(ctx, h, p.row)
            key = (h.source.source_id, p.row["account_ref"], h.effective_at)
            group = groups.setdefault(key, [])
            if any(_item(x) == _item(line) for x in group):
                raise RowError(Reason.DUPLICATE_LINE, "item already in this snapshot")
        except RowError as err:
            rows.append(row_outcome(p, RowStatus.QUARANTINED, err))
            continue
        seen[ref] = obs.fingerprint
        group.append(line)
        rows.append(row_outcome(p, RowStatus.ACCEPTED, obs=obs, line=line))
    result = build_preview(ctx, parsed, rows, None, {})
    if result.state != "validated":
        return SnapshotPreview(result, ())
    snapshots = tuple(
        SourceSnapshot(
            src,
            ref,
            as_of,
            tuple(x for x in g if isinstance(x, HoldingLine)),
            tuple(x for x in g if isinstance(x, Money)),
            complete,
        )
        for (src, ref, as_of), g in sorted(groups.items(), key=lambda kv: kv[0][2])
    )
    return SnapshotPreview(result, snapshots)
