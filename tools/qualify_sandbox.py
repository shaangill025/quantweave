#!/usr/bin/env python3
"""Owner-run sandbox qualification for QUAL17 (T007; spec §11, §15).

Run as root from a clean repo checkout on the reference Linux host:

    sudo env "PATH=$PATH" QW_SANDBOX_RUNSC=... QW_SANDBOX_ROOTFS=... \\
        uv run --frozen --no-sync python tools/qualify_sandbox.py

It collects host facts, checks runsc and the rootfs, runs the real gVisor tests
(`QW_REQUIRE_GVISOR=1`) once per platform (systrap, then ptrace) and prints one JSON
evidence block between BEGIN/END markers plus a short summary.
`host_passes_sandbox_suite` is true only when every real-runtime test passed with
nothing skipped on at least one platform, cgroup support is available, it ran as root
on a clean commit (no modified or untracked files) and nothing else failed. Anything
missing fails closed with a reason. Logs and downloads go to a fresh private dir under
a work dir that must be a non-symlink, runner-owned, not group/world-writable
directory. The script never edits repo files and QUAL17 is not marked by it: the owner
pastes the block back for the receipt (docs/operator/sandbox-qualification.md). No
network access unless `--fetch-runsc` is passed. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also under python -P
from candidate_rootfs import tree_digest  # shared with the CI rootfs job

ROOT = Path(__file__).resolve().parent.parent
TEST_FILES = ("packages/adapters/tests/test_gvisor_sandbox.py",
              "packages/domain/tests/test_sandbox.py")  # fmt: skip
PLATFORMS = ("systrap", "ptrace")
GVISOR_RELEASE = "20261005.0"  # exercised in T007 increment 1
GVISOR_URL = "https://storage.googleapis.com/gvisor/releases/release/{rel}/{arch}"
BUSYBOX = "busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"  # noqa: E501
V1_CONTROLLERS = ("memory", "pids", "cpu", "cpuacct")
V2_CONTROLLERS = ("cpu", "memory", "pids")
COUNT_KEYS = ("passed", "failed", "skipped", "errors", "xfailed", "xpassed")
BEGIN = "----- BEGIN QW SANDBOX QUALIFICATION EVIDENCE -----"
END = "----- END QW SANDBOX QUALIFICATION EVIDENCE -----"
PYTEST_TIMEOUT_S = 3600
MAX_ARCHIVE_BYTES = 512 << 20
SUMMARY = re.compile(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b")


def distro(os_release: str) -> str:
    parts = (line.partition("=") for line in os_release.splitlines())
    fields = {k: v.strip().strip('"') for k, _, v in parts if k}
    return fields.get("PRETTY_NAME") or fields.get("NAME") or "unknown"


def cgroup_facts(root: Path) -> dict[str, Any]:
    """v2 (unified root), v1 (per-controller hierarchies) or none; the controllers
    the sandbox adapter needs."""
    if root.joinpath("cgroup.controllers").is_file():
        have = root.joinpath("cgroup.controllers").read_text().split()
        ok = all(c in have for c in V2_CONTROLLERS)
        return {"version": "v2", "controllers": sorted(have), "supported": ok}
    have = [c for c in V1_CONTROLLERS if root.joinpath(c, "tasks").exists()]
    version = "v1" if have else "none"
    return {"version": version, "controllers": have, "supported": len(have) == 4}


def _meminfo_kb(text: str, key: str) -> int | None:
    m = re.search(rf"^{key}:\s+(\d+) kB", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def host_facts(
    os_release: Path = Path("/etc/os-release"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    kvm: Path = Path("/dev/kvm"),
    meminfo: Path = Path("/proc/meminfo"),
) -> dict[str, Any]:
    mem = _read(meminfo)
    return {
        "kernel": os.uname().release, "distro": distro(_read(os_release)),
        "arch": os.uname().machine, "cgroup": cgroup_facts(cgroup_root),
        "kvm": kvm.exists(), "root": os.geteuid() == 0, "cpu_count": os.cpu_count(),
        "mem_total_kb": _meminfo_kb(mem, "MemTotal"),
        "swap_total_kb": _meminfo_kb(mem, "SwapTotal"),
        "python": platform.python_version(),
    }  # fmt: skip


def parse_pytest(text: str) -> dict[str, Any]:
    """Counts from the final summary line and the PASSED ids of `pytest -rA`."""
    counts, found_summary = dict.fromkeys(COUNT_KEYS, 0), False
    for line in reversed(text.splitlines()):
        found = SUMMARY.findall(line)
        if found and re.search(r" in [\d.]+s", line):
            for n, word in found:
                counts["errors" if word.startswith("error") else word] = int(n)
            found_summary = True
            break
    lines = text.splitlines()
    passed = [line[7:].rstrip() for line in lines if line.startswith("PASSED ")]
    return {"counts": counts, "passed_ids": passed, "summary_found": found_summary}


def platform_result(
    name: str, exit_code: int, parsed: dict[str, Any], real_ids: list[str],
    duration_s: str,
) -> dict[str, Any]:  # fmt: skip
    counts = parsed["counts"]
    real_passed = sum(1 for t in real_ids if t in set(parsed["passed_ids"]))
    clean = all(counts[k] == 0 for k in COUNT_KEYS if k != "passed")
    ok = (
        exit_code == 0 and clean and parsed["summary_found"] and bool(real_ids)
        and real_passed == len(real_ids) and counts["passed"] >= len(real_ids)
    )  # fmt: skip
    return {
        "platform": name, "exit_code": exit_code, "counts": counts,
        "real_runtime_tests": len(real_ids), "real_runtime_passed": real_passed,
        "all_real_runtime_passed": ok, "duration_s": duration_s,
    }  # fmt: skip


def decide(
    host: dict[str, Any], runsc: dict[str, Any] | None, rootfs: dict[str, Any] | None,
    platforms: list[dict[str, Any]], git: dict[str, Any], reasons: list[str],
) -> list[str]:  # fmt: skip
    """Every reason the run fails; empty means the host passes the suite."""
    cg = host.get("cgroup", {})
    checks = (
        (not host.get("root"), "not_root"),
        (not cg.get("supported"), f"cgroup_unsupported: {cg.get('version')}"),
        (runsc is None, "runsc_missing"),
        (rootfs is None, "rootfs_missing"),
        (not git.get("commit"), "git_commit_unknown"),
        (git.get("dirty", True), "worktree_dirty: modified or untracked files"),
        (not any(p["all_real_runtime_passed"] for p in platforms),
         "no_platform_passed_every_real_runtime_test_with_zero_skips"),
    )  # fmt: skip
    return [*reasons, *(why for failed, why in checks if failed)]


def build_evidence(
    host: dict[str, Any], runsc: dict[str, Any] | None, rootfs: dict[str, Any] | None,
    platforms: list[dict[str, Any]], git: dict[str, Any], generated_at: str,
    reasons: list[str],
) -> dict[str, Any]:  # fmt: skip
    why = decide(host, runsc, rootfs, platforms, git, reasons)
    return {
        "schema": "quantweave.sandbox_qualification/1",
        "qualification_id": "QUAL17",
        "generated_at": generated_at,
        "git": git, "host": host, "runsc": runsc, "rootfs": rootfs,
        "platforms": platforms, "host_passes_sandbox_suite": not why,
        "reasons": why, "note": "Evidence for review, recorded in "
        "docs/receipts/T007.md. QUAL17 is not marked by this script.",
    }  # fmt: skip


def owners(environ: Mapping[str, str]) -> set[int]:
    """Accepted owners of the work dir and runsc: the runner, and the sudo user."""
    sudo = environ.get("SUDO_UID", "")
    return {os.geteuid(), *([int(sudo)] if sudo.isdigit() else [])}


def secure_dir(path: Path, allowed: frozenset[int] | set[int]) -> Path:
    """Create or accept a work dir only if it is a real directory owned by an
    allowed uid and not group/world-writable; root never follows a planted path."""
    with contextlib.suppress(FileExistsError):  # checked below, not followed
        path.mkdir(mode=0o700, parents=True)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"{path} is not a plain directory")
    if st.st_uid not in allowed:
        raise ValueError(f"{path} owner {st.st_uid}")
    if st.st_mode & 0o022:
        raise ValueError(f"{path} is group/world-writable")
    return path


def open_new(path: Path) -> IO[bytes]:
    """A new 0600 file; fails if anything, including a symlink, is there."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def copy_capped(src: IO[bytes], path: Path, limit: int) -> None:
    with open_new(path) as f:
        total = 0
        while chunk := src.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"download exceeds {limit} bytes")
            f.write(chunk)


