"""`GET /api/v1/budgets` (listBudgets, T037 increment 6) through TestClient on a
migrated PostgreSQL 16 database as the non-superuser, non-owner, NOBYPASSRLS
qw_app login. Books are written through qw_adapters.budget_store as qw_app, the
gateway's role; the superuser only records the owner's authorizations (as
qw_migrate), writes last month's book with triggers off (0052 opens only the
current month) and tampers. SYNTHETIC tenants, ids and amounts, hand-computed;
times follow the database clock. Bodies are validated strictly against the
spec's `BudgetSummary` inside the C-02 page."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    FormatChecker,
)
from psycopg.types.json import Jsonb
from qw_adapters import budget_store as bs
from qw_adapters import tenancy as tn
from qw_domain.budget import BudgetBook, BudgetCaps, Category, period_of
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.instants import ensure_aware_utc
from referencing import Registry, Resource

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _module("qw_api_tests", ROOT / "apps/api/tests/test_api.py")
PC = _module("qw_project_contract", ROOT / "tools/project_contract.py")
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem = api.login, api.assert_problem
SPEC = ROOT / "docs/spec/contracts"
OPENAPI = yaml.safe_load((SPEC / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource("common.schema.json", Resource.from_contents(
    json.loads((SPEC / "schemas/common.schema.json").read_text())))  # fmt: skip
URL = "/api/v1/budgets"
REF = "SYNTHETIC-owner-auth-1"
CAPS = BudgetCaps(Usd("100"), Usd("20"), REF)
R, IMP, MON, VER = (Category.RESEARCH, Category.IMPROVEMENT, Category.MONITORING,
                    Category.VERIFICATION)  # fmt: skip
POOLS = ["inference", "monitoring", "verification", "research", "improvement"]
SHARED = ("monitoring", "verification", "research")
BOUND = "99999999999999.999999"
pytestmark = pytest.mark.db


def page_errors(body: Any) -> list[tuple[list[Any], str]]:
    page = PC.page("calculation")  # the C-02 page shape, then the spec's item
    page["properties"]["items"]["items"] = PC._local(
        OPENAPI["components"]["schemas"]["BudgetSummary"])  # fmt: skip
    v = Draft202012Validator(page, registry=REGISTRY, format_checker=FormatChecker())
    return [(list(e.absolute_path), e.message) for e in v.iter_errors(body)]


def conforms(body: Any) -> None:
    assert page_errors(body) == []


def clock(c: Any) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def near_month_turn(at: datetime) -> None:
    """The one time skip (as test_budget_sweep): 0052 opens only the current UTC
    month's book, so a test that writes one cannot straddle a month boundary."""
    if period_of(at + timedelta(seconds=60)) != period_of(at):
        pytest.skip(
            "explicit time skip: a UTC month boundary falls within 60 s "
            "of the test's start (0052 opens only the current month)"
        )


def authorize(su: Any, t: uuid.UUID, ref: str = REF) -> None:
    with su.transaction():  # the owner records an authorization
        su.execute("SET LOCAL ROLE qw_migrate")
        su.execute("INSERT INTO app.budget_authorization (tenant_id, "
                   "authorization_ref) VALUES (%s, %s)", (t, ref))  # fmt: skip


def step(app: Any, t: uuid.UUID, fn: Callable[[BudgetBook, datetime], BudgetBook],
         ) -> BudgetBook:  # fmt: skip
    """One domain step on t's current book, installed by qw_app."""
    with tn.tenant_transaction(app, t) as tx:
        at = clock(app)
        cur = bs.load(tx, period_of(at)) or bs.open_book(tx, at, CAPS)
        return bs.install(tx, fn(cur.book, at), cur.book.version).book


def reserve(rid: str, cat: Category, amount: str) -> Callable[..., BudgetBook]:
    def go(b: BudgetBook, at: datetime) -> BudgetBook:
        new, why = b.reserve(rid, cat, Usd(amount), "SYNTHETIC-acct-1", 100, at,
                             at + timedelta(hours=1))  # fmt: skip
        assert why is None, why
        return new

    return go


def dispatch(rid: str) -> Callable[..., BudgetBook]:
    return lambda b, at: b.dispatched(rid, at)


def settle(rid: str, amount: str) -> Callable[..., BudgetBook]:
    return lambda b, at: b.settle(rid, Usd(amount), at)


def run(app: Any, t: uuid.UUID, *steps: Callable[..., BudgetBook]) -> BudgetBook:
    for f in steps:
        b = step(app, t, f)
    return b


