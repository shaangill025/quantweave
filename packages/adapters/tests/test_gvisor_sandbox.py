"""gVisor sandbox adapter (T007). Unit tests always run. `gvisor` tests run SYNTHETIC
probes under runsc and need root, `QW_SANDBOX_RUNSC` (or runsc on PATH) and
`QW_SANDBOX_ROOTFS`; otherwise they skip (a skip is not a pass), or fail when
`QW_REQUIRE_GVISOR=1`. Each probe has a busybox form and a form that needs only the
rootfs's own `python3 -I` (the pinned candidate rootfs); `probe_form` picks one and a
rootfs with neither fails."""

import hashlib
import io
import json
import os
import re
import socket
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


PYTHONS = ("usr/local/bin/python3", "usr/bin/python3")  # rootfs-relative, in order
BUSYBOX_TOOLS = (
    "uname hostname id grep ls cat env mount mkdir kill wget nslookup cut tail tr "
    "touch dd wc sort head yes sleep"
)
PY_ERR = r"""
import errno, json, os, socket, sys
def err(f):
    try:
        f()
    except OSError as x:
        return errno.errorcode.get(x.errno, type(x).__name__)
    return "OK"
"""


@dataclass(frozen=True)
class Probes:
    """The real-runtime probe form for the rootfs under test. `busybox`: /bin/sh
    scripts over busybox tools. `python`: only the rootfs's own `python3 -I -c`."""

    runner: GVisorRunner
    form: str
    python: str = ""

    def run(
        self, shell: str, py: str, *args: str, run_id: str = "t007", **limits: int
    ) -> sb.SandboxResult:
        argv = (self.python, "-I", "-c", py, *args)
        if self.form == "busybox":
            argv = ("/bin/sh", "-c", shell)
        return self.runner.run(SandboxRequest(run_id, argv, SandboxLimits(**limits)))


def probe_form(rootfs: Path, override: str | None) -> tuple[str, str]:
    """`QW_SANDBOX_PROBES` when set (busybox or python), else python if the rootfs
    has python3, else busybox if it has /bin/sh; nothing else is a form. Only the
    names are checked on the host; the guest preflight proves the form runs."""
    python = next((f"/{p}" for p in PYTHONS if os.path.lexists(rootfs / p)), "")
    shell = os.path.lexists(rootfs / "bin/sh")
    form = override if override is not None else "python" if python else "busybox"
    if form not in ("busybox", "python"):
        raise ValueError(f"QW_SANDBOX_PROBES must be busybox or python: {form!r}")
    if not (python if form == "python" else shell):
        raise ValueError(f"no {form} probe form in {rootfs} (python3, /bin/sh)")
    return form, python


@pytest.mark.parametrize(
    ("tree", "override", "want"),
    [
        (("bin/sh",), None, ("busybox", "")),
        (("bin/sh", "usr/bin/python3"), None, ("python", "/usr/bin/python3")),
        (("usr/local/bin/python3", "usr/bin/python3"), None,
         ("python", "/usr/local/bin/python3")),
        (("bin/sh", "usr/bin/python3"), "busybox", ("busybox", "/usr/bin/python3")),
        ((), None, "no busybox probe form"),
        (("bin/sh",), "python", "no python probe form"),
        (("usr/bin/python3",), "busybox", "no busybox probe form"),
        (("bin/sh", "usr/bin/python3"), "", "must be busybox or python"),
        (("bin/sh", "usr/bin/python3"), "auto", "must be busybox or python"),
    ],
)  # fmt: skip
def test_probe_form_is_explicit_and_fails_closed(
    tmp_path: Path, tree: tuple[str, ...], override: str | None, want: object
) -> None:
    for rel in tree:  # SYNTHETIC rootfs; a dangling link still names the form
        tmp_path.joinpath(rel).parent.mkdir(parents=True, exist_ok=True)
        tmp_path.joinpath(rel).symlink_to("/nonexistent-on-the-host")
    if isinstance(want, str):
        with pytest.raises(ValueError, match=want):
            probe_form(tmp_path, override)
    else:
        assert probe_form(tmp_path, override) == want


