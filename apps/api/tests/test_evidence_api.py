"""`GET /claims/{claim_id}` (readClaim) and `GET /evidence/{evidence_id}`
(readEvidence) through TestClient on a migrated PostgreSQL 16 database as the
non-superuser qw_app login, over qw_adapters.evidence_store. Reuses the T010 API
harness and the SYNTHETIC scenario of test_evidence_store: c-1 (100 USD) is
supported by the release only (the wire copy deleted, the note withdrawn, the memo
past retention), c-2 (120 USD) disagrees and the resolution preferred c-1. Bodies
are validated strictly against project contract 0.3.0 (tools/project_contract.py),
with date-time formats enforced, and the null material is named in coverage.
The routes read the database clock, so the scenario and the rights are placed
relative to it (`clock_relative`): no expectation depends on the calendar date."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ingest_rights_helper import qualified_registry
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_domain.instants import format_instant, parse_instant
from qw_domain.rights import Registry, Use

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
ev = _module("qw_estore_tests",
             ROOT / "packages/adapters/tests/test_evidence_store.py")  # fmt: skip
rs_tests = _module("qw_rstore_tests",
                   ROOT / "packages/adapters/tests/test_rights_store.py")  # fmt: skip
research = importlib.import_module("qw_api.research")
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
owner = rs_tests.owner
COUNTED = ("evidence_source", "evidence_content", "claim", "claim_evidence")


@pytest.fixture(autouse=True)
def clock_relative(conn: Any, monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Receipt 30 days before the database clock: the release (kept 365 days, the
    helper's KEEP) is retained, the memo (kept one day) expired; every grant ends
    ten years after now. Returns the scenario's receipt time T0."""
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    now: datetime = row[0].astimezone(UTC).replace(microsecond=0)
    until = now + timedelta(days=3650)

    def registry(tenant: str, missing: Use | None = None) -> Registry:
        return qualified_registry(("feed-a", "feed-b"), missing, tenant, until)

    monkeypatch.setattr(ev, "T0", now - timedelta(days=30))
    monkeypatch.setattr(ev, "registry", registry)
    return now - timedelta(days=30)


def conforms(name: str, body: dict[str, Any]) -> None:
    v = Draft202012Validator(PC.schema(name), format_checker=FormatChecker())
    assert [(list(e.absolute_path), e.message) for e in v.iter_errors(body)] == []


def tenant_of(w: Any, user: str) -> uuid.UUID:
    row = w.db.execute(
        "SELECT tenant_id FROM app.membership WHERE user_id = %s", (w.users[user],)
    ).fetchone()
    assert row is not None
    return uuid.UUID(str(row[0]))


def rows(w: Any) -> list[Any]:
    return [w.db.execute(f"SELECT count(*) FROM app.{t}").fetchone() for t in COUNTED]


@pytest.mark.db
def test_claims_and_evidence_are_served_tenant_scoped(
    world: Any, app_conn: Any, clock_relative: datetime
) -> None:
    a, b = world.tenant_a, tenant_of(world, "carol")
    ev.stored(app_conn, a)
    ev.stored(app_conn, b)  # the same ids in tenant B
    world.client.app.state.rights = lambda tx: ev.registry(str(a))
    before = rows(world)
    login(world.client, "bob")  # a member may read
    got = world.client.get("/api/v1/claims/c-1")
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/json"
    one = got.json()
    conforms("claim", one)
    assert {k: one[k] for k in ("id", "kind", "evidence_ids", "counterevidence_ids",
                                 "verification", "material", "calculation_ids")} == {
        "id": "c-1", "kind": "reported_fact", "evidence_ids": ["p-release"],
        "counterevidence_ids": ["p-blog"], "verification": "supported",
        "material": None, "calculation_ids": []}  # fmt: skip
    assert one["instrument_ids"] == [str(ev.E1.uuid)]
    assert one["text"] == (f"us-gaap:Revenues of {ev.E1.uuid} for "
                           "2025-01-01/2025-12-31: 100 USD")  # fmt: skip
    assert one["coverage"]["status"] == "partial"
    assert set(one["coverage"]["reasons"]) == {
        "unknown:material",
        "invalid:p-memo=retention_expired",
        "invalid:p-note=withdrawn",
        "invalid:p-wire=deleted",
        "replay_limited",
    }
    two = world.client.get("/api/v1/claims/c-2").json()
    conforms("claim", two)
    assert (two["verification"], two["evidence_ids"], two["counterevidence_ids"]) == (
        "contested", ["p-blog"], ["p-release"])  # fmt: skip

    release = world.client.get("/api/v1/evidence/p-release").json()
    conforms("evidence", release)
    assert (release["availability"], release["passage"]) == ("available", ev.RELEASE)
    assert (release["source_id"], release["locator"]) == ("s-release", "chars:0-52")
    assert release["rights_profile_id"] == "feed-a"
    received = format_instant(clock_relative)
    assert release["retrieved_at"] == release["published_at"] == received
    assert release["coverage"] == {"status": "complete", "reasons": []}
    for pid, availability, why, text in (
        ("p-note", "unavailable", "withdrawn", "total revenue"),
        ("p-memo", "unavailable", "retention_expired", "SYNTHETIC memo"),
        ("p-wire", "deleted", "deleted", "revenues were"),  # the deleted copy
    ):
        got = world.client.get(f"/api/v1/evidence/{pid}")
        body = got.json()
        conforms("evidence", body)
        assert (body["availability"], body["passage"]) == (availability, None)
        assert body["coverage"] == {"status": "partial", "reasons": [why]}
        assert text not in got.text
    assert body["source_relationship_group"] == release["source_relationship_group"]

    login(world.client, "carol")  # tenant B is served its own c-1
    assert world.client.get("/api/v1/claims/c-1").status_code == 200
    login(world.client, "bob")
    assert_problem(world.client.get("/api/v1/evidence/p-none"), 404, "not_found")
    for path in ("/api/v1/claims/-bad", "/api/v1/evidence/-bad"):
        assert_problem(world.client.get(path), 400, "invalid_request")
    world.client.cookies.clear()
    for path in ("/api/v1/claims/c-1", "/api/v1/evidence/p-release"):
        assert_problem(world.client.get(path), 401, "unauthenticated")
    assert rows(world) == before  # reads write nothing


