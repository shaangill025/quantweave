"""The watchlists import function (`0064`, `app.portability_import_watchlists`) called
directly on PostgreSQL 16 as the qw_app login, outside `destination_import`'s own
checks: the lock, emptiness, the importing owner's step-up session (decisions 116
and 125), the shape and the key (decision 126) must hold in SQL alone, and the
store must re-verify what SQL sealed. SYNTHETIC lists (`watchlist_rows`) in fresh
tenants.
"""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import tenancy as tn
from qw_adapters import watchlist_store as ws
from qw_domain import portability as pt
from qw_domain.instants import ensure_aware_utc
from test_portability_tenant_sections import watchlist_rows  # SYNTHETIC

Conn = psycopg.Connection[TupleRow]
CALL = "SELECT app.portability_import_watchlists(%s, %s, %s, %s)"
EXPORT = "export-synth-1"
# json.dumps escapes for the SQL seal: quotes, a backslash, controls, DEL, Latin-1,
# a CJK letter and a character outside the BMP (a surrogate pair)
ODD = 'SYNTHETIC "q" \\ \n\t\x01\x7f é 中 \U0001f600 x\'); DROP TABLE x; --'
A1 = "00000000-0000-4000-8000-0000000000a1"


def tenant_of(conn: Conn, step_up: bool = True) -> tuple[uuid.UUID, uuid.UUID,
                                                         uuid.UUID]:  # fmt: skip
    """A tenant, its owner and the owner's session (stepped up): (t, owner, s)."""
    tenant = uuid.uuid4()
    with tn.tenant_transaction(conn, tenant) as tx:
        conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
        owner = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, owner, tn.MembershipRole.TENANT_OWNER)
        session = session_of(tx, owner, step_up)
    return tenant, owner, session


def session_of(tx: tn.TenantTx, user: uuid.UUID, step_up: bool = True,
               ttl: timedelta = timedelta(hours=1)) -> uuid.UUID:  # fmt: skip
    session, _ = tn.create_session(tx, user, ttl)
    if step_up:
        tn.mark_step_up(tx, session)
    return session


def section(tenant: uuid.UUID) -> list[dict[str, Any]]:
    rows = watchlist_rows(str(tenant))
    rows[0]["name"] = ODD[:200]
    rows[0]["entries"][0]["thesis"] = ODD
    return rows


def key(wid: str, revision: int, export: str = EXPORT) -> str:  # decision 126
    text = f"import:{export}:{wid}:{revision}"
    return "imp" + hashlib.sha256(text.encode()).hexdigest()


def lock(tx: tn.TenantTx, fn: str = "pg_advisory_xact_lock", bit: int = 0) -> None:
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0) # %s)",
                    (f"import:{tx.tenant_id}", bit))  # fmt: skip


def call(tx: tn.TenantTx, body: Any, who: uuid.UUID, session: uuid.UUID,
         export: Any = EXPORT) -> int:  # fmt: skip
    row = tx.conn.execute(CALL, (Jsonb(body), export, who, session)).fetchone()
    assert row is not None
    return int(row[0])


def run(conn: Conn, t: tuple[uuid.UUID, uuid.UUID, uuid.UUID], body: Any,
        before: Callable[[tn.TenantTx], None] = lock, **kw: Any) -> int:  # fmt: skip
    tenant, who, session = t
    with tn.tenant_transaction(conn, tenant) as tx:
        before(tx)
        return call(tx, body, kw.pop("who", who), kw.pop("session", session), **kw)


def count(conn: Conn, tenant: uuid.UUID) -> int:
    with tn.tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute("SELECT count(*) FROM app.watchlist_revision"
                              ).fetchone()  # fmt: skip
    assert row is not None
    return int(row[0])


def failing(conn: Conn, t: tuple[uuid.UUID, uuid.UUID, uuid.UUID], body: Any,
            **kw: Any) -> str | None:  # fmt: skip
    with pytest.raises(psycopg.Error) as e:
        run(conn, t, body, **kw)
    assert count(conn, t[0]) == 0
    return e.value.sqlstate


def stored(conn: Conn, tenant: uuid.UUID) -> list[tuple[Any, ...]]:
    with tn.tenant_transaction(conn, tenant) as tx:
        return [tuple(r) for r in tx.conn.execute(
            "SELECT watchlist_id::text, revision, action, principal_id, session_id, "
            "idempotency_key, previous_hash, instrument_id, review_at_set, "
            "recorded_at, event_hash, name, description, entries "
            "FROM app.watchlist_revision ORDER BY watchlist_id")]  # fmt: skip


