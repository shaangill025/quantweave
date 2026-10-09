"""API trust-boundary tests (T010 increments 2, 3a) through TestClient on a really
migrated PostgreSQL 16 database, connected as the non-superuser, non-owner qw_app
login of the adapters harness. All tenants, users and passwords are SYNTHETIC."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
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
from qw_api.security import (
    SESSION_COOKIE,
    LocalPasswordAuthenticator,
    client_address,
    csrf_token,
)
from starlette.requests import Request

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls, app_conn = _harness.conn, _harness.runtime_urls, _harness.app_conn

ORIGIN = "https://testserver"
KEY = b"SYNTHETIC-server-key-0123456789abcdef"
JSON = {"Content-Type": "application/json"}
PASSWORD = "SYNTHETIC-correct-horse-1"
OWNER, MEMBER = MembershipRole.TENANT_OWNER, MembershipRole.TENANT_MEMBER
# Cheap argon2id parameters keep the suite fast; the default profile is tested below.
AUTH = LocalPasswordAuthenticator(
    PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
)


def client_for(url: str, **settings: Any) -> TestClient:
    config = Settings(frozenset({ORIGIN}), KEY, **settings)
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


def csrf(client: TestClient, key: str | None = None) -> dict[str, str]:
    """CSRF header plus an Idempotency-Key (fresh unless given)."""
    token = csrf_token(client.cookies[SESSION_COOKIE])
    return {"X-CSRF-Token": token, "Idempotency-Key": key or uuid.uuid4().hex}


def step_up(client: TestClient) -> Response:
    body = {"password": PASSWORD}
    response = client.post("/api/v1/session/step-up", json=body, headers=csrf(client))
    return cast(Response, response)


def patch_role(
    client: TestClient, user: uuid.UUID, role: str, version: int | None = 1,
    key: str | None = None,
) -> Response:  # fmt: skip
    headers = {**csrf(client, key), **({"If-Match": f'"{version}"'} if version else {})}
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
    spec = create_app(Settings(frozenset({ORIGIN}), KEY), lambda: None, AUTH).openapi()  # type: ignore[arg-type, return-value]
    operations = [op for item in spec["paths"].values() for op in item.values()]
    assert len(operations) == 11  # HEAD /health/live and T019 policies included
    params = [p["name"] for op in operations for p in op.get("parameters", [])]
    keys = re.findall(r'"([^"]+)": ', json.dumps(spec))  # every object key, refs too
    assert {"login", "password", "role", "user_id", "If-Match"} <= {*keys, *params}
    assert not [k for k in [*keys, *params] if "tenant" in k.lower()]


def test_settings_and_hasher_defaults() -> None:
    settings = Settings(frozenset({ORIGIN}), KEY)
    assert settings.idle_timeout == timedelta(minutes=30)
    assert settings.account_throttle.max_attempts == 5
    assert (settings.step_up_window, settings.session_ttl) == (
        timedelta(minutes=10), timedelta(hours=12)
    )  # fmt: skip
    for bad in ({"http://evil.example"}, {"https://a.example/path"}, set()):
        with pytest.raises(ValueError, match="origins"):
            Settings(frozenset(bad), KEY)
    with pytest.raises(ValueError, match="step_up_window"):
        Settings(frozenset({ORIGIN}), KEY, step_up_window=timedelta(hours=2))
    with pytest.raises(ValueError, match="secret_key"):
        Settings(frozenset({ORIGIN}), b"short")
    with pytest.raises(ValueError, match="throttle"):
        tenancy.ThrottlePolicy(0, timedelta(1), timedelta(1), timedelta(1))
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


def test_client_address_trusts_only_configured_proxies() -> None:
    def req(peer: str, xff: str) -> Request:
        headers = [(b"x-forwarded-for", xff.encode())]
        return Request({"type": "http", "client": (peer, 1), "headers": headers})

    plain = Settings(frozenset({ORIGIN}), KEY)
    proxied = Settings(
        frozenset({ORIGIN}), KEY, trusted_proxies=frozenset({"10.0.0.1"})
    )
    assert client_address(req("203.0.113.9", "198.51.100.1"), plain) == "203.0.113.9"
    spoofed = req("10.0.0.1", "198.51.100.66, 192.0.2.7, 10.0.0.1")
    assert client_address(spoofed, proxied) == "192.0.2.7"  # rightmost untrusted hop
    cidr = Settings(frozenset({ORIGIN}), KEY, trusted_proxies=frozenset({"10.0.0.0/8"}))
    chain = req("10.1.2.3", "2001:DB8::0001, 10.9.9.9")
    assert client_address(chain, cidr) == "2001:db8::1"  # canonicalised
    with pytest.raises(ValueError):
        Settings(frozenset({ORIGIN}), KEY, trusted_proxies=frozenset({"10.0.0.1/33"}))


@pytest.mark.db
def test_lockout_curve_is_progressive_capped_and_hides_existence(
    world: World, conn: Conn
) -> None:
    policy = tenancy.ThrottlePolicy(
        3, timedelta(minutes=15), timedelta(minutes=1), timedelta(minutes=4)
    )
    limits = {"account_throttle": policy,  # wide address limit isolates the account
              "address_throttle": replace(policy, max_attempts=1000)}  # fmt: skip
    curves = []
    for who in ("alice", "nobody"):
        with client_for(world.url, **limits) as client:
            seen = []
            for _ in range(4):  # four lock cycles
                statuses = [login(client, who, "wrong").status_code for _ in range(4)]
                locked = assert_problem(login(client, who), 429, "too_many_attempts")
                assert statuses == [401, 401, 401, 429]
                assert locked["retry_after_s"] == int(login(client, who).headers[
                    "retry-after"])  # fmt: skip
                seen.append(locked["retry_after_s"])
                conn.execute("UPDATE app.auth_throttle SET locked_until = now()")
            curves.append(seen)
    assert curves[0] == curves[1] == [60, 120, 240, 240]  # base * 2^n, capped
    events = audit(world, "auth.locked_out")
    assert [(e[1], e[2]) for e in events] == [(None, world.users["alice"])] * 4
    with client_for(world.url, **limits) as client:
        assert login(client, "alice").status_code == 201  # unlocked; clears the count
        assert [login(client, "alice", "x").status_code for _ in range(3)] == [401] * 3


@pytest.mark.db
def test_concurrent_attempts_cannot_exceed_the_limit(
    runtime_urls: dict[str, str],
) -> None:
    policy = tenancy.ThrottlePolicy(
        5, timedelta(minutes=15), timedelta(minutes=1), timedelta(hours=1)
    )
    results: list[bool] = []
    barrier = threading.Barrier(20)

    def attempt() -> None:
        with tenancy.runtime_connection(runtime_urls["qw_app"]) as c:
            barrier.wait()
            for _ in range(2):
                results.append(tenancy.throttle_hit(c, b"k" * 32, policy)[0] is None)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (len(results), results.count(True)) == (40, 5)


@pytest.mark.db
def test_idempotency_replays_and_rejects_reuse(world: World) -> None:
    bob, key = world.users["bob"], "SYNTHETIC-key-0001"
    login(world.client, "alice")
    first_step = world.client.post(
        "/api/v1/session/step-up", json={"password": PASSWORD},
        headers=csrf(world.client, key),
    )  # fmt: skip
    again = world.client.post(
        "/api/v1/session/step-up", json={"password": PASSWORD},
        headers=csrf(world.client, key),
    )  # fmt: skip
    assert (
        again.content == first_step.content and "idempotent-replayed" in again.headers
    )
    first = patch_role(world.client, bob, "tenant_owner", key=key)
    replay = patch_role(world.client, bob, "tenant_owner", key=key)
    assert (replay.status_code, replay.content) == (200, first.content)
    assert replay.headers["etag"] == '"2"' and "idempotent-replayed" in replay.headers
    other = patch_role(world.client, bob, "tenant_member", version=2, key=key)
    assert_problem(other, 409, "idempotency_key_reused")
    assert len(audit(world, "membership.role_changed")) == 1
    assert len(audit(world, "session.step_up")) == 1
    short = patch_role(world.client, bob, "tenant_member", version=2, key="short")
    assert_problem(short, 400, "invalid_idempotency_key")
    headers = {"X-CSRF-Token": csrf_token(world.client.cookies[SESSION_COOKIE])}
    missing = world.client.delete("/api/v1/session", headers=headers)
    assert [f["pointer"] for f in assert_problem(missing, 400, "invalid_request")[
        "field_errors"]] == ["/header/Idempotency-Key"]  # fmt: skip


@pytest.mark.db
def test_idle_sessions_expire_and_last_use_only_moves_forward(
    world: World, app_conn: Conn
) -> None:
    with client_for(world.url, idle_timeout=timedelta(seconds=1)) as client:
        login(client, "alice")
        for _ in range(2):  # each use within the timeout keeps the session alive
            time.sleep(0.6)
            assert client.get("/api/v1/session").status_code == 200
        time.sleep(1.2)
        assert_problem(client.get("/api/v1/session"), 401, "unauthenticated")
    for shift in ("- interval '1 h'", "+ interval '1 h'"):  # backdate, or pre-date
        with (
            pytest.raises(Exception, match="last_used_at may only move forward"),
            tenancy.tenant_transaction(app_conn, world.tenant_a),
        ):
            app_conn.execute(f"UPDATE app.session SET last_used_at = now() {shift}")


@pytest.mark.db
def test_cursors_page_and_are_bound_to_tenant_and_principal(
    world: World, app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenancy.tenant_transaction(app_conn, world.tenant_a) as tx:
        for n in range(3):
            user = tenancy.create_user(tx, f"SYNTHETIC extra {n}")
            tenancy.add_membership(tx, user, MEMBER)
    login(world.client, "alice")
    url, ids, cursor = "/api/v1/memberships?limit=2", [], None
    while True:
        page = world.client.get(url + (f"&cursor={cursor}" if cursor else "")).json()
        ids += [i["user_id"] for i in page["items"]]
        if not (cursor := page["next_cursor"]):
            break
    assert len(ids) == 5 and ids == sorted(ids)
    first = world.client.get(url).json()["next_cursor"]
    raw = bytearray(base64.urlsafe_b64decode(first + "=="))
    raw[0] ^= 1  # SYNTHETIC tampering: a different starting user id
    forged = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    for bad in (forged, first[:-2], "!!", "A" * 54):
        response = world.client.get(f"{url}&cursor={bad}")
        assert_problem(response, 400, "invalid_cursor")
    monkeypatch.setattr("qw_api.routes.CURSOR_TTL", -1)  # issued already expired
    stale = world.client.get(url).json()["next_cursor"]
    assert_problem(world.client.get(f"{url}&cursor={stale}"), 400, "invalid_cursor")
    login(world.client, "bob")  # same tenant, other principal
    assert_problem(world.client.get(f"{url}&cursor={first}"), 400, "invalid_cursor")
    login(world.client, "carol")  # other tenant
    assert_problem(world.client.get(f"{url}&cursor={first}"), 400, "invalid_cursor")


@pytest.mark.db
def test_bootstrap_runs_once_and_its_owner_can_log_in(
    runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from qw_api.bootstrap import main

    argv = ["--login", "root", "--display-name", "SYNTHETIC Root"]
    monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_app"])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(argv) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(argv) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["--login", "root2", "--display-name", "SYNTHETIC Again"]) == 1
    with client_for(runtime_urls["qw_app"]) as client:
        response = login(client, "root")
        assert (response.status_code, response.json()["role"]) == (201, "tenant_owner")
        assert login(client, "root2").status_code == 401


@pytest.mark.db
def test_idempotency_keys_do_not_cross_sessions(world: World) -> None:
    key = "SYNTHETIC-key-0002"
    login(world.client, "alice")
    body = {"password": PASSWORD}
    url = "/api/v1/session/step-up"
    assert world.client.post(url, json=body, headers=csrf(world.client, key)).is_success
    attempts = "SELECT sum(attempts) FROM app.auth_throttle"
    before = world.db.execute(attempts).fetchone()
    malformed = world.client.post(url, json=body, headers=csrf(world.client, "bad"))
    assert_problem(malformed, 400, "invalid_idempotency_key")
    assert world.db.execute(attempts).fetchone() == before  # checked before counting
    login(world.client, "alice")  # a new session reuses the step-up key
    reused = world.client.post(url, json=body, headers=csrf(world.client, key))
    assert_problem(reused, 409, "idempotency_key_reused")
    assert world.client.get("/api/v1/session").json()["step_up_at"] is None
    first = world.client.delete("/api/v1/session", headers=csrf(world.client, key))
    assert first.status_code == 204
    login(world.client, "alice")  # a new session reuses the logout key
    reused = world.client.delete("/api/v1/session", headers=csrf(world.client, key))
    assert_problem(reused, 409, "idempotency_key_reused")
    assert world.client.get("/api/v1/session").status_code == 200  # still live


@pytest.mark.db
def test_throttle_rows_stay_bounded_and_only_functions_change_them(
    world: World, conn: Conn, app_conn: Conn
) -> None:
    policy = tenancy.ThrottlePolicy(
        3, timedelta(minutes=15), timedelta(minutes=1), timedelta(hours=1)
    )

    def rows() -> int:
        found = conn.execute("SELECT count(*) FROM app.auth_throttle").fetchone()
        return int(found[0]) if found else -1

    with client_for(world.url, address_throttle=policy) as client:
        statuses = [login(client, f"spray{n}", "x").status_code for n in range(300)]
        assert statuses[:3] == [401] * 3 and set(statuses[3:]) == {429}
        assert rows() == 1 + 3  # one address row, accounts only before the lock
    old = "UPDATE app.auth_throttle SET window_start = now() - interval '2 days'"
    conn.execute(old)  # every row is old, but the address row is still locked
    assert tenancy.throttle_purge(app_conn) == 3 and rows() == 1
    for statement in (
        "UPDATE app.auth_throttle SET attempts = 0",
        "INSERT INTO app.auth_throttle (key_hash) VALUES ('\\x00')",
        "DELETE FROM app.auth_throttle",
    ):
        with pytest.raises(Exception, match="permission denied"):
            app_conn.execute(statement)
