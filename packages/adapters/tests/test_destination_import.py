"""The destination apply (T057 increment 6, `qw_adapters.destination_import`) on
PostgreSQL 16. A SYNTHETIC tenant is exported from the test database as the qw_app
login (conftest) and applied, as the same non-superuser login, to a second migrated
database standing for the other installation, where the destination tenant has the
source's id. The oracle is the source's own export: the destination must export the
same sections. The superuser only creates and migrates the second database.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from qw_adapters import account_store as st
from qw_adapters import destination_import as di
from qw_adapters import import_store as ims
from qw_adapters import journal_store as js
from qw_adapters import policy_store as ps
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as tb
from qw_adapters import watchlist_store as ws
from qw_adapters.export_archive import write_archive
from qw_adapters.migrations import migrate
from qw_domain import csv_import as ci
from qw_domain import portability as pt
from qw_domain import retention as rt
from qw_domain import sources as src
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money
from qw_domain.identity import InstrumentId, Listing, Mic, SecurityMaster, Ticker
from qw_domain.postings import Header, deposit
from qw_domain.simulation import VirtualPortfolio
from test_portability_tenant_sections import draft  # SYNTHETIC answers

Conn = psycopg.Connection[TupleRow]
R = pt.Refusal
J, P, W, A, T = (pt.JOURNAL_PATH, pt.POLICIES_PATH, pt.WATCHLISTS_PATH,
                 pt.ACCOUNTS_PATH, pt.TOMBSTONES_PATH)  # fmt: skip
ACK = "SYNTHETIC: I adopt this"
KEY = rt.RecordKey(b"SYNTHETIC-test-record-key-0123456789")  # test only
OTHER = rt.RecordKey(b"SYNTHETIC-another-installation-key-0123")
DAY = timedelta(days=1)
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
CSV = (FIXTURES / "canonical_transactions.csv").read_bytes()  # SYNTHETIC


@pytest.fixture
def dest_urls(
    pg_admin_url: str, runtime_urls: dict[str, str]
) -> Iterator[dict[str, str]]:
    """The other installation: a second database, migrated by the superuser, reached
    by the same runtime logins."""
    name = f"qw_dest_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'"
            ).format(sql.Identifier(name))
        )
    try:
        with psycopg.connect(make_conninfo(pg_admin_url, dbname=name),
                             autocommit=True) as su:  # fmt: skip
            migrate(su)
        yield {g: make_conninfo(u, dbname=name) for g, u in runtime_urls.items()}
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                sql.Identifier(name)))  # fmt: skip


@pytest.fixture
def dest(dest_urls: dict[str, str]) -> Iterator[Conn]:
    with psycopg.connect(dest_urls["qw_app"], autocommit=True) as c:
        yield c


@pytest.fixture
def dest_worker(dest_urls: dict[str, str]) -> Iterator[Conn]:
    with psycopg.connect(dest_urls["qw_worker"], autocommit=True) as c:
        yield c


def owner_of(conn: Conn, tenant: uuid.UUID,
             role: tn.MembershipRole = tn.MembershipRole.TENANT_OWNER,
             new: bool = True) -> tuple[uuid.UUID, uuid.UUID]:  # fmt: skip
    """A tenant with this id (when `new`) and a member: (user, session)."""
    with tn.tenant_transaction(conn, tenant) as tx:
        if new:
            conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
        user = tn.create_user(tx, "SYNTHETIC user")
        tn.add_membership(tx, user, role)
        session, _ = tn.create_session(tx, user, timedelta(hours=1))
        tn.mark_step_up(tx, session)
    return user, session


def seed(app_conn: Conn) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Policy a: v1 adopted, then v2; policy b: v1 (SYNTHETIC). Nothing else."""
    tenant = uuid.uuid4()
    owner, session = owner_of(app_conn, tenant)
    a, b = sorted((uuid.uuid4(), uuid.uuid4()))
    with tn.tenant_transaction(app_conn, tenant) as tx:
        first = ps.propose(tx, a, draft(tenant=str(tenant)), owner).versions[0]
        ps.adopt(
            tx,
            a,
            1,
            first.content_hash,
            principal=owner,
            session_id=session,
            window=timedelta(minutes=10),
            acknowledged=frozenset(),
            text=ACK,
        )
        ps.propose(tx, a, draft("income", str(tenant)), owner)
        ps.propose(tx, b, draft("income", str(tenant)), owner)
    return tenant, a, b


