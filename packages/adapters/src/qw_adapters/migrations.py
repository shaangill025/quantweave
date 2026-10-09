"""Forward-only PostgreSQL migration runner (ADR-013; T008 review section 5).

Migrations are `NNNN_name.sql` files numbered contiguously from 0001. Each one runs
in its own transaction and is recorded in `public.schema_migrations` with its file
name, SHA-256 of the file bytes and applied time. Rules:

- An applied file whose checksum or name changed, an applied version with no file, a
  gap in the file numbers, or a pending file older than an applied one stops the run
  before anything is applied. Recovery is backup plus a new forward migration.
- A session-level advisory lock serialises concurrent runners on one database.
- Versions after the bootstrap (0001, which creates the roles) run under
  `SET LOCAL ROLE qw_migrate`, so the objects they create are owned by the
  non-superuser schema owner. A migration that changes the role, ends the
  transaction, or leaves a column that fails `schema_guard` is rolled back and not
  recorded.
- Migration SQL must not contain transaction control (BEGIN/COMMIT/ROLLBACK).

The caller (a composition root) supplies the autocommit connection; this module
reads no environment and opens no connections.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from qw_adapters.schema_guard import column_type_violations

MIGRATIONS_DIR = Path(__file__).resolve().parent.joinpath("sql")
FILE_PATTERN = re.compile(r"([0-9]{4})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql")
LOCK_KEY = 0x71776D6967726174  # b"qwmigrat"; advisory locks are per database
MIGRATE_ROLE = "qw_migrate"
BOOTSTRAP_VERSION = 1

_CREATE_TABLE = b"""
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    name text NOT NULL UNIQUE,
    checksum text NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """The migration set or the database history is inconsistent, or a step failed."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    body: bytes

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Read and validate the migration files. Non-`.sql` files are ignored."""
    found: dict[int, Migration] = {}
    for path in sorted(p for p in directory.iterdir() if p.suffix == ".sql"):
        match = FILE_PATTERN.fullmatch(path.name)
        if match is None or not path.is_file():
            raise MigrationError(f"{path.name}: not a NNNN_lower_snake.sql file")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"{path.name}: duplicate version {version} ({found[version].name})"
            )
        body = path.read_bytes()
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"{path.name}: not UTF-8") from exc
        found[version] = Migration(version, path.name, body)
    expected = list(range(1, len(found) + 1))
    if sorted(found) != expected:
        missing = sorted(set(expected) - set(found))
        raise MigrationError(
            f"migration numbers must be contiguous from 0001; "
            f"have {sorted(found)}, missing {missing}"
        )
    return tuple(found[v] for v in expected)


def applied_migrations(
    conn: psycopg.Connection[TupleRow],
) -> dict[int, tuple[str, str]]:
    """version -> (name, checksum) from schema_migrations."""
    rows = conn.execute(
        "SELECT version, name, checksum FROM public.schema_migrations ORDER BY version"
    ).fetchall()
    return {int(v): (str(n), str(c)) for v, n, c in rows}


def pending_migrations(
    migrations: tuple[Migration, ...], applied: dict[int, tuple[str, str]]
) -> list[Migration]:
    """Check the recorded history against the files; return what is left to apply."""
    by_version = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(applied.items()):
        current = by_version.get(version)
        if current is None:
            raise MigrationError(f"applied migration {version} ({name}) has no file")
        if current.name != name:
            raise MigrationError(
                f"migration {version} applied as {name} but the file is {current.name}"
            )
        if current.checksum != checksum:
            raise MigrationError(
                f"{name}: checksum changed since it was applied "
                f"({checksum} -> {current.checksum}); add a new migration instead"
            )
    pending = [m for m in migrations if m.version not in applied]
    if pending and applied and pending[0].version < max(applied):
        raise MigrationError(
            f"{pending[0].name} is pending but newer migration {max(applied)} is "
            "already applied (out of order)"
        )
    return pending


def migrate(
    conn: psycopg.Connection[TupleRow],
    directory: Path = MIGRATIONS_DIR,
    *,
    run_as: str | None = MIGRATE_ROLE,
) -> list[int]:
    """Apply pending migrations in order and return the versions applied."""
    if not conn.autocommit:
        raise MigrationError("migrate() needs an autocommit connection")
    encoding = _scalar(conn, "SELECT current_setting('server_encoding')")
    if encoding != "UTF8":
        raise MigrationError(f"database encoding must be UTF8, not {encoding!r}")
    migrations = load_migrations(directory)
    conn.execute("SELECT %s::bigint", (LOCK_KEY,))
    try:
        conn.execute(_CREATE_TABLE)
        pending = pending_migrations(migrations, applied_migrations(conn))
        done: list[int] = []
        for migration in pending:
            _apply(conn, migration, run_as)
            done.append(migration.version)
        return done
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _apply(
    conn: psycopg.Connection[TupleRow], migration: Migration, run_as: str | None
) -> None:
    role = run_as if migration.version > BOOTSTRAP_VERSION else None
    try:
        with conn.transaction():
            xact = _scalar(conn, "SELECT pg_current_xact_id()::text")
            if role is not None:
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            conn.execute(migration.body)
            if _scalar(conn, "SELECT pg_current_xact_id()::text") != xact:
                raise MigrationError("it ended its own transaction")
            if role is not None:
                if _scalar(conn, "SELECT current_user::text") != role:
                    raise MigrationError("it changed the current role")
                conn.execute("RESET ROLE")
            problems = column_type_violations(conn)
            if problems:
                raise MigrationError("column type policy: " + "; ".join(problems))
            conn.execute(
                "INSERT INTO public.schema_migrations (version, name, checksum) "
                "VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
    except (MigrationError, psycopg.Error) as exc:
        raise MigrationError(f"{migration.name} rolled back: {exc}") from exc


def _scalar(conn: psycopg.Connection[TupleRow], query: LiteralString) -> object:
    row = conn.execute(query).fetchone()
    return None if row is None else row[0]
