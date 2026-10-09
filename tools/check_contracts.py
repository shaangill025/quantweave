#!/usr/bin/env python3
"""Financial and authority invariants of docs/spec/contracts that T008 relies on.

Artifact check only: it reads the designed contracts, not a running API.

1. No broker mutation route (default deny): a mutating operation on the account,
   connection or broker surface, or whose path or operationId contains a trading or
   money-movement verb (order, trade, close, liquidate, buy, sell, cancel, amend,
   place, rebalance, exercise, withdraw, transfer, ...), fails unless its (method,
   path, operationId) is in the reviewed ALLOWED_MUTATIONS list. The simulator order and user-reported execution
   report routes must also return `broker_submission`/`broker_confirmed` const false.
2. Every mutating operation requires Idempotency-Key and X-CSRF-Token headers.
3. Every money/quantity-named property or parameter is a decimal string, and no schema
   (components, files or inline under paths) uses JSON `number`. A `$ref` counts only
   if its resolved target carries one of the reviewed decimal patterns.
4. Every `*_at` timestamp in the contract examples is RFC 3339 with an explicit
   offset, in range, not `-00:00`, and parses as a real instant.

Known limits: money fields are found by name (a field called `pnl`, `nav` or
`premium` is not checked); timestamps are checked only in examples and only under keys
ending `_at`.

It also reports, without failing, whether the installed jsonschema enforces
`format: date-time` (T008 finding C-05). `--self-test` injects one defect per rule into
in-memory copies and requires each to be reported.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "docs" / "spec" / "contracts"
MUTATING = ("post", "put", "patch", "delete")

TRADING_VERB = re.compile(
    r"order|trade|execut|exercis|withdraw|transfer|close|liquidat|sell|buy|cancel"
    r"|amend|place|rebalanc|submit|fill|short|cover|assign|roll|replace|redeem|wire"
    r"|payout|position|broker|dispos|sweep|unwind|settl|holding",
    re.IGNORECASE,
)
BROKER_SURFACE = re.compile(
    r"^/(v\d+/)?(accounts|connections|brokers?|orders|positions|trades|holdings)"
)
SIM_ORDER_PATH = "/simulations/{simulation_id}/orders"
REPORT_PATH = "/accounts/{account_id}/execution-reports"
# Reviewed in T008 (docs/architecture/T008_contract_review.md): none reaches a broker.
ALLOWED_MUTATIONS = frozenset(
    {
        ("post", "/accounts", "createAccount"),  # application record of an account
        ("patch", "/accounts/{account_id}", "updateAccount"),
        (
            "post",
            "/accounts/{account_id}/reconcile",
            "reconcileAccount",
        ),  # internal journal job
        (
            "post",
            REPORT_PATH,
            "reportExternalExecution",
        ),  # user statement, broker_confirmed=false
        (
            "post",
            "/connections",
            "createConnection",
        ),  # read-only connector registration
        ("post", "/connections/{connection_id}/authorize", "authorizeConnection"),
        (
            "post",
            "/connections/{connection_id}/refresh",
            "refreshConnection",
        ),  # read sync
        (
            "delete",
            "/connections/{connection_id}",
            "revokeConnection",
        ),  # revoke our access
        ("post", SIM_ORDER_PATH, "submitVirtualOrder"),  # simulator ledger only
        ("post", "/feedback", "submitFeedback"),
        ("post", "/experiments/{experiment_id}/cancel", "cancelExperiment"),
        ("post", "/releases/{release_id}/rollback", "rollbackRelease"),
        ("post", "/jobs/{job_id}/cancel", "cancelJob"),
    }
)

MONEY_NAME = re.compile(
    r"(amount|quantity|price|fees?|cash|cost|strike|multiplier|_usd|capital|balance"
    r"|proceeds|notional)$|^value$"
)
DECIMAL_PATTERNS = frozenset(
    {r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$", r"^(0|[1-9][0-9]*)(\.[0-9]+)?$"}
)
DECIMAL_REFS = ("/$defs/Decimal", "/$defs/NonnegativeDecimal", "/$defs/Money")
OFFSET_TS = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(\.\d{1,6})?(Z|\+([01]\d|2[0-3]):[0-5]\d|-(?!00:00)([01]\d|2[0-3]):[0-5]\d)$"
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


def is_decimal(node: Json, schemas: dict[str, Json], depth: int = 0) -> bool:
    if not isinstance(node, dict) or depth > 8:
        return False
    ref = node.get("$ref")
    if isinstance(ref, str):
        if not ref.endswith(DECIMAL_REFS):
            return False
        name = ref.rsplit("/", 1)[1]
        target = schemas.get("common.schema.json", {}).get("$defs", {}).get(name)
        if name == "Money":
            props = target.get("properties", {}) if isinstance(target, dict) else {}
            return is_decimal(props.get("amount"), schemas, depth + 1)
        return is_decimal(target, schemas, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in node:
            alts = node[key]
            return all(
                is_decimal(x, schemas, depth + 1) or x == {"type": "null"} for x in alts
            ) and any(is_decimal(x, schemas, depth + 1) for x in alts)
    kind = node.get("type")
    if kind == "array":
        return is_decimal(node.get("items"), schemas, depth + 1)
    if kind == "string" or (isinstance(kind, list) and set(kind) <= {"string", "null"}):
        return node.get("pattern") in DECIMAL_PATTERNS
    return False


def is_money_name(key: str) -> bool:
    return bool(MONEY_NAME.search(key)) and not key.endswith(("_id", "_ids", "_basis"))


def check_decimals(
    name: str, node: Json, schemas: dict[str, Json], path: str = ""
) -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "number" or (isinstance(kind, list) and "number" in kind):
            errors.append(f"{name}{path}: JSON number type")
        props = node.get("properties")
        if isinstance(props, dict):
            for key, sub in props.items():
                if is_money_name(key) and not is_decimal(sub, schemas):
                    errors.append(
                        f"{name}{path}/{key}: money/quantity not a decimal string"
                    )
        # OpenAPI parameter objects: {name, in, schema}.
        pname = node.get("name")
        if (
            isinstance(pname, str)
            and "in" in node
            and is_money_name(pname)
            and not is_decimal(node.get("schema"), schemas)
        ):
            errors.append(f"{name}{path}: money/quantity parameter {pname} not decimal")
        for key, sub in node.items():
            errors += check_decimals(name, sub, schemas, f"{path}/{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            errors += check_decimals(name, sub, schemas, f"{path}[{i}]")
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
            sensitive = (
                BROKER_SURFACE.search(path)
                or TRADING_VERB.search(path)
                or TRADING_VERB.search(op_id)
            )
            if not sensitive:
                continue
            if (method, path, op_id) not in ALLOWED_MUTATIONS:
                errors.append(f"{where} ({op_id}): possible broker mutation route")
                continue
            ok = {"201", "200", "202"} & set(op["responses"])
            body: dict[str, Json] = (
                op["responses"][min(ok)].get("content", {}) if ok else {}
            )
            media: Json = next(iter(body.values()), {})
            ref = media.get("schema", {}).get("$ref", "")
            props = resolve(api, schemas, ref).get("properties", {}) if ref else {}
            if path == SIM_ORDER_PATH and props.get("broker_submission") != {
                "const": False
            }:
                errors.append(f"{where}: simulator order lacks broker_submission=false")
            if path == REPORT_PATH and props.get("broker_confirmed") != {
                "const": False
            }:
                errors.append(f"{where}: execution report lacks broker_confirmed=false")
    return errors


def valid_ts(value: str) -> bool:
    """Range-limited pattern, then a semantic parse (the pattern accepts Feb 30)."""
    if not OFFSET_TS.match(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_timestamps(name: str, node: Json, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        for key, sub in node.items():
            if key.endswith("_at") and isinstance(sub, str) and not valid_ts(sub):
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
    errors += check_decimals("openapi#/components", api["components"], schemas)
    errors += check_decimals("openapi#/paths", api["paths"], schemas)
    for name, schema in schemas.items():
        errors += check_decimals(name, schema, schemas)
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

    def close_position(a: Json, s: Json, e: Json) -> None:
        op = copy.deepcopy(a["paths"]["/accounts/{account_id}/reconcile"]["post"])
        a["paths"]["/accounts/{account_id}/positions/close"] = {
            "post": op | {"operationId": "closePosition"}
        }

    def liquidate(a: Json, s: Json, e: Json) -> None:
        op = copy.deepcopy(a["paths"]["/connections/{connection_id}/refresh"]["post"])
        a["paths"]["/connections/{connection_id}/liquidate"] = {
            "post": op | {"operationId": "refreshHoldings"}
        }

    def neutral_name(a: Json, s: Json, e: Json) -> None:
        op = copy.deepcopy(a["paths"]["/accounts/{account_id}/reconcile"]["post"])
        a["paths"]["/accounts/{account_id}/sync-holdings"] = {
            "post": op | {"operationId": "syncHoldings"}
        }

    def loose_ref(a: Json, s: Json, e: Json) -> None:
        s["common.schema.json"]["$defs"]["Decimal"]["pattern"] = "^.*$"

    def inline_number(a: Json, s: Json, e: Json) -> None:
        a["paths"]["/accounts"]["get"]["parameters"].append(
            {"name": "page_weight", "in": "query", "schema": {"type": "number"}}
        )

    def money_param(a: Json, s: Json, e: Json) -> None:
        a["paths"]["/accounts"]["get"]["parameters"].append(
            {"name": "min_cash", "in": "query", "schema": {"type": "integer"}}
        )

    def inline_body(a: Json, s: Json, e: Json) -> None:
        body = a["paths"]["/imports"]["post"]["requestBody"]["content"]
        body["multipart/form-data"]["schema"]["properties"]["fee"] = {"type": "string"}

    def bad_offset(a: Json, s: Json, e: Json) -> None:
        e["review.valid.json"]["completed_at"] = "2026-10-08T14:45:00-00:00"

    def bad_date(a: Json, s: Json, e: Json) -> None:
        e["review.valid.json"]["completed_at"] = "2026-02-30T14:45:00Z"

    cases = {
        "broker order route": (broker_route, "possible broker mutation route"),
        "disguised close route": (close_position, "possible broker mutation route"),
        "connection liquidate": (liquidate, "possible broker mutation route"),
        "unlisted account mutation": (neutral_name, "possible broker mutation route"),
        "decimal $ref target loosened": (loose_ref, "not a decimal string"),
        "inline number parameter": (inline_number, "JSON number type"),
        "money parameter integer": (money_param, "parameter min_cash not decimal"),
        "inline body money string": (inline_body, "fee: money/quantity not a decimal"),
        "unknown offset -00:00": (bad_offset, "timestamp without offset"),
        "impossible date": (bad_date, "timestamp without offset"),
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
