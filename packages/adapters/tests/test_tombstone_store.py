"""0057 and qw_adapters.tombstone_store on PostgreSQL 16 as non-owner qw_worker,
qw_app, qw_assessor and qw_release logins; the superuser `conn` only observes or
tampers. SYNTHETIC tenants, records, deletion requests and backups; recorded and
deletion times are the database clock."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as st
from qw_domain import retention as rt
from qw_domain import tombstone_wire as tw

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
SECRET = "SYNTHETIC-private-note-content"  # the acknowledgement: never stored
RAW = b"SYNTHETIC-test-record-key-0123456789"  # decision 104, test only
KEY = rt.RecordKey(RAW)


@pytest.fixture
def tenant(app_conn: Conn) -> Callable[[], uuid.UUID]:
    def make() -> uuid.UUID:
        with tn.provision_tenant(app_conn) as tx:
            return tx.tenant_id

    return make


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def call(conn: Conn, t: uuid.UUID, fn: str, *args: Any) -> Any:
    with tn.tenant_transaction(conn, t) as tx:
        return getattr(st, fn)(tx, *args)


def code(fn: Callable[..., object], *args: Any) -> str:
    with pytest.raises(st.TombstoneStoreError) as err:
        fn(*args)
    return err.value.code


def plan_for(t: uuid.UUID, now: datetime, ids: tuple[str, ...] = ("rec-a", "rec-b"),
             job: str = "del-1") -> rt.Plan:  # fmt: skip
    """A SYNTHETIC deletion request for research records under owner_defaults."""
    items = [rt.Item(str(t), r, rt.RetentionClass.RESEARCH, now - timedelta(days=9))
             for r in ids] + [rt.Item(str(t), "rec-fin", rt.RetentionClass.FINANCIAL,
                                      now - timedelta(days=9))]  # fmt: skip
    req = rt.DeletionRequest(job, str(t), "scope-1", frozenset(
        {rt.RetentionClass.RESEARCH, rt.RetentionClass.FINANCIAL}),
        now - timedelta(seconds=1), "reauth-1", SECRET)  # fmt: skip
    return rt.plan(req, items, rt.owner_defaults(), rt.Holds(), KEY)


def deletes(p: rt.Plan) -> list[rt.Decision]:
    return [d for d in p.decisions if d.action is rt.Action.DELETE]


def seed(conn: Conn, t: uuid.UUID, backups: tuple[rt.Backup, ...] = ()) -> rt.Plan:
    p = plan_for(t, clock(conn))
    for d in deletes(p):
        _, wrote = call(conn, t, "append", KEY, d, p, backups)
        assert wrote
    return p


def tamper(conn: Conn, sql: str) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql)


def test_round_trip_retry_checkpoint_and_restore(worker_conn: Conn, conn: Conn,
                                                 tenant: Any) -> None:  # fmt: skip
    t = tenant()
    assert call(worker_conn, t, "load") == st.StoredLedger(
        (), rt.LedgerCheckpoint(0, rt.GENESIS)
    )
    now = clock(conn)
    old = rt.backup("bk-1", now - timedelta(days=2), now - timedelta(days=2))
    p = seed(worker_conn, t, (old,))
    got = call(worker_conn, t, "load")
    assert [e.seq for e in got.entries] == [0, 1]
    assert {e.record_hash for e in got.entries} == {d.record_hash for d in deletes(p)}
    assert got.checkpoint == got.ledger().checkpoint()
    assert got.checkpoint.length == 2
    first = got.entries[0]
    assert (first.job_id, first.requested_at, first.cls) == (
        "del-1",
        p.trigger_at,
        rt.RetentionClass.RESEARCH,
    )
    assert first.residual_backups == ("bk-1",)
    assert first.backup_expiry_at == old.expires_at
    assert first.deleted_at >= now and first.prev_hash == rt.GENESIS
    # a retry (or a later job) returns the first tombstone and writes nothing
    again, wrote = call(worker_conn, t, "append", KEY, deletes(p)[0], p, ())
    assert not wrote and again in got.entries
    same = p.trigger_at + timedelta(seconds=1)  # the same incarnation of rec-a
    p2 = plan_for(t, same, ids=("rec-a",), job="del-2")
    again, wrote = call(worker_conn, t, "append", KEY, deletes(p2)[0], p2, ())
    assert not wrote and again.job_id == "del-1"
    assert call(worker_conn, t, "load") == got
    # the stored ledger and checkpoint drive the domain restore: no reappearance
    created = p.decisions[0].created_at
    items = tuple(rt.Item(str(t), r, rt.RetentionClass.RESEARCH, created)
                  for r in ("rec-a", "rec-b", "rec-c"))  # fmt: skip
    snap = rt.Snapshot(old, items, ())
    back = rt.restore(snap, got.entries, got.checkpoint, clock(conn), key=KEY)
    assert [i.record_id for i in back.items] == ["rec-c"]
    # a re-created rec-a (a newer incarnation) is never suppressed
    reborn = replace(items[0], created_at=now)
    snap = rt.Snapshot(old, (reborn,), ())
    back = rt.restore(snap, got.entries, got.checkpoint, clock(conn), key=KEY)
    assert back.items == (reborn,)
    assert call(worker_conn, t, "load", got.checkpoint) == got
    assert call(worker_conn, t, "load", rt.LedgerCheckpoint(1, first.entry_hash)) == got


def test_no_deleted_content_is_stored(worker_conn: Conn, conn: Conn,
                                      tenant: Any) -> None:  # fmt: skip
    t = tenant()
    seed(worker_conn, t)
    cols = conn.execute(
        "SELECT table_name, array_agg(column_name::text ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_schema = 'app' AND table_name "
        "LIKE 'deletion_tombstone%' GROUP BY 1 ORDER BY 1").fetchall()  # fmt: skip
    assert cols == [
        ("deletion_tombstone", ["tenant_id", "seq", "record_hash", "cls", "job_id",
                                "requested_at", "deleted_at", "residual_backups",
                                "backup_expiry_at", "hash_scheme", "key_id",
                                "prev_hash",
                                "entry_hash",
                                "recorded_at", "row_hash",
                                "residual_backups_unknown"]),  # 0059
        ("deletion_tombstone_checkpoint", ["tenant_id", "length", "head",
                                           "recorded_at", "row_hash"]),
    ]  # fmt: skip
    text = " ".join(r[0] for r in conn.execute(
        "SELECT x::text FROM app.deletion_tombstone x UNION ALL "
        "SELECT y::text FROM app.deletion_tombstone_checkpoint y"))  # fmt: skip
    for needle in ("rec-a", "rec-b", "rec-fin", SECRET, "scope-1", "reauth-1"):
        assert needle not in text
    with tn.tenant_transaction(worker_conn, t, snapshot=True) as tx:
        assert "rec-a" not in repr(st.export_tombstones(tx))


@pytest.mark.parametrize("sql", [
    "UPDATE app.deletion_tombstone SET record_hash = repeat('a', 64) WHERE seq = 0",
    "UPDATE app.deletion_tombstone SET deleted_at = deleted_at + interval '1 us'",
    "UPDATE app.deletion_tombstone SET requested_at = requested_at - interval '1 s'",
    "UPDATE app.deletion_tombstone SET cls = 'market' WHERE seq = 1",
    "UPDATE app.deletion_tombstone SET job_id = 'del-9'",
    "UPDATE app.deletion_tombstone SET residual_backups = '{bk-9}'",
    "UPDATE app.deletion_tombstone SET residual_backups_unknown = true WHERE seq = 1",
    "UPDATE app.deletion_tombstone SET recorded_at = recorded_at + interval '1 s' "
    "WHERE seq = 1",
    "UPDATE app.deletion_tombstone SET row_hash = entry_hash WHERE seq = 0",
    "UPDATE app.deletion_tombstone SET prev_hash = repeat('0', 64) WHERE seq = 1",
    "DELETE FROM app.deletion_tombstone WHERE seq = 0",
    "DELETE FROM app.deletion_tombstone WHERE seq = 1",
    "UPDATE app.deletion_tombstone SET seq = seq + 10",
    "UPDATE app.deletion_tombstone_checkpoint SET head = repeat('b', 64)",
    "UPDATE app.deletion_tombstone_checkpoint SET row_hash = repeat('0', 64) "
    "WHERE length = 1",
    "UPDATE app.deletion_tombstone_checkpoint SET recorded_at = recorded_at "
    "- interval '1 s' WHERE length = 2",
    "DELETE FROM app.deletion_tombstone_checkpoint WHERE length = 2",
    "INSERT INTO app.deletion_tombstone_checkpoint SELECT tenant_id, 3, head, "
    "recorded_at, row_hash FROM app.deletion_tombstone_checkpoint WHERE length = 2",
])  # fmt: skip
def test_chain_and_checkpoint_tamper_is_integrity(
    worker_conn: Conn, conn: Conn, tenant: Any, sql: str
) -> None:
    t = tenant()
    p = seed(worker_conn, t)
    call(worker_conn, t, "load")
    tamper(conn, sql)
    assert code(call, worker_conn, t, "load") == "integrity"
    p3 = plan_for(t, clock(conn), ids=("rec-z",), job="del-3")
    assert code(call, worker_conn, t, "append", KEY, deletes(p3)[0], p3) == (
        "integrity"
    )
    with tn.tenant_transaction(worker_conn, t, snapshot=True) as tx:
        assert code(st.export_tombstones, tx) == "integrity"
    assert deletes(p)


def test_a_dropped_tail_with_its_checkpoint_needs_the_external_anchor(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """The stored checkpoint shares the database's failure domain: removing the last
    tombstone together with its checkpoint leaves a consistent prefix. Only an
    anchor kept elsewhere detects it (`integrity`)."""
    t = tenant()
    seed(worker_conn, t)
    anchor = call(worker_conn, t, "load").checkpoint
    tamper(conn, "DELETE FROM app.deletion_tombstone_checkpoint WHERE length = 2")
    tamper(conn, "DELETE FROM app.deletion_tombstone WHERE seq = 1")
    assert call(worker_conn, t, "load").checkpoint.length == 1
    assert code(call, worker_conn, t, "load", anchor) == "integrity"
    other = rt.LedgerCheckpoint(1, "c" * 64)
    assert code(call, worker_conn, t, "load", other) == "integrity"
    assert code(call, worker_conn, t, "load", (1, "c" * 64)) == "argument"


def test_rows_are_append_only(worker_conn: Conn, app_conn: Conn, conn: Conn,
                              tenant: Any) -> None:  # fmt: skip
    t = tenant()
    seed(worker_conn, t)
    for c in (worker_conn, app_conn):
        for stmt in ("UPDATE app.deletion_tombstone SET job_id = 'x'",
                     "DELETE FROM app.deletion_tombstone",
                     "TRUNCATE app.deletion_tombstone",
                     "UPDATE app.deletion_tombstone_checkpoint SET length = 9",
                     "DELETE FROM app.deletion_tombstone_checkpoint"):  # fmt: skip
            with pytest.raises(errors.InsufficientPrivilege), \
                    tn.tenant_transaction(c, t):  # fmt: skip
                c.execute(stmt)
    for stmt in ("UPDATE app.deletion_tombstone SET seq = seq",
                 "DELETE FROM app.deletion_tombstone_checkpoint",
                 "TRUNCATE app.deletion_tombstone CASCADE"):  # fmt: skip
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(stmt)  # even the owner's session
    assert call(worker_conn, t, "load").checkpoint.length == 2


def raw(tx: tn.TenantTx, seq: int, prev: str, deleted: datetime,
        extra: dict[str, Any] | None = None) -> Any:  # fmt: skip
    """A raw row whose entry_hash is the domain's (unless `extra` overrides it)."""
    expiry = deleted + timedelta(days=30)
    stone = rt.Tombstone(seq, str(tx.tenant_id), "d" * 64, rt.RetentionClass.RESEARCH,
                         "del-raw", deleted, deleted, ("bk-1", "bk-2"), expiry,
                         rt.HASH_SCHEME, (extra or {}).get("key_id", KEY.key_id),
                         prev, "")  # fmt: skip
    values = {"tenant_id": tx.tenant_id, "seq": seq, "record_hash": "d" * 64,
              "cls": "research", "job_id": "del-raw", "requested_at": deleted,
              "deleted_at": deleted, "residual_backups": ["bk-1", "bk-2"],
              "backup_expiry_at": expiry, "hash_scheme": rt.HASH_SCHEME,
              "key_id": KEY.key_id,
              "prev_hash": prev, "entry_hash": rt.entry_hash(stone),
              **(extra or {})}  # fmt: skip
    return tx.conn.execute(
        f"INSERT INTO app.deletion_tombstone ({', '.join(values)}) VALUES "
        f"({', '.join(f'%({k})s' for k in values)}) RETURNING recorded_at, "
        "row_hash, entry_hash", values).fetchone()  # fmt: skip


