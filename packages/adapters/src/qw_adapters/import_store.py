"""Persisted CSV import previews and their commit into the journal store (T014
increment 2; `0008_import_preview.sql`; spec §4 "Import workflow"; T008 C-04, F-23;
T014 review note N3).

`save_preview` runs the domain preview (`qw_domain.csv_import.preview`) on the stored
journals of the file's accounts and keeps the bytes, the request, the per-account
revisions it saw, a content hash (tenant, request, file) and a hash of the row
outcomes. The commit token is the preview id plus the content hash. A new validated
preview of an account supersedes the tenant's open previews that name it (previews
saved concurrently do not see each other; the revision check settles them).

`commit_preview` works in the caller's transaction:
1. it locks the preview row, so concurrent commits of one preview serialise, and a
   committed preview returns its stored result (idempotent);
2. it refuses an unknown or other-tenant id (one answer for both), another hash,
   stored inputs that no longer reproduce the content hash, and an expired (then
   marked so) or superseded preview;
3. it locks the accounts in sorted order and requires each current revision to be
   the bound one, else `conflict`;
4. it re-derives the preview from the stored inputs under those locks and requires
   the same row outcomes (else `conflict` `preview_changed`, e.g. the SourceMap or
   SecurityMaster changed) and a validated state;
5. it observes the accepted source records and posts their events (`post_many`) in a
   savepoint, then marks the preview committed. A refusal writes no journal row.
Role: the import job runs as qw_worker (qw_app for an interactive commit). qw_ingest
writes source records only (T008 §5), and a commit writes ledger events.
`redact_settled_preview` drops a settled preview's raw file (it may hold personal
data); its hashes and result stay, so a re-commit still returns the result.
LIMITATIONS: transaction files only (snapshot files never post); the SourceMap and
SecurityMaster are not persisted, so the caller supplies them at both steps; no
retention job or class schedules redaction yet, and rows are never deleted.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from qw_domain import csv_import as ci
from qw_domain.identity import Mic, SecurityMaster
from qw_domain.journal import Journal, Outcome
from qw_domain.postings import JournalError
from qw_domain.sources import SourceMap

from qw_adapters import journal_store as js
from qw_adapters.tenancy import TenantTx

DEFAULT_TTL = timedelta(hours=24)  # owner-adjustable assumption; the spec sets none
MAX_FILE_BYTES = 8 * 1024 * 1024  # the 0008 CHECK on file_bytes
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class CommitToken:
    """The `commit_token` of `import_preview.schema.json`: "<preview id>:<hash>"."""

    preview_id: uuid.UUID
    content_hash: str

    def __post_init__(self) -> None:
        if not _HASH.fullmatch(self.content_hash):
            raise ValueError("commit token: content hash is not lowercase SHA-256")

    def wire(self) -> str:
        return f"{self.preview_id}:{self.content_hash}"

    @classmethod
    def from_wire(cls, text: str) -> CommitToken:
        head, sep, digest = text.partition(":")
        try:
            preview_id = uuid.UUID(head)
        except ValueError:
            raise ValueError("commit token: preview id") from None
        if not sep or str(preview_id) != head:
            raise ValueError("commit token: not '<uuid>:<hash>'")
        return cls(preview_id, digest)


def _request_doc(req: ci.ImportRequest) -> dict[str, Any]:
    return {
        "format_version": req.format_version,
        "file_kind": req.file_kind.value,
        "mapping": {"version_id": req.mapping.version_id,
                    "columns": dict(sorted(req.mapping.columns.items()))},
        "mic": None if req.mic is None else req.mic.code,
        "limits": dataclasses.asdict(req.limits),
    }  # fmt: skip


def _request_from(tenant: uuid.UUID, doc: Mapping[str, Any]) -> ci.ImportRequest:
    mapping = ci.ColumnMapping(doc["mapping"]["version_id"], doc["mapping"]["columns"])
    mic = None if doc["mic"] is None else Mic(doc["mic"])
    return ci.ImportRequest(
        str(tenant), ci.FileKind(doc["file_kind"]), mapping, doc["format_version"],
        ci.Limits(**doc["limits"]), mic,
    )  # fmt: skip


def _sha256(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def content_hash(tenant: uuid.UUID, doc: Mapping[str, Any], data: bytes) -> str:
    """SHA-256 over the tenant, the request and the file's SHA-256."""
    file_hash = hashlib.sha256(data).hexdigest()
    return _sha256({"tenant_id": str(tenant), "request": doc, "file": file_hash})


