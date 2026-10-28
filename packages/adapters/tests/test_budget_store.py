"""0052 and qw_adapters.budget_store on a real PostgreSQL 16, as throwaway qw_app,
qw_worker, qw_assessor and qw_release logins (non-superuser, non-owner,
NOBYPASSRLS); the superuser `conn` observes, waits, tampers and, as qw_migrate,
records the owner's authorizations. Tenants, ids, accounts and refs are
SYNTHETIC; every time is the database clock; amounts are hand-computed. Direct
SQL writes as the runtime role inside its tenant transaction with a correct
`book_hash` and version OLD + 1, so only 0052's guard stands in the way."""

from __future__ import annotations

import copy
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb
from qw_adapters import budget_store as bs
from qw_adapters import tenancy as tn
from qw_adapters.migrations import MIGRATIONS_DIR
from qw_domain.budget import (
    MAX_DEADLINE,
    BudgetBook,
    BudgetCaps,
    Category,
    Status,
    period_of,
)
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.instants import ensure_aware_utc, format_instant

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
Wire = dict[str, Any]
REF = "SYNTHETIC-owner-auth-1"
CAPS = BudgetCaps(Usd("100"), Usd("20"), REF)
UNAUTHORIZED = replace(CAPS, authorization_ref=None)
LONG_ID = "S" + "y_.:-9" * 21 + "z"  # 128 characters, every allowed kind


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def record(conn: Conn, t: uuid.UUID, ref: str) -> None:
    """The owner records an authorization (qw_migrate, direct SQL)."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE qw_migrate")
        conn.execute("INSERT INTO app.budget_authorization (tenant_id, "
                     "authorization_ref) VALUES (%s, %s)", (t, ref))  # fmt: skip


@pytest.fixture
def tenant(app_conn: Conn, conn: Conn) -> Callable[..., uuid.UUID]:
    def make(ref: str | None = REF) -> uuid.UUID:
        with tn.provision_tenant(app_conn) as tx:
            t = tx.tenant_id
        if ref is not None:
            record(conn, t, ref)
        return t

    return make


def reserve(book: BudgetBook, rid: str, at: datetime, amount: str = "15",
            category: Category = Category.RESEARCH,
            deadline: datetime | None = None) -> BudgetBook:  # fmt: skip
    new, refusal = book.reserve(rid, category, Usd(amount), "SYNTHETIC-acct-1", 100,
                                at, deadline or at + timedelta(hours=1))  # fmt: skip
    assert refusal is None, refusal
    return new


def call[T](conn: Conn, t: uuid.UUID, fn: Callable[[tn.TenantTx], T]) -> T:
    with tn.tenant_transaction(conn, t) as tx:
        return fn(tx)


def opened(conn: Conn, t: uuid.UUID, at: datetime,
           caps: BudgetCaps = CAPS) -> BudgetBook:  # fmt: skip
    return call(conn, t, lambda tx: bs.open_book(tx, at, caps)).book


def load(conn: Conn, t: uuid.UUID, at: datetime) -> bs.StoredBook | None:
    return call(conn, t, lambda tx: bs.load(tx, period_of(at)))


def install(conn: Conn, t: uuid.UUID, new: BudgetBook, v: int) -> BudgetBook:
    return call(conn, t, lambda tx: bs.install(tx, new, v)).book


def book_v1(conn: Conn, t: uuid.UUID, at: datetime) -> BudgetBook:
    return install(conn, t, reserve(opened(conn, t, at), "SYNTHETIC-r1", at), 0)


def code(fn: Callable[[], object]) -> str:
    with pytest.raises(bs.BudgetStoreError) as exc:
        fn()
    return exc.value.code


def test_round_trip_open_reserve_retry_and_reload(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    t, at = tenant(), clock(app_conn)
    s0 = call(worker_conn, t, lambda tx: bs.open_book(tx, at, CAPS))
    assert (s0.book.scope_id, s0.book.version, s0.book.period) == (
        str(t), 0, period_of(at))  # fmt: skip
    assert call(app_conn, t, lambda tx: bs.open_book(tx, at, CAPS)) == s0
    other = BudgetCaps(Usd("100"), Usd("10"), REF)
    assert code(lambda: opened(app_conn, t, at, other)) == "caps_mismatch"
    b1 = reserve(s0.book, "SYNTHETIC-r1", at, "7", Category.IMPROVEMENT)
    s1 = call(worker_conn, t, lambda tx: bs.install(tx, b1, 0))
    assert s1.book == b1 and s1.book.version == 1
    retry, refusal = s1.book.reserve(
        "SYNTHETIC-r1", Category.IMPROVEMENT, Usd("7"), "SYNTHETIC-acct-1", 100,
        at + timedelta(seconds=1), at + timedelta(hours=2))  # fmt: skip
    assert refusal is None and retry is s1.book  # same terms: the original record
    assert call(app_conn, t, lambda tx: bs.install(tx, retry, 1)) == s1
    loaded = load(app_conn, t, at)
    assert loaded == s1
    r = loaded.book.get("SYNTHETIC-r1")
    assert (r.created_at, r.deadline) == (at, at + timedelta(hours=1))
    imp = loaded.book.summary(Category.IMPROVEMENT)
    assert (imp.reserved.to_wire(), imp.available.to_wire()) == ("7", "13")
    two = reserve(reserve(s1.book, "SYNTHETIC-r2", at), "SYNTHETIC-r3", at)
    assert code(lambda: install(app_conn, t, two, 1)) == "invalid"  # two steps
    old = reserve(s0.book, "SYNTHETIC-r2", at)
    assert code(lambda: install(app_conn, t, old, 0)) == "stale_version"
    late = reserve(s1.book, "SYNTHETIC-r4", clock(app_conn) - timedelta(minutes=2))
    if period_of(late.get("SYNTHETIC-r4").created_at) == period_of(at):
        assert code(lambda: install(worker_conn, t, late, 1)) == "stale_clock"
    foreign = reserve(BudgetBook.open(str(uuid.uuid4()), at, CAPS), "SYN-x", at)
    assert code(lambda: install(app_conn, t, foreign, 0)) == "invalid"
    assert code(lambda: bs.load(tn.TenantTx(app_conn, t), "2026-10")) == (
        "tenant_transaction")  # outside its transaction  # fmt: skip

    def elsewhere(tx: tn.TenantTx) -> object:  # a context of another tenant
        return bs.load(tn.TenantTx(tx.conn, uuid.uuid4()), "2026-10")

    assert code(lambda: call(app_conn, t, elsewhere)) == "tenant_transaction"


def test_rls_isolates_tenants(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    a, b, at = tenant(), tenant(REF), clock(app_conn)
    a1 = book_v1(worker_conn, a, at)
    assert load(worker_conn, b, at) is None
    assert opened(worker_conn, b, at).scope_id == str(b)  # B's own, same ref name
    sa = load(worker_conn, a, at)
    assert sa is not None and sa.book == a1
    with tn.tenant_transaction(worker_conn, b):
        cur = worker_conn.execute("UPDATE app.budget_book SET book_hash = book_hash "
                                  "WHERE tenant_id = %s", (a,))  # fmt: skip
        assert cur.rowcount == 0
        seen = worker_conn.execute("SELECT tenant_id FROM app.budget_book")
        assert [r[0] for r in seen.fetchall()] == [b]
        refs = worker_conn.execute("SELECT tenant_id FROM app.budget_authorization")
        assert [r[0] for r in refs.fetchall()] == [b]
    with (pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
          tn.tenant_transaction(worker_conn, b)):  # fmt: skip
        _insert(worker_conn, a, BudgetBook.open(str(a), at, UNAUTHORIZED).to_wire())
    assert conn.execute("SELECT count(*) FROM app.budget_book").fetchone() == (2,)


def test_concurrent_compare_and_set_one_winner_then_the_caps_hold(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """Round 1: 8 worker connections load version 0 behind a barrier and each
    installs its own USD15 reservation: exactly one wins. Round 2: 12 more retry
    on `stale_version` until resolved: 85 / 15 = 5 granted (USD90 in all), 7
    refused `inference_cap`; reserved + spent never exceeds USD100."""
    t, at = tenant(), clock(app_conn)
    url = runtime_urls["qw_worker"]
    opened(app_conn, t, at)
    first, second = threading.Barrier(8), threading.Barrier(12)

    def once(i: int) -> str:
        with psycopg.connect(url, autocommit=True) as c, \
                tn.tenant_transaction(c, t) as tx:  # fmt: skip
            cur = bs.load(tx, period_of(at))
            assert cur is not None and cur.book.version == 0
            new = reserve(cur.book, f"SYNTHETIC-p{i}", at)
            first.wait(timeout=30)
            try:
                bs.install(tx, new, 0)
            except bs.BudgetStoreError as exc:
                return exc.code
            return "won"

    def until_resolved(i: int) -> tuple[str, int]:
        stale = 0
        with psycopg.connect(url, autocommit=True) as c:
            second.wait(timeout=30)
            for _ in range(200):
                try:
                    with tn.tenant_transaction(c, t) as tx:
                        cur = bs.load(tx, period_of(at))
                        assert cur is not None
                        new, refusal = cur.book.reserve(
                            f"SYNTHETIC-q{i}", Category.RESEARCH, Usd("15"),
                            "SYNTHETIC-acct-1", 100, at,
                            at + timedelta(hours=1))  # fmt: skip
                        if refusal is not None:
                            return refusal.code, stale
                        bs.install(tx, new, cur.book.version)
                        return "granted", stale
                except bs.BudgetStoreError as exc:
                    assert exc.code == "stale_version"
                    stale += 1
        raise AssertionError("unresolved")

    with ThreadPoolExecutor(8) as ex:
        assert sorted(ex.map(once, range(8))) == ["stale_version"] * 7 + ["won"]
    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(until_resolved, range(12)))
    assert sorted(c for c, _ in results) == ["granted"] * 5 + ["inference_cap"] * 7
    assert sum(n for _, n in results) > 0  # the writers really collided
    final = load(app_conn, t, at)
    assert final is not None and final.book.version == 6
    s = final.book.summary()
    assert ((s.reserved + s.spent).to_wire(), len(final.book.reservations)) == (
        "90", 6)  # fmt: skip


def test_the_guarded_update_refuses_a_writer_that_lost_the_row(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, conn: Conn,
    runtime_urls: dict[str, str],
) -> None:  # fmt: skip
    """B loads version 0 while A's install is uncommitted; B's `UPDATE ... WHERE
    version = 0` waits on A's row lock and, once A commits, matches no row."""
    t, at = tenant(), clock(app_conn)
    url = runtime_urls["qw_worker"]
    b0 = opened(app_conn, t, at)

    def b_side() -> str:
        with psycopg.connect(url, autocommit=True) as c, \
                tn.tenant_transaction(c, t) as tx:  # fmt: skip
            cur = bs.load(tx, period_of(at))
            assert cur is not None and cur.book.version == 0
            try:
                bs.install(tx, reserve(cur.book, "SYNTHETIC-b", at), 0)
            except bs.BudgetStoreError as exc:
                return exc.code
            return "won"

    with psycopg.connect(url, autocommit=True) as a, ThreadPoolExecutor(1) as ex:
        with tn.tenant_transaction(a, t) as ta:
            bs.install(ta, reserve(b0, "SYNTHETIC-a", at), 0)
            fut, row = ex.submit(b_side), None
            for _ in range(400):
                row = conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type "
                    "= 'Lock' AND datname = current_database()").fetchone()  # fmt: skip
                if row == (1,):
                    break
                time.sleep(0.025)
            assert row == (1,), "B never waited on A's row"
        assert fut.result(timeout=30) == "stale_version"
    final = load(app_conn, t, at)
    assert final is not None
    assert [r.reservation_id for r in final.book.reservations] == ["SYNTHETIC-a"]


