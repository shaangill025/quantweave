"""The durable T058 tombstone ledger (`0057_deletion_tombstone.sql`; spec §4, §12,
§15; data dictionary `deletion_tombstone`; qw_domain.retention TombstoneLedger and
LedgerCheckpoint; R052, R087, R088).

- One chain per tenant. Each row is exactly a domain `Tombstone`: the record's
  pseudonym (`record_hash`, never its id), its class, the deletion job id, the
  job's trigger time, the deletion time, the residual backup ids and their
  expiry, the hash scheme and the chain hashes. Nothing else is taken or stored:
  a `Decision`'s `record_id` is used only to re-check its hash.
- Owner decision 104: the pseudonym is keyed by the installation's `RecordKey`,
  which the caller passes in (operator-held, like the API secret_key); it is never
  written, logged or exported. `append` and `find` need it (`key_unavailable`);
  `load` and `export_tombstones` verify integrity without it. A ledger holds one
  key (`key_id`, a one-way MAC); another key is `key_mismatch` on `append` and
  `find`, never a silent miss. Rotation starts a new chain (not built).
- `append(tx, key, decision, plan, backups)` records one deletion through the domain
  `TombstoneLedger.append` (residual backups, expiry, seq, prev and entry hash)
  on the verified stored ledger, at the database clock (never before the latest
  tombstone), and writes the new checkpoint in the same transaction. The
  decision must be one of the plan's delete intents (`not_in_plan`,
  `not_a_deletion`; a `held` intent is accepted because the caller,
  `Retention.execute`, appends it only once its hold is released), of this
  tenant (`tenant`), not of the ledger class itself (`tombstone_ledger`), with
  its hash matching (`record_hash`), and not before the plan's trigger time
  (`time_order`). The pseudonym binds the record's incarnation (`created_at`), so
  an existing tombstone means this incarnation was already deleted: it is
  returned with `wrote` False (a retry or a later job). A re-created record gets
  its own tombstone and residual backups.
- `find(tx, key, item)` is the stored tombstone of that incarnation, or None.
- `load(tx, anchor)` rebuilds every row as a domain `Tombstone`, checks each
  seal (row_hash over the entry hash and the stamped recorded_at, in order),
  runs `retention.verify` over the chain, and checks every checkpoint against
  the chain (its head is the entry at its length; the latest covers the whole
  ledger, so a dropped tail is detected). An `anchor` held outside the backup
  failure domain must be a prefix of the stored ledger. Any failure is
  `integrity`. The stored checkpoint shares the database's failure domain: it
  cannot detect a rollback of the whole database; only the caller's anchor can.
- `export_tombstones(tx)` returns every stored row of both tables, re-verified
  first; read-only, only in a snapshot transaction (`tenancy.require_snapshot`).
  The T057 `audit/tombstones.json` section is not wired to it yet.
- Errors: `forbidden` (no grant: only qw_worker writes; qw_app and qw_worker
  read), `argument`, `database`, `integrity` and the codes above.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from qw_domain.account_wire import seal
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.retention import (
    GENESIS,
    Action,
    Backup,
    Decision,
    Item,
    LedgerCheckpoint,
    Plan,
    RecordKey,
    RetentionClass,
    RetentionError,
    Tombstone,
    TombstoneLedger,
    record_hash,
    verify,
)

from qw_adapters.tenancy import TenantTx, require_snapshot

__all__ = ["StoredLedger", "TombstoneStoreError", "append", "export_tombstones",
           "find", "load", "seal"]  # fmt: skip


class TombstoneStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredLedger:
    entries: tuple[Tombstone, ...]  # seq order, verified
    checkpoint: LedgerCheckpoint  # the latest stored one (length 0: none yet)

    def ledger(self) -> TombstoneLedger:
        return TombstoneLedger(list(self.entries))


_TOMBSTONES = ("SELECT seq, record_hash, cls, job_id, requested_at, deleted_at, "
               "residual_backups, backup_expiry_at, hash_scheme, key_id, "
               "prev_hash, entry_hash, "
               "recorded_at, row_hash FROM app.deletion_tombstone "
               "WHERE tenant_id = %s ORDER BY seq")  # fmt: skip
_CHECKPOINTS = ("SELECT length, head, recorded_at, row_hash FROM "
                "app.deletion_tombstone_checkpoint WHERE tenant_id = %s "
                "ORDER BY length")  # fmt: skip
_T_COLS = ("seq", "record_hash", "cls", "job_id", "requested_at", "deleted_at",
           "residual_backups", "backup_expiry_at", "hash_scheme", "key_id",
           "prev_hash",
           "entry_hash",
           "recorded_at", "row_hash")  # fmt: skip
_C_COLS = ("length", "head", "recorded_at", "row_hash")
_INSTANTS = ("requested_at", "deleted_at", "backup_expiry_at", "recorded_at")

type Raw = tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]


def _database(exc: psycopg.Error) -> TombstoneStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return TombstoneStoreError("forbidden", type(exc).__name__)
    return TombstoneStoreError("database", type(exc).__name__)


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    try:
        tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                        (f"deletion_tombstone:{tx.tenant_id}",))  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None


def _read(tx: TenantTx) -> Raw:
    try:
        return (
            [tuple(r) for r in tx.conn.execute(_TOMBSTONES, (tx.tenant_id,))],
            [tuple(r) for r in tx.conn.execute(_CHECKPOINTS, (tx.tenant_id,))],
        )
    except psycopg.Error as exc:
        raise _database(exc) from None


def _utc(value: Any) -> datetime:
    return ensure_aware_utc(value)


def load(tx: TenantTx, anchor: LedgerCheckpoint | None = None) -> StoredLedger:
    _lock(tx, shared=True)
    return _load(tx, anchor)


def _load(tx: TenantTx, anchor: LedgerCheckpoint | None,
          raw: Raw | None = None) -> StoredLedger:  # fmt: skip
    if anchor is not None and type(anchor) is not LedgerCheckpoint:
        raise TombstoneStoreError("argument", "anchor must be a LedgerCheckpoint")
    tenant = str(tx.tenant_id)
    rows, checkpoints = _read(tx) if raw is None else raw
    entries: list[Tombstone] = []
    stamps: list[datetime] = []
    try:
        for (
            seq,
            rh,
            cls,
            job,
            req,
            dele,
            kept,
            exp,
            hs,
            kid,
            prev,
            eh,
            rec,
            row,
        ) in rows:
            at = _utc(rec)
            if row != seal(eh, at) or (stamps and at < stamps[-1]):
                raise ValueError(f"tombstone {seq} seal")
            entries.append(Tombstone(
                seq, tenant, rh, RetentionClass(cls), job, _utc(req), _utc(dele),
                tuple(kept), None if exp is None else _utc(exp), hs, kid, prev,
                eh))  # fmt: skip
            stamps.append(at)
        verify(entries)
        length, head, last = 0, GENESIS, None
        for n, h, rec, row in checkpoints:
            at = _utc(rec)
            if (not length < n <= len(entries) or h != entries[n - 1].entry_hash
                    or row != seal(h, at) or at < stamps[n - 1]
                    or (last is not None and at < last)):  # fmt: skip
                raise ValueError(f"checkpoint {n}")
            length, head, last = n, h, at
        if length != len(entries):
            raise ValueError("the latest checkpoint does not cover the ledger")
        if anchor is not None:
            ok = anchor.length == 0 and anchor.head == GENESIS
            ok = ok or (
                0 < anchor.length <= len(entries)
                and entries[anchor.length - 1].entry_hash == anchor.head
            )
            if not ok:
                raise ValueError("the anchor is not a prefix of the stored ledger")
    except (RetentionError, ValueError, TypeError) as exc:
        raise TombstoneStoreError("integrity", str(exc)) from None
    return StoredLedger(tuple(entries), LedgerCheckpoint(length, head))


def _hash(key: RecordKey | None, tenant: str, cls: RetentionClass, rid: str,
          created: datetime) -> str:  # fmt: skip
    try:
        return record_hash(key, tenant, cls, rid, created)
    except RetentionError as exc:
        raise TombstoneStoreError(exc.code, "the record key") from None


def _check(tx: TenantTx, key: RecordKey | None, d: Decision, p: Plan,
           backups: Sequence[Backup]) -> None:  # fmt: skip
    if type(d) is not Decision or type(p) is not Plan or isinstance(
            backups, (str, bytes)) or not all(
            type(b) is Backup for b in backups):  # fmt: skip
        raise TombstoneStoreError("argument", "a Decision, its Plan and Backups")
    if p.tenant_id != str(tx.tenant_id):
        raise TombstoneStoreError("tenant", "a plan of another tenant")
    if d not in p.decisions:
        raise TombstoneStoreError("not_in_plan", "the decision is not the plan's")
    if d.action is Action.RETAIN:
        raise TombstoneStoreError("not_a_deletion", d.reason)
    if d.cls is RetentionClass.PRIVACY:
        raise TombstoneStoreError("tombstone_ledger", "the ledger is never deleted")
    if isinstance(key, RecordKey) and p.key_id != key.key_id:
        raise TombstoneStoreError("key_mismatch", "planned under another key")
    if d.record_hash != _hash(key, p.tenant_id, d.cls, d.record_id, d.created_at):
        raise TombstoneStoreError("record_hash", "does not match the record")


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return _utc(row[0])


def find(tx: TenantTx, key: RecordKey | None, item: Item) -> Tombstone | None:
    if type(item) is not Item or item.tenant_id != str(tx.tenant_id):
        raise TombstoneStoreError("argument", "an Item of this tenant")
    rh = _hash(key, item.tenant_id, item.cls, item.record_id, item.created_at)
    entries = load(tx).entries
    if entries and isinstance(key, RecordKey) and entries[-1].key_id != key.key_id:
        raise TombstoneStoreError("key_mismatch", "the ledger holds another key")
    return next((t for t in entries if t.record_hash == rh), None)


def append(tx: TenantTx, key: RecordKey | None, decision: Decision, plan: Plan,
           backups: Sequence[Backup] = ()) -> tuple[Tombstone, bool]:  # fmt: skip
    _check(tx, key, decision, plan, backups)
    _lock(tx, shared=False)
    stored = _load(tx, None)
    ledger = stored.ledger()
    old = ledger.find(plan.tenant_id, decision.record_hash)
    if old is not None:
        return old, False  # the first tombstone stands
    at = max([_now(tx), *(t.deleted_at for t in stored.entries[-1:])])
    if at < plan.trigger_at:
        raise TombstoneStoreError("time_order", "before the job's trigger time")
    try:
        t = ledger.append(decision, plan, at, tuple(backups))
    except RetentionError as exc:
        raise TombstoneStoreError(exc.code, "refused by the domain") from None
    values = {"tenant_id": tx.tenant_id, "seq": t.seq, "record_hash": t.record_hash,
              "cls": t.cls.value, "job_id": t.job_id,
              "requested_at": t.requested_at, "deleted_at": t.deleted_at,
              "residual_backups": list(t.residual_backups),
              "backup_expiry_at": t.backup_expiry_at,
              "hash_scheme": t.hash_scheme, "key_id": t.key_id,
              "prev_hash": t.prev_hash,
              "entry_hash": t.entry_hash}  # fmt: skip
    try:
        with tx.conn.transaction():
            tx.conn.execute(
                f"INSERT INTO app.deletion_tombstone ({', '.join(values)}) VALUES "
                f"({', '.join(f'%({k})s' for k in values)})", values)  # fmt: skip
            tx.conn.execute(
                "INSERT INTO app.deletion_tombstone_checkpoint (tenant_id, length, "
                "head) VALUES (%s, %s, %s)",
                (tx.tenant_id, t.seq + 1, t.entry_hash),
            )
    except psycopg.Error as exc:
        raise _database(exc) from None
    got = _load(tx, None)
    if got.entries[-1] != t:
        raise TombstoneStoreError("integrity", "the stored tombstone differs")
    return t, True


def export_tombstones(tx: TenantTx) -> dict[str, list[dict[str, Any]]]:
    """Every stored row, re-verified first (`integrity` on any failure). The
    snapshot alone makes it consistent, so no lock is taken."""
    require_snapshot(tx)
    raw = _read(tx)
    _load(tx, None, raw)
    tenant = str(tx.tenant_id)
    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows, cols in (("tombstones", raw[0], _T_COLS),
                             ("checkpoints", raw[1], _C_COLS)):  # fmt: skip
        out[name] = []
        for r in rows:
            d: dict[str, Any] = {"tenant_id": tenant, **dict(zip(cols, r, strict=True))}
            for k in _INSTANTS:
                if d.get(k) is not None:
                    d[k] = format_instant(d[k])
            if "residual_backups" in d:
                d["residual_backups"] = list(d["residual_backups"])
            out[name].append(d)
    return out