def test_time_is_stamped_and_bounded_by_the_database(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    t = tenant()
    seed(worker_conn, t)
    head = call(worker_conn, t, "load")
    last = head.entries[-1]
    now = clock(conn)
    for extra in ({"recorded_at": now - timedelta(days=1)}, {"row_hash": "0" * 64}):
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            raw(tx, 2, last.entry_hash, now, extra)
    with pytest.raises(errors.CheckViolation, match="stale_clock"), \
            tn.tenant_transaction(worker_conn, tenant()) as tx:  # fmt: skip
        raw(tx, 0, rt.GENESIS, now - timedelta(minutes=2))  # no earlier tombstone
    cases = [
        (2, last.entry_hash, now + timedelta(minutes=2), "stale_clock"),
        (2, last.entry_hash, last.deleted_at - timedelta(seconds=1), "time_order"),
        (3, last.entry_hash, now, "not the next entry"),
        (2, rt.GENESIS, now, "not the next entry"),
    ]  # fmt: skip
    for seq, prev, at, match in cases:
        with pytest.raises(errors.CheckViolation, match=match), \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            raw(tx, seq, prev, at)
    for length, h in ((2, last.entry_hash), (3, last.entry_hash), (1, last.prev_hash)):
        with pytest.raises(errors.CheckViolation, match="not the head"), \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            tx.conn.execute("INSERT INTO app.deletion_tombstone_checkpoint "
                            "(tenant_id, length, head) VALUES (%s, %s, %s)",
                            (t, length, h))  # fmt: skip
    # the owner's backdated recorded_at is replaced by the server time
    back = {"recorded_at": now - timedelta(days=1), "row_hash": "0" * 64}
    with conn.transaction():
        row = raw(tn.TenantTx(conn, t), 2, last.entry_hash,
                  max(clock(conn), last.deleted_at), back)  # fmt: skip
    assert row is not None and row[0] >= now and row[1] == st.seal(row[2], row[0])
    assert code(call, worker_conn, t, "load") == "integrity"  # no checkpoint


def test_tenants_are_isolated(worker_conn: Conn, tenant: Any) -> None:
    a, b = tenant(), tenant()
    p = seed(worker_conn, a)
    assert call(worker_conn, b, "load").entries == ()
    assert code(call, worker_conn, b, "append", KEY, deletes(p)[0], p) == "tenant"
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"), \
            tn.tenant_transaction(worker_conn, b) as tx:  # fmt: skip
        raw(replace(tx, tenant_id=a), 0, rt.GENESIS, clock(worker_conn))
    # b's own chain starts at seq 0, independent of a's
    pb = seed(worker_conn, b)
    got = call(worker_conn, b, "load")
    assert [e.seq for e in got.entries] == [0, 1] and got.entries[0].prev_hash == (
        rt.GENESIS
    )
    assert {e.record_hash for e in got.entries} == {d.record_hash for d in deletes(pb)}
    assert call(worker_conn, a, "load").entries[0].tenant_id == str(a)


def test_append_refuses_what_the_ledger_rules_refuse(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    t = tenant()
    p = plan_for(t, clock(conn))
    d = deletes(p)[0]
    keep = next(x for x in p.decisions if x.action is rt.Action.RETAIN)
    assert code(call, worker_conn, t, "append", KEY, keep, p) == "not_a_deletion"
    stray = replace(d, record_id="rec-other")
    assert code(call, worker_conn, t, "append", KEY, stray, p) == "not_in_plan"
    forged = replace(p, decisions=(*p.decisions, stray))
    assert code(call, worker_conn, t, "append", KEY, stray, forged) == ("record_hash")
    priv = replace(d, cls=rt.RetentionClass.PRIVACY)
    with_priv = replace(p, decisions=(*p.decisions, priv))
    assert code(call, worker_conn, t, "append", KEY, priv, with_priv) == (
        "tombstone_ledger"
    )
    future = replace(p, trigger_at=clock(conn) + timedelta(hours=1))
    assert code(call, worker_conn, t, "append", KEY, d, future) == "time_order"
    for args in ((d.record_id, p), (d, "plan"), (d, p, "bk-1"), (d, p, [object()])):
        assert code(call, worker_conn, t, "append", KEY, *args) == "argument"
    assert call(worker_conn, t, "load").entries == ()


def test_roles_without_grants_are_refused(
    worker_conn: Conn, app_conn: Conn, assessor_conn: Conn, release_conn: Conn,
    tenant: Any,
) -> None:  # fmt: skip
    t = tenant()
    p = seed(worker_conn, t)
    for c in (assessor_conn, release_conn):
        assert code(call, c, t, "load") == "forbidden"
        with tn.tenant_transaction(c, t, snapshot=True) as tx:
            assert code(st.export_tombstones, tx) == "forbidden"
    assert call(app_conn, t, "load").checkpoint.length == 2
    p2 = plan_for(t, clock(app_conn), ids=("rec-q",), job="del-q")
    assert code(call, app_conn, t, "append", KEY, deletes(p2)[0], p2) == ("forbidden")
    assert call(app_conn, t, "load").checkpoint.length == 2
    assert deletes(p)


def test_export_is_snapshot_only_and_lossless(
    worker_conn: Conn, app_conn: Conn, tenant: Any
) -> None:
    t = tenant()
    seed(worker_conn, t)
    stored = call(app_conn, t, "load")
    with tn.tenant_transaction(app_conn, t) as tx, pytest.raises(
            tn.SnapshotRequired):  # fmt: skip
        st.export_tombstones(tx)
    with tn.tenant_transaction(app_conn, t, snapshot=True) as tx:
        out = st.export_tombstones(tx)
    assert sorted(out) == ["checkpoints", "ledger", "tombstones"]
    assert [r["entry_hash"] for r in out["tombstones"]] == [
        e.entry_hash for e in stored.entries
    ]
    assert [(r["length"], r["head"]) for r in out["checkpoints"]] == [
        (1, stored.entries[0].entry_hash),
        (2, stored.entries[1].entry_hash),
    ]
    first = out["tombstones"][0]
    assert first["tenant_id"] == str(t) and first["residual_backups"] == []
    assert first["deleted_at"].endswith("Z") and first["backup_expiry_at"] is None
    for r in out["tombstones"]:
        assert r["row_hash"] == st.seal(
            r["entry_hash"], datetime.fromisoformat(r["recorded_at"])
        )


def test_unknown_backups_are_stored_exported_and_imported(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:  # owner decision 114: no catalog yet, never an empty list alone
    t = tenant()
    p = plan_for(t, clock(conn))
    one, wrote = call(worker_conn, t, "append", KEY, deletes(p)[0], p, None)
    assert wrote and one.backups_unknown and one.residual_backups == ()
    two, _ = call(worker_conn, t, "append", KEY, deletes(p)[1], p, ())
    assert not two.backups_unknown
    assert call(worker_conn, t, "load").entries == (one, two)
    with tn.tenant_transaction(worker_conn, t, snapshot=True) as tx:
        body = st.export_tombstones(tx)
    marks = [r["residual_backups_unknown"] for r in body["tombstones"]]
    assert marks == [True, False]
    assert tw.parse_section(str(t), body).entries == (one, two)
    for bad, why in ((False, "entry"), (1, "malformed"), (None, "malformed")):
        forged = json.loads(json.dumps(body))
        forged["tombstones"][0]["residual_backups_unknown"] = bad
        with pytest.raises(tw.WireError) as err:
            tw.parse_section(str(t), forged)
        assert err.value.code == why
    forged = json.loads(json.dumps(body))
    forged["tombstones"][0]["residual_backups"] = ["bk-1"]  # contradicts the marker
    with pytest.raises(tw.WireError, match="malformed"):
        tw.parse_section(str(t), forged)
    last = call(worker_conn, t, "load").entries[-1]
    with pytest.raises(errors.CheckViolation), \
            tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
        raw(
            tx,
            2,
            last.entry_hash,
            max(clock(conn), last.deleted_at),
            {"residual_backups_unknown": True},
        )  # and a listed backup: refused


def test_matching_needs_the_key_and_integrity_does_not(
    worker_conn: Conn, app_conn: Conn, conn: Conn, tenant: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Owner decision 104: the record hash is keyed; the key never reaches the
    database, the export or the logs."""
    caplog.set_level(logging.DEBUG)
    t = tenant()
    now = clock(conn)
    p = seed(worker_conn, t)
    rec = rt.Item(
        str(t), "rec-a", rt.RetentionClass.RESEARCH, p.decisions[0].created_at
    )
    hit = call(worker_conn, t, "find", KEY, rec)
    assert hit is not None and hit.hash_scheme == rt.HASH_SCHEME
    other = rt.RecordKey(b"SYNTHETIC-another-record-key-98765432")
    assert code(call, worker_conn, t, "find", other, rec) == "key_mismatch"
    p_other = rt.plan(rt.DeletionRequest(
        "del-o", str(t), "scope-1", frozenset({rt.RetentionClass.RESEARCH}),
        clock(conn), "reauth-o", SECRET), [replace(rec, record_id="rec-o")],
        rt.owner_defaults(), rt.Holds(), other)  # fmt: skip
    assert code(call, worker_conn, t, "append", other, p_other.decisions[0],
                p_other) == "key_mismatch"  # fmt: skip
    assert code(call, worker_conn, t, "append", KEY, p_other.decisions[0],
                p_other) == "key_mismatch"  # fmt: skip
    with pytest.raises(errors.CheckViolation, match="key_mismatch"), \
            tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
        last = st.load(tx).entries[-1]
        raw(tx, 2, last.entry_hash, max(clock(conn), last.deleted_at),
            {"key_id": other.key_id})  # fmt: skip
    newer = replace(rec, created_at=now)  # another incarnation of rec-a
    assert call(worker_conn, t, "find", KEY, newer) is None
    assert code(call, worker_conn, t, "find", None, rec) == "key_unavailable"
    p2 = plan_for(t, clock(conn), ids=("rec-k",), job="del-k")
    assert code(call, worker_conn, t, "append", None, deletes(p2)[0], p2) == (
        "key_unavailable"
    )
    stored = call(app_conn, t, "load")  # no key: the chain still verifies
    assert stored.checkpoint.length == 2
    with tn.tenant_transaction(app_conn, t, snapshot=True) as tx:
        out = st.export_tombstones(tx)
    assert {r["hash_scheme"] for r in out["tombstones"]} == {rt.HASH_SCHEME}
    rows = " ".join(r[0] for r in conn.execute(
        "SELECT x::text FROM app.deletion_tombstone x UNION ALL "
        "SELECT y::text FROM app.deletion_tombstone_checkpoint y"))  # fmt: skip
    for secret in (RAW.decode(), RAW.hex()):
        assert secret not in rows and secret not in repr(out)
        assert secret not in caplog.text and secret not in repr(stored)
    # the reviewer's probe: guessing ids without the key recovers nothing
    target = hit.record_hash
    guesses = [f"rec-{c}" for c in "abcdefghijklmnopqrstuvwxyz"]
    unkeyed = [
        g
        for g in guesses
        if hashlib.sha256(
            json.dumps(["qw-record-1", str(t), "research", g]).encode()
        ).hexdigest()
        == target
    ]
    assert unkeyed == [] and not [
        g
        for g in guesses
        if rt.record_hash(other, str(t), rt.RetentionClass.RESEARCH, g, rec.created_at)
        == target
    ]


def test_a_recreated_record_gets_its_own_tombstone(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """Review finding 2: the second deletion of a re-created rec-a discloses the
    new backup; a retry of either incarnation stays idempotent."""
    t = tenant()
    seed(worker_conn, t)
    now = clock(conn)
    bk = rt.backup("bk-new", now, now)
    later = now + timedelta(milliseconds=1)
    item = rt.Item(
        str(t), "rec-a", rt.RetentionClass.RESEARCH, now - timedelta(hours=1)
    )
    req = rt.DeletionRequest("del-2", str(t), "scope-1",
                             frozenset({rt.RetentionClass.RESEARCH}), later,
                             "reauth-2", SECRET)  # fmt: skip
    p2 = rt.plan(req, [item], rt.owner_defaults(), rt.Holds(), KEY)
    with tn.tenant_transaction(worker_conn, t) as tx:
        tx.conn.execute("SELECT pg_sleep(0.01)")
    two, wrote = call(worker_conn, t, "append", KEY, p2.decisions[0], p2, (bk,))
    assert wrote and (two.job_id, two.residual_backups, two.seq) == (
        "del-2",
        ("bk-new",),
        2,
    )
    again, wrote = call(worker_conn, t, "append", KEY, p2.decisions[0], p2, (bk,))
    assert not wrote and again == two
    got = call(worker_conn, t, "load")
    assert len(got.entries) == 3 and got.checkpoint.length == 3


def test_a_worker_cannot_seal_a_forged_entry_hash(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:
    """Review finding 3: the guard recomputes entry_hash, so the reviewer's raw row
    with an arbitrary entry_hash is refused and the ledger stays usable."""
    t = tenant()
    seed(worker_conn, t)
    last = call(worker_conn, t, "load").entries[-1]
    for extra in ({"entry_hash": "b" * 64}, {"hash_scheme": "qw-record-hmac-9"}):
        with pytest.raises(errors.CheckViolation, match=r"entry_hash|hash_scheme"), \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            raw(tx, 2, last.entry_hash, max(clock(conn), last.deleted_at), extra)
    with tn.tenant_transaction(worker_conn, t) as tx:  # the domain's hash passes
        row = raw(tx, 2, last.entry_hash, max(clock(worker_conn), last.deleted_at))
        tx.conn.execute("INSERT INTO app.deletion_tombstone_checkpoint (tenant_id, "
                        "length, head) VALUES (%s, 3, %s)", (t, row[2]))  # fmt: skip
    assert call(worker_conn, t, "load").checkpoint.length == 3
    p3 = plan_for(t, clock(conn), ids=("rec-z",), job="del-3")
    _, wrote = call(worker_conn, t, "append", KEY, deletes(p3)[0], p3)
    assert wrote


def test_duplicate_backup_ids_are_refused_before_writing(
    worker_conn: Conn, conn: Conn, tenant: Any
) -> None:  # review finding 4
    t = tenant()
    now = clock(conn)
    b1 = rt.backup("bk-1", now - timedelta(days=3), now - timedelta(days=3))
    b2 = rt.backup("bk-1", now - timedelta(days=2), now - timedelta(days=2))
    p = plan_for(t, now)
    assert code(call, worker_conn, t, "append", KEY, deletes(p)[0], p, (b1, b2)) == (
        "duplicate_backup"
    )
    assert call(worker_conn, t, "load").entries == ()
