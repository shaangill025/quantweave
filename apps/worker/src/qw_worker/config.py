"""Worker settings (T025): explicit values validated at construction, like
`qw_api.Settings`. The database URL is not a setting: it comes from QW_DATABASE_URL
in the operator's environment (`__main__`), so no secret lives in code or config.

Retention windows feed `qw_adapters.jobs.purge`, which also refuses windows below its
minimums. Their defaults are the configured retention defaults
(`qw_domain.retention_config`, owner decision 9): idempotency records 48 h (A-05: at
least 24 h), outbox 7 days, inbox 30 days. QW_RETENTION_CONFIG names an operator
JSON file (`retention_config.parse`) that sets the windows and purge holds; a held
window is not purged.
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
    INBOX_RETENTION_MIN,
    MAX_WORKER_NAME,
    OUTBOX_RETENTION_MIN,
    RELAY_BACKOFF,
    RELAY_MAX_ATTEMPTS,
)
from qw_domain.jobs import Backoff, Pool
from qw_domain.retention_config import DEFAULT_WINDOWS, Window, parse

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """`pool`: a worker serves one pool (experiments never run on standard workers).
    `jobs_per_tick` / `relay_per_tick` bound one iteration; every relayed event is
    published and marked in its own transaction. `purge_every` schedules the
    retention purges against the database clock; `budget_sweep_every` the
    budget-sweep timer of a standard-pool worker (owner decision 100), at most
    an hour so an overdue reservation is queued for its sweep within the hour
    after its tenant's last sweep slot."""

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
    budget_sweep_every: timedelta = timedelta(minutes=5)
    outbox_retention: timedelta = DEFAULT_WINDOWS[Window.OUTBOX]
    inbox_retention: timedelta = DEFAULT_WINDOWS[Window.INBOX]
    idempotency_retention: timedelta = DEFAULT_WINDOWS[Window.IDEMPOTENCY]
    purge_held: frozenset[Window] = frozenset()  # under a purge hold: not purged

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
        if not timedelta(minutes=1) <= self.budget_sweep_every <= timedelta(hours=1):
            raise ValueError("budget_sweep_every must be in [1 minute, 1 hour]")
        for name, window, least in (
            ("outbox_retention", self.outbox_retention, OUTBOX_RETENTION_MIN),
            ("inbox_retention", self.inbox_retention, INBOX_RETENTION_MIN),
            ("idempotency_retention", self.idempotency_retention,
             IDEMPOTENCY_RETENTION),
        ):  # fmt: skip
            if window < least:
                raise ValueError(f"{name} must be at least {least}")
        if type(self.purge_held) is not frozenset or not all(
                type(w) is Window for w in self.purge_held):  # fmt: skip
            raise ValueError("purge_held must be a frozenset of Window")


# Optional integer overrides from the environment; anything else keeps its default.
_ENV: Mapping[str, tuple[str, timedelta | None]] = {
    "QW_WORKER_JOBS_PER_TICK": ("jobs_per_tick", None),
    "QW_WORKER_RELAY_PER_TICK": ("relay_per_tick", None),
    "QW_WORKER_PURGE_EVERY_MINUTES": ("purge_every", timedelta(minutes=1)),
    "QW_WORKER_BUDGET_SWEEP_EVERY_MINUTES": ("budget_sweep_every",
                                             timedelta(minutes=1)),
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
    if (path := environ.get("QW_RETENTION_CONFIG")) is not None:
        if any(f.endswith("_retention") for f in values):
            raise ValueError("set retention windows in QW_RETENTION_CONFIG or the "
                             "QW_WORKER_*_RETENTION_* variables, not both")  # fmt: skip
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read(1 << 17)
        except OSError as exc:
            raise ValueError(
                f"QW_RETENTION_CONFIG unreadable: {exc.strerror}"
            ) from None
        cfg = parse(text)  # RetentionError is a ValueError
        values.update(outbox_retention=cfg.windows[Window.OUTBOX],
                      inbox_retention=cfg.windows[Window.INBOX],
                      idempotency_retention=cfg.windows[Window.IDEMPOTENCY],
                      purge_held=cfg.held())  # fmt: skip
    pool = Pool(environ.get("QW_WORKER_POOL", Pool.STANDARD.value))
    name = environ.get("QW_WORKER_NAME", f"worker-{os.getpid()}")
    return WorkerSettings(name, pool, **values)  # type: ignore[arg-type]
