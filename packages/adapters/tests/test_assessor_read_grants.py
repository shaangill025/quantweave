"""qw_assessor's read grants on the assessor-run inputs (owner decisions 70 and 78,
2026-10-23; migration 0043). The assessor reads strategy
manifests (`strategy_store.manifest`, which never touches adoptions) and the
research rights registry (`rights_store.load`), and nothing else of either store:
no adoption receipt, no write. Every check runs as a real qw_assessor login
(`runtime_connection(role="qw_assessor")`); records are written by the schema owner
qw_migrate and the superuser `conn` only observes or plays a tampering owner. Every
manifest, feed, provider and tenant is SYNTHETIC."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import rights_store as rs
from qw_adapters import strategy_store as ss
from qw_adapters import tenancy as tn
from qw_domain import strategy_registry as sr
from qw_domain.allocation import allocation_manifest
from qw_domain.income import income_manifest
from qw_domain.opening_range import orb_manifest
from qw_domain.quality_value import quality_manifest
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
from qw_domain.swing_momentum import trend_manifest

Conn = psycopg.Connection[TupleRow]
READ = ("strategy_manifest", *rs.TABLES)  # what qw_assessor may read, and only read
KINDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
FEED = "feed-a"
PROVIDER = Provider("provider-synth", "SYNTHETIC provider")
SID, VER = "STR-ALLOC-001", "0.1.0-research"


def six() -> list[sr.StrategyManifest]:
    """The reference manifests bound to a SYNTHETIC feed id (no ai_thesis)."""
    return [allocation_manifest(FEED), quality_manifest(FEED), income_manifest(FEED),
            trend_manifest(FEED), orb_manifest(FEED)]  # fmt: skip


def clock(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now: datetime = row[0].astimezone(UTC)
    return now - timedelta(seconds=5)  # just known


def granted(at: datetime) -> RightsProfile:
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")
    g = Grant(RightState.GRANTED, permit, at + timedelta(days=3650), None)
    return RightsProfile({u: g for u in Use if u is not Use.RETENTION},
                         {UseScope.PERSONAL: g}, {"CA-ON": g})  # fmt: skip


def qualified_feed(feed_id: str, at: datetime) -> FeedHistory:
    auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", at)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", at)
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at, "counsel-synth")
    return (
        FeedHistory.new(feed_id, PROVIDER.provider_id)
        .revise("synthetic_feed", frozenset({Use.DISPLAY}), granted(at), at)
        .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), at)
        .transition(FeedStatus.QUALIFIED, "operator-synth", (work, permit), at)
    )


def entitlement(t: uuid.UUID, at: datetime) -> EntitlementHistory:
    return EntitlementHistory.new(str(t), FEED).revise(
        EntitlementSource.INSTALLATION_LICENSE, True, granted(at), None, at
    )


def grant_all(owner: Conn, t: uuid.UUID) -> None:
    at = clock(owner)
    rs.record_provider(owner, PROVIDER)
    rs.record_feed(owner, qualified_feed(FEED, at))
    with tn.tenant_transaction(owner, t) as tx:
        rs.record_entitlement(tx, entitlement(t, at))


def consent(at: datetime) -> sr.StrategyConsent:
    return sr.StrategyConsent("principal-synth", "stepup-synth-1", at,
                              "SYNTHETIC acknowledgement")  # fmt: skip


def adopt(owner: Conn, t: uuid.UUID) -> None:
    with tn.tenant_transaction(owner, t) as tx:
        ss.record_adoption(tx, SID, VER, {}, consent(clock(owner)))


pytestmark = pytest.mark.db


@pytest.fixture
def owner(database_url: str, runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """The schema owner's role, the only writer of both registries."""
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("SET ROLE qw_migrate")
        yield c


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    """A login checked to be exactly qw_assessor."""
    url = runtime_urls["qw_assessor"]
    with tn.runtime_connection(url, role="qw_assessor") as c:
        yield c


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


def stored(owner: Conn, t: uuid.UUID) -> None:
    """Five manifests, one adoption receipt and a qualified, entitled feed."""
    for m in six():
        ss.record_manifest(owner, m)
    adopt(owner, t)
    grant_all(owner, t)


def counts(conn: Conn) -> dict[str, int]:
    out = {}
    for table in (*READ, "strategy_adoption"):
        row = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
        assert row is not None
        out[table] = int(row[0])
    return out