def export(conn: Conn, tenant: uuid.UUID) -> dict[str, Any]:
    with tn.tenant_transaction(conn, tenant, snapshot=True) as tx:
        return {J: js.export_journal(tx), P: ps.export_policies(tx),
                W: ws.export_watchlists(tx), A: st.export_accounts(tx),
                T: tb.export_tombstones(tx)}  # fmt: skip


def archive(conn: Conn, tenant: uuid.UUID, **edit: object) -> bytes:
    at = conn.execute("SELECT clock_timestamp()").fetchone()
    assert at is not None
    sections = {**export(conn, tenant), **edit}
    return write_archive(pt.build_export("export-synth-1", str(tenant), at[0],
                                         sections))  # fmt: skip


def state(conn: Conn, tenant: uuid.UUID) -> tuple[int, ...]:
    """What an apply could have written: policy versions, adoptions, journal
    events and import audit events."""
    with tn.tenant_transaction(conn, tenant) as tx:
        row = tx.conn.execute(
            "SELECT (SELECT count(*) FROM app.policy_version), (SELECT count(*) FROM "
            "app.policy_adoption), (SELECT count(*) FROM app.ledger_event), "
            "(SELECT count(*) FROM app.audit_event WHERE action = %s)",
            (di.AUDIT_ACTION,),
        ).fetchone()
    assert row is not None
    return tuple(int(n) for n in row)


def apply(conn: Conn, tenant: uuid.UUID, data: bytes, who: uuid.UUID,
          key: rt.RecordKey | None = None) -> di.Applied:  # fmt: skip
    with tn.tenant_transaction(conn, tenant) as tx:
        return di.apply_archive(tx, data, who, key)


def refused(conn: Conn, tenant: uuid.UUID, data: bytes, who: uuid.UUID,
            key: rt.RecordKey | None = None) -> pt.Refusal:  # fmt: skip
    before = state(conn, tenant)
    with pytest.raises(pt.PortabilityError) as e:
        apply(conn, tenant, data, who, key)
    assert state(conn, tenant) == before  # nothing written
    return e.value.code


@pytest.mark.db
def test_an_empty_destination_receives_the_same_state(app_conn: Conn,
                                                      dest: Conn) -> None:  # fmt: skip
    tenant, a, _ = seed(app_conn)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    got = apply(dest, tenant, data, who)
    assert (got.export_id, got.scope_id, got.policy_versions) == (
        "export-synth-1",
        str(tenant),
        3,
    )
    assert (
        got.sections == tuple(sorted((A, J, P, W, T)))
        and got.required_reconnections == ()
    )
    assert got.omissions == pt.ALWAYS_OMITTED
    assert export(dest, tenant) == export(app_conn, tenant)  # ids, hashes, versions
    with tn.tenant_transaction(dest, tenant) as tx:
        history = ps.load(tx, a)
        by = {r[0] for r in tx.conn.execute("SELECT created_by FROM "
                                            "app.policy_version")}  # fmt: skip
    assert history is not None and history.receipts == ()  # re-adopt at the destination
    assert by == {who}  # the importing owner, not the source's principal
    assert state(dest, tenant) == (3, 0, 0, 1)
    assert refused(dest, tenant, data, who) is R.DESTINATION_NOT_EMPTY  # only once


def _policy(tx: tn.TenantTx, who: uuid.UUID, _: uuid.UUID) -> None:
    ps.propose(tx, uuid.uuid4(), draft(tenant=str(tx.tenant_id)), who)


def _journal(tx: tn.TenantTx, *_: uuid.UUID) -> None:
    h = Header("SYN-ACCT", tx.conn.execute("SELECT now()").fetchone()[0] - DAY,  # type: ignore[index]
               SourceRef("SYN-BROKER", "r0"))  # fmt: skip
    js.post(tx, deposit(h, Money.of("10", "USD")))


