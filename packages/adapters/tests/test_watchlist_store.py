"""0022 and qw_adapters.watchlist_store as qw_app; the superuser `conn` observes or
tampers. SYNTHETIC tenants, users, sessions and names only; every time is the
database clock."""

from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from qw_adapters import tenancy as tn
from qw_adapters import watchlist_store as ws
from qw_domain.identity import InstrumentId
from qw_domain.instants import format_instant
from qw_domain.monitoring import Condition
from qw_domain.watchlists import Entry

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
K1, K2, K3, K4 = (f"SYNTHETIC-wl-key-000{i}" for i in range(1, 5))


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
    return Ctx(*ids)


def run(conn: Conn, c: Ctx, fn: str, *args: Any, b: bool = False,
        other: bool = False) -> Any:  # fmt: skip
    tenant = c.tenant_b if b else c.tenant
    who = (c.user_b, c.session_b) if b else (c.other, c.other_session) if other else (
        c.user, c.session)  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        if fn in ("load", "page"):
            return getattr(ws, fn)(tx, *args)
        return getattr(ws, fn)(tx, *(args[:1] if fn != "create" else ()), *who,
                               *(args[1:] if fn != "create" else args))  # fmt: skip


def count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM app.watchlist_revision").fetchone()
    assert row is not None
    return int(row[0])


def test_lifecycle_is_versioned_and_delete_is_a_final_tombstone(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    r, wrote = run(app_conn, c, "create", K1, "SYNTHETIC growth", "first")
    wid = r.watchlist.watchlist_id
    assert wrote and (r.revision, r.action, r.watchlist.name) == (
        1,
        "create",
        "SYNTHETIC growth",
    )
    assert r.watchlist.entries == () and str(uuid.UUID(wid)) == wid
    r2, _ = run(app_conn, c, "update", wid, K2, 1, "SYNTHETIC value", "second")
    assert (r2.revision, r2.watchlist.revision, r2.watchlist.description) == (
        2, 2, "second")  # fmt: skip
    for stale in (1, 3, 0):
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, "update", wid, K3, stale, "x", "")
        assert err.value.code == "stale"
    assert count(conn) == 2 and run(app_conn, c, "load", wid) == r2
    gone, wrote = run(app_conn, c, "delete", wid, K3, 2)
    assert wrote and (gone.revision, gone.action, gone.watchlist) == (
        3,
        "delete",
        r2.watchlist,
    )
    assert (
        run(app_conn, c, "load", wid) is None
        and run(app_conn, c, "page", 9, None) == []
    )
    for fn, args in (("update", (K4, 3, "x", "")), ("delete", (K4, 3))):
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, fn, wid, *args)
        assert err.value.code == "not_found"
    assert count(conn) == 3


def test_keys_replay_the_same_request_and_refuse_another(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    first, _ = run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    wid = first.watchlist.watchlist_id
    assert run(app_conn, c, "create", K1, "SYNTHETIC a", "") == (first, False)
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "create", K1, "SYNTHETIC b", "")
    assert err.value.code == "idempotency_key_reused"
    other, wrote = run(app_conn, c, "create", K1, "SYNTHETIC a", "", other=True)
    assert wrote and other.watchlist.watchlist_id != wid  # keys are per principal
    upd, _ = run(app_conn, c, "update", wid, K2, 1, "SYNTHETIC c", "")
    run(app_conn, c, "update", wid, K3, 2, "SYNTHETIC d", "")
    assert run(app_conn, c, "update", wid, K2, 1, "SYNTHETIC c", "") == (upd, False)
    for args in ((K2, 1, "SYNTHETIC e", ""), (K2, 2, "SYNTHETIC c", "")):
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, "update", wid, *args)
        assert err.value.code == "idempotency_key_reused"
    theirs, wrote = run(
        app_conn, c, "update", wid, K2, 3, "SYNTHETIC o", "", other=True
    )
    assert wrote and (theirs.revision, theirs.principal) == (4, c.other)  # not a replay
    gone, _ = run(app_conn, c, "delete", wid, K4, 4)
    assert run(app_conn, c, "delete", wid, K4, 4) == (gone, False)
    assert count(conn) == 6


