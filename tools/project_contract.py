#!/usr/bin/env python3
"""Project contract 0.9.0 (T008 A-01): the docs/spec/contracts 0.1.0 schemas with the
amendments of contracts/amendments.json applied (all owner decisions: see `versions`
there). docs/spec is never edited; this derives the amended schemas in memory.

- `nullable`: the property (or `parent/property`; a `$ref` parent is inlined from
  common.schema.json first) also accepts null.
- `add`: required properties appended to a closed schema.
- `active_nullable`: the `state == active` if/then rules also accept null there.
- `enum_add`: values appended to a property's enum; a value already there is refused.
- `all_of`: rules appended to the schema's `allOf` (created when absent).
- `new_schemas`: whole schemas the spec lacks, written like the schema files (refs to
  `common.schema.json`, `#/$defs/Coverage` added); a name the contract already has
  (schema file, openapi component or amended schema) is refused.
- `operations`: openapi path items the spec lacks; `openapi()` adds them, and the
  new schemas as components, to a copy of openapi.yaml. A path already in the
  contract, a missing operationId or one already used is refused. Each call starts
  again from the unmodified base, so applying twice gives the same document.
- `security_schemes`: only the `sessionCookie` apiKey-in-cookie scheme's `name`, and
  only to exactly `__Host-pi_session` (owner decision 14, 2026-10-21);
  `openapi()` applies it. Anything else, or a name already in place, is refused.
- `mutations`: the owner-approved allowlist of amended mutating operations, with
  their decision-25 `exemptions`, read by tools/check_contracts.py rules 1, 2 and 6
  only (`openapi()` merges operations the same way whatever their method; the
  checker refuses an unlisted mutation).
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


def _new_schema(name: str) -> dict[str, Any]:
    taken = (SPEC / f"{name}.schema.json").exists() or name in AMENDMENTS["schemas"]
    if taken or name in _openapi()["components"]["schemas"] or name == "Coverage":
        raise ValueError(f"{name}: schema name already in the contract")
    s: dict[str, Any] = copy.deepcopy(AMENDMENTS["new_schemas"][name])
    s.setdefault("$defs", {})["Coverage"] = copy.deepcopy(AMENDMENTS["Coverage"])
    s["title"] = f"{s['title']} (project contract {AMENDMENTS['version']})"
    return s


def schema(name: str) -> dict[str, Any]:
    if name in AMENDMENTS.get("new_schemas", {}):
        return _new_schema(name)
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


def _spec_refs(node: Any) -> Any:
    """A project schema's refs in openapi.yaml form (the inverse of `_local`)."""
    if isinstance(node, dict):
        out = {k: _spec_refs(v) for k, v in node.items() if k != "$defs"}
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("common.schema.json"):
            out["$ref"] = f"./schemas/{ref}"
        elif ref == "#/$defs/Coverage":
            out["$ref"] = "#/components/schemas/Coverage"
        elif isinstance(ref, str) and ref.startswith("#/$defs/"):
            out["$ref"] = f"#/components/schemas/{ref.rsplit('/', 1)[1]}"
        return out
    return [_spec_refs(v) for v in node] if isinstance(node, list) else node


METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options"})
SESSION_COOKIE_NAME = "__Host-pi_session"  # owner decision 14, 2026-10-21


def openapi(base: dict[str, Any] | None = None) -> dict[str, Any]:
    """openapi.yaml (or `base`) with `operations` and `new_schemas` applied, a copy."""
    api = copy.deepcopy(_openapi() if base is None else base)
    used = {op.get("operationId") for item in api["paths"].values()
            for m, op in item.items() if m in METHODS}  # fmt: skip
    for path, item in AMENDMENTS.get("operations", {}).items():
        if path in api["paths"]:
            raise ValueError(f"{path}: path already in the contract")
        for method, op in item.items():
            op_id = op.get("operationId") if method in METHODS else None
            if not isinstance(op_id, str) or not op_id or op_id in used:
                raise ValueError(f"{method} {path}: operationId missing or used")
            used.add(op_id)
        api["paths"][path] = copy.deepcopy(item)
    schemes = api["components"].get("securitySchemes", {})
    for key, change in AMENDMENTS.get("security_schemes", {}).items():
        scheme, name = schemes.get(key), change.get("name")
        if key != "sessionCookie" or not isinstance(scheme, dict):
            raise ValueError(f"{key}: no such amendable security scheme")
        if set(change) != {"name"} or (scheme.get("type"), scheme.get("in")) != (
            "apiKey", "cookie",
        ):  # fmt: skip
            raise ValueError(f"{key}: only the name of an apiKey cookie may change")
        if name != SESSION_COOKIE_NAME:
            raise ValueError(
                f"{key}: {name!r} is not {SESSION_COOKIE_NAME} (decision 14)"
            )
        if name == scheme.get("name"):
            raise ValueError(f"{key}: name {name!r} already in the contract")
        scheme["name"] = name
    components = api["components"]["schemas"]
    for name in AMENDMENTS.get("new_schemas", {}):
        s = schema(name)  # refuses a name the contract already has
        for key, sub in {**s["$defs"], name: s}.items():
            if components.setdefault(key, _spec_refs(sub)) != _spec_refs(sub):
                raise ValueError(f"{key}: component already in the contract")
    return api


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
    for name in AMENDMENTS.get("new_schemas", {}):
        print(name, "new schema")
        schema(name)  # raises on a name the contract already has
    for path in AMENDMENTS.get("operations", {}):
        print(path, "new operations", sorted(AMENDMENTS["operations"][path]))
    for key, change in AMENDMENTS.get("security_schemes", {}).items():
        print(key, "security scheme", json.dumps(change, sort_keys=True))
    openapi()  # raises on a duplicate path or operationId, or a refused scheme
    for name in AMENDMENTS["requests"]:
        print(name, json.dumps(AMENDMENTS["requests"][name], sort_keys=True))
        request(name)  # raises on a missing request or an existing property
    print(f"project contract {AMENDMENTS['version']}: OK")
