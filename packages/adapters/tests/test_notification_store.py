"""Persisted notifications (T056 increments 2-3; 0017_notifications.sql,
0018_notification_seal_kid.sql) on a migrated PostgreSQL 16 database: the worker
records and appends, the API role reads and records an open. SYNTHETIC tenants,
users, objects, addresses and keys only; every time is taken from the database
clock."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from qw_adapters import notification_store as ns
from qw_adapters import tenancy as tn
from qw_adapters.migrations import MIGRATIONS_DIR, migrate
from qw_domain import notifications as N
from qw_domain.instants import ensure_aware_utc
from qw_domain.notifications import Category, Channel, DeliveryStatus, SendOutcome

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
A, B = b"SYNTHETIC-seal-key-A-000000000000", b"SYNTHETIC-seal-key-B-000000000000"
ID_KEY = b"SYNTHETIC-notification-id-key-0001"
KEY = ns.Keyring("syn-a", {"syn-a": A}, ID_KEY)
ON = N.Preferences(email=True, browser_push=True)
PUSH = N.PushSubscription("https://push.example.invalid", "granted", True, None, False)


class Tx:
    """SYNTHETIC transport: answers from a script, records nothing external."""

    def __init__(self, *answers: SendOutcome) -> None:
        self.answers = list(answers)

    def send(self, message: N.ExternalMessage, r: N.Recipient) -> SendOutcome:
        return self.answers.pop(0) if self.answers else SendOutcome.ACCEPTED


def now(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


Ids = tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]


def people(app_conn: Conn) -> Ids:
    """Tenant A with users u1 and u2, tenant B with u3."""
    with tn.provision_tenant(app_conn) as a:
        u1, u2 = tn.create_user(a, "SYNTHETIC u1"), tn.create_user(a, "SYNTHETIC u2")
    with tn.provision_tenant(app_conn) as b:
        u3 = tn.create_user(b, "SYNTHETIC u3")
    return a.tenant_id, u1, u2, b.tenant_id, u3


def me(tenant: uuid.UUID, user: uuid.UUID, prefs: N.Preferences = ON) -> N.Recipient:
    return N.Recipient(str(tenant), str(user), prefs, "s@example.invalid", True, PUSH)


def submit(
    conn: Conn, tenant: uuid.UUID, user: uuid.UUID, obj: str = "job-1",
    prefs: N.Preferences = ON, c: N.Center | None = None,
) -> N.Submitted:  # fmt: skip
    at = now(conn)
    s = N.Signal(str(tenant), Category.JOB_OUTCOME, obj, "succeeded",
                 at - timedelta(seconds=5))  # fmt: skip
    c = c or N.Center(str(tenant), timedelta(0), 3, ID_KEY)
    return c.submit(s, me(tenant, user, prefs), at)


def write(conn: Conn, tenant: uuid.UUID, fn: Callable[..., Any], *args: Any,
          keys: ns.Keyring = KEY) -> Any:  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        return fn(tx, keys, *args)


def code_of(conn: Conn, t: uuid.UUID, fn: Callable[..., Any], *args: Any,
            keys: ns.Keyring = KEY) -> str:  # fmt: skip
    with pytest.raises(ns.NotificationStoreError) as err:
        write(conn, t, fn, *args, keys=keys)
    return err.value.code


def kids(conn: Conn, nid: str) -> list[str | None]:
    """Every seal kid of one record, record row first (owner view)."""
    rows = conn.execute(
        "SELECT 0, '', seal_kid FROM app.notification WHERE id = %(n)s UNION ALL "
        "SELECT 1, channel, seal_kid FROM app.notification_delivery WHERE "
        "notification_id = %(n)s UNION ALL SELECT 2, channel || position, seal_kid "
        "FROM app.notification_delivery_event WHERE notification_id = %(n)s UNION "
        "ALL SELECT 3, '', seal_kid FROM app.notification_seen WHERE "
        "notification_id = %(n)s ORDER BY 1, 2", {"n": nid}).fetchall()  # fmt: skip
    return [r[2] for r in rows]


def unavailable(rows: list[ns.Row]) -> bool:
    return bool(rows) and all(r.item is None for r in rows)


def test_round_trip_events_repeat_and_seen(worker_conn: Conn, app_conn: Conn) -> None:
    t, u1, *_ = people(app_conn)
    c = N.Center(str(t), timedelta(0), 3, ID_KEY)
    sub = submit(worker_conn, t, u1, c=c)
    stored = write(worker_conn, t, ns.record, sub)
    assert (stored.notification, stored.deliveries) == (sub.notification,
                                                        sub.deliveries)  # fmt: skip
    assert [d.channel for d in stored.deliveries] == [Channel.EMAIL,
                                                      Channel.BROWSER_PUSH]  # fmt: skip
    # a delivery attempt is a new event; a rejection then an acceptance
    r = me(t, u1)
    first = c.attempt(sub.notification.id, Channel.EMAIL, Tx(SendOutcome.REJECTED), r,
                      now(worker_conn))  # fmt: skip
    write(worker_conn, t, ns.append, first)
    done = c.attempt(sub.notification.id, Channel.EMAIL, Tx(), r, now(worker_conn))
    got = write(worker_conn, t, ns.append, done)
    email = got.deliveries[0]
    assert email == done and email.status is DeliveryStatus.DELIVERED
    assert [e.status.value for e in email.events] == [
        "queued", "attempted", "failed", "attempted", "delivered"]  # fmt: skip
    assert got.notification.seen_at is None  # delivery is not reading
    # an outbox redelivery to a fresh Center keeps the original record and times
    again = submit(worker_conn, t, u1)
    assert again.notification.id == sub.notification.id
    assert write(worker_conn, t, ns.record, again) == got
    assert write(worker_conn, t, ns.append, first) == got  # a stale prefix: no-op
    # the owner's open records the first time only
    seen = write(app_conn, t, ns.record_seen, str(u1), sub.notification.id,
                 now(app_conn))  # fmt: skip
    later = write(app_conn, t, ns.record_seen, str(u1), sub.notification.id,
                  now(app_conn))  # fmt: skip
    assert seen.notification.seen_at is not None
    assert later.notification.seen_at == seen.notification.seen_at
    assert write(app_conn, t, ns.load, str(u1), sub.notification.id) == later


def test_appends_must_extend_the_stored_events(
    worker_conn: Conn, app_conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    sub = submit(worker_conn, t, u1)
    write(worker_conn, t, ns.record, sub)
    q = sub.deliveries[0]
    at = now(worker_conn)
    att = N.DeliveryEvent(DeliveryStatus.ATTEMPTED, at)

    def ev(status: DeliveryStatus, code: str = "") -> N.DeliveryEvent:
        return N.DeliveryEvent(status, at, code)

    forged = {
        "conflict": [
            replace(q, events=(ev(DeliveryStatus.DELIVERED),)),  # not queued first
            replace(q, not_before=at + timedelta(hours=1), events=(*q.events, att)),
        ],
        "illegal_event": [
            replace(q, events=(*q.events, ev(DeliveryStatus.DELIVERED))),  # no attempt
            replace(q, events=(*q.events, att, ev(DeliveryStatus.QUEUED))),
            replace(q, events=(*q.events, att, ev(DeliveryStatus.FAILED, "made_up"))),
            replace(q, events=(*q.events, replace(att, at=at - timedelta(hours=1)))),
        ],
    }
    for code, cases in forged.items():
        for d in cases:
            assert code_of(worker_conn, t, ns.append, d) == code
    unknown = replace(q, notification_id="ntf-" + "0" * 32)
    assert code_of(worker_conn, t, ns.append, unknown) == "not_found"
    assert len(write(worker_conn, t, ns.load, str(u1), q.notification_id)
               .deliveries[0].events) == 1  # fmt: skip


def test_tenant_and_user_isolation(worker_conn: Conn, app_conn: Conn) -> None:
    t, u1, u2, t_b, u3 = people(app_conn)
    sub = submit(worker_conn, t, u1)
    write(worker_conn, t, ns.record, sub)
    nid = sub.notification.id
    assert write(app_conn, t, ns.load, str(u2), nid) is None  # same tenant, other user
    assert write(app_conn, t_b, ns.load, str(u1), nid) is None  # other tenant
    assert write(app_conn, t, ns.page, str(u2), 10, None) == []
    assert write(app_conn, t_b, ns.page, str(u1), 10, None) == []
    at = now(app_conn)
    assert code_of(app_conn, t, ns.record_seen, str(u2), nid, at) == "not_found"
    foreign = submit(worker_conn, t_b, u3)
    assert code_of(worker_conn, t, ns.record, foreign) == "tenant"
    assert code_of(worker_conn, t_b, ns.append, sub.deliveries[0]) == "not_found"
    count = app_conn.execute("SELECT count(*) FROM app.notification").fetchone()
    assert count == (0,)  # outside a tenant transaction RLS shows nothing


def test_append_only_privileges_and_db_clock(
    worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    sub = submit(worker_conn, t, u1)
    write(worker_conn, t, ns.record, sub)
    tables = ("notification", "notification_delivery", "notification_delivery_event")
    for table in (*tables, "notification_seen"):
        for sql in (f"UPDATE app.{table} SET tenant_id = tenant_id",
                    f"DELETE FROM app.{table}", f"TRUNCATE app.{table}"):  # fmt: skip
            for c in (worker_conn, conn):  # the owner too
                with pytest.raises(psycopg.Error), tn.tenant_transaction(c, t) as tx:
                    tx.conn.execute(sql)
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        tn.tenant_transaction(app_conn, t) as tx,  # the API never creates one
    ):
        ns._insert_record(tx, KEY, sub.notification)
    old = now(worker_conn) - timedelta(minutes=5)
    late = replace(sub.notification, id="ntf-" + "1" * 32, created_at=old,
                   trigger_at=old - timedelta(seconds=1))  # fmt: skip
    with (
        pytest.raises(psycopg.errors.CheckViolation),
        tn.tenant_transaction(worker_conn, t) as tx,
    ):
        ns._insert_record(tx, KEY, late)  # not the database clock


def tamper(conn: Conn, sql: str) -> None:
    """The owner bypassing the append-only triggers (replica role)."""
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql)


def test_tampered_rows_fail_closed(
    worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    t, u1, u2, *_ = people(app_conn)
    sub = submit(worker_conn, t, u1)
    write(worker_conn, t, ns.record, sub)
    nid = sub.notification.id
    write(app_conn, t, ns.record_seen, str(u1), nid, now(app_conn))
    good = write(app_conn, t, ns.load, str(u1), nid)
    where = f"WHERE id = '{nid}'"
    edits = [
        ("notification", "listed = NOT listed", where),
        ("notification", "version = 'failed'", where),
        ("notification", "object_id = 'job-2'", where),
        ("notification", "created_at = created_at + interval '1 second'", where),
        ("notification", f"user_id = '{u2}'", where),
        ("notification_delivery", "not_before = not_before + interval '1 hour'",
         f"WHERE notification_id = '{nid}'"),
        ("notification_delivery_event", "status = 'delivered'",
         f"WHERE notification_id = '{nid}'"),
        ("notification_seen", "seen_at = seen_at - interval '1 second'",
         f"WHERE notification_id = '{nid}'"),
    ]  # fmt: skip
    for table, change, cond in edits:
        tamper(conn, f"CREATE TEMP TABLE keep AS SELECT * FROM app.{table} {cond}")
        tamper(conn, f"UPDATE app.{table} SET {change} {cond}")
        user = str(u2) if "user_id" in change else str(u1)
        assert code_of(app_conn, t, ns.load, user, nid) == "integrity"
        assert unavailable(write(app_conn, t, ns.page, user, 10, None))
        tamper(conn, f"DELETE FROM app.{table} {cond}")
        tamper(
            conn, f"INSERT INTO app.{table} OVERRIDING SYSTEM VALUE SELECT * FROM keep"
        )
        conn.execute("DROP TABLE keep")
        assert write(app_conn, t, ns.load, str(u1), nid) == good
    # a forged event row with a valid-looking seal under another key
    other = N.DeliveryEvent(DeliveryStatus.ATTEMPTED, now(worker_conn))
    forger = ns.Keyring("syn-a", {"syn-a": b"SYNTHETIC-another-key-00000000000"},
                        ID_KEY)  # fmt: skip
    with tn.tenant_transaction(worker_conn, t) as tx:
        ns._insert_event(tx, forger, nid, Channel.EMAIL, 1, other)
    assert code_of(app_conn, t, ns.load, str(u1), nid) == "integrity"


def test_rows_sealed_with_the_key_still_need_a_true_id_and_positions(
    worker_conn: Conn, app_conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    sub = submit(worker_conn, t, u1)
    write(worker_conn, t, ns.record, sub)
    nid = sub.notification.id
    n = sub.notification
    other = replace(n, id="ntf-" + "2" * 32, created_at=now(worker_conn),
                    subject=replace(n.subject, version="failed"))  # fmt: skip
    with tn.tenant_transaction(worker_conn, t) as tx:  # not the HMAC of its key
        ns._insert_record(tx, KEY, other)
    assert code_of(app_conn, t, ns.load, str(u1), other.id) == "integrity"
    e = N.DeliveryEvent(DeliveryStatus.ATTEMPTED, now(worker_conn))
    with tn.tenant_transaction(worker_conn, t) as tx:  # position 1 is skipped
        ns._insert_event(tx, KEY, nid, Channel.EMAIL, 2, e)
    assert code_of(app_conn, t, ns.load, str(u1), nid) == "integrity"


def test_page_is_newest_first_per_channel_with_keyset(
    worker_conn: Conn, app_conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    off = N.Preferences(in_app=False, email=True)  # unlisted, email only
    a = write(worker_conn, t, ns.record, submit(worker_conn, t, u1, "job-a"))
    b = write(worker_conn, t, ns.record, submit(worker_conn, t, u1, "job-b", off))
    rows = write(app_conn, t, ns.page, str(u1), 10, None)
    keys = [(r.item.notification.subject.object_id, r.channel.value) for r in rows]
    assert keys == [("job-b", "email"), ("job-a", "in_app"), ("job-a", "email"),
                    ("job-a", "browser_push")]  # fmt: skip
    assert rows[0].item == b and rows[1].item == a
    walked, after = [], None
    for _ in range(len(rows) + 1):  # bounded: a keyset that repeats fails
        got = write(app_conn, t, ns.page, str(u1), 2, after)
        walked += got[:1]
        if len(got) < 2:
            break
        after = (got[0].seq, ns.RANK[got[0].channel])
    else:
        pytest.fail("the keyset did not advance")
    assert [(r.seq, r.channel) for r in walked] == [(r.seq, r.channel) for r in rows]


def test_rotation_seals_with_the_active_kid_and_verifies_old_ones(
    worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    c = N.Center(str(t), timedelta(0), 3, ID_KEY)
    sub = submit(worker_conn, t, u1, c=c)
    old = write(worker_conn, t, ns.record, sub)
    nid = sub.notification.id
    assert kids(conn, nid) == ["syn-a"] * 5  # record, 2 deliveries, 2 events
    rotated = ns.Keyring("syn-b", {"syn-a": A, "syn-b": B}, ID_KEY)
    assert write(app_conn, t, ns.load, str(u1), nid, keys=rotated) == old
    done = c.attempt(nid, Channel.EMAIL, Tx(), me(t, u1), now(worker_conn))
    write(worker_conn, t, ns.append, done, keys=rotated)
    got = write(app_conn, t, ns.record_seen, str(u1), nid, now(app_conn),
                keys=rotated)  # fmt: skip
    # the id does not depend on the seal keys: a redelivery finds the same record
    again = submit(worker_conn, t, u1)
    assert write(worker_conn, t, ns.record, again, keys=rotated) == got
    assert kids(conn, nid) == ["syn-a"] * 5 + ["syn-b"] * 3  # 2 events, the open
    page = write(app_conn, t, ns.page, str(u1), 10, None, keys=rotated)
    assert [r.item.notification.seen_at is not None for r in page if r.item] == [
        True] * 3  # fmt: skip
    # retiring A leaves its rows unverifiable: unknown kid, fail closed
    only_b = ns.Keyring("syn-b", {"syn-b": B}, ID_KEY)
    assert code_of(app_conn, t, ns.load, str(u1), nid, keys=only_b) == "integrity"
    assert unavailable(write(app_conn, t, ns.page, str(u1), 10, None, keys=only_b))
    # a kid relabelled to another known kid, or to an unknown one, fails closed
    for kid in ("syn-b", "syn-z"):
        tamper(conn, f"UPDATE app.notification SET seal_kid = '{kid}' "
                     f"WHERE id = '{nid}'")  # fmt: skip
        assert code_of(app_conn, t, ns.load, str(u1), nid, keys=rotated) == (
            "integrity")  # fmt: skip
    tamper(conn, f"UPDATE app.notification SET seal_kid = 'syn-a' WHERE id = '{nid}'")
    assert write(app_conn, t, ns.load, str(u1), nid, keys=rotated) is not None


def test_keyring_is_validated() -> None:
    short = b"SYNTHETIC-31-bytes-000000000000"
    assert len(short) == 31
    bad: list[tuple[str, Any, Any]] = [
        ("syn-a", {"syn-a": short}, ID_KEY),  # a seal key under 32 bytes
        ("syn-a", {"syn-a": A}, short),  # an id key under 32 bytes
        ("syn-b", {"syn-a": A}, ID_KEY),  # the active kid has no key
        ("syn-a", {}, ID_KEY),
        ("Syn A", {"Syn A": A}, ID_KEY),  # not a kid
        ("syn-a", {"syn-a": A.decode()}, ID_KEY),  # not bytes
        ("syn-a", {"syn-a": A}, A),  # the id key is a seal key
        ("syn-a", {"syn-a": A, "syn-b": A}, ID_KEY),  # one key under two kids
        ("syn-a", [("syn-a", A)], ID_KEY),
    ]
    for args in bad:
        with pytest.raises(ValueError):
            ns.Keyring(*args)
    k = ns.Keyring("syn-a", {"syn-a": A}, ID_KEY)
    assert A.decode() not in repr(k) and ID_KEY.decode() not in repr(k)
    with pytest.raises(TypeError):
        k.seal_keys["syn-b"] = B  # type: ignore[index]


def test_one_bad_record_does_not_hide_the_others(
    worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    t, u1, *_ = people(app_conn)
    a = write(worker_conn, t, ns.record, submit(worker_conn, t, u1, "job-a"))
    b = write(worker_conn, t, ns.record, submit(worker_conn, t, u1, "job-b"))
    tamper(conn, "UPDATE app.notification SET version = 'failed' WHERE id = "
                 f"'{a.notification.id}'")  # fmt: skip
    rows = write(app_conn, t, ns.page, str(u1), 10, None)
    assert [(r.channel.value, r.item) for r in rows] == [
        ("in_app", b), ("email", b), ("browser_push", b),
        ("in_app", None), ("email", None), ("browser_push", None)]  # fmt: skip
    assert code_of(app_conn, t, ns.load, str(u1), a.notification.id) == "integrity"


def test_rows_from_0017_fail_closed(
    database_url: str, conn: Conn, tmp_path: Path
) -> None:
    """A record written before 0018 (no kid) is never verified; new rows need one."""
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name < "0018":
            shutil.copy(f, tmp_path / f.name)
    assert migrate(conn, tmp_path)[-1] == 17
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, "SYNTHETIC legacy")
    t, nid = tx.tenant_id, "ntf-" + "3" * 32
    with tn.tenant_transaction(conn, t):
        conn.execute(
            "INSERT INTO app.notification (tenant_id, id, user_id, category, object_id,"
            " version, trigger_at, created_at, listed, seal) VALUES (%s, %s, %s, "
            "'job_outcome', 'job-legacy', 'succeeded', now(), now(), true, %s)",
            (t, nid, user, "0" * 64),
        )  # fmt: skip
    assert migrate(conn) == [18]
    assert kids(conn, nid) == [None]
    assert code_of(conn, t, ns.load, str(user), nid) == "integrity"
    assert unavailable(write(conn, t, ns.page, str(user), 10, None))
    with pytest.raises(psycopg.errors.CheckViolation), tn.tenant_transaction(conn, t):
        conn.execute(
            "INSERT INTO app.notification (tenant_id, id, user_id, category, object_id,"
            " version, trigger_at, created_at, listed, seal) VALUES (%s, %s, %s, "
            "'job_outcome', 'job-new', 'succeeded', now(), now(), true, %s)",
            (t, "ntf-" + "4" * 32, user, "0" * 64),
        )  # fmt: skip
    # a correctly sealed row stripped of its kid (constraint dropped) fails too
    nid = write(conn, t, ns.record, submit(conn, t, user)).notification.id
    conn.execute("ALTER TABLE app.notification DROP CONSTRAINT "
                 "notification_seal_kid_set")  # fmt: skip
    tamper(conn, f"UPDATE app.notification SET seal_kid = NULL WHERE id = '{nid}'")
    assert code_of(conn, t, ns.load, str(user), nid) == "integrity"
