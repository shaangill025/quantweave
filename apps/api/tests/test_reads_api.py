"""Read routes for the T053 web UI (`GET /accounts`, `/accounts/{id}/snapshot`,
`/accounts/{id}/ledger`, `/proposals`, `/proposals/{id}`) through TestClient on a
migrated PostgreSQL 16 database as the non-superuser qw_app login. Reuses the T010
API harness and the T026 proposal-store helpers. All tenants, accounts,
instruments, amounts and proposals are SYNTHETIC.
Hand-computed oracle: deposit 10000 CAD and 300 USD, buy 10 at 50.25 with a 4.95
fee: cost 10 x 50.25 + 4.95 = 507.45 CAD, ledger cash 10000 - 507.45 = 9492.55 CAD.
Responses are validated strictly against the project contract 0.4.0 (T008 A-01,
tools/project_contract.py): no schema error, no extra field, and every null in an
amended field is named in the response's `coverage.reasons`."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from qw_adapters import journal_store as js
from qw_adapters import pause_store
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_api import reads
from qw_api.reads import KINDS, horizon, ledger_item, position
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.journal import Entry
from qw_domain.postings import EventKind, Header, Holding, buy, deposit, sell, split
from qw_domain.proposals import ProposalState
from qw_domain.simulation import VirtualPortfolio
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
    reasons = [f"unavailable:{f}" for f in ("qualification", "final_check_receipt_id")]
    active = {**json.loads(example.read_text()), "qualification": None,
              "final_check_receipt_id": None,
              "coverage": {"status": "partial", "reasons": reasons}}  # fmt: skip
    assert strict("proposal", active, reasons) == {
        "qualification", "final_check_receipt_id"}  # fmt: skip
    with pytest.raises(AssertionError):  # an unexplained null
        strict("proposal", active, reasons[1:])
    with pytest.raises(AssertionError):  # an extra field
        strict("proposal", {**active, "extra": 1}, reasons)
    with pytest.raises(AssertionError):  # 0.4.0: a proposal carries its coverage
        strict("proposal", {k: v for k, v in active.items() if k != "coverage"},
               reasons)  # fmt: skip
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
    assert "not_rechecked:risk_evaluation" in reasons  # the live one
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
    assert "not_rechecked:risk_evaluation" not in page["coverage"]["reasons"]
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


def all_rows(w: Any) -> dict[str, int]:
    """Row count of every app table (audit, idempotency and outbox included)."""
    names = w.db.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'app' "
                         "ORDER BY tablename").fetchall()  # fmt: skip
    return {t: w.db.execute(f"SELECT count(*) FROM app.{t}").fetchone()[0]
            for (t,) in names}  # fmt: skip


@pytest.mark.db
def test_read_proposal_matches_the_list_and_is_tenant_scoped(
    world: Any, app_conn: Any
) -> None:
    seed_proposals(world, app_conn)
    time.sleep(0.5)  # prop-syn-short is past its expiry; nothing records it
    login(world.client, "bob")
    before = all_rows(world)
    listed = {i["id"]: i for i in world.client.get("/api/v1/proposals").json()["items"]}
    for pid, state, codes in (("prop-syn-live", "candidate", ["RISK_NOT_RECHECKED"]),
                              ("prop-syn-short", "expired", ["EXPIRED"])):  # fmt: skip
        got = world.client.get(f"/api/v1/proposals/{pid}")
        assert got.status_code == 200, got.text
        body = got.json()
        assert (body["id"], body["state"], body["reason_codes"]) == (pid, state, codes)
        assert body == listed[pid]  # one view for both routes
        reasons = body["coverage"]["reasons"]
        assert body["coverage"]["status"] == "partial"
        assert {"sleeve_id", "origin", "qualification"} <= strict(
            "proposal", body, reasons)  # fmt: skip
        named = [f"unavailable:{f}" for f in (
            "origin", "conditions", "claim_ids", "binding/strategy_version_id",
            "review_id", "final_check_receipt_id", "qualification", "joint_set_id",
        )] + [f"none:{pid}/sleeve_id"]  # fmt: skip
        assert set(named) <= set(reasons)
        rechecked = "not_rechecked:risk_evaluation" in reasons
        assert rechecked is (state == "candidate")
    live = world.client.get("/api/v1/proposals/prop-syn-live").json()
    assert live["legs"][0]["estimated_cash_effect"] == {
        "amount": "-4000", "currency": "CAD"}  # fmt: skip
    assert live["binding"]["account_revision"] == 1
    page_conforms("proposal", world.client.get("/api/v1/proposals").json())
    assert all_rows(world) == before  # reads write nothing, not even an expiry

    unknown = world.client.get("/api/v1/proposals/prop-none")
    login(world.client, "carol")  # tenant B: the same 404 as an unknown id
    foreign = world.client.get("/api/v1/proposals/prop-syn-live")
    assert {**assert_problem(foreign, 404, "not_found"), "correlation_id": ""} == {
        **assert_problem(unknown, 404, "not_found"), "correlation_id": ""}  # fmt: skip
    assert_problem(world.client.get("/api/v1/proposals/-bad"), 400, "invalid_request")


ACCT = "acct-led-1"


def seed_ledger(w: Any) -> dict[str, str]:
    """SYNTHETIC journal, dated back from the database clock. Hand-computed:
    buy 10 x 50.25 + 4.95 fee = 507.45 CAD cost; sell 4 at 60 with a 1.00 fee:
    remaining cost 507.45 x 6/10 = 304.47, allocated 202.98, net proceeds 239,
    realised 239 - 202.98 = 36.02. The 300 USD deposit is corrected to 250 USD."""
    now = ps.db_now(w.db)

    def h(record: str, minutes: int) -> Header:
        return Header(ACCT, now - timedelta(minutes=minutes),
                      SourceRef("csv-syn", record))  # fmt: skip

    d1 = deposit(h("D1", 60), Money.of("10000", "CAD"))
    d2 = deposit(h("D2", 59), Money.of("300", "USD"))
    b1 = buy(h("B1", 58), SYN, PositiveQuantity(10), Price("50.25"),
             Money.of("4.95", "CAD"))  # fmt: skip
    held = Holding(ACCT, SYN, Quantity("10"), Money.of("507.45", "CAD"))
    s1 = sell(h("S1", 57), PositiveQuantity(4), Price("60"), Money.of("1", "CAD"), held)
    post(w, w.tenant_a, d1, d2, b1, s1, deposit(hdr("acct-led-2", "D1"),
         Money.of("1", "CAD")))  # fmt: skip
    fix = replace(deposit(h("D2-fix", 59), Money.of("250", "USD")),
                  supersedes=d2.event_id)  # fmt: skip
    with tn.runtime_connection(w.url) as c, tn.tenant_transaction(c, w.tenant_a) as tx:
        js.correct(tx, fix)
    rev = d2.reversal(fix.source)
    return {"d1": d1.event_id, "d2": d2.event_id, "b1": b1.event_id,
            "s1": s1.event_id, "rev": rev.event_id, "fix": fix.event_id}  # fmt: skip


def ledger(w: Any, query: str = "", account: str = ACCT) -> Any:
    return w.client.get(f"/api/v1/accounts/{account}/ledger{query}")


def walk(w: Any, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    page = ledger(w, f"?limit={limit}").json()
    items, reasons = list(page["items"]), list(page["coverage"]["reasons"])
    while page["next_cursor"]:
        assert len(items) <= 50, "paging does not advance"
        page_conforms("ledger_event", page)
        page = ledger(w, f"?limit={limit}&cursor={page['next_cursor']}").json()
        assert page["items"], "a cursor led to an empty page"
        items, reasons = items + page["items"], reasons + page["coverage"]["reasons"]
    page_conforms("ledger_event", page)
    return items, reasons


def money(account: str, commodity: str, amount: str) -> dict[str, str]:
    return {"book_account": account, "commodity": commodity, "amount": amount}


@pytest.mark.db
def test_ledger_pages_every_event_once_with_hand_computed_postings(
    world: Any,
) -> None:
    ids = seed_ledger(world)
    login(world.client, "bob")
    before = all_rows(world)
    whole, reasons = walk(world, 200)
    order = ("d1", "d2", "b1", "s1", "rev", "fix")
    assert [e["id"] for e in whole] == [ids[k] for k in order]
    for limit in (1, 2, 4, 5, 6):  # every item exactly once, in revision order
        paged, _ = walk(world, limit)
        assert paged == whole
    by = dict(zip(order, whole, strict=True))
    iid, cost = str(SYN.uuid), f"security_cost:{SYN.uuid}"
    assert by["b1"]["postings"] == [
        money("cash", "CAD", "-507.45"), money(cost, "CAD", "507.45"),
        money("position", iid, "10"), money("unit_clearing", iid, "-10")]  # fmt: skip
    assert by["s1"]["postings"] == [
        money("cash", "CAD", "239"), money(cost, "CAD", "-202.98"),
        money(f"realized_pl:{SYN.uuid}", "CAD", "-36.02"),
        money("position", iid, "-4"), money("unit_clearing", iid, "4")]  # fmt: skip
    assert by["s1"]["instrument_movements"] == [{"instrument_id": iid,
                                                 "quantity": "-4"}]  # fmt: skip
    assert by["d1"]["instrument_movements"] == []
    assert by["b1"]["economic_annotations"] == [
        "fee:4.95 CAD", "lot_policy:economic_average_cost/1"]  # fmt: skip
    cash: dict[str, Decimal] = {}
    for e in whole:  # each event balances per commodity; cash by currency
        sums: dict[str, Decimal] = {}
        for p in e["postings"]:
            sums[p["commodity"]] = sums.get(p["commodity"], Decimal(0)) + Decimal(
                p["amount"])  # fmt: skip
            if p["book_account"] == "cash":
                cash[p["commodity"]] = cash.get(p["commodity"], Decimal(0)) + Decimal(
                    p["amount"])  # fmt: skip
        assert set(sums.values()) == {Decimal(0)}, e["id"]
    assert cash == {"CAD": Decimal("9731.55"), "USD": Decimal("250")}
    assert (by["d2"]["status"], by["d2"]["kind"]) == ("reversed", "deposit")
    assert (by["rev"]["kind"], by["rev"]["reverses_event_id"]) == (
        "correction", ids["d2"])  # fmt: skip
    assert by["rev"]["postings"] == [
        money("cash", "USD", "-300"),
        money("external_capital", "USD", "300"),
    ]
    assert by["fix"]["economic_annotations"] == [
        "lot_policy:economic_average_cost/1", f"supersedes:{ids['d2']}"]  # fmt: skip
    assert {e["status"] for k, e in by.items() if k != "d2"} == {"accepted"}
    assert by["fix"]["source_record_id"] == "D2-fix"
    assert f"mapped:{ids['rev']}/kind=reversal->correction" in reasons
    stored = dict(world.db.execute(
        "SELECT event_id, idempotency_key FROM app.ledger_event WHERE account_id = %s",
        (ACCT,)).fetchall())  # fmt: skip
    times = dict(world.db.execute(
        "SELECT event_id, recorded_at FROM app.ledger_event WHERE account_id = %s",
        (ACCT,)).fetchall())  # fmt: skip
    for e in whole:
        assert e["idempotency_key"] == stored[e["id"]]
        assert datetime.fromisoformat(e["recorded_at"]) == times[e["id"]]
        assert (e["account_id"], e["tenant_id"]) == (ACCT, str(world.tenant_a))
        assert e["lot_policy_id"] is None
        assert strict("ledger_event", e, reasons) == {"lot_policy_id"}
    assert all_rows(world) == before  # reads write nothing


@pytest.mark.db
def test_ledger_refuses_foreign_cursors_simulations_and_other_tenants(
    world: Any, app_conn: Any
) -> None:
    seed_ledger(world)
    login(world.client, "bob")
    first = ledger(world, "?limit=2").json()
    cursor = first["next_cursor"]
    assert cursor and len(first["items"]) == 2
    for account in ("acct-led-2",):  # bound to the account's route
        assert_problem(ledger(world, f"?cursor={cursor}", account), 400,
                       "invalid_cursor")  # fmt: skip
    assert_problem(world.client.get(f"/api/v1/accounts?cursor={cursor}"), 400,
                   "invalid_cursor")  # fmt: skip
    sim = VirtualPortfolio.open("SIM-LED", Money.of("500", "USD"), ps.db_now(world.db))
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:
        ss.save(tx, ss.Simulation("SYNTHETIC", "exec-synth-1", "bench-synth-1", sim))
    unknown = assert_problem(ledger(world, account="acct-none"), 404, "not_found")
    simulated = assert_problem(ledger(world, account="SIM-LED"), 404, "not_found")
    assert {**simulated, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    assert_problem(ledger(world, account="-bad"), 400, "invalid_request")
    assert_problem(ledger(world, "?limit=0"), 400, "invalid_request")

    login(world.client, "carol")  # tenant B
    foreign = assert_problem(ledger(world), 404, "not_found")
    assert {**foreign, "correlation_id": ""} == {**unknown, "correlation_id": ""}
    assert_problem(ledger(world, f"?cursor={cursor}"), 400, "invalid_cursor")
    world.client.cookies.clear()
    assert_problem(ledger(world), 401, "unauthenticated")


def test_every_journal_kind_has_a_wire_kind_or_a_reason() -> None:
    held = Holding(ACCT, SYN, Quantity("3"), Money.of("30", "CAD"))
    action = Split("sp-1", 1, SYN, T0.date(), SourceRef("csv-syn", "SP"), T0,
                   PositiveRatio(2, 1))  # fmt: skip
    split_ = split(hdr(ACCT, "SP"), action, held)
    entry = Entry(split_, T0)
    view, why = ledger_item("t-1", entry, set())
    assert (view["kind"], why) == ("split", [])
    iid = str(SYN.uuid)
    assert view["postings"] == [
        money("position", iid, "3"),
        money("corporate_action_clearing", iid, "-3"),
    ]
    assert view["instrument_movements"] == [{"instrument_id": iid, "quantity": "3"}]
    assert {k for k in EventKind if k is not EventKind.REVERSAL} <= set(KINDS)
    rev = Entry(split_.reversal(SourceRef("csv-syn", "SP-x")), T0)
    view, why = ledger_item("t-1", rev, set())
    assert view["kind"] == "correction"
    assert why == [f"mapped:{rev.event.event_id}/kind=reversal->correction"]
    _, gone = ledger_item("t-1", entry, {split_.event_id})
    assert _["status"] == "reversed" and gone == []


@pytest.mark.db
def test_an_unmapped_journal_kind_fails_closed(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future EventKind with no wire kind is a 500, never a dropped or guessed
    event, and the problem body names no kind, id or detail."""
    ids = seed_ledger(world)
    login(world.client, "bob")
    monkeypatch.setattr(reads, "KINDS", {k: v for k, v in reads.KINDS.items()
                                         if k != "deposit"})  # fmt: skip
    monkeypatch.setattr(reads, "MAPPED", {k: v for k, v in reads.MAPPED.items()
                                          if k != "deposit"})  # fmt: skip
    body = assert_problem(ledger(world), 500, "internal_error")
    text = json.dumps(body)
    for leak in ("deposit", "KeyError", ACCT, *ids.values()):
        assert leak not in text, leak
    assert body["field_errors"] == [] and body["reasons"] == []


