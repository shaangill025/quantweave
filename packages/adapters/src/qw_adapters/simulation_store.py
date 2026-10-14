"""Persisted virtual portfolios and simulations (T041 increment 3;
`0014_simulation.sql`; spec 14 three separate records, R063, R098).

A `Simulation` is a definition (contract SimulationSummary) over a domain
`VirtualPortfolio` (account id = simulation id) and its runs' `OrderResult`s, each
with the run's as-of (`Outcome`). Callers build them with qw_domain.simulation.
- `save` refuses `unsupported` (written options, shadow mode), `account_conflict`
  (a real journal account id; the 0014 guard under the journal's account lock)
  and `append_only` (not an extension of the stored); it inserts the new rows and a
  `digest` row, then reads back: a difference is `integrity`.
- `load`/`page` replay the ledger through `VirtualPortfolio.open`/`record`,
  re-check every outcome and fill against it (`_check`) and compare the canonical
  state and counts with the latest digest: one changed, deleted or appended row is
  `integrity`. Rows rewritten with a matching digest are not detected.
- Another tenant's simulation is unknown (None), also on an RLS-bypassing role.
Locks: the tenant's simulation lock, shared to read and exclusive to `save` (then
the account lock inside the insert); evidence and rights locks come first. Do not
`load` before `save` in one transaction (two callers would deadlock upgrading);
`save` reads the stored state itself. Database failures are `database`; on any
error the caller must roll back.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal, localcontext
from enum import Enum, StrEnum
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import simulation as sim
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    BoundedDecimal,
    Money,
    PositiveQuantity,
    Price,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.risk import Side
from qw_domain.simulation import EntryKind, OrderStatus, VirtualEntry

from qw_adapters.tenancy import TenantTx

__all__ = ["Mode", "Outcome", "Simulation", "SimulationStoreError", "load", "page",
           "save"]  # fmt: skip
TABLES = ("simulation", "simulation_entry", "simulation_order", "simulation_fill",
          "simulation_digest")  # fmt: skip
KINDS = frozenset({EntryKind.CAPITAL, EntryKind.SEED, EntryKind.FILL,
                   EntryKind.DIVIDEND, EntryKind.SPLIT})  # fmt: skip
AMOUNTS = ("cash", "units", "cost", "realized", "income", "fees", "capital")


class SimulationStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Mode(StrEnum):
    MANUAL = "manual"
    SHADOW = "approved_policy_shadow"  # contract value; not modelled yet


@dataclass(frozen=True, slots=True)
class Outcome:
    result: sim.OrderResult
    as_of: datetime


@dataclass(frozen=True, slots=True)
class Simulation:
    name: str
    execution_model_id: str
    benchmark_policy_id: str
    portfolio: sim.VirtualPortfolio
    outcomes: tuple[Outcome, ...] = ()
    mode: Mode = Mode.MANUAL

    @property
    def simulation_id(self) -> str:
        return self.portfolio.account_id

    def run(self, r: sim.SimRun, as_of: datetime) -> Simulation:
        at = ensure_aware_utc(as_of)
        done = tuple(Outcome(x, at) for x in r.results)
        return replace(self, portfolio=r.portfolio, outcomes=(*self.outcomes, *done))

    def fills(self) -> tuple[sim.VirtualFill, ...]:
        return tuple(f for o in self.outcomes for f in o.result.fills)


def _bad(why: str) -> SimulationStoreError:
    return SimulationStoreError("integrity", why)


def _replay(sid: str, currency: str, entries: Sequence[VirtualEntry]) -> None:
    """The entries must be what opening `sid` and recording each in turn gives."""
    if not entries or entries[0].kind is not EntryKind.CAPITAL:
        raise _bad("the ledger does not open with its capital")
    first = entries[0]
    p = sim.VirtualPortfolio.open(sid, Money.of(first.capital, currency),
                                  first.effective_at)  # fmt: skip
    for e in entries[1:]:
        if e.kind not in KINDS or e.kind is EntryKind.CAPITAL:
            raise _bad(f"entry kind {e.kind.value}")
        p = p.record(e)
    if p.entries != tuple(entries):
        raise _bad("the ledger does not replay")


def _status_ok(o: Outcome) -> bool:
    r, od = o.result, o.result.order
    has, left = bool(r.fills), r.remaining
    expired = od.expires_at is not None and od.expires_at <= o.as_of
    return {
        OrderStatus.FILLED: has and left == 0,
        OrderStatus.PARTIALLY_FILLED: has and left > 0,
        OrderStatus.CANCELLED: left > 0,
        OrderStatus.UNSUPPORTED: not has and od.unsupported(),
        OrderStatus.SUBMITTED: not has and not expired,
        OrderStatus.EXPIRED: not has and expired,
        OrderStatus.UNFILLED: not has,
    }[r.status]


def _check(s: Simulation) -> None:
    p = s.portfolio
    if p.virtual is not True or p.account_id != s.simulation_id:
        raise _bad("not a virtual portfolio of this simulation")
    _replay(s.simulation_id, p.currency, p.entries)
    entries = {e.entry_id: e for e in p.entries}
    orders: set[str] = set()
    fills: set[str] = set()
    for o in s.outcomes:
        r, od = o.result, o.result.order
        if type(od) is not sim.VirtualOrder or od.order_id in orders:
            raise _bad("order")
        orders.add(od.order_id)
        with localcontext(DOMAIN_CONTEXT):
            done = sum((f.quantity for f in r.fills), Decimal(0))
            if done + r.remaining != od.quantity.value or not _status_ok(o):
                raise _bad(f"outcome of {od.order_id}")
            buy = od.side is Side.BUY
            for f in r.fills:
                e, gross = entries.get(f.fill_id), f.price * f.quantity
                cash = -gross - f.fees if buy else gross - f.fees
                if (e is None or f.fill_id in fills or e.kind is not EntryKind.FILL
                        or (f.order_id, e.ref) != (od.order_id, od.order_id)
                        or e.instrument_id != od.instrument_id
                        or e.effective_at != f.filled_at
                        or f.quantity <= 0 or f.price <= 0
                        or (e.units, e.fees, e.cash) != (
                            f.quantity if buy else -f.quantity, f.fees, cash)
                        or (buy and e.cost != gross)
                        or f.execution_model_id != s.execution_model_id
                        or not od.submitted_at <= f.filled_at <= o.as_of
                        or f.known_at > o.as_of
                        or f.actual_execution is not False):  # fmt: skip
                    raise _bad(f"fill {f.fill_id}")
                fills.add(f.fill_id)
    if {i for i, e in entries.items() if e.kind is EntryKind.FILL} != fills:
        raise _bad("a fill entry without its fill")


def _wire(v: object) -> object:
    """Canonical JSON value: decimals without exponent or trailing zeros (so a
    numeric(38,12) round trip hashes alike), UTC instants, enums by value."""
    if isinstance(v, BoundedDecimal):
        v = v.value
    if isinstance(v, Decimal):
        with localcontext(DOMAIN_CONTEXT):
            return "0" if v == 0 else format(v.normalize(), "f")
    if isinstance(v, datetime):
        return format_instant(v)
    if isinstance(v, InstrumentId):
        return v.to_wire()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, tuple | list):
        return [_wire(x) for x in v]
    if is_dataclass(v):
        return {f.name: _wire(getattr(v, f.name)) for f in fields(v)}
    return v


def digest(s: Simulation) -> str:
    """SHA-256 of the canonical simulation: every stored and served field."""
    body = json.dumps(_wire(s), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    key = "hashtextextended('simulation:' || %s, 0)"
    tx.conn.execute(f"SELECT {fn}({key})", (str(tx.tenant_id),))


def _rows(tx: TenantTx, sql: str, sid: str) -> list[tuple[Any, ...]]:
    cur = tx.conn.execute(sql, (tx.tenant_id, sid))
    return [tuple(r) for r in cur.fetchall()]


def _price(v: Decimal | None) -> Price | None:
    return None if v is None else Price(v)


def _strs(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise TypeError("a list of strings")
    return tuple(v)


def _build(tx: TenantTx, sid: str) -> Simulation | None:
    head = _rows(tx, "SELECT name, currency, capital, execution_model_id, "
                 "benchmark_policy_id, mode FROM app.simulation "
                 "WHERE tenant_id = %s AND id = %s", sid)  # fmt: skip
    if not head:
        return None
    name, currency, capital, model, bench, mode = head[0]
    w = "WHERE tenant_id = %s AND simulation_id = %s ORDER BY position"
    entries = []
    cols = ", ".join(AMOUNTS)
    for i, (n, eid, kind, at, iid, *amounts, ref) in enumerate(_rows(tx,
            "SELECT position, entry_id, kind, effective_at, instrument_id, "
            f"{cols}, ref FROM app.simulation_entry {w}", sid)):  # fmt: skip
        if n != i:
            raise _bad("ledger positions")
        entries.append(VirtualEntry(
            eid, EntryKind(kind), ensure_aware_utc(at),
            None if iid is None else InstrumentId(iid), sid,
            **dict(zip(AMOUNTS, amounts, strict=True)), ref=ref))  # fmt: skip
    if not entries or entries[0].capital != capital:
        raise _bad("starting capital")
    by_order: dict[str, list[sim.VirtualFill]] = {}
    for fid, oid, *rest, assumptions, amb in _rows(tx,
            "SELECT f.fill_id, f.order_id, f.filled_at, f.known_at, f.bar_start, "
            "f.quantity, f.price, f.fees, f.execution_model_id, f.assumptions, "
            "f.ambiguous FROM app.simulation_fill f JOIN app.simulation_entry e "
            "ON (e.tenant_id, e.simulation_id, e.entry_id) = "
            "(f.tenant_id, f.simulation_id, f.fill_id) WHERE f.tenant_id = %s "
            "AND f.simulation_id = %s ORDER BY e.position", sid):  # fmt: skip
        filled, known, start, qty, px, fees, mid = rest
        by_order.setdefault(oid, []).append(sim.VirtualFill(
            fid, oid, ensure_aware_utc(filled), ensure_aware_utc(known),
            ensure_aware_utc(start), qty, px, fees, mid, _strs(assumptions),
            amb))  # fmt: skip
    outcomes = []
    for i, (n, oid, iid, side, qty, style, sub, lim, stop, exp, prop, oco,
            evaluated, status, left, notes) in enumerate(_rows(tx,
            "SELECT position, order_id, instrument_id, side, quantity, style, "
            "submitted_at, limit_price, stop_price, expires_at, proposal_id, "
            f"oco_group, evaluated_at, status, remaining, notes "
            f"FROM app.simulation_order {w}", sid)):  # fmt: skip
        if n != i:
            raise _bad("order positions")
        od = sim.VirtualOrder(
            oid, InstrumentId(iid), Side(side), PositiveQuantity(qty),
            sim.OrderStyle(style), ensure_aware_utc(sub), _price(lim), _price(stop),
            None if exp is None else ensure_aware_utc(exp), prop, oco)  # fmt: skip
        fills = tuple(by_order.pop(oid, ()))
        result = sim.OrderResult(od, OrderStatus(status), fills, left, _strs(notes))
        outcomes.append(Outcome(result, ensure_aware_utc(evaluated)))
    if by_order:
        raise _bad("a fill without its order")
    portfolio = sim.VirtualPortfolio(sid, currency, tuple(entries))
    s = Simulation(name, model, bench, portfolio, tuple(outcomes), Mode(mode))
    if s.mode is not Mode.MANUAL:
        raise _bad("shadow mode is not modelled")
    _check(s)
    last = _rows(tx, "SELECT entry_count, order_count, digest FROM "
                 "app.simulation_digest WHERE tenant_id = %s AND simulation_id = %s "
                 "ORDER BY position DESC LIMIT 1", sid)  # fmt: skip
    if last != [(len(entries), len(outcomes), digest(s))]:
        raise _bad("the stored rows do not match the latest digest")
    return s


def _read(tx: TenantTx, sid: str) -> Simulation | None:
    try:
        return _build(tx, sid)
    except (KeyError, TypeError, ValueError) as exc:  # SimError is a ValueError
        raise _bad(f"{type(exc).__name__}: {exc}") from None


def load(tx: TenantTx, simulation_id: str) -> Simulation | None:
    """This tenant's simulation, replayed and re-checked; None when unknown."""
    try:
        _lock(tx, shared=True)
        return _read(tx, simulation_id)
    except psycopg.Error as exc:
        raise SimulationStoreError("database", type(exc).__name__) from None


