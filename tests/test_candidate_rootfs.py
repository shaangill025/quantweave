"""Python candidate rootfs tool (T007 increment 3, owner decision 13): tree digest,
prune, audit, platform digest, manifest and the CI job's shape. SYNTHETIC trees,
image indexes and workflow expectations only; no image is pulled."""

import errno
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "candidate_rootfs.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load("candidate_rootfs", TOOL)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(autouse=True)
def _umask() -> Any:
    old = os.umask(0o022)  # expected modes do not depend on the runner's umask
    yield
    os.umask(old)


def _tree(root: Path, order: list[str] | None = None) -> Path:
    """SYNTHETIC rootfs: a dir, a file, a relative and an absolute symlink, a
    symlinked dir and an empty dir; `order` permutes creation order."""
    root.mkdir()
    for d in ("usr/bin", "e"):
        (root / d).mkdir(parents=True)
    for key in order or ["file", "rel", "abs", "bin"]:
        if key == "file":
            (root / "usr/bin/python3").write_bytes(b"SYNTHETIC py\n")
        elif key == "rel":
            (root / "usr/bin/python").symlink_to("python3")
        elif key == "abs":
            (root / "usr/bin/host").symlink_to("/etc/passwd")
        else:
            (root / "bin").symlink_to("usr/bin")
    (root / "usr/bin/python3").chmod(0o755)
    return root


def test_digest_matches_a_hand_written_canonical_form(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "d").mkdir(parents=True)
    (root / "d/f").write_bytes(b"abc")
    (root / "d/f").chmod(0o644)
    (root / "d").chmod(0o755)
    root.chmod(0o755)
    (root / "l").symlink_to("d/f")
    (root / "s p%").symlink_to("\u00e9 x")  # bytes c3 a9 20 78
    os.setxattr(root / "d/f", "user.k=v,w", b"1")  # `=` and `,` must be escaped
    abc = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    lines = [  # oracle: the documented format, written out by hand
        "quantweave.rootfs_tree/3",
        ". d 755 0 - -",
        "d d 755 0 - -",
        f"d/f f 644 3 {abc} user.k%3Dv%2Cw={hashlib.sha256(b'1').hexdigest()}",
        "l l 777 3 d/f -",
        "s%20p%25 l 777 4 %C3%A9%20x -",
    ]
    want = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    got = cr.tree_digest(root)
    assert got == {"path": str(root), "sha256_tree": want, "entries": 4,
                   "format": "quantweave.rootfs_tree/3"}  # fmt: skip


