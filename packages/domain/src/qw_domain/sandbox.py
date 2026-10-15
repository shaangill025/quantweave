"""Sandbox runner port for untrusted candidate code (T007; spec §11 "Sandboxed
execution", §12 "Sandbox and supply chain"; R038, R076; QUAL17).

A request carries only a run id, an argv and bounded int limits: there is no field for
env, mounts, network or credentials. A result is exited, limit_exceeded (naming the
limit), blocked (nothing ran) or runtime_error; unreadable limit evidence is an error,
never a clean exit. Outputs keep a bounded head plus the full stream's size and
SHA-256. `execute` returns `blocked` without running when the probe fails; there is no
weaker fallback (§11). Passing these tests does not qualify the release gate
(capabilities.Reason.SANDBOX). Adapter: qw_adapters.gvisor_sandbox. Stdlib only.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

MIB = 1024 * 1024
CEILINGS: dict[str, int] = {
    "cpu_millicores": 8000, "cpu_seconds": 3600, "memory_bytes": 8192 * MIB,
    "wall_millis": 3_600_000, "pids": 1024, "scratch_bytes": 1024 * MIB,
    "output_bytes": 4 * MIB,
}  # fmt: skip
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_ARGS, MAX_ARGV_CHARS = 64, 8192


class SandboxError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class RunStatus(StrEnum):
    EXITED = "exited"
    LIMIT = "limit_exceeded"
    BLOCKED = "blocked"
    ERROR = "runtime_error"


class LimitHit(StrEnum):
    NONE = "none"
    WALL_TIME = "wall_time"
    CPU_TIME = "cpu_time"
    MEMORY = "memory"
    PIDS = "pids"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    cpu_millicores: int = 1000
    cpu_seconds: int = 10
    memory_bytes: int = 256 * MIB
    wall_millis: int = 30_000
    pids: int = 32
    scratch_bytes: int = 16 * MIB
    output_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for name, ceiling in CEILINGS.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise SandboxError("limit", f"{name} must be an int in 1..{ceiling}")


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    run_id: str
    argv: tuple[str, ...]
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _RUN_ID.match(self.run_id):
            raise SandboxError("run_id", "lowercase [a-z0-9-], at most 64")
        argv = self.argv
        if (
            type(argv) is not tuple
            or not 1 <= len(argv) <= MAX_ARGS
            or not all(isinstance(a, str) and "\x00" not in a for a in argv)
            or sum(len(a) for a in argv) > MAX_ARGV_CHARS
            or not argv[0].startswith("/")
        ):
            raise SandboxError("argv", "1..64 NUL-free strings, absolute argv[0]")
        if not isinstance(self.limits, SandboxLimits):
            raise SandboxError("limits", "SandboxLimits required")


@dataclass(frozen=True, slots=True)
class StreamCapture:
    head: bytes
    total_bytes: int
    sha256: str  # of the full stream, not only the head

    def __post_init__(self) -> None:
        sha_ok = _SHA256.match(self.sha256)
        if not sha_ok or not 0 <= len(self.head) <= self.total_bytes:
            raise SandboxError("stream", "head within total and a sha256 digest")

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.head)


EMPTY_STREAM = StreamCapture(b"", 0, hashlib.sha256(b"").hexdigest())


@dataclass(frozen=True, slots=True)
class SandboxResult:
    run_id: str
    status: RunStatus
    limit_hit: LimitHit
    exit_code: int | None
    stdout: StreamCapture
    stderr: StreamCapture
    runtime: str  # runtime name, version and platform that actually ran it
    reason: str  # why it was blocked or failed; empty otherwise

    def __post_init__(self) -> None:
        s, hit = self.status, self.limit_hit
        ok = {
            RunStatus.EXITED: hit is LimitHit.NONE and self.exit_code is not None,
            RunStatus.LIMIT: hit is not LimitHit.NONE,
            RunStatus.BLOCKED: hit is LimitHit.NONE
            and self.exit_code is None
            and self.stdout == self.stderr == EMPTY_STREAM
            and bool(self.reason),
            RunStatus.ERROR: hit is LimitHit.NONE and bool(self.reason),
        }[s]
        if not ok:
            raise SandboxError("result", f"inconsistent {s.value} result")

    def digest(self) -> str:
        """SHA-256 of the canonical record, including captured heads and digests."""
        out, err = self.stdout, self.stderr
        record = {
            "run_id": self.run_id, "status": self.status.value,
            "limit_hit": self.limit_hit.value, "exit_code": self.exit_code,
            "runtime": self.runtime, "reason": self.reason,
            "stdout": [out.head.hex(), out.total_bytes, out.sha256],
            "stderr": [err.head.hex(), err.total_bytes, err.sha256],
        }  # fmt: skip
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SandboxGate:
    available: bool
    runtime: str
    reason: str

    def __post_init__(self) -> None:
        if not self.available and not self.reason:
            raise SandboxError("gate", "a blocked gate states its reason")


class SandboxRunner(Protocol):
    def probe(self) -> SandboxGate: ...

    def run(self, request: SandboxRequest) -> SandboxResult: ...


def blocked(run_id: str, reason: str, runtime: str = "none") -> SandboxResult:
    return SandboxResult(
        run_id, RunStatus.BLOCKED, LimitHit.NONE, None, EMPTY_STREAM, EMPTY_STREAM,
        runtime, reason,
    )  # fmt: skip


def classify(
    *,
    exit_status: int | None,
    timed_out: bool,
    oom_kills: int | None,
    pids_max_events: int | None,
    cpu_ns: int | None,
    cpu_limit_ns: int,
) -> tuple[RunStatus, LimitHit, int | None]:
    """CPU time is measured usage (runtime overhead included), never the exit code.
    `None` evidence is unreadable: only a host-observed wall timeout then counts."""
    if timed_out:
        return RunStatus.LIMIT, LimitHit.WALL_TIME, exit_status
    if oom_kills is None or pids_max_events is None or cpu_ns is None:
        return RunStatus.ERROR, LimitHit.NONE, exit_status
    if oom_kills > 0:
        return RunStatus.LIMIT, LimitHit.MEMORY, exit_status
    if cpu_ns >= cpu_limit_ns:
        return RunStatus.LIMIT, LimitHit.CPU_TIME, exit_status
    if pids_max_events > 0:
        return RunStatus.LIMIT, LimitHit.PIDS, exit_status
    if exit_status is None:
        return RunStatus.ERROR, LimitHit.NONE, None
    return RunStatus.EXITED, LimitHit.NONE, exit_status


def execute(runner: SandboxRunner, request: SandboxRequest) -> SandboxResult:
    gate = runner.probe()
    if not gate.available:
        return blocked(request.run_id, gate.reason, gate.runtime)
    return runner.run(request)
