"""The decision 112 accounts import function (`0062`,
`app.portability_import_accounts`) called directly on PostgreSQL 16, as the qw_app
login, outside `destination_import`'s own checks: the lock, emptiness, content
hash, seal, both chains, the lifecycle and knowledge order must hold in SQL alone.
SYNTHETIC rows (`accounts_body`, sealed as the store seals them) in fresh tenants.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import account_store as st
from qw_adapters import tenancy as tn
from qw_domain import account_wire as aw
from qw_domain.instants import format_instant, parse_instant
from qw_domain.research_data import digest
from qw_domain.sources import MappedInterval, Source, SourceKind
from test_portability_apply import NAME, accounts_body  # SYNTHETIC rows

Conn = psycopg.Connection[TupleRow]
CALL = "SELECT app.portability_import_accounts(%s)"
TABLES = ("account", "account_revision", "data_source", "source_mapping_revision")


def tenant_of(conn: Conn) -> uuid.UUID:
    tenant = uuid.uuid4()
    with tn.tenant_transaction(conn, tenant) as tx:
        conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
        tn.add_membership(tx, tn.create_user(tx, "SYNTHETIC owner"),
                          tn.MembershipRole.TENANT_OWNER)  # fmt: skip
    return tenant


def body(tenant: uuid.UUID) -> dict[str, Any]:
    return accounts_body(str(tenant))


def lock(tx: tn.TenantTx, fn: str = "pg_advisory_xact_lock", bit: int = 0) -> None:
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0) # %s)",
                    (f"import:{tx.tenant_id}", bit))  # fmt: skip


def run(conn: Conn, tenant: uuid.UUID, section: Any,
        before: Callable[[tn.TenantTx], None] = lock) -> int:  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        before(tx)
        row = conn.execute(CALL, (Jsonb(section),)).fetchone()
    assert row is not None
    return int(row[0])


def rows(conn: Conn, tenant: uuid.UUID) -> int:
    with tn.tenant_transaction(conn, tenant) as tx:
        counts = " + ".join(f"(SELECT count(*) FROM app.{t})" for t in TABLES)
        row = tx.conn.execute(f"SELECT {counts}").fetchone()
    assert row is not None
    return int(row[0])


def _register(tx: tn.TenantTx) -> None:  # SYNTHETIC, through the store
    a = st.account_from_text(tx, "SYN-X", "SYN", "cash", "USD")
    st.register_account(tx, a, "SYNTHETIC", "hypothetical")


def failing(conn: Conn, tenant: uuid.UUID, section: Any, **kw: Any) -> str | None:
    with pytest.raises(psycopg.Error) as e:
        run(conn, tenant, section, **kw)
    assert rows(conn, tenant) == 0
    return e.value.sqlstate


def _reseal(r: dict[str, Any], content: str) -> None:
    r["content_hash"] = content
    r["row_hash"] = aw.seal(content, parse_instant(r["recorded_at"]))


@pytest.mark.db
def test_the_stamps_are_kept_and_the_store_re_exports_the_section(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    section = body(tenant)
    assert run(app_conn, tenant, section) == 10
    with tn.tenant_transaction(app_conn, tenant, snapshot=True) as tx:
        assert st.export_accounts(tx) == section  # every stamp, hash and revision
    with tn.tenant_transaction(app_conn, tenant) as tx:  # the store appends after
        reg = st.load(tx)
        assert reg.accounts["SYN-A"].name == NAME  # escapes and non-ASCII kept
        got, wrote = st.archive_account(tx, "SYN-A", 1)
        assert wrote and got.revision == 2
        assert got.recorded_at == parse_instant(section["accounts"][0]["recorded_at"])


@pytest.mark.db
def test_without_the_import_lock_or_into_a_non_empty_tenant_is_refused(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    section = body(tenant)
    assert failing(app_conn, tenant, section, before=lambda tx: None) == "QI002"

    def other(tx: tn.TenantTx) -> None:  # another tenant's import lock
        tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"import:{uuid.uuid4()}",))  # fmt: skip

    assert failing(app_conn, tenant, section, before=other) == "QI002"
    for fn, bit in (("pg_advisory_xact_lock", 1), ("pg_advisory_xact_lock", 1 << 32),
                    ("pg_advisory_xact_lock_shared", 0)):  # fmt: skip
        near = partial(lock, fn=fn, bit=bit)
        assert failing(app_conn, tenant, section, before=near) == "QI002"
    for table in ("account", "data_source"):  # any of the four tables occupied
        t = tenant_of(app_conn)
        with tn.tenant_transaction(app_conn, t) as tx:
            if table == "account":
                _register(tx)
            else:
                st.add_source(tx, Source("SYN-X", str(t), SourceKind.MANUAL),
                              "syn")  # fmt: skip
        with pytest.raises(psycopg.Error) as e:
            run(app_conn, t, body(t))
        assert e.value.sqlstate == "QI001" and rows(app_conn, t) == 1


def _edit(table: str, n: int, **cols: Any) -> Callable[[dict[str, Any]], None]:
    def edit(b: dict[str, Any]) -> None:
        b[table][n].update(cols)

    return edit


def _content(b: dict[str, Any]) -> None:  # a column changed, the hashes kept
    b["accounts"][1]["name"] = "SYNTHETIC tampered"


def _resealed_chain(b: dict[str, Any]) -> None:  # a valid seal, a broken chain
    r = b["mappings"][2]  # SYN-BRK 1002 revision 2
    r["previous_hash"] = "0" * 64
    t = r["tenant_id"]
    ivs = tuple(MappedInterval(i["account_id"], parse_instant(i["valid_from"]),
                                  None if i["valid_to"] is None else
                                  parse_instant(i["valid_to"]))
                for i in r["intervals"])  # fmt: skip
    _reseal(r, aw.revision_content(t, "SYN-BRK", "1002", 2, ivs, "0" * 64))


def _unarchived(b: dict[str, Any]) -> None:  # a second lifecycle revision
    first = b["account_revisions"][0]
    r = {**first, "revision": 3, "previous_hash": first["row_hash"],
         "recorded_at": "2026-01-01T00:01:00Z"}  # fmt: skip
    _reseal(r, aw.lifecycle_content(r["tenant_id"], "SYN-OLD", 3, "archived",
                                    first["row_hash"]))  # fmt: skip
    b["account_revisions"].append(r)


def _backdated(b: dict[str, Any]) -> None:  # the archive before the account
    r = b["account_revisions"][0]
    r["recorded_at"] = "2025-12-31T00:00:00Z"
    _reseal(r, r["content_hash"])


@pytest.mark.db
def test_a_tampered_hash_seal_chain_or_stamp_is_refused_with_no_rows(
    app_conn: Conn,
) -> None:
    tenant = tenant_of(app_conn)
    good = body(tenant)
    for edit in (_content, _edit("accounts", 0, content_hash="0" * 64),
                 _edit("sources", 0, row_hash="0" * 64),
                 _edit("mappings", 2, recorded_at="2026-01-01T00:00:07.000251Z"),
                 _edit("accounts", 0, tenant_id=str(uuid.uuid4()))):  # fmt: skip
        bad = copy.deepcopy(good)
        edit(bad)
        assert failing(app_conn, tenant, bad) == "QI003"
    assert failing(app_conn, tenant, body(uuid.uuid4())) == "QI003"  # its tenant
    for edit in (_resealed_chain, _unarchived, _backdated):  # the 0056 guard
        bad = copy.deepcopy(good)
        edit(bad)
        assert failing(app_conn, tenant, bad) == "23514"
    gap = copy.deepcopy(good)
    del gap["mappings"][1]  # SYN-BRK 1002 revision 1: revision 2 has no parent
    assert failing(app_conn, tenant, gap) == "23514"
    assert run(app_conn, tenant, good) == 10  # the untampered rows still apply


@pytest.mark.db
def test_a_non_object_row_is_a_shape_refusal(app_conn: Conn) -> None:
    """A scalar, null or array element is QI003 (not a 22023 from an object
    operator), and so is every other element type, before any insert."""
    tenant = tenant_of(app_conn)
    good = body(tenant)
    for element in (5, "SYN", None, True, [], [good["accounts"][0]]):
        for table in ("accounts", "account_revisions"):
            bad = copy.deepcopy(good)
            bad[table].append(element)
            assert failing(app_conn, tenant, bad) == "QI003", (table, element)
    assert run(app_conn, tenant, good) == 10


def _stamped(b: dict[str, Any], at: datetime) -> None:  # the archive, re-sealed
    r = b["account_revisions"][0]  # SYN-OLD's archive: no row depends on it
    r["recorded_at"] = format_instant(at)
    _reseal(r, r["content_hash"])


@pytest.mark.db
def test_a_stamp_beyond_the_clock_is_refused(app_conn: Conn) -> None:
    """A recorded_at more than five minutes past the database clock is QI003, so an
    archive cannot set the tenant's knowledge floor in the future."""
    tenant = tenant_of(app_conn)
    now = datetime.now(UTC).replace(microsecond=0)
    for at in (now + timedelta(minutes=10), datetime(2999, 1, 1, tzinfo=UTC)):
        bad = copy.deepcopy(body(tenant))
        _stamped(bad, at)
        assert failing(app_conn, tenant, bad) == "QI003", at
    near = copy.deepcopy(body(tenant))
    _stamped(near, now + timedelta(minutes=1))  # within the allowed skew
    assert run(app_conn, tenant, near) == 10


