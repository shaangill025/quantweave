"""The privacy deletion request route (T058 increment 9; owner decisions 113, 127,
128): `POST /privacy/deletions` (requestDeletion) and `GET /privacy/deletions/
{deletion_id}` (readDeletion), through TestClient on a migrated PostgreSQL 16 as
the non-superuser qw_app login, with the real qw_worker `retention.delete` job.
Tenants, users, simulations, holds, principals, receipts and the record key are
SYNTHETIC. Bodies are checked against the spec `job` schema and the project
contract's `deletion_status`."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import deletion_requests as dr
from qw_adapters import legal_hold_store as hs
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_domain import retention as rt
from qw_domain import simulation as sim
from qw_domain.decimals import Money
from qw_domain.jobs import Backoff, JobState
from qw_domain.risk import Side
from qw_domain.simulation import OrderStyle
from qw_worker import deletion_job as dj
from qw_worker.config import WorkerSettings
from qw_worker.loop import Worker
from referencing import Registry, Resource
from test_simulation import LATE, T0, at, order, run

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
worker_conn = api._harness.worker_conn
login, csrf, step_up, assert_problem = (
    api.login,
    api.csrf,
    api.step_up,
    api.assert_problem,
)
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
SCHEMAS = ROOT / "docs/spec/contracts/schemas"
REGISTRY = Registry().with_resources(
    [(n, Resource.from_contents(json.loads((SCHEMAS / n).read_text())))
     for n in ("common.schema.json", "job.schema.json")]
)  # fmt: skip
FORMATS = FormatChecker()
JOB = Draft202012Validator(json.loads((SCHEMAS / "job.schema.json").read_text()),
                           registry=REGISTRY, format_checker=FORMATS)  # fmt: skip
STATUS = Draft202012Validator(PC.schema("deletion_status"), registry=REGISTRY,
                              format_checker=FORMATS)  # fmt: skip
REQUEST = Draft202012Validator(
    PC.openapi()["components"]["schemas"]["DeleteRequest"], format_checker=FORMATS
)
URL = "/api/v1/privacy/deletions"
C, R = rt.RetentionClass, rt.HoldRole
KEY = rt.RecordKey(b"SYNTHETIC-test-record-key-route-0123456789")  # test only
SETTINGS = WorkerSettings(
    "synthetic-delete-route", job_backoff=Backoff(timedelta(0), timedelta(0))
)
ACK = "SYNTHETIC: I understand deletion cannot be undone."
POLICY = rt.owner_defaults()
KEEP_SIM = rt.Policy("synthetic-keep-sim", 1, {**POLICY.rules, C.SIMULATION: rt.Rule(
    C.SIMULATION, rt.OnRequest.RETAIN, None, "synthetic-authority",
    "synthetic-basis")})  # an operator who retains simulations  # fmt: skip
pytestmark = pytest.mark.db


def body(*classes: str, scope: str = "tenant", ack: str = ACK) -> dict[str, Any]:
    doc = {"scope_id": scope, "classes": list(classes),
           "reauthentication_receipt_id": "SYNTHETIC-client-receipt-1",
           "acknowledgement": ack}  # fmt: skip
    assert [e.message for e in REQUEST.iter_errors(doc)] == []  # the contract body
    return doc


def post(w: Any, doc: dict[str, Any], key: str | None = None) -> Any:
    return w.client.post(URL, json=doc, headers=csrf(w.client, key))


def owner(w: Any, who: str = "alice") -> None:
    assert login(w.client, who).status_code == 201
    assert step_up(w.client).status_code == 200


def jobs(w: Any, tenant: uuid.UUID | None = None) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in w.db.execute(
        "SELECT id, tenant_id, input_revision, state::text, last_error, payload "
        "FROM app.job WHERE kind = %s AND (%s::uuid IS NULL OR tenant_id = %s) "
        "ORDER BY created_at, input_revision", (dj.KIND, tenant, tenant))]  # fmt: skip


def audits(w: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in w.db.execute(
        "SELECT tenant_id, actor_user_id, target_id, correlation_id FROM "
        "app.audit_event WHERE action = 'privacy.deletion_requested' ORDER BY "
        "occurred_at")]  # fmt: skip


def details(w: Any) -> list[Any]:
    return [r[0] for r in w.db.execute(
        "SELECT detail FROM app.audit_event WHERE action = "
        "'privacy.deletion_requested' ORDER BY occurred_at")]  # fmt: skip


def same_session(w: Any, **settings: Any) -> Any:
    """Another app (other settings) on the world client's session."""
    other = api.client_for(w.url, **settings)
    other.cookies.set(api.SESSION_COOKIE, w.client.cookies[api.SESSION_COOKIE])
    return other