@pytest.mark.db
def test_the_lists_are_kept_attributed_and_re_exported(app_conn: Conn) -> None:
    """Decisions 116, 125 and 126: one `import` row per live list at its archive
    revision, made by the importing owner in their step-up session, with the
    archive-derived key; the store re-hashes SQL's seal and re-exports the section
    byte for byte; it then appends after the imported revision."""
    t = tenant_of(app_conn)
    tenant, owner, session = t
    body = section(tenant)
    clock = app_conn.execute("SELECT clock_timestamp()").fetchone()[0]  # type: ignore[index]
    assert run(app_conn, t, body) == 2
    with tn.tenant_transaction(app_conn, tenant, snapshot=True) as tx:
        assert pt.canonical(ws.export_watchlists(tx)) == pt.canonical(body)
    rows = stored(app_conn, tenant)
    assert [r[:9] for r in rows] == [
        (w["id"], w["revision"], "import", owner, session, key(w["id"], w["revision"]),
         None, None, False) for w in body]  # fmt: skip
    for wid, rev, action, who, s, k, _, _, _, at, h, name, text, entries in rows:
        assert clock <= at <= clock + timedelta(minutes=1)  # the destination clock
        assert h == ws.revision_hash(
            tenant,
            uuid.UUID(wid),
            rev,
            action,
            who,
            s,
            k,
            name,
            text,
            entries,
            ensure_aware_utc(at),
            None,
        )
    with tn.tenant_transaction(app_conn, tenant) as tx:  # the store appends after
        got, wrote = ws.update(tx, A1, owner, session, "synthetic-key-after-01", 3,
                               "SYNTHETIC renamed", "")  # fmt: skip
        assert wrote and got.revision == 4 and got.watchlist.revision == 4
        assert ws.delete(tx, A1, owner, session, "synthetic-key-after-02", 4)[1]
        assert [w.watchlist.watchlist_id for w in ws.page(tx, 10, None)] == [
            body[1]["id"]]  # fmt: skip
        with pytest.raises(ws.WatchlistStoreError):  # an import key is no create's
            ws.create(tx, owner, session, key(body[1]["id"], 1), "Empty", "")


@pytest.mark.db
def test_a_retry_gives_the_same_key(app_conn: Conn) -> None:
    """Decision 126: the key depends only on the archive, so a rolled-back attempt
    and its retry record the same keys; another export id gives other keys."""
    t = tenant_of(app_conn)
    body = section(t[0])
    with pytest.raises(errors.RaiseException), tn.tenant_transaction(
        app_conn, t[0]
    ) as tx:  # fmt: skip
        lock(tx)
        call(tx, body, t[1], t[2])
        first = [
            r[0]
            for r in tx.conn.execute(
                "SELECT idempotency_key FROM app.watchlist_revision ORDER BY 1"
            )
        ]
        tx.conn.execute("DO $$ BEGIN RAISE 'SYNTHETIC failure'; END $$")
    assert count(app_conn, t[0]) == 0
    assert run(app_conn, t, body) == 2
    again = sorted(r[5] for r in stored(app_conn, t[0]))
    assert again == first == sorted(key(w["id"], w["revision"]) for w in body)
    other = tenant_of(app_conn)
    assert run(app_conn, other, section(other[0]), export="export-synth-2") == 2
    assert {r[5] for r in stored(app_conn, other[0])}.isdisjoint(again)
    assert failing(app_conn, tenant_of(app_conn), body, export="bad id") == "QI003"


@pytest.mark.db
def test_without_the_lock_or_into_a_non_empty_tenant_is_refused(
    app_conn: Conn,
) -> None:
    t = tenant_of(app_conn)
    body = section(t[0])
    assert failing(app_conn, t, body, before=lambda tx: None) == "QI002"
    for fn, bit in (("pg_advisory_xact_lock", 1), ("pg_advisory_xact_lock", 1 << 32),
                    ("pg_advisory_xact_lock_shared", 0)):  # fmt: skip
        near = partial(lock, fn=fn, bit=bit)
        assert failing(app_conn, t, body, before=near) == "QI002"
    with tn.tenant_transaction(app_conn, t[0]) as tx:
        ws.create(tx, t[1], t[2], "synthetic-key-occupy1", "SYNTHETIC", "")
    with pytest.raises(psycopg.Error) as e:
        run(app_conn, t, body)
    assert e.value.sqlstate == "QI001" and count(app_conn, t[0]) == 1
    with pytest.raises(psycopg.Error) as e:  # emptiness comes before the shape
        run(app_conn, t, "not a list")
    assert e.value.sqlstate == "QI001"


