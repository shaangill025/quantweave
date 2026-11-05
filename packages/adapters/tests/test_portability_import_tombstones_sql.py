"""The tombstone import function (`0066`, `app.portability_import_tombstones`) called
directly on PostgreSQL 16 as the qw_app login, outside `destination_import`'s own
checks: the lock, emptiness, the shape, the clock bound, the kept stamps and seals
(decision 112) and decision 114's marker must hold in SQL alone, and
`tombstone_store` must re-verify and re-export what SQL wrote. SYNTHETIC ledgers
(`ledger`) in fresh tenants; the record key is SYNTHETIC.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as tb
from qw_domain import portability as pt
from qw_domain import retention as rt
from qw_domain import tombstone_wire as tw
from qw_domain.account_wire import seal
from qw_domain.instants import format_instant, parse_instant

Conn = psycopg.Connection[TupleRow]
Body = dict[str, Any]
CALL = "SELECT app.portability_import_tombstones(%s)"
KEY = rt.RecordKey(b"SYNTHETIC-tombstone-import-key-0123456789")  # test only
OTHER = rt.RecordKey(b"SYNTHETIC-another-installation-key-0123")
DAY = timedelta(days=1)


def now(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def tenant_of(conn: Conn) -> uuid.UUID:
    tenant = uuid.uuid4()
    with tn.tenant_transaction(conn, tenant):
        conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
    return tenant


def _entry(r: Body) -> str:
    exp = r["backup_expiry_at"]
    return rt.entry_hash(rt.Tombstone(
        r["seq"], r["tenant_id"], r["record_hash"], rt.RetentionClass(r["cls"]),
        r["job_id"], parse_instant(r["requested_at"]),
        parse_instant(r["deleted_at"]), tuple(r["residual_backups"]),
        None if exp is None else parse_instant(exp), r["hash_scheme"], r["key_id"],
        r["prev_hash"], "", r["residual_backups_unknown"]))  # fmt: skip


def reseal(b: Body, rehash: bool = True) -> Body:
    """What a forger can recompute: entry hashes (`rehash`), links, seals, heads
    and the header."""
    prev = rt.GENESIS
    for r in b["tombstones"]:
        r["prev_hash"] = prev
        r["entry_hash"] = _entry(r) if rehash else r["entry_hash"]
        r["row_hash"] = seal(r["entry_hash"], parse_instant(r["recorded_at"]))
        prev = r["entry_hash"]
    for c in b["checkpoints"]:
        c["head"] = b["tombstones"][c["length"] - 1]["entry_hash"]
        c["row_hash"] = seal(c["head"], parse_instant(c["recorded_at"]))
    b["ledger"] = tw.header(b["ledger"]["tenant_id"], b["tombstones"])
    return b


def ledger(tenant: uuid.UUID, start: datetime) -> Body:
    """Three SYNTHETIC deletions from `start`: two residual backups with an expiry;
    decision 114's unknown marker; none. Checkpoints at 1 and 3 (a gap the wire
    allows), each stamped after its entry and before the next."""
    t, at = str(tenant), start
    rows: list[Body] = []
    cps: list[Body] = []

    def tick() -> str:
        nonlocal at
        at += timedelta(seconds=1, microseconds=250)
        return format_instant(at)

    for n, (kept, exp, unknown) in enumerate((
            (["bk-synth-a", "bk-synth-b"], format_instant(start + 30 * DAY), False),
            ([], None, True), ([], None, False))):  # fmt: skip
        rows.append({"tenant_id": t, "seq": n, "record_hash": hashlib.sha256(
            f"SYNTHETIC-{n}".encode()).hexdigest(), "cls": "research",
            "job_id": f"del-synth-{n}", "requested_at": tick(), "deleted_at": tick(),
            "residual_backups": kept, "backup_expiry_at": exp,
            "hash_scheme": rt.HASH_SCHEME, "key_id": KEY.key_id,
            "prev_hash": "", "entry_hash": "", "recorded_at": tick(), "row_hash": "",
            "residual_backups_unknown": unknown})  # fmt: skip
        if n + 1 in (1, 3):
            cps.append({"tenant_id": t, "length": n + 1, "head": "",
                        "recorded_at": tick(), "row_hash": ""})  # fmt: skip
    b = reseal({"ledger": {"tenant_id": t}, "tombstones": rows, "checkpoints": cps})
    tw.parse_section(t, b)  # the export side accepts it
    return b


def lock(tx: tn.TenantTx, fn: str = "pg_advisory_xact_lock", bit: int = 0) -> None:
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0) # %s)",
                    (f"import:{tx.tenant_id}", bit))  # fmt: skip


def call(tx: tn.TenantTx, body: Any) -> int:
    row = tx.conn.execute(CALL, (Jsonb(body),)).fetchone()
    assert row is not None
    return int(row[0])


def run(conn: Conn, tenant: uuid.UUID, body: Any,
        before: Callable[[tn.TenantTx], None] = lock) -> int:  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        before(tx)
        return call(tx, body)


def count(conn: Conn, tenant: uuid.UUID) -> tuple[int, int]:
    with tn.tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT (SELECT count(*) FROM app.deletion_tombstone), "
            "(SELECT count(*) FROM app.deletion_tombstone_checkpoint)"
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def failing(conn: Conn, tenant: uuid.UUID, body: Any, **kw: Any) -> str | None:
    with pytest.raises(psycopg.Error) as e:
        run(conn, tenant, body, **kw)
    assert count(conn, tenant) == (0, 0)
    return e.value.sqlstate


def exported(conn: Conn, tenant: uuid.UUID) -> bytes:
    with tn.tenant_transaction(conn, tenant, snapshot=True) as tx:
        return pt.canonical(tb.export_tombstones(tx))


def append(tx: tn.TenantTx, rid: str = "acct-synth-1") -> bool:
    """One SYNTHETIC deletion through the store (qw_worker), under KEY."""
    at = now(tx.conn)
    item = rt.Item(str(tx.tenant_id), rid, rt.RetentionClass.RESEARCH, at - DAY)
    req = rt.DeletionRequest(f"del-{rid}", str(tx.tenant_id), "scope-1",
                             frozenset({rt.RetentionClass.RESEARCH}),
                             at - timedelta(seconds=1), "reauth-1",
                             "SYNTHETIC")  # fmt: skip
    p = rt.plan(req, [item], rt.owner_defaults(), rt.Holds(), KEY)
    return tb.append(tx, KEY, p.decisions[0], p, None)[1]


def delete_one(worker: Conn, tenant: uuid.UUID, rid: str = "acct-synth-1") -> bool:
    with tn.tenant_transaction(worker, tenant) as tx:
        return append(tx, rid)


@pytest.mark.db
def test_the_ledger_is_kept_and_re_exported_byte_for_byte(
    app_conn: Conn, worker_conn: Conn
) -> None:
    """Decision 112: every stamp and seal is the archive's (days old, not the
    destination clock); decision 114's marker is carried; the store re-verifies
    and re-exports the section byte for byte, then appends after it."""
    tenant = tenant_of(app_conn)
    body = ledger(tenant, now(app_conn) - 3 * DAY)
    assert run(app_conn, tenant, body) == 5  # 3 tombstones, 2 checkpoints
    assert exported(app_conn, tenant) == pt.canonical(body)
    with tn.tenant_transaction(app_conn, tenant) as tx:
        stored = tb.load(tx)
    assert [e.backups_unknown for e in stored.entries] == [False, True, False]
    assert stored.entries[0].residual_backups == ("bk-synth-a", "bk-synth-b")
    assert stored.checkpoint == rt.LedgerCheckpoint(3, body["ledger"]["head"])
    assert delete_one(worker_conn, tenant)  # the store's next entry follows
    with tn.tenant_transaction(app_conn, tenant) as tx:
        after = tb.load(tx)
    assert after.entries[:3] == stored.entries and after.checkpoint.length == 4
    empty = tenant_of(app_conn)  # an empty ledger imports as nothing
    assert (
        run(
            app_conn,
            empty,
            {"ledger": tw.header(str(empty), []), "tombstones": [], "checkpoints": []},
        )
        == 0
    )


@pytest.mark.db
def test_without_the_lock_or_into_a_non_empty_tenant_is_refused(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant = tenant_of(app_conn)
    body = ledger(tenant, now(app_conn) - DAY)
    assert failing(app_conn, tenant, body, before=lambda tx: None) == "QI002"
    for fn, bit in (("pg_advisory_xact_lock", 1), ("pg_advisory_xact_lock", 1 << 32),
                    ("pg_advisory_xact_lock_shared", 0)):  # fmt: skip
        near = partial(lock, fn=fn, bit=bit)
        assert failing(app_conn, tenant, body, before=near) == "QI002"
    assert delete_one(worker_conn, tenant)
    for b in (body, "not a section"):  # emptiness comes before the shape
        with pytest.raises(psycopg.Error) as e:
            run(app_conn, tenant, b)
        assert e.value.sqlstate == "QI001" and count(app_conn, tenant) == (1, 1)


def _edit(path: str, value: object) -> Callable[[Body], None]:
    def edit(b: Body) -> None:
        *head, last = path.split(".")
        node: Any = b
        for p in head:
            node = node[int(p)] if p.isdigit() else node[p]
        if value is KeyError:
            del node[last]
        else:
            node[int(last) if last.isdigit() else last] = value

    return edit


SHAPES = [  # each QI003 before any insert
    _edit("extra", 1), _edit("checkpoints", KeyError), _edit("tombstones", {}),
    _edit("ledger", []), _edit("ledger.extra", 1), _edit("ledger.key_id", KeyError),
    _edit("ledger.length", "3"), _edit("ledger.length", True),
    _edit("ledger.length", 3.5), _edit("ledger.head", 5),
    _edit("tombstones.0", "row"), _edit("tombstones.0.extra", 1),
    _edit("tombstones.0.row_hash", KeyError),
    _edit("tombstones.0.tenant_id", str(uuid.uuid4())),
    _edit("tombstones.0.seq", "0"), _edit("tombstones.0.seq", True),
    _edit("tombstones.0.seq", -1), _edit("tombstones.0.seq", 0.5),
    _edit("tombstones.1.seq", 2), _edit("tombstones.0.record_hash", "AB" * 32),
    _edit("tombstones.0.cls", "privacy"), _edit("tombstones.0.cls", 5),
    _edit("tombstones.0.job_id", "bad id"), _edit("tombstones.0.job_id", "é"),
    _edit("tombstones.0.requested_at", "2026-01-01T00:00:00+00:00"),
    _edit("tombstones.0.deleted_at", "2026-01-01T00:00:00.000000Z"),
    _edit("tombstones.0.deleted_at", "2000-01-01T00:00:00Z"),  # before requested
    _edit("tombstones.0.recorded_at", "2000-01-01T00:00:00Z"),  # before deleted
    _edit("tombstones.0.recorded_at", None),
    _edit("tombstones.0.residual_backups", "bk-synth-a"),
    _edit("tombstones.0.residual_backups", [1]),
    _edit("tombstones.0.residual_backups", ["bk-synth-b", "bk-synth-a"]),
    _edit("tombstones.0.residual_backups", ["bk-synth-a", "bk-synth-a"]),
    _edit("tombstones.0.residual_backups", [f"bk-{n:04d}" for n in range(1001)]),
    _edit("tombstones.0.backup_expiry_at", 5),
    _edit("tombstones.2.backup_expiry_at", "2026-01-01T00:00:00Z"),
    _edit("tombstones.1.residual_backups", ["bk-synth-a"]),
    _edit("tombstones.0.residual_backups_unknown", "false"),
    _edit("tombstones.0.residual_backups_unknown", None),
    _edit("tombstones.0.hash_scheme", "qw-record-hmac-9"),
    _edit("tombstones.0.key_id", "a" * 31), _edit("tombstones.0.prev_hash", "0" * 63),
    _edit("tombstones.1.prev_hash", "0" * 64), _edit("tombstones.0.entry_hash", 1),
    _edit("checkpoints.0", []), _edit("checkpoints.0.extra", 1),
    _edit("checkpoints.0.tenant_id", str(uuid.uuid4())),
    _edit("checkpoints.0.length", "1"), _edit("checkpoints.0.length", True),
    _edit("checkpoints.0.length", 0), _edit("checkpoints.0.length", 3),
    _edit("checkpoints.1.length", 2), _edit("checkpoints.1.length", 4),
    _edit("checkpoints.0.head", "x"), _edit("checkpoints.0.recorded_at", "today"),
    _edit("checkpoints.0.row_hash", None),
]  # fmt: skip


@pytest.mark.db
def test_every_shape_is_checked_before_any_insert(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant = tenant_of(app_conn)
    good = ledger(tenant, now(app_conn) - DAY)
    for n, edit in enumerate(SHAPES):
        bad = copy.deepcopy(good)
        edit(bad)
        assert failing(app_conn, tenant, bad) == "QI003", n
    odd: list[Any] = [{}, [], "x", None, 5]
    for body in odd:
        assert failing(app_conn, tenant, body) == "QI003", body
    with pytest.raises(psycopg.Error) as e:  # jsonb cannot hold a NUL: refused
        run(app_conn, tenant, {**good, "tombstones": [{"job_id": "x\x00"}]})
    assert e.value.sqlstate == "22P05" and count(app_conn, tenant) == (0, 0)
    with pytest.raises(errors.InsufficientPrivilege, match="portability_import"):
        run(worker_conn, tenant, good)  # no EXECUTE
    assert run(app_conn, tenant, good) == 5  # the untampered ledger still applies


@pytest.mark.db
def test_a_stamp_beyond_the_clock_is_refused(app_conn: Conn) -> None:
    """Any imported time more than 5 minutes past the database clock is QI003, the
    hashes and seals recomputed so only the bound can refuse it; a ledger ending
    one minute ahead is accepted. A backup expiry is a future time by nature and
    is not bounded."""
    for ahead, ok in ((10, False), (60 * 24 * 365 * 900, False), (1, True)):
        tenant = tenant_of(app_conn)
        body = ledger(tenant, now(app_conn) + timedelta(minutes=ahead, seconds=-12))
        if ok:
            assert run(app_conn, tenant, body) == 5
        else:
            assert failing(app_conn, tenant, body) == "QI003", ahead
    tenant = tenant_of(app_conn)  # a checkpoint stamp alone past the bound
    body = ledger(tenant, now(app_conn) - DAY)
    body["checkpoints"][-1]["recorded_at"] = format_instant(now(app_conn) + DAY)
    assert failing(app_conn, tenant, reseal(body)) == "QI003"


def _later(b: Body) -> None:  # tombstone 1 recorded before checkpoint 1's stamp
    b["checkpoints"][0]["recorded_at"] = format_instant(
        parse_instant(b["tombstones"][1]["recorded_at"]) + timedelta(microseconds=1)
    )


def _backwards(b: Body) -> None:  # tombstone 1 deleted before tombstone 0
    for k in ("requested_at", "deleted_at"):
        b["tombstones"][1][k] = "2020-01-01T00:00:00Z"


def _stamps(b: Body) -> None:  # tombstone 0 recorded after tombstone 1
    b["tombstones"][0]["recorded_at"] = format_instant(
        parse_instant(b["tombstones"][1]["recorded_at"]) + timedelta(microseconds=1)
    )


NO, AS_IS = {"rehash": False}, None
TAMPER: list[tuple[str, Callable[[Body], None], dict[str, bool] | None, str]] = [
    ("row seal", _edit("tombstones.1.row_hash", "ab" * 32), AS_IS, "QI003"),
    ("checkpoint seal", _edit("checkpoints.0.row_hash", "ab" * 32), AS_IS, "QI003"),
    ("header head", _edit("ledger.head", rt.GENESIS), AS_IS, "QI003"),
    ("header key", _edit("ledger.key_id", OTHER.key_id), AS_IS, "QI003"),
    ("header scheme", _edit("ledger.hash_scheme", None), AS_IS, "QI003"),
    ("header length", _edit("ledger.length", 2), AS_IS, "QI003"),
    ("record hash, not rehashed", _edit("tombstones.0.record_hash", "cd" * 32), NO,
     "23514"),
    ("job id, not rehashed", _edit("tombstones.0.job_id", "del-other"), NO, "23514"),
    ("marker, not rehashed", _edit("tombstones.1.residual_backups_unknown", False),
     NO, "23514"),
    ("checkpoint head", _edit("checkpoints.0.head", "ab" * 32), AS_IS, "23514"),
    ("mixed key", _edit("tombstones.1.key_id", OTHER.key_id), {}, "23514"),
    ("deletion time backwards", _backwards, {}, "23514"),
    ("stamp order", _stamps, {}, "23514"),
    ("tombstone before the previous checkpoint", _later, {}, "23514"),
]  # fmt: skip


@pytest.mark.db
def test_seals_and_hashes_are_verified_in_sql(app_conn: Conn) -> None:
    """Rows a direct caller tampers with: a seal or header that does not reproduce
    (QI003, the function's recompute), and hashes, keys, heads and times the
    replaced 0057 guard refuses (23514); nothing is written. A full rewrite
    verifies: integrity, not authenticity (T060)."""
    tenant = tenant_of(app_conn)
    good = ledger(tenant, now(app_conn) - DAY)
    for name, edit, how, want in TAMPER:
        bad = copy.deepcopy(good)
        edit(bad)
        if how is not None:
            bad = reseal(bad, **how)
        assert (name, failing(app_conn, tenant, bad)) == (name, want)
    forged = copy.deepcopy(good)
    forged["tombstones"][0]["record_hash"] = "cd" * 32
    assert run(app_conn, tenant, reseal(forged)) == 5


@pytest.mark.db
def test_only_the_definer_writes_imported_rows(
    app_conn: Conn, worker_conn: Conn
) -> None:
    """A runtime session that sets the import flag itself is still stamped by the
    destination clock and held to the 60-second rule; it cannot give a stamp or a
    seal (column grants), and qw_app cannot insert at all."""
    tenant = tenant_of(app_conn)
    row = ledger(tenant, now(app_conn) - DAY)["tombstones"][0]
    cols = [k for k in row if k not in ("tenant_id", "recorded_at", "row_hash")]
    for conn, extra, why, text in (
        (worker_conn, "", errors.CheckViolation, "stale_clock"),  # a day old
        (worker_conn, "recorded_at", errors.InsufficientPrivilege, "permission"),
        (worker_conn, "row_hash", errors.InsufficientPrivilege, "permission"),
        (app_conn, "", errors.InsufficientPrivilege, "permission"),
    ):
        names = ["tenant_id", *cols, *([extra] if extra else [])]
        args = [tenant, *(row[k] for k in names[1:])]
        with pytest.raises(why, match=text), tn.tenant_transaction(conn, tenant) as tx:
            tx.conn.execute("SELECT set_config('app.tombstones_import', 'on', true)")
            tx.conn.execute(
                f"INSERT INTO app.deletion_tombstone ({', '.join(names)}) "
                f"VALUES ({', '.join(['%s'] * len(names))})",
                args,
            )
    assert count(app_conn, tenant) == (0, 0)


@pytest.mark.db
def test_tenants_are_isolated(app_conn: Conn) -> None:
    """RLS binds the definer: another tenant cannot take this tenant's archive and
    sees none of its rows; its own ledger goes into its own rows."""
    a, b = tenant_of(app_conn), tenant_of(app_conn)
    start = now(app_conn) - DAY
    body = ledger(a, start)
    assert failing(app_conn, b, body) == "QI003"
    assert run(app_conn, a, body) == 5
    assert count(app_conn, b) == (0, 0)
    with tn.tenant_transaction(app_conn, b) as tx:
        assert tb.load(tx).entries == ()
    assert run(app_conn, b, ledger(b, start)) == 5
    assert count(app_conn, a) == count(app_conn, b) == (3, 2)


@pytest.mark.db
def test_a_store_writer_and_the_import_exclude_each_other(
    app_conn: Conn, worker_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """The replaced guard and `tombstone_store._lock` share `import:<tenant>` first
    (import, then `deletion_tombstone:`): a reader, a store writer and a direct
    insert on other connections wait for an open import, and an open writer holds
    the import off (then QI001)."""
    tenant = tenant_of(app_conn)
    body = ledger(tenant, now(app_conn) - DAY)

    def direct(otx: tn.TenantTx) -> None:
        otx.conn.execute(
            "INSERT INTO app.deletion_tombstone_checkpoint (tenant_id, "
            "length, head) VALUES (%s, 1, %s)",
            (tenant, "0" * 64),
        )

    with tn.tenant_transaction(app_conn, tenant) as tx:
        lock(tx)
        acts: list[tuple[str, Callable[[tn.TenantTx], object]]] = [
            ("qw_app", tb.load),
            ("qw_worker", append),
            ("qw_worker", direct),
        ]
        for login, act in acts:
            with psycopg.connect(runtime_urls[login], autocommit=True) as other:
                other.execute("SET lock_timeout = '300ms'")
                with (pytest.raises((errors.LockNotAvailable,
                                     tb.TombstoneStoreError)) as e,
                      tn.tenant_transaction(other, tenant) as otx):  # fmt: skip
                    act(otx)
                assert "LockNotAvailable" in f"{type(e.value).__name__} {e.value}"
        assert call(tx, body) == 5
    tenant = tenant_of(app_conn)
    with tn.tenant_transaction(worker_conn, tenant) as wtx:  # an open writer first
        assert append(wtx)
        with (pytest.raises(errors.LockNotAvailable),
              tn.tenant_transaction(app_conn, tenant) as tx):  # fmt: skip
            app_conn.execute("SET LOCAL lock_timeout = '300ms'")
            lock(tx)
    with pytest.raises(psycopg.Error) as err:
        run(app_conn, tenant, ledger(tenant, now(app_conn) - DAY))
    assert err.value.sqlstate == "QI001"


@pytest.mark.db
def test_a_tombstone_writer_waits_for_the_import_without_deadlock(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """The import holds `import:<tenant>` exclusive and then takes the tombstone
    lock through the guard; a store writer on another connection shares the
    import lock before the tombstone lock, so it waits holding nothing the import
    needs, the import completes with no 40P01, and the writer appends after it."""
    tenant = tenant_of(app_conn)
    body = ledger(tenant, now(app_conn) - DAY)
    failures: list[BaseException] = []

    def write(other: Conn) -> None:
        try:
            assert delete_one(other, tenant, "acct-waiting")
        except BaseException as e:  # reported below, not lost in the thread
            failures.append(e)

    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as other:
        pid = other.info.backend_pid
        with tn.tenant_transaction(app_conn, tenant) as tx:
            lock(tx)
            thread = threading.Thread(target=write, args=(other,))
            thread.start()
            waiting = ("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = %s "
                       "AND NOT granted)")  # fmt: skip
            for _ in range(200):
                if app_conn.execute(waiting, (pid,)).fetchone() == (True,):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("the writer never waited")
            assert call(tx, body) == 5
        thread.join(30)
    assert not thread.is_alive() and failures == []
    assert count(app_conn, tenant) == (4, 3)
