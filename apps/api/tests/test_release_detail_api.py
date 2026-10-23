"""Project contract 0.13.0, the rest of Read B: readRelease (`GET /releases/{id}`)
and listDeploymentEvents (`GET /deployment/events`), with the disclosure rules of
0.12.0 (owner decisions 10 and 55 to 58). Reuses the 0.12.0 harness and its
SYNTHETIC ledger (test_releases_api.py): rel-1 and rel-2 installed, then rolled
back to rel-1; the definer reads of 0039 return explicit columns only."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_release as tr
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import release_store as rs
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rel = _module("qw_releases_api_tests", ROOT / "apps/api/tests/test_releases_api.py")
pg_admin_url, database_url, conn = rel.pg_admin_url, rel.database_url, rel.conn
runtime_urls, app_conn, world = rel.runtime_urls, rel.app_conn, rel.world
worker_conn, release_conn, ledger = rel.worker_conn, rel.release_conn, rel.ledger
client, login, assert_problem, st = rel.client, rel.login, rel.assert_problem, rel.st
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
REGISTRY = Registry().with_resource("common.schema.json",
                                    Resource.from_contents(rel.COMMON))  # fmt: skip
FC = FormatChecker()
EVENTS = Draft202012Validator(rel.PC.schema("deploymentEventPage"),
                              registry=REGISTRY, format_checker=FC)  # fmt: skip
PAGE = rel.PC.schema("releasePage")
ITEM = {"$defs": PAGE["$defs"], "$ref": "#/$defs/Release"}
RELEASE = Draft202012Validator(ITEM, registry=REGISTRY, format_checker=FC)
WHO = ["none:operator_id", "none:step_up_receipt_id"]  # decision 10 (Q5)


def ok(v: Any, body: Any) -> None:
    errors = [e.message for e in v.iter_errors(body)]
    assert errors == [], errors


def get(c: Any, path: str, **query: Any) -> Any:
    return c.get(f"/api/v1{path}", params=query)


def test_read_release_is_exactly_the_list_item(ledger: Any, worker_conn: Conn) -> None:
    assert st.record(worker_conn, tr.signed("rel-3", (1, 2, 2)))  # not installed
    with client(ledger) as c:
        listed = rel.releases(c).json()["items"]
        for item in listed:
            r = get(c, f"/releases/{item['id']}")
            assert r.status_code == 200, r.text
            ok(RELEASE, r.json())
            assert r.json() == item  # same disclosure rules (decisions 10, 55-58)
        for unknown in ("rel-3", "rel-9"):  # recorded but not installed: unknown
            assert_problem(get(c, f"/releases/{unknown}"), 404, "not_found")
        assert_problem(get(c, "/releases/bad id"), 400, "invalid_request")
    ok_200 = rel.PC.openapi()["paths"]["/releases/{release_id}"]["get"]["responses"]
    schema = ok_200["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Release"}  # the list's item
    with client(ledger) as c:
        assert login(c, "carol").status_code == 201  # tenant B: not a member
        assert_problem(get(c, "/releases/rel-1"), 404, "not_found")
    for dep in (None, "deploy-unrecorded"):
        with client(ledger, dep) as c:
            assert login(c, "bob").status_code == 201
            assert_problem(get(c, "/releases/rel-1"), 404, "not_found")


def _pause(worker_conn: Conn) -> None:
    """Roll back again from rel-1: rel-2 is withdrawn, so a pause is appended."""
    rs.rollback(worker_conn, st.DEP.deployment_id, tr.KEYS, "ev-p", tr.COMP,
                "operator-synth", "stepup-op-p", strategies=tr.NO)  # fmt: skip


def test_events_in_ledger_order_without_identities(
    ledger: Any, worker_conn: Conn
) -> None:
    _pause(worker_conn)
    assert st.record(worker_conn, tr.signed("rel-3"))  # (1, 2, 1): writes schema 1
    st.install(worker_conn, "rel-3", 3)  # so the state schema stays at 2
    with client(ledger) as c:
        installs = {x["id"]: x["install"]["installed_at"]
                    for x in rel.releases(c).json()["items"]}  # fmt: skip
        r = get(c, "/deployment/events")
        assert r.status_code == 200, r.text
        body = r.json()
        ok(EVENTS, body)
        got = [(e["id"], e["kind"], e["release_id"], e["from_release_id"],
                e["to_release_id"], e["reasons"], e["schema_after"])
               for e in body["items"]]  # fmt: skip
        assert got == [
            ("ev-0", "install", "rel-1", None, None, [], 1),
            ("ev-1", "install", "rel-2", None, None, [], 2),
            ("ev-rb", "rollback", None, "rel-2", "rel-1", [], 2),
            ("ev-p", "pause", "rel-1", None, None, ["rel-2:withdrawn"], 2),
            ("ev-3", "install", "rel-3", None, None, [], 2),  # never downgraded
        ]
        ats = [e["at"] for e in body["items"]]
        assert ats == sorted(ats) and ats[:2] == [installs["rel-1"], installs["rel-2"]]
        for e in body["items"]:
            assert (e["operator_id"], e["step_up_receipt_id"]) == (None, None)
            assert e["component"] == tr.COMP
            missing = (["none:release_id"] if e["kind"] == "rollback" else
                       ["none:from_release_id", "none:to_release_id"])  # fmt: skip
            assert e["coverage"] == {"status": "partial",
                                     "reasons": sorted([*WHO, *missing])}  # fmt: skip
        assert "none:ev-rb/release_id" in body["coverage"]["reasons"]
        assert "none:ev-0/operator_id" in body["coverage"]["reasons"]
        for secret in ("operator-synth", "maint-synth", "stepup"):
            assert secret not in r.text
        first = get(c, "/deployment/events", limit=3).json()
        ok(EVENTS, first)
        assert [e["id"] for e in first["items"]] == ["ev-0", "ev-1", "ev-rb"]
        rest = get(c, "/deployment/events", cursor=first["next_cursor"]).json()
        assert [e["id"] for e in rest["items"]] == ["ev-p", "ev-3"]
        assert rest["next_cursor"] is None
        moved = {"cursor": first["next_cursor"], "component": "strategy:other"}
        assert_problem(get(c, "/deployment/events", **moved), 400, "invalid_cursor")
        none = get(c, "/deployment/events", component="strategy:other").json()
        assert none["items"] == [] and none["coverage"]["status"] == "complete"
        only = get(c, "/deployment/events", component=tr.COMP).json()
        assert only["items"] == body["items"]


def test_events_are_refused_like_the_list(ledger: Any) -> None:
    with client(ledger) as c:
        assert login(c, "carol").status_code == 201
        assert_problem(get(c, "/deployment/events"), 404, "not_found")
    for dep in (None, "deploy-unrecorded"):
        with client(ledger, dep) as c:
            assert login(c, "bob").status_code == 201
            assert_problem(get(c, "/deployment/events"), 404, "not_found")
    with client(ledger, hook=None) as c:  # the test key verifies only via the hook
        assert login(c, "bob").status_code == 201
        assert_problem(get(c, "/deployment/events"), 409, "integrity")
        assert_problem(get(c, "/releases/rel-1"), 409, "integrity")
    db = ledger.db
    with db.transaction():  # an event row changed with triggers off
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute("UPDATE app.deployment_event SET operator_id = 'maint-synth' "
                   "WHERE seq = 1")  # fmt: skip
    with client(ledger) as c:
        assert login(c, "bob").status_code == 201
        assert_problem(get(c, "/deployment/events"), 409, "integrity")
        assert_problem(get(c, "/releases/rel-1"), 409, "integrity")


COLUMNS = {  # the reviewer's note on 0037: explicit columns, never whole rows
    "deployment": ["deployment_id", "edition", "operators", "tenants",
                   "initial_schema", "allows_test_keys", "solo_maintainer",
                   "recorded_at", "hash_ok"],
    "events": ["deployment_id", "seq", "event_id", "kind", "component",
               "release_id", "to_release_id", "reasons", "operator_id",
               "step_up_receipt_id", "at", "schema_after", "self_approved",
               "event_hash"],
    "releases": ["release_id", "manifest_hash", "key_id", "algorithm", "signature",
                 "body", "approved_at", "verdict_source", "recorded_at",
                 "event_hash"],
    "keys": ["key_id", "principal_id", "algorithm", "public_key", "test_only",
             "valid_from", "valid_until", "recorded_at", "event_hash"],
    "revocations": ["key_id", "reason", "revoked_at", "event_hash"],
}  # fmt: skip


def test_definer_reads_return_explicit_columns(ledger: Any, conn: Conn) -> None:
    for fn, cols in COLUMNS.items():
        row = conn.execute(
            "SELECT p.prorettype::regtype::text, array(SELECT n FROM unnest("
            "p.proargnames, p.proargmodes) AS a (n, m) WHERE m = 't') FROM pg_proc p "
            "WHERE p.oid = %s::regprocedure", (f"app.release_history_{fn}(text)",)
        ).fetchone()  # fmt: skip
        assert row == ("record", cols), fn


def _bare(r: Any) -> tuple[int, bytes, dict[str, str]]:
    """Status, the exact body bytes with the per-request correlation id blanked,
    and the headers except that id: everything a caller could compare."""
    cid = r.headers["x-correlation-id"]
    assert r.content.count(cid.encode()) == 1  # the only varying byte run
    headers = {k: v for k, v in r.headers.items()
               if k not in ("x-correlation-id", "date")}  # fmt: skip
    return r.status_code, r.content.replace(cid.encode(), b"CID"), headers


def test_every_404_is_byte_identical(ledger: Any, worker_conn: Conn) -> None:
    """Review F1: an unknown or uninstalled release must not answer differently
    from a non-member or a missing deployment, or the body tells a caller whether
    its tenant is in the deployment."""
    assert st.record(worker_conn, tr.signed("rel-3", (1, 2, 2)))  # not installed
    seen = []
    with client(ledger) as c:
        assert login(c, "bob").status_code == 201
        seen += [get(c, "/releases/rel-9"), get(c, "/releases/rel-3")]
    with client(ledger) as c:
        assert login(c, "carol").status_code == 201  # tenant B: not a member
        seen += [get(c, p) for p in ("/releases/rel-1", "/releases",
                                     "/deployment/events")]  # fmt: skip
    for dep in (None, "deploy-unrecorded"):
        with client(ledger, dep) as c:
            assert login(c, "bob").status_code == 201
            seen += [get(c, p) for p in ("/releases/rel-1", "/releases",
                                         "/deployment/events")]  # fmt: skip
    bodies = set()
    for r in seen:
        _, body, headers = _bare(r)
        bodies.add((body, tuple(sorted(headers.items()))))
    assert len(bodies) == 1 and {r.status_code for r in seen} == {404}
    assert_problem(seen[0], 404, "not_found")
