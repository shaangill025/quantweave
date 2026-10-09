"""Catalogue guard for column types (ADR-013 decimal policy; T008 review section 3.2).

Convention, checked by `migrations.migrate` inside every migration's transaction (a
violation rolls the migration back) and by the DB tests:

1. No column of a user relation (table, partitioned table, view, materialized view,
   foreign table, composite type) in a non-system schema has base type `real`,
   `double precision` (`float`) or `money`, directly or through a domain, array,
   range or multirange.
2. A money-named column, whose last `_`-separated token is in MONEY_TOKENS or whose
   name ends in `_usd`, must be `numeric(p,s)` with (p, s) one of the section 3.2
   storage classes in DECIMAL_STORAGE. Extend MONEY_TOKENS when a new money noun is
   used in a column name.

A PostgreSQL event trigger was not used: CREATE EVENT TRIGGER needs a superuser,
migrations after 0001 run as the non-superuser `qw_migrate`, and managed PostgreSQL
services may not allow it.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

FLOAT4, FLOAT8, MONEY, NUMERIC = 700, 701, 790, 1700  # fixed built-in type OIDs
FORBIDDEN = {FLOAT4: "real", FLOAT8: "double precision", MONEY: "money"}
DECIMAL_STORAGE: dict[tuple[int, int], str] = {
    (38, 12): "MoneyAmount, Price, Quantity",
    (24, 12): "Multiplier",
    (30, 18): "FxRate, Ratio",
    (20, 6): "UsdBudget",
}
MONEY_TOKENS = frozenset(
    {
        "amount", "balance", "budget", "cash", "commitment", "cost", "fee", "fees",
        "multiplier", "nav", "notional", "price", "proceeds", "qty", "quantity",
        "strike",
    }
)  # fmt: skip

_COLUMNS = """
SELECT n.nspname, c.relname, a.attname, a.atttypid, a.atttypmod,
       format_type(a.atttypid, a.atttypmod)
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'c')
  AND n.nspname <> 'information_schema' AND n.nspname !~ '^pg_'
ORDER BY 1, 2, a.attnum
"""
_TYPES = """
SELECT t.oid, t.typtype, t.typbasetype, t.typtypmod, t.typelem, t.typcategory,
       r.rngsubtype, mr.rngsubtype
FROM pg_catalog.pg_type t
LEFT JOIN pg_catalog.pg_range r ON r.rngtypid = t.oid
LEFT JOIN pg_catalog.pg_range mr ON mr.rngmultitypid = t.oid
"""


@dataclass(frozen=True, slots=True)
class _Type:
    kind: str
    base: int
    typmod: int
    elem: int
    category: str
    subtype: int | None


def _resolve(oid: int, typmod: int, types: dict[int, _Type]) -> tuple[int, int]:
    """Follow domains, arrays, ranges and multiranges to the underlying type."""
    for _ in range(64):
        t = types[oid]
        if t.kind == "d":
            oid, typmod = t.base, typmod if typmod != -1 else t.typmod
        elif t.category == "A" and t.elem:
            oid = t.elem
        elif t.kind in {"r", "m"} and t.subtype is not None:
            oid, typmod = t.subtype, -1
        else:
            return oid, typmod
    raise RuntimeError(f"type {oid} does not resolve")


def numeric_precision_scale(typmod: int) -> tuple[int, int] | None:
    """(precision, scale) of a numeric typmod, or None when unconstrained."""
    if typmod < 4:
        return None
    packed = typmod - 4
    return (packed >> 16) & 0xFFFF, ((packed & 0x7FF) ^ 1024) - 1024


def is_money_name(column: str) -> bool:
    return column.rsplit("_", 1)[-1] in MONEY_TOKENS or column.endswith("_usd")


def column_type_violations(conn: psycopg.Connection[TupleRow]) -> list[str]:
    """Columns that break the convention, as `schema.relation.column: reason`."""
    types = {
        int(oid): _Type(str(k), int(b), int(m), int(e), str(cat), sub or msub)
        for oid, k, b, m, e, cat, sub, msub in conn.execute(_TYPES).fetchall()
    }
    problems: list[str] = []
    for schema, rel, col, oid, typmod, shown in conn.execute(_COLUMNS).fetchall():
        where = f"{schema}.{rel}.{col} ({shown})"
        base, base_mod = _resolve(int(oid), int(typmod), types)
        if base in FORBIDDEN:
            problems.append(f"{where}: {FORBIDDEN[base]} is forbidden")
        elif is_money_name(str(col)) and (
            base != NUMERIC or numeric_precision_scale(base_mod) not in DECIMAL_STORAGE
        ):
            allowed = ", ".join(f"numeric({p},{s})" for p, s in DECIMAL_STORAGE)
            problems.append(f"{where}: money-named column must be one of {allowed}")
    return problems
