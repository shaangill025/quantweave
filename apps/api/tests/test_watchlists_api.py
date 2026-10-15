"""`/api/v1/watchlists` (listWatchlists, createWatchlist, readWatchlist,
updateWatchlist, deleteWatchlist; T024 increment 2; the three entry operations,
increment 3) through TestClient on a migrated
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


def ok(response: Any, status: int, revision: int,
       entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert [e.message for e in WATCHLIST.iter_errors(body)] == []
    assert body["revision"] == revision and body["entries"] == (entries or [])
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


IID = "00000000-0000-4000-8000-0000000000a1"
MOVE = {"metric": "pct_move", "operator": "between", "values": ["1", "2.5"],
        "data_spec_id": "SYNTHETIC-feed", "description": "SYNTHETIC move"}  # fmt: skip
ENTRY: dict[str, Any] = {
    "instrument_id": IID, "thesis": "SYNTHETIC thesis", "tags": ["SYNTHETIC"],
    "monitoring_priority": 60, "invalidation_text": "SYNTHETIC invalidation",
    "conditions": [MOVE]}  # fmt: skip
SERVED = ENTRY | {"review_at": None}  # the contract response entry; never `needs`


@pytest.mark.db
def test_entries_follow_if_match_and_idempotency_and_never_touch_holdings(
    world: Any,
) -> None:
    rd.seed_journal(world)
    login(world.client, "bob")
    snap = world.client.get("/api/v1/accounts/acct-syn-1/snapshot").json()
    wid = ok(write(world, "POST", URL, GROWTH), 201, 1)["id"]
    url, one = f"{URL}/{wid}/entries", f"{URL}/{wid}/entries/{IID}"
    for stale in ('"2"', '"01"', 'W/"1"'):  # an If-Match sent is honoured
        assert_problem(write(world, "POST", url, ENTRY, stale), 412,
                       "version_mismatch")  # fmt: skip
    k = "SYNTHETIC-wl-api-e-01"
    added = ok(write(world, "POST", url, ENTRY, '"1"', k), 200, 2, [SERVED])
    replay = write(world, "POST", url, ENTRY, '"1"', k)
    assert ok(replay, 200, 2, [SERVED]) == added and "idempotent-replayed" in (
        replay.headers)  # fmt: skip
    purge(world)  # the store replays
    assert ok(write(world, "POST", url, ENTRY, '"1"', k), 200, 2, [SERVED]) == added
    assert_problem(write(world, "POST", url, ENTRY | {"thesis": "x"}, '"1"', k), 409,
                   "idempotency_key_reused")  # fmt: skip
    for match in ('"2"', None):  # add replays on the key and body alone
        assert ok(write(world, "POST", url, ENTRY, match, k), 200, 2,
                  [SERVED]) == added  # fmt: skip
    assert ok(world.client.get(f"{URL}/{wid}"), 200, 2, [SERVED]) == added
    assert_problem(write(world, "POST", url, ENTRY, '"2"'), 409, "entry_exists")
    bad: list[tuple[dict[str, Any], int, str]] = [
        (ENTRY | {"review_at": "2026-11-02"}, 400, "invalid_request"),  # 0.5.0
        (ENTRY | {"needs": "delayed"}, 400, "invalid_request"),
        (ENTRY | {"monitoring_priority": 101}, 400, "invalid_request"),
        (ENTRY | {"monitoring_priority": True}, 400, "invalid_request"),
        (ENTRY | {"thesis": "a\x00b"}, 400, "invalid_request"),
        ({k: v for k, v in ENTRY.items() if k != "tags"}, 400, "invalid_request"),
        (ENTRY | {"instrument_id": IID.upper()}, 422, "invalid_entry"),
        (ENTRY | {"conditions": [MOVE | {"operator": "eq"}]}, 422,
         "invalid_entry"),
        (ENTRY | {"conditions": [MOVE | {"values": ["2", "1"]}]},
         422, "invalid_entry"),
        (ENTRY | {"conditions": [MOVE] * 17}, 400, "invalid_request"),  # bounds
        (ENTRY | {"tags": ["SYNTHETIC"] * 33}, 400, "invalid_request"),
        (ENTRY | {"conditions": [MOVE | {"values": ["1"] * 9}]}, 400,
         "invalid_request"),
        (ENTRY | {"conditions": [MOVE | {"values": ["1"] * 8}]}, 422,
         "invalid_entry"),  # 8 passes the bound; the domain needs 1 or 2
    ]  # fmt: skip
    for body, status, code in bad:
        other = body | {"instrument_id": body["instrument_id"].replace("a1", "b2")}
        problem = assert_problem(write(world, "POST", url, other, '"2"'), status,
                                 code)  # fmt: skip
        assert status != 422 or problem["field_errors"] == []  # none for 422
    new = ENTRY | {"thesis": "SYNTHETIC revised", "monitoring_priority": 0}
    assert_problem(write(world, "PATCH", one, new), 428, "if_match_required")
    assert_problem(write(world, "PATCH", one, new, '"1"'), 412, "version_mismatch")
    elsewhere = f"{URL}/{wid}/entries/00000000-0000-4000-8000-0000000000c3"
    assert_problem(write(world, "PATCH", elsewhere, new, '"2"'), 400,
                   "instrument_mismatch")  # fmt: skip
    assert_problem(write(world, "PATCH", elsewhere, new | {
        "instrument_id": elsewhere[-36:]}, '"2"'), 404, "not_found")  # fmt: skip
    k2 = "SYNTHETIC-wl-api-e-02"
    served = new | {"review_at": None}
    changed = ok(write(world, "PATCH", one, new, '"2"', k2), 200, 3, [served])
    assert_problem(write(world, "PATCH", one, new, '"3"', k2), 409,
                   "idempotency_key_reused")  # another If-Match  # fmt: skip
    purge(world)
    assert ok(write(world, "PATCH", one, new, '"2"', k2), 200, 3, [served]) == changed
    assert_problem(write(world, "DELETE", one), 428, "if_match_required")
    assert_problem(write(world, "DELETE", one, None, '"2"'), 412, "version_mismatch")
    k3 = "SYNTHETIC-wl-api-e-03"
    removed = ok(write(world, "DELETE", one, None, '"3"', k3), 200, 4)
    assert_problem(write(world, "DELETE", one, None, '"4"', k3), 409,
                   "idempotency_key_reused")  # another If-Match  # fmt: skip
    purge(world)
    assert ok(write(world, "DELETE", one, None, '"3"', k3), 200, 4) == removed
    for response in (write(world, "DELETE", one, None, '"4"'),
                     write(world, "PATCH", one, new, '"4"'),
                     write(world, "DELETE", f"{URL}/{wid}/entries/{IID.upper()}",
                           None, '"4"')):  # fmt: skip
        assert_problem(response, 404, "not_found")
    assert revisions(world) == 4
    events = [api.audit(world, f"watchlist.entry_{a}")
              for a in ("added", "updated", "removed")]  # fmt: skip
    assert [len(e) for e in events] == [1, 1, 1]
    assert {e[0][1:3] for e in events} == {(world.users["bob"], uuid.UUID(wid))}
    assert world.client.get("/api/v1/accounts/acct-syn-1/snapshot").json() == snap
    login(world.client, "carol")  # tenant B: the same 404 as an unknown list
    for response in (write(world, "POST", url, ENTRY, '"4"'),
                     write(world, "PATCH", one, ENTRY, '"4"'),
                     write(world, "DELETE", one, None, '"4"'),
                     write(world, "POST", f"{URL}/{uuid.uuid4()}/entries", ENTRY,
                           '"1"')):  # fmt: skip
        body = assert_problem(response, 404, "not_found")
        assert body["detail"] == "No such watchlist."
    assert revisions(world) == 4


@pytest.mark.db
def test_a_tampered_entry_is_409_integrity(world: Any) -> None:
    login(world.client, "alice")
    wid = ok(write(world, "POST", URL, GROWTH), 201, 1)["id"]
    ok(write(world, "POST", f"{URL}/{wid}/entries", ENTRY, '"1"'), 200, 2, [SERVED])
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.watchlist_revision SET entries = jsonb_set("
                         "entries, '{0,thesis}', '\"SYNTHETIC forged\"')")  # fmt: skip
    for response in (world.client.get(f"{URL}/{wid}"),
                     write(world, "DELETE", f"{URL}/{wid}/entries/{IID}", None,
                           '"2"')):  # fmt: skip
        assert "forged" not in json.dumps(assert_problem(response, 409, "integrity"))


@pytest.mark.db
def test_add_entry_if_match_is_optional_as_in_the_contract(world: Any) -> None:
    login(world.client, "alice")
    wid = ok(write(world, "POST", URL, GROWTH), 201, 1)["id"]
    url = f"{URL}/{wid}/entries"
    k = "SYNTHETIC-wl-api-o-01"
    first = ok(write(world, "POST", url, ENTRY, None, k), 200, 2, [SERVED])
    b2 = ENTRY | {"instrument_id": IID.replace("a1", "b2")}
    both = [SERVED, b2 | {"review_at": None}]
    ok(write(world, "POST", url, b2), 200, 3, both)  # none: the latest revision
    c3 = ENTRY | {
        "instrument_id": IID.replace("a1", "c3"),
        "conditions": [MOVE] * 16,
        "tags": ["SYNTHETIC"] * 32,
    }  # bounds
    assert_problem(write(world, "POST", url, c3, '"2"'), 412, "version_mismatch")
    ok(write(world, "POST", url, c3, '"3"'), 200, 4, [*both, c3 | {"review_at": None}])
    for match in (None, '"1"'):  # a replay of the first add, by key and body
        assert ok(write(world, "POST", url, ENTRY, match, k), 200, 2,
                  [SERVED]) == first  # fmt: skip
    assert revisions(world) == 4
    assert len(api.audit(world, "watchlist.entry_added")) == 3


REQUEST = Draft202012Validator(rd.PC.request("WatchlistEntry"), registry=rd.REGISTRY,
                               format_checker=FormatChecker())  # fmt: skip
BAD_REVIEW = ["2026-11-02", "2026-11-02T14:00:00", "2026-11-02 14:00:00Z",
              "2026-02-30T14:00:00Z", "2026-11-02T14:00:00-00:00", "soon", "",
              1798207200, True, ["2026-11-02T14:00:00Z"]]  # fmt: skip


@pytest.mark.db
def test_review_at_is_settable_and_kept_when_omitted(world: Any) -> None:
    """Project contract 0.5.0: `review_at` is optional on the request, an offset
    timestamp served in UTC; an update without it keeps the stored value."""
    login(world.client, "alice")
    wid = ok(write(world, "POST", URL, GROWTH), 201, 1)["id"]
    url, one = f"{URL}/{wid}/entries", f"{URL}/{wid}/entries/{IID}"
    set_at = ENTRY | {"review_at": "2026-11-02T09:00:00-05:00"}
    assert [e.message for e in REQUEST.iter_errors(set_at)] == []
    k = "SYNTHETIC-wl-api-r-01"
    at_14 = [ENTRY | {"review_at": "2026-11-02T14:00:00Z"}]
    added = ok(write(world, "POST", url, set_at, '"1"', k), 200, 2, at_14)
    assert ok(write(world, "POST", url, set_at, '"1"', k), 200, 2, at_14) == added
    purge(world)  # the store replays too
    assert ok(write(world, "POST", url, set_at, '"1"', k), 200, 2, at_14) == added
    assert_problem(write(world, "POST", url, ENTRY | {
        "review_at": "2026-11-03T14:00:00Z"}, '"1"', k), 409,
        "idempotency_key_reused")  # fmt: skip
    new = ENTRY | {"thesis": "SYNTHETIC kept"}
    kept = [new | {"review_at": "2026-11-02T14:00:00Z"}]
    ok(write(world, "PATCH", one, new, '"2"'), 200, 3, kept)  # omitted: kept
    later = new | {"review_at": "2026-12-01T09:30:00.250000+00:00"}
    k2 = "SYNTHETIC-wl-api-r-02"
    moved = [new | {"review_at": "2026-12-01T09:30:00.250000Z"}]
    changed = ok(write(world, "PATCH", one, later, '"3"', k2), 200, 4, moved)
    purge(world)
    assert ok(write(world, "PATCH", one, later, '"3"', k2), 200, 4, moved) == changed
    for other in (new | {"review_at": "2026-12-02T09:30:00Z"}, new):
        assert_problem(write(world, "PATCH", one, other, '"3"', k2), 409,
                       "idempotency_key_reused")  # fmt: skip
    k3 = "SYNTHETIC-wl-api-r-03"
    ok(write(world, "PATCH", one, new | {"review_at": None}, '"4"', k3), 200, 5,
       [new | {"review_at": None}])  # null clears it  # fmt: skip
    assert_problem(write(world, "PATCH", one, new, '"4"', k3), 409,
                   "idempotency_key_reused")  # omitted is not null  # fmt: skip
    for bad in BAD_REVIEW:  # the route refuses each; the contract all but -00:00
        body = ENTRY | {"review_at": bad}
        contract = [e.message for e in REQUEST.iter_errors(body)]
        assert (contract == []) == (bad == "2026-11-02T14:00:00-00:00"), bad  # C-05
        assert_problem(write(world, "PATCH", one, body, '"5"'), 400,
                       "invalid_request")  # fmt: skip
        assert_problem(write(world, "POST", url, body | {
            "instrument_id": IID.replace("a1", "b2")}, '"5"'), 400,
            "invalid_request")  # fmt: skip
    assert_problem(write(world, "PATCH", one, ENTRY | {"needs": "delayed"}, '"5"'),
                   400, "invalid_request")  # fmt: skip
    assert revisions(world) == 5
    stored = world.db.execute(
        "SELECT entries->0->>'needs', review_at_set FROM "
        "app.watchlist_revision ORDER BY revision"
    ).fetchall()
    assert stored[1:] == [("realtime_exchange_limited", False),
                          ("realtime_exchange_limited", False),
                          ("realtime_exchange_limited", True),
                          ("realtime_exchange_limited", True)]  # fmt: skip