def test_tenancy_paging_and_non_canonical_ids(app_conn: Conn, c: Ctx) -> None:
    made = [run(app_conn, c, "create", f"SYNTHETIC-wl-page-{i:04}", f"SYNTHETIC {i}",
                "")[0].watchlist.watchlist_id for i in range(3)]  # fmt: skip
    ids = sorted(made)
    assert [r.watchlist.watchlist_id for r in run(app_conn, c, "page", 2, None)] == ids[
        :2
    ]
    assert [
        r.watchlist.watchlist_id for r in run(app_conn, c, "page", 2, ids[0])
    ] == ids[1:]
    wid = ids[0]
    assert run(app_conn, c, "page", 9, None, b=True) == []
    assert run(app_conn, c, "load", wid, b=True) is None
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "update", wid, K1, 1, "x", "", b=True)
    assert err.value.code == "not_found"
    for alias in (wid.upper(), wid.replace("-", ""), f"{{{wid}}}", "no-such-id"):
        assert run(app_conn, c, "load", alias) is None


def tamper(conn: Conn, sql: str, args: Any = None) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(sql, args)


def rows(conn: Conn) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM app.watchlist_revision ORDER BY "
                           "watchlist_id, revision").fetchall()  # fmt: skip


def insert(conn: Conn, row: dict[str, Any], replica: bool = False) -> None:
    row = {**row, "entries": Jsonb(row["entries"])}
    sql = (f"INSERT INTO app.watchlist_revision ({', '.join(row)}) VALUES "
           f"({', '.join(f'%({k})s' for k in row)})")  # fmt: skip
    if replica:
        tamper(conn, sql, row)
    else:
        conn.execute(sql, row)


