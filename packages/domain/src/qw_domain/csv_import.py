"""Canonical CSV import: strict parsing, column mapping, preview and commit (T014).

Spec §4 "Import workflow" and "Idempotency and conflict resolution", spec §12
"Untrusted input controls", T008 review F-25 and C-04:
- A file is size, NUL, encoding, row, column and field-length limited, then parsed by
  the stdlib `csv` module in strict mode. File-level failures reject the whole file.
- A versioned `ColumnMapping` maps source headers to canonical fields. Headers it does
  not name are kept per row as `unknown_fields` and never interpreted.
- A file is declared either a transaction journal or a holdings snapshot; a row of
  the other kind is quarantined, so the two never mix in one import.
- Each row is accepted (with its proposed event or snapshot line), a duplicate of a
  journal record, a conflict (same source record, other content), or quarantined
  with a typed `Reason`. Amounts go through the decimal wire classes and timestamps
  through `parse_instant`; nothing is rounded or guessed. Accounts resolve through
  the `SourceMap` and symbols through the `SecurityMaster`.
- Rows are evaluated in (effective time, account, source, record) order on a copy of
  the journal, so the preview never mutates it and row order does not matter.
- Cells starting with = + - @ TAB or CR in text fields are quarantined
  (`formula_injection`); numeric fields must match the decimal grammar instead.
  Messages never echo a cell raw; `spreadsheet_safe` is the export-side neutralizer.
- `commit` is all or nothing against the account revisions captured by the preview.
Stdlib only.
"""

import csv
import hashlib
import io
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from qw_domain.corporate_actions import CorporateActionError, SourceRef
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    BoundedDecimal,
    DecimalValueError,
    Money,
    MoneyAmount,
    PositiveQuantity,
    Price,
    Quantity,
    safe_repr,
)
from qw_domain.identity import IdentityError, InstrumentId, Mic, SecurityMaster, Ticker
from qw_domain.instants import InstantError, parse_instant
from qw_domain.journal import Journal, ObservationLog, Outcome
from qw_domain.postings import (
    Header,
    JournalError,
    JournalEvent,
    SourceObservation,
    buy,
    deposit,
    dividend,
    fee,
    interest,
    sell,
    withdrawal,
)
from qw_domain.sources import (
    ID_PATTERN,
    HoldingLine,
    SourceMap,
    SourceSnapshot,
)

FORMAT_VERSION = "canonical-csv/1"
CANONICAL_FIELDS = (
    "account_ref",
    "source_ref",
    "record_id",
    "effective_at",
    "kind",
    "symbol",
    "quantity",
    "price",
    "gross_cash",
    "fee",
    "currency",
)
OPTIONAL_FIELDS = ("fee_currency",)
NUMERIC_FIELDS = frozenset({"quantity", "price", "gross_cash", "fee"})
INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_UTF8_BOM = b"\xef\xbb\xbf"
_PLAIN = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", re.ASCII)
TRANSACTION_KINDS = frozenset(
    {"deposit", "withdrawal", "buy", "sell", "fee", "interest", "dividend"}
)
SNAPSHOT_KINDS = frozenset({"position", "cash"})


class FileKind(StrEnum):
    TRANSACTIONS = "transactions"
    SNAPSHOT = "snapshot"


class RowStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    QUARANTINED = "quarantined"


class Reason(StrEnum):
    """Row quarantine reason codes."""

    COLUMN_COUNT = "column_count"
    FIELD_TOO_LONG = "field_too_long"
    FORMULA_INJECTION = "formula_injection"
    INVALID_REFERENCE = "invalid_reference"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    MALFORMED_DECIMAL = "malformed_decimal"
    SCALE_OVERFLOW = "scale_overflow"
    UNKNOWN_KIND = "unknown_kind"
    MIXED_FILE_KIND = "mixed_file_kind"
    CURRENCY_INVALID = "currency_invalid"
    MIXED_CURRENCY = "mixed_currency"
    FIELD_INVALID = "field_invalid"
    AMOUNT_MISMATCH = "amount_mismatch"
    UNMAPPED_ACCOUNT = "unmapped_account"
    UNRESOLVED_SYMBOL = "unresolved_symbol"
    UNKNOWN_COST = "unknown_cost"
    INSUFFICIENT_UNITS = "insufficient_units"
    DUPLICATE_LINE = "duplicate_line"


type State = Literal["validated", "needs_resolution", "rejected"]


class MappingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def spreadsheet_safe(cell: str) -> str:
    """Neutralize a cell for spreadsheet-targeted export (spec §12); raw values in
    the journal and observations are never altered."""
    return f"'{cell}" if cell.startswith(INJECTION_PREFIXES) else cell


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """Versioned map from source header to canonical field. Every canonical field
    must be mapped exactly once; optional fields at most once."""

    version_id: str
    columns: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.version_id, str) or not ID_PATTERN.fullmatch(
            self.version_id
        ):
            raise MappingError("mapping_version", safe_repr(self.version_id))
        cols = dict(self.columns)
        targets = list(cols.values())
        unknown = set(targets) - {*CANONICAL_FIELDS, *OPTIONAL_FIELDS}
        if unknown:
            raise MappingError("mapping_unknown_field", safe_repr(sorted(unknown)))
        if len(set(targets)) != len(targets):
            raise MappingError("mapping_duplicate", "a field is mapped twice")
        missing = [f for f in CANONICAL_FIELDS if f not in targets]
        if missing:
            raise MappingError("mapping_missing", safe_repr(missing))
        object.__setattr__(self, "columns", MappingProxyType(cols))

    @classmethod
    def identity(cls) -> "ColumnMapping":
        return cls("canonical-identity-1", {f: f for f in CANONICAL_FIELDS})


@dataclass(frozen=True, slots=True)
class Limits:
    max_bytes: int = 5 * 1024 * 1024
    max_rows: int = 50_000
    max_columns: int = 64
    max_field: int = 256


@dataclass(frozen=True, slots=True)
class ImportRequest:
    tenant_id: str
    file_kind: FileKind
    mapping: ColumnMapping
    format_version: str = FORMAT_VERSION
    limits: Limits = Limits()
    snapshot_complete: bool = False  # a complete snapshot reports omissions as 0
    mic: Mic | None = None  # restrict symbol resolution to one exchange


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class RowOutcome:
    row_number: int  # 1 = first data record after the header
    status: RowStatus
    record_id: str
    reason: Reason | None = None
    message: str = ""
    event: JournalEvent | None = None
    observation: SourceObservation | None = None
    line: HoldingLine | Money | None = None
    unknown_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImportPreview:
    import_id: str
    file_hash: str  # SHA-256 of the uploaded bytes
    file_kind: FileKind
    format_version: str
    mapping_version_id: str
    issues: tuple[Issue, ...]
    rows: tuple[RowOutcome, ...]
    expected_revisions: Mapping[str, int]
    snapshots: tuple[SourceSnapshot, ...] = ()

    def _count(self, status: RowStatus) -> int:
        return sum(r.status is status for r in self.rows)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def accepted_rows(self) -> int:
        return self._count(RowStatus.ACCEPTED)

    @property
    def duplicate_rows(self) -> int:
        return self._count(RowStatus.DUPLICATE)

    @property
    def rejected_rows(self) -> int:
        return self._count(RowStatus.QUARANTINED) + self._count(RowStatus.CONFLICT)

    @property
    def state(self) -> State:
        if any(i.blocking for i in self.issues):
            return "rejected"
        return "needs_resolution" if self.rejected_rows else "validated"

    def _reasons(self) -> list[tuple[str, str, bool]]:
        found = [(i.code, i.message, i.blocking) for i in self.issues]
        for r in self.rows:
            if r.status is RowStatus.CONFLICT:
                found.append(("conflict", f"row {r.row_number}: record differs", True))
            elif r.reason is not None:
                found.append((r.reason, f"row {r.row_number}: {r.message}", True))
        return found

    def to_wire(self) -> dict[str, object]:
        """`import_preview.schema.json` fields (state, counts and issues)."""
        accounts = sorted(self.expected_revisions)
        return {
            "id": self.import_id,
            "state": self.state,
            "file_hash": self.file_hash,
            "source_kind": "canonical",
            # accepted rows always resolve to an existing mapped account
            "classification": "existing_account_view",
            "target_account_id": accounts[0] if len(accounts) == 1 else None,
            "mapping_version_id": self.mapping_version_id,
            "row_count": self.row_count,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "duplicate_rows": self.duplicate_rows,
            "issues": [
                {"code": c, "message": m, "affected_ids": [], "blocking": b}
                for c, m, b in self._reasons()
            ],
            "commit_token": None,
        }


