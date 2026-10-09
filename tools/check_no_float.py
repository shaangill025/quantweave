#!/usr/bin/env python3
"""Ban binary floats in money modules (ADR-013 decimal policy, control 1).

Rules, each with a reviewed per-file ALLOWLIST entry as the only exception:
- float-literal: `0.1`, `1e3`, `1j`.
- float-name: any use of the name `float` (annotations, calls, casts, isinstance),
  also inside quoted annotations, quoted subscripts and `cast("...")` arguments.
- float-attr: `builtins.float`, `x.float`, `Decimal.from_float`.
- float-import: `from builtins import float [as x]` (any module).
- division: `/` and `/=` (true division of ints yields a float; Decimal division
  must be a reviewed, quantized step).
- coverage: every directory under packages/ is in MONEY_PATHS or NOT_MONEY, and the
  listed paths of an existing package exist.

`mypy --strict` accepts `Decimal == float` and the decimal context does not trap float
equality, so this check and the value types' typed comparisons (control 2) are the
controls. Known limits (not detected): dynamic access such as getattr(builtins, "f"
"loat") or eval; floats returned by library calls (json.loads without parse_float,
math.*, time.time(), statistics.*); `int.__truediv__`; `**` with a negative exponent.
Standard library only.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Money modules per ADR-013, per package. Packages that do not exist yet are skipped.
MONEY_PATHS: dict[str, tuple[str, ...]] = {
    "adapters": ("src",),  # PostgreSQL adapters persist money (NUMERIC <-> Decimal)
    "domain": ("src",),
    "portfolio": ("src",),
    "strategies": ("src/qw_strategies/sizing", "src/qw_strategies/valuation"),
}
# Packages reviewed as float-tolerant, with the reason.
NOT_MONEY: dict[str, str] = {
    "quant": "statistics may use floats behind declared tolerances (ADR-013)",
}
# Reviewed exceptions: (repo-relative file path, rule) -> reason. None are needed yet.
ALLOWLIST: dict[tuple[str, str], str] = {}


def _quoted(expr: ast.expr) -> list[ast.expr]:
    """Parse string constants inside a type expression, recursively."""
    found: list[ast.expr] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                inner = ast.parse(node.value.strip(), mode="eval").body
            except SyntaxError:
                if re.search(r"\bfloat\b", node.value):
                    found.append(ast.copy_location(ast.Name("float"), node))
                continue
            for sub in ast.walk(inner):
                ast.copy_location(sub, node)
            found += [inner, *_quoted(inner)]
    return found


def _type_expressions(tree: ast.AST) -> list[ast.expr]:
    exprs: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.arg) and node.annotation:
            exprs.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            exprs.append(node.returns)
        elif isinstance(node, ast.TypeAlias):
            exprs.append(node.value)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else ""
            name = func.id if isinstance(func, ast.Name) else name
            if name == "cast":
                exprs.append(node.args[0])
    return exprs


def findings(path: Path) -> list[tuple[str, str]]:
    """(rule, message) pairs for one file, in line order."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    quoted = [q for e in _type_expressions(tree) for q in _quoted(e)]
    found: set[tuple[int, str, str]] = set()
    for root in [tree, *quoted]:
        for node in ast.walk(root):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Constant) and isinstance(
                node.value, float | complex
            ):
                found.add((line, "float-literal", f"float literal {node.value!r}"))
            elif isinstance(node, ast.Name) and node.id == "float":
                kind = "float(...) call" if id(node) in calls else "float reference"
                found.add((line, "float-name", kind))
            elif isinstance(node, ast.Attribute) and node.attr in {
                "float",
                "from_float",
            }:
                found.add((line, "float-attr", f"attribute .{node.attr}"))
            elif isinstance(node, ast.ImportFrom) and any(
                alias.name == "float" for alias in node.names
            ):
                found.add((line, "float-import", f"imports float from {node.module}"))
            elif isinstance(node, ast.BinOp | ast.AugAssign) and isinstance(
                node.op, ast.Div
            ):
                found.add((line, "division", "true division"))
    return [(rule, f"{path}:{line}: {msg}") for line, rule, msg in sorted(found)]


def coverage(packages: Path) -> tuple[list[str], list[Path]]:
    """Unclassified packages and stale entries, plus the money paths to scan."""
    problems: list[str] = []
    targets: list[Path] = []
    for package in sorted(p for p in packages.iterdir() if p.is_dir()):
        if package.name in NOT_MONEY:
            continue
        if package.name not in MONEY_PATHS:
            problems.append(f"{package}: no MONEY_PATHS or NOT_MONEY entry")
            continue
        for sub in MONEY_PATHS[package.name]:
            if not (package / sub).is_dir():
                problems.append(f"{package / sub}: MONEY_PATHS entry does not exist")
            targets.append(package / sub)
    return problems, targets


def main(argv: list[str]) -> int:
    packages = ROOT / "packages"
    if argv[:1] == ["--packages"]:
        packages, argv = Path(argv[1]), argv[2:]
    problems: list[str] = []
    targets = [Path(a) for a in argv]
    if not targets:
        problems, targets = coverage(packages)
    files = [t for t in targets if t.is_file()]
    files += [f for t in targets if t.is_dir() for f in sorted(t.rglob("*.py"))]
    if not files and not problems:
        print(f"FAIL: no Python files under {[str(t) for t in targets]}")
        return 2
    for file in files:
        rel = file.resolve().as_posix().removeprefix(f"{ROOT}/")
        problems += [
            msg for rule, msg in findings(file) if (rel, rule) not in ALLOWLIST
        ]
    for problem in problems:
        print(problem)
    verdict = "FAIL" if problems else "PASS"
    print(f"{verdict}: {len(problems)} problem(s) in {len(files)} file(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