def test_digest_does_not_depend_on_the_locale(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / os.fsdecode("caf\u00e9".encode())).mkdir(parents=True)
    (root / os.fsdecode(b"n\xff")).write_bytes(b"SYNTHETIC")
    (root / "lnk").symlink_to(os.fsdecode("\u00fcber".encode()))
    base = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG"))}
    probe = "import sys; print(sys.getfilesystemencoding())"
    got = {}
    for name, extra in (
        ("utf8", {"LC_ALL": "C.UTF-8"}),
        ("c", {"LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}),
    ):  # fmt: skip
        env = base | extra
        enc = subprocess.run([sys.executable, "-c", probe], env=env,
                             capture_output=True, text=True, check=True)  # fmt: skip
        got[name] = enc.stdout.strip()
        out = subprocess.run([sys.executable, str(TOOL), "digest", str(root)],
                             env=env, capture_output=True, check=True)  # fmt: skip
        got[name + "_digest"] = json.loads(out.stdout)["sha256_tree"]
    assert (got["utf8"], got["c"]) == ("utf-8", "ascii")  # the two really differ
    assert got["utf8_digest"] == got["c_digest"] == cr.tree_digest(root)["sha256_tree"]


def test_digest_ignores_mtimes_and_creation_order(tmp_path: Path) -> None:
    a = _tree(tmp_path / "a")
    b = _tree(tmp_path / "b", order=["bin", "abs", "file", "rel"])
    for dirpath, dirs, files in os.walk(b):
        for name in [*dirs, *files]:
            os.utime(Path(dirpath, name), (1, 1), follow_symlinks=False)
    first = cr.tree_digest(a)
    assert first["sha256_tree"] == cr.tree_digest(b)["sha256_tree"]
    assert first["entries"] == 7 and HEX64.match(first["sha256_tree"])


@pytest.mark.parametrize(
    "change",
    [
        "content", "size", "mode", "dir_mode", "link_target", "file_to_link",
        "rename", "empty_dir", "setuid", "root_mode", "xattr", "xattr_value",
    ],
)  # fmt: skip
def test_digest_changes_with_every_recorded_field(tmp_path: Path, change: str) -> None:
    root = _tree(tmp_path / "r")
    py = root / "usr/bin/python3"
    if change == "xattr_value":
        os.setxattr(py, "user.qw_synthetic", b"1")
    before = cr.tree_digest(root)["sha256_tree"]
    if change == "content":
        py.write_bytes(b"SYNTHETIC pz\n")  # same size
    elif change == "size":
        py.write_bytes(b"SYNTHETIC py\n\n")
    elif change == "mode":
        py.chmod(0o750)
    elif change == "dir_mode":
        (root / "e").chmod(0o700)
    elif change == "link_target":
        (root / "usr/bin/python").unlink()
        (root / "usr/bin/python").symlink_to("/usr/bin/python3")
    elif change == "file_to_link":
        py.unlink()
        py.symlink_to("SYNTHETIC py\n")
    elif change == "rename":
        py.rename(root / "usr/bin/python4")
    elif change == "empty_dir":
        (root / "e/x").mkdir()
    elif change == "root_mode":
        root.chmod(0o700)
    elif change == "xattr":
        os.setxattr(root / "e", "user.qw_synthetic", b"1")
    elif change == "xattr_value":
        os.setxattr(py, "user.qw_synthetic", b"2")
    else:
        py.chmod(0o4755)
    assert cr.tree_digest(root)["sha256_tree"] != before


def test_digest_frames_odd_names_and_never_follows_links(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    (one / "a\nb").mkdir(parents=True)  # a newline cannot forge a second entry
    (two / "a").mkdir(parents=True)
    (two / "b").mkdir()
    assert cr.tree_digest(one)["sha256_tree"] != cr.tree_digest(two)["sha256_tree"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"1")
    root = tmp_path / "r"
    root.mkdir()
    (root / "lnk").symlink_to(outside)
    before = cr.tree_digest(root)
    (outside / "secret").write_bytes(b"2")
    (outside / "more").write_bytes(b"3")
    assert cr.tree_digest(root) == before and before["entries"] == 1


def test_cli_digest_equals_the_qualification_script(tmp_path: Path) -> None:
    root = _tree(tmp_path / "r")
    qs = _load("qualify_sandbox_for_rootfs", ROOT / "tools" / "qualify_sandbox.py")
    out = subprocess.run(
        [sys.executable, str(TOOL), "digest", str(root)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert json.loads(out.stdout) == qs.tree_digest(root) == cr.tree_digest(root)
    cmd = [sys.executable, str(TOOL), "digest", str(root), "--expect"]
    want = cr.tree_digest(root)["sha256_tree"]
    codes = [subprocess.run([*cmd, x], capture_output=True, check=False).returncode
             for x in (want, "0" * 64, "")]  # fmt: skip
    assert codes == [0, 1, 1]  # an empty expectation is a mismatch, not a skip
    audit = subprocess.run([sys.executable, str(TOOL), "audit", str(root)],
                           capture_output=True, text=True, check=False)  # fmt: skip
    assert audit.returncode == 1 and "missing: usr/local/bin/python3" in audit.stdout


def _entry(char: str, os_: str, arch: str, variant: str | None = None) -> Any:
    plat = {"os": os_, "architecture": arch} | ({"variant": variant} if variant else {})
    return {"digest": "sha256:" + char * 64, "platform": plat}


INDEX: dict[str, Any] = {  # SYNTHETIC OCI index, with an attestation entry
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        _entry("a", "linux", "arm64", "v8"), _entry("b", "linux", "amd64"),
        _entry("c", "unknown", "unknown"), _entry("d", "linux", "arm", "v7"),
    ],
}  # fmt: skip


def test_platform_digest_picks_exactly_one_manifest() -> None:
    assert cr.platform_digest(INDEX, "linux/amd64") == "sha256:" + "b" * 64
    assert cr.platform_digest(INDEX, "linux/arm64/v8") == "sha256:" + "a" * 64
    dup = {"manifests": [*INDEX["manifests"], INDEX["manifests"][1]]}
    bad = {"manifests": [_entry("b", "linux", "amd64") | {"digest": "sha256:xyz"}]}
    for index, plat in ((INDEX, "linux/s390x"), (INDEX, "linux/arm/v6"),
                        (dup, "linux/amd64"),
                        (bad, "linux/amd64"), ({}, "linux/amd64")):  # fmt: skip
        with pytest.raises(ValueError):
            cr.platform_digest(index, plat)


def _manifest() -> dict[str, Any]:
    return {
        "remove": [".dockerenv", "dev/*", "usr/local/bin/pip*", "usr/bin/apt*"],
        "forbidden": ["^pip[0-9.]*$", "^apt(-.*)?$", "^wget$", "^curl$"],
        "required": ["usr/local/bin/python3", "bin"],
    }  # SYNTHETIC


def _image_like(root: Path) -> Path:
    for d in ("usr/local/bin", "usr/bin", "dev", "tmp", "usr/share/doc/apt"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "bin").symlink_to("usr/bin")
    for rel in ("usr/local/bin/python3", "usr/local/bin/pip3", "usr/bin/apt-get",
                "usr/bin/su", ".dockerenv", "dev/console"):  # fmt: skip
        (root / rel).write_bytes(b"SYNTHETIC\n")
        (root / rel).chmod(0o755)
    (root / "usr/local/bin/pip").symlink_to("pip3")
    (root / "usr/bin/su").chmod(0o4755)
    (root / "usr/bin/wall").write_bytes(b"SYNTHETIC\n")
    (root / "usr/bin/wall").chmod(0o2755)
    (root / "tmp").chmod(0o1777)
    return root


def test_prune_removes_patterns_strips_setid_and_audit_passes(tmp_path: Path) -> None:
    root = _image_like(tmp_path / "r")
    problems = cr.audit(root, _manifest())
    assert sorted(problems) == sorted([
        "forbidden: usr/bin/apt-get", "forbidden: usr/local/bin/pip",
        "forbidden: usr/local/bin/pip3", "setid: usr/bin/su", "setid: usr/bin/wall",
    ])  # fmt: skip
    removed = cr.prune(root, _manifest()["remove"])
    assert sorted(removed) == [".dockerenv", "dev/console", "usr/bin/apt-get",
                               "usr/local/bin/pip", "usr/local/bin/pip3"]  # fmt: skip
    assert cr.audit(root, _manifest()) == []
    assert stat.S_IMODE((root / "usr/bin/su").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "usr/bin/wall").stat().st_mode) == 0o755
    assert (root / "usr/share/doc/apt").is_dir()  # docs are not executables
    assert cr.prune(root, _manifest()["remove"]) == []  # idempotent


def test_prune_never_follows_symlinks_out_of_the_root(tmp_path: Path) -> None:
    outside = tmp_path / "host"  # stands in for the host filesystem
    (outside / "bin").mkdir(parents=True)
    (outside / "bin/pip3").write_bytes(b"host file\n")
    (outside / "keep").mkdir()
    (outside / "keep/x").write_bytes(b"host file\n")
    (outside / "bin/python3").write_bytes(b"host file\n")
    root = tmp_path / "r"
    (root / "usr").mkdir(parents=True)
    (root / "usr/local").symlink_to(outside)  # usr/local -> host
    (root / "dev").symlink_to(outside / "keep")  # matched pattern parent is a link
    (root / "usr/bin").mkdir()
    (root / "usr/bin/apt").symlink_to(outside / "keep")  # matched entry is a link
    removed = cr.prune(root, _manifest()["remove"])
    assert removed == ["usr/bin/apt"]  # the link itself, not its target
    assert "missing: usr/local/bin/python3" in cr.audit(root, _manifest())
    assert (outside / "bin/pip3").read_bytes() == b"host file\n"
    assert (outside / "keep/x").read_bytes() == b"host file\n"
    for bad in (Path("/"), root / "usr/local", tmp_path / "missing"):
        with pytest.raises(ValueError):
            cr.prune(bad, [])
    for pattern in ("/etc/x", "../x", "a/../../x", ""):
        with pytest.raises(ValueError):
            cr.prune(root, [pattern])


def test_audit_flags_writable_special_and_missing_entries(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "usr/bin").mkdir(parents=True)
    (root / "w").mkdir()
    (root / "w").chmod(0o777)  # world-writable, no sticky bit
    (root / "f").write_bytes(b"")
    (root / "f").chmod(0o666)
    (root / "lnk").symlink_to("f")  # a link's own mode is not a problem
    os.mkfifo(root / "fifo", 0o644)
    (root / "curl").mkdir()  # a directory named like a tool is not an executable
    (root / "usr/bin/wget").symlink_to("/nonexistent")  # dangling link still counts
    root.chmod(0o777)  # the root itself is audited
    (root / "sticky_file").write_bytes(b"")
    (root / "sticky_file").chmod(0o1666)  # sticky exempts directories only
    os.setxattr(root / "f", "user.qw_synthetic", b"ok")  # not a capability
    got = cr.audit(root, _manifest())
    assert sorted(got) == sorted([
        "world_writable: f", "world_writable: w", "special: fifo",
        "world_writable: sticky_file", "world_writable: .",
        "forbidden: usr/bin/wget", "missing: usr/local/bin/python3", "missing: bin",
    ])  # fmt: skip


def test_unreadable_directory_is_an_error_not_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "r")
    real = os.scandir

    def deny(path: Any = ".") -> Any:
        if os.fsdecode(path).endswith("usr/bin"):
            raise PermissionError(13, "SYNTHETIC denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", deny)
    with pytest.raises(PermissionError):
        cr.tree_digest(root)


@pytest.mark.parametrize("mode", ["host_label", "unsupported", "capability"])
def test_xattr_labels_unsupported_filesystems_and_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = _image_like(tmp_path / "r")
    cr.prune(root, _manifest()["remove"])
    want = cr.tree_digest(root)["sha256_tree"]
    name = {"host_label": "security.selinux", "capability": "security.capability"}

    def fake(path: Any, *, follow_symlinks: bool = True) -> list[str]:
        assert follow_symlinks is False
        if mode == "unsupported":
            raise OSError(errno.ENOTSUP, "SYNTHETIC not supported")
        return [name[mode]] if os.fsdecode(path).endswith("python3") else []

    monkeypatch.setattr(os, "listxattr", fake)
    monkeypatch.setattr(os, "getxattr", lambda *a, **k: b"SYNTHETIC")
    caps = ["capability: usr/local/bin/python3"] if mode == "capability" else []
    assert cr.audit(root, _manifest()) == caps
    assert (cr.tree_digest(root)["sha256_tree"] == want) is (mode != "capability")


def test_xattr_order_does_not_matter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "r")
    for name in ("user.a", "user.b"):
        os.setxattr(root / "usr/bin/python3", name, name.encode())
    want = cr.tree_digest(root)["sha256_tree"]
    real = os.listxattr
    monkeypatch.setattr(os, "listxattr", lambda p, **k: real(p, **k)[::-1])
    assert cr.tree_digest(root)["sha256_tree"] == want


@pytest.mark.parametrize(
    ("call", "code"),
    [("listxattr", errno.EACCES), ("listxattr", errno.EIO),
     ("listxattr", errno.ENODATA),
     ("getxattr", errno.EACCES), ("getxattr", errno.EIO),
     ("getxattr", errno.ENODATA), ("getxattr", errno.ENOTSUP)],
)  # fmt: skip
def test_xattr_read_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: str, code: int
) -> None:
    root = _image_like(tmp_path / "r")
    cr.prune(root, _manifest()["remove"])
    py = root / "usr/local/bin/python3"
    os.setxattr(py, "user.sibling", b"SYNTHETIC")
    real_list, real_get = os.listxattr, os.getxattr

    def listx(path: Any, **k: Any) -> list[str]:  # a capability beside a sibling
        if call == "listxattr" and os.fsdecode(path) == str(py):
            raise OSError(code, "SYNTHETIC")
        extra = ["security.capability"] if os.fsdecode(path) == str(py) else []
        return [*real_list(path, **k), *extra]

    def getx(path: Any, name: Any, **k: Any) -> bytes:
        if os.fsdecode(name) == "user.sibling":
            if call == "getxattr":  # e.g. the sibling vanished (ENODATA)
                raise OSError(code, "SYNTHETIC")
            return bytes(real_get(path, name, **k))
        return b"SYNTHETIC"

    monkeypatch.setattr(os, "listxattr", listx)
    monkeypatch.setattr(os, "getxattr", getx)
    with pytest.raises(OSError) as exc:  # never a silently missed capability
        cr.audit(root, _manifest())
    with pytest.raises(OSError) as exc2:
        cr.tree_digest(root)
    assert exc.value.errno == exc2.value.errno == code