@pytest.mark.parametrize("tamper", ["amount", "rehashed_non_canonical", "version"])
def test_a_tampered_row_is_integrity(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, conn: Conn, tamper: str
) -> None:
    t, at = tenant(), clock(app_conn)
    wire = book_v1(app_conn, t, at).to_wire()
    rows: Any = wire["reservations"]
    rows[0]["reserved"] = "1" if tamper == "amount" else "15.0"
    h = bs.book_hash(tn.TenantTx(conn, t), wire) if tamper != "amount" else None
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")  # no triggers
        if tamper == "version":  # the row's version, not the book's
            conn.execute("ALTER TABLE app.budget_book DROP CONSTRAINT "
                         "budget_book_wire")  # fmt: skip
            conn.execute("UPDATE app.budget_book SET version = 7")
        else:
            conn.execute("UPDATE app.budget_book SET book = %s, "
                         "book_hash = coalesce(%s, book_hash)",
                         (Jsonb(wire), h))  # fmt: skip
    assert code(lambda: load(app_conn, t, at)) == "integrity"


def test_roles_without_grants_are_refused(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, assessor_conn: Conn,
    release_conn: Conn, worker_conn: Conn,
) -> None:  # fmt: skip
    t, at = tenant(), clock(app_conn)
    book_v1(app_conn, t, at)
    for c in (assessor_conn, release_conn):
        assert code(partial(load, c, t, at)) == "forbidden"
        assert code(partial(opened, c, tenant(), at)) == "forbidden"
    for c in (app_conn, worker_conn):
        for stmt in ("DELETE FROM app.budget_book", "TRUNCATE app.budget_book",
                     "UPDATE app.budget_book SET scope_id = 'SYNTHETIC-x'",
                     "UPDATE app.budget_book SET tenant_id = tenant_id",
                     "UPDATE app.budget_book SET updated_at = now()",
                     "INSERT INTO app.budget_authorization (tenant_id, "
                     "authorization_ref) VALUES (app.current_tenant_id(), 'SYN-y')",
                     "UPDATE app.budget_authorization SET authorization_ref = 'Z'",
                     "DELETE FROM app.budget_authorization",
                     "TRUNCATE app.budget_authorization"):  # fmt: skip
            with pytest.raises(errors.InsufficientPrivilege), \
                    tn.tenant_transaction(c, t):  # fmt: skip
                c.execute(stmt)


