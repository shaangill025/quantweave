"""In-sandbox test harness for T049 code candidates. It runs on the pinned rootfs's own
`python3 -I -S -B`, needs no pytest and is never imported by the host. The stager
mounts it read-only at /mnt/harness.py, beside the candidate tree at /mnt/tree.

Parent (the sandbox's first process; never imports candidate code): it makes itself
non-dumpable, so a same-uid child cannot open /proc/1/fd or /proc/1/mem, then reads
the per-stage channel key from stdin and attests the interpreter. It starts the child
with a fresh token on the child's stdin and prints exactly one line:
`qw-harness/1 <json> <hex hmac-sha256(key, json)>`.

Child (`--child`): it reads the token and moves its stdout to a private report fd,
pointing fd 1 at stderr. It then imports the protected test files from the tree and
runs their unittest cases and plain `test_*` functions (no fixtures, no pytest). It
reports the counts and the token on the report fd.

LIMIT: the counts come from the child, which is the candidate's own process. Code
that finds the token in harness memory (frames, gc) or writes the report fd can still
forge them, so a pass stays unverified (`in_process_test_summary`). The key never
enters the child, so it cannot forge or replay the parent's line.
"""

import hashlib
import hmac
import importlib.util
import inspect
import json
import os
import platform
import secrets
import subprocess
import sys
import traceback
import unittest
from pathlib import Path

FORMAT = "qw-harness/1"
COUNTS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
TREE = Path(__file__).resolve().parent.joinpath("tree")
PR_GET_DUMPABLE, PR_SET_DUMPABLE = 3, 4


def sign(key: bytes, body: dict[str, object]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
    return f"{FORMAT} {text} {mac}"


def _rel(p: str) -> Path:
    parts = p.split("/")
    if p.startswith("/") or any(s in ("", ".", "..") for s in parts):
        raise SystemExit(f"harness: unsafe tree path {p!r}")
    return TREE.joinpath(*parts)


def _args(args: list[str]) -> tuple[list[Path], list[Path]]:
    srcs, files, it = [], [], iter(args)
    for a in it:
        if a == "--src":
            srcs.append(_rel(next(it, "")))
        else:
            files.append(_rel(a))
    if not files:
        raise SystemExit("harness: no test files")
    return srcs, files


def _nondumpable() -> bool:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        done = int(libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)) == 0
        return done and int(libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0)) == 0
    except (ImportError, OSError, AttributeError):
        return False


def _read_report(raw: bytes, token: str) -> tuple[str, dict[str, int]]:
    lines = raw.splitlines()
    if len(lines) != 1:
        return ("missing" if not lines else "multiple"), dict.fromkeys(COUNTS, 0)
    try:
        got = json.loads(lines[0])
        counts = got["counts"]
        ok = set(got) == {"token", "counts"} and set(counts) == set(COUNTS)
        ok = ok and all(type(v) is int and v >= 0 for v in counts.values())
    except (ValueError, TypeError, KeyError):
        ok = False
    if not ok:
        return "malformed", dict.fromkeys(COUNTS, 0)
    if not hmac.compare_digest(str(got["token"]), token):
        return "token_mismatch", dict.fromkeys(COUNTS, 0)
    return "ok", {k: counts[k] for k in COUNTS}


def parent(args: list[str]) -> int:
    nondumpable = _nondumpable()  # before anything else can run
    key = sys.stdin.buffer.read(4097)
    if not 16 <= len(key) <= 4096:
        print("harness: no channel key on stdin", file=sys.stderr)
        return 2
    _args(args)
    token = secrets.token_hex(16)
    me = str(Path(__file__).resolve())
    argv = [sys.executable, "-I", "-S", "-B", me, "--child", *args]
    proc = subprocess.run(argv, input=token.encode(), stdout=subprocess.PIPE,
                          close_fds=True, check=False)  # fmt: skip
    report, counts = _read_report(proc.stdout, token)
    body: dict[str, object] = {
        "executable": sys.executable, "version": platform.python_version(),
        "implementation": sys.implementation.name, "nondumpable": nondumpable,
        "child_exit": proc.returncode, "report": report, "counts": counts,
    }  # fmt: skip
    print(sign(key, body), flush=True)
    bad = sum(v for k, v in counts.items() if k != "passed")
    return 0 if (report, proc.returncode, bad) == ("ok", 0, 0) else 1


def child(args: list[str]) -> int:
    token = sys.stdin.read(64).strip()  # before any candidate code runs
    report = os.dup(1)
    os.dup2(2, 1)  # stray prints from the tree go to stderr
    srcs, files = _args(args)
    sys.path[:0] = [str(s) for s in srcs]
    result, import_errors = unittest.TestResult(), 0
    for i, path in enumerate(files):
        try:
            spec = importlib.util.spec_from_file_location(f"qw_harness_t{i}", path)
            if spec is None or spec.loader is None:
                raise ImportError(str(path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            import_errors += 1
            traceback.print_exc()
            continue
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        for name, fn in sorted(vars(module).items()):
            if name.startswith("test_") and inspect.isfunction(fn) and (
                fn.__module__ == module.__name__
            ):  # fmt: skip
                suite.addTest(unittest.FunctionTestCase(fn))
        suite.run(result)
    for _, text in result.failures + result.errors:
        print(text, file=sys.stderr)
    counts = {
        "failed": len(result.failures), "errors": len(result.errors) + import_errors,
        "skipped": len(result.skipped), "xfailed": len(result.expectedFailures),
        "xpassed": len(result.unexpectedSuccesses),
    }  # fmt: skip
    counts["passed"] = result.testsRun - sum(counts.values()) + import_errors
    line = json.dumps({"token": token, "counts": counts}) + "\n"
    os.write(report, line.encode())
    return 0


if __name__ == "__main__":
    if sys.flags.optimize or not sys.flags.isolated:
        raise SystemExit("harness: needs -I and no -O (asserts must run)")
    rest = sys.argv[1:]
    sys.exit(child(rest[1:]) if rest[:1] == ["--child"] else parent(rest))
