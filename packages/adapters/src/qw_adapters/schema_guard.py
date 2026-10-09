"""Catalogue guard for column types (ADR-013 decimal policy; T008 review section 3.2).

Checked by `migrations.migrate` inside every migration's transaction and by the DB
tests, over columns of user tables, views, materialized and foreign tables and
composite types, resolving domains, arrays, ranges and multiranges:
1. No `real`, `double precision` (`float`) or `money`.
2. Every `numeric` is `numeric(p,s)` with (p, s) a section 3.2 class in
   DECIMAL_STORAGE, unless the column has a reviewed EXEMPT entry.
3. Second net: a money-named column must be numeric. Names are split on
   non-alphanumerics and camelCase and lowercased; any MONEY_TOKENS token makes a
   name money-named unless the last token is in NON_AMOUNT_SUFFIXES.
Needs a UTF8 database (on SQL_ASCII psycopg returns catalogue text as bytes). No
event trigger: it needs a superuser, and later migrations run as `qw_migrate`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import psycopg
from psycopg.rows import TupleRow

FLOAT4, FLOAT8, MONEY, NUMERIC = 700, 701, 790, 1700  # fixed built-in type OIDs
FORBIDDEN = {FLOAT4: "real", FLOAT8: "double precision", MONEY: "money"}
DECIMAL_STORAGE: dict[tuple[int, int], str] = {
    (38, 12): "MoneyAmount, Price, Quantity", (24, 12): "Multiplier",
    (30, 18): "FxRate, Ratio", (20, 6): "UsdBudget",
}  # fmt: skip
MONEY_TOKENS = frozenset(
    {
        "amount", "balance", "budget", "cash", "collateral", "commission",
        "commitment", "cost", "credit", "debit", "dividend", "dividends", "equity",
        "exposure", "fee", "fees", "fx", "income", "margin", "multiplier", "nav",
        "notional", "pnl", "premium", "price", "proceeds", "px", "qty", "quantity",
        "rate", "strike", "tax", "units", "usd", "value",
    }
)  # fmt: skip
NON_AMOUNT_SUFFIXES = frozenset(
    {"ccy", "code", "currency", "id", "kind", "method", "source", "status", "type"}
)
# Reviewed exemptions: "schema.relation.column" -> reason. None are needed yet.
EXEMPT: dict[str, str] = {}

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
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", column).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", spaced) if t]
    return (
        bool(tokens)
        and tokens[-1] not in NON_AMOUNT_SUFFIXES
        and any(t in MONEY_TOKENS for t in tokens)
    )


def column_type_violations(conn: psycopg.Connection[TupleRow]) -> list[str]:
    """Columns that break the convention, as `schema.relation.column (type): reason`."""
    encoding = conn.execute("SELECT current_setting('server_encoding')").fetchone()
    if encoding != ("UTF8",):
        raise RuntimeError(f"schema guard needs a UTF8 database, not {encoding}")
    types = {
        int(oid): _Type(str(k), int(b), int(m), int(e), str(cat), sub or msub)
        for oid, k, b, m, e, cat, sub, msub in conn.execute(_TYPES).fetchall()
    }
    allowed = ", ".join(f"numeric({p},{s})" for p, s in DECIMAL_STORAGE)
    problems: list[str] = []
    for schema, rel, col, oid, typmod, shown in conn.execute(_COLUMNS).fetchall():
        if f"{schema}.{rel}.{col}" in EXEMPT:
            continue
        where = f"{schema}.{rel}.{col} ({shown})"
        base, base_mod = _resolve(int(oid), int(typmod), types)
        if base in FORBIDDEN:
            problems.append(f"{where}: {FORBIDDEN[base]} is forbidden")
        elif base == NUMERIC:
            if numeric_precision_scale(base_mod) not in DECIMAL_STORAGE:
                problems.append(f"{where}: numeric must be one of {allowed}")
        elif is_money_name(str(col)):
            problems.append(f"{where}: money-named column must be one of {allowed}")
    return problems
