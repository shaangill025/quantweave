"""Durable job state machine, priorities and retry backoff (T025; spec 03 "Durable
coordination"; T008 C-17).

Delivery is at-least-once, never exactly-once: handlers must be idempotent.

States: a claim moves `queued` to `running` and counts an attempt. A retryable
failure or an expired lease returns the job to `queued` with a later
`next_available_at` (the retry wait of C-17), or moves it to `dead_letter` once
`max_attempts` attempts were used. `dead_letter` leaves only by an operator requeue.
`succeeded`, `failed` (not retryable), `cancelled` and `expired` are terminal.
Stdlib only.
"""

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    DEAD_LETTER = "dead_letter"


class Priority(StrEnum):
    """Claim order, highest first (spec 03): safety invalidations/reconciliation,
    monitoring and required verification, interactive research, scheduled research,
    improvement experiments."""

    SAFETY = "safety"
    MONITORING = "monitoring"
    INTERACTIVE_RESEARCH = "interactive_research"
    SCHEDULED_RESEARCH = "scheduled_research"
    EXPERIMENT = "experiment"


PRIORITY_ORDER = tuple(Priority)


class Pool(StrEnum):
    """Experiments run in their own pool, never on standard workers."""

    STANDARD = "standard"
    EXPERIMENT = "experiment"


def pool_for(priority: Priority) -> Pool:
    return Pool.EXPERIMENT if priority is Priority.EXPERIMENT else Pool.STANDARD


S = JobState
TRANSITIONS: MappingProxyType[JobState, frozenset[JobState]] = MappingProxyType(
    {
        S.QUEUED: frozenset({S.RUNNING, S.CANCELLED, S.EXPIRED}),
        S.RUNNING: frozenset({S.SUCCEEDED, S.FAILED, S.QUEUED, S.DEAD_LETTER}),
        S.DEAD_LETTER: frozenset({S.QUEUED}),
        S.SUCCEEDED: frozenset(),
        S.FAILED: frozenset(),
        S.CANCELLED: frozenset(),
        S.EXPIRED: frozenset(),
    }
)


class JobError(Exception):
    """Illegal transition, bad error code or bad policy."""


def check_transition(src: JobState, dst: JobState) -> None:
    if dst not in TRANSITIONS[src]:
        raise JobError(f"illegal job transition {src} -> {dst}")


def after_failure(attempts: int, max_attempts: int, retryable: bool) -> JobState:
    """The state after attempt number `attempts` failed (or its lease expired)."""
    if not 1 <= attempts <= max_attempts:
        raise JobError(f"attempts {attempts} outside 1..{max_attempts}")
    if not retryable:
        return S.FAILED
    return S.DEAD_LETTER if attempts >= max_attempts else S.QUEUED


_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def check_error_code(code: str) -> str:
    """Errors are stored as short codes, never free text (no secrets in rows)."""
    if not _ERROR_CODE.fullmatch(code):
        raise JobError(f"error code must match {_ERROR_CODE.pattern}")
    return code


class Rng(Protocol):
    def randint(self, a: int, b: int, /) -> int: ...


_US = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class Backoff:
    """Capped exponential backoff with equal jitter, in whole microseconds.

    step(n) = min(cap, base * 2^(n-1)); delay(n) = step // 2 + U[0, step - step // 2]
    with U drawn from the injected RNG, so a seeded `random.Random` is deterministic
    and the wait is never under half the step (no synchronized retry storms)."""

    base: timedelta
    cap: timedelta

    def __post_init__(self) -> None:
        if not timedelta(0) <= self.base <= self.cap <= timedelta(days=1):
            raise JobError("backoff needs 0 <= base <= cap <= 1 day")

    def step(self, attempt: int) -> int:
        if attempt < 1:
            raise JobError("attempt numbers start at 1")
        base, cap = self.base // _US, self.cap // _US
        return min(cap, base << min(attempt - 1, 64))

    def delay(self, attempt: int, rng: Rng) -> timedelta:
        step = self.step(attempt)
        half = step // 2
        return timedelta(microseconds=half + rng.randint(0, step - half))
