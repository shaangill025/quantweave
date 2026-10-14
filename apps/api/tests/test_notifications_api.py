"""`GET /api/v1/notifications` (T056 increment 2) through TestClient on a migrated
PostgreSQL 16 database as the non-superuser qw_app login. Records are written by
the qw_worker login through qw_adapters.notification_store, as a producer job
would. SYNTHETIC tenants, users, proposals, pauses and jobs only (the T026 store
proposal: buy 80 at 50, acct-1); every time follows the database clock. Bodies are
validated strictly against the contract `Notification` inside the C-02 page."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import notification_store as ns
from qw_adapters import pause_store as pz
from qw_adapters import policy_store
from qw_adapters import tenancy as tn
from qw_api.app import Settings
from qw_api.notifications import id_key
from qw_domain import notifications as N

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
ps = _module("qw_pstore_tests", ROOT / "packages/adapters/tests/test_proposal_store.py")
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
OPENAPI = yaml.safe_load((ROOT / "docs/spec/contracts/openapi.yaml").read_text())
KEY = id_key(Settings(frozenset({api.ORIGIN}), api.KEY))
URL = "/api/v1/notifications"


def conforms(body: dict[str, Any]) -> None:
    page = PC.page("calculation")  # the C-02 page shape, then the inline item
    page["properties"]["items"]["items"] = OPENAPI["components"]["schemas"][
        "Notification"]  # fmt: skip
    v = Draft202012Validator(page, format_checker=FormatChecker())
    assert [(list(e.absolute_path), e.message) for e in v.iter_errors(body)] == []


def seed(w: Any, app_conn: Any, runtime_urls: dict[str, str]) -> tuple[Any, ...]:
    """For bob: a proposal notified at version 1 then revised, an active pause and
    a job outcome, each with an email delivery. Returns object id -> record id,
    the store context and a function recording another signal for bob."""
    alice, bob = w.users["alice"], w.users["bob"]
    with tn.tenant_transaction(app_conn, w.tenant_a) as tx:
        session, _ = tn.create_session(tx, alice, timedelta(hours=1))
        tn.mark_step_up(tx, session)
    c = ps.Ctx(w.tenant_a, alice, session, uuid.uuid4())
    ps.adopt(app_conn, c)
    s = ps.snap(c, ps.db_now(w.db))
    ps.run(app_conn, c, "submit", ps.ver(app_conn, c, s, "prop-syn-n"), c.policy, s)
    v1 = ps.run(app_conn, c, "load", "prop-syn-n")
    with tn.tenant_transaction(app_conn, w.tenant_a) as tx:
        held = pz.pause(tx, c.policy, principal=alice, reason="SYNTHETIC",
                        text="SYNTHETIC stop new risk")  # fmt: skip
    pause = held.book.active()[0]
    tenant = str(w.tenant_a)
    prefs = N.Preferences(email=True)
    me = N.Recipient(tenant, str(bob), prefs, "bob@example.invalid", True, None)
    center, ids = N.Center(tenant, timedelta(0), 3, KEY), {}

    def notify(sig: N.Signal) -> str:
        sub = center.submit(sig, me, ps.db_now(w.db))
        with (psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as worker,
              tn.tenant_transaction(worker, w.tenant_a) as tx):  # fmt: skip
            return str(ns.record(tx, KEY, sub).notification.id)

    for sig in (N.from_proposal(v1), N.from_pause(pause, tenant),
                N.Signal(tenant, N.Category.JOB_OUTCOME, "job-syn-1", "succeeded",
                         ps.db_now(w.db) - timedelta(seconds=1))):  # fmt: skip
        ids[sig.object_id] = notify(sig)
    two = replace(ps.ver(app_conn, c, s, "prop-syn-n", 70), version=2)
    assert ps.run(app_conn, c, "revise", "prop-syn-n", two, s).accepted
    return ids, c, notify


def walk(client: Any, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    reasons: list[str] = []
    url = f"{URL}?limit={limit}"
    for _ in range(20):  # bounded: a cursor that does not advance fails
        if not url:
            break
        got = client.get(url)
        assert got.status_code == 200, got.text
        body = got.json()
        conforms(body)
        items, reasons = items + body["items"], reasons + body["coverage"]["reasons"]
        url = f"{URL}?limit={limit}&cursor={body['next_cursor']}" * bool(
            body["next_cursor"])  # fmt: skip
    assert not url
    return items, reasons


@pytest.mark.db
def test_rows_resolve_current_state_page_and_write_nothing(
    world: Any, app_conn: Any, runtime_urls: dict[str, str]
) -> None:
    ids, *_ = seed(world, app_conn, runtime_urls)
    job, prop = ids["job-syn-1"], ids["prop-syn-n"]
    pause = next(k for k in ids if k not in ("job-syn-1", "prop-syn-n"))
    login(world.client, "bob")
    before = api_rows(world)
    items, reasons = walk(world.client, 1)
    assert walk(world.client, 2)[0] == walk(world.client, 50)[0] == items
    got = [(i["id"], i["object_id"], i["channel"], i["status"],
            i["current_object_state"]) for i in items]  # fmt: skip
    assert got == [
        (job, "job-syn-1", "in_app", "delivered", "unavailable"),
        (f"{job}:email", "job-syn-1", "email", "queued", "unavailable"),
        (ids[pause], pause, "in_app", "delivered", "paused"),
        (f"{ids[pause]}:email", pause, "email", "queued", "paused"),
        (prop, "prop-syn-n", "in_app", "delivered", "superseded"),  # not "1:candidate"
        (f"{prop}:email", "prop-syn-n", "email", "queued", "superseded"),
    ]
    assert sorted(set(reasons)) == [
        f"unavailable:{job}/current_object_state",
        f"unavailable:{job}:email/current_object_state",
    ]
    assert api_rows(world) == before  # listing is not opening: no seen, no audit
    login(world.client, "alice")  # same tenant, another user
    assert world.client.get(URL).json()["items"] == []
    login(world.client, "carol")  # tenant B
    assert world.client.get(URL).json()["items"] == []


def api_rows(w: Any) -> dict[str, int]:
    names = w.db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app' "
                         "ORDER BY tablename").fetchall()  # fmt: skip
    return {t: w.db.execute(f"SELECT count(*) FROM app.{t}").fetchone()[0]
            for (t,) in names}  # fmt: skip


@pytest.mark.db
def test_foreign_cursors_and_tampered_records_fail_closed(
    world: Any, app_conn: Any, runtime_urls: dict[str, str]
) -> None:
    ids, c, notify = seed(world, app_conn, runtime_urls)
    pause = next(k for k in ids if k not in ("job-syn-1", "prop-syn-n"))
    two = notify(N.from_proposal(ps.run(app_conn, c, "load", "prop-syn-n")))

    def same_as_read_proposal(expected: str) -> None:  # one state on both routes
        login(world.client, "bob")
        rows = world.client.get(URL).json()["items"]
        read = world.client.get("/api/v1/proposals/prop-syn-n").json()["state"]
        assert [i["current_object_state"] for i in rows if i["id"] == two] == [
            read] == [expected]  # fmt: skip

    same_as_read_proposal("invalidated")  # v2 is live under the active pause
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:  # the owner resumes
        held = pz.load(tx)
        history = policy_store.load(tx, c.policy)
        assert history is not None
        version = history.versions[-1].version
        pz.resume(tx, c.policy, pz.tag(version, held.active_for(c.policy)),
                  principal=c.owner,
                  session_id=c.session, window=timedelta(minutes=10),
                  reason="SYNTHETIC", text="SYNTHETIC resume")  # fmt: skip
    # a subject the proposal never had: resolution fails, so it is not served stale
    odd = notify(
        N.Signal(
            str(world.tenant_a),
            N.Category.PROPOSAL,
            "prop-syn-n",
            "9:active",
            ps.db_now(world.db) - timedelta(seconds=1),
        )
    )
    same_as_read_proposal("candidate")
    states = {i["id"]: i["current_object_state"]
              for i in world.client.get(URL).json()["items"]}  # fmt: skip
    assert states[ids[pause]] == "resumed" and states[odd] == "unavailable"
    body = world.client.get(f"{URL}?limit=1").json()
    cursor = body["next_cursor"]
    assert cursor and world.client.get(f"{URL}?cursor={cursor}").status_code == 200
    login(world.client, "alice")  # bob's cursor is not alice's
    assert_problem(world.client.get(f"{URL}?cursor={cursor}"), 400, "invalid_cursor")
    forged = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
    assert_problem(world.client.get(f"{URL}?cursor={forged}"), 400, "invalid_cursor")
    for bad in ("0", "201"):
        assert_problem(world.client.get(f"{URL}?limit={bad}"), 400, "invalid_request")
    login(world.client, "bob")
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.notification SET version = '3:candidate' "
                         "WHERE id = %s", (ids["prop-syn-n"],))  # fmt: skip
    crash = assert_problem(world.client.get(URL), 500, "integrity")
    assert "prop-syn-n" not in json.dumps(crash)
