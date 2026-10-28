"""The `budget.expire_due` job (T037 increment 4) and its timer (increment 5,
owner decision 100: 0054's lister) on a real PostgreSQL 16: the
sweep runs as the non-owner, NOBYPASSRLS qw_worker login in each tenant's
transaction; qw_app is the other writer; the superuser `conn` records the
owner's authorizations (as qw_migrate) and observes lock waits. Tenants, ids,
accounts and refs are SYNTHETIC; times are the database clock; amounts are
hand-computed in the comments."""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from qw_adapters import budget_store as bs
from qw_adapters import jobs as qj
from qw_adapters import tenancy as tn
from qw_adapters.jobs import ClaimedJob
from qw_domain.budget import BudgetBook, BudgetCaps, Category, Status, period_of
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import Backoff, JobState, Priority
from qw_worker import budget_sweep as sw
from qw_worker.config import WorkerSettings
from qw_worker.loop import HandlerFailed, Timer, Worker

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness_sweep", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn, worker_conn = _harness.app_conn, _harness.worker_conn
assessor_conn, release_conn = _harness.assessor_conn, _harness.release_conn

Conn = psycopg.Connection[Any]
REF = "SYNTHETIC-owner-auth-1"
CAPS = BudgetCaps(Usd("100"), Usd("20"), REF)
NOW = Backoff(timedelta(0), timedelta(0))
SETTINGS = WorkerSettings("synthetic-sweep", job_backoff=NOW)  # standard pool
R, IMP, MON = Category.RESEARCH, Category.IMPROVEMENT, Category.MONITORING


