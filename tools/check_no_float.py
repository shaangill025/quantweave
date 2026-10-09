#!/usr/bin/env python3
"""Ban binary floats in money modules (ADR-013 decimal policy, control 1).

Fails on a float literal or any use of the name `float`: annotations (quoted ones too),
`float(...)` calls, casts and isinstance checks. `mypy --strict` accepts `Decimal ==
float` and the decimal context does not trap float equality, so this AST check and the
value types' typed comparisons (control 2) are the controls. Scans MONEY_PATHS by
default, or the paths given as arguments. Standard library only.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Money modules per ADR-013. Paths that do not exist yet are skipped until created.
MONEY_PATHS = (
    "packages/domain/src",
    "packages/portfolio/src",
    "packages/strategies/src/qw_strategies/sizing",
    "packages/strategies/src/qw_strategies/valuation",
)
# Reviewed exemptions: repo-relative file path -> reason. Keep short; none needed yet.
ALLOWLIST: dict[str, str] = {}


def _quoted_annotations(tree: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        for ann in (getattr(node, "annotation", None), getattr(node, "returns", None)):
            if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                expr = ast.parse(ann.value, mode="eval").body
                found.append(ast.copy_location(expr, ann))
    return found


def findings(path: Path) -> list[str]:
    """Float literals, the name `float` (also in quoted annotations), `from_float`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    found: list[str] = []
    for root in [tree, *_quoted_annotations(tree)]:
        for node in ast.walk(root):
            at = f"{path}:{getattr(node if root is tree else root, 'lineno', 0)}"
            if isinstance(node, ast.Constant) and isinstance(
                node.value, float | complex
            ):
                found.append(f"{at}: float literal {node.value!r}")
            elif isinstance(node, ast.Name) and node.id == "float":
                kind = "float(...) call" if id(node) in calls else "float reference"
                found.append(f"{at}: {kind}")
            elif isinstance(node, ast.Attribute) and node.attr == "from_float":
                found.append(f"{at}: Decimal.from_float")
    return found


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv] or [ROOT / p for p in MONEY_PATHS]
    files = [t for t in targets if t.is_file()]
    files += [f for t in targets if t.is_dir() for f in sorted(t.rglob("*.py"))]
    if not files:
        print(f"FAIL: no Python files under {[str(t) for t in targets]}")
        return 2
    problems: list[str] = []
    for file in files:
        if file.resolve().as_posix().removeprefix(f"{ROOT}/") not in ALLOWLIST:
            problems += findings(file)
    for problem in problems:
        print(problem)
    verdict = "FAIL" if problems else "PASS"
    print(f"{verdict}: {len(problems)} float use(s) in {len(files)} file(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
