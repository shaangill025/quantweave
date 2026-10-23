"""Project contract 0.12.0, listReleases (`GET /releases`, Read B; owner decision 45
of 2026-10-22 with decisions 5, 10, 23, 33, 47 and 48) through TestClient on a
migrated PostgreSQL 16 database as the non-superuser qw_app login. The operator
(qw_release) records the deployment and the test-only key; the worker records and
installs the releases. SYNTHETIC tenants, keys and releases (test_release); the
test-only HMAC key verifies only through the in-process hook."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_release as tr
from fastapi.testclient import TestClient
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import release_store as rs
from qw_adapters import tenancy
from qw_api import Settings, create_app
from qw_domain import release as rl
from qw_domain.instants import format_instant
from qw_domain.research_data import digest
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
worker_conn, release_conn = api._harness.worker_conn, api._harness.release_conn
login, assert_problem = api.login, api.assert_problem
st = _module("qw_release_store_tests",
             ROOT / "packages/adapters/tests/test_release_store.py")  # fmt: skip
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
COMMON = json.loads(
    (ROOT / "docs/spec/contracts/schemas/common.schema.json").read_text()
)
VALIDATOR = Draft202012Validator(
    PC.schema("releasePage"),
    registry=Registry().with_resource(
        "common.schema.json", Resource.from_contents(COMMON)
    ),
    format_checker=FormatChecker(),
)
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
DEP_ID, KEY = "deploy-synth", tr.KEY
HOOK = {KEY.key_id: KEY}  # the in-process test verifier, never stored
TEST_KEY = rs.KeyRecord(KEY.key_id, "maint-synth", KEY.algorithm,
                        tr.TRUST.valid_from, None, None, test_only=True)  # fmt: skip
TABLES = ("release_deployment", "signed_release", "deployment_event",
          "release_trusted_key", "release_key_revocation")  # fmt: skip
HIDDEN = ["none:signature/key_id",
          "none:tenant_adoption/version", "unavailable:candidate_id",
          "unavailable:experiment_id", "unavailable:receipts",
          "unavailable:verdict_id"]  # fmt: skip


def client(w: Any, dep: str | None = DEP_ID, hook: Any = HOOK) -> TestClient:
    settings = Settings(frozenset({api.ORIGIN}), api.KEY, release_deployment_id=dep)
    app = create_app(settings, lambda: tenancy.runtime_connection(w.url), api.AUTH,
                     release_test_verifiers=hook)  # fmt: skip
    return TestClient(app, api.ORIGIN, headers={"Origin": api.ORIGIN},
                      raise_server_exceptions=False)  # fmt: skip


@pytest.fixture
def ledger(world: Any, release_conn: Conn, worker_conn: Conn) -> Iterator[Any]:
    """Tenant A's deployment: rel-1 and rel-2 installed, then rolled back to rel-1."""
    dep = replace(st.DEP, tenants=frozenset({str(world.tenant_a), "tenant-other"}))
    assert rs.record_deployment(release_conn, dep)
    assert rs.record_key(release_conn, TEST_KEY)
    for sr in (tr.signed(), tr.signed("rel-2", (1, 2, 2))):
        assert st.record(worker_conn, sr)
    st.install(worker_conn, "rel-1")
    st.install(worker_conn, "rel-2", 1)
    st.rollback(worker_conn)
    yield world


def releases(c: TestClient, who: str = "bob", **query: Any) -> Any:
    assert login(c, who).status_code == 201
    return c.get("/api/v1/releases", params=query)


def conforms(body: Any) -> None:
    errors = [e.message for e in VALIDATOR.iter_errors(body)]
    assert errors == [], errors


