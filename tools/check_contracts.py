#!/usr/bin/env python3
"""Financial and authority invariants of docs/spec/contracts that T008 relies on.

Artifact check only: it reads the designed contracts, not a running API.

1. No broker mutation route (default deny): a mutating operation on the account,
   connection or broker surface, or whose path or operationId contains a trading or
   money-movement verb (order, trade, close, liquidate, buy, sell, cancel, amend,
   place, rebalance, exercise, withdraw, transfer, ...), fails unless its (method,
   path, operationId) is in the reviewed ALLOWED_MUTATIONS list. The simulator order and user-reported execution
   report routes must also return `broker_submission`/`broker_confirmed` const false.
2. Every mutating operation requires Idempotency-Key and X-CSRF-Token headers,
   except an approved amended POST with the `unauthenticated_login` exemption.
3. Every money/quantity-named property or parameter is a decimal string, and no schema
   (components, files or inline under paths) uses JSON `number`. A `$ref` counts only
   if its resolved target carries one of the reviewed decimal patterns.
4. Every `*_at` timestamp in the contract examples is RFC 3339 with an explicit
   offset, in range, not `-00:00`, and parses as a real instant.
5. Every project contract schema (contracts/amendments.json applied by
   tools/project_contract.py) is a valid 2020-12 schema that keeps rule 3, and its
   `all_of` rules name only properties the schema has. A new schema (`new_schemas`)
   is also closed, and each `*_at` property is a `Timestamp` (nullable at most).
6. Every new operation (`operations`, merged into openapi.yaml by
   `project_contract.openapi`, which refuses a path, operationId or schema name the
   contract already has) passes rules 1 to 3 and the contract's conventions: session
   security (exactly `sessionCookie`); only the spec's response keys (200, 201, 202,
   `default`); `default` exactly an `application/problem+json` Problem; every 2xx
   exactly `application/json`, naming a known component that does not reach Problem
   through `$ref` or allOf/oneOf/anyOf/if/then/else/not; `Cache-Control: no-store`
   on each (the API sends no-store on every response); no `servers`; and no tenant
   parameter (the tenant comes from the session; `$ref` and path-level parameters
   resolved). Amendments add GET operations; a mutating one (POST, PUT, PATCH,
   DELETE) is refused unless `mutations` lists its method, path and operationId
   with an owner decision id (`YYYY-MM-DD#N`) whose section in
   docs/decisions/YYYY-MM-DD-owner-decisions.md has exactly one `Selected label:`
   line and a whole line ``Approves: `METHOD /path` (`operationId`)`` in the
   trailing directive block (`approvals`: after the label and the option text,
   only directives until `Implemented by:`, all outside quoted text); an entry
   that cites no such section, or matches no amended operation, fails. The
   decisions file has no integrity check beyond owner review of its diff. An
   approved entry's `exemptions` (owner decision 25) are the only relaxations,
   each pinned to one operation and citing a decision with one `Selected label:`
   and a line ``Exempts: `name` (`operationId`)`` in that block:
   `unauthenticated_login` (only `POST /session`, createSession) needs `security:
   []` on the operation and waives rule 2's headers; `no_content` (only `DELETE
   /session`, revokeSession) allows its one 2xx to be a 204 without content (still
   no-store). Neither applies to a read or any other operation. A listed mutation still passes rules 1 and 2 and every rule
   above, and one on the broker surface or with a trading verb is never amendable.
   Any other method is refused. `security_schemes` may change only the
   `sessionCookie` apiKey-in-cookie `name`, to exactly `__Host-pi_session`
   (owner decision 14).
   `check_shapes` runs first: every structural amendment key must be one a rule
   reviews and every value the type the rules read, else the amendment is reported
   and not applied. Any exception a rule still raises is reported as a crash.
7. No name an amendment adds (property and patternProperties keys at any depth,
   parameter and response header names, from every amendment source; the one
   security scheme name allowed is fixed by rule 6) looks like a
   secret (token, secret, password, passwd, pwd, api key, private key, credential;
   any case, `_` or `-`) unless `sensitive_fields` in amendments.json lists that
   schema and JSON pointer with the decision that allows it; an unused or undecided
   entry fails.

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
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import project_contract as pc
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

PATH_FIELDS = frozenset({"summary", "description", "servers", "parameters"})
SESSION: list[dict[str, list[str]]] = [{"sessionCookie": []}]
# The spec's six words plus passwd and pwd (owner decision 16, 2026-10-21); bearer
# and jwt are not added (they overlap `token`).
SECRET_NAME = re.compile(
    r"token|secret|password|passwd|pwd|apikey|privatekey|credential"
)
# Every amendment key a rule reviews, with its type (`check_shapes`).
TOP_KEYS: dict[str, type] = {
    "version": str, "base": str, "versions": dict, "about": str, "changes": str,
    "rule": str, "schemas": dict, "requests": dict, "operations": dict,
    "new_schemas": dict, "sensitive_fields": list, "page": dict, "Coverage": dict,
    "security_schemes": dict, "mutations": list,
}  # fmt: skip
MUTATION_KEYS = ("method", "path", "operationId", "decision")
DECISION_ID = re.compile(r"(\d{4}-\d{2}-\d{2})#([1-9]\d*)")
APPROVES = re.compile(r"Approves: `([A-Z]+) (/[^`\s]*)` \(`([A-Za-z0-9]+)`\)")
EXEMPTS = re.compile(r"Exempts: `([a-z_]+)` \(`([A-Za-z0-9]+)`\)")
QUOTED = ("Question:", "Option text as presented:")
CLOSES = ('."', '?"', '!"')  # a quoted block ends at a sentence-final quote
DIRECTIVES = ("Approves:", "Exempts:")
# Owner decision 25: each exemption is pinned to the one operation it was made for.
EXEMPTIONS = {
    "unauthenticated_login": ("post", "/session", "createSession"),
    "no_content": ("delete", "/session", "revokeSession"),
}
# project_contract raises these on a malformed amendment: reported as a refusal.
REFUSED = (KeyError, ValueError, TypeError, AttributeError, IndexError)
OP_KEYS = frozenset({"operationId", "summary", "description", "security",
                     "parameters", "requestBody", "responses"})  # fmt: skip
PARAM_KEYS = frozenset({"name", "in", "required", "schema", "description"})
HEADER_KEYS = frozenset({"schema", "description", "required"})
RESPONSE_CODES = frozenset({"200", "201", "202", "default"})  # the spec's codes
REQUIRED_KEYS = frozenset({"version", "schemas", "requests", "page", "Coverage"})
SCHEMA_KEYS: dict[str, tuple[type, type | None]] = {
    "nullable": (list, str), "active_nullable": (list, str), "add": (dict, dict),
    "enum_add": (dict, list), "all_of": (list, dict),
}  # fmt: skip

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
    kinds = (
        kind if isinstance(kind, list) and all(isinstance(k, str) for k in kind) else []
    )
    if kind == "string" or (kinds and set(kinds) <= {"string", "null"}):
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


def check_routes(
    api: Json, schemas: dict[str, Json], login: frozenset[tuple[str, str]] = frozenset()
) -> list[str]:
    """Rules 1 and 2; `login` holds the (method, path) of approved mutations with
    the unauthenticated_login exemption, which need no CSRF or idempotency header."""
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
                if needed not in headers and (method, path) not in login:
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


def _named(rule: Json) -> set[str]:
    """Property names an if/then/else rule constrains."""
    out: set[str] = set()
    for key in ("if", "then", "else"):
        sub = rule.get(key)
        props = sub.get("properties") if isinstance(sub, dict) else None
        out |= set(props) if isinstance(props, dict) else set()
    return out


def check_times(where: str, node: Json, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(node, dict):
        stamp = {"$ref": "common.schema.json#/$defs/Timestamp"}
        props = node.get("properties")
        for key, sub in (props if isinstance(props, dict) else {}).items():
            if key.endswith("_at") and sub not in (
                stamp,
                {"anyOf": [stamp, {"type": "null"}]},
            ):
                errors.append(f"{where}{path}/{key}: not a Timestamp")
        for key, sub in node.items():
            errors += check_times(where, sub, f"{path}/{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            errors += check_times(where, sub, f"{path}[{i}]")
    return errors


def amended_api(api: Json, amendments: Json) -> tuple[Json, list[str]]:
    saved, pc.AMENDMENTS = pc.AMENDMENTS, amendments
    try:
        return pc.openapi(api), []
    except REFUSED as exc:
        return api, [f"project contract operations: amendment refused: {exc!r}"]
    finally:
        pc.AMENDMENTS = saved


def check_shapes(m: Json) -> list[str]:
    """The amendment document's shape, before any rule reads it: a wrong type is
    reported here, never raised later; a key no rule reviews is refused."""
    w, errors = "project contract", []

    def want(where: str, node: Json, kind: type, of: type | None = None) -> bool:
        if not isinstance(node, kind):
            errors.append(
                f"{w} {where} is not {'a list' if kind is list else 'an object'}"
            )
            return False
        items: Json = node.values() if isinstance(node, dict) else node
        if of is not None and not all(isinstance(x, of) for x in items):
            errors.append(f"{w} {where}: an entry is not {of.__name__}")
        return True

    def keys(node: Json, allowed: Any) -> None:
        errors.extend(
            f"{w}: unreviewed amendment key {k!r}" for k in node if k not in allowed
        )

    def each(key: str) -> Any:
        return m[key].items() if isinstance(m.get(key), dict) else ()

    if not want("amendments", m, dict):
        return errors
    keys(m, TOP_KEYS)
    for key, kind in TOP_KEYS.items():
        if key in m or key in REQUIRED_KEYS:
            listed = key in ("sensitive_fields", "mutations")
            want(key, m.get(key), kind, dict if listed else None)
    for key, change in each("security_schemes"):
        if key != "sessionCookie":
            errors.append(f"{w}: security scheme {key!r} refused (sessionCookie only)")
        elif want(f"security_schemes.{key}", change, dict):
            keys(change, {"name"})
            name = change.get("name")
            if name != pc.SESSION_COOKIE_NAME:
                errors.append(f"{w} security_schemes.{key}.name is not "
                              f"{pc.SESSION_COOKIE_NAME} (decision 14)")  # fmt: skip
    mutations = m.get("mutations") if isinstance(m.get("mutations"), list) else []
    no_content: set[tuple[Json, Json]] = set()  # (path, operationId) of DELETEs
    for i, entry in enumerate(mutations):
        if not isinstance(entry, dict):
            continue  # reported above
        keys(entry, (*MUTATION_KEYS, "exemptions"))
        fields = {k: v for k, v in entry.items() if k != "exemptions"}
        want(f"mutations[{i}]", fields, dict, str)
        listed = entry.get("exemptions", [])
        if want(f"mutations[{i}].exemptions", listed, list, dict):
            names = [x.get("name") for x in listed if isinstance(x, dict)]
            for x in [x for x in listed if isinstance(x, dict)]:
                keys(x, ("name", "decision"))
                name, where_ = x.get("name"), f"{w} mutations[{i}] exemption"
                if name not in EXEMPTIONS:
                    errors.append(f"{where_} {name!r} is not a reviewed exemption")
                elif EXEMPTIONS[name] != tuple(entry.get(k) for k in MUTATION_KEYS[:3]):
                    method, path, op_id = EXEMPTIONS[name]
                    errors.append(f"{where_} {name} applies only to "
                                  f"{method.upper()} {path} ({op_id})")  # fmt: skip
                elif name == "no_content":
                    no_content.add((entry.get("path"), entry.get("operationId")))
                if not DECISION_ID.fullmatch(str(x.get("decision"))):
                    errors.append(f"{where_} {name}: decision is not an owner "
                                  "decision id (YYYY-MM-DD#N)")  # fmt: skip
            if len(set(map(str, names))) != len(names):
                errors.append(f"{w} mutations[{i}]: duplicate exemption")
        if missing := [k for k in MUTATION_KEYS if k not in entry]:
            errors.append(f"{w} mutations[{i}] lacks {missing}")
        elif entry["method"] not in MUTATING:
            errors.append(f"{w} mutations[{i}]: {entry['method']!r} is not a "
                          "mutating method")  # fmt: skip
        elif not DECISION_ID.fullmatch(str(entry["decision"])):
            errors.append(f"{w} mutations[{i}]: decision is not an owner decision "
                          "id (YYYY-MM-DD#N)")  # fmt: skip
    for name, a in each("schemas"):
        if want(f"schemas.{name}", a, dict):
            keys(a, SCHEMA_KEYS)
            for key in set(a) & set(SCHEMA_KEYS):
                want(f"schemas.{name}.{key}", a[key], *SCHEMA_KEYS[key])
    for name, r in each("requests"):
        if want(f"requests.{name}", r, dict):
            keys(r, {"optional"})
            want(f"requests.{name}.optional", r.get("optional", {}), dict, dict)
    for name, s in each("new_schemas"):
        want(f"new_schemas.{name}", s, dict)
    if isinstance(m.get("page"), dict):
        keys(m["page"], {"add"})
        want("page.add", m["page"].get("add", {}), dict, dict)
    for path, item in each("operations"):
        if not want(f"operations {path}", item, dict):
            continue
        keys(item, pc.METHODS | PATH_FIELDS)
        want(f"{path} parameters", item.get("parameters", []), list, dict)
        ops = [(f"{k.upper()} {path}", op) for k, op in item.items() if k in pc.METHODS]
        for where, op in [(path, item), *ops]:
            if (op is item or want(where, op, dict)) and "servers" in op:
                errors.append(f"{w} {where}: servers refused")
        for where, op in ops:
            if not isinstance(op, dict):
                continue
            keys(op, OP_KEYS)
            params = op.get("parameters", [])
            if want(f"{where} parameters", params, list) and not all(
                isinstance(x, dict) for x in params
            ):
                errors.append(f"{w} {where}: parameter is not an object")
            lists = (item.get("parameters", []), params)
            for param in [x for ps in lists if isinstance(ps, list) for x in ps]:
                if isinstance(param, dict) and "$ref" in param:
                    keys(param, {"$ref"})
                elif isinstance(param, dict):
                    keys(param, PARAM_KEYS)
                    name = param.get("name")
                    if param.get("in") not in {"query", "header", "path", "cookie"}:
                        errors.append(f"{w} {where}: parameter {name} lacks a valid in")
                        if _secret(name):  # rule 7 does not run on a refused shape
                            errors.append(f"{w} {where}: parameter {name}: sensitive "
                                          "field name")  # fmt: skip
            want(f"{where} security", op.get("security", []), list, dict)
            want(f"{where} requestBody", op.get("requestBody", {}), dict)
            if not want(f"{where} responses", op.get("responses"), dict):
                continue
            listed_204 = where.startswith("DELETE ") and (
                path, op.get("operationId")) in no_content  # fmt: skip
            for code, response in op["responses"].items():
                if code not in RESPONSE_CODES and not (code == "204" and listed_204):
                    errors.append(f"{w} {where}: response key {code!r} refused")
                if not isinstance(response, dict):
                    errors.append(f"{w} {where}: response {code} is not an object")
                    continue
                keys(response, {"description", "headers", "content"})
                for key, allowed in (("headers", HEADER_KEYS), ("content", {"schema"})):
                    sub = response.get(key, {})
                    if want(f"{where} {code} {key}", sub, dict, dict):
                        for obj in sub.values():
                            keys(obj, allowed)
    return errors


def _parameters(api: Json, where: str, params: Json) -> list[str]:
    """Tenant-parameter rule over a parameters list, `$ref`s resolved."""
    errors: list[str] = []
    for param in params:
        param = _resolve_param(api, param)
        if not isinstance(param, dict) or "$ref" in param:
            errors.append(f"{where}: unresolved parameter")
        elif "tenant" in str(param.get("name", "")).lower():
            errors.append(f"{where}: tenant parameter {param['name']}")
    return errors


def _resolve_param(api: Json, param: Json) -> Json:
    prefix, known = "#/components/parameters/", api["components"].get("parameters", {})
    for _ in range(8):
        ref = param.get("$ref") if isinstance(param, dict) else None
        if not isinstance(ref, str):
            break
        param = known.get(ref[len(prefix) :]) if ref.startswith(prefix) else None
    return param


def _wraps_problem(
    api: Json, schemas: dict[str, Json], node: Json, seen: frozenset[str] = frozenset()
) -> bool:
    """`node` is Problem, or reaches it through `$ref` and allOf/oneOf/anyOf or
    if/then/else/not at any depth (component and schema-file refs followed). A cycle or a ref that does not
    resolve counts as Problem: it is refused, not followed."""
    if not isinstance(node, dict):
        return False
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref.endswith("/Problem") or ref in seen or len(seen) > 16:
            return True
        target: Json = None
        if ref.startswith("#/components/schemas/"):
            target = api["components"]["schemas"].get(ref.rsplit("/", 1)[1])
        else:
            file, _, frag = ref.removeprefix("./schemas/").partition("#")
            target = schemas.get(file) if file else None
            for part in [x for x in frag.split("/") if x]:
                target = target.get(part) if isinstance(target, dict) else None
        if target is None or _wraps_problem(api, schemas, target, seen | {ref}):
            return True
    subs = [node.get(k) for k in ("allOf", "oneOf", "anyOf")]
    subs += [[node.get(k)] for k in ("if", "then", "else", "not")]
    return any(
        _wraps_problem(api, schemas, sub, seen)
        for alts in subs
        if isinstance(alts, list)
        for sub in alts
    )


def _decision_section(decision: str) -> str | None:
    """The text of owner decision `YYYY-MM-DD#N`: from its `N. **` line to the next
    numbered decision or heading of that day's decisions file; None if absent."""
    found = DECISION_ID.fullmatch(decision)
    if found is None:
        return None
    date, number = found.groups()
    file = ROOT / "docs" / "decisions" / f"{date}-owner-decisions.md"
    text = file.read_text(encoding="utf-8") if file.is_file() else ""
    text = text.replace("\r\n", "\n")
    section = re.search(rf"^{number}\. \*\*.*?(?=^\d+\. \*\*|^#|\Z)", text, re.M | re.S)
    return section.group(0) if section else None


