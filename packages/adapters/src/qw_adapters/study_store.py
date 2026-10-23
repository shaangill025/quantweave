"""The study input store (`0045_study_input_store.sql`; owner
decisions 70, 72 and 74). The strategy search space and the evaluation protocol an
assessment request binds by content hash (decision 67) are stored here, so the
assessor can load them:
- `record_space(tx, space)` and `record_evaluation(tx, protocol)`: qw_worker only
  (decision 72). The body is the domain's strict wire, keyed on tenant and content
  hash. A different content under an id already recorded is `study_input_exists`
  (decision 74, refused by the database's unique index on the id); an identical
  re-record is a no-op that keeps the first row and its time. Each returns True
  when it stored a row, False for the no-op.
- `space(tx, content_hash)` and `evaluation(tx, content_hash)`: qw_assessor only.
  Each re-hashes the event and the content and re-parses the body strictly
  (`SearchSpace.from_wire`, `EvaluationProtocol.from_wire`), else `integrity`.
  A missing row is None.
- Every protocol row carries `approved_by_caller_asserted` (decision 72), true and
  hashed: `approved_by` is whatever the caller wrote, unverified, until a step-up
  approval flow exists. `StoredEvaluation` carries the mark to the reader.
Any other login is `forbidden` (the database refuses it). Times are the database
clock. LIMITATIONS: nothing loads these in the assessor run yet (the wiring
increment); the store does not check a space against its base manifest or a
protocol against its plan (`StrategyStudy` and `check_plan` do, in the domain)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.backtest import EvaluationProtocol
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import digest
from qw_domain.strategy_improvement import SearchSpace

from qw_adapters.tenancy import TenantTx

CALLER_ASSERTED = True  # decision 72: the only value until step-up approval exists
_SPACE = ("space_id", "content_hash", "body", "recorded_at", "event_hash")
_EVAL = ("protocol_id", "content_hash", "body", "approved_by_caller_asserted",
         "recorded_at", "event_hash")  # fmt: skip


class StudyStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredSpace:
    space: SearchSpace
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEvaluation:
    protocol: EvaluationProtocol
    approved_by_caller_asserted: bool  # decision 72: `approved_by` is unverified
    recorded_at: datetime


def _space_hash(tx: TenantTx, h: str, wire: object, at: datetime) -> str:
    return digest(["strategy_search_space", str(tx.tenant_id), h, wire,
                   format_instant(at)])  # fmt: skip


def _eval_hash(tx: TenantTx, h: str, wire: object, mark: bool, at: datetime) -> str:
    return digest(["evaluation_protocol", str(tx.tenant_id), h, wire, mark,
                   format_instant(at)])  # fmt: skip


def _database(exc: psycopg.Error) -> StudyStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return StudyStoreError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return StudyStoreError("study_input_exists", type(exc).__name__)
    return StudyStoreError("database", type(exc).__name__)


def _insert(
    tx: TenantTx, table: str, cols: tuple[str, ...],
    row: Callable[[datetime], tuple[object, ...]],
) -> bool:  # fmt: skip
    """Insert `row(at)` at the database clock's `at`. Only identical content
    conflicts on the primary key (tenant, content_hash): that is the no-op, and the
    stored row and its time are kept. Any other unique violation is the id's
    index, a different content (decision 74). qw_worker holds no SELECT, so
    `ON CONFLICT (tenant_id, content_hash)` is not available to it."""
    try:
        with tx.conn.transaction():
            now = tx.conn.execute("SELECT clock_timestamp()").fetchone()
            assert now is not None
            tx.conn.execute(
                f"INSERT INTO app.{table} (tenant_id, {', '.join(cols)}) VALUES "
                f"({', '.join(['%s'] * (len(cols) + 1))})",
                (tx.tenant_id, *row(ensure_aware_utc(now[0]))),
            )
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == f"{table}_pkey":
            return False
        raise _database(exc) from None
    except psycopg.Error as exc:
        raise _database(exc) from None
    return True


def record_space(tx: TenantTx, space: SearchSpace) -> bool:
    """Store `space` (qw_worker); False when this content was already stored."""
    wire, h = space.to_wire(), space.content_hash
    return _insert(tx, "strategy_search_space", _SPACE, lambda at: (
        space.space_id, h, Jsonb(wire), at, _space_hash(tx, h, wire, at)))  # fmt: skip


def record_evaluation(tx: TenantTx, protocol: EvaluationProtocol) -> bool:
    """Store `protocol` (qw_worker), marked caller-asserted (decision 72); False
    when this content was already stored."""
    wire, h, mark = protocol.to_wire(), protocol.content_hash, CALLER_ASSERTED
    return _insert(tx, "evaluation_protocol", _EVAL, lambda at: (
        protocol.protocol_id, h, Jsonb(wire), mark, at,
        _eval_hash(tx, h, wire, mark, at)))  # fmt: skip


def _row(tx: TenantTx, table: str, cols: tuple[str, ...], h: str,
         ) -> dict[str, Any] | None:  # fmt: skip
    try:
        row = tx.conn.execute(
            f"SELECT {', '.join(cols)} FROM app.{table} WHERE tenant_id = %s "
            "AND content_hash = %s",
            (tx.tenant_id, h),
        ).fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    return None if row is None else dict(zip(cols, row, strict=True))


_BROKEN = (ValueError, TypeError, KeyError, AttributeError)


def space(tx: TenantTx, content_hash: str) -> StoredSpace | None:
    """The stored search space with this content hash, re-verified, or None."""
    r = _row(tx, "strategy_search_space", _SPACE, content_hash)
    if r is None:
        return None
    try:
        s, at = SearchSpace.from_wire(r["body"]), ensure_aware_utc(r["recorded_at"])
        if (r["space_id"], r["content_hash"]) != (s.space_id, content_hash) or (
            s.content_hash != content_hash
            or r["event_hash"] != _space_hash(tx, content_hash, s.to_wire(), at)
        ):  # fmt: skip
            raise ValueError("strategy_search_space")
    except _BROKEN as exc:  # the domain errors are ValueErrors
        raise StudyStoreError("integrity", type(exc).__name__) from None
    return StoredSpace(s, at)


def evaluation(tx: TenantTx, content_hash: str) -> StoredEvaluation | None:
    """The stored evaluation protocol with this content hash, re-verified, or
    None. The mark is checked to be the one value decision 72 allows."""
    r = _row(tx, "evaluation_protocol", _EVAL, content_hash)
    if r is None:
        return None
    try:
        p = EvaluationProtocol.from_wire(r["body"])
        at, mark = ensure_aware_utc(r["recorded_at"]), r["approved_by_caller_asserted"]
        if (r["protocol_id"], r["content_hash"]) != (p.protocol_id, content_hash) or (
            p.content_hash != content_hash or mark is not CALLER_ASSERTED
            or r["event_hash"] != _eval_hash(tx, content_hash, p.to_wire(), mark, at)
        ):  # fmt: skip
            raise ValueError("evaluation_protocol")
    except _BROKEN as exc:
        raise StudyStoreError("integrity", type(exc).__name__) from None
    return StoredEvaluation(p, mark, at)