@pytest.fixture(scope="module")
def probes(gvisor: GVisorRunner) -> Probes:
    assert gvisor.config.rootfs is not None
    try:  # a missing form fails, never skips, once the gate is available
        form, python = probe_form(
            gvisor.config.rootfs, os.environ.get("QW_SANDBOX_PROBES")
        )
    except ValueError as exc:
        pytest.fail(str(exc))
    p = Probes(gvisor, form, python)
    r = p.run(
        f"for t in {BUSYBOX_TOOLS}; do command -v $t >/dev/null || echo NOPROBE $t; "
        "done; echo QW-PROBES busybox",
        "import sys; print('QW-PROBES python', sys.version_info >= (3, 12))",
    )
    out = (
        r.stdout.head.decode()
        if (r.status, r.exit_code) == (RunStatus.EXITED, 0)
        else ""
    )
    if out != f"QW-PROBES {form}{' True' if form == 'python' else ''}\n":
        pytest.fail(f"{form} probes cannot run in {gvisor.config.rootfs}: {r}")
    return p


def _ok(result: sb.SandboxResult) -> str:
    assert result.status is RunStatus.EXITED, result
    return result.stdout.head.decode()


def _json(result: sb.SandboxResult) -> Any:
    assert result.exit_code == 0, (result, result.stderr.head[-800:])
    return json.loads(_ok(result))


@pytest.mark.gvisor
def test_candidate_runs_on_the_gvisor_kernel_not_the_host(probes: Probes) -> None:
    py = r"""
import os
caps = [s for s in open("/proc/self/status") if s.startswith("CapEff:")]
u = os.uname()
print(u.release, u.nodename, os.getuid(), caps[0].strip(), u.sysname, sep="\n")
"""
    sh = "uname -r; hostname; id -u; grep CapEff /proc/self/status; uname -s"
    r = probes.run(sh, py)
    out = _ok(r).split("\n")
    assert out[0].endswith("gvisor") and out[0] != os.uname().release
    assert out[1:3] == ["candidate", "65534"]
    assert out[3].split() == ["CapEff:", "0000000000000000"]
    assert out[4:] == ["Linux", ""]
    assert r.runtime.startswith("gvisor runsc release-") and len(r.digest()) == 64


PY_ISOLATION = (
    PY_ERR
    + r"""
import ctypes
host_file, host_pid = sys.argv[1], int(sys.argv[2])
environ = []
for p in filter(str.isdigit, os.listdir("/proc")):
    try:
        environ.append(open(f"/proc/{p}/environ", "rb").read().decode("latin-1"))
    except OSError:
        pass
os.mkdir("/tmp/m")
libc = ctypes.CDLL(None, use_errno=True)
rc = libc.mount(b"none", b"/tmp/m", b"tmpfs", 0, None)
socks = ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd")
print(json.dumps({
    "host_file": err(lambda: open(host_file).read()),
    "host_pid": os.path.exists(f"/proc/{host_pid}"),
    "pids": sum(p.isdigit() for p in os.listdir("/proc")),
    "environ": environ, "env": dict(os.environ),
    "sockets": [s for s in socks if os.path.lexists(s)],
    "mount": [rc, errno.errorcode.get(ctypes.get_errno())],
    "unshare_mount_ns": err(lambda: os.unshare(os.CLONE_NEWNS)),
    "kill": err(lambda: os.kill(host_pid, 9)),
}))
"""
)


@pytest.mark.gvisor
def test_isolation_and_no_host_control(
    probes: Probes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        "command -v mount >/dev/null || echo NOPROBE; "  # no vacuous pass
        "mkdir -p /tmp/m && mount -t tmpfs none /tmp/m 2>/dev/null && echo MOUNTED; "
        f"kill -9 {os.getpid()} 2>/dev/null && echo KILLED; echo still-here"
    )
    r = probes.run(script, PY_ISOLATION, str(host_file), str(os.getpid()))
    out = _ok(r)
    assert CANARY not in out
    if probes.form == "python":
        got = _json(r)
        assert (got["host_file"], got["host_pid"], got["sockets"]) == (
            "ENOENT",
            False,
            [],
        )
        assert 1 <= got["pids"] <= 4  # only the candidate's own processes
        assert got["mount"] == [-1, "EPERM"] and got["unshare_mount_ns"] == "EPERM"
        assert got["kill"] == "ESRCH"  # the host pid does not exist in the guest
        assert {k: v for k, v in got["env"].items() if k != "LC_CTYPE"} == dict(
            e.split("=", 1) for e in gv.GUEST_ENV
        )  # the fixed guest env (Python may add its own LC_CTYPE coercion)
        return
    assert "HOSTFILE" not in out and "HOSTPID" not in out
    assert "SOCKET" not in out and "MOUNTED" not in out and "KILLED" not in out
    assert "NOPROBE" not in out
    lines = out.split("\n")
    assert int(lines[0]) <= 4  # only the candidate's own processes are visible
    assert lines[-2] == "still-here"


