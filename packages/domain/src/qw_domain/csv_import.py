"""Canonical CSV import: strict parsing, column mapping, preview and commit (T014).

Spec §4 "Import workflow" and "Idempotency and conflict resolution", spec §12, T008
F-25 and C-04. Files are size, NUL, UTF-8, row, column and field limited, then parsed
by a strict stdlib `csv` reader; `csv.field_size_limit` (process-global) is left at
its default, so a longer cell rejects the file. A versioned `ColumnMapping` names the
canonical fields; other headers stay raw in `unknown_fields`, are never interpreted,
and must go through `spreadsheet_safe` on export. An unmapped `fee_currency` means the
fee is in the row's `currency`. Each transaction row is accepted, a duplicate, a
conflict, or quarantined with a `Reason`; nothing is rounded or guessed. Rows run in
(effective time, account, source, record) order on a journal copy, so a preview never
mutates the journal. Mapped text cells starting with = + - @ TAB or CR are quarantined;
messages never echo a cell. `commit` is all or nothing and bound to the tenant, the
journal identity and the account revisions; SourceMap and SecurityMaster changes
after the preview are NOT part of that binding. Snapshot files: `csv_snapshot`.
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

from qw_domain import postings as ev
from qw_domain.corporate_actions import CorporateActionError, SourceRef
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    BoundedDecimal,
    DecimalValueError,
    Money,
    MoneyAmount,
    PositiveQuantity,
    Price,
    safe_repr,
)
from qw_domain.identity import IdentityError, InstrumentId, Mic, SecurityMaster, Ticker
from qw_domain.instants import InstantError, parse_instant
from qw_domain.journal import Journal, ObservationLog, Outcome
from qw_domain.postings import Header, JournalError, JournalEvent, SourceObservation
from qw_domain.sources import ID_PATTERN, SourceMap

FORMAT_VERSION = "canonical-csv/1"
_FIELDS = "account_ref source_ref record_id effective_at kind symbol quantity price"
CANONICAL_FIELDS = (*_FIELDS.split(), "gross_cash", "fee", "currency")
OPTIONAL_FIELDS = ("fee_currency",)
NUMERIC_FIELDS = frozenset({"quantity", "price", "gross_cash", "fee"})
INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
RETAINED_CHARS = 64  # raw text kept on a quarantined row
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


def _retain(text: str) -> str:
    return spreadsheet_safe(text[:RETAINED_CHARS])


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
    """Defaults keep a worst-case (all sells, one account) preview to a few seconds;
    see the T014 receipt for measurements."""

    max_bytes: int = 2 * 1024 * 1024
    max_rows: int = 10_000
    max_columns: int = 64
    max_field: int = 256


@dataclass(frozen=True, slots=True)
class ImportRequest:
    tenant_id: str
    file_kind: FileKind
    mapping: ColumnMapping
    format_version: str = FORMAT_VERSION
    limits: Limits = Limits()
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
    record_id: str  # raw, or truncated and neutralized when quarantined
    reason: Reason | None = None
    message: str = ""
    event: JournalEvent | None = None
    observation: SourceObservation | None = None
    line: object = None  # a snapshot line (qw_domain.csv_snapshot)
    unknown_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImportPreview:
    import_id: str
    file_hash: str  # SHA-256 of the uploaded bytes
    file_kind: FileKind
    format_version: str
    mapping_version_id: str
    tenant_id: str
    journal_id: str | None  # the journal previewed against; None for snapshots
    issues: tuple[Issue, ...]
    rows: tuple[RowOutcome, ...]
    expected_revisions: Mapping[str, int]

    def count(self, *statuses: RowStatus) -> int:
        return sum(r.status in statuses for r in self.rows)

    @property
    def rejected_rows(self) -> int:
        return self.count(RowStatus.QUARANTINED, RowStatus.CONFLICT)

    @property
    def state(self) -> State:
        if any(i.blocking for i in self.issues):
            return "rejected"
        return "needs_resolution" if self.rejected_rows else "validated"


class RowError(Exception):
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


def text(row: Mapping[str, str], name: str) -> str:
    return row.get(name, "")


def wire_decimal[T: BoundedDecimal](
    cls: type[T], row: Mapping[str, str], name: str
) -> T:
    """Wire-grammar decimal. Plain digits outside the class bounds are a scale or
    range overflow; anything else (exponents, signs, separators) is malformed."""
    raw = row.get(name, "")
    try:
        return cls(raw)
    except DecimalValueError as exc:
        code = exc.code
    if _PLAIN.fullmatch(raw):
        try:
            cls(Decimal(raw))
        except DecimalValueError as exc:
            code = exc.code
    scale = code in ("decimal_scale_exceeded", "decimal_out_of_range")
    reason = Reason.SCALE_OVERFLOW if scale else Reason.MALFORMED_DECIMAL
    raise RowError(reason, f"{name}: {code}")


def require_empty(row: Mapping[str, str], kind: str, *names: str) -> None:
    for name in names:
        if row.get(name, ""):
            raise RowError(Reason.FIELD_INVALID, f"{name} not expected for {kind}")


def require_zero_fee(row: Mapping[str, str], kind: str) -> None:
    if row.get("fee", "") and wire_decimal(MoneyAmount, row, "fee").value:
        raise RowError(Reason.FIELD_INVALID, f"fee not expected for {kind}")


def _screen(row: Mapping[str, str], unknown: Mapping[str, str], lim: Limits) -> None:
    """Every cell is length-limited. Only mapped text fields get the formula check:
    numeric fields are validated by the decimal grammar (a leading '-' is a sign) and
    unknown fields are never interpreted, only neutralized on export."""
    for name, cell in [*row.items(), *unknown.items()]:
        if len(cell) > lim.max_field:
            raise RowError(Reason.FIELD_TOO_LONG, f"{safe_repr(name)} too long")
    for name, cell in row.items():
        if name not in NUMERIC_FIELDS and cell.startswith(INJECTION_PREFIXES):
            raise RowError(Reason.FORMULA_INJECTION, f"{name} looks like a formula")


@dataclass(frozen=True, slots=True)
class Context:
    req: ImportRequest
    source_map: SourceMap
    master: SecurityMaster
    at: datetime


@dataclass(slots=True)
class ParsedRow:
    number: int
    row: dict[str, str]  # by canonical field
    unknown: dict[str, str]
    header: Header | None = None
    error: RowError | None = None

    @property
    def order(self) -> tuple[datetime, str, str, str]:
        assert self.header is not None
        h = self.header
        return (h.effective_at, h.account_id, h.source.source_id, h.source.record_id)


def _resolve_header(ctx: Context, row: Mapping[str, str]) -> Header:
    try:
        ref = SourceRef(text(row, "source_ref"), text(row, "record_id"))
    except CorporateActionError:
        raise RowError(Reason.INVALID_REFERENCE, "source_ref or record_id") from None
    try:
        at = parse_instant(text(row, "effective_at"))
    except InstantError:
        raise RowError(Reason.MALFORMED_TIMESTAMP, "effective_at") from None
    kind = text(row, "kind")
    if kind not in TRANSACTION_KINDS | SNAPSHOT_KINDS:
        raise RowError(Reason.UNKNOWN_KIND, f"kind {safe_repr(kind)}")
    tx = ctx.req.file_kind is FileKind.TRANSACTIONS
    if kind not in (TRANSACTION_KINDS if tx else SNAPSHOT_KINDS):
        raise RowError(Reason.MIXED_FILE_KIND, f"{kind} in a {ctx.req.file_kind} file")
    source = ctx.source_map.source(ref.source_id)
    acct = None
    if source is not None and source.tenant_id == ctx.req.tenant_id:
        acct = ctx.source_map.resolve(ref.source_id, text(row, "account_ref"), at)
    if acct is None:  # unknown, foreign-tenant and unmapped look the same
        raise RowError(Reason.UNMAPPED_ACCOUNT, "no account mapping in force")
    cur = text(row, "currency")
    if len(cur) != 3 or not cur.isascii() or not cur.isupper():
        raise RowError(Reason.CURRENCY_INVALID, "currency is not ISO 4217-shaped")
    fee_cur = text(row, "fee_currency")
    if fee_cur and fee_cur != cur:
        raise RowError(Reason.MIXED_CURRENCY, "fee currency differs from currency")
    return Header(acct.account_id, at, ref)


def resolve_instrument(
    ctx: Context, row: Mapping[str, str], at: datetime
) -> InstrumentId:
    """Found in the SecurityMaster on the UTC date of `at`, or quarantined."""
    symbol = text(row, "symbol")
    if not symbol:
        raise RowError(Reason.FIELD_INVALID, "symbol required")
    try:
        found = ctx.master.resolve(Ticker(symbol), ctx.req.mic, at.date())
    except IdentityError:
        found = None
    if found is None or found.instrument_id is None:
        status = "malformed" if found is None else found.status
        raise RowError(Reason.UNRESOLVED_SYMBOL, f"symbol {status}; resolve first")
    return found.instrument_id


@dataclass(frozen=True, slots=True)
class ParsedFile:
    file_hash: str
    issues: tuple[Issue, ...]
    rows: tuple[ParsedRow, ...]

    def ready(self) -> list[ParsedRow]:
        """Rows with a resolved header in evaluation order."""
        found = [p for p in self.rows if p.error is None and p.header is not None]
        return sorted(found, key=lambda p: (p.order, p.number))


def parse_file(data: bytes, ctx: Context) -> ParsedFile:
    """File checks, mapping, cell screening and header resolution."""
    req = ctx.req
    issues: list[Issue] = []
    try:
        header, records = _read(data, req)
    except MappingError as err:
        issues.append(Issue(err.code, str(err), True))
        header, records = [], []
    unknown_cols = [h for h in header if h not in req.mapping.columns]
    if unknown_cols:
        issues.append(Issue("unknown_columns", safe_repr(unknown_cols, 256), False))
    cols = req.mapping.columns
    parsed = []
    for number, rec in enumerate(records, 1):
        if len(rec) != len(header):
            bad = RowError(Reason.COLUMN_COUNT, f"{len(rec)} != {len(header)} cells")
            parsed.append(ParsedRow(number, {}, {}, error=bad))
            continue
        raw = dict(zip(header, rec, strict=True))
        row = {cols[k]: v for k, v in raw.items() if k in cols}
        p = ParsedRow(number, row, {k: v for k, v in raw.items() if k not in cols})
        try:
            _screen(p.row, p.unknown, req.limits)
            p.header = _resolve_header(ctx, p.row)
        except RowError as err:
            p.error = err
        parsed.append(p)
    return ParsedFile(hashlib.sha256(data).hexdigest(), tuple(issues), tuple(parsed))


def row_outcome(
    p: ParsedRow,
    status: RowStatus,
    err: RowError | None = None,
    event: JournalEvent | None = None,
    obs: SourceObservation | None = None,
    line: object = None,
) -> RowOutcome:
    reason, message = (None, "") if err is None else (err.reason, err.message)
    rid, unknown = text(p.row, "record_id"), p.unknown
    if status is RowStatus.QUARANTINED:  # truncated and neutralized
        rid, unknown = (
            _retain(rid),
            {_retain(k): _retain(v) for k, v in unknown.items()},
        )
    return RowOutcome(p.number, status, rid, reason, message, event, obs, line, unknown)


def build_preview(
    ctx: Context,
    parsed: ParsedFile,
    rows: list[RowOutcome],
    journal_id: str | None,
    revisions: Mapping[str, int],
) -> ImportPreview:
    req = ctx.req
    bad = [
        row_outcome(p, RowStatus.QUARANTINED, p.error) for p in parsed.rows if p.error
    ]
    rows = sorted([*rows, *bad], key=lambda r: r.row_number)
    return ImportPreview(
        f"imp:{parsed.file_hash[:32]}:{req.file_kind}",
        parsed.file_hash,
        req.file_kind,
        req.format_version,
        req.mapping.version_id,
        req.tenant_id,
        journal_id,
        parsed.issues,
        tuple(rows),
        MappingProxyType(dict(sorted(revisions.items()))),
    )


# ---- Transaction files


def _signed(row: Mapping[str, str], kind: str, sign: int) -> Money:
    gross = wire_decimal(MoneyAmount, row, "gross_cash")
    if gross.value * sign <= 0:
        raise RowError(Reason.FIELD_INVALID, f"gross_cash sign wrong for {kind}")
    return Money(gross if sign > 0 else -gross, text(row, "currency"))


_CASH: Mapping[str, tuple[Callable[[Header, Money], JournalEvent], int]] = {
    "deposit": (ev.deposit, 1),
    "withdrawal": (ev.withdrawal, -1),
    "fee": (ev.fee, -1),
    "interest": (ev.interest, 1),
}


def _transaction(
    ctx: Context, scratch: Journal, h: Header, row: Mapping[str, str]
) -> JournalEvent:
    kind, cur = text(row, "kind"), text(row, "currency")
    if kind in _CASH or kind == "dividend":
        extra = () if kind == "dividend" else ("symbol",)
        require_empty(row, kind, "quantity", "price", *extra)
        require_zero_fee(row, kind)
        if kind == "dividend":
            iid = resolve_instrument(ctx, row, h.effective_at)
            return ev.dividend(h, iid, _signed(row, kind, 1))
        make, sign = _CASH[kind]
        return make(h, _signed(row, kind, sign))
    iid = resolve_instrument(ctx, row, h.effective_at)
    qty = wire_decimal(PositiveQuantity, row, "quantity")
    price = wire_decimal(Price, row, "price")
    if price.value <= 0:  # a zero price would create a known cost of 0
        raise RowError(Reason.FIELD_INVALID, "price must be positive")
    trade_fee = Money(wire_decimal(MoneyAmount, row, "fee"), cur)
    if trade_fee.amount.value < 0:
        raise RowError(Reason.FIELD_INVALID, "fee must not be negative")
    gross = wire_decimal(MoneyAmount, row, "gross_cash")
    try:
        if kind == "buy":
            event = ev.buy(h, iid, qty, price, trade_fee)
        else:
            held = scratch.holding(h, iid)
            if held.cost is None:
                raise RowError(Reason.UNKNOWN_COST, "no prior cost in the journal")
            if held.quantity.value < qty.value:
                raise RowError(Reason.INSUFFICIENT_UNITS, "sale exceeds held units")
            event = ev.sell(h, qty, price, trade_fee, held)
    except DecimalValueError as exc:
        raise RowError(Reason.SCALE_OVERFLOW, f"quantity x price: {exc.code}") from None
    except JournalError as exc:
        mixed = exc.code in ("currency_mismatch", "cost_currency_mixed")
        raise RowError(
            Reason.MIXED_CURRENCY if mixed else Reason.FIELD_INVALID, exc.code
        ) from None
    with localcontext(DOMAIN_CONTEXT):
        value = qty.value * price.value
    if gross.value != (-value if kind == "buy" else value):
        raise RowError(Reason.AMOUNT_MISMATCH, "gross_cash != quantity x price")
    return event


_STATUS = {
    Outcome.POSTED: RowStatus.ACCEPTED,
    Outcome.DUPLICATE: RowStatus.DUPLICATE,
    Outcome.CONFLICT: RowStatus.CONFLICT,
}


def preview(
    data: bytes,
    request: ImportRequest,
    journal: Journal,
    source_map: SourceMap,
    master: SecurityMaster,
    at: datetime,
) -> ImportPreview:
    """Evaluate a transaction file against `journal` without mutating it."""
    if request.file_kind is not FileKind.TRANSACTIONS:
        raise ValueError("snapshot files are previewed by qw_domain.csv_snapshot")
    ctx = Context(request, source_map, master, at)
    parsed = parse_file(data, ctx)
    scratch = journal.copy()
    last = journal.entries()[-1].recorded_at if journal.entries() else at
    posted_at = max(at, last)
    rows: list[RowOutcome] = []
    for p in parsed.ready():
        assert p.header is not None
        h = p.header
        try:
            event = _transaction(ctx, scratch, h, p.row)
        except RowError as err:
            rows.append(row_outcome(p, RowStatus.QUARANTINED, err))
            continue
        obs = SourceObservation(h.account_id, h.source, at, h.effective_at, p.row)
        status = _STATUS[scratch.post(event, posted_at)]
        rows.append(row_outcome(p, status, event=event, obs=obs))
    accounts = {p.header.account_id for p in parsed.ready() if p.header}
    revisions = {a: journal.revision(a) for a in accounts}
    return build_preview(ctx, parsed, rows, journal.journal_id, revisions)


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
    tenant_id: str,
    log: ObservationLog | None = None,
) -> CommitResult:
    """Post every accepted row, or nothing; never raises on a refused commit. A
    changed account revision since the preview is a conflict: re-preview. Only
    accepted rows are observed, so a duplicate written differently (e.g. `50.0` for
    `50`) cannot record an observation conflict."""
    if preview_.file_kind is not FileKind.TRANSACTIONS:
        return CommitResult("rejected", reason="snapshot files do not post events")
    if preview_.tenant_id != tenant_id:
        return CommitResult("rejected", reason="tenant_mismatch")
    if preview_.journal_id != journal.journal_id:
        return CommitResult("rejected", reason="journal_mismatch")
    if preview_.state != "validated":
        return CommitResult("rejected", reason=f"preview is {preview_.state}")
    expected = preview_.expected_revisions.items()
    stale = [a for a, rev in expected if journal.revision(a) != rev]
    if stale:
        return CommitResult("conflict", reason=f"revision changed: {stale}")
    accepted = [r for r in preview_.rows if r.status is RowStatus.ACCEPTED]
    events = [r.event for r in accepted if r.event is not None]
    events.sort(key=lambda e: (e.effective_at, e.key))  # the preview order
    trial = journal.copy()
    try:
        if any(trial.post(e, recorded_at) is not Outcome.POSTED for e in events):
            return CommitResult("conflict", reason="journal no longer accepts it")
    except JournalError as exc:
        return CommitResult("rejected", reason=exc.code)
    for e in events:
        journal.post(e, recorded_at)
    if log is not None:
        for r in accepted:
            if r.observation is not None:
                log.observe(r.observation)
    return CommitResult("committed", len(events), preview_.count(RowStatus.DUPLICATE))
