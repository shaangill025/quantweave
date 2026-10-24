"""Owner decisions 40, 62 and 65: the `qw-assessor` process. It holds exactly a
qw_worker and a qw_assessor login, and `qw-worker` refuses the assessor login.
Its job loop claims `assessment.promotion` jobs, runs `promotion_run` and records
a refusal as a job failure; its built-in strategy registry starts empty. Real
logins; SYNTHETIC data only (test_assessor_run's fixtures)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from qw_adapters import assessment_request_store as ars
from qw_adapters import experiment_store as es
from qw_adapters import holdout_series_store as hs
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_adapters.jobs import LeaseLost
from qw_domain import improvement as im
from qw_domain.budget import BudgetBook
from qw_domain.jobs import Backoff, JobState, Pool, Priority
from qw_worker import __main__ as worker_entry
from qw_worker import assessor as qa
from qw_worker.config import WorkerSettings


def _load(name: str, path: Path) -> Any:
    """`path` as module `name`, executed once per session."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_tests = Path(__file__).parents[3] / "packages" / "adapters" / "tests"
_harness = _load("qw_pg_harness", _tests / "conftest.py")
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn = _harness.app_conn
run_tests = _load("qw_assessor_run_tests", _tests / "test_assessor_run.py")
owner, ing = run_tests.owner, run_tests.ing
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
NOW = Backoff(timedelta(0), timedelta(0))
SETTINGS = WorkerSettings("synthetic-assessor", job_backoff=NOW)
JOB = qa.AssessmentJob("req-synth-1", "exp-synth-1", "cand-1")


@pytest.fixture
def logins(runtime_urls: dict[str, str]) -> Iterator[tuple[Conn, Conn]]:
    with (
        tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as w,
        tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as a,
    ):
        yield w, a


@pytest.fixture
def world(app_conn: Conn, logins: tuple[Conn, Conn], owner: Conn, ing: Conn) -> Any:
    """test_assessor_run's world: rights recorded, the dataset frozen, series
    preregistered, the experiment scheduled."""
    wrk, asr = logins
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    w = run_tests.stored_world(owner, t)
    run_tests.record_dataset(ing, w)
    with tn.tenant_transaction(asr, t) as tx:
        hs.preregister(tx, w.plan.plan_id, "holdout", w.holdout.series)
        hs.preregister(tx, w.plan.plan_id, "px-synth-1", w.holdout.series)
    es.record(wrk, asr, t, w.reg, run_tests.EXP, "cand-1", w.plan, w.book,
              im.Scheduled(run_tests.EXP, "bud-exp-1"))  # fmt: skip
    return w


def submit(wrk: Conn, t: uuid.UUID, payload: object, rev: str = "r-1") -> uuid.UUID:
    with tn.tenant_transaction(wrk, t) as tx:
        jid, _ = qj.enqueue(tx, qa.KIND, rev, Priority.EXPERIMENT,
                            payload)  # type: ignore[arg-type]  # fmt: skip
    return jid


def state(wrk: Conn, t: uuid.UUID, jid: uuid.UUID) -> tuple[JobState, str | None]:
    with tn.tenant_transaction(wrk, t) as tx:
        row = tx.conn.execute("SELECT state, last_error FROM app.job WHERE id = %s",
                              (jid,)).fetchone()  # fmt: skip
    assert row is not None
    return JobState(row[0]), row[1]


def source(w: Any) -> qa.RequestSource:
    def get(a: Conn, t: uuid.UUID, job: qa.AssessmentJob) -> Any:
        return run_tests.request(w)

    return get


def loop(logins: tuple[Conn, Conn], **kw: Any) -> qa.AssessorLoop:
    return qa.AssessorLoop(*logins, SETTINGS, **kw)


# --- the job loop ---


def test_a_run_completes_its_job_and_a_redelivery_completes_too(
    logins: tuple[Conn, Conn], conn: Conn, world: Any
) -> None:
    wrk, _ = logins
    jid = submit(wrk, world.t, JOB.to_payload())
    lp = loop(logins, source=source(world), strategies=run_tests.REG)
    assert lp.run_one() == (jid, JobState.SUCCEEDED)
    assert state(wrk, world.t, jid) == (JobState.SUCCEEDED, None)
    before = run_tests.counts(conn, world.t)
    assert before["evaluation_receipt"] == 1
    again = submit(wrk, world.t, JOB.to_payload(), rev="r-2")  # the same request
    assert lp.run_one() == (again, JobState.SUCCEEDED)  # run_recorded: done
    assert run_tests.counts(conn, world.t) == before
    assert lp.run_one() is None  # nothing left to claim


