"""The `budget.expire_due` job (T037 increment 4; spec §10, §15; T008 F-18): a
standard-pool worker on qw_worker sweeps one tenant's budget books in that
tenant's transaction. `handle`: the strict payload (`{"slot": ...}`), then
`budget_store.expire_due` at the database clock for every month whose book
still holds a `reserved` reservation (`open_periods`: the current month and any
earlier one, since a reservation's deadline can fall up to MAX_DEADLINE into
the next month). Each month is swept in its own savepoint, so one month's
failure (a tampered book is `integrity`) neither rolls back nor stops the
others. Each sweep is one compare-and-set step: a dispatched
reservation past its deadline is charged its full reservation (`expired`), an
undispatched one is released (it never reached a provider: the domain's
`release`); nothing else changes, so no deadline, creation time or cost is
reset. A repeat finds nothing due and writes nothing (idempotent).
`stale_version` (another writer installed first) reloads and sweeps again, up
to `_CAS_TRIES` times. Each month's failure is logged with its code; after every
month, the job fails with its swept months kept (`keep_effects`): retryable as
`database` if any failure was `stale_version`, `stale_clock` or `database`,
else `budget_refused` (`integrity`, `forbidden`, ...; not retried).
`submit(tx)`: the idempotent enqueue for `tx`'s tenant, one job per hour of the
database clock (`input_revision` `expire_due:<slot>`).
GAP: no scheduler calls `submit` yet. Nothing in the job layer recurs, and
qw_worker cannot list tenants under RLS, so until a per-tenant scheduler exists
(an operator cron or a future recurring-job mechanism, T025), a sweep runs only
when something enqueues it; between sweeps an overdue reservation stays
`reserved` and keeps holding its amount (fail-closed: it blocks spend, never
frees it early)."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from qw_adapters import budget_store as bs
from qw_adapters import jobs as qj
from qw_adapters.jobs import ClaimedJob
from qw_adapters.tenancy import TenantTx
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import Priority

from qw_worker.loop import Handler, HandlerFailed

log = logging.getLogger("qw_worker.budget_sweep")
KIND = "budget.expire_due"
_SLOT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}", re.ASCII)
_RETRY = frozenset({"database", "stale_version", "stale_clock"})
_CAS_TRIES = 3


def submit(tx: TenantTx) -> tuple[uuid.UUID, bool]:
    """Enqueue this hour's sweep of `tx`'s tenant once: (job id, created)."""
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    slot = ensure_aware_utc(row[0]).strftime("%Y-%m-%dT%H")
    return qj.enqueue(tx, KIND, f"expire_due:{slot}", Priority.SAFETY,
                      {"slot": slot})  # fmt: skip


def _month(tx: TenantTx, period: str) -> bs.StoredBook | None:
    """One month's sweep, reloaded on `stale_version` up to `_CAS_TRIES` times."""
    for _ in range(_CAS_TRIES):
        try:
            return bs.expire_due(tx, period)
        except bs.BudgetStoreError as exc:
            if exc.code != "stale_version":
                raise
    raise bs.BudgetStoreError("stale_version", period)


def sweep(tx: TenantTx) -> tuple[list[bs.StoredBook], list[tuple[str, str]]]:
    """Expire what is due in every open month of `tx`'s tenant, each month in its
    own savepoint: (the swept books, the (period, code) of each failed month)."""
    out, failed = [], []
    for period in bs.open_periods(tx):
        try:
            with tx.conn.transaction():  # a failure rolls back this month only
                stored = _month(tx, period)
        except bs.BudgetStoreError as exc:
            failed.append((period, exc.code))
            continue
        if stored is not None:
            out.append(stored)
    return out, failed


def handle(tx: TenantTx, job: ClaimedJob) -> None:
    p = job.payload
    if not (isinstance(p, dict) and set(p) == {"slot"} and isinstance(p["slot"], str)
            and _SLOT.fullmatch(p["slot"])):  # fmt: skip
        raise HandlerFailed("payload_invalid", retryable=False)
    try:
        _, failed = sweep(tx)
    except bs.BudgetStoreError as exc:  # listing the months: nothing swept
        log.warning("job %s: budget sweep failed: %s", job.id, exc.code)
        retry = exc.code in _RETRY
        raise HandlerFailed("database" if retry else "budget_refused",
                            retryable=retry) from None  # fmt: skip
    for period, code in failed:
        log.warning("job %s: budget sweep of %s failed: %s", job.id, period, code)
    if any(code in _RETRY for _, code in failed):
        raise HandlerFailed("database", retryable=True, keep_effects=True)
    if failed:
        raise HandlerFailed("budget_refused", retryable=False, keep_effects=True)


HANDLERS: Mapping[str, Handler] = MappingProxyType({KIND: handle})
