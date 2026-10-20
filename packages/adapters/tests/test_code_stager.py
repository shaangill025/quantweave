"""Real T049 stager and in-sandbox harness. Unit tests always run: staging, the
read-only mounts, the rootfs digest plumbing and the harness/domain agreement, run on
the host with SYNTHETIC trusted test files only. `gvisor` tests run candidates under
runsc on the Python candidate rootfs. They need root, `QW_SANDBOX_RUNSC` and
`QW_SANDBOX_ROOTFS`; otherwise they skip (a skip is not a pass), or fail when
`QW_REQUIRE_GVISOR=1`. Hostile candidates run only in the sandbox: they go straight to
the staged runner, past the static gate, to test containment itself.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import qw_adapters.gvisor_sandbox as gv
import qw_domain.sandbox as sb
from qw_adapters.code_stager import HARNESS, GVisorStager
from qw_domain import code_improvement as ci
from qw_domain import improvement as im
from qw_domain import release as rl
from test_improvement import ASSESSOR, PRODUCER, card, proto
from test_trial_ledger import T0, ledger

ROOT = Path(__file__).resolve().parents[3]
SRC = "packages/domain/src/qw_domain/"
FIX, TEST, GONE = SRC + "returns.py", "packages/x/tests/test_fm.py", SRC + "old.py"
BUGGY = b"def add(a: int, b: int) -> int:\n    return a - b\n"  # the seeded defect
FIXED = b"def add(a: int, b: int) -> int:\n    return a + b\n"
TEST_SRC = b"""import unittest

from qw_domain.returns import add


def test_add() -> None:
    assert add(2, 3) == 5


class AddCase(unittest.TestCase):
    def test_zero(self) -> None:
        self.assertEqual(add(4, 0), 4)
