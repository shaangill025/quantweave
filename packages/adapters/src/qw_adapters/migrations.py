"""Forward-only PostgreSQL migration runner (ADR-013; T008 review section 5).

`NNNN_name.sql` files, contiguous from 0001, each run in its own transaction and are
recorded in `public.schema_migrations` (file name, SHA-256 of the bytes, applied
time). A session advisory lock serialises runners on one database.

Refused before anything runs: a name that is not `NNNN_lower_snake.sql` (`.SQL`
too), a symlink, a duplicate or gap, an applied file whose checksum or name changed,
an applied version with no file, a pending file older than an applied one, and a
top-level statement controlling the transaction, role or session authorization
(BEGIN, COMMIT, END, SAVEPOINT, SET ROLE, RESET ALL ...). The scanner skips comments
and quoted or dollar-quoted text and refuses files it cannot scan; `BEGIN ATOMIC`
bodies are refused (use quoted bodies).

Versions after 0001 (which creates the roles) run under `SET LOCAL ROLE qw_migrate`.
Inside the transaction a migration is rolled back if it ended the transaction,
finished under another role or session user, left a user object not owned by
`qw_migrate` (except the ledger), or left a column failing `schema_guard`. RESET ALL
stops settings such as search_path leaking. These checks enforce object OWNERSHIP
only. A file run by a superuser can still change roles or privileges (set_config,
quoted GUC names, DO blocks, GRANT ... TO PUBLIC), so the production migrate login must
not be a superuser (open gate). Recovery is backup plus a new forward
migration. The caller supplies the autocommit connection; no environment is read.
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


_TOKEN = re.compile(
    r"(?P<skip>\s+|--[^\n]*|/\*(?:(?!/\*)[\s\S])*?\*/)"
    r"|(?P<quoted>[Ee]'(?:[^'\\]|\\[\s\S]|'')*'|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\""
    r"|(?P<tag>\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)[\s\S]*?(?P=tag))"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_$]*)|(?P<other>[\s\S])"
)
_OPENER = re.compile(r"['\"]|/\*|\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_FORBIDDEN = re.compile(
    r"(abort|begin|commit|end|release|rollback|savepoint|discard|reset all"
    r"|(start|prepare) transaction"
    r"|(re)?set( local| session)? (role|session authorization))( |$)"
)


def statement_heads(text: str) -> list[list[str]]:
    """First four tokens (lowercased words, a quote mark for quoted text, or single
    characters) of each top-level statement. Raises MigrationError for an
    unterminated quote, dollar quote or comment, or a nested comment."""
    heads: list[list[str]] = [[]]
    for m in _TOKEN.finditer(text):
        if m.group("skip") is not None:
            continue
        word, quoted = m.group("word"), m.group("quoted")
        failed_e_string = word in {"e", "E"} and text.startswith("'", m.end())
        if failed_e_string or (m.group("other") and _OPENER.match(text, m.start())):
            raise MigrationError(f"unterminated quote or comment at {m.start()}")
        token = "'" if quoted is not None else m.group().lower()
        if token == ";":
            heads.append([])
        elif len(heads[-1]) < 4:
            heads[-1].append(token)
    return [h for h in heads if h]


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Read and validate the migration files. Non-`.sql` files are ignored."""
    found: dict[int, Migration] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() != ".sql":
            continue
        match = FILE_PATTERN.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            raise MigrationError(
                f"{path.name}: not a regular NNNN_lower_snake.sql file"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"{path.name}: duplicate version {version} ({found[version].name})"
            )
        body = path.read_bytes()
        try:
            heads = statement_heads(body.decode("utf-8"))
        except (UnicodeDecodeError, MigrationError) as exc:
            raise MigrationError(f"{path.name}: cannot scan: {exc}") from exc
        for head in (" ".join(h) for h in heads):
            if _FORBIDDEN.match(head):
                raise MigrationError(f"{path.name}: forbidden statement {head.upper()}")
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
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    done: list[int] = []
    try:
        conn.execute(_CREATE_TABLE)
        for migration in pending_migrations(migrations, applied_migrations(conn)):
            _apply(conn, migration, run_as)
            done.append(migration.version)
    except BaseException as exc:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        except psycopg.Error as unlock_exc:  # keep the original error
            exc.add_note(f"advisory unlock also failed: {unlock_exc}")
        raise
    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    return done


