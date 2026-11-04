"""Store deletion targets for the `retention.delete` job (T058; spec §4, §12, §15;
R052, R086-R088): for each retention class the job deletes, list the tenant's
records with their exact `created_at` and delete one record by (id, created_at).
The worker wraps the registered ones as `deletion_job.Target`s (simulation only);
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
- `evidence` (0012; data dictionary evidence_record): NOT REGISTERED in
  `deletion_job.TARGETS` until owner decision 124's passage-reading redaction
  lands (receipt T058 increment 7). These functions delete less than decision
  124 requires, so the job must not report the class deleted through them; they
  are kept, with their direct tests, as the base for that redesign. A record is
  one evidence source whose text is still held, its `created_at` the source row's
  `recorded_at`. Deleting it is the evidence store's own deletion
  (`EvidenceStore.delete` then `evidence_store.save`): a `deleted` event, and the
  text removed; hashes, passages and history remain by that design, and
  citations of the source become replay-limited when the store is read. No new
  grant: 0012 already lets qw_worker do this. Owner decision 124 also requires
  the passages' readings (`evidence_record.reading`) to go; that is not built.
- Locks: a listing first checks, without a lock, whether the tenant has any
  candidate; with none it returns [] and takes no lock. Otherwise it takes the
  store's tenant lock exclusively and keeps it to the end of the transaction, so
  the job holds every store lock before the first tombstone lock, and two jobs
  of one tenant cannot deadlock. The registered path takes only `simulation`;
  the evidence listing keeps its `evidence` lock, which in class order comes
  first (as the simulation store requires) once evidence is registered. A
  sweep with candidates therefore blocks the store's writers for its duration
  (an accepted residual).
- Failures are `TargetStoreError(code, retryable)`: `database`, retryable only
  for a transient error (`TRANSIENT`), or the evidence store's `integrity` / its
  domain refusal (not retryable).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import errors
from qw_domain import retention as rt
from qw_domain.evidence import EvidenceError
from qw_domain.instants import ensure_aware_utc

from qw_adapters import evidence_store as es
from qw_adapters.tenancy import TenantTx

__all__ = ["DELETE_ORDER", "TargetStoreError", "evidence_delete", "evidence_items",
           "simulation_delete", "simulation_items"]  # fmt: skip
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


_SOURCES = ("SELECT s.id, s.recorded_at FROM app.evidence_source s WHERE "
            "s.tenant_id = %s AND (cardinality(%s::text[]) = 0 OR s.id = ANY(%s)) "
            "AND EXISTS (SELECT 1 FROM app.evidence_content c WHERE c.tenant_id = "
            "s.tenant_id AND c.source_id = s.id) ORDER BY s.id")  # fmt: skip


_HELD = ("SELECT 1 FROM app.evidence_source s JOIN app.evidence_content c ON "
         "c.tenant_id = s.tenant_id AND c.source_id = s.id WHERE s.tenant_id = %s "
         "AND s.id = %s AND s.recorded_at = %s")  # fmt: skip


def evidence_items(tx: TenantTx, ids: frozenset[str]) -> list[rt.Item]:
    """The tenant's evidence sources whose text is held (all when `ids` is empty),
    the store exclusively locked. The store is verified per deletion, not here,
    so a corrupt evidence row fails its own deletions, never a sweep."""

    def go() -> list[rt.Item]:
        if not _rows(tx, _SOURCES + " LIMIT 1", ids):  # lock-free pre-check
            return []
        _lock(tx, "evidence")
        return _items(tx, rt.RetentionClass.EVIDENCE, _rows(tx, _SOURCES, ids))

    return _guarded(go)


def evidence_delete(tx: TenantTx, item: rt.Item) -> bool:
    """Tombstone the source `item` names and drop its text; False when absent
    (no such source or incarnation, or its text is already gone)."""

    def held() -> bool:
        row = tx.conn.execute(_HELD, (tx.tenant_id, item.record_id,
                                      item.created_at)).fetchone()  # fmt: skip
        return row is not None

    def go() -> bool:
        if not held():  # cheap first: a retry of a deleted record never loads
            return False
        store = es.load(tx)  # the exclusive lock; then check again under it
        if not held():
            return False
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        store.delete(str(tx.tenant_id), item.record_id, row[0])
        es.save(tx, store)
        return True

    return _guarded(go)