def test_committed_manifest_is_well_formed_and_honest() -> None:
    m = cr.load_manifest(cr.MANIFEST)
    assert m["schema"] == "quantweave.candidate_rootfs/1"
    assert m["base_image"] == "python:3.13-slim" and m["python"] == "3.13"
    assert m["platform"] == "linux/amd64"
    for key in ("base_manifest_digest", "tree_sha256"):  # never invented
        value = m[key]
        assert value is None or HEX64.match(value.removeprefix("sha256:")), key
    forbidden = [re.compile(p) for p in m["forbidden"]]
    for tool in ("pip", "pip3", "pip3.13", "apt", "apt-get", "dpkg", "curl", "wget",
                 "nc", "ssh", "busybox"):  # fmt: skip
        assert any(p.search(tool) for p in forbidden), tool
    assert not any(p.search(n) for p in forbidden for n in ("python3", "sh", "ls"))
    assert "usr/local/lib/python3.13/ensurepip" in m["remove"]
    with pytest.raises(ValueError):
        cr.load_manifest_text(json.dumps(m | {"remove": ["../etc"]}))
    with pytest.raises(ValueError):
        cr.load_manifest_text(json.dumps(m | {"tree_sha256": "abc"}))


def _jobs() -> dict[str, Any]:
    jobs: dict[str, Any] = yaml.safe_load(CI.read_text())["jobs"]
    return jobs