@pytest.mark.db
def test_qw_worker_unknown_shapes_and_injection_are_refused(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant = tenant_of(app_conn)
    good = body(tenant)
    assert failing(worker_conn, tenant, good) == "42501"  # no EXECUTE
    hostile = "x'); DROP TABLE app.account; --"
    shapes: list[Any] = [[], "not an object", {**good, "status": []},
                         {k: v for k, v in good.items() if k != "sources"},
                         {**good, "accounts": {}}]  # fmt: skip
    for edit in (_edit("accounts", 0, status="active"), _edit("accounts", 0, name=5),
                 _edit("mappings", 0, revision="1"),
                 _edit("accounts", 0, recorded_at="2026-01-01T00:00:00.000000Z")
                 ):  # fmt: skip
        bad = copy.deepcopy(good)
        edit(bad)
        shapes.append(bad)
    numeric = copy.deepcopy(good)  # name 5, sealed as if it were the text "5"
    r = numeric["accounts"][1]
    r["name"] = 5
    _reseal(r, digest(["account", str(tenant), r["account_id"], "5", r["broker"],
                       r["account_type"], r["reporting_currency"],
                       r["analysis_scope"]]))  # fmt: skip
    shapes.append(numeric)
    for section in shapes:
        assert failing(app_conn, tenant, section) == "QI003"
    injected = copy.deepcopy(good)  # resealed, so only the id CHECK can refuse it
    r = injected["sources"][0]
    r["source_id"] = hostile
    _reseal(r, _source_digest(str(tenant), hostile, r))
    assert failing(app_conn, tenant, injected) == "23514"
    assert run(app_conn, tenant, good) == 10  # the tables are still there


def _source_digest(t: str, sid: str, r: dict[str, Any]) -> str:  # no domain check
    return digest(["data_source", t, sid, r["kind"], r["provider"]])


@pytest.mark.db
def test_the_flag_alone_does_not_skip_the_stamp(app_conn: Conn) -> None:
    """A runtime session that sets the import flag itself is still stamped: only
    the definer (current_user qw_migrate) keeps given stamps."""
    tenant = tenant_of(app_conn)
    with tn.tenant_transaction(app_conn, tenant) as tx:
        tx.conn.execute("SELECT set_config('app.accounts_import', 'on', true)")
        _register(tx)
        row = tx.conn.execute("SELECT recorded_at >= now() FROM app.account"
                              ).fetchone()  # fmt: skip
    assert row == (True,)
    with (pytest.raises(errors.InsufficientPrivilege),  # no column grant either
          tn.tenant_transaction(app_conn, tenant) as tx):  # fmt: skip
        tx.conn.execute("INSERT INTO app.data_source (tenant_id, recorded_at) "
                        "VALUES (%s, now())", (tenant,))  # fmt: skip


@pytest.mark.db
def test_a_direct_insert_waits_for_the_import(
    app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """The guard itself shares the import lock (not only `account_store._lock`): a
    qw_app insert outside the store, on another connection, waits for an import."""
    tenant = tenant_of(app_conn)
    with (psycopg.connect(runtime_urls["qw_app"], autocommit=True) as other,
          tn.tenant_transaction(app_conn, tenant) as tx):  # fmt: skip
        lock(tx)  # the import lock only: no source_map lock is held yet
        with (pytest.raises(errors.LockNotAvailable),
              tn.tenant_transaction(other, tenant)):  # fmt: skip
            other.execute("SET LOCAL lock_timeout = '300ms'")
            other.execute("INSERT INTO app.data_source (tenant_id, source_id, kind, "
                          "provider, content_hash) VALUES (%s, 'SYN-X', 'manual', "
                          "'syn', %s)", (tenant, "0" * 64))  # fmt: skip
        assert tx.conn.execute(CALL, (Jsonb(body(tenant)),)).fetchone() == (10,)
    assert rows(app_conn, tenant) == 10
