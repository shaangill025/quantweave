"""Seeding the six strategy families (T027; owner decision 10, 2026-10-15) through
qw_adapters.strategy_seed on PostgreSQL 16. The seed runs as the schema owner
qw_migrate (`owner`); reads run as the non-superuser runtime logins. Seeded feeds are
placeholders of a data class (free, delayed or end-of-day) with no provider chosen:
registered, never qualified, every right unknown, no entitlement. The tests that
simulate a code change replace a seeded manifest's hash by a SYNTHETIC one."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import rights_store as rs
from qw_adapters import strategy_seed as seed
from qw_adapters import strategy_store as ss
from qw_adapters import tenancy as tn
from qw_domain import gateway as gw
from qw_domain import strategy_registry as sr
from qw_domain.budget import BudgetBook, Category
from qw_domain.decimals import UsdBudget
from qw_domain.researcher import UNCONFIGURED_MODEL, ai_thesis_caps
from qw_domain.rights import (
    FAIL_CLOSED,
    FeedHistory,
    FeedStatus,
    Provider,
    Registry,
    Use,
    UseScope,
)
from qw_domain.strategy_gate import GateCode, GateInputs, research_eligible

Conn = psycopg.Connection[TupleRow]
SEED_TABLES = ("strategy_manifest", "rights_provider", "rights_feed",
               "rights_feed_revision", "rights_feed_transition",
               "rights_entitlement")  # fmt: skip


@pytest.fixture
def owner(database_url: str, runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("SET ROLE qw_migrate")
        yield c


def counts(c: Conn) -> dict[str, Any]:
    return {t: c.execute(f"SELECT count(*) FROM app.{t}").fetchone() for t in
            SEED_TABLES}  # fmt: skip


def snapshot(c: Conn, t: uuid.UUID) -> tuple[ss.Snapshot, Registry]:
    with tn.tenant_transaction(c, t) as tx:
        return ss.snapshot(tx), rs.load(tx)


def thesis() -> sr.StrategyManifest:
    (m,) = [m for m in seed.manifests() if m.family is sr.Family.AI_THESIS]
    return m


def test_seed_set_is_six_families_on_free_or_delayed_feeds() -> None:
    ms = seed.manifests()
    assert sorted(m.family for m in ms) == sorted(sr.Family)
    assert {m.strategy_id for m in ms} == {
        "STR-ALLOC-001", "STR-QUALITY-001", "STR-INCOME-001", "STR-TREND-001",
        "STR-ORB-001", "STR-AITHESIS-001"}  # fmt: skip
    assert {m.version for m in ms} == {"0.1.0-research"}
    feeds = {d.feed_id for m in ms for d in m.data}
    assert feeds == set(seed.FEEDS) and "feed-config-check" not in feeds
    assert all(f.startswith("free-") for f in feeds)  # a data class, never a vendor
    uses = seed.feed_uses(ms)
    assert uses["free-public-filings"] == {Use.DERIVED_DATA, Use.MODEL_PROCESSING}
    assert uses["free-eod-daily-bars"] == {Use.DERIVED_DATA}
    assert uses["free-delayed-intraday-bars"] == {Use.DERIVED_DATA}
    assert seed.feed_uses(reversed(ms)) == uses  # a union, not the last declaration
    bound = {m.family: [d.feed_id for d in m.data] for m in ms}
    eod, delayed, filings = (["free-eod-daily-bars"], ["free-delayed-intraday-bars"],
                             ["free-public-filings"])  # fmt: skip
    assert bound == {
        sr.Family.ALLOCATION: eod, sr.Family.SWING_MOMENTUM: eod,
        sr.Family.OPENING_RANGE_BREAKOUT: delayed, sr.Family.QUALITY_VALUE: filings,
        sr.Family.INCOME: filings, sr.Family.AI_THESIS: filings}  # fmt: skip
    # Neutral literals: a placeholder provider and model, naming no vendor or model.
    assert seed.PROVIDER.provider_id == "unassigned-free-source"
    assert seed.PROVIDER.name == (
        "Placeholder: no provider chosen; free, delayed or end-of-day class only"
    )
    assert thesis().model_ref == "model-gateway-unconfigured"


def test_ai_thesis_has_a_zero_budget_and_needs_ai_mode() -> None:
    m = thesis()
    assert (m.model_ref, m.horizon) == (UNCONFIGURED_MODEL, sr.Horizon.LONG_TERM)
    assert UNCONFIGURED_MODEL == "model-gateway-unconfigured"
    (budget,) = m.parameters
    assert (budget.name, budget.default, budget.minimum, budget.maximum) == (
        "monthly_budget_usd", Decimal(0), Decimal(0), Decimal(0))  # fmt: skip
    with pytest.raises(sr.StrategyError, match="out of bounds"):
        m.resolve({"monthly_budget_usd": Decimal("0.000001")})
    caps = ai_thesis_caps(m, {})
    assert (caps.inference, caps.improvement, caps.authorization_ref) == (
        UsdBudget(0), UsdBudget(0), None)  # fmt: skip
    # The gate reads AI mode from the adopted policy: with none, ai_thesis is refused
    # even for research; a rule family is not asked for one.
    reg = sr.StrategyRegistry()
    for x in seed.manifests():
        reg = reg.register(x)
    at = datetime(2026, 10, 15, 15, tzinfo=UTC)
    ins = GateInputs(Registry(), UseScope.PERSONAL, "CA-ON", None, {}, frozenset())
    ai = research_eligible(reg, "tenant-synth", m.strategy_id, m.version, {}, at, ins)
    assert GateCode.POLICY_NOT_ADOPTED in {r.code for r in ai.reasons}
    trend = research_eligible(reg, "tenant-synth", "STR-TREND-001",
                              "0.1.0-research", {}, at, ins)  # fmt: skip
    assert GateCode.POLICY_NOT_ADOPTED not in {r.code for r in trend.reasons}
    assert not ai.allowed and not trend.allowed  # no rights: fail closed


def test_the_seeded_gateway_config_cannot_spend() -> None:
    """The seeded chain is empty (`no_route`); even a SYNTHETIC qualified, priced
    route is refused by the zero, unauthorized caps before any dispatch."""
    m, at = thesis(), datetime(2026, 10, 15, 15, tzinfo=UTC)
    out = gw.OutputSchema("out-synth", (("text", gw.Kind.TEXT),))
    req = gw.ModelRequest("req-synth-1", "tenant-synth", Category.RESEARCH,
        gw.DataClass.NONE, frozenset(), 10, 10, timedelta(seconds=5), 1,
        frozenset(), out, "SYNTHETIC prompt")  # fmt: skip
    ctx = gw.GatewayContext(Registry(), UseScope.PERSONAL, "CA-ON", None,
                            timedelta(minutes=1))  # fmt: skip
    book = BudgetBook.open("tenant-synth", at, ai_thesis_caps(m, {}))
    refused = gw.admit(req, seed.ROUTES, ctx, book, at)
    assert isinstance(refused, gw.Refused)
    assert refused.denials == (gw.Denial(gw.Reason.NO_ROUTE),)
    rate = UsdBudget(Decimal(1))
    route = gw.Route("route-synth", "family-synth", m.model_ref or "", "rt-synth",
        "acct-synth", "cred-synth", frozenset(Category), gw.DataClass.NONE, False,
        False, False, frozenset(), gw.Bounds(100, 100, timedelta(seconds=9), 1),
        gw.Rates(rate, rate, "rate-synth"), gw.Qualification.QUALIFIED,
        "qual-synth")  # fmt: skip
    priced = gw.admit(req, (route,), ctx, book, at)
    assert isinstance(priced, gw.Refused)
    assert priced.tried == (("route-synth", (gw.Denial(
        gw.Reason.BUDGET, "spending_not_authorized"),)),)  # fmt: skip


@pytest.mark.db
def test_seeding_is_idempotent_and_grants_no_rights(
    app_conn: Conn, worker_conn: Conn, owner: Conn
) -> None:
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    first = seed.seed(owner)
    assert sorted(first.manifests) == sorted(
        f"{m.strategy_id}@{m.version}" for m in seed.manifests())  # fmt: skip
    assert sorted(first.feeds) == sorted(seed.FEEDS)
    after = counts(owner)
    again = seed.seed(owner)  # a rerun records nothing
    assert (again.manifests, again.feeds) == ((), ())
    assert counts(owner) == after
    assert after["rights_feed_transition"] == after["rights_entitlement"] == (0,)
    for c in (app_conn, worker_conn):
        snap, rights = snapshot(c, t)
        assert {m.family for m in snap.latest} == set(sr.Family)
        assert list(snap.latest) == sorted(seed.manifests(),
                                           key=lambda m: m.strategy_id)  # fmt: skip
        assert set(rights.feeds) == set(seed.FEEDS) and not rights.entitlements
        for fid, h in rights.feeds.items():
            (rev,) = h.revisions
            assert (h.status, h.provider_id, rev.rights) == (
                FeedStatus.REGISTERED,
                seed.PROVIDER.provider_id,
                FAIL_CLOSED,
            )
            assert rev.required_uses == seed.feed_uses(seed.manifests())[fid]
        for m in snap.latest:
            s = sr.status(snap.registry, str(t), m.strategy_id, m.version, {},
                          snap.at)  # fmt: skip
            assert (s.operational, s.evidence, s.eligibility) == (
                sr.OperationalState.NOT_TESTED, sr.EvidenceState.NONE,
                sr.EligibilityState.NOT_EVALUATED)  # fmt: skip


@pytest.mark.db
def test_an_existing_feed_is_left_as_the_operator_declared_it(
    app_conn: Conn, owner: Conn
) -> None:
    other = Provider("provider-synth", "SYNTHETIC operator provider")
    rs.record_provider(owner, other)
    (at,) = owner.execute("SELECT clock_timestamp()").fetchone() or ()
    declared = FeedHistory.new("free-eod-daily-bars", other.provider_id).revise(
        "free-eod-daily-bars", frozenset({Use.DISPLAY}), FAIL_CLOSED, at
    )
    rs.record_feed(owner, declared)
    done = seed.seed(owner)
    assert sorted(done.feeds) == ["free-delayed-intraday-bars", "free-public-filings"]
    with tn.provision_tenant(app_conn) as tx:
        assert rs.load(tx).feeds["free-eod-daily-bars"] == declared


@pytest.mark.db
def test_a_code_change_under_the_same_version_is_refused_until_bumped(
    app_conn: Conn, owner: Conn
) -> None:
    """Release step: edit a family module, bump its VERSION, then reseed."""
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    seed.seed(owner)
    before = counts(owner)
    edited = [
        replace(m, implementation_hash="c" * 64) if m.family is sr.Family.INCOME else m
        for m in seed.manifests()
    ]
    new = replace(seed.manifests()[0], version="0.1.1-research")  # alone: recorded
    with pytest.raises(ss.StrategyStoreError, match="immutable"):
        seed.seed(owner, [new, *edited])
    assert counts(owner) == before  # all or nothing
    bumped = [
        replace(m, version="0.1.1-research") if m.family is sr.Family.INCOME else m
        for m in edited
    ]
    done = seed.seed(owner, bumped)
    assert (done.manifests, done.feeds) == (("STR-INCOME-001@0.1.1-research",), ())
    snap, _ = snapshot(app_conn, t)
    income = [m for m in snap.latest if m.strategy_id == "STR-INCOME-001"]
    assert [(m.version, m.implementation_hash) for m in income] == [
        ("0.1.1-research", "c" * 64)]  # fmt: skip
    assert len(snap.registry.manifests) == 7  # the old version stays


@pytest.mark.db
def test_runtime_roles_cannot_seed(app_conn: Conn, database_url: str) -> None:
    with seed.connect(database_url) as c:  # the CLI acts as the schema owner
        assert c.execute("SELECT current_user").fetchone() == ("qw_migrate",)
    with pytest.raises(ss.StrategyStoreError, match="database"):
        seed.seed(app_conn)
    assert counts(app_conn)["strategy_manifest"] == (0,)
