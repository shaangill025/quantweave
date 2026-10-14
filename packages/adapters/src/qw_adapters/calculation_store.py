"""Persisted calculation receipts (T054 increment 2; `0011_calculation_record.sql`;
spec §5 numeric-function outputs; T008 C-15).

The domain `CalculationRecord` is the model; this module stores and reloads it in the
caller's tenant transaction.
- `record` checks the domain rules and inserts the canonical record under its id
  `calc:<content hash>`. The id is content-addressed and excludes computed_at, so a
  recomputation of the same figure is ignored and the first computed_at is kept;
  an existing row whose hash or body differs from `rec` is an integrity error.
  computed_at is outside the hash: only `as_of <= computed_at` guards it.
- `load` returns the tenant's record or None (another tenant's id is unknown here).
  It recomputes the content hash and re-checks the domain rules (status ok iff a
  result exists, the unavailable code's status, canonical decimals, as_of not after
  computed_at); any disagreement raises `CalculationStoreError("integrity")`, so a
  tampered or forged row never reaches a caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb
from qw_domain import calculation as dc
from qw_domain.instants import ensure_aware_utc, parse_instant
from qw_domain.valuation import Unavailable

from qw_adapters.tenancy import TenantTx

PREFIX = "calc:"


class CalculationStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def to_json(rec: dc.CalculationRecord) -> dict[str, object]:
    return rec.canonical()


def _input(j: Mapping[str, Any]) -> dc.CalcInput:
    at = j["as_of"]
    return dc.CalcInput(j["name"], dc.InputKind(j["kind"]), j["value"], j["unit"],
                        j["source_record_id"],
                        None if at is None else parse_instant(at))  # fmt: skip


def _check(rec: dc.CalculationRecord) -> None:
    gap, ok = rec.unavailable, rec.status is dc.CalcStatus.OK
    names = [i.name for i in rec.inputs]
    rules = (
        dc._ID.fullmatch(rec.formula_id) is not None,
        bool(rec.formula_version and rec.unit and rec.rounding),
        rec.coverage in ("complete", "partial"),
        len(set(names)) == len(names),
        all(type(i) is dc.CalcInput for i in rec.inputs),
        rec.as_of <= rec.computed_at,
        (rec.result is not None) == ok == (gap is None),
        gap is None or rec.status is dc._status(gap),
        rec.result is None or (len(rec.result) <= dc.MAX_DECIMAL_CHARS
                               and dc._CANONICAL.fullmatch(rec.result) is not None),
        all(isinstance(w, str) for w in rec.warnings),
    )  # fmt: skip
    if not all(rules):
        raise CalculationStoreError("integrity", "the record breaks a domain rule")


def from_json(j: Mapping[str, Any], computed_at: datetime) -> dc.CalculationRecord:
    """Rebuild a stored record; anything malformed is an integrity error."""
    try:
        if j["format"] != dc.RECORD_FORMAT:
            raise ValueError("record format")
        gap = j["unavailable"]
        rec = dc.CalculationRecord(
            j["formula_id"], j["formula_version"],
            tuple(_input(i) for i in j["inputs"]), j["result"], j["unit"],
            dc.CalcStatus(j["status"]),
            None if gap is None else Unavailable(*gap), tuple(j["warnings"]),
            j["rounding"], parse_instant(j["as_of"]), j["coverage"],
            ensure_aware_utc(computed_at),
        )  # fmt: skip
    except (KeyError, TypeError, ValueError) as exc:
        raise CalculationStoreError("integrity", type(exc).__name__) from None
    _check(rec)
    if rec.canonical() != dict(j):
        raise CalculationStoreError("integrity", "the record is not canonical")
    return rec


def _insert(tx: TenantTx, rec: dc.CalculationRecord) -> str:
    calc_id = PREFIX + rec.content_hash
    tx.conn.execute(
        "INSERT INTO app.calculation_record (tenant_id, id, content_hash, record, "
        "computed_at) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, id) DO NOTHING",
        (tx.tenant_id, calc_id, rec.content_hash, Jsonb(to_json(rec)),
         rec.computed_at),
    )  # fmt: skip
    return calc_id


def record(tx: TenantTx, rec: dc.CalculationRecord) -> str:
    """Store `rec` in this tenant; returns its id. A stored id is left unchanged,
    and must hold this same record, or this is an integrity error."""
    _check(rec)
    calc_id = _insert(tx, rec)
    row = tx.conn.execute(
        "SELECT content_hash, record FROM app.calculation_record "
        "WHERE tenant_id = %s AND id = %s",
        (tx.tenant_id, calc_id),
    ).fetchone()
    if row is None or tuple(row) != (rec.content_hash, to_json(rec)):
        raise CalculationStoreError("integrity", "the stored record differs")
    return calc_id


def load(tx: TenantTx, calc_id: str) -> dc.CalculationRecord | None:
    row = tx.conn.execute(
        "SELECT content_hash, record, computed_at FROM app.calculation_record "
        "WHERE tenant_id = %s AND id = %s",
        (tx.tenant_id, calc_id),
    ).fetchone()
    if row is None:
        return None
    digest, body, computed_at = row
    rec = from_json(body, computed_at)
    if rec.content_hash != digest or calc_id != PREFIX + digest:
        raise CalculationStoreError("integrity", "content hash")
    return rec
