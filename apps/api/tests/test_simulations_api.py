"""Simulation reads (T041 increments 3 and 4): `GET /simulations`,
`/simulations/{id}/fills` and `/simulations/{id}/results` through TestClient on a
migrated PostgreSQL 16 database as the non-superuser qw_app login, over
qw_adapters.simulation_store. Reuses the T010 API harness and the SYNTHETIC
simulation of test_simulation_store (its dates shifted to the database clock).
Bodies are validated strictly (date-time formats enforced) against the contract
item schemas inside the project contract C-02 page."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_domain.decimals import Money
from qw_domain.simulation import VirtualPortfolio
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]
SPEC = ROOT / "docs/spec/contracts"


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
st = _module("qw_sstore_tests",
             ROOT / "packages/adapters/tests/test_simulation_store.py")  # fmt: skip
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem, csrf = api.login, api.assert_problem, api.csrf
OPENAPI = yaml.safe_load((SPEC / "openapi.yaml").read_text())
COMMON = json.loads((SPEC / "schemas/common.schema.json").read_text())
REGISTRY = Registry().with_resource(
    "common.schema.json", Resource.from_contents(COMMON)
)


@pytest.fixture(autouse=True)
def _db_clock(conn: Any) -> None:
    st.SHIFT = st.shift_for(conn)


def conforms(page_schema: dict[str, Any], body: dict[str, Any]) -> None:
    v = Draft202012Validator(page_schema, registry=REGISTRY,
                             format_checker=FormatChecker())  # fmt: skip
    assert [(list(e.absolute_path), e.message) for e in v.iter_errors(body)] == []


def summary_page() -> dict[str, Any]:
    page = PC.page("calculation")  # the C-02 page shape, then the inline item
    page["properties"]["items"]["items"] = OPENAPI["components"]["schemas"][
        "SimulationSummary"]  # fmt: skip
    return cast(dict[str, Any], page)


def tenant_of(w: Any, user: str) -> uuid.UUID:
    row = w.db.execute("SELECT tenant_id FROM app.membership WHERE user_id = %s",
                       (w.users[user],)).fetchone()  # fmt: skip
    assert row is not None
    return uuid.UUID(str(row[0]))


def second(sid: str = "SIM-2") -> ss.Simulation:
    p = VirtualPortfolio.open(sid, Money.of("500", "USD"), st.at(13, 0))
    return ss.Simulation("SYNTHETIC two", "exec-synth-1", "bench-synth-1", p)


def put(app_conn: Any, tenant: uuid.UUID, s: ss.Simulation) -> None:
    with tn.tenant_transaction(app_conn, tenant) as tx:
        ss.save(tx, s)


def rows(w: Any) -> list[Any]:
    return [w.db.execute(f"SELECT count(*) FROM app.{t}").fetchone()
            for t in (*ss.TABLES, "calculation_record")]  # fmt: skip


@pytest.mark.db
def test_simulations_are_listed_tenant_scoped_and_marked_simulated(
    world: Any, app_conn: Any
) -> None:
    login(world.client, "bob")  # a member may read
    empty = world.client.get("/api/v1/simulations")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "next_cursor": None, "coverage": {
        "status": "partial", "reasons": ["simulated:separate_from_actual"]},
    }  # fmt: skip
    put(app_conn, world.tenant_a, st.ran())
    put(app_conn, world.tenant_a, second())
    put(app_conn, tenant_of(world, "carol"), second("SIM-B"))
    before = rows(world)
    first = world.client.get("/api/v1/simulations?limit=1").json()
    conforms(summary_page(), first)
    assert first["items"] == [{
        "id": "SIM-1", "name": "SYNTHETIC sim", "execution_model_id": "exec-synth-1",
        "benchmark_policy_id": "bench-synth-1", "mode": "manual",
        "separate_from_actual": True}]  # fmt: skip
    assert first["coverage"]["reasons"] == ["simulated:separate_from_actual",
        "unverified:execution_model_id", "unverified:benchmark_policy_id"]  # fmt: skip
    nxt = world.client.get(f"/api/v1/simulations?limit=1&cursor={first['next_cursor']}")
    assert [i["id"] for i in nxt.json()["items"]] == ["SIM-2"]
    assert nxt.json()["next_cursor"] is None
    cursor = first["next_cursor"]
    forged = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
    for bad in ("x" * 30, forged):
        assert_problem(world.client.get(f"/api/v1/simulations?cursor={bad}"), 400,
                       "invalid_cursor")  # fmt: skip
    login(world.client, "carol")  # tenant B
    assert [i["id"] for i in world.client.get("/api/v1/simulations").json()[
        "items"]] == ["SIM-B"]  # fmt: skip
    assert rows(world) == before  # reads write nothing


@pytest.mark.db
def test_results_are_simulated_and_honest(world: Any, app_conn: Any) -> None:
    put(app_conn, world.tenant_a, st.ran())
    put(app_conn, tenant_of(world, "carol"), second("SIM-B"))
    login(world.client, "alice")
    before = rows(world)
    results = world.client.get("/api/v1/simulations/SIM-1/results")
    body = results.json()
    conforms(PC.page("calculation"), body)
    assert body["next_cursor"] is None and body["coverage"] == {
        "status": "partial", "reasons": [
            "simulated:separate_from_actual", "unverified:execution_model_id",
            "unavailable:market_value", "unavailable:total_return",
            "unavailable:benchmark_comparison", "unavailable:scorecard"]}  # fmt: skip
    items = body["items"]
    assert [i["id"] for i in items] == sorted(i["id"] for i in items)
    by = {i["formula_id"]: i for i in items}
    assert len(items) == len(by) == 9
    assert {k: (i["result"], i["unit"], i["status"]) for k, i in by.items()} == {
        "virtual_ending_cash": ("99018", "USD", "ok"),
        "virtual_realized_net": ("0", "USD", "ok"),
        "virtual_realized_gain_before_fees": ("0", "USD", "ok"),
        "virtual_fees": ("2", "USD", "ok"),
        "virtual_income": ("25", "USD", "ok"),
        "virtual_position_units": ("100", "units", "ok"),
        "virtual_market_value": (None, "USD", "insufficient_inputs"),
        "virtual_total_return": (None, "USD", "insufficient_inputs"),
        "virtual_benchmark_comparison": (None, "USD", "insufficient_inputs"),
    }  # fmt: skip
    digest = ss.digest(st.ran())
    for i in items:
        assert i["warnings"][0] == "simulated:separate_from_actual"
        assert {"ref:simulation=SIM-1", f"ref:simulation_digest={digest}"} <= set(
            i["warnings"])  # fmt: skip
        assert world.client.get(f"/api/v1/calculations/{i['id']}").json() == i
    assert "unavailable:missing_market_inputs" in by["virtual_market_value"][
        "warnings"]  # fmt: skip
    seen, cursor = [], ""
    while cursor is not None:  # pages of 3 cover every record once
        got = world.client.get(f"/api/v1/simulations/SIM-1/results?limit=3"
                               f"&cursor={cursor}").json()  # fmt: skip
        seen += got["items"]
        cursor = got["next_cursor"]
    assert seen == items
    assert rows(world) == before  # results are read, never written, on a GET
    world.db.execute("SET session_replication_role = replica")  # pre-increment-4
    world.db.execute("DELETE FROM app.calculation_record")
    none = world.client.get("/api/v1/simulations/SIM-1/results").json()
    conforms(PC.page("calculation"), none)
    assert none["items"] == [] and none["coverage"]["reasons"] == [
        "simulated:separate_from_actual",
        "unavailable:results_not_recorded"]  # fmt: skip
    world.db.execute("SET session_replication_role = origin")
    foreign = world.client.get("/api/v1/simulations/SIM-B/results")
    unknown = world.client.get("/api/v1/simulations/SIM-9/results")
    same = {"correlation_id": ""}
    assert {**assert_problem(foreign, 404, "not_found"), **same} == {
        **assert_problem(unknown, 404, "not_found"), **same}  # fmt: skip
    assert_problem(world.client.get("/api/v1/simulations/-bad/results"), 400,
                   "invalid_request")  # fmt: skip
    before = rows(world)
    # No mutating simulation route exists in this increment, and none reaches a broker.
    create = world.client.post(
        "/api/v1/simulations", json={}, headers=csrf(world.client)
    )
    assert create.status_code == 405, create.text
    order = world.client.post("/api/v1/simulations/SIM-1/orders", json={},
                              headers=csrf(world.client))  # fmt: skip
    assert order.status_code == 404, order.text
    assert rows(world) == before
    world.client.cookies.clear()
    assert_problem(world.client.get("/api/v1/simulations"), 401, "unauthenticated")


@pytest.mark.db
def test_a_tampered_simulation_is_a_crash_not_an_answer(
    world: Any, app_conn: Any
) -> None:
    put(app_conn, world.tenant_a, st.ran())
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.simulation_fill SET price = 9.05")
    login(world.client, "alice")
    for path in ("", "/SIM-1/results", "/SIM-1/fills"):
        got = world.client.get(f"/api/v1/simulations{path}")
        assert_problem(got, 500, "internal_error")
        assert "9.05" not in got.text


@pytest.mark.db
def test_fills_are_hypothetical_and_honestly_marked(world: Any, app_conn: Any) -> None:
    put(app_conn, world.tenant_a, st.ran())
    put(app_conn, world.tenant_a, second())
    put(app_conn, tenant_of(world, "carol"), st.ran())
    login(world.client, "bob")
    before = rows(world)
    got = world.client.get("/api/v1/simulations/SIM-1/fills?limit=1")
    assert got.status_code == 200, got.text
    conforms(PC.page("simulated_fill"), got.json())
    (f,) = st.ran().fills()
    assert got.json() == {"items": [{
        "id": "o1:fill:1", "simulated_order_id": "o1",
        "filled_at": f.filled_at.isoformat().replace("+00:00", "Z"),
        "quantity": "100", "price": {"amount": "10.05", "currency": "USD"},
        "fees": {"amount": "2", "currency": "USD"}, "data_record_ids": [],
        "assumption_ids": [], "execution_model_id": "exec-synth-1",
        "actual_execution": False}], "next_cursor": None, "coverage": {
        "status": "partial", "reasons": ["simulated:separate_from_actual",
        "unverified:execution_model_id", "unavailable:data_record_ids"]}}  # fmt: skip
    empty = world.client.get("/api/v1/simulations/SIM-2/fills").json()
    marked = {"status": "partial", "reasons": ["simulated:separate_from_actual"]}
    assert empty == {"items": [], "next_cursor": None, "coverage": marked}
    login(world.client, "carol")  # tenant B's own SIM-1, never tenant A's SIM-2
    assert len(world.client.get("/api/v1/simulations/SIM-1/fills").json()[
        "items"]) == 1  # fmt: skip
    foreign = world.client.get("/api/v1/simulations/SIM-2/fills")
    unknown = world.client.get("/api/v1/simulations/SIM-9/fills")
    same = {"correlation_id": ""}
    assert {**assert_problem(foreign, 404, "not_found"), **same} == {
        **assert_problem(unknown, 404, "not_found"), **same}  # fmt: skip
    assert_problem(world.client.get("/api/v1/simulations/SIM-1/fills?cursor=" +
                                    "x" * 30), 400, "invalid_cursor")  # fmt: skip
    assert rows(world) == before
