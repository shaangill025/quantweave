"""Durable jobs, transactional outbox and consumer inbox on PostgreSQL (T025;
`0005_durable_jobs.sql`; spec 03 "Durable coordination").

Guarantee: **at-least-once** delivery, never exactly-once. A job runs again when its
lease expires, even if the worker committed its effect first. `consume` makes
effects idempotent: the (consumer, event id) inbox row and the handler's writes
commit in one transaction, so a redelivery finds the row and is skipped.

Claims pick the highest priority class with ready work, then the least recently
served tenant in it (`app.job_pick`). Heartbeat, complete and fail need the current
lease token and an unexpired lease, else `LeaseLost`; complete and fail run in the
caller's `TenantTx`, so the outcome commits with the business writes and any
`write_outbox` rows. The outbox relay is T025 increment 2.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import pq
from psycopg.types.json import Jsonb
from qw_domain.jobs import (
    Backoff,
    JobState,
    Pool,
    Priority,
    Rng,
    after_failure,
    check_error_code,
    pool_for,
)

from qw_adapters.tenancy import Conn, TenancyError, TenantTx, tenant_transaction

type Json = str | int | bool | Sequence[Json] | Mapping[str, Json] | None
DEFAULT_BACKOFF = Backoff(base=timedelta(seconds=2), cap=timedelta(minutes=30))


MAX_GENERATION = 10  # operator requeues per job (database CHECK)
MAX_WORKER_NAME = 160  # the lease token adds ":" and 32 hex digits (<= 200)


class LeaseLost(Exception):
    """The lease token is no longer valid (expired, reaped or never held)."""


class IdempotencyConflict(Exception):
    """The key (tenant, kind, input revision) exists with another priority, payload
    or max_attempts. Priority is refused rather than promoted: it fixes the pool
    (experiment isolation) and claim order, so a silent change would move work
    between pools or let a repeat jump the queue. Use a new input revision."""


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: str
    input_revision: str
    priority: Priority
    payload: Json
    attempt: int
    max_attempts: int
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str
    payload: Json


def _payload(value: Json) -> Jsonb:
    """Only str, int, bool, None, sequences and str-keyed mappings: decimals travel
    as strings (the database also refuses fractional JSON numbers)."""
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping) and all(isinstance(k, str) for k in item):
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
        elif item is not None and not isinstance(item, str | int):
            raise TypeError(f"unsupported payload value {type(item).__name__}")
    return Jsonb(value)


def _idle(conn: Conn) -> None:
    if not conn.autocommit or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise TenancyError("needs an idle autocommit connection")


def enqueue(
    tx: TenantTx,
    kind: str,
    input_revision: str,
    priority: Priority,
    payload: Json,
    max_attempts: int = 5,
) -> tuple[uuid.UUID, bool]:
    """(job id, created). A repeat of the key with the same priority, payload and
    max_attempts returns the existing job; any difference raises IdempotencyConflict."""
    priority, body = Priority(priority), _payload(payload)
    row = tx.conn.execute(
        "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, pool, "
        "payload, max_attempts) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, kind, input_revision) DO NOTHING RETURNING id",
        (uuid.uuid4(), tx.tenant_id, kind, input_revision, priority.value,
         pool_for(priority).value, body, max_attempts),
    ).fetchone()  # fmt: skip
    if row is not None:
        return row[0], True
    found = tx.conn.execute(
        "SELECT id, priority = %s AND payload = %s AND max_attempts = %s FROM app.job "
        "WHERE tenant_id = %s AND kind = %s AND input_revision = %s",
        (priority.value, body, max_attempts, tx.tenant_id, kind, input_revision),
    ).fetchone()
    assert found is not None
    if not found[1]:
        raise IdempotencyConflict(f"{kind}:{input_revision} exists with other input")
    return found[0], False


def _check_lease(lease: timedelta, worker: str | None = None) -> None:
    if lease <= timedelta(0):
        raise ValueError("lease must be positive")
    if worker is not None and not 1 <= len(worker) <= MAX_WORKER_NAME:
        raise ValueError(f"worker name must be 1..{MAX_WORKER_NAME} characters")


def claim(conn: Conn, worker: str, pool: Pool, lease: timedelta) -> ClaimedJob | None:
    """Claim the next ready job of `pool` for `lease`, or None."""
    _check_lease(lease, worker)
    _idle(conn)
    token = f"{worker}:{uuid.uuid4().hex}"
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', '', true)")
        picked = conn.execute(
            "SELECT tenant_id, id FROM app.job_pick(%s) WHERE id IS NOT NULL",
            (Pool(pool).value,),
        ).fetchone()
        if picked is None:
            return None
        tenant_id, job_id = picked
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        row = conn.execute(
            "UPDATE app.job SET state = 'running', attempts = attempts + 1, "
            "total_attempts = total_attempts + 1, "
            "lease_owner = %s, lease_expires_at = now() + %s, heartbeat_at = now(), "
            "claimed_at = now(), updated_at = now() "
            "WHERE tenant_id = %s AND id = %s AND pool = %s AND state = 'queued' "
            "RETURNING kind, input_revision, priority::text, payload, attempts, "
            "max_attempts, lease_expires_at",
            (token, lease, tenant_id, job_id, Pool(pool).value),
        ).fetchone()
        assert row is not None  # locked by job_pick, still queued
    kind, revision, priority, payload, attempt, max_attempts, expires = row
    return ClaimedJob(
        job_id, tenant_id, kind, revision, Priority(priority), payload, attempt,
        max_attempts, token, expires,
    )  # fmt: skip


_OWNED = "tenant_id = %s AND id = %s AND state = 'running' AND lease_owner = %s"


def heartbeat(conn: Conn, job: ClaimedJob, lease: timedelta) -> datetime:
    """Extend a still-valid lease (never shortening it); returns the expiry or
    raises LeaseLost."""
    _check_lease(lease)
    with tenant_transaction(conn, job.tenant_id) as tx:
        row = tx.conn.execute(
            "UPDATE app.job SET heartbeat_at = now(), "
            "lease_expires_at = greatest(lease_expires_at, now() + %s), "
            f"updated_at = now() WHERE {_OWNED} AND lease_expires_at > now() "
            "RETURNING lease_expires_at",
            (lease, job.tenant_id, job.id, job.lease_token),
        ).fetchone()
    if row is None:
        raise LeaseLost(str(job.id))
    expires: datetime = row[0]
    return expires


def _finish(tx: TenantTx, job: ClaimedJob, state: JobState, *extra: object) -> None:
    cur = tx.conn.execute(
        "UPDATE app.job SET state = %s, lease_owner = NULL, lease_expires_at = NULL, "
        "next_available_at = now() + %s, last_error = %s, updated_at = now() "
        f"WHERE {_OWNED} AND lease_expires_at > now()",
        (state.value, *extra, job.tenant_id, job.id, job.lease_token),
    )
    if cur.rowcount != 1:
        raise LeaseLost(str(job.id))


def complete(tx: TenantTx, job: ClaimedJob) -> None:
    """Mark succeeded in the caller's transaction (with its business writes)."""
    _check_tenant(tx, job.tenant_id)
    _finish(tx, job, JobState.SUCCEEDED, timedelta(0), None)


