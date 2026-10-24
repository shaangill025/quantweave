"""Research dataset store (`0046_research_dataset.sql`; T032; owner
decisions 73 to 77) on PostgreSQL 16 with real, non-superuser logins: qw_ingest
writes, qw_worker and qw_assessor read, qw_app and qw_release hold nothing. The
superuser `conn` only observes, or plays a tampering operator where a test says so.

All data is SYNTHETIC: invented feeds, CIK, accessions and instruments; rights are
recorded through the real T021 registry store as the schema owner. Times are set
relative to the database clock, which the store requires."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import errors
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import dataset_store as dst
from qw_adapters import rights_store as rs
from qw_adapters import tenancy as tn
from qw_domain.filings import KnowledgeBasis
from qw_domain.instants import ensure_aware_utc
from qw_domain.research_data import (
    Change,
    EvidenceClass,
    PriceMode,
    ResearchDataset,
    Universe,
    Window,
    freeze_dataset,
)
from qw_domain.rights import (
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    RightsProfile,
    RightState,
    Use,
    UseScope,
)
from test_research_data import FEED, event, fact

pytestmark = pytest.mark.db
Conn = psycopg.Connection[TupleRow]
REGION, KEEP = "CA-ON", timedelta(days=365)
PROVIDER = Provider("provider-synth", "SYNTHETIC provider")


def clock(c: Conn) -> datetime:
    """Just before the database clock (rights knowledge times must be known)."""
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0]) - timedelta(seconds=30)


def profile(at: datetime, until: datetime) -> RightsProfile:
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")

    def grant(use: Use | None = None) -> Grant:
        return Grant(RightState.GRANTED, permit, until,
                     KEEP if use is Use.RETENTION else None)  # fmt: skip

    uses = {u: grant(u) for u in Use}
    return RightsProfile(uses, {UseScope.PERSONAL: grant()}, {REGION: grant()})


def qualified_feed(feed_id: str, at: datetime, until: datetime) -> FeedHistory:
    auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", at)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", at)
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")
    return (FeedHistory.new(feed_id, PROVIDER.provider_id)
            .revise("synthetic_feed", frozenset(Use), profile(at, until), at)
            .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), at)
            .transition(FeedStatus.QUALIFIED, "operator-synth", (work, permit),
                        at))  # fmt: skip


def grant_all(owner: Conn, tenant: uuid.UUID) -> datetime:
    """A SYNTHETIC qualified feed and entitlement, recorded as the schema owner."""
    at = clock(owner)
    until = at + timedelta(days=3650)
    rs.record_provider(owner, PROVIDER)
    rs.record_feed(owner, qualified_feed(FEED, at, until))
    ent = EntitlementHistory.new(str(tenant), FEED).revise(
        EntitlementSource.INSTALLATION_LICENSE, True, profile(at, until), None, at
    )
    with tn.tenant_transaction(owner, tenant) as tx:
        rs.record_entitlement(tx, ent)
    return at


@pytest.fixture
def owner(database_url: str, runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The schema owner's role: the only one that records rights (0013)."""
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("SET ROLE qw_migrate")
        yield c


TERMS = dst.DatasetTerms(UseScope.PERSONAL, REGION, timedelta(days=300))
COLS = ", ".join(dst._COLS)


@pytest.fixture
def login_in(
    conn: Conn, database_url: str, runtime_urls: dict[str, str]
) -> Iterator[Callable[[str], str]]:
    """Throwaway passwordless LOGIN roles in one group (trust auth, as in conftest),
    after `runtime_urls` migrated the database."""
    names: list[str] = []

    def make(group: str) -> str:
        names.append(name := f"qwtest_ds_{uuid.uuid4().hex[:12]}")
        conn.execute(pgsql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE {}").format(
            pgsql.Identifier(name), pgsql.Identifier(group)))  # fmt: skip
        return make_conninfo(database_url, user=name)

    yield make
    for name in names:
        conn.execute(pgsql.SQL("SELECT pg_terminate_backend(pid) FROM "
                               "pg_stat_activity WHERE usename = {}").format(
            pgsql.Literal(name)))  # fmt: skip
        conn.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(name)))


