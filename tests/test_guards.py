"""Repository guards: import boundaries (T008 review §2), float ban (ADR-013).

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
APPS = ROOT / "apps"

# Web and UI frameworks: denied in every packages/* file, allowed in apps/api only.
WEB = (
    "fastapi", "starlette", "pydantic", "flask", "django", "uvicorn", "aiohttp.web",
    "tornado", "sanic", "litestar", "reactpy", "react", "streamlit", "gradio", "dash",
    "panel", "nicegui", "jinja2",
)  # fmt: skip
# Denied in every packages/* and apps/* file (src and tests). Matched by dotted
# prefix. New packages are covered without changes here.
PROVIDERS = (
    # model-provider SDKs
    "anthropic", "openai", "google.generativeai", "google.genai", "mistralai", "cohere",
    "langchain", "langchain_openai", "litellm", "groq", "together", "ollama",
    # broker and market-data SDKs
    "alpaca", "alpaca_trade_api", "ib_insync", "ib_async", "ibapi", "questrade_api",
    "wealthsimple", "robin_stocks", "tda", "schwab", "polygon", "yfinance", "finnhub",
)  # fmt: skip
DENIED = WEB + PROVIDERS

# Third-party imports a package's non-test code may use beyond the stdlib and its own
# name. Every directory under packages/ must have an entry (review §2 table).
ALLOWED_THIRD_PARTY: dict[str, frozenset[str]] = {
    # the only package that may import psycopg; domain per review section 2
    "adapters": frozenset({"psycopg", "qw_domain"}),
    "domain": frozenset(),  # stdlib only
}
# apps/*: the API is the composition root (review §2). It reaches PostgreSQL only
# through qw_adapters, so psycopg stays adapter-only. apps/web has no Python.
APP_ALLOWED_THIRD_PARTY: dict[str, frozenset[str]] = {
    "api": frozenset(
        {"fastapi", "starlette", "pydantic", "argon2", "qw_adapters", "qw_domain"}
    ),
    "web": frozenset(),
}
# Standard-library modules denied in a package's non-test code (I/O, processes, code
# loading). Matched by dotted prefix like DENIED.
DENIED_STDLIB: dict[str, tuple[str, ...]] = {
    "domain": (
        "socket", "socketserver", "ssl", "http", "urllib", "ftplib", "smtplib",
        "poplib", "imaplib", "xmlrpc", "webbrowser", "sqlite3", "dbm", "shelve",
        "subprocess", "asyncio.subprocess", "multiprocessing", "ctypes", "pickle",
        "marshal", "runpy", "importlib.util", "zipimport", "os", "asyncio",
        "threading", "concurrent", "shutil", "select", "selectors", "tempfile",
        "wsgiref",
    ),
}  # fmt: skip
DYNAMIC = "<non-literal>"
IMPORTERS = {"import_module", "__import__"}


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Absolute imports and import_module/__import__ targets, including aliased and
    keyword calls. A target that is not a string literal is reported as DYNAMIC."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = set(IMPORTERS)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # `from asyncio import subprocess` is checked as asyncio.subprocess.
            found += [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
            aliases |= {
                a.asname for a in node.names if a.name in IMPORTERS and a.asname
            }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else ""
        if (isinstance(func, ast.Name) and func.id in aliases) or name in IMPORTERS:
            target = node.args[0] if node.args else None
            target = next((k.value for k in node.keywords if k.arg == "name"), target)
            literal = target.value if isinstance(target, ast.Constant) else None
            found.append(
                (node.lineno, literal if isinstance(literal, str) else DYNAMIC)
            )
    return sorted(found)


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def boundary_violations(
    packages_dir: Path,
    rules: dict[str, frozenset[str]] = ALLOWED_THIRD_PARTY,
    denied: tuple[str, ...] = DENIED,
) -> list[str]:
    problems: list[str] = []
    for package in sorted(p for p in packages_dir.iterdir() if p.is_dir()):
        allowed = rules.get(package.name)
        if allowed is None:
            problems.append(f"{package.name}: no entry in ALLOWED_THIRD_PARTY")
            continue
        own = {p.name for p in (package / "src").glob("*") if p.is_dir()}
        denied_stdlib = DENIED_STDLIB.get(package.name, ())
        for file in sorted(package.rglob("*.py")):
            rel = file.relative_to(package)
            if "node_modules" in rel.parts:
                continue
            is_test = rel.parts[0] == "tests" or rel.name == "conftest.py"
            for line, module in imported_modules(file):
                where = f"{file.relative_to(packages_dir)}:{line} imports {module}"
                top = module.split(".")[0]
                if module == DYNAMIC:
                    problems.append(f"{where} (unreviewable dynamic import)")
                elif _matches(module, denied):
                    problems.append(f"{where} (denied in core)")
                elif is_test:
                    continue
                elif _matches(module, denied_stdlib):
                    problems.append(
                        f"{where} (stdlib module denied for {package.name})"
                    )
                elif not (
                    top in sys.stdlib_module_names or top in own or top in allowed
                ):
                    problems.append(f"{where} (not allowed for {package.name})")
    return problems


def test_core_packages_respect_import_boundaries() -> None:
    assert (PACKAGES / "domain" / "src" / "qw_domain").is_dir()
    assert boundary_violations(PACKAGES) == []


def test_app_boundaries_on_the_tree_and_an_injected_sample(tmp_path: Path) -> None:
    assert boundary_violations(APPS, APP_ALLOWED_THIRD_PARTY, PROVIDERS) == []
    (src := tmp_path / "api" / "src" / "qw_api").mkdir(parents=True)  # SYNTHETIC
    (src / "bad.py").write_text("import fastapi, qw_adapters, psycopg, openai\n")
    (tmp_path / "worker").mkdir()
    assert boundary_violations(tmp_path, APP_ALLOWED_THIRD_PARTY, PROVIDERS) == [
        "api/src/qw_api/bad.py:1 imports openai (denied in core)",
        "api/src/qw_api/bad.py:1 imports psycopg (not allowed for api)",
        "worker: no entry in ALLOWED_THIRD_PARTY",
    ]


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
        "from importlib import import_module as load\n"
        "load('anthropic')\n"
        "importlib.import_module(name='httpx')\n"
        "importlib.import_module('open' + 'ai')\n"
        "__import__(f'{x}')\n"
        "import socket, urllib.request\n"
        "from asyncio import subprocess\n"
        "import asyncio.subprocess\n"
        "import json, asyncio\n"
    )
    tests = tmp_path / "domain" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text(
        "import pytest, socket\nfrom starlette import status\n"
        "import importlib\nimportlib.import_module(name)\n"
    )
    (tmp_path / "domain" / "stray.py").write_text(
        "import requests\nimport subprocess\n"
    )
    (tmp_path / "newpkg").mkdir()
    bad, test_x, stray = (
        f"domain/{p}:" for p in ("src/qw_domain/bad.py", "tests/test_x.py", "stray.py")
    )
    assert boundary_violations(tmp_path) == [
        f"{bad}2 imports fastapi (denied in core)",
        f"{bad}3 imports openai.OpenAI (denied in core)",
        f"{bad}5 imports alpaca.trading (denied in core)",
        f"{bad}6 imports psycopg (not allowed for domain)",
        f"{bad}11 imports anthropic (denied in core)",
        f"{bad}12 imports httpx (not allowed for domain)",
        f"{bad}13 imports {DYNAMIC} (unreviewable dynamic import)",
        f"{bad}14 imports {DYNAMIC} (unreviewable dynamic import)",
        f"{bad}15 imports socket (stdlib module denied for domain)",
        f"{bad}15 imports urllib.request (stdlib module denied for domain)",
        f"{bad}16 imports asyncio.subprocess (stdlib module denied for domain)",
        f"{bad}17 imports asyncio.subprocess (stdlib module denied for domain)",
        f"{bad}18 imports asyncio (stdlib module denied for domain)",
        f"{stray}1 imports requests (not allowed for domain)",
        f"{stray}2 imports subprocess (stdlib module denied for domain)",
        f"{test_x}2 imports starlette.status (denied in core)",
        f"{test_x}4 imports {DYNAMIC} (unreviewable dynamic import)",
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
    assert result.stdout.startswith("PASS: 0 problem(s)")


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
        ("import decimal\nz = decimal.Decimal.from_float\n", ":2: attribute .from_"),
        ("from typing import cast\nw = cast(float, 1)\n", ":2: float reference"),
        # increment 2a: bypasses found in review
        ("import builtins\nf = builtins.float\n", ":2: attribute .float"),
        ("from builtins import float as f\n", ":1: imports float from builtins"),
        ("from builtins import float\n", ":1: imports float from builtins"),
        ("x: list['float'] = []\n", ":1: float reference"),
        ("x: 'dict[str, \"list[float]\"]' = {}\n", ":1: float reference"),
        ("from typing import cast\nw = cast('float', 1)\n", ":2: float reference"),
        ("import typing\nw = typing.cast(list['float'], [])\n", ":2: float reference"),
        ("type T = 'float | None'\n", ":1: float reference"),
        ("half = 1 / 2\n", ":1: true division"),
        ("x = 4\nx /= 3\n", ":2: true division"),
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


def test_no_float_ignores_floor_division_and_docstrings(tmp_path: Path) -> None:
    sample = tmp_path / "synthetic_ok.py"  # SYNTHETIC positive control
    sample.write_text('"""Never a float."""\nx: "list[int]" = [7 // 2]\n')
    assert run_no_float(str(sample)).returncode == 0


def test_no_float_requires_every_package_to_be_classified(tmp_path: Path) -> None:
    for package in ("domain", "quant", "newpkg", "strategies"):
        (tmp_path / package).mkdir()
    (tmp_path / "domain" / "src").mkdir()
    (tmp_path / "domain" / "src" / "m.py").write_text("x = 1\n")
    result = run_no_float("--packages", str(tmp_path))
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert lines[0] == f"{tmp_path / 'newpkg'}: no MONEY_PATHS or NOT_MONEY entry"
    stale = "strategies/src/qw_strategies/sizing: MONEY_PATHS entry does not exist"
    assert lines[1] == f"{tmp_path}/{stale}"
    assert len(lines) == 4  # unclassified, two stale paths, verdict
    assert lines[-1] == "FAIL: 3 problem(s) in 1 file(s)"


def test_no_float_fails_closed_without_files(tmp_path: Path) -> None:
    assert run_no_float(str(tmp_path)).returncode == 2