def fail(
    tx: TenantTx,
    job: ClaimedJob,
    error_code: str,
    *,
    retryable: bool,
    backoff: Backoff = DEFAULT_BACKOFF,
    rng: Rng | None = None,
) -> JobState:
    """Record a failed attempt: queued with backoff, failed, or dead_letter."""
    _check_tenant(tx, job.tenant_id)
    state = after_failure(job.attempt, job.max_attempts, retryable)
    delay = _delay(state, job.attempt, backoff, rng)
    _finish(tx, job, state, delay, check_error_code(error_code))
    return state


def _delay(
    state: JobState, attempt: int, backoff: Backoff, rng: Rng | None
) -> timedelta:
    if state is not JobState.QUEUED:
        return timedelta(0)
    return backoff.delay(attempt, rng or random.SystemRandom())


def _check_tenant(tx: TenantTx, tenant_id: uuid.UUID) -> None:
    if tx.tenant_id != tenant_id:
        raise TenancyError("job belongs to another tenant")


def reap_expired(
    conn: Conn, backoff: Backoff = DEFAULT_BACKOFF, rng: Rng | None = None,
    limit: int = 100,
) -> list[tuple[uuid.UUID, JobState]]:  # fmt: skip
    """Requeue (with backoff) or dead-letter running jobs whose lease expired."""
    _idle(conn)
    done: list[tuple[uuid.UUID, JobState]] = []
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', '', true)")
        rows = conn.execute(
            "SELECT * FROM app.job_expired_leases(%s)", (limit,)
        ).fetchall()
        for tenant_id, job_id, attempts, max_attempts in rows:
            state = after_failure(attempts, max_attempts, retryable=True)
            conn.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),)
            )
            conn.execute(
                "UPDATE app.job SET state = %s, lease_owner = NULL, "
                "lease_expires_at = NULL, next_available_at = now() + %s, "
                "last_error = 'lease_expired', updated_at = now() "
                "WHERE tenant_id = %s AND id = %s AND state = 'running'",
                (state.value, _delay(state, attempts, backoff, rng), tenant_id, job_id),
            )
            done.append((job_id, state))
    return done


