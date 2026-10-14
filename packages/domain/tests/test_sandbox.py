"""Sandbox runner port (T007): limits, requests, results and fail-closed execution.
All payloads are SYNTHETIC; digests are recomputed here with hashlib."""

import hashlib
from dataclasses import replace

import pytest
import qw_domain.sandbox as sb
from qw_domain.sandbox import EMPTY_STREAM, LimitHit, RunStatus, SandboxError

L = 1_000_000_000  # SYNTHETIC CPU limit of 1 s, in ns


def _req(**kw: object) -> sb.SandboxRequest:
    base = {"run_id": "cand-1", "argv": ("/bin/sh", "-c", "echo SYNTHETIC")} | kw
    return sb.SandboxRequest(**base)  # type: ignore[arg-type]


def _stream(data: bytes, cap: int) -> sb.StreamCapture:
    return sb.StreamCapture(data[:cap], len(data), hashlib.sha256(data).hexdigest())


def _exited(code: int = 0, out: bytes = b"SYNTHETIC\n") -> sb.SandboxResult:
    return sb.SandboxResult(
        "cand-1", RunStatus.EXITED, LimitHit.NONE, code, _stream(out, 1024),
        EMPTY_STREAM, "test-runtime", "",
    )  # fmt: skip


def test_default_limits_are_bounded_and_every_field_has_a_ceiling() -> None:
    limits = sb.SandboxLimits()
    assert set(sb.CEILINGS) == set(sb.SandboxLimits.__dataclass_fields__)
    for name, ceiling in sb.CEILINGS.items():
        assert 1 <= getattr(limits, name) <= ceiling


@pytest.mark.parametrize("name", sorted(sb.CEILINGS))
@pytest.mark.parametrize("bad", [0, -1, True, "5", None, "ceiling+1"])
def test_limits_reject_unbounded_or_non_integer_values(name: str, bad: object) -> None:
    value = sb.CEILINGS[name] + 1 if bad == "ceiling+1" else bad
    with pytest.raises(SandboxError) as exc:
        sb.SandboxLimits(**{name: value})  # type: ignore[arg-type]
    assert exc.value.code == "limit"


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"run_id": ""}, "run_id"),
        ({"run_id": "Has Space"}, "run_id"),
        ({"run_id": "x" * 65}, "run_id"),
        ({"argv": ()}, "argv"),
        ({"argv": ("sh", "-c", "true")}, "argv"),  # not absolute
        ({"argv": ("/bin/sh", "a\x00b")}, "argv"),
        ({"argv": ("/bin/sh", "x" * 9000)}, "argv"),
        ({"argv": ("/bin/true",) * 65}, "argv"),
        ({"argv": ["/bin/true"]}, "argv"),  # must be an immutable tuple
        ({"limits": {"wall_millis": 5}}, "limits"),
    ],
)
def test_requests_reject_malformed_input(kw: dict[str, object], code: str) -> None:
    with pytest.raises(SandboxError) as exc:
        _req(**kw)
    assert exc.value.code == code


def test_request_has_no_channel_for_env_mounts_or_network() -> None:
    assert set(sb.SandboxRequest.__dataclass_fields__) == {"run_id", "argv", "limits"}


@pytest.mark.parametrize(
    ("status", "timed_out", "oom", "pids", "cpu", "expected"),
    [
        (0, False, 0, 0, 1, (RunStatus.EXITED, LimitHit.NONE, 0)),
        (143, False, 0, 0, 1, (RunStatus.EXITED, LimitHit.NONE, 143)),  # SIGTERM
        (152, False, 0, 0, L - 1, (RunStatus.EXITED, LimitHit.NONE, 152)),  # own exit
        (None, True, 0, 0, 1, (RunStatus.LIMIT, LimitHit.WALL_TIME, None)),
        (137, True, 1, 0, 2 * L, (RunStatus.LIMIT, LimitHit.WALL_TIME, 137)),
        (None, False, 1, 0, 1, (RunStatus.LIMIT, LimitHit.MEMORY, None)),
        (137, False, 2, 0, 2 * L, (RunStatus.LIMIT, LimitHit.MEMORY, 137)),
        (152, False, 0, 0, L, (RunStatus.LIMIT, LimitHit.CPU_TIME, 152)),  # SIGXCPU
        (137, False, 0, 0, 2 * L, (RunStatus.LIMIT, LimitHit.CPU_TIME, 137)),  # trapped
        (
            0,
            False,
            0,
            0,
            L + 5,
            (RunStatus.LIMIT, LimitHit.CPU_TIME, 0),
        ),  # trap: exit 0
        (1, False, 0, 4, 1, (RunStatus.LIMIT, LimitHit.PIDS, 1)),
        (None, False, 0, 0, 1, (RunStatus.ERROR, LimitHit.NONE, None)),
        (None, False, None, None, None, (RunStatus.ERROR, LimitHit.NONE, None)),
        # evidence unreadable: a limit cannot be ruled out, so never a clean exit
        (137, False, None, 0, 1, (RunStatus.ERROR, LimitHit.NONE, 137)),
        (0, False, 0, None, 1, (RunStatus.ERROR, LimitHit.NONE, 0)),
        (0, False, 0, 0, None, (RunStatus.ERROR, LimitHit.NONE, 0)),
        (None, True, None, None, None, (RunStatus.LIMIT, LimitHit.WALL_TIME, None)),
    ],
)
def test_classify_names_the_limit_and_never_reads_unknown_as_success(
    status: int | None, timed_out: bool, oom: int | None, pids: int | None,
    cpu: int | None, expected: tuple[RunStatus, LimitHit, int | None],
) -> None:  # fmt: skip
    evidence = {"oom_kills": oom, "pids_max_events": pids, "cpu_ns": cpu}
    got = sb.classify(
        exit_status=status, timed_out=timed_out, cpu_limit_ns=L, **evidence
    )
    assert got == expected