def binary_problem(path: Path, allowed: frozenset[int] | set[int]) -> str:
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
        return f"not an executable file: {path}"
    if st.st_uid not in allowed:
        return f"owner {st.st_uid} not in {sorted(allowed)}: {path}"
    if st.st_mode & 0o022:
        return f"group/world-writable: {path}"
    return ""


def _file_sha(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify_sha512(path: Path, sha_text: str) -> str:
    expected = (sha_text.split() or [""])[0].lower()
    actual = _file_sha(path, "sha512")
    if len(expected) != 128 or not hmac.compare_digest(expected, actual):
        raise ValueError(f"sha512 mismatch for {path.name}")
    return actual


def extract_runsc(archive: Path, dest: Path) -> Path:
    """Extract with the `data` filter (no absolute, parent, device or setuid
    entries); the archive's top-level `runsc` must be a regular file."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            tar.extractall(dest, filter="data")
        except TypeError as exc:  # a Python without extraction filters
            raise tarfile.TarError(f"no tarfile extraction filter: {exc}") from exc
    runsc = dest / "runsc"
    if not runsc.is_file() or runsc.is_symlink():
        raise tarfile.TarError("archive has no regular runsc file")
    if os.geteuid() == 0:
        os.chown(runsc, 0, 0)
    runsc.chmod(0o755)
    return runsc


def fetch_runsc(work_dir: Path, release: str) -> dict[str, Any]:
    """Network: the official gVisor release channel only, sha512-verified."""
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        platform.machine()
    )
    if arch is None:
        raise ValueError(f"no gVisor release for arch {platform.machine()}")
    url = GVISOR_URL.format(rel=release, arch=arch) + "/gvisor.tar.bz2"
    dest = work_dir / f"gvisor-{release}-{arch}"
    dest.mkdir(mode=0o700)
    archive = dest / "gvisor.tar.bz2"
    with urllib.request.urlopen(url, timeout=300) as r:
        copy_capped(r, archive, MAX_ARCHIVE_BYTES)
    with urllib.request.urlopen(url + ".sha512", timeout=60) as r:
        sha_text = r.read(4096).decode()
    digest = verify_sha512(archive, sha_text)
    runsc = extract_runsc(archive, dest / "bin")
    print(f"fetched {url}\nsha512 OK {digest}")
    return {"path": runsc, "archive_url": url, "archive_sha512": digest}


def runsc_info(
    path: Path, source: dict[str, Any], allowed: set[int]
) -> tuple[dict[str, Any], str]:
    info = {"path": str(path), "source": source.get("archive_url", "env")} | {
        k: v for k, v in source.items() if k == "archive_sha512"
    }
    try:
        if problem := binary_problem(path, allowed):
            return info, f"runsc_unusable: {problem}"
        info["sha512"] = _file_sha(path, "sha512")
    except OSError as exc:
        return info, f"runsc_unusable: {exc}"
    try:
        out = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=30,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        return info, f"runsc_version_failed: {exc}"
    first = (out.stdout.splitlines() or [""])[0].split()
    if out.returncode != 0 or first[:2] != ["runsc", "version"] or len(first) < 3:
        return info, f"runsc_version_failed: exit {out.returncode} {first}"
    info["version"] = first[2]
    print(f"runsc {first[2]} ({path})")
    return info, ""


def git_state(repo: Path = ROOT) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=repo, capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return out.stdout.strip()

    status = git("status", "--porcelain", "--untracked-files=all")
    return {"commit": git("rev-parse", "HEAD"), "dirty": status != ""}


def child_env(environ: Mapping[str, str], extra: dict[str, str]) -> dict[str, str]:
    """The child env without PYTEST_* overrides (addopts, plugins)."""
    keep = {k: v for k, v in environ.items() if not k.startswith("PYTEST_")}
    return keep | extra


def _pytest(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    return subprocess.run(
        cmd, cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=PYTEST_TIMEOUT_S, check=False,
    )  # fmt: skip


def run_platform(name: str, env: dict[str, str], log_dir: Path) -> dict[str, Any]:
    env = env | {"QW_REQUIRE_GVISOR": "1", "QW_SANDBOX_PLATFORM": name}
    try:
        collected = _pytest(["--collect-only", "-q", "-m", "gvisor", *TEST_FILES], env)
        real = [line for line in collected.stdout.splitlines() if "::" in line]
    except subprocess.TimeoutExpired:
        real = []  # an empty real-runtime set never passes
    print(f"[{name}] running {len(real)} real-runtime tests (+ unit tests) ...")
    t0 = time.monotonic()
    try:
        done = _pytest(["-q", "-rA", *TEST_FILES], env)
        code, text = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        code, text = -1, f"timed out after {PYTEST_TIMEOUT_S}s"
    log = log_dir / f"pytest-{name}.log"
    with open_new(log) as f:
        f.write(text.encode())
    dur = f"{time.monotonic() - t0:.1f}"
    result = platform_result(name, code, parse_pytest(text), real, dur)
    print(f"[{name}] exit {code}; {result['counts']}; log: {log}")
    return result


def instructions(work: Path) -> str:
    base = GVISOR_URL.format(rel=GVISOR_RELEASE, arch="$(uname -m)")
    return f"""
To provide runsc, either re-run with --fetch-runsc (downloads gVisor release
{GVISOR_RELEASE} from the official channel and verifies its sha512), or by hand:
  mkdir -p {work}/gvisor && cd {work}/gvisor
  curl -fsSL -O "{base}/gvisor.tar.bz2" -O "{base}/gvisor.tar.bz2.sha512"
  sha512sum -c gvisor.tar.bz2.sha512
  tar -xjf gvisor.tar.bz2 && ./runsc --version
  export QW_SANDBOX_RUNSC={work}/gvisor/runsc

To provide the rootfs (pinned busybox, as in T007 increment 1):
  docker pull {BUSYBOX}
  cid=$(docker create {BUSYBOX})
  mkdir -p {work}/rootfs-busybox
  docker export "$cid" | tar -x -C {work}/rootfs-busybox && docker rm "$cid"
  export QW_SANDBOX_ROOTFS={work}/rootfs-busybox
The Python candidate rootfs (tools/candidate_rootfs.json) is rebuilt as in
docs/operator/sandbox-qualification.md; it lacks the busybox probes the suite needs.
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Owner-run sandbox qualification (QUAL17)")
    ap.add_argument(
        "--fetch-runsc",
        action="store_true",
        help="download and sha512-verify gVisor (the only network use)",
    )
    ap.add_argument("--runsc-release", default=GVISOR_RELEASE)
    ap.add_argument("--work-dir", type=Path, default=Path("/var/tmp/qw-sandbox"))
    ap.add_argument("--expect-rootfs-digest", help="sha256_tree the rootfs must have")
    args = ap.parse_args(argv)  # fmt: skip
    host, git, reasons = host_facts(), git_state(), []
    allowed = owners(os.environ)
    work: Path = args.work_dir
    run_dir: Path | None = None
    try:  # a fresh private dir per run: nothing pre-planted is ever opened
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=secure_dir(work, allowed)))
    except (OSError, ValueError) as exc:
        reasons.append(f"work_dir_unsafe: {exc}")
    runsc: dict[str, Any] | None = None
    rootfs: dict[str, Any] | None = None
    platforms: list[dict[str, Any]] = []
    env_runsc = os.environ.get("QW_SANDBOX_RUNSC")
    source: dict[str, Any] = {}
    if env_runsc and args.fetch_runsc:
        print("QW_SANDBOX_RUNSC is set: --fetch-runsc ignored, nothing downloaded")
    elif args.fetch_runsc and run_dir is not None:
        try:
            source = fetch_runsc(run_dir, args.runsc_release)
        except (OSError, ValueError, tarfile.TarError) as exc:
            reasons.append(f"runsc_fetch_failed: {exc}")
    runsc_path = Path(env_runsc) if env_runsc else source.get("path")
    if runsc_path is not None:
        runsc, problem = runsc_info(runsc_path, source, allowed)
        reasons += [problem] if problem else []
    env_rootfs = os.environ.get("QW_SANDBOX_ROOTFS")
    try:
        if env_rootfs and Path(env_rootfs, "bin").is_dir():
            rootfs = tree_digest(Path(env_rootfs))
            want = args.expect_rootfs_digest
            if want and want != rootfs["sha256_tree"]:
                reasons.append(f"rootfs_digest_mismatch: expected {want}")
        elif env_rootfs:
            reasons.append(f"rootfs_unusable: {env_rootfs} has no bin/")
    except OSError as exc:
        reasons.append(f"rootfs_unusable: {exc}")
    if runsc is None or rootfs is None:
        print(instructions(work))
    # A dirty tree still runs the tests (useful evidence) but never passes.
    clean = {"commit": "-", "dirty": False}
    pre = decide(host, runsc, rootfs, [{"all_real_runtime_passed": True}], clean,
                 reasons)  # fmt: skip
    if not pre and runsc is not None and rootfs is not None and run_dir is not None:
        env = child_env(os.environ, {
            "QW_SANDBOX_RUNSC": runsc["path"], "QW_SANDBOX_ROOTFS": rootfs["path"],
            "PYTHONDONTWRITEBYTECODE": "1",
        })  # fmt: skip
        platforms = [run_platform(p, env, run_dir) for p in PLATFORMS]
    else:
        print("not running the gVisor tests: " + "; ".join(pre))
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = build_evidence(host, runsc, rootfs, platforms, git, now, reasons)
    print(f"\nPaste everything from BEGIN to END back for the T007 receipt.\n{BEGIN}")
    print(json.dumps(ev, indent=2, sort_keys=True))
    print(END)
    for p in platforms:
        print(f"{p['platform']}: real-runtime {p['real_runtime_passed']}/"
              f"{p['real_runtime_tests']} passed; {p['counts']}")  # fmt: skip
    ok = ev["host_passes_sandbox_suite"]
    print("host passes the sandbox suite: " + ("yes" if ok else "no"))
    for r in ev["reasons"]:
        print(f"  - {r}")
    print("QUAL17 is not marked by this script; the pasted block is evidence only.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
