"""Job state machine and backoff (T025). Expected delays are hand-computed."""

import random
from datetime import timedelta

import pytest
from qw_domain import jobs
from qw_domain.jobs import TRANSITIONS, Backoff, JobError, JobState, Pool, Priority

S = JobState
LEGAL = {
    ("queued", "running"), ("queued", "cancelled"), ("queued", "expired"),
    ("running", "succeeded"), ("running", "failed"), ("running", "queued"),
    ("running", "dead_letter"), ("dead_letter", "queued"),
}  # fmt: skip


@pytest.mark.parametrize("src", list(S))
@pytest.mark.parametrize("dst", list(S))
def test_transitions_match_the_hand_written_table(src: S, dst: S) -> None:
    if (src.value, dst.value) in LEGAL:
        jobs.check_transition(src, dst)
    else:
        with pytest.raises(JobError, match="illegal"):
            jobs.check_transition(src, dst)


def test_terminal_states_and_priority_order() -> None:
    assert {s for s, nxt in TRANSITIONS.items() if not nxt} == {
        S.SUCCEEDED, S.FAILED, S.CANCELLED, S.EXPIRED,
    }  # fmt: skip
    assert [p.value for p in jobs.PRIORITY_ORDER] == [
        "safety", "monitoring", "interactive_research", "scheduled_research",
        "experiment",
    ]  # fmt: skip
    assert jobs.pool_for(Priority.EXPERIMENT) is Pool.EXPERIMENT
    assert {jobs.pool_for(p) for p in jobs.PRIORITY_ORDER[:4]} == {Pool.STANDARD}


def test_after_failure_dead_letters_at_max_attempts() -> None:
    assert jobs.after_failure(1, 3, retryable=True) is S.QUEUED
    assert jobs.after_failure(2, 3, retryable=True) is S.QUEUED
    assert jobs.after_failure(3, 3, retryable=True) is S.DEAD_LETTER
    assert jobs.after_failure(1, 3, retryable=False) is S.FAILED
    for bad in ((0, 3), (4, 3)):
        with pytest.raises(JobError):
            jobs.after_failure(*bad, retryable=True)


class Edge:
    """Stub RNG returning the low or high end of the requested range."""

    def __init__(self, high: bool) -> None:
        self.high = high

    def randint(self, a: int, b: int, /) -> int:
        return b if self.high else a


def test_backoff_against_hand_computed_values() -> None:
    policy = Backoff(base=timedelta(seconds=1), cap=timedelta(seconds=60))
    # steps 1, 2, 4, 8, 16, 32, then capped at 60 s
    high = [1, 2, 4, 8, 16, 32, 60, 60, 60]
    low_ms = [500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]
    for n, (hi, lo) in enumerate(zip(high, low_ms, strict=True), start=1):
        assert policy.delay(n, Edge(high=True)) == timedelta(seconds=hi)
        assert policy.delay(n, Edge(high=False)) == timedelta(milliseconds=lo)
    assert policy.delay(10_000, Edge(high=True)) == timedelta(seconds=60)
    # odd step: 3 us -> half 1, jitter in [0, 2]
    odd = Backoff(base=timedelta(microseconds=3), cap=timedelta(seconds=1))
    assert [odd.delay(1, Edge(h)) for h in (False, True)] == [
        timedelta(microseconds=1), timedelta(microseconds=3),
    ]  # fmt: skip
    assert Backoff(timedelta(0), timedelta(0)).delay(5, random.Random(1)) == timedelta(
        0
    )


def test_seeded_backoff_is_deterministic_and_bounded() -> None:
    policy = Backoff(base=timedelta(milliseconds=200), cap=timedelta(seconds=30))
    a = [policy.delay(n, random.Random(42)) for n in range(1, 12)]
    b = [policy.delay(n, random.Random(42)) for n in range(1, 12)]
    assert a == b
    rng = random.Random(7)
    for n in range(1, 40):
        step = min(30_000_000, 200_000 * 2 ** (n - 1))
        delay_us = policy.delay(n, rng) // timedelta(microseconds=1)
        assert step // 2 <= delay_us <= step


def test_rejections() -> None:
    with pytest.raises(JobError):
        Backoff(base=timedelta(seconds=2), cap=timedelta(seconds=1))
    with pytest.raises(JobError):
        Backoff(base=timedelta(seconds=-1), cap=timedelta(seconds=1))
    with pytest.raises(JobError):
        Backoff(timedelta(seconds=1), timedelta(seconds=2)).delay(0, Edge(True))
    assert jobs.check_error_code("lease_expired") == "lease_expired"
    for bad in ("", "Bad", "has space", "x" * 65, "1abc"):
        with pytest.raises(JobError):
            jobs.check_error_code(bad)