def test_ci_mirror_job_is_least_privilege_and_same_repo_only() -> None:
    job = _jobs()["python-base-image"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert not any("uses" in s for s in job["steps"])  # runs no repo code
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert re.search(r'if \[ "\$SAME_REPO" != "true" \]; then\n\s*echo "::error::'
                     r'[^\n]*cannot create it"; exit 1\n', script)  # fmt: skip
    assert "docker logout ghcr.io" in script
    assert "head.repo.full_name == github.repository" in job["env"]["SAME_REPO"]
    triggers = yaml.safe_load(CI.read_text())[True]  # YAML 1.1 reads `on` as True
    assert set(triggers) == {"pull_request", "push"}  # never pull_request_target
    assert "pull_request_target" not in CI.read_text()


def test_ci_build_job_pins_reads_only_and_checks_the_digest() -> None:
    jobs = _jobs()
    job = jobs["candidate-rootfs"]
    assert job["needs"] == "python-base-image"
    assert job["permissions"] == {"contents": "read", "packages": "read"}
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses)
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert job["env"]["TOOL"] == "tools/candidate_rootfs.py"
    for needle in ('"$TOOL" prune "$dir"', '"$TOOL" audit "$dir"', '"$REPO@$digest"',
                   "--userspec=65534:65534", "docker logout ghcr.io",
                   'mkdir -m 755 "$dir"'):  # fmt: skip
        assert needle in script, needle
    for step in job["steps"]:  # a failing producer in a pipe fails the step
        if re.search(r"(?<!\|)\|(?!\|)", step.get("run", "")):
            assert step["run"].startswith("set -o pipefail\n"), step["name"]
    text = CI.read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}


