"""Repository guards: core import boundaries (T008 review §2), float ban (ADR-013).

Both guards get a negative control on an injected SYNTHETIC sample and a positive run
on the real tree.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ROOT / "packages"

# Denied in every packages/* file (src and tests): web/UI frameworks, provider SDKs.
# Matched by dotted prefix. New packages are covered without changes here.
DENIED = (
    # web and UI frameworks
    "fastapi", "starlette", "pydantic", "flask", "django", "uvicorn", "aiohttp.web",
    "tornado", "sanic", "litestar", "reactpy", "react", "streamlit", "gradio", "dash",
    "panel", "nicegui", "jinja2",
    # model-provider SDKs
    "anthropic", "openai", "google.generativeai", "google.genai", "mistralai", "cohere",
    "langchain", "langchain_openai", "litellm", "groq", "together", "ollama",
    # broker and market-data SDKs
    "alpaca", "alpaca_trade_api", "ib_insync", "ib_async", "ibapi", "questrade_api",
    "wealthsimple", "robin_stocks", "tda", "schwab", "polygon", "yfinance", "finnhub",
)  # fmt: skip

# Third-party imports a package's src/ may use beyond the stdlib and its own name.
# Every directory under packages/ must have an entry (review §2 table).
ALLOWED_THIRD_PARTY: dict[str, frozenset[str]] = {
    "domain": frozenset(),  # stdlib only
}


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Absolute imports, plus literal targets of importlib.import_module/__import__."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Call) and node.args:
            func, arg = node.func, node.args[0]
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in {"import_module", "__import__"} and isinstance(
                arg, ast.Constant
            ):
                found.append((node.lineno, str(arg.value)))
    return sorted(found)


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def boundary_violations(packages_dir: Path) -> list[str]:
    problems: list[str] = []
    for package in sorted(p for p in packages_dir.iterdir() if p.is_dir()):
        allowed = ALLOWED_THIRD_PARTY.get(package.name)
        if allowed is None:
            problems.append(f"{package.name}: no entry in ALLOWED_THIRD_PARTY")
            continue
        own = {p.name for p in (package / "src").glob("*") if p.is_dir()}
        for file in sorted(package.rglob("*.py")):
            in_src = (package / "src") in file.parents
            for line, module in imported_modules(file):
                where = f"{file.relative_to(packages_dir)}:{line} imports {module}"
                top = module.split(".")[0]
                if any(_matches(module, denied) for denied in DENIED):
                    problems.append(f"{where} (denied in core)")
                elif in_src and not (
                    top in sys.stdlib_module_names or top in own or top in allowed
                ):
                    problems.append(f"{where} (not allowed for {package.name})")
    return problems


def test_core_packages_respect_import_boundaries() -> None:
    assert (PACKAGES / "domain" / "src" / "qw_domain").is_dir()
    assert boundary_violations(PACKAGES) == []


def test_boundary_check_fires_on_injected_sample(tmp_path: Path) -> None:
    src = tmp_path / "domain" / "src" / "qw_domain"
    src.mkdir(parents=True)
    (src / "bad.py").write_text(
        "# SYNTHETIC negative-control fixture\n"
        "import fastapi\n"
        "from openai import OpenAI\n"
        "import importlib\n"
        "importlib.import_module('alpaca.trading')\n"
        "import psycopg\n"
        "from qw_domain import decimals\n"
        "from . import sibling\n"
        "import decimal\n"
    )
    tests = tmp_path / "domain" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("import pytest\nfrom starlette import status\n")
    (tmp_path / "newpkg").mkdir()
    problems = boundary_violations(tmp_path)
    assert problems == [
        "domain/src/qw_domain/bad.py:2 imports fastapi (denied in core)",
        "domain/src/qw_domain/bad.py:3 imports openai (denied in core)",
        "domain/src/qw_domain/bad.py:5 imports alpaca.trading (denied in core)",
        "domain/src/qw_domain/bad.py:6 imports psycopg (not allowed for domain)",
        "domain/tests/test_x.py:2 imports starlette (denied in core)",
        "newpkg: no entry in ALLOWED_THIRD_PARTY",
    ]


def run_no_float(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check_no_float.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_no_float_passes_on_real_money_modules() -> None:
    result = run_no_float()
    assert result.returncode == 0, result.stdout
    assert result.stdout.startswith("PASS: 0 float use(s)")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from decimal import Decimal\ndef f(a: Decimal, b: float) -> bool:\n"
         "    return a == b\n", ":2: float reference"),
        ("x = 0.1\n", ":1: float literal 0.1"),
        ("x = 1e3\n", ":1: float literal 1000.0"),
        ("def f(s: str) -> object:\n    return float(s)\n", ":2: float(...) call"),
        ("def f() -> 'list[float]':\n    return []\n", ":1: float reference"),
        ("y: 'float | None' = None\n", ":1: float reference"),
        ("import decimal\nz = decimal.Decimal.from_float\n", ":2: Decimal.from_float"),
        ("from typing import cast\nw = cast(float, 1)\n", ":2: float reference"),
    ],
)  # fmt: skip
def test_no_float_fires_on_injected_sample(
    tmp_path: Path, source: str, expected: str
) -> None:
    sample = tmp_path / "synthetic_money.py"  # SYNTHETIC negative-control fixture
    sample.write_text(source)
    result = run_no_float(str(sample))
    assert result.returncode == 1
    assert expected in result.stdout
    assert "FAIL" in result.stdout


def test_no_float_fails_closed_without_files(tmp_path: Path) -> None:
    assert run_no_float(str(tmp_path)).returncode == 2