"""  # SYNTHETIC protected suite: one plain function, one unittest case
CONTENT = {FIX: BUGGY, TEST: TEST_SRC, GONE: b"X = 1\n"}
GUEST_PY = "/usr/local/bin/python3"
ARGS = ("/mnt/harness.py", "--src", "packages/domain/src", TEST)
ARGV = (GUEST_PY, "-I", "-S", "-B", *ARGS)
LIMITS = sb.SandboxLimits(wall_millis=60_000)  # T007 defaults otherwise
CODE = im.Target.APPLICATION_CODE
CANARY = "synthetic-canary-not-a-real-key-49b1"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "candidate_rootfs", ROOT / "tools/candidate_rootfs.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CR = _tool()
PIN = CR.load_manifest(CR.MANIFEST)["tree_sha256"]


def tree_sha(root: Path) -> str:
    return str(CR.tree_digest(root)["sha256_tree"])


BASE = ci.Snapshot({p: sha(b) for p, b in CONTENT.items()})


def profile(pin: str = PIN, py: tuple[str, str] = (GUEST_PY, "3.13"),
            argv: tuple[str, ...] = ARGV) -> ci.Profile:  # fmt: skip
    return ci.Profile("profile-t049", BASE, ci.CodePolicy(), argv, LIMITS, 2, pin, py)


def code(*changes: ci.FileChange) -> ci.CodeCandidate:
    prov = ci.Provenance(PRODUCER, "e" * 64, datetime(2026, 10, 17, tzinfo=UTC))
    paths = frozenset(c.path for c in changes)
    return ci.candidate(BASE, ci.Patch(changes), paths, prov)


def fix(body: bytes) -> ci.FileChange:
    return ci.FileChange(FIX, body, sha(BUGGY))


def checkout(root: Path) -> Path:
    for rel, data in CONTENT.items():
        root.joinpath(rel).parent.mkdir(parents=True, exist_ok=True)
        root.joinpath(rel).write_bytes(data)
    return root


def stager_at(tmp: Path, digest: Callable[[Path], str] = tree_sha) -> GVisorStager:
    rootfs = tmp / "rootfs"  # SYNTHETIC rootfs: never run by the unit tests
    rootfs.joinpath("bin").mkdir(parents=True, exist_ok=True)
    cfg = gv.GVisorConfig(None, rootfs, tmp / "state")
    return GVisorStager(cfg, checkout(tmp / "base"), digest)


def test_stage_writes_base_plus_patch_read_only_and_attests(tmp_path: Path) -> None:
    calls: list[Path] = []

    def digest(p: Path) -> str:
        calls.append(p)
        return "d" * 64

    stager = stager_at(tmp_path, digest)
    patch = ci.Patch((fix(FIXED), ci.FileChange(GONE, None, sha(b"X = 1\n"))))
    st = stager.stage(BASE, patch)
    assert st.tree_digest == ci.applied(BASE, patch).digest
    assert (st.rootfs_digest, calls) == ("d" * 64, [tmp_path / "rootfs"])
    (root,) = stager.stages
    runner = st.runner
    assert isinstance(runner, gv.GVisorRunner)
    assert runner.binds == ((root, "/mnt"),) and runner.stdin == st.channel_key
    assert len(st.channel_key) == 32
    assert stager.stage(BASE, patch).channel_key != st.channel_key  # per stage
    tree = root / "tree"
    assert tree.joinpath(FIX).read_bytes() == FIXED
    assert not tree.joinpath(GONE).exists()
    assert root.joinpath("harness.py").read_bytes() == HARNESS.read_bytes()
    modes = {p.stat().st_mode & 0o777 for p in root.rglob("*") if p.is_file()}
    dirs = {p.stat().st_mode & 0o777 for p in [root, *root.rglob("*")] if p.is_dir()}
    assert (modes, dirs) == ({0o444}, {0o555})
    cfg = gv.oci_config(
        sb.SandboxRequest("s1", ARGV, LIMITS), root, "/cg", runner.binds
    )
    (bind,) = [m for m in cfg["mounts"] if m["type"] == "bind"]
    opts = ("rbind", "ro", "nosuid", "nodev", "noexec")
    assert bind == {"destination": "/mnt", "type": "bind", "source": str(root),
                    "options": opts}  # fmt: skip
    stager.close()
    assert stager.stages == [] and list((tmp_path / "state").iterdir()) == []


def test_stage_refuses_a_base_that_differs_or_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = stager_at(tmp_path)
    planted = tmp_path.joinpath("planted/tree/packages")  # a stray file in the stage
    planted.mkdir(parents=True)
    planted.joinpath("conftest.py").write_bytes(b"X = 1\n")
    with monkeypatch.context() as m:
        m.setattr(tempfile, "mkdtemp", lambda **_: str(planted.parents[1]))
        e = ci.evaluate(code(fix(FIXED)), profile(), stager)
    assert e.reasons == ("image_mismatch",)  # the digest is of what is on disk
    base = tmp_path / "base"
    base.joinpath(FIX).write_bytes(FIXED)  # the checkout drifted from the snapshot
    with pytest.raises(ci.CodeError, match="differs from the snapshot"):
        stager.stage(BASE, ci.Patch((fix(FIXED),)))
    base.joinpath(FIX).unlink()
    tmp_path.joinpath("outside.py").write_bytes(BUGGY)
    base.joinpath(FIX).symlink_to(tmp_path / "outside.py")  # same bytes, via a link
    with pytest.raises(ci.CodeError, match="not a regular checkout file"):
        stager.stage(BASE, ci.Patch((fix(FIXED),)))
    cfg = stager.config
    for binds in (
        [(Path("rel"), "/mnt")],
        [(tmp_path, "mnt")],
        [(tmp_path, "/a/../b")],
    ):
        with pytest.raises(ValueError, match="binds"):
            gv.GVisorRunner(cfg, binds=binds)
    with pytest.raises(ValueError, match="stdin"):
        gv.GVisorRunner(cfg, stdin=b"k" * 4097)
    runner = gv.GVisorRunner(cfg, binds=[(tmp_path, "/mnt")])
    runner.probe = lambda: sb.SandboxGate(True, "SYNTHETIC", "")  # type: ignore[method-assign]
    assert cfg.rootfs is not None
    for link in (None, tmp_path):  # missing, then a link: runsc would mkdir
        if link is not None:
            cfg.rootfs.joinpath("mnt").symlink_to(link)
        r = runner.run(sb.SandboxRequest("m1", ARGV, LIMITS))
        assert (r.status, r.reason) == (sb.RunStatus.BLOCKED, "no_mountpoint: /mnt")
    assert sorted(p.name for p in cfg.rootfs.iterdir()) == ["bin", "mnt"]


def test_path_conflicts_are_typed_and_close_tries_every_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stager = stager_at(tmp_path)
    over = ci.FileChange(SRC[:-1], b"X = 1\n", None)  # a file where the base has a dir
    with pytest.raises(ci.CodeError, match="path_conflict"):
        stager.stage(BASE, ci.Patch((over,)))
    stager.stage(BASE, ci.Patch((fix(FIXED),)))
    stages, real_rmtree = list(stager.stages), shutil.rmtree
    assert len(stages) == 2

    def flaky(path: Path) -> None:
        if path == stages[0]:
            raise OSError("SYNTHETIC busy")
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", flaky)
    with pytest.raises(OSError, match="SYNTHETIC busy"):
        stager.close()
    assert stages[0].exists() and not stages[1].exists()  # the second still went
    assert stager.stages == [stages[0]]  # kept for a retry
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    stager.close()
    assert stager.stages == [] and not stages[0].exists()


def test_rootfs_digest_is_checked_against_the_pin_before_anything_runs(
    tmp_path: Path,
) -> None:
    stager = stager_at(tmp_path)
    got = tree_sha(tmp_path / "rootfs")  # the real tools/candidate_rootfs digest
    p = profile(pin=got)
    e = ci.evaluate(code(fix(FIXED)), profile(), stager)  # the CI pin: mismatch
    assert (e.outcome, e.reasons, e.rootfs_digest) == (
        ci.Outcome.BLOCKED, ("rootfs_mismatch",), got)  # fmt: skip
    tmp_path.joinpath("rootfs/bin/sh").write_bytes(b"#!SYNTHETIC\n")  # drifted
    e = ci.evaluate(code(fix(FIXED)), p, stager)
    assert e.reasons == ("rootfs_mismatch",) and e.rootfs_digest != got
    stager.close()


def _host_harness(st: ci.Staged, root: Path) -> sb.StreamCapture:
    """The staged harness on the host's own python, with only SYNTHETIC trusted
    test files; it exercises the signing format, never containment."""
    argv = [sys.executable, "-I", "-S", "-B", str(root / "harness.py"), *ARGS[1:]]
    r = subprocess.run(argv, input=st.channel_key, capture_output=True, check=False)
    return sb.StreamCapture(r.stdout, len(r.stdout), sha(r.stdout))


def test_host_harness_signs_what_the_domain_verifies(tmp_path: Path) -> None:
    stager = stager_at(tmp_path, lambda _: PIN)
    py = (sys.executable, f"{sys.version_info.major}.{sys.version_info.minor}")
    p = profile(py=py)
    forged = b"print('qw-harness/1 {\"counts\":{\"passed\":2}} ' + 64 * '0')\n"
    cases = [
        (FIXED, []),
        (BUGGY, ["summary:failed", "summary:passed=1!=2"]),
        (forged + FIXED, []),  # a printed line is unsigned and goes to stderr
        (b"raise ImportError('SYNTHETIC')\n",
         ["summary:errors", "summary:passed=0!=2"]),
    ]  # fmt: skip
    for body, want in cases:
        st = stager.stage(BASE, ci.Patch((fix(body),)))
        out = _host_harness(st, stager.stages[-1])
        reasons, interp = ci.harness_report(out, st.channel_key, p)
        assert reasons == want, (body, out.head)
        assert interp is not None and interp.startswith(sys.executable + " 3.")
        other = ci.harness_report(out, b"SYNTHETIC-other-key-0123456789ab", p)
        assert other == (["summary:signed_lines=0"], None)
    stager.close()


# ---- real runtime (gvisor) -------------------------------------------------------


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GVisorStager]:
    tmp = tmp_path_factory.mktemp("t049-real")
    cfg = gv.GVisorConfig.from_env(state_dir=tmp / "state")
    runner = gv.GVisorRunner(cfg)
    gate = runner.probe()
    py = cfg.rootfs is not None and cfg.rootfs.joinpath(GUEST_PY[1:]).exists()
    if not gate.available or not py:
        why = gate.reason or f"no {GUEST_PY} in {cfg.rootfs} (Python rootfs needed)"
        if os.environ.get("QW_REQUIRE_GVISOR") == "1":
            pytest.fail(f"QW_REQUIRE_GVISOR=1 but blocked: {why}")
        pytest.skip(f"gVisor code stager blocked: {why}")
    stager = GVisorStager(cfg, checkout(tmp / "base"), tree_sha)
    yield stager
    stager.close()
    runner.close()
    assert cfg.rootfs is not None and tree_sha(cfg.rootfs) == PIN  # never written
    cg = cfg.cgroup_root
    dirs = ["qw-sandbox", *(f"{c}/qw-sandbox" for c in gv.CONTROLLERS)]
    leaked = [p for d in dirs for p in cg.glob(f"{d}/qw-*")]
    assert leaked == [] and not list((tmp / "state").glob("bundle-*"))


@pytest.mark.gvisor
def test_real_candidate_runs_on_the_pinned_rootfs_with_attested_evidence(
    real: GVisorStager,
) -> None:
    e = ci.evaluate(code(fix(FIXED)), profile(), real)
    assert e.outcome is ci.Outcome.PASSED, e
    assert e.rootfs_digest == PIN and e.runtime.startswith("gvisor runsc")
    assert e.runtime.endswith(f"platform={real.config.platform}")
    assert e.interpreter is not None and e.interpreter.startswith(GUEST_PY + " 3.13.")
    wrong = ci.evaluate(code(fix(FIXED.replace(b"+", b"*"))), profile(), real)
    want = (ci.Outcome.FAILED, ("tests_failed:exit=1",))
    assert (wrong.outcome, wrong.reasons) == want
    assert wrong.interpreter == e.interpreter  # attested on failures too
    other = ci.evaluate(code(fix(FIXED)), profile(py=(GUEST_PY, "3.12")), real)
    assert other.reasons == ("interpreter_mismatch",)
    _gates_still_hold(code(fix(FIXED)), e)


def _gates_still_hold(c: ci.CodeCandidate, e: ci.Evaluation) -> None:
    """Real evidence reaches review, yet nothing can promote it: QUAL17 is open."""
    p = proto(target=CODE, scope=im.Scope.INSTALLATION, scope_id="install-synth",
              baseline_version="app-v0", baseline_hash=BASE.digest,
              scorecard=card(CODE), sandbox_profile_id="profile-t049",
              mutation_space_hash=profile().digest)  # fmt: skip
    cand = ci.to_candidate(c, "code-cand-1", p.experiment_id, "app-v0", T0)
    reg = im.ExperimentRegistry("install-synth").register(p).register_candidate(cand)
    route = ci.route_for_review(ci.package(p, profile(), c, e, cand), p)
    assert route.state == "awaiting_human_review" and not route.automatic
    assert route.blocked_by == ("sandbox_unqualified",)
    assert "in_process_test_summary" in route.limitations  # the pass is unverified
    book: Any = None  # never read: code targets are blocked before the budget check
    v = im.assess(reg, p.experiment_id, cand.candidate_id, ledger(), book, ASSESSOR,
                  T0 + timedelta(days=1), "verdict-1")  # fmt: skip
    assert (v.decision, v.reasons) == (im.Decision.BLOCKED, ("sandbox_unqualified",))
    ok = rl.MaintainerApproval("maintainer-synth", "stepup-synth", T0 + timedelta(2))
    with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
        rl.prepare_release(reg, cand.candidate_id, "r", rl.Compatibility(1, 1, 1), ok)


Run = tuple[sb.SandboxResult, list[str]]


def hostile(real: GVisorStager, body: str, run_id: str) -> Run:
    """Stage `body` as the improvable module and run the harness directly (no static
    gate: containment is what is tested); the stage on the host never changes."""
    st = real.stage(BASE, ci.Patch((fix(body.encode()),)))
    before = tree_sha(real.stages[-1])  # modes and bytes of the tree and harness
    r = sb.execute(st.runner, sb.SandboxRequest(run_id, ARGV, LIMITS))
    assert tree_sha(real.stages[-1]) == before
    assert st.channel_key.hex() not in (r.stdout.head + r.stderr.head).decode("latin1")
    reasons = [f"{r.status.value}:{r.limit_hit.value}"]
    if r.status is sb.RunStatus.EXITED:
        reasons = ci.harness_report(r.stdout, st.channel_key, profile())[0]
    return r, reasons


def _qw(r: sb.SandboxResult) -> dict[str, str]:
    lines = r.stderr.head.decode().splitlines()
    return dict(line.split(" ")[1:3] for line in lines if line[:3] == "QW ")


PROBE = """
import errno, os, socket, sys
def t(name, f):
    try:
        f()
        r = "OK"
    except OSError as x:
        r = errno.errorcode.get(x.errno, type(x).__name__)
    sys.stderr.write(f"QW {name} {r}\\n")
