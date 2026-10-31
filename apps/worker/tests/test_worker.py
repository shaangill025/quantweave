"""Worker loop (T025 increment 3) on a really migrated PostgreSQL 16 database. The
worker connects as the non-superuser qw_worker login of the adapters harness; the
superuser `conn` only injects faults and backdated rows. Ticks are driven one at a
time (no sleeping loop); times are relative to the database clock. Tenants, kinds
and payloads are SYNTHETIC.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import signal
import socket
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_adapters.jobs import ClaimedJob, OutboxEvent, Purged, Relayed
from qw_adapters.tenancy import Conn, TenantTx, append_audit_event, tenant_transaction
from qw_domain.jobs import Backoff, JobState, Priority
from qw_domain.retention_config import DEFAULT_WINDOWS, WINDOW_FLOORS, Window
from qw_worker import (
    HANDLERS,
    HandlerFailed,
    Worker,
    WorkerSettings,
    loop,
    settings_from_env,
)
from qw_worker import __main__ as entry
from qw_worker.loop import install_signal_handlers

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn, worker_conn = _harness.app_conn, _harness.worker_conn

S = JobState
NOW = Backoff(timedelta(0), timedelta(0))  # retry immediately (tests only)
SETTINGS = WorkerSettings("synthetic-w1", job_backoff=NOW, relay_backoff=NOW)


def make_tenant(conn: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        return tx.tenant_id, user


def enqueue(conn: Conn, tenant: uuid.UUID, kind: str, rev: str, **kw: Any) -> uuid.UUID:
    with tenant_transaction(conn, tenant) as tx:
        return qj.enqueue(tx, kind, rev, Priority.MONITORING, {"v": "1.50"}, **kw)[0]


def job_row(conn: Conn, tenant: uuid.UUID, job_id: uuid.UUID) -> tuple[Any, ...]:
    """(state, attempts, last_error)"""
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT state::text, attempts, last_error FROM app.job WHERE id = %s",
            (job_id,),
        ).fetchone()
    assert row is not None
    return (S(row[0]), row[1], row[2])


def audits(conn: Conn, tenant: uuid.UUID, action: str) -> int:
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT count(*) FROM app.audit_event WHERE action = %s", (action,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def emit(conn: Conn, tenant: uuid.UUID, n: int) -> list[uuid.UUID]:
    with tenant_transaction(conn, tenant) as tx:
        return [qj.write_outbox(tx, "synthetic.changed", {"i": i}) for i in range(n)]


def published(conn: Conn, tenant: uuid.UUID) -> set[uuid.UUID]:
    with tenant_transaction(conn, tenant) as tx:
        rows = tx.conn.execute(
            "SELECT id FROM app.outbox WHERE published_at IS NOT NULL"
        ).fetchall()
    return {r[0] for r in rows}


class Recorder:
    """Test double publish target: keeps events in memory, sends nothing."""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    def __call__(self, event: OutboxEvent) -> None:
        self.events.append(event)


def outbox_rows(conn: Conn, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    with tenant_transaction(conn, tenant) as tx:
        return tx.conn.execute(
            "SELECT id, published_at, parked_at, attempts, next_attempt_at, "
            "last_error, created_at FROM app.outbox ORDER BY id"
        ).fetchall()


def handled(tx: TenantTx, job: ClaimedJob) -> None:
    append_audit_event(tx, "synthetic.handled", target_id=job.id)


@pytest.mark.db
def test_tick_runs_a_registered_handler_and_marks_the_job_succeeded(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn)
    ok = enqueue(app_conn, tenant, "synthetic.echo", "r1")
    bad = enqueue(app_conn, tenant, "synthetic.refuse", "r1")
    seen: list[ClaimedJob] = []

    def refuse(tx: TenantTx, job: ClaimedJob) -> None:
        append_audit_event(tx, "synthetic.handled", target_id=job.id)  # rolled back
        raise HandlerFailed("synthetic_invalid", retryable=False)

    def echo(tx: TenantTx, job: ClaimedJob) -> None:
        seen.append(job)
        handled(tx, job)

    handlers = {"synthetic.echo": echo, "synthetic.refuse": refuse}
    tick = Worker(worker_conn, SETTINGS, handlers=handlers).tick()
    assert dict(tick.jobs) == {ok: S.SUCCEEDED, bad: S.FAILED}
    assert [(j.id, j.payload, j.attempt) for j in seen] == [(ok, {"v": "1.50"}, 1)]
    assert job_row(app_conn, tenant, ok) == (S.SUCCEEDED, 1, None)
    assert job_row(app_conn, tenant, bad) == (S.FAILED, 1, "synthetic_invalid")
    assert audits(app_conn, tenant, "synthetic.handled") == 1  # only echo's effect
    assert Worker(worker_conn, SETTINGS, handlers=handlers).tick().jobs == []


@pytest.mark.db
def test_unknown_kind_is_never_claimed_and_a_crashing_handler_never_succeeds(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    assert dict(HANDLERS) == {}  # no domain handler exists yet: the default is empty
    tenant, _ = make_tenant(app_conn)
    unknown = enqueue(app_conn, tenant, "synthetic.unknown", "r1", max_attempts=2)
    snapshot = ("SELECT state::text, attempts, available_at, next_available_at, "
                "claimed_at, updated_at FROM app.job WHERE id = %s")  # fmt: skip
    before = conn.execute(snapshot, (unknown,)).fetchone()
    assert Worker(worker_conn, SETTINGS).tick().jobs == []  # empty registry
    boom = enqueue(app_conn, tenant, "synthetic.boom", "r1", max_attempts=3)

    def crash(tx: TenantTx, job: ClaimedJob) -> None:
        handled(tx, job)
        raise RuntimeError("SYNTHETIC crash text that is never stored")

    one = WorkerSettings("synthetic-w1", jobs_per_tick=1, job_backoff=NOW)
    worker = Worker(worker_conn, one, handlers={"synthetic.boom": crash})
    assert worker.tick().jobs == [(boom, S.QUEUED)]  # past the unknown head job
    assert job_row(app_conn, tenant, boom) == (S.QUEUED, 1, "handler_failed")
    assert audits(app_conn, tenant, "synthetic.handled") == 0
    assert job_row(app_conn, tenant, unknown) == (S.QUEUED, 0, None)
    assert conn.execute(snapshot, (unknown,)).fetchone() == before


@pytest.mark.db
def test_workers_with_disjoint_registries_claim_only_their_own_kinds(
    app_conn: Conn, worker_conn: Conn
) -> None:
    a, _ = make_tenant(app_conn)
    b, _ = make_tenant(app_conn)
    left = [enqueue(app_conn, t, "synthetic.left", f"r{t}") for t in (a, b)]
    right = [enqueue(app_conn, t, "synthetic.right", f"r{t}") for t in (a, b)]
    ran: dict[str, list[uuid.UUID]] = {"left": [], "right": []}

    def worker(side: str) -> Worker:
        return Worker(worker_conn, SETTINGS, handlers={
            f"synthetic.{side}": lambda tx, job: ran[side].append(job.id)})  # fmt: skip

    lefty, righty = worker("left"), worker("right")
    assert sorted(lefty.tick().jobs) == sorted((j, S.SUCCEEDED) for j in left)
    assert [job_row(app_conn, t, j) for t, j in zip((a, b), right, strict=True)] == [
        (S.QUEUED, 0, None)] * 2  # fmt: skip
    assert lefty.tick().jobs == []
    assert sorted(righty.tick().jobs) == sorted((j, S.SUCCEEDED) for j in right)
    assert sorted(ran["left"]) == sorted(left)
    assert sorted(ran["right"]) == sorted(right)


@pytest.mark.db
def test_a_claimed_kind_without_a_handler_still_fails_closed(
    app_conn: Conn, worker_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, _ = make_tenant(app_conn)
    stray = enqueue(app_conn, tenant, "synthetic.stray", "r1", max_attempts=2)
    real_claim = qj.claim

    def leaky(c: Conn, worker: str, pool: Any, lease: timedelta,
              kinds: tuple[str, ...]) -> ClaimedJob | None:  # fmt: skip
        return real_claim(c, worker, pool, lease, (*kinds, "synthetic.stray"))

    monkeypatch.setattr(qj, "claim", leaky)  # as if the kind filter were lost
    worker = Worker(worker_conn, SETTINGS, handlers={"synthetic.echo": handled})
    # Never succeeded: retried with backoff, then dead-lettered (operator requeue).
    assert worker.tick().jobs == [(stray, S.QUEUED), (stray, S.DEAD_LETTER)]
    assert job_row(app_conn, tenant, stray) == (S.DEAD_LETTER, 2, "no_handler")


@pytest.mark.db
def test_relay_commits_each_event_alone_so_a_crash_republishes_one(
    app_conn: Conn, worker_conn: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenant, _ = make_tenant(app_conn)
    emit(app_conn, tenant, 3)
    first: list[uuid.UUID] = []
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as relay:
        pid = relay.info.backend_pid

        def die_on_second(event: OutboxEvent) -> None:
            first.append(event.id)  # reached the transport ...
            if len(first) == 2:  # ... then the worker dies before marking it
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))

        with pytest.raises(psycopg.OperationalError):
            Worker(relay, SETTINGS, publish=die_on_second).tick()
    assert published(worker_conn, tenant) == {first[0]}  # its own committed mark
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as relay:
        sink = Recorder()
        tick = Worker(relay, SETTINGS, publish=sink).tick()
    again = [e.id for e in sink.events]
    assert again[0] == first[1] and len(again) == 2  # only the in-flight event twice
    assert first[0] not in again
    assert [o for _, o in tick.relayed] == [Relayed.PUBLISHED] * 2
    assert len(published(worker_conn, tenant)) == 3


@pytest.mark.db
def test_relay_per_tick_bounds_events_and_failures_back_off(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn)
    ids = emit(app_conn, tenant, 3)
    cfg = WorkerSettings("synthetic-w1", relay_per_tick=2, relay_max_attempts=1)

    def down(event: OutboxEvent) -> None:
        raise qj.PublishError("synthetic_down")

    assert len(Worker(worker_conn, cfg, publish=down).tick().relayed) == 2
    rest = Worker(worker_conn, cfg, publish=down).tick().relayed
    assert [o for _, o in rest] == [Relayed.PARKED]  # max attempts 1: parked
    assert published(worker_conn, tenant) == set() and len(ids) == 3


def backdate(conn: Conn, tenant: uuid.UUID, user: uuid.UUID, hours: int) -> None:
    """As the superuser: one published outbox row 9 days old and one idempotency
    record `hours` old (SYNTHETIC)."""
    conn.execute(
        "INSERT INTO app.outbox (id, tenant_id, event_type, payload, created_at, "
        "published_at) VALUES (%s, %s, 'synthetic.old', '{}', now() - interval "
        "'10 days', now() - interval '9 days')",
        (uuid.uuid4(), tenant),
    )
    conn.execute(
        "INSERT INTO app.idempotency_record (tenant_id, user_id, method, route, "
        "idem_key, request_hash, status_code, response_body, created_at) VALUES "
        "(%s, %s, 'POST', '/synthetic', %s, %s, 201, '', "
        "now() - make_interval(hours => %s))",
        (tenant, user, uuid.uuid4().hex, bytes(32), hours),
    )


@pytest.mark.db
def test_purges_run_on_the_configured_schedule_with_48h_idempotency(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant, user = make_tenant(app_conn)
    row = worker_conn.execute("SELECT now()").fetchone()
    assert row is not None
    now: list[datetime] = [row[0]]  # a clock that starts at the database clock
    worker = Worker(worker_conn, SETTINGS, clock=lambda: now[0])
    backdate(conn, tenant, user, 30)  # 30 h: inside the 48 h default, kept
    backdate(conn, tenant, user, 50)
    assert worker.tick().purged == Purged(outbox=2, inbox=0, idempotency=1)
    backdate(conn, tenant, user, 50)
    now[0] += timedelta(minutes=59)
    assert worker.tick().purged is None  # purge_every (1 h) not yet elapsed
    now[0] += timedelta(minutes=1)
    assert worker.tick().purged == Purged(outbox=1, inbox=0, idempotency=1)
    now[0] += timedelta(minutes=30)
    assert worker.tick().purged is None
    with tenant_transaction(app_conn, tenant) as tx:
        left = tx.conn.execute("SELECT count(*) FROM app.idempotency_record").fetchone()
    assert left == (1,)  # the 30 h record
    short = Worker(worker_conn, WorkerSettings(
        "synthetic-w1", idempotency_retention=timedelta(hours=24)))  # fmt: skip
    assert short.tick().purged == Purged(outbox=0, inbox=0, idempotency=1)


def retention_file(tmp_path: Path, **fields: object) -> dict[str, str]:
    """A SYNTHETIC operator retention config (owner decision 9) and its env var."""
    body: dict[str, object] = {"policy_id": "synthetic-ops-2", "version": 2}
    body.update(fields)
    path = tmp_path / "retention.json"
    path.write_text(json.dumps(body))
    return {"QW_WORKER_NAME": "synthetic-w3", "QW_RETENTION_CONFIG": str(path)}


@pytest.mark.db
def test_purge_hold_in_the_retention_config_beats_the_purge(
    app_conn: Conn, worker_conn: Conn, conn: Conn, tmp_path: Path
) -> None:
    tenant, user = make_tenant(app_conn)
    hold = {"hold_id": "ph-1", "windows": ["idempotency"], "reason": "litigation",
            "authority_id": "counsel-1"}  # fmt: skip
    cfg = settings_from_env(retention_file(tmp_path, purge_holds=[hold]))
    assert cfg.purge_held == frozenset({Window.IDEMPOTENCY})
    backdate(conn, tenant, user, 50)  # past the 48 h window, but held
    got = Worker(worker_conn, cfg).tick().purged
    assert got == Purged(outbox=1, inbox=0, idempotency=0, held=("idempotency",))
    with tenant_transaction(app_conn, tenant) as tx:
        left = tx.conn.execute("SELECT count(*) FROM app.idempotency_record").fetchone()
    assert left == (1,)
    free = settings_from_env(retention_file(tmp_path, windows={"idempotency": "49h"}))
    assert Worker(worker_conn, free).tick().purged == Purged(0, 0, 1)


def test_worker_reads_the_retention_config(tmp_path: Path) -> None:
    got = settings_from_env(retention_file(
        tmp_path, windows={"idempotency": "72h", "inbox": "45d"}))  # fmt: skip
    assert (got.idempotency_retention, got.outbox_retention, got.inbox_retention,
            got.purge_held) == (timedelta(hours=72), timedelta(days=7),
                                timedelta(days=45), frozenset())  # fmt: skip
    env = retention_file(tmp_path)
    for extra in ({"QW_WORKER_IDEMPOTENCY_RETENTION_HOURS": "72"},
                  {"QW_WORKER_OUTBOX_RETENTION_DAYS": "7"},
                  {"QW_RETENTION_CONFIG": str(tmp_path / "missing.json")}):  # fmt: skip
        with pytest.raises(ValueError):
            settings_from_env({**env, **extra})
    for bad in ({"windows": {"idempotency": "23h"}},
                {"windows": {"idempotency": "-48h"}},
                {"classes": {"journal": {"on_request": "delete"}}}):  # fmt: skip
        with pytest.raises(ValueError):
            settings_from_env(retention_file(tmp_path, **bad))
    with pytest.raises(ValueError):
        WorkerSettings("synthetic-w1", purge_held=frozenset({"idempotency"}))  # type: ignore[arg-type]


def test_worker_defaults_are_the_configured_defaults() -> None:
    s = WorkerSettings("synthetic-w1")
    mine = {
        Window.IDEMPOTENCY: s.idempotency_retention,
        Window.OUTBOX: s.outbox_retention,
        Window.INBOX: s.inbox_retention,
    }
    assert mine == dict(DEFAULT_WINDOWS) == {Window.IDEMPOTENCY: timedelta(hours=48),
        Window.OUTBOX: timedelta(days=7), Window.INBOX: timedelta(days=30)}  # fmt: skip
    assert s.purge_held == frozenset()
    assert dict(WINDOW_FLOORS) == {Window.IDEMPOTENCY: qj.IDEMPOTENCY_RETENTION,
                                   Window.OUTBOX: qj.OUTBOX_RETENTION_MIN,
                                   Window.INBOX: qj.INBOX_RETENTION_MIN}  # fmt: skip


@pytest.mark.db
def test_overdue_queued_jobs_expire_and_never_run(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn)
    overdue = uuid.uuid4()
    conn.execute(  # superuser: a queued job whose start-by deadline passed
        "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, pool, "
        "payload, max_attempts, created_at, deadline_at) VALUES (%s, %s, "
        "'synthetic.echo', 'late', 'monitoring', 'standard', '{}', 3, "
        "now() - interval '1 hour', now() - interval '1 minute')",
        (overdue, tenant),
    )
    row = worker_conn.execute("SELECT now() + interval '1 hour'").fetchone()
    assert row is not None
    later = enqueue(app_conn, tenant, "synthetic.echo", "open", deadline_at=row[0])
    ran: list[uuid.UUID] = []
    worker = Worker(worker_conn, SETTINGS, handlers={
        "synthetic.echo": lambda tx, job: ran.append(job.id)})  # fmt: skip
    tick = worker.tick()
    assert tick.expired == [overdue]
    assert ran == [later] and tick.jobs == [(later, S.SUCCEEDED)]
    assert job_row(app_conn, tenant, overdue) == (S.EXPIRED, 0, "deadline_passed")
    assert worker.tick().expired == []


@pytest.mark.db
def test_sigterm_finishes_the_current_job_and_claims_no_more(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn)
    jobs = [enqueue(app_conn, tenant, "synthetic.echo", f"r{i}") for i in range(3)]
    (event,) = emit(app_conn, tenant, 1)

    def term_mid_job(tx: TenantTx, job: ClaimedJob) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # delivered while this job runs
        handled(tx, job)

    sleeps: list[timedelta] = []
    sink = Recorder()  # a publisher is injected, so only the stop keeps the relay off
    worker = Worker(worker_conn, SETTINGS, handlers={"synthetic.echo": term_mid_job},
                    publish=sink, sleep=sleeps.append)  # fmt: skip
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        install_signal_handlers(worker)
        assert worker.run() == 1  # returns by itself: no max_ticks
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    states = sorted(job_row(app_conn, tenant, j)[:2] for j in jobs)
    assert states == [(S.QUEUED, 0), (S.QUEUED, 0), (S.SUCCEEDED, 1)]
    assert audits(app_conn, tenant, "synthetic.handled") == 1  # finished, committed
    assert published(worker_conn, tenant) == set() and sink.events == []  # no relay
    assert event
    assert sleeps == [] and worker.tick().jobs == []


@pytest.mark.db
def test_idle_run_waits_between_ticks_without_sleeping(worker_conn: Conn) -> None:
    sleeps: list[timedelta] = []
    worker = Worker(worker_conn, SETTINGS, sleep=sleeps.append)
    assert worker.run(max_ticks=3) == 3
    assert sleeps == [SETTINGS.idle_wait] * 3


@pytest.mark.db
def test_without_a_publisher_the_relay_is_off_and_events_stay_pending(
    app_conn: Conn, worker_conn: Conn, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    tenant, _ = make_tenant(app_conn)
    emit(app_conn, tenant, 2)
    before = outbox_rows(worker_conn, tenant)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("SYNTHETIC guard: the worker used the network")

    for name in ("socket", "create_connection", "getaddrinfo", "socketpair"):
        monkeypatch.setattr(socket, name, no_network)
    row = worker_conn.execute("SELECT now()").fetchone()
    assert row is not None
    now: list[datetime] = [row[0]]

    def clock() -> datetime:
        now[0] += timedelta(hours=2)  # every tick is a purge tick
        return now[0]

    worker = Worker(worker_conn, SETTINGS, clock=clock)  # no publisher injected
    assert worker.publish is None
    with caplog.at_level(logging.DEBUG, "qw_worker"):
        ticks = [worker.tick() for _ in range(3)]
    assert [t.relayed for t in ticks] == [[], [], []]
    assert all(t.purged is not None for t in ticks)  # purges ran, events kept
    assert published(worker_conn, tenant) == set()
    assert outbox_rows(worker_conn, tenant) == before  # untouched, still pending
    assert not [m for m in caplog.messages if "synthetic.changed" in m or "'i'" in m]
    with pytest.raises(AssertionError):
        socket.create_connection(("192.0.2.1", 9))  # the guard is live


@pytest.mark.db
def test_injected_publisher_commits_each_event_before_the_next(
    app_conn: Conn, worker_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenant, _ = make_tenant(app_conn)
    emit(app_conn, tenant, 3)
    seen: list[tuple[uuid.UUID, set[uuid.UUID]]] = []

    def observe(event: OutboxEvent) -> None:  # what other sessions see as committed
        seen.append((event.id, published(worker_conn, tenant)))

    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as relay:
        tick = Worker(relay, SETTINGS, publish=observe).tick()
    order = [i for i, _ in seen]
    assert [c for _, c in seen] == [set(), {order[0]}, {order[0], order[1]}]
    assert [i for i, _ in tick.relayed] == order
    assert published(worker_conn, tenant) == set(order)


@pytest.mark.db
def test_lease_lost_inside_a_handler_rolls_back_and_leaves_it_to_the_reaper(
    app_conn: Conn, worker_conn: Conn, conn: Conn, caplog: pytest.LogCaptureFixture
) -> None:
    tenant, _ = make_tenant(app_conn)
    job_id = enqueue(app_conn, tenant, "synthetic.slow", "r1")

    def outlive_lease(tx: TenantTx, job: ClaimedJob) -> None:
        handled(tx, job)
        conn.execute(  # superuser: the lease runs out while the handler works
            "UPDATE app.job SET lease_expires_at = now() - interval '1 second' "
            "WHERE id = %s", (job.id,),
        )  # fmt: skip

    worker = Worker(worker_conn, SETTINGS, handlers={"synthetic.slow": outlive_lease})
    with caplog.at_level(logging.INFO, "qw_worker"):
        assert worker.tick().jobs == [(job_id, None)]
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", f"job {job_id} lost its lease; its effects rolled back")
    ]
    assert job_row(app_conn, tenant, job_id) == (S.RUNNING, 1, None)
    assert audits(app_conn, tenant, "synthetic.handled") == 0
    assert qj.reap_expired(worker_conn, NOW) == [(job_id, S.QUEUED)]
    assert job_row(app_conn, tenant, job_id) == (S.QUEUED, 1, "lease_expired")


@pytest.mark.db
def test_entry_point_wires_no_publisher_and_rejects_overflowing_env(
    runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    started: list[Worker] = []

    def fake_run(self: Worker, max_ticks: int | None = None) -> int:
        started.append(self)
        return 0

    monkeypatch.setattr(loop.Worker, "run", fake_run)
    monkeypatch.setattr(entry, "install_signal_handlers", lambda worker: None)
    monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_worker"])
    monkeypatch.setenv("QW_WORKER_NAME", "synthetic-main")
    assert entry.main() == 0
    assert [w.publish for w in started] == [None]  # no transport is authorised
    monkeypatch.setenv("QW_WORKER_INBOX_RETENTION_DAYS", "9" * 30)
    assert entry.main() == 2
    assert "invalid worker settings" in capsys.readouterr().err
    assert len(started) == 1


@pytest.mark.db
def test_entry_point_refuses_a_login_that_is_not_exactly_qw_worker(
    runtime_urls: dict[str, str], conn: Conn, database_url: str,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """Owner decision 17: the worker declares role="qw_worker", so a qw_app,
    qw_assessor or mixed login is refused at startup, before any tick."""
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    started: list[Worker] = []

    def fake_run(self: Worker, max_ticks: int | None = None) -> int:
        started.append(self)
        return 0

    monkeypatch.setattr(loop.Worker, "run", fake_run)
    monkeypatch.setattr(entry, "install_signal_handlers", lambda worker: None)
    monkeypatch.setenv("QW_WORKER_NAME", "synthetic-main")
    mixed = f"qwtest_mixed_{uuid.uuid4().hex[:12]}"
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE qw_app, qw_worker"
                         ).format(sql.Identifier(mixed)))  # fmt: skip
    try:
        wrong = [runtime_urls["qw_app"], runtime_urls["qw_assessor"],
                 make_conninfo(runtime_urls["qw_app"], user=mixed)]  # fmt: skip
        for url in wrong:
            monkeypatch.setenv("QW_DATABASE_URL", url)
            assert entry.main() == 1
            assert "refused: " in capsys.readouterr().err
        assert started == []
        monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_worker"])
        assert entry.main() == 0
        assert len(started) == 1
    finally:
        terminate = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
        conn.execute(sql.SQL(terminate + "usename = {}").format(sql.Literal(mixed)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(mixed)))


CANARY = "SYNTHETIC-canary-worker-5d1f8b2a"


def test_entry_point_reads_a_passfile_only_url_and_checks_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """T060: QW_DATABASE_URL goes through the same file_secrets passfile rules as
    qw-api and qw-migrate, refused (exit 2) before any connection; `--check-config`
    checks the configuration and never connects (no server is at this host)."""
    for name in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE",
                 "QW_ASSESSOR_DATABASE_URL"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    passfile = tmp_path / "worker.pgpass"
    passfile.write_text(f"*:*:*:qw_worker_login:{CANARY}\n")
    passfile.chmod(0o600)
    url = f"host={tmp_path / 'no-socket'} dbname=qw user=qw_worker_login"
    good = f"{url} passfile={passfile}"
    monkeypatch.setenv("QW_DATABASE_URL", good)
    assert entry.main(["--check-config"]) == 0
    assert capsys.readouterr().out == "config ok: qw-worker\n"
    assert entry.main(["--check-config", "extra"]) == 2
    monkeypatch.setenv("QW_WORKER_POOL", "gpu")
    assert entry.main(["--check-config"]) == 2
    assert "invalid worker settings" in capsys.readouterr().err
    monkeypatch.delenv("QW_WORKER_POOL")
    for bad in (url, f"{good} password={CANARY}",
                f"postgresql://w:{CANARY}@db/qw?passfile={passfile}"):  # fmt: skip
        monkeypatch.setenv("QW_DATABASE_URL", bad)
        for argv in ([], ["--check-config"]):
            assert entry.main(argv) == 2
            err = capsys.readouterr().err
            assert err.startswith("refused: QW_DATABASE_URL") and CANARY not in err
    monkeypatch.setenv("QW_DATABASE_URL", good)
    monkeypatch.setenv("PGPASSWORD", CANARY)
    assert entry.main([]) == 2
    assert capsys.readouterr().err.startswith("refused: PGPASSWORD")
    monkeypatch.delenv("PGPASSWORD")
    passfile.chmod(0o644)
    assert entry.main(["--check-config"]) == 2
    assert "group or world" in capsys.readouterr().err


def test_settings_validate_and_env_overrides() -> None:
    assert SETTINGS.idempotency_retention == timedelta(hours=48)
    for bad in ({"idempotency_retention": timedelta(hours=23)},
                {"inbox_retention": timedelta(days=6)},
                {"outbox_retention": timedelta(hours=23)},
                {"purge_every": timedelta(seconds=30)},
                {"relay_per_tick": 0}, {"jobs_per_tick": 1001},
                {"lease": timedelta(0)}, {"pool": "standard"}):  # fmt: skip
        with pytest.raises(ValueError):
            WorkerSettings("synthetic-w1", **bad)
    for name in ("", "a b", "x" * 161, "-lead"):
        with pytest.raises(ValueError):
            WorkerSettings(name)
    env = {"QW_WORKER_NAME": "synthetic-w2", "QW_WORKER_POOL": "experiment",
           "QW_WORKER_PURGE_EVERY_MINUTES": "15", "QW_WORKER_RELAY_PER_TICK": "3",
           "QW_WORKER_IDEMPOTENCY_RETENTION_HOURS": "72"}  # fmt: skip
    got = settings_from_env(env)
    assert (got.worker_name, got.pool.value, got.purge_every, got.relay_per_tick,
            got.idempotency_retention) == ("synthetic-w2", "experiment",
            timedelta(minutes=15), 3, timedelta(hours=72))  # fmt: skip
    for key, value in (("QW_WORKER_RELAY_PER_TICK", "-1"),
                       ("QW_WORKER_INBOX_RETENTION_DAYS", "9" * 30),
                       ("QW_WORKER_IDEMPOTENCY_RETENTION_HOURS", "9" * 5000),
                       ("QW_WORKER_PURGE_EVERY_MINUTES", "1.5"),
                       ("QW_WORKER_IDEMPOTENCY_RETENTION_HOURS", "12"),
                       ("QW_WORKER_POOL", "gpu")):  # fmt: skip
        with pytest.raises(ValueError):
            settings_from_env({key: value})
    assert settings_from_env({}).idempotency_retention == timedelta(hours=48)
