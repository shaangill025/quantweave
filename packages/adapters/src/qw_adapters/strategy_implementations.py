"""The reviewed strategy implementations the qw-assessor process may run (owner
decision 65; decision 49: strategy code is manifest-registered and trusted, never
generated at run time). A stored request cannot hold a Python function, so it
names one by `StrategyRef`: strategy id, version and implementation hash.
`Implementations.resolve` returns the registered build only when the id and
version are registered (`strategy_implementation_unavailable`) and the hash is the
registered one (`strategy_implementation_mismatch`).

The implementation hash (`implementation_hash`) is
`digest(["strategy_implementation", strategy_id, version, qualname,
sha256(source of the module defining build), [[module, sha256(source)], ...]])`,
with `digest` the canonical-JSON SHA-256 of `research_data`. The list (decision
68) is every project module (`PROJECT_PACKAGES`, the workspace packages) the
defining module imports, transitively, with the package `__init__` files those
imports run, sorted by module name. Imports are found by parsing each file's
import statements (anywhere in it, relative ones resolved against its package)
and files are located on sys.path without importing anything; a project import
that cannot be found is refused (`strategy_implementation_unresolved`). The
standard library and the locked third-party packages are not hashed (the
lockfile pins them). The hash depends only on source text and names, not on the
interpreter, the import mode or hash seeds, so it is the same in every process.
Only a module-level function has a stable name: a lambda or a nested function is
refused (`strategy_implementation_unnamed`). A registry is
built from entries carrying their reviewed hash; an entry whose code no longer
hashes to it is refused when the registry is built
(`strategy_implementation_changed`), so edited code cannot run under an old
review. A file in the closure that calls `import_module` or `__import__` (by any
name or attribute) is refused (`strategy_implementation_dynamic_import`): what it
would load cannot be hashed. LIMITATIONS: other dynamic loading (`importlib.util`
loaders, `runpy`, `exec`, and a call through an alias or `getattr`), star imports
of submodules and non-Python files are not seen; a strategy outside the project
packages is hashed by its own file only."""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from qw_domain.backtest import Strategy
from qw_domain.research_data import check_id, digest
from qw_domain.strategy_registry import ParamValue

from qw_adapters.assessor_store import ExperimentStoreError

Build = Callable[[Mapping[str, ParamValue]], Strategy]
# Owner decision 68: the workspace's packages, whose code is hashed when a
# strategy imports it; the standard library and locked third-party code are not.
PROJECT_PACKAGES = ("qw_adapters", "qw_api", "qw_domain", "qw_worker")
_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class StrategyRef:
    """What a stored request holds in place of the code (decision 65)."""

    strategy_id: str
    version: str
    implementation_hash: str

    def __post_init__(self) -> None:
        check_id(self.strategy_id, "strategy")
        check_id(self.version, "version")
        if not _HEX.fullmatch(self.implementation_hash):
            raise ValueError("strategy_ref: implementation_hash is 64 hex digits")


@dataclass(frozen=True, slots=True)
class Implementation:
    ref: StrategyRef
    build: Build


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find(name: str) -> Path | None:
    """The source of module `name` on sys.path, found without importing it."""
    parts = name.split(".")
    for entry in sys.path:
        base = Path(entry or ".").joinpath(*parts)
        for f in (base.joinpath("__init__.py"), base.with_name(f"{parts[-1]}.py")):
            if f.is_file():
                return f
    return None


def _name(path: Path, project: tuple[str, ...]) -> str | None:
    """The project module name of the file at `path`, or None (not project)."""
    path = path.resolve()
    for entry in sys.path:
        try:
            parts = list(path.relative_to(Path(entry or ".").resolve()).parts)
        except ValueError:
            continue
        if parts and parts[0] in project:
            parts[-1] = parts[-1].removesuffix(".py")
            return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    return None


def _dynamic(f: ast.expr) -> bool:
    """A call of `import_module` or `__import__`, by any name or attribute."""
    name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
    return name in ("import_module", "__import__")


def _closure(root: Path, project: tuple[str, ...]) -> dict[str, Path]:
    """Every project module `root` imports, transitively, with its package
    parents (their `__init__` runs too), by parsing import statements."""
    found: dict[str, Path] = {}
    todo = [(_name(root, project), root)]
    while todo:
        name, path = todo.pop()
        package = None
        if name is not None:
            package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        wanted: list[tuple[str, bool]] = []  # (module, must resolve)
        if path == root and package:  # the root's own packages run at import
            wanted.append((package, True))
        for node in ast.walk(ast.parse(path.read_bytes(), str(path))):
            if isinstance(node, ast.Call) and _dynamic(node.func):  # gap 2
                raise ValueError(f"strategy_implementation_dynamic_import: {path}")
            if isinstance(node, ast.Import):
                wanted += [(a.name, True) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    if package is None:  # a relative import outside the project
                        continue
                    pkg = package.split(".")
                    up = pkg[: len(pkg) - node.level + 1]
                    base = ".".join([*up, *([base] if base else [])])
                wanted.append((base, True))
                wanted += [(f"{base}.{a.name}", False) for a in node.names]
        for mod, must in wanted:
            parts = mod.split(".")
            if not mod or parts[0] not in project:
                continue
            for i in range(1, len(parts) + 1):
                sub = ".".join(parts[:i])
                if sub in found:
                    continue
                f = _find(sub)
                if f is None:
                    if must or i < len(parts):
                        raise ValueError(f"strategy_implementation_unresolved: {sub}")
                    continue  # `from a import b`: b is a name, not a module
                found[sub] = f
                todo.append((sub, f))
    return found


def implementation_hash(
    strategy_id: str, version: str, build: Build,
    project: tuple[str, ...] = PROJECT_PACKAGES,
) -> str:  # fmt: skip
    name = getattr(build, "__qualname__", "")
    if not name or "<" in name:  # <lambda>, <locals>: no stable name
        raise ValueError(f"strategy_implementation_unnamed: {name!r}")
    source = inspect.getsourcefile(build)
    if source is None:
        raise ValueError(f"strategy_implementation_unnamed: {name!r} has no source")
    root = Path(source)
    mods = [[n, _sha(f)] for n, f in sorted(_closure(root, project).items())]
    return digest(["strategy_implementation", strategy_id, version, name, _sha(root),
                   mods])  # fmt: skip


class Implementations:
    """A built-in registry: (strategy id, version) -> its reviewed build."""

    def __init__(self, entries: Iterable[tuple[str, str, Build, str]]) -> None:
        got: dict[tuple[str, str], Implementation] = {}
        for sid, version, build, reviewed in entries:
            ref = StrategyRef(sid, version, reviewed)
            if implementation_hash(sid, version, build) != reviewed:
                raise ValueError(f"strategy_implementation_changed: {sid} {version}")
            if (sid, version) in got:
                raise ValueError(f"strategy_implementation_duplicate: {sid} {version}")
            got[sid, version] = Implementation(ref, build)
        self._entries = MappingProxyType(got)

    def resolve(self, ref: StrategyRef) -> Implementation:
        got = self._entries.get((ref.strategy_id, ref.version))
        if got is None:
            raise ExperimentStoreError("strategy_implementation_unavailable",
                                       f"{ref.strategy_id} {ref.version}")  # fmt: skip
        if got.ref.implementation_hash != ref.implementation_hash:
            raise ExperimentStoreError("strategy_implementation_mismatch",
                                       f"{ref.strategy_id} {ref.version}")  # fmt: skip
        return got