class _RowError(Exception):
    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason, self.message = reason, message


# ---- File level: limits, encoding, strict CSV, header


def _read(data: bytes, req: ImportRequest) -> tuple[list[str], list[list[str]]]:
    lim = req.limits
    if req.format_version != FORMAT_VERSION:
        raise MappingError("format_version", safe_repr(req.format_version))
    if len(data) > lim.max_bytes:
        raise MappingError("file_too_large", f"{len(data)} > {lim.max_bytes} bytes")
    if b"\x00" in data:
        raise MappingError("nul_byte", "NUL bytes are not allowed")
    try:
        text = data.removeprefix(_UTF8_BOM).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise MappingError("encoding", f"not UTF-8 at byte {exc.start}") from None
    reader = csv.reader(io.StringIO(text, newline=""), strict=True, doublequote=True)
    records: list[list[str]] = []
    try:
        for rec in reader:
            if not rec:
                continue  # a blank line is not a record
            if len(rec) > lim.max_columns:
                raise MappingError("too_many_columns", f"> {lim.max_columns}")
            if len(records) > lim.max_rows:
                raise MappingError("too_many_rows", f"> {lim.max_rows} data rows")
            records.append(rec)
    except csv.Error as exc:
        raise MappingError(
            "csv_malformed", f"record {reader.line_num}: {exc}"
        ) from None
    if not records:
        raise MappingError("empty_file", "no header")
    header = records[0]
    if any(len(h) > lim.max_field for h in header):
        raise MappingError("field_too_long", "header cell too long")
    if len(set(header)) != len(header):
        raise MappingError("duplicate_header", "a header appears twice")
    missing = [h for h in req.mapping.columns if h not in header]
    if missing:
        raise MappingError("missing_column", safe_repr(missing))
    return header, records[1:]


# ---- Row level


def _text(row: Mapping[str, str], name: str) -> str:
    return row.get(name, "")


def _dec[T: BoundedDecimal](cls: type[T], row: Mapping[str, str], name: str) -> T:
    """Wire-grammar decimal. Plain digits outside the class bounds are a scale or
    range overflow; anything else (exponents, signs, separators) is malformed."""
    text = row.get(name, "")
    try:
        return cls(text)
    except DecimalValueError as exc:
        code = exc.code
    if _PLAIN.fullmatch(text):
        try:
            cls(Decimal(text))
        except DecimalValueError as exc:
            code = exc.code
    scale = code in ("decimal_scale_exceeded", "decimal_out_of_range")
    reason = Reason.SCALE_OVERFLOW if scale else Reason.MALFORMED_DECIMAL
    raise _RowError(reason, f"{name}: {code}")


def _empty(row: Mapping[str, str], kind: str, *names: str) -> None:
    for name in names:
        if row.get(name, ""):
            raise _RowError(Reason.FIELD_INVALID, f"{name} not expected for {kind}")


def _fee_zero(row: Mapping[str, str], kind: str) -> None:
    if row.get("fee", "") and _dec(MoneyAmount, row, "fee").value:
        raise _RowError(Reason.FIELD_INVALID, f"fee not expected for {kind}")


def _screen(row: Mapping[str, str], unknown: Mapping[str, str], lim: Limits) -> None:
    """Length and formula checks. Mapped numeric fields are exempt from the formula
    check (a leading '-' is a sign) because the decimal grammar validates them."""
    cells = [(n, v, n in NUMERIC_FIELDS) for n, v in row.items()]
    for name, cell, numeric in [*cells, *((n, v, False) for n, v in unknown.items())]:
        if len(cell) > lim.max_field:
            raise _RowError(Reason.FIELD_TOO_LONG, f"{safe_repr(name)} too long")
        if not numeric and cell.startswith(INJECTION_PREFIXES):
            raise _RowError(
                Reason.FORMULA_INJECTION, f"{safe_repr(name)} looks like a formula"
            )


@dataclass(slots=True)
class _Ctx:
    req: ImportRequest
    source_map: SourceMap
    master: SecurityMaster
    scratch: Journal
    at: datetime
    seen: dict[tuple[str, str, str], str]


@dataclass(slots=True)
class _Parsed:
    number: int
    row: dict[str, str]
    unknown: dict[str, str]
    header: Header | None = None
    error: _RowError | None = None


