"""Worker settings (T025): explicit values validated at construction, like
`qw_api.Settings`. The database URL is not a setting: it comes from QW_DATABASE_URL
in the operator's environment (`__main__`), so no secret lives in code or config.

Retention windows feed `qw_adapters.jobs.purge`, which also refuses windows below its
minimums. Idempotency records (A-05: at least 24 h) default to 48 h for margin.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from qw_adapters.jobs import (
    DEFAULT_BACKOFF,
    IDEMPOTENCY_RETENTION,
    INBOX_RETENTION,
    INBOX_RETENTION_MIN,
    MAX_WORKER_NAME,
    OUTBOX_RETENTION,
    OUTBOX_RETENTION_MIN,
    RELAY_BACKOFF,
    RELAY_MAX_ATTEMPTS,
)
from qw_domain.jobs import Backoff, Pool

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """`pool`: a worker serves one pool (experiments never run on standard workers).
    `jobs_per_tick` / `relay_per_tick` bound one iteration; every relayed event is
    published and marked in its own transaction. `purge_every` schedules the
    retention purges against the database clock."""

    worker_name: str
    pool: Pool = Pool.STANDARD
    lease: timedelta = timedelta(minutes=5)
    idle_wait: timedelta = timedelta(seconds=1)
    jobs_per_tick: int = 10
    relay_per_tick: int = 10
    relay_max_attempts: int = RELAY_MAX_ATTEMPTS
    job_backoff: Backoff = DEFAULT_BACKOFF
    relay_backoff: Backoff = RELAY_BACKOFF
    purge_every: timedelta = timedelta(hours=1)
    outbox_retention: timedelta = OUTBOX_RETENTION
    inbox_retention: timedelta = INBOX_RETENTION
    idempotency_retention: timedelta = 2 * IDEMPOTENCY_RETENTION  # 48 h

    def __post_init__(self) -> None:
        if not isinstance(self.pool, Pool):
            raise ValueError("pool must be a Pool")
        if not (
            1 <= len(self.worker_name) <= MAX_WORKER_NAME
            and _NAME.fullmatch(self.worker_name)
        ):
            raise ValueError(f"worker_name must be 1..{MAX_WORKER_NAME} [A-Za-z0-9._-]")
        if not timedelta(seconds=1) <= self.lease <= timedelta(hours=1):
            raise ValueError("lease must be in [1 s, 1 hour]")
        if not timedelta(0) < self.idle_wait <= timedelta(minutes=1):
            raise ValueError("idle_wait must be in (0, 1 minute]")
        if not 1 <= self.jobs_per_tick <= 1000 or not 1 <= self.relay_per_tick <= 1000:
            raise ValueError("jobs_per_tick and relay_per_tick must be 1..1000")
        if not 1 <= self.relay_max_attempts <= 100:
            raise ValueError("relay_max_attempts must be 1..100")
        if not timedelta(minutes=1) <= self.purge_every <= timedelta(days=1):
            raise ValueError("purge_every must be in [1 minute, 1 day]")
        for name, window, least in (
            ("outbox_retention", self.outbox_retention, OUTBOX_RETENTION_MIN),
            ("inbox_retention", self.inbox_retention, INBOX_RETENTION_MIN),
            ("idempotency_retention", self.idempotency_retention,
             IDEMPOTENCY_RETENTION),
        ):  # fmt: skip
            if window < least:
                raise ValueError(f"{name} must be at least {least}")


# Optional integer overrides from the environment; anything else keeps its default.
_ENV: Mapping[str, tuple[str, timedelta | None]] = {
    "QW_WORKER_JOBS_PER_TICK": ("jobs_per_tick", None),
    "QW_WORKER_RELAY_PER_TICK": ("relay_per_tick", None),
    "QW_WORKER_PURGE_EVERY_MINUTES": ("purge_every", timedelta(minutes=1)),
    "QW_WORKER_OUTBOX_RETENTION_DAYS": ("outbox_retention", timedelta(days=1)),
    "QW_WORKER_INBOX_RETENTION_DAYS": ("inbox_retention", timedelta(days=1)),
    "QW_WORKER_IDEMPOTENCY_RETENTION_HOURS": (
        "idempotency_retention", timedelta(hours=1)),
}  # fmt: skip


def settings_from_env(environ: Mapping[str, str] = os.environ) -> WorkerSettings:
    """QW_WORKER_NAME (default `worker-<pid>`), QW_WORKER_POOL (`standard` or
    `experiment`) and the integer overrides in `_ENV`; ValueError when invalid."""
    values: dict[str, object] = {}
    for key, (field, unit) in _ENV.items():
        raw = environ.get(key)
        if raw is None:
            continue
        if not (raw.isascii() and raw.isdigit()):
            raise ValueError(f"{key} must be a non-negative integer")
        try:  # int() refuses > 4300 digits; timedelta overflows past ~2.7e6 years
            values[field] = int(raw) if unit is None else unit * int(raw)
        except OverflowError as exc:
            raise ValueError(f"{key} is out of range") from exc
    pool = Pool(environ.get("QW_WORKER_POOL", Pool.STANDARD.value))
    name = environ.get("QW_WORKER_NAME", f"worker-{os.getpid()}")
    return WorkerSettings(name, pool, **values)  # type: ignore[arg-type]