def last_month(su: Any, t: uuid.UUID, now: datetime) -> BudgetBook:
    """t's book of last month, never authorized, holding a settled USD2
    verification call; written as the owner with triggers off."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    at = start - timedelta(hours=2)
    b, why = BudgetBook.open(str(t), at, CAPS).reserve(
        "SYNTHETIC-old-1", VER, Usd("3"), "SYNTHETIC-acct-1", 100, at,
        at + timedelta(hours=1))  # fmt: skip
    assert why is None
    b = b.dispatched("SYNTHETIC-old-1", at).settle("SYNTHETIC-old-1", Usd("2"), at)
    b = replace(b, caps=replace(CAPS, authorization_ref=None))
    wire = b.to_wire()
    with su.transaction():
        su.execute("SET LOCAL session_replication_role = replica")
        su.execute("INSERT INTO app.budget_book (tenant_id, scope_id, period, "
                   "version, book, book_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                   (t, str(t), b.period, b.version, Jsonb(wire),
                    bs.book_hash(tn.TenantTx(su, t), wire)))  # fmt: skip
    return b


def get(client: Any, url: str = URL) -> dict[str, Any]:
    got = client.get(url)
    assert got.status_code == 200, got.text
    assert got.headers["content-type"] == "application/json"
    assert got.headers["cache-control"] == "no-store"
    body: dict[str, Any] = got.json()
    conforms(body)
    return body


def walk(client: Any, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    items: list[dict[str, Any]] = []
    reasons: list[str] = []
    url = f"{URL}?limit={limit}"
    for _ in range(30):  # bounded: a cursor that does not advance fails
        if not url:
            break
        body = get(client, url)
        assert len(body["items"]) <= limit
        items, reasons = items + body["items"], reasons + body["coverage"]["reasons"]
        url = f"{URL}?limit={limit}&cursor={body['next_cursor']}" * bool(
            body["next_cursor"])  # fmt: skip
    assert not url
    return items, reasons


def why(period: str, blocked: str | None = None, saturated: bool = False,
        ) -> list[str]:  # fmt: skip
    """A book's page reasons in item order: the shared pool cap on monitoring,
    verification and research, then saturation, then a blocked book."""
    out = []
    for c in POOLS:
        key = f"{period}:{c}"
        if c in SHARED:
            out.append(f"shared:{key}/cap_is_inference_pool")
        if saturated:
            out.append(f"saturated:{key}/totals_at_bound")
        if blocked:
            out.append(f"blocked:{key}/{blocked}")
    return out


def row(t: uuid.UUID, period: str, cat: str, *usd: str) -> dict[str, str]:
    keys = ("cap_usd", "spent_usd", "reserved_usd", "available_usd")
    return {"scope_id": str(t), "period": period, "category": cat,
            **dict(zip(keys, usd, strict=True))}  # fmt: skip


def test_the_page_schema_is_the_spec_item_in_a_closed_c02_page() -> None:
    item = row(uuid.uuid4(), "2026-10", "inference", "100", "0", "0", "100")
    ok = {"items": [item], "next_cursor": None,
          "coverage": {"status": "complete", "reasons": []}}  # fmt: skip
    conforms(ok)
    for bad in ({**item, "saturated": True}, {**item, "spent_usd": "-1"},
                {**item, "cap_usd": 100}, {**item, "available_usd": "1e3"},
                {k: v for k, v in item.items() if k != "reserved_usd"}):  # fmt: skip
        assert page_errors({**ok, "items": [bad]}), bad
    assert page_errors({k: v for k, v in ok.items() if k != "coverage"})


def test_a_tenant_without_a_book_gets_an_empty_page_never_zero_spend(
    world: Any, app_conn: Any
) -> None:
    near_month_turn(clock(app_conn))
    other = world.users["carol"]  # tenant B has a book; tenant A has none
    b_tenant = world.db.execute("SELECT tenant_id FROM app.membership WHERE "
                                "user_id = %s", (other,)).fetchone()[0]  # fmt: skip
    authorize(world.db, b_tenant)
    run(app_conn, b_tenant, reserve("SYNTHETIC-b1", R, "9"))
    login(world.client, "bob")
    empty: dict[str, Any] = {"items": [], "next_cursor": None,
             "coverage": {"status": "complete", "reasons": []}}  # fmt: skip
    assert get(world.client) == empty
    assert get(world.client, f"{URL}?limit=1") == empty
    login(world.client, "carol")
    items, _ = walk(world.client, 50)
    assert {i["scope_id"] for i in items} == {str(b_tenant)}
    assert items[0] == row(b_tenant, period_of(clock(app_conn)), "inference",
                           "100", "0", "9", "91")  # fmt: skip


def test_a_book_across_categories_totals_and_nests_the_improvement_cap(
    world: Any, app_conn: Any
) -> None:
    now = clock(app_conn)
    near_month_turn(now)
    t = world.tenant_a
    authorize(world.db, t)
    b = run(app_conn, t, reserve("SYNTHETIC-r1", R, "80"),
            reserve("SYNTHETIC-i1", IMP, "7"), reserve("SYNTHETIC-v1", VER, "3"),
            dispatch("SYNTHETIC-v1"), settle("SYNTHETIC-v1", "4"),
            reserve("SYNTHETIC-m1", MON, "5"),
            lambda b, at: b.release("SYNTHETIC-m1"))  # fmt: skip
    old = last_month(world.db, t, now)
    # inference: reserved 80 + 7 = 87, spent 4 (an overrun of a USD3 reservation
    # is kept as reported), available 100 - 91 = 9; improvement's own headroom is
    # 20 - 7 = 13, but the inference pool leaves only 9.
    p, q = b.period, old.period
    current = [row(t, p, "inference", "100", "4", "87", "9"),
               row(t, p, "monitoring", "100", "0", "0", "9"),
               row(t, p, "verification", "100", "4", "0", "9"),
               row(t, p, "research", "100", "0", "80", "9"),
               row(t, p, "improvement", "20", "0", "7", "9")]  # fmt: skip
    earlier = [row(t, q, c, "20" if c == "improvement" else "100",
                   "2" if c in ("inference", "verification") else "0", "0",
                   "20" if c == "improvement" else "98") for c in POOLS]  # fmt: skip
    blocked = why(p) + why(q, "spending_not_authorized")
    login(world.client, "bob")  # a tenant_member reads
    first = get(world.client)
    assert first["items"] == current + earlier and first["next_cursor"] is None
    assert first["coverage"] == {"status": "partial", "reasons": blocked}
    for limit in (1, 2, 4, 5, 6, 9, 10, 200):
        assert walk(world.client, limit) == (current + earlier, blocked), limit
    one = get(world.client, f"{URL}?limit=5")  # exactly the current month
    assert one["items"] == current and one["next_cursor"]
    assert one["coverage"] == {"status": "partial", "reasons": why(p)}
    login(world.client, "alice")  # the owner reads the same
    assert walk(world.client, 3) == (current + earlier, blocked)


def test_another_tenants_book_is_invisible_and_cursors_do_not_cross(
    world: Any, app_conn: Any
) -> None:
    near_month_turn(clock(app_conn))
    t = world.tenant_a
    with tn.provision_tenant(app_conn) as tx:
        other = tx.tenant_id
    for x, amount in ((t, "11"), (other, "13")):
        authorize(world.db, x)
        run(app_conn, x, reserve("SYNTHETIC-same-id", R, amount))
    login(world.client, "bob")
    items, _ = walk(world.client, 2)
    assert len(items) == 5 and {i["scope_id"] for i in items} == {str(t)}
    assert items[0]["reserved_usd"] == "11"
    cursor = get(world.client, f"{URL}?limit=2")["next_cursor"]
    login(world.client, "carol")  # tenant B: no book, and A's cursor is refused
    assert get(world.client)["items"] == []
    assert_problem(world.client.get(f"{URL}?cursor={cursor}"), 400, "invalid_cursor")


def test_no_session_is_401_and_nothing_mutates(world: Any) -> None:
    assert_problem(world.client.get(URL), 401, "unauthenticated")
    world.client.cookies.set(api.SESSION_COOKIE, "A" * 43)
    assert_problem(world.client.get(URL), 401, "unauthenticated")
    world.client.cookies.clear()
    login(world.client, "bob")
    token = world.client.cookies[api.SESSION_COOKIE]
    out = world.client.delete("/api/v1/session", headers=api.csrf(world.client))
    assert out.status_code == 204
    world.client.cookies.set(api.SESSION_COOKIE, token)  # revoked
    assert_problem(world.client.get(URL), 401, "unauthenticated")
    world.client.cookies.clear()
    login(world.client, "bob")
    for method in ("post", "put", "patch", "delete"):
        got = world.client.request(method, URL, headers=api.csrf(world.client))
        assert_problem(got, 405, "method_not_allowed")
    for bad in ("limit=0", "limit=201", "limit=x", "cursor=" + "A" * 257):
        assert_problem(world.client.get(f"{URL}?{bad}"), 400, "invalid_request")
    assert_problem(world.client.get(f"{URL}?cursor=bogus"), 400, "invalid_cursor")


def test_a_tampered_row_is_integrity_never_a_page(world: Any, app_conn: Any) -> None:
    near_month_turn(clock(app_conn))
    t = world.tenant_a
    authorize(world.db, t)
    b = run(app_conn, t, reserve("SYNTHETIC-r1", R, "15"))
    login(world.client, "bob")
    assert get(world.client)["items"][0]["reserved_usd"] == "15"
    wire: Any = b.to_wire()
    wire["reservations"][0]["reserved"] = "1"  # the hash no longer matches
    with world.db.transaction():
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute("UPDATE app.budget_book SET book = %s WHERE tenant_id = %s",
                         (Jsonb(wire), t))  # fmt: skip
    body = assert_problem(world.client.get(URL), 409, "integrity")
    # the random correlation id can itself contain "15"; check everything else
    shown = json.dumps({k: v for k, v in body.items() if k != "correlation_id"})
    assert "15" not in shown and str(t) not in json.dumps(body)
    with world.db.transaction():  # a correct hash of another tenant: still refused
        world.db.execute("SET LOCAL session_replication_role = replica")
        world.db.execute(
            "UPDATE app.budget_book SET book = %s, book_hash = %s WHERE "
            "tenant_id = %s",
            (Jsonb(b.to_wire()), bs.book_hash(tn.TenantTx(world.db, uuid.uuid4()),
                                              b.to_wire()), t))  # fmt: skip
    assert_problem(world.client.get(URL), 409, "integrity")


def test_an_over_cap_book_goes_negative_then_saturates_with_a_reason(
    world: Any, app_conn: Any
) -> None:
    near_month_turn(clock(app_conn))
    t = world.tenant_a
    authorize(world.db, t)
    ids = ("SYNTHETIC-o1", "SYNTHETIC-o2", "SYNTHETIC-o3")
    b = run(app_conn, t, *(reserve(i, R, "10") for i in ids),
            *(dispatch(i) for i in ids), settle(ids[0], "150"))  # fmt: skip
    p = b.period
    login(world.client, "bob")
    over = get(world.client)
    # spent 150, reserved 20: available 100 - 170 = -70, exact
    assert over["items"][0] == row(t, p, "inference", "100", "150", "20", "-70")
    assert over["items"][4] == row(t, p, "improvement", "20", "0", "0", "-70")
    assert over["coverage"] == {"status": "partial", "reasons": why(p)}
    huge = "99999999999999"
    run(app_conn, t, settle(ids[1], huge), settle(ids[2], huge))
    sat = get(world.client)
    # spent 150 + 2 x 99999999999999 exceeds UsdBudget's bound: shown at it
    assert sat["items"][0] == row(t, p, "inference", "100", BOUND, "0", "-" + BOUND)
    assert sat["items"][1] == row(t, p, "monitoring", "100", "0", "0", "-" + BOUND)
    assert sat["items"][3] == row(t, p, "research", "100", BOUND, "0", "-" + BOUND)
    assert sat["items"][4] == row(t, p, "improvement", "20", "0", "0", "-" + BOUND)
    assert sat["coverage"] == {"status": "partial",
                               "reasons": why(p, saturated=True)}  # fmt: skip


def test_a_revoked_authorization_ref_is_reported_blocked(
    world: Any, app_conn: Any
) -> None:
    """The book keeps its recorded ref; once the owner deletes that ref (as
    qw_migrate), 0052 refuses every new reservation, and the read says so."""
    near_month_turn(clock(app_conn))
    t = world.tenant_a
    authorize(world.db, t)
    b = run(app_conn, t, reserve("SYNTHETIC-r1", R, "15"))
    assert b.caps.authorization_ref == REF
    login(world.client, "bob")
    live = get(world.client)
    assert live["coverage"]["reasons"] == why(b.period)
    assert live["items"][0] == row(t, b.period, "inference", "100", "0", "15", "85")
    with world.db.transaction():  # the owner revokes it
        world.db.execute("SET LOCAL ROLE qw_migrate")
        world.db.execute("DELETE FROM app.budget_authorization WHERE tenant_id = %s",
                         (t,))  # fmt: skip
    with pytest.raises(bs.BudgetStoreError) as refused:  # 0052 refuses new spend
        run(app_conn, t, reserve("SYNTHETIC-r2", R, "1"))
    assert refused.value.code == "invalid"
    revoked = get(world.client)
    assert revoked["items"] == live["items"]  # the amounts are unchanged
    assert revoked["coverage"] == {"status": "partial", "reasons": why(
        b.period, "spending_authorization_revoked")}  # fmt: skip
    with tn.tenant_transaction(app_conn, t) as tx:  # qw_app reads its own refs
        assert bs.recorded_refs(tx) == frozenset()
    authorize(world.db, t)  # recorded again: the same ref spends again
    assert get(world.client)["coverage"]["reasons"] == why(b.period)
    with tn.provision_tenant(app_conn) as tx:
        other = tx.tenant_id
    authorize(world.db, other, "SYNTHETIC-owner-auth-2")  # never t's (RLS)
    with tn.tenant_transaction(app_conn, t) as tx:
        assert bs.recorded_refs(tx) == frozenset({REF})
    with tn.tenant_transaction(app_conn, other) as tx:
        assert bs.recorded_refs(tx) == frozenset({"SYNTHETIC-owner-auth-2"})
