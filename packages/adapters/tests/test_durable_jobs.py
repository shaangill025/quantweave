"""Durable jobs, outbox and inbox (0005, qw_adapters.jobs) on a real PostgreSQL 16.

Delivery is at-least-once; these tests show that duplicates are absorbed by the
inbox, not that they never happen. Runtime connections are throwaway non-superuser
LOGIN roles in qw_app / qw_worker (conftest). Tenants and payloads are SYNTHETIC.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_adapters.jobs import ClaimedJob, IdempotencyConflict, LeaseLost, OutboxEvent
from qw_adapters.tenancy import TenantTx, append_audit_event, tenant_transaction
from qw_domain.jobs import TRANSITIONS, Backoff, JobState, Pool, Priority

Conn = psycopg.Connection[TupleRow]
P = Priority
S = JobState
LEASE = timedelta(minutes=5)
NOW = Backoff(timedelta(0), timedelta(0))  # retry immediately (tests only)


def make_tenant(conn: Conn, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, f"SYNTHETIC {name}")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        append_audit_event(tx, "tenant.created", actor_user_id=user)
        return tx.tenant_id, user


def add(conn: Conn, tenant: uuid.UUID, rev: str, prio: P = P.MONITORING,
        max_attempts: int = 3) -> uuid.UUID:  # fmt: skip
    with tenant_transaction(conn, tenant) as tx:
        return qj.enqueue(tx, "synthetic.refresh", rev, prio, {"n": 1}, max_attempts)[0]


def must_claim(conn: Conn, pool: Pool = Pool.STANDARD,
               lease: timedelta = LEASE) -> ClaimedJob:  # fmt: skip
    job = qj.claim(conn, "w1", pool, lease)
    assert job is not None
    return job


def audit_count(conn: Conn, tenant: uuid.UUID) -> int:
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute("SELECT count(*) FROM app.audit_event").fetchone()
    assert row is not None
    return int(row[0]) - 1  # make_tenant writes one


def outbox_rows(conn: Conn, tenant: uuid.UUID) -> list[tuple[object, ...]]:
    with tenant_transaction(conn, tenant) as tx:
        return tx.conn.execute(
            "SELECT id, published_at, attempts FROM app.outbox"
        ).fetchall()


def in_threads(n: int, url: str, body: Callable[[Conn], None]) -> None:
    """Run `body` on n connections at once; re-raise the first failure."""
    barrier, failures = threading.Barrier(n), list[BaseException]()

    def run() -> None:
        with psycopg.connect(url, autocommit=True) as c:
            barrier.wait()
            try:
                body(c)
            except BaseException as exc:
                failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if failures:
        raise failures[0]


def history(conn: Conn, tenant: uuid.UUID, job_id: uuid.UUID) -> tuple[object, ...]:
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT attempts, generation, total_attempts, available_at FROM app.job "
            "WHERE id = %s", (job_id,),
        ).fetchone()  # fmt: skip
    assert row is not None
    return tuple(row)


def state(conn: Conn, tenant: uuid.UUID, job_id: uuid.UUID) -> tuple[S, int] | None:
    with tenant_transaction(conn, tenant) as tx:
        return qj.job_state(tx, job_id)


@pytest.mark.db
def test_enqueue_is_idempotent_per_tenant_kind_revision(app_conn: Conn) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    first = add(app_conn, a, "rev-1")
    assert add(app_conn, a, "rev-1") == first  # an identical repeat: existing job
    add(app_conn, a, "exp", P.EXPERIMENT)
    mismatches = [("rev-1", P.SAFETY, {"n": 1}, 3), ("exp", P.SAFETY, {"n": 1}, 3),
                  ("rev-1", P.MONITORING, {"n": 2}, 3),
                  ("rev-1", P.MONITORING, {"n": 1}, 4)]  # fmt: skip
    for rev, prio, payload, max_attempts in mismatches:
        with pytest.raises(IdempotencyConflict), tenant_transaction(app_conn, a) as tx:
            qj.enqueue(tx, "synthetic.refresh", rev, prio, payload, max_attempts)
    assert add(app_conn, a, "rev-2") != first
    assert add(app_conn, b, "rev-1") != first
    with tenant_transaction(app_conn, a) as tx:
        row = tx.conn.execute(
            "SELECT idempotency_key, priority::text, count(*) OVER () FROM app.job "
            "WHERE id = %s", (first,),
        ).fetchone()  # fmt: skip
        assert row == ("synthetic.refresh:rev-1", "monitoring", 1)
        exp = tx.conn.execute("SELECT pool::text FROM app.job WHERE input_revision "
                              "= 'exp'").fetchone()  # fmt: skip
        assert exp == ("experiment",)  # never moved to the standard pool
        with pytest.raises(TypeError):
            qj.enqueue(tx, "synthetic.x", "r", P.SAFETY, {"price": 1.5})  # type: ignore[dict-item]
    with pytest.raises(errors.CheckViolation), tenant_transaction(app_conn, a) as tx:
        tx.conn.execute(  # a fractional JSON number bypassing the adapter
            "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, "
            "pool, payload, max_attempts) VALUES (gen_random_uuid(), %s, 'x', 'r', "
            "'safety', 'standard', '{\"a\": [1, {\"p\": 0.1}]}', 1)", (a,),
        )  # fmt: skip


@pytest.mark.db
def test_crash_before_commit_leaves_no_outbox_row_and_no_job_change(
    app_conn: Conn, worker_conn: Conn, runtime_urls: dict[str, str], conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    job_id = add(app_conn, tenant, "r1")
    job = must_claim(worker_conn)
    # Rollback after the business write, outbox write and completion.
    with pytest.raises(RuntimeError), tenant_transaction(worker_conn, tenant) as tx:
        append_audit_event(tx, "synthetic.effect")
        qj.write_outbox(tx, "synthetic.done", {"job": str(job_id)})
        qj.complete(tx, job)
        raise RuntimeError("SYNTHETIC crash before commit")
    # Killed backend mid-transaction (the process dies before COMMIT).
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as victim:
        pid = victim.info.backend_pid
        with pytest.raises(psycopg.OperationalError):  # noqa: SIM117
            with tenant_transaction(victim, tenant) as tx:
                append_audit_event(tx, "synthetic.effect")
                qj.write_outbox(tx, "synthetic.done", {})
                qj.complete(tx, job)
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
    assert outbox_rows(worker_conn, tenant) == []
    assert audit_count(app_conn, tenant) == 0
    assert state(app_conn, tenant, job_id) == (S.RUNNING, 1)
    assert qj.heartbeat(worker_conn, job, LEASE) > job.lease_expires_at  # lease intact
    with tenant_transaction(worker_conn, tenant) as tx:  # and the commit path works
        append_audit_event(tx, "synthetic.effect")
        qj.write_outbox(tx, "synthetic.done", {})
        qj.complete(tx, job)
    assert len(outbox_rows(worker_conn, tenant)) == audit_count(app_conn, tenant) == 1
    assert state(app_conn, tenant, job_id) == (S.SUCCEEDED, 1)


def effect(tx: TenantTx, event: OutboxEvent) -> None:
    append_audit_event(tx, "synthetic.effect", target_id=event.id)


@pytest.mark.db
def test_crash_after_commit_before_ack_is_redelivered_and_deduped(
    app_conn: Conn, worker_conn: Conn
) -> None:
    """The worker commits its effect, dies before qj.complete(); the lease expires,
    the job is redelivered and the inbox (keyed by job id) dedupes."""
    tenant, _ = make_tenant(app_conn, "A")
    job_id = add(app_conn, tenant, "r1")
    for expected_attempt, done in ((1, True), (2, False)):
        job = must_claim(worker_conn, lease=timedelta(microseconds=1))
        assert job.attempt == expected_attempt
        as_event = OutboxEvent(job.id, tenant, "synthetic.job", None)
        assert qj.consume(worker_conn, "synthetic.jobs", as_event, effect) is done
        assert qj.reap_expired(worker_conn, NOW) == [(job_id, S.QUEUED)]
    job = must_claim(worker_conn)
    with tenant_transaction(worker_conn, tenant) as tx:
        qj.complete(tx, job)
    assert audit_count(app_conn, tenant) == 1
    assert state(app_conn, tenant, job_id) == (S.SUCCEEDED, 3)


@pytest.mark.db
def test_duplicate_delivery_has_exactly_one_effect(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    event = OutboxEvent(uuid.uuid4(), tenant, "synthetic.changed", {})
    results: list[bool] = []

    def deliver(c: Conn) -> None:
        results.append(qj.consume(c, "synthetic.alerts", event, effect))

    in_threads(6, runtime_urls["qw_worker"], deliver)
    assert sorted(results) == [False] * 5 + [True]
    assert audit_count(app_conn, tenant) == 1
    # Another consumer of the same event has its own dedupe row.
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as c:
        assert qj.consume(c, "synthetic.ledger", event, lambda tx, e: None)
        assert not qj.consume(c, "synthetic.ledger", event, lambda tx, e: None)


@pytest.mark.db
def test_concurrent_claimers_never_share_a_valid_lease(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenants = [make_tenant(app_conn, f"T{i}")[0] for i in range(3)]
    for i in range(60):
        add(app_conn, tenants[i % 3], f"r{i}")
    claimed: list[uuid.UUID] = []

    def work(c: Conn) -> None:
        while (job := qj.claim(c, "w", Pool.STANDARD, LEASE)) is not None:
            claimed.append(job.id)

    in_threads(8, runtime_urls["qw_worker"], work)
    assert len(claimed) == 60 and len(set(claimed)) == 60


@pytest.mark.db
def test_expired_lease_is_reclaimed_and_the_old_holder_is_fenced(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    job_id = add(app_conn, tenant, "r1")
    old = must_claim(worker_conn, lease=timedelta(microseconds=1))
    assert qj.claim(worker_conn, "w2", Pool.STANDARD, LEASE) is None  # not yet reaped
    with pytest.raises(LeaseLost):
        qj.heartbeat(worker_conn, old, LEASE)  # an expired lease is not extended
    with pytest.raises(LeaseLost), tenant_transaction(worker_conn, tenant) as tx:
        qj.complete(tx, old)  # nor completed
    assert qj.reap_expired(worker_conn, NOW) == [(job_id, S.QUEUED)]
    new = must_claim(worker_conn)
    assert (new.id, new.attempt, old.attempt) == (job_id, 2, 1)
    with pytest.raises(LeaseLost), tenant_transaction(worker_conn, tenant) as tx:
        qj.complete(tx, old)  # same worker name, stale token
    assert qj.heartbeat(worker_conn, new, LEASE) >= new.lease_expires_at
    long = qj.heartbeat(worker_conn, new, timedelta(hours=2))
    assert qj.heartbeat(worker_conn, new, timedelta(seconds=1)) == long  # no shrink
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            qj.claim(worker_conn, "w", Pool.STANDARD, bad)
        with pytest.raises(ValueError):
            qj.heartbeat(worker_conn, new, bad)
    for name in ("", "w" * 161):
        with pytest.raises(ValueError):
            qj.claim(worker_conn, name, Pool.STANDARD, LEASE)
    assert qj.reap_expired(worker_conn, NOW) == []  # a valid lease is not reaped


@pytest.mark.db
def test_max_attempts_dead_letters_and_operator_requeues(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    job_id = add(app_conn, tenant, "r1", max_attempts=2)
    trigger_time = history(app_conn, tenant, job_id)[3]
    for expected in (S.QUEUED, S.DEAD_LETTER):
        job = must_claim(worker_conn)
        with tenant_transaction(worker_conn, tenant) as tx:
            assert qj.fail(tx, job, "upstream_timeout", retryable=True,
                        backoff=NOW) is expected  # fmt: skip
    assert qj.claim(worker_conn, "w", Pool.STANDARD, LEASE) is None
    assert state(app_conn, tenant, job_id) == (S.DEAD_LETTER, 2)
    with tenant_transaction(worker_conn, tenant) as tx:
        assert qj.requeue(tx, job_id) and not qj.requeue(tx, job_id)
    # (attempts this generation, generation, total attempts, trigger time)
    assert history(app_conn, tenant, job_id) == (0, 1, 2, trigger_time)
    job = must_claim(worker_conn)
    with tenant_transaction(worker_conn, tenant) as tx:
        assert qj.fail(tx, job, "bad_input", retryable=False) is S.FAILED
    assert history(app_conn, tenant, job_id) == (1, 1, 3, trigger_time)
    # Requeues are bounded: generation 10 is the last.
    capped = add(app_conn, tenant, "capped", max_attempts=1)
    for generation in range(11):
        job = must_claim(worker_conn)
        with tenant_transaction(worker_conn, tenant) as tx:
            qj.fail(tx, job, "upstream_timeout", retryable=True)
            assert qj.requeue(tx, capped) is (generation < 10)
    assert history(app_conn, tenant, capped)[1:3] == (10, 11)
    # Lease expiry also counts: the last attempt's expiry dead-letters.
    add(app_conn, tenant, "r2", max_attempts=1)
    must_claim(worker_conn, lease=timedelta(microseconds=1))
    assert [s for _, s in qj.reap_expired(worker_conn, NOW)] == [S.DEAD_LETTER]
    queued = add(app_conn, tenant, "r3")
    with tenant_transaction(worker_conn, tenant) as tx:
        assert qj.cancel(tx, queued) and not qj.cancel(tx, queued)
        assert not qj.requeue(tx, queued)  # cancelled is terminal


@pytest.mark.db
def test_attempt_history_resets_only_through_requeue(
    app_conn: Conn, worker_conn: Conn
) -> None:
    """Direct qw_worker writes cannot start a generation outside dead_letter ->
    queued, nor move total_attempts apart from attempts."""
    tenant, _ = make_tenant(app_conn, "A")
    running = add(app_conn, tenant, "running")
    must_claim(worker_conn)
    queued = add(app_conn, tenant, "queued")
    bad = ["generation = generation + 1, attempts = 0",
           "attempts = attempts + 1", "total_attempts = total_attempts + 1",
           "attempts = attempts + 1, total_attempts = total_attempts + 2"]  # fmt: skip
    for job_id, change in itertools.product((running, queued), bad):
        with (pytest.raises(errors.RaiseException, match="history"),
              tenant_transaction(worker_conn, tenant) as tx):  # fmt: skip
            tx.conn.execute(f"UPDATE app.job SET {change} WHERE id = %s", (job_id,))
    with tenant_transaction(worker_conn, tenant) as tx:  # a requeue jumping 2
        tx.conn.execute(
            "UPDATE app.job SET state = 'dead_letter', lease_owner = NULL, "
            "lease_expires_at = NULL WHERE id = %s", (running,),
        )  # fmt: skip
    with (pytest.raises(errors.RaiseException, match="history"),
          tenant_transaction(worker_conn, tenant) as tx):  # fmt: skip
        tx.conn.execute(
            "UPDATE app.job SET state = 'queued', attempts = 0, "
            "generation = generation + 2 WHERE id = %s", (running,),
        )  # fmt: skip
    with tenant_transaction(worker_conn, tenant) as tx:
        assert qj.requeue(tx, running)
    assert history(app_conn, tenant, running)[:3] == (0, 1, 1)


@pytest.mark.db
def test_backoff_delays_the_retry(app_conn: Conn, worker_conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    add(app_conn, tenant, "r1")
    job = must_claim(worker_conn)
    hour = Backoff(timedelta(hours=1), timedelta(hours=1))
    with tenant_transaction(worker_conn, tenant) as tx:
        qj.fail(tx, job, "rate_limited", retryable=True, backoff=hour)
        row = tx.conn.execute(
            "SELECT next_available_at - now() BETWEEN interval '30 minutes' AND "
            "interval '1 hour', last_error FROM app.job"
        ).fetchone()
    assert row == (True, "rate_limited")
    assert qj.claim(worker_conn, "w", Pool.STANDARD, LEASE) is None


@pytest.mark.db
def test_priority_order(app_conn: Conn, worker_conn: Conn) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    add(app_conn, a, "served", P.SAFETY)
    must_claim(worker_conn)  # A was just served in the safety class
    order = [P.SCHEDULED_RESEARCH, P.SAFETY, P.INTERACTIVE_RESEARCH, P.MONITORING]
    for i, prio in enumerate(order):
        add(app_conn, b if prio is P.SCHEDULED_RESEARCH else a, f"r{i}", prio)
    got = [must_claim(worker_conn).priority for _ in order]
    assert got == [P.SAFETY, P.MONITORING, P.INTERACTIVE_RESEARCH,
                   P.SCHEDULED_RESEARCH]  # fmt: skip


@pytest.mark.db
def test_tenant_fairness_round_robin(app_conn: Conn, worker_conn: Conn) -> None:
    big, _ = make_tenant(app_conn, "big")
    small, _ = make_tenant(app_conn, "small")
    with tenant_transaction(app_conn, big) as tx:
        for i in range(1000):
            qj.enqueue(tx, "synthetic.refresh", f"r{i}", P.MONITORING, {})
    add(app_conn, small, "only")
    first_two = {must_claim(worker_conn).tenant_id for _ in range(2)}
    assert first_two == {big, small}  # small is served within two claims
    add(app_conn, small, "later")  # arrives behind 998 queued jobs of `big`
    assert {must_claim(worker_conn).tenant_id for _ in range(2)} == {big, small}
    add(app_conn, small, "urgent", P.SAFETY)  # priority still beats fairness
    assert must_claim(worker_conn).tenant_id == small


@pytest.mark.db
def test_experiment_pool_is_separate(app_conn: Conn, worker_conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    job_id = add(app_conn, tenant, "exp", P.EXPERIMENT)
    assert qj.claim(worker_conn, "std", Pool.STANDARD, LEASE) is None
    assert must_claim(worker_conn, Pool.EXPERIMENT).id == job_id
    add(app_conn, tenant, "std", P.SAFETY)
    assert qj.claim(worker_conn, "exp", Pool.EXPERIMENT, LEASE) is None
    with (
        pytest.raises(errors.CheckViolation),
        tenant_transaction(app_conn, tenant) as tx,
    ):
        tx.conn.execute(
            "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, "
            "pool, payload, max_attempts) VALUES (gen_random_uuid(), %s, 'x', 'r', "
            "'experiment', 'standard', '{}', 1)", (tenant,),
        )  # fmt: skip


@pytest.mark.db
def test_cross_tenant_reads_and_writes_are_impossible(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    add(app_conn, a, "r1")
    with tenant_transaction(app_conn, a) as tx:
        qj.write_outbox(tx, "synthetic.changed", {})
    job = must_claim(worker_conn)
    forged = ClaimedJob(**{**{f: getattr(job, f) for f in job.__slots__},
                           "tenant_id": b})  # fmt: skip
    with pytest.raises(LeaseLost), tenant_transaction(worker_conn, b) as tx:
        qj.complete(tx, forged)  # B's context cannot see A's job
    with pytest.raises(LeaseLost):
        qj.heartbeat(worker_conn, forged, LEASE)
    for c, tables in ((app_conn, ("job",)), (worker_conn, ("job", "outbox"))):
        with tenant_transaction(c, b) as tx:
            for table in tables:
                row = tx.conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
                assert row == (0,), table
        row = c.execute("SELECT count(*) FROM app.job").fetchone()  # no context
        assert row == (0,)
    for fn in ("app.job_pick('standard')", "app.job_expired_leases(1)"):
        with pytest.raises(errors.InsufficientPrivilege):
            app_conn.execute(f"SELECT * FROM {fn}")
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("UPDATE app.job SET state = 'cancelled'")
    with pytest.raises(errors.InsufficientPrivilege):
        worker_conn.execute("DELETE FROM app.outbox")
    assert state(app_conn, a, job.id) == (S.RUNNING, 1)
    with pytest.raises(errors.InsufficientPrivilege), tenant_transaction(app_conn, a):
        app_conn.execute("SELECT lease_owner FROM app.job")  # the bearer lease token
    with pytest.raises(errors.InsufficientPrivilege), tenant_transaction(app_conn, a):
        app_conn.execute("SELECT * FROM app.job")
    with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
        conn.execute("SET LOCAL ROLE qw_migrate")  # the owner sees and locks rows
        conn.execute("UPDATE app.job SET updated_at = now()")  # but cannot write


@pytest.mark.db
def test_database_transitions_match_the_domain_table(conn: Conn) -> None:
    """As the superuser (bypasses RLS, not triggers): each (from, to) update is
    accepted by the trigger exactly when qw_domain.jobs.TRANSITIONS allows it."""
    from qw_adapters.migrations import migrate

    migrate(conn)
    tenant = uuid.uuid4()
    conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
    lease = "CASE WHEN %(r)s THEN 'w' END, CASE WHEN %(r)s THEN now() END"
    insert = (
        "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, pool, "
        "payload, max_attempts, attempts, total_attempts, state, lease_owner, "
        "lease_expires_at) VALUES (gen_random_uuid(), %(t)s, 'x', 'r', 'safety', "
        f"'standard', '{{}}', 3, 1, 1, %(s)s::app.job_state, {lease})"
    )
    update = (
        "UPDATE app.job SET (state, lease_owner, lease_expires_at, generation, "
        f"attempts) = (%(s)s::app.job_state, {lease}, generation + %(g)s::int, "
        "CASE WHEN %(g)s::int = 1 THEN 0 ELSE attempts END)"
    )  # an operator requeue (dead_letter -> queued) starts a new generation
    for src, dst in itertools.product(S, S):
        with conn.transaction(force_rollback=True):
            conn.execute(insert, {"t": tenant, "s": src, "r": src is S.RUNNING})
            try:
                with conn.transaction():
                    requeue = src is S.DEAD_LETTER and dst is S.QUEUED
                    conn.execute(update, {"s": dst, "r": dst is S.RUNNING,
                                          "g": int(requeue)})  # fmt: skip
                allowed = True
            except errors.RaiseException:
                allowed = False
        assert allowed == (src == dst or dst in TRANSITIONS[src]), (src, dst)
    conn.execute(insert, {"t": tenant, "s": "queued", "r": False})
    for change in ("payload = '{\"x\": 1}'", "available_at = now() - interval '1h'"):
        with pytest.raises(errors.RaiseException, match="immutable"):
            conn.execute(f"UPDATE app.job SET {change}")
    for change in ("attempts = 0", "total_attempts = 0", "generation = 2",
                   "generation = 1, total_attempts = 0"):  # fmt: skip
        with pytest.raises(errors.RaiseException, match="history"):
            conn.execute(f"UPDATE app.job SET {change}")
    conn.execute("DELETE FROM app.job")  # superuser cleanup; runtime roles cannot
    conn.execute(insert, {"t": tenant, "s": "dead_letter", "r": False})
    conn.execute(  # the operator requeue
        "UPDATE app.job SET state = 'queued', attempts = 0, generation = 1 "
        "WHERE state = 'dead_letter'"
    )


@pytest.mark.db
def test_security_definer_functions_are_hardened(conn: Conn) -> None:
    from qw_adapters.migrations import migrate

    migrate(conn)
    rows = conn.execute(
        "SELECT p.proname, p.proowner::regrole::text, p.prosecdef, p.proconfig, "
        "array(SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE a.grantee::regrole::text END FROM "
        "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE a.privilege_type = 'EXECUTE' ORDER BY 1) FROM pg_proc p "
        "WHERE p.pronamespace = 'app'::regnamespace AND p.prosecdef ORDER BY 1"
    ).fetchall()
    jobs = [r for r in rows if r[0].startswith("job_")]
    assert [r[0] for r in jobs] == ["job_expired_leases", "job_pick"]
    for _, owner, secdef, config, grantees in jobs:
        assert (owner, secdef, config) == (
            "qw_migrate",
            True,
            ["search_path=pg_catalog"],
        )
        assert grantees == ["qw_migrate", "qw_worker"]
