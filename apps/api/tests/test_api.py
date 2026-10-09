"""API trust-boundary tests (T010 increment 2) through TestClient against a really
migrated PostgreSQL 16 database, connected as the non-superuser, non-owner qw_app
login of the adapters harness. All tenants, users and passwords are SYNTHETIC."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from httpx import Response
from qw_adapters import tenancy
from qw_adapters.tenancy import Conn, MembershipRole
from qw_api import Settings, create_app
from qw_api.security import SESSION_COOKIE, LocalPasswordAuthenticator, csrf_token

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls, app_conn = _harness.conn, _harness.runtime_urls, _harness.app_conn

ORIGIN = "https://testserver"
JSON = {"Content-Type": "application/json"}
PASSWORD = "SYNTHETIC-correct-horse-1"
OWNER, MEMBER = MembershipRole.TENANT_OWNER, MembershipRole.TENANT_MEMBER
# Cheap argon2id parameters keep the suite fast; the default profile is tested below.
AUTH = LocalPasswordAuthenticator(
    PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
)


def client_for(url: str, **settings: Any) -> TestClient:
    config = Settings(frozenset({ORIGIN}), **settings)
    app = create_app(config, lambda: tenancy.runtime_connection(url), AUTH)
    headers = {"Origin": ORIGIN}
    return TestClient(app, ORIGIN, headers=headers, raise_server_exceptions=False)


Ids = dict[str, uuid.UUID]


@dataclass
class World:
    client: TestClient
    url: str
    db: Conn  # superuser, for assertions only
    tenant_a: uuid.UUID
    users: Ids


def seed(app_conn: Conn, users: dict[str, MembershipRole]) -> tuple[uuid.UUID, Ids]:
    ids: Ids = {}
    with tenancy.provision_tenant(app_conn) as tx:
        for login, role in users.items():
            ids[login] = tenancy.create_user(tx, f"SYNTHETIC {login}")
            tenancy.add_membership(tx, ids[login], role)
            AUTH.enroll(tx, ids[login], login, PASSWORD)
    return tx.tenant_id, ids


@pytest.fixture
def world(runtime_urls: dict[str, str], conn: Conn, app_conn: Conn) -> Iterator[World]:
    a, a_users = seed(app_conn, {"alice": OWNER, "bob": MEMBER})
    _, b_users = seed(app_conn, {"carol": OWNER})  # tenant B
    with client_for(url := runtime_urls["qw_app"]) as client:
        yield World(client, url, conn, a, a_users | b_users)


def login(client: TestClient, who: str, password: str = PASSWORD) -> Response:
    return cast(
        Response,
        client.post("/api/v1/session", json={"login": who, "password": password}),
    )


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_token(client.cookies[SESSION_COOKIE])}


def step_up(client: TestClient) -> Response:
    body = {"password": PASSWORD}
    response = client.post("/api/v1/session/step-up", json=body, headers=csrf(client))
    return cast(Response, response)


def patch_role(
    client: TestClient, user: uuid.UUID, role: str, version: int | None = 1
) -> Response:
    headers = {**csrf(client), **({"If-Match": f'"{version}"'} if version else {})}
    url = f"/api/v1/memberships/{user}"
    return cast(Response, client.patch(url, json={"role": role}, headers=headers))


def assert_problem(response: Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert set(body) == {"type", "title", "status", "code", "detail", "correlation_id",
                         "retryable", "retry_after_s", "field_errors", "reasons",
                         "error_schema"}  # fmt: skip
    assert (body["status"], body["code"], body["error_schema"]) == (status, code, "1")
    assert body["type"] == f"/problems/{code}"
    assert body["correlation_id"] == response.headers["x-correlation-id"]
    return body


def audit(world: World, action: str) -> list[tuple[Any, ...]]:
    return world.db.execute(
        "SELECT tenant_id, actor_user_id, target_id, correlation_id "
        "FROM app.audit_event WHERE action = %s ORDER BY occurred_at",
        (action,),
    ).fetchall()


def test_health_caps_and_malformed_input_use_the_envelope() -> None:
    big, url = b"x" * (64 * 1024 + 1), "/api/v1/session"
    with client_for("host=/nonexistent port=1") as client:
        response = client.get("/api/v1/health/live")
        assert (response.status_code, response.json()) == (200, {"status": "healthy"})
        assert "set-cookie" not in response.headers
        assert client.head("/api/v1/health/live").status_code == 200
        crash = assert_problem(client.get("/api/v1/session"), 500, "internal_error")
        assert crash["retryable"] is True and "nonexistent" not in str(crash)
        declared = assert_problem(
            client.post(url, content=big), 413, "payload_too_large"
        )
        assert declared["detail"] == "Content-Length is too large."  # before reading
        chunked = client.post(url, content=iter([big[:40000], big[40000:]]))
        assert "content-length" not in chunked.request.headers
        streamed = assert_problem(chunked, 413, "payload_too_large")
        assert streamed["detail"] == "The streamed body is too large."
        bad = client.post(url, content=b'{"login": "\xff"}', headers=JSON)
        assert_problem(bad, 400, "invalid_request")
        deep = client.post(url, content=b"[" * 50000, headers=JSON)
        assert_problem(deep, 400, "invalid_request")
        twice = [("Origin", ORIGIN), ("Origin", ORIGIN), *JSON.items()]
        repeated = client.post(url, content=b"{}", headers=twice)
        assert_problem(repeated, 403, "origin_rejected")


def test_no_implemented_operation_accepts_a_tenant_id() -> None:
    spec = create_app(Settings(frozenset({ORIGIN})), lambda: None, AUTH).openapi()  # type: ignore[arg-type, return-value]
    operations = [op for item in spec["paths"].values() for op in item.values()]
    assert len(operations) == 8  # HEAD /health/live included
    params = [p["name"] for op in operations for p in op.get("parameters", [])]
    keys = re.findall(r'"([^"]+)": ', json.dumps(spec))  # every object key, refs too
    assert {"login", "password", "role", "user_id", "If-Match"} <= {*keys, *params}
    assert not [k for k in [*keys, *params] if "tenant" in k.lower()]


def test_settings_and_hasher_defaults() -> None:
    settings = Settings(frozenset({ORIGIN}))
    assert (settings.step_up_window, settings.session_ttl) == (
        timedelta(minutes=10), timedelta(hours=12)
    )  # fmt: skip
    for bad in ({"http://evil.example"}, {"https://a.example/path"}, set()):
        with pytest.raises(ValueError, match="origins"):
            Settings(frozenset(bad))
    with pytest.raises(ValueError, match="step_up_window"):
        Settings(frozenset({ORIGIN}), step_up_window=timedelta(hours=2))
    verifier = LocalPasswordAuthenticator().hasher.hash("SYNTHETIC")  # RFC 9106
    assert verifier.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


@pytest.mark.db
def test_login_cookie_then_me(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    response = login(world.client, "alice")
    assert response.status_code == 201, response.text
    cookie = response.headers["set-cookie"]
    token = world.client.cookies[SESSION_COOKIE]
    assert cookie.startswith(f"{SESSION_COOKIE}={token};")
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attribute in cookie
    assert "domain" not in cookie.lower()
    body = world.client.get("/api/v1/session").json()
    assert body["user_id"] == str(world.users["alice"])
    assert (body["role"], body["step_up_at"]) == ("tenant_owner", None)
    assert body["csrf_token"] == csrf_token(token) == response.json()["csrf_token"]
    assert body["expires_at"].endswith("Z") and "tenant_id" not in body
    [(tenant, actor, _, cid)] = audit(world, "session.created")
    assert (tenant, actor, cid) == (
        world.tenant_a, world.users["alice"], response.headers["x-correlation-id"]
    )  # fmt: skip
    step_up(world.client)
    assert PASSWORD not in caplog.text and token not in caplog.text
    assert f"POST /api/v1/session 201 cid={cid}" in caplog.text


@pytest.mark.db
def test_bad_credentials_are_indistinguishable(world: World) -> None:
    wrong = assert_problem(login(world.client, "alice", "nope"), 401,
                           "invalid_credentials")  # fmt: skip
    unknown = assert_problem(login(world.client, "mallory"), 401, "invalid_credentials")
    assert {**wrong, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    assert SESSION_COOKIE not in world.client.cookies


@pytest.mark.db
def test_revoked_expired_and_forged_sessions_get_401(
    world: World, app_conn: Conn
) -> None:
    login(world.client, "alice")
    token = world.client.cookies[SESSION_COOKIE]
    logout = world.client.delete("/api/v1/session", headers=csrf(world.client))
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert len(audit(world, "session.revoked")) == 1
    with tenancy.tenant_transaction(app_conn, world.tenant_a) as tx:
        _, expired = tenancy.create_session(
            tx, world.users["alice"], timedelta(microseconds=1)
        )
    for stale in (token, expired, "A" * 43, "not-a-token"):
        world.client.cookies.set(SESSION_COOKIE, stale)
        assert_problem(world.client.get("/api/v1/session"), 401, "unauthenticated")
    world.client.cookies.clear()
    assert_problem(world.client.get("/api/v1/memberships"), 401, "unauthenticated")


@pytest.mark.db
def test_origin_and_csrf_are_enforced_on_writes(world: World) -> None:
    evil = {"Origin": "https://evil.example"}
    body = {"login": "alice", "password": PASSWORD}
    response = world.client.post("/api/v1/session", json=body, headers=evil)
    assert_problem(response, 403, "origin_rejected")
    assert len(audit(world, "session.created")) == 0
    login(world.client, "alice")
    good = csrf(world.client)
    attempts = [
        ({}, "csrf_failed"),
        ({"X-CSRF-Token": "0" * 64}, "csrf_failed"),
        ({**good, **evil}, "origin_rejected"),
        ({**good, "Origin": "null"}, "origin_rejected"),
    ]
    for headers, code in attempts:
        response = world.client.delete("/api/v1/session", headers=headers)
        assert_problem(response, 403, code)
    assert world.client.get("/api/v1/session", headers=evil).status_code == 200
    no_origin = TestClient(world.client.app, base_url=ORIGIN)
    no_origin.cookies = world.client.cookies
    for bad in ({}, {"Referer": "https://testserver.evil.example/x"}):
        response = no_origin.delete("/api/v1/session", headers={**good, **bad})
        assert_problem(response, 403, "origin_rejected")
    referer = {**good, "Referer": f"{ORIGIN}/settings"}
    assert no_origin.delete("/api/v1/session", headers=referer).status_code == 204


@pytest.mark.db
def test_memberships_are_tenant_scoped(world: World) -> None:
    login(world.client, "alice")
    page = world.client.get("/api/v1/memberships").json()
    assert {i["user_id"] for i in page["items"]} == {
        str(world.users["alice"]), str(world.users["bob"])
    }  # fmt: skip
    assert page["coverage"] == {"status": "complete", "reasons": []}
    assert step_up(world.client).status_code == 200
    carol = patch_role(world.client, world.users["carol"], "tenant_member")
    foreign = assert_problem(carol, 404, "not_found")
    nobody = patch_role(world.client, uuid.uuid4(), "tenant_member")
    missing = assert_problem(nobody, 404, "not_found")
    assert foreign["detail"] == missing["detail"]
    role = world.db.execute(
        "SELECT role::text FROM app.membership WHERE user_id = %s",
        (world.users["carol"],),
    ).fetchone()
    assert role == ("tenant_owner",)


@pytest.mark.db
def test_role_change_needs_owner_and_recent_step_up(world: World) -> None:
    bob = world.users["bob"]
    login(world.client, "bob")  # the role gate runs before the step-up check
    assert_problem(patch_role(world.client, bob, "tenant_owner"), 403, "forbidden")
    assert step_up(world.client).status_code == 200
    assert_problem(patch_role(world.client, bob, "tenant_owner"), 403, "forbidden")
    login(world.client, "alice")
    assert_problem(patch_role(world.client, bob, "tenant_owner"), 401,
                   "step_up_required")  # fmt: skip
    wrong = world.client.post("/api/v1/session/step-up", json={"password": "x"},
                              headers=csrf(world.client))  # fmt: skip
    assert_problem(wrong, 403, "reauthentication_failed")
    assert step_up(world.client).json()["step_up_at"] is not None
    response = patch_role(world.client, bob, "tenant_owner")
    assert response.status_code == 200, response.text
    assert response.json() == {"user_id": str(bob), "role": "tenant_owner",
                               "version": 2, "display_name": None}  # fmt: skip
    assert response.headers["etag"] == '"2"'
    [(tenant, actor, target, cid)] = audit(world, "membership.role_changed")
    assert (tenant, actor, target) == (world.tenant_a, world.users["alice"], bob)
    assert cid == response.headers["x-correlation-id"]
    with client_for(world.url, step_up_window=timedelta(microseconds=1)) as strict:
        login(strict, "alice")
        step_up(strict)
        assert_problem(patch_role(strict, bob, "tenant_member", version=2), 401,
                       "step_up_required")  # fmt: skip


@pytest.mark.db
def test_last_owner_and_self_promotion_are_blocked(world: World) -> None:
    alice, bob = world.users["alice"], world.users["bob"]
    login(world.client, "alice")
    step_up(world.client)
    assert_problem(patch_role(world.client, alice, "tenant_member"), 409, "last_owner")
    stale = patch_role(world.client, bob, "tenant_owner", version=7)
    assert_problem(stale, 412, "version_mismatch")
    unversioned = patch_role(world.client, bob, "tenant_owner", version=None)
    assert_problem(unversioned, 428, "if_match_required")
    assert patch_role(world.client, bob, "tenant_owner").status_code == 200
    login(world.client, "bob")
    step_up(world.client)  # with two owners, an owner may step down
    assert patch_role(world.client, bob, "tenant_member", version=2).status_code == 200
    # bob is a member again: a self-promotion fails at the role gate.
    login(world.client, "bob")
    step_up(world.client)
    promote = patch_role(world.client, bob, "tenant_owner", version=3)
    assert_problem(promote, 403, "forbidden")
    roles = world.db.execute(
        "SELECT user_id, role::text FROM app.membership WHERE tenant_id = %s",
        (world.tenant_a,),
    ).fetchall()
    assert dict(roles) == {alice: "tenant_owner", bob: "tenant_member"}


@pytest.mark.db
def test_client_tenant_id_and_bad_input_are_rejected(world: World) -> None:
    body = {"login": "alice", "password": PASSWORD, "tenant_id": str(uuid.uuid4())}
    response = world.client.post("/api/v1/session", json=body)
    problem = assert_problem(response, 400, "invalid_request")
    assert [f["pointer"] for f in problem["field_errors"]] == ["/tenant_id"]
    assert PASSWORD not in response.text
    login(world.client, "alice")
    assert_problem(world.client.get("/api/v1/nope"), 404, "not_found")
    assert_problem(world.client.get("/api/v1/memberships/x"), 405, "method_not_allowed")
    step_up(world.client)
    response = world.client.patch("/api/v1/memberships/not-a-uuid", json={},
                                  headers=csrf(world.client))  # fmt: skip
    problem = assert_problem(response, 400, "invalid_request")
    pointers = {f["pointer"] for f in problem["field_errors"]}
    assert pointers == {"/path/user_id", "/role"}
