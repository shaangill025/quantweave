"""gVisor (runsc) adapter for the sandbox runner port (T007; spec §11, §12; R038,
R076). runsc is a host tool, not a dependency: `QW_SANDBOX_RUNSC` (or PATH), the
immutable rootfs `QW_SANDBOX_ROOTFS`, `QW_SANDBOX_PLATFORM` (systrap or ptrace).

A foreground `runsc --network=none run` of an OCI bundle (exit code = candidate's):
read-only root; only proc/tmpfs mounts (no host path, socket or key); read-only /dev
and /dev/shm; /tmp the one capped noexec scratch dir; uid 65534, no capabilities, a
fixed env; guest RLIMIT_CPU/NPROC. runsc joins an existing cgroup without setting its
limits, so the adapter creates the run's cgroup v1 dirs, writes and verifies memory
(+swap), pids and CPU quota, and reads oom_kill, pids events and cpuacct.usage after
the run. Probe failures return `blocked` (no fallback); every cleanup step runs and
any failure makes the result an error. Needs host root.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import qw_domain.sandbox as sb
from qw_domain.sandbox import RunStatus, SandboxRequest, SandboxResult, StreamCapture

GUEST_ENV = ("PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/tmp", "LANG=C.UTF-8")
HOST_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
NOBODY = 65534
SENTRY_PIDS_HEADROOM = 256  # host threads of the Sentry and gofer, not guest tasks
CPU_PERIOD_US = 100_000
PLATFORMS = frozenset({"systrap", "ptrace"})
CONTROL_TIMEOUT_S = 30
CONTROLLERS = ("memory", "pids", "cpu", "cpuacct")  # cgroup v1
MEMINFO = Path("/proc/meminfo")
_CAPS = ("bounding", "effective", "inheritable", "permitted", "ambient")
_NAMESPACES = ("pid", "network", "ipc", "uts", "mount")
log = logging.getLogger(__name__)


def _tmpfs(dest: str, *opts: str) -> dict[str, Any]:
    opts = ("nosuid", "nodev", "noexec", *opts)
    return {"destination": dest, "type": "tmpfs", "source": "tmpfs", "options": opts}


def oci_config(request: SandboxRequest, rootfs: Path, cgroup: str) -> dict[str, Any]:
    lim = request.limits
    rlimits = [
        {"type": "RLIMIT_CPU", "soft": lim.cpu_seconds, "hard": lim.cpu_seconds + 1},
        {"type": "RLIMIT_NPROC", "soft": lim.pids, "hard": lim.pids},
    ]
    process = {
        "terminal": False, "user": {"uid": NOBODY, "gid": NOBODY},
        "args": list(request.argv), "env": list(GUEST_ENV), "cwd": "/tmp",
        "noNewPrivileges": True, "capabilities": {k: [] for k in _CAPS},
        "rlimits": rlimits,
    }  # fmt: skip
    mounts = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        _tmpfs("/dev", "ro", "mode=755"),
        _tmpfs("/dev/shm", "ro", "size=4k"),
        _tmpfs("/tmp", "mode=1777", f"size={lim.scratch_bytes}"),
    ]
    linux = {"namespaces": [{"type": t} for t in _NAMESPACES], "cgroupsPath": cgroup}
    return {
        "ociVersion": "1.0.2", "process": process, "hostname": "candidate",
        "root": {"path": str(rootfs), "readonly": True}, "mounts": mounts,
        "linux": linux,
    }  # fmt: skip


def cgroup_limits(limits: sb.SandboxLimits) -> dict[str, int]:
    """cgroup v1 file -> value, in write order (memsw only after the memory limit)."""
    return {
        "memory/memory.limit_in_bytes": limits.memory_bytes,
        "memory/memory.memsw.limit_in_bytes": limits.memory_bytes,
        "pids/pids.max": SENTRY_PIDS_HEADROOM + limits.pids,
        "cpu/cpu.cfs_period_us": CPU_PERIOD_US,
        "cpu/cpu.cfs_quota_us": limits.cpu_millicores * CPU_PERIOD_US // 1000,
    }


def drain(stream: IO[bytes], limit: int) -> StreamCapture:
    """Read to EOF, keeping at most `limit` bytes and hashing everything."""
    head, total, digest = bytearray(), 0, hashlib.sha256()
    while chunk := stream.read(65536):
        total += len(chunk)
        digest.update(chunk)
        if len(head) < limit:
            head += chunk[: limit - len(head)]
    return StreamCapture(bytes(head), total, digest.hexdigest())


def _counter(path: Path, key: str | None) -> int | None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for name, *values in (line.split() for line in lines):
        if key is None and name.isdigit():  # single-value file
            return int(name)
        if name == key and values and values[0].isdigit():
            return int(values[0])
    return None


def read_counters(cgroups: Path, cgroup: str) -> tuple[int | None, ...]:
    """(oom kills, pids-limit events, CPU ns) from cgroup v1; None if unreadable."""
    rel = cgroup.lstrip("/")
    return (
        _counter(cgroups.joinpath("memory", rel, "memory.oom_control"), "oom_kill"),
        _counter(cgroups.joinpath("pids", rel, "pids.events"), "max"),
        _counter(cgroups.joinpath("cpuacct", rel, "cpuacct.usage"), None),
    )


def _ceil_s(millis: int) -> int:
    return -(-millis // 1000)


@dataclass(frozen=True, slots=True)
class GVisorConfig:
    runsc: Path | None
    rootfs: Path | None
    state_dir: Path
    platform: str = "systrap"
    cgroup_root: Path = Path("/sys/fs/cgroup")
    cgroup_parent: str = "qw-sandbox"

    @classmethod
    def from_env(cls, state_dir: Path) -> "GVisorConfig":
        runsc = os.environ.get("QW_SANDBOX_RUNSC") or shutil.which("runsc")
        rootfs = os.environ.get("QW_SANDBOX_ROOTFS")
        platform = os.environ.get("QW_SANDBOX_PLATFORM", "systrap")
        return cls(
            Path(runsc) if runsc else None, Path(rootfs) if rootfs else None,
            state_dir, platform,
        )  # fmt: skip


def runsc_argv(config: GVisorConfig, *args: str) -> list[str]:
    """The only global flags: state root, no network, the platform."""
    return [str(config.runsc), "--root", str(config.state_dir), "--network=none",
            f"--platform={config.platform}", *args]  # fmt: skip


def setup_cgroups(
    config: GVisorConfig, cid: str, limits: sb.SandboxLimits, made: list[Path]
) -> None:
    """Create the run's cgroups (appending to `made`), write and verify the limits."""
    for ctrl in CONTROLLERS:
        path = config.cgroup_root.joinpath(ctrl, config.cgroup_parent, cid)
        if path.resolve() not in made:  # cpu and cpuacct may be one hierarchy
            path.mkdir(parents=True)
            made.append(path.resolve())
    for rel, value in cgroup_limits(limits).items():
        ctrl, name = rel.split("/")
        file = config.cgroup_root.joinpath(ctrl, config.cgroup_parent, cid, name)
        if name.startswith("memory.memsw") and not file.exists():
            if _counter(MEMINFO, "SwapTotal:") != 0:
                raise OSError("swap is enabled but not accounted (swapaccount=0)")
            continue
        file.write_text(str(value))
        if _counter(file, None) != value:
            raise OSError(f"{rel} not applied")


