"""The durable budget book (`0052_budget_book.sql`; T037; spec §10, §11, §15;
T008 F-18; decision 94 will reserve the optimizer verdict's cost here). One
`BudgetBook` per tenant and UTC month, its scope the session tenant id (the
gateway's `req.tenant_id == book.scope_id`), written only by compare-and-set:
- `load(tx, period)`: the book or None; every read re-hashes the row and
  re-parses the strict domain wire, else `integrity`.
- `open_book(tx, at, caps)`: the book of `at`'s month, opened from `caps` if
  absent (0052: configured caps, current month, a null or owner-recorded ref,
  else `invalid`). A book differing only by a null ref is authorized to
  `caps`'s; any other difference is `caps_mismatch`.
- `authorize(tx, period, ref)`: the one caps change, null -> a ref the owner
  recorded in `app.budget_authorization` (direct SQL as qw_migrate; no API).
- `install(tx, new, expected_version)`: `new` one step on from `expected_version`,
  `UPDATE ... WHERE version = expected`: of concurrent writers one wins, the
  rest get `stale_version`; an idempotent retry (`new` at `expected`) writes
  nothing and must equal the stored book. 0052 refuses what no domain step makes
  (`invalid`) and a reservation created over 60 s before the write
  (`stale_clock`: reload and retry; a row-lock wait can cause it).
- `expire_due(tx, period)`: `BudgetBook.expire_due` at the database clock as one
  step (installed at `version + 1`; the domain counts one per expiry).
No retry resets a reservation, deadline or cost. Every call needs the open
`tenant_transaction` of `tx` (`tenant_transaction`); only qw_app and qw_worker
may (`forbidden`)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import psycopg
from psycopg import pq
from psycopg.types.json import Jsonb
from qw_domain.budget import BudgetBook, BudgetCaps, BudgetError
from qw_domain.instants import ensure_aware_utc
from qw_domain.research_data import digest

from qw_adapters.tenancy import TenantTx

_COLS = ("scope_id", "period", "version", "book", "book_hash", "updated_at")


class BudgetStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredBook:
    book: BudgetBook
    book_hash: str
    updated_at: datetime


def book_hash(tx: TenantTx, wire: dict[str, Any]) -> str:
    return digest(["budget_book", str(tx.tenant_id), wire["scope_id"],
                   wire["period"], wire["version"], wire])  # fmt: skip


def _database(exc: psycopg.Error) -> BudgetStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return BudgetStoreError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.CheckViolation):
        stale = "app.budget_book: stale_clock" in str(exc)
        return BudgetStoreError("stale_clock" if stale else "invalid", str(exc))
    return BudgetStoreError("database", type(exc).__name__)


def _tenant(tx: TenantTx) -> str:
    """The caller's `tenant_transaction` must still be open for its tenant; the
    tenant id is the book's scope."""
    status = tx.conn.info.transaction_status
    if status != pq.TransactionStatus.INTRANS:
        raise BudgetStoreError("tenant_transaction", "no open transaction")
    row = tx.conn.execute("SELECT current_setting('app.tenant_id', true)").fetchone()
    if row is None or row[0] != str(tx.tenant_id):
        raise BudgetStoreError("tenant_transaction", "another tenant's context")
    return str(tx.tenant_id)


def _parse(tx: TenantTx, scope: str, period: str, row: Any) -> StoredBook:
    try:
        r = dict(zip(_COLS, row, strict=True))
        wire = r["book"]
        book = BudgetBook.from_wire(wire)
        if (
            r["book_hash"] != book_hash(tx, wire)
            or (r["scope_id"], r["period"], r["version"])
            != (scope, period, book.version)
            or (book.scope_id, book.period) != (scope, period)
        ):
            raise ValueError("budget_book")
        at = ensure_aware_utc(r["updated_at"])
    except (ValueError, TypeError, KeyError) as exc:  # BudgetError, DecimalError
        raise BudgetStoreError("integrity", type(exc).__name__) from None
    return StoredBook(book, r["book_hash"], at)


def load(tx: TenantTx, period: str) -> StoredBook | None:
    """The session tenant's book of `period`, re-verified, or None."""
    scope = _tenant(tx)
    try:
        row = tx.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM app.budget_book WHERE tenant_id = %s "
            "AND period = %s",
            (tx.tenant_id, period),
        ).fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    return None if row is None else _parse(tx, scope, period, row)


