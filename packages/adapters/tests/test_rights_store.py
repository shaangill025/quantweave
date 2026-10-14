"""Rights registry store (0013_rights_registry, qw_adapters.rights_store) on
PostgreSQL 16. Reads run as the non-superuser qw_app/qw_worker logins; writes as
the schema owner qw_migrate (`owner`), the only role allowed to record rights. The
superuser `conn` only observes or plays a tampering owner where a test says so.
Every provider, feed, tenant and evidence id is SYNTHETIC: no real provider is
named, approved or qualified. Knowledge times sit just before the database clock,
which the store requires; expectations are hand-written deny codes."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import rights_store as rs
from qw_adapters import tenancy as tn
from qw_domain.rights import (
    DenyCode,
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsProfile,
    RightState,
    Use,
    UseScope,
    check_use,
)

Conn = psycopg.Connection[TupleRow]
RIGHTS_TABLES = rs.TABLES
KEY = {
    "rights_provider": "id",
    "rights_feed": "id",
    "rights_feed_revision": "feed_id",
    "rights_feed_transition": "feed_id",
    "rights_entitlement": "feed_id",
}
REGION, KEEP = "CA-ON", timedelta(days=365)
PROVIDER = Provider("provider-synth", "SYNTHETIC provider")


def clock(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now: datetime = row[0].astimezone(UTC)
    return now - timedelta(seconds=30)  # just known


def profile(at: datetime, until: datetime, missing: Use | None = None) -> RightsProfile:
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")

    def grant(use: Use | None = None) -> Grant:
        keep = KEEP if use is Use.RETENTION else None
        return Grant(RightState.GRANTED, permit, until, keep)

    uses = {u: grant(u) for u in Use if u is not missing}
    return RightsProfile(uses, {UseScope.PERSONAL: grant()}, {REGION: grant()})


def qualified_feed(feed_id: str, at: datetime, until: datetime) -> FeedHistory:
    auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", at)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", at)
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")
    return (
        FeedHistory.new(feed_id, PROVIDER.provider_id)
        .revise("synthetic_feed", frozenset(Use), profile(at, until), at)
        .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), at)
        .transition(FeedStatus.QUALIFIED, "operator-synth", (work, permit), at)
    )


def entitlement(
    tenant: str, feed_id: str, at: datetime, until: datetime, missing: Use | None = None
) -> EntitlementHistory:
    return EntitlementHistory.new(tenant, feed_id).revise(
        EntitlementSource.INSTALLATION_LICENSE,
        True,
        profile(at, until, missing),
        None,
        at,
    )


def grant_all(
    owner: Conn,
    tenant: uuid.UUID,
    feeds: tuple[str, ...] = ("feed-a",),
    missing: Use | None = None,
    until: datetime | None = None,
    at: datetime | None = None,
) -> datetime:
    """Record a SYNTHETIC qualified feed and entitlement; returns the stamp."""
    at = at or clock(owner)
    until = until or at + timedelta(days=3650)
    rs.record_provider(owner, PROVIDER)
    for f in feeds:
        rs.record_feed(owner, qualified_feed(f, at, until))
        with tn.tenant_transaction(owner, tenant) as tx:
            rs.record_entitlement(tx, entitlement(str(tenant), f, at, until, missing))
    return at


@pytest.fixture
def owner(database_url: str, runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The schema owner's role (operator-held; future admin tooling)."""
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("SET ROLE qw_migrate")
        yield c


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


def load(c: Conn, t: uuid.UUID) -> Registry:
    with tn.tenant_transaction(c, t) as tx:
        return rs.load(tx)


def display(
    reg: Registry, t: uuid.UUID, at: datetime, feed: str = "feed-a"
) -> set[str]:
    d = check_use(
        reg, str(t), feed, Use.DISPLAY, at, scope=UseScope.PERSONAL, jurisdiction=REGION
    )
    return {r.code.value for r in d.reasons}