def approvals(section: str) -> tuple[int, set[tuple[str, ...]], set[tuple[str, ...]]]:
    """A decision section's `Selected label:` lines, and the operations it approves
    and exempts, read only outside quoted text. A `Question:` or `Option text as
    presented:` quote runs to the first line ending with `."`, `?"` or `!"` (one
    left open runs to the end, so it fails closed). The directives are whole lines
    ``Approves: `METHOD /path` (`operationId`)`` and ``Exempts: `name`
    (`operationId`)``. They count only in one trailing block that starts after the
    label and the option text and is closed by `Implemented by:`, with nothing but
    directives (and blank lines) between its first line and `Implemented by:`.
    Limit: quote ends are found by punctuation, so a quoted line that itself ends
    with `."` closes the quote early; a directive after it still has to sit in
    that closed, directive-only trailing block."""
    labels, block = 0, list[str]()
    quoted = options = done = broken = False
    for line in section.replace("\r\n", "\n").split("\n")[1:]:
        text = line.strip()
        if quoted:
            quoted = not text.endswith(CLOSES)
        elif text.startswith(QUOTED):
            rest = text.split(":", 1)[1].strip()
            quoted = not (len(rest) > 1 and rest[0] == '"' and rest.endswith(CLOSES))
            options = options or text.startswith(QUOTED[1])
            broken = broken or (bool(block) and not done)
        elif text.startswith("Selected label:"):
            labels += 1
            broken = broken or (bool(block) and not done)
        elif text.startswith("Implemented by:"):
            done = True
        elif done:
            continue
        elif text.startswith(DIRECTIVES) and labels and options:
            block.append(text)
        elif block and text:
            broken = True  # only directives between the first one and Implemented by
    lines = block if done and not broken else []
    approved = {m.groups() for t in lines if (m := APPROVES.fullmatch(t))}
    exempts = {m.groups() for t in lines if (m := EXEMPTS.fullmatch(t))}
    return labels, approved, exempts