class _Drainer(threading.Thread):
    def __init__(self, stream: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self.stream, self.limit = stream, limit
        self.capture: StreamCapture | None = None

    def run(self) -> None:
        self.capture = drain(self.stream, self.limit)


class GVisorRunner:
    def __init__(self, config: GVisorConfig) -> None:
        self.config = config

    def _control(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            runsc_argv(self.config, *args), capture_output=True,
            timeout=CONTROL_TIMEOUT_S, env=HOST_ENV, stdin=subprocess.DEVNULL,
            check=False,
        )  # fmt: skip

    def probe(self) -> sb.SandboxGate:
        c = self.config

        def no(reason: str) -> sb.SandboxGate:
            return sb.SandboxGate(False, "none", reason)

        if c.runsc is None or not os.access(c.runsc, os.X_OK) or c.runsc.is_dir():
            return no(f"runsc_unavailable: {c.runsc}")
        if c.rootfs is None or not c.rootfs.joinpath("bin").is_dir():
            return no(f"rootfs_unavailable: {c.rootfs}")
        if c.rootfs.resolve() == Path("/"):
            return no("rootfs_is_host_root")
        if c.platform not in PLATFORMS:
            return no(f"platform_unsupported: {c.platform}")
        try:
            out = subprocess.run(
                [str(c.runsc), "--version"], capture_output=True, timeout=10,
                env=HOST_ENV, stdin=subprocess.DEVNULL, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            return no(f"runsc_version_failed: {exc}")
        first = out.stdout.decode(errors="replace").partition("\n")[0].split()
        if out.returncode != 0 or first[:2] != ["runsc", "version"] or len(first) < 3:
            return no(f"runsc_version_failed: {first}")
        cg = c.cgroup_root
        missing = [x for x in CONTROLLERS if not cg.joinpath(x, "tasks").exists()]
        if missing:
            return no(f"cgroup_v1_unavailable: {missing}")
        if os.geteuid() != 0:
            return no("runsc_requires_root: cgroup limits")
        return sb.SandboxGate(
            True, f"gvisor runsc {first[2]} platform={c.platform}", ""
        )

    def close(self) -> None:
        """Unmount the netns file `--network=none` pins once per state dir."""
        netns = self.config.state_dir.joinpath("null-netns")
        if os.path.ismount(netns):
            subprocess.run(["umount", str(netns)], env=HOST_ENV, check=True)

    def run(self, request: SandboxRequest) -> SandboxResult:
        gate = self.probe()
        if not gate.available:
            return sb.blocked(request.run_id, gate.reason, gate.runtime)
        c, lim = self.config, request.limits
        assert c.rootfs is not None  # the probe checked it
        c.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cid = f"qw-{request.run_id}-{uuid.uuid4().hex[:12]}"
        cgroup = f"/{c.cgroup_parent}/{cid}"
        bundle = Path(tempfile.mkdtemp(prefix="bundle-", dir=c.state_dir))
        pid_file = bundle.joinpath("pid")
        made: list[Path] = []
        drains: list[_Drainer] = []
        proc: subprocess.Popen[bytes] | None = None
        problems: list[str] = []
        exit_status: int | None = None
        counters: tuple[int | None, ...] = (None, None, None)
        timed_out = started = False
        try:
            config = oci_config(request, c.rootfs, cgroup)
            bundle.joinpath("config.json").write_text(json.dumps(config))
            setup_cgroups(c, cid, lim, made)
            proc = subprocess.Popen(
                runsc_argv(c, "run", "--pid-file", str(pid_file), "--bundle",
                           str(bundle), cid),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=HOST_ENV, close_fds=True,
            )  # fmt: skip
            assert proc.stdout is not None and proc.stderr is not None
            drains = [_Drainer(s, lim.output_bytes) for s in (proc.stdout, proc.stderr)]
            for d in drains:
                d.start()
            try:
                exit_status = proc.wait(timeout=_ceil_s(lim.wall_millis))
            except subprocess.TimeoutExpired:
                timed_out = True
                self._control("kill", "--all", cid, "KILL")
                exit_status = proc.wait(timeout=CONTROL_TIMEOUT_S)
            started = pid_file.exists() and pid_file.read_text().strip() != ""
            counters = read_counters(c.cgroup_root, cgroup)
        except (OSError, subprocess.SubprocessError) as exc:
            problems.append(f"runtime: {exc!r}")
        finally:
            problems += self._cleanup(cid, proc, drains, bundle, made)
        out = drains[0].capture if drains else None
        err = drains[1].capture if drains else None
        if problems:
            reason = "; ".join(problems)
        elif out is None or err is None:
            reason = "output_drain_incomplete"
        elif not started and not timed_out:
            reason = "runsc_start_failed: " + err.head[:300].decode(errors="replace")
        else:
            oom, pids, cpu = counters
            status, hit, code = sb.classify(
                exit_status=exit_status, timed_out=timed_out, oom_kills=oom,
                pids_max_events=pids, cpu_ns=cpu,
                cpu_limit_ns=lim.cpu_seconds * 1_000_000_000,
            )  # fmt: skip
            if status is not RunStatus.ERROR:
                return SandboxResult(
                    request.run_id, status, hit, code, out, err, gate.runtime, ""
                )
            gap = "limit_evidence" if None in counters else "exit_status"
            reason = f"{gap}_unavailable"
        out, err = out or sb.EMPTY_STREAM, err or sb.EMPTY_STREAM
        return SandboxResult(
            request.run_id, RunStatus.ERROR, sb.LimitHit.NONE, exit_status, out, err,
            gate.runtime, reason.strip(),
        )  # fmt: skip

    def _cleanup(self, cid: str, proc: "subprocess.Popen[bytes] | None",
                 drains: list[_Drainer], bundle: Path, made: list[Path],
                 ) -> list[str]:  # fmt: skip
        """Run every step even when an earlier one fails; return the failures."""
        problems: list[str] = []

        def step(name: str, action: Any) -> None:
            try:
                action()
            except Exception as exc:  # each step is independent
                problems.append(f"cleanup_failed: {name}: {exc!r}")

        def stop() -> None:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=CONTROL_TIMEOUT_S)

        def gone() -> None:
            # `runsc run` deletes its container on exit, so a failed delete is
            # expected; only a container still listed afterwards is a problem.
            deleted = self._control("delete", "--force", cid)
            if cid in self._control("list").stdout.decode(errors="replace"):
                raise RuntimeError(f"still listed: {deleted.stderr[-200:]!r}")

        step("stop", stop)
        step("delete", gone)
        for d in drains:
            step("drain", lambda d=d: d.join(CONTROL_TIMEOUT_S))
        step("bundle", lambda: shutil.rmtree(bundle))
        for path in reversed(made):
            step("cgroup", path.rmdir)
        for p in problems:
            log.error("sandbox %s: %s", cid, p)
        return problems