def blocked_source(w: Any) -> qa.RequestSource:
    def get(a: Conn, t: uuid.UUID, job: qa.AssessmentJob) -> Any:
        book = BudgetBook.open(str(t), run_tests.AT, run_tests.CAPS)  # unreserved
        return run_tests.request(w, book=book)

    return get


def test_refusals_fail_the_job_without_retry_and_read_nothing(
    logins: tuple[Conn, Conn], conn: Conn, world: Any
) -> None:
    wrk, _ = logins
    before = run_tests.counts(conn, world.t)
    cases: list[tuple[object, dict[str, Any], str]] = [
        (JOB.to_payload(), {}, "request_missing"),  # the default (store) source
        (JOB.to_payload() | {"extra": "x"}, {"source": source(world)},
         "payload_invalid"),
        ({"request_id": "req-1"}, {"source": source(world)}, "payload_invalid"),
        (JOB.to_payload(), {"source": source(world)},
         "strategy_implementation_unavailable"),  # the built-in registry is empty
        (qa.AssessmentJob("req-synth-1", "exp-other", "cand-1").to_payload(),
         {"source": source(world), "strategies": run_tests.REG}, "request_mismatch"),
        (JOB.to_payload(), {"source": blocked_source(world),
                            "strategies": run_tests.REG}, "blocked"),  # decision 62
    ]  # fmt: skip
    for n, (payload, kw, want) in enumerate(cases):
        jid = submit(wrk, world.t, payload, rev=f"r-{n}")
        assert loop(logins, **kw).run_one() == (jid, JobState.FAILED), want
        assert state(wrk, world.t, jid) == (JobState.FAILED, want)
        assert run_tests.counts(conn, world.t) == before, want