"""


@pytest.mark.gvisor
def test_hostile_writes_and_reads_are_contained(
    real: GVisorStager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "host-secret.txt"
    secret.write_text(CANARY)
    monkeypatch.setenv("QW_T049_SYNTHETIC_CANARY_API_KEY", CANARY)
    stage = real.config.state_dir
    body = PROBE + f"""
for p in ("/mnt/tree/{FIX}", "/mnt/harness.py", "/mnt/new.py",
          "/usr/local/lib/python3.13/os.py",
          "/etc/passwd", "/x", "{stage}/x", "{secret}"):
    t("write:" + p, lambda: open(p, "a").write("pwned"))
t("scratch", lambda: open("/tmp/ok", "w").write("x"))
t("scratch_cap", lambda: open("/tmp/big", "wb").write(b"0" * (64 << 20)))
for p in ("{secret}", "/proc/1/environ", "/proc/1/mem", "/proc/1/fd/0", "/proc/1/maps"):
    t("read:" + p, lambda: open(p, "rb").read(64))
sys.stderr.write("QW env " + str("{CANARY}" in str(os.environ)) + "\\n")
mnt = [m.split()[3] for m in open("/proc/mounts") if m.split()[1] == "/mnt"]
sys.stderr.write("QW mnt " + ";".join(mnt) + "\\n")
""" + FIXED.decode()  # fmt: skip
    r, reasons = hostile(real, body, "t049-io")
    got = _qw(r)
    assert reasons == [], r  # contained; the protected tests still ran and passed
    writes = {k: v for k, v in got.items() if k.startswith("write:")}
    assert len(writes) == 8 and "OK" not in writes.values(), got
    assert (got["scratch"], got["scratch_cap"]) == ("OK", "ENOSPC")
    reads = {k: v for k, v in got.items() if k.startswith("read:")}
    assert len(reads) == 5 and "OK" not in reads.values(), got
    assert got["env"] == "False" and secret.read_text() == CANARY
    assert {"ro", "noexec"} <= set(got["mnt"].split(",")), got["mnt"]  # one mount


@pytest.mark.gvisor
def test_hostile_candidate_sees_no_key_in_env_argv_or_fds(real: GVisorStager) -> None:
    body = """