PY_EGRESS = (
    PY_ERR
    + r"""
loop, eth, udp_port = (int(sys.argv[i]) for i in (1, 3, 4))
host = sys.argv[2]
def tcp(addr):
    with socket.create_connection(addr, timeout=3):
        pass
def udp(addr):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"SYNTHETIC", addr)
try:
    socket.getaddrinfo("example.com", 80)
    dns = "OK"
except socket.gaierror as x:
    dns = [n for n in dir(socket) if n.startswith("EAI_")
           and getattr(socket, n) == x.errno]
dev = open("/proc/net/dev").read().splitlines()[2:]
print(json.dumps({
    "tcp_loop": err(lambda: tcp(("127.0.0.1", loop))),
    "tcp_host": err(lambda: tcp((host, eth))),
    "tcp_egress": err(lambda: tcp(("1.1.1.1", 80))),
    "udp_host": err(lambda: udp((host, udp_port))),
    "udp_egress": err(lambda: udp(("1.1.1.1", 53))),
    "dns": dns,
    "if_nameindex": [n for _, n in socket.if_nameindex()],
    "proc_net_dev": [line.split(":")[0].strip() for line in dev],
}))
"""
)
UNREACHABLE = {"ENETUNREACH", "EHOSTUNREACH"}


@pytest.mark.gvisor
def test_egress_is_denied_including_host_listeners(probes: Probes) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect(("192.0.2.1", 9))  # route lookup only; nothing is sent
        host_ip = udp.getsockname()[0]  # a host interface address, not loopback
    assert not host_ip.startswith("127.")
    with (
        socket.create_server(("127.0.0.1", 0)) as lo,
        socket.create_server((host_ip, 0)) as eth,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as dgram,
    ):
        dgram.bind((host_ip, 0))
        script = (  # a rootfs without these probes fails here, never passes
            "for t in wget nslookup; do command -v $t >/dev/null || echo NOPROBE; "
            "done; "
            f"wget -q -T 2 -O- http://127.0.0.1:{lo.getsockname()[1]}/ && echo LOOP; "
            f"wget -q -T 2 -O- http://{host_ip}:{eth.getsockname()[1]}/ && echo HOST; "
            "wget -q -T 2 -O- http://1.1.1.1/ && echo EGRESS; "
            "nslookup example.com >/dev/null 2>&1 && echo DNS; "
            "cut -d: -f1 /proc/net/dev | tail -n +3 | tr -d ' '"
        )
        ports = (lo.getsockname()[1], eth.getsockname()[1], dgram.getsockname()[1])
        args = (str(ports[0]), host_ip, str(ports[1]), str(ports[2]))
        r = probes.run(script, PY_EGRESS, *args, wall_millis=20_000)
        out = _ok(r)
        for srv in (lo, eth, dgram):  # a completed handshake would wait in the backlog
            srv.setblocking(False)
            with pytest.raises(BlockingIOError):
                srv.recv(1) if srv is dgram else srv.accept()
    if probes.form == "python":
        got = _json(r)
        assert got["tcp_loop"] == "ECONNREFUSED"  # the guest's own empty loopback
        for k in ("tcp_host", "tcp_egress", "udp_host", "udp_egress"):
            assert got[k] in UNREACHABLE, (k, got)
        assert got["dns"] in (["EAI_AGAIN"], ["EAI_NONAME"], ["EAI_FAIL"]), got
        assert got["if_nameindex"] == got["proc_net_dev"] == ["lo"]
        return
    assert (
        "HOST" not in out
        and "LOOP" not in out
        and "EGRESS" not in out
        and "DNS" not in out
    )
    assert out.split() == ["lo"]


