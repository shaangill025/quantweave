"""The `budget.expire_due` job (T037 increment 4) on a real PostgreSQL 16: the
sweep runs as the non-owner, NOBYPASSRLS qw_worker login in each tenant's
transaction; qw_app is the other writer; the superuser `conn` records the
owner's authorizations (as qw_migrate) and observes lock waits. Tenants, ids,
accounts and refs are SYNTHETIC; times are the database clock; amounts are
hand-computed in the comments."""

from __future__ import annotations

import contextlib
import importlib.util
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from qw_adapters import budget_store as bs
from qw_adapters import tenancy as tn
from qw_adapters.jobs import ClaimedJob
from qw_domain.budget import BudgetBook, BudgetCaps, Category, Status, period_of
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import Backoff, JobState, Priority
from qw_worker import budget_sweep as sw
from qw_worker.config import WorkerSettings
from qw_worker.loop import HandlerFailed, Worker

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness_sweep", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn, worker_conn = _harness.app_conn, _harness.worker_conn

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
