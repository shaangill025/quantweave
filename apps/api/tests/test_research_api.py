"""`GET /calculations/{calculation_id}` (readCalculation) for the T054 research views,
through TestClient on a migrated PostgreSQL 16 database as the non-superuser qw_app
login, over qw_adapters.calculation_store. Reuses the T010 API harness. All tenants,
accounts and figures are SYNTHETIC. Hand-computed oracle: a simple return from 100
to 110 CAD is (110 - 100) / 100 = 0.1. Bodies are validated strictly against the
spec calculation schema, with date-time formats enforced. No amendment is needed:
the schema's fields are all filled, and what it has no field for (as-of, rounding,
coverage, date and reference inputs, input as-of times, the unavailable code) is
named in `warnings`, which the tests assert."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import calculation_store as cs
from qw_adapters import tenancy as tn

ROOT = Path(__file__).parents[3]
SCHEMA = json.loads(
    (ROOT / "docs/spec/contracts/schemas/calculation.schema.json").read_text()
)


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
store = _module("qw_cstore_tests",
                ROOT / "packages/adapters/tests/test_calculation_store.py")  # fmt: skip
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem


def conforms(body: dict[str, Any]) -> None:
    v = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    assert [(list(e.absolute_path), e.message) for e in v.iter_errors(body)] == []


def tenant_of(w: Any, user: str) -> uuid.UUID:
    row = w.db.execute(
        "SELECT tenant_id FROM app.membership WHERE user_id = %s", (w.users[user],)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def put(app_conn: Any, tenant: uuid.UUID, rec: Any) -> str:
    with tn.tenant_transaction(app_conn, tenant) as tx:
        return cs.record(tx, rec)


@pytest.mark.db
def test_calculation_is_served_tenant_scoped_and_conformant(
    world: Any, app_conn: Any
) -> None:
    ret, gap = store.simple_return(), store.gap()
    ret_id = put(app_conn, world.tenant_a, ret)
    gap_id = put(app_conn, world.tenant_a, gap)
    b_only = put(app_conn, tenant_of(world, "carol"),
                 replace(ret, formula_version="2"))  # fmt: skip
    rows = world.db.execute("SELECT count(*) FROM app.calculation_record").fetchone()
    login(world.client, "bob")  # a member may read
    got = world.client.get(f"/api/v1/calculations/{ret_id}")
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/json"
    body = got.json()
    conforms(body)
    assert body == ret.to_contract()
    assert (body["id"], body["result"], body["status"]) == (ret_id, "0.1", "ok")
    assert body["computed_at"] == "2026-10-01T14:00:00Z"
    assert body["inputs"][:2] == [
        {"name": "start_value", "value": "100", "unit": "CAD",
         "source_record_id": "acct-syn-1"},
        {"name": "end_value", "value": "110", "unit": "CAD",
         "source_record_id": "acct-syn-1"}]  # fmt: skip
    # What the schema cannot hold is named in warnings, never dropped silently.
    assert {
        "coverage:complete",
        "rounding:half_even:1e-18",
        "ref:series=syn-series@2",
        "input_as_of:end_value=2026-10-01T13:00:00Z",
    } <= set(body["warnings"])
    unavailable = world.client.get(f"/api/v1/calculations/{gap_id}").json()
    conforms(unavailable)
    assert (unavailable["result"], unavailable["status"]) == (None, "undefined")
    assert "unavailable:no_sign_change" in unavailable["warnings"]

    foreign = world.client.get(f"/api/v1/calculations/{b_only}")
    unknown = world.client.get(f"/api/v1/calculations/calc:{'0' * 64}")
    assert {**assert_problem(foreign, 404, "not_found"), "correlation_id": ""} == {
        **assert_problem(unknown, 404, "not_found"), "correlation_id": ""}  # fmt: skip
    assert_problem(world.client.get("/api/v1/calculations/-bad"), 400,
                   "invalid_request")  # fmt: skip
    login(world.client, "carol")  # tenant B: A's ids are unknown there
    assert_problem(world.client.get(f"/api/v1/calculations/{gap_id}"), 404,
                   "not_found")  # fmt: skip
    assert world.client.get(f"/api/v1/calculations/{b_only}").status_code == 200
    world.client.cookies.clear()
    assert_problem(world.client.get(f"/api/v1/calculations/{ret_id}"), 401,
                   "unauthenticated")  # fmt: skip
    after = world.db.execute("SELECT count(*) FROM app.calculation_record").fetchone()
    assert after == rows  # reads write nothing


@pytest.mark.db
def test_a_tampered_record_is_a_crash_not_an_answer(world: Any, app_conn: Any) -> None:
    calc_id = put(app_conn, world.tenant_a, store.simple_return())
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.calculation_record SET record = jsonb_set(record, "
                     "'{result}', '\"0.2\"')")  # fmt: skip
    login(world.client, "alice")
    got = world.client.get(f"/api/v1/calculations/{calc_id}")
    body = assert_problem(got, 500, "internal_error")
    assert "0.2" not in got.text and body["detail"]