def result_hash(p: ci.ImportPreview) -> str:
    """SHA-256 over what the user saw: each row's status, reason, event id and
    observation fingerprint, the file issues, the bound revisions and the state."""
    rows = [
        [r.row_number, r.status.value, None if r.reason is None else r.reason.value,
         None if r.event is None else r.event.event_id,
         None if r.observation is None else r.observation.fingerprint]
        for r in p.rows
    ]  # fmt: skip
    issues = [[i.code, i.blocking] for i in p.issues]
    revisions = dict(p.expected_revisions)
    return _sha256({"rows": rows, "issues": issues, "revisions": revisions,
                    "state": p.state})  # fmt: skip


def _load(tx: TenantTx, accounts: Iterable[str]) -> Journal:
    """One journal holding the stored accounts (each account in revision order)."""
    entries = [e for a in sorted(accounts) for e in js.load_journal(tx, a).entries()]
    journal = Journal()
    for entry in sorted(entries, key=lambda e: e.recorded_at):  # stable per account
        if journal.post(entry.event, entry.recorded_at) is not Outcome.POSTED:
            raise js.JournalIntegrityError(f"{entry.event.event_id} does not replay")
    return journal


def _derive(
    tx: TenantTx,
    data: bytes,
    request: ci.ImportRequest,
    source_map: SourceMap,
    master: SecurityMaster,
    at: datetime,
) -> ci.ImportPreview:
    parsed = ci.parse_file(data, ci.Context(request, source_map, master, at))
    accounts = {p.header.account_id for p in parsed.ready() if p.header is not None}
    return ci.preview(data, request, _load(tx, accounts), source_map, master, at)


def save_preview(
    tx: TenantTx,
    data: bytes,
    request: ci.ImportRequest,
    source_map: SourceMap,
    master: SecurityMaster,
    ttl: timedelta = DEFAULT_TTL,
) -> tuple[ci.ImportPreview, CommitToken]:
    """Preview a transaction file against the stored journal and persist it. The
    preview time (also each observation's `observed_at`) is the transaction time."""
    if request.tenant_id != str(tx.tenant_id):
        raise ValueError("the request's tenant is not the transaction's tenant")
    if request.file_kind is not ci.FileKind.TRANSACTIONS:
        raise ValueError("only transaction files are committed")
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if len(data) > MAX_FILE_BYTES or request.limits.max_bytes > MAX_FILE_BYTES:
        raise ValueError("a stored import file is at most 8 MiB")
    row = tx.conn.execute("SELECT now()").fetchone()
    assert row is not None
    p = _derive(tx, data, request, source_map, master, row[0])
    doc = _request_doc(request)
    token = CommitToken(uuid.uuid4(), content_hash(tx.tenant_id, doc, data))
    if p.state == "validated":  # a failed re-preview leaves the others usable
        tx.conn.execute(
            "UPDATE app.import_preview SET status = 'superseded' WHERE tenant_id = %s "
            "AND status = 'open' AND expected_revisions ?| %s::text[]",
            (tx.tenant_id, sorted(p.expected_revisions)),
        )
    tx.conn.execute(
        "INSERT INTO app.import_preview (tenant_id, preview_id, content_hash, "
        "result_hash, request, file_bytes, expected_revisions, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s)",
        (tx.tenant_id, token.preview_id, token.content_hash, result_hash(p),
         Jsonb(doc), data, Jsonb(dict(p.expected_revisions)), ttl),
    )  # fmt: skip
    return p, token


