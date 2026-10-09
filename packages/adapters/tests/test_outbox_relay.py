"""Outbox relay and retention purges (0006, qw_adapters.jobs) on a real PostgreSQL 16.

The relay is at-least-once: these tests show a crash republishes and the inbox
absorbs the duplicate, not that duplicates never happen. Runtime connections are
non-superuser qw_app / qw_worker logins (conftest); the superuser `conn` only
injects faults and old rows. Tenants and payloads are SYNTHETIC.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_adapters.jobs import OutboxEvent, PublishError, Relayed
from qw_adapters.tenancy import TenantTx, append_audit_event, tenant_transaction
from qw_domain.jobs import Backoff, JobError

Conn = psycopg.Connection[TupleRow]
R = Relayed
NOW = Backoff(timedelta(0), timedelta(0))  # retry immediately (tests only)


def make_tenant(conn: Conn, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, f"SYNTHETIC {name}")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        return tx.tenant_id, user


def emit(conn: Conn, tenant: uuid.UUID, n: int = 1) -> list[uuid.UUID]:
    with tenant_transaction(conn, tenant) as tx:
        return [qj.write_outbox(tx, "synthetic.changed", {"i": i}) for i in range(n)]


def outbox(conn: Conn, tenant: uuid.UUID, event_id: uuid.UUID) -> tuple[object, ...]:
    """(published, parked, attempts, last_error, created_at)"""
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT published_at IS NOT NULL, parked_at IS NOT NULL, attempts, "
            "last_error, created_at FROM app.outbox WHERE id = %s", (event_id,),
        ).fetchone()  # fmt: skip
    assert row is not None
    return tuple(row)


def effects(conn: Conn, tenant: uuid.UUID) -> int:
    with tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT count(*) FROM app.audit_event WHERE action = 'synthetic.effect'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def effect(tx: TenantTx, event: OutboxEvent) -> None:
    append_audit_event(tx, "synthetic.effect", target_id=event.id)


@pytest.mark.db
def test_crash_after_publish_before_mark_republishes_and_inbox_dedupes(
    app_conn: Conn, worker_conn: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    (event_id,) = emit(app_conn, tenant)
    created = outbox(worker_conn, tenant, event_id)[4]
    delivered: list[OutboxEvent] = []

    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as relay:
        pid = relay.info.backend_pid

        def publish_then_die(event: OutboxEvent) -> None:
            delivered.append(event)  # reached the transport ...
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))  # ... then crash

        with pytest.raises(psycopg.OperationalError):
            qj.relay_outbox(relay, publish_then_die)
    assert outbox(worker_conn, tenant, event_id)[:3] == (False, False, 0)
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as relay:
        assert qj.relay_outbox(relay, delivered.append) == [(event_id, R.PUBLISHED)]
        assert qj.relay_outbox(relay, delivered.append) == []  # marked: not again
        assert [e.id for e in delivered] == [event_id, event_id]  # at-least-once
        assert delivered[0] == delivered[1]
        results = [qj.consume(relay, "synthetic.alerts", e, effect) for e in delivered]
    assert results == [True, False]
    assert effects(app_conn, tenant) == 1
    assert outbox(worker_conn, tenant, event_id) == (True, False, 0, None, created)


@pytest.mark.db
def test_publish_failure_backs_off_retries_then_parks(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    first, second = emit(app_conn, tenant, 2)
    created = outbox(worker_conn, tenant, first)[4]

    def down(event: OutboxEvent) -> None:
        if event.id == first:
            raise PublishError("broker_unavailable")

    hour = Backoff(timedelta(hours=1), timedelta(hours=1))
    passed = qj.relay_outbox(worker_conn, down, backoff=hour, max_attempts=3)
    assert dict(passed) == {first: R.RETRY, second: R.PUBLISHED}  # same created_at
    with tenant_transaction(worker_conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT next_attempt_at - now() BETWEEN interval '30 minutes' AND "
            "interval '1 hour' FROM app.outbox WHERE id = %s", (first,),
        ).fetchone()  # fmt: skip
    assert row == (True,)
    assert qj.relay_outbox(worker_conn, down) == []  # not due yet
    with tenant_transaction(worker_conn, tenant) as tx:  # the wait elapses
        tx.conn.execute("UPDATE app.outbox SET next_attempt_at = now() WHERE id = %s",
                        (first,))  # fmt: skip

    def broken(event: OutboxEvent) -> None:
        raise RuntimeError("SYNTHETIC failure with free text that is never stored")

    assert qj.relay_outbox(worker_conn, broken, backoff=NOW, max_attempts=3) == [
        (first, R.RETRY)]  # fmt: skip
    assert outbox(worker_conn, tenant, first)[:4] == (False, False, 2, "publish_failed")
    assert qj.relay_outbox(worker_conn, down, backoff=NOW, max_attempts=3) == [
        (first, R.PARKED)]  # fmt: skip
    assert outbox(worker_conn, tenant, first) == (
        False, True, 3, "broker_unavailable", created)  # fmt: skip
    assert qj.relay_outbox(worker_conn, down, backoff=NOW) == []  # parked stays put
    with pytest.raises(JobError):
        PublishError("Connection refused by 10.0.0.1")  # codes only, no free text
    for bad in ({"limit": 0}, {"max_attempts": 0}):
        with pytest.raises(ValueError):
            qj.relay_outbox(worker_conn, down, **bad)  # type: ignore[arg-type]


@pytest.mark.db
def test_concurrent_relays_publish_each_event_once_per_pass(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    tenants = [make_tenant(app_conn, f"T{i}")[0] for i in range(3)]
    expected = {e for t in tenants for e in emit(app_conn, t, 20)}
    published: list[uuid.UUID] = []
    barrier, failures = threading.Barrier(6), list[BaseException]()

    def publish(event: OutboxEvent) -> None:
        published.append(event.id)
        time.sleep(0.002)  # hold the locks while others pick

    def run() -> None:
        with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as c:
            barrier.wait()
            try:
                qj.relay_outbox(c, publish, limit=15)
            except BaseException as exc:
                failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures
    assert len(published) == len(set(published))  # no row published twice
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as c:
        while qj.relay_outbox(c, lambda e: published.append(e.id)):
            pass
    assert sorted(published) == sorted(expected)  # every row exactly once


@pytest.mark.db
def test_relay_tenant_boundary_and_column_grants(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    (ea,) = emit(app_conn, a)
    (eb,) = emit(app_conn, b)
    seen: list[OutboxEvent] = []
    assert {e for e, _ in qj.relay_outbox(worker_conn, seen.append)} == {ea, eb}
    assert {(e.id, e.tenant_id) for e in seen} == {(ea, a), (eb, b)}
    with tenant_transaction(worker_conn, b) as tx:  # B's context never sees A's row
        assert tx.conn.execute("SELECT id FROM app.outbox").fetchall() == [(eb,)]
        cur = tx.conn.execute("UPDATE app.outbox SET attempts = 9 WHERE id = %s", (ea,))
        assert cur.rowcount == 0
    for column in ("created_at = now()", "payload = '{}'", "tenant_id = tenant_id"):
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tenant_transaction(worker_conn, a) as tx,
        ):
            tx.conn.execute(f"UPDATE app.outbox SET {column}")
    (fresh,) = emit(app_conn, a)
    with (
        pytest.raises(errors.RaiseException, match="final"),
        tenant_transaction(worker_conn, a) as tx,
    ):  # a published event cannot be reopened
        tx.conn.execute("UPDATE app.outbox SET published_at = NULL WHERE id = %s",
                        (ea,))  # fmt: skip
    with (
        pytest.raises(errors.RaiseException, match="decrease"),
        tenant_transaction(worker_conn, a) as tx,
    ):
        tx.conn.execute("UPDATE app.outbox SET attempts = 1 WHERE id = %s", (fresh,))
        tx.conn.execute("UPDATE app.outbox SET attempts = 0 WHERE id = %s", (fresh,))
    for statement in ("SELECT * FROM app.outbox_pick(1)",
                      "UPDATE app.outbox SET attempts = 1"):  # fmt: skip
        with pytest.raises(errors.InsufficientPrivilege):
            app_conn.execute(statement)
    with pytest.raises(errors.RaiseException, match="immutable"):
        conn.execute("UPDATE app.outbox SET created_at = now() - interval '1 day' "
                     "WHERE id = %s", (fresh,))  # fmt: skip


def backdate(conn: Conn, tenant: uuid.UUID, user: uuid.UUID) -> dict[str, uuid.UUID]:
    """As the superuser, insert SYNTHETIC rows of known age (days)."""
    ids = {name: uuid.uuid4() for name in (
        "old_published", "new_published", "old_unpublished", "old_parked",
        "old_dead_job", "old_done_job")}  # fmt: skip
    for name, age, published, parked in (
        ("old_published", 10, 9, None),
        ("new_published", 3, 2, None),
        ("old_unpublished", 40, None, None),
        ("old_parked", 40, None, 39),
    ):
        conn.execute(
            "INSERT INTO app.outbox (id, tenant_id, event_type, payload, created_at, "
            "published_at, parked_at) VALUES (%s, %s, 'synthetic.old', '{}', "
            "now() - make_interval(days => %s), now() - make_interval(days => %s), "
            "now() - make_interval(days => %s))",
            (ids[name], tenant, age, published, parked),
        )
    for name, state in (("old_dead_job", "dead_letter"), ("old_done_job", "succeeded")):
        conn.execute(
            "INSERT INTO app.job (id, tenant_id, kind, input_revision, priority, pool, "
            "payload, max_attempts, state) VALUES (%s, %s, 'synthetic.x', %s, "
            "'monitoring', 'standard', '{}', 1, %s)",
            (ids[name], tenant, name, state),
        )
    for name, age in ((n, 40) for n in ids):  # inbox rows for every id, 40 days old
        conn.execute(
            "INSERT INTO app.inbox (tenant_id, consumer, event_id, processed_at) "
            "VALUES (%s, 'synthetic.c', %s, now() - make_interval(days => %s))",
            (tenant, ids[name], age),
        )
    ids["recent_inbox"] = uuid.uuid4()
    conn.execute(
        "INSERT INTO app.inbox (tenant_id, consumer, event_id, processed_at) "
        "VALUES (%s, 'synthetic.c', %s, now() - interval '20 days')",
        (tenant, ids["recent_inbox"]),
    )
    for key, hours in (("A" * 16, 25), ("B" * 16, 23)):
        conn.execute(
            "INSERT INTO app.idempotency_record (tenant_id, user_id, method, route, "
            "idem_key, request_hash, status_code, response_body, created_at) VALUES "
            "(%s, %s, 'POST', '/synthetic', %s, %s, 201, '', "
            "now() - make_interval(hours => %s))",
            (tenant, user, key, bytes(32), hours),
        )
    return ids


@pytest.mark.db
def test_purge_applies_windows_and_keeps_redeliverable_rows(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant, user = make_tenant(app_conn, "A")
    ids = backdate(conn, tenant, user)
    conn.execute("INSERT INTO app.installation_bootstrap (tenant_id) VALUES (%s)",
                 (tenant,))  # fmt: skip
    assert qj.purge(worker_conn) == qj.Purged(outbox=1, inbox=3, idempotency=1)
    with tenant_transaction(app_conn, tenant) as tx:
        keys = tx.conn.execute("SELECT idem_key FROM app.idempotency_record").fetchall()
    assert keys == [("B" * 16,)]  # 23 h old: kept
    with tenant_transaction(worker_conn, tenant) as tx:
        events = {r[0] for r in tx.conn.execute("SELECT id FROM app.outbox")}
        inbox = {r[0] for r in tx.conn.execute("SELECT event_id FROM app.inbox")}
    names = {v: k for k, v in ids.items()}
    assert {names[e] for e in events} == {"new_published", "old_unpublished",
                                          "old_parked"}  # fmt: skip
    # Deleted: the old inbox rows of published events and of a finished job.
    # Kept: events that can still be delivered from here (unpublished, parked), a
    # dead-lettered job (an operator requeue redelivers it), rows inside the window.
    assert {names[e] for e in inbox} == {"old_unpublished", "old_parked",
                                         "old_dead_job", "recent_inbox"}  # fmt: skip
    assert qj.purge(worker_conn, inbox=timedelta(days=7)) == qj.Purged(0, 1, 0)
    row = conn.execute("SELECT count(*) FROM app.installation_bootstrap").fetchone()
    assert row == (1,)  # the bootstrap marker is never purged


@pytest.mark.db
def test_purge_refuses_short_windows_and_runtime_roles_cannot_delete(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant, user = make_tenant(app_conn, "A")
    backdate(conn, tenant, user)
    for kwargs in ({"outbox": timedelta(hours=23)}, {"inbox": timedelta(days=6)},
                   {"idempotency": timedelta(hours=23)}):  # fmt: skip
        with pytest.raises(ValueError):
            qj.purge(worker_conn, **kwargs)
    for call in ("outbox_purge('23 hours')", "inbox_purge('6 days')",
                 "idempotency_purge('23 hours')", "inbox_purge(NULL)"):  # fmt: skip
        with pytest.raises(errors.RaiseException, match="at least"):
            worker_conn.execute(f"SELECT app.{call}")
    for c in (app_conn, worker_conn):
        for table in ("outbox", "inbox", "idempotency_record", "job"):
            with (
                pytest.raises(errors.InsufficientPrivilege),
                tenant_transaction(c, tenant),
            ):
                c.execute(f"DELETE FROM app.{table}")
    for fn in ("outbox_purge('7 days')", "inbox_purge('30 days')",
               "idempotency_purge('1 day')", "job_overdue(1)"):  # fmt: skip
        with pytest.raises(errors.InsufficientPrivilege):
            app_conn.execute(f"SELECT app.{fn}")
    # Even the owner deletes only what the fixed policies allow.
    with conn.transaction():
        conn.execute("SET LOCAL ROLE qw_migrate")
        for table in ("outbox", "inbox", "idempotency_record"):
            conn.execute(f"DELETE FROM app.{table}")
    row = conn.execute(
        "SELECT (SELECT count(*) FROM app.outbox), (SELECT count(*) FROM app.inbox), "
        "(SELECT count(*) FROM app.idempotency_record)"
    ).fetchone()
    # Outbox: unpublished and parked. Inbox: unpublished, parked, dead-lettered job.
    assert row == (2, 3, 1)
