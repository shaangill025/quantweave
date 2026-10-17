#!/usr/bin/env python3
"""The Python candidate rootfs (T007, owner decision 13): CI (job `candidate-rootfs`)
and the owner prune, audit and digest the pinned image (`candidate_rootfs.json`);
`qualify_sandbox.py --expect-rootfs-digest` uses `tree_digest` from here.

Digest `quantweave.rootfs_tree/3`: SHA-256 over a header and one line per entry, the
root first as `.` (its mode is verified), then the rest sorted by path bytes. A line
is `path type mode size extra xattrs`, built from raw bytes, so no locale applies.
Path and link target are percent-encoded bytes (`quote_from_bytes`, `/` kept), which
is injective and leaves no space or newline in a field. type is d f l p s c b; mode
is octal with setuid/setgid/sticky; size is the file size, link length or 0; extra
is the SHA-256, link target, `major:minor` or `-`; xattrs is `-` or sorted
`name=sha256(value)` pairs (`security.selinux`, a host label, excluded; unsupported
reads as none). Links are never followed. Owners and mtimes are excluded (they depend
on the extractor); the guest runs as uid 65534 on a read-only root. Stdlib, 3.12+.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_from_bytes

MANIFEST = Path(__file__).resolve().parent / "candidate_rootfs.json"
FORMAT = "quantweave.rootfs_tree/3"
SCHEMA = "quantweave.candidate_rootfs/1"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TYPES = ((stat.S_ISFIFO, "p"), (stat.S_ISSOCK, "s"), (stat.S_ISCHR, "c"),
         (stat.S_ISBLK, "b"))  # fmt: skip
HOST_XATTRS = frozenset({b"security.selinux"})
NO_XATTRS = (errno.ENOTSUP, errno.EOPNOTSUPP)  # listxattr only


def _raise(exc: OSError) -> None:
    raise exc


def _walk(root: Path) -> list[tuple[bytes, bytes, os.stat_result]]:
    """The root (as `.`), then every entry below it sorted by path bytes; symlinks
    are not followed and an unreadable directory is an error, not a silent gap."""
    top = os.fsencode(root)
    out = []
    for dirpath, dirs, files in os.walk(top, onerror=_raise):
        for name in [*dirs, *files]:
            p = os.path.join(dirpath, name)
            out.append((os.path.relpath(p, top), p, os.lstat(p)))
    return [(b".", top, os.lstat(top)), *sorted(out, key=lambda e: e[0])]


def _xattrs(path: bytes) -> dict[bytes, bytes]:
    """Extended attributes, not following links. Only listing may report "not
    supported" (no xattrs); any read error, even a vanished attribute, raises, so a
    listed `security.capability` is never silently dropped."""
    try:
        names = [os.fsencode(n) for n in os.listxattr(path, follow_symlinks=False)]
    except OSError as exc:
        if exc.errno in NO_XATTRS:
            return {}
        raise
    return {n: os.getxattr(path, n, follow_symlinks=False)
            for n in names if n not in HOST_XATTRS}  # fmt: skip


def _sha256(path: bytes) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: Path) -> dict[str, Any]:
    """The rootfs content digest (format in the module docstring)."""
    lines = [FORMAT.encode()]
    seen: dict[tuple[int, int], str] = {}  # hard links hashed once
    for rel, p, st in _walk(root):
        m = st.st_mode
        if stat.S_ISDIR(m):
            kind, size, extra = "d", 0, "-"
        elif stat.S_ISLNK(m):
            kind, size, extra = "l", st.st_size, quote_from_bytes(os.readlink(p))
        elif stat.S_ISREG(m):
            key = (st.st_dev, st.st_ino)
            if key not in seen:
                seen[key] = _sha256(p)
            kind, size, extra = "f", st.st_size, seen[key]
        else:
            kind = next(k for test, k in TYPES if test(m))
            dev = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
            size, extra = 0, dev if kind in "cb" else "-"
        xa = sorted(_xattrs(p).items())
        xattrs = ",".join(f"{quote_from_bytes(n)}={hashlib.sha256(v).hexdigest()}"
                          for n, v in xa) or "-"  # fmt: skip
        fields = (quote_from_bytes(rel), kind, f"{stat.S_IMODE(m):o}", str(size))
        lines.append(" ".join((*fields, extra, xattrs)).encode())
    digest = hashlib.sha256(b"\n".join(lines)).hexdigest()
    below = len(lines) - 2  # entries below the root
    return {"path": str(root), "sha256_tree": digest, "entries": below,
            "format": FORMAT}  # fmt: skip


def _rel_parts(rel: object) -> list[str]:
    """A relative path's components; absolute, empty, `.` and `..` are refused."""
    parts = rel.split("/") if isinstance(rel, str) else []
    if not parts or any(c in ("", ".", "..") for c in parts):  # "/x" gives ""
        raise ValueError(f"unsafe relative path or pattern: {rel!r}")
    return parts


def _check_root(root: Path) -> None:
    st = root.lstat()  # FileNotFoundError is an OSError, reported as unusable
    if not stat.S_ISDIR(st.st_mode) or root.resolve() == Path("/"):
        raise ValueError(f"not a usable rootfs directory: {root}")


