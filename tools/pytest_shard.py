#!/usr/bin/env python3
"""Split the pytest suite into disjoint shards for CI, so the DB-backed suite runs
in parallel jobs instead of one job near its time limit.

`python3 tools/pytest_shard.py I N` prints shard I's test files (0-based, one per
line). The files are every `test_*.py` and `*_test.py` under pyproject's pytest
`testpaths`, sorted and dealt round robin, so each file lands in exactly one
shard and the N shards together run the whole suite. An empty shard is an error
(pytest given no paths would run everything). `--self-test` checks the partition.
Standard library only.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "node_modules", "__pycache__"}


def collect(root: Path = ROOT) -> list[str]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    found: set[str] = set()
    for p in paths:
        for pattern in ("test_*.py", "*_test.py"):
            for f in (root / p).rglob(pattern):
                if not SKIP.intersection(f.relative_to(root).parts):
                    found.add(f.relative_to(root).as_posix())
    return sorted(found)


def shard(files: list[str], index: int, count: int) -> list[str]:
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"shard {index} of {count}")
    picked = files[index::count]
    if not picked:
        raise ValueError(f"shard {index} of {count} is empty")
    return picked


def self_test() -> None:
    files = collect()
    assert files, "no test files found"
    for count in (1, 2, 3, 4):
        parts = [shard(files, i, count) for i in range(count)]
        flat = [f for part in parts for f in part]
        assert sorted(flat) == files, count  # every file, exactly once
    for bad in ((0, 0), (4, 4), (-1, 2)):
        try:
            shard(files, *bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        shard(["a"], 1, 2)
    except ValueError:
        pass
    else:
        raise AssertionError("an empty shard was accepted")
    print(f"PASS: {len(files)} test files partition into 1 to 4 shards")


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        self_test()
        return 0
    if len(argv) != 2 or not all(a.isdigit() for a in argv):
        print("usage: pytest_shard.py INDEX COUNT | --self-test", file=sys.stderr)
        return 2
    try:
        print("\n".join(shard(collect(), int(argv[0]), int(argv[1]))))
    except ValueError as exc:
        print(f"pytest_shard: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
