"""gVisor sandbox adapter (T007). Unit tests always run. `gvisor` tests run SYNTHETIC
commands under runsc and need root, `QW_SANDBOX_RUNSC` (or runsc on PATH) and a busybox
`QW_SANDBOX_ROOTFS`; otherwise they skip (a skip is not a pass), or fail when
`QW_REQUIRE_GVISOR=1`."""

import hashlib
import io
import json
import os
import re
import socket
import stat
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import qw_adapters.gvisor_sandbox as gv
import qw_domain.sandbox as sb
from qw_adapters.gvisor_sandbox import GVisorConfig, GVisorRunner
from qw_domain.sandbox import LimitHit, RunStatus, SandboxLimits, SandboxRequest

CANARY_ENV = "QW_T007_SYNTHETIC_CANARY_API_KEY"
CANARY = "synthetic-canary-not-a-real-key-7f3a"


def _req(script: str, run_id: str = "t007", **limits: int) -> SandboxRequest:
    return SandboxRequest(run_id, ("/bin/sh", "-c", script), SandboxLimits(**limits))


def test_oci_config_has_no_host_mounts_secrets_network_or_privileges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CANARY_ENV, CANARY)
    limits = SandboxLimits(
        cpu_millicores=250, cpu_seconds=7, memory_bytes=64 << 20, pids=9,
        scratch_bytes=3 << 20,
    )  # fmt: skip
    req = SandboxRequest("c1", ("/bin/true",), limits)
    cfg = gv.oci_config(req, tmp_path, "/qw/c1")
    assert CANARY not in json.dumps(cfg)
    proc, linux = cfg["process"], cfg["linux"]
    assert cfg["root"] == {"path": str(tmp_path), "readonly": True}
    assert proc["env"] == list(gv.GUEST_ENV)
    assert proc["user"] == {"uid": 65534, "gid": 65534}
    assert proc["noNewPrivileges"] is True
    assert all(v == [] for v in proc["capabilities"].values())
    assert len(proc["capabilities"]) == 5
    mounts = {m["destination"]: m for m in cfg["mounts"]}
    assert set(mounts) == {"/proc", "/dev", "/dev/shm", "/tmp"}
    for m in cfg["mounts"]:  # no bind mount, no host source path
        assert m["type"] in {"proc", "tmpfs"} and m["source"] == m["type"]
        assert not {"bind", "rbind"} & set(m.get("options", []))
    assert "ro" in mounts["/dev"]["options"] and "ro" in mounts["/dev/shm"]["options"]
    assert f"size={3 << 20}" in mounts["/tmp"]["options"]
    assert "noexec" in mounts["/tmp"]["options"]
    assert all(set(n) == {"type"} for n in linux["namespaces"])  # no `path` to join
    namespaces = sorted(n["type"] for n in linux["namespaces"])
    assert namespaces == ["ipc", "mount", "network", "pid", "uts"]
    rl = {r["type"]: (r["soft"], r["hard"]) for r in proc["rlimits"]}
    assert rl["RLIMIT_NPROC"] == (9, 9) and rl["RLIMIT_CPU"] == (7, 8)
    assert linux == {"namespaces": linux["namespaces"], "cgroupsPath": "/qw/c1"}
    guest = {"/proc", "/dev", "/dev/shm", "/tmp", "/bin/true", "/qw/c1"}
    paths = set(re.findall(r'"(/[^"]*)"', json.dumps(cfg)))  # every path string
    assert paths - guest == {str(tmp_path)}  # the rootfs is the only host path


def test_runsc_argv_is_exactly_the_isolating_flag_set(tmp_path: Path) -> None:
    cfg = GVisorConfig(tmp_path / "runsc", tmp_path / "rootfs", tmp_path / "st")
    argv = gv.runsc_argv(cfg, "run", "x")
    assert argv == [str(tmp_path / "runsc"), "--root", str(tmp_path / "st"),
                    "--network=none", "--platform=systrap", "run", "x"]  # fmt: skip
    for bad in ("--host-uds", "--rootless", "--network=host", "--ignore-cgroups"):
        assert not any(a.startswith(bad) for a in argv)


