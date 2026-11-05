"""Store deletion targets for the `retention.delete` job (T058; spec §4, §12, §15;
R052, R086-R088): for each retention class the job deletes, list the tenant's
records with their exact `created_at` and delete one record by (id, created_at).
`HANDLERS` registers them (the worker's `deletion_job.Target`s and the request
route's `SUPPORTED` classes both come from it);
everything runs as qw_worker in the job's tenant transaction, under forced RLS,
and every statement also names the tenant.

- `simulation` (0014; data dictionary simulation_account, simulation_order_fill):
  a record is one simulation, its `created_at` the row's own column. Deleting it
  removes every row of that simulation (fills, entries, orders, digests, then the
  definition). 0063 grants qw_worker DELETE on the five tables, lets only a
  qw_worker member (never a superuser or the owner) delete, and refuses at commit
  any deletion that leaves the definition behind. A record with the id but
  another `created_at` is another incarnation and is not touched (absent).
  Calculation records of the simulation (audit class) are kept.
- `evidence` (0012, 0065; data dictionary evidence_record): a record is one
  evidence source not yet deleted (its text held, or already purged after
  retention, with its readings still stored), its `created_at` the source row's
  `recorded_at`. Deleting it is the evidence store's own deletion
  (`EvidenceStore.delete` then `evidence_store.save`), to owner decision 124's
  scope: a `deleted` event, the text removed (if still held) and its passages'
  readings set to
  NULL (0065's qw_worker-only UPDATE; a tombstone cannot commit while a reading
  remains). Ids, offsets, hashes, citations (replay-limited) and the source
  row's metadata remain. Claims' stated values are research class (decision
  121) and stay.
- Locks: a listing first checks, without a lock, whether the tenant has any
  candidate; with none it returns [] and takes no lock. Otherwise it takes the
  store's tenant lock exclusively and keeps it to the end of the transaction, so
  the job holds every store lock before the first tombstone lock, and two jobs
  of one tenant cannot deadlock. In class order the job takes `evidence`, then
  `simulation` (the simulation store's documented order). A sweep with
  candidates therefore blocks the store's writers for its duration (an
  accepted residual).
- Failures are `TargetStoreError(code, retryable)`: `database`, retryable only
  for a transient error (`TRANSIENT`), or the evidence store's `integrity` / its
  domain refusal (not retryable).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

import psycopg
from psycopg import errors
from qw_domain import retention as rt
from qw_domain.evidence import EvidenceError
from qw_domain.instants import ensure_aware_utc

from qw_adapters import evidence_store as es
from qw_adapters.tenancy import TenantTx

__all__ = ["DELETE_ORDER", "HANDLERS", "SUPPORTED", "TargetStoreError",
           "evidence_delete", "evidence_items", "simulation_delete",
           "simulation_items"]  # fmt: skip
# Children first (fills reference orders and entries); the definition last.
DELETE_ORDER = ("simulation_fill", "simulation_entry", "simulation_order",
                "simulation_digest")  # fmt: skip


# Errors a retry may cure; every other database error is not retryable.
TRANSIENT = (errors.SerializationFailure, errors.DeadlockDetected,
             errors.LockNotAvailable, errors.QueryCanceled,
             psycopg.OperationalError)  # fmt: skip


class TargetStoreError(Exception):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code, self.retryable = code, retryable


def _guarded[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except psycopg.Error as exc:
        raise TargetStoreError("database", retryable=_transient(exc)) from None
    except es.EvidenceStoreError as exc:  # its database error keeps the cause
        retry = exc.code == "database" and _transient(exc.__context__)
        raise TargetStoreError(exc.code, retryable=retry) from None
    except EvidenceError:
        raise TargetStoreError("evidence_refused", retryable=False) from None


def _transient(exc: BaseException | None) -> bool:
    return isinstance(exc, TRANSIENT)


def _lock(tx: TenantTx, name: str) -> None:
    tx.conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{name}:{tx.tenant_id}",),
    )  # as the store's own lock


def _rows(tx: TenantTx, sql: str, ids: frozenset[str]) -> list[tuple[Any, ...]]:
    cur = tx.conn.execute(sql, (tx.tenant_id, sorted(ids), sorted(ids)))
    return [tuple(r) for r in cur.fetchall()]


def _items(tx: TenantTx, cls: rt.RetentionClass,
           rows: list[tuple[Any, ...]]) -> list[rt.Item]:  # fmt: skip
    return [rt.Item(str(tx.tenant_id), i, cls, ensure_aware_utc(at))
            for i, at in rows]  # fmt: skip


_SIMULATIONS = (
    "SELECT id, created_at FROM app.simulation WHERE tenant_id = %s AND "
    "(cardinality(%s::text[]) = 0 OR id = ANY(%s)) ORDER BY id"
)


def simulation_items(tx: TenantTx, ids: frozenset[str]) -> list[rt.Item]:
    """The tenant's simulations (all when `ids` is empty), exclusively locked."""

    def go() -> list[rt.Item]:
        if not _rows(tx, _SIMULATIONS + " LIMIT 1", ids):  # lock-free pre-check
            return []
        _lock(tx, "simulation")
        return _items(tx, rt.RetentionClass.SIMULATION, _rows(tx, _SIMULATIONS, ids))

    return _guarded(go)


