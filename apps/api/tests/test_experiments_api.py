"""`/api/v1/experiments` (listExperiments, readExperiment; T044 increment 2) through
TestClient on a migrated PostgreSQL 16 database as qw_app (T010 harness), with
experiments recorded by qw_worker through `experiment_store.record` (which runs
`improvement.record`) over SYNTHETIC fixtures. Responses are validated against the
contract `experiment` and `experimentPage` (project contract coverage)."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import experiment_store as es
from qw_domain import improvement as im

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
rd = _module("qw_reads_api_tests", ROOT / "apps/api/tests/test_reads_api.py")
STORE_TESTS = ROOT / "packages/adapters/tests/test_experiment_store.py"
st = _module("qw_estore_tests", STORE_TESTS)
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
URL = "/api/v1/experiments"
EXPERIMENT = Draft202012Validator(rd.PC.schema("experiment"), registry=rd.REGISTRY,
                                  format_checker=FormatChecker())  # fmt: skip
SYN, OPT = "evidence:synthetic_only", im.Target.RECURSIVE_OPTIMIZER


def put(urls: dict[str, str], t: uuid.UUID, eid: str, target: im.Target,
        stage: str = "proposed") -> None:  # fmt: skip
    """Record `eid` through the store as far as `stage`."""
    reg = st.reg_for(t, eid, target, f"bud-{eid}")
    with psycopg.connect(urls["qw_worker"], autocommit=True) as c:
        st.put(c, t, reg, st.empty(t), eid=eid)
        if stage != "proposed":
            st.put(c, t, reg, st.booked(reg, eid), im.Scheduled(eid, f"bud-{eid}"),
                   eid=eid)  # fmt: skip
        if stage == "decided":
            st.put(c, t, reg, st.booked(reg, eid), None, st.run(t), eid=eid)


def valid(item: dict[str, Any]) -> dict[str, Any]:
    assert [e.message for e in EXPERIMENT.iter_errors(item)] == []
    assert item["requires_human_promotion"] is True
    assert item["evaluation_receipt_ids"] == [] and item["release_receipt_id"] is None
    return item


def tenant_of(w: Any, login_name: str) -> uuid.UUID:
    row = w.db.execute("SELECT tenant_id FROM app.membership WHERE user_id = %s",
                       (w.users[login_name],)).fetchone()  # fmt: skip
    return uuid.UUID(str(row[0]))


@pytest.mark.db
def test_reads_serve_the_contract_and_name_every_reason(
    world: Any, runtime_urls: dict[str, str]
) -> None:
    a = world.tenant_a
    put(runtime_urls, a, st.EXP, im.Target.STRATEGY, "decided")
    put(runtime_urls, a, "exp-a", im.Target.STRATEGY, "authorized")
    put(runtime_urls, a, "exp-code", OPT)
    login(world.client, "bob")
    page = world.client.get(URL).json()
    rd.page_conforms("experiment", page)
    [code] = [valid(i) for i in page["items"]]  # the others are withheld
    assert (code["id"], code["state"], code["target"]) == (
        "exp-code",
        "proposed",
        "recursive_optimizer",
    )
    assert code["parent_optimizer_version"] == code["baseline_version"] == "opt-v0"
    assert code["scope_id"] == str(a) and code["sandbox_profile_id"] == "sbx-synth"
    assert (
        code["protocol_id"]
        == st.reg_for(a, "exp-code", OPT, "bud-exp-code")
        .protocol("exp-code")
        .content_hash
    )
    e = st.EXP
    assert page["coverage"] == {
        "status": "partial",
        "reasons": [
            "evidence:exp-a/synthetic_only",
            "unavailable:exp-a/parent_optimizer_version",
            "withheld:exp-a/item",
            "blocked:exp-code/sandbox_unqualified",
            "evidence:exp-code/synthetic_only",
            f"evidence:{e}/synthetic_only",
            f"none:{e}/evaluation_receipt_ids",
            f"unavailable:{e}/parent_optimizer_version",
            f"withheld:{e}/item",
        ],
    }
    assert all(len(r) <= 300 for r in page["coverage"]["reasons"])
    for eid, why in (("exp-code", "blocked:sandbox_unqualified"), (e, SYN),
                     ("exp-a", "unavailable:parent_optimizer_version")):  # fmt: skip
        body = assert_problem(world.client.get(f"{URL}/{eid}"), 409,
                              "coverage_required")  # fmt: skip
        assert why in body["detail"]
    assert_problem(world.client.get(f"{URL}/exp-none"), 404, "not_found")
    assert world.client.get(f"{URL}/bad%20id").status_code == 400
    for method, path in (("POST", URL), ("POST", f"{URL}/exp-a/promote"),
                         ("POST", f"{URL}/exp-a/cancel"), ("DELETE", f"{URL}/exp-a"),
                         ("PATCH", f"{URL}/exp-a")):  # fmt: skip
        assert world.client.request(method, path).status_code in (403, 404, 405)
    count = world.db.execute("SELECT count(*) FROM app.experiment_revision")
    assert count.fetchone() == (6,)  # nothing written by the API


@pytest.mark.db
def test_tenancy_and_cursors(world: Any, runtime_urls: dict[str, str]) -> None:
    for eid, target in (("exp-0", OPT), ("exp-1", im.Target.STRATEGY),
                        ("exp-2", OPT)):  # fmt: skip
        put(runtime_urls, world.tenant_a, eid, target)
    login(world.client, "alice")
    first = world.client.get(URL, params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == ["exp-0"]  # exp-1 withheld
    assert "withheld:exp-1/item" in first["coverage"]["reasons"]
    rest = world.client.get(URL, params={"limit": 2, "cursor": first["next_cursor"]})
    rd.page_conforms("experiment", rest.json())
    assert [valid(i)["id"] for i in rest.json()["items"]] == ["exp-2"]
    assert rest.json()["next_cursor"] is None
    cursor = first["next_cursor"]
    for bad_url, bad in (("/api/v1/watchlists", cursor), (URL, "x"),
                         (URL, cursor[:-2] + "AA")):  # fmt: skip
        assert_problem(world.client.get(bad_url, params={"cursor": bad}), 400,
                       "invalid_cursor")  # fmt: skip
    login(world.client, "bob")  # same tenant, another principal
    assert_problem(world.client.get(URL, params={"cursor": cursor}), 400,
                   "invalid_cursor")  # fmt: skip
    put(runtime_urls, tenant_of(world, "carol"), "exp-b", OPT)
    login(world.client, "carol")  # tenant B: foreign ids are the unknown 404
    assert [i["id"] for i in world.client.get(URL).json()["items"]] == ["exp-b"]
    for eid in ("exp-0", "exp-9"):
        assert_problem(world.client.get(f"{URL}/{eid}"), 404, "not_found")
    login(world.client, "alice")
    assert_problem(world.client.get(f"{URL}/exp-b"), 404, "not_found")


@pytest.mark.db
def test_cursor_walks_past_pages_of_withheld_rows(
    world: Any, runtime_urls: dict[str, str]
) -> None:
    ids = ["exp-w0", "exp-w1", "exp-w2"]
    for eid in ids:
        put(runtime_urls, world.tenant_a, eid, im.Target.STRATEGY)
    login(world.client, "alice")
    seen, cursor = [], None
    for _ in range(len(ids) + 1):  # terminates: one page per row
        response = world.client.get(URL, params={"limit": 1, **(
            {"cursor": cursor} if cursor else {})})  # fmt: skip
        assert response.status_code == 200, response.text
        page = response.json()
        rd.page_conforms("experiment", page)
        assert page["items"] == []
        own = [r for r in page["coverage"]["reasons"] if r.startswith("withheld:")]
        assert len(own) == 1 and all(
            r.split(":", 1)[1].startswith(own[0][9:-5] + "/")
            for r in page["coverage"]["reasons"])  # fmt: skip
        seen.append(own[0].removeprefix("withheld:").removesuffix("/item"))
        if (cursor := page["next_cursor"]) is None:
            break
    assert seen == ids and cursor is None


@pytest.mark.db
def test_tampered_rows_are_409_and_database_errors_500(
    world: Any, runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put(runtime_urls, world.tenant_a, "exp-code", OPT)
    login(world.client, "alice")
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.experiment_revision SET hypothesis = "
                         "'SYNTHETIC forged'")  # fmt: skip
    for response in (world.client.get(URL), world.client.get(f"{URL}/exp-code")):
        body = assert_problem(response, 409, "integrity")
        assert "forged" not in json.dumps(body)

    def broken(*_: Any) -> None:
        raise es.ExperimentStoreError("database", "OperationalError")

    for name, path in (("page", URL), ("load", f"{URL}/exp-code")):
        monkeypatch.setattr(es, name, broken)  # a database failure is never a 409
        assert_problem(world.client.get(path), 500, "internal_error")