Key = tuple[str, str, str]


def approved_mutations(
    amendments: Json,
) -> tuple[set[Key], dict[Key, set[str]], list[str]]:
    """`mutations` entries whose cited owner decision has one `Selected label:`
    line and approves the operation (`approvals`) and that match an amended
    operation, with the exemptions of approved entries whose own decision has one
    `Selected label:` line and exempts that operation on an `Exempts:` line
    (rule 6); every other entry or exemption is an error."""
    approved: set[Key] = set()
    exempt: dict[Key, set[str]] = {}
    errors: list[str] = []
    ops = amendments.get("operations", {})
    for entry in amendments.get("mutations", []):
        method, path, op_id, decision = (entry[k] for k in MUTATION_KEYS)
        where = f"project contract mutations {method.upper()} {path} ({op_id})"
        op = ops.get(path, {}).get(method)
        section = _decision_section(decision)
        labels, ok, _ = approvals(section or "")
        if not isinstance(op, dict) or op.get("operationId") != op_id:
            errors.append(f"{where}: unused mutations entry")
        elif section is None:
            errors.append(f"{where}: cites no owner decision ({decision})")
        elif labels != 1:
            errors.append(f"{where}: decision {decision} has {labels} Selected label "
                          "lines outside quoted text, not one")  # fmt: skip
        elif (method.upper(), path, op_id) not in ok:
            errors.append(f"{where}: decision {decision} does not approve it on an "
                          f"`Approves: `{method.upper()} {path}` (`{op_id}`)` line")  # fmt: skip
        else:
            approved.add((method, path, op_id))
            for x in entry.get("exemptions", []):
                cited = _decision_section(x["decision"])
                marks, _, exempts = approvals(cited or "")
                if cited is None:
                    errors.append(f"{where}: exemption {x['name']} cites no owner "
                                  f"decision ({x['decision']})")  # fmt: skip
                elif marks != 1:
                    errors.append(f"{where}: exemption {x['name']}: decision "
                                  f"{x['decision']} has no single Selected label")  # fmt: skip
                elif (x["name"], op_id) not in exempts:
                    errors.append(f"{where}: exemption {x['name']}: decision "
                                  f"{x['decision']} does not exempt it on an "
                                  f"`Exempts: `{x['name']}` (`{op_id}`)` line")  # fmt: skip
                else:
                    exempt.setdefault((method, path, op_id), set()).add(x["name"])
    return approved, exempt, errors