def test_only_an_owner_recorded_ref_authorizes_and_only_from_null(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant("SYNTHETIC-owner-auth-other")  # another tenant's ref
    at = clock(app_conn)
    t = tenant(None)  # the owner has recorded nothing for t yet
    for ref in ("bogus-x", "SYNTHETIC-owner-auth-other", REF):  # unrecorded for t
        caps = replace(CAPS, authorization_ref=ref)
        assert code(partial(opened, app_conn, t, at, caps)) == "invalid"
    b0 = opened(worker_conn, t, at, UNAUTHORIZED)  # opens; nothing can spend
    assert code(lambda: call(worker_conn, t, lambda tx: bs.authorize(
        tx, period_of(at), REF))) == "invalid"  # fmt: skip
    long_caps = replace(CAPS, authorization_ref=LONG_ID)  # the longest ref
    record(conn, t, LONG_ID)
    record(conn, t, "SYNTHETIC-owner-auth-2")
    b1 = opened(worker_conn, t, at, long_caps)  # null -> the recorded ref, a step
    assert (b1.caps, b1.version) == (long_caps, 1)
    assert code(lambda: call(worker_conn, t, lambda tx: bs.authorize(
        tx, period_of(at), "SYNTHETIC-owner-auth-2"))) == "caps_mismatch"  # fmt: skip
    to2 = BudgetCaps(Usd("100"), Usd("20"), "SYNTHETIC-owner-auth-2").to_wire()
    _refused(worker_conn, t, _next(b1) | {"caps": to2}, "version")  # valid -> other
    _refused(worker_conn, t, _next(b0) | {"version": 2}, "version")  # back to null
    b2 = install(worker_conn, t, reserve(b1, "SYNTHETIC-r1", at), 1)
    b3 = install(worker_conn, t, b2.dispatched("SYNTHETIC-r1", at), 2)
    with conn.transaction():  # the owner revokes: settling still records spend,
        conn.execute("SET LOCAL ROLE qw_migrate")  # but nothing new is reserved
        conn.execute("DELETE FROM app.budget_authorization WHERE tenant_id = %s", (t,))
    b4 = install(worker_conn, t, b3.settle("SYNTHETIC-r1", Usd("2"), at), 3)
    _refused(worker_conn, t, _next(b4, _added()), "reservation")


# --- direct SQL as the runtime role ---


def _insert(conn: Conn, t: uuid.UUID, wire: Wire) -> None:
    """Inside the caller's tenant transaction."""
    conn.execute(
        "INSERT INTO app.budget_book (tenant_id, scope_id, period, version, book, "
        "book_hash) VALUES (%s, %s, %s, %s, %s, %s)",
        (t, wire["scope_id"], wire["period"], wire["version"], Jsonb(wire),
         bs.book_hash(tn.TenantTx(conn, t), wire)))  # fmt: skip


def _raw(conn: Conn, t: uuid.UUID, wire: Wire) -> None:
    with tn.tenant_transaction(conn, t) as tx:
        conn.execute(
            "UPDATE app.budget_book SET version = %s, book = %s, book_hash = %s",
            (wire["version"], Jsonb(wire), bs.book_hash(tx, wire)))  # fmt: skip


def _next(b: BudgetBook, edit: Callable[[list[Wire]], object] | None = None, /,
          **first: object) -> Wire:  # fmt: skip
    """b's wire at version + 1, `first` set on its first reservation, `edit`
    applied to the reservation list."""
    w: Wire = copy.deepcopy(b.to_wire()) | {"version": b.version + 1}
    if first:
        w["reservations"][0].update(first)
    if edit is not None:
        edit(w["reservations"])
    return w


def _refused(conn: Conn, t: uuid.UUID, wire: Wire, why: str) -> None:
    with pytest.raises(errors.CheckViolation, match=rf"budget_book: (?:{why})(?!\w)"):
        _raw(conn, t, wire)


def _added(**fields: object) -> Callable[[list[Wire]], object]:
    return lambda rows: rows.append(rows[0] | {"reservation_id": "SYNTHETIC-new",
                                               "status": "reserved", "actual": None,
                                               "dispatched": False, "sends": 0,
                                               "returned": 0} | fields)  # fmt: skip


def test_direct_sql_cannot_move_the_version_or_rewrite_history(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    t, at = tenant(), clock(app_conn)
    b1 = book_v1(app_conn, t, at)  # one open, undispatched USD15 reservation
    _refused(worker_conn, t, BudgetBook.open(str(t), at, CAPS).to_wire(), "version")
    for v in (3, 1):  # skipped, unchanged
        _refused(worker_conn, t, _next(b1) | {"version": v}, "version")
    _refused(worker_conn, t, _next(b1, lambda rows: rows.clear()), "history")
    for change in ({"reserved": "1"}, {"deadline": "2099-01-01T00:00:00Z"},
                   {"category": "monitoring"}, {"max_output_tokens": 101},
                   {"reservation_id": "SYNTHETIC-r2"}):  # fmt: skip
        _refused(worker_conn, t, _next(b1, **change), "transition|history")
    _refused(worker_conn, t, _next(b1, lambda rows: rows.append(dict(rows[0]))),
             "duplicate")  # fmt: skip
    with pytest.raises(errors.CheckViolation, match="budget_book_wire"):
        _raw(worker_conn, t, _next(b1) | {"x": 1})
    after = load(app_conn, t, at)
    assert after is not None and after.book == b1


STEPS = [  # (dispatched first, change, refusal)
    (False, {"status": "settled", "actual": "0"}, "transition"),  # never dispatched
    (False, {"status": "settled", "actual": "0", "dispatched": True}, "transition"),
    (False, {"status": "expired", "actual": "15"}, "transition"),
    (False, {"status": "released", "sends": 1}, "transition"),
    (False, {"sends": 1}, "transition"),  # a send without a dispatch
    (False, {"returned": 1}, "transition"),  # a return without a send
    (False, {"dispatched": True, "sends": 2}, "transition"),
    (False, {"dispatched": True, "sends": 1, "returned": 1}, "transition"),
    (False, {"actual": "0"}, "reservation_wire"),  # an actual on an open reservation
    (False, {"period": "2020-01"}, "reservation_wire"),
    (True, {"status": "released"}, "transition"),  # a sent call may still bill
    (True, {"status": "expired", "actual": "15"}, "transition"),  # before the deadline
    (True, {"status": "expired", "actual": "0"}, "transition"),
    (True, {"dispatched": False}, "transition"),
    (True, {"sends": 2}, "transition"),  # two sends in one step
    (True, {"status": "settled", "actual": "-1000"}, "reservation_wire"),
    (True, {"status": "settled", "actual": "1e1"}, "reservation_wire"),
    (True, {"status": "settled", "actual": None}, "reservation_wire"),
    (True, {"status": "settled", "actual": "0.5", "sends": 1}, "transition"),
]


@pytest.mark.parametrize(("dispatched", "first", "why"), STEPS)
def test_direct_sql_refuses_what_no_domain_step_makes(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn,
    dispatched: bool, first: Wire, why: str,
) -> None:  # fmt: skip
    t, at = tenant(), clock(app_conn)
    b = book_v1(app_conn, t, at)
    if dispatched:
        b = install(app_conn, t, b.dispatched("SYNTHETIC-r1", at), 1)
    _refused(worker_conn, t, _next(b, **first), why)
    if not dispatched:  # the domain's release
        _raw(worker_conn, t, _next(b, status="released"))
        return
    _raw(worker_conn, t, _next(b, status="settled", actual="100"))  # a settle
    s = load(app_conn, t, at)
    assert s is not None and s.book.summary().spent.to_wire() == "100"
    for again in ({"status": "reserved", "actual": None}, {"actual": "0.25"},
                  {"status": "expired", "actual": "15"}):  # fmt: skip
        _refused(worker_conn, t, _next(s.book, **again), "transition")


def test_send_steps_follow_the_domain(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    t, at = tenant(), clock(app_conn)
    b = install(app_conn, t, book_v1(app_conn, t, at).begin_send(
        "SYNTHETIC-r1", at, 5), 1)  # one send in flight  # fmt: skip
    for bad in ({"sends": 2}, {"returned": 2}, {"dispatched": False},
                {"status": "released"}):  # fmt: skip
        _refused(worker_conn, t, _next(b, **bad), "transition")
    b = install(app_conn, t, b.end_send("SYNTHETIC-r1"), 2)
    for bad in ({"sends": 0, "returned": 0}, {"returned": 2}, {"sends": 3}):
        _refused(worker_conn, t, _next(b, **bad), "transition")
    _raw(worker_conn, t, _next(b, sends=2))  # the next begin_send, by raw SQL
    got = load(app_conn, t, at)
    assert got is not None and got.book.get("SYNTHETIC-r1").sends == 2


WIRE = "reservation_wire"
BAD_NEW = [
    *(({"reserved": v}, WIRE) for v in ("-1000", "0", "1e1", "NaN", "15.0",
                                        "0.0000001", 10)),  # 10: a JSON number
    *(({k: v}, WIRE) for k, v in (("category", "bogus"), ("period", "2099-01"),
        ("scope_id", "SYNTHETIC-other"), ("provider_account_ref", "a b"),
        ("provider_account_ref", "SYNTHETIC-" + chr(0xFF41)),  # non-ASCII
        ("reservation_id", "S" * 129), ("max_output_tokens", 0),
        ("max_output_tokens", 1.5))),
    *(({"deadline": v}, WIRE) for v in ("+zero-fraction", "+24:00", "+:60",
                                        "2026-02-30T00:00:00Z", "+offset")),
    ({"created_at": "2020-01-01T00:00:00Z"}, "reservation"),
    ({"created_at": "2999-01-01T00:00:00Z"}, "reservation"),  # in the future
    ({"created_at": "+61s"}, "stale_clock"),
    ({"deadline": "2000-01-01T00:00:00Z"}, "reservation"),  # before creation
    ({"deadline": "+max+1us"}, "reservation"),  # beyond MAX_DEADLINE
    ({"status": "settled", "actual": "1", "dispatched": True}, "reservation"),
    ({"reserved": "86"}, "cap"),  # 15 + 86 > 100
    ({"reserved": "21", "category": "improvement"}, "cap"),  # > 20
    # edge forms 0052 accepts (why None): each must still load
    *(({"reserved": v}, None) for v in ("0.000001", "84.999999")),
    *(({k: LONG_ID}, None) for k in ("reservation_id", "provider_account_ref")),
    *(({"max_output_tokens": v}, None) for v in (1, 999999999)),
    *(({"category": v}, None) for v in ("monitoring", "verification")),
    ({"category": "improvement", "reserved": "20"}, None),
    *(({"created_at": v}, None) for v in ("+1us", "+100000us")),
    *(({"deadline": v}, None) for v in ("+max", "+max-whole")),
]  # fmt: skip


def _form(value: object, now: datetime, created: datetime) -> object:
    """A relative instant (`+...`) made concrete; `created` is a whole second."""
    if not isinstance(value, str) or not value.startswith("+"):
        return value
    hour, us = format_instant(created + timedelta(hours=1))[:-1], timedelta(0, 0, 1)
    return {"+zero-fraction": hour + ".000000Z", "+offset": hour + "+00:00",
            "+24:00": f"{created.date()}T24:00:00Z",
            "+:60": f"{created.date()}T23:59:60Z", "+61s": now - timedelta(0, 61),
            "+max+1us": created + MAX_DEADLINE + us, "+max": created + MAX_DEADLINE,
            "+max-whole": created + MAX_DEADLINE - timedelta(seconds=1),
            "+1us": created + us,
            "+100000us": created + 100000 * us}[value]  # fmt: skip


@pytest.mark.parametrize(("fields", "why"), BAD_NEW)
def test_direct_sql_refuses_a_bad_new_reservation_and_loads_every_edge(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn,
    fields: Wire, why: str | None,
) -> None:  # fmt: skip
    t, at = tenant(), clock(app_conn)
    b1 = book_v1(app_conn, t, at)
    now = clock(app_conn)
    created = (now - timedelta(seconds=2)).replace(microsecond=0)
    forms = {k: _form(v, now, created) for k, v in fields.items()}
    wire = _next(b1, _added(**{"created_at": format_instant(created)} | {
        k: format_instant(v) if isinstance(v, datetime) else v
        for k, v in forms.items()}))  # fmt: skip
    if why is None:  # the SQL accepts it, so the domain must load it
        _raw(worker_conn, t, wire)
        got = load(app_conn, t, at)
        assert got is not None and got.book.to_wire() == wire
        return
    _refused(worker_conn, t, wire, why)
    _raw(worker_conn, t, _next(b1, _added(reserved="85")))  # to exactly USD100
    got = load(app_conn, t, at)
    assert got is not None and got.book.summary().available.to_wire() == "0"


def test_one_configured_book_per_tenant_and_month(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    t, at = tenant(), clock(app_conn)
    sizes = (("1000000", "1000000"), ("1000", "20"), ("100", "10"))
    bad = [BudgetCaps(Usd(i), Usd(m), REF) for i, m in sizes]
    for caps in bad:
        with pytest.raises(errors.CheckViolation, match="budget_book_caps"), \
                tn.tenant_transaction(worker_conn, t):  # fmt: skip
            _insert(worker_conn, t, BudgetBook.open(str(t), at, caps).to_wire())
    good = BudgetBook.open(str(t), at, CAPS).to_wire()
    for wire, why in ((good | {"period": "2099-01"}, "not_opened"),
                      (good | {"version": 5}, "not_opened"),
                      (good | {"scope_id": "SYNTHETIC-s2"}, "budget_book_scope"),
                      (good | {"caps": replace(CAPS, authorization_ref="bogus-x")
                               .to_wire()}, "authorization")):  # fmt: skip
        with pytest.raises(errors.CheckViolation, match=why), \
                tn.tenant_transaction(worker_conn, t):  # fmt: skip
            _insert(worker_conn, t, wire)
    assert code(lambda: opened(app_conn, t, at, bad[0])) == "invalid"
    opened(worker_conn, t, at)
    with pytest.raises(errors.UniqueViolation), tn.tenant_transaction(worker_conn, t):
        _insert(worker_conn, t, good)


def test_expire_due_sweeps_at_the_database_clock_in_one_step(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    """r1 dispatched and r2 undispatched, both past a deadline 5 s out: early or
    wrong-actual raw expiries are refused; one sweep charges r1 its full USD15
    and releases r2, at version + 1; a second sweep changes nothing; neither id
    can be reserved again; a late report settles r1."""
    t, at = tenant(), clock(app_conn)
    deadline = at + timedelta(seconds=5)
    if period_of(deadline) != period_of(at):
        pytest.skip("the UTC month turns within the test's five seconds")
    b = opened(worker_conn, t, at)
    for step in (lambda x: reserve(x, "SYNTHETIC-r1", at, deadline=deadline),
                 lambda x: reserve(x, "SYNTHETIC-r2", at, "5", deadline=deadline),
                 lambda x: x.dispatched("SYNTHETIC-r1", at)):  # fmt: skip
        b = install(worker_conn, t, step(b), b.version)
    _refused(worker_conn, t, _next(b, status="expired", actual="15"), "transition")
    while clock(app_conn) < deadline:
        time.sleep(0.1)
    for bad in ({"status": "expired", "actual": "0"}, {"sends": 1}):
        _refused(worker_conn, t, _next(b, **bad), "transition")
    swept = call(worker_conn, t, lambda tx: bs.expire_due(tx, period_of(at)))
    assert swept is not None and swept.book.version == 4
    r1, r2 = swept.book.get("SYNTHETIC-r1"), swept.book.get("SYNTHETIC-r2")
    assert (r1.status, r1.actual, r1.created_at, r1.deadline) == (
        Status.EXPIRED, Usd("15"), at, deadline)  # fmt: skip
    assert (r2.status, r2.actual) == (Status.RELEASED, None)
    assert swept.book.summary().spent.to_wire() == "15"
    assert call(worker_conn, t, lambda tx: bs.expire_due(tx, period_of(at))) == swept
    for rid in ("SYNTHETIC-r1", "SYNTHETIC-r2"):
        _, refusal = swept.book.reserve(rid, Category.RESEARCH, Usd("15"),
            "SYNTHETIC-acct-1", 100, at, at + timedelta(hours=1))  # fmt: skip
        assert refusal is not None and refusal.code == "reservation_closed"
    late = swept.book.settle("SYNTHETIC-r1", Usd("4"), clock(app_conn))
    assert install(worker_conn, t, late, 4).summary().spent.to_wire() == "4"
    assert call(worker_conn, tenant(), lambda tx: bs.expire_due(
        tx, period_of(at))) is None  # fmt: skip


def test_a_reported_overrun_is_kept_and_blocks_new_reservations(
    tenant: Callable[..., uuid.UUID], app_conn: Conn, worker_conn: Conn
) -> None:
    """An overrun is kept as reported, even an absurd one: it then locks the
    book for the month (fails closed; no spend record is lost)."""
    t, at = tenant(), clock(app_conn)
    b = install(app_conn, t, reserve(book_v1(app_conn, t, at), "SYNTHETIC-r2", at,
                                     "85"), 1)  # USD100 reserved  # fmt: skip
    b = install(app_conn, t, b.dispatched("SYNTHETIC-r1", at), 2)
    huge = Usd("99999999999999")
    b = install(worker_conn, t, b.settle("SYNTHETIC-r1", huge, at), 3)
    assert b.summary().available.to_wire() == "-99999999999984"  # 100 - 85 - huge
    _refused(worker_conn, t, _next(b, _added(reserved="1")), "cap")


def test_max_deadline_is_one_bound_in_the_domain_and_0052() -> None:
    # no config bounds the gateway's at + wall x attempts + grace: the domain's
    # reserve refuses a later deadline (`deadline_too_far`); 0052 matches it
    sql = MIGRATIONS_DIR.joinpath("0052_budget_book.sql").read_text()
    line = "max_deadline CONSTANT interval := interval '7 days';  -- MAX_DEADLINE"
    assert line in sql and sql.count("interval '7 days'") == 1
    assert timedelta(days=7) == MAX_DEADLINE