def _resolve_header(ctx: _Ctx, row: Mapping[str, str]) -> Header:
    try:
        ref = SourceRef(_text(row, "source_ref"), _text(row, "record_id"))
    except CorporateActionError:
        raise _RowError(Reason.INVALID_REFERENCE, "source_ref or record_id") from None
    try:
        at = parse_instant(_text(row, "effective_at"))
    except InstantError:
        raise _RowError(Reason.MALFORMED_TIMESTAMP, "effective_at") from None
    kind = _text(row, "kind")
    if kind not in TRANSACTION_KINDS | SNAPSHOT_KINDS:
        raise _RowError(Reason.UNKNOWN_KIND, f"kind {safe_repr(kind)}")
    wanted = (
        TRANSACTION_KINDS
        if ctx.req.file_kind is FileKind.TRANSACTIONS
        else SNAPSHOT_KINDS
    )
    if kind not in wanted:
        raise _RowError(Reason.MIXED_FILE_KIND, f"{kind} in a {ctx.req.file_kind} file")
    source = ctx.source_map.source(ref.source_id)
    acct = None
    if source is not None and source.tenant_id == ctx.req.tenant_id:
        acct = ctx.source_map.resolve(ref.source_id, _text(row, "account_ref"), at)
    if acct is None:  # unknown, foreign-tenant and unmapped look the same
        raise _RowError(Reason.UNMAPPED_ACCOUNT, "no account mapping in force")
    cur = _text(row, "currency")
    if len(cur) != 3 or not cur.isascii() or not cur.isupper():
        raise _RowError(Reason.CURRENCY_INVALID, "currency is not ISO 4217-shaped")
    fee_cur = _text(row, "fee_currency")
    if fee_cur and fee_cur != cur:
        raise _RowError(Reason.MIXED_CURRENCY, "fee currency differs from currency")
    return Header(acct.account_id, at, ref)


def _instrument(ctx: _Ctx, row: Mapping[str, str], at: datetime) -> InstrumentId:
    symbol = _text(row, "symbol")
    if not symbol:
        raise _RowError(Reason.FIELD_INVALID, "symbol required")
    try:
        found = ctx.master.resolve(Ticker(symbol), ctx.req.mic, at.date())
    except IdentityError:
        found = None
    if found is None or found.instrument_id is None:
        status = "malformed" if found is None else found.status
        raise _RowError(Reason.UNRESOLVED_SYMBOL, f"symbol {status}; resolve first")
    return found.instrument_id


def _signed(row: Mapping[str, str], kind: str, sign: int) -> Money:
    gross = _dec(MoneyAmount, row, "gross_cash")
    if gross.value * sign <= 0:
        raise _RowError(Reason.FIELD_INVALID, f"gross_cash sign wrong for {kind}")
    return Money(gross if sign > 0 else -gross, _text(row, "currency"))


def _transaction(ctx: _Ctx, h: Header, row: Mapping[str, str]) -> JournalEvent:
    kind, cur = _text(row, "kind"), _text(row, "currency")
    cash: dict[str, Callable[[Header, Money], JournalEvent]] = {
        "deposit": deposit,
        "withdrawal": withdrawal,
        "fee": fee,
        "interest": interest,
    }
    if kind in cash or kind == "dividend":
        _empty(
            row, kind, "quantity", "price", *(() if kind == "dividend" else ("symbol",))
        )
        _fee_zero(row, kind)
        amount = _signed(
            row, kind, 1 if kind in ("deposit", "interest", "dividend") else -1
        )
        if kind == "dividend":
            return dividend(h, _instrument(ctx, row, h.effective_at), amount)
        return cash[kind](h, amount)
    iid = _instrument(ctx, row, h.effective_at)
    qty, price = _dec(PositiveQuantity, row, "quantity"), _dec(Price, row, "price")
    trade_fee = Money(_dec(MoneyAmount, row, "fee"), cur)
    if trade_fee.amount.value < 0:
        raise _RowError(Reason.FIELD_INVALID, "fee must not be negative")
    gross = _dec(MoneyAmount, row, "gross_cash")
    try:
        if kind == "buy":
            event = buy(h, iid, qty, price, trade_fee)
        else:
            held = ctx.scratch.holding(h, iid)
            if held.cost is None:
                raise _RowError(Reason.UNKNOWN_COST, "no prior cost in the journal")
            if held.quantity.value < qty.value:
                raise _RowError(Reason.INSUFFICIENT_UNITS, "sale exceeds held units")
            event = sell(h, qty, price, trade_fee, held)
    except DecimalValueError as exc:
        raise _RowError(
            Reason.SCALE_OVERFLOW, f"quantity x price: {exc.code}"
        ) from None
    except JournalError as exc:
        mixed = exc.code == "currency_mismatch"
        reason = Reason.MIXED_CURRENCY if mixed else Reason.FIELD_INVALID
        raise _RowError(reason, exc.code) from None
    with localcontext(DOMAIN_CONTEXT):
        value = qty.value * price.value
    if gross.value != (-value if kind == "buy" else value):
        raise _RowError(Reason.AMOUNT_MISMATCH, "gross_cash != quantity x price")
    return event