@pytest.mark.db
def test_empty_store_is_an_empty_fail_closed_registry(app_conn: Conn) -> None:
    a = tenant(app_conn)
    reg = load(app_conn, a)
    assert reg == Registry()
    assert display(reg, a, clock(app_conn)) == {DenyCode.NOT_QUALIFIED.value}
    for table in rs.TABLES:  # the migration seeds nothing
        assert app_conn.execute(f"SELECT count(*) FROM app.{table}").fetchone() == (0,)


@pytest.mark.db
def test_round_trip_rebuilds_the_same_registry(
    app_conn: Conn, worker_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    at = grant_all(owner, a, ("feed-a", "feed-b"), missing=Use.REDISTRIBUTION)
    until = at + timedelta(days=3650)
    expected = Registry().with_provider(PROVIDER)
    for f in ("feed-a", "feed-b"):
        expected = expected.with_feed(qualified_feed(f, at, until)).with_entitlement(
            entitlement(str(a), f, at, until, Use.REDISTRIBUTION)
        )
    assert load(app_conn, a) == expected == load(worker_conn, a)
    assert display(load(app_conn, a), a, at + timedelta(seconds=1)) == set()
    # Recording the same records again adds nothing.
    grant_all(owner, a, ("feed-a", "feed-b"), Use.REDISTRIBUTION, until, at)
    assert load(app_conn, a) == expected
    got = check_use(
        load(app_conn, a),
        str(a),
        "feed-b",
        Use.REDISTRIBUTION,
        at,
        scope=UseScope.PERSONAL,
        jurisdiction=REGION,
    )
    assert [r.subject for r in got.reasons] == ["entitlement:use:redistribution"]


@pytest.mark.db
def test_entitlements_are_tenant_scoped(app_conn: Conn, owner: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    at = grant_all(owner, a)
    seen_b = load(app_conn, b)
    assert set(seen_b.feeds) == {"feed-a"} and dict(seen_b.entitlements) == {}
    assert display(seen_b, b, at) == {DenyCode.NOT_ENTITLED.value}
    with tn.tenant_transaction(app_conn, b) as tx:
        assert tx.conn.execute(
            "SELECT count(*) FROM app.rights_entitlement"
        ).fetchone() == (0,)
    # The owner cannot write tenant A's entitlement from tenant B's transaction.
    until = at + timedelta(days=1)
    with (
        pytest.raises(rs.RightsStoreError, match="tenant"),
        tn.tenant_transaction(owner, b) as tx,
    ):
        rs.record_entitlement(tx, entitlement(str(a), "feed-a", at, until))
    with (
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
        tn.tenant_transaction(owner, b) as tx,
    ):
        tx.conn.execute(
            "INSERT INTO app.rights_entitlement (tenant_id, feed_id, revision, source,"
            " active, rights, content_hash, recorded_at) VALUES (%s, 'feed-a', 2, "
            "'installation_license', true, '{}', repeat('a', 64), now())",
            (a,),
        )


@pytest.mark.db
def test_runtime_roles_cannot_record_or_rewrite_rights(
    app_conn: Conn, worker_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    at = grant_all(owner, a)
    for c in (app_conn, worker_conn):
        with pytest.raises(rs.RightsStoreError, match="database"):
            rs.record_provider(c, Provider("provider-rogue", "SYNTHETIC rogue"))
        held = load(c, a).entitlements[(str(a), "feed-a")]
        rogue = held.revise(
            EntitlementSource.USER_CREDENTIAL,
            True,
            profile(at, at + KEEP),
            None,
            clock(c),
        )
        with (
            pytest.raises(rs.RightsStoreError, match="database"),
            tn.tenant_transaction(c, a) as tx,
        ):
            rs.record_entitlement(tx, rogue)
        for table in rs.TABLES:
            for sql in (
                f"UPDATE app.{table} SET {KEY[table]} = {KEY[table]}",
                f"DELETE FROM app.{table}",
                f"TRUNCATE app.{table}",
            ):
                with (
                    pytest.raises(errors.InsufficientPrivilege),
                    tn.tenant_transaction(c, a) as tx,
                ):
                    tx.conn.execute(sql)
    for table in rs.TABLES:  # the owner cannot rewrite either: revocation is a row
        with (
            pytest.raises(errors.RaiseException, match="append-only"),
            tn.tenant_transaction(owner, a) as tx,
        ):
            tx.conn.execute(f"DELETE FROM app.{table}")
    assert display(load(app_conn, a), a, at) == set()


@pytest.mark.db
def test_revocation_and_expiry_are_honoured_after_reload(
    app_conn: Conn, owner: Conn
) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    at = grant_all(owner, a, ("feed-a", "feed-b"), until=clock(owner) + KEEP)
    with tn.tenant_transaction(owner, b) as tx:
        rs.record_entitlement(tx, entitlement(str(b), "feed-a", at, at + KEEP))
    reg = load(app_conn, a)
    assert display(reg, a, at + KEEP) == {DenyCode.EXPIRED.value}
    assert display(reg, a, at + KEEP - timedelta(seconds=1)) == set()
    # Tenant A's entitlement to feed-a is revoked: a new, inactive revision.
    later = clock(owner)
    revoked = reg.entitlements[(str(a), "feed-a")].revise(
        EntitlementSource.INSTALLATION_LICENSE,
        False,
        profile(at, at + KEEP),
        None,
        later,
    )
    with tn.tenant_transaction(owner, a) as tx:
        rs.record_entitlement(tx, revoked)
    # feed-b is suspended for every tenant: a recorded transition.
    note = Evidence(EvidenceKind.DOCS, "synthetic-suspension", later)
    rs.record_feed(
        owner,
        reg.feeds["feed-b"].transition(
            FeedStatus.SUSPENDED, "operator-synth", (note,), later
        ),
    )
    after, now = load(app_conn, a), clock(app_conn) + timedelta(seconds=31)
    assert display(after, a, now) == {DenyCode.NOT_ENTITLED.value}
    assert display(after, a, now, "feed-b") == {DenyCode.NOT_QUALIFIED.value}
    assert display(after, a, at) == set()  # what was known before the revocation
    assert display(load(app_conn, b), b, now) == set()  # B keeps its entitlement
    count = app_conn.execute(
        "SELECT count(*) FROM app.rights_feed_transition WHERE feed_id = 'feed-b'"
    ).fetchone()
    assert count == (3,)


@pytest.mark.db
def test_stale_or_rewritten_histories_are_refused(app_conn: Conn, owner: Conn) -> None:
    a = tenant(app_conn)
    at = grant_all(owner, a)
    reg = load(app_conn, a)
    other = FeedHistory.new("feed-a", PROVIDER.provider_id).revise(
        "synthetic_other", frozenset({Use.DISPLAY}), profile(at, at + KEEP), at
    )
    with pytest.raises(rs.RightsStoreError, match="append_only"):
        rs.record_feed(owner, other)  # not an extension of the stored history
    with pytest.raises(rs.RightsStoreError, match="append_only"):
        rs.record_provider(owner, Provider(PROVIDER.provider_id, "SYNTHETIC renamed"))
    with pytest.raises(rs.RightsStoreError, match="provider_unregistered"):
        rs.record_feed(
            owner,
            FeedHistory.new("feed-x", "provider-none").revise(
                "synthetic_feed", frozenset({Use.DISPLAY}), profile(at, at + KEEP), at
            ),
        )
    # A back-dated knowledge time is refused by the database clock.
    old = at - timedelta(hours=1)
    stale = reg.feeds["feed-a"]
    with pytest.raises(rs.RightsStoreError, match="database"):
        rs.record_feed(
            owner,
            FeedHistory.new("feed-old", PROVIDER.provider_id).revise(
                "synthetic_feed", frozenset({Use.DISPLAY}), profile(old, at), old
            ),
        )
    with pytest.raises(rs.RightsStoreError, match="database"):
        future = clock(owner) + timedelta(hours=1)
        rs.record_feed(
            owner,
            stale.transition(
                FeedStatus.SUSPENDED,
                "operator-synth",
                (Evidence(EvidenceKind.DOCS, "synthetic-x", at),),
                future,
            ),
        )
    assert load(app_conn, a) == reg


@pytest.mark.db
@pytest.mark.parametrize(
    "forge",
    [
        "UPDATE app.rights_feed_revision SET rights = jsonb_set(rights, "
        "'{uses,display,0}', '\"denied\"')",  # a revocation by edit, not by row
        "UPDATE app.rights_feed_revision SET content_hash = repeat('b', 64)",
        "UPDATE app.rights_feed_revision SET rights = jsonb_set(rights, "
        "'{uses,display,2}', '\"2999-01-01T00:00:00Z\"')",
        "UPDATE app.rights_entitlement SET rights = jsonb_set(rights, "
        "'{uses,display}', rights #> '{uses,derived_data}')",  # a silent grant
        "UPDATE app.rights_entitlement SET source = 'user_credential'",
        "DELETE FROM app.rights_feed_transition WHERE status = 'connected'",
        "UPDATE app.rights_feed_transition SET evidence = '[]' "
        "WHERE status = 'qualified'",
        "UPDATE app.rights_feed_transition SET status = 'qualified' "
        "WHERE status = 'connected'",
        'UPDATE app.rights_feed_revision SET required_uses = \'["display", '
        '"display"]\'',
        "UPDATE app.rights_feed SET provider_id = 'provider-none'",
        # Equal meaning, non-canonical form: still refused.
        "UPDATE app.rights_feed_revision SET required_uses = (SELECT "
        "jsonb_agg(u ORDER BY u DESC) FROM jsonb_array_elements(required_uses) u)",
        "UPDATE app.rights_feed_revision SET rights = "
        "replace(rights::text, 'Z\"', '+00:00\"')::jsonb",
    ],
)
def test_tampered_rows_fail_closed(
    app_conn: Conn, owner: Conn, conn: Conn, forge: str
) -> None:
    a = tenant(app_conn)
    grant_all(owner, a, missing=Use.DISPLAY)
    conn.execute("SET session_replication_role = replica")  # a tampering superuser
    if "provider-none" in forge:
        conn.execute(
            "INSERT INTO app.rights_provider (id, name) "
            "VALUES ('provider-none', 'SYNTHETIC')"
        )
    conn.execute(forge)
    with pytest.raises(rs.RightsStoreError, match="integrity"):
        load(app_conn, a)


@pytest.mark.db
def test_a_forged_qualification_by_the_owner_fails_closed(
    app_conn: Conn, owner: Conn
) -> None:
    """The owner can insert rows past the adapter, but a transition the domain
    would refuse (qualified without evidence) makes the registry unloadable."""
    a = tenant(app_conn)
    at = clock(owner)
    rs.record_provider(owner, PROVIDER)
    rs.record_feed(
        owner,
        FeedHistory.new("feed-a", PROVIDER.provider_id).revise(
            "synthetic_feed", frozenset(Use), profile(at, at + KEEP), at
        ),
    )
    digest = load(app_conn, a).feeds["feed-a"].current.content_hash
    owner.execute(
        "INSERT INTO app.rights_feed_transition (feed_id, position, status, actor_id,"
        " evidence, at, revision, revision_hash) VALUES ('feed-a', 0, 'qualified', "
        "'operator-synth', '[]', clock_timestamp(), 1, %s)",
        (digest,),
    )
    with pytest.raises(rs.RightsStoreError, match="integrity"):
        load(app_conn, a)


def new_feed(feed_id: str, at: datetime) -> FeedHistory:
    return FeedHistory.new(feed_id, PROVIDER.provider_id).revise(
        "synthetic_feed", frozenset({Use.DISPLAY}), profile(at, at + KEEP), at
    )


@pytest.mark.db
def test_knowledge_times_are_at_most_a_minute_old(owner: Conn) -> None:
    """The 0013 window: 60 seconds of back-dating, nothing in the future."""
    rs.record_provider(owner, PROVIDER)
    now = clock(owner) + timedelta(seconds=30)  # clock() is 30 s back
    rs.record_feed(owner, new_feed("feed-in", now - timedelta(seconds=50)))
    for feed_id, at in (("feed-old", now - timedelta(seconds=70)),
                        ("feed-new", now + timedelta(seconds=10))):  # fmt: skip
        with pytest.raises(rs.RightsStoreError, match="database"):
            rs.record_feed(owner, new_feed(feed_id, at))
    assert owner.execute("SELECT id FROM app.rights_feed").fetchall() == [("feed-in",)]


@pytest.mark.db
def test_a_row_never_predates_the_previous_one(app_conn: Conn, owner: Conn) -> None:
    """Even a raw owner insert inside the window cannot go before the last
    revision or transition of its feed, or the last revision of an entitlement."""
    a = tenant(app_conn)
    at = grant_all(owner, a)
    digest = load(app_conn, a).feeds["feed-a"].current.content_hash
    early = at - timedelta(seconds=10)
    for sql, args in (
        ("INSERT INTO app.rights_feed_transition (feed_id, position, status, "
         "actor_id, evidence, at, revision, revision_hash) VALUES ('feed-a', 2, "
         "'suspended', 'operator-synth', '[]', %s, 1, %s)", (early, digest)),
        ("INSERT INTO app.rights_feed_revision (feed_id, revision, feed, "
         "required_uses, rights, content_hash, recorded_at) VALUES ('feed-a', 2, "
         "'synthetic_feed', '[]', '{}', %s, %s)", (digest, early)),
        ("INSERT INTO app.rights_entitlement (tenant_id, feed_id, revision, source,"
         " active, rights, content_hash, recorded_at) VALUES (%s, 'feed-a', 2, "
         "'installation_license', false, '{}', %s, %s)", (a, digest, early)),
    ):  # fmt: skip
        with (
            pytest.raises(errors.CheckViolation, match="previous row"),
            tn.tenant_transaction(owner, a) as tx,
        ):
            tx.conn.execute(sql, args)
    assert display(load(app_conn, a), a, at) == set()


@pytest.mark.db
def test_load_keeps_only_this_tenants_entitlements(
    app_conn: Conn, owner: Conn, conn: Conn
) -> None:
    """The tenant filter holds even on a connection that bypasses RLS (the
    superuser), so another tenant's entitlement never enters this registry."""
    a, b = tenant(app_conn), tenant(app_conn)
    at = grant_all(owner, a)
    with tn.tenant_transaction(owner, b) as tx:
        rs.record_entitlement(tx, entitlement(str(b), "feed-a", at, at + KEEP))
    mine = load(app_conn, a)
    assert set(mine.entitlements) == {(str(a), "feed-a")}
    assert load(conn, a) == mine  # superuser: RLS is bypassed, the filter is not
    assert load(conn, b).entitlements.keys() == {(str(b), "feed-a")}


@pytest.mark.db
def test_a_reader_holds_the_lock_until_its_transaction_ends(
    app_conn: Conn, owner: Conn
) -> None:
    """A load holds the shared lock: a writer waits (here: times out) until the
    reader's transaction ends, so a load never sees half a write."""
    a = tenant(app_conn)
    owner.execute("SET lock_timeout = '300ms'")
    with tn.tenant_transaction(app_conn, a) as tx:
        rs.load(tx)
        held = tx.conn.execute(
            "SELECT mode FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid()"
        ).fetchall()
        assert held == [("ShareLock",)]
        with pytest.raises(rs.RightsStoreError, match="database"):
            rs.record_provider(owner, PROVIDER)
    rs.record_provider(owner, PROVIDER)  # free once the reader has finished
    assert set(load(app_conn, a).providers) == {PROVIDER.provider_id}