@pytest.fixture
def ing(login_in: Callable[[str], str]) -> Iterator[Conn]:
    """A real qw_ingest login, a declared runtime role (decision 83)."""
    with tn.runtime_connection(login_in("qw_ingest"), role="qw_ingest") as c:
        yield c


@pytest.fixture
def t(app_conn: Conn) -> uuid.UUID:
    with tn.provision_tenant(app_conn) as tx:
        return tx.tenant_id


@pytest.fixture
def granted(owner: Conn, t: uuid.UUID) -> datetime:
    return grant_all(owner, t)


def feed_hash(c: Conn, t: uuid.UUID, revision: int = -1) -> str:
    with tn.tenant_transaction(c, t) as tx:
        return rs.load(tx).feeds[FEED].revisions[revision].content_hash


def build(c: Conn, t: uuid.UUID, ds_id: str = "ds-synth-1", value: str = "100",
          received: timedelta = timedelta(days=9), fh: str | None = None,
          terms: dst.DatasetTerms = TERMS,
          ahead: timedelta = timedelta(0)) -> ResearchDataset:  # fmt: skip
    """Freeze through the domain with rights at the database clock (`ahead` shifts
    the freeze time past it, as a caller-chosen clock would)."""
    with tn.tenant_transaction(c, t) as tx:
        rights = dst.research_rights(tx, terms)
    rights = replace(rights, at=rights.at + ahead)
    now = rights.at
    pub = now - timedelta(days=10)
    f = replace(fact(value, "0009990002-26-000001", pub), received_at=now - received,
                feed_hash=fh or feed_hash(c, t))  # fmt: skip
    ev = event(1, Change.ADD, (now - timedelta(days=30)).date(), now - timedelta(2))
    return freeze_dataset(
        ds_id, rights, frozenset({FEED}), (f,), Universe("univ-synth", (ev,)),
        observed=Window(now - timedelta(days=400), now - timedelta(days=2)),
        as_of=now - timedelta(days=1), basis=KnowledgeBasis.PUBLICATION,
        price_mode=PriceMode.RAW_TRADABLE, evidence_class=EvidenceClass.SYNTHETIC,
    )  # fmt: skip


def record(c: Conn, t: uuid.UUID, ds: ResearchDataset,
           terms: dst.DatasetTerms = TERMS) -> bool:  # fmt: skip
    with tn.tenant_transaction(c, t) as tx:
        return dst.record(tx, ds, terms)


def load(c: Conn, t: uuid.UUID, ds: ResearchDataset) -> dst.StoredDataset | None:
    with tn.tenant_transaction(c, t) as tx:
        return dst.load(tx, ds.manifest)


def refused(fn: Callable[[], object]) -> dst.DatasetStoreError:
    with pytest.raises(dst.DatasetStoreError) as exc:
        fn()
    return exc.value


def code(fn: Callable[[], object]) -> str:
    return refused(fn).code