PY_ROOT_RO = (
    PY_ERR
    + r"""
points, opts = [], {}
for line in open("/proc/self/mounts"):
    f = line.split()
    points.append(f[1])
    opts[f[1]] = f[3].split(",")[0]
fd, fill = os.open("/tmp/fill", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "OK"
for _ in range(4096):
    try:
        os.write(fd, bytes(1024))
    except OSError as x:
        fill = errno.errorcode[x.errno]
        break
os.close(fd)
ro = ("/", "/usr", "/dev", "/dev/shm", "/tmp")
print(json.dumps({
    "points": sorted(points), "opts": {d: opts.get(d) for d in ro},
    "statvfs_ro": {d: bool(os.statvfs(d).f_flag & os.ST_RDONLY) for d in ro},
    "write": {d: err(lambda: open(os.path.join(d, ".w"), "x").close())
              for d in sys.argv[1:]},
    "fill": fill, "fill_bytes": os.stat("/tmp/fill").st_size,
}))
"""
)
WRITE_DIRS = ("/", "/usr", "/etc", "/bin", "/dev", "/dev/shm", "/proc", "/sys",
              "/home", "/root", "/var", "/tmp")  # fmt: skip


@pytest.mark.gvisor
def test_root_is_read_only_and_scratch_is_the_only_bounded_writable_dir(
    probes: Probes,
) -> None:
    script = (
        "grep -E ' (/|/dev|/dev/shm|/tmp) ' /proc/mounts | cut -d' ' -f2,4 | "
        "cut -d, -f1; "
        "for d in / /etc /bin /dev /dev/shm /proc /sys /home /root /var /tmp; do "
        "touch $d/.w 2>/dev/null && echo WRITABLE $d; done; "
        "dd if=/dev/zero of=/tmp/fill bs=1024 count=4096 2>/dev/null; "
        "wc -c < /tmp/fill; cut -d' ' -f2 /proc/mounts | sort | tr '\\n' ' '"
    )
    r = probes.run(script, PY_ROOT_RO, *WRITE_DIRS, scratch_bytes=1 << 20)
    # the complete mount table: gVisor's own /sys and /dev/pts, nothing from the host
    mounts = ["/", "/dev", "/dev/pts", "/dev/shm", "/proc", "/sys", "/tmp"]
    if probes.form == "python":
        got = _json(r)
        assert got["points"] == mounts
        modes = {"/": "ro", "/usr": None, "/dev": "ro", "/dev/shm": "ro", "/tmp": "rw"}
        assert got["opts"] == modes  # /usr is part of the read-only root mount
        assert got["statvfs_ro"] == {d: d != "/tmp" for d in modes}
        assert got["write"].pop("/tmp") == "OK"
        assert set(got["write"]) == set(WRITE_DIRS) - {"/tmp"}
        # Defence in depth only: uid 65534 gets EACCES on any root-owned directory,
        # even a writable one. The read-only proof is the exact mount table and
        # statvfs above.
        assert set(got["write"].values()) <= {"EROFS", "EACCES"}, got["write"]
        assert got["fill"] == "ENOSPC" and 0 < got["fill_bytes"] <= 1 << 20
        return
    out = _ok(r).split("\n")
    assert sorted(out[:4]) == ["/ ro", "/dev ro", "/dev/shm ro", "/tmp rw"]
    assert out[4] == "WRITABLE /tmp"
    assert int(out[5]) <= 1 << 20  # the 4 MiB write stopped at the 1 MiB bound
    assert out[6].split() == mounts


PY_SPIN = {
    "": "while True: pass",
    "trap '' XCPU;": "import signal\nsignal.signal(signal.SIGXCPU, signal.SIG_IGN)\n"
    "while True: pass",
    "trap 'exit 0' XCPU;": "import os, signal\n"
    "signal.signal(signal.SIGXCPU, lambda *_: os._exit(0))\nwhile True: pass",
}


@pytest.mark.gvisor
@pytest.mark.parametrize("trap", list(PY_SPIN))
def test_cpu_time_limit_is_measured_not_inferred(probes: Probes, trap: str) -> None:
    # Ignoring SIGXCPU ends in the hard-limit SIGKILL; `exit 0` hides it entirely.
    t0 = time.monotonic()
    shell = trap + "while :; do :; done"
    r = probes.run(shell, PY_SPIN[trap], cpu_seconds=1, wall_millis=30_000)
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.CPU_TIME), r
    assert time.monotonic() - t0 < 15