import os, sys
sys.stderr.write("QW-ENV " + repr(dict(os.environ)) + "\\n")
sys.stderr.write("QW-ARGV " + repr(open("/proc/self/cmdline", "rb").read()) + "\\n")
for fd in sorted(os.listdir("/proc/self/fd"), key=int):
    try:
        sys.stderr.write(f"QW-FD {fd} {os.readlink('/proc/self/fd/' + fd)}\\n")
    except OSError as e:
        sys.stderr.write(f"QW-FD {fd} {e}\\n")
""" + FIXED.decode()  # fmt: skip
    r, reasons = hostile(real, body, "t049-keyless")  # asserts the key hex is absent
    err = r.stderr.head.decode()
    assert reasons == [] and "QW-ENV {" in err and "QW-ARGV b'" in err, r
    fds = [line.split()[1] for line in err.splitlines() if line[:6] == "QW-FD "]
    assert fds[:4] == ["0", "1", "2", "3"], err  # stdin, stderr x2, the report fd


@pytest.mark.gvisor
def test_hostile_network_and_fork_bomb_are_contained(real: GVisorStager) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.connect(("192.0.2.1", 9))  # route lookup only; nothing is sent
        host_ip = udp.getsockname()[0]
    assert not host_ip.startswith("127.")
    listener = socket.create_server(("0.0.0.0", 0))  # loopback and the host address
    port = listener.getsockname()[1]
    body = PROBE + f"""