def clock(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def tenant(app: Conn, su: Conn) -> uuid.UUID:
    with tn.provision_tenant(app) as tx:
        t = tx.tenant_id
    with su.transaction():  # the owner records an authorization
        su.execute("SET LOCAL ROLE qw_migrate")
        su.execute("INSERT INTO app.budget_authorization (tenant_id, "
                   "authorization_ref) VALUES (%s, %s)", (t, REF))  # fmt: skip
    return t


def call[T](c: Conn, t: uuid.UUID, fn: Callable[[tn.TenantTx], T]) -> T:
    with tn.tenant_transaction(c, t) as tx:
        return fn(tx)


def step(c: Conn, t: uuid.UUID, fn: Callable[[BudgetBook], BudgetBook],
         at: datetime) -> BudgetBook:  # fmt: skip
    """One domain step on t's book of `at`'s month, installed by `c`."""

    def go(tx: tn.TenantTx) -> BudgetBook:
        cur = bs.load(tx, period_of(at)) or bs.open_book(tx, at, CAPS)
        return bs.install(tx, fn(cur.book), cur.book.version).book

    return call(c, t, go)


def reserved(rid: str, cat: Category, amount: str, at: datetime,
             deadline: datetime) -> Callable[[BudgetBook], BudgetBook]:  # fmt: skip
    def go(b: BudgetBook) -> BudgetBook:
        new, why = b.reserve(rid, cat, Usd(amount), "SYNTHETIC-acct-1", 100, at,
                             deadline)  # fmt: skip
        assert why is None, why
        return new

    return go


def book(c: Conn, t: uuid.UUID, at: datetime) -> BudgetBook:
    stored = call(c, t, lambda tx: bs.load(tx, period_of(at)))
    assert stored is not None
    return stored.book


def due_book(app: Conn, t: uuid.UUID, at: datetime, deadline: datetime,
             later: datetime) -> BudgetBook:  # fmt: skip
    """r1 research USD15 dispatched and r2 improvement USD20 undispatched, due at
    `deadline`; r3 research USD10 dispatched and r4 monitoring USD5
    undispatched, due at `later`: USD50 reserved, improvement full (20 of 20)."""
    for f in (reserved("SYNTHETIC-r1", R, "15", at, deadline),
              reserved("SYNTHETIC-r2", IMP, "20", at, deadline),
              reserved("SYNTHETIC-r3", R, "10", at, later),
              reserved("SYNTHETIC-r4", MON, "5", at, later),
              lambda b: b.dispatched("SYNTHETIC-r1", at),
              lambda b: b.dispatched("SYNTHETIC-r3", at)):  # fmt: skip
        b = step(app, t, f, clock(app))
    return b


def wait_until(c: Conn, deadline: datetime) -> None:
    while clock(c) < deadline:
        time.sleep(0.05)


def near_month_turn(at: datetime) -> None:
    """The one time skip: 0052 opens only the current UTC month's book, so a
    test that opens one and reserves in it cannot straddle a month boundary."""
    if period_of(at + timedelta(seconds=60)) != period_of(at):
        pytest.skip(
            "explicit time skip: a UTC month boundary falls within 60 s "
            "of the test's start (0052 opens only the current month)"
        )


def fixed(b: BudgetBook) -> list[dict[str, object]]:
    """Every reservation field a sweep must never change."""
    moving = ("status", "actual")
    return [{k: v for k, v in r.to_book_wire().items() if k not in moving}
            for r in b.reservations]  # fmt: skip


@pytest.mark.db
def test_the_job_expires_releases_leaves_the_rest_and_repeats_as_a_no_op(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    t, u, at = tenant(app_conn, conn), tenant(app_conn, conn), clock(app_conn)
    near_month_turn(at)
    deadline, later = at + timedelta(seconds=2), at + timedelta(hours=1)
    before = due_book(app_conn, t, at, deadline, later)
    other = step(app_conn, u, reserved("SYNTHETIC-r1", R, "15", at, deadline), at)
    s = before.summary()
    assert (s.reserved, s.spent, before.summary(IMP).available) == (
        Usd("50"), Usd("0"), Usd("0"))  # fmt: skip
    wait_until(app_conn, deadline)
    hour = clock(app_conn).strftime("%Y-%m-%dT%H")
    jid, created = call(worker_conn, t, sw.submit)
    assert created
    assert Worker(worker_conn, SETTINGS, handlers=sw.HANDLERS).tick().jobs == [
        (jid, JobState.SUCCEEDED)]  # fmt: skip
    after = book(worker_conn, t, at)
    assert after.version == before.version + 1  # one compare-and-set step
    got = {r.reservation_id: (r.status, r.actual) for r in after.reservations}
    assert got == {"SYNTHETIC-r1": (Status.EXPIRED, Usd("15")),  # full worst case
                   "SYNTHETIC-r2": (Status.RELEASED, None),  # never sent: freed
                   "SYNTHETIC-r3": (Status.RESERVED, None),  # not due yet
                   "SYNTHETIC-r4": (Status.RESERVED, None)}  # fmt: skip
    assert fixed(after) == fixed(before)  # no deadline, time or cost reset
    s = after.summary()  # reserved 10 + 5; spent 15; free 100 - 15 - 15 = 70
    assert (s.reserved, s.spent, s.available) == (Usd("15"), Usd("15"), Usd("70"))
    assert after.summary(IMP).available == Usd("20")  # the released USD20 is free
    # a repeat: the same hour's enqueue is the same job; a sweep changes nothing
    again, created = call(worker_conn, t, sw.submit)
    if clock(app_conn).strftime("%Y-%m-%dT%H") == hour:  # else the hour turned
        assert (again, created) == (jid, False)
    assert call(worker_conn, t, sw.sweep) == ([call(worker_conn, t, lambda tx:
        bs.load(tx, period_of(at)))], [])  # fmt: skip
    assert book(worker_conn, t, at) == after
    # RLS: the other tenant's due reservation is untouched by t's sweep
    assert book(worker_conn, u, at) == other
    assert other.get("SYNTHETIC-r1").status is Status.RESERVED
    # the freed improvement cap admits a new USD20 improvement reservation
    now = clock(app_conn)
    grown = step(app_conn, t, reserved("SYNTHETIC-r5", IMP, "20", now,
                                       now + timedelta(minutes=5)), now)  # fmt: skip
    assert grown.summary(IMP).available == Usd("0")


@pytest.mark.db
def test_a_concurrent_writer_wins_or_loses_cleanly_under_compare_and_set(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """The sweep loads version v while qw_app holds v + 1 (a late USD4 report on
    r1) uncommitted; the sweep's update waits on the row, then finds v gone
    (`stale_version`), reloads and sweeps v + 1: r1 stays settled at USD4 (the
    report is not overwritten by `expired`), r2 is released, at v + 2. Then a
    writer still holding v + 1 loses with `stale_version`."""
    t, at = tenant(app_conn, conn), clock(app_conn)
    near_month_turn(at)
    deadline, later = at + timedelta(seconds=2), at + timedelta(hours=1)
    v = due_book(app_conn, t, at, deadline, later)
    wait_until(app_conn, deadline)
    pid = worker_conn.info.backend_pid
    with ThreadPoolExecutor(1) as pool, tn.tenant_transaction(app_conn, t) as tx:
        report = v.settle("SYNTHETIC-r1", Usd("4"), clock(app_conn))
        bs.install(tx, report, v.version)  # row locked until this block commits
        swept = pool.submit(call, worker_conn, t, sw.sweep)
        for _ in range(200):
            row = conn.execute("SELECT wait_event_type FROM pg_stat_activity "
                               "WHERE pid = %s", (pid,)).fetchone()  # fmt: skip
            if row is not None and row[0] == "Lock":
                break
            time.sleep(0.05)
        else:
            pytest.fail("the sweep never waited on the row lock")
        assert not swept.done()
    (out,), failed = swept.result(timeout=30)
    assert failed == []
    assert out.book.version == v.version + 2
    r1, r2 = out.book.get("SYNTHETIC-r1"), out.book.get("SYNTHETIC-r2")
    assert (r1.status, r1.actual, r2.status) == (
        Status.SETTLED, Usd("4"), Status.RELEASED)  # fmt: skip
    assert fixed(out.book) == fixed(v)
    stale = report.dispatched("SYNTHETIC-r4", clock(app_conn))  # from v + 1
    with pytest.raises(bs.BudgetStoreError) as exc:
        call(app_conn, t, lambda tx: bs.install(tx, stale, v.version + 1))
    assert exc.value.code == "stale_version"
    assert book(worker_conn, t, at) == out.book


@pytest.mark.db
def test_the_sweep_reaches_last_months_book(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """A reservation's deadline can fall in the next month: last month's book
    (written as the owner, triggers off: 0052 opens only the current month) with
    an undispatched USD5 reservation created 1 h before this month and due 1 h
    into it (or 1 s ago, if sooner) is swept and released; a book with nothing
    open is not listed."""
    t, now = tenant(app_conn, conn), clock(app_conn)
    near_month_turn(now)
    old = last_month(conn, t, now)
    step(app_conn, t, lambda b: b, now)  # this month's book, nothing in it
    assert call(worker_conn, t, bs.open_periods) == [old.period]
    (out,), failed = call(worker_conn, t, sw.sweep)
    assert failed == []
    assert (out.book.period, out.book.get("SYNTHETIC-r1").status) == (
        old.period, Status.RELEASED)  # fmt: skip
    assert call(worker_conn, t, bs.open_periods) == []
    assert call(worker_conn, t, sw.sweep) == ([], [])


def last_month(su: Conn, t: uuid.UUID, now: datetime,
               poison: bool = False) -> BudgetBook:  # fmt: skip
    """t's book of last month holding a due undispatched USD5 reservation,
    inserted as the owner with triggers off; `poison`: with a wrong hash."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = start - timedelta(hours=1)
    due = min(start + timedelta(hours=1), now - timedelta(seconds=1))
    old = reserved("SYNTHETIC-r1", IMP, "5", last, due)(
        BudgetBook.open(str(t), last, CAPS))  # fmt: skip
    wire = old.to_wire()
    digest = "0" * 64 if poison else bs.book_hash(tn.TenantTx(su, t), wire)
    with su.transaction():
        su.execute("SET LOCAL session_replication_role = replica")
        su.execute("INSERT INTO app.budget_book (tenant_id, scope_id, period, "
                   "version, book, book_hash) VALUES (%s, %s, %s, %s, %s, %s)",
                   (t, str(t), old.period, old.version, Jsonb(wire),
                    digest))  # fmt: skip
    return old


@pytest.mark.db
def test_a_poisoned_old_month_does_not_block_or_undo_the_current_month(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """Review fix (medium): last month's book fails re-verification
    (`integrity`); the job still sweeps this month (r1 expired, r2 released),
    commits it with its failure, and fails as `budget_refused` (not retried).
    The poisoned row is untouched."""
    t, at = tenant(app_conn, conn), clock(app_conn)
    near_month_turn(at)
    deadline, later = at + timedelta(seconds=2), at + timedelta(hours=1)
    before = due_book(app_conn, t, at, deadline, later)
    old = last_month(conn, t, at, poison=True)
    assert call(worker_conn, t, bs.open_periods) == [old.period, before.period]
    wait_until(app_conn, deadline)
    jid, _ = call(worker_conn, t, sw.submit)
    assert Worker(worker_conn, SETTINGS, handlers=sw.HANDLERS).tick().jobs == [
        (jid, JobState.FAILED)]  # fmt: skip
    with tn.tenant_transaction(worker_conn, t) as tx:
        row = tx.conn.execute("SELECT last_error FROM app.job WHERE id = %s",
                              (jid,)).fetchone()  # fmt: skip
    assert row == ("budget_refused",)
    after = book(worker_conn, t, at)
    assert after.version == before.version + 1
    assert (after.get("SYNTHETIC-r1").status, after.get("SYNTHETIC-r2").status) == (
        Status.EXPIRED, Status.RELEASED)  # fmt: skip
    stored = conn.execute("SELECT version, book_hash FROM app.budget_book WHERE "
                          "tenant_id = %s AND period = %s",
                          (t, old.period)).fetchone()  # fmt: skip
    assert stored == (old.version, "0" * 64)  # unchanged


class _Conn:
    def transaction(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()


FAKE: Any = tn.TenantTx(_Conn(), uuid.uuid4())  # type: ignore[arg-type]


def _job(payload: Any) -> ClaimedJob:
    return ClaimedJob(uuid.uuid4(), uuid.uuid4(), sw.KIND, "expire_due:x",
                      Priority.SAFETY, payload, 1, 5, "synthetic:token",
                      datetime(2026, 10, 28, tzinfo=UTC))  # fmt: skip


@pytest.mark.parametrize(("raised", "code", "retryable", "calls"), [
    ("stale_version", "database", True, 3),  # three reloads per month
    ("stale_clock", "database", True, 1),
    ("database", "database", True, 1),
    ("integrity", "budget_refused", False, 1),
    ("forbidden", "budget_refused", False, 1),
])  # fmt: skip
def test_the_handler_retries_and_refuses_with_codes(
    monkeypatch: pytest.MonkeyPatch, raised: str, code: str, retryable: bool,
    calls: int,
) -> None:  # fmt: skip
    seen: list[str] = []

    def fail(tx: object, period: str) -> None:
        seen.append(period)
        raise bs.BudgetStoreError(raised)

    monkeypatch.setattr(bs, "open_periods", lambda tx: ["2026-09", "2026-10"])
    monkeypatch.setattr(bs, "expire_due", fail)
    with pytest.raises(HandlerFailed) as exc:
        sw.handle(FAKE, _job({"slot": "2026-10-28T14"}))
    assert (exc.value.code, exc.value.retryable, exc.value.keep_effects) == (
        code, retryable, True)  # fmt: skip
    assert seen == ["2026-09"] * calls + ["2026-10"] * calls  # every month tried


def test_any_retryable_month_makes_the_job_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codes = iter(["integrity", "stale_clock"])

    def fail(tx: object, period: str) -> None:
        raise bs.BudgetStoreError(next(codes))

    monkeypatch.setattr(bs, "open_periods", lambda tx: ["2026-09", "2026-10"])
    monkeypatch.setattr(bs, "expire_due", fail)
    assert sw.sweep(FAKE) == ([], [("2026-09", "integrity"),
                                   ("2026-10", "stale_clock")])  # fmt: skip
    codes = iter(["integrity", "stale_clock"])
    with pytest.raises(HandlerFailed) as exc:
        sw.handle(FAKE, _job({"slot": "2026-10-28T14"}))
    assert (exc.value.code, exc.value.retryable) == ("database", True)
    monkeypatch.setattr(bs, "open_periods", lambda tx: (_ for _ in ()).throw(
        bs.BudgetStoreError("integrity")))  # fmt: skip
    with pytest.raises(HandlerFailed) as exc:  # nothing listed: nothing to keep
        sw.handle(FAKE, _job({"slot": "2026-10-28T14"}))
    assert (exc.value.code, exc.value.keep_effects) == ("budget_refused", False)


def test_the_handler_refuses_a_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bs, "open_periods", lambda tx: pytest.fail("read"))
    bad: object
    for bad in ({}, {"slot": "2026-10-28"}, {"slot": 1}, [],
                {"slot": "2026-10-28T14", "x": "1"}):  # fmt: skip
        with pytest.raises(HandlerFailed) as exc:
            sw.handle(FAKE, _job(bad))
        assert (exc.value.code, exc.value.retryable) == ("payload_invalid", False)


def test_a_stale_version_once_reloads_and_sweeps_every_open_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def once(tx: object, period: str) -> None:
        seen.append(period)
        if len(seen) == 1:
            raise bs.BudgetStoreError("stale_version")

    monkeypatch.setattr(bs, "open_periods", lambda tx: ["2026-09", "2026-10"])
    monkeypatch.setattr(bs, "expire_due", once)
    assert sw.sweep(FAKE) == ([], [])
    assert seen == ["2026-09", "2026-09", "2026-10"]


# --- the budget-sweep timer (owner decision 100, T037 increment 5; 0054) ---


def clear_of_hour_turn(c: Conn) -> None:
    """The timer's idempotency is per DB-clock hour: start at least 30 s before
    an hour boundary (waiting out at most 30 s), never skip."""
    now = clock(c)
    turn = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if turn - now < timedelta(seconds=30):
        wait_until(c, turn + timedelta(seconds=1))


def overdue_fixture(app: Conn, su: Conn) -> tuple[list[uuid.UUID], datetime]:
    """Tenants a and b (an overdue reservation each; a also one not yet due), c
    (not yet due), d (expired), e (released and settled), f (an empty book) and
    g (no book). Returns ([a, b] sorted, the overdue deadline), after it."""
    at = clock(app)
    near_month_turn(at)
    due, later = at + timedelta(seconds=4), at + timedelta(hours=1)
    a, b, c, d, e, f, _g = (tenant(app, su) for _ in range(7))
    step(app, a, reserved("SYNTHETIC-r1", R, "15", at, due), at)
    step(app, a, reserved("SYNTHETIC-r2", R, "5", at, later), clock(app))
    step(app, b, reserved("SYNTHETIC-r1", IMP, "20", at, due), at)
    step(app, c, reserved("SYNTHETIC-r1", R, "15", at, later), at)
    for go in (reserved("SYNTHETIC-r1", R, "15", at, due),
               lambda x: x.dispatched("SYNTHETIC-r1", at)):  # fmt: skip
        step(app, d, go, clock(app))
    for go in (reserved("SYNTHETIC-r1", R, "15", at, due),
               reserved("SYNTHETIC-r2", R, "5", at, due),
               lambda x: x.release("SYNTHETIC-r1"),
               lambda x: x.dispatched("SYNTHETIC-r2", at),
               lambda x: x.settle("SYNTHETIC-r2", Usd("1"), at)):  # fmt: skip
        step(app, e, go, clock(app))
    step(app, f, lambda x: x, at)
    wait_until(app, due)
    _, failed = call(app, d, sw.sweep)  # d's reservation is now `expired`
    assert failed == []
    assert book(app, d, at).get("SYNTHETIC-r1").status is Status.EXPIRED
    return sorted([a, b]), due


@pytest.mark.db
def test_the_lister_returns_exactly_the_tenants_with_an_overdue_reservation(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """Not a not-yet-due, expired, released or settled reservation, an empty
    book or no book; last month's overdue book counts; only ids come back."""
    due_ones, _ = overdue_fixture(app_conn, conn)
    assert bs.overdue_tenants(worker_conn) == due_ones
    h = tenant(app_conn, conn)
    last_month(conn, h, clock(app_conn))  # due, undispatched, last month
    every = sorted([*due_ones, h])
    assert bs.overdue_tenants(worker_conn) == every
    with tn.tenant_transaction(worker_conn, due_ones[0]) as tx:  # a context
        got = tx.conn.execute("SELECT * FROM app.budget_overdue_tenants(NULL)")
        assert [r[0] for r in got.fetchall()] == every  # changes nothing
    cur = worker_conn.execute("SELECT * FROM app.budget_overdue_tenants(NULL)")
    assert [d.name for d in cur.description or []] == ["budget_overdue_tenants"]
    assert [r[0] for r in cur.fetchall()] == every  # one uuid column, nothing else
    after = worker_conn.execute("SELECT * FROM app.budget_overdue_tenants(%s)",
                                (every[0],)).fetchall()  # fmt: skip
    assert [r[0] for r in after] == every[1:]  # keyset: strictly greater ids
    # without a tenant context qw_worker still reads no book directly (0052)
    row = worker_conn.execute("SELECT count(*) FROM app.budget_book").fetchone()
    assert row == (0,)


@pytest.mark.db
def test_the_lister_pages_past_its_bound_in_ascending_order(
    worker_conn: Conn, conn: Conn
) -> None:
    """1001 tenants with an overdue reservation (written as the owner, triggers
    off): the lister's pages are 500, 500 and 1 ids, ascending, none repeated;
    the adapter returns all 1001."""
    now = clock(conn)
    at = now - timedelta(hours=1)
    base = reserved("SYNTHETIC-r1", IMP, "5", at, now - timedelta(seconds=1))(
        BudgetBook.open("SCOPE", at, CAPS))  # fmt: skip
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        ids = [r[0] for r in conn.execute(
            "INSERT INTO app.tenant (id) SELECT gen_random_uuid() "
            "FROM generate_series(1, 1001) RETURNING id").fetchall()]  # fmt: skip
        conn.execute(
            "INSERT INTO app.budget_book (tenant_id, scope_id, period, version, "
            "book, book_hash) SELECT t, t::text, %s, %s, "
            "replace(%s::text, 'SCOPE', t::text)::jsonb, repeat('0', 64) "
            "FROM unnest(%s::uuid[]) t",
            (base.period, base.version, Jsonb(base.to_wire()), ids),
        )
    pages: list[list[uuid.UUID]] = []
    after: uuid.UUID | None = None
    while not pages or pages[-1]:
        pages.append([r[0] for r in worker_conn.execute(
            "SELECT * FROM app.budget_overdue_tenants(%s::uuid)",
            (after,)).fetchall()])  # fmt: skip
        after = pages[-1][-1] if pages[-1] else after
    assert [len(p) for p in pages] == [500, 500, 1, 0]
    assert [t for p in pages for t in p] == sorted(ids)
    assert bs.OVERDUE_PAGE == 500
    assert bs.overdue_tenants(worker_conn) == sorted(ids)


@pytest.mark.db
def test_only_qw_worker_may_list_and_nobody_else_gains_access(
    app_conn: Conn, worker_conn: Conn, assessor_conn: Conn, release_conn: Conn,
    conn: Conn, database_url: str,
) -> None:  # fmt: skip
    due_ones, _ = overdue_fixture(app_conn, conn)
    ingest = f"qwtest_ingest_{uuid.uuid4().hex[:12]}"
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE qw_ingest"
                         ).format(sql.Identifier(ingest)))  # fmt: skip
    try:
        with psycopg.connect(make_conninfo(database_url, user=ingest),
                             autocommit=True) as ing:  # fmt: skip
            for c in (app_conn, assessor_conn, release_conn, ing):
                with pytest.raises(bs.BudgetStoreError) as exc:
                    bs.overdue_tenants(c)
                assert exc.value.code == "forbidden"
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute("SELECT app.budget_overdue_tenants(NULL)")
                with pytest.raises(bs.BudgetStoreError):  # the timer step too
                    sw.schedule(c)
    finally:
        conn.execute(sql.SQL("SELECT pg_terminate_backend(pid) FROM "
                             "pg_stat_activity WHERE usename = {}").format(
            sql.Literal(ingest)))  # fmt: skip
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(ingest)))
    # the owner may execute it, but a qw_migrate session whose login is not a
    # qw_worker member sees nothing: 0054's policy admits only worker logins
    with conn.transaction():
        conn.execute("SET LOCAL SESSION AUTHORIZATION qw_migrate")
        got = conn.execute("SELECT * FROM app.budget_overdue_tenants(NULL)")
        assert got.fetchall() == []
        row = conn.execute("SELECT count(*) FROM app.budget_book").fetchone()
        assert row == (0,)
    assert bs.overdue_tenants(worker_conn) == due_ones
    pol = conn.execute(
        "SELECT policyname, roles::text, cmd FROM pg_policies WHERE schemaname = "
        "'app' AND tablename = 'budget_book' ORDER BY 1").fetchall()  # fmt: skip
    assert pol == [("operator_read", "{qw_migrate}", "SELECT"),
                   ("overdue_list", "{qw_migrate}", "SELECT"),
                   ("tenant_isolation", "{public}", "ALL")]  # fmt: skip