def test_setup_cgroups_writes_and_verifies_the_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"  # SYNTHETIC; a fake cgroup root has no memsw file
    monkeypatch.setattr(gv, "MEMINFO", meminfo)
    limits = SandboxLimits(cpu_millicores=250, memory_bytes=64 << 20, pids=9)
    meminfo.write_text("SwapTotal:       2048 kB\n")  # swap on but not accounted
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=tmp_path / "cg1")
    with pytest.raises(OSError, match="swap"):
        gv.setup_cgroups(cfg, "c1", limits, [])
    meminfo.write_text("SwapTotal:       0 kB\n")
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=tmp_path / "cg")
    made: list[Path] = []
    gv.setup_cgroups(cfg, "c1", limits, made)
    cg = cfg.cgroup_root
    got = {f.relative_to(cg).as_posix(): f.read_text() for f in cg.rglob("*.*")}
    assert got == {
        "memory/qw-sandbox/c1/memory.limit_in_bytes": str(64 << 20),
        "pids/qw-sandbox/c1/pids.max": str(gv.SENTRY_PIDS_HEADROOM + 9),
        "cpu/qw-sandbox/c1/cpu.cfs_period_us": "100000",
        "cpu/qw-sandbox/c1/cpu.cfs_quota_us": "25000",
    }  # memsw is skipped only because this fake has no such file and no swap
    assert gv.cgroup_limits(limits)["memory/memory.memsw.limit_in_bytes"] == 64 << 20
    assert len(made) == 4


def _v2_root(tmp_path: Path, controllers: str = "cpuset cpu io memory pids") -> Path:
    root = tmp_path / "cg2"  # SYNTHETIC unified (cgroup v2) hierarchy
    root.joinpath("qw-sandbox").mkdir(parents=True)
    root.joinpath("cgroup.controllers").write_text(controllers + "\n")
    for d in (root, root / "qw-sandbox"):  # kernel format: names, no "+"
        d.joinpath("cgroup.subtree_control").write_text("cpu memory pids\n")
    return root


def test_setup_cgroups_v2_writes_and_verifies_the_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meminfo = tmp_path / "meminfo"  # SYNTHETIC
    monkeypatch.setattr(gv, "MEMINFO", meminfo)
    meminfo.write_text("SwapTotal:       2048 kB\n")
    limits = SandboxLimits(cpu_millicores=250, memory_bytes=64 << 20, pids=9)
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=_v2_root(tmp_path))
    with pytest.raises(OSError, match="swap"):  # no memory.swap.max, swap on
        gv.setup_cgroups(cfg, "c0", limits, [])
    meminfo.write_text("SwapTotal:       0 kB\n")
    made: list[Path] = []
    gv.setup_cgroups(cfg, "c1", limits, made)
    run = cfg.cgroup_root / "qw-sandbox" / "c1"
    assert made == [run.resolve()]  # one directory; v1 controller dirs untouched
    assert {f.name: f.read_text() for f in run.iterdir()} == {
        "memory.max": str(64 << 20),
        "pids.max": str(gv.SENTRY_PIDS_HEADROOM + 9),
        "cpu.max": "25000 100000",
    }
    assert gv.cgroup_v2_limits(limits)["memory.swap.max"] == "0"
    assert not cfg.cgroup_root.joinpath("memory").exists()


@pytest.mark.parametrize("level", [".", "qw-sandbox"])
def test_setup_cgroups_v2_fails_closed_when_controllers_are_not_delegated(
    tmp_path: Path, level: str
) -> None:
    root = _v2_root(tmp_path)
    ctl = root.joinpath(level, "cgroup.subtree_control")
    ctl.write_text("memory\n")  # cpu and pids not enabled for children
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=root)
    with pytest.raises(OSError, match="not enabled"):
        gv.setup_cgroups(cfg, "c1", SandboxLimits(), [])
    # it asked for exactly the missing ones; a plain file does not apply the write
    assert ctl.read_text() == "+cpu +pids"
    assert not root.joinpath("qw-sandbox", "c1").exists()