@pytest.mark.db
def test_only_the_owners_live_step_up_session_is_accepted(
    app_conn: Conn, conn: Conn
) -> None:
    """Decision 125: the session must be the given owner's, live and stepped up
    within the hour; anything else is QI004 with no row."""
    t = tenant_of(app_conn)
    tenant, owner, session = t
    body = section(tenant)
    with tn.tenant_transaction(app_conn, tenant) as tx:
        member = tn.create_user(tx, "SYNTHETIC member")
        tn.add_membership(tx, member, tn.MembershipRole.TENANT_MEMBER)
        theirs = session_of(tx, member)
        plain = session_of(tx, owner, step_up=False)
        revoked = session_of(tx, owner)
        tn.revoke_session(tx, revoked)
        brief = session_of(tx, owner, ttl=timedelta(seconds=1))
        stale = session_of(tx, owner)
    foreign = tenant_of(app_conn)
    # SYNTHETIC: a step-up two hours old, written by the superuser past the 0002
    # update guard (which only lets the column move to the transaction time)
    conn.execute("SET session_replication_role = replica")
    conn.execute(
        "UPDATE app.session SET created_at = now() - interval '3 hours', "
        "step_up_at = now() - interval '2 hours' WHERE id = %s",
        (stale,),
    )
    conn.execute("SET session_replication_role = origin")
    time.sleep(1.2)  # `brief` expires
    for who, s in ((member, theirs), (owner, theirs), (member, session),
                   (owner, plain), (owner, revoked), (owner, brief), (owner, stale),
                   (foreign[1], foreign[2]), (owner, uuid.uuid4())):  # fmt: skip
        assert failing(app_conn, t, body, who=who, session=s) == "QI004", (who, s)
    assert run(app_conn, t, body) == 2


def _edit(n: int, path: str, value: object) -> Callable[[list[Any]], None]:
    def edit(rows: list[Any]) -> None:
        *head, last = path.split(".")
        node: Any = rows[n]
        for p in head:
            node = node[int(p)] if p.isdigit() else node[p]
        if value is KeyError:
            del node[last]
        else:
            node[int(last) if last.isdigit() else last] = value

    return edit


SHAPES = [  # each refused QI003 before any insert (the second list is untouched)
    _edit(0, "extra", 1), _edit(0, "description", KeyError),
    _edit(0, "tenant_id", str(uuid.uuid4())), _edit(0, "id", A1.upper()),
    _edit(0, "id", "wl-synth-1"), _edit(0, "revision", "3"),
    _edit(0, "revision", 3.5), _edit(0, "revision", 0), _edit(0, "name", 5),
    _edit(0, "name", ""), _edit(0, "description", None), _edit(0, "entries", {}),
    _edit(0, "entries.0", "entry"), _edit(0, "entries.0.extra", 1),
    _edit(0, "entries.0.needs", KeyError), _edit(0, "entries.0.instrument_id", 5),
    _edit(0, "entries.0.monitoring_priority", "10"),
    _edit(0, "entries.0.monitoring_priority", 10.5),
    _edit(0, "entries.0.monitoring_priority", 101), _edit(0, "entries.0.tags", "syn"),
    _edit(0, "entries.0.tags", [1]), _edit(0, "entries.0.thesis", None),
    _edit(0, "entries.0.review_at", "2026-11-08T12:00:00.000000Z"),
    _edit(0, "entries.0.review_at", 5), _edit(0, "entries.0.conditions", {}),
    _edit(0, "entries.0.conditions.0", []),
    _edit(0, "entries.0.conditions.0.values", "100"),
    _edit(0, "entries.0.conditions.0.values", [100]),
    _edit(0, "entries.0.conditions.0.extra", 1),
    _edit(0, "entries.1.instrument_id", "00000000-0000-4000-8000-000000000050"),
    _edit(0, "entries.1.instrument_id", "00000000-0000-4000-8000-000000000051"),
    _edit(0, "entries.1.instrument_id", "00000000-0000-4000-8000-0000000000AB"),
    _edit(0, "entries.1.instrument_id", "00000000000040008000000000000052"),
    _edit(1, "id", A1), _edit(1, "id", "00000000-0000-4000-8000-0000000000a0"),
]  # fmt: skip


@pytest.mark.db
def test_every_shape_is_checked_before_any_insert(
    app_conn: Conn, worker_conn: Conn
) -> None:
    t = tenant_of(app_conn)
    good = section(t[0])
    for n, edit in enumerate(SHAPES):
        bad = copy.deepcopy(good)
        edit(bad)
        assert failing(app_conn, t, bad) == "QI003", n
    odd: list[Any] = [{}, "x", None, [5], [None], [[]], [True], [good[0], 7]]
    for body in odd:
        assert failing(app_conn, t, body) == "QI003", body
    assert failing(app_conn, t, good[:1] * 2) == "QI003"  # a repeated id
    with pytest.raises(errors.InsufficientPrivilege, match="portability_import"):
        run(worker_conn, t, good)  # no EXECUTE
    assert run(app_conn, t, good) == 2  # the untampered lists still apply


