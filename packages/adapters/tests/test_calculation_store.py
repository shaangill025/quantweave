"""Calculation receipts (0011_calculation_record, qw_adapters.calculation_store) on
PostgreSQL 16, as the non-superuser qw_app/qw_worker logins of the conftest harness.
The superuser `conn` only observes or plays a tampering owner where a test says so.
All inputs, sources and accounts are SYNTHETIC. Hand-computed oracle: a simple
return from 100 to 110 is (110 - 100) / 100 = 0.1."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb
from qw_adapters import calculation_store as cs
from qw_adapters import tenancy as tn
from qw_domain.calculation import (
    CalcInput,
    CalculationRecord,
    record_calculation,
)
from qw_domain.decimals import Money, Ratio
from qw_domain.valuation import Unavailable

Conn = psycopg.Connection[TupleRow]
T0 = datetime(2026, 10, 1, 14, tzinfo=UTC)


def simple_return(computed_at: datetime = T0) -> CalculationRecord:
    """SYNTHETIC: (110 - 100) / 100 = 0.1, with a date and an as-of input."""
    return record_calculation(
        "simple_return/1",
        [CalcInput.money("start_value", Money.of("100", "CAD"), "acct-syn-1"),
         CalcInput.money("end_value", Money.of("110", "CAD"), "acct-syn-1",
                         as_of=T0 - timedelta(hours=1)),
         CalcInput.journal_revision("acct-syn-1", 4),
         CalcInput.reference("series", "syn-series@2", "series", "syn-series")],
        Ratio("0.1"), unit="ratio", rounding="half_even:1e-18",
        as_of=T0 - timedelta(hours=1), computed_at=computed_at, coverage="complete",
    )  # fmt: skip


def gap() -> CalculationRecord:
    return record_calculation(
        "mwr/1", [CalcInput.decimal("flows", Decimal(2), "count", "acct-syn-1")],
        Unavailable("no_sign_change", "SYNTHETIC: all flows positive"),
        unit="ratio", rounding="half_even:1e-18", as_of=T0, computed_at=T0,
        coverage="partial",
    )  # fmt: skip


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


def put(c: Conn, t: uuid.UUID, rec: CalculationRecord) -> str:
    with tn.tenant_transaction(c, t) as tx:
        return cs.record(tx, rec)


def get(c: Conn, t: uuid.UUID, calc_id: str) -> CalculationRecord | None:
    with tn.tenant_transaction(c, t) as tx:
        return cs.load(tx, calc_id)


@pytest.mark.db
def test_round_trip_keeps_the_record_and_its_first_computed_at(
    app_conn: Conn, worker_conn: Conn
) -> None:
    a = tenant(app_conn)
    rec = simple_return()
    calc_id = put(worker_conn, a, rec)
    assert calc_id == f"calc:{rec.content_hash}"
    loaded = get(app_conn, a, calc_id)
    assert loaded == rec
    assert loaded is not None and loaded.result == "0.1"
    assert loaded.to_contract() == rec.to_contract()
    # The same figure recomputed later has the same id; the first time is kept.
    again = simple_return(T0 + timedelta(days=1))
    assert put(app_conn, a, again) == calc_id
    assert get(app_conn, a, calc_id) == rec
    unavailable = gap()
    assert get(app_conn, a, put(app_conn, a, unavailable)) == unavailable
    assert get(app_conn, a, "calc:" + "0" * 64) is None
    assert get(app_conn, a, "not-a-calc-id") is None


@pytest.mark.db
def test_other_tenants_see_nothing(app_conn: Conn, conn: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    calc_id = put(app_conn, a, simple_return())
    assert get(app_conn, b, calc_id) is None
    put(app_conn, b, simple_return())  # the same figure, computed in tenant B
    rows = conn.execute(
        "SELECT tenant_id FROM app.calculation_record WHERE id = %s ORDER BY 1",
        (calc_id,),
    ).fetchall()
    assert sorted(r[0] for r in rows) == sorted([a, b])
    with tn.tenant_transaction(app_conn, b) as tx:
        seen = tx.conn.execute("SELECT tenant_id FROM app.calculation_record")
        assert [r[0] for r in seen.fetchall()] == [b]


@pytest.mark.db
def test_rows_are_append_only_and_checked(app_conn: Conn) -> None:
    a = tenant(app_conn)
    calc_id = put(app_conn, a, simple_return())
    for statement in ("UPDATE app.calculation_record SET computed_at = now()",
                      "DELETE FROM app.calculation_record",
                      "TRUNCATE app.calculation_record"):  # fmt: skip
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            tx.conn.execute(statement)
    # id not the content hash; a malformed hash; a non-object record
    bad: list[tuple[str, str, object]] = [
        ("calc:" + "1" * 64, "2" * 64, {}),
        ("calc:xyz", "xyz", {}),
        ("calc:" + "3" * 64, "3" * 64, []),
    ]
    for row_id, digest, body in bad:
        with (
            pytest.raises(errors.CheckViolation),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            tx.conn.execute(
                "INSERT INTO app.calculation_record (tenant_id, id, content_hash, "
                "record, computed_at) VALUES (%s, %s, %s, %s, %s)",
                (a, row_id, digest, Jsonb(body), T0),
            )
    assert get(app_conn, a, calc_id) is not None


@pytest.mark.db
def test_tampering_and_hash_consistent_forgeries_fail_load(
    app_conn: Conn, conn: Conn
) -> None:
    a = tenant(app_conn)
    calc_id = put(app_conn, a, simple_return())
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(
        "UPDATE app.calculation_record SET record = jsonb_set(record, '{result}', "
        "'\"0.2\"')"
    )
    with pytest.raises(cs.CalculationStoreError, match="integrity"):
        get(app_conn, a, calc_id)
    # A hostile writer stores records whose hashes agree but that break the domain
    # rules: a result with an unavailable status, as_of after computed_at.
    ok = simple_return()
    forgeries = [
        replace(ok, result=None),
        replace(gap(), result="0.5"),
        replace(ok, as_of=ok.computed_at + timedelta(seconds=1)),
    ]
    for forged in forgeries:
        with tn.tenant_transaction(app_conn, a) as tx:
            forged_id = cs._insert(tx, forged)  # skips the domain checks
        with pytest.raises(cs.CalculationStoreError, match="integrity"):
            get(app_conn, a, forged_id)


def test_record_json_is_decimal_text_only() -> None:
    body = cs.to_json(simple_return())
    text = json.dumps(body)
    assert '"value": "110"' in text and '"result": "0.1"' in text
    assert cs.from_json(body, T0) == simple_return()
    with pytest.raises(cs.CalculationStoreError, match="integrity"):
        cs.from_json({**body, "format": "calculation_record/0"}, T0)


def raw_insert(c: Conn, t: uuid.UUID, row_tenant: uuid.UUID, digest: str,
               body: object) -> None:  # fmt: skip
    """Writes a row as a hostile qw_app would, bypassing the adapter."""
    with tn.tenant_transaction(c, t) as tx:
        tx.conn.execute(
            "INSERT INTO app.calculation_record (tenant_id, id, content_hash, "
            "record, computed_at) VALUES (%s, %s, %s, %s, %s)",
            (row_tenant, f"calc:{digest}", digest, Jsonb(body), T0),
        )


@pytest.mark.db
def test_recording_over_a_poisoned_row_is_refused(app_conn: Conn) -> None:
    """A row under the legitimate id whose body differs (its hash column agrees
    with the id): recording the real figure must not silently keep the forgery."""
    a, rec = tenant(app_conn), simple_return()
    raw_insert(app_conn, a, a, rec.content_hash, {**cs.to_json(rec), "result": "0.2"})
    with (
        pytest.raises(cs.CalculationStoreError, match="integrity"),
        tn.tenant_transaction(app_conn, a) as tx,
    ):
        cs.record(tx, rec)


@pytest.mark.db
def test_a_non_canonical_row_with_a_matching_hash_fails_load(app_conn: Conn) -> None:
    """An extra key is outside the canonical form, so the recomputed hash still
    matches; only the canonical round-trip check refuses it."""
    a, rec = tenant(app_conn), simple_return()
    raw_insert(app_conn, a, a, rec.content_hash, {**cs.to_json(rec), "extra": 1})
    with pytest.raises(cs.CalculationStoreError, match="integrity"):
        get(app_conn, a, f"calc:{rec.content_hash}")


@pytest.mark.db
def test_a_row_for_another_tenant_cannot_be_inserted(app_conn: Conn) -> None:
    a, b, rec = tenant(app_conn), tenant(app_conn), simple_return()
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        raw_insert(app_conn, a, b, rec.content_hash, cs.to_json(rec))
    assert get(app_conn, b, f"calc:{rec.content_hash}") is None