def read(w: Any, deletion_id: str) -> Any:
    return w.client.get(f"{URL}/{deletion_id}")


def status(w: Any, deletion_id: str) -> dict[str, Any]:
    response = read(w, deletion_id)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    doc: dict[str, Any] = response.json()
    assert [e.message for e in STATUS.iter_errors(doc)] == []
    return doc


def simulation(sid: str) -> ss.Simulation:
    p = sim.VirtualPortfolio.open(sid, Money.of("100000", "USD"), T0)
    s = ss.Simulation("SYNTHETIC sim", "exec-synth-1", "bench-synth-1", p)
    return s.run(run(p, order("o1", Side.BUY, "100", OrderStyle.MARKET,
                              at(13, 29))), LATE)  # fmt: skip


def tick(c: Any, targets: Any = dj.TARGETS) -> list[tuple[uuid.UUID, JobState | None]]:
    handlers = dj.handlers(KEY, POLICY, targets)
    return list(Worker(c, SETTINGS, handlers=handlers).tick().jobs)


def test_the_route_and_the_job_share_one_registry() -> None:
    """Decision 127: the route accepts exactly the classes the job can delete."""
    assert dr.SUPPORTED == frozenset(dj.TARGETS) == {C.EVIDENCE, C.SIMULATION}
    assert C.SECURITY not in dr.SUPPORTED  # decision 128: never by class request
    for classes, policy, code, indexes in (
            (["simulation", "research", "security"], POLICY,
             "account_closure_required", (2,)),
            (["research", "simulation", "policy"], POLICY,
             "deletion_not_supported", (0, 2)),
            (["evidence", "simulation"], KEEP_SIM, "retained_by_policy", (1,)),
            (["evidence", "simulation"], rt.spec_baseline("synthetic-unset"),
             "retained_by_policy", (0, 1))):  # decision 137  # fmt: skip
        with pytest.raises(dr.RequestRefused) as refused:
            dr.check(classes, "tenant", policy)
        assert (refused.value.code, refused.value.indexes) == (code, indexes)
    assert dr.check(["simulation", "evidence"], "tenant", POLICY) == frozenset(
        {C.SIMULATION, C.EVIDENCE})  # fmt: skip


def test_filing_needs_an_owner_with_a_fresh_step_up_and_csrf(world: Any) -> None:
    doc = body("simulation")
    anonymous = {"X-CSRF-Token": "0" * 64, "Idempotency-Key": uuid.uuid4().hex}
    assert_problem(world.client.post(URL, json=doc, headers=anonymous), 401,
                   "unauthenticated")  # fmt: skip
    assert login(world.client, "bob").status_code == 201
    assert step_up(world.client).status_code == 200
    assert_problem(post(world, doc), 403, "forbidden")  # a member, stepped up
    assert_problem(read(world, str(uuid.uuid4())), 403, "forbidden")
    assert login(world.client, "alice").status_code == 201
    assert_problem(post(world, doc), 401, "step_up_required")
    assert step_up(world.client).status_code == 200
    no_csrf = world.client.post(
        URL, json=doc, headers={"Idempotency-Key": uuid.uuid4().hex}
    )
    assert_problem(no_csrf, 403, "csrf_failed")
    bad = {"X-CSRF-Token": "0" * 64, "Idempotency-Key": uuid.uuid4().hex}
    assert_problem(world.client.post(URL, json=doc, headers=bad), 403, "csrf_failed")
    tiny = timedelta(microseconds=1)  # a step-up older than the window
    with api.client_for(world.url, step_up_window=tiny) as stale:
        assert login(stale, "alice").status_code == 201
        assert step_up(stale).status_code == 200
        response = stale.post(URL, json=doc, headers=csrf(stale))
        assert_problem(response, 401, "step_up_required")
    assert jobs(world) == [] and audits(world) == []