@pytest.mark.db
def test_display_rights_are_enforced(world: Any, app_conn: Any) -> None:
    a = world.tenant_a
    ev.stored(app_conn, a)
    login(world.client, "alice")
    # The server default: no rights registry is stored, so nothing may be shown.
    got = world.client.get("/api/v1/evidence/p-release")
    body = got.json()
    conforms("evidence", body)
    assert (body["availability"], body["passage"]) == ("restricted", None)
    assert ev.RELEASE not in got.text
    assert body["coverage"]["reasons"] == ["restricted:not_qualified"]
    claim = world.client.get("/api/v1/claims/c-1").json()
    conforms("claim", claim)
    assert (claim["verification"], claim["evidence_ids"]) == ("unavailable", [])
    assert "invalid:p-release=rights_revoked" in claim["coverage"]["reasons"]
    assert_withheld(world.client.get("/api/v1/claims/c-1"))
    # A registry granting every use but display: still withheld.
    world.client.app.state.rights = lambda tx: ev.registry(str(a), Use.DISPLAY)
    got = world.client.get("/api/v1/evidence/p-release")
    assert (got.json()["availability"], got.json()["passage"]) == ("restricted", None)
    assert got.json()["coverage"]["reasons"] == ["restricted:right_unknown"]
    assert ev.RELEASE not in got.text
    got = world.client.get("/api/v1/claims/c-1")
    assert got.json()["verification"] == "supported"  # derived-data use is granted
    assert got.json()["evidence_ids"] == ["p-release"]
    assert_withheld(got)  # owner decision 2026-10-15: its text is not displayable
    login(world.client, "carol")  # tenant B: A's ids are the same 404 as unknown
    for kind, known in (("claims", "c-1"), ("evidence", "p-release")):
        foreign = assert_problem(world.client.get(f"/api/v1/{kind}/{known}"), 404,
                                 "not_found")  # fmt: skip
        unknown = assert_problem(world.client.get(f"/api/v1/{kind}/x-none"), 404,
                                 "not_found")  # fmt: skip
        assert {**foreign, "correlation_id": ""} == {**unknown, "correlation_id": ""}


def assert_withheld(got: Any) -> None:
    """A claim whose cited evidence may not be displayed: status, ids and support
    stay; the text is empty and named in coverage (owner decision 2026-10-15)."""
    assert got.status_code == 200, got.text
    body = got.json()
    conforms("claim", body)
    assert body["text"] == ""
    assert "100 USD" not in got.text and "us-gaap:Revenues" not in got.text
    assert body["coverage"]["status"] == "partial"
    assert f"withheld:{body['id']}/text:rights" in body["coverage"]["reasons"]
    assert "unknown:material" in body["coverage"]["reasons"]