@pytest.mark.db
def test_a_stored_pause_invalidates_new_risk_until_resumed(
    world: Any, app_conn: Any
) -> None:
    c, _ = seed_proposals(world, app_conn)
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:
        pause_store.pause(tx, c.policy, principal=world.users["bob"], reason="",
                          text="SYNTHETIC pause")  # fmt: skip
    login(world.client, "bob")
    page = world.client.get("/api/v1/proposals").json()
    live = next(i for i in page["items"] if i["id"] == "prop-syn-live")
    assert (live["state"], live["reason_codes"]) == ("invalidated", ["RISK_PAUSED"])
    assert "not_rechecked:risk_evaluation" not in page["coverage"]["reasons"]
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:
        etag = pause_store.tag(1, pause_store.load(tx).active_for(c.policy))
        pause_store.resume(tx, c.policy, etag, principal=c.owner, session_id=c.session,
                           window=timedelta(minutes=10), reason="SYNTHETIC ok",
                           text="SYNTHETIC resume")  # fmt: skip
    page = world.client.get("/api/v1/proposals").json()
    live = next(i for i in page["items"] if i["id"] == "prop-syn-live")
    assert (live["state"], live["reason_codes"]) == (
        "candidate",
        ["RISK_NOT_RECHECKED"],
    )
    assert "not_rechecked:risk_evaluation" in page["coverage"]["reasons"]
    with tn.tenant_transaction(app_conn, world.tenant_a) as tx:  # N1: both reasons
        pause_store.pause(
            tx, c.policy, principal=world.users["bob"], reason="", text="t"
        )
    post(world, world.tenant_a, deposit(hdr("acct-1", "D9"), Money.of("1", "CAD")))
    live = world.client.get("/api/v1/proposals/prop-syn-live").json()
    assert live["reason_codes"] == ["ACCOUNT_CHANGED", "RISK_PAUSED"]