def test_a_member_sees_verified_history_without_identities(ledger: Any) -> None:
    with client(ledger) as c:
        r = releases(c)
        assert r.status_code == 200, r.text
        body = r.json()
        conforms(body)
        one, two = body["items"]
        assert [one["id"], two["id"]] == ["rel-1", "rel-2"]  # (installed_at, id)
        assert one["install"]["installed_at"] < two["install"]["installed_at"]
        assert [x["install"]["state"] for x in (one, two)] == ["head", "withdrawn"]
        for x in (one, two):
            assert x["signature"] == {
                "key_id": None, "algorithm": KEY.algorithm, "reasons": [],
                "signature_reference": x["signature"]["signature_reference"],
                "verification": "verified"}  # fmt: skip
            assert x["approved_at"] is None  # decision 58: unavailable:receipts
            assert (x["install"]["runnable"], x["install"]["self_approved"]) == (
                x is one, False)  # fmt: skip
            assert (x["experiment_id"], x["candidate_id"], x["verdict_id"]) == (
                None, None, None)  # fmt: skip
            assert (x["receipt_status"], x["verdict_source"]) == (
                "unavailable", "caller_supplied")  # fmt: skip
            assert x["tenant_adoption"] == {"state": "not_adopted", "version": None,
                                            "reasons": []}  # fmt: skip
            assert x["coverage"] == {"status": "partial", "reasons": HIDDEN}
        assert two["install"]["reasons"] == ["withdrawn"]
        pair = (tr.signed(), tr.signed("rel-2", (1, 2, 2)))
        signed = {sr.manifest.release_id: sr for sr in pair}
        for x in (one, two):  # decision 55: hash-only, no key id
            sr, ref = signed[x["id"]], x["signature"]["signature_reference"]
            assert ref == f"sig-sha256.{digest(sr.signature)[:32]}"
            assert ref.removeprefix("sig-sha256.") == sr.signature_reference.rsplit(
                ".", 1)[1]  # matches the internal reference's hash part  # fmt: skip
            assert KEY.key_id not in ref and "maint" not in ref
        assert one["signature"]["signature_reference"] != two["signature"][
            "signature_reference"]  # fmt: skip
        assert KEY.key_id not in r.text
        page = {**body, "items": [one]}
        for bad in ({"signature": {**one["signature"], "signature_reference":
                     signed["rel-1"].signature_reference}},  # the internal form
                    {"approved_at": "yesterday"}):  # fmt: skip
            assert list(VALIDATOR.iter_errors({**page, "items": [one | bad]}))
        assert one["strategy_version"] == tr.M1.version and one["change_kind"] == (
            "strategy")  # fmt: skip
        assert body["next_cursor"] is None and body["coverage"]["status"] == "partial"
        assert "unavailable:rel-2/receipts" in body["coverage"]["reasons"]
        assert "operator-synth" not in r.text and "maint-synth" not in r.text
        assert "tenant-other" not in r.text  # no other tenant of the deployment
        first = c.get("/api/v1/releases", params={"limit": 1}).json()
        conforms(first)
        assert [x["id"] for x in first["items"]] == ["rel-1"]
        nxt = c.get("/api/v1/releases", params={"cursor": first["next_cursor"]})
        assert [x["id"] for x in nxt.json()["items"]] == ["rel-2"]
        moved = {"cursor": first["next_cursor"], "component": "strategy:other"}
        assert_problem(c.get("/api/v1/releases", params=moved), 400, "invalid_cursor")
        none = c.get("/api/v1/releases", params={"component": "strategy:other"})
        assert none.json()["items"] == [] and none.json()["coverage"] == {
            "status": "complete", "reasons": []}  # fmt: skip
    with client(ledger) as c:  # an owner reads the same history
        assert releases(c, "alice").json()["items"] == body["items"]


def test_no_deployment_or_no_membership_is_404(ledger: Any) -> None:
    with client(ledger) as c:
        assert_problem(releases(c, "carol"), 404, "not_found")  # tenant B
    for dep in (None, "deploy-unrecorded"):
        with client(ledger, dep) as c:
            assert_problem(releases(c), 404, "not_found")
    with client(ledger) as c:  # an anonymous caller is 401 first
        assert_problem(c.get("/api/v1/releases"), 401, "unauthenticated")


def test_a_test_key_verifies_only_through_the_in_process_hook(ledger: Any) -> None:
    with client(ledger, hook=None) as c:  # the installs cannot replay
        assert_problem(releases(c), 409, "integrity")


def test_a_revoked_key_reads_key_not_valid(ledger: Any, release_conn: Conn) -> None:
    rs.revoke_key(release_conn, KEY.key_id, "SYNTHETIC: rotated")
    with client(ledger) as c:
        body = releases(c).json()
    conforms(body)
    for x in body["items"]:
        assert x["signature"]["verification"] == "refused"
        assert x["signature"]["reasons"] == ["key_not_valid"]
        assert x["install"]["runnable"] is False
        assert "key_not_valid" in x["install"]["reasons"]


