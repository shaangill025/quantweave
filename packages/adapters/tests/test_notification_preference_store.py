"""0020 and qw_adapters.notification_preference_store as qw_app; the superuser
`conn` observes or tampers. SYNTHETIC tenants, users and sessions only; every time
is the database clock."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from qw_adapters import notification_preference_store as pst
from qw_adapters import tenancy as tn
from qw_domain import notifications as N

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
QUIET = N.Preferences(in_app=False, quiet_hours_timezone="America/Toronto",
                      quiet_hours=("22:00-07:00", "12:00-13:00"))  # fmt: skip
KEY1, KEY2, KEY3 = ("SYNTHETIC-pref-key-0001", "SYNTHETIC-pref-key-0002",
                    "SYNTHETIC-pref-key-0003")  # fmt: skip


@dataclass(frozen=True)
class Ctx:
    tenant: uuid.UUID
    user: uuid.UUID
    session: uuid.UUID
    other: uuid.UUID  # same tenant
    other_session: uuid.UUID
    tenant_b: uuid.UUID
    user_b: uuid.UUID
    session_b: uuid.UUID


@pytest.fixture
def c(app_conn: Conn) -> Ctx:
    ids: list[uuid.UUID] = []
    for n in (2, 1):
        with tn.provision_tenant(app_conn) as tx:
            ids.append(tx.tenant_id)
            for i in range(n):
                user = tn.create_user(tx, f"SYNTHETIC u{i}")
                tn.add_membership(tx, user, tn.MembershipRole.TENANT_MEMBER)
                ids += [user, tn.create_session(tx, user, timedelta(hours=1))[0]]
    t, u, s, o, os_, tb, ub, sb = ids
    return Ctx(t, u, s, o, os_, tb, ub, sb)


def put(conn: Conn, c: Ctx, expected: int, p: N.Preferences, key: str = KEY1,
        user: uuid.UUID | None = None, session: uuid.UUID | None = None,
        tenant: uuid.UUID | None = None) -> pst.Current:  # fmt: skip
    with tn.tenant_transaction(conn, tenant or c.tenant) as tx:
        return pst.set_preferences(tx, user or c.user, session or c.session, key,
                                   expected, p)[0]  # fmt: skip


def get(conn: Conn, c: Ctx, user: uuid.UUID | None = None,
        tenant: uuid.UUID | None = None) -> pst.Current:  # fmt: skip
    with tn.tenant_transaction(conn, tenant or c.tenant) as tx:
        return pst.load(tx, user or c.user)


def count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM app.notification_preference").fetchone()
    assert row is not None
    return int(row[0])


def db_now(conn: Conn) -> Any:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]


def refused(app: Conn, conn: Conn, c: Ctx, code: str, *args: Any, **kw: Any) -> None:
    before = count(conn)
    with pytest.raises(pst.PreferenceStoreError) as err:
        put(app, c, *args, **kw)
    assert err.value.code == code
    assert count(conn) == before


def test_revisions_round_trip_and_stay_per_user(app_conn: Conn, conn: Conn,
                                                c: Ctx) -> None:  # fmt: skip
    assert get(app_conn, c) == pst.Current(0, N.Preferences(), None)  # the defaults
    start = db_now(conn)
    one = put(app_conn, c, 0, QUIET)
    assert (one.revision, one.preferences) == (1, QUIET)
    assert one.recorded_at is not None and start <= one.recorded_at <= db_now(conn)
    assert get(app_conn, c) == one
    two = put(app_conn, c, 1, N.Preferences(), KEY2)
    assert (two.revision, two.preferences) == (2, N.Preferences())
    assert get(app_conn, c) == two and count(conn) == 2
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        assert pst.by_key(tx, c.user, KEY1) == one
        assert pst.by_key(tx, c.user, KEY2) == two
        assert pst.by_key(tx, c.user, KEY3) is None
        assert pst.by_key(tx, c.other, KEY1) is None
    # another user of the tenant and another tenant keep their own defaults
    assert get(app_conn, c, c.other) == pst.Current(0, N.Preferences(), None)
    assert get(app_conn, c, c.user_b, c.tenant_b).revision == 0
    other = put(app_conn, c, 0, QUIET, KEY1, c.other, c.other_session)
    assert other.revision == 1 and get(app_conn, c) == two
    with tn.tenant_transaction(app_conn, c.tenant_b) as tx:  # RLS: no A rows
        rows = tx.conn.execute("SELECT count(*) FROM app.notification_preference")
        assert rows.fetchone() == (0,)


def test_refusals_write_nothing(app_conn: Conn, conn: Conn, c: Ctx) -> None:
    put(app_conn, c, 0, QUIET)
    for expected in (0, 2, 7):
        refused(app_conn, conn, c, "stale", expected, N.Preferences(), KEY2)
    for p in (N.Preferences(email=True), N.Preferences(browser_push=True),
              N.Preferences(email=True, browser_push=True)):  # fmt: skip
        refused(app_conn, conn, c, "step_up_required", 1, p, KEY2)  # none recorded
    # the key of revision 1 with other content, or not as revision 1: a conflict
    refused(app_conn, conn, c, "idempotency_key_reused", 0, N.Preferences())
    refused(app_conn, conn, c, "idempotency_key_reused", 1, QUIET)
    # a retry after the idempotency record was purged returns the original
    with tn.tenant_transaction(app_conn, c.tenant) as tx:  # and says it wrote nothing
        again = pst.set_preferences(tx, c.user, c.session, KEY1, 0, QUIET)
    assert again == (get(app_conn, c), False) and count(conn) == 1
    # another user's session, a revoked session, or another tenant's user
    refused(
        app_conn, conn, c, "database", 1, N.Preferences(), KEY2, session=c.other_session
    )
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        tn.revoke_session(tx, c.session)
    refused(app_conn, conn, c, "database", 1, N.Preferences(), KEY2)
    refused(
        app_conn, conn, c, "database", 0, QUIET, KEY2, c.user, c.session_b, c.tenant_b
    )
    assert count(conn) == 1


def raw_insert(conn: Conn, c: Ctx, **over: Any) -> None:
    with tn.tenant_transaction(conn, c.tenant) as tx:
        last = tx.conn.execute(
            "SELECT revision, event_hash FROM app.notification_preference WHERE "
            "user_id = %s ORDER BY revision DESC LIMIT 1", (c.user,),
        ).fetchone()  # fmt: skip
        row: dict[str, Any] = {
            "tenant_id": c.tenant, "user_id": c.user,
            "revision": (last[0] if last else 0) + 1, "session_id": c.session,
            "idempotency_key": KEY3, "in_app": True, "email": False,
            "browser_push": False, "quiet_hours_timezone": "UTC", "quiet_hours": [],
            "redact_financial_values": True, "recorded_at": db_now(tx.conn),
            "previous_hash": last[1] if last else None, "event_hash": "0" * 64,
        } | over  # fmt: skip
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        tx.conn.execute(f"INSERT INTO app.notification_preference ({cols}) VALUES "
                        f"({marks})", tuple(row.values()))  # fmt: skip


def test_the_database_guards_every_insert(app_conn: Conn, conn: Conn, c: Ctx) -> None:
    one = put(app_conn, c, 0, QUIET)
    assert one.recorded_at is not None
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        expired, _ = tn.create_session(tx, c.user, timedelta(hours=1))
    tamper(conn, "UPDATE app.session SET created_at = created_at - interval '2 hours',"
           " expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
           (expired,))  # fmt: skip
    now = db_now(conn)
    bad: list[dict[str, Any]] = [
        {"session_id": expired},
        {"recorded_at": one.recorded_at - timedelta(milliseconds=1)},
        {"idempotency_key": KEY1},
        {"email": True},  # enabling without a step-up (owner decision 4)
        {"browser_push": True},
        {"email": True, "step_up_at": one.recorded_at},  # not the session's step-up
        {"redact_financial_values": False},
        {"recorded_at": now - timedelta(minutes=5)},
        {"recorded_at": now + timedelta(minutes=5)},
        {"revision": 3},
        {"revision": 1, "previous_hash": None},
        {"previous_hash": "f" * 64},
        {"session_id": c.other_session},
        {"quiet_hours": ["7:00-8:00"]},
        {"quiet_hours": ["22:00-07:00"] * 9},
        {"quiet_hours": ["22:00-07:00", None]},
        {"quiet_hours_timezone": "../../etc/passwd"},
        {"idempotency_key": "short"},
    ]
    for over in bad:
        with pytest.raises(psycopg.Error):
            raw_insert(app_conn, c, **over)
    assert count(conn) == 1
    raw_insert(app_conn, c)  # the control: a well-formed next revision is accepted
    assert count(conn) == 2
    with pytest.raises(pst.PreferenceStoreError, match="integrity"):
        get(app_conn, c)  # its hash is not the record's: fail closed
    for sql in ("UPDATE app.notification_preference SET in_app = true",
                "DELETE FROM app.notification_preference",
                "TRUNCATE app.notification_preference"):  # fmt: skip
        for who in (app_conn, conn):
            with pytest.raises(psycopg.Error), tn.tenant_transaction(who, c.tenant):
                who.execute(sql)
    assert count(conn) == 2
    found = app_conn.execute("SELECT count(*) FROM app.notification_preference")
    assert found.fetchone() == (0,)  # outside a tenant transaction: no rows


def tamper(conn: Conn, sql: str, args: Any = None) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql, args)


@pytest.mark.parametrize("edit", [
    "in_app = NOT in_app", "quiet_hours = '{}'", "quiet_hours_timezone = 'UTC'",
    "recorded_at = recorded_at - interval '1 second'", "session_id = %(o)s",
    "idempotency_key = 'SYNTHETIC-pref-key-9999'", "revision = 3",
    "previous_hash = repeat('a', 64)", "event_hash = repeat('b', 64)",
])  # fmt: skip
def test_tampered_revisions_fail_closed(app_conn: Conn, conn: Conn, c: Ctx,
                                        edit: str) -> None:  # fmt: skip
    put(app_conn, c, 0, QUIET)
    put(app_conn, c, 1, QUIET, KEY2)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT * FROM app.notification_preference").fetchall()
    tamper(conn, f"UPDATE app.notification_preference SET {edit} WHERE revision = 2",
           {"o": c.other_session})  # fmt: skip
    for write in (False, True):
        with pytest.raises(pst.PreferenceStoreError) as err:
            put(app_conn, c, 2, N.Preferences(), KEY3) if write else get(app_conn, c)
        assert err.value.code == "integrity"
    tamper(conn, "DELETE FROM app.notification_preference")
    for row in rows:  # restored: the read is restored
        cols = ", ".join(row)
        tamper(conn, f"INSERT INTO app.notification_preference ({cols}) VALUES "
               f"({', '.join(f'%({k})s' for k in row)})", row)  # fmt: skip
    assert get(app_conn, c).revision == 2


def test_a_rehashed_row_still_passes_the_domain(app_conn: Conn, conn: Conn,
                                                c: Ctx) -> None:  # fmt: skip
    """A writer that recomputes the hash (no signing, as pause_store) is not stopped
    by the hash, but values the domain refuses still fail closed."""
    one = put(app_conn, c, 0, QUIET)
    assert one.recorded_at is not None
    for quiet in (["25:00-07:00"], ["00:00-12:00", "12:00-00:00"]):  # whole day
        digest = pst.revision_hash(c.tenant, c.user, 1, c.session, KEY1, False, False,
                                   False, "America/Toronto", quiet, True,
                                   one.recorded_at, None)  # fmt: skip
        tamper(conn, "UPDATE app.notification_preference SET quiet_hours = %(q)s, "
               "event_hash = %(h)s", {"q": quiet, "h": digest})  # fmt: skip
        with pytest.raises(pst.PreferenceStoreError, match="integrity"):
            get(app_conn, c)
    good = pst.revision_hash(c.tenant, c.user, 1, c.session, KEY1, False, False,
                             False, "America/Toronto", ["22:00-07:00", "12:00-13:00"],
                             True, one.recorded_at, None)  # fmt: skip
    tamper(conn, "UPDATE app.notification_preference SET quiet_hours = %(q)s, "
           "event_hash = %(h)s", {"q": list(QUIET.quiet_hours), "h": good})  # fmt: skip
    assert get(app_conn, c) == one  # the original values and hash read again


def rehash(conn: Conn, c: Ctx, at: int, **over: Any) -> None:
    """Rewrite one revision with `over` and a recomputed, matching hash."""
    with conn.cursor(row_factory=dict_row) as cur:
        r = cur.execute("SELECT * FROM app.notification_preference WHERE revision = "
                        "%s", (at,)).fetchone()  # fmt: skip
    assert r is not None
    r |= over
    r["event_hash"] = pst.revision_hash(
        r["tenant_id"], r["user_id"], r["revision"], r["session_id"],
        r["idempotency_key"], r["in_app"], r["email"], r["browser_push"],
        r["quiet_hours_timezone"], r["quiet_hours"], r["redact_financial_values"],
        r["recorded_at"], r["previous_hash"],
        step_up_at=r["step_up_at"])  # fmt: skip
    sets = ", ".join(f"{k} = %({k})s" for k in r)
    tamper(conn, f"UPDATE app.notification_preference SET {sets} WHERE revision = "
           f"{at}", r)  # fmt: skip


@pytest.mark.parametrize("forgery", ["gap", "chain", "time", "email", "push",
                                     "step_up_after"])  # fmt: skip
def test_rehashed_forgeries_still_fail_closed(app_conn: Conn, conn: Conn, c: Ctx,
                                              forgery: str) -> None:  # fmt: skip
    """Each check that a hash-recomputing writer cannot satisfy by the hash alone:
    revision numbering, the previous-hash chain, time order, and an external
    channel turned on only by a revision recording step-up evidence (0025)."""
    one = put(app_conn, c, 0, QUIET)
    put(app_conn, c, 1, QUIET, KEY2)
    assert one.recorded_at is not None
    later = one.recorded_at + timedelta(days=1)
    if forgery == "step_up_after":  # past the 0025 CHECK, as a schema owner could
        conn.execute("ALTER TABLE app.notification_preference DROP CONSTRAINT "
                     "notification_preference_step_up_before")  # fmt: skip
    forged: dict[str, dict[str, Any]] = {
        "gap": {"revision": 3},
        "chain": {"previous_hash": "a" * 64},
        "time": {"recorded_at": one.recorded_at - timedelta(seconds=1)},
        "email": {"email": True},  # turned on with no step-up recorded
        "push": {"browser_push": True},
        "step_up_after": {"email": True, "step_up_at": later},  # after the revision
    }
    over = forged[forgery]
    rehash(conn, c, 2)  # the control: a faithful rewrite still reads
    assert get(app_conn, c).revision == 2
    rehash(conn, c, 2, **over)
    with pytest.raises(pst.PreferenceStoreError, match="integrity"):
        get(app_conn, c)


def test_a_missing_middle_revision_fails_closed(app_conn: Conn, conn: Conn,
                                                c: Ctx) -> None:  # fmt: skip
    for n, key in enumerate((KEY1, KEY2, KEY3)):
        put(app_conn, c, n, QUIET, key)
    tamper(conn, "DELETE FROM app.notification_preference WHERE revision = 2")
    with pytest.raises(pst.PreferenceStoreError, match="integrity"):
        get(app_conn, c)


# --- external channels behind step-up (0025, increment 5; owner decision 4) ---

GOLDEN_0020 = (
    "ba81bd71600f39bc15a4de10db159ccf7120c245f0e47e2ef8478ba8e61a3743",
    "eb72862b36bf4faf676117ddc7c2bee3955f6af5f4b580621763b95930f5bf4b",
)


def test_hashes_without_step_up_are_the_0020_hashes() -> None:
    """Golden values computed by the increment 4 code before 0025: a revision with
    no step-up evidence hashes exactly as before, so earlier revisions still load."""
    t, u, s = (uuid.UUID(int=n) for n in (1, 2, 3))
    at = datetime(2026, 10, 15, 12, 0, 0, 123456, tzinfo=UTC)
    h1 = pst.revision_hash(t, u, 1, s, KEY1, True, False, False, "UTC", [], True, at,
                           None)  # fmt: skip
    h2 = pst.revision_hash(t, u, 2, s, KEY2, False, False, False, "America/Toronto",
                           ["22:00-07:00"], True, at, h1)  # fmt: skip
    assert (h1, h2) == GOLDEN_0020
    assert pst.revision_hash(t, u, 1, s, KEY1, True, False, False, "UTC", [], True,
                             at, None, step_up_at=None) == h1  # fmt: skip
    stepped = pst.revision_hash(t, u, 1, s, KEY1, True, False, False, "UTC", [], True,
                                at, None, step_up_at=at)  # fmt: skip
    assert stepped not in GOLDEN_0020


def stored_step_up(conn: Conn, revision: int) -> Any:
    row = conn.execute("SELECT step_up_at FROM app.notification_preference WHERE "
                       "revision = %s", (revision,)).fetchone()  # fmt: skip
    assert row is not None
    return row[0]


def step_up(app: Conn, c: Ctx) -> Any:
    with tn.tenant_transaction(app, c.tenant) as tx:
        return tn.mark_step_up(tx, c.session)


def age_step_up(conn: Conn, c: Ctx, by: timedelta) -> None:
    tamper(conn, "UPDATE app.session SET created_at = least(created_at, now() - "
           "%(by)s), step_up_at = now() - %(by)s WHERE id = %(s)s",
           {"by": by, "s": c.session})  # fmt: skip


def test_enabling_an_external_channel_needs_a_recent_step_up(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    put(app_conn, c, 0, QUIET)  # recorded before any step-up: no evidence needed
    assert stored_step_up(conn, 1) is None
    email, push = replace(QUIET, email=True), replace(QUIET, browser_push=True)
    refused(app_conn, conn, c, "step_up_required", 1, email, KEY2)
    at = step_up(app_conn, c)
    narrow = timedelta(microseconds=1)  # a narrower API window
    with (tn.tenant_transaction(app_conn, c.tenant) as tx,
          pytest.raises(pst.PreferenceStoreError, match="step_up")):  # fmt: skip
        pst.set_preferences(tx, c.user, c.session, KEY2, 1, email,
                            step_up_window=narrow)  # fmt: skip
    assert count(conn) == 1
    two = put(app_conn, c, 1, email, KEY2)
    assert (two.revision, two.preferences) == (2, email) and get(app_conn, c) == two
    assert stored_step_up(conn, 2) == at  # the evidence: the session's step-up time
    age_step_up(conn, c, timedelta(hours=2))  # expired (beyond 1 hour)
    both = replace(email, browser_push=True)
    refused(app_conn, conn, c, "step_up_required", 2, both, KEY3)
    # keeping email on (only the windows change) and turning it off need none
    keep = replace(email, quiet_hours=())
    assert put(app_conn, c, 2, keep, KEY3).preferences == keep
    assert stored_step_up(conn, 3) is None
    k4, k5 = "SYNTHETIC-pref-key-0004", "SYNTHETIC-pref-key-0005"
    assert put(app_conn, c, 3, QUIET, k4).preferences == QUIET
    # a same-key retry of the enabling request replays its revision without step-up
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        again = pst.set_preferences(tx, c.user, c.session, KEY2, 1, email)
    assert again == (two, False) and count(conn) == 4
    refused(app_conn, conn, c, "step_up_required", 4, email, k5)  # again off -> on
    refused(app_conn, conn, c, "step_up_required", 4, push, k5)
    step_up(app_conn, c)
    assert put(app_conn, c, 4, push, k5).preferences == push
    assert [r.current.revision for r in _all(app_conn, c)] == [1, 2, 3, 4, 5]


def _all(app: Conn, c: Ctx) -> list[Any]:
    with tn.tenant_transaction(app, c.tenant) as tx:
        return pst._read(tx, c.user)


def test_the_database_requires_step_up_to_enable(app_conn: Conn, conn: Conn,
                                                 c: Ctx) -> None:  # fmt: skip
    """Direct qw_app inserts: turning email or push on needs the inserting session's
    own step-up, at most 1 hour old; turning one off or keeping it needs none."""
    put(app_conn, c, 0, QUIET)
    for over in ({"email": True}, {"browser_push": True},
                 {"email": True, "step_up_at": db_now(conn)}):  # fmt: skip
        with pytest.raises(psycopg.Error, match="step-up"):
            raw_insert(app_conn, c, **over)  # no step-up on the session
    at = step_up(app_conn, c)
    early = at - timedelta(microseconds=1)  # not the session's step-up time
    for over in ({"email": True}, {"email": True, "step_up_at": early},
                 {"step_up_at": early}):  # fmt: skip
        with pytest.raises(psycopg.Error, match="step-up"):
            raw_insert(app_conn, c, **over)
    age_step_up(conn, c, timedelta(hours=1, seconds=5))
    with tn.tenant_transaction(conn, c.tenant) as tx:
        old = tx.conn.execute("SELECT step_up_at FROM app.session WHERE id = %s",
                              (c.session,)).fetchone()  # fmt: skip
    assert old is not None
    with pytest.raises(psycopg.Error, match="step-up"):
        raw_insert(app_conn, c, email=True, step_up_at=old[0])  # too old
    assert count(conn) == 1
    at = step_up(app_conn, c)
    raw_insert(app_conn, c, email=True, step_up_at=at)  # the control
    raw_insert(app_conn, c, email=True, idempotency_key="SYNTHETIC-pref-key-0004")
    raw_insert(app_conn, c, idempotency_key="SYNTHETIC-pref-key-0005")  # off
    assert count(conn) == 4


def test_the_0025_check_refuses_a_step_up_after_the_revision(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    """A qw_app insert that passes the guard (the session's own fresh step-up, a
    recorded_at after the previous revision and within the clock bound) but records
    the step-up after the revision is refused by the CHECK itself."""
    one = put(app_conn, c, 0, QUIET)
    assert one.recorded_at is not None
    at = step_up(app_conn, c)
    assert at > one.recorded_at
    with pytest.raises(psycopg.errors.CheckViolation) as err:
        raw_insert(app_conn, c, email=True, step_up_at=at, recorded_at=one.recorded_at)
    assert err.value.diag.constraint_name == "notification_preference_step_up_before"
    assert count(conn) == 1
    raw_insert(app_conn, c, email=True, step_up_at=at)  # the control: recorded after
    assert count(conn) == 2


def test_a_revoked_session_cannot_enable_even_after_a_step_up(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    put(app_conn, c, 0, QUIET)
    step_up(app_conn, c)
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        assert tn.revoke_session(tx, c.session)
    for p in (replace(QUIET, email=True), replace(QUIET, browser_push=True)):
        refused(app_conn, conn, c, "step_up_required", 1, p, KEY2)  # not `database`


def test_concurrent_writers_at_one_revision_one_wins_one_is_stale(
    app_conn: Conn, conn: Conn, c: Ctx, runtime_urls: dict[str, str]
) -> None:
    """A holds revision 2 uncommitted; B, at the same expected revision, is let
    through only once the database shows it waiting on an advisory lock (the
    store's own lock, or the 0020/0025 guard's without it). B must then read A's
    revision and answer `stale`, never reach the insert (`database`)."""
    put(app_conn, c, 0, QUIET)
    got: dict[str, object] = {}
    with psycopg.connect(runtime_urls["qw_app"], autocommit=True) as b_conn:

        def b() -> None:
            try:
                got["b"] = put(b_conn, c, 1, replace(QUIET, in_app=True), KEY3)
            except pst.PreferenceStoreError as exc:
                got["b"] = exc.code

        thread = threading.Thread(target=b)
        with tn.tenant_transaction(app_conn, c.tenant) as tx:
            a, wrote = pst.set_preferences(tx, c.user, c.session, KEY2, 1,
                                           N.Preferences())  # fmt: skip
            assert wrote and a.revision == 2
            thread.start()
            deadline = time.monotonic() + 30
            while conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s AND locktype ="
                " 'advisory' AND NOT granted)", (b_conn.info.backend_pid,),
            ).fetchone() != (True,):  # fmt: skip
                assert thread.is_alive() and time.monotonic() < deadline, got
                time.sleep(0.01)
        thread.join(30)
    assert not thread.is_alive()
    assert got == {"b": "stale"}
    assert count(conn) == 2 and get(app_conn, c) == a