def check_operations(
    api: Json, schemas: dict[str, Json], amendments: Json,
    approved: set[Key], exempt: dict[Key, set[str]],
) -> list[str]:  # fmt: skip
    errors: list[str] = []
    schemes = set(api["components"].get("securitySchemes", {}))
    problem = {"$ref": "#/components/schemas/Problem"}
    prefix = "#/components/schemas/"
    for path, item in amendments.get("operations", {}).items():
        errors += _parameters(
            api, f"project contract {path}", item.get("parameters", [])
        )
        for method, op in item.items():
            where = f"project contract {method.upper()} {path}"
            if method in PATH_FIELDS:
                continue
            op_id = str(op.get("operationId", ""))
            if method in MUTATING and (method, path, op_id) not in approved:
                errors.append(f"{where}: mutating operation refused (not in the "
                              "owner-approved mutations allowlist)")  # fmt: skip
            if method in MUTATING and (
                BROKER_SURFACE.search(path)
                or TRADING_VERB.search(path)
                or TRADING_VERB.search(op_id)
            ):
                errors.append(f"{where}: broker or trading mutation is never "
                              "amendable")  # fmt: skip
            elif method not in MUTATING and method != "get":
                errors.append(f"{where}: only GET operations may be amended")
            ex = exempt.get((method, path, op_id), set())
            security = op.get("security", api.get("security"))
            if "unauthenticated_login" in ex:
                if op.get("security") != []:  # explicit, never inherited
                    errors.append(f"{where}: unauthenticated_login exemption needs "
                                  "security: [] on the operation")  # fmt: skip
            elif security != SESSION or "sessionCookie" not in schemes:
                errors.append(f"{where}: no session security (exactly sessionCookie)")
            responses = op.get("responses", {})
            media = responses.get("default", {}).get("content", {})
            if media.get("application/problem+json", {}).get("schema") != problem:
                errors.append(f"{where}: default is not a Problem response")
            if set(media) != {"application/problem+json"}:
                errors.append(f"{where}: default content is not exactly "
                              "application/problem+json")  # fmt: skip
            ok = sorted(k for k in responses if re.fullmatch(r"2\d\d", k))
            if not ok:
                errors.append(f"{where}: no 2xx response")
            if "no_content" in ex and ok != ["204"]:
                errors.append(f"{where}: no_content exemption needs one 2xx, a 204")
            for code in ok:
                body = responses[code]
                cache = body.get("headers", {}).get("Cache-Control", {})
                if cache.get("schema") != {"type": "string", "const": "no-store"}:
                    errors.append(f"{where}: success lacks Cache-Control: no-store")
                if code == "204":
                    if "no_content" not in ex:
                        errors.append(f"{where}: 204 needs the no_content exemption")
                    if "content" in body:
                        errors.append(f"{where}: 204 has content")
                    continue
                if set(body.get("content", {})) != {"application/json"}:
                    errors.append(
                        f"{where} {code}: content is not exactly application/json"
                    )
                schema = body.get("content", {}).get("application/json", {})
                schema = schema.get("schema")  # media objects: check_shapes
                ref = schema.get("$ref") if isinstance(schema, dict) else None
                ref = ref if isinstance(ref, str) else ""
                name = ref[len(prefix) :] if ref.startswith(prefix) else ""
                if name not in api["components"]["schemas"]:
                    errors.append(f"{where} {code}: unknown response schema {ref!r}")
                elif _wraps_problem(api, schemas, {"$ref": ref}):
                    errors.append(f"{where} {code}: 2xx body is a Problem")
            errors += _parameters(api, where, op.get("parameters", []))
    return errors


def _secret(name: Json) -> bool:
    return bool(SECRET_NAME.search(re.sub(r"[_\-]", "", str(name).lower())))


def _secret_names(api: Json, node: Json, pointer: str = "") -> list[str]:
    """Pointers of every name a client sees that looks like a secret, at any depth:
    `properties` and `patternProperties` keys, parameter names (`$ref`s resolved)
    and response header names."""
    out: list[str] = []
    if isinstance(node, dict):
        for key in ("properties", "patternProperties", "headers"):
            names = node.get(key)
            if isinstance(names, dict):
                out += [f"{pointer}/{key}/{n}" for n in names if _secret(n)]
        param = _resolve_param(api, node) if "$ref" in node else node
        name = param.get("name") if isinstance(param, dict) else None
        if isinstance(name, str) and _secret(name):
            out.append(f"{pointer} parameter {name}")
        for key, sub in node.items():
            out += _secret_names(api, sub, f"{pointer}/{key}")
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            out += _secret_names(api, sub, f"{pointer}/{i}")
    return out


def check_sensitive(api: Json, amendments: Json) -> list[str]:
    """Every amendment source that adds a name (rule 7); `check_shapes` refuses an
    amendment key that is not one of these or a reviewed non-name field."""
    added: dict[str, Json] = {
        **amendments.get("new_schemas", {}),
        **{n: {"properties": a.get("add", {}), "allOf": a.get("all_of", [])}
           for n, a in amendments["schemas"].items()},
        **{f"request {n}": {"properties": r.get("optional", {})}
           for n, r in amendments.get("requests", {}).items()},
        **{f"operation {p}": item for p, item in amendments.get("operations", {}).items()},
        "page": {"properties": amendments["page"].get("add", {})},
        "Coverage": amendments["Coverage"],
    }  # fmt: skip
    found = {(n, ptr) for n, s in added.items() for ptr in _secret_names(api, s)}
    allowed: set[tuple[str, str]] = set()
    errors: list[str] = []
    for entry in amendments.get("sensitive_fields", []):
        key = (str(entry.get("schema")), str(entry.get("field")))
        decision = entry.get("decision")
        if not isinstance(decision, str) or not decision.strip():
            errors.append(f"project contract sensitive_fields {key}: no decision")
        elif key not in found:
            errors.append(f"project contract sensitive_fields {key}: unused entry")
        else:
            allowed.add(key)
    errors += [f"project contract {name}#{ptr}: sensitive field name"
               for name, ptr in sorted(found - allowed)]  # fmt: skip
    return errors


def check_amended(schemas: dict[str, Json], amendments: Json) -> list[str]:
    from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

    errors: list[str] = []
    saved, pc.AMENDMENTS = pc.AMENDMENTS, amendments
    try:
        for name, amended in amendments["schemas"].items():
            where = f"project contract {name}"
            try:
                s = pc.schema(name)
            except REFUSED as exc:
                errors.append(f"{where}: amendment refused: {exc!r}")
                continue
            meta = Draft202012Validator(Draft202012Validator.META_SCHEMA)
            errors += [f"{where}: invalid schema: {e.message}"
                       for e in meta.iter_errors(s)]  # fmt: skip
            for rule in amended.get("all_of", []):
                if unknown := _named(rule) - set(s["properties"]):
                    errors.append(f"{where}: all_of names unknown property {unknown}")
            errors += check_decimals(where, s, schemas)
        for name in amendments.get("new_schemas", {}):
            where = f"project contract new schema {name}"
            try:
                s = pc.schema(name)
            except REFUSED as exc:
                errors.append(f"{where}: amendment refused: {exc!r}")
                continue
            meta = Draft202012Validator(Draft202012Validator.META_SCHEMA)
            errors += [f"{where}: invalid schema: {e.message}"
                       for e in meta.iter_errors(s)]  # fmt: skip
            if s.get("additionalProperties") is not False:
                errors.append(f"{where}: not closed")
            errors += check_decimals(where, s, schemas)
            errors += check_times(where, s)
    finally:
        pc.AMENDMENTS = saved
    return errors


def run_checks(
    api: Json, schemas: dict[str, Json], examples: dict[str, Json],
    amendments: Json = None,
) -> list[str]:  # fmt: skip
    amendments = pc.AMENDMENTS if amendments is None else amendments
    errors = check_shapes(amendments)
    login: frozenset[tuple[str, str]] = frozenset()
    if not errors:  # a malformed amendment is reported, never half-applied
        api, errors = amended_api(api, amendments)
        approved, exempt, found = approved_mutations(amendments)
        login = frozenset((m, p) for (m, p, _), names in exempt.items()
                          if "unauthenticated_login" in names)  # fmt: skip
        errors += found + check_operations(api, schemas, amendments, approved, exempt)
        errors += check_sensitive(api, amendments)
        errors += check_amended(schemas, amendments)
    errors += check_routes(api, schemas, login)
    errors += check_decimals("openapi#/components", api["components"], schemas)
    errors += check_decimals("openapi#/paths", api["paths"], schemas)
    for name, schema in schemas.items():
        errors += check_decimals(name, schema, schemas)
    for name, doc in examples.items():
        errors += check_timestamps(name, doc)
    return errors


def guarded(fn: Any, *args: Any) -> list[str]:
    """A check that raises on a malformed amended document is a refusal."""
    try:
        result: list[str] = fn(*args)
    except Exception as exc:  # any crash is a reported refusal (exit nonzero)
        return [f"checker crashed on malformed amendment: {type(exc).__name__}"]
    return result