@pytest.mark.db
def test_only_the_definer_writes_an_import_row(app_conn: Conn) -> None:
    """A runtime session that sets the import flag itself cannot insert an `import`
    row (the replaced 0022 guard), nor start a chain with `create` past revision
    1; the store's own actions are unchanged."""
    t = tenant_of(app_conn)
    for action, rev, prev, why in (("import", 3, None, "not an import"),
                                   ("import", 1, "0" * 64, "not an import"),
                                   ("create", 2, None, "not the next revision")
                                   ):  # fmt: skip
        with pytest.raises(errors.CheckViolation, match=why), tn.tenant_transaction(
            app_conn, t[0]
        ) as tx:  # fmt: skip
            tx.conn.execute("SELECT set_config('app.watchlists_import', 'on', true)")
            tx.conn.execute(
                "INSERT INTO app.watchlist_revision (tenant_id, watchlist_id, "
                "revision, action, principal_id, session_id, idempotency_key, name, "
                "description, entries, recorded_at, previous_hash, event_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'synthetic-key-forge01', "
                "'SYNTHETIC', '', '[]', clock_timestamp(), %s, %s)",
                (t[0], uuid.uuid4(), rev, action, t[1], t[2], prev, "0" * 64),
            )
    assert count(app_conn, t[0]) == 0


@pytest.mark.db
def test_tenants_are_isolated(app_conn: Conn) -> None:
    """RLS binds the definer: the tenant is the transaction's, and another tenant
    sees none of the imported rows and cannot take this tenant's archive."""
    a, b = tenant_of(app_conn), tenant_of(app_conn)
    body = section(a[0])
    assert failing(app_conn, b, body) == "QI003"  # another tenant's rows
    assert run(app_conn, a, body) == 2
    assert count(app_conn, b[0]) == 0
    with tn.tenant_transaction(app_conn, b[0]) as tx:
        assert ws.page(tx, 10, None) == [] and ws.load(tx, A1) is None
    assert run(app_conn, b, section(b[0])) == 2  # the same list ids, its own rows
    assert count(app_conn, a[0]) == count(app_conn, b[0]) == 2


@pytest.mark.db
def test_a_store_writer_and_the_import_exclude_each_other(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """The replaced guard and `watchlist_store._lock` share `import:<tenant>` first
    (import, then `watchlist:`): a writer on another connection waits for an open
    import, and an open writer holds the import off."""
    t = tenant_of(app_conn)
    body = section(t[0])
    with (psycopg.connect(runtime_urls["qw_app"], autocommit=True) as other,
          tn.tenant_transaction(app_conn, t[0]) as tx):  # fmt: skip
        lock(tx)
        with (pytest.raises(errors.LockNotAvailable),
              tn.tenant_transaction(other, t[0]) as otx):  # fmt: skip
            other.execute("SET LOCAL lock_timeout = '300ms'")
            ws.load(otx, A1)  # a reader too
        assert call(tx, body, t[1], t[2]) == 2
    t = tenant_of(app_conn)
    with (psycopg.connect(runtime_urls["qw_app"], autocommit=True) as other,
          tn.tenant_transaction(other, t[0]) as otx):  # fmt: skip
        ws.create(otx, t[1], t[2], "synthetic-key-writer1", "SYNTHETIC", "")
        with (pytest.raises(errors.LockNotAvailable),
              tn.tenant_transaction(app_conn, t[0]) as tx):  # fmt: skip
            app_conn.execute("SET LOCAL lock_timeout = '300ms'")
            lock(tx)
    with pytest.raises(psycopg.Error) as e:
        run(app_conn, t, section(t[0]))
    assert e.value.sqlstate == "QI001"


@pytest.mark.db
def test_a_direct_insert_waits_for_the_import(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """The guard itself shares the import lock (not only `watchlist_store._lock`):
    a qw_app insert outside the store, on another connection, waits."""
    t = tenant_of(app_conn)
    with (psycopg.connect(runtime_urls["qw_app"], autocommit=True) as other,
          tn.tenant_transaction(app_conn, t[0]) as tx):  # fmt: skip
        lock(tx)
        with (pytest.raises(errors.LockNotAvailable),
              tn.tenant_transaction(other, t[0])):  # fmt: skip
            other.execute("SET LOCAL lock_timeout = '300ms'")
            other.execute(
                "INSERT INTO app.watchlist_revision (tenant_id, watchlist_id, "
                "revision, action, principal_id, session_id, idempotency_key, name, "
                "description, entries, recorded_at, event_hash) VALUES (%s, %s, 1, "
                "'create', %s, %s, 'synthetic-key-direct1', 'SYNTHETIC', '', '[]', "
                "clock_timestamp(), %s)",
                (t[0], uuid.uuid4(), t[1], t[2], "0" * 64),
            )
        assert call(tx, section(t[0]), t[1], t[2]) == 2