def _snapshot_line(ctx: _Ctx, h: Header, row: Mapping[str, str]) -> HoldingLine | Money:
    kind, cur = _text(row, "kind"), _text(row, "currency")
    _empty(row, kind, "price")
    _fee_zero(row, kind)
    if kind == "cash":
        _empty(row, kind, "symbol", "quantity")
        return Money(_dec(MoneyAmount, row, "gross_cash"), cur)
    _empty(row, kind, "gross_cash")
    iid = _instrument(ctx, row, h.effective_at)
    return HoldingLine(iid, cur, _dec(Quantity, row, "quantity"))


def _parse(
    number: int, rec: list[str], header: list[str], req: ImportRequest
) -> _Parsed:
    if len(rec) != len(header):
        parsed = _Parsed(number, {}, {})
        parsed.error = _RowError(
            Reason.COLUMN_COUNT, f"{len(rec)} != {len(header)} cells"
        )
        return parsed
    raw = dict(zip(header, rec, strict=True))
    cols = req.mapping.columns
    row = {cols[k]: v for k, v in raw.items() if k in cols}
    parsed = _Parsed(number, row, {k: v for k, v in raw.items() if k not in cols})
    try:
        _screen(parsed.row, parsed.unknown, req.limits)
    except _RowError as err:
        parsed.error = err
    return parsed


def _row(
    p: _Parsed,
    status: RowStatus,
    err: _RowError | None = None,
    event: JournalEvent | None = None,
    obs: SourceObservation | None = None,
    line: HoldingLine | Money | None = None,
) -> RowOutcome:
    reason, message = (None, "") if err is None else (err.reason, err.message)
    rid = p.row.get("record_id", "")
    return RowOutcome(
        p.number, status, rid, reason, message, event, obs, line, p.unknown
    )


def _item(line: HoldingLine | Money) -> tuple[InstrumentId | None, str]:
    if isinstance(line, Money):
        return (None, line.currency)
    return (line.instrument_id, line.currency)


type _Group = tuple[str, str, datetime]  # (source, account reference, as of)


def _evaluate(
    ctx: _Ctx, p: _Parsed, h: Header, groups: dict[_Group, list[HoldingLine | Money]]
) -> RowOutcome:
    obs = SourceObservation(h.account_id, h.source, ctx.at, h.effective_at, p.row)
    if ctx.req.file_kind is FileKind.TRANSACTIONS:
        event = _transaction(ctx, h, p.row)
        status = _STATUS[ctx.scratch.post(event, ctx.at)]
        return _row(p, status, event=event, obs=obs)
    line = _snapshot_line(ctx, h, p.row)
    ref = ("snapshot", h.source.source_id, h.source.record_id)
    prior = ctx.seen.get(ref)
    if prior is not None:
        same = prior == obs.fingerprint
        return _row(p, RowStatus.DUPLICATE if same else RowStatus.CONFLICT, obs=obs)
    ctx.seen[ref] = obs.fingerprint
    group = groups.setdefault(
        (h.source.source_id, p.row["account_ref"], h.effective_at), []
    )
    if any(_item(x) == _item(line) for x in group):
        raise _RowError(Reason.DUPLICATE_LINE, "item already in this snapshot")
    group.append(line)
    return _row(p, RowStatus.ACCEPTED, obs=obs, line=line)


_STATUS = {
    Outcome.POSTED: RowStatus.ACCEPTED,
    Outcome.DUPLICATE: RowStatus.DUPLICATE,
    Outcome.CONFLICT: RowStatus.CONFLICT,
}


def _order(p: _Parsed) -> tuple[datetime, str, str, str]:
    assert p.header is not None
    h = p.header
    return (h.effective_at, h.account_id, h.source.source_id, h.source.record_id)