def test_database_guards_refuse_out_of_chain_rows(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    r, _ = run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    run(app_conn, c, "update", r.watchlist.watchlist_id, K2, 1, "SYNTHETIC b", "")
    last = rows(conn)[-1]
    nxt = {**last, "revision": 3, "previous_hash": last["event_hash"],
           "idempotency_key": K3}  # fmt: skip
    fresh = {**rows(conn)[0], "watchlist_id": uuid.uuid4(), "idempotency_key": K4}
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        revoked, expired = (tn.create_session(tx, c.user, timedelta(hours=1))[0]
                            for _ in range(2))  # fmt: skip
        tn.revoke_session(tx, revoked)
    tamper(conn, "UPDATE app.session SET created_at = created_at - interval '2 hours',"
           " expires_at = clock_timestamp() - interval '1 second' WHERE id = %s",
           (expired,))  # fmt: skip
    bad = [
        ({**nxt, "revision": 4}, 0),  # skips a revision
        ({**nxt, "previous_hash": "a" * 64}, 0),  # names another hash
        ({**nxt, "session_id": c.other_session}, 0),  # another user's session
        ({**nxt, "action": "create"}, 0),  # only revision 1 creates
        (fresh, -120),  # a create two minutes back (no previous revision to blame)
        (nxt, 3600),  # an hour ahead of the database clock
        (nxt, -30),  # inside the window, but before the previous revision
        ({**nxt, "session_id": revoked}, 0),  # the user's own, revoked
        ({**nxt, "session_id": expired}, 0),  # the user's own, expired
    ]
    for row, seconds in [*bad, (fresh, 0), (nxt, 0)]:  # the last two: controls
        with tn.tenant_transaction(app_conn, c.tenant) as tx:
            stamp = tx.conn.execute(
                f"SELECT clock_timestamp() + interval '{seconds} seconds'"
            ).fetchone()
            assert stamp is not None
            with tx.conn.transaction(force_rollback=True):
                if (row, seconds) in bad:
                    with pytest.raises(psycopg.errors.Error):
                        insert(tx.conn, {**row, "recorded_at": stamp[0]})
                else:
                    insert(tx.conn, {**row, "recorded_at": stamp[0]})
    for tenant, n in ((c.tenant_b, 0), (c.tenant, 2)):  # RLS: B reads none of A's
        with tn.tenant_transaction(app_conn, tenant) as tx:
            got = tx.conn.execute("SELECT count(*) FROM app.watchlist_revision")
            assert got.fetchone() == (n,)
    for sql in ("UPDATE app.watchlist_revision SET name = 'x'",
                "DELETE FROM app.watchlist_revision",
                "TRUNCATE app.watchlist_revision"):  # fmt: skip
        with (
            tn.tenant_transaction(app_conn, c.tenant) as tx,
            pytest.raises(psycopg.errors.Error),
        ):
            tx.conn.execute(sql)
        with conn.transaction(), pytest.raises(psycopg.errors.Error):
            conn.execute(sql)  # the superuser too: the append-only trigger
    run(app_conn, c, "delete", r.watchlist.watchlist_id, K3, 2)
    tomb = rows(conn)[-1]
    after = {**tomb, "revision": 4, "previous_hash": tomb["event_hash"],
             "action": "update", "idempotency_key": K4}  # fmt: skip
    with (
        tn.tenant_transaction(app_conn, c.tenant) as tx,
        pytest.raises(psycopg.errors.Error),
    ):
        stamp = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        insert(tx.conn, {**after, "recorded_at": stamp[0]})  # type: ignore[index]
    assert count(conn) == 3


@pytest.mark.parametrize("edit", [
    "name = 'SYNTHETIC forged'", "description = 'forged'", "entries = '[{}]'",
    "recorded_at = recorded_at - interval '1 hour'", "principal_id = %(o)s",
    "recorded_at = recorded_at + interval '1 second'",
    "session_id = %(s)s", "idempotency_key = 'SYNTHETIC-wl-key-9999'",
    "revision = 5", "previous_hash = repeat('a', 64)", "event_hash = repeat('b', 64)",
])  # fmt: skip
def test_tampered_revisions_fail_closed(app_conn: Conn, conn: Conn, c: Ctx,
                                        edit: str) -> None:  # fmt: skip
    r, _ = run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    wid = r.watchlist.watchlist_id
    run(app_conn, c, "update", wid, K2, 1, "SYNTHETIC b", "")
    saved = rows(conn)
    tamper(conn, f"UPDATE app.watchlist_revision SET {edit} WHERE revision = 2",
           {"o": c.other, "s": c.other_session})  # fmt: skip
    for fn, args in (
        ("load", (wid,)),
        ("page", (9, None)),
        ("update", (wid, K3, 2, "x", "")),
        ("delete", (wid, K3, 2)),
    ):
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, fn, *args)
        assert err.value.code == "integrity", fn
    tamper(conn, "DELETE FROM app.watchlist_revision")
    for row in saved:  # restored: reads are restored
        insert(conn, row, replica=True)
    assert run(app_conn, c, "load", wid).revision == 2


@pytest.mark.parametrize("change", ["entries", "tombstone", "revision", "previous_hash",
                                    "recorded_at"])  # fmt: skip
def test_a_rehashed_row_still_meets_the_domain(app_conn: Conn, conn: Conn, c: Ctx,
                                               change: str) -> None:  # fmt: skip
    r, _ = run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    wid = r.watchlist.watchlist_id
    if change != "entries":  # a second revision; the chain cases rewrite it
        fn = "delete" if change == "tombstone" else "update"
        run(app_conn, c, fn, wid, K2, 1, *(("SYNTHETIC b", "") * (fn == "update")))
    row = rows(conn)[-1]
    old = row["revision"]
    edits: dict[str, dict[str, Any]] = {
            "entries": {"entries": [{"instrument_id": "SYNTHETIC-x"}]},
            "tombstone": {"name": "SYNTHETIC renamed by a tombstone"},
            "revision": {"revision": 3}, "previous_hash": {"previous_hash": "a" * 64},
            "recorded_at": {"recorded_at": row["recorded_at"] - timedelta(hours=1)},
            }  # fmt: skip
    row |= edits[change]
    # Re-hashed, so only the chain (or domain) check can refuse it.
    row["event_hash"] = ws.revision_hash(*(row[k] for k in (
        "tenant_id", "watchlist_id", "revision", "action", "principal_id",
        "session_id", "idempotency_key", "name", "description", "entries",
        "recorded_at", "previous_hash")))  # fmt: skip
    tamper(conn, "DELETE FROM app.watchlist_revision WHERE revision = %s", (old,))
    insert(conn, row, replica=True)
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "page", 9, None)
    assert err.value.code == "integrity"