for h in ("127.0.0.1", "{host_ip}", "1.1.1.1"):
    t("tcp:" + h, lambda: socket.create_connection((h, {port}), timeout=3))
t("dns", lambda: socket.getaddrinfo("example.com", 443))
""" + FIXED.decode()  # fmt: skip
    r, reasons = hostile(real, body, "t049-net")
    got = _qw(r)
    assert reasons == [] and len(got) == 4 and "OK" not in got.values(), r
    listener.setblocking(False)  # a completed handshake would wait in the backlog
    with pytest.raises(BlockingIOError):
        listener.accept()
    listener.close()
    bomb = "import os\nwhile True:\n    os.fork()\n" + FIXED.decode()
    r, reasons = hostile(real, bomb, "t049-bomb")
    assert r.status is sb.RunStatus.LIMIT, r  # pids or memory, whichever first
    assert r.limit_hit in (sb.LimitHit.PIDS, sb.LimitHit.MEMORY)


@pytest.mark.gvisor
def test_forged_summaries_are_not_counted(real: GVisorStager) -> None:
    zero = ',"failed":0,"errors":0,"skipped":0,"xfailed":0,"xpassed":0}'
    fake = '{"counts":{"passed":2' + zero
    body = PROBE + f"""
line = 'qw-harness/1 {fake},"report":"ok"}} ' + 64 * "0" + "\\n"
print(line)
t("proc1_stdout", lambda: open("/proc/1/fd/1", "w").write(line))
import ctypes
sys.stderr.write("QW ptrace " + str(ctypes.CDLL(None).ptrace(16, 1, 0, 0)) + "\\n")
os.write(3, ('{fake},"token":"guess"}}' + "\\n").encode())
os._exit(0)
"""  # fmt: skip
    r, reasons = hostile(real, body + BUGGY.decode(), "t049-forge")
    assert (_qw(r)["proc1_stdout"], _qw(r)["ptrace"]) == ("EACCES", "-1"), r
    want = ["summary:report_token_mismatch", "summary:passed=0!=2"]
    assert (reasons, r.exit_code) == (want, 1), r
    r, reasons = hostile(real, "import os\nos.kill(1, 9)\n", "t049-kill")
    assert (reasons, r.exit_code) == (["summary:signed_lines=0"], 137)  # no line


@pytest.mark.gvisor
def test_in_process_forgery_remains_possible_so_a_pass_stays_unverified(
    real: GVisorStager,
) -> None:
    """The documented limit: candidate code that walks the harness frames can read
    the child token and forge passing counts for buggy code. This is why the bundle
    keeps `in_process_test_summary` and the route stays human-reviewed."""
    body = """
