"""Persisted evidence store and claim graph (T036 increment 3; `0012_evidence.sql`;
spec §10). The domain `EvidenceStore` is the only writer: callers `load` the
tenant's store, mutate it through the domain methods, then `save` it in the same
tenant transaction. `save` takes a per-tenant lock, inserts the records the
tables lack (never updating a row), removes the text of tombstoned or purged
sources, and reads everything back: the stored snapshot must equal the store's,
else `EvidenceStoreError("integrity")` (another writer's row, a forgery, or a
differing row under one key) and the transaction rolls back. `load` rebuilds the
store with `EvidenceStore.restore`, which re-checks every stored invariant.
Database failures are `EvidenceStoreError("database")`. On any EvidenceStoreError
the caller must roll back the transaction (leave the `tenant_transaction` block by
the exception); its partial inserts must never be committed.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.evidence import (
    Citation,
    ClaimKind,
    ClaimRevision,
    EvidenceError,
    EvidenceStore,
    Passage,
    Reading,
    Resolution,
    Snapshot,
    Source,
    SourceKind,
)
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.rights import UseScope

from qw_adapters.tenancy import TenantTx

__all__ = ["EvidenceError", "EvidenceStoreError", "load", "save"]
TABLES = ("evidence_source", "evidence_content", "evidence_record", "claim",
          "claim_evidence", "claim_resolution", "evidence_event")  # fmt: skip


class EvidenceStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def reading_json(r: Reading) -> dict[str, object]:
    start = r.period.start
    return {"entity": r.entity.to_wire(), "attribute": r.attribute,
            "start": None if start is None else start.isoformat(),
            "end": r.period.end.isoformat(), "unit": r.unit,
            "value": str(r.value)}  # fmt: skip


def reading_of(j: Mapping[str, Any]) -> Reading:
    try:
        if set(j) != {"entity", "attribute", "start", "end", "unit", "value"}:
            raise ValueError("reading keys")
        if not isinstance(j["value"], str):
            raise TypeError("value")
        start = None if j["start"] is None else date.fromisoformat(j["start"])
        period = Period(start, date.fromisoformat(j["end"]))
        r = Reading(InstrumentId.from_wire(j["entity"]), j["attribute"], period,
                    j["unit"], Decimal(j["value"]))  # fmt: skip
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise EvidenceStoreError("integrity", "reading") from None
    if reading_json(r) != dict(j):
        raise EvidenceStoreError("integrity", "reading is not canonical")
    return r


def _rows(tx: TenantTx, sql: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in tx.conn.execute(sql, (tx.tenant_id,)).fetchall()]


def _utc(at: datetime) -> datetime:
    return ensure_aware_utc(at)


def _read(tx: TenantTx) -> Snapshot:
    t, where = str(tx.tenant_id), " WHERE tenant_id = %s"
    sources = tuple(sorted(
        (Source(t, i, SourceKind(k), f, fh, ch, o, _utc(p), _utc(r), _utc(u),
                UseScope(sc), j)
         for i, k, f, fh, ch, o, p, r, u, sc, j in _rows(tx,
            "SELECT id, kind, feed_id, feed_hash, content_hash, origin, published_at,"
            " received_at, retain_until, scope, jurisdiction FROM app.evidence_source"
            + where)),
        key=lambda s: s.source_id))  # fmt: skip
    content = dict(sorted(_rows(tx, "SELECT source_id, content FROM "
                                    "app.evidence_content" + where)))  # fmt: skip
    passages = tuple(sorted(
        (Passage(t, i, s, a, b, hh, reading_of(rd), _utc(k))
         for i, s, a, b, hh, rd, k in _rows(tx,
            "SELECT id, source_id, start_offset, end_offset, hash, reading, known_at"
            " FROM app.evidence_record" + where)),
        key=lambda p: p.passage_id))  # fmt: skip
    revisions = tuple(sorted(
        (ClaimRevision(t, i, n, ClaimKind(k), reading_of(st), _utc(at))
         for i, n, k, st, at in _rows(tx, "SELECT id, revision, kind, stated, "
                                          "known_at FROM app.claim" + where)),
        key=lambda r: (r.claim_id, r.revision)))  # fmt: skip
    citations = tuple(sorted(
        (Citation(i, t, c, n, e, _utc(at))
         for i, c, n, e, at in _rows(tx, "SELECT id, claim_id, revision, "
            "evidence_id, linked_at FROM app.claim_evidence" + where)),
        key=lambda c: c.citation_id))  # fmt: skip
    events: dict[str, dict[str, datetime]] = {
        "withdrawn": {}, "deleted": {}, "purged": {}}  # fmt: skip
    for kind, subject, at in _rows(
        tx, "SELECT kind, subject, at FROM app.evidence_event" + where
    ):
        events[kind][subject] = _utc(at)
    resolutions = tuple(
        Resolution(t, frozenset((c, n) for c, n in m), p, a, _utc(at))
        for _, m, p, a, at in _rows(tx, "SELECT position, members, preferred, "
            "actor_id, at FROM app.claim_resolution" + where + " ORDER BY position")
    )  # fmt: skip
    withdrawn, deleted, purged = (
        dict(sorted(events[k].items())) for k in ("withdrawn", "deleted", "purged")
    )
    return Snapshot(t, sources, content, passages, revisions, citations,
                    withdrawn, deleted, purged, resolutions)  # fmt: skip


def _snapshot(tx: TenantTx) -> Snapshot:
    try:
        return _read(tx)
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceStoreError("integrity", type(exc).__name__) from None


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    key = "hashtextextended('evidence:' || %s, 0)"
    tx.conn.execute(f"SELECT {fn}({key})", (str(tx.tenant_id),))


def load(tx: TenantTx, *, for_write: bool = True) -> EvidenceStore:
    """The tenant's store, re-verified; an empty store for a tenant without rows.
    Takes the tenant lock until the transaction ends, so the reads see one state:
    exclusive by default (a caller that will `save` cannot deadlock with another
    writer), shared with `for_write=False` for read-only callers."""
    try:
        _lock(tx, shared=not for_write)
        return EvidenceStore.restore(_snapshot(tx))
    except EvidenceError as exc:
        raise EvidenceStoreError("integrity", str(exc)) from None
    except psycopg.Error as exc:
        raise EvidenceStoreError("database", type(exc).__name__) from None


def _insert(tx: TenantTx, table: str, cols: str, rows: list[tuple[Any, ...]]) -> None:
    marks = ", ".join(["%s"] * (cols.count(",") + 2))
    sql = (f"INSERT INTO app.{table} (tenant_id, {cols}) VALUES ({marks}) "
           "ON CONFLICT DO NOTHING")  # fmt: skip
    with tx.conn.cursor() as cur:
        cur.executemany(sql, [(tx.tenant_id, *row) for row in rows])


def save(tx: TenantTx, store: EvidenceStore) -> None:
    """Append what `store` holds for this tenant; the stored result must equal it."""
    try:
        _save(tx, store)
    except psycopg.Error as exc:
        raise EvidenceStoreError("database", type(exc).__name__) from None


def _save(tx: TenantTx, store: EvidenceStore) -> None:
    t: UUID = tx.tenant_id
    _lock(tx, shared=False)
    snap = store.snapshot(str(t))
    _insert(tx, "evidence_source", "id, kind, feed_id, feed_hash, content_hash, "
            "origin, published_at, received_at, retain_until, scope, jurisdiction",
            [(s.source_id, s.kind.value, s.feed_id, s.feed_hash, s.content_hash,
              s.origin, s.published_at, s.received_at, s.retain_until,
              s.scope.value, s.jurisdiction) for s in snap.sources])  # fmt: skip
    _insert(tx, "evidence_content", "source_id, content", list(snap.content.items()))
    _insert(tx, "evidence_record", "id, source_id, start_offset, end_offset, hash, "
            "reading, known_at",
            [(p.passage_id, p.source_id, p.start, p.end, p.text_hash,
              Jsonb(reading_json(p.reading)), p.known_at)
             for p in snap.passages])  # fmt: skip
    _insert(tx, "claim", "id, revision, kind, stated, known_at",
            [(r.claim_id, r.revision, r.kind.value, Jsonb(reading_json(r.stated)),
              r.known_at) for r in snap.revisions])  # fmt: skip
    _insert(tx, "claim_evidence", "id, claim_id, revision, evidence_id, linked_at",
            [(c.citation_id, c.claim_id, c.revision, c.passage_id, c.linked_at)
             for c in snap.citations])  # fmt: skip
    _insert(tx, "claim_resolution", "position, members, preferred, actor_id, at",
            [(i, Jsonb(sorted(r.members)), r.preferred, r.actor_id, r.at)
             for i, r in enumerate(snap.resolutions)])  # fmt: skip
    events = [("withdrawn", snap.withdrawn), ("deleted", snap.deleted),
              ("purged", snap.purged)]  # fmt: skip
    _insert(
        tx,
        "evidence_event",
        "kind, subject, at",
        [(k, s, at) for k, table in events for s, at in table.items()],
    )
    removed = sorted({*snap.deleted, *snap.purged})
    tx.conn.execute(
        "DELETE FROM app.evidence_content WHERE tenant_id = %s AND source_id = ANY(%s)",
        (t, removed),
    )
    if _snapshot(tx) != snap:
        raise EvidenceStoreError("integrity", "the stored records differ")