def test_the_store_and_its_table_reach_no_holdings(app_conn: Conn, conn: Conn) -> None:
    """AT057 at the persistence level: no import of the journal, postings, import or
    proposal stores, and no foreign key beyond membership and session."""
    src = Path(ws.__file__).read_text()
    names = {
        a.name
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in n.names
    }
    mods = {n.module for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.ImportFrom) and n.module}  # fmt: skip
    assert names | mods and not [
        x
        for x in names | mods
        if any(s in x for s in ("journal", "posting", "import_store", "proposal"))
    ]
    refs = conn.execute(
        "SELECT confrelid::regclass::text FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'app.watchlist_revision'::regclass ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in refs] == ["app.membership", "app.session"]


# --- entries (0023, increment 3) ---

A = "00000000-0000-4000-8000-00000000000a"  # sorts after B
B = "00000000-0000-4000-8000-000000000001"
C = "00000000-0000-4000-8000-00000000000c"
FEED = "SYNTHETIC-feed-iex"


def entry(iid: str, thesis: str = "SYNTHETIC thesis", priority: int = 40) -> Entry:
    cond = Condition("price_cross", "gt", ("100.25",), FEED, "SYNTHETIC cross")
    return Entry(InstrumentId.from_wire(iid), thesis, ("SYNTHETIC-tag",),
                      None, priority, (cond,), "SYNTHETIC invalidation")  # fmt: skip


def stored(iid: str, thesis: str = "SYNTHETIC thesis") -> dict[str, Any]:
    """Hand-written storage form (independent of `entry_json`)."""
    return {"instrument_id": iid, "thesis": thesis, "tags": ["SYNTHETIC-tag"],
            "review_at": None, "monitoring_priority": 40, "conditions": [
                {"metric": "price_cross", "operator": "gt", "values": ["100.25"],
                 "data_spec_id": FEED, "description": "SYNTHETIC cross"}],
            "invalidation_text": "SYNTHETIC invalidation",
            "needs": "realtime_exchange_limited"}  # fmt: skip


def iid_of(s: str) -> InstrumentId:
    return InstrumentId.from_wire(s)


def test_entries_follow_the_domain_and_round_trip(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    r, _ = run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    wid = r.watchlist.watchlist_id
    r2, wrote = run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    assert wrote and (r2.revision, r2.action, r2.instrument) == (2, "add_entry", A)
    assert rows(conn)[-1]["entries"] == [stored(A)]
    assert rows(conn)[-1]["instrument_id"] == uuid.UUID(A)
    assert run(app_conn, c, "load", wid) == r2
    refusals = [("add_entry", (K3, 2, entry(A)), "entry_exists"),
                ("update_entry", (K3, 2, entry(C)), "entry_missing"),
                ("remove_entry", (K3, 2, iid_of(C)), "entry_missing"),
                ("add_entry", (K3, 1, entry(B)), "stale"),
                ("add_entry", (K3, 3, entry(B)), "stale")]  # fmt: skip
    for fn, args, code in refusals:
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, fn, wid, *args)
        assert err.value.code == code, fn
    assert count(conn) == 2
    run(app_conn, c, "add_entry", wid, K3, 2, entry(B))
    assert [e["instrument_id"] for e in rows(conn)[-1]["entries"]] == [B, A]
    r4, _ = run(app_conn, c, "update_entry", wid, K4, 3, entry(A, "SYNTHETIC new"))
    assert rows(conn)[-1]["entries"] == [stored(B), stored(A, "SYNTHETIC new")]
    assert r4.watchlist.entry(iid_of(A)).thesis == "SYNTHETIC new"
    r5, _ = run(app_conn, c, "remove_entry", wid, K1, 4, iid_of(B))
    assert (r5.instrument, rows(conn)[-1]["entries"]) == (
        B,
        [stored(A, "SYNTHETIC new")],
    )
    run(app_conn, c, "update", wid, K2, 5, "SYNTHETIC b", "")  # a rename keeps entries
    gone, _ = run(app_conn, c, "delete", wid, K3, 6)
    assert rows(conn)[-1]["entries"] == [stored(A, "SYNTHETIC new")]
    assert gone.instrument is None and run(app_conn, c, "load", wid) is None
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "add_entry", wid, "SYNTHETIC-wl-key-0005", 7, entry(C))
    assert err.value.code == "not_found" and count(conn) == 7


