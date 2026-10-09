#!/usr/bin/env python3
"""Financial and authority invariants of docs/spec/contracts that T008 relies on.

Artifact check only: it reads the designed contracts, not a running API.

1. No broker order mutation route: a mutating operation whose path or operationId
   names orders, trades, execution, exercise, withdrawals or transfers must be on the
   simulator order path or the user-reported execution-report path, and those
   responses must carry `broker_submission`/`broker_confirmed` const false.
2. Every mutating operation requires Idempotency-Key and X-CSRF-Token headers.
3. Every money/quantity-named property is a decimal string (common Decimal,
   NonnegativeDecimal or Money, optionally nullable or in an array), and no schema
   anywhere uses JSON `number`.
4. Every `*_at` timestamp in the contract examples is RFC 3339 with an explicit
   offset (and not the unknown-offset `-00:00`).

It also reports, without failing, whether the installed jsonschema enforces
`format: date-time` (T008 finding C-05). `--self-test` injects one defect per rule into
in-memory copies and requires each to be reported.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "docs" / "spec" / "contracts"
MUTATING = ("post", "put", "patch", "delete")

BROKERISH = re.compile(r"order|trade|execut|exercise|withdraw|transfer", re.IGNORECASE)
ORDER_VERB = re.compile(
    r"(submit|place|amend|replace|cancel|modify)\w*order", re.IGNORECASE
)
SIM_ORDER_PATH = "/simulations/{simulation_id}/orders"
REPORT_PATH = "/accounts/{account_id}/execution-reports"

MONEY_NAME = re.compile(
    r"(amount|quantity|price|fees?|cash|cost|strike|multiplier|_usd|capital|balance"
    r"|proceeds|notional)$|^value$"
)
DECIMAL_PATTERNS = frozenset(
    {r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", r"^(0|[1-9][0-9]*)(\.[0-9]+)?$"}
)
DECIMAL_REFS = ("/$defs/Decimal", "/$defs/NonnegativeDecimal", "/$defs/Money")
OFFSET_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

Json = Any


def load() -> tuple[Json, dict[str, Json], dict[str, Json]]:
    api = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((CONTRACTS / "schemas").glob("*.json"))
    }
    examples = {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((CONTRACTS / "examples").glob("*.json"))
        if p.name != "manifest.json"
    }
    return api, schemas, examples


def is_decimal(node: Json) -> bool:
    if not isinstance(node, dict):
        return False
    ref = node.get("$ref")
    if isinstance(ref, str):
        return ref.endswith(DECIMAL_REFS)
    for key in ("anyOf", "oneOf"):
        if key in node:
            return all(
                is_decimal(x) or x == {"type": "null"} for x in node[key]
            ) and any(is_decimal(x) for x in node[key])
    kind = node.get("type")
    if kind == "array":
        return is_decimal(node.get("items"))
    if kind == "string" or (isinstance(kind, list) and set(kind) <= {"string", "null"}):
        return node.get("pattern") in DECIMAL_PATTERNS
    return False


def check_decimals(name: str, node: Json, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "number" or (isinstance(kind, list) and "number" in kind):
            errors.append(f"{name}{path}: JSON number type")
        props = node.get("properties")
        if isinstance(props, dict):
            for key, sub in props.items():
                if (
                    MONEY_NAME.search(key)
                    and not key.endswith(("_id", "_ids", "_basis"))
                    and not is_decimal(sub)
                ):
                    errors.append(
                        f"{name}{path}/{key}: money/quantity not a decimal string"
                    )
        for key, sub in node.items():
            errors += check_decimals(name, sub, f"{path}/{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            errors += check_decimals(name, sub, f"{path}[{i}]")
    return errors


def resolve(api: Json, schemas: dict[str, Json], ref: str) -> Json:
    if ref.startswith("#/components/schemas/"):
        target = api["components"]["schemas"][ref.rsplit("/", 1)[1]]
        inner = target.get("$ref") if isinstance(target, dict) else None
        return resolve(api, schemas, inner) if isinstance(inner, str) else target
    return schemas.get(ref.split("/")[-1].split("#")[0], {})


def check_routes(api: Json, schemas: dict[str, Json]) -> list[str]:
    errors: list[str] = []
    for path, item in api["paths"].items():
        for method, op in item.items():
            if method not in MUTATING:
                continue
            where = f"{method.upper()} {path}"
            headers = {
                p.get("name")
                for p in op.get("parameters", [])
                if p.get("in") == "header" and p.get("required")
            }
            for needed in ("Idempotency-Key", "X-CSRF-Token"):
                if needed not in headers:
                    errors.append(f"{where}: missing required {needed}")
            op_id = str(op.get("operationId", ""))
            if not (BROKERISH.search(path) or ORDER_VERB.search(op_id)):
                continue
            ok = {"201", "200", "202"} & set(op["responses"])
            body: dict[str, Json] = (
                op["responses"][min(ok)].get("content", {}) if ok else {}
            )
            media: Json = next(iter(body.values()), {})
            ref = media.get("schema", {}).get("$ref", "")
            props = resolve(api, schemas, ref).get("properties", {}) if ref else {}
            if path == SIM_ORDER_PATH and method == "post":
                if props.get("broker_submission") != {"const": False}:
                    errors.append(
                        f"{where}: simulator order lacks broker_submission=false"
                    )
            elif path == REPORT_PATH and method == "post":
                if props.get("broker_confirmed") != {"const": False}:
                    errors.append(
                        f"{where}: execution report lacks broker_confirmed=false"
                    )
            else:
                errors.append(f"{where} ({op_id}): possible broker mutation route")
    return errors


def check_timestamps(name: str, node: Json, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        for key, sub in node.items():
            if (
                key.endswith("_at")
                and isinstance(sub, str)
                and (not OFFSET_TS.match(sub) or sub.endswith("-00:00"))
            ):
                errors.append(f"{name}{path}/{key}: timestamp without offset: {sub!r}")
            errors += check_timestamps(name, sub, f"{path}/{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            errors += check_timestamps(name, sub, f"{path}[{i}]")
    return errors


def run_checks(
    api: Json, schemas: dict[str, Json], examples: dict[str, Json]
) -> list[str]:
    errors = check_routes(api, schemas)
    errors += check_decimals("openapi#/components", api["components"])
    for name, schema in schemas.items():
        errors += check_decimals(name, schema)
    for name, doc in examples.items():
        errors += check_timestamps(name, doc)
    return errors


def format_checker_enforces_datetime() -> bool:
    from jsonschema import FormatChecker  # type: ignore[import-untyped]

    return not FormatChecker().conforms("2026-10-08T10:00:00", "date-time")


def self_test(api: Json, schemas: dict[str, Json], examples: dict[str, Json]) -> int:
    def broker_route(a: Json, s: Json, e: Json) -> None:
        a["paths"]["/accounts/{account_id}/orders"] = {
            "post": copy.deepcopy(a["paths"][SIM_ORDER_PATH]["post"])
            | {"operationId": "submitBrokerOrder"}
        }

    def cancel_verb(a: Json, s: Json, e: Json) -> None:
        a["paths"]["/accounts/{account_id}/reconcile"]["post"]["operationId"] = (
            "cancelOrder"
        )

    def sim_flag(a: Json, s: Json, e: Json) -> None:
        del s["simulated_order.schema.json"]["properties"]["broker_submission"]

    def no_idem(a: Json, s: Json, e: Json) -> None:
        op = a["paths"]["/watchlists"]["post"]
        op["parameters"] = [
            p for p in op["parameters"] if p["name"] != "Idempotency-Key"
        ]

    def float_money(a: Json, s: Json, e: Json) -> None:
        s["common.schema.json"]["$defs"]["Money"]["properties"]["amount"] = {
            "type": "number"
        }

    def int_quantity(a: Json, s: Json, e: Json) -> None:
        s["simulated_fill.schema.json"]["properties"]["quantity"] = {"type": "integer"}

    def naive_ts(a: Json, s: Json, e: Json) -> None:
        e["review.valid.json"]["completed_at"] = "2026-10-08T14:45:00"

    cases = {
        "broker order route": (broker_route, "possible broker mutation route"),
        "order-cancel operationId": (cancel_verb, "possible broker mutation route"),
        "simulator flag removed": (sim_flag, "lacks broker_submission=false"),
        "missing idempotency": (no_idem, "missing required Idempotency-Key"),
        "number money": (float_money, "JSON number type"),
        "integer quantity": (int_quantity, "quantity: money/quantity not a decimal"),
        "offset-less timestamp": (naive_ts, "timestamp without offset"),
    }
    failures = 0
    baseline = run_checks(api, schemas, examples)
    if baseline:
        print("self-test: unmodified contracts fail:", *baseline, sep="\n  ")
        failures += 1
    for label, (inject, expected) in cases.items():
        a, s, e = copy.deepcopy(api), copy.deepcopy(schemas), copy.deepcopy(examples)
        inject(a, s, e)
        found = [m for m in run_checks(a, s, e) if expected in m]
        print(f"self-test {label}: {'detected' if found else 'MISSED'}")
        failures += not found
    print(f"self-test: {len(cases) - failures}/{len(cases)} defects detected")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    api, schemas, examples = load()
    if "--self-test" in argv:
        return self_test(api, schemas, examples)
    errors = run_checks(api, schemas, examples)
    for err in errors:
        print("FAIL", err)
    enforced = format_checker_enforces_datetime()
    print(
        "INFO jsonschema format date-time enforced:",
        enforced,
        ""
        if enforced
        else "(C-05: offset-less timestamps pass schema validation here)",
    )
    ops = sum(m in MUTATING for item in api["paths"].values() for m in item)
    print(
        f"{'FAIL' if errors else 'PASS'}: {len(api['paths'])} paths, {ops} mutating "
        f"operations, {len(schemas)} schemas, {len(examples)} examples"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