def page(tx: TenantTx, limit: int, after: str | None) -> list[Simulation]:
    """Up to `limit` of this tenant's simulations in byte order of id after
    `after` (keyset), each replayed and re-checked."""
    try:
        _lock(tx, shared=True)
        ids = tx.conn.execute(
            "SELECT id FROM app.simulation WHERE tenant_id = %s AND (%s::text IS "
            'NULL OR id COLLATE "C" > %s) ORDER BY id COLLATE "C" LIMIT %s',
            (tx.tenant_id, after, after, limit),
        ).fetchall()
        found = [_read(tx, str(r[0])) for r in ids]
    except psycopg.Error as exc:
        raise SimulationStoreError("database", type(exc).__name__) from None
    return [s for s in found if s is not None]


def _definition(s: Simulation) -> tuple[object, ...]:
    p = s.portfolio
    return (s.name, s.execution_model_id, s.benchmark_policy_id, s.mode,
            p.currency, p.entries[0])  # fmt: skip


def save(tx: TenantTx, s: Simulation) -> None:
    """Append what `s` adds to the stored simulation (or create it)."""
    if s.mode is not Mode.MANUAL or s.portfolio.options:
        raise SimulationStoreError("unsupported", "shadow mode or written options")
    try:
        _check(s)
    except (TypeError, ValueError) as exc:
        raise _bad(str(exc)) from None
    try:
        _save(tx, s)
    except psycopg.Error as exc:  # the 0014 guard: a real journal account id
        code = "account_conflict" if "virtual account" in str(exc) else "database"
        raise SimulationStoreError(code, type(exc).__name__) from None