def _matches(root: Path, pattern: str) -> list[Path]:
    """Entries matching a per-component glob, descending only through real
    directories: a symlinked component never leads outside the root."""
    *parents, last = _rel_parts(pattern)
    dirs = [root]
    for comp in parents:
        found = [e for d in dirs for e in os.scandir(d)
                 if e.is_dir(follow_symlinks=False)]  # fmt: skip
        dirs = [Path(e.path) for e in found if fnmatch.fnmatchcase(e.name, comp)]
    found = [e for d in dirs for e in os.scandir(d)]
    return [Path(e.path) for e in found if fnmatch.fnmatchcase(e.name, last)]


def prune(root: Path, patterns: list[str]) -> list[str]:
    """Remove every entry matching `patterns` (a link is removed, never its target)
    and strip setuid/setgid from regular files. Returns the removed paths."""
    try:
        _check_root(root)
    except OSError as exc:
        raise ValueError(f"not a usable rootfs directory: {root}") from exc
    for pattern in patterns:
        _rel_parts(pattern)
    removed: list[str] = []
    for pattern in patterns:
        for p in _matches(root, pattern):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(p.relative_to(root).as_posix())
    for _, path, st in _walk(root):
        if stat.S_ISREG(st.st_mode) and st.st_mode & 0o6000:
            os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o6000)
    return sorted(removed)


def audit(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Problems that fail the build: forbidden tools (executable files or links),
    setuid/setgid files, file capabilities (`security.capability`), world-writable
    entries including the root (sticky dirs excepted), special files and missing
    required paths."""
    forbidden = [re.compile(p) for p in manifest["forbidden"]]
    problems = []
    for brel, p, st in _walk(root):
        rel = os.fsdecode(brel)
        m, name = st.st_mode, rel.rsplit("/", 1)[-1]
        tool = stat.S_ISLNK(m) or (stat.S_ISREG(m) and m & 0o111)
        if tool and any(pat.search(name) for pat in forbidden):
            problems.append(f"forbidden: {rel}")
        if stat.S_ISREG(m) and m & 0o6000:
            problems.append(f"setid: {rel}")
        if not stat.S_ISLNK(m) and b"security.capability" in _xattrs(p):
            problems.append(f"capability: {rel}")
        if stat.S_ISREG(m) or stat.S_ISDIR(m):
            if m & 0o002 and not (stat.S_ISDIR(m) and m & stat.S_ISVTX):
                problems.append(f"world_writable: {rel}")
        elif not stat.S_ISLNK(m):
            problems.append(f"special: {rel}")
    for rel in manifest["required"]:
        *parents, last = _rel_parts(rel)
        here = root
        for comp in parents:  # real directories only, as in prune
            here = here / comp
            if not here.is_dir() or here.is_symlink():
                break
        else:
            if os.path.lexists(here / last):
                continue
        problems.append(f"missing: {rel}")
    return problems


def platform_digest(index: dict[str, Any], plat: str) -> str:
    """The one manifest digest for `os/arch[/variant]` in an OCI image index."""
    os_, arch, *variant = plat.split("/")
    want = (os_, arch, variant[0] if variant else None)
    found = [
        m.get("digest") for m in index.get("manifests", [])
        if (m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture"),
            m.get("platform", {}).get("variant")) == want
    ]  # fmt: skip
    if len(found) != 1 or not isinstance(found[0], str) or not DIGEST.match(found[0]):
        raise ValueError(f"expected one valid digest for {plat}, found {found}")
    return found[0]


def load_manifest_text(text: str) -> dict[str, Any]:
    m: dict[str, Any] = json.loads(text)
    if m.get("schema") != SCHEMA:
        raise ValueError(f"manifest schema is not {SCHEMA}")
    for rel in [*m["remove"], *m["required"]]:
        _rel_parts(rel)
    for pattern in m["forbidden"]:
        re.compile(pattern)
    base, tree = m["base_manifest_digest"], m["tree_sha256"]
    if base is not None and not DIGEST.match(base):
        raise ValueError("base_manifest_digest must be null or sha256:<64 hex>")
    if tree is not None and not HEX64.match(tree):
        raise ValueError("tree_sha256 must be null or 64 hex")
    return m


def load_manifest(path: Path) -> dict[str, Any]:
    return load_manifest_text(path.read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest", help="print the tree digest as JSON")
    d.add_argument("root", type=Path)
    d.add_argument("--expect", help="sha256_tree the tree must have")
    for name in ("prune", "audit"):
        p = sub.add_parser(name)
        p.add_argument("root", type=Path)
        p.add_argument("--manifest", type=Path, default=MANIFEST)
    pd = sub.add_parser("platform-digest", help="read an OCI index on stdin")
    pd.add_argument("--platform", required=True)
    sh = sub.add_parser("show", help="print a manifest value (empty for null)")
    sh.add_argument("key")
    sh.add_argument("--manifest", type=Path, default=MANIFEST)
    args = ap.parse_args(argv)
    if args.cmd == "digest":
        result = tree_digest(args.root)
        print(json.dumps(result, sort_keys=True))
        if args.expect is not None and args.expect != result["sha256_tree"]:
            print(f"digest mismatch: expected {args.expect}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "platform-digest":
        print(platform_digest(json.load(sys.stdin), args.platform))
        return 0
    manifest = load_manifest(args.manifest)
    if args.cmd == "show":
        value = manifest[args.key]
        print("" if value is None else value)
        return 0
    if args.cmd == "prune":
        print("\n".join(f"removed {r}" for r in prune(args.root, manifest["remove"])))
        return 0
    problems = audit(args.root, manifest)
    print("\n".join(problems) or "audit: no problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
