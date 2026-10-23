"""Owner decision 65: the qw-assessor process's built-in, reviewed strategy
registry. A stored request names strategy code by id, version and implementation
hash; only an entry registered with its reviewed hash resolves, and the hash is
computed deterministically from the code. SYNTHETIC strategies only."""

from __future__ import annotations

import hashlib
import inspect
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from qw_adapters import strategy_implementations as si
from qw_adapters.assessor_store import ExperimentStoreError
from qw_domain.backtest import Strategy
from qw_domain.research_data import digest
from qw_domain.strategy_registry import ParamValue
from test_backtest import scripted

ID, V = "str-synth-swing_momentum", "0.1.0"


def synth_in(cfg: Mapping[str, ParamValue]) -> Strategy:  # SYNTHETIC: always in
    return scripted(["1"] * 12)


def synth_out(cfg: Mapping[str, ParamValue]) -> Strategy:  # SYNTHETIC: always out
    return scripted(["0"] * 12)


def code(fn: object) -> str:
    with pytest.raises(ExperimentStoreError) as e:
        fn()  # type: ignore[operator]
    return e.value.code


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_the_hash_names_the_code_and_its_version() -> None:
    h = si.implementation_hash(ID, V, synth_in)
    assert si.implementation_hash(ID, V, synth_out) != h  # other code (name)
    assert si.implementation_hash(ID, "0.2.0", synth_in) != h  # other version
    assert si.implementation_hash("str-synth-other", V, synth_in) != h


PKG = {  # a SYNTHETIC project package: strat imports helper, helper imports deep
    "synthpkg/__init__.py": "",
    "synthpkg/strat.py": (
        "import json  # standard library: never hashed\n"
        "from . import helper\n"
        "def build(cfg):\n    return helper.make(cfg)\n"
    ),
    "synthpkg/helper.py": (
        "import synthpkg.sub.deep\ndef make(cfg):\n    return synthpkg.sub.deep.K\n"
    ),
    "synthpkg/sub/__init__.py": "",
    "synthpkg/sub/deep.py": "K = 1\n",
    "synthpkg/unused.py": "K = 2\n",  # imported by nothing: not hashed
}


def _forget() -> None:
    for name in [k for k in sys.modules if k.split(".")[0] == "synthpkg"]:
        del sys.modules[name]


@pytest.fixture
def pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for rel, text in PKG.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    monkeypatch.syspath_prepend(str(tmp_path))
    _forget()
    yield tmp_path
    _forget()


def _strat(pkg: Path) -> Any:
    import synthpkg.strat  # type: ignore[import-not-found]  # static: this module's own code may not import
    # dynamically (gap 2), and synthpkg is not project code for it

    return synthpkg.strat.build


def test_decision_68_project_imports_are_hashed_transitively(pkg: Path) -> None:
    build, project = _strat(pkg), ("synthpkg",)
    h = si.implementation_hash(ID, V, build, project=project)
    mods = [[n, sha(pkg / f)] for n, f in sorted({  # the exact closure, by hand
        "synthpkg": "synthpkg/__init__.py", "synthpkg.helper": "synthpkg/helper.py",
        "synthpkg.sub": "synthpkg/sub/__init__.py",
        "synthpkg.sub.deep": "synthpkg/sub/deep.py"}.items())]  # fmt: skip
    assert h == digest(["strategy_implementation", ID, V, "build",
                        sha(pkg / "synthpkg" / "strat.py"), mods])  # fmt: skip
    for rel in ("synthpkg/helper.py", "synthpkg/sub/deep.py",
                "synthpkg/sub/__init__.py", "synthpkg/__init__.py"):  # fmt: skip
        f = pkg / rel
        f.write_text(f.read_text() + "# a reviewed line changed\n")
        assert si.implementation_hash(ID, V, build, project=project) != h, rel
        f.write_text(PKG[rel])
        assert si.implementation_hash(ID, V, build, project=project) == h, rel
    (pkg / "synthpkg" / "unused.py").write_text("K = 3\n")  # not imported
    assert si.implementation_hash(ID, V, build, project=project) == h
    (pkg / "synthpkg" / "helper.py").write_text(PKG["synthpkg/helper.py"] + "#\n")
    assert si.implementation_hash(ID, V, build, project=()) == digest(
        [
            "strategy_implementation",
            ID,
            V,
            "build",
            sha(pkg / "synthpkg" / "strat.py"),
            [],
        ]
    )  # not project code: not followed


def test_decision_68_the_roots_own_packages_are_hashed(pkg: Path) -> None:
    (pkg / "synthpkg" / "sub" / "lone.py").write_text("def build(cfg):\n    return 1\n")
    import synthpkg.sub.lone  # type: ignore[import-not-found]

    build = synthpkg.sub.lone.build  # imports nothing
    h = si.implementation_hash(ID, V, build, project=("synthpkg",))
    for init in ("synthpkg/__init__.py", "synthpkg/sub/__init__.py"):
        (pkg / init).write_text("# changed\n")  # runs when lone is imported
        assert si.implementation_hash(ID, V, build, project=("synthpkg",)) != h
        (pkg / init).write_text("")