def test_entry_keys_replay_the_same_request_only(app_conn: Conn, conn: Conn,
                                                 c: Ctx) -> None:  # fmt: skip
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    added, _ = run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    upd, _ = run(app_conn, c, "update_entry", wid, K2, 2, entry(A, "SYNTHETIC u"))
    rem, _ = run(app_conn, c, "remove_entry", wid, K2, 3, iid_of(A))
    assert run(app_conn, c, "add_entry", wid, K2, 1, entry(A)) == (added, False)
    assert run(app_conn, c, "update_entry", wid, K2, 2,
               entry(A, "SYNTHETIC u")) == (upd, False)  # fmt: skip
    assert run(app_conn, c, "remove_entry", wid, K2, 3, iid_of(A)) == (rem, False)
    for expected in (2, None):  # add: the contract names no If-Match; body only
        assert run(app_conn, c, "add_entry", wid, K2, expected, entry(A)) == (
            added, False)  # fmt: skip
    cond = Condition("volume", "gt", ("1000",), FEED, "SYNTHETIC cross")
    for fn, args in (("add_entry", (K2, 1, entry(A, "SYNTHETIC other"))),
                     ("add_entry", (K2, 1, entry(B))),
                     ("add_entry", (K2, 1, entry(A, priority=41))),
                     ("add_entry", (K2, 1, replace(entry(A), conditions=(cond,)))),
                     ("add_entry", (K2, 1, replace(entry(A), tags=("SYNTHETIC-x",)))),
                     ("add_entry", (K2, 1, replace(entry(A), invalidation_text="x"))),
                     ("update_entry", (K2, 2, entry(A, "SYNTHETIC v"))),
                     ("remove_entry", (K2, 3, iid_of(B)))):  # fmt: skip
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, fn, wid, *args)
        assert err.value.code == "idempotency_key_reused", args
    theirs, wrote = run(app_conn, c, "add_entry", wid, K2, 4, entry(A), other=True)
    assert wrote and theirs.revision == 5
    latest, wrote = run(app_conn, c, "add_entry", wid, K3, None, entry(B))
    assert wrote and latest.revision == 6 and count(conn) == 6  # the latest, locked


def test_entry_limit(app_conn: Conn, c: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws, "MAX_ENTRIES", 1)
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "add_entry", wid, K3, 2, entry(B))
    assert err.value.code == "entry_limit"
    run(app_conn, c, "update_entry", wid, K3, 2, entry(A, "SYNTHETIC u"))  # still ok


