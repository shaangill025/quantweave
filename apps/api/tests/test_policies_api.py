"""Policy routes (T019 increment 2) through TestClient on a migrated PostgreSQL 16
database as the non-superuser qw_app login. Reuses the T010 API harness and the
SYNTHETIC drafts of packages/adapters/tests/test_policy_store.py. Responses are
validated against docs/spec/contracts/schemas/policy.schema.json."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import Response
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import policy_store as ps
from qw_adapters import tenancy
from qw_domain.policy import ConflictCode

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
store = _module("qw_store_tests", ROOT / "packages/adapters/tests/test_policy_store.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, csrf, step_up, assert_problem = (
    api.login,
    api.csrf,
    api.step_up,
    api.assert_problem,
)
SCHEMA = json.loads(
    (ROOT / "docs/spec/contracts/schemas/policy.schema.json").read_text()
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
ACK = [ConflictCode.OPTION_PERMISSION_UNKNOWN.value]


def seed(w: Any) -> tuple[uuid.UUID, str]:
    """One SYNTHETIC proposed policy in tenant A; returns (policy id, content hash)."""
    policy = uuid.uuid4()
    with (
        tenancy.runtime_connection(w.url) as c,
        tenancy.tenant_transaction(c, w.tenant_a) as tx,
    ):
        history = ps.propose(tx, policy, store.draft(w.tenant_a), w.users["alice"])
    return policy, history.versions[-1].content_hash


def conforms(body: dict[str, Any]) -> None:
    errors = [e.message for e in VALIDATOR.iter_errors(body)]
    assert errors == [], errors


def adopt(
    w: Any, policy: uuid.UUID, digest: str, *, version: int = 1,
    match: str | None = '"1"', key: str | None = None, ack: list[str] = ACK,
    client: Any = None, text: str = "SYNTHETIC I adopt",
) -> Response:  # fmt: skip
    c = client or w.client
    headers = {**csrf(c, key), **({"If-Match": match} if match else {})}
    body = {"version_id": str(version), "content_hash": digest,
            "expected_revision": version, "acknowledgement": text,
            "acknowledged_conflicts": ack}  # fmt: skip
    url = f"/api/v1/policies/{policy}/adopt"
    response: Response = c.post(url, json=body, headers=headers)
    return response


@pytest.mark.db
def test_reads_are_tenant_scoped_and_conform_to_the_schema(world: Any) -> None:
    policy, digest = seed(world)
    login(world.client, "bob")  # a member may read
    response = world.client.get(f"/api/v1/policies/{policy}")
    assert response.status_code == 200, response.text
    body = response.json()
    conforms(body)
    assert (body["id"], body["version"], body["status"]) == (
        str(policy), 1, "awaiting_adoption")  # fmt: skip
    assert (body["content_hash"], response.headers["etag"]) == (digest, '"1"')
    assert body["scope_account_ids"] == ["acct-syn-1"]
    assert body["horizons"] == ["long_term", "intraday"]
    assert {"metric": "drawdown", "value": "2500.5", "unit": "CAD",
            "denominator": "sleeve_nav", "scope_id": "acct-syn-1:CAD:s1"} in body[
        "numerical_limits"]  # fmt: skip
    assert (body["accepted_by"], body["accepted_at"]) == (None, None)
    second, _ = seed(world)
    ids, url = [], "/api/v1/policies?limit=1"
    page = world.client.get(url).json()
    while page["next_cursor"]:
        ids += [p["id"] for p in page["items"]]
        page = world.client.get(f"{url}&cursor={page['next_cursor']}").json()
    assert ids + [p["id"] for p in page["items"]] == sorted([str(policy), str(second)])
    login(world.client, "carol")  # tenant B
    assert world.client.get("/api/v1/policies").json()["items"] == []
    foreign = assert_problem(world.client.get(f"/api/v1/policies/{policy}"), 404,
                             "not_found")  # fmt: skip
    unknown = world.client.get(f"/api/v1/policies/{uuid.uuid4()}")
    assert {**foreign, "correlation_id": ""} == {
        **assert_problem(unknown, 404, "not_found"), "correlation_id": ""}  # fmt: skip


@pytest.mark.db
def test_adoption_gates(world: Any) -> None:
    policy, digest = seed(world)
    login(world.client, "alice")
    assert_problem(adopt(world, policy, digest), 401, "step_up_required")
    login(world.client, "bob")
    step_up(world.client)
    assert_problem(adopt(world, policy, digest), 403, "forbidden")  # members read only
    login(world.client, "alice")
    step_up(world.client)
    url = f"/api/v1/policies/{policy}/adopt"
    no_csrf = world.client.post(url, json={}, headers={"Idempotency-Key": "x" * 16})
    assert_problem(no_csrf, 403, "csrf_failed")
    evil = {**csrf(world.client), "Origin": "https://evil.example"}
    assert_problem(
        world.client.post(url, json={}, headers=evil), 403, "origin_rejected"
    )
    assert_problem(adopt(world, policy, digest, match=None), 428, "if_match_required")
    assert_problem(adopt(world, policy, digest, match='"2"'), 400, "revision_mismatch")
    assert_problem(adopt(world, policy, digest, version=2, match='"2"'), 412,
                   "version_mismatch")  # fmt: skip
    assert_problem(adopt(world, policy, "e" * 64), 412, "version_mismatch")
    assert_problem(adopt(world, policy, digest, ack=["nope"]), 400, "invalid_request")
    long = adopt(world, policy, digest, text="€" * 2000)  # 6000 bytes, 2000 chars
    assert assert_problem(long, 400, "invalid_request")["field_errors"][0][
        "pointer"] == "/acknowledgement"  # fmt: skip
    refused = assert_problem(adopt(world, policy, digest, ack=[]), 422,
                             "unacknowledged_conflict")  # fmt: skip
    assert "account_option_permission_unknown" in refused["detail"]
    with api.client_for(world.url, step_up_window=timedelta(microseconds=1)) as strict:
        login(strict, "alice")
        step_up(strict)
        assert_problem(adopt(world, policy, digest, client=strict), 401,
                       "step_up_required")  # fmt: skip
    assert world.db.execute("SELECT count(*) FROM app.policy_adoption").fetchone() == (
        0,
    )


@pytest.mark.db
def test_adopt_once_with_replay_and_no_existence_oracle(world: Any) -> None:
    policy, digest = seed(world)
    login(world.client, "alice")
    step_up(world.client)
    key = "SYNTHETIC-adopt-0001"
    response = adopt(world, policy, digest, key=key)
    assert response.status_code == 200, response.text
    body = response.json()
    conforms(body)
    assert (body["status"], body["accepted_by"]) == (
        "adopted",
        str(world.users["alice"]),
    )
    assert body["accepted_at"].endswith("Z") and response.headers["etag"] == '"1"'
    replay = adopt(world, policy, digest, key=key)
    assert (
        replay.content == response.content and "idempotent-replayed" in replay.headers
    )
    assert_problem(adopt(world, policy, digest), 409, "already_adopted")
    assert world.client.get(f"/api/v1/policies/{policy}").json() == body
    [(tenant, actor, target, _)] = api.audit(world, "policy.adopted")
    assert (tenant, actor, target) == (world.tenant_a, world.users["alice"], policy)
    login(world.client, "carol")  # tenant B owner, stepped up
    step_up(world.client)
    foreign = assert_problem(adopt(world, policy, digest), 404, "not_found")
    unknown = assert_problem(adopt(world, uuid.uuid4(), digest), 404, "not_found")
    assert {**foreign, "correlation_id": ""} == {**unknown, "correlation_id": ""}