def requeue(tx: TenantTx, job_id: uuid.UUID) -> bool:
    """Operator action: dead_letter -> queued in a new generation with a fresh
    per-generation attempt budget; total_attempts and the idempotency key are kept
    (C-17). False when not dead-lettered or after MAX_GENERATION requeues."""
    cur = tx.conn.execute(
        "UPDATE app.job SET state = 'queued', attempts = 0, "
        "generation = generation + 1, next_available_at = now(), updated_at = now() "
        "WHERE tenant_id = %s "
        "AND id = %s AND state = 'dead_letter' AND generation < %s",
        (tx.tenant_id, job_id, MAX_GENERATION),
    )
    return cur.rowcount == 1


def cancel(tx: TenantTx, job_id: uuid.UUID) -> bool:
    cur = tx.conn.execute(
        "UPDATE app.job SET state = 'cancelled', updated_at = now() "
        "WHERE tenant_id = %s AND id = %s AND state = 'queued'",
        (tx.tenant_id, job_id),
    )
    return cur.rowcount == 1


def job_state(tx: TenantTx, job_id: uuid.UUID) -> tuple[JobState, int] | None:
    row = tx.conn.execute(
        "SELECT state::text, attempts FROM app.job WHERE tenant_id = %s AND id = %s",
        (tx.tenant_id, job_id),
    ).fetchone()
    return None if row is None else (JobState(row[0]), int(row[1]))


def write_outbox(tx: TenantTx, event_type: str, payload: Json) -> uuid.UUID:
    """Record an event in the caller's transaction, beside the business state."""
    event_id = uuid.uuid4()
    tx.conn.execute(
        "INSERT INTO app.outbox (id, tenant_id, event_type, payload) "
        "VALUES (%s, %s, %s, %s)",
        (event_id, tx.tenant_id, event_type, _payload(payload)),
    )
    return event_id


def consume(
    conn: Conn,
    consumer: str,
    event: OutboxEvent,
    handler: Callable[[TenantTx, OutboxEvent], None],
) -> bool:
    """Run `handler` once per (consumer, event id): the inbox row and the handler's
    writes commit together. False when the event was already processed. A
    concurrent duplicate waits on the first one's inbox row, then is skipped."""
    with tenant_transaction(conn, event.tenant_id) as tx:
        row = conn.execute(
            "INSERT INTO app.inbox (tenant_id, consumer, event_id) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING event_id",
            (tx.tenant_id, consumer, event.id),
        ).fetchone()
        if row is None:
            return False
        handler(tx, event)
    return True