def test_stream_capture_reports_truncation_and_rejects_impossible_shapes() -> None:
    data = b"y\n" * 1000  # SYNTHETIC
    cap = _stream(data, 100)
    assert cap.truncated and len(cap.head) == 100 and cap.total_bytes == 2000
    assert not _stream(b"ok", 100).truncated
    with pytest.raises(SandboxError):
        sb.StreamCapture(b"abc", 2, hashlib.sha256(b"abc").hexdigest())
    with pytest.raises(SandboxError):
        sb.StreamCapture(b"", 0, "not-a-digest")


def test_result_invariants_and_blocked_shape() -> None:
    ok = _exited()
    with pytest.raises(SandboxError):
        replace(ok, limit_hit=LimitHit.MEMORY)  # exited but a limit named
    with pytest.raises(SandboxError):
        replace(ok, status=RunStatus.LIMIT)  # limit status without a limit
    with pytest.raises(SandboxError):
        replace(ok, exit_code=None)  # exited without a code
    with pytest.raises(SandboxError):
        replace(sb.blocked("cand-1", "runsc_unavailable"), exit_code=0)
    with pytest.raises(SandboxError):
        sb.blocked("cand-1", "")  # blocked needs a reason
    with pytest.raises(SandboxError):
        replace(ok, status=RunStatus.ERROR, reason="")
    r = sb.blocked("cand-1", "runsc_unavailable: not found")
    assert r.status is RunStatus.BLOCKED and r.exit_code is None
    assert r.limit_hit is LimitHit.NONE and r.reason.startswith("runsc_unavailable")
    assert r.stdout == r.stderr == EMPTY_STREAM
    assert EMPTY_STREAM.sha256 == hashlib.sha256(b"").hexdigest()


def test_digest_is_stable_and_covers_every_field() -> None:
    a, b = _exited(), _exited()
    assert a.digest() == b.digest() and len(a.digest()) == 64
    variants = [
        _exited(code=1),
        _exited(out=b"SYNTHETIC!\n"),
        replace(a, run_id="cand-2"),
        replace(a, runtime="other"),
        replace(a, stderr=_stream(b"e", 10)),
        replace(a, status=RunStatus.LIMIT, limit_hit=LimitHit.CPU_TIME),
        replace(a, status=RunStatus.ERROR, reason="x"),
    ]
    assert len({v.digest() for v in variants} | {a.digest()}) == len(variants) + 1


class _FakeRunner:
    def __init__(self, gate: sb.SandboxGate) -> None:
        self.gate, self.ran = gate, 0

    def probe(self) -> sb.SandboxGate:
        return self.gate

    def run(self, request: sb.SandboxRequest) -> sb.SandboxResult:
        self.ran += 1
        return _exited()


def test_execute_fails_closed_without_running_when_the_gate_is_blocked() -> None:
    down = _FakeRunner(sb.SandboxGate(False, "none", "runsc_unavailable: missing"))
    r = sb.execute(down, _req())
    assert down.ran == 0
    assert r.status is RunStatus.BLOCKED and r.reason == "runsc_unavailable: missing"
    up = _FakeRunner(sb.SandboxGate(True, "fake", ""))
    assert sb.execute(up, _req()).status is RunStatus.EXITED and up.ran == 1
    with pytest.raises(SandboxError):
        sb.SandboxGate(False, "none", "")  # a blocked gate always says why