def open_book(tx: TenantTx, at: datetime, caps: BudgetCaps) -> StoredBook:
    """The book of `at`'s UTC month, opened from `caps` if absent."""
    scope = _tenant(tx)
    try:
        fresh = BudgetBook.open(scope, at, caps)
    except (BudgetError, TypeError) as exc:
        raise BudgetStoreError("invalid", type(exc).__name__) from None
    stored = load(tx, fresh.period)
    if stored is None:
        wire = fresh.to_wire()
        try:
            with tx.conn.transaction():
                tx.conn.execute(
                    "INSERT INTO app.budget_book (tenant_id, scope_id, period, "
                    "version, book, book_hash) VALUES (%s, %s, %s, 0, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (tx.tenant_id, scope, fresh.period, Jsonb(wire),
                     book_hash(tx, wire)),
                )  # fmt: skip
        except psycopg.Error as exc:
            raise _database(exc) from None
        stored = load(tx, fresh.period)
        if stored is None:  # inserted or present: only RLS could hide it
            raise BudgetStoreError("integrity", "the book is not visible")
    old, ref = stored.book.caps, caps.authorization_ref
    if old == caps:
        return stored
    if (
        old.authorization_ref is None
        and ref
        and replace(caps, authorization_ref=None) == old
    ):
        return authorize(tx, fresh.period, ref)  # the one caps change
    raise BudgetStoreError("caps_mismatch", fresh.period)


def authorize(tx: TenantTx, period: str, ref: str) -> StoredBook:
    """Set the book's null authorization ref to the owner-recorded `ref`."""
    current = load(tx, period)
    if current is None:
        raise BudgetStoreError("no_book", period)
    b = current.book
    if b.caps.authorization_ref is not None:
        raise BudgetStoreError("caps_mismatch", "already authorized")
    try:
        caps = replace(b.caps, authorization_ref=ref)  # BudgetCaps checks the id
    except (BudgetError, TypeError) as exc:
        raise BudgetStoreError("invalid", type(exc).__name__) from None
    return install(tx, replace(b, caps=caps, version=b.version + 1), b.version)


def install(tx: TenantTx, new: BudgetBook, expected_version: int) -> StoredBook:
    """Compare-and-set `new` over the stored book at `expected_version`."""
    if type(new) is not BudgetBook or type(expected_version) is not int:
        raise TypeError("a BudgetBook and an int version")
    scope = _tenant(tx)
    try:
        wire = BudgetBook.from_wire(new.to_wire()).to_wire()  # canonical, valid
    except (BudgetError, TypeError, ValueError) as exc:
        raise BudgetStoreError("invalid", type(exc).__name__) from None
    if new.scope_id != scope:
        raise BudgetStoreError("invalid", "another tenant's book")
    if new.version not in (expected_version, expected_version + 1):
        raise BudgetStoreError("invalid", "one version step at a time")
    current = load(tx, new.period)
    if current is None:
        raise BudgetStoreError("no_book", new.period)
    if new.version == expected_version:  # the domain's idempotent retry
        if current.book != new:
            raise BudgetStoreError("stale_version", "the stored book differs")
        return current
    try:
        with tx.conn.transaction():
            row = tx.conn.execute(
                "UPDATE app.budget_book SET version = %s, book = %s, book_hash = %s"
                " WHERE tenant_id = %s AND period = %s AND version = %s"
                " RETURNING version",
                (new.version, Jsonb(wire), book_hash(tx, wire), tx.tenant_id,
                 new.period, expected_version),
            ).fetchone()  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None
    if row is None:  # another writer installed this version first
        raise BudgetStoreError("stale_version", str(expected_version))
    stored = load(tx, new.period)
    if stored is None or stored.book != new:
        raise BudgetStoreError("integrity", "the installed book differs")
    return stored


def expire_due(tx: TenantTx, period: str) -> StoredBook | None:
    """Expire what is due at the database clock, as one compare-and-set step."""
    current = load(tx, period)
    if current is None:
        return None
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    book = current.book
    swept = book.expire_due(ensure_aware_utc(row[0]))
    if swept.version == book.version:
        return current
    return install(tx, replace(swept, version=book.version + 1), book.version)
