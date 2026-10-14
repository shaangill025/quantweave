"""`POST /policies/{id}/pause` and `/resume` (T020 increment 2) through TestClient
on a migrated PostgreSQL 16 database as the non-superuser qw_app login. Reuses the
T010 API harness and the T019 policy seeds; every policy, user and account is
SYNTHETIC. Responses are validated against docs/spec/contracts/schemas/
policy.schema.json."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import Response
from qw_adapters import pause_store as pz
from qw_adapters import policy_store as ps
from qw_adapters import tenancy
from qw_domain.risk_pauses import Pause, PauseScope, PauseTrigger, ScopeKind

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
pol = _module("qw_policies_api_tests", ROOT / "apps/api/tests/test_policies_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, csrf, step_up, assert_problem = (
    api.login,
    api.csrf,
    api.step_up,
    api.assert_problem,
)
seed, conforms = pol.seed, pol.conforms


def act(
    w: Any, policy: uuid.UUID, op: str, *, revision: int = 1, key: str | None = None,
    match: str | None = None, reason: str = "SYNTHETIC check", client: Any = None,
) -> Response:  # fmt: skip
    c = client or w.client
    headers = {**csrf(c, key), **({"If-Match": match} if match else {})}
    body = {"expected_revision": revision, "acknowledgement": f"SYNTHETIC {op}",
            "reason": reason}  # fmt: skip
    response: Response = c.post(f"/api/v1/policies/{policy}/{op}", json=body,
                                headers=headers)  # fmt: skip
    return response


def tag(w: Any, policy: uuid.UUID) -> str:
    return str(w.client.get(f"/api/v1/policies/{policy}").headers["etag"])


def count(w: Any, table: str) -> int:
    return int(w.db.execute(f"SELECT count(*) FROM app.{table}").fetchone()[0])


def new_version(w: Any, policy: uuid.UUID) -> None:
    with tenancy.runtime_connection(w.url) as c, tenancy.tenant_transaction(
            c, w.tenant_a) as tx:  # fmt: skip
        ps.propose(tx, policy, pol.store.draft(w.tenant_a, ("income",)),
                   w.users["alice"])  # fmt: skip


@pytest.mark.db
def test_pause_is_idempotent_and_never_blocked_by_staleness(world: Any) -> None:
    policy, _ = seed(world)
    new_version(world, policy)  # the client below still thinks version 1
    login(world.client, "bob")  # a member, never stepped up, no journal, no adoption
    key = "SYNTHETIC-pause-0001"
    first = act(world, policy, "pause", revision=1, key=key, match='"7"')
    assert first.status_code == 200, first.text
    body = first.json()
    conforms(body)
    assert (body["status"], body["version"]) == ("paused", 2)
    assert first.headers["etag"].startswith('"2-')
    replay = act(world, policy, "pause", revision=1, key=key, match='"7"')
    assert replay.content == first.content and "idempotent-replayed" in replay.headers
    other_body = act(world, policy, "pause", revision=1, key=key, reason="different")
    assert_problem(other_body, 409, "idempotency_key_reused")
    again = act(world, policy, "pause", revision=99)  # a new key, already paused
    assert again.status_code == 200 and again.json()["status"] == "paused"
    assert count(world, "risk_pause") == 1
    assert world.client.get(f"/api/v1/policies/{policy}").json()["status"] == "paused"
    listed = world.client.get("/api/v1/policies").json()["items"]
    assert [i["status"] for i in listed] == ["paused"]
    audited = [row[:3] for row in api.audit(world, "policy.paused")]  # each request
    assert audited == [(world.tenant_a, world.users["bob"], policy)] * 2
    url = f"/api/v1/policies/{policy}/pause"
    no_csrf = world.client.post(url, json={}, headers={"Idempotency-Key": "x" * 16})
    assert_problem(no_csrf, 403, "csrf_failed")  # still a write: CSRF holds
    login(world.client, "carol")  # tenant B
    foreign = assert_problem(act(world, policy, "pause"), 404, "not_found")
    unknown = assert_problem(act(world, uuid.uuid4(), "pause"), 404, "not_found")
    assert {**foreign, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    assert count(world, "risk_pause") == 1


@pytest.mark.db
def test_resume_needs_if_match_and_a_verified_step_up(world: Any) -> None:
    policy, _ = seed(world)
    login(world.client, "alice")
    unseen = tag(world, policy)  # read before any pause: '"1"'
    login(world.client, "bob")
    assert act(world, policy, "pause").status_code == 200
    t1 = tag(world, policy)  # the version plus a digest of the active pause ids
    assert t1.startswith('"1-') and act(world, policy, "pause").headers["etag"] == t1
    login(world.client, "alice")
    assert_problem(act(world, policy, "resume", match=t1), 401, "step_up_required")
    login(world.client, "bob")
    step_up(world.client)
    assert_problem(act(world, policy, "resume", match=t1), 403, "forbidden")
    login(world.client, "alice")
    step_up(world.client)
    assert_problem(act(world, policy, "resume"), 428, "if_match_required")
    assert_problem(act(world, policy, "resume", match='"2"'), 400, "revision_mismatch")
    assert_problem(act(world, policy, "resume", match=unseen), 412, "version_mismatch")
    blank = act(world, policy, "resume", match=t1, reason=" ")
    assert_problem(blank, 422, "reason")
    with api.client_for(world.url, step_up_window=timedelta(microseconds=1)) as strict:
        login(strict, "alice")
        step_up(strict)
        late = act(world, policy, "resume", match=t1, client=strict)
        assert_problem(late, 401, "step_up_required")
    new_version(world, policy)
    stale = act(world, policy, "resume", match=t1)
    assert_problem(stale, 412, "version_mismatch")
    assert count(world, "risk_pause_resume") == 0
    key = "SYNTHETIC-resume-0001"
    t2 = tag(world, policy)
    done = act(world, policy, "resume", revision=2, match=t2, key=key)
    assert done.status_code == 200, done.text
    conforms(done.json())
    assert (done.json()["status"], done.headers["etag"]) == ("awaiting_adoption", '"2"')
    replay = act(world, policy, "resume", revision=2, match=t2, key=key)
    assert replay.content == done.content and "idempotent-replayed" in replay.headers
    assert_problem(act(world, policy, "resume", revision=2, match='"2"'), 409,
                   "not_paused")  # fmt: skip
    assert count(world, "risk_pause_resume") == 1
    [(_, actor, target, _)] = api.audit(world, "policy.resumed")
    assert (actor, target) == (world.users["alice"], policy)
    login(world.client, "carol")  # tenant B owner, stepped up
    step_up(world.client)
    assert_problem(act(world, policy, "resume", revision=2, match='"2"'), 404,
                   "not_found")  # fmt: skip


@pytest.mark.db
def test_typed_refusals_for_text_reconciliation_and_tamper(world: Any) -> None:
    policy, _ = seed(world)
    login(world.client, "alice")
    step_up(world.client)
    for op, text in (("pause", "a\x00b"), ("resume", "\x07")):
        bad = assert_problem(act(world, policy, op, match='"1"', reason=text), 400,
                             "invalid_request")  # fmt: skip
        assert bad["field_errors"][0]["pointer"] == "/reason"
    with (
        tenancy.runtime_connection(world.url) as c,
        tenancy.tenant_transaction(c, world.tenant_a) as tx,
    ):  # a SYNTHETIC stale-data pause, as a future trigger would record it
        at = tx.conn.execute("SELECT clock_timestamp()").fetchone()[0]  # type: ignore[index]
        p = Pause(str(uuid.uuid4()), PauseScope(ScopeKind.ACCOUNT, "acct-syn-1"),
                  PauseTrigger.STALE, at, "stale:feed-syn", False)  # fmt: skip
        pz._insert_pause(tx, policy, 1, world.users["bob"], "", "t", p)
    refused = act(world, policy, "resume", match=tag(world, policy))
    assert_problem(refused, 409, "reconciled_state_required")
    assert count(world, "risk_pause_resume") == 0
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.risk_pause SET event_hash = repeat('a', 64)")
    assert_problem(act(world, policy, "pause"), 409, "integrity")
    assert_problem(act(world, policy, "resume", match='"1"'), 409, "integrity")