@pytest.mark.gvisor
@pytest.mark.parametrize("code", [0, 3])
def test_instant_exit_keeps_its_status(probes: Probes, code: int) -> None:
    py = "import sys; sys.exit(int(sys.argv[1]))"
    r = probes.run(f"exit {code}", py, str(code))
    assert (r.status, r.limit_hit, r.exit_code) == (
        RunStatus.EXITED,
        LimitHit.NONE,
        code,
    )


@pytest.mark.gvisor
def test_cpu_quota_throttles_the_sandbox(probes: Probes) -> None:
    work = "i=0; while [ $i -lt 150000 ]; do i=$((i+1)); done; echo done"
    py = "i = 0\nwhile i < 3_000_000:\n    i += 1\nprint('done')"

    def elapsed(millicores: int) -> float:
        t0 = time.monotonic()
        r = probes.run(work, py, cpu_millicores=millicores, wall_millis=120_000)
        assert _ok(r) == "done\n"
        return time.monotonic() - t0

    fast, slow = elapsed(1000), elapsed(100)
    assert slow > 3 * fast, (fast, slow)


@pytest.mark.gvisor
def test_memory_limit(probes: Probes) -> None:
    hog = 'x=$(head -c 300000000 /dev/zero | tr "\\0" a); echo ${#x}'
    py = "x = b'a' * 300_000_000\nprint(len(x))"
    r = probes.run(hog, py, memory_bytes=64 << 20, cpu_seconds=120, wall_millis=180_000)
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.MEMORY), r


@pytest.mark.gvisor
def test_wall_time_limit_kills_and_cleans_up(probes: Probes) -> None:
    t0 = time.monotonic()
    py = "import time\nprint('started', flush=True)\ntime.sleep(60)"
    r = probes.run("echo started; sleep 60", py, run_id="t007-wall", wall_millis=1500)
    assert (r.status, r.limit_hit) == (RunStatus.LIMIT, LimitHit.WALL_TIME)
    assert r.stdout.head == b"started\n" and time.monotonic() - t0 < 15
    assert b"t007-wall" not in probes.runner._control("list").stdout


PY_FORK = r"""
import errno, os, time
failed = set()
for _ in range(50):
    try:
        pid = os.fork()
    except OSError as x:
        failed.add(errno.errorcode[x.errno])
        continue
    if pid == 0:
        time.sleep(20)
        os._exit(0)
print(sum(p.isdigit() for p in os.listdir("/proc")), *sorted(failed))
"""


@pytest.mark.gvisor
def test_process_limit_holds_a_fork_bomb(probes: Probes) -> None:
    # Ignores fork failures; counts live tasks with a builtin glob (no fork).
    script = (
        "i=0; while [ $i -lt 50 ]; do (sleep 20 &) 2>/dev/null; i=$((i+1)); done; "
        "set -- /proc/[0-9]*; echo $#; exit 0"
    )
    r = probes.run(script, PY_FORK, pids=8, wall_millis=20_000)
    live, *failed = r.stdout.head.split()
    assert r.exit_code == 0 and 2 <= int(live) <= 8, r  # never more than 8
    assert failed == ([b"EAGAIN"] if probes.form == "python" else [])
    # Not detected: the breach attempt reads as a clean exit (docs/receipts/T007.md).
    assert (r.status, r.limit_hit) == (RunStatus.EXITED, LimitHit.NONE)


@pytest.mark.gvisor
def test_output_is_truncated_with_a_full_stream_digest(probes: Probes) -> None:
    py = "import sys\nsys.stdout.buffer.write(b'y\\n' * 100_000)"
    r = probes.run("yes | head -c 200000", py, output_bytes=1024)
    full = b"y\n" * 100_000  # independent oracle
    assert r.status is RunStatus.EXITED and r.exit_code == 0
    assert r.stdout.head == full[:1024] and r.stdout.truncated
    assert r.stdout.total_bytes == 200_000
    assert r.stdout.sha256 == hashlib.sha256(full).hexdigest()