def test_setup_cgroups_v2_delegates_the_root_before_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _v2_root(tmp_path)
    for d in (root, root / "qw-sandbox"):
        d.joinpath("cgroup.subtree_control").write_text("")
    write = Path.write_text

    def kernel(self: Path, data: str) -> int:  # SYNTHETIC subtree_control rules
        if self.name != "cgroup.subtree_control":
            return write(self, data)
        up = self.parent.parent.joinpath("cgroup.subtree_control")
        asked = [t[1:] for t in data.split()]
        if self.parent != root and set(asked) - set(up.read_text().split()):
            raise OSError("ENOENT: controller not enabled in the parent")
        names = {*self.read_text().split(), *asked}
        return write(self, " ".join(sorted(names)))

    monkeypatch.setattr(gv, "MEMINFO", tmp_path / "meminfo")
    (tmp_path / "meminfo").write_text("SwapTotal:       0 kB\n")
    monkeypatch.setattr(Path, "write_text", kernel)
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=root)
    gv.setup_cgroups(cfg, "c1", SandboxLimits(), [])
    for d in (root, root / "qw-sandbox"):
        assert d.joinpath("cgroup.subtree_control").read_text() == "cpu memory pids"


@pytest.mark.parametrize("v2", [False, True])
def test_setup_cgroups_fails_when_the_kernel_does_not_apply_a_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, v2: bool
) -> None:
    write = Path.write_text

    def clamping(self: Path, data: str) -> int:  # SYNTHETIC kernel ignoring a write
        return write(self, "max" if self.name == "pids.max" else data)

    root = _v2_root(tmp_path) if v2 else tmp_path / "cg"
    monkeypatch.setattr(gv, "MEMINFO", tmp_path / "meminfo")
    (tmp_path / "meminfo").write_text("SwapTotal:       0 kB\n")
    monkeypatch.setattr(Path, "write_text", clamping)
    cfg = GVisorConfig(None, None, tmp_path, cgroup_root=root)
    with pytest.raises(OSError, match=r"pids\.max not applied"):
        gv.setup_cgroups(cfg, "c1", SandboxLimits(), [])


def test_read_counters_v2(tmp_path: Path) -> None:
    files = {
        "cgroup.controllers": "cpu memory pids\n",
        "qw/c/memory.events": "low 0\nhigh 0\nmax 7\noom 2\noom_kill 1\n",
        "qw/c/pids.events": "max 3\n",
        "qw/c/cpu.stat": "usage_usec 2215352\nuser_usec 2000000\n",
    }  # SYNTHETIC cgroup v2 files
    for rel, text in files.items():
        tmp_path.joinpath(rel).parent.mkdir(parents=True, exist_ok=True)
        tmp_path.joinpath(rel).write_text(text)
    assert gv.read_counters(tmp_path, "/qw/c") == (1, 3, 2_215_352_000)
    tmp_path.joinpath("qw/c/cpu.stat").write_text("user_usec 1\n")
    assert gv.read_counters(tmp_path, "/qw/c") == (1, 3, None)


def _runner(tmp_path: Path, **kw: object) -> GVisorRunner:
    (tmp_path / "rootfs/bin").mkdir(parents=True, exist_ok=True)
    base = {"rootfs": tmp_path / "rootfs", "state_dir": tmp_path / "state"} | kw
    return GVisorRunner(GVisorConfig(**base))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"runsc": None}, "runsc_unavailable"),
        ({"runsc": "MISSING"}, "runsc_unavailable"),
        ({"rootfs": None}, "rootfs_unavailable"),
        ({"rootfs": Path("/")}, "rootfs_is_host_root"),
        ({"platform": "kvm"}, "platform_unsupported"),
        ({"runsc": "FAKE:exit 1"}, "runsc_version_failed"),
        ({"runsc": "FAKE:echo runc version 1.2"}, "runsc_version_failed"),
        ({"cgroup_root": Path("/nonexistent-cg")}, "cgroup_v1_unavailable"),
        ({"cgroup_root": "V2:cpu memory"}, "cgroup_v2_controllers_unavailable"),
    ],
)
def test_probe_and_run_fail_closed(
    tmp_path: Path, kw: dict[str, object], reason: str
) -> None:
    calls = tmp_path / "calls.log"
    runsc = kw.setdefault("runsc", 'FAKE:echo "runsc version release-20990101.0"')
    if runsc == "MISSING":
        kw["runsc"] = tmp_path / "missing-runsc"
    elif isinstance(runsc, str):  # SYNTHETIC stand-in that logs every invocation
        fake = kw["runsc"] = tmp_path / "fake-runsc"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}; {runsc[5:]}\n')
        fake.chmod(stat.S_IRWXU)
    if str(kw.get("cgroup_root")).startswith("V2:"):  # pids not available
        kw["cgroup_root"] = _v2_root(tmp_path, str(kw["cgroup_root"])[3:])
    runner = _runner(tmp_path, **kw)
    gate = runner.probe()
    assert not gate.available and gate.reason.startswith(reason)
    result = runner.run(_req("echo SYNTHETIC"))
    assert result.status is RunStatus.BLOCKED and result.reason.startswith(reason)
    assert result.exit_code is None and result.stdout.total_bytes == 0
    logged = calls.read_text().split("\n") if calls.exists() else []
    assert set(logged) <= {"--version", ""}  # probed only; never `run`
    assert not (tmp_path / "state").exists()


