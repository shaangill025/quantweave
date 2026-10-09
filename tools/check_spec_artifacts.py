#!/usr/bin/env python3
"""Integrity check of docs/spec against the package's own artifact manifest.

docs/spec holds the Portfolio Intelligence specification package as received. Every file
in reports/ARTIFACT_MANIFEST.json must exist with its recorded SHA-256, no unlisted file
may appear, and the manifest itself is excluded as the package documents. A few files are
working state the project is expected to change (task status, regenerated validation
reports); they must still exist but are not hash-compared. For the backlog pair, every
status must be in the receipt-template vocabulary and the CSV and JSON must agree on
task IDs and statuses; other backlog edits are not detected here. Standard library
only. Content consistency (schemas, traceability, oracles) is checked separately by
docs/spec/tools/validate_spec.py.

`--self-test` copies the package to a temporary directory, injects known defects and
confirms that each one is reported.
"""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec"
MANIFEST = "reports/ARTIFACT_MANIFEST.json"

# Package files the project is expected to update after import.
MUTABLE = frozenset(
    {
        "planning/backlog.csv",  # canonical task status
        "planning/backlog.json",
        "reports/VALIDATION.md",  # rewritten by every validate_spec.py run
        "reports/validation_results.json",
    }
)
# Status vocabulary from handoff/TASK_RECEIPT_TEMPLATE.md.
STATUSES = frozenset(
    {"Not started", "In progress", "Blocked", "Completed with evidence"}
)


def check_backlog(spec: Path) -> list[str]:
    try:
        with open(spec / "planning/backlog.csv", newline="", encoding="utf-8") as f:
            rows = {r["task_id"]: r["status"] for r in csv.DictReader(f)}
        tasks = json.loads((spec / "planning/backlog.json").read_text(encoding="utf-8"))
        items = {t["task_id"]: t["status"] for t in tasks["tasks"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"backlog: unreadable ({type(exc).__name__})"]
    errors = [
        f"backlog: {tid} has unknown status {st!r}"
        for tid, st in sorted(rows.items())
        if st not in STATUSES
    ]
    if rows != items:
        errors.append("backlog: CSV and JSON disagree on task IDs or statuses")
    return errors


def check_manifest(spec: Path) -> list[str]:
    errors: list[str] = []
    try:
        entries = json.loads((spec / MANIFEST).read_text(encoding="utf-8"))["files"]
        paths = [e["path"] for e in entries]
        listed = {e["path"]: e["sha256"] for e in entries}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"manifest: unreadable ({type(exc).__name__})"]
    if not listed:
        return ["manifest: lists no files"]
    if len(paths) != len(listed):
        errors.append("manifest: duplicate entries")
    for name in sorted(listed):
        if name.startswith("/") or ".." in Path(name).parts:
            errors.append(f"manifest: unsafe path {name}")
            del listed[name]

    actual = {
        p.relative_to(spec).as_posix() for p in spec.rglob("*") if p.is_file()
    } - {MANIFEST}
    for name in sorted(set(listed) - actual):
        errors.append(f"manifest: missing {name}")
    for name in sorted(actual - set(listed)):
        errors.append(f"manifest: unlisted file {name}")
    for name in sorted(set(listed) & actual - MUTABLE):
        if hashlib.sha256((spec / name).read_bytes()).hexdigest() != listed[name]:
            errors.append(f"manifest: hash mismatch for {name}")
    return errors


def run_checks(spec: Path) -> list[str]:
    errors = check_manifest(spec)
    if not any("planning/backlog" in e for e in errors):
        errors += check_backlog(spec)
    return errors


def self_test() -> int:
    """Inject known defects into a temporary copy and require each to be reported."""

    def edit_spec(spec: Path) -> None:
        with open(spec / "MASTER_SPEC.md", "a", encoding="utf-8") as f:
            f.write("\ntampered\n")

    def empty_manifest(spec: Path) -> None:
        (spec / MANIFEST).write_text('{"files": []}', encoding="utf-8")

    def edit_manifest(spec: Path, change: str) -> None:
        data = json.loads((spec / MANIFEST).read_text(encoding="utf-8"))
        if change == "duplicate":
            data["files"].append(dict(data["files"][0]))
        else:
            data["files"].append({"path": "../outside", "sha256": "0" * 64})
        (spec / MANIFEST).write_text(json.dumps(data), encoding="utf-8")

    def set_status(spec: Path, status: str, json_too: bool) -> None:
        for name in ("backlog.csv", "backlog.json")[: 2 if json_too else 1]:
            path = spec / "planning" / name
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("Not started", status, 1), encoding="utf-8")

    cases = {
        "edited file": (edit_spec, "hash mismatch for MASTER_SPEC.md"),
        "deleted file": (
            lambda s: (s / "spec/05_CALCULATIONS.md").unlink(),
            "missing spec/05_CALCULATIONS.md",
        ),
        "added file": (
            lambda s: (s / "contracts/extra.json").write_text("{}"),
            "unlisted file contracts/extra.json",
        ),
        "empty manifest": (empty_manifest, "lists no files"),
        "corrupt manifest": (
            lambda s: (s / MANIFEST).write_text("not json"),
            "unreadable",
        ),
        "duplicate manifest entry": (
            lambda s: edit_manifest(s, "duplicate"),
            "duplicate entries",
        ),
        "unsafe manifest path": (
            lambda s: edit_manifest(s, "unsafe"),
            "unsafe path ../outside",
        ),
        "deleted working-state file": (
            lambda s: (s / "planning/backlog.json").unlink(),
            "missing planning/backlog.json",
        ),
        "unknown backlog status": (
            lambda s: set_status(s, "Done", json_too=True),
            "unknown status 'Done'",
        ),
        "CSV/JSON status drift": (
            lambda s: set_status(s, "Blocked", json_too=False),
            "disagree",
        ),
    }
    failures = 0
    for label, (inject, expected) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "spec"
            shutil.copytree(SPEC, copy)
            inject(copy)
            caught = any(expected in e for e in run_checks(copy))
        failures += not caught
        print(f"{'PASS' if caught else 'FAIL'} self-test: {label}")
    if run_checks(SPEC):
        failures += 1
        print("FAIL self-test: unmodified package does not pass")
    return 1 if failures else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    errors = run_checks(SPEC)
    for e in errors:
        print(f"FAIL {e}")
    if errors:
        return 1
    print(
        "PASS docs/spec matches reports/ARTIFACT_MANIFEST.json; backlog statuses valid"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
