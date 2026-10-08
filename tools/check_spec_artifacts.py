#!/usr/bin/env python3
"""Consistency checks for the specification artifacts committed under docs/spec.

Checks that the SHA-256 manifest lists exactly the expected files and that each hash
matches; that the workbook-derived register counts equal the counts reported in
docs/spec/VALIDATION.md (transcribed into EXPECTED_COUNTS); task ID uniqueness; that task
prerequisites resolve and form an acyclic graph; and task-to-requirement traceability.
Standard library only. This checks planning artifacts, not application behaviour.

`--self-test` copies the artifacts to a temporary directory, injects known defects and
confirms that each one is reported.
"""

from __future__ import annotations

import csv
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
DERIVED = SPEC / "derived"

# Counts as reported by the package validator in docs/spec/VALIDATION.md.
EXPECTED_COUNTS = {
    "tasks.csv": 66,
    "requirements.csv": 99,
    "decisions.csv": 79,
    "gates.csv": 16,
    "qualifications.csv": 24,
    "risks.csv": 18,
    "sources.csv": 26,
}


IMPORTED_FILES = (
    "BOOTSTRAP_PROMPT.md",
    "MASTER_SPEC.md",
    "Portfolio_Intelligence_Implementation_Tracker_v1.0.xlsx",
    "Portfolio_Intelligence_Master_Spec_v1.0.pdf",
    "VALIDATION.md",
)
MANIFEST_FILES = frozenset(IMPORTED_FILES) | {f"derived/{n}" for n in EXPECTED_COUNTS}


def read_rows(name: str) -> list[dict[str, str]]:
    with open(DERIVED / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split_ids(cell: str) -> list[str]:
    return [p.strip() for p in cell.split(",") if p.strip()]


def check_manifest() -> list[str]:
    errors = []
    listed: set[str] = set()
    for n, line in enumerate(
        (SPEC / "SHA256SUMS").read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            errors.append(f"manifest: malformed line {n}")
            continue
        digest, name = parts[0], parts[1].strip()
        listed.add(name)
        path = SPEC / name
        if not path.is_file():
            errors.append(f"manifest: missing {name}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            errors.append(f"manifest: hash mismatch for {name}")
    for name in sorted(MANIFEST_FILES - listed):
        errors.append(f"manifest: {name} not listed")
    for name in sorted(listed - MANIFEST_FILES):
        errors.append(f"manifest: unexpected entry {name}")
    return errors


def check_counts() -> list[str]:
    errors = []
    for name, expected in EXPECTED_COUNTS.items():
        observed = len(read_rows(name))
        if observed != expected:
            errors.append(f"{name}: expected {expected} rows, observed {observed}")
    return errors


def check_tasks() -> list[str]:
    errors = []
    tasks = read_rows("tasks.csv")
    ids = [t["Task"] for t in tasks]
    if len(ids) != len(set(ids)):
        errors.append("tasks.csv: duplicate task IDs")
    known_tasks = set(ids)
    known_reqs = {r["Requirement"] for r in read_rows("requirements.csv")}

    graph: dict[str, list[str]] = {}
    covered: set[str] = set()
    for t in tasks:
        prereqs = split_ids(t["Prerequisites"])
        for p in prereqs:
            if p not in known_tasks:
                errors.append(f"{t['Task']}: unknown prerequisite {p}")
        graph[t["Task"]] = [p for p in prereqs if p in known_tasks]
        for r in split_ids(t["Requirements"]):
            if r not in known_reqs:
                errors.append(f"{t['Task']}: unknown requirement {r}")
            covered.add(r)

    uncovered = sorted(known_reqs - covered)
    if uncovered:
        errors.append(f"requirements without a task: {', '.join(uncovered)}")

    # Depth-first search for cycles: 0 = unvisited, 1 = on stack, 2 = done.
    state = dict.fromkeys(graph, 0)

    def visit(node: str) -> bool:
        state[node] = 1
        for dep in graph[node]:
            if state[dep] == 1 or (state[dep] == 0 and visit(dep)):
                return True
        state[node] = 2
        return False

    if any(state[n] == 0 and visit(n) for n in graph):
        errors.append("tasks.csv: prerequisite graph has a cycle")
    return errors


def run_checks() -> list[str]:
    return check_manifest() + check_counts() + check_tasks()


def self_test() -> int:
    """Inject known defects into a temporary copy and require each to be reported."""
    global SPEC, DERIVED
    original = SPEC

    def cycle(spec: Path) -> None:
        path = spec / "derived" / "tasks.csv"
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        col = rows[0].index("Prerequisites")
        for row in rows:
            if row[0] == "T001":
                row[col] = "T002"  # T002 already depends on T001
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)

    def drop_line(spec: Path) -> None:
        manifest = spec / "SHA256SUMS"
        manifest.write_text("".join(manifest.read_text().splitlines(True)[1:]))

    def malformed(spec: Path) -> None:
        with open(spec / "SHA256SUMS", "a") as f:
            f.write("not-a-hash\n")

    cases = {
        "cycle": (cycle, "cycle"),
        "empty manifest": (lambda s: (s / "SHA256SUMS").write_text(""), "not listed"),
        "dropped manifest line": (drop_line, "not listed"),
        "malformed manifest line": (malformed, "malformed"),
    }
    failures = 0
    for label, (inject, expected) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            SPEC = Path(tmp) / "spec"
            DERIVED = SPEC / "derived"
            shutil.copytree(original, SPEC)
            inject(SPEC)
            errors = run_checks()
        caught = any(expected in e for e in errors)
        failures += not caught
        print(
            f"{'PASS' if caught else 'FAIL'} self-test: {label} -> {'reported' if caught else 'NOT reported'}"
        )
    SPEC, DERIVED = original, original / "derived"
    clean = run_checks()
    if clean:
        failures += 1
        print("FAIL self-test: unmodified artifacts do not pass")
    return 1 if failures else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    errors = run_checks()
    for e in errors:
        print(f"FAIL {e}")
    if errors:
        return 1
    print(
        "PASS spec artifacts: manifest, register counts, task IDs, prerequisite DAG, traceability"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