# Catalogues of owned objects -> column prefix (relowner, relnamespace, relname ...).
_NAMESPACED = {
    "pg_class": "rel", "pg_proc": "pro", "pg_type": "typ", "pg_operator": "opr",
    "pg_collation": "coll", "pg_conversion": "con", "pg_opclass": "opc",
    "pg_opfamily": "opf", "pg_ts_config": "cfg", "pg_ts_dict": "dict",
    "pg_statistic_ext": "stx",
}  # fmt: skip
_GLOBAL = {
    "pg_namespace": "nsp", "pg_event_trigger": "evt", "pg_extension": "ext",
    "pg_foreign_data_wrapper": "fdw", "pg_foreign_server": "srv",
    "pg_publication": "pub", "pg_language": "lan",
}  # fmt: skip
_OWNERS_SQL = " UNION ALL ".join(
    [
        f"SELECT '{cat}', n.nspname || '.' || o.{p}name, o.{p}owner::regrole::text "
        f"FROM pg_catalog.{cat} o JOIN pg_catalog.pg_namespace n "
        f"ON n.oid = o.{p}namespace WHERE o.oid >= 16384 "
        f"AND o.{p}owner <> %(role)s::regrole AND n.nspname !~ '^pg_' "
        "AND n.nspname <> 'information_schema'"
        for cat, p in _NAMESPACED.items()
    ]
    + [
        f"SELECT '{cat}', o.{p}name::text, o.{p}owner::regrole::text "
        f"FROM pg_catalog.{cat} o WHERE o.oid >= 16384 "
        f"AND o.{p}owner <> %(role)s::regrole AND o.{p}name::text !~ '^pg_'"
        for cat, p in _GLOBAL.items()
    ]
)
_LEDGER = {
    ("pg_class", "public.schema_migrations"),
    ("pg_class", "public.schema_migrations_pkey"),
    ("pg_class", "public.schema_migrations_name_key"),
    ("pg_type", "public.schema_migrations"),
    ("pg_type", "public._schema_migrations"),
}


def foreign_owned_objects(conn: psycopg.Connection[TupleRow], role: str) -> list[str]:
    """User objects (outside pg_* and information_schema) not owned by `role`,
    except the schema_migrations ledger."""
    rows = conn.execute(_OWNERS_SQL, {"role": role}).fetchall()
    return [
        f"{cat} {name} owned by {owner}"
        for cat, name, owner in rows
        if (cat, name) not in _LEDGER
    ]


def _apply(
    conn: psycopg.Connection[TupleRow], migration: Migration, run_as: str | None
) -> None:
    role = run_as if migration.version > BOOTSTRAP_VERSION else None
    try:
        with conn.transaction():
            xact = _scalar(conn, "SELECT pg_current_xact_id()::text")
            session = _scalar(conn, "SELECT session_user::text")
            if role is not None:
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            conn.execute(migration.body)
            if _scalar(conn, "SELECT pg_current_xact_id()::text") != xact:
                raise MigrationError("it ended its own transaction")
            if _scalar(conn, "SELECT session_user::text") != session:
                raise MigrationError("it changed the session authorization")
            current = _scalar(conn, "SELECT current_user::text")
            if role is not None and current != role:
                raise MigrationError("it changed the current role")
            conn.execute("RESET ROLE; RESET ALL")  # RESET ALL skips the role
            owned = foreign_owned_objects(conn, run_as) if run_as else []
            problems = [f"ownership: {p}" for p in owned]
            problems += [
                f"column type policy: {p}" for p in column_type_violations(conn)
            ]
            if problems:
                raise MigrationError("; ".join(problems))
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