FAKE_TOOL = """import json, os, sys
a = sys.argv[1:]
if a[0] == "show":
    print(os.environ.get("FAKE_" + a[1].upper(), ""))
elif a[0] == "digest":
    d = os.environ["FAKE_D" + a[1][-1]]
    print(json.dumps({"sha256_tree": d, "entries": 9, "format": "SYNTHETIC"}))
    sys.exit(1 if "--expect" in a and a[a.index("--expect") + 1] != d else 0)
"""
FAKE_BIN = {  # SYNTHETIC stand-ins; the step's own shell logic runs unchanged
    "docker": 'case "$1" in create) echo SYNTHETIC-cid;; esac',
    "sudo": 'case "$1" in tar) cat >/dev/null;; chroot) echo "$FAKE_VER";; '
            '*) exec "$@";; esac',
}  # fmt: skip


def _run_step(tmp_path: Path, prefix: str, fake: dict[str, str]) -> Any:
    """Execute one candidate-rootfs step script as Actions does (`bash -e`)."""
    job = _jobs()["candidate-rootfs"]
    script = next(
        s["run"] for s in job["steps"] if s.get("name", "").startswith(prefix)
    )
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in FAKE_BIN.items():
        (bindir / name).write_text(f"#!/bin/bash\n{body}\n")
        (bindir / name).chmod(0o755)
    (tmp_path / "tool.py").write_text(FAKE_TOOL)
    temp = tmp_path / "runner"
    temp.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}", "RUNNER_TEMP": str(temp),
        "GITHUB_ENV": str(tmp_path / "env"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "sum"),
        "TOOL": str(tmp_path / "tool.py"), "REPO": "ghcr.io/SYNTHETIC/python",
        "PLATFORM": "linux/amd64", "BASE_DIGEST": "sha256:" + "b" * 64, "TREE": "t1",
    } | fake  # fmt: skip
    return subprocess.run(["bash", "-e", "-c", script], env=env, cwd=tmp_path,
                          capture_output=True, text=True, check=False)  # fmt: skip