def _watchlist(tx: tn.TenantTx, who: uuid.UUID, session: uuid.UUID) -> None:
    ws.create(tx, who, session, "synthetic-key-dest-1", "SYNTHETIC", "SYNTHETIC")


def _account(tx: tn.TenantTx, *_: uuid.UUID) -> None:
    a = st.account_from_text(tx, "SYN-A", "Interactive Brokers", "margin", "USD")
    st.register_account(tx, a, "SYNTHETIC SYN-A", "complete_declared")


def _simulation(tx: tn.TenantTx, *_: uuid.UUID) -> None:
    now = tx.conn.execute("SELECT now()").fetchone()[0]  # type: ignore[index]
    sim = VirtualPortfolio.open("SIM-SYN", Money.of("500", "USD"), now)
    ss.save(tx, ss.Simulation("SYNTHETIC", "exec-synth-1", "bench-synth-1", sim))


def _preview(tx: tn.TenantTx, *_: uuid.UUID) -> None:
    t, start = str(tx.tenant_id), datetime(2025, 1, 1, tzinfo=UTC)
    smap = src.SourceMap()
    smap.add_source(src.Source("SYN-BROKER", t, src.SourceKind.BROKER_EXPORT))
    for acct, ref in (("acct-syn-1", "SYN-ACCOUNT"), ("acct-syn-2", "SYN-ACCOUNT2")):
        smap.add_account(src.UnderlyingAccount(acct, t, "SYN", "cash", "USD"))
        smap.remap("SYN-BROKER", ref, [src.MappedInterval(acct, start)], start)
    master = SecurityMaster()
    master.add_listing(Listing(SYN, Mic("XNYS"), Ticker("SYNTH"), date(2025, 1, 1)))
    req = ci.ImportRequest(t, ci.FileKind.TRANSACTIONS, ci.ColumnMapping.identity())
    ims.save_preview(tx, CSV, req, smap, master)


def _tombstone(worker: Conn, tenant: uuid.UUID) -> None:
    now = worker.execute("SELECT clock_timestamp()").fetchone()[0]  # type: ignore[index]
    item = rt.Item(str(tenant), "acct-synth-1", rt.RetentionClass.RESEARCH, now - DAY)
    req = rt.DeletionRequest(
        "del-synth-1",
        str(tenant),
        "scope-1",
        frozenset({rt.RetentionClass.RESEARCH}),
        now - timedelta(seconds=1),
        "reauth-1",
        "SYNTHETIC",
    )
    p = rt.plan(req, [item], rt.owner_defaults(), rt.Holds(), KEY)
    with tn.tenant_transaction(worker, tenant) as tx:
        assert tb.append(tx, KEY, p.decisions[0], p, ())[1]


@pytest.mark.db
@pytest.mark.parametrize("occupy", [_policy, _journal, _watchlist, _account,
                                    _simulation, _preview, "tombstone"])  # fmt: skip
def test_a_destination_with_any_state_is_refused(
    app_conn: Conn, dest: Conn, dest_worker: Conn, occupy: Callable[..., None] | str
) -> None:
    tenant, *_ = seed(app_conn)
    data = archive(app_conn, tenant)
    who, session = owner_of(dest, tenant)
    if occupy == "tombstone":
        _tombstone(dest_worker, tenant)
    else:
        with tn.tenant_transaction(dest, tenant) as tx:
            occupy(tx, who, session)  # type: ignore[operator]
    assert refused(dest, tenant, data, who) is R.DESTINATION_NOT_EMPTY


