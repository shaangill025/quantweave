"""`GET /api/v1/jobs/{job_id}` (readJob; T025 increment 6) on PostgreSQL 16 as qw_app
(T010 harness). SYNTHETIC tenants, users, kinds and payloads; times are the database
clock. Bodies are checked against `job.schema.json`; the oracles are the 202 bodies
the writes returned and the rows read back by the superuser."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from qw_adapters import feedback_store as fs
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_domain.jobs import JobState, Pool, Priority

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
fbt = _module("qw_feedback_api_tests", ROOT / "apps/api/tests/test_feedback_api.py")
wlt = _module("qw_watchlists_api_tests", ROOT / "apps/api/tests/test_watchlists_api.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
URL = "/api/v1/jobs"
SECRET = "SYNTHETIC-payload-secret-7f3a"
KINDS = ("synthetic.read_job",)


def read(w: Any, job_id: str) -> Any:
    return w.client.get(f"{URL}/{job_id}")


def same_404(w: Any, *job_ids: str) -> None:
    """Every id is the same 404 as a fresh unknown uuid (correlation id aside)."""
    unknown = assert_problem(read(w, str(uuid.uuid4())), 404, "not_found")
    for job_id in job_ids:
        body = assert_problem(read(w, job_id), 404, "not_found")
        assert {**body, "correlation_id": ""} == {**unknown, "correlation_id": ""}


def conforms(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    body: dict[str, Any] = response.json()
    assert [e.message for e in fbt.JOB.iter_errors(body)] == []
    return body


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.mark.db
def test_feedback_jobs_read_back_exactly_for_their_principal_only(world: Any) -> None:
    fbt.simulation(world, world.tenant_a)
    login(world.client, "bob")
    key = "SYNTHETIC-readjob-fb-0001"
    first = fbt.post(world, fbt.body(), key)
    assert first.status_code == 202, first.text
    job_id = first.json()["id"]
    got = read(world, job_id)
    conforms(got)
    assert got.content == first.content  # byte for byte what the 202 returned
    at = world.db.execute("SELECT recorded_at FROM app.feedback WHERE record_id = %s",
                          (job_id,)).fetchone()[0]  # fmt: skip
    assert instant(got.json()["created_at"]) == at  # the record's own time, kept
    wlt.purge(world)  # the idempotency record is gone; the job is still readable
    assert read(world, job_id).content == first.content
    with tn.runtime_connection(world.url) as c, \
            tn.tenant_transaction(c, world.tenant_a) as tx:  # fmt: skip
        fs.delete(tx, principal=world.users["bob"], record_id=job_id)
    assert read(world, job_id).content == first.content  # history, not content
    login(world.client, "alice")  # same tenant, another member: private feedback
    same_404(world, job_id)
    login(world.client, "carol")  # tenant B
    same_404(world, job_id)


@pytest.mark.db
def test_watchlist_delete_jobs_read_back_for_the_tenant(world: Any) -> None:
    login(world.client, "bob")
    made = wlt.write(world, "POST", wlt.URL, wlt.GROWTH).json()
    wid = made["id"]
    live = wlt.write(world, "POST", wlt.URL, wlt.GROWTH).json()["id"]
    gone = wlt.write(world, "DELETE", f"{wlt.URL}/{wid}", None, '"1"',
                     "SYNTHETIC-readjob-wl-0001")  # fmt: skip
    assert gone.status_code == 202, gone.text
    job_id = gone.json()["id"]
    assert job_id == f"{wid}.deleted"
    got = read(world, job_id)
    conforms(got)
    assert got.content == gone.content
    at = world.db.execute(
        "SELECT recorded_at FROM app.watchlist_revision WHERE watchlist_id = %s "
        "AND action = 'delete'", (wid,)).fetchone()[0]  # fmt: skip
    assert instant(got.json()["created_at"]) == at
    wlt.purge(world)
    assert read(world, job_id).content == gone.content
    login(world.client, "alice")  # watchlists are the tenant's: any member reads
    assert read(world, job_id).content == gone.content
    same_404(world, f"{live}.deleted", f"{wid.upper()}.deleted", f"{wid}.removed",
             f"{wid}.deleted.deleted", wid, f"{uuid.uuid4()}.deleted")  # fmt: skip
    login(world.client, "carol")
    same_404(world, job_id)


def enqueue(w: Any, revision: str, max_attempts: int = 3,
            deadline: datetime | None = None) -> uuid.UUID:  # fmt: skip
    with tn.runtime_connection(w.url) as c, \
            tn.tenant_transaction(c, w.tenant_a) as tx:  # fmt: skip
        job_id, created = qj.enqueue(tx, KINDS[0], revision, Priority.MONITORING,
                                     {"secret": SECRET}, max_attempts,
                                     deadline)  # fmt: skip
    assert created
    return job_id


def claim(worker_url: str) -> qj.ClaimedJob:
    with tn.runtime_connection(worker_url) as c:
        job = qj.claim(c, "syn-reader", Pool.STANDARD, timedelta(minutes=5), KINDS)
    assert job is not None
    return job


def fail(worker_url: str, job: qj.ClaimedJob, code: str, retryable: bool) -> None:
    with tn.runtime_connection(worker_url) as c, \
            tn.tenant_transaction(c, job.tenant_id) as tx:  # fmt: skip
        qj.fail(tx, job, code, retryable=retryable)


def row(w: Any, job_id: uuid.UUID) -> tuple[Any, ...]:
    found = w.db.execute("SELECT created_at, deadline_at, total_attempts FROM app.job "
                         "WHERE id = %s", (job_id,)).fetchone()  # fmt: skip
    return tuple(found)


@pytest.mark.db
def test_durable_jobs_are_read_by_their_tenant_without_internals(
    world: Any, runtime_urls: dict[str, str]
) -> None:
    worker = runtime_urls["qw_worker"]
    now = world.db.execute("SELECT now()").fetchone()[0]
    deadline = now + timedelta(hours=2)
    ran = enqueue(world, "syn-rev-1", deadline=deadline)
    created, stored_deadline, _ = row(world, ran)
    login(world.client, "bob")  # a member is enough
    queued = conforms(read(world, str(ran)))
    assert queued == {
        "id": str(ran), "kind": KINDS[0], "state": "queued",
        "created_at": queued["created_at"], "attempt": 0,
        "idempotency_key": f"{KINDS[0]}:syn-rev-1",
        "deadline_at": queued["deadline_at"], "result_reference": None,
        "reasons": [],
    }  # fmt: skip
    assert instant(queued["created_at"]) == created
    assert instant(queued["deadline_at"]) == stored_deadline == deadline
    assert queued["created_at"].endswith("Z")
    job = claim(worker)
    running = read(world, str(ran))
    body = conforms(running)
    assert (body["state"], body["attempt"]) == ("running", 1)
    for hidden in (SECRET, job.lease_token, job.lease_token.split(":")[1], "secret",
                   "lease", "payload", "syn-reader"):  # fmt: skip
        assert hidden not in running.text
    fail(worker, job, "synthetic_bad_input", retryable=False)
    failed = conforms(read(world, str(ran)))
    assert (failed["state"], failed["attempt"], failed["created_at"]) == (
        "failed", 1, queued["created_at"])  # fmt: skip
    assert failed["reasons"] == [{
        "code": "synthetic_bad_input", "affected_ids": [], "blocking": True,
        "message": failed["reasons"][0]["message"]}]  # fmt: skip
    assert SECRET not in str(failed)

    dead = enqueue(world, "syn-rev-2", max_attempts=1)
    fail(worker, claim(worker), "synthetic_flaky", retryable=True)
    lettered = conforms(read(world, str(dead)))
    assert lettered["state"] == "failed"  # dead_letter is not in job.schema.json
    assert [(r["code"], r["blocking"]) for r in lettered["reasons"]] == [
        ("synthetic_flaky", True), ("dead_letter", True)]  # fmt: skip
    with tn.runtime_connection(worker) as c, \
            tn.tenant_transaction(c, world.tenant_a) as tx:  # fmt: skip
        assert qj.requeue(tx, dead)
    again = conforms(read(world, str(dead)))  # a new generation resets nothing here
    assert (again["state"], again["attempt"], again["created_at"]) == (
        "queued", 1, lettered["created_at"])  # fmt: skip
    assert again["reasons"] == [{**lettered["reasons"][0], "blocking": False}]

    login(world.client, "alice")
    assert conforms(read(world, str(ran))) == failed
    same_404(world, str(ran).upper(), str(ran).replace("-", ""), f"urn:uuid:{ran}")
    assert_problem(read(world, f"{{{ran}}}"), 400, "invalid_request")  # pattern
    login(world.client, "carol")  # tenant B: the same 404 as an unknown id
    same_404(world, str(ran), str(dead))
    world.client.cookies.clear()
    assert_problem(read(world, str(ran)), 401, "unauthenticated")


@pytest.mark.db
def test_an_id_naming_two_jobs_fails_closed(world: Any) -> None:
    fbt.simulation(world, world.tenant_a)
    login(world.client, "bob")
    first = fbt.post(world, fbt.body(), "SYNTHETIC-readjob-fb-0002")
    job_id = first.json()["id"]
    world.db.execute(
        "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, pool, "
        "payload, max_attempts) VALUES (%s, %s, 'synthetic.twin', 'r1', "
        "'monitoring', 'standard', '{}', 1)", (job_id, world.tenant_a))  # fmt: skip
    assert_problem(read(world, job_id), 409, "ambiguous_job")
    login(world.client, "alice")  # not bob's feedback: only the durable job is hers
    assert conforms(read(world, job_id))["kind"] == "synthetic.twin"


@pytest.mark.db
def test_tampered_records_are_409_integrity(world: Any) -> None:
    fbt.simulation(world, world.tenant_a)
    login(world.client, "bob")
    fb_id = fbt.post(world, fbt.body(), "SYNTHETIC-readjob-fb-0003").json()["id"]
    wid = wlt.write(world, "POST", wlt.URL, wlt.GROWTH).json()["id"]
    wlt.write(world, "DELETE", f"{wlt.URL}/{wid}", None, '"1"')
    for table, column in (("watchlist_revision", "recorded_at"),
                          ("feedback", "recorded_at")):  # fmt: skip
        with world.db.transaction():
            world.db.execute("SET LOCAL session_replication_role = replica")
            world.db.execute(f"UPDATE app.{table} SET {column} = {column} "
                             "- interval '1 microsecond'")  # fmt: skip
    for job_id in (f"{wid}.deleted", fb_id):
        assert_problem(read(world, job_id), 409, "integrity")


@pytest.mark.db
def test_a_tampered_feedback_row_fails_only_its_own_job(world: Any) -> None:
    fbt.simulation(world, world.tenant_a)
    login(world.client, "bob")
    fb_id = fbt.post(world, fbt.body(), "SYNTHETIC-readjob-fb-0004").json()["id"]
    durable_id = enqueue(world, "syn-rev-tamper")
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.feedback SET recorded_at = recorded_at "
                         "- interval '1 microsecond'")  # fmt: skip
    assert conforms(read(world, str(durable_id)))["state"] == "queued"
    same_404(world)  # an unknown uuid is still the plain 404
    assert_problem(read(world, fb_id), 409, "integrity")  # the owner's own job
    login(world.client, "alice")  # not hers: the same 404, nothing revealed
    same_404(world, fb_id)


def test_job_wire_form_is_the_schema() -> None:
    from qw_api import jobs as routes

    at = datetime(2026, 10, 15, 12, 0, 0, 123456, tzinfo=UTC)
    status = qj.JobStatus(uuid.UUID(int=1), "synthetic.kind", JobState.EXPIRED,
                          "synthetic.kind:r", 0, "deadline_passed", at,
                          None)  # fmt: skip
    body = routes.durable(status)
    assert [e.message for e in fbt.JOB.iter_errors(body)] == []
    assert (body["state"], body["created_at"], body["deadline_at"]) == (
        "expired", "2026-10-15T12:00:00.123456Z", None)  # fmt: skip
    assert [(r["code"], r["blocking"]) for r in body["reasons"]] == [
        ("deadline_passed", True)]  # fmt: skip
