"""Project contract 0.7.0: `GET /session` (readSession, documented as T010 serves it)
and `GET /session/approval-classes` (readApprovalClasses, owner decisions 5, 8, 10,
11 and 12 of 2026-10-20), through TestClient on a migrated PostgreSQL 16 database as
the non-superuser qw_app login. Bodies are validated strictly against the amended
schemas. Also the amendment tooling (`operations`, `new_schemas`): idempotent and
fail closed. Every tenant, user and password is SYNTHETIC.
Hand-computed oracles: the step-up expires exactly `step_up_window` (10 minutes by
default) after the stored step_up_at; authority scopes follow spec §11 (hosted:
maintainer, maintainer, user; self-hosted: maintainer, operator, user)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_domain.release import Edition
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem, csrf = api.login, api.assert_problem, api.csrf
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
COMMON = json.loads(
    (ROOT / "docs/spec/contracts/schemas/common.schema.json").read_text()
)
REGISTRY = Registry().with_resource(
    "common.schema.json", Resource.from_contents(COMMON)
)
URL = "/api/v1/session/approval-classes"
CLASSES = ["maintainer_release", "operator_install", "user_strategy_adoption"]
HOSTED = ["hosted_maintainer", "hosted_maintainer", "user_strategy_adoption"]
SELF = ["hosted_maintainer", "self_hosted_operator", "user_strategy_adoption"]


def errors(name: str, body: Any) -> list[str]:
    v = Draft202012Validator(
        PC.schema(name), registry=REGISTRY, format_checker=FormatChecker()
    )
    return [e.message for e in v.iter_errors(body)]


def read(client: Any) -> dict[str, Any]:
    got = client.get(URL)
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "no-store"
    body: dict[str, Any] = got.json()
    assert errors("approval_classes", body) == []
    assert body["advisory"] is True
    assert [c["class"] for c in body["classes"]] == CLASSES
    assert all(c["served_operations"] == [] for c in body["classes"])
    assert body["coverage"] == {"status": "complete", "reasons": []}
    return body


def by_class(body: dict[str, Any]) -> dict[str, tuple[bool, list[str]]]:
    return {c["class"]: (c["held"], c["reasons"]) for c in body["classes"]}


def state(w: Any) -> dict[str, Any]:
    tables = w.db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'app' AND table_type = 'BASE TABLE'"
    ).fetchall()
    out = {
        t: w.db.execute(f'SELECT count(*) FROM app."{t}"').fetchone()[0]
        for (t,) in tables
    }
    out["sessions"] = w.db.execute(  # last_used_at is authentication bookkeeping
        "SELECT id, user_id, step_up_at, expires_at, revoked_at FROM app.session "
        "ORDER BY id"
    ).fetchall()
    out["roles"] = w.db.execute(
        "SELECT user_id, role::text, version FROM app.membership ORDER BY user_id"
    ).fetchall()
    return out


@pytest.mark.db
def test_both_session_reads_require_a_session(world: Any) -> None:
    for url in ("/api/v1/session", URL):
        body = assert_problem(world.client.get(url), 401, "unauthenticated")
        assert body["correlation_id"]
        world.client.cookies.set(api.SESSION_COOKIE, "SYNTHETIC-not-a-session")
        assert_problem(world.client.get(url), 401, "unauthenticated")
        world.client.cookies.clear()


@pytest.mark.db
def test_read_session_conforms_to_the_documented_contract(world: Any) -> None:
    login(world.client, "alice")
    got = world.client.get("/api/v1/session")
    assert (got.status_code, got.headers["cache-control"]) == (200, "no-store")
    body = got.json()
    assert errors("session", body) == []
    assert uuid.UUID(body["user_id"]) == world.users["alice"]
    assert (body["role"], body["step_up_at"]) == ("tenant_owner", None)
    assert body["csrf_token"] == csrf(world.client)["X-CSRF-Token"]
    assert api.step_up(world.client).status_code == 200
    stepped = world.client.get("/api/v1/session").json()
    assert errors("session", stepped) == [] and stepped["step_up_at"] is not None
    for bad in (
        {**body, "tenant_id": "t"},
        {**body, "role": "maintainer"},
        {**body, "expires_at": "2026-10-20T10:00:00"},
    ):
        assert errors("session", bad) != [], bad


@pytest.mark.db
def test_every_class_state_hosted_and_self_hosted(world: Any) -> None:
    login(world.client, "bob")  # tenant_member
    member = read(world.client)
    assert member["deployment"] == "hosted"
    assert [c["authority_scope"] for c in member["classes"]] == HOSTED
    assert by_class(member) == {
        "maintainer_release": (
            False,
            ["role_not_served", "step_up_required", "operation_not_served"],
        ),
        "operator_install": (
            False,
            ["role_not_served", "step_up_required", "operation_not_served"],
        ),
        "user_strategy_adoption": (
            False,
            ["role_not_assigned", "step_up_required", "operation_not_served"],
        ),
    }
    login(world.client, "alice")  # tenant_owner: adoption held, not usable yet
    owner = by_class(read(world.client))
    assert owner["user_strategy_adoption"] == (
        True,
        ["step_up_required", "operation_not_served"],
    )
    assert not owner["maintainer_release"][0] and not owner["operator_install"][0]
    settings = world.client.app.state.settings
    world.client.app.state.settings = replace(settings, deployment=Edition.SELF_HOSTED)
    self_hosted = read(world.client)  # an owner is never an operator or maintainer
    assert self_hosted["deployment"] == "self_hosted"
    assert [c["authority_scope"] for c in self_hosted["classes"]] == SELF
    assert by_class(self_hosted) == {
        "maintainer_release": (
            False,
            [
                "role_not_served",
                "self_hosted_release_not_configured",
                "step_up_required",
                "operation_not_served",
            ],
        ),
        "operator_install": (
            False,
            ["role_not_served", "step_up_required", "operation_not_served"],
        ),
        "user_strategy_adoption": owner["user_strategy_adoption"],
    }
    login(world.client, "bob")  # self-hosted grants a member nothing either
    assert by_class(read(world.client)) == {
        **by_class(self_hosted),
        "user_strategy_adoption": by_class(member)["user_strategy_adoption"],
    }


@pytest.mark.db
def test_step_up_state_follows_the_stored_step_up_and_window(world: Any) -> None:
    login(world.client, "alice")
    before = read(world.client)["classes"]
    assert all(
        c["step_up"] == {"required": True, "satisfied": False, "expires_at": None}
        for c in before
    )
    assert api.step_up(world.client).status_code == 200
    (at,) = world.db.execute(
        "SELECT step_up_at FROM app.session WHERE user_id = %s AND revoked_at IS NULL",
        (world.users["alice"],),
    ).fetchone()
    body = read(world.client)
    for c in body["classes"]:
        assert c["step_up"]["satisfied"] is True
        expires = datetime.fromisoformat(c["step_up"]["expires_at"])
        assert expires == at + timedelta(minutes=10)
        assert "step_up_required" not in c["reasons"]
    assert by_class(body)["user_strategy_adoption"] == (True, ["operation_not_served"])
    assert datetime.fromisoformat(body["evaluated_at"]) >= at
    settings = world.client.app.state.settings  # the step-up guard forbids edits
    world.client.app.state.settings = replace(
        settings, step_up_window=timedelta(microseconds=1)
    )
    for c in read(world.client)["classes"]:
        assert c["step_up"]["satisfied"] is False
        assert c["step_up"]["expires_at"] is not None
        assert "step_up_required" in c["reasons"]


@pytest.mark.db
def test_the_read_writes_nothing_grants_nothing_and_leaks_no_identity(
    world: Any,
) -> None:
    login(world.client, "bob")
    before = state(world)
    got = world.client.get(URL)
    assert state(world) == before
    text = got.text
    secrets = [
        *map(str, world.users.values()),
        str(world.tenant_a),
        *(str(r[0]) for r in before["sessions"]),
    ]
    assert not [s for s in secrets if s in text]
    assert set(got.json()) == {
        "deployment",
        "evaluated_at",
        "advisory",
        "classes",
        "coverage",
    }
    # A member's read does not unlock an owner action: still independently refused.
    alice = world.users["alice"]
    assert_problem(
        api.patch_role(world.client, alice, "tenant_member"), 403, "forbidden"
    )
    login(world.client, "alice")
    assert by_class(read(world.client))["user_strategy_adoption"][0] is True
    # held does not waive step-up: the mutation re-derives it and refuses.
    bob = world.users["bob"]
    assert_problem(
        api.patch_role(world.client, bob, "tenant_owner"), 401, "step_up_required"
    )
    for method in ("post", "put", "patch", "delete"):  # no endpoint accepts it
        response = world.client.request(method, URL, headers=csrf(world.client))
        assert_problem(response, 405, "method_not_allowed")
    assert state(world)["roles"] == before["roles"]


@pytest.mark.db
def test_classes_come_from_the_callers_own_session_only(world: Any) -> None:
    login(world.client, "carol")  # tenant B's owner
    carol = by_class(read(world.client))
    login(world.client, "bob")  # tenant A's member: B's owner changes nothing here
    bob = by_class(world.client.get(f"{URL}?tenant_id={uuid.uuid4()}").json())
    assert carol["user_strategy_adoption"][0] is True
    assert bob["user_strategy_adoption"] == (
        False,
        ["role_not_assigned", "step_up_required", "operation_not_served"],
    )


def test_the_schema_refuses_a_grant_or_an_unexplained_class() -> None:
    held: dict[str, Any] = {
        "class": "maintainer_release",
        "authority_scope": "hosted_maintainer",
        "held": False,
        "served_operations": [],
        "reasons": ["role_not_served"],
        "step_up": {"required": True, "satisfied": False, "expires_at": None},
    }
    body: dict[str, Any] = {
        "deployment": "hosted",
        "evaluated_at": "2026-10-20T10:00:00Z",
        "advisory": True,
        "coverage": {"status": "complete", "reasons": []},
        "classes": [
            held,
            {**held, "class": "operator_install"},
            {
                **held,
                "class": "user_strategy_adoption",
                "authority_scope": "user_strategy_adoption",
            },
        ],
    }
    assert errors("approval_classes", body) == []
    stepped = {**held, "step_up": {**held["step_up"], "satisfied": True}}
    bad = [
        {**body, "advisory": False},
        {k: v for k, v in body.items() if k != "advisory"},
        {**body, "grant_token": "x"},
        {**body, "classes": body["classes"][:2]},
        {**body, "classes": [*body["classes"], held]},
        {**body, "classes": [body["classes"][1], held, body["classes"][2]]},
        {**body, "classes": [{**held, "reasons": []}, *body["classes"][1:]]},
        {**body, "classes": [{**held, "approved_by": "u"}, *body["classes"][1:]]},
        {**body, "classes": [stepped, *body["classes"][1:]]},
        {**body, "evaluated_at": "2026-10-20T10:00:00"},
    ]
    for case in bad:
        assert errors("approval_classes", case) != [], case


def test_amendment_tooling_is_idempotent_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = PC.openapi(), PC.openapi()
    assert first == second  # each call starts again from the unmodified base
    assert "/session" not in PC._openapi()["paths"]
    ops = {
        p: {m: o["operationId"] for m, o in first["paths"][p].items()}
        for p in ("/session", "/session/approval-classes")
    }
    assert ops == {
        "/session": {"get": "readSession"},
        "/session/approval-classes": {"get": "readApprovalClasses"},
    }
    with pytest.raises(ValueError, match="path already in the contract"):
        PC.openapi(first)  # applying again on top of the result is refused
    component = first["components"]["schemas"]["approval_classes"]
    assert component["properties"]["evaluated_at"] == {
        "$ref": "./schemas/common.schema.json#/$defs/Timestamp"
    }
    assert component["properties"]["coverage"] == {
        "$ref": "#/components/schemas/Coverage"
    }

    def refused(change: Any, match: str, call: Any = None) -> None:
        amended = copy.deepcopy(PC.AMENDMENTS)
        change(amended)
        monkeypatch.setattr(PC, "AMENDMENTS", amended)
        with pytest.raises(ValueError, match=match):
            (call or PC.openapi)()
        monkeypatch.undo()

    def op(m: Any) -> Any:
        return m["operations"]["/session/approval-classes"]["get"]

    refused(lambda m: op(m).update(operationId="readCapabilities"), "operationId")
    refused(lambda m: op(m).update(operationId="readSession"), "operationId")
    refused(lambda m: op(m).pop("operationId"), "operationId")
    refused(
        lambda m: m["operations"].update(
            {"/capabilities": {"get": {"operationId": "x"}}}
        ),
        "path already",
    )
    for taken in ("experiment", "Capabilities", "Coverage", "proposal"):
        refused(
            lambda m, t=taken: m["new_schemas"].update(
                {t: m["new_schemas"]["session"]}
            ),
            "already in the contract",
        )
        refused(  # the schema itself refuses the name, not only the component merge
            lambda m, t=taken: m["new_schemas"].update(
                {t: m["new_schemas"]["session"]}
            ),
            "schema name already in the contract",
            lambda t=taken: PC.schema(t),
        )
    refused(
        lambda m: (
            m["new_schemas"]["session"]
            .setdefault("$defs", {})
            .update({"Problem": {"type": "object"}})
        ),
        "Problem: component already",
    )
