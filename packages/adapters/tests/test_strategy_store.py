"""Strategy registry store (0015_strategy_registry, qw_adapters.strategy_store) on
PostgreSQL 16. Reads run as the non-superuser qw_app/qw_worker logins; writes as
the schema owner qw_migrate (`owner`). The superuser `conn` only plays a tampering
owner where a test says so. The manifests are the domain's reference manifests
bound to a SYNTHETIC feed id plus SYNTHETIC ones built here (an ai_thesis with a
SYNTHETIC model ref, every parameter kind); nothing is qualified. Signing times
come from the database clock, which the store requires."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import strategy_store as ss
from qw_adapters import tenancy as tn
from qw_domain import strategy_registry as sr
from qw_domain.allocation import allocation_manifest
from qw_domain.income import income_manifest
from qw_domain.opening_range import orb_manifest
from qw_domain.quality_value import quality_manifest
from qw_domain.rights import Use
from qw_domain.swing_momentum import trend_manifest

Conn = psycopg.Connection[TupleRow]
FEED = "feed-synth"
CONFIG = {"lookback": 9, "weight": Decimal("0.5")}
REWRITES = ("UPDATE app.{} SET strategy_id = strategy_id", "DELETE FROM app.{}")


def synth(version: str = "0.1.0", **changes: object) -> sr.StrategyManifest:
    """SYNTHETIC ai_thesis manifest declaring every parameter kind and retention."""
    params = (
        sr.Parameter("lookback", sr.ParamKind.INTEGER, 7, 1, 30),
        sr.Parameter("weight", sr.ParamKind.DECIMAL, Decimal("0.050"),
                     Decimal("0.00"), Decimal("1")),
        sr.Parameter("mode", sr.ParamKind.CHOICE, "b", allowed=("b", "a")),
        sr.Parameter("hedge", sr.ParamKind.BOOLEAN, False),
    )  # fmt: skip
    data = (
        sr.DataNeed("feed-synth-b", frozenset({Use.DISPLAY, Use.RETENTION}),
                    timedelta(days=30, microseconds=5)),
        sr.DataNeed(FEED, frozenset({Use.MODEL_PROCESSING})),
    )  # fmt: skip
    m = sr.StrategyManifest(
        "STR-SYNTH-AI", version, sr.Family.AI_THESIS, "ab" * 32, data,
        frozenset({sr.AssetClass.STOCKS, sr.AssetClass.OPTIONS}),
        sr.Horizon.SWING_POSITION, False, params, "protocol-synth-1",
        "model-synth-1", "SYNTHETIC thesis",
    )  # fmt: skip
    return replace(m, **changes)  # type: ignore[arg-type]


def six() -> list[sr.StrategyManifest]:
    return [allocation_manifest(FEED), quality_manifest(FEED), income_manifest(FEED),
            trend_manifest(FEED), orb_manifest(FEED), synth()]  # fmt: skip


def clock(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    now: datetime = row[0].astimezone(UTC)
    return now - timedelta(seconds=5)


def consent(at: datetime, who: str = "principal-synth") -> sr.StrategyConsent:
    return sr.StrategyConsent(who, "stepup-synth-1", at, "SYNTHETIC acknowledgement")


@pytest.fixture
def owner(database_url: str, runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("SET ROLE qw_migrate")
        yield c


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


def load(c: Conn, t: uuid.UUID) -> sr.StrategyRegistry:
    with tn.tenant_transaction(c, t) as tx:
        return ss.load(tx)


@contextmanager
def refused(c: Conn, t: uuid.UUID, exc: type[Exception], match: str) -> Iterator[Any]:
    """A tenant transaction whose body must raise `exc` (and so rolls back)."""
    with pytest.raises(exc, match=match), tn.tenant_transaction(c, t) as tx:
        yield tx


def adopt(
    owner: Conn, t: uuid.UUID, config: dict[str, object] | None = None
) -> datetime:
    at = clock(owner)
    with tn.tenant_transaction(owner, t) as tx:
        ss.record_adoption(tx, "STR-SYNTH-AI", "0.1.0", config or {}, consent(at))
    return at


def recorded(owner: Conn, t: uuid.UUID) -> sr.StrategyRegistry:
    for m in six():
        ss.record_manifest(owner, m)
    adopt(owner, t, CONFIG)
    return load(owner, t)


@pytest.mark.db
def test_empty_store_is_an_empty_registry(app_conn: Conn) -> None:
    a = tenant(app_conn)
    assert load(app_conn, a) == sr.StrategyRegistry()
    with tn.tenant_transaction(app_conn, a) as tx:
        snap = ss.snapshot(tx)
    assert (snap.latest, snap.registry) == ((), sr.StrategyRegistry())
    for table in ss.TABLES:  # the migration seeds nothing
        assert app_conn.execute(f"SELECT count(*) FROM app.{table}").fetchone() == (0,)


@pytest.mark.db
def test_round_trip_rebuilds_the_same_registry(
    app_conn: Conn, worker_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    reg = recorded(owner, a)
    expected = sr.StrategyRegistry()
    for m in six():
        expected = expected.register(m)
    receipt = reg.adoptions[(str(a), "STR-SYNTH-AI")][0]
    expected = expected.adopt(str(a), "STR-SYNTH-AI", "0.1.0", CONFIG,
                              consent(receipt.signed_at))  # fmt: skip
    assert load(app_conn, a) == expected == load(worker_conn, a) == reg
    for m in six():  # identical content hashes, field for field
        got = reg.manifest(m.strategy_id, m.version)
        assert (got, got.content_hash, got.material_hash) == (
            m, m.content_hash, m.material_hash)  # fmt: skip
    assert {m.family for m in reg.manifests.values()} == set(sr.Family)
    for m in six():  # recording the same manifest again adds nothing
        ss.record_manifest(owner, m)
    assert load(app_conn, a) == expected
    # A display-only new version becomes the latest; the old one stays.
    ss.record_manifest(owner, synth("0.2.0", title="SYNTHETIC thesis v2"))
    with tn.tenant_transaction(app_conn, a) as tx:
        snap = ss.snapshot(tx)
    assert [(m.strategy_id, m.version) for m in snap.latest] == sorted(
        [(m.strategy_id, m.version) for m in six()[:5]] + [("STR-SYNTH-AI", "0.2.0")]
    )
    assert ("STR-SYNTH-AI", "0.1.0") in snap.registry.manifests
    assert abs(snap.at - clock(app_conn)) < timedelta(seconds=10)
    # Decimal 0.50 is the adopted 0.5: same configuration hash, so adoption is current.
    now = sr.status(snap.registry, str(a), "STR-SYNTH-AI", "0.1.0",
                    {"lookback": 9, "weight": Decimal("0.50")}, snap.at)  # fmt: skip
    assert (now.operational, now.evidence, now.eligibility, now.adoption) == (
        sr.OperationalState.NOT_TESTED, sr.EvidenceState.NONE,
        sr.EligibilityState.NOT_EVALUATED, sr.AdoptionState.CURRENT)  # fmt: skip


@pytest.mark.db
def test_manifests_and_receipts_are_append_only(
    app_conn: Conn, worker_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    recorded(owner, a)
    bound = sr.Parameter("lookback", sr.ParamKind.INTEGER, 7, 1, 31)
    changed = synth(parameters=(bound,))
    with pytest.raises(ss.StrategyStoreError, match="immutable"):
        ss.record_manifest(owner, changed)
    with refused(owner, a, ss.StrategyStoreError, "unknown_strategy") as tx:
        ss.record_adoption(tx, "STR-NONE", "0.1.0", {}, consent(clock(owner)))
    with refused(owner, a, ss.StrategyStoreError, "config") as tx:
        ss.record_adoption(tx, "STR-SYNTH-AI", "0.1.0", {"lookback": 99},
                           consent(clock(owner)))  # fmt: skip
    for c in (app_conn, worker_conn):  # runtime roles only read
        with pytest.raises(ss.StrategyStoreError, match="database"):
            ss.record_manifest(c, synth("0.3.0"))
        with pytest.raises(ss.StrategyStoreError, match="database"):
            adopt(c, a)
        for table in ss.TABLES:
            for sql in (*REWRITES, "TRUNCATE app.{}"):
                with refused(c, a, errors.InsufficientPrivilege, "denied") as tx:
                    tx.conn.execute(sql.format(table))
    for table in ss.TABLES:  # the owner cannot rewrite either
        for sql in REWRITES:
            with refused(owner, a, errors.RaiseException, "append-only") as tx:
                tx.conn.execute(sql.format(table))
    assert len(load(app_conn, a).manifests) == 6


@pytest.mark.db
def test_adoptions_are_tenant_scoped(app_conn: Conn, owner: Conn, conn: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    recorded(owner, a)
    seen_b = load(app_conn, b)
    assert len(seen_b.manifests) == 6 and dict(seen_b.adoptions) == {}
    with tn.tenant_transaction(app_conn, b) as tx:
        rows = tx.conn.execute("SELECT count(*) FROM app.strategy_adoption")
        assert rows.fetchone() == (0,)
    assert load(conn, b) == seen_b  # superuser bypasses RLS; the filter holds
    row = conn.execute("SELECT * FROM app.strategy_adoption").fetchone()
    assert row is not None
    with refused(owner, b, errors.InsufficientPrivilege, "row-level security") as tx:
        tx.conn.execute(
            "INSERT INTO app.strategy_adoption VALUES (%s, %s, 1, %s, %s, %s, %s, "
            "%s, clock_timestamp(), %s, %s)",  # tenant A's row from B's transaction
            (*row[:2], *row[3:8], *row[9:]),
        )
    adopt(owner, b)
    assert set(load(app_conn, b).adoptions) == {(str(b), "STR-SYNTH-AI")}
    assert set(load(app_conn, a).adoptions) == {(str(a), "STR-SYNTH-AI")}


@pytest.mark.db
def test_signing_times_follow_the_database_clock(app_conn: Conn, owner: Conn) -> None:
    a = tenant(app_conn)
    ss.record_manifest(owner, synth())
    now = clock(owner) + timedelta(seconds=5)
    for at in (now - timedelta(seconds=70), now + timedelta(seconds=10)):
        with refused(owner, a, ss.StrategyStoreError, "database") as tx:
            ss.record_adoption(tx, "STR-SYNTH-AI", "0.1.0", {}, consent(at))
    first = adopt(owner, a)
    with refused(owner, a, errors.CheckViolation, "previous row") as tx:
        tx.conn.execute(
            "INSERT INTO app.strategy_adoption SELECT tenant_id, strategy_id, 1, "
            "version, manifest_hash, config_hash, principal_id, step_up_receipt_id, "
            "%s, acknowledgement_text, receipt_hash FROM app.strategy_adoption",
            (first - timedelta(seconds=1),),
        )
    assert len(load(app_conn, a).adoptions[(str(a), "STR-SYNTH-AI")]) == 1


@pytest.mark.db
@pytest.mark.parametrize(
    "forge",
    [
        # A changed bound under the stored hashes.
        "UPDATE app.strategy_manifest SET body = jsonb_set(body, "
        "'{parameters,0,4}', '\"31\"') WHERE strategy_id = 'STR-SYNTH-AI'",
        # Equal meaning, non-canonical form: still refused.
        "UPDATE app.strategy_manifest SET body = jsonb_set(body, "
        "'{parameters,0,2}', '\"007\"') WHERE strategy_id = 'STR-SYNTH-AI'",
        "UPDATE app.strategy_manifest SET body = body || '{\"extra\": 1}'",
        "UPDATE app.strategy_manifest SET body = jsonb_set(body, '{title}', "
        "'\"renamed\"') WHERE strategy_id = 'STR-SYNTH-AI'",
        "UPDATE app.strategy_manifest SET family = 'income' "
        "WHERE strategy_id = 'STR-SYNTH-AI'",
        "UPDATE app.strategy_manifest SET material_hash = repeat('0', 64) "
        "WHERE strategy_id = 'STR-ALLOC-001'",
        "UPDATE app.strategy_manifest SET version = '0.9.0' "
        "WHERE strategy_id = 'STR-ORB-001'",
        "UPDATE app.strategy_adoption SET config_hash = repeat('0', 64)",
        "UPDATE app.strategy_adoption SET acknowledgement_text = 'other'",
        "UPDATE app.strategy_adoption SET signed_at = signed_at - interval '1 day'",
        "DELETE FROM app.strategy_manifest WHERE strategy_id = 'STR-SYNTH-AI'",
    ],
)
def test_tampered_rows_fail_closed(
    app_conn: Conn, owner: Conn, conn: Conn, forge: str
) -> None:
    a = tenant(app_conn)
    recorded(owner, a)
    conn.execute("SET session_replication_role = replica")  # a tampering superuser
    conn.execute(forge)
    with pytest.raises(ss.StrategyStoreError, match="integrity"):
        load(app_conn, a)


@pytest.mark.db
def test_a_reader_holds_the_lock_until_its_transaction_ends(
    app_conn: Conn, owner: Conn
) -> None:
    a = tenant(app_conn)
    owner.execute("SET lock_timeout = '300ms'")
    with tn.tenant_transaction(app_conn, a) as tx:
        ss.load(tx)
        with pytest.raises(ss.StrategyStoreError, match="database"):
            ss.record_manifest(owner, synth())
    ss.record_manifest(owner, synth())
    assert set(load(app_conn, a).manifests) == {("STR-SYNTH-AI", "0.1.0")}