def test_probe_accepts_a_unified_hierarchy_with_the_controllers(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "fake-runsc"  # SYNTHETIC
    fake.write_text('#!/bin/sh\necho "runsc version release-20990101.0"\n')
    fake.chmod(stat.S_IRWXU)
    gate = _runner(tmp_path, runsc=fake, cgroup_root=_v2_root(tmp_path)).probe()
    if os.geteuid() == 0:
        assert gate.available and gate.runtime.endswith("platform=systrap"), gate
    else:
        assert gate.reason == "runsc_requires_root: cgroup limits"


def test_cleanup_reports_a_listed_container_and_runs_every_step(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "fake-runsc"  # SYNTHETIC: delete fails, list still shows it
    fake.write_text('#!/bin/sh\ncase "$*" in *list*) echo qw-c1;; *) exit 1;; esac\n')
    fake.chmod(stat.S_IRWXU)
    cgroup = tmp_path / "cg-dir"
    cgroup.mkdir()
    runner = _runner(tmp_path, runsc=fake)
    problems = runner._cleanup("qw-c1", None, [], tmp_path / "no-bundle", [cgroup])
    assert [p.split(":")[1].strip() for p in problems] == ["delete", "bundle"]
    assert not cgroup.exists()  # later steps still ran after two failures


def test_drain_keeps_a_bounded_head_and_hashes_the_full_stream() -> None:
    data = bytes(range(256)) * 400  # SYNTHETIC, 102400 bytes: two read chunks
    cap = gv.drain(io.BytesIO(data), 1000)
    assert cap.head == data[:1000] and cap.total_bytes == len(data)
    assert cap.sha256 == hashlib.sha256(data).hexdigest() and cap.truncated


def test_read_counters_and_missing(tmp_path: Path) -> None:
    files = {
        "memory/qw/c/memory.oom_control": "under_oom 0\noom_kill 2\n",
        "pids/qw/c/pids.events": "max 5\n",
        "cpuacct/qw/c/cpuacct.usage": "2215352869\n",
    }  # SYNTHETIC cgroup v1 files
    for rel, text in files.items():
        tmp_path.joinpath(rel).parent.mkdir(parents=True, exist_ok=True)
        tmp_path.joinpath(rel).write_text(text)
    assert gv.read_counters(tmp_path, "/qw/c") == (2, 5, 2215352869)
    assert gv.read_counters(tmp_path / "none", "/qw/c") == (None, None, None)


@pytest.fixture(scope="module")
def gvisor(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GVisorRunner]:
    state = tmp_path_factory.mktemp("runsc-state")
    runner = GVisorRunner(GVisorConfig.from_env(state_dir=state))
    gate = runner.probe()
    if not gate.available:
        if os.environ.get("QW_REQUIRE_GVISOR") == "1":
            pytest.fail(f"QW_REQUIRE_GVISOR=1 but gVisor blocked: {gate.reason}")
        pytest.skip(f"gVisor sandbox gate blocked: {gate.reason}")
    yield runner
    runner.close()
    assert not os.path.ismount(state / "null-netns")
    cg = runner.config.cgroup_root
    dirs = ["qw-sandbox", *(f"{c}/qw-sandbox" for c in gv.CONTROLLERS)]  # v2, v1
    leaked = [p for d in dirs for p in cg.glob(f"{d}/qw-t007*")]
    assert leaked == [] and not list(state.glob("bundle-*"))


def _ok(result: sb.SandboxResult) -> str:
    assert result.status is RunStatus.EXITED, result
    return result.stdout.head.decode()


@pytest.mark.gvisor
def test_candidate_runs_on_the_gvisor_kernel_not_the_host(gvisor: GVisorRunner) -> None:
    r = gvisor.run(_req("uname -r; hostname; id -u; grep CapEff /proc/self/status"))
    out = _ok(r).split("\n")
    assert out[0].endswith("gvisor") and out[0] != os.uname().release
    assert out[1:3] == ["candidate", "65534"]
    assert out[3].split() == ["CapEff:", "0000000000000000"]
    assert r.runtime.startswith("gvisor runsc release-") and len(r.digest()) == 64


@pytest.mark.gvisor
def test_isolation_and_no_host_control(
    gvisor: GVisorRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CANARY_ENV, CANARY)
    host_file = tmp_path / "host-canary.txt"
    host_file.write_text(CANARY)  # SYNTHETIC
    script = (
        f"cat {host_file} 2>/dev/null && echo HOSTFILE; "
        f"[ -e /proc/{os.getpid()} ] && echo HOSTPID; "
        "ls /proc | grep -c '^[0-9]'; cat /proc/*/environ 2>/dev/null; env; "
        "for s in /var/run/docker.sock /run/docker.sock /run/containerd; do "
        "[ -e $s ] && echo SOCKET $s; done; "
        "mkdir -p /tmp/m && mount -t tmpfs none /tmp/m 2>/dev/null && echo MOUNTED; "
        f"kill -9 {os.getpid()} 2>/dev/null && echo KILLED; echo still-here"
    )
    out = _ok(gvisor.run(_req(script)))
    assert CANARY not in out and "HOSTFILE" not in out and "HOSTPID" not in out
    assert "SOCKET" not in out and "MOUNTED" not in out and "KILLED" not in out
    lines = out.split("\n")
    assert int(lines[0]) <= 4  # only the candidate's own processes are visible
    assert lines[-2] == "still-here"


@pytest.mark.gvisor
def test_egress_is_denied_including_host_listeners(gvisor: GVisorRunner) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect(("192.0.2.1", 9))  # route lookup only; nothing is sent
        host_ip = udp.getsockname()[0]  # a host interface address, not loopback
    assert not host_ip.startswith("127.")
    with (
        socket.create_server(("127.0.0.1", 0)) as lo,
        socket.create_server((host_ip, 0)) as eth,
    ):
        script = (
            f"wget -q -T 2 -O- http://127.0.0.1:{lo.getsockname()[1]}/ && echo LOOP; "
            f"wget -q -T 2 -O- http://{host_ip}:{eth.getsockname()[1]}/ && echo HOST; "
            "wget -q -T 2 -O- http://1.1.1.1/ && echo EGRESS; "
            "nslookup example.com >/dev/null 2>&1 && echo DNS; "
            "cut -d: -f1 /proc/net/dev | tail -n +3 | tr -d ' '"
        )
        out = _ok(gvisor.run(_req(script, wall_millis=20_000)))
        for srv in (lo, eth):  # a completed handshake would wait in the backlog
            srv.setblocking(False)
            with pytest.raises(BlockingIOError):
                srv.accept()
    assert (
        "HOST" not in out
        and "LOOP" not in out
        and "EGRESS" not in out
        and "DNS" not in out
    )
    assert out.split() == ["lo"]


@pytest.mark.gvisor
def test_root_is_read_only_and_scratch_is_the_only_bounded_writable_dir(
    gvisor: GVisorRunner,
) -> None:
    script = (
        "grep -E ' (/|/dev|/dev/shm|/tmp) ' /proc/mounts | cut -d' ' -f2,4 | "
        "cut -d, -f1; "
        "for d in / /etc /bin /dev /dev/shm /proc /sys /home /root /var /tmp; do "
        "touch $d/.w 2>/dev/null && echo WRITABLE $d; done; "
        "dd if=/dev/zero of=/tmp/fill bs=1024 count=4096 2>/dev/null; "
        "wc -c < /tmp/fill; cut -d' ' -f2 /proc/mounts | sort | tr '\\n' ' '"
    )
    out = _ok(gvisor.run(_req(script, scratch_bytes=1 << 20))).split("\n")
    assert sorted(out[:4]) == ["/ ro", "/dev ro", "/dev/shm ro", "/tmp rw"]
    assert out[4] == "WRITABLE /tmp"
    assert int(out[5]) <= 1 << 20  # the 4 MiB write stopped at the 1 MiB bound
    # the complete mount table: gVisor's own /sys and /dev/pts, nothing from the host
    mounts = ["/", "/dev", "/dev/pts", "/dev/shm", "/proc", "/sys", "/tmp"]
    assert out[6].split() == mounts


@pytest.mark.gvisor
@pytest.mark.parametrize("trap", ["", "trap '' XCPU;", "trap 'exit 0' XCPU;"])
def test_cpu_time_limit_is_measured_not_inferred(
    gvisor: GVisorRunner, trap: str
) -> None:
    # Ignoring SIGXCPU ends in the hard-limit SIGKILL; `exit 0` hides it entirely.
    t0 = time.monotonic()
    r = gvisor.run(
        _req(trap + "while :; do :; done", cpu_seconds=1, wall_millis=30_000)
    )
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.CPU_TIME), r
    assert time.monotonic() - t0 < 15


@pytest.mark.gvisor
@pytest.mark.parametrize("code", [0, 3])
def test_instant_exit_keeps_its_status(gvisor: GVisorRunner, code: int) -> None:
    r = gvisor.run(_req(f"exit {code}"))
    assert (r.status, r.limit_hit, r.exit_code) == (
        RunStatus.EXITED,
        LimitHit.NONE,
        code,
    )


@pytest.mark.gvisor
def test_cpu_quota_throttles_the_sandbox(gvisor: GVisorRunner) -> None:
    work = "i=0; while [ $i -lt 150000 ]; do i=$((i+1)); done; echo done"

    def elapsed(millicores: int) -> float:
        t0 = time.monotonic()
        r = gvisor.run(_req(work, cpu_millicores=millicores, wall_millis=120_000))
        assert _ok(r) == "done\n"
        return time.monotonic() - t0

    fast, slow = elapsed(1000), elapsed(100)
    assert slow > 3 * fast, (fast, slow)


@pytest.mark.gvisor
def test_memory_limit(gvisor: GVisorRunner) -> None:
    hog = 'x=$(head -c 300000000 /dev/zero | tr "\\0" a); echo ${#x}'
    r = gvisor.run(
        _req(hog, memory_bytes=64 << 20, cpu_seconds=120, wall_millis=180_000)
    )
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.MEMORY), r


