"""`POST /feedback` on PostgreSQL 16 as qw_app (T010 harness), SYNTHETIC data;
bodies checked against the contract `Feedback` and `job.schema.json`."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from httpx import Response
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import feedback_store as fs
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_domain import feedback as fb
from qw_domain.decimals import Money
from qw_domain.simulation import VirtualPortfolio
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]
SPEC = ROOT / "docs/spec/contracts"


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
rd = _module("qw_reads_api_tests", ROOT / "apps/api/tests/test_reads_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, csrf, assert_problem = api.login, api.csrf, api.assert_problem
COMMON = json.loads((SPEC / "schemas/common.schema.json").read_text())
JOB = Draft202012Validator(
    json.loads((SPEC / "schemas/job.schema.json").read_text()),
    registry=Registry().with_resource("common.schema.json",
                                      Resource.from_contents(COMMON)),
    format_checker=FormatChecker(),
)  # fmt: skip
REQUEST = Draft202012Validator(
    yaml.safe_load((SPEC / "openapi.yaml").read_text())["components"]["schemas"][
        "Feedback"])  # fmt: skip
SIM = "SIM-syn-fb-1"
TABLES = ("feedback", "feedback_content", "feedback_tombstone")


def body(object_id: str = SIM, **kw: Any) -> dict[str, Any]:
    return {"object_id": object_id, "category": "incorrect_fact",
            "message": "SYNTHETIC the drawdown figure looks wrong",
            "cross_user_contribution_consent": False} | kw  # fmt: skip


def post(w: Any, b: dict[str, Any], key: str | None = None,
         headers: dict[str, str] | None = None) -> Response:  # fmt: skip
    h = csrf(w.client, key) if headers is None else headers
    return w.client.post("/api/v1/feedback", json=b, headers=h)  # type: ignore[no-any-return]


def counts(w: Any) -> list[int]:
    return [int(w.db.execute(f"SELECT count(*) FROM app.{t}").fetchone()[0])
            for t in TABLES]  # fmt: skip


def simulation(w: Any, tenant: uuid.UUID, sid: str = SIM) -> ss.Simulation:
    now = w.db.execute("SELECT now()").fetchone()[0]
    p = VirtualPortfolio.open(sid, Money.of("500", "USD"), now - timedelta(days=1))
    s = ss.Simulation("SYNTHETIC feedback", "exec-synth-1", "bench-synth-1", p)
    with tn.runtime_connection(w.url) as c, tn.tenant_transaction(c, tenant) as tx:
        ss.save(tx, s)
    return s


def evidence(w: Any, tenant: uuid.UUID) -> fb.ImprovementEvidence:
    with tn.runtime_connection(w.url) as c, tn.tenant_transaction(c, tenant) as tx:
        return fs.load(tx)


@pytest.mark.db
def test_feedback_is_recorded_once_per_key_and_conforms(world: Any) -> None:
    s = simulation(world, world.tenant_a)
    login(world.client, "bob")  # a member: no step-up needed
    assert list(REQUEST.iter_errors(body())) == []
    before = world.db.execute("SELECT clock_timestamp()").fetchone()[0]
    key = "SYNTHETIC-feedback-0001"
    first = post(world, body(), key)
    assert first.status_code == 202, first.text
    assert first.headers["content-type"] == "application/json"
    job = first.json()
    assert [e.message for e in JOB.iter_errors(job)] == []
    assert (job["state"], job["kind"], job["idempotency_key"], job["reasons"]) == (
        "succeeded", "feedback.record", key, [])  # fmt: skip
    assert job["result_reference"] == job["id"]
    replay = post(world, body(), key)
    assert replay.status_code == 202 and replay.content == first.content
    assert replay.headers["idempotent-replayed"] == "true"
    other = post(world, body(message="SYNTHETIC something else"), key)
    assert_problem(other, 409, "idempotency_key_reused")
    assert counts(world) == [1, 1, 0]
    [rec], held = evidence(world, world.tenant_a).export_view(str(world.tenant_a))
    assert held == () and isinstance(rec, fb.Feedback)
    assert (rec.record_id, rec.principal_id, rec.category.value, rec.message) == (
        job["id"], str(world.users["bob"]), "incorrect_fact",
        body()["message"])  # fmt: skip
    assert rec.subject == fb.Subject(fb.SubjectKind.SIMULATION, SIM, 1, ss.digest(s))
    after = world.db.execute("SELECT clock_timestamp()").fetchone()[0]
    assert before <= rec.recorded_at <= after  # the DB clock
    audited = api.audit(world, "feedback.recorded")
    assert [a[:3] for a in audited] == [(world.tenant_a, world.users["bob"],
                                         uuid.UUID(job["id"]))]  # fmt: skip
    logged = str(world.db.execute("SELECT * FROM app.audit_event").fetchall())
    assert "drawdown" not in logged and SIM not in logged  # ids only, no text
    login(world.client, "carol")  # tenant B: A's simulation is just unknown
    foreign = assert_problem(post(world, body()), 404, "not_found")
    unknown = assert_problem(post(world, body("SIM-unknown")), 404, "not_found")
    assert {**foreign, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    assert counts(world) == [1, 1, 0]


@pytest.mark.db
def test_refusals_write_nothing(world: Any) -> None:
    simulation(world, world.tenant_a)
    anonymous = post(world, body(), headers={"Idempotency-Key": "x" * 16})
    assert_problem(anonymous, 401, "unauthenticated")
    login(world.client, "alice")
    no_csrf = post(world, body(), headers={"Idempotency-Key": "x" * 16})
    assert_problem(no_csrf, 403, "csrf_failed")
    no_key = csrf(world.client)
    del no_key["Idempotency-Key"]
    assert_problem(post(world, body(), headers=no_key), 400, "invalid_request")
    bad_key = post(world, body(), "short")
    assert_problem(bad_key, 400, "invalid_idempotency_key")
    for bad in (body(cross_user_contribution_consent=True), body(category="thumbs_up"),
                body(message="x" * 4001), body(tenant_id=str(world.tenant_a)),
                body(object_id="../x"), {k: v for k, v in body().items()
                                          if k != "message"}):  # fmt: skip
        assert list(REQUEST.iter_errors(bad)), bad  # the contract refuses it too
        assert_problem(post(world, bad), 400, "invalid_request")
    assert_problem(post(world, body(message="a\x00b")), 400, "invalid_request")
    assert counts(world) == [0, 0, 0]
    assert post(world, body(message="x" * 4000)).status_code == 202  # the limit


@pytest.mark.db
def test_proposals_bind_their_latest_version_and_store_keys_outlive_purges(
    world: Any, app_conn: Any
) -> None:
    c, snap = rd.seed_proposals(world, app_conn)
    two = replace(rd.ps.ver(app_conn, c, snap, "prop-syn-live", 70), version=2)
    assert rd.ps.run(app_conn, c, "revise", "prop-syn-live", two, snap).accepted
    login(world.client, "alice")
    key, b = "SYNTHETIC-feedback-0002", body("prop-syn-live")
    first = post(world, b, key).json()
    digest = world.db.execute(
        "SELECT content_hash FROM app.proposal_version WHERE proposal_id = "
        "'prop-syn-live' ORDER BY version DESC LIMIT 1").fetchone()[0]  # fmt: skip
    [rec], _ = evidence(world, world.tenant_a).export_view(str(world.tenant_a))
    assert rec.subject == fb.Subject(fb.SubjectKind.PROPOSAL, "prop-syn-live", 2,
                                     digest)  # fmt: skip
    world.db.execute("DELETE FROM app.idempotency_record")  # retention purge
    again = post(world, b, key)
    assert again.status_code == 202 and "idempotent-replayed" not in again.headers
    assert again.json()["id"] == first["id"]  # the same record, its own time
    assert again.json()["created_at"] == first["created_at"]
    world.db.execute("DELETE FROM app.idempotency_record")
    assert_problem(post(world, b | {"category": "clear"}, key), 409,
                   "idempotency_key_reused")  # fmt: skip
    assert post(world, b, key).status_code == 202  # the API record is stored again
    with tn.runtime_connection(world.url) as c, tn.tenant_transaction(
            c, world.tenant_a) as tx:  # fmt: skip
        fs.delete(tx, principal=world.users["alice"], record_id=first["id"])
    assert_problem(post(world, b, key), 409, "deleted")  # before the purge (N2)
    world.db.execute("DELETE FROM app.idempotency_record")
    assert_problem(post(world, b, key), 409, "deleted")
    simulation(world, world.tenant_a, "prop-syn-live")  # an id of both kinds (N5)
    assert_problem(post(world, b), 409, "ambiguous_object")
    assert counts(world) == [1, 0, 1]
