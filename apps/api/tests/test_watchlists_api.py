"""`/api/v1/watchlists` (listWatchlists, createWatchlist, readWatchlist,
updateWatchlist, deleteWatchlist; T024 increment 2) through TestClient on a migrated
PostgreSQL 16 database as qw_app (T010 harness). SYNTHETIC tenants, users, names and
journal (test_reads_api.seed_journal); watchlist times are the database clock.
Responses are validated against the contract `watchlist`, `watchlistPage` (project
contract coverage) and `job`."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
rd = _module("qw_reads_api_tests", ROOT / "apps/api/tests/test_reads_api.py")
fbt = _module("qw_feedback_api_tests", ROOT / "apps/api/tests/test_feedback_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, csrf, assert_problem = api.login, api.csrf, api.assert_problem
URL = "/api/v1/watchlists"
WATCHLIST = Draft202012Validator(rd.PC.schema("watchlist"), registry=rd.REGISTRY,
                                 format_checker=FormatChecker())  # fmt: skip
GROWTH = {"name": "SYNTHETIC growth", "description": "SYNTHETIC thesis list"}


def ok(response: Any, status: int, revision: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert [e.message for e in WATCHLIST.iter_errors(body)] == []
    assert body["revision"] == revision and body["entries"] == []
    assert response.headers["etag"] == f'"{revision}"'
    return body


def write(w: Any, method: str, url: str, body: Any = None, match: str | None = None,
          key: str | None = None) -> Any:  # fmt: skip
    headers = {**csrf(w.client, key), **({"If-Match": match} if match else {})}
    return w.client.request(method, url, json=body, headers=headers)


K3 = "SYNTHETIC-wl-api-k-03"


def purge(w: Any) -> None:
    w.db.execute("DELETE FROM app.idempotency_record")


def revisions(w: Any) -> int:
    return int(w.db.execute("SELECT count(*) FROM app.watchlist_revision")
               .fetchone()[0])  # fmt: skip


@pytest.mark.db
def test_crud_follows_if_match_and_idempotency_and_never_touches_holdings(
    world: Any,
) -> None:
    rd.seed_journal(world)
    login(world.client, "bob")
    snap = world.client.get("/api/v1/accounts/acct-syn-1/snapshot").json()
    made = ok(write(world, "POST", URL, GROWTH, key="SYNTHETIC-wl-api-k-01"), 201, 1)
    wid = made["id"]
    assert made["name"] == GROWTH["name"] and "description" not in made
    replay = write(world, "POST", URL, GROWTH, key="SYNTHETIC-wl-api-k-01")
    assert ok(replay, 201, 1) == made and "idempotent-replayed" in replay.headers
    purge(world)  # the store replays
    assert ok(write(world, "POST", URL, GROWTH, key="SYNTHETIC-wl-api-k-01"), 201,
              1) == made  # fmt: skip
    reused = write(world, "POST", URL, GROWTH | {"name": "x"},
                   key="SYNTHETIC-wl-api-k-01")  # fmt: skip
    assert_problem(reused, 409, "idempotency_key_reused")
    one = f"{URL}/{wid}"
    assert ok(world.client.get(one), 200, 1) == made
    renamed = GROWTH | {"name": "SYNTHETIC value"}
    assert_problem(write(world, "PATCH", one, renamed), 428, "if_match_required")
    for stale in ('"2"', 'W/"1"', "1", '"01"', "*", '"1", "2"'):
        assert_problem(write(world, "PATCH", one, renamed, stale), 412,
                       "version_mismatch")  # fmt: skip
    assert revisions(world) == 1
    body = ok(write(world, "PATCH", one, renamed, '"1"', K3), 200, 2)
    assert body["name"] == "SYNTHETIC value"
    assert_problem(write(world, "PATCH", one, GROWTH, '"1"', K3), 409,
                   "idempotency_key_reused")  # same key, another body  # fmt: skip
    purge(world)  # the store replays; no second audit event (checked below)
    assert ok(write(world, "PATCH", one, renamed, '"1"', K3), 200, 2) == body
    assert_problem(write(world, "DELETE", one), 428, "if_match_required")
    assert_problem(write(world, "DELETE", one, None, '"1"'), 412, "version_mismatch")
    gone = write(world, "DELETE", one, None, '"2"', "SYNTHETIC-wl-api-k-02")
    assert gone.status_code == 202, gone.text
    job = gone.json()
    assert [e.message for e in fbt.JOB.iter_errors(job)] == []
    assert (job["state"], job["kind"], job["result_reference"]) == (
        "succeeded", "watchlist.delete", wid)  # fmt: skip
    again = write(world, "DELETE", one, None, '"2"', "SYNTHETIC-wl-api-k-02")
    assert again.json() == job and "idempotent-replayed" in again.headers
    assert_problem(write(world, "DELETE", one, None, '"1"', "SYNTHETIC-wl-api-k-02"),
                   409, "idempotency_key_reused")  # another If-Match  # fmt: skip
    purge(world)
    assert (
        write(world, "DELETE", one, None, '"2"', "SYNTHETIC-wl-api-k-02").json() == job
    )
    for response in (world.client.get(one), write(world, "PATCH", one, renamed, '"3"'),
                     write(world, "DELETE", one, None, '"3"')):  # fmt: skip
        assert_problem(response, 404, "not_found")
    assert revisions(world) == 3
    actions = [
        api.audit(world, f"watchlist.{a}") for a in ("created", "updated", "deleted")
    ]
    assert [len(a) for a in actions] == [1, 1, 1]
    assert {a[0][1:3] for a in actions} == {(world.users["bob"], uuid.UUID(wid))}
    # AT057: the list's life changed no account, holding or cash (hand oracle in
    # test_reads_api: 10 units, 9492.55 CAD ledger cash).
    assert world.client.get("/api/v1/accounts/acct-syn-1/snapshot").json() == snap


@pytest.mark.db
def test_tenancy_cursors_and_bad_input(world: Any) -> None:
    login(world.client, "alice")
    ids = sorted(ok(write(world, "POST", URL, {"name": f"SYNTHETIC {i}",
                                               "description": ""}), 201, 1)["id"]
                 for i in range(3))  # fmt: skip
    seen, cursor = [], None
    for _ in range(3):
        page = world.client.get(URL, params={"limit": 2, **(
            {"cursor": cursor} if cursor else {})}).json()  # fmt: skip
        rd.page_conforms("watchlist", page)
        assert page["coverage"] == {"status": "complete", "reasons": []}
        seen += [w["id"] for w in page["items"]]
        if (cursor := page["next_cursor"]) is None:
            break
    assert seen == ids
    first = world.client.get(URL, params={"limit": 1}).json()["next_cursor"]
    assert_problem(world.client.get("/api/v1/policies", params={"cursor": first}),
                   400, "invalid_cursor")  # fmt: skip
    for bad in ("x", first[:-2] + "AA"):
        assert_problem(world.client.get(URL, params={"cursor": bad}), 400,
                       "invalid_cursor")  # fmt: skip
    login(world.client, "bob")  # same tenant, another principal
    assert_problem(world.client.get(URL, params={"cursor": first}), 400,
                   "invalid_cursor")  # fmt: skip
    assert len(world.client.get(URL).json()["items"]) == 3
    login(world.client, "carol")  # tenant B
    assert world.client.get(URL).json()["items"] == []
    one = f"{URL}/{ids[0]}"
    for response in (world.client.get(one), world.client.get(f"{URL}/{uuid.uuid4()}"),
                     world.client.get(f"{URL}/{ids[0].upper()}"),
                     write(world, "PATCH", one, GROWTH, '"1"'),
                     write(world, "DELETE", one, None, '"1"')):  # fmt: skip
        assert_problem(response, 404, "not_found")
    bodies: list[dict[str, Any]] = [
        {"name": "", "description": ""}, {"name": "x"},
        GROWTH | {"tenant_id": str(world.tenant_a)}, GROWTH | {"entries": []},
        {"name": "a\x00b", "description": ""}, {"name": 1, "description": ""},
        {"name": "x" * 201, "description": ""},
    ]  # fmt: skip
    for body in bodies:
        assert_problem(write(world, "POST", URL, body), 400, "invalid_request")
    no_csrf = world.client.post(
        URL, json=GROWTH, headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    assert_problem(no_csrf, 403, "csrf_failed")
    no_key = world.client.post(URL, json=GROWTH, headers={
        "X-CSRF-Token": csrf(world.client)["X-CSRF-Token"]})  # fmt: skip
    assert_problem(no_key, 400, "invalid_request")
    assert revisions(world) == 3
    for _ in range(2):  # an exact multiple of the limit: no further page
        ok(write(world, "POST", URL, GROWTH), 201, 1)
    page = world.client.get(URL, params={"limit": 2}).json()
    assert (len(page["items"]), page["next_cursor"]) == (2, None)


@pytest.mark.db
def test_tampered_revisions_are_409_integrity(world: Any) -> None:
    login(world.client, "alice")
    wid = ok(write(world, "POST", URL, GROWTH), 201, 1)["id"]
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.watchlist_revision SET name = 'SYNTHETIC forged'")
    for response in (
        world.client.get(URL),
        world.client.get(f"{URL}/{wid}"),
        write(world, "PATCH", f"{URL}/{wid}", GROWTH, '"1"'),
    ):
        body = assert_problem(response, 409, "integrity")
        assert "forged" not in json.dumps(body)