def test_tampered_rows_read_integrity(ledger: Any) -> None:
    db = ledger.db

    def raw(sql: str) -> None:  # committed, triggers off (replica role)
        with db.transaction():
            db.execute("SET LOCAL session_replication_role = replica")
            db.execute(sql)

    tampers = (  # each keeps tenant A a member, so only the checks refuse
        ("release_deployment", "tenants = tenants || '{tenant-zz}'::text[]"),
        ("release_deployment", "initial_schema = 2"),
        ("signed_release", "signature = repeat('0', 64)"),
        ("deployment_event", "operator_id = 'maint-synth'"),
        ("release_trusted_key", "principal_id = 'maint-other'"),
    )
    with client(ledger) as c:
        assert login(c, "bob").status_code == 201
        for table, change in tampers:
            db.execute(f"CREATE TABLE public.keep AS SELECT * FROM app.{table}")
            raw(f"UPDATE app.{table} SET {change}")
            r = c.get("/api/v1/releases")
            assert_problem(r, 409, "integrity")
            raw(f"DELETE FROM app.{table}")
            raw(f"INSERT INTO app.{table} SELECT * FROM public.keep")
            db.execute("DROP TABLE public.keep")
            assert c.get("/api/v1/releases").status_code == 200, table


def test_qw_app_has_no_table_access_and_reads_only_as_a_member(
    ledger: Any, app_conn: Conn, worker_conn: Conn
) -> None:
    assert st.record(worker_conn, tr.signed("rel-3", (1, 2, 2)))  # not installed
    for t in TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(f"SELECT 1 FROM app.{t}")
    granted = app_conn.execute("SELECT has_function_privilege("
                               "'app.release_history_member(text)', 'EXECUTE')"
                               ).fetchone()  # fmt: skip
    assert granted == (False,)  # the helper runs only inside the definer reads
    reads = ("deployment", "events", "releases", "keys")
    for fn in reads:  # no tenant set: nothing
        q = f"SELECT count(*) FROM app.release_history_{fn}(%s)"
        assert app_conn.execute(q, (DEP_ID,)).fetchone() == (0,)
    with tenancy.tenant_transaction(app_conn, uuid.uuid4()) as tx:  # not a member
        for fn in reads:
            q = f"SELECT count(*) FROM app.release_history_{fn}(%s)"
            assert tx.conn.execute(q, (DEP_ID,)).fetchone() == (0,)
    with tenancy.tenant_transaction(app_conn, ledger.tenant_a) as tx:
        row = tx.conn.execute("SELECT tenants, hash_ok FROM "
                              "app.release_history_deployment(%s)", (DEP_ID,)
                              ).fetchone()  # fmt: skip
        assert row == ([str(ledger.tenant_a)], True)  # never the other tenant
        named = tx.conn.execute("SELECT release_id FROM app.release_history_releases"
                                "(%s)", (DEP_ID,)).fetchall()  # fmt: skip
        assert named == [("rel-1",), ("rel-2",)]  # only those its events name
        h = rs.history(tx, DEP_ID, HOOK)
        assert h is not None and h.ledger.head(tr.COMP) == "rel-1"


def test_the_database_hash_is_the_store_hash(
    world: Any, app_conn: Conn, conn: Conn
) -> None:
    """0037 re-computes `release_deployment`'s hash in SQL: escaped and non-ASCII
    members, a whole-second time and a row from before 0032 (solo NULL)."""
    a = str(world.tenant_a)
    at = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
    for n, (legacy, ops) in enumerate(((False, ['op"é\\', "op\t1"]), (True, ["o"]))):
        d = rl.Deployment(f"dep-{n}", rl.Edition.HOSTED, frozenset(ops),
                          frozenset({a, "t-✓"}), 1, False)  # fmt: skip
        wire = rs._deployment_wire(d, legacy)
        good = digest(["release_deployment", wire, format_instant(at)])
        with conn.transaction():  # a pre-0032 row: its NOT VALID CHECK lifted here
            c = "release_deployment_solo_maintainer"
            check = conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                 "WHERE conname = %s", (c,)).fetchone()  # fmt: skip
            assert check is not None
            conn.execute(f"ALTER TABLE app.release_deployment DROP CONSTRAINT {c}")
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(
                "INSERT INTO app.release_deployment (deployment_id, edition, "
                "operators, tenants, initial_schema, allows_test_keys, recorded_at, "
                "event_hash, solo_maintainer) VALUES (%s, 'hosted', %s, %s, 1, "
                "false, %s, %s, %s)", (d.deployment_id, sorted(ops),
                                       sorted(d.tenants), at, good,
                                       None if legacy else False))  # fmt: skip
            conn.execute(f"ALTER TABLE app.release_deployment ADD CONSTRAINT {c} "
                         f"{check[0]}")  # fmt: skip
        with tenancy.tenant_transaction(app_conn, world.tenant_a) as tx:
            row = tx.conn.execute("SELECT hash_ok FROM app.release_history_deployment"
                                  "(%s)", (d.deployment_id,)).fetchone()  # fmt: skip
        assert row == (True,), (legacy, ops)
