"""Real `Stager` for T049 code candidates on the gVisor runner (spec §11, §12; R038,
R076, R086). `stage` copies each base file from a trusted checkout, refusing any
whose sha256 differs from the snapshot or that leaves the checkout through a link. It
writes the whole-file patch into a fresh directory under the runner's state dir and
makes that directory read-only. It returns:
- the digest of the tree as written, read back from disk;
- the rootfs tree digest, computed now by the injected function
  (`tools/candidate_rootfs.py` `tree_digest`), which `evaluate` compares with the
  profile's pin before anything runs;
- a fresh 32-byte channel key;
- a `GVisorRunner` that mounts the stage read-only and noexec at /mnt, an empty
  directory of the pinned rootfs (the harness at /mnt/harness.py, the tree at
  /mnt/tree), and writes the key to the guest's stdin.

The rootfs digest is taken at staging. The rootfs is a root-owned host directory
mounted read-only, so after that only host root can change it. `close` removes the
stages. Needs host root to run (the runner's cgroups).
"""

import hashlib
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from qw_domain import code_improvement as ci

from qw_adapters.gvisor_sandbox import GVisorConfig, GVisorRunner

HARNESS = Path(__file__).with_name("sandbox_harness.py")
GUEST = "/mnt"  # exists in the rootfs: runsc creates no host mountpoint


def _read_back(tree: Path) -> dict[str, str]:
    """sha256 of every file written; anything but directories and files is refused."""
    out = {}
    for path in sorted(tree.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ci.CodeError("staged_entry", str(path.relative_to(tree)))
        if path.is_file():
            data = path.read_bytes()
            out[path.relative_to(tree).as_posix()] = hashlib.sha256(data).hexdigest()
    return out


def _chmod_tree(root: Path, files: int, dirs: int) -> None:
    for here, subdirs, names in os.walk(root, topdown=False):
        for n in names:
            os.chmod(os.path.join(here, n), files, follow_symlinks=False)
        for n in subdirs:
            os.chmod(os.path.join(here, n), dirs)
    os.chmod(root, dirs)


class GVisorStager:
    def __init__(self, config: GVisorConfig, base_root: Path,
                 rootfs_digest: Callable[[Path], str]) -> None:  # fmt: skip
        self.config, self.base_root = config, base_root.resolve()
        self.rootfs_digest = rootfs_digest
        self.stages: list[Path] = []

    def _base_file(self, rel: str, sha: str) -> bytes:
        path = self.base_root.joinpath(rel)
        real = path.resolve()
        if real != path or not real.is_file():  # no link, inside the checkout
            raise ci.CodeError("base_file", f"{rel} is not a regular checkout file")
        data = real.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ci.CodeError("base_file", f"{rel} differs from the snapshot")
        return data

    def stage(self, base: ci.Snapshot, patch: ci.Patch) -> ci.Staged:
        rootfs = self.config.rootfs
        if rootfs is None:
            raise ci.CodeError("rootfs", "no rootfs configured")
        files = {p: self._base_file(p, s) for p, s in base.files.items()}
        for ch in patch.changes:
            if ch.content is None:
                files.pop(ch.path, None)
            else:
                files[ch.path] = ch.content
        self.config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="stage-", dir=self.config.state_dir))
        self.stages.append(root)
        tree = root.joinpath("tree")
        for rel, data in files.items():  # domain-validated segments: no "..", no "/"
            dest = tree.joinpath(rel)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "xb") as f:  # never through an existing entry
                    f.write(data)
            except (FileExistsError, IsADirectoryError, NotADirectoryError):
                raise ci.CodeError("path_conflict", rel) from None
        shutil.copyfile(HARNESS, root.joinpath("harness.py"))
        _chmod_tree(root, 0o444, 0o555)
        written = ci.Snapshot(_read_back(tree)).digest
        key = secrets.token_bytes(32)
        runner = GVisorRunner(self.config, binds=((root, GUEST),), stdin=key)
        return ci.Staged(written, self.rootfs_digest(rootfs), runner, key)

    def close(self) -> None:
        """Remove every stage, keeping any that failed for a retry; then raise."""
        failed: list[tuple[Path, OSError]] = []
        for root in self.stages:
            try:
                _chmod_tree(root, 0o644, 0o755)
                shutil.rmtree(root)
            except OSError as exc:
                failed.append((root, exc))
        self.stages = [root for root, _ in failed]
        if failed:
            raise failed[0][1]
