"""Read routes for the T053 web UI (`GET /accounts`, `/accounts/{id}/snapshot`,
`/proposals`) through TestClient on a migrated PostgreSQL 16 database as the
non-superuser qw_app login. Reuses the T010 API harness and the T026 proposal-store
helpers. All tenants, accounts, instruments, amounts and proposals are SYNTHETIC.
Hand-computed oracle: deposit 10000 CAD and 300 USD, buy 10 at 50.25 with a 4.95
fee: cost 10 x 50.25 + 4.95 = 507.45 CAD, ledger cash 10000 - 507.45 = 9492.55 CAD.
Responses are validated strictly against the project contract 0.2.0 (T008 A-01,
tools/project_contract.py): no schema error, no extra field, and every null in an
amended field is named in the response's `coverage.reasons`."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import journal_store as js
from qw_adapters import tenancy as tn
from qw_api.reads import horizon, position
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.postings import Header, Holding, buy, deposit
from qw_domain.proposals import ProposalState
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]
SCHEMAS = ROOT / "docs/spec/contracts/schemas"


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
ps = _module("qw_pstore_tests", ROOT / "packages/adapters/tests/test_proposal_store.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
COMMON = json.loads((SCHEMAS / "common.schema.json").read_text())
REGISTRY = Registry().with_resource(
    "common.schema.json", Resource.from_contents(COMMON)
)
SYN = InstrumentId(uuid.UUID(int=0x5E))
T0 = datetime(2026, 10, 1, 14, tzinfo=UTC)


def strict(name: str, body: dict[str, Any], reasons: list[str]) -> set[str]:
    """Validate `body` against the amended schema; return its amended nulls, each of
    which must be named by a reason (`<prefix>:<field>` or `<prefix>:<id>/<field>`)."""
    v = Draft202012Validator(PC.schema(name), registry=REGISTRY,
                             format_checker=FormatChecker())  # fmt: skip
    errors = [(list(e.absolute_path), e.message) for e in v.iter_errors(body)]
    assert errors == [], errors
    amended = PC.AMENDMENTS["schemas"][name]
    named = {r.split(":", 1)[1].removeprefix(f"{body['id']}/") for r in reasons}
    nulls = set()
    for path in [*amended["nullable"], *amended.get("active_nullable", [])]:
        node: Any = body
        for part in path.split("/"):
            node = node[part]
        if node is None:
            nulls.add(path)
    assert nulls <= named, nulls - named
    return nulls


def page_conforms(name: str, page: dict[str, Any]) -> None:
    v = Draft202012Validator(PC.page(name), registry=REGISTRY)
    assert [e.message for e in v.iter_errors(page)] == []


def post(w: Any, tenant: uuid.UUID, *events: Any) -> None:
    with (
        tn.runtime_connection(w.url) as c,
        tn.tenant_transaction(c, tenant) as tx,
    ):
        js.post_many(tx, events)


def hdr(account: str, record: str, minutes: int = 0) -> Header:
    at = T0 + timedelta(minutes=minutes)
    return Header(account, at, SourceRef("csv-syn", record))


def seed_journal(w: Any) -> None:
    post(w, w.tenant_a,
         deposit(hdr("acct-syn-1", "D1"), Money.of("10000", "CAD")),
         deposit(hdr("acct-syn-1", "D2", 1), Money.of("300", "USD")),
         buy(hdr("acct-syn-1", "B1", 2), SYN, PositiveQuantity(10), Price("50.25"),
             Money.of("4.95", "CAD")),
         deposit(hdr("acct-syn-2", "D1"), Money.of("1", "CAD")))  # fmt: skip


@pytest.mark.db
def test_accounts_and_snapshot_are_tenant_scoped_and_honest(world: Any) -> None:
    seed_journal(world)
    login(world.client, "bob")  # a member may read
    page = world.client.get("/api/v1/accounts?limit=1")
    assert page.status_code == 200, page.text
    first = page.json()
    assert [a["id"] for a in first["items"]] == ["acct-syn-1"]
    item, coverage = first["items"][0], first["coverage"]
    assert (item["revision"], item["source_ids"], item["cash_basis"]) == (
        4, ["csv-syn"], "unknown")  # fmt: skip
    assert item["owner_tenant_id"] == str(world.tenant_a)
    assert item["name"] is None and coverage["status"] == "partial"
    page_conforms("account", first)
    assert strict("account", item, coverage["reasons"]) == {
        "name", "broker", "account_type", "reporting_currency", "analysis_scope",
        "status"}  # fmt: skip
    empty = world.client.get("/api/v1/accounts?limit=1&cursor=")  # as absent
    assert empty.json()["items"] == first["items"]
    nxt = world.client.get(f"/api/v1/accounts?limit=1&cursor={first['next_cursor']}")
    assert [a["id"] for a in nxt.json()["items"]] == ["acct-syn-2"]
    assert nxt.json()["next_cursor"] is None
    forged = first["next_cursor"][:-2] + ("AA" if first["next_cursor"][-2:] != "AA"
                                          else "BB")  # fmt: skip
    for bad in (forged, "!!", "AAAA"):
        assert_problem(world.client.get(f"/api/v1/accounts?cursor={bad}"), 400,
                       "invalid_cursor")  # fmt: skip
    other_route = world.client.get(f"/api/v1/proposals?cursor={first['next_cursor']}")
    assert_problem(other_route, 400, "invalid_cursor")

    snap = world.client.get("/api/v1/accounts/acct-syn-1/snapshot")
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert (body["account_id"], body["revision"]) == ("acct-syn-1", 4)
    assert body["ledger_cash"] == [{"amount": "9492.55", "currency": "CAD"},
                                   {"amount": "300", "currency": "USD"}]  # fmt: skip
    assert body["positions"] == [{
        "instrument_id": str(SYN.uuid), "quantity": "10",
        "economic_cost": {"amount": "507.45", "currency": "CAD"},
        "lot_coverage": "complete"}]  # fmt: skip
    assert (body["effective_at"], body["freshness_status"]) == (
        "2026-10-01T14:02:00Z", "unknown")  # fmt: skip
    assert body["received_at"].endswith("Z")
    assert (body["available_cash"], body["reconciliation"]) == (None, None)
    reasons = body["coverage"]["reasons"]
    assert strict("account_snapshot", body, reasons) == {
        "reconciliation", "available_cash", "unresolved_ids"}  # fmt: skip

    login(world.client, "carol")  # tenant B
    assert world.client.get("/api/v1/accounts").json() == {
        "items": [], "next_cursor": None,
        "coverage": {"status": "complete", "reasons": []}}  # fmt: skip
    foreign = world.client.get("/api/v1/accounts/acct-syn-1/snapshot")
    unknown = world.client.get("/api/v1/accounts/acct-none/snapshot")
    assert {**assert_problem(foreign, 404, "not_found"), "correlation_id": ""} == {
        **assert_problem(unknown, 404, "not_found"), "correlation_id": ""}  # fmt: skip
    assert_problem(world.client.get("/api/v1/accounts/-bad/snapshot"), 400,
                   "invalid_request")  # fmt: skip
    world.client.cookies.clear()
    assert_problem(world.client.get("/api/v1/accounts"), 401, "unauthenticated")


def test_unknown_cost_stays_unknown() -> None:
    held = Holding("acct-syn-1", SYN, Quantity("3"), None)
    view, why = position(held)
    assert view == {"instrument_id": str(SYN.uuid), "quantity": "3",
                    "economic_cost": None, "lot_coverage": "unknown"}  # fmt: skip
    assert why == [f"unknown:positions/{SYN.uuid}/economic_cost"]


def test_project_contract_amendments_are_strict() -> None:
    """Negative controls for the strict check and the amended active rule."""
    example = ROOT / "docs/spec/contracts/examples/proposal_active_synthetic.valid.json"
    active = {**json.loads(example.read_text()), "qualification": None,
              "final_check_receipt_id": None}  # fmt: skip
    reasons = [f"unavailable:{f}" for f in ("qualification", "final_check_receipt_id")]
    assert strict("proposal", active, reasons) == {
        "qualification", "final_check_receipt_id"}  # fmt: skip
    with pytest.raises(AssertionError):  # an unexplained null
        strict("proposal", active, reasons[1:])
    with pytest.raises(AssertionError):  # an extra field
        strict("proposal", {**active, "extra": 1}, reasons)
    base = json.loads((SCHEMAS / "proposal.schema.json").read_text())
    rejected = Draft202012Validator(base, registry=REGISTRY).iter_errors(active)
    assert any(True for _ in rejected)  # the spec 0.1.0 schema refuses it
    assert horizon("p-1", "long_term") == ("long_term", [])
    assert horizon("p-1", "decade") == (None, ["unknown:p-1/horizon"])


def seed_proposals(w: Any, app_conn: Any) -> tuple[Any, Any]:
    alice = w.users["alice"]
    with tn.tenant_transaction(app_conn, w.tenant_a) as tx:
        session, _ = tn.create_session(tx, alice, timedelta(hours=1))
        tn.mark_step_up(tx, session)
    c = ps.Ctx(w.tenant_a, alice, session, uuid.uuid4())
    ps.adopt(app_conn, c)
    s = ps.snap(c, ps.db_now(w.db))
    ps.run(app_conn, c, "submit", ps.ver(app_conn, c, s, "prop-syn-live"), c.policy, s)
    short = ps.ver(app_conn, c, s, "prop-syn-short", life=timedelta(milliseconds=400))
    ps.run(app_conn, c, "submit", short, c.policy, s)
    return c, s


def events(w: Any) -> int:
    row = w.db.execute("SELECT count(*) FROM app.proposal_event").fetchone()
    return int(row[0])


@pytest.mark.db
def test_proposals_resolve_state_at_read_time_without_writing(
    world: Any, app_conn: Any
) -> None:
    seed_proposals(world, app_conn)
    time.sleep(0.5)  # prop-syn-short is now past its expiry; nothing records it
    before = events(world)
    login(world.client, "bob")
    page = world.client.get("/api/v1/proposals?limit=1").json()
    items, reasons = list(page["items"]), list(page["coverage"]["reasons"])
    assert "not_rechecked:risk_evaluation_and_pauses" in reasons  # the live one
    while page["next_cursor"]:
        page = world.client.get(f"/api/v1/proposals?cursor={page['next_cursor']}")
        page = page.json()
        items, reasons = items + page["items"], reasons + page["coverage"]["reasons"]
    live, short = items
    assert (live["id"], live["state"]) == ("prop-syn-live", "candidate")
    assert live["reason_codes"] == ["RISK_NOT_RECHECKED"]
    assert (short["id"], short["state"]) == ("prop-syn-short", "expired")
    assert short["reason_codes"] == ["EXPIRED"]
    assert live["legs"][0]["estimated_cash_effect"] == {
        "amount": "-4000", "currency": "CAD"}  # fmt: skip
    assert live["binding"]["account_revision"] == 1
    assert live["binding"]["policy_version_id"].endswith(":1")
    assert events(world) == before  # a read records no expiry
    assert "not_rechecked:risk_evaluation_and_pauses" not in page["coverage"]["reasons"]
    page_conforms("proposal", page)
    for item in items:
        found = strict("proposal", item, reasons)
        assert {"sleeve_id", "origin", "qualification"} <= found
        assert item["horizon"] == "long_term"
        assert item["risk_summary"].startswith("Risk evaluation at binding: ")

    post(world, world.tenant_a, deposit(hdr("acct-1", "D9"), Money.of("1", "CAD")))
    again = world.client.get("/api/v1/proposals").json()["items"]
    assert [(i["state"], i["reason_codes"]) for i in again] == [
        (ProposalState.INVALIDATED.value, ["ACCOUNT_CHANGED"]),
        (ProposalState.EXPIRED.value, ["EXPIRED"])]  # fmt: skip
    assert events(world) == before

    login(world.client, "carol")  # tenant B sees none of them
    assert world.client.get("/api/v1/proposals").json()["items"] == []
