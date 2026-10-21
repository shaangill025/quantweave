"""Project contract 0.8.0 and 0.9.0 (owner decisions 14, 15 and 24 to 26 of
2026-10-21): the documented session cookie is the `__Host-pi_session` the server
sets; every route the server serves is in the merged contract, except a justified
few listed below; the four session and membership mutations conform to it.
DB tests run through TestClient on a migrated PostgreSQL 16 database as the
non-superuser qw_app login. Every tenant, user and password is SYNTHETIC."""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Response
from fastapi.routing import APIRoute
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from pydantic import BaseModel
from qw_adapters import tenancy
from qw_adapters.tenancy import MembershipRole
from qw_api import security as sec
from qw_api.app import Settings, create_app
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
sys.path.insert(0, str(ROOT / "tools"))
CC = _module("qw_check_contracts", ROOT / "tools/check_contracts.py")
COMMON = json.loads(
    (ROOT / "docs/spec/contracts/schemas/common.schema.json").read_text()
)
REGISTRY = Registry().with_resource(
    "common.schema.json", Resource.from_contents(COMMON)
)
PREFIX = "/api/v1"  # openapi.yaml `servers`

# Served but deliberately outside the contract. Not an API operation: the
# unauthenticated liveness probe (T008 C-11, owned by T060). The spec's
# `GET /health` is authenticated component health; a probe cannot carry a session,
# so it cannot meet rule 6 (exactly sessionCookie) and is not amended.
NOT_API = {("get", "/health/live"), ("head", "/health/live")}
# `GET /policies` (listPolicies, T008 C-18) is documented by its owning task T019
# (owner decision 27, 2026-10-21); it is not a session or membership route.
OUTSIDE_DECISION_15 = {("get", "/policies")}
MUTATIONS = {
    ("post", "/session", "createSession"),
    ("delete", "/session", "revokeSession"),
    ("post", "/session/step-up", "stepUpSession"),
    ("patch", "/memberships/{user_id}", "changeMembershipRole"),
}  # owner decision 24


def _app() -> Any:
    settings = Settings(frozenset({api.ORIGIN}), api.KEY)
    logging.getLogger("qw_api").disabled = True  # the trusted_proxies warning
    try:
        return create_app(settings, lambda: None, api.AUTH)  # type: ignore[arg-type, return-value]
    finally:
        logging.getLogger("qw_api").disabled = False


def _walk(routes: Any) -> set[tuple[str, str]]:
    """Every (method, path) the app routes, through included routers."""
    out: set[tuple[str, str]] = set()
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            out |= _walk(inner.routes)
        elif isinstance(route, APIRoute):
            out |= {(m.lower(), route.path) for m in route.methods or ()}
        else:  # a mount, websocket or plain route would escape the review
            raise AssertionError(f"unreviewed route kind: {route!r}")
    return out


def served() -> set[tuple[str, str]]:
    app = _app()
    walked = _walk(app.router.routes)
    generated = {
        (m, p) for p, item in app.openapi()["paths"].items() for m in item
    }  # a route hidden from the generated schema would differ here
    assert walked == generated
    assert all(p.startswith(PREFIX + "/") for _, p in walked)
    return {(m, p.removeprefix(PREFIX)) for m, p in walked}


def documented() -> set[tuple[str, str]]:
    paths = PC.openapi()["paths"]
    return {(m, p) for p, item in paths.items() for m in item if m in PC.METHODS}


def errors(name: str, body: Any) -> list[str]:
    v = Draft202012Validator(
        PC.schema(name), registry=REGISTRY, format_checker=FormatChecker()
    )
    return [e.message for e in v.iter_errors(body)]


def cookie_name() -> str:
    name: str = PC.openapi()["components"]["securitySchemes"]["sessionCookie"]["name"]
    return name


def validator(schema: Any) -> Any:
    return Draft202012Validator(
        schema, registry=REGISTRY, format_checker=FormatChecker()
    )


def member_errors(body: Any) -> list[str]:
    page = PC.schema("membershipPage")  # decision 26: the list's item schema
    item = {"$defs": page["$defs"], "$ref": "#/$defs/Membership"}
    return [e.message for e in validator(item).iter_errors(body)]


