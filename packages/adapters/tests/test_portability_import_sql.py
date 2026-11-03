"""The decision 112 journal import function (`0060`,
`app.portability_import_journal`) called directly on PostgreSQL 16, as the qw_app
login, outside `destination_import`'s own checks: the lock, emptiness, chain, event
id, revision and knowledge-time checks must hold in SQL alone. SYNTHETIC rows
(`journal_rows`, the domain section fixture) in fresh empty tenants.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import destination_import as di
from qw_adapters import journal_store as js
from qw_adapters import tenancy as tn
from qw_domain import ledger_wire as lw
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money
from qw_domain.instants import parse_instant
from qw_domain.postings import Header, deposit
from test_portability_tenant_sections import journal_rows  # SYNTHETIC rows

Conn = psycopg.Connection[TupleRow]
CALL = "SELECT app.portability_import_journal(%s, %s)"
PATH = ["search_path=pg_catalog, pg_temp"]  # pg_temp last: never shadows


def _journal(tx: tn.TenantTx) -> None:
    """A SYNTHETIC deposit into SYN-OTHER, through the store (stamped)."""
    h = Header("SYN-OTHER", parse_instant("2026-01-01T00:00:00Z"),
               SourceRef("SYN-BROKER", "o0"))  # fmt: skip
    js.post(tx, deposit(h, Money.of("10", "USD")))


def tenant_of(conn: Conn) -> uuid.UUID:
    tenant = uuid.uuid4()
    with tn.tenant_transaction(conn, tenant) as tx:
        conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
        user = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
    return tenant


def body(tenant: uuid.UUID, edit: Callable[[list[dict[str, Any]]], None] | None = None,
         rechain: bool = True) -> dict[str, Any]:  # fmt: skip
    """The section; `edit` changes the rows, then the chains are recomputed over
    them (`rechain`) or kept as the original rows' chains."""
    rows = journal_rows(str(tenant))
    chains = lw.chains(str(tenant), rows)
    if edit is not None:
        edit(rows)
    return {"chains": lw.chains(str(tenant), rows) if rechain else chains,
            "events": rows}  # fmt: skip


def run(conn: Conn, tenant: uuid.UUID, args: tuple[Any, Any], lock: bool = True,
        before: Callable[[tn.TenantTx], None] | None = None) -> int:  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        if before is not None:
            before(tx)
        if lock:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         (f"import:{tenant}",))  # fmt: skip
        row = conn.execute(CALL, (Jsonb(args[0]), Jsonb(args[1]))).fetchone()
    assert row is not None
    return int(row[0])


def events(conn: Conn, tenant: uuid.UUID) -> int:
    with tn.tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute("SELECT count(*) FROM app.ledger_event").fetchone()
    assert row is not None
    return int(row[0])


def failing(conn: Conn, tenant: uuid.UUID, args: tuple[Any, Any],
            **kw: Any) -> str | None:  # fmt: skip
    with pytest.raises(psycopg.Error) as e:
        run(conn, tenant, args, **kw)
    assert events(conn, tenant) == 0
    return e.value.sqlstate


@pytest.mark.db
def test_the_stamps_are_kept_and_the_store_re_exports_the_section(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    section = body(tenant)
    assert run(app_conn, tenant, di.journal_args(section)) == 5
    with tn.tenant_transaction(app_conn, tenant, snapshot=True) as tx:
        assert js.export_journal(tx) == section  # recorded_at and revisions kept
    with tn.tenant_transaction(app_conn, tenant) as tx:  # a later post appends
        h = Header("SYN-ACCT", parse_instant("2026-01-05T00:00:00Z"),
                   SourceRef("SYN-BROKER", "r9"))  # fmt: skip
        js.post(tx, deposit(h, Money.of("5", "USD")))
        assert js.revision(tx, "SYN-ACCT") == 5


@pytest.mark.db
def test_without_the_import_lock_or_into_a_non_empty_tenant_is_refused(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    args = di.journal_args(body(tenant))
    assert failing(app_conn, tenant, args, lock=False) == "QI002"

    def other_lock(tx: tn.TenantTx) -> None:  # another tenant's import lock
        tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"import:{uuid.uuid4()}",))  # fmt: skip

    assert failing(app_conn, tenant, args, lock=False, before=other_lock) == "QI002"
    for bit in (1, 1 << 32, 0):  # a near key (low or high half), then shared mode
        with (
            pytest.raises(psycopg.Error) as e,
            tn.tenant_transaction(app_conn, tenant) as tx,
        ):
            tx.conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0) # %s)"
                if bit else "SELECT pg_advisory_xact_lock_shared(hashtextextended("
                "%s, 0) # %s)", (f"import:{tenant}", bit))  # fmt: skip
            tx.conn.execute(CALL, (Jsonb(args[0]), Jsonb(args[1])))
        assert e.value.sqlstate == "QI002"
    with tn.tenant_transaction(app_conn, tenant) as tx:
        _journal(tx)
    with pytest.raises(psycopg.Error) as e:
        run(app_conn, tenant, args)
    assert e.value.sqlstate == "QI001" and events(app_conn, tenant) == 1