@pytest.mark.db
def test_claim_text_follows_display_rights_of_every_cited_source(
    world: Any, app_conn: Any, clock_relative: datetime
) -> None:
    """Only feed-a is qualified. c-2 cites only feed-a evidence, so its text is
    shown; c-1 also cites the (deleted) wire copy on feed-b, whose display is not
    granted, so its text is withheld although the release still supports it."""
    a = world.tenant_a
    ev.stored(app_conn, a)
    until = clock_relative + timedelta(days=3650)
    world.client.app.state.rights = lambda tx: qualified_registry(
        ("feed-a",), None, str(a), until)  # fmt: skip
    login(world.client, "bob")  # a member may read
    two = world.client.get("/api/v1/claims/c-2").json()
    conforms("claim", two)
    assert two["text"] == (f"us-gaap:Revenues of {ev.E1.uuid} for "
                           "2025-01-01/2025-12-31: 120 USD")  # fmt: skip
    assert not [r for r in two["coverage"]["reasons"] if r.startswith("withheld:")]
    got = world.client.get("/api/v1/claims/c-1")
    assert (got.json()["verification"], got.json()["evidence_ids"]) == (
        "supported", ["p-release"])  # fmt: skip
    assert_withheld(got)
    world.client.app.state.rights = lambda tx: ev.registry(str(a))  # both feeds
    shown = world.client.get("/api/v1/claims/c-1").json()
    assert shown["text"].endswith(": 100 USD")
    assert not [r for r in shown["coverage"]["reasons"] if r.startswith("withheld:")]


@pytest.mark.db
def test_a_tampered_store_is_a_crash_not_an_answer(world: Any, app_conn: Any) -> None:
    ev.stored(app_conn, world.tenant_a)
    world.client.app.state.rights = lambda tx: ev.registry(str(world.tenant_a))
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.evidence_content SET content = "
                     "replace(content, '100', '999')")  # fmt: skip
    login(world.client, "alice")
    for path in ("/api/v1/evidence/p-release", "/api/v1/claims/c-1"):
        got = world.client.get(path)
        assert_problem(got, 500, "internal_error")
        assert "999" not in got.text


@pytest.mark.db
def test_now_is_read_after_the_lock(
    world: Any, app_conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`valid_at` is the database clock taken after the store's lock is held, in
    the same transaction: never before a state the claim was evaluated on."""
    a = world.tenant_a
    ev.stored(app_conn, a)
    world.client.app.state.rights = lambda tx: ev.registry(str(a))
    real, locked = research.evidence_store.load, []

    def load(tx: Any, **kw: Any) -> Any:
        store = real(tx, **kw)
        locked.append(tx.conn.execute("SELECT clock_timestamp()").fetchone()[0])
        return store

    monkeypatch.setattr(research.evidence_store, "load", load)
    login(world.client, "alice")
    body = world.client.get("/api/v1/claims/c-1").json()
    assert parse_instant(body["valid_at"]) >= locked[0]


@pytest.mark.db
def test_stored_rights_are_loaded_per_request(
    world: Any, app_conn: Any, owner: Any
) -> None:
    """The server default reads the registry store in the request's tenant
    transaction: a SYNTHETIC qualified feed and entitlement recorded by the owner
    serve the text; another tenant without an entitlement is still restricted;
    a later revocation row takes effect on the next request; a tampered registry
    is a 500, never an answer."""
    a, b = world.tenant_a, tenant_of(world, "carol")
    ev.stored(app_conn, a)
    ev.stored(app_conn, b)
    rs_tests.grant_all(owner, a, ("feed-a", "feed-b"))
    login(world.client, "alice")
    got = world.client.get("/api/v1/evidence/p-release")
    assert (got.json()["availability"], got.json()["passage"]) == (
        "available", ev.RELEASE)  # fmt: skip
    assert world.client.get("/api/v1/claims/c-1").json()["verification"] == (
        "supported")  # fmt: skip
    login(world.client, "carol")
    body = world.client.get("/api/v1/evidence/p-release").json()
    assert (body["availability"], body["coverage"]["reasons"]) == (
        "restricted", ["restricted:not_entitled"])  # fmt: skip
    login(world.client, "alice")
    with api.tenancy.tenant_transaction(owner, a) as tx:
        held = rs_tests.rs.load(tx).entitlements[(str(a), "feed-a")]
        last = held.current
        assert last is not None
        rs_tests.rs.record_entitlement(tx, held.revise(
            last.source, False, last.rights, None, rs_tests.clock(owner)))  # fmt: skip
    got = world.client.get("/api/v1/evidence/p-release")
    assert (got.json()["availability"], got.json()["passage"]) == ("restricted", None)
    assert ev.RELEASE not in got.text
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.rights_entitlement SET active = true")
    got = world.client.get("/api/v1/evidence/p-release")
    assert_problem(got, 500, "internal_error")
    assert ev.RELEASE not in got.text