def test_assessor_reads_manifests_and_rights(
    asr: Conn, app_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    assert ss.manifest(asr, SID, VER) is None  # nothing stored yet
    stored(owner, a)
    for m in six():  # outside any tenant: manifests are installation facts
        assert ss.manifest(asr, m.strategy_id, m.version) == m
    with tn.tenant_transaction(asr, a) as tx:
        assert ss.manifest(tx, SID, VER) == allocation_manifest(FEED)
        assert ss.manifest(tx, SID, "0.9.9") is None
        assert ss.manifest(tx, "STR-NONE", VER) is None
        reg = rs.load(tx)
    with tn.tenant_transaction(app_conn, a) as tx:
        assert reg == rs.load(tx)  # the same registry the app sees
    assert set(reg.providers) == {PROVIDER.provider_id}
    assert set(reg.feeds) == {FEED}
    assert set(reg.entitlements) == {(str(a), FEED)}


def test_assessor_holds_exactly_select_on_the_inputs(conn: Conn, asr: Conn) -> None:
    rows = conn.execute(
        "SELECT t, k FROM unnest(%s::text[]) t CROSS JOIN unnest(%s::text[]) k "
        "WHERE has_table_privilege('qw_assessor', 'app.' || t, k) OR (k IN "
        "('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') AND "
        "has_any_column_privilege('qw_assessor', 'app.' || t, k))",
        ([*READ, "strategy_adoption"], list(KINDS)),
    ).fetchall()
    assert set(rows) == {(t, "SELECT") for t in READ}  # no adoption, no write
    for role in ("qw_app", "qw_worker"):  # their 0013/0015 reads are unchanged
        row = conn.execute(
            "SELECT bool_and(has_table_privilege(%s, 'app.' || t, 'SELECT')) "
            "FROM unnest(%s::text[]) t",
            (role, [*READ, "strategy_adoption"]),
        ).fetchone()
        assert row == (True,), role


def test_assessor_cannot_read_adoption_records(
    asr: Conn, app_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    stored(owner, a)
    for sql in ("SELECT * FROM app.strategy_adoption",
                "SELECT count(*) FROM app.strategy_adoption",
                "SELECT receipt_hash FROM app.strategy_adoption"):  # fmt: skip
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tn.tenant_transaction(asr, a) as tx,
        ):
            tx.conn.execute(sql)
    with (
        pytest.raises(ss.StrategyStoreError, match="database"),
        tn.tenant_transaction(asr, a) as tx,
    ):
        ss.load(tx)  # the registry read includes adoptions: refused, not partial
    with tn.tenant_transaction(asr, a) as tx:  # the manifest read still works
        assert ss.manifest(tx, SID, VER) == allocation_manifest(FEED)


WRITES = [
    "INSERT INTO app.strategy_manifest (strategy_id, version, family, body, "
    "material_hash, content_hash) SELECT 'STR-X', version, family, body, "
    "material_hash, content_hash FROM app.strategy_manifest LIMIT 1",
    "UPDATE app.strategy_manifest SET version = version",
    "DELETE FROM app.strategy_manifest",
    "TRUNCATE app.strategy_manifest",
    "INSERT INTO app.strategy_adoption (tenant_id, strategy_id, position, version, "
    "manifest_hash, config_hash, principal_id, step_up_receipt_id, signed_at, "
    "acknowledgement_text, receipt_hash) VALUES (app.current_tenant_id(), "
    "'STR-ALLOC-001', 9, '0.1.0-research', repeat('a', 64), repeat('a', 64), 'p', "
    "'s', clock_timestamp(), 'x', repeat('a', 64))",
    "UPDATE app.strategy_adoption SET position = 0",
    "DELETE FROM app.strategy_adoption",
    "TRUNCATE app.strategy_adoption",
    "INSERT INTO app.rights_provider (id, name) VALUES ('p-x', 'SYNTHETIC x')",
    "UPDATE app.rights_provider SET name = name",
    "DELETE FROM app.rights_provider",
    "TRUNCATE app.rights_provider CASCADE",
    "INSERT INTO app.rights_feed SELECT * FROM app.rights_feed",
    "UPDATE app.rights_feed SET provider_id = provider_id",
    "DELETE FROM app.rights_feed",
    "TRUNCATE app.rights_feed CASCADE",
    "INSERT INTO app.rights_feed_revision SELECT * FROM app.rights_feed_revision",
    "UPDATE app.rights_feed_revision SET feed = feed",
    "DELETE FROM app.rights_feed_revision",
    "TRUNCATE app.rights_feed_revision CASCADE",
    "INSERT INTO app.rights_feed_transition SELECT * FROM app.rights_feed_transition",
    "UPDATE app.rights_feed_transition SET status = status",
    "DELETE FROM app.rights_feed_transition",
    "TRUNCATE app.rights_feed_transition",
    "INSERT INTO app.rights_entitlement SELECT * FROM app.rights_entitlement",
    "UPDATE app.rights_entitlement SET active = active",
    "DELETE FROM app.rights_entitlement",
    "TRUNCATE app.rights_entitlement",
]


def test_every_assessor_write_is_refused_at_the_database(
    asr: Conn, app_conn: Conn, owner: Conn, conn: Conn
) -> None:
    a = tenant(app_conn)
    stored(owner, a)
    before = counts(conn)
    for sql in WRITES:
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tn.tenant_transaction(asr, a) as tx,
        ):
            tx.conn.execute(sql)
    # the store writers too: the refusal is the database's, not the adapter's
    with pytest.raises(ss.StrategyStoreError, match="database"):
        ss.record_manifest(asr, replace(trend_manifest(FEED), version="0.2.0"))
    with (
        pytest.raises(ss.StrategyStoreError, match="database"),
        tn.tenant_transaction(asr, a) as tx,
    ):
        ss.record_adoption(tx, SID, VER, {}, consent(clock(conn)))
    with pytest.raises(rs.RightsStoreError, match="database"):
        rs.record_provider(asr, Provider("provider-x", "SYNTHETIC x"))
    with pytest.raises(rs.RightsStoreError, match="database"):
        rs.record_feed(asr, qualified_feed("feed-x", clock(conn)))
    with (
        pytest.raises(rs.RightsStoreError, match="database"),
        tn.tenant_transaction(asr, a) as tx,
    ):
        held = rs.load(tx).entitlements[(str(a), FEED)]  # extend what is stored
        at = clock(conn)
        source = EntitlementSource.INSTALLATION_LICENSE
        rs.record_entitlement(tx, held.revise(source, False, granted(at), None, at))
    assert counts(conn) == before


