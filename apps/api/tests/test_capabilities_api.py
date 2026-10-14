"""`GET /capabilities` (readCapabilities) through TestClient on a migrated PostgreSQL
16 database as the non-superuser qw_app login, with the default rights store. Bodies
are validated against the spec `Capabilities` schema (closed; project contract 0.3.0
does not amend it). Every provider, feed, tenant and evidence id is SYNTHETIC; the
rights are recorded by the schema owner just before the database clock."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_adapters import rights_store as rs
from qw_adapters import tenancy as tn
from qw_domain.release import Edition
from qw_domain.rights import (
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Use,
)

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
rst = _module("qw_rstore_tests",
              ROOT / "packages/adapters/tests/test_rights_store.py")  # fmt: skip
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem, owner = api.login, api.assert_problem, rst.owner
OPENAPI = yaml.safe_load((ROOT / "docs/spec/contracts/openapi.yaml").read_text())
SCHEMA = OPENAPI["components"]["schemas"]["Capabilities"]
URL = "/api/v1/capabilities"
TEN = 10  # features in qw_domain.capabilities.FEATURES


def read(client: Any) -> tuple[str, dict[str, list[str]]]:
    got = client.get(URL)
    assert got.status_code == 200, got.text
    body = got.json()
    assert list(Draft202012Validator(SCHEMA).iter_errors(body)) == []
    features = body["features"]
    assert len(features) == TEN
    for f in features:
        assert f["enabled"] == (f["reason_codes"] == [])
    return body["deployment"], {f["id"]: f["reason_codes"] for f in features}


def self_hosted(w: Any) -> None:
    settings = w.client.app.state.settings
    w.client.app.state.settings = replace(
        settings, deployment=Edition.SELF_HOSTED, jurisdiction=rst.REGION
    )


def tenant_of(w: Any, user: str) -> uuid.UUID:
    row = w.db.execute("SELECT tenant_id FROM app.membership WHERE user_id = %s",
                       (w.users[user],)).fetchone()  # fmt: skip
    assert row is not None
    return uuid.UUID(str(row[0]))


def grant_display(owner: Any, tenant: uuid.UUID) -> None:
    """Record a SYNTHETIC feed qualified for display only, entitled to `tenant`."""
    at = rst.clock(owner)
    until = at + timedelta(days=3650)
    auth = Evidence(EvidenceKind.AUTH, "synthetic-auth", at)
    work = Evidence(EvidenceKind.WORKLOAD, "synthetic-workload", at)
    permit = Evidence(EvidenceKind.PERMISSION, "synthetic-permit", at,
                      "counsel-synth")  # fmt: skip
    rights = rst.profile(at, until)
    feed = (
        FeedHistory.new("feed-a", rst.PROVIDER.provider_id)
        .revise("synthetic_feed", frozenset({Use.DISPLAY}), rights, at)
        .transition(FeedStatus.CONNECTED, "operator-synth", (auth,), at)
        .transition(FeedStatus.QUALIFIED, "operator-synth", (work, permit), at)
    )
    rs.record_provider(owner, rst.PROVIDER)
    rs.record_feed(owner, feed)
    with tn.tenant_transaction(owner, tenant) as tx:
        rs.record_entitlement(tx, rst.entitlement(str(tenant), "feed-a", at, until))


def counts(w: Any) -> dict[str, Any]:
    tables = w.db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'app' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {t: w.db.execute(f'SELECT count(*) FROM app."{t}"').fetchone()[0]
            for (t,) in tables}  # fmt: skip


@pytest.mark.db
def test_authentication_is_required(world: Any) -> None:
    assert_problem(world.client.get(URL), 401, "unauthenticated")


@pytest.mark.db
def test_the_empty_store_disables_every_rights_feature(world: Any) -> None:
    login(world.client, "bob")  # a member suffices
    deployment, got = read(world.client)  # the default: hosted
    assert deployment == "hosted"
    assert [k for k, v in got.items() if not v] == []
    assert got["provider_data_display"] == ["jurisdiction_unknown",
                                            "provider_rights_unqualified"]  # fmt: skip
    self_hosted(world)
    deployment, got = read(world.client)
    assert deployment == "self_hosted"
    assert got["provider_data_display"] == ["provider_rights_unqualified"]
    assert all("provider_rights_unqualified" in got[k] for k in (
        "strategy_proposals", "intraday_signals", "options", "ai_research",
        "ai_evaluator", "governed_chat"))  # fmt: skip
    assert got["code_improvement"] == [
        "experiment_budget_unqualified", "human_promotion_unqualified",
        "independent_assessment_unqualified", "model_route_unqualified",
        "sandbox_unqualified"]  # fmt: skip


@pytest.mark.db
def test_a_stored_grant_enables_what_it_qualifies_for_its_tenant_only(
    world: Any, owner: Any
) -> None:
    a, b = world.tenant_a, tenant_of(world, "carol")
    grant_display(owner, a)
    self_hosted(world)
    login(world.client, "alice")
    before = counts(world)
    _, got = read(world.client)
    assert [k for k, v in got.items() if not v] == ["provider_data_display"]
    assert got["ai_research"] == ["evaluation_evidence_unqualified",
                                  "model_route_unqualified",
                                  "provider_rights_unqualified"]  # fmt: skip
    assert got["strategy_proposals"] == ["provider_rights_unqualified",
                                         "strategy_unqualified"]  # fmt: skip
    assert counts(world) == before  # the read writes nothing
    login(world.client, "carol")  # tenant B: A's entitlement is not B's
    assert b != a
    _, got_b = read(world.client)
    assert got_b["provider_data_display"] == ["provider_rights_unqualified"]
    assert [k for k, v in got_b.items() if not v] == []
    world.client.app.state.settings = replace(
        world.client.app.state.settings, jurisdiction="US"
    )
    login(world.client, "alice")  # the grant covers CA-ON only
    assert read(world.client)[1]["provider_data_display"] == [
        "provider_rights_unqualified"]  # fmt: skip


@pytest.mark.db
def test_a_tampered_registry_is_a_crash_not_an_answer(world: Any, owner: Any) -> None:
    grant_display(owner, world.tenant_a)
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.rights_entitlement SET active = false")
    login(world.client, "alice")
    assert_problem(world.client.get(URL), 500, "internal_error")


def test_settings_refuse_a_hosted_or_malformed_jurisdiction() -> None:
    base = api.Settings(frozenset({api.ORIGIN}), api.KEY)
    assert (base.deployment, base.jurisdiction) == (Edition.HOSTED, None)
    for bad in ({"jurisdiction": "CA-ON"},
                {"deployment": Edition.SELF_HOSTED, "jurisdiction": "ca-on"},
                {"deployment": "self_hosted"}):  # fmt: skip
        with pytest.raises(ValueError):
            replace(base, **bad)