def _insert(
    tx: TenantTx, sid: str, table: str, cols: str, rows: list[tuple[Any, ...]]
) -> None:
    marks = ", ".join(["%s"] * (cols.count(",") + 3))
    sql = f"INSERT INTO app.{table} (tenant_id, simulation_id, {cols}) VALUES "
    with tx.conn.cursor() as cur:
        cur.executemany(f"{sql}({marks})", [(tx.tenant_id, sid, *r) for r in rows])


def _save(tx: TenantTx, s: Simulation) -> None:
    sid, p = s.simulation_id, s.portfolio
    _lock(tx, shared=False)
    old = _read(tx, sid)
    if old is None:
        tx.conn.execute(
            "INSERT INTO app.simulation (tenant_id, id, name, currency, capital, "
            "execution_model_id, benchmark_policy_id, mode) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (tx.tenant_id, sid, s.name, p.currency, p.entries[0].capital,
             s.execution_model_id, s.benchmark_policy_id, s.mode.value),
        )  # fmt: skip
        ne, no = 0, 0
    else:
        ne, no = len(old.portfolio.entries), len(old.outcomes)
        same = _definition(old) == _definition(s) and s.outcomes[:no] == old.outcomes
        if not same or p.entries[:ne] != old.portfolio.entries:
            raise SimulationStoreError("append_only", "does not extend the stored")
    _insert(tx, sid, "simulation_entry", "position, entry_id, kind, effective_at, "
            f"instrument_id, {', '.join(AMOUNTS)}, ref", [
                (i, e.entry_id, e.kind.value, e.effective_at,
                 None if e.instrument_id is None else e.instrument_id.uuid,
                 *(getattr(e, a) for a in AMOUNTS), e.ref)
                for i, e in enumerate(p.entries) if i >= ne])  # fmt: skip
    new = list(enumerate(s.outcomes))[no:]
    _insert(tx, sid, "simulation_order", "position, order_id, instrument_id, side, "
            "quantity, style, submitted_at, limit_price, stop_price, expires_at, "
            "proposal_id, oco_group, evaluated_at, status, remaining, notes", [
                (i, (od := o.result.order).order_id, od.instrument_id.uuid,
                 od.side.value, od.quantity.value, od.style.value, od.submitted_at,
                 None if od.limit_price is None else od.limit_price.value,
                 None if od.stop_price is None else od.stop_price.value,
                 od.expires_at, od.proposal_id, od.oco_group, o.as_of,
                 o.result.status.value, o.result.remaining,
                 Jsonb(list(o.result.notes)))
                for i, o in new])  # fmt: skip
    _insert(tx, sid, "simulation_fill", "fill_id, order_id, filled_at, known_at, "
            "bar_start, quantity, price, fees, execution_model_id, assumptions, "
            "ambiguous", [
                (f.fill_id, f.order_id, f.filled_at, f.known_at, f.bar_start,
                 f.quantity, f.price, f.fees, f.execution_model_id,
                 Jsonb(list(f.assumptions)), f.ambiguous)
                for _, o in new for f in o.result.fills])  # fmt: skip
    if old is None or (ne, no) != (len(p.entries), len(s.outcomes)):
        tx.conn.execute(
            "INSERT INTO app.simulation_digest (tenant_id, simulation_id, position, "
            "entry_count, order_count, digest) SELECT %s, %s, count(*), %s, %s, %s "
            "FROM app.simulation_digest WHERE tenant_id = %s AND simulation_id = %s",
            (tx.tenant_id, sid, len(p.entries), len(s.outcomes), digest(s),
             tx.tenant_id, sid),
        )  # fmt: skip
    if _read(tx, sid) != s:
        raise _bad("the stored simulation differs")