def _amount(rows: list[dict[str, Any]]) -> None:
    rows[0]["postings"][0]["amount"] = "1001"  # the id no longer reproduces


def _backdated(rows: list[dict[str, Any]]) -> None:
    rows[1]["recorded_at"] = "2025-01-01T00:00:00Z"  # before revision 1's


@pytest.mark.db
def test_a_tampered_chain_event_or_stamp_is_refused_with_no_rows(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    good = body(tenant)
    head = [dict(c) for c in good["chains"]]
    head[0]["head"] = "0" * 64
    other = body(uuid.uuid4())  # another tenant's chain: the chain binds the id
    store = di.journal_args(good)
    late = [dict(e) for e in store[0]]
    late[0]["recorded_at"] = "2026-01-02T16:00:00.000001Z"  # store form, after export
    for args in ((store[0], head), (store[0], good["chains"][:1]),
                 (store[0], []), (store[0], other["chains"]), (late, store[1]),
                 di.journal_args(body(tenant, _amount, rechain=False))):  # fmt: skip
        assert failing(app_conn, tenant, args) == "QI003"
    # revision and knowledge-time order are checked by the stamp trigger itself
    gap = [{**e, "account_rev": e["account_rev"] + 1} if n in (1, 2) else e
           for n, e in enumerate(store[0])]  # SYN-ACCT 1, 3, 4  # fmt: skip
    backdated = di.journal_args(body(tenant, _backdated))  # chains recomputed
    for args in ((gap, store[1]), backdated):
        assert failing(app_conn, tenant, args) == "23514"
    assert run(app_conn, tenant, store) == 5  # the untampered rows still apply


def event_id(e: dict[str, Any]) -> str:
    """An independent oracle of `JournalEvent.event_id` over the store form."""
    content = {"kind": e["kind"], "account": e["account_id"],
               "effective_at": e["effective_at"],
               "source": [e["source_id"], e["source_record_id"]],
               "money": sorted([a, c, w, i or ""] for a, c, w, i in e["money"]),
               "units": sorted(e["units"]), "fee": e["terms"].get("fee"),
               "reverses": e["reverses"], "supersedes": e["supersedes"],
               "lot_policy": e["lot_policy"]}  # fmt: skip
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.db
def test_an_unbalanced_event_is_refused_inside_the_call(app_conn: Conn) -> None:
    """0007's deferred balance check runs inside the function, so the caller's
    savepoint holds the refusal and its transaction stays usable."""
    tenant = tenant_of(app_conn)
    rows = journal_rows(str(tenant))
    store = di.journal_args({"chains": [], "events": rows})[0]
    assert [event_id(e) for e in store] == [r["id"] for r in rows]  # the oracle
    rows = rows[:1]  # the deposit, one line changed and its id recomputed
    rows[0]["postings"][0]["amount"] = "1001"
    rows[0]["id"] = event_id(di.journal_args({"chains": [], "events": rows})[0][0])
    args = di.journal_args({"chains": lw.chains(str(tenant), rows), "events": rows})
    with tn.tenant_transaction(app_conn, tenant) as tx:
        tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"import:{tenant}",))  # fmt: skip
        with pytest.raises(errors.CheckViolation), tx.conn.transaction():
            tx.conn.execute(CALL, (Jsonb(args[0]), Jsonb(args[1])))
        assert tx.conn.execute("SELECT count(*) FROM app.ledger_event").fetchone() == (
            0,
        )