def only(tick: Any) -> tuple[str, list[uuid.UUID], list[tuple[uuid.UUID, str]]]:
    """The one timer result of a tick: (name, queued job ids, failures)."""
    assert len(tick.timed) == 1
    name, result = tick.timed[0]
    queued, failed = result
    return name, queued, failed


@pytest.mark.db
def test_the_timer_queues_one_sweep_per_tenant_and_the_worker_frees_them(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    clear_of_hour_turn(app_conn)
    (a, b), due = overdue_fixture(app_conn, conn)
    sweeps = sw.timer(timedelta(minutes=5))
    worker = Worker(worker_conn, SETTINGS, handlers=sw.HANDLERS, timers=[sweeps])
    first = worker.tick()  # the claims run before the timer: nothing queued yet
    name, queued, failed = only(first)
    assert (first.jobs, name, len(queued), failed) == ([], sw.KIND, 2, [])
    assert sw.schedule(worker_conn) == ([], [])  # same hour: nothing new
    second = worker.tick()  # the timer is not due again for 5 minutes
    assert sorted(second.jobs) == sorted((j, JobState.SUCCEEDED) for j in queued)
    assert second.timed == []
    books = [book(worker_conn, t, due) for t in (a, b)]
    assert [x.get("SYNTHETIC-r1").status for x in books] == [Status.RELEASED] * 2
    # the one not-yet-due reservation (fixture tenant a) stays reserved
    assert [r.status for x in books if (r := x.find("SYNTHETIC-r2"))] == [
        Status.RESERVED]  # fmt: skip
    assert bs.overdue_tenants(worker_conn) == []
    again = Worker(worker_conn, SETTINGS, handlers=sw.HANDLERS, timers=[sweeps])
    assert again.tick().timed == [(sw.KIND, ([], []))]
    for t in (a, b):  # exactly one sweep job per tenant this hour
        with tn.tenant_transaction(worker_conn, t) as tx:
            row = tx.conn.execute("SELECT count(*) FROM app.job WHERE kind = %s",
                                  (sw.KIND,)).fetchone()  # fmt: skip
        assert row == (1,)


@pytest.mark.db
def test_one_tenants_failure_does_not_stop_the_others(
    app_conn: Conn, worker_conn: Conn, conn: Conn,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """b's slot already holds a different job (a conflicting enqueue): b's
    `submit` raises IdempotencyConflict, which is logged, and a and h are still
    queued and swept. A timer that raises is logged and the next one runs."""
    clear_of_hour_turn(app_conn)
    (a, b), due = overdue_fixture(app_conn, conn)
    h, now = tenant(app_conn, conn), clock(app_conn)
    step(app_conn, h, reserved("SYNTHETIC-r1", R, "5", now,
                               now + timedelta(seconds=1)), now)  # fmt: skip
    wait_until(app_conn, now + timedelta(seconds=1))
    slot = clock(app_conn).strftime("%Y-%m-%dT%H")
    with tn.tenant_transaction(app_conn, b) as tx:
        qj.enqueue(tx, sw.KIND, f"expire_due:{slot}", Priority.SAFETY,
                   {"slot": "SYNTHETIC"})  # fmt: skip
    with caplog.at_level(logging.WARNING, "qw_worker"):
        queued, failed = sw.schedule(worker_conn)
    assert (len(queued), failed) == (2, [(b, "IdempotencyConflict")])
    assert f"budget sweep for tenant {b} not queued: IdempotencyConflict" in (
        caplog.text)  # fmt: skip

    def boom(c: Conn) -> object:
        raise RuntimeError("SYNTHETIC")

    worker = Worker(worker_conn, SETTINGS, handlers=sw.HANDLERS,
                    timers=[Timer("synthetic.boom", timedelta(minutes=1), boom),
                            sw.timer(timedelta(minutes=5))])  # fmt: skip
    with caplog.at_level(logging.ERROR, "qw_worker"):
        tick = worker.tick()  # the conflicting job fails; a and h are swept
    assert "timer synthetic.boom failed" in caplog.text
    assert only(tick) == (sw.KIND, [], [(b, "IdempotencyConflict")])
    assert sorted(s for _, s in tick.jobs if s) == [
        JobState.FAILED, JobState.SUCCEEDED, JobState.SUCCEEDED]  # fmt: skip
    for t, status in ((a, Status.RELEASED), (h, Status.RELEASED),
                      (b, Status.RESERVED)):  # fmt: skip
        assert book(worker_conn, t, due).get("SYNTHETIC-r1").status is status
    assert bs.overdue_tenants(worker_conn) == [b]


def test_timer_settings_are_bounded() -> None:
    for bad in (timedelta(seconds=59), timedelta(minutes=61)):
        with pytest.raises(ValueError):
            WorkerSettings("synthetic-w", budget_sweep_every=bad)
    with pytest.raises(ValueError):
        Timer("synthetic.t", timedelta(0), lambda c: None)
    t = Timer("synthetic.t", timedelta(minutes=1), lambda c: None)
    with pytest.raises(ValueError):
        Worker(FAKE.conn, SETTINGS, timers=[t, t])
    assert WorkerSettings("synthetic-w").budget_sweep_every == timedelta(minutes=5)


@pytest.mark.db
def test_the_entry_point_runs_the_timer_on_the_standard_pool_only(
    runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from qw_worker import __main__ as entry

    started: list[Worker] = []
    monkeypatch.setattr(Worker, "run", lambda w, max_ticks=None: started.append(w))
    monkeypatch.setattr(entry, "install_signal_handlers", lambda worker: None)
    monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_worker"])
    monkeypatch.delenv("QW_ASSESSOR_DATABASE_URL", raising=False)
    monkeypatch.setenv("QW_WORKER_BUDGET_SWEEP_EVERY_MINUTES", "7")
    assert entry.main() == 0
    monkeypatch.setenv("QW_WORKER_POOL", "experiment")
    assert entry.main() == 0
    assert [[(t.name, t.every, t.run) for t in w.timers] for w in started] == [
        [(sw.KIND, timedelta(minutes=7), sw.schedule)], []]  # fmt: skip
    monkeypatch.setenv("QW_WORKER_BUDGET_SWEEP_EVERY_MINUTES", "61")
    assert entry.main() == 2