def test_0023_names_one_instrument_per_entry_action(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    run(app_conn, c, "create", K1, "SYNTHETIC a", "")
    last = rows(conn)[-1]
    nxt = {**last, "revision": 2, "previous_hash": last["event_hash"],
           "idempotency_key": K2, "action": "add_entry",
           "instrument_id": uuid.UUID(A)}  # fmt: skip
    bad = [{**nxt, "instrument_id": None}, {**nxt, "action": "update"},
           {**nxt, "action": "rename_entry"},
           {**nxt, "entries": [stored(A)] * 501}]  # fmt: skip
    for row in [*bad, nxt, {**nxt, "entries": [stored(A)] * 500}]:  # two controls
        with tn.tenant_transaction(app_conn, c.tenant) as tx:
            stamp = tx.conn.execute("SELECT clock_timestamp()").fetchone()
            assert stamp is not None
            with tx.conn.transaction(force_rollback=True):
                if row in bad:
                    with pytest.raises(psycopg.errors.CheckViolation):
                        insert(tx.conn, {**row, "recorded_at": stamp[0]})
                else:
                    insert(tx.conn, {**row, "recorded_at": stamp[0]})
    assert count(conn) == 1


def _rehash(row: dict[str, Any]) -> str:
    return ws.revision_hash(*(row[k] for k in (
        "tenant_id", "watchlist_id", "revision", "action", "principal_id",
        "session_id", "idempotency_key", "name", "description", "entries",
        "recorded_at", "previous_hash")), instrument=row["instrument_id"])  # fmt: skip


def _forge(change: str, row: dict[str, Any]) -> None:
    e = row["entries"]
    if change == "priority-text":
        e[0]["monitoring_priority"] = "40"
    elif change == "no-needs":
        del e[0]["needs"]
    elif change == "extra-key":
        e[0]["owned"] = True
    elif change == "unsorted":
        e.reverse()
    elif change == "other-entry-too":
        e[0]["thesis"] = "SYNTHETIC forged"  # B also changed by A's add
    elif change == "target-absent":
        row["instrument_id"] = uuid.UUID(C)
    elif change == "review-at-format":
        e[1]["review_at"] = row["recorded_at"].isoformat()  # "+00:00", not "Z"
    elif change == "unsupported-op":
        e[1]["conditions"][0]["operator"] = "eq"
    elif change == "values-not-list":
        e[1]["conditions"][0]["values"] = "100.25"
    elif change == "renamed":
        row["name"] = "SYNTHETIC renamed by an entry"
    elif change == "as-remove":
        row["action"] = "remove_entry"
    elif change == "described":
        row["description"] = "SYNTHETIC forged description"


FORGED = ["priority-text", "no-needs", "extra-key", "unsorted", "other-entry-too",
          "target-absent", "review-at-format", "unsupported-op", "values-not-list",
          "renamed", "as-remove", "described"]  # fmt: skip


@pytest.mark.parametrize("change", FORGED)
def test_a_rehashed_entry_snapshot_must_replay(app_conn: Conn, conn: Conn, c: Ctx,
                                               change: str) -> None:  # fmt: skip
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "add_entry", wid, K2, 1, entry(B))
    run(app_conn, c, "add_entry", wid, K3, 2, entry(A))
    row = rows(conn)[-1]
    assert [e["instrument_id"] for e in row["entries"]] == [B, A]
    _forge(change, row)
    row["event_hash"] = _rehash(row)
    tamper(conn, "DELETE FROM app.watchlist_revision WHERE revision = 3")
    insert(conn, row, replica=True)
    for fn, args in (("load", (wid,)), ("page", (9, None)),
                     ("remove_entry", (wid, K4, 3, iid_of(A)))):  # fmt: skip
        with pytest.raises(ws.WatchlistStoreError) as err:
            run(app_conn, c, fn, *args)
        assert err.value.code == "integrity", fn


def test_an_unrehashed_instrument_edit_fails_the_hash(app_conn: Conn, conn: Conn,
                                                     c: Ctx) -> None:  # fmt: skip
    """A no-op update of B re-pointed at A still replays, so only the hash (which
    covers the instrument that key replays match on) refuses it."""
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    run(app_conn, c, "add_entry", wid, K3, 2, entry(B))
    run(app_conn, c, "update_entry", wid, K4, 3, entry(B))  # unchanged
    tamper(conn, "UPDATE app.watchlist_revision SET instrument_id = %s "
           "WHERE revision = 4", (uuid.UUID(A),))  # fmt: skip
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "load", wid)
    assert err.value.code == "integrity"