@pytest.mark.db
def test_qw_worker_and_unknown_arguments_are_refused(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant = tenant_of(app_conn)
    store = di.journal_args(body(tenant))
    assert failing(worker_conn, tenant, store) == "42501"  # no EXECUTE
    hostile = "x'); DROP TABLE app.ledger_event; --"
    extra = [{**store[0][0], "tenant_id": str(uuid.uuid4())}, *store[0][1:]]
    injected = [{**store[0][0], "account_id": hostile}, *store[0][1:]]
    sourced = [{**store[0][0], "source_record_id": hostile}, *store[0][1:]]
    for args in ((extra, store[1]), (store[0], {"chains": store[1]}),
                 ("not json", store[1])):  # fmt: skip
        assert failing(app_conn, tenant, args) in ("QI003", "22P02")
    assert failing(app_conn, tenant, (injected, store[1])) == "23514"
    assert failing(app_conn, tenant, (sourced, store[1])) == "QI003"  # the id
    assert run(app_conn, tenant, store) == 5  # the table is still there


@pytest.mark.db
def test_the_import_functions_are_hardened(conn: Conn) -> None:
    from qw_adapters.migrations import migrate

    migrate(conn)
    rows = conn.execute(
        "SELECT p.proname, p.proowner::regrole::text, p.prosecdef, p.proconfig, "
        "array(SELECT a.grantee::regrole::text FROM aclexplode(coalesce(p.proacl, "
        "acldefault('f', p.proowner))) a WHERE a.privilege_type = 'EXECUTE' "
        "ORDER BY 1) FROM pg_proc p WHERE p.pronamespace = 'app'::regnamespace "
        "AND p.proname LIKE 'portability%%' ORDER BY 1"
    ).fetchall()
    assert rows == [
        ("portability_chain", "qw_migrate", False, PATH,
         ["qw_migrate"]),
        ("portability_event_id", "qw_migrate", False, PATH,
         ["qw_migrate"]),
        ("portability_import_journal", "qw_migrate", True,
         PATH,
         ["qw_app", "qw_migrate"]),
        ("portability_instant", "qw_migrate", False, PATH,
         ["qw_migrate"]),
        ("portability_json", "qw_migrate", False, PATH,
         ["qw_migrate"]),
    ]  # fmt: skip


@pytest.mark.db
def test_the_flag_alone_does_not_skip_the_stamp(app_conn: Conn) -> None:
    """A runtime session that sets the import flag itself still has its event
    stamped: only the definer (current_user qw_migrate) keeps given stamps."""
    tenant = tenant_of(app_conn)
    with tn.tenant_transaction(app_conn, tenant) as tx:
        tx.conn.execute("SELECT set_config('app.journal_import', 'on', true)")
        _journal(tx)
        row = tx.conn.execute("SELECT account_rev, recorded_at >= now() FROM "
                              "app.ledger_event").fetchone()  # fmt: skip
    assert row == (1, True)
    with (pytest.raises(errors.InsufficientPrivilege),  # no column grant either
          tn.tenant_transaction(app_conn, tenant) as tx):  # fmt: skip
        tx.conn.execute("INSERT INTO app.ledger_event (tenant_id, account_rev) "
                        "VALUES (%s, 7)", (tenant,))  # fmt: skip


@pytest.mark.db
def test_a_temporary_table_cannot_stand_in_for_the_catalogue(
    conn: Conn, app_conn: Conn
) -> None:
    """Even with TEMPORARY granted to qw_app (0001 revokes it), a temporary
    `pg_locks` cannot fake the import lock: 0060 names pg_catalog's relations and
    searches pg_temp last."""
    conn.execute(sql.SQL("GRANT TEMPORARY ON DATABASE {} TO qw_app").format(
        sql.Identifier(conn.info.dbname)))  # fmt: skip
    tenant = tenant_of(app_conn)
    args = di.journal_args(body(tenant))
    fake = sql.SQL(
        "CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT 'advisory'::text locktype, "
        "pg_backend_pid() pid, true granted, 'ExclusiveLock'::text mode, d.oid "
        "database, ((k >> 32) & 4294967295)::oid classid, (k & 4294967295)::oid "
        "objid, 1::smallint objsubid FROM pg_catalog.pg_database d, (SELECT "
        "hashtextextended({}, 0) k) x WHERE d.datname = current_database()"
    )
    with pytest.raises(psycopg.Error) as e, tn.tenant_transaction(app_conn, tenant):
        app_conn.execute(fake.format(sql.Identifier("pg_locks"),
                                     sql.Literal(f"import:{tenant}")))  # fmt: skip
        app_conn.execute("GRANT SELECT ON pg_temp.pg_locks TO PUBLIC")  # the definer
        app_conn.execute(CALL, (Jsonb(args[0]), Jsonb(args[1])))
    assert e.value.sqlstate == "QI002" and events(app_conn, tenant) == 0