@pytest.mark.gvisor
def test_wall_time_limit_kills_and_cleans_up(gvisor: GVisorRunner) -> None:
    t0 = time.monotonic()
    r = gvisor.run(_req("echo started; sleep 60", run_id="t007-wall", wall_millis=1500))
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.WALL_TIME)
    assert r.stdout.head == b"started\n" and time.monotonic() - t0 < 15
    assert b"t007-wall" not in gvisor._control("list").stdout


@pytest.mark.gvisor
def test_process_limit_holds_a_fork_bomb(gvisor: GVisorRunner) -> None:
    # Ignores fork failures; counts live tasks with a builtin glob (no fork).
    script = (
        "i=0; while [ $i -lt 50 ]; do (sleep 20 &) 2>/dev/null; i=$((i+1)); done; "
        "set -- /proc/[0-9]*; echo $#; exit 0"
    )
    r = gvisor.run(_req(script, pids=8, wall_millis=20_000))
    assert r.exit_code == 0 and 2 <= int(r.stdout.head) <= 8, r  # never more than 8
    # Not detected: the breach attempt reads as a clean exit (docs/receipts/T007.md).
    assert (r.status, r.limit_hit) == (RunStatus.EXITED, LimitHit.NONE)


@pytest.mark.gvisor
def test_output_is_truncated_with_a_full_stream_digest(gvisor: GVisorRunner) -> None:
    r = gvisor.run(_req("yes | head -c 200000", output_bytes=1024))
    full = b"y\n" * 100_000  # independent oracle
    assert r.status is RunStatus.EXITED and r.exit_code == 0
    assert r.stdout.head == full[:1024] and r.stdout.truncated
    assert r.stdout.total_bytes == 200_000
    assert r.stdout.sha256 == hashlib.sha256(full).hexdigest()
