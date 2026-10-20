#!/usr/bin/env python3
"""Project contract 0.6.0 (T008 A-01): the docs/spec/contracts 0.1.0 schemas with the
amendments of contracts/amendments.json applied (all owner decisions: see `versions`
there). docs/spec is never edited; this derives the amended schemas in memory.

- `nullable`: the property (or `parent/property`; a `$ref` parent is inlined from
  common.schema.json first) also accepts null.
- `add`: required properties appended to a closed schema.
- `active_nullable`: the `state == active` if/then rules also accept null there.
- `enum_add`: values appended to a property's enum; a value already there is refused.
- `all_of`: rules appended to the schema's `allOf` (created when absent).
- `page(name)`: the C-02 page `{items, next_cursor, coverage}` of an item schema.
- `request(name)`: an openapi.yaml request component; `requests.<name>.optional` adds
  properties that are not required. Its `./schemas/` refs become the bare file names
  the schema files use. Reading openapi.yaml needs PyYAML (dev group, as in
  tools/check_contracts.py); everything else is standard library.
The cross-field rule (every amended null is named in coverage.reasons) is not
expressible in JSON Schema; consumers check it (`rule` in amendments.json).
Run directly, it prints each amended schema's changes.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "spec" / "contracts" / "schemas"
AMENDMENTS = json.loads((ROOT / "contracts" / "amendments.json").read_text())


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((SPEC / f"{name}.schema.json").read_text())
    return data


def _null(prop: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [prop, {"type": "null"}]}


def schema(name: str) -> dict[str, Any]:
    s, a = _load(name), AMENDMENTS["schemas"].get(name, {})
    common = _load("common")["$defs"]
    s.setdefault("$defs", {})["Coverage"] = AMENDMENTS["Coverage"]
    for path in a.get("nullable", []):
        *parents, leaf = path.split("/")
        props = s["properties"]
        for p in parents:
            if "$ref" in props[p]:  # inline so the amendment stays local
                props[p] = copy.deepcopy(common[props[p]["$ref"].rsplit("/", 1)[1]])
            props = props[p]["properties"]
        props[leaf] = _null(props[leaf])
    for key, values in a.get("enum_add", {}).items():
        enum = s["properties"][key]["enum"]
        if set(values) & set(enum) or len(set(values)) != len(values):
            raise ValueError(f"{name}.{key}: enum value already present")
        enum.extend(values)
    if "all_of" in a:
        s.setdefault("allOf", []).extend(copy.deepcopy(a["all_of"]))
    for key, prop in a.get("add", {}).items():
        s["properties"][key] = prop
        s["required"].append(key)
    for rule in s.get("allOf", []):
        then = rule.get("then", {}).get("properties", {})
        for key in a.get("active_nullable", []):
            if key in then:
                then[key] = _null(then[key])
    s["title"] = f"{s['title']} (project contract {AMENDMENTS['version']})"
    return s


@functools.cache
def _openapi() -> dict[str, Any]:
    import yaml  # type: ignore[import-untyped]

    data: dict[str, Any] = yaml.safe_load((SPEC.parent / "openapi.yaml").read_text())
    return data


def _local(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: v.removeprefix("./schemas/") if k == "$ref" else _local(v)
            for k, v in node.items()
        }
    return [_local(v) for v in node] if isinstance(node, list) else node


def request(name: str) -> dict[str, Any]:
    s: dict[str, Any] = _local(_openapi()["components"]["schemas"][name])
    for key, prop in AMENDMENTS["requests"].get(name, {}).get("optional", {}).items():
        if key in s["properties"]:
            raise ValueError(f"{name}.{key} is already in the request")
        s["properties"][key] = prop
    s["title"] = f"{name} (project contract {AMENDMENTS['version']})"
    return s


def page(name: str) -> dict[str, Any]:
    return {
        "type": "object",
        "$defs": {"Coverage": AMENDMENTS["Coverage"]},
        "properties": {
            "items": {"type": "array", "items": schema(name)},
            "next_cursor": {"type": ["string", "null"]},
            **AMENDMENTS["page"]["add"],
        },
        "required": ["items", "next_cursor", "coverage"],
        "additionalProperties": False,
    }


if __name__ == "__main__":
    for name in AMENDMENTS["schemas"]:
        print(name, json.dumps(AMENDMENTS["schemas"][name], sort_keys=True))
        schema(name)  # raises on an amendment that names a missing property
    for name in AMENDMENTS["requests"]:
        print(name, json.dumps(AMENDMENTS["requests"][name], sort_keys=True))
        request(name)  # raises on a missing request or an existing property
    print(f"project contract {AMENDMENTS['version']}: OK")