import json, os, sys
f = sys._getframe()
while "token" not in f.f_locals:
    f = f.f_back
counts = dict.fromkeys(("failed", "errors", "skipped", "xfailed", "xpassed"), 0)
counts["passed"] = 2
os.write(f.f_locals["report"], (json.dumps({"token": f.f_locals["token"],
                                            "counts": counts}) + "\\n").encode())
os._exit(0)
"""
    r, reasons = hostile(real, body + BUGGY.decode(), "t049-limit")
    assert reasons == [], (reasons, r)  # forged: BUGGY would fail both tests
    assert "in_process_test_summary" in ci.LIMITATIONS
    assert json.loads(r.stdout.head.split(b" ")[1])["counts"]["passed"] == 2


IMPORT_PROBE = """
import importlib.util, os, sys
sys.stderr.write("QW-PATH " + repr(sys.path) + "\\n")
for root, dirs, files in os.walk("/mnt"):
    sys.stderr.write("QW-WALK " + root + " " + repr(sorted(files)) + "\\n")
for m in ("qw_domain", "qw_domain.old", "qw_domain.returns", "qw_domain.sandbox",
          "qw_adapters", "qw_adapters.gvisor_sandbox", "qw_api", "qw_worker"):
    try:
        s = importlib.util.find_spec(m)
        where = "none" if s is None else str(
            s.origin or list(s.submodule_search_locations or []))
    except ImportError as e:
        where = "error:" + type(e).__name__
    sys.stderr.write(f"QW-SPEC {m} {where}\\n")
"""


@pytest.mark.gvisor
def test_in_repo_imports_inside_the_stage_and_their_refusal(real: GVisorStager) -> None:
    """What a staged candidate can import (probe staged past the gate on purpose),
    and that the gate refuses an in-repo or relative import before staging."""
    r, reasons = hostile(real, IMPORT_PROBE + FIXED.decode(), "t049-imports")
    err = r.stderr.head.decode()
    spec = dict(ln.split(" ", 2)[1:] for ln in err.splitlines() if ln[:8] == "QW-SPEC ")
    assert reasons == [], r
    assert spec["qw_domain.old"] == "/mnt/tree/" + GONE  # the staged base is importable
    assert spec["qw_domain.returns"] == "/mnt/tree/" + FIX
    for m in ("qw_domain.sandbox", "qw_adapters", "qw_adapters.gvisor_sandbox",
              "qw_api", "qw_worker"):  # fmt: skip
        assert spec[m] in ("none", "error:ModuleNotFoundError"), (m, spec)
    before = len(real.stages)
    for body in (b"from qw_adapters.gvisor_sandbox import Path\n",
                 b"from . import old\n", b"from qw_domain.old import X\n"):  # fmt: skip
        e = ci.evaluate(code(fix(body + FIXED)), profile(), real)
        assert e.outcome is ci.Outcome.REFUSED and e.findings, e
    assert len(real.stages) == before  # refused before staging; nothing ran
