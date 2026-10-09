"""Float-column and money-column guard (ADR-013; T008 review section 3.2 and section 4).

Positive run on the real migrated schema, then SYNTHETIC negative controls: columns
added out of band must be reported, and a migration that adds one is rolled back.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters.migrations import (
    MIGRATIONS_DIR,
    MigrationError,
    applied_migrations,
    migrate,
)
from qw_adapters.schema_guard import (
    column_type_violations,
    is_money_name,
    numeric_precision_scale,
)

Conn = psycopg.Connection[TupleRow]


def test_numeric_typmod_decoding() -> None:
    # Independent oracle: PostgreSQL packs ((precision << 16) | scale) + 4.
    assert numeric_precision_scale((38 << 16 | 12) + 4) == (38, 12)
    assert numeric_precision_scale((20 << 16 | 6) + 4) == (20, 6)
    assert numeric_precision_scale((5 << 16 | (-2 & 0x7FF)) + 4) == (5, -2)
    assert numeric_precision_scale(-1) is None


@pytest.mark.parametrize(
    ("name", "money"),
    [
        ("amount", True), ("unit_price", True), ("fee_amount", True),
        ("planned_cost", True), ("model_cost_usd", True), ("qty", True),
        ("price_currency", False), ("price_source", False), ("costume", False),
        ("id", False), ("amount_kind", False),
    ],
)  # fmt: skip
def test_money_name_rule(name: str, money: bool) -> None:
    assert is_money_name(name) is money


@pytest.mark.db
def test_real_schema_has_no_violations(conn: Conn) -> None:
    migrate(conn)
    assert column_type_violations(conn) == []


@pytest.mark.db
def test_guard_reports_each_forbidden_shape(conn: Conn) -> None:
    migrate(conn)
    conn.execute(
        """
        CREATE DOMAIN app.score AS double precision;
        CREATE DOMAIN app.money_amount AS numeric(38, 12);
        CREATE TYPE app.frange AS RANGE (subtype = float8);
        CREATE TABLE app.synthetic (
            ok_amount app.money_amount,
            unit_price numeric(38, 12),
            fx_multiplier numeric(24, 12),
            model_cost_usd numeric(20, 6),
            plain_ratio numeric,
            a real,
            b float,
            c double precision[],
            d app.score,
            e app.frange,
            f money,
            cash_balance numeric,
            fee_amount numeric(10, 2),
            quantity bigint,
            strike text
        );
        CREATE VIEW public.v AS SELECT b AS view_float FROM app.synthetic;
        CREATE TYPE public.pair AS (x real);
        """
    )
    found = column_type_violations(conn)
    flagged = sorted(p.split(" ", 1)[0] for p in found)
    assert flagged == sorted(
        [
            *(f"app.synthetic.{c}" for c in "abcdef"),
            "app.synthetic.cash_balance",
            "app.synthetic.fee_amount",
            "app.synthetic.quantity",
            "app.synthetic.strike",
            "public.v.view_float",
            "public.pair.x",
        ]
    )
    assert any(p == "app.synthetic.a (real): real is forbidden" for p in found)
    assert any(p.startswith("app.synthetic.f (money): money is") for p in found)


@pytest.mark.db
@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE app.t (id integer, score double precision);",
        "CREATE TABLE app.t (id integer);\nALTER TABLE app.t ADD COLUMN w real;",
        "CREATE TABLE app.t (id integer, price numeric);",
    ],
)
def test_migration_adding_float_or_loose_money_column_is_rolled_back(
    conn: Conn, tmp_path: Path, ddl: str
) -> None:
    shutil.copy(MIGRATIONS_DIR / "0001_roles_and_baseline.sql", tmp_path)
    (tmp_path / "0002_bad_column.sql").write_text(f"-- SYNTHETIC\n{ddl}\n")
    with pytest.raises(MigrationError, match=r"column type policy: app\.t\."):
        migrate(conn, tmp_path)
    assert sorted(applied_migrations(conn)) == [1]
    exists = conn.execute("SELECT to_regclass('app.t') IS NOT NULL").fetchone()
    assert exists == (False,)