def test_unsupported_classes_identity_and_other_scopes_are_refused(world: Any) -> None:
    owner(world)
    key = "SYNTHETIC-deletion-refused-1"
    for classes in (["research"], ["simulation", "policy"], ["operations"],
                    ["financial"], ["privacy"], ["backup"]):  # fmt: skip
        got = assert_problem(post(world, body(*classes), key), 422,
                             "deletion_not_supported")  # fmt: skip
        assert "not yet supported" in got["detail"]
        bad = [n for n, c in enumerate(classes) if c not in ("simulation",)]
        assert [(e["pointer"], e["code"]) for e in got["field_errors"]] == [
            (f"/classes/{n}", "not_yet_supported") for n in bad]  # fmt: skip
    every = [c.value for c in C if c not in dr.SUPPORTED and c is not C.SECURITY]
    assert_problem(post(world, body(*every), key), 422, "deletion_not_supported")
    for classes in (["security"], ["research", "security"], ["simulation", "security"]):
        got = assert_problem(post(world, body(*classes), key), 422,
                             "account_closure_required")  # fmt: skip
        assert "account closure" in got["detail"]
        assert [e["pointer"] for e in got["field_errors"]] == [
            f"/classes/{classes.index('security')}"]  # fmt: skip
    got = assert_problem(post(world, body("simulation", scope="acct-1"), key), 422,
                         "deletion_scope_not_supported")  # fmt: skip
    assert [e["pointer"] for e in got["field_errors"]] == ["/scope_id"]
    for policy in (KEEP_SIM, rt.spec_baseline("synthetic-unset")):  # decision 137
        with same_session(world, retention_policy=policy) as narrowed:
            response = narrowed.post(URL, json=body("simulation"),
                                     headers=csrf(narrowed, key))  # fmt: skip
            got = assert_problem(response, 422, "retained_by_policy")
            assert [e["pointer"] for e in got["field_errors"]] == ["/classes/0"]
    for doc in (body("nonsense"), body("simulation", "simulation"),
                {**body("simulation"), "classes": []},
                {**body("simulation"), "record_ids": ["x"]},
                body("simulation", ack="SYNTHETIC\x07bell"),
                body("simulation", ack="é" * 2049),
                body("simulation", ack="\U0001f600" * 1100)):  # 4400 bytes  # fmt: skip
        assert_problem(post(world, doc, key), 400, "invalid_request")
    assert jobs(world) == [] and audits(world) == []
    # a refusal stored nothing, so the same key now files the corrected request
    assert post(world, body("simulation"), key).status_code == 202


def test_a_filed_request_enqueues_the_job_once_and_is_audited(world: Any) -> None:
    owner(world)
    key = "SYNTHETIC-deletion-filed-01"
    before = world.db.execute("SELECT clock_timestamp()").fetchone()[0]
    first = post(world, body("simulation", "evidence"), key)
    assert first.status_code == 202, first.text
    job = first.json()
    assert [e.message for e in JOB.iter_errors(job)] == []
    assert (job["kind"], job["state"], job["attempt"], job["reasons"]) == (
        dj.KIND, "queued", 0, [])  # fmt: skip
    [(jid, tenant, revision, state, error, payload)] = jobs(world)
    assert (str(jid), tenant, state, error) == (job["id"], world.tenant_a, "queued",
                                                None)  # fmt: skip
    assert revision == f"request:{payload['request_id']}"
    assert job["idempotency_key"] == f"{dj.KIND}:{revision}"
    uuid.UUID(payload["request_id"])  # opaque, server-chosen
    assert payload["classes"] == ["evidence", "simulation"]
    assert (payload["scope_id"], payload["record_ids"]) == ("tenant", [])
    receipt = payload["reauthentication_receipt_id"]
    assert receipt.startswith("step-up:") and "SYNTHETIC-client" not in receipt
    trig = dj._trigger(payload, str(world.tenant_a))  # the worker reads it back
    assert isinstance(trig, rt.DeletionRequest)
    assert before <= trig.requested_at <= world.db.execute(
        "SELECT clock_timestamp()").fetchone()[0]  # fmt: skip
    assert ACK not in json.dumps(payload)  # the acknowledgement is never queued
    again = post(world, body("simulation", "evidence"), key)  # a retry
    assert (again.status_code, again.content) == (202, first.content)
    assert again.headers["idempotent-replayed"] == "true"
    assert_problem(post(world, body("simulation"), key), 409, "idempotency_key_reused")
    assert len(jobs(world)) == 1
    [(t, actor, target, cid)] = audits(world)
    assert (t, actor, str(target), cid) == (
        world.tenant_a, world.users["alice"], job["id"],
        first.headers["x-correlation-id"])  # fmt: skip
    assert details(world) == [
        {
            "acknowledgement": ACK,
            "scope_id": "tenant",
            "classes": ["evidence", "simulation"],
        }
    ]  # 136
    for changed in (
        body("simulation", "evidence", ack=ACK + " (edited)"),
        {
            **body("simulation", "evidence"),
            "reauthentication_receipt_id": "SYNTHETIC-client-receipt-2",
        },
    ):
        assert_problem(post(world, changed, key), 409, "idempotency_key_reused")
    other = post(world, body("simulation"))  # another key: another request
    assert other.status_code == 202 and other.json()["id"] != job["id"]
    assert len(jobs(world)) == 2 and len(audits(world)) == 2