def test_a_crash_is_retried_and_reruns_the_same_read(
    logins: tuple[Conn, Conn], conn: Conn, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrk, _ = logins
    jid = submit(wrk, world.t, JOB.to_payload())
    lp = loop(logins, source=source(world), strategies=run_tests.REG)
    run_tests.crash(monkeypatch, run_tests.si, "holdout_run")  # after the reservation
    assert lp.run_one() == (jid, JobState.QUEUED)  # retryable: back in the queue
    assert state(wrk, world.t, jid) == (JobState.QUEUED, "assessment_failed")
    monkeypatch.undo()
    assert lp.run_one() == (jid, JobState.SUCCEEDED)  # the retry: attempt 2
    assert [a for a, *_ in run_tests.attempts(conn, world.t)] == [1, 2]
    assert run_tests.counts(conn, world.t)["holdout_access"] == 1


def test_a_database_fault_is_retried_not_final(
    logins: tuple[Conn, Conn], world: Any
) -> None:
    def faulty(a: Conn, t: uuid.UUID, job: qa.AssessmentJob) -> Any:
        raise es.ExperimentStoreError("database", "OperationalError")

    wrk, _ = logins
    jid = submit(wrk, world.t, JOB.to_payload())
    assert loop(logins, source=faulty).run_one() == (jid, JobState.QUEUED)
    assert state(wrk, world.t, jid) == (JobState.QUEUED, "database")


# --- the request store (decisions 43, 64 and 67) and the heartbeat ---


def inputs(w: Any, seen: list[tuple[Any, ...]] | None = None) -> qa.InputsProvider:
    def get(tx: tn.TenantTx, stored: Any) -> qa.BoundInputs:
        if seen is not None:  # a live qw_assessor transaction, the request's own
            q = "SELECT current_user, count(*) FROM app.assessment_request WHERE "
            q += "verdict_id = %s GROUP BY 1"
            row = tx.conn.execute(q, (stored.verdict_id,)).fetchone()
            seen.append((tx.tenant_id, tx.conn.info.transaction_status, row))
        return qa.BoundInputs(w.study, w.reg, run_tests.fx.STRATS, w.ds, w.rights)

    return get


def test_a_submitted_request_and_its_job_are_one_transaction(
    logins: tuple[Conn, Conn], conn: Conn, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrk, _ = logins
    r = run_tests.request(world)
    with tn.tenant_transaction(wrk, world.t) as tx:
        jid = qa.submit_job(tx, r)
    assert state(wrk, world.t, jid) == (JobState.QUEUED, None)
    rows = "SELECT count(*) FROM app.assessment_request"
    with (
        pytest.raises(ars.AssessmentRequestError, match="request_exists"),
        tn.tenant_transaction(wrk, world.t) as tx,
    ):
        qa.submit_job(tx, r)
    jobs = "SELECT count(*) FROM app.job WHERE kind = 'assessment.promotion'"
    assert [conn.execute(q).fetchone() for q in (rows, jobs)] == [(1,), (1,)]

    def no_queue(*args: object, **kw: object) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(qj, "enqueue", no_queue)
    with pytest.raises(RuntimeError), tn.tenant_transaction(wrk, world.t) as tx:
        qa.submit_job(tx, run_tests.request(world, verdict_id="v-q"))
    assert conn.execute(rows).fetchone() == (1,)  # the request rolled back too


def test_the_store_source_runs_only_with_every_bound_input(
    logins: tuple[Conn, Conn], conn: Conn, world: Any
) -> None:
    wrk, _ = logins
    with tn.tenant_transaction(wrk, world.t) as tx:
        jid = qa.submit_job(tx, run_tests.request(world))
    before = run_tests.counts(conn, world.t)
    lp = loop(logins, strategies=run_tests.REG)  # the default: STORE_SOURCE
    assert lp.run_one() == (jid, JobState.FAILED)  # it reads the input stores, and
    # this world stored no search space (test_store_inputs stores every input)
    assert state(wrk, world.t, jid) == (JobState.FAILED, "search_space_missing")
    other = run_tests.world(world.t)  # the same inputs, rebuilt: the hashes match
    wrong = qa.BoundInputs(world.study, world.reg.register_candidate(
        run_tests.fx.cand(run_tests.si.derive(run_tests.fx.BASE, run_tests.fx.SPACE,
                                              {"lookback": 80}, "cand-2"))),
        run_tests.fx.STRATS, other.ds, other.rights)  # fmt: skip
    for n, (provider, want) in enumerate([(lambda tx, s: wrong, "request_mismatch")]):
        with tn.tenant_transaction(wrk, world.t) as tx:
            j = qa.submit_job(tx, run_tests.request(world, verdict_id=f"v-{n}"))
        assert loop(
            logins, source=qa.store_source(provider), strategies=run_tests.REG
        ).run_one() == (j, JobState.FAILED)
        assert state(wrk, world.t, j) == (JobState.FAILED, want)
    assert run_tests.counts(conn, world.t) == before  # nothing was read
    with tn.tenant_transaction(wrk, world.t) as tx:
        j = qa.submit_job(tx, run_tests.request(world, verdict_id="v-run"))
    seen: list[tuple[Any, ...]] = []
    ok = loop(logins, source=qa.store_source(inputs(world, seen)),
              strategies=run_tests.REG)  # fmt: skip
    assert ok.run_one() == (j, JobState.SUCCEEDED)
    login = logins[1].info.user  # the qw_assessor login
    assert seen == [(world.t, psycopg.pq.TransactionStatus.INTRANS, (login, 1))]
    assert [v.verdict_id for v, *_ in run_tests.stored(logins[1], world)] == ["v-run"]


def test_a_long_run_keeps_its_lease_and_stops_when_it_is_lost(
    logins: tuple[Conn, Conn], conn: Conn, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrk, _ = logins
    beats: list[uuid.UUID] = []
    real = qj.heartbeat
    first = submit(wrk, world.t, JOB.to_payload())

    def counted(c: Conn, job: Any, lease: timedelta) -> Any:
        beats.append(job.id)
        if job.id == first and len(beats) == 2:
            raise LeaseLost(str(job.id))  # lost after the evaluation
        return real(c, job, lease)

    monkeypatch.setattr(qj, "heartbeat", counted)
    lp = loop(logins, source=source(world), strategies=run_tests.REG)
    assert lp.run_one() == (first, None)  # stopped: nothing more is recorded
    assert state(wrk, world.t, first)[0] is JobState.RUNNING  # left for the reaper
    assert run_tests.counts(conn, world.t)["holdout_run_outcome"] == 0
    beats.clear()
    again = submit(wrk, world.t, JOB.to_payload(), rev="r-2")  # the same request
    assert lp.run_one() == (again, JobState.SUCCEEDED)  # attempt 2
    assert beats == [again] * 3  # after the reservation, evaluation and outcome


def test_ordinary_workers_never_claim_an_assessment_job(
    logins: tuple[Conn, Conn], world: Any
) -> None:
    from qw_worker.loop import Worker

    wrk, _ = logins
    jid = submit(wrk, world.t, JOB.to_payload())
    for pool in ("standard", "experiment"):
        s = WorkerSettings(f"synthetic-{pool}", pool=Pool(pool), job_backoff=NOW)
        assert Worker(wrk, s).tick().jobs == []  # no handler for the kind
    assert state(wrk, world.t, jid) == (JobState.QUEUED, None)


# --- the entrypoints (decision 40) ---


@pytest.fixture
def mixed_login(conn: Conn, database_url: str) -> Iterator[Callable[[str], str]]:
    names: list[str] = []

    def make(groups: str) -> str:
        names.append(name := f"qwtest_mix_{uuid.uuid4().hex[:12]}")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE " + groups
                             ).format(sql.Identifier(name)))  # fmt: skip
        return make_conninfo(database_url, user=name)

    yield make
    for name in names:
        kill = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = "
        conn.execute(sql.SQL(kill + "{}").format(sql.Literal(name)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_qw_assessor_holds_exactly_its_two_logins(
    runtime_urls: dict[str, str], mixed_login: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    started: list[qa.AssessorLoop] = []

    def fake_run(self: qa.AssessorLoop, max_ticks: int | None = None) -> int:
        started.append(self)
        return 0

    monkeypatch.setattr(qa.AssessorLoop, "run", fake_run)
    monkeypatch.setattr(qa, "_stop_on_signals", lambda loop: None)
    u = runtime_urls
    env = {"QW_WORKER_NAME": "synthetic-assessor"}
    assert qa.main(env | {"QW_DATABASE_URL": u["qw_worker"]}) == 2  # one login
    assert qa.main(env | {qa.ASSESSOR_URL: u["qw_assessor"]}) == 2
    both = mixed_login("qw_worker, qw_assessor")
    wrong = [
        (u["qw_assessor"], u["qw_assessor"]), (u["qw_app"], u["qw_assessor"]),
        (u["qw_worker"], u["qw_worker"]), (u["qw_worker"], u["qw_app"]),
        (u["qw_worker"], u["qw_release"]), (both, u["qw_assessor"]),
        (u["qw_worker"], both), (u["qw_worker"], mixed_login("qw_app, qw_worker")),
    ]  # fmt: skip
    for w_url, a_url in wrong:
        got = qa.main(env | {"QW_DATABASE_URL": w_url, qa.ASSESSOR_URL: a_url})
        assert got == 1 and "refused: " in capsys.readouterr().err
    assert started == []
    ok = env | {"QW_DATABASE_URL": u["qw_worker"], qa.ASSESSOR_URL: u["qw_assessor"]}
    assert qa.main(ok) == 0
    (lp,) = started
    assert lp.strategies is qa.STRATEGIES  # the built-in registry
    assert lp.source is qa.STORE_SOURCE  # the request and input stores


def test_qw_worker_refuses_to_start_with_the_assessor_login_configured(
    runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    from qw_worker import loop as wl

    started: list[object] = []

    def fake_run(self: object, max_ticks: int | None = None) -> int:
        started.append(self)
        return 0

    monkeypatch.setattr(wl.Worker, "run", fake_run)
    monkeypatch.setattr(worker_entry, "install_signal_handlers", lambda worker: None)
    monkeypatch.setenv("QW_WORKER_NAME", "synthetic-main")
    monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_worker"])
    for value in (runtime_urls["qw_assessor"], "host=unused"):  # any value refuses
        monkeypatch.setenv(qa.ASSESSOR_URL, value)
        assert worker_entry.main() == 1
        assert "QW_ASSESSOR_DATABASE_URL" in capsys.readouterr().err
    assert started == []
    monkeypatch.delenv(qa.ASSESSOR_URL)
    assert worker_entry.main() == 0 and len(started) == 1
