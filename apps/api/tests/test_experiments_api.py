"""`/api/v1/experiments` (listExperiments, readExperiment; T044 increment 2) through
TestClient on a migrated PostgreSQL 16 database as qw_app (T010 harness), with
experiments recorded through `experiment_store.record` (which runs
`improvement.record`; receipts as qw_assessor, revisions as qw_worker) over
SYNTHETIC fixtures. Responses are validated against the contract `experiment` and
`experimentPage` (project contract 0.6.0: item coverage, nullable
`parent_optimizer_version`, blocked and insufficient_evidence)."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import experiment_store as es
from qw_api import experiments as ex
from qw_api.app import ApiError
from qw_domain import improvement as im
from test_optimizer_improvement import (
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
)
from test_optimizer_lineage import lineage_receipt, optimizer_record

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
AST = _module("qw_assessor_store_tests",
              ROOT / "packages/adapters/tests/test_assessor_store.py")  # fmt: skip


def put(urls: dict[str, str], t: uuid.UUID, eid: str, target: im.Target,
        stage: str = "proposed") -> None:  # fmt: skip
    """Record `eid` through the store as far as `stage`."""
    reg = st.reg_for(t, eid, target, f"bud-{eid}")
    with (
        psycopg.connect(urls["qw_worker"], autocommit=True) as w,
        psycopg.connect(urls["qw_assessor"], autocommit=True) as a,
    ):
        c = (w, a)
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
    a, e = world.tenant_a, st.EXP
    put(runtime_urls, a, e, im.Target.STRATEGY, "decided")
    put(runtime_urls, a, "exp-a", im.Target.STRATEGY, "authorized")
    put(runtime_urls, a, "exp-code", OPT)
    login(world.client, "bob")
    page = world.client.get(URL).json()
    rd.page_conforms("experiment", page)
    items = {i["id"]: valid(i) for i in page["items"]}  # 0.6.0: none withheld
    assert list(items) == ["exp-a", "exp-code", e]
    code = items["exp-code"]
    assert (code["state"], code["target"]) == ("blocked", "recursive_optimizer")
    assert code["parent_optimizer_version"] == code["baseline_version"] == "opt-v0"
    assert code["scope_id"] == str(a) and code["sandbox_profile_id"] == "sbx-synth"
    reg = st.reg_for(a, "exp-code", OPT, "bud-exp-code")
    assert code["protocol_id"] == reg.protocol("exp-code").content_hash
    assert [items[k]["state"] for k in ("exp-a", e)] == [
        "authorized",
        "awaiting_review",
    ]
    own = {  # 0.11.0: no lineage link is named none:retest_of
        "exp-a": [SYN, "none:parent_optimizer_version", "none:retest_of"],
        "exp-code": ["blocked:sandbox_unqualified", SYN, "none:retest_of"],
        e: [
            SYN,
            "none:evaluation_receipt_ids",
            "none:parent_optimizer_version",
            "none:retest_of",
        ],
    }
    for k, why in own.items():
        assert items[k]["coverage"] == {"status": "partial", "reasons": why}
        assert rd.strict("experiment", items[k], why) == (
            set() if k == "exp-code" else {"parent_optimizer_version"})  # fmt: skip
    assert page["coverage"] == {"status": "partial", "reasons": [
        f"{r.split(':', 1)[0]}:{k}/{r.split(':', 1)[1]}"
        for k in items for r in own[k]]}  # fmt: skip
    assert all(len(r) <= 300 for r in page["coverage"]["reasons"])
    for eid, item in items.items():  # readExperiment: 200 with the item coverage
        response = world.client.get(f"{URL}/{eid}")
        assert response.status_code == 200, response.text
        assert valid(response.json()) == item
    assert_problem(world.client.get(f"{URL}/exp-none"), 404, "not_found")
    assert world.client.get(f"{URL}/bad%20id").status_code == 400
    for method, path in (("POST", URL), ("POST", f"{URL}/exp-a/promote"),
                         ("POST", f"{URL}/exp-a/cancel"), ("DELETE", f"{URL}/exp-a"),
                         ("PATCH", f"{URL}/exp-a")):  # fmt: skip
        assert world.client.request(method, path).status_code in (403, 404, 405)
    count = world.db.execute("SELECT count(*) FROM app.experiment_revision")
    assert count.fetchone() == (6,)  # nothing written by the API


def test_amended_experiment_schema() -> None:
    """0.6.0: coverage required, the new states, and a parent version that is a
    string exactly for the optimizer; 0.5.0 records still conform as stored."""
    t = uuid.uuid4()
    reg = st.reg_for(t)
    rec = im.record(reg, st.EXP, "cand-1", st.plan(), st.empty(t))
    item = ex.wire(rec)

    def errors(body: dict[str, Any]) -> list[str]:
        return [e.message for e in EXPERIMENT.iter_errors(body)]

    assert errors(item) == [] and item["parent_optimizer_version"] is None
    for state in ("blocked", "insufficient_evidence", "failed"):
        assert errors({**item, "state": state}) == []
    opt = {**item, "target": "recursive_optimizer"}
    assert errors(opt) != [] and errors({**opt, "parent_optimizer_version": "v0"}) == []
    assert errors({**item, "parent_optimizer_version": "v0"}) != []
    assert errors({k: v for k, v in item.items() if k != "coverage"}) != []
    assert errors({**item, "state": "installed"}) != []
    assert item["retest_of"] is None and "none:retest_of" in item["coverage"]["reasons"]
    assert errors({**item, "retest_of": "exp-0"}) == []  # 0.11.0: a nullable id
    for bad in ("bad id", "", 7):
        assert errors({**item, "retest_of": bad}) != [], bad
    assert errors({k: v for k, v in item.items() if k != "retest_of"}) != []
    old = st.legacy(t)[2]  # stored under 0.5.0: failed, unavailable parent
    assert errors(ex.wire(old)) == []
    assert rd.strict("experiment", ex.wire(old), list(old.reasons)) == {
        "parent_optimizer_version"}  # fmt: skip


@pytest.mark.db
def test_tenancy_and_cursors(world: Any, runtime_urls: dict[str, str]) -> None:
    for eid, target in (("exp-0", OPT), ("exp-1", im.Target.STRATEGY),
                        ("exp-2", OPT)):  # fmt: skip
        put(runtime_urls, world.tenant_a, eid, target)
    login(world.client, "alice")
    first = world.client.get(URL, params={"limit": 2}).json()
    assert [i["id"] for i in first["items"]] == ["exp-0", "exp-1"]
    assert first["items"][1]["parent_optimizer_version"] is None
    rest = world.client.get(URL, params={"limit": 2, "cursor": first["next_cursor"]})
    rd.page_conforms("experiment", rest.json())
    assert [valid(i)["id"] for i in rest.json()["items"]] == ["exp-2"]
    assert rest.json()["next_cursor"] is None
    cursor = first["next_cursor"]
    for bad_url, bad in (("/api/v1/watchlists", cursor), (URL, "x"),
                         (URL, cursor[:-2] + ("AA" if cursor[-2:] != "AA"
                                              else "BB"))):  # fmt: skip
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
def test_cursor_walks_one_experiment_per_page(
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
        [item] = page["items"]
        assert all(r.split(":", 1)[1].startswith(f"{item['id']}/")
                   for r in page["coverage"]["reasons"])  # fmt: skip
        seen.append(valid(item)["id"])
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


@pytest.mark.db
def test_contract_0110_serves_the_lineage_link(
    world: Any, runtime_urls: dict[str, str]
) -> None:
    """Owner decision 19: a retest names the experiment it re-tests, from the
    revision's copy of its frozen protocol's link; otherwise null and named."""
    a, e = world.tenant_a, st.EXP
    put(runtime_urls, a, e, im.Target.STRATEGY, "authorized")
    with (
        psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as w,
        psycopg.connect(runtime_urls["qw_assessor"], autocommit=True) as s,
    ):
        AST.put_on((w, s), a, "exp-r", AST.other_plan("plan-e", AST.EARLY),
                   retest_of=e)  # fmt: skip
    login(world.client, "bob")
    page = world.client.get(URL).json()
    rd.page_conforms("experiment", page)
    items = {i["id"]: valid(i) for i in page["items"]}
    assert {k: i["retest_of"] for k, i in items.items()} == {e: None, "exp-r": e}
    assert "none:retest_of" in items[e]["coverage"]["reasons"]
    assert "none:retest_of" not in items["exp-r"]["coverage"]["reasons"]
    assert f"none:{e}/retest_of" in page["coverage"]["reasons"]
    assert not [r for r in page["coverage"]["reasons"] if "exp-r/retest_of" in r]
    got = world.client.get(f"{URL}/exp-r")
    assert got.status_code == 200 and valid(got.json()) == items["exp-r"]
    login(world.client, "carol")  # another tenant sees neither end of the link
    assert world.client.get(URL).json()["items"] == []
    assert_problem(world.client.get(f"{URL}/exp-r"), 404, "not_found")


@pytest.mark.usefixtures("gate_lifted_install")
def test_next_cycle_run_id_is_served_from_bound_lineage() -> None:
    """T050 increment 3, SYNTHETIC and GATE-LIFTED: given the experiment's lineage
    receipt the item names the cycle the installed V1 ran next, and conforms as
    `experiment_recursive.valid.json` does; the self-promotion shape of
    `experiment_self_promotion.invalid.json` is refused. No route has a lineage
    yet (decision 87's installation store), so served items stay null."""
    reg, _, _, lin = lineage_receipt()
    rec = optimizer_record(reg)
    item = ex.wire(rec, lin)
    assert item["next_cycle_run_id"] == "cycle-v1-1" == lin.claim.next_cycle.cycle_id
    assert item["release_receipt_id"] is None  # T051 release receipts: not stored
    assert valid(item)["target"] == "recursive_optimizer"
    assert ex.wire(rec)["next_cycle_run_id"] is None  # no lineage: never invented
    with pytest.raises(ApiError) as refused:  # another experiment's lineage
        ex.wire(replace(rec, experiment_id="exp-opt-2"), lin)
    assert (refused.value.status, refused.value.code) == (409, "integrity")
    spec = ROOT / "docs/spec/contracts"
    base = json.loads((spec / "schemas/experiment.schema.json").read_text())
    check = Draft202012Validator(base, format_checker=FormatChecker())
    manifest = json.loads((spec / "examples/manifest.json").read_text())["examples"]
    shown = {e["path"]: e["expected_valid"] for e in manifest
             if e["schema"] == "experiment"}  # fmt: skip
    assert shown == {"experiment_recursive.valid.json": True,
                     "experiment_self_promotion.invalid.json": False}  # fmt: skip
    core = {k: item[k] for k in base["properties"]}
    assert set(core) == set(base["required"])
    examples = {p: json.loads((spec / "examples" / p).read_text()) for p in shown}
    for path, ok in shown.items():
        assert check.is_valid(examples[path]) is ok, path

    def errs(body: dict[str, Any]) -> set[str]:
        return {"/".join(map(str, e.absolute_path)) for e in check.iter_errors(body)}

    # the base schema differs only by the 0.6.0 amended state `blocked` (T007 gate)
    assert errs(core) == {"state"} and errs({**core, "state": "proposed"}) == set()
    recursive = examples["experiment_recursive.valid.json"]
    assert errs({**recursive, "next_cycle_run_id": item["next_cycle_run_id"]}) == set()
    promoted = {"requires_human_promotion": False}  # the invalid example's change
    assert errs({**core, "state": "proposed"} | promoted) == {
        "requires_human_promotion"}  # fmt: skip
    assert not EXPERIMENT.is_valid(item | promoted)