@pytest.mark.db
def test_every_tenant_table_is_checked_or_excluded_with_a_reason(
    conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """A new tenant table fails here until it is placed in one list (the superuser
    reads the catalogue of the migrated test database)."""
    tables = {r[0] for r in conn.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
        "ON n.oid = c.relnamespace JOIN pg_catalog.pg_attribute a ON a.attrelid = "
        "c.oid WHERE n.nspname = 'app' AND c.relkind IN ('r', 'p') AND a.attname = "
        "'tenant_id' AND NOT a.attisdropped")}  # fmt: skip
    lists = [set(di.EMPTY_TABLES), set(di.CHILDREN), set(di.EXCLUDED)]
    assert sum(len(x) for x in lists) == len(set().union(*lists))  # disjoint
    listed = set().union(*lists)
    assert (sorted(listed - tables), sorted(tables - listed)) == ([], [])
    assert all(why for why in di.EXCLUDED.values())
    for child, parent in di.CHILDREN.items():  # covered through a real key
        assert parent in di.EMPTY_TABLES or parent in di.CHILDREN
        assert conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint WHERE contype = "
            "'f' AND conrelid = %s::regclass AND confrelid = %s::regclass)",
            (f"app.{child}", f"app.{parent}"),
        ).fetchone() == (True,), child
    for table in di.EMPTY_TABLES:  # readable by the importing login, under RLS
        assert conn.execute(
            "SELECT has_table_privilege('qw_app', %s, 'SELECT'), relforcerowsecurity "
            "FROM pg_catalog.pg_class WHERE oid = %s::regclass",
            (f"app.{table}", f"app.{table}"),
        ).fetchone() == (True, True), table


@pytest.mark.db
def test_scope_principal_and_sections_are_checked_before_any_write(
    app_conn: Conn, dest: Conn
) -> None:
    tenant, *_ = seed(app_conn)
    data = archive(app_conn, tenant)
    other = uuid.uuid4()
    stranger, _ = owner_of(dest, other)
    assert refused(dest, other, data, stranger) is R.DESTINATION_SCOPE
    who, _ = owner_of(dest, tenant)
    member, _ = owner_of(dest, tenant, tn.MembershipRole.TENANT_MEMBER, new=False)
    assert refused(dest, tenant, data, member) is R.APPLY_PRINCIPAL
    assert refused(dest, tenant, data, stranger) is R.APPLY_PRINCIPAL  # not here
    with tn.tenant_transaction(app_conn, tenant) as tx:
        _journal(tx)
    journal = archive(app_conn, tenant)  # the policies travel with it
    assert refused(dest, tenant, journal, who) is R.SECTION_NOT_APPLICABLE


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in files.items():
            z.writestr(name, raw)
    return buf.getvalue()


@pytest.mark.db
def test_the_archive_is_verified_again_not_trusted(app_conn: Conn,
                                                   dest: Conn) -> None:  # fmt: skip
    tenant, *_ = seed(app_conn)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    edited = files[P].replace(b'"version":2', b'"version":3')
    assert edited != files[P]
    assert refused(dest, tenant, _zip({**files, P: edited}), who) is R.HASH_MISMATCH
    assert refused(dest, tenant, data[:-1], who) is R.ARCHIVE_MALFORMED
    with pytest.raises(TypeError):
        apply(dest, tenant, bytearray(data), who)  # type: ignore[arg-type]
    assert state(dest, tenant) == (0, 0, 0, 0)