def test_requests_and_their_status_stay_in_their_tenant(
    world: Any, app_conn: Any
) -> None:
    owner(world)
    mine = post(world, body("simulation")).json()["id"]
    assert status(world, mine)["state"] == "pending"
    owner(world, "carol")  # tenant B's owner
    unknown = assert_problem(read(world, str(uuid.uuid4())), 404, "not_found")
    for other in (mine, "not-a-uuid"):  # foreign and malformed ids: the same 404
        got = assert_problem(read(world, other), 404, "not_found")
        assert {**got, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    theirs = post(world, body("evidence")).json()["id"]
    assert status(world, theirs)["classes"] == ["evidence"]
    assert [str(r[0]) for r in jobs(world, world.tenant_a)] == [mine]
    [(jid, tenant, *_)] = [r for r in jobs(world) if r[1] != world.tenant_a]
    assert str(jid) == theirs
    with tn.tenant_transaction(app_conn, tenant) as tx:  # B's RLS hides A's job
        assert dr.status(tx, uuid.UUID(mine)) is None
        assert dr.status(tx, uuid.UUID(theirs)) is not None
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:
        sweep, _ = dj.submit_sweep(tx)  # a sweep is no deletion request
    big = {"x": "a" * 16384}  # 0067: a detail holds at most 16 KiB
    with (pytest.raises(psycopg.errors.CheckViolation),
          tn.tenant_transaction(app_conn, world.tenant_a) as tx):  # fmt: skip
        tn.append_audit_event(tx, "privacy.synthetic", detail=big)
    owner(world)
    assert_problem(read(world, str(sweep)), 404, "not_found")


def test_status_follows_the_request_through_a_hold_to_done(
    world: Any, app_conn: Any, worker_conn: Any
) -> None:  # owner decision 113
    t = world.tenant_a
    held(app_conn, t, ("SIM-1",), ())  # class-wide
    owner(world)
    filed = post(world, body("simulation"))
    assert filed.status_code == 202, filed.text
    did = filed.json()["id"]
    pending = status(world, did)
    assert pending == {"id": did, "state": "pending", "classes": ["simulation"],
                       "requested_at": pending["requested_at"],
                       "reasons": []}  # fmt: skip
    assert tick(worker_conn) == [(uuid.UUID(did), JobState.FAILED)]
    suspended = status(world, did)
    assert suspended["state"] == "deletion_suspended"
    assert [(r["code"], r["blocking"]) for r in suspended["reasons"]] == [
        ("deletion_suspended", True)]  # fmt: skip
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") is not None
    release_hold(app_conn, t)
    resumed = status(world, did)  # the fresh job (request:<id>:r1) is queued
    assert (resumed["state"], resumed["reasons"]) == ("pending", [])
    assert (
        resumed["requested_at"] == suspended["requested_at"] == pending["requested_at"]
    )
    [(_, resumed_state)] = tick(worker_conn)
    assert resumed_state is JobState.SUCCEEDED
    done = status(world, did)
    assert (done["state"], done["reasons"]) == ("done", [])
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") is None
    revisions = [r[2] for r in jobs(world, t)]
    assert revisions[1] == revisions[0] + ":r1" and len(revisions) == 2


def test_a_job_level_refusal_reads_as_refused(world: Any, app_conn: Any,
                                              worker_conn: Any) -> None:  # fmt: skip
    """The job's `no_target` backstop (a request no route accepts today)."""
    t = world.tenant_a
    with tn.tenant_transaction(app_conn, t) as tx:
        now = dr.now(tx)
        req = rt.DeletionRequest("del-synth-research", str(t), "tenant",
                                 frozenset({C.RESEARCH}), now, "step-up:synthetic",
                                 dj.ACK)  # fmt: skip
        jid, _ = dr.submit_request(tx, req)
    assert tick(worker_conn) == [(jid, JobState.FAILED)]
    owner(world)
    got = status(world, str(jid))
    assert (got["state"], got["classes"]) == ("refused", ["research"])
    assert [(r["code"], r["blocking"]) for r in got["reasons"]] == [
        ("no_target", True)]  # fmt: skip


def test_an_accepted_request_replays_after_its_step_up_lapsed_or_policy_narrowed(
    world: Any,
) -> None:  # review finding 2: the checks run after the key's claim
    owner(world)
    key = "SYNTHETIC-deletion-replay-1"
    first = post(world, body("simulation"), key)
    assert first.status_code == 202, first.text
    tiny = timedelta(microseconds=1)
    for settings, refusal in (({"step_up_window": tiny}, (401, "step_up_required")),
                              ({"retention_policy": KEEP_SIM},
                               (422, "retained_by_policy"))):  # fmt: skip
        with same_session(world, **settings) as later:
            again = later.post(URL, json=body("simulation"), headers=csrf(later, key))
            assert (again.status_code, again.content) == (202, first.content)
            assert again.headers["idempotent-replayed"] == "true"
            fresh = later.post(URL, json=body("simulation"), headers=csrf(later))
            assert_problem(fresh, *refusal)  # a new request is still refused
    assert len(jobs(world)) == 1 and len(audits(world)) == 1


def test_members_cannot_read_deletion_jobs_through_read_job(world: Any) -> None:
    owner(world)  # owner decision 138 (review finding 1)
    did = post(world, body("simulation")).json()["id"]
    assert world.client.get(f"/api/v1/jobs/{did}").json()["id"] == did
    assert login(world.client, "bob").status_code == 201
    unknown = assert_problem(world.client.get(f"/api/v1/jobs/{uuid.uuid4()}"), 404,
                             "not_found")  # fmt: skip
    got = assert_problem(world.client.get(f"/api/v1/jobs/{did}"), 404, "not_found")
    assert {**got, "correlation_id": ""} == {**unknown, "correlation_id": ""}


def release_hold(app_conn: Any, t: uuid.UUID) -> None:
    with tn.tenant_transaction(app_conn, t) as tx:
        hs.release(tx, "hold-1", "counsel-1", "inquiry_closed", (
            rt.Approval(R.TENANT_OWNER, "owner-synth-1", "stepup-o-1"),
            rt.Approval(R.OPERATOR, "operator-synth-1", "stepup-x-1")))  # fmt: skip


def held(app_conn: Any, t: uuid.UUID, sims: tuple[str, ...],
         ids: tuple[str, ...]) -> None:  # fmt: skip
    with tn.tenant_transaction(app_conn, t) as tx:
        for sid in sims:
            ss.save(tx, simulation(sid))
        hs.place(tx, "hold-1", {C.SIMULATION}, ids, "litigation", "counsel-1",
                 R.TENANT_OWNER, "owner-synth-1")  # fmt: skip


def test_a_record_hold_suspends_only_its_record(
    world: Any, app_conn: Any, worker_conn: Any
) -> None:
    t = world.tenant_a
    held(app_conn, t, ("SIM-1", "SIM-2"), ("SIM-1",))
    owner(world)
    did = post(world, body("simulation")).json()["id"]
    assert tick(worker_conn) == [(uuid.UUID(did), JobState.FAILED)]
    assert status(world, did)["state"] == "deletion_suspended"
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") is not None and ss.load(tx, "SIM-2") is None
    release_hold(app_conn, t)
    assert status(world, did)["state"] == "pending"
    assert [s for _, s in tick(worker_conn)] == [JobState.SUCCEEDED]
    assert status(world, did)["state"] == "done"
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") is None


def test_a_held_request_whose_resume_is_refused_reads_refused(
    world: Any, app_conn: Any, worker_conn: Any
) -> None:  # the latest job decides, not the first
    t = world.tenant_a
    held(app_conn, t, ("SIM-1",), ())
    owner(world)
    did = post(world, body("simulation")).json()["id"]
    assert tick(worker_conn) == [(uuid.UUID(did), JobState.FAILED)]
    assert status(world, did)["state"] == "deletion_suspended"
    release_hold(app_conn, t)
    # SYNTHETIC: a worker without the simulation target refuses the resume
    assert [s for _, s in tick(worker_conn, {})] == [JobState.FAILED]
    got = status(world, did)
    assert (got["state"], [(r["code"], r["blocking"]) for r in got["reasons"]]) == (
        "refused", [("no_target", True)])  # fmt: skip
    assert [r[3:5] for r in jobs(world, t)] == [
        ("failed", "deletion_suspended"), ("failed", "no_target")]  # fmt: skip
