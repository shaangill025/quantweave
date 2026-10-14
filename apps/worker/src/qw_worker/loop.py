"""The worker loop (T025; spec 03 "Durable coordination"). One `tick` runs, in order:

1. `expire_overdue`: queued jobs past their start-by deadline become `expired`;
2. `reap_expired`: running jobs whose lease ran out are requeued or dead-lettered;
3. up to `jobs_per_tick` claims from the worker's pool, each run by the handler
   registered for its kind in a tenant transaction that also marks it succeeded;
4. only when a publish target is injected: up to `relay_per_tick` outbox events,
   each published and marked in its own transaction (`relay_outbox(limit=1)`), so a
   crash republishes at most the event in flight and no row lock is held across
   more than one publish. Without one the relay is off: events stay pending (never
   marked published, so never purged). No transport ships; adding one (network,
   email, broker) needs explicit owner authorisation;
5. the retention purges, when `purge_every` has passed on the database clock.

A worker claims only the kinds in its handler registry (`0021_job_pick_kinds.sql`):
any other kind stays queued, untouched, for a worker that handles it, and an empty
registry claims nothing. As a fail-closed fallback, a claimed job whose kind has no
handler is recorded as the retryable error `no_handler` (backoff, then
`dead_letter` at max_attempts, where an operator requeue can redrive it); it is
never marked succeeded. No handler is registered yet: no job-producing domain code
exists.

Stopping (`request_stop`, SIGTERM via `install_signal_handlers`) lets the current
job or event finish and claims nothing more. Delivery stays at-least-once.
"""

from __future__ import annotations

import logging
import signal
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import FrameType, MappingProxyType

from qw_adapters import jobs as qj
from qw_adapters.jobs import ClaimedJob, LeaseLost, OutboxEvent, Purged, Relayed
from qw_adapters.tenancy import Conn, TenantTx, tenant_transaction
from qw_domain.jobs import JobState, check_error_code

from qw_worker.config import WorkerSettings

log = logging.getLogger("qw_worker")
Handler = Callable[[TenantTx, ClaimedJob], None]
Publisher = Callable[[OutboxEvent], None]
Clock = Callable[[], datetime]
Sleep = Callable[[timedelta], None]
# Job kind -> handler. Empty: no domain handler exists yet (claims nothing).
HANDLERS: Mapping[str, Handler] = MappingProxyType({})


class HandlerFailed(Exception):
    """Raised by a handler to fail its attempt with a short code."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(check_error_code(code))
        self.code, self.retryable = code, retryable


def database_clock(conn: Conn) -> Clock:
    def now() -> datetime:
        row = conn.execute("SELECT now()").fetchone()
        assert row is not None
        stamp: datetime = row[0]
        return stamp

    return now


@dataclass
class Tick:
    expired: list[uuid.UUID] = field(default_factory=list)
    reaped: list[tuple[uuid.UUID, JobState]] = field(default_factory=list)
    jobs: list[tuple[uuid.UUID, JobState | None]] = field(default_factory=list)
    relayed: list[tuple[uuid.UUID, Relayed]] = field(default_factory=list)
    purged: Purged | None = None

    @property
    def busy(self) -> bool:
        return bool(self.expired or self.reaped or self.jobs or self.relayed)


class Worker:
    def __init__(
        self,
        conn: Conn,
        settings: WorkerSettings,
        *,
        handlers: Mapping[str, Handler] = HANDLERS,
        publish: Publisher | None = None,
        clock: Clock | None = None,
        sleep: Sleep = lambda wait: time.sleep(wait.total_seconds()),
    ) -> None:
        self.conn, self.settings = conn, settings
        self.handlers = MappingProxyType(dict(handlers))
        self.publish = publish
        self.clock = clock if clock is not None else database_clock(conn)
        self.sleep = sleep
        self.stopping = False
        self._last_purge: datetime | None = None

    def request_stop(self) -> None:
        self.stopping = True

    def tick(self) -> Tick:
        s, out = self.settings, Tick()
        if self.stopping:
            return out
        out.expired = qj.expire_overdue(self.conn)
        out.reaped = qj.reap_expired(self.conn, s.job_backoff)
        for _ in range(s.jobs_per_tick):
            if self.stopping or (done := self._run_one()) is None:
                break
            out.jobs.append(done)
        if (publish := self.publish) is not None:  # None: relay off, events pending
            for _ in range(s.relay_per_tick):
                if self.stopping:
                    break
                one = qj.relay_outbox(
                    self.conn, publish, limit=1,
                    max_attempts=s.relay_max_attempts, backoff=s.relay_backoff,
                )  # fmt: skip
                if not one:
                    break
                out.relayed += one
        now = self.clock()
        due = self._last_purge is None or now - self._last_purge >= s.purge_every
        if due and not self.stopping:
            out.purged = qj.purge(
                self.conn, outbox=s.outbox_retention, inbox=s.inbox_retention,
                idempotency=s.idempotency_retention,
            )  # fmt: skip
            self._last_purge = now
        return out

    def run(self, max_ticks: int | None = None) -> int:
        """Tick until stopped (or `max_ticks`); waits `idle_wait` after an idle
        tick. Returns the number of ticks."""
        ticks = 0
        while not self.stopping and (max_ticks is None or ticks < max_ticks):
            busy = self.tick().busy
            ticks += 1
            if not busy and not self.stopping:
                self.sleep(self.settings.idle_wait)
        return ticks

    def _run_one(self) -> tuple[uuid.UUID, JobState | None] | None:
        s = self.settings
        job = qj.claim(self.conn, s.worker_name, s.pool, s.lease, tuple(self.handlers))
        if job is None:
            return None
        handler = self.handlers.get(job.kind)
        code, retryable = "no_handler", True
        if handler is not None:
            try:
                with tenant_transaction(self.conn, job.tenant_id) as tx:
                    handler(tx, job)
                    qj.complete(tx, job)
                return job.id, JobState.SUCCEEDED
            except LeaseLost:
                log.warning("job %s lost its lease; its effects rolled back", job.id)
                return job.id, None
            except HandlerFailed as exc:
                code, retryable = exc.code, exc.retryable
            except Exception:
                log.exception("job %s (%s) handler failed", job.id, job.kind)
                code = "handler_failed"
        try:
            with tenant_transaction(self.conn, job.tenant_id) as tx:
                state = qj.fail(tx, job, code, retryable=retryable,
                                backoff=s.job_backoff)  # fmt: skip
        except LeaseLost:
            log.warning("job %s lost its lease before its failure was recorded", job.id)
            return job.id, None
        return job.id, state


def install_signal_handlers(worker: Worker) -> None:
    """SIGTERM and SIGINT stop the worker after the current unit of work."""

    def stop(signum: int, frame: FrameType | None) -> None:
        log.info("signal %s: stopping after the current unit", signum)
        worker.request_stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