def format_checker_enforces_datetime() -> bool:
    from jsonschema import FormatChecker

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

    def amended_money(m: Json) -> None:
        m["schemas"]["experiment"]["add"]["fee"] = {"type": "number"}

    def unknown_rule(m: Json) -> None:
        rule = m["schemas"]["experiment"]["all_of"][0]
        rule["then"]["properties"] = {"parent_optimiser_version": {"type": "string"}}

    def duplicate_state(m: Json) -> None:
        m["schemas"]["experiment"]["enum_add"]["state"].append("failed")

    def bad_rule(m: Json) -> None:
        m["schemas"]["experiment"]["all_of"][0]["if"] = {"type": "nonsense"}

    def new_op(m: Json) -> Json:
        return m["operations"]["/session/approval-classes"]["get"]

    def new_schema(m: Json) -> Json:
        return m["new_schemas"]["approval_classes"]

    def dup_operation_id(m: Json) -> None:
        new_op(m)["operationId"] = "readCapabilities"

    def dup_new_operation_id(m: Json) -> None:
        new_op(m)["operationId"] = "readSession"

    def dup_path(m: Json) -> None:
        m["operations"]["/capabilities"] = {
            "get": copy.deepcopy(new_op(m)) | {"operationId": "readCapabilities2"}
        }

    def dup_schema(m: Json) -> None:
        m["new_schemas"]["experiment"] = copy.deepcopy(new_schema(m))

    def dup_component(m: Json) -> None:
        m["new_schemas"]["Health"] = copy.deepcopy(new_schema(m))

    def public_op(m: Json) -> None:
        new_op(m)["security"] = []

    def unknown_scheme(m: Json) -> None:
        new_op(m)["security"] = [{"bearer": []}]

    def no_problem(m: Json) -> None:
        del new_op(m)["responses"]["default"]

    def cacheable(m: Json) -> None:
        del new_op(m)["responses"]["200"]["headers"]

    def unknown_body(m: Json) -> None:
        body = new_op(m)["responses"]["200"]["content"]["application/json"]
        body["schema"] = {"$ref": "#/components/schemas/approval_class"}

    def tenant_param(m: Json) -> None:
        new_op(m)["parameters"] = [
            {"name": "tenant_id", "in": "query", "schema": {"type": "string"}}
        ]

    def new_mutation(m: Json) -> None:
        item = m["operations"]["/session/approval-classes"]
        item["post"] = copy.deepcopy(item["get"]) | {"operationId": "grantClasses"}

    def listed_mutation(decision: str, op_id: str = "noteViewed") -> Any:
        def change(m: Json) -> None:
            item = m["operations"]["/session/approval-classes"]
            item["post"] = copy.deepcopy(item["get"]) | {"operationId": op_id}
            item["post"]["parameters"] = copy.deepcopy(hdr)
            m["mutations"].append(mut | {"operationId": op_id, "decision": decision})

        return change

    def exemption(op_id: str, change: Any) -> Any:
        def inject(m: Json) -> None:
            (entry,) = [e for e in m["mutations"] if e["operationId"] == op_id]
            change(entry, m)

        return inject

    def login_security(e: Json, m: Json) -> None:
        m["operations"]["/session"]["post"]["security"] = copy.deepcopy(SESSION)

    def delete_body(e: Json, m: Json) -> None:
        m["operations"]["/session"]["delete"]["responses"]["204"]["content"] = {
            "application/json": {"schema": {"$ref": "#/components/schemas/session"}}
        }

    def delete_200(e: Json, m: Json) -> None:
        responses = m["operations"]["/session"]["delete"]["responses"]
        responses["200"] = copy.deepcopy(
            m["operations"]["/session"]["get"]["responses"]["200"]
        )

    login_x = {"name": "unauthenticated_login", "decision": "2026-10-21#25"}

    def logout_csrf(e: Json, m: Json) -> None:  # no_content never waives rule 2
        op = m["operations"]["/session"]["delete"]
        op["parameters"] = [p for p in op["parameters"] if p["name"] != "X-CSRF-Token"]

    exemption_cases = {
        "login without its exemption": (exemption("createSession", lambda e, m: e.pop(
            "exemptions")), "POST /session: missing required X-CSRF-Token"),
        "login exemption not on an approved entry": (exemption("createSession",
            lambda e, m: e.update(decision="2026-10-21#15")),
            "POST /session: missing required Idempotency-Key"),
        "login exemption citing a missing decision": (exemption("createSession",
            lambda e, m: e.update(exemptions=[login_x | {"decision": "2026-10-21#99"}])),
            "exemption unauthenticated_login cites no owner decision"),
        "login exemption with session security": (exemption("createSession",
            login_security), "needs security: [] on the operation"),
        "logout without no_content": (exemption("revokeSession", lambda e, m: e.pop(
            "exemptions")), "DELETE /session: response key '204' refused"),
        "logout without its CSRF header": (exemption("revokeSession", logout_csrf),
            "DELETE /session: missing required X-CSRF-Token"),
        "no_content with a body": (exemption("revokeSession", delete_body),
            "204 has content"),
        "no_content with another 2xx": (exemption("revokeSession", delete_200),
            "needs one 2xx, a 204"),
        "exemption on the wrong method": (exemption("revokeSession", lambda e, m: e[
            "exemptions"].append(login_x)), "unauthenticated_login applies only to POST"),
        "exemption on step-up": (exemption("stepUpSession", lambda e, m: e.update(
            exemptions=[login_x])),
            "unauthenticated_login applies only to POST /session (createSession)"),
        "exemption on another approved op": (exemption("changeMembershipRole",
            lambda e, m: e.update(exemptions=[login_x | {"name": "no_content"}])),
            "no_content applies only to DELETE /session (revokeSession)"),
        "exemption citing a decision without the Exempts line": (exemption(
            "createSession", lambda e, m: e.update(exemptions=[login_x | {
                "decision": "2026-10-21#24"}])), "does not exempt it on an `Exempts:"),
        "unreviewed exemption": (exemption("createSession", lambda e, m: e.update(
            exemptions=[login_x | {"name": "no_csrf"}])), "not a reviewed exemption"),
        "duplicate exemption": (exemption("createSession", lambda e, m: e.update(
            exemptions=[login_x, login_x])), "duplicate exemption"),
        "exemption decision not an id": (exemption("createSession", lambda e, m: e.update(
            exemptions=[login_x | {"decision": "25"}])), "not an owner decision id"),
        "exemption extra key": (exemption("createSession", lambda e, m: e.update(
            exemptions=[login_x | {"scope": "all"}])), "unreviewed amendment key 'scope'"),
        "exemptions not a list": (exemption("createSession", lambda e, m: e.update(
            exemptions={})), "exemptions is not a list"),
    }  # fmt: skip

    def new_number(m: Json) -> None:
        new_schema(m)["properties"]["cost"] = {"type": "string"}

    def new_open(m: Json) -> None:
        new_schema(m)["additionalProperties"] = True

    def new_naive_at(m: Json) -> None:
        new_schema(m)["properties"]["evaluated_at"] = {"type": "string"}

    def new_invalid(m: Json) -> None:
        new_schema(m)["properties"]["advisory"] = {"type": "nonsense"}

    # Rule 6/7 hardening: (label, parent path, key, value, expected); each sets
    # amendments[path...][key] = value on a copy.
    OP = ("operations", "/session/approval-classes")
    GET, NS = (*OP, "get"), ("new_schemas", "approval_classes")
    RS, OK = (*OP, "get", "responses"), (*OP, "get", "responses", "200")
    JS = (*OK, "content", "application/json")
    get = pc.AMENDMENTS["operations"]["/session/approval-classes"]["get"]
    s, csrf = (
        {"type": "string"},
        {"schema": "session", "field": "/properties/csrf_token"},
    )
    hdr = [
        {"name": h, "in": "header", "required": True, "schema": s}
        for h in ("Idempotency-Key", "X-CSRF-Token")
    ]
    q = {"in": "query", "schema": s}
    prob = {"$ref": "#/components/schemas/Problem"}
    ok2 = get["responses"]["200"] | {"content": {"application/json": {"schema": prob}}}
    outcome = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Outcome",
        "additionalProperties": False,
        "allOf": [{"$ref": "common.schema.json#/$defs/Problem"}],
    }
    SC, real = ("security_schemes",), pc.AMENDMENTS["mutations"]
    mut = {"method": "post", "path": "/session/approval-classes",
           "operationId": "noteViewed", "decision": "2026-10-21#15"}  # fmt: skip
    held = (
        "new_schemas",
        "approval_classes",
        "$defs",
        "Held",
        "properties",
        "step_up",
        "properties",
    )
    table = [
        ("innocuous mutating operation", OP, "post", get | {"operationId": "noteViewed",
         "parameters": hdr}, "mutating operation refused"),
        ("amended HEAD operation", OP, "head", get | {"operationId": "headClasses"},
         "only GET operations may be amended"),
        ("path-level tenant parameter", OP, "parameters", [q | {"name": "tenant_id"}],
         "tenant parameter tenant_id"),
        ("path-level summary (no crash)", OP, "summary", "x", "amendment refused"),
        ("dangling parameter $ref", GET, "parameters",
         [{"$ref": "#/components/parameters/Missing"}], "unresolved parameter"),
        ("2xx Problem body", JS, "schema", prob, "200: 2xx body is a Problem"),
        ("second 2xx Problem body", RS, "202", ok2, "202: 2xx body is a Problem"),
        ("wildcard 2XX response", RS, "2XX", ok2, "response key '2XX' refused"),
        ("lower-case 2xx response", RS, "2xx", ok2, "response key '2xx' refused"),
        ("2xx wraps Problem (new schema)", JS, "schema",
         {"$ref": "#/components/schemas/outcome"}, "200: 2xx body is a Problem"),
        ("session with scopes", GET, "security", [{"sessionCookie": ["admin"]}],
         "no session security"),
        ("anonymous alternative", GET, "security", [{"sessionCookie": []}, {}],
         "no session security"),
        ("path-level servers", OP, "servers", [{"url": "/x"}], "servers refused"),
        ("operation servers", GET, "servers", [{"url": "/x"}], "servers refused"),
        ("new schema secret field", (*NS, "properties"), "apiKey", s, "apiKey: sensi"),
        ("nested secret field", held, "Refresh-Token", s, "Refresh-Token: sensi"),
        ("amended add secret", ("schemas", "claim", "add"), "private_key", s,
         "private_key: sensi"),
        ("request optional secret", ("requests", "WatchlistEntry", "optional"),
         "PASSWORD", s, "PASSWORD: sensi"),
        ("page.add secret", ("page", "add"), "session_token", s,
         "page#/properties/session_token: sensi"),
        ("Coverage secret", ("Coverage", "properties"), "secretRef", s,
         "Coverage#/properties/secretRef: sensi"),
        ("operation parameter secret", GET, "parameters", [q | {"name": "api_key"}],
         "parameter api_key: sensi"),
        ("path-level parameter secret", OP, "parameters", [q | {"name": "X-Api-Key"}],
         "parameter X-Api-Key: sensi"),
        ("response header secret", (*OK, "headers"), "X-Refresh-Token", {"schema": s},
         "headers/X-Refresh-Token: sensi"),
        ("patternProperties secret", NS, "patternProperties", {"^secret_": s},
         "patternProperties/^secret_: sensi"),
        ("additionalProperties secret", (*NS, "properties"), "meta", {
         "additionalProperties": {"properties": {"Password": s}}}, "Password: sensi"),
        ("inline operation schema secret", GET, "requestBody", {"content": {
         "application/json": {"schema": {"properties": {"client_secret": s}}}}},
         "client_secret: sensi"),
        ("csrf_token not allowlisted", (), "sensitive_fields", [],
         "csrf_token: sensitive field"),
        ("allowlist without decision", (), "sensitive_fields",
         [csrf | {"decision": " "}], "no decision"),
        ("allowlist unused entry", (), "sensitive_fields", [csrf | {"decision": "y"},
         csrf | {"field": "/properties/x", "decision": "y"}], "unused entry"),
        ("unreviewed amendment key", (), "extra", {}, "unreviewed amendment key"),
        ("unreviewed schema amendment key", ("schemas", "claim"), "rename", {},
         "unreviewed amendment key 'rename'"),
        ("responses not an object", GET, "responses", [], "responses is not an object"),
        ("response not an object", RS, "200", "ok", "response 200 is not an object"),
        ("headers not an object", OK, "headers", [], "headers is not an object"),
        ("content not an object", OK, "content", "json", "content is not an object"),
        ("media schema not an object", JS, "schema", "x", "unknown response schema"),
        ("body $ref not a string", JS, "schema", {"$ref": 7}, "unknown response"),
        ("parameter not an object", GET, "parameters", ["tenant_id"],
         "parameter is not an object"),
        ("security not a list", GET, "security", {"sessionCookie": []},
         "security is not a list"),
        ("sensitive_fields null", (), "sensitive_fields", None, "is not a list"),
        ("sensitive_fields string", (), "sensitive_fields", "csrf", "is not a list"),
        ("add not an object", ("schemas", "claim"), "add", [], "add is not an object"),
        ("nullable not a list", ("schemas", "claim"), "nullable", "x", "not a list"),
        ("all_of boolean if", ("schemas", "experiment"), "all_of",
         [{"if": True, "then": {"properties": {"ghost": {}}}}], "{'ghost'}"),
        ("new schema not an object", ("new_schemas",), "session", [],
         "new_schemas.session is not an object"),
        ("new schema $defs not an object", NS, "$defs", [], "amendment refused"),
        ("new schema properties a list", NS, "properties", [], "approval_classes"),
        ("optional not an object", ("requests", "WatchlistEntry"), "optional", None,
         "optional is not an object"),
        ("operations not an object", (), "operations", [], "is not an object"),
        ("page not an object", (), "page", None, "page is not an object"),
        ("passwd field", (*NS, "properties"), "db_passwd", s, "db_passwd: sensi"),
        ("pwd parameter", GET, "parameters", [q | {"name": "PWD"}], "PWD: sensi"),
        ("unhashable type list", ("schemas", "account_snapshot", "add", "ledger_cash"),
         "type", [[]], "invalid schema"),
        ("operation x- key", GET, "x-internal", True, "unreviewed amendment key 'x-in"),
        ("operation tags", GET, "tags", ["a"], "unreviewed amendment key 'tags'"),
        ("operation callbacks", GET, "callbacks", {}, "unreviewed amendment key 'cal"),
        ("response links", OK, "links", {}, "unreviewed amendment key 'links'"),
        ("media examples", JS, "examples", {}, "unreviewed amendment key 'examples'"),
        ("header x- key", (*OK, "headers", "Cache-Control"), "x-a", 1, "key 'x-a'"),
        ("parameter x- key", GET, "parameters", [q | {"name": "p", "x-b": 1}],
         "unreviewed amendment key 'x-b'"),
        ("$ref parameter extra key", GET, "parameters", [{"$ref": "#/components/"
         "parameters/X", "name": "p"}], "unreviewed amendment key 'name'"),
        ("parameter without in", GET, "parameters", [{"name": "api_key", "schema": s}],
         "parameter api_key lacks a valid in"),
        ("parameter without in (name scanned)", GET, "parameters",
         [{"name": "api_key", "schema": s}], "parameter api_key: sensi"),
        ("parameter in body", GET, "parameters", [q | {"name": "p", "in": "body"}],
         "parameter p lacks a valid in"),
        ("2xx extra media type", (*OK, "content"), "text/html", {"schema": s},
         "200: content is not exactly application/json"),
        ("default extra media type", (*RS, "default", "content"), "application/json",
         {"schema": prob}, "default content is not exactly application/problem+json"),
        ("4xx response key", RS, "404", ok2, "response key '404' refused"),
        ("3xx response key", RS, "302", ok2, "response key '302' refused"),
        ("1xx response key", RS, "100", ok2, "response key '100' refused"),
        ("204 response key", RS, "204", ok2, "response key '204' refused"),
        ("cookie without __Host-", SC, "sessionCookie", {"name": "pi_session2"},
         "sessionCookie.name is not __Host-pi_session (decision 14)"),
        ("other __Host- cookie name", SC, "sessionCookie", {"name": "__Host-other"},
         "sessionCookie.name is not __Host-pi_session (decision 14)"),
        ("cookie name not a string", SC, "sessionCookie", {"name": 7}, "sessionCookie.name is not __Host-pi_session (decision 14)"),
        ("cookie scheme extra key", (*SC, "sessionCookie"), "in", "header",
         "unreviewed amendment key 'in'"),
        ("other security scheme", SC, "bearer", {"name": "__Host-x"},
         "security scheme 'bearer' refused"),
        ("cookie scheme not an object", SC, "sessionCookie", "__Host-x",
         "security_schemes.sessionCookie is not an object"),
        ("security_schemes not an object", (), "security_schemes", [],
         "security_schemes is not an object"),
        ("mutations not a list", (), "mutations", {}, "mutations is not a list"),
        ("mutation entry not an object", (), "mutations", ["x"],
         "mutations: an entry is not dict"),
        ("mutation entry without decision", (), "mutations",
         [{k: v for k, v in mut.items() if k != "decision"}], "lacks ['decision']"),
        ("mutation entry extra key", (), "mutations", [mut | {"waive": "rule 2"}],
         "unreviewed amendment key 'waive'"),
        ("mutation decision not an id", (), "mutations",
         [mut | {"decision": "owner said yes"}], "not an owner decision id"),
        ("mutation decision not a string", (), "mutations", [mut | {"decision": 15}],
         "mutations[0]: an entry is not str"),
        ("mutation GET method", (), "mutations", [mut | {"method": "get"}],
         "not a mutating method"),
        ("mutation entry unused", (), "mutations", [*real, mut],
         "unused mutations entry"),
    ]  # fmt: skip

    def setter(path: tuple[str, ...], key: str, value: Json) -> Any:
        def change(m: Json) -> None:
            node = m
            for part in path:
                node = node[part]
            node[key] = copy.deepcopy(value)
            if value == {"$ref": "#/components/schemas/outcome"}:
                m["new_schemas"]["outcome"] = copy.deepcopy(outcome)

        return change

    amended_cases = {
        **{label: (setter(p, k, v), want) for label, p, k, v, want in table},
        "operation duplicate operationId": (dup_operation_id, "amendment refused"),
        "operation duplicate new operationId": (dup_new_operation_id, "refused"),
        "operation duplicate path": (dup_path, "amendment refused"),
        "new schema existing name": (dup_schema, "amendment refused"),
        "new schema existing component": (dup_component, "amendment refused"),
        "operation without session": (public_op, "no session security"),
        "operation unknown scheme": (unknown_scheme, "no session security"),
        "operation without Problem": (no_problem, "not a Problem response"),
        "operation cacheable": (cacheable, "lacks Cache-Control: no-store"),
        "operation unknown body": (unknown_body, "unknown response schema"),
        "operation tenant parameter": (tenant_param, "tenant parameter"),
        "new mutating operation": (new_mutation, "missing required Idempotency-Key"),
        "mutation citing a decision without an Approves line": (
            listed_mutation("2026-10-21#15"),
            "decision 2026-10-21#15 does not approve",
        ),
        "mutation citing a missing decision": (
            listed_mutation("2026-10-21#99"),
            "cites no owner decision",
        ),
        "mutation citing a missing decisions file": (
            listed_mutation("2001-01-01#1"),
            "cites no owner decision",
        ),
        "listed mutation still refused": (
            listed_mutation("2026-10-21#15"),
            "mutating operation refused",
        ),
        **exemption_cases,
        "trading-verb mutation": (
            listed_mutation("2026-10-21#15", "placeOrder"),
            "never amendable",
        ),
        "new schema string money": (
            new_number,
            "new schema approval_classes/cost: money/quantity not a decimal",
        ),
        "new schema open": (new_open, "not closed"),
        "new schema naive _at": (new_naive_at, "evaluated_at: not a Timestamp"),
        "new schema invalid": (new_invalid, "invalid schema"),
        "amended number money": (amended_money, "fee: money/quantity not a decimal"),
        "all_of unknown property": (unknown_rule, "all_of names unknown property"),
        "enum_add duplicate": (duplicate_state, "amendment refused"),
        "invalid amended schema": (bad_rule, "invalid schema"),
    }

    def api_case(add: Json, path: tuple[str, ...], key: str, value: Json) -> Any:
        def change(a: Json, m: Json) -> None:
            for kind, defs in add.items():
                a["components"].setdefault(kind, {}).update(copy.deepcopy(defs))
            setter(path, key, value)(m)

        return change

    pref, sref = "#/components/parameters/", "#/components/schemas/"
    api_cases = {
        "tenant parameter via $ref": (api_case(
            {"parameters": {"T": q | {"name": "tenant_id"}}}, GET, "parameters",
            [{"$ref": pref + "T"}]), "tenant parameter tenant_id"),
        "$ref parameter secret": (api_case(
            {"parameters": {"K": q | {"name": "access_token"}}}, GET, "parameters",
            [{"$ref": pref + "K"}]), "parameter access_token: sensi"),
        "$ref parameter without in": (api_case(
            {"parameters": {"K": {"name": "api_key", "schema": s}}}, GET, "parameters",
            [{"$ref": pref + "K"}]), "parameter api_key: sensi"),
        "2xx alias of Problem": (api_case(
            {"schemas": {"Outcome": prob}}, JS, "schema", {"$ref": sref + "Outcome"}),
            "200: 2xx body is a Problem"),
        "2xx allOf wraps Problem": (api_case(
            {"schemas": {"W": {"allOf": [prob]}}}, JS, "schema", {"$ref": sref + "W"}),
            "200: 2xx body is a Problem"),
        "2xx oneOf/anyOf deep Problem": (api_case({"schemas": {
            "Outer": {"oneOf": [{"properties": {}}, {"$ref": sref + "Mid"}]},
            "Mid": {"anyOf": [{"allOf": [
                {"$ref": "./schemas/common.schema.json#/$defs/Problem"}]}]}}},
            JS, "schema", {"$ref": sref + "Outer"}), "200: 2xx body is a Problem"),
        "2xx if/then Problem": (api_case({"schemas": {"C": {"if": {"type": "object"},
            "then": prob}}}, JS, "schema", {"$ref": sref + "C"}), "2xx body is a Problem"),
        "2xx else Problem": (api_case({"schemas": {"E": {"if": {}, "else": {"allOf": [
            prob]}}}}, JS, "schema", {"$ref": sref + "E"}), "2xx body is a Problem"),
        "2xx not Problem": (api_case({"schemas": {"N": {"not": prob}}}, JS, "schema",
            {"$ref": sref + "N"}), "2xx body is a Problem"),
        "another known scheme": (api_case({"securitySchemes": {"serviceKey": {
            "type": "apiKey", "in": "header", "name": "X-Service-Key"}}}, GET,
            "security", [{"serviceKey": []}]), "no session security"),
    }  # fmt: skip
    failures = 0
    baseline = guarded(run_checks, api, schemas, examples)
    if baseline:
        print("self-test: unmodified contracts fail:", *baseline, sep="\n  ")
        failures += 1
    for label, (inject, expected) in cases.items():
        a, s, e = copy.deepcopy(api), copy.deepcopy(schemas), copy.deepcopy(examples)
        inject(a, s, e)
        found = [m for m in guarded(run_checks, a, s, e) if expected in m]
        print(f"self-test {label}: {'detected' if found else 'MISSED'}")
        failures += not found
    for label, (change, expected) in amended_cases.items():
        m = copy.deepcopy(pc.AMENDMENTS)
        change(m)
        found = [
            x for x in guarded(run_checks, api, schemas, examples, m) if expected in x
        ]
        print(f"self-test {label}: {'detected' if found else 'MISSED'}")
        failures += not found
    for label, (both, expected) in api_cases.items():
        a, m = copy.deepcopy(api), copy.deepcopy(pc.AMENDMENTS)
        both(a, m)
        found = [
            x for x in guarded(run_checks, a, schemas, examples, m) if expected in x
        ]
        print(f"self-test {label}: {'detected' if found else 'MISSED'}")
        failures += not found

    # Decision text (rule 6): each case is section 1 of a scratch decisions file;
    # section 2 approves the operation, so section 1 must not run into it.
    named = "`POST /session/approval-classes` (`noteViewed`)"
    label, a = '   Selected label: "Yes".\n', f"   Approves: {named}\n"
    ask, done = '   Question: "Approve?"\n', "   Implemented by: x.\n"
    o = """   Option text as presented: "Do it. Not 'no'."\n"""
    opt = "   Option text as presented: "
    decision_cases = {
        "approval inside multi-line option text": (
            f'{ask}{label}{opt}"Do it.\n{a}   Not \'no\'."\n{done}', "does not approve"),
        "crafted negative label, Approves inside option text": (
            f'{ask}   Selected label: "Reject".\n{opt}"Keep it out.\n{a}'
            '   Not this."\n' f"{done}", "does not approve"),  # review residual R1
        "quote with an inner quoted word": (
            f'{ask}{label}{opt}"Do it.\n   He said "no"\n{a}   Not this."\n{done}',
            "does not approve"),  # review: an inner quote must not end the block
        "inner quote, then directives inside the quote": (
            f'{ask}{label}{opt}"Do it.\n   He said "no"\n{a}   Implemented by: quoted."\n',
            "does not approve"),  # only a sentence-final quote closes the block
        "Approves after a U+2028 separator": (
            f"{ask}{label}{o}   Note:\u2028Approves: {named}\n{done}",
            "does not approve"),  # lines are split on \\n only, never splitlines()
        "Approves line with trailing text": (
            f"{ask}{label}{o}{a.rstrip()} and more\n{done}", "does not approve"),
        "non-directive line in the directive block": (
            f"{ask}{label}{o}{a}   Note: and more.\n{done}", "does not approve"),
        "directive block without Implemented by": (
            f"{ask}{label}{o}{a}", "does not approve"),
        "label after the directive block": (
            f"{ask}{o}{a}{label}{done}", "does not approve"),
        "approval only in the question": (
            f'   Question: "Approve\n{a}   now?"\n{label}{o}{done}', "does not approve"),
        "approval as one-line option text": (
            f'{ask}{label}{opt}"Approves: {named}"\n{done}', "does not approve"),
        "Approves after other text on a line": (
            f"{ask}{label}{o}   Note: Approves: {named}\n{done}", "does not approve"),
        "Approves before the option text": (f"{ask}{label}{a}{o}{done}", "not approve"),
        "Approves before the label": (f"{ask}{o}{a}{label}{done}", "does not approve"),
        "Approves after Implemented by": (f"{ask}{label}{o}{done}{a}", "not approve"),
        "Approves line without Selected label": (
            f"{ask}{o}{a}{done}", "0 Selected label lines"),
        "crafted label inside the question": (
            f'   Question: "Pick\n{label}   one?"\n{o}{a}{done}', "0 Selected label"),
        "crafted label inside option text": (
            f'{ask}{opt}"Pick.\n{label}   One."\n{a}{done}', "0 Selected label"),
        "two Selected label lines": (f"{ask}{label}{label}{o}{a}{done}", "2 Selected"),
        "Approves line for another operation": (
            f"{ask}{label}{o}   Approves: `POST /session/approval-classes` (`other`)\n"
            f"{done}", "does not approve"),
        "Approves line for another method": (
            f"{ask}{label}{o}   Approves: `PUT /session/approval-classes` (`noteViewed`)"
            f"\n{done}", "does not approve"),
    }  # fmt: skip
    global ROOT
    saved_root = ROOT
    for label_, (text, expected) in decision_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "docs" / "decisions"
            folder.mkdir(parents=True)
            body = (
                f"# SYNTHETIC\n\n1. **Case.**\n{text}2. **Next.**\n{ask}{label}{o}{a}"
            )
            (folder / "2026-10-21-owner-decisions.md").write_text(body)
            m = copy.deepcopy(pc.AMENDMENTS)
            listed_mutation("2026-10-21#1")(m)
            ROOT = Path(tmp)
            try:
                found = [
                    x
                    for x in guarded(run_checks, api, schemas, examples, m)
                    if expected in x and "(noteViewed)" in x  # not a real entry
                ]
            finally:
                ROOT = saved_root
        print(f"self-test {label_}: {'detected' if found else 'MISSED'}")
        failures += not found

    # Exemption text: createSession's exemption cites section 99, appended to a copy
    # of the real decisions file (whose decision 24 still approves the entries).
    real = (saved_root / "docs/decisions/2026-10-21-owner-decisions.md").read_text()
    exempts = "   Exempts: `unauthenticated_login` (`createSession`)\n"
    exempt_cases = {
        "Exempts line naming another operation": (
            f"{ask}{label}{o}{exempts.replace('createSession', 'revokeSession')}{done}",
            "does not exempt"),
        "Exempts line naming another exemption": (
            f"{ask}{label}{o}{exempts.replace('unauthenticated_login', 'no_content')}"
            f"{done}", "does not exempt"),
        "Exempts inside option text": (
            f'{ask}{label}{opt}"Yes.\n{exempts}   Done."\n{done}', "does not exempt"),
        "Exempts after Implemented by": (
            f"{ask}{label}{o}{done}{exempts}", "does not exempt"),
        "Exempts with two Selected labels": (
            f"{ask}{label}{label}{o}{exempts}{done}", "no single Selected label"),
    }  # fmt: skip
    for label_, (text, expected) in exempt_cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "docs" / "decisions"
            folder.mkdir(parents=True)
            body = f"{real}\n## SYNTHETIC\n\n99. **Case.**\n{text}"
            (folder / "2026-10-21-owner-decisions.md").write_text(body)
            m = copy.deepcopy(pc.AMENDMENTS)
            (entry,) = [
                e for e in m["mutations"] if e["operationId"] == "createSession"
            ]
            entry["exemptions"] = [login_x | {"decision": "2026-10-21#99"}]
            ROOT = Path(tmp)
            try:
                found = [
                    x
                    for x in guarded(run_checks, api, schemas, examples, m)
                    if expected in x and "(createSession)" in x
                ]
            finally:
                ROOT = saved_root
        print(f"self-test {label_}: {'detected' if found else 'MISSED'}")
        failures += not found

    def crash() -> list[str]:
        raise TypeError("unhashable")

    wrapped = guarded(crash) == ["checker crashed on malformed amendment: TypeError"]
    print(f"self-test crash reported: {'detected' if wrapped else 'MISSED'}")
    failures += not wrapped
    total = len(cases) + len(amended_cases) + len(api_cases) + 1
    total += len(decision_cases) + len(exempt_cases)
    print(f"self-test: {total - failures}/{total} defects detected")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    api, schemas, examples = load()
    if "--self-test" in argv:
        return self_test(api, schemas, examples)
    errors = guarded(run_checks, api, schemas, examples)
    for err in errors:
        print("FAIL", err)
    for entry in pc.AMENDMENTS.get("sensitive_fields", []):
        print(f"INFO sensitive field allowed: {entry['schema']}#{entry['field']}:",
              entry["decision"])  # fmt: skip
    for key, change in pc.AMENDMENTS.get("security_schemes", {}).items():
        print(f"INFO security scheme amended: {key} name {change['name']}")
    for entry in pc.AMENDMENTS.get("mutations", []):
        print(f"INFO mutation allowed: {entry['method'].upper()} {entry['path']} "
              f"({entry['operationId']}): {entry['decision']}")  # fmt: skip
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
        f"operations, {len(schemas)} schemas, {len(examples)} examples, project "
        f"contract {pc.AMENDMENTS['version']} ({len(pc.AMENDMENTS['schemas'])} "
        f"amended schemas, {len(pc.AMENDMENTS.get('new_schemas', {}))} new schemas, "
        f"{sum(map(len, pc.AMENDMENTS.get('operations', {}).values()))} new "
        "operations)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
