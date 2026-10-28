"""The qw-assessor process's `assessment.optimizer` jobs (T050 increment 5): the
loop runs `assessor_run_optimizer.trial` on its qw_assessor login and maps its
outcome as for promotions: a refusal fails without retry, a `database` fault or a
crash retries, a redelivered recorded run completes. Real logins; SYNTHETIC data
only (test_assessor_run_optimizer's world). The real path ends at
`sandbox_unqualified`; the tests marked GATE-LIFTED inject that module's
test-only ports by monkeypatching `aro.trial`; production has no slot."""

from __future__ import annotations

import importlib.util
import inspect
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import jobs as qj
from qw_adapters import optimizer_store as os_
from qw_adapters import tenancy as tn
from qw_domain.jobs import Backoff, JobState, Priority
from qw_worker import assessor as qa
from qw_worker.config import WorkerSettings


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_tests = Path(__file__).parents[3] / "packages" / "adapters" / "tests"
_harness = _load("qw_pg_harness", _tests / "conftest.py")
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls, app_conn = _harness.conn, _harness.runtime_urls, _harness.app_conn
ot = _load("qw_assessor_run_optimizer_tests", _tests / "test_assessor_run_optimizer.py")
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
SETTINGS = WorkerSettings("synthetic-assessor",
                          job_backoff=Backoff(timedelta(0), timedelta(0)))  # fmt: skip


@pytest.fixture
def logins(runtime_urls: dict[str, str]) -> Iterator[tuple[Conn, Conn]]:
    with (
        tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as w,
        tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as a,
    ):
        yield w, a


def tenant(app_conn: Conn, bootstrap: bool = False) -> uuid.UUID:
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    if bootstrap:  # as qw-bootstrap records it (0004): the operator's tenant
        app_conn.execute("INSERT INTO app.installation_bootstrap (tenant_id) "
                         "VALUES (%s)", (t,))  # fmt: skip
    return t


@pytest.fixture
def world(app_conn: Conn, conn: Conn, logins: tuple[Conn, Conn]) -> tuple[
    uuid.UUID, aro.Request
]:  # fmt: skip
    """The bootstrap tenant routes the jobs (decisions 92, 93) and holds the
    protocol's frozen reservation in its book (decision 98)."""
    t = tenant(app_conn, bootstrap=True)
    wrk, asr = logins
    ot.authorize(conn, t)
    ot.reserve_in(wrk, t)
    return t, ot.stored_world(asr, wrk)


# what a tenant may see in `last_error` (decision 93): nothing about the registry
SHOWN = {None, "optimizer_refused", "installation_scope", "database",
         "assessment_failed"}  # fmt: skip


def shown(wrk: Conn, t: uuid.UUID) -> set[str | None]:
    with tn.tenant_transaction(wrk, t) as tx:
        rows = tx.conn.execute("SELECT DISTINCT last_error FROM app.job").fetchall()
    return {r[0] for r in rows}


def spy(calls: list[aro.Request]) -> Trial:
    def run(a: Conn, r: aro.Request, beat: Callable[[], object]) -> object:
        calls.append(r)  # the trial is the only registry reader in the loop
        raise AssertionError("the trial ran")

    return run


def submit(
    wrk: Conn, t: uuid.UUID, payload: object, rev: str | None = None
) -> uuid.UUID:
    with tn.tenant_transaction(wrk, t) as tx:
        if rev is None and isinstance(payload, aro.Request):
            return qa.submit_optimizer_job(tx, payload)
        jid, _ = qj.enqueue(tx, qa.OPT_KIND, rev or "r-1", Priority.EXPERIMENT,
                            payload)  # type: ignore[arg-type]  # fmt: skip
    return jid


def state(wrk: Conn, t: uuid.UUID, jid: uuid.UUID) -> tuple[JobState, str | None]:
    with tn.tenant_transaction(wrk, t) as tx:
        row = tx.conn.execute("SELECT state, last_error FROM app.job WHERE id = %s",
                              (jid,)).fetchone()  # fmt: skip
    assert row is not None
    return JobState(row[0]), row[1]


def loop(logins: tuple[Conn, Conn], **kw: Any) -> qa.AssessorLoop:
    return qa.AssessorLoop(*logins, SETTINGS, **kw)


Trial = Callable[[Conn, aro.Request, Callable[[], object]], object]


def lifted(log: Any, crash: bool = False) -> Trial:
    """GATE-LIFTED: a stand-in for `aro.trial` (monkeypatched; the loop has no
    slot) running its private phases with the test-only ports; `crash` dies once
    after the reservation (a crash-like fault, not a refusal)."""
    once = [crash]

    def run(a: Conn, r: aro.Request, beat: Callable[[], object]) -> object:
        def die() -> None:
            if once[0]:
                once[0] = False
                raise RuntimeError("SYNTHETIC process death")
            beat()

        return ot.lifted(a, r, log, die)

    return run


