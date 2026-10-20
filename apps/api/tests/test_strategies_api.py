"""Strategy reads (T027): `GET /strategies` and `/strategies/{id}` through TestClient
on a migrated PostgreSQL 16 database as the non-superuser qw_app login, over
qw_adapters.strategy_store. Manifests are recorded by the schema owner from the
SYNTHETIC set of test_strategy_store (reference manifests on a SYNTHETIC feed id);
nothing is seeded or qualified. Bodies are validated strictly against the spec's
strategy_version schema (no project amendment) and the project contract C-02 page."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from qw_adapters import strategy_store as ss


def tamper(cursor: str) -> str:
    """A cursor whose first character differs from the real one. The first
    base64url character is always 6 signed bits; the last one may be padding."""
    return ("B" if cursor[0] != "B" else "C") + cursor[1:]


ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
sim = _module("qw_sim_api_tests", ROOT / "apps/api/tests/test_simulations_api.py")
st = _module("qw_strategy_store_tests",
             ROOT / "packages/adapters/tests/test_strategy_store.py")  # fmt: skip
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem, csrf = api.login, api.assert_problem, api.csrf
owner, conforms, PC = st.owner, sim.conforms, sim.PC
GAPS = ["default:configuration_hash", "unavailable:qualification",
        "unavailable:action_gate", "unavailable:source_decisions"]  # fmt: skip
FAMILIES = ["allocation", "quality_value", "income", "swing_momentum",
            "opening_range_breakout", "ai_thesis"]  # fmt: skip


@pytest.fixture
def rows(world: Any) -> Iterator[Any]:
    def count() -> list[Any]:
        return [world.db.execute(f"SELECT count(*) FROM app.{t}").fetchone()
                for t in ss.TABLES]  # fmt: skip

    yield count


@pytest.mark.db
def test_an_empty_registry_names_every_missing_family(world: Any) -> None:
    login(world.client, "bob")  # a member may read
    got = world.client.get("/api/v1/strategies")
    assert got.status_code == 200, got.text
    conforms(PC.page("strategy_version"), got.json())
    missing = [f"none:family/{f}" for f in FAMILIES]
    cover = {"status": "partial", "reasons": missing}
    assert got.json() == {"items": [], "next_cursor": None, "coverage": cover}
    assert_problem(world.client.get("/api/v1/strategies/STR-ALLOC-001"), 404,
                   "not_found")  # fmt: skip


@pytest.mark.db
def test_strategies_conform_and_are_never_action_eligible(
    world: Any, owner: psycopg.Connection[Any], rows: Any
) -> None:
    st.recorded(owner, world.tenant_a)  # six families; tenant A adopts STR-SYNTH-AI
    ss.record_manifest(owner, st.synth("0.2.0", title="SYNTHETIC thesis v2"))
    login(world.client, "alice")
    before = rows()
    first = world.client.get("/api/v1/strategies?limit=4").json()
    conforms(PC.page("strategy_version"), first)
    assert first["coverage"] == {"status": "partial", "reasons": GAPS}
    nxt = world.client.get(f"/api/v1/strategies?cursor={first['next_cursor']}").json()
    assert nxt["next_cursor"] is None
    items = first["items"] + nxt["items"]
    assert [i["id"] for i in items] == sorted(m.strategy_id for m in st.six())
    assert {i["family"] for i in items} == set(FAMILIES)
    for i in items:  # nothing is qualified, approved or eligible
        assert (i["operational_status"], i["evidence_status"], i["action_eligible"],
                i["approved_by"], i["source_decisions"]) == (
            "not_tested", "none", False, None, [])  # fmt: skip
    thesis = world.client.get("/api/v1/strategies/STR-SYNTH-AI")
    assert thesis.status_code == 200, thesis.text
    conforms(PC.schema("strategy_version"), thesis.json())
    m = st.synth("0.2.0")  # the latest version; the adoption binds 0.1.0
    assert thesis.json() == {
        "id": "STR-SYNTH-AI", "family": "ai_thesis", "version": "0.2.0",
        "implementation_hash": "ab" * 32, "configuration_hash": m.config_hash({}),
        "required_feed_spec_ids": ["feed-synth", "feed-synth-b"],
        "policy_requirements": ["ai_mode:ai_enabled", "horizon:swing_position",
                                "instrument:options", "instrument:stocks"],
        "research_protocol_id": "protocol-synth-1", "operational_status": "not_tested",
        "evidence_status": "none", "action_eligible": False, "approved_by": None,
        "source_decisions": [],
    }  # fmt: skip
    orb = world.client.get("/api/v1/strategies/STR-ORB-001").json()
    assert orb["policy_requirements"] == [
        "horizon:intraday",
        "instrument:etfs",
        "instrument:stocks",
        "session:regular",
    ]
    login(world.client, "carol")  # tenant B sees the same installation registry
    assert world.client.get("/api/v1/strategies?limit=4").json()["items"] == first[
        "items"]  # fmt: skip
    for bad in ("x" * 30, tamper(first["next_cursor"])):
        assert_problem(world.client.get(f"/api/v1/strategies?cursor={bad}"), 400,
                       "invalid_cursor")  # fmt: skip
    assert_problem(world.client.get("/api/v1/strategies/STR-NONE"), 404, "not_found")
    assert_problem(world.client.get("/api/v1/strategies/-bad"), 400, "invalid_request")
    adopt = world.client.post("/api/v1/strategies/STR-SYNTH-AI/adopt", json={},
                              headers=csrf(world.client))  # fmt: skip
    assert adopt.status_code == 404, adopt.text  # not served in this increment
    assert rows() == before  # reads write nothing
    world.client.cookies.clear()
    assert_problem(world.client.get("/api/v1/strategies"), 401, "unauthenticated")


@pytest.mark.db
def test_a_tampered_manifest_is_a_crash_not_an_answer(
    world: Any, owner: psycopg.Connection[Any]
) -> None:
    st.recorded(owner, world.tenant_a)
    world.db.execute("SET session_replication_role = replica")  # a tampering owner
    world.db.execute("UPDATE app.strategy_manifest SET body = jsonb_set(body, "
                     "'{title}', '\"forged title\"')")  # fmt: skip
    login(world.client, "alice")
    for path in ("", "/STR-ORB-001"):
        got = world.client.get(f"/api/v1/strategies{path}")
        assert_problem(got, 500, "internal_error")
        assert "forged" not in got.text


@pytest.mark.db
def test_seeded_families_are_listed_not_tested_and_never_eligible(
    world: Any, database_url: str, runtime_urls: dict[str, str], rows: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """`qw-seed-strategies` (owner decision 10): all six, nothing qualified."""
    from qw_api.bootstrap import seed_main

    monkeypatch.delenv("QW_MIGRATE_DATABASE_URL", raising=False)
    assert seed_main([]) == 2
    monkeypatch.setenv("QW_MIGRATE_DATABASE_URL", runtime_urls["qw_app"])
    assert seed_main([]) == 1  # a runtime login is not the schema owner
    assert rows() == [(0,), (0,)]
    monkeypatch.setenv("QW_MIGRATE_DATABASE_URL", database_url)
    assert seed_main([]) == 0
    seeded = rows()
    assert seed_main([]) == 0 and rows() == seeded == [(6,), (0,)]
    login(world.client, "bob")
    got = world.client.get("/api/v1/strategies").json()
    conforms(PC.page("strategy_version"), got)
    assert got["coverage"] == {"status": "partial", "reasons": GAPS}  # no none:family
    items = {i["family"]: i for i in got["items"]}
    assert sorted(items) == sorted(FAMILIES)
    for i in items.values():
        assert (i["version"], i["operational_status"], i["evidence_status"],
                i["action_eligible"], i["approved_by"]) == (
            "0.1.0-research", "not_tested", "none", False, None)  # fmt: skip
        assert all(f.startswith("free-") for f in i["required_feed_spec_ids"])
    thesis = items["ai_thesis"]
    assert (thesis["id"], thesis["required_feed_spec_ids"]) == (
        "STR-AITHESIS-001", ["free-public-filings"])  # fmt: skip
    assert thesis["policy_requirements"] == [
        "ai_mode:ai_enabled", "horizon:long_term", "instrument:stocks"]  # fmt: skip
    assert items["opening_range_breakout"]["required_feed_spec_ids"] == [
        "free-delayed-intraday-bars"]  # fmt: skip