@pytest.mark.parametrize("call", [
    "importlib.import_module('json')", "__import__('json')",
    "import_module('json')", "builtins.__import__('json')"])  # fmt: skip
def test_gap_2_a_dynamic_import_in_the_closure_is_refused(pkg: Path, call: str) -> None:
    build = _strat(pkg)
    h = si.implementation_hash(ID, V, build, project=("synthpkg",))
    f = pkg / "synthpkg" / "sub" / "deep.py"  # two imports down from the strategy
    f.write_text(PKG["synthpkg/sub/deep.py"] + f"def load():\n    return {call}\n")
    with pytest.raises(ValueError, match="strategy_implementation_dynamic_import"):
        si.implementation_hash(ID, V, build, project=("synthpkg",))
    f.write_text(PKG["synthpkg/sub/deep.py"] + "# the name import_module, not a call\n")
    assert si.implementation_hash(ID, V, build, project=("synthpkg",)) != h


def test_decision_68_an_unresolvable_project_import_is_refused(pkg: Path) -> None:
    (pkg / "synthpkg" / "helper.py").write_text(
        "def make(cfg):\n    import synthpkg.missing  # lazily: parsed, not run\n"
    )
    with pytest.raises(ValueError, match="strategy_implementation_unresolved"):
        si.implementation_hash(ID, V, _strat(pkg), project=("synthpkg",))


def test_decision_68_the_project_is_the_workspace_packages() -> None:
    root = Path(__file__).parents[3]
    text = (root / "pyproject.toml").read_text()
    members = tomllib.loads(text)["tool"]["uv"]["workspace"]["members"]
    found = {d.name for m in members for src in root.glob(f"{m}/src")
             for d in src.iterdir() if (d / "__init__.py").is_file()}  # fmt: skip
    assert set(si.PROJECT_PACKAGES) == found
    src = Path(inspect.getfile(synth_in))  # this module imports project code
    assert si.implementation_hash(ID, V, synth_in) != digest(
        ["strategy_implementation", ID, V, "synth_in", sha(src), []]
    )


def test_the_hash_is_the_same_in_a_fresh_process() -> None:
    here = si.implementation_hash(ID, V, synth_in)
    probe = (
        "import sys; sys.path[:0] = sys.argv[1:3]; import test_strategy_implementations"
        " as t; from qw_adapters import strategy_implementations as si; "
        f"print(si.implementation_hash({ID!r}, {V!r}, t.synth_in))"
    )  # a new interpreter: no state shared with this one (hash seeds differ)
    tests = str(Path(__file__).parent)
    domain = str(Path(__file__).parents[2] / "domain" / "tests")
    out = subprocess.run([sys.executable, "-c", probe, tests, domain],
                         capture_output=True, text=True, check=True)  # fmt: skip
    assert out.stdout.strip() == here


def test_only_reviewed_entries_resolve() -> None:
    h = si.implementation_hash(ID, V, synth_in)
    reg = si.Implementations([(ID, V, synth_in, h)])
    got = reg.resolve(si.StrategyRef(ID, V, h))
    assert (got.build, got.ref) == (synth_in, si.StrategyRef(ID, V, h))
    assert code(lambda: reg.resolve(si.StrategyRef(ID, "0.2.0", h))) == (
        "strategy_implementation_unavailable")  # fmt: skip
    other = si.implementation_hash(ID, V, synth_out)
    assert code(lambda: reg.resolve(si.StrategyRef(ID, V, other))) == (
        "strategy_implementation_mismatch")  # fmt: skip
    empty = si.Implementations(())  # the built-in registry starts empty
    assert code(lambda: empty.resolve(si.StrategyRef(ID, V, h))) == (
        "strategy_implementation_unavailable")  # fmt: skip


def test_a_registry_refuses_unreviewed_or_unnamed_code() -> None:
    h = si.implementation_hash(ID, V, synth_in)
    with pytest.raises(ValueError, match="strategy_implementation_changed"):
        si.Implementations([(ID, V, synth_out, h)])  # the reviewed hash is stale
    with pytest.raises(ValueError, match="strategy_implementation_duplicate"):
        si.Implementations([(ID, V, synth_in, h), (ID, V, synth_in, h)])
    with pytest.raises(ValueError, match="strategy_implementation_unnamed"):
        si.implementation_hash(ID, V, lambda cfg: synth_in(cfg))
    with pytest.raises(ValueError, match="strategy_implementation_unnamed"):

        def local(cfg: Mapping[str, ParamValue]) -> Strategy:
            return synth_in(cfg)

        si.implementation_hash(ID, V, local)
    with pytest.raises(ValueError, match="strategy_ref"):
        si.StrategyRef(ID, V, "not-a-hash")