def preview(
    data: bytes,
    request: ImportRequest,
    journal: Journal,
    source_map: SourceMap,
    master: SecurityMaster,
    at: datetime,
) -> ImportPreview:
    """Evaluate a file against `journal` without mutating it."""
    file_hash = hashlib.sha256(data).hexdigest()
    issues: list[Issue] = []
    rows: list[RowOutcome] = []
    revisions: dict[str, int] = {}
    snapshots: tuple[SourceSnapshot, ...] = ()
    try:
        header, records = _read(data, request)
    except MappingError as err:
        issues.append(Issue(err.code, str(err), True))
        header, records = [], []
    unknown_cols = [h for h in header if h not in request.mapping.columns]
    if unknown_cols:
        issues.append(Issue("unknown_columns", safe_repr(unknown_cols, 256), False))
    last = journal.entries()[-1].recorded_at if journal.entries() else at
    ctx = _Ctx(request, source_map, master, journal.copy(), max(at, last), {})
    parsed = [_parse(n, rec, header, request) for n, rec in enumerate(records, 1)]
    for p in parsed:
        if p.error is None:
            try:
                p.header = _resolve_header(ctx, p.row)
            except _RowError as err:
                p.error = err
    groups: dict[_Group, list[HoldingLine | Money]] = {}
    ready = sorted((p for p in parsed if p.header), key=lambda p: (_order(p), p.number))
    for p in ready:
        assert p.header is not None
        revisions[p.header.account_id] = journal.revision(p.header.account_id)
        try:
            rows.append(_evaluate(ctx, p, p.header, groups))
        except _RowError as err:
            rows.append(_row(p, RowStatus.QUARANTINED, err))
    rows += [
        _row(p, RowStatus.QUARANTINED, p.error) for p in parsed if p.error is not None
    ]
    rows.sort(key=lambda r: r.row_number)
    ok = (RowStatus.ACCEPTED, RowStatus.DUPLICATE)
    clean = all(r.status in ok for r in rows)
    if request.file_kind is FileKind.SNAPSHOT and clean:
        snapshots = tuple(
            SourceSnapshot(
                src,
                ref,
                as_of,
                tuple(x for x in g if isinstance(x, HoldingLine)),
                tuple(x for x in g if isinstance(x, Money)),
                request.snapshot_complete,
            )
            for (src, ref, as_of), g in sorted(groups.items(), key=lambda kv: kv[0][2])
        )
    return ImportPreview(
        f"imp:{file_hash[:32]}:{request.file_kind}",
        file_hash,
        request.file_kind,
        request.format_version,
        request.mapping.version_id,
        tuple(issues),
        tuple(rows),
        MappingProxyType(dict(sorted(revisions.items()))),
        snapshots,
    )


@dataclass(frozen=True, slots=True)
class CommitResult:
    outcome: Literal["committed", "conflict", "rejected"]
    posted: int = 0
    duplicates: int = 0
    reason: str = ""


def commit(
    preview_: ImportPreview,
    journal: Journal,
    recorded_at: datetime,
    log: ObservationLog | None = None,
) -> CommitResult:
    """Post every accepted row, or nothing. A changed account revision since the
    preview is a conflict: re-preview against the current journal."""
    if preview_.file_kind is not FileKind.TRANSACTIONS:
        return CommitResult("rejected", reason="snapshot files do not post events")
    if preview_.state != "validated":
        return CommitResult("rejected", reason=f"preview is {preview_.state}")
    stale = [
        a
        for a, rev in preview_.expected_revisions.items()
        if journal.revision(a) != rev
    ]
    if stale:
        return CommitResult("conflict", reason=f"revision changed: {stale}")
    events = [
        r.event for r in preview_.rows if r.status is RowStatus.ACCEPTED and r.event
    ]
    events.sort(
        key=lambda e: (
            e.effective_at,
            e.account_id,
            e.source.source_id,
            e.source.record_id,
        )
    )
    trial = journal.copy()
    if any(trial.post(e, recorded_at) is not Outcome.POSTED for e in events):
        return CommitResult("conflict", reason="journal no longer accepts the preview")
    for e in events:
        journal.post(e, recorded_at)
    if log is not None:
        for r in preview_.rows:
            if r.observation is not None and r.status is not RowStatus.CONFLICT:
                log.observe(r.observation)
    return CommitResult("committed", len(events), preview_.duplicate_rows)