@pytest.mark.parametrize(("d1", "d2", "ok"), [("t1", "t1", True), ("t1", "t2", False)])
def test_ci_two_exports_must_agree(tmp_path: Path, d1: str, d2: str, ok: bool) -> None:
    r = _run_step(tmp_path, "Build, prune", {"FAKE_D1": d1, "FAKE_D2": d2})
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if ok:
        assert "TREE=t1" in (tmp_path / "env").read_text()
    else:
        assert "different tree digests" in r.stdout


@pytest.mark.parametrize(
    ("base", "tree", "ver", "d1", "why"),
    [
        ("sha256:" + "b" * 64, "t1", "3.13.4", "t1", ""),
        ("", "t1", "3.13.4", "t1", "not pinned"),  # base alone unpinned
        ("sha256:" + "b" * 64, "", "3.13.4", "t1", "not pinned"),
        ("sha256:" + "b" * 64, "t1", "3.12.9", "t1", "is not 3.13"),
        ("sha256:" + "b" * 64, "t1", "3.13.4", "t2", ""),  # --expect mismatch
    ],
)  # fmt: skip
def test_ci_pins_and_python_version_gate_the_job(
    tmp_path: Path, base: str, tree: str, ver: str, d1: str, why: str
) -> None:
    (tmp_path / "runner").mkdir()
    (tmp_path / "runner/digest-1.json").write_text('{"entries": 9, "format": "x"}')
    fake = {"FAKE_BASE_MANIFEST_DIGEST": base, "FAKE_TREE_SHA256": tree,
            "FAKE_PYTHON": "3.13", "FAKE_VER": ver, "FAKE_D1": d1}  # fmt: skip
    r = _run_step(tmp_path, "Check Python", fake)
    passes = bool(base and tree) and ver.startswith("3.13.") and d1 == tree
    assert (r.returncode == 0) is passes, r.stdout + r.stderr
    assert why in r.stdout