def _reissue(data: bytes, path: str, body: Any) -> bytes:
    """An archive edit with the manifest recomputed over it (no signature stops
    that), so only the section's own re-verification can refuse it."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        files = {i.filename: z.read(i) for i in z.infolist()}
    m = pt.ExportManifest.from_wire(pt.parse_json(files.pop("manifest.json"), "m"))
    files[path] = pt.canonical(body)
    entries = tuple(pt.FileEntry(f.path, pt.sha256(files[f.path]),
                                 len(files[f.path]), f.content_class)
                    for f in m.files)  # fmt: skip
    m = pt.ExportManifest(m.id, m.format_version, m.created_at, m.scope_id, entries,
                          m.omissions, m.required_reconnections,
                          m.compatibility_version)  # fmt: skip
    return _zip({"manifest.json": pt.canonical(m.to_wire()), **dict(sorted(
        files.items()))})  # fmt: skip


@pytest.mark.db
def test_a_version_gap_or_a_wrong_hash_is_refused_with_no_rows(
    app_conn: Conn, dest: Conn
) -> None:
    tenant, a, b = seed(app_conn)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    rows = export(app_conn, tenant)[P]
    assert [(r["policy_id"], r["version"]) for r in rows] == [
        (str(a), 1), (str(a), 2), (str(b), 1)]  # fmt: skip
    gap = [{**r, "version": 3} if n == 1 else r for n, r in enumerate(rows)]
    wrong = [{**r, "content_hash": "0" * 64} if n == 2 else r
             for n, r in enumerate(rows)]  # fmt: skip
    for body in (gap, wrong, rows[1:]):  # v1, v3; a wrong hash; a missing v1
        assert refused(dest, tenant, _reissue(data, P, body), who) is R.MALFORMED
    assert state(dest, tenant) == (0, 0, 0, 0)
    assert apply(dest, tenant, _reissue(data, P, rows), who).policy_versions == 3


@pytest.mark.db
def test_an_archive_with_no_policies_applies_as_nothing(
    app_conn: Conn, dest: Conn
) -> None:
    """Chosen: an empty source tenant moves to an empty destination; only the
    import's audit event is written, so the move is still on record. Nothing was
    written, so the tenant stays empty and a repeat is a no-op with another audit
    event (not a refusal)."""
    tenant = uuid.uuid4()
    owner_of(app_conn, tenant)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    got = apply(dest, tenant, data, who)
    assert got.policy_versions == 0 and got.sections == tuple(sorted((A, J, P, W, T)))
    assert state(dest, tenant) == (0, 0, 0, 1)
    assert apply(dest, tenant, data, who).policy_versions == 0
    assert state(dest, tenant) == (0, 0, 0, 2)
    assert export(dest, tenant) == export(app_conn, tenant)


@pytest.mark.db
def test_a_failed_write_leaves_nothing(
    app_conn: Conn, dest: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant, *_ = seed(app_conn)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    real, calls = ps.propose, []

    def flaky(*args: Any) -> Any:
        calls.append(args)
        if len(calls) == 3:
            raise ps.PolicyStoreError("synthetic", "SYNTHETIC failure")
        return real(*args)

    monkeypatch.setattr(ps, "propose", flaky)
    with tn.tenant_transaction(dest, tenant) as tx:
        with pytest.raises(ps.PolicyStoreError):
            di.apply_archive(tx, data, who)
        # the caller's transaction is still usable, and holds none of the writes
        assert tx.conn.execute("SELECT count(*) FROM app.policy_version"
                               ).fetchone() == (0,)  # fmt: skip
    assert len(calls) == 3 and state(dest, tenant) == (0, 0, 0, 0)

    def lying(*args: Any) -> Any:  # a store that writes other content for b v1
        calls.append(args)
        tx, pid, d, by = args
        if len(calls) == 6:
            d = draft("growth", str(tx.tenant_id))
        return real(tx, pid, d, by)

    monkeypatch.setattr(ps, "propose", lying)
    assert refused(dest, tenant, data, who) is R.APPLY_VERIFY

    # a writer inside the window: another policy's version, or a journal event
    def version(tx: tn.TenantTx, by: uuid.UUID) -> None:  # not via the patch
        real(tx, uuid.uuid4(), draft(tenant=str(tx.tenant_id)), by)

    for writer, code in (
        (version, R.APPLY_VERIFY),
        (_journal, R.DESTINATION_NOT_EMPTY),
    ):

        def racing(*args: Any, writer: Callable[..., None] = writer) -> Any:
            out = real(*args)
            writer(args[0], args[3])
            return out

        monkeypatch.setattr(ps, "propose", racing)
        assert refused(dest, tenant, data, who) is code


@pytest.mark.db
def test_the_record_key_is_checked_before_anything(
    app_conn: Conn, worker_conn: Conn, dest: Conn
) -> None:
    tenant, *_ = seed(app_conn)
    _tombstone(worker_conn, tenant)
    data = archive(app_conn, tenant)
    who, _ = owner_of(dest, tenant)
    assert refused(dest, tenant, data, who) is R.KEY_UNAVAILABLE
    assert refused(dest, tenant, data, who, OTHER) is R.KEY_MISMATCH
    # with the right key, the ledger itself still needs an insert path (0057 stamps)
    assert refused(dest, tenant, data, who, KEY) is R.SECTION_NOT_APPLICABLE