def operation(path: str, method: str) -> Any:
    return PC.openapi()["paths"][path][method]


def request_errors(path: str, method: str, body: Any) -> list[str]:
    content = operation(path, method)["requestBody"]["content"]
    schema = content["application/json"]["schema"]
    return [e.message for e in validator(schema).iter_errors(body)]


def test_every_served_route_is_documented_or_explicitly_excluded() -> None:
    missing = served() - documented()
    unexpected = missing - NOT_API - OUTSIDE_DECISION_15
    assert not unexpected, f"served but not in the merged contract: {unexpected}"
    stale = (NOT_API | OUTSIDE_DECISION_15) - missing
    assert not stale, f"listed as undocumented but documented or unserved: {stale}"
    assert {("get", "/session"), ("get", "/memberships")} <= served() & documented()
    listed = {(e["method"], e["path"], e["operationId"]) for e in PC.AMENDMENTS[
        "mutations"]}  # fmt: skip
    assert listed == MUTATIONS
    assert {(m, p) for m, p, _ in MUTATIONS} <= served() & documented()
    assert all(operation(p, m)["operationId"] == o for m, p, o in MUTATIONS)


def test_merged_decisions_24_and_25_approve_and_exempt_exactly() -> None:
    section = CC._decision_section("2026-10-21#24")
    approved = {(m.upper(), p, o) for m, p, o in MUTATIONS}
    assert CC.approvals(section) == (1, approved, set())
    assert CC.approvals(CC._decision_section("2026-10-21#25")) == (1, set(), {
        ("unauthenticated_login", "createSession"), ("no_content", "revokeSession"),
    })  # fmt: skip
    assert CC.approvals(section.replace("\n", "\r\n")) == (1, approved, set())