def simulation_delete(tx: TenantTx, item: rt.Item) -> bool:
    """Delete the simulation `item` names, every row of it; False when absent."""

    def go() -> bool:
        _lock(tx, "simulation")
        key = (tx.tenant_id, item.record_id)
        row = tx.conn.execute(
            "SELECT 1 FROM app.simulation WHERE tenant_id = %s AND id = %s AND "
            "created_at = %s", (*key, item.created_at)).fetchone()  # fmt: skip
        if row is None:
            return False
        for table in DELETE_ORDER:
            tx.conn.execute(f"DELETE FROM app.{table} WHERE tenant_id = %s AND "
                            "simulation_id = %s", key)  # fmt: skip
        cur = tx.conn.execute("DELETE FROM app.simulation WHERE tenant_id = %s AND "
                              "id = %s", key)  # fmt: skip
        return cur.rowcount == 1

    return _guarded(go)


# Not yet deleted: no `deleted` event (a purged source's readings remain).
_LIVE = ("NOT EXISTS (SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = "
         "s.tenant_id AND e.kind = 'deleted' AND e.subject = s.id)")  # fmt: skip
_SOURCES = ("SELECT s.id, s.recorded_at FROM app.evidence_source s WHERE "
            "s.tenant_id = %s AND (cardinality(%s::text[]) = 0 OR s.id = ANY(%s)) "
            f"AND {_LIVE} ORDER BY s.id")  # fmt: skip


_PRESENT = ("SELECT 1 FROM app.evidence_source s WHERE s.tenant_id = %s "
         f"AND s.id = %s AND s.recorded_at = %s AND {_LIVE}")  # fmt: skip


def evidence_items(tx: TenantTx, ids: frozenset[str]) -> list[rt.Item]:
    """The tenant's evidence sources not yet deleted (all when `ids` is empty),
    the store exclusively locked. The store is verified per deletion, not here,
    so a corrupt evidence row fails its own deletions, never a sweep."""

    def go() -> list[rt.Item]:
        if not _rows(tx, _SOURCES + " LIMIT 1", ids):  # lock-free pre-check
            return []
        _lock(tx, "evidence")
        return _items(tx, rt.RetentionClass.EVIDENCE, _rows(tx, _SOURCES, ids))

    return _guarded(go)


def evidence_delete(tx: TenantTx, item: rt.Item) -> bool:
    """Tombstone the source `item` names, drop any text and redact its readings;
    False when absent (no such source or incarnation, or already deleted)."""

    def present() -> bool:
        row = tx.conn.execute(_PRESENT, (tx.tenant_id, item.record_id,
                                      item.created_at)).fetchone()  # fmt: skip
        return row is not None

    def go() -> bool:
        if not present():  # cheap first: a retry of a deleted record never loads
            return False
        store = es.load(tx)  # the exclusive lock; then check again under it
        if not present():
            return False
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        store.delete(str(tx.tenant_id), item.record_id, row[0])
        es.save(tx, store)
        return True

    return _guarded(go)


type ItemsFn = Callable[[TenantTx, frozenset[str]], list[rt.Item]]
type DeleteFn = Callable[[TenantTx, rt.Item], bool]
# The registry (owner decision 127): one (listing, deletion) per class these stores
# delete completely to the owner's required scope. The worker's `TARGETS` is built
# from it and the request route accepts exactly its classes (`SUPPORTED`), so the
# two cannot disagree. Identity (security) is never here (decision 128).
HANDLERS: Mapping[rt.RetentionClass, tuple[ItemsFn, DeleteFn]] = MappingProxyType({
    rt.RetentionClass.EVIDENCE: (evidence_items, evidence_delete),
    rt.RetentionClass.SIMULATION: (simulation_items, simulation_delete),
})  # fmt: skip
SUPPORTED = frozenset(HANDLERS)
