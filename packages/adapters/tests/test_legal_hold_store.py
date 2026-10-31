"""0058 and qw_adapters.legal_hold_store on PostgreSQL 16 as non-owner qw_app and
qw_worker logins; the superuser `conn` only observes or tampers. SYNTHETIC
tenants, principals, step-up receipts and holds; event times are the database
clock."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors
from qw_adapters import legal_hold_store as hs
from qw_adapters import tenancy as tn
from qw_domain import retention as rt
from qw_domain.account_wire import seal
from qw_domain.release import Edition

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
C, R = rt.RetentionClass, rt.HoldRole


def tenant(app: Conn) -> uuid.UUID:
    with tn.provision_tenant(app) as tx:
        return tx.tenant_id


def call(c: Conn, t: uuid.UUID, fn: str, *args: Any, **kw: Any) -> Any:
    with tn.tenant_transaction(c, t) as tx:
        return getattr(hs, fn)(tx, *args, **kw)


def code(c: Conn, t: uuid.UUID, fn: str, *args: Any, **kw: Any) -> str:
    with pytest.raises(hs.HoldStoreError) as err:
        call(c, t, fn, *args, **kw)
    return err.value.code


def put(c: Conn, t: uuid.UUID, hid: str = "hold-1",
        ids: tuple[str, ...] = ("rec-a",)) -> tuple[rt.LegalHold, bool]:  # fmt: skip
    got: tuple[rt.LegalHold, bool] = call(
        c, t, "place", hid, {C.RESEARCH}, ids, "litigation", "counsel-1",
        R.TENANT_OWNER, "owner-synth-1")  # fmt: skip
    return got


def both(tag: str, at: datetime | None = None, later: datetime | None = None,
         operator: str = "operator-synth-1") -> tuple[rt.Approval, ...]:  # fmt: skip
    return (rt.Approval(R.TENANT_OWNER, "owner-synth-1", f"stepup-o-{tag}", at),
            rt.Approval(R.OPERATOR, operator, f"stepup-x-{tag}", later))  # fmt: skip


def op_place(c: Conn, t: uuid.UUID) -> str:
    return code(c, t, "place", "hold-3", {C.RESEARCH}, (), "litigation",
                "counsel-1", R.OPERATOR, "operator-synth-1")  # fmt: skip


def item(t: uuid.UUID, rid: str, at: datetime) -> rt.Item:
    return rt.Item(str(t), rid, C.RESEARCH, at)


def tamper(conn: Conn, sql: str, *args: Any) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql, args)


def test_place_retry_release_and_replay(app_conn: Conn, worker_conn: Conn,
                                        conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn)
    assert call(worker_conn, t, "load").history(str(t)) == ()
    h, wrote = put(app_conn, t)
    assert wrote and h.record_ids == frozenset({"rec-a"})
    again, wrote = put(app_conn, t)  # a retry keeps the first placing time
    assert not wrote and again == h
    assert code(app_conn, t, "place", "hold-1", {C.RESEARCH}, ("rec-b",),
                "litigation", "counsel-1", R.TENANT_OWNER,
                "owner-synth-1") == "conflicting_duplicate"  # fmt: skip
    held = call(worker_conn, t, "load")
    later = h.placed_at + timedelta(days=1)
    assert held.active(item(t, "rec-a", h.placed_at), later) == h
    assert held.active(item(t, "rec-b", h.placed_at), later) is None
    row = row_of(conn, t)
    assert row[0] == h.placed_at
    assert row[1] == seal(row[2], h.placed_at)
    rel = call(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed",
               both("1"))  # fmt: skip
    assert rel.released_at >= h.placed_at and not rel.solo
    assert code(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed",
                both("2")) == "released"  # fmt: skip
    put(app_conn, t, "hold-2")
    assert code(app_conn, t, "release", "hold-2", "counsel-1", "inquiry_closed",
                both("1")) == "step_up_reused"  # fmt: skip
    one = both("3", operator="owner-synth-1")  # one person, no solo opt-in
    assert code(app_conn, t, "release", "hold-2", "counsel-1", "inquiry_closed",
                one) == "distinct_principals"  # fmt: skip
    assert code(app_conn, t, "release", "hold-9", "counsel-1", "inquiry_closed",
                both("4")) == "not_found"  # fmt: skip
    got = call(worker_conn, t, "load")
    h2 = got.history(str(t))[2]
    assert got.history(str(t)) == (h, rel, h2) and h2.placed_at >= rel.released_at
    rec = item(t, "rec-a", h.placed_at)
    assert got.active(rec, rel.released_at - timedelta(microseconds=1)) == h
    if h2.placed_at > rel.released_at:
        assert got.active(rec, rel.released_at) is None
    assert got.active(rec, h2.placed_at) == h2
    other = tenant(app_conn)  # isolation: another tenant sees nothing
    assert call(worker_conn, other, "load").history(str(other)) == ()


def test_solo_release_needs_the_opt_in_and_the_delay(app_conn: Conn,
        worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn)
    solo = rt.SoloRelease(Edition.SELF_HOSTED, frozenset({"owner-synth-1"}))
    h, _ = put(app_conn, t)
    me = both("s", h.placed_at, h.placed_at + timedelta(hours=25), "owner-synth-1")
    assert code(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed", me,
                True, solo=solo) == "time_order"  # a step-up after it  # fmt: skip
    # SYNTHETIC backdating (superuser, resealed): the hold was placed 2 days ago
    old = h.placed_at - timedelta(days=2)
    tamper(conn, "UPDATE app.legal_hold_event SET recorded_at = %s, row_hash = %s "
           "WHERE tenant_id = %s", old, seal(row_of(conn, t)[2], old), t)  # fmt: skip
    near = both("t", old, old + timedelta(hours=23), "owner-synth-1")
    assert code(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed",
                near, True, solo=solo) == "solo_delay"  # fmt: skip
    me = both("u", old, old + timedelta(hours=24), "owner-synth-1")
    assert code(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed",
                me) == "distinct_principals"  # no opt-in  # fmt: skip
    rel = call(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed", me,
               True, solo=solo)  # fmt: skip
    assert rel.solo
    assert len(call(worker_conn, t, "load", solo).history(str(t))) == 2
    assert code(worker_conn, t, "load") == "integrity"  # the opt-in is required


def row_of(conn: Conn, t: uuid.UUID, seq: int = 0) -> tuple[Any, ...]:
    row = conn.execute("SELECT recorded_at, row_hash, entry_hash FROM "
                       "app.legal_hold_event WHERE tenant_id = %s AND seq = %s",
                       (t, seq)).fetchone()  # fmt: skip
    assert row is not None
    return tuple(row)


def test_tamper_and_forgery_make_the_state_unknown(app_conn: Conn, worker_conn: Conn,
                                                   conn: Conn) -> None:  # fmt: skip
    for sql in (
        "UPDATE app.legal_hold_event SET record_ids = '{rec-z}' WHERE seq = 0",
        "UPDATE app.legal_hold_event SET recorded_at = recorded_at + interval "
        "'1 microsecond' WHERE seq = 1",
        "UPDATE app.legal_hold_event SET row_hash = repeat('0', 64) WHERE seq = 1",
        "UPDATE app.legal_hold_event SET seq = 2 WHERE seq = 1",
        "DELETE FROM app.legal_hold_event WHERE seq = 0",
    ):
        t = tenant(app_conn)
        put(app_conn, t)
        put(app_conn, t, "hold-2")
        tamper(conn, sql + " AND tenant_id = %s", t)
        assert code(worker_conn, t, "load") == "integrity", sql
        assert op_place(app_conn, t) == "integrity"
    # a well-sealed forged release that the domain refuses: one person, no opt-in
    t = tenant(app_conn)
    put(app_conn, t)
    forged = both("f", operator="owner-synth-1")
    with pytest.raises(hs.HoldStoreError) as err, tn.tenant_transaction(
            app_conn, t) as tx:  # fmt: skip
        _, prev, seq = hs._load(tx, None)
        r = {"seq": seq, "kind": "release", "hold_id": "hold-1",
             "reason": "inquiry_closed", "authority_id": "counsel-1", "classes": [],
             "record_ids": [], "placed_by": None, "principal_id": None,
             "approval_roles": [a.role.value for a in forged],
             "approval_principals": [a.principal_id for a in forged],
             "approval_receipts": [a.step_up_receipt_id for a in forged],
             "approval_step_ups": [None, None], "solo": False,
             "prev_hash": prev}  # fmt: skip
        hs._insert(tx, r, None)  # the row goes in; the replay refuses it
    assert err.value.code == "integrity"
    assert len(call(worker_conn, t, "load").history(str(t))) == 1  # rolled back


def test_roles_append_only_and_db_stamped_time(app_conn: Conn, worker_conn: Conn,
                                               conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn)
    assert op_place(worker_conn, t) == "forbidden"
    put(app_conn, t)
    for c in (app_conn, worker_conn):
        for sql in ("UPDATE app.legal_hold_event SET reason = 'x'",
                    "DELETE FROM app.legal_hold_event"):  # fmt: skip
            with (pytest.raises(errors.InsufficientPrivilege),
                  tn.tenant_transaction(c, t) as tx):  # fmt: skip
                tx.conn.execute(sql)
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM app.legal_hold_event")
    with (pytest.raises(errors.InsufficientPrivilege),  # recorded_at: no grant
          tn.tenant_transaction(app_conn, t) as tx):  # fmt: skip
        tx.conn.execute(
            "INSERT INTO app.legal_hold_event (tenant_id, seq, kind, hold_id, "
            "reason, authority_id, classes, record_ids, placed_by, principal_id, "
            "approval_roles, approval_principals, approval_receipts, "
            "approval_step_ups, prev_hash, entry_hash, recorded_at) VALUES (%s, 1, "
            "'place', 'h', 'r', 'a', '{research}', '{}', 'operator', 'p', '{}', "
            "'{}', '{}', '{}', %s, %s, now() - interval '1 day')",
            (t, "0" * 64, "1" * 64))  # fmt: skip
    for bad in ("not the next entry", "not_found"):
        with (pytest.raises(errors.CheckViolation, match=bad),
              tn.tenant_transaction(app_conn, t) as tx):  # fmt: skip
            head = tx.conn.execute(
                "SELECT entry_hash FROM app.legal_hold_event WHERE seq = 0"
            ).fetchone()
            assert head is not None
            seq = 5 if bad != "not_found" else 1
            tx.conn.execute(
                "INSERT INTO app.legal_hold_event (tenant_id, seq, kind, hold_id,"
                " reason, authority_id, classes, record_ids, approval_roles, "
                "approval_principals, approval_receipts, approval_step_ups, "
                "prev_hash, entry_hash) VALUES (%s, %s, 'release', 'hold-x', "
                "'closed', 'counsel-1', '{}', '{}', '{tenant_owner,operator}', "
                "'{p-1,p-2}', '{s-1,s-2}', '{NULL,NULL}', %s, %s)",
                (t, seq, head[0], "2" * 64))  # fmt: skip
    other = tenant(app_conn)
    with (pytest.raises(errors.InsufficientPrivilege),  # RLS: another tenant's id
          tn.tenant_transaction(app_conn, other) as tx):  # fmt: skip
        tx.conn.execute(
            "INSERT INTO app.legal_hold_event (tenant_id, seq, kind, hold_id, "
            "reason, authority_id, classes, record_ids, placed_by, principal_id, "
            "approval_roles, approval_principals, approval_receipts, "
            "approval_step_ups, prev_hash, entry_hash) VALUES (%s, 0, 'place', "
            "'h', 'r', 'a', '{research}', '{}', 'operator', 'p', '{}', '{}', "
            "'{}', '{}', %s, %s)", (t, "0" * 64, "1" * 64))  # fmt: skip


def test_a_place_waits_for_a_job_that_read_the_holds(app_conn: Conn,
        worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    """Two connections: the job side's `load` holds 'legal_hold:<tenant>' shared
    to its commit, so a concurrent `place` waits on the lock and is stamped after
    the job committed. The job planned on a hold state that stayed current."""
    t = tenant(app_conn)
    done, out = threading.Event(), list[Any]()

    def place() -> None:
        try:
            out.append(put(app_conn, t))
        except Exception as exc:  # reported by the assertions below
            out.append(exc)
        finally:
            done.set()

    waiting = ("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
               "AND NOT granted AND database = (SELECT oid FROM pg_database "
               "WHERE datname = current_database())")  # fmt: skip
    with tn.tenant_transaction(worker_conn, t) as tx:
        assert hs.load(tx).history(str(t)) == ()  # the job reads: no holds
        th = threading.Thread(target=place, daemon=True)
        th.start()
        end, blocked = time.monotonic() + 10, False
        while not done.is_set() and time.monotonic() < end:
            row = conn.execute(waiting).fetchone()
            if row is not None and row[0] > 0:
                blocked = True
                break
            done.wait(0.02)
        assert blocked and not done.is_set(), out  # the place waits on the lock
        done.wait(0.3)
        assert not done.is_set(), out  # still waiting while the job is open
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        job_end = row[0]
    th.join(10)
    assert done.is_set() and len(out) == 1, out
    h, wrote = out[0]
    assert wrote and h.placed_at > job_end  # stamped after the job committed
    assert call(worker_conn, t, "load").history(str(t)) == (h,)


def test_load_refuses_a_snapshot_taken_before_its_lock(
    worker_conn: Conn, app_conn: Conn
) -> None:
    # A REPEATABLE READ snapshot starts before the shared lock, so a hold placed in
    # between would be invisible while the lock is held: refuse, never read stale.
    t = tenant(app_conn)
    with pytest.raises(hs.HoldStoreError) as err, tn.tenant_transaction(
            worker_conn, t, snapshot=True) as tx:  # fmt: skip
        hs.load(tx)
    assert err.value.code == "isolation"
    assert isinstance(call(worker_conn, t, "load"), rt.Holds)  # READ COMMITTED reads


def test_the_store_trusts_qw_app_for_step_up_receipts(app_conn: Conn,
        worker_conn: Conn) -> None:  # fmt: skip
    """A documented limit, not a guarantee. The store checks only the domain's
    combination rules; nothing here verifies a step-up receipt. A qw_app caller
    supplies every hashed input, so it can release a hold with FABRICATED
    receipt ids and step-up times, by the public call or by a raw, well-sealed
    row, and `load` accepts both. The hold route is the trust boundary."""
    t = tenant(app_conn)
    h, _ = put(app_conn, t)
    put(app_conn, t, "hold-2")
    never = h.placed_at + timedelta(days=365)  # unissued, and a year ahead
    fake = (
        rt.Approval(R.TENANT_OWNER, "owner-synth-1", "fabricated-o-1", never),
        rt.Approval(R.OPERATOR, "operator-synth-1", "fabricated-x-1", never),
    )
    rel = call(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed", fake)
    assert rel.approvals == fake
    with tn.tenant_transaction(app_conn, t) as tx:  # the raw path, sealed by qw_app
        _, prev, seq = hs._load(tx, None)
        raw = hs._insert(tx, {"seq": seq, "kind": "release", "hold_id": "hold-2",
             "reason": "inquiry_closed", "authority_id": "counsel-1", "classes": [],
             "record_ids": [], "placed_by": None, "principal_id": None,
             "approval_roles": ["tenant_owner", "operator"],
             "approval_principals": ["owner-synth-1", "operator-synth-1"],
             "approval_receipts": ["fabricated-o-2", "fabricated-x-2"],
             "approval_step_ups": [None, None], "solo": False,
             "prev_hash": prev}, None)  # fmt: skip
    assert isinstance(raw, rt.HoldRelease)
    got = call(worker_conn, t, "load")
    assert got.history(str(t))[2:] == (rel, raw)
    late = raw.released_at + timedelta(days=1)
    assert got.active(item(t, "rec-a", h.placed_at), late) is None


def test_one_corrupt_row_blocks_every_hold_action_of_the_tenant(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    """Until decision 115's quarantine step exists, one corrupt row makes the
    whole tenant's hold state unknown: no deletion of any of its records can be
    planned (`load` is `integrity`), and no unrelated hold can be placed or
    released. Other tenants are unaffected."""
    t, other = tenant(app_conn), tenant(app_conn)
    put(app_conn, t)
    put(app_conn, t, "hold-2", ("rec-b",))
    put(app_conn, other)
    tamper(conn, "UPDATE app.legal_hold_event SET reason = 'edited' WHERE seq = 0 "
           "AND tenant_id = %s", t)  # fmt: skip
    assert code(worker_conn, t, "load") == "integrity"
    assert code(app_conn, t, "release", "hold-2", "counsel-1", "inquiry_closed",
                both("q")) == "integrity"  # fmt: skip
    assert op_place(app_conn, t) == "integrity"
    assert len(call(worker_conn, other, "load").history(str(other))) == 1
