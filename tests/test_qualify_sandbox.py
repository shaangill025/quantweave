"""Owner-run sandbox qualification script (T007, QUAL17): the pure parts, and the
fail-closed path end to end. SYNTHETIC host files, pytest output and trees only."""

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "qualify_sandbox.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qualify_sandbox", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qs = _load()

OS_RELEASE = 'NAME="Debian GNU/Linux"\nVERSION_ID="12"\nPRETTY_NAME="Debian 12"\n'
REAL = [
    "packages/adapters/tests/test_gvisor_sandbox.py::test_memory_limit",
    "packages/adapters/tests/test_gvisor_sandbox.py::test_cpu[trap '' XCPU;]",
]
GREEN = (
    "....                                    [100%]\n"
    "=========================== short test summary info ============================\n"
    f"PASSED {REAL[0]}\nPASSED {REAL[1]}\n"
    "PASSED packages/domain/tests/test_sandbox.py::test_limits\n"
    "PASSED packages/domain/tests/test_sandbox.py::test_gate\n"
    "4 passed, 2 warnings in 25.61s\n"
)  # SYNTHETIC pytest -q -rA output


def _cgroup_root(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        tmp_path.joinpath(rel).parent.mkdir(parents=True, exist_ok=True)
        tmp_path.joinpath(rel).write_text(text)
    return tmp_path


@pytest.mark.parametrize(
    ("files", "version", "supported"),
    [
        ({"cgroup.controllers": "cpuset cpu io memory pids\n"}, "v2", True),
        ({"cgroup.controllers": "cpu io memory\n"}, "v2", False),
        ({f"{c}/tasks": "" for c in ("memory", "pids", "cpu", "cpuacct")}, "v1", True),
        ({"memory/tasks": "", "unified/cgroup.controllers": ""}, "v1", False),
        ({}, "none", False),
    ],
)  # SYNTHETIC cgroupfs layouts
def test_cgroup_facts(
    tmp_path: Path, files: dict[str, str], version: str, supported: bool
) -> None:
    facts = qs.cgroup_facts(_cgroup_root(tmp_path, files))
    assert (facts["version"], facts["supported"]) == (version, supported)


def test_host_facts_read_the_injected_files(tmp_path: Path) -> None:
    (tmp_path / "os-release").write_text(OS_RELEASE)
    (tmp_path / "meminfo").write_text("MemTotal: 16384000 kB\nSwapTotal: 0 kB\n")
    cg = _cgroup_root(tmp_path / "cg", {"cgroup.controllers": "cpu memory pids\n"})
    facts = qs.host_facts(
        os_release=tmp_path / "os-release", cgroup_root=cg,
        kvm=tmp_path / "no-kvm", meminfo=tmp_path / "meminfo",
    )  # fmt: skip
    assert facts["distro"] == "Debian 12" and facts["kvm"] is False
    assert qs.distro("NAME=Arch\nID=arch\n") == "Arch" and qs.distro("") == "unknown"
    assert facts["cgroup"]["version"] == "v2"
    assert facts["mem_total_kb"] == 16384000 and facts["swap_total_kb"] == 0
    assert facts["root"] is (os.geteuid() == 0)
    assert facts["kernel"] == os.uname().release
    assert facts["arch"] == os.uname().machine


def test_parse_pytest_counts_and_passed_ids() -> None:
    got = qs.parse_pytest(GREEN)
    assert got["counts"] == {
        "passed": 4, "failed": 0, "skipped": 0, "errors": 0,
        "xfailed": 0, "xpassed": 0,
    }  # fmt: skip
    assert set(REAL) <= set(got["passed_ids"]) and len(got["passed_ids"]) == 4
    bad = qs.parse_pytest(
        f"FAILED {REAL[0]} - AssertionError\nSKIPPED [1] x.py:3: no\n"
        "1 failed, 2 passed, 1 skipped, 1 error in 3.00s\n"
    )
    assert bad["counts"]["failed"] == 1 and bad["counts"]["errors"] == 1
    assert bad["counts"]["skipped"] == 1 and bad["passed_ids"] == []
    assert qs.parse_pytest("garbage")["summary_found"] is False
    lookalike = "3 failed in 1.00s\n" + GREEN  # captured output, then the summary
    assert qs.parse_pytest(lookalike)["counts"]["failed"] == 0


def _platform(text: str = GREEN, code: int = 0, real: list[str] = REAL) -> Any:
    return qs.platform_result("systrap", code, qs.parse_pytest(text), real, "25.6")


def test_platform_passes_only_when_every_real_runtime_test_passed() -> None:
    assert _platform()["all_real_runtime_passed"] is True
    skipped = GREEN.replace("4 passed,", "4 passed, 1 skipped,")
    assert _platform(skipped)["all_real_runtime_passed"] is False
    assert _platform(code=1)["all_real_runtime_passed"] is False
    missing = [*REAL, "packages/adapters/tests/test_gvisor_sandbox.py::test_egress"]
    assert _platform(real=missing)["all_real_runtime_passed"] is False
    assert _platform(real=[])["all_real_runtime_passed"] is False  # none collected
    no_summary = GREEN.replace("4 passed, 2 warnings in 25.61s\n", "")
    assert _platform(no_summary)["all_real_runtime_passed"] is False
    too_few = GREEN.replace("4 passed,", "1 passed,")  # fewer than the 2 real ids
    assert _platform(too_few)["all_real_runtime_passed"] is False
    for word in ("failed", "error", "xfailed", "xpassed"):
        text = GREEN.replace("4 passed,", f"4 passed, 1 {word},")
        assert _platform(text)["all_real_runtime_passed"] is False, word


def _evidence(**over: Any) -> Any:
    host = {"root": True, "cgroup": {"version": "v2", "supported": True}}
    args: dict[str, Any] = {
        "host": host,
        "runsc": {"path": "/opt/runsc", "version": "release-20261005.0"},
        "rootfs": {"path": "/opt/rootfs", "sha256_tree": "ab" * 32},
        "platforms": [_platform()],
        "git": {"commit": "3acf16a" + "0" * 33, "dirty": False},
        "generated_at": "2026-10-15T12:00:00Z",
        "reasons": [],
    } | over
    return qs.build_evidence(**args)


def test_qualifies_only_under_the_strict_rule() -> None:
    ev = _evidence()
    assert ev["host_passes_sandbox_suite"] is True and ev["reasons"] == []
    failing = _platform(GREEN.replace("4 passed,", "4 passed, 1 skipped,"))
    cases: dict[str, dict[str, Any]] = {
        "not_root": {"host": {"root": False, "cgroup": {"supported": True}}},
        "cgroup_unsupported": {
            "host": {"root": True, "cgroup": {"version": "v1", "supported": False}}
        },
        "runsc_missing": {"runsc": None},
        "rootfs_missing": {"rootfs": None},
        "worktree_dirty": {"git": {"commit": "x", "dirty": True}},
        "git_commit_unknown": {"git": {"commit": None, "dirty": False}},
        "no_platform_passed": {"platforms": [failing, failing]},
        "runsc_version_failed": {"reasons": ["runsc_version_failed: exit 1"]},
    }
    for reason, over in cases.items():
        ev = _evidence(**over)
        assert ev["host_passes_sandbox_suite"] is False, reason
        assert any(r.startswith(reason) for r in ev["reasons"]), (reason, ev)
    one = _evidence(platforms=[failing, _platform()])  # ptrace alone is enough
    assert one["host_passes_sandbox_suite"] is True


def test_evidence_json_shape() -> None:
    ev = json.loads(json.dumps(_evidence()))
    assert set(ev) == {
        "schema", "qualification_id", "generated_at", "git", "host", "runsc",
        "rootfs", "platforms", "host_passes_sandbox_suite", "reasons", "note",
    }  # fmt: skip
    assert ev["schema"] == "quantweave.sandbox_qualification/1"
    assert ev["qualification_id"] == "QUAL17"
    assert "QUAL17 is not marked by this script" in ev["note"]
    assert set(ev["platforms"][0]) == {
        "platform", "exit_code", "counts", "real_runtime_tests",
        "real_runtime_passed", "all_real_runtime_passed", "duration_s",
    }  # fmt: skip


def test_tree_digest_covers_names_modes_links_and_content(tmp_path: Path) -> None:
    root = tmp_path / "rootfs"  # SYNTHETIC
    (root / "bin").mkdir(parents=True)
    (root / "bin/sh").write_bytes(b"#!synthetic\n")
    (root / "bin/ls").symlink_to("sh")
    first = qs.tree_digest(root)
    assert first["entries"] == 3 and len(first["sha256_tree"]) == 64
    assert qs.tree_digest(root) == first
    (root / "bin/sh").chmod(0o700)
    second = qs.tree_digest(root)
    assert second != first
    (root / "bin/ls").unlink()
    (root / "bin/ls").symlink_to("/bin/sh")
    assert qs.tree_digest(root) != second


def test_verify_sha512_and_safe_extract(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:  # SYNTHETIC release
        for name, data in (("runsc", b"#!/bin/sh\n"), ("gvisor-bin/x", b"y")):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    archive = tmp_path / "gvisor.tar.bz2"
    archive.write_bytes(buf.getvalue())
    digest = hashlib.sha512(buf.getvalue()).hexdigest()
    assert qs.verify_sha512(archive, f"{digest}  gvisor.tar.bz2\n") == digest
    with pytest.raises(ValueError, match="sha512 mismatch"):
        qs.verify_sha512(archive, f"{'0' * 128}  gvisor.tar.bz2\n")
    with pytest.raises(ValueError, match="sha512 mismatch"):
        qs.verify_sha512(archive, "")
    runsc = qs.extract_runsc(archive, tmp_path / "out")
    assert runsc == tmp_path / "out/runsc" and os.access(runsc, os.X_OK)
    evil = tmp_path / "evil.tar.bz2"
    with tarfile.open(evil, mode="w:bz2") as tar:
        info = tarfile.TarInfo("../escape")
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(tarfile.TarError):
        qs.extract_runsc(evil, tmp_path / "out2")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize(
    ("value", "reasons"),
    [
        (None, ["runsc_missing", "rootfs_missing"]),
        ("/x" * 3000, ["runsc_unusable", "rootfs_unusable"]),  # ENAMETOOLONG
    ],
)
def test_end_to_end_fails_closed_without_a_traceback(
    tmp_path: Path, value: str | None, reasons: list[str]
) -> None:
    def status() -> bytes:
        cmd = ["git", "status", "--porcelain", "--untracked-files=all"]
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, check=True).stdout

    before = status()
    env = {k: v for k, v in os.environ.items() if not k.startswith("QW_SANDBOX_")}
    if value:
        env |= {"QW_SANDBOX_RUNSC": value, "QW_SANDBOX_ROOTFS": value}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--work-dir", str(tmp_path)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 1 and "Traceback" not in proc.stderr, proc.stderr
    assert "sha512sum -c" in proc.stdout and "--fetch-runsc" in proc.stdout
    ev = json.loads(proc.stdout.split(qs.BEGIN)[1].split(qs.END)[0])
    assert ev["host_passes_sandbox_suite"] is False and ev["platforms"] == []
    assert all(any(r.startswith(x) for r in ev["reasons"]) for x in reasons)
    assert ev["host"]["kernel"] == os.uname().release
    assert status() == before  # the script never edits repo files


def test_work_dir_must_be_private_to_the_runner(tmp_path: Path) -> None:
    me = frozenset({os.geteuid()})
    good = qs.secure_dir(tmp_path / "new", me)
    assert stat.S_IMODE(good.lstat().st_mode) == 0o700
    (tmp_path / "real").mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(tmp_path / "real")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared").chmod(0o770)
    (tmp_path / "file").write_text("SYNTHETIC")
    for bad in ("link", "shared", "file"):
        with pytest.raises(ValueError, match=bad):
            qs.secure_dir(tmp_path / bad, me)
    with pytest.raises(ValueError, match="owner"):
        qs.secure_dir(tmp_path / "real", frozenset({os.geteuid() + 4242}))
    assert qs.owners({"SUDO_UID": "1000"}) == {os.geteuid(), 1000}
    assert qs.owners({}) == {os.geteuid()}


def test_new_files_refuse_an_existing_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("SYNTHETIC victim")
    (tmp_path / "pytest-systrap.log").symlink_to(victim)
    with pytest.raises(FileExistsError):
        qs.open_new(tmp_path / "pytest-systrap.log")
    assert victim.read_text() == "SYNTHETIC victim"
    qs.open_new(tmp_path / "fresh.log").close()
    assert stat.S_IMODE((tmp_path / "fresh.log").stat().st_mode) == 0o600


def test_binary_must_not_be_writable_by_others(tmp_path: Path) -> None:
    exe = tmp_path / "runsc"  # SYNTHETIC
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    me = frozenset({os.geteuid()})
    assert qs.binary_problem(exe, me) == ""
    assert "owner" in qs.binary_problem(exe, frozenset({os.geteuid() + 4242}))
    exe.chmod(0o775)
    assert "writable" in qs.binary_problem(exe, me)
    info, problem = qs.runsc_info(exe, {}, me)  # never executed, never hashed
    assert problem.startswith("runsc_unusable: group/world-writable")
    assert "sha512" not in info


def test_git_state_counts_untracked_files_and_failures(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("SYNTHETIC")
    git = ["git", "-c", "user.name=SYNTHETIC", "-c", "user.email=s@example.invalid"]
    for args in (["init", "-q"], ["add", "a.txt"], ["commit", "-q", "-m", "SYN"]):
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)
    clean = qs.git_state(repo)
    assert clean["dirty"] is False and len(clean["commit"]) == 40
    (repo / "tests").mkdir()
    (repo / "tests/conftest.py").write_text("# SYNTHETIC untracked plugin\n")
    assert qs.git_state(repo)["dirty"] is True
    assert qs.git_state(tmp_path / "not-a-repo-dir") == {"commit": None, "dirty": True}


def test_child_env_drops_pytest_overrides() -> None:
    env = {"PATH": "/bin", "PYTEST_ADDOPTS": "-k x", "PYTEST_PLUGINS": "SYNTHETIC"}
    assert qs.child_env(env, {"A": "1"}) == {"PATH": "/bin", "A": "1"}


def test_collect_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(args: list[str], env: dict[str, str]) -> Any:
        raise subprocess.TimeoutExpired("pytest", 1)

    monkeypatch.setattr(qs, "_pytest", hang)
    got = qs.run_platform("systrap", {}, tmp_path)
    assert got["all_real_runtime_passed"] is False and got["exit_code"] == -1


def test_download_is_capped_and_filterless_tarfile_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        qs.copy_capped(io.BytesIO(b"x" * 10), tmp_path / "a.bin", limit=9)
    qs.copy_capped(io.BytesIO(b"x" * 9), tmp_path / "b.bin", limit=9)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        tar.addfile(tarfile.TarInfo("runsc"), io.BytesIO(b""))
    (tmp_path / "g.tar.bz2").write_bytes(buf.getvalue())

    def old_python(*a: Any, **kw: Any) -> None:
        raise TypeError("unexpected keyword argument 'filter'")

    monkeypatch.setattr(tarfile.TarFile, "extractall", old_python)
    with pytest.raises(tarfile.TarError, match="filter"):
        qs.extract_runsc(tmp_path / "g.tar.bz2", tmp_path / "x")


def test_unsafe_work_dir_and_rootfs_digest_mismatch_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "rootfs/bin").mkdir(parents=True)  # SYNTHETIC rootfs
    monkeypatch.delenv("QW_SANDBOX_RUNSC", raising=False)
    monkeypatch.setenv("QW_SANDBOX_ROOTFS", str(tmp_path / "rootfs"))
    (tmp_path / "elsewhere").mkdir(mode=0o700)
    (tmp_path / "wd").symlink_to(tmp_path / "elsewhere")
    args = ["--expect-rootfs-digest", "0" * 64]
    for wd, reason in (("wd", "work_dir_unsafe"), ("ok", "rootfs_digest_mismatch")):
        assert qs.main(["--work-dir", str(tmp_path / wd), *args]) == 1
        ev = json.loads(capsys.readouterr().out.split(qs.BEGIN)[1].split(qs.END)[0])
        assert ev["host_passes_sandbox_suite"] is False
        assert any(r.startswith(reason) for r in ev["reasons"]), ev["reasons"]
    assert list((tmp_path / "elsewhere").iterdir()) == []  # nothing written there