def test_request_bodies_mirror_the_server_models() -> None:
    from qw_api import routes

    models: list[tuple[str, str, type[BaseModel]]] = [
        ("/session", "post", routes.Login),
        ("/session/step-up", "post", routes.StepUp),
        ("/memberships/{user_id}", "patch", routes.RoleChange),
    ]
    for path, method, model in models:
        served_schema = model.model_json_schema()
        for prop in served_schema["properties"].values():
            prop.pop("title")
        del served_schema["title"]
        documented_schema = copy.deepcopy(
            operation(path, method)["requestBody"]["content"]["application/json"]
        )["schema"]
        for schema in (served_schema, documented_schema):  # required is a set
            schema["required"] = sorted(schema["required"])
        assert documented_schema == served_schema | {"type": "object"}, path
    ok = operation("/memberships/{user_id}", "patch")["responses"]["200"]
    ref = ok["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/Membership"  # the list's item (decision 26)
    assert operation("/session", "delete")["responses"].keys() == {"204", "default"}
    assert operation("/session", "post")["security"] == []  # decision 25


def test_the_documented_session_cookie_is_the_one_the_server_sets() -> None:
    base = PC._openapi()["components"]["securitySchemes"]["sessionCookie"]
    assert base["name"] == "pi_session"  # docs/spec stays unchanged
    assert cookie_name() == sec.SESSION_COOKIE == "__Host-pi_session"
    response = Response()
    sec.set_session_cookie(response, "SYNTHETIC-token", timedelta(minutes=1))
    (header,) = response.headers.getlist("set-cookie")
    name, _, rest = header.partition("=")
    assert name == cookie_name()
    attrs = {a.strip().split("=")[0].lower() for a in rest.split(";")[1:]}
    assert {"secure", "httponly", "path"} <= attrs and "domain" not in attrs
    assert "path=/;" in header.lower() + ";"  # __Host-: Path=/, Secure, no Domain


def test_security_scheme_amendment_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(change: Any, match: str) -> None:
        amended = copy.deepcopy(PC.AMENDMENTS)
        change(amended["security_schemes"])
        monkeypatch.setattr(PC, "AMENDMENTS", amended)
        with pytest.raises(ValueError, match=match):
            PC.openapi()
        monkeypatch.undo()

    exact = "is not __Host-pi_session"  # decision 14 names exactly one cookie
    for name in ("pi_session2", "pi_session", "__Host-other", "__Host-pi_session2"):
        refused(lambda s, n=name: s.update(sessionCookie={"name": n}), exact)
    refused(lambda s: s["sessionCookie"].update({"in": "header"}), "only the name")
    refused(lambda s: s.update(bearer={"name": "__Host-x"}), "no such")
    base = copy.deepcopy(PC._openapi())  # only the scheme already amended
    base["components"]["securitySchemes"] = PC.openapi()["components"][
        "securitySchemes"
    ]
    with pytest.raises(ValueError, match="'__Host-pi_session' already"):
        PC.openapi(base)


@pytest.mark.db
def test_login_sets_and_logout_clears_the_documented_cookie(world: Any) -> None:
    got = login(world.client, "alice")
    (header,) = got.headers.get_list("set-cookie")
    assert header.split("=", 1)[0] == cookie_name()
    assert world.client.cookies[cookie_name()]
    out = world.client.delete(
        "/api/v1/session", headers=csrf(world.client, "SYNTHETIC-logout-key-01")
    )
    assert out.status_code == 204
    (cleared,) = out.headers.get_list("set-cookie")
    assert cleared.startswith(f'{cookie_name()}=""') or cleared.startswith(
        f"{cookie_name()}=;"
    )
    assert "max-age=0" in cleared.lower()


@pytest.mark.db
def test_list_memberships_conforms_to_the_documented_contract(
    world: Any, app_conn: Any
) -> None:
    with tenancy.tenant_transaction(app_conn, world.tenant_a) as tx:
        for n in range(3):
            user = tenancy.create_user(tx, f"SYNTHETIC extra {n} ü")
            tenancy.add_membership(tx, user, MembershipRole.TENANT_MEMBER)
    login(world.client, "bob")  # any member may read
    url, pages, cursor = "/api/v1/memberships?limit=2", [], None
    while True:
        got = world.client.get(url + (f"&cursor={cursor}" if cursor else ""))
        assert (got.status_code, got.headers["cache-control"]) == (200, "no-store")
        pages.append(body := got.json())
        assert errors("membershipPage", body) == [], body
        if not (cursor := body["next_cursor"]):
            break
    assert [len(p["items"]) for p in pages] == [2, 2, 1]
    assert all(p["next_cursor"] for p in pages[:-1])
    full = world.client.get("/api/v1/memberships").json()
    assert errors("membershipPage", full) == [] and len(full["items"]) == 5
    item = full["items"][0]
    assert set(item) == {"user_id", "role", "version", "display_name"}
    tenant = str(world.tenant_a)
    assert tenant not in json.dumps(full)  # the tenant is never served
    page = {**full, "items": [item]}
    for bad in (
        {**page, "tenant_id": tenant},
        {**page, "next_cursor": "not a cursor"},
        {**page, "items": [{**item, "display_name": None}]},
        {**page, "items": [{**item, "display_name": ""}]},
        {**page, "items": [{**item, "version": 0}]},
        {**page, "items": [{**item, "version": "1"}]},
        {**page, "items": [{**item, "role": "maintainer"}]},
        {**page, "items": [{**item, "tenant_id": tenant}]},
        {**page, "items": [{k: v for k, v in item.items() if k != "version"}]},
        {**page, "items": [item] * 201},
        {k: v for k, v in page.items() if k != "coverage"},
    ):
        assert errors("membershipPage", bad) != [], bad
    for query in ("limit=0", "limit=201", "cursor=" + "A" * 129):
        assert_problem(world.client.get(f"/api/v1/memberships?{query}"), 400,
                       "invalid_request")  # fmt: skip


@pytest.mark.db
def test_the_session_mutations_conform_to_the_documented_contract(
    world: Any,
) -> None:
    body = {"login": "alice", "password": api.PASSWORD}
    assert request_errors("/session", "post", body) == []
    for bad in (
        {**body, "tenant_id": "t"},
        {"login": "alice"},
        {**body, "password": ""},
    ):
        assert request_errors("/session", "post", bad) != [], bad
        response = world.client.post("/api/v1/session", json=bad)
        assert_problem(response, 400, "invalid_request")
    got = login(world.client, "alice")
    assert (got.status_code, got.headers["cache-control"]) == (201, "no-store")
    assert errors("session", got.json()) == []
    stepped = api.step_up(world.client)
    assert (stepped.status_code, stepped.headers["cache-control"]) == (200, "no-store")
    assert errors("step_up", stepped.json()) == []
    for bad in (
        {**stepped.json(), "tenant_id": "t"},
        {"step_up_at": "2026-10-21T10:00"},
    ):
        assert errors("step_up", bad) != [], bad
    assert request_errors("/session/step-up", "post", {"password": "x"}) == []
    bob, key = world.users["bob"], "SYNTHETIC-role-change-01"
    assert request_errors("/memberships/{user_id}", "patch", {"role": "x"}) != []
    changed = api.patch_role(world.client, bob, "tenant_owner", key=key)
    assert (changed.status_code, changed.headers["etag"]) == (200, '"2"')
    assert member_errors(changed.json()) == []
    assert changed.json()["display_name"] == "SYNTHETIC bob"  # decision 26
    items = world.client.get("/api/v1/memberships").json()["items"]
    assert changed.json() in items  # one object, one schema
    replay = api.patch_role(world.client, bob, "tenant_owner", key=key)
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == changed.json()
    out = world.client.delete("/api/v1/session", headers=csrf(world.client))
    assert (out.status_code, out.content) == (204, b"")
    assert out.headers["cache-control"] == "no-store"


def test_list_memberships_parameters_mirror_the_server() -> None:
    op = PC.openapi()["paths"]["/memberships"]["get"]
    params = {p["name"]: p for p in op["parameters"]}
    assert params["limit"]["schema"] == {
        "type": "integer", "minimum": 1, "maximum": 200, "default": 50,
    }  # fmt: skip
    assert params["cursor"]["schema"] == {"type": "string", "maxLength": 128}
    generated = _app().openapi()["paths"][PREFIX + "/memberships"]["get"]
    served_params = {p["name"]: p["schema"] for p in generated["parameters"]}
    assert served_params["limit"] | {"title": None} == {
        "type": "integer", "minimum": 1, "maximum": 200, "default": 50,
        "title": None,
    }  # fmt: skip
    cursor = served_params["cursor"]["anyOf"][0]
    assert cursor == {"type": "string", "maxLength": 128}
    assert set(params) == set(served_params)


def _scratch(m: Any) -> Any:
    """The amendments without the four real mutations, plus a well-formed amended
    mutation on a scratch path (checker tests against a scratch decisions file)."""
    for _, path, _ in MUTATIONS:
        item = m["operations"][path]
        for method in [k for k in item if k in CC.MUTATING]:
            del item[method]
    m["mutations"] = []
    m["sensitive_fields"] = [
        e for e in m["sensitive_fields"] if e["schema"] == "session"
    ]
    get = copy.deepcopy(m["operations"]["/memberships"]["get"])
    s = {"type": "string", "minLength": 1}
    get["parameters"] = [
        {"name": h, "in": "header", "required": True, "schema": s}
        for h in ("Idempotency-Key", "X-CSRF-Token")
    ]
    m["operations"]["/scratch/notes"] = {"post": get | {"operationId": "noteSeen"}}
    return m


def _checks(m: Any) -> list[str]:
    a, schemas, examples = CC.load()
    found: list[str] = CC.guarded(CC.run_checks, a, schemas, examples, m)
    return found


def test_an_approval_is_only_a_dedicated_line_after_label_and_option_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _checks(copy.deepcopy(PC.AMENDMENTS)) == []
    m = _scratch(copy.deepcopy(PC.AMENDMENTS))
    assert any("mutating operation refused" in e for e in _checks(m))
    entry = {"method": "post", "path": "/scratch/notes", "operationId": "noteSeen",
             "decision": "2026-10-21#15"}  # fmt: skip
    m["mutations"] = [entry]  # decision 15 has no Approves: line
    assert any("does not approve" in e for e in _checks(m))
    decisions = tmp_path / "docs" / "decisions"
    decisions.mkdir(parents=True)
    a = "   Approves: `POST /scratch/notes` (`noteSeen`)\n"
    q, lab = '   Question: "Approve?"\n', '   Selected label: "Yes".\n'
    o, done = (
        """   Option text as presented: "Do it. Not 'no'."\n""",
        "   Implemented by: x.\n",
    )
    cases = [  # (section body, expected error or None when approved)
        (q + lab + o, "does not approve"),  # must not run into the next section
        (q + lab + o + a + done, None),
        ('   Question: "Approve\n' + a + '   now?"\n' + lab + o + done, "not approve"),
        (q + lab + '   Option text as presented: "Do it.\n' + a + "   Not 'no'.\"\n"
         + done, "does not approve"),  # inside multi-line option text
        (q + lab + a + o + done, "does not approve"),  # before the option text
        (q + o + a + lab + done, "does not approve"),  # before the label
        (q + lab + o + done + a, "does not approve"),  # after Implemented by
        (q + o + a + done, "Selected label"),  # no label
        ('   Question: "Pick\n' + lab + '   one?"\n' + o + a + done,
         "Selected label"),  # a crafted label inside the question
        (q + lab + lab + o + a + done, "Selected label"),  # two labels
        (q + lab + o + "   Approves: `POST /scratch/notes` noteSeen\n" + done,
         "does not approve"),  # malformed
        (q + lab + o + a.replace("noteSeen", "x") + done, "does not approve"),
        (q + lab + o + "\n## Notes\n\n" + a, "does not approve"),  # a heading ends it
    ]  # fmt: skip
    text = "# SYNTHETIC\n\n" + "".join(
        f"{n}. **Case {n}.**\n{body}" for n, (body, _) in enumerate(cases, 1)
    )
    (decisions / "2026-10-21-owner-decisions.md").write_text(text)
    monkeypatch.setattr(CC, "ROOT", tmp_path)
    m["mutations"] = [entry | {"decision": "2026-10-21#2"}]
    header = "/post/parameters/1 parameter X-CSRF-Token"  # rule 2 needs it; rule 7
    assert [e for e in _checks(m) if "X-CSRF-Token: sensitive" in e]  # flags it
    allowed = {"schema": "operation /scratch/notes", "field": header}
    m["sensitive_fields"].append(allowed | {"decision": "SYNTHETIC #2"})
    assert _checks(m) == []
    for n, (_, want) in enumerate(cases, 1):
        m["mutations"] = [entry | {"decision": f"2026-10-21#{n}"}]
        found = _checks(m)
        assert (found == []) if want is None else any(want in e for e in found), n
    login_x = {"name": "unauthenticated_login", "decision": "2026-10-21#2"}
    m["mutations"] = [entry | {"decision": "2026-10-21#2", "exemptions": [login_x]}]
    assert any(  # pinned to createSession, whatever the decision says
        "unauthenticated_login applies only to POST /session (createSession)" in e
        for e in _checks(m)
    )
    m["mutations"] = [entry | {"decision": f"2026-10-21#{len(cases) + 1}"}]
    assert any("cites no owner decision" in e for e in _checks(m))
    m["mutations"] = [entry | {"decision": "2026-10-21#2"}]
    m["operations"]["/scratch/notes"]["post"]["parameters"].pop(0)
    assert any("missing required Idempotency-Key" in e for e in _checks(m))


def test_exemptions_apply_only_to_the_listed_approved_operation() -> None:
    def without(op_id: str, name: str) -> list[str]:
        m = copy.deepcopy(PC.AMENDMENTS)
        for e in m["mutations"]:
            if e["operationId"] == op_id:
                e["exemptions"] = [x for x in e["exemptions"] if x["name"] != name]
        return _checks(m)

    login_errors = without("createSession", "unauthenticated_login")
    for want in ("POST /session: no session security",
                 "POST /session: missing required Idempotency-Key",
                 "POST /session: missing required X-CSRF-Token"):  # fmt: skip
        assert any(want in e for e in login_errors), want
    assert any("response key '204' refused" in e for e in without("revokeSession",
               "no_content"))  # fmt: skip