def rows(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM app.research_dataset").fetchone()
    assert row is not None
    return int(row[0])


def raw_insert(c: Conn, t: uuid.UUID, ds: ResearchDataset,
               terms: dst.DatasetTerms = TERMS, at: datetime | None = None,
               row_tenant: uuid.UUID | None = None) -> None:  # fmt: skip
    """Insert a row with correct hashes in tenant `t`'s context, bypassing the
    store's checks; `row_tenant` names another tenant in the row."""
    at = at or clock(c) + timedelta(seconds=29)
    m, tw, rt = ds.manifest, terms.to_wire(), row_tenant or t
    with c.transaction():  # a savepoint when the caller holds a transaction open
        c.execute("SELECT set_config('app.tenant_id', %s, true)", (str(t),))
        h = dst._hash(tn.TenantTx(c, t), m.dataset_id, m.content_hash, tw, at)
        c.execute(f"INSERT INTO app.research_dataset ({COLS}) VALUES "
                  "(%s, %s, %s, %s, %s, %s, %s)",
                  (rt, m.dataset_id, m.content_hash, Jsonb(ds.to_wire()), Jsonb(tw), at,
                   h))  # fmt: skip


# --- round trip, decision 74 and the database clock -----------------------------


def test_ingest_records_and_worker_and_assessor_load_at_the_database_clock(
    ing: Conn, worker_conn: Conn, assessor_conn: Conn, conn: Conn, t: uuid.UUID,
    granted: datetime,
) -> None:  # fmt: skip
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    for c in (worker_conn, assessor_conn):
        before = clock(c) + timedelta(seconds=30)
        got = load(c, t, ds)
        after = clock(c) + timedelta(seconds=30)
        assert got is not None and got.dataset == ds and got.terms == TERMS
        assert got.dataset.to_wire() == ds.to_wire()
        assert before <= got.rights.at <= after  # decision 77: time of use, DB clock
        assert (got.rights.scope, got.rights.jurisdiction, got.rights.retain_for) == (
            TERMS.scope, TERMS.jurisdiction, TERMS.retain_for)  # fmt: skip
        assert got.rights.tenant_id == str(t)
    other = replace(ds.manifest, dataset_id="ds-synth-none")
    with tn.tenant_transaction(worker_conn, t) as tx:
        assert dst.load(tx, other) is None  # a missing row is None
    assert rows(conn) == 1


def test_decision_74_one_content_per_id(
    ing: Conn, worker_conn: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    assert record(ing, t, ds) is False  # the identical re-freeze: a no-op
    assert code(lambda: record(ing, t, build(ing, t, value="101"))) == "dataset_exists"
    longer = replace(TERMS, retain_for=timedelta(days=301))
    assert code(lambda: record(ing, t, ds, longer)) == "dataset_exists"  # terms too
    assert rows(conn) == 1
    got = load(worker_conn, t, ds)
    assert got is not None and got.dataset == ds and got.terms == TERMS
    assert record(ing, t, build(ing, t, "ds-synth-2", "101")) is True  # a new id


def test_insert_outside_the_database_clock_window_is_refused(
    ing: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    for at in (clock(ing) - timedelta(minutes=2), clock(ing) + timedelta(minutes=2)):
        with pytest.raises(errors.CheckViolation, match="database clock"):
            raw_insert(ing, t, ds, at=at)


# --- rights, feed revisions, retention (decisions 75 and 77) ----------------------


def test_record_checks_research_rights_at_freeze(
    ing: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    """The record re-runs the domain check at the database clock under the terms
    it stores; terms the registry does not grant refuse it with every reason."""
    ds = build(ing, t)
    elsewhere = replace(TERMS, jurisdiction="US-NY")
    err = refused(lambda: record(ing, t, ds, elsewhere))
    assert err.code == "rights_refused"
    assert {(f, r.code.value, r.subject) for f, r in err.reasons} == {
        (FEED, "right_unknown", f"{layer}:jurisdiction:US-NY")
        for layer in ("feed", "entitlement")}  # fmt: skip
    too_long = replace(TERMS, retain_for=KEEP + timedelta(days=1))
    err = refused(lambda: record(ing, t, ds, too_long))
    assert {r.code.value for _, r in err.reasons} == {"retention_exceeded"}
    assert rows(conn) == 0


def test_each_fact_feed_hash_must_be_a_recorded_revision(
    ing: Conn, owner: Conn, worker_conn: Conn, t: uuid.UUID,
    granted: datetime,
) -> None:  # fmt: skip
    bad = build(ing, t, fh="e" * 64)
    assert code(lambda: record(ing, t, bad)) == "feed_revision_unknown"
    first = feed_hash(ing, t)
    at = clock(owner)  # the feed is revised and re-qualified after ingestion
    until = granted + timedelta(days=3650)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload-2", at)
    permit = Evidence(
        EvidenceKind.PERMISSION, "synthetic-permit-2", at, "counsel-synth"
    )
    revised = (
        qualified_feed(FEED, granted, until)
        .revise("synthetic_feed_v2", frozenset(Use), profile(granted, until), at)
        .transition(FeedStatus.QUALIFIED, "operator-synth", (work, permit), at)
    )
    rs.record_feed(owner, revised)
    assert feed_hash(ing, t) != first
    ds = build(ing, t, fh=first)  # an older recorded revision: accepted
    assert ds.manifest.sources[FEED] != first and record(ing, t, ds) is True
    got = load(worker_conn, t, ds)
    assert got is not None and got.dataset == ds


def test_revoked_rights_refuse_a_stored_dataset_at_load(
    ing: Conn, owner: Conn, worker_conn: Conn, assessor_conn: Conn,
    t: uuid.UUID, granted: datetime,
) -> None:  # fmt: skip
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    at = clock(owner)
    until = granted + timedelta(days=3650)
    retire = Evidence(EvidenceKind.AUTH, "synthetic-retirement", at)
    rs.record_feed(owner, qualified_feed(FEED, granted, until).transition(
        FeedStatus.RETIRED, "operator-synth", (retire,), at))  # fmt: skip
    for c in (worker_conn, assessor_conn):  # decision 75: even a pending re-run
        err = refused(lambda c=c: load(c, t, ds))  # type: ignore[misc]
        assert err.code == "rights_refused"
        assert {r.code.value for _, r in err.reasons} == {"feed_retired"}


def test_expired_retention_refuses_record_and_load(
    ing: Conn, worker_conn: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    short = replace(TERMS, retain_for=timedelta(days=9))  # held since receipt 9d ago
    ds = build(ing, t, received=timedelta(days=9, seconds=1), terms=short)
    assert code(lambda: record(ing, t, ds, short)) == "retention_expired"
    assert rows(conn) == 0
    raw_insert(conn, t, ds, short)  # stored anyway (a tampering operator)
    assert code(lambda: load(worker_conn, t, ds)) == "retention_expired"
    fresh = build(ing, t, "ds-synth-2", received=timedelta(days=8), terms=short)
    assert record(ing, t, fresh, short) is True  # 8 days held: not yet expired


def test_retention_counts_from_the_earliest_holding() -> None:
    now = datetime(2026, 10, 23, tzinfo=UTC)
    from test_research_data import freeze

    ds = freeze()
    f = replace(fact("1", "0009990002-26-000001", datetime(2025, 1, 1, tzinfo=UTC)),
                received_at=datetime(2025, 1, 2, tzinfo=UTC))  # fmt: skip
    held = freeze(facts=(f,))
    week = replace(TERMS, retain_for=timedelta(days=7))
    assert dst._retention_end(ds, week) == ds.manifest.frozen_at + timedelta(days=7)
    assert dst._retention_end(held, week) == datetime(2025, 1, 9, tzinfo=UTC) < now
    known = datetime(2024, 6, 1, tzinfo=UTC)  # a universe event known even earlier
    both = freeze(facts=(f,), events=(event(1, Change.ADD, known.date(), known),))
    assert dst._retention_end(both, week) == known + timedelta(days=7)
    far = datetime(9990, 1, 1, tzinfo=UTC)  # a stub: only the anchor's times matter
    late = SimpleNamespace(manifest=SimpleNamespace(frozen_at=far),
                           facts=SimpleNamespace(facts=()),
                           universe=SimpleNamespace(events=()))  # fmt: skip
    century = replace(TERMS, retain_for=dst.MAX_RETAIN)
    assert code(lambda: dst._retention_end(late, century)) == "invalid"  # type: ignore[arg-type]


def test_the_store_itself_refuses_undeclared_feeds() -> None:
    """Defence in depth past the domain (a stub, as `ResearchDataset` refuses it)."""
    rec = SimpleNamespace(feed_id="feed-synth-b", feed_hash="f" * 64)
    for facts, events in (((rec,), ()), ((), (rec,))):
        stub = SimpleNamespace(manifest=SimpleNamespace(sources={FEED: "f" * 64},
                               dataset_id="ds-x"), facts=SimpleNamespace(facts=facts),
                               universe=SimpleNamespace(events=events))  # fmt: skip
        assert code(lambda s=stub: dst._usable(s, TERMS, None)) == "undeclared_source"  # type: ignore[misc,arg-type]


def test_retention_is_capped_at_one_hundred_years(
    ing: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    assert timedelta(days=36525) == dst.MAX_RETAIN
    assert replace(TERMS, retain_for=dst.MAX_RETAIN).retain_for == dst.MAX_RETAIN
    with pytest.raises(dst.DatasetStoreError, match="invalid"):
        replace(TERMS, retain_for=dst.MAX_RETAIN + timedelta(microseconds=1))
    over = TERMS.to_wire() | {"retain_for_us": dst.MAX_RETAIN // dst._US + 1}
    with pytest.raises(dst.DatasetStoreError, match="integrity"):
        dst.DatasetTerms.from_wire(over)
    ds = build(ing, t)
    with conn.transaction():  # the SQL CHECK holds the same bound
        conn.execute("SET LOCAL session_replication_role = replica")
        with pytest.raises(errors.CheckViolation, match="research_dataset_terms"):
            conn.execute(f"INSERT INTO app.research_dataset ({COLS}) VALUES "
                         "(%s, %s, %s, %s, %s, clock_timestamp(), repeat('a', 64))",
                         (t, ds.manifest.dataset_id, ds.manifest.content_hash,
                          Jsonb(ds.to_wire()), Jsonb(over)))  # fmt: skip


def test_tenant_mismatch_and_bad_terms_are_refused(
    ing: Conn, app_conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    assert code(lambda: record(ing, b, ds)) == "tenant_mismatch"
    future = build(ing, t, "ds-synth-2", ahead=timedelta(hours=1))
    assert code(lambda: record(ing, t, future)) == "invalid"  # frozen after the clock
    for bad in ({"scope": UseScope.PERSONAL, "jurisdiction": "", "retain_for": KEEP},
                {"scope": "personal", "jurisdiction": REGION, "retain_for": KEEP},
                {"scope": UseScope.PERSONAL, "jurisdiction": REGION,
                 "retain_for": timedelta(0)}):  # fmt: skip
        with pytest.raises(dst.DatasetStoreError, match="invalid"):
            dst.DatasetTerms(**bad)  # type: ignore[arg-type]
    w = TERMS.to_wire()
    assert w == {"scope": "personal", "jurisdiction": REGION,
                 "retain_for_us": 300 * 86_400_000_000}  # fmt: skip
    for bad_w in (w | {"x": 1}, w | {"retain_for_us": True},
                  w | {"retain_for_us": 1.5}, w | {"scope": "pooled"}):  # fmt: skip
        with pytest.raises(dst.DatasetStoreError, match="integrity"):
            dst.DatasetTerms.from_wire(bad_w)
    assert dst.DatasetTerms.from_wire(w) == TERMS


# --- tampering ------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        "body = jsonb_set(body, '{facts,0,value}', '\"101\"')",
        "body = jsonb_set(body, '{facts,0,instrument,status}', '\"unknown\"')",
        "body = jsonb_set(body, '{universe,events}', '[]')",
        "terms = jsonb_set(terms, '{retain_for_us}', '1000000')",
        "recorded_at = recorded_at - interval '1 second'",
        "event_hash = repeat('0', 64)",
        "body = jsonb_set(body, '{facts,0,fy}', '2023.0')",
    ],
)
def test_tampered_rows_are_integrity(
    ing: Conn, worker_conn: Conn, conn: Conn, t: uuid.UUID, granted: datetime,
    change: str,
) -> None:  # fmt: skip
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"UPDATE app.research_dataset SET {change}")
    assert code(lambda: load(worker_conn, t, ds)) == "integrity"


def test_the_plans_manifest_must_equal_the_stored_one(
    ing: Conn, worker_conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    for other in (replace(ds.manifest, evidence_class=EvidenceClass.HISTORICAL),
                  replace(ds.manifest, content_hash="0" * 64)):  # fmt: skip
        with tn.tenant_transaction(worker_conn, t) as tx:
            assert code(lambda m=other: dst.load(tx, m)) == "integrity"  # type: ignore[misc]


# --- roles, privileges, RLS, append-only (decision 73) -----------------------------

PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def privs(conn: Conn, role: str) -> set[str]:
    out = set()
    for p in PRIVS:
        row = conn.execute("SELECT has_table_privilege(%s, 'app.research_dataset', %s)",
                           (role, p)).fetchone()  # fmt: skip
        if row == (True,):
            out.add(p)
    return out


def column_privs(conn: Conn, role: str, priv: str) -> set[str]:
    got = conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'app.research_dataset'"
        "::regclass AND attnum > 0 AND NOT attisdropped AND "
        "has_column_privilege(%s, attrelid, attname, %s)", (role, priv),
    ).fetchall()  # fmt: skip
    return {r[0] for r in got}


def test_exact_privileges(conn: Conn, runtime_urls: dict[str, str]) -> None:
    every = set(dst._COLS)
    assert privs(conn, "qw_ingest") == set()  # INSERT by column only
    assert column_privs(conn, "qw_ingest", "INSERT") == every
    for p in ("SELECT", "UPDATE", "REFERENCES"):
        assert column_privs(conn, "qw_ingest", p) == set(), p
    for role in ("qw_worker", "qw_assessor"):
        assert privs(conn, role) == {"SELECT"}, role
        for p in ("INSERT", "UPDATE", "REFERENCES"):
            assert column_privs(conn, role, p) == set(), (role, p)
    for role in ("qw_app", "qw_release", "public"):
        assert privs(conn, role) == set(), role
        for p in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
            assert column_privs(conn, role, p) == set(), (role, p)
    for table in rs.TABLES:  # qw_ingest reads rights (decision 77), never writes
        for p in PRIVS:
            row = conn.execute("SELECT has_table_privilege('qw_ingest', %s, %s)",
                               (f"app.{table}", p)).fetchone()  # fmt: skip
            assert row == ((p == "SELECT"),), (table, p)
    row = conn.execute(
        "SELECT prosecdef, proconfig, proowner::regrole::text, "
        "has_function_privilege('public', oid, 'EXECUTE') FROM pg_proc "
        "WHERE proname = 'research_dataset_guard'").fetchone()  # fmt: skip
    assert row == (True, ["search_path=pg_catalog"], "qw_migrate", False)


def test_logins_without_the_grant_are_forbidden(
    ing: Conn, t: uuid.UUID, granted: datetime, runtime_urls: dict[str, str],
    login_in: Callable[[str], str],
) -> None:  # fmt: skip
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    assert code(lambda: load(ing, t, ds)) == "forbidden"  # ingest writes only
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_release", "qw_worker",
                                         "qw_assessor")  # fmt: skip
    for role in roles:
        with tn.runtime_connection(runtime_urls[role], role=role) as c:
            assert code(lambda c=c: record(c, t, build(ing, t, "ds-x"))) == "forbidden"  # type: ignore[misc]
            if role in ("qw_app", "qw_release"):
                assert code(lambda c=c: load(c, t, ds)) == "forbidden"  # type: ignore[misc]


def test_rls_hides_and_refuses_other_tenants(
    ing: Conn, worker_conn: Conn, app_conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    assert record(ing, t, ds) is True
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    with tn.tenant_transaction(worker_conn, b) as tx:
        assert tx.conn.execute("SELECT count(*) FROM app.research_dataset"
                               ).fetchone() == (0,)  # fmt: skip
        assert dst.load(tx, ds.manifest) is None
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        raw_insert(ing, b, ds, row_tenant=t)  # a row of `t` under `b`'s context


def test_append_only_even_for_the_owner(
    ing: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    assert record(ing, t, build(ing, t)) is True
    for stmt in ("UPDATE app.research_dataset SET dataset_id = 'x'",
                 "DELETE FROM app.research_dataset",
                 "TRUNCATE app.research_dataset"):  # fmt: skip
        with pytest.raises(errors.RaiseException, match="append-only"), \
                conn.transaction():  # fmt: skip
            conn.execute(stmt)
    assert rows(conn) == 1


# --- concurrency (decision 74 under racing freezes) ------------------------------


def blocked_on(conn: Conn, pid: int) -> str | None:
    """The lock type `pid` waits for, polled until it waits (or 30 s pass)."""
    for _ in range(300):
        row = conn.execute("SELECT locktype FROM pg_locks WHERE pid = %s AND NOT "
                           "granted", (pid,)).fetchone()  # fmt: skip
        if row is not None:
            return str(row[0])
        time.sleep(0.1)
    return None


def race(first: Callable[[], AbstractContextManager[None]],
         second: Callable[[], object], waiter: int,
         conn: Conn) -> tuple[str | None, object]:  # fmt: skip
    """Run `second` in a thread while `first`'s transaction is open; returns the
    lock `second` waited on and its outcome (a result or a store error code)."""
    out: list[object] = []

    def run() -> None:
        try:
            out.append(second())
        except dst.DatasetStoreError as exc:
            out.append(exc.code)

    worker = threading.Thread(target=run)
    with first():
        worker.start()
        lock = blocked_on(conn, waiter)
    worker.join(60)
    return lock, out[0] if out else "hung"


def test_racing_freezes_wait_on_the_per_id_lock(
    ing: Conn, login_in: Callable[[str], str], conn: Conn, t: uuid.UUID,
    granted: datetime,
) -> None:  # fmt: skip
    with tn.runtime_connection(login_in("qw_ingest"), role="qw_ingest") as other:
        for ds_id, value, expected in (("ds-synth-1", "101", "dataset_exists"),
                                       ("ds-synth-2", "100", False)):  # fmt: skip
            ds = build(ing, t, ds_id)
            rival = ds if expected is False else build(ing, t, ds_id, value)

            @contextmanager
            def held(ds: ResearchDataset = ds) -> Iterator[None]:
                with tn.tenant_transaction(ing, t) as tx:  # stored, uncommitted
                    assert dst.record(tx, ds, TERMS) is True
                    yield

            lock, got = race(held, lambda r=rival: record(other, t, r),  # type: ignore[misc]
                             other.info.backend_pid, conn)  # fmt: skip
            assert (lock, got) == ("advisory", expected), ds_id
    assert rows(conn) == 2


def test_a_freeze_raced_into_the_primary_key_rechecks_identity(
    ing: Conn, conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    """An identical row committed past the lock (an operator insert with triggers
    off) makes the store's insert hit the primary key; the retry lets the guard
    see it is identical, so the freeze is the no-op, not `dataset_exists`."""
    ds = build(ing, t)

    @contextmanager
    def operator() -> Iterator[None]:
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            raw_insert(conn, t, ds)
            yield

    with psycopg.connect(conn.info.dsn, autocommit=True) as observer:
        lock, got = race(operator, lambda: record(ing, t, ds),
                         ing.info.backend_pid, observer)  # fmt: skip
    assert (lock, got) == ("transactionid", False)
    assert rows(conn) == 1


def test_a_garbage_body_with_the_right_hash_blocks_nothing_silently(
    ing: Conn, conn: Conn, worker_conn: Conn, t: uuid.UUID, granted: datetime
) -> None:
    ds = build(ing, t)
    with conn.transaction():  # a garbage body under the right hash and terms
        conn.execute("SET LOCAL session_replication_role = replica")
        raw_insert(conn, t, ds)
        conn.execute("UPDATE app.research_dataset SET body = jsonb_set(body, "
                     "'{facts,0,value}', '\"999\"')")  # fmt: skip
    assert code(lambda: record(ing, t, ds)) == "dataset_exists"  # not a no-op
    assert code(lambda: load(worker_conn, t, ds)) == "integrity"