def _rejected(reason: str) -> ci.CommitResult:
    return ci.CommitResult("rejected", reason=reason)


def commit_preview(
    tx: TenantTx, token: CommitToken, source_map: SourceMap, master: SecurityMaster
) -> ci.CommitResult:
    """Commit a saved preview all or nothing (see the module docstring)."""
    where = "WHERE tenant_id = %s AND preview_id = %s"
    key = (tx.tenant_id, token.preview_id)
    row = tx.conn.execute(
        "SELECT content_hash, request, file_bytes, expected_revisions, result_hash, "
        "status::text, created_at, expires_at <= now(), posted_count, "
        f"duplicate_count FROM app.import_preview {where} FOR UPDATE",
        key,
    ).fetchone()
    if row is None:
        return _rejected("unknown_preview")
    stored, doc, data, revisions, digest, status, created, expired, posted, dups = row
    if token.content_hash != stored:
        return _rejected("token_mismatch")
    if status == "committed":
        return ci.CommitResult("committed", posted, dups)
    if status != "open":
        return _rejected(status)
    data = None if data is None else bytes(data)  # NULL only past the CHECK
    if data is None or content_hash(tx.tenant_id, doc, data) != stored:
        return _rejected("hash_mismatch")
    if expired:
        tx.conn.execute(
            f"UPDATE app.import_preview SET status = 'expired' {where}", key
        )
        return _rejected("expired")
    for account in sorted(revisions):
        js.lock_account(tx, account)
    stale = sorted(a for a, rev in revisions.items() if js.revision(tx, a) != rev)
    if stale:
        return ci.CommitResult("conflict", reason=f"revision changed: {stale}")
    request = _request_from(tx.tenant_id, doc)
    p = _derive(tx, data, request, source_map, master, created)
    if result_hash(p) != digest:
        return ci.CommitResult("conflict", reason="preview_changed")
    if p.state != "validated":
        return _rejected(f"preview is {p.state}")
    accepted = [r for r in p.rows if r.status is ci.RowStatus.ACCEPTED]
    events = sorted(
        (r.event for r in accepted if r.event is not None),
        key=lambda e: (e.effective_at, e.key),  # the preview's order
    )
    try:
        with tx.conn.transaction():  # a savepoint: all of it or none
            for r in accepted:
                if r.observation is not None:
                    js.observe(tx, r.observation)
            outcomes = js.post_many(tx, events)
            if any(o is not Outcome.POSTED for o in outcomes):
                raise js.JournalConflict(("import",), "an accepted row was held")
    except js.JournalConflict:
        return ci.CommitResult("conflict", reason="journal no longer accepts it")
    except JournalError as exc:
        return _rejected(exc.code)
    result = ci.CommitResult("committed", len(events), p.count(ci.RowStatus.DUPLICATE))
    tx.conn.execute(
        "UPDATE app.import_preview SET status = 'committed', committed_at = now(), "
        f"posted_count = %s, duplicate_count = %s {where}",
        (result.posted, result.duplicates, *key),
    )
    return result


def redact_settled_preview(tx: TenantTx, preview_id: uuid.UUID) -> None:
    """Drop a committed, expired or superseded preview's raw file (idempotent). Runs
    as qw_worker (the retention job); an open preview raises `ValueError`, an
    unknown or other-tenant id `LookupError`."""
    row = tx.conn.execute(
        "SELECT status::text FROM app.import_preview WHERE tenant_id = %s AND "
        "preview_id = %s FOR UPDATE",
        (tx.tenant_id, preview_id),
    ).fetchone()
    if row is None:
        raise LookupError("unknown preview")
    if row[0] == "open":
        raise ValueError("an open preview keeps its file")
    tx.conn.execute(
        "UPDATE app.import_preview SET file_bytes = NULL WHERE tenant_id = %s AND "
        "preview_id = %s AND file_bytes IS NOT NULL",
        (tx.tenant_id, preview_id),
    )