def test_a_list_action_naming_an_instrument_fails_without_0023(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    """The adapter check behind the 0023 CHECK: with the constraint dropped (this
    test's own database), a rehashed rename that names an instrument is refused."""
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "update", wid, K2, 1, "SYNTHETIC b", "")
    conn.execute("ALTER TABLE app.watchlist_revision "
                 "DROP CONSTRAINT watchlist_revision_instrument")  # fmt: skip
    row = rows(conn)[-1] | {"instrument_id": uuid.UUID(A)}
    row["event_hash"] = _rehash(row)
    tamper(conn, "DELETE FROM app.watchlist_revision WHERE revision = 2")
    insert(conn, row, replica=True)
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "load", wid)
    assert err.value.code == "integrity"


def test_a_canonical_review_time_loads(app_conn: Conn, conn: Conn, c: Ctx) -> None:
    """Control for `review-at-format`: the canonical `Z` form replays and is kept,
    also through an update."""
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    row = rows(conn)[-1]
    at = row["recorded_at"] + timedelta(days=7)
    row["entries"][0] |= {"review_at": format_instant(at), "needs": "delayed"}
    row["event_hash"] = _rehash(row)
    tamper(conn, "DELETE FROM app.watchlist_revision WHERE revision = 2")
    insert(conn, row, replica=True)
    assert run(app_conn, c, "load", wid).watchlist.entries[0].review_at == at
    # An update sets only the contract fields: the review time and needs are kept.
    run(app_conn, c, "update_entry", wid, K3, 2, entry(A, "SYNTHETIC u"))
    assert rows(conn)[-1]["entries"] == [stored(A, "SYNTHETIC u") | {
        "review_at": format_instant(at), "needs": "delayed"}]  # fmt: skip


def test_a_rehashed_update_must_keep_review_at_and_needs(
    app_conn: Conn, conn: Conn, c: Ctx
) -> None:
    """The store keeps both on an update, so a stored update that changes either is
    forged even when its snapshot replays."""
    wid = run(app_conn, c, "create", K1, "SYNTHETIC a", "")[0].watchlist.watchlist_id
    run(app_conn, c, "add_entry", wid, K2, 1, entry(A))
    run(app_conn, c, "update_entry", wid, K3, 2, entry(A, "SYNTHETIC u"))
    row = rows(conn)[-1]
    row["entries"][0]["needs"] = "delayed"
    row["event_hash"] = _rehash(row)
    tamper(conn, "DELETE FROM app.watchlist_revision WHERE revision = 3")
    insert(conn, row, replica=True)
    with pytest.raises(ws.WatchlistStoreError) as err:
        run(app_conn, c, "load", wid)
    assert err.value.code == "integrity"


def test_0022_era_hashes_are_golden() -> None:
    """Hard-coded: sha256 of the hand-written canonical JSON of a SYNTHETIC create
    and rename with no instrument (computed outside `revision_hash`)."""
    t, w, p, s = (uuid.UUID(f"{d * 8}-{d * 4}-4{d * 3}-8{d * 3}-{d * 12}")
                  for d in "1234")  # fmt: skip
    at = datetime(2026, 1, 1, tzinfo=UTC)  # a fixed hash input, not a stored time
    h1 = ws.revision_hash(t, w, 1, "create", p, s, K1, "SYNTHETIC a", "", [], at,
                          None)  # fmt: skip
    assert h1 == "4e9985f1f6419432be688b60b8b743824ddea2ce32c3d4e19778990ed07b1df7"
    h2 = ws.revision_hash(t, w, 2, "update", p, s, K2, "SYNTHETIC b", "d", [],
                          at + timedelta(seconds=1.5), h1)  # fmt: skip
    assert h2 == "ee7cdb7d31d7f8af7b4dbfc2a441eba5d7046f79153d84bf7e23ea72674de9c2"