def test_the_real_optimizer_job_fails_blocked_without_retry(
    logins: tuple[Conn, Conn],
    conn: Conn,
    world: tuple[uuid.UUID, aro.Request],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (wrk, _), (t, r) = logins, world
    assert "optimizer" not in inspect.signature(qa.AssessorLoop).parameters
    before = ot.entries(conn)
    jid = submit(wrk, t, r)
    assert submit(wrk, t, r) == jid  # idempotent on the request
    assert loop(logins).run_one() == (jid, JobState.FAILED)  # the production loop
    assert state(wrk, t, jid) == (JobState.FAILED, "optimizer_refused")
    assert "sandbox_unqualified" in caplog.text  # the real code: the process log
    assert ot.entries(conn) == before  # nothing accepted, nothing recorded
    assert loop(logins).run_one() is None
    assert shown(wrk, t) <= SHOWN


def test_only_the_bootstrap_tenants_job_runs(
    logins: tuple[Conn, Conn],
    app_conn: Conn,
    world: tuple[uuid.UUID, aro.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 93: another tenant's job is refused before any registry read."""
    (wrk, _), (_, r) = logins, world
    calls: list[aro.Request] = []
    monkeypatch.setattr(aro, "trial", spy(calls))
    other = tenant(app_conn)
    jid = submit(wrk, other, r)
    assert loop(logins).run_one() == (jid, JobState.FAILED)
    assert (state(wrk, other, jid), calls) == ((JobState.FAILED,
                                                "installation_scope"), [])  # fmt: skip
    assert shown(wrk, other) <= SHOWN


def test_without_a_bootstrap_row_every_optimizer_job_is_refused(
    logins: tuple[Conn, Conn],
    app_conn: Conn,
    conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hosted edition (no bootstrap row): fail closed, the same code."""
    wrk, asr = logins
    calls: list[aro.Request] = []
    monkeypatch.setattr(aro, "trial", spy(calls))
    t, r = tenant(app_conn), ot.stored_world(asr, wrk)
    assert qa.bootstrap_tenant(asr) is None
    grants = conn.execute(  # 0048 and 0051: one column, to these two alone
        "SELECT r, a.attname FROM unnest(%s::text[]) r CROSS JOIN pg_attribute a "
        "WHERE a.attrelid = 'app.installation_bootstrap'::regclass AND a.attnum > 0 "
        "AND has_column_privilege(r, a.attrelid, a.attnum, 'SELECT')",
        (["qw_worker", "qw_assessor", "qw_release", "qw_ingest"],),
    ).fetchall()
    assert sorted(grants) == [("qw_assessor", "tenant_id"), ("qw_worker", "tenant_id")]
    jid = submit(wrk, t, r)
    assert loop(logins).run_one() == (jid, JobState.FAILED)
    assert (state(wrk, t, jid), calls) == ((JobState.FAILED, "installation_scope"),
                                           [])  # fmt: skip


def faulty(a: Conn, r: aro.Request, beat: Callable[[], object]) -> object:
    raise os_.OptimizerStoreError("database", "OperationalError")


def test_refusals_fail_permanently_and_faults_retry(
    logins: tuple[Conn, Conn],
    conn: Conn,
    world: tuple[uuid.UUID, aro.Request],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every refusal shows only `optimizer_refused`; the real code is logged."""
    (wrk, _), (t, r) = logins, world
    before, log = ot.entries(conn), ot.MemoryLog()
    w = r.to_payload()
    refused = (JobState.FAILED, "optimizer_refused")
    cases: list[tuple[object, Trial | None, tuple[JobState, str], str | None]] = [
        (w | {"extra": "x"}, None, refused, "payload_invalid"),
        (w | {"candidate_id": "cand-x"}, None, refused, "unknown_candidate"),
        (w | {"source_cycle_id": "cycle-x"}, None, refused, "cycle_missing"),
        (replace(r, assessor=ot.PRODUCER).to_payload(), lifted(log), refused,
         "self_certification"),
        (w, faulty, (JobState.QUEUED, "database"), None),  # retried, says nothing
    ]  # fmt: skip
    for n, (payload, stand_in, want, logged) in enumerate(cases):
        jid = submit(wrk, t, payload, rev=f"r-{n}")
        caplog.clear()
        with monkeypatch.context() as m:
            if stand_in is not None:
                m.setattr(aro, "trial", stand_in)
            assert loop(logins).run_one() == (jid, want[0]), logged
        assert state(wrk, t, jid) == want, logged
        assert logged is None or logged in caplog.text, logged
    assert (log.runs, ot.entries(conn)) == ({}, before)
    assert shown(wrk, t) <= SHOWN


def test_gate_lifted_job_retries_a_crash_and_completes_a_redelivery(
    logins: tuple[Conn, Conn],
    conn: Conn,
    world: tuple[uuid.UUID, aro.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATE-LIFTED SYNTHETIC: a crash after the reservation is retried; the retry
    re-runs under the recorded read; a redelivered recorded run completes."""
    (wrk, _), (t, r) = logins, world
    log, before = os_.RunLog(logins[1], ot.INST), ot.entries(conn)  # durable (0049)
    jid = submit(wrk, t, r)
    monkeypatch.setattr(aro, "trial", lifted(log, crash=True))
    assert loop(logins).run_one() == (jid, JobState.QUEUED)
    assert state(wrk, t, jid) == (JobState.QUEUED, "assessment_failed")
    monkeypatch.setattr(aro, "trial", lifted(log))
    lp = loop(logins)
    assert lp.run_one() == (jid, JobState.SUCCEEDED)
    assert len(log.attempts(ot.MP.plan_id)) == 2 and ot.entries(conn) == before + 1
    again = submit(wrk, t, r.to_payload(), rev="r-again")
    assert lp.run_one() == (again, JobState.SUCCEEDED)  # run_recorded: done
    assert len(log.attempts(ot.MP.plan_id)) == 2 and ot.entries(conn) == before + 1
    assert shown(wrk, t) <= SHOWN
