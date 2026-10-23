"""The protected holdout series store (`0036_holdout_series.sql`; owner decisions
41, 51, 52 and 53). Used on a qw_assessor connection only: the operator
preregisters each plan's holdout series and its baseline series (`series_role` is
'holdout' or the baseline's series id) before any assessor protocol names the
plan, and the assessor reads them back as `HoldoutData`. The database enforces
who may read or write (qw_assessor alone; any other login is `forbidden`), the
freeze (`plan_frozen`), one row per plan, instrument and role (`series_exists`;
a mistake is fixed with a new plan), tenant isolation and append-only. Every read
rebuilds each row through the domain wire and re-hashes it; a mismatch is
`integrity`. One row per plan and baseline (decision 54): a second load of a
baseline is `series_exists` at load. Should a baseline role still be read for
more than one instrument of the plan, the read refuses it as `baseline_ambiguous`
(no choice of series is ever made). LIMITATIONS: the store
does not check the series against the plan's frozen holdout instruments or the
protocol's baselines (the plan is not stored until the freeze); the domain run
(`holdout_run`) refuses a series it may not use."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.backtest import PriceSeries
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import ResearchError, check_id, digest
from qw_domain.strategy_improvement import HoldoutData

from qw_adapters.tenancy import TenantTx

HOLDOUT = "holdout"
_COLS = (
    "tenant_id",
    "plan_id",
    "instrument_id",
    "series_role",
    "series_id",
    "content_hash",
    "body",
    "recorded_at",
    "event_hash",
)


class HoldoutSeriesError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredSeries:
    plan_id: str
    series_role: str
    series: PriceSeries
    recorded_at: datetime


def _hash(tx: TenantTx, plan_id: str, role: str, wire: object, at: datetime) -> str:
    return digest(
        ["holdout_series", str(tx.tenant_id), plan_id, role, wire, format_instant(at)]
    )


def _database(exc: psycopg.Error) -> HoldoutSeriesError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return HoldoutSeriesError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return HoldoutSeriesError("series_exists", type(exc).__name__)
    if "plan_frozen" in (exc.diag.message_primary or ""):
        return HoldoutSeriesError("plan_frozen", type(exc).__name__)
    return HoldoutSeriesError("database", type(exc).__name__)


def preregister(tx: TenantTx, plan_id: str, role: str, series: PriceSeries) -> None:
    """Store `series` for `plan_id` under `role` before the plan's freeze."""
    try:
        check_id(plan_id, "plan")
        check_id(role, "series_role")
        wire = series.to_wire()
    except ResearchError as exc:
        raise HoldoutSeriesError("invalid", str(exc)) from None
    if role != HOLDOUT and role != series.series_id:
        raise HoldoutSeriesError("role_mismatch", role)
    if series.tenant_id != str(tx.tenant_id):
        raise HoldoutSeriesError("tenant_mismatch", series.series_id)
    try:
        with tx.conn.transaction():
            row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
            assert row is not None
            at = ensure_aware_utc(row[0])
            values = (
                tx.tenant_id,
                plan_id,
                series.instrument.uuid,
                role,
                series.series_id,
                series.content_hash,
                Jsonb(wire),
                at,
                _hash(tx, plan_id, role, wire, at),
            )
            tx.conn.execute(
                f"INSERT INTO app.holdout_series ({', '.join(_COLS)}) "
                f"VALUES ({', '.join(['%s'] * len(_COLS))})",
                values,
            )
    except psycopg.Error as exc:
        raise _database(exc) from None


def series(tx: TenantTx, plan_id: str) -> tuple[StoredSeries, ...]:
    """Every stored series of `plan_id`, each re-verified, by role and instrument."""
    try:
        cur = tx.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM app.holdout_series WHERE tenant_id = %s "
            "AND plan_id = %s ORDER BY series_role, instrument_id",
            (tx.tenant_id, plan_id),
        )
        rows = [dict(zip(_COLS, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise _database(exc) from None
    return tuple(_verified(tx, r) for r in rows)


def _verified(tx: TenantTx, r: dict[str, Any]) -> StoredSeries:
    try:
        s = PriceSeries.from_wire(r["body"])
        at = ensure_aware_utc(r["recorded_at"])
        role = r["series_role"]
        if (
            (r["tenant_id"], r["instrument_id"], r["series_id"], r["content_hash"])
            != (tx.tenant_id, s.instrument.uuid, s.series_id, s.content_hash)
            or s.tenant_id != str(tx.tenant_id)
            or (role != HOLDOUT and role != s.series_id)
            or r["event_hash"] != _hash(tx, r["plan_id"], role, s.to_wire(), at)
        ):
            raise ValueError("holdout_series")
    except (ValueError, TypeError, KeyError, ResearchError) as exc:
        raise HoldoutSeriesError("integrity", type(exc).__name__) from None
    return StoredSeries(r["plan_id"], role, s, at)


def holdout_data(tx: TenantTx, plan_id: str, instrument: InstrumentId) -> HoldoutData:
    """The plan's holdout series for `instrument` and all its baseline series."""
    stored = series(tx, plan_id)
    main = [
        x.series
        for x in stored
        if x.series_role == HOLDOUT and x.series.instrument == instrument
    ]
    if not main:
        raise HoldoutSeriesError("series_missing", plan_id)
    baselines: dict[str, PriceSeries] = {}
    for x in stored:
        if x.series_role != HOLDOUT:
            if x.series_role in baselines:
                raise HoldoutSeriesError("baseline_ambiguous", x.series_role)
            baselines[x.series_role] = x.series
    return HoldoutData(main[0], baselines)