def test_entitlement_rls_holds_for_the_assessor(
    asr: Conn, app_conn: Conn, owner: Conn
) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    grant_all(owner, a)
    for t, n in ((a, 1), (b, 0)):
        with tn.tenant_transaction(asr, t) as tx:
            got = tx.conn.execute("SELECT count(*) FROM app.rights_entitlement")
            assert got.fetchone() == (n,), t
            seen = set(rs.load(tx).entitlements)
            assert seen == ({(str(a), FEED)} if n else set()), t
    with tn.tenant_transaction(asr, b) as tx:  # naming tenant A cannot cross
        got = tx.conn.execute(
            "SELECT count(*) FROM app.rights_entitlement WHERE tenant_id = %s", (a,)
        )
        assert got.fetchone() == (0,)
    with asr.transaction():  # no tenant context: nothing, not everything
        got = asr.execute("SELECT count(*) FROM app.rights_entitlement")
        assert got.fetchone() == (0,)


ALLOC = "WHERE strategy_id = 'STR-ALLOC-001'"


@pytest.mark.parametrize(
    "forge",
    [
        f"UPDATE app.strategy_manifest SET body = jsonb_set(body, '{{title}}', "
        f"'\"other\"') {ALLOC}",
        f"UPDATE app.strategy_manifest SET body = body || '{{\"extra\": 1}}' {ALLOC}",
        f"UPDATE app.strategy_manifest SET body = jsonb_set(body, '{{version}}', "
        f"'\"0.2.0\"') {ALLOC}",
        f"UPDATE app.strategy_manifest SET body = jsonb_set(body, "
        f"'{{implementation_hash}}', to_jsonb(repeat('0', 64))) {ALLOC}",
        f"UPDATE app.strategy_manifest SET material_hash = repeat('0', 64) {ALLOC}",
        f"UPDATE app.strategy_manifest SET content_hash = repeat('0', 64) {ALLOC}",
        f"UPDATE app.strategy_manifest SET family = 'income' {ALLOC}",
        f"UPDATE app.strategy_manifest SET body = '{{}}' {ALLOC}",
        # another manifest's row moved under this key
        "UPDATE app.strategy_manifest SET strategy_id = 'STR-ALLOC-001', version = "
        "'0.1.0-research' WHERE strategy_id = 'STR-GONE'",
    ],
)
def test_a_tampered_manifest_is_integrity(
    asr: Conn, app_conn: Conn, owner: Conn, conn: Conn, forge: str
) -> None:
    for m in six():
        ss.record_manifest(owner, m)
    conn.execute("SET session_replication_role = replica")  # a tampering superuser
    if "STR-GONE" in forge:  # free the key first, then move the income row into it
        conn.execute(f"UPDATE app.strategy_manifest SET strategy_id = 'STR-X' {ALLOC}")
        conn.execute(
            "UPDATE app.strategy_manifest SET strategy_id = 'STR-GONE' "
            "WHERE family = 'income'"
        )
    conn.execute(forge)
    a = tenant(app_conn)
    with pytest.raises(ss.StrategyStoreError, match="integrity"):
        ss.manifest(asr, SID, VER)
    with (
        pytest.raises(ss.StrategyStoreError, match="integrity"),
        tn.tenant_transaction(asr, a) as tx,
    ):
        ss.manifest(tx, SID, VER)


def test_a_manifest_read_holds_the_shared_lock_until_the_transaction_ends(
    asr: Conn, app_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    ss.record_manifest(owner, allocation_manifest(FEED))
    owner.execute("SET lock_timeout = '300ms'")
    with tn.tenant_transaction(asr, a) as tx:
        ss.manifest(tx, SID, VER)
        with pytest.raises(ss.StrategyStoreError, match="database"):
            ss.record_manifest(owner, income_manifest(FEED))
    ss.record_manifest(owner, income_manifest(FEED))
