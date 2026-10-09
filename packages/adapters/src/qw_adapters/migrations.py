"""Forward-only PostgreSQL migration runner (ADR-013; T008 review section 5).

`NNNN_name.sql` files, contiguous from 0001, each run in its own transaction and are
recorded in `public.schema_migrations` (file name, SHA-256 of the bytes, applied
time). A session advisory lock serialises runners on one database.

Refused before anything runs: a name that is not `NNNN_lower_snake.sql` (`.SQL`
too), a symlink, a duplicate or gap, an applied file whose checksum or name changed,
an applied version with no file, a pending file older than an applied one, and a
top-level statement controlling the transaction, role or session authorization
(BEGIN, COMMIT, END, SAVEPOINT, SET [LOCAL] ROLE or "role", RESET ALL ...), a
`set_config('role' | 'session_authorization', ...)` call, and `GRANT ... TO PUBLIC`.
The scanner skips comments and quoted or dollar-quoted text and refuses files it
cannot scan; `BEGIN ATOMIC` bodies are refused (use quoted bodies).

Versions after 0001 (which creates the roles) run under `SET LOCAL ROLE qw_migrate`.
Inside the transaction a migration is rolled back if it ended the transaction,
finished under another role or session user, left a user object not owned by
`qw_migrate` (except the ledger), left a column failing `schema_guard`, or changed
privileges: a role or membership created, altered or dropped (outside 0001), a
changed database, public/app schema or ledger ACL (outside 0001), any new grant to
PUBLIC, a default ACL not defined by `qw_migrate` or removed, any large object
created or changed (metadata or content), any change to schema_migrations rows
(the runner inserts its own row after the checks), or any change to
pg_db_role_setting (ALTER DATABASE/ROLE ... SET). These snapshots also catch dynamic
SQL (DO blocks, built set_config names) that the scanner cannot see. RESET ALL stops
settings such as search_path leaking. Not covered: effects outside the compared
catalogues, such as NOTIFY, pg_sleep, advisory locks, COPY ... TO PROGRAM or server
files, password changes (pg_roles hides passwords), comments and security labels,
and data in existing tables, so the production migrate login must not be a
superuser. Recovery is backup
plus a new forward migration. The caller supplies the autocommit connection; no
environment is read.
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
    r"|(re)?set( local| session)? "
    r"(role|\"role\"|session authorization|\"?session_authorization\"?))( |$)"
)
_SET_CONFIG_GUCS = {("(", "'role'"), ("(", "'session_authorization'")}
_PUBLIC = {"public", '"public"'}


def _forbidden(tokens: list[str]) -> str | None:
    """Why a statement (its lowercased tokens) is refused, or None."""
    head = " ".join(tokens[:4])
    if _FORBIDDEN.match(head):
        return head.upper()
    for i, token in enumerate(tokens):
        if token == "set_config" and tuple(tokens[i + 1 : i + 3]) in _SET_CONFIG_GUCS:
            return f"SET_CONFIG({tokens[i + 2]})"
        public = tokens[i + 1 : i + 2] and tokens[i + 1] in _PUBLIC
        if token == "to" and public and "grant" in tokens[:i]:
            return "GRANT ... TO PUBLIC"
    return None


def statement_tokens(text: str) -> list[list[str]]:
    """Lowercased tokens of each top-level statement: words, single characters,
    string literals as `'text'` (an E prefix dropped), quoted identifiers as
    `"name"`, and `$` for a dollar-quoted body. Raises MigrationError for an
    unterminated quote, dollar quote or comment, or a nested comment."""
    heads: list[list[str]] = [[]]
    for m in _TOKEN.finditer(text):
        if m.group("skip") is not None:
            continue
        word = m.group("word")
        failed_e_string = word in {"e", "E"} and text.startswith("'", m.end())
        if failed_e_string or (m.group("other") and _OPENER.match(text, m.start())):
            raise MigrationError(f"unterminated quote or comment at {m.start()}")
        token = m.group().lower()
        if m.group("tag") is not None:
            token = "$"
        elif token.startswith("e'"):
            token = token[1:]
        if token == ";":
            heads.append([])
        else:
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
            statements = statement_tokens(body.decode("utf-8"))
        except (UnicodeDecodeError, MigrationError) as exc:
            raise MigrationError(f"{path.name}: cannot scan: {exc}") from exc
        for why in filter(None, map(_forbidden, statements)):
            raise MigrationError(f"{path.name}: forbidden statement {why}")
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


# Role, membership and privilege state, as (kind, item) rows. Taken as the session
# user before and after each migration, inside its transaction.
_USER_NS = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
_PRIVILEGES_SQL = f"""
SELECT 'role', r::text FROM (SELECT rolname, rolsuper, rolinherit, rolcreaterole,
    rolcreatedb, rolcanlogin, rolreplication, rolbypassrls, rolconnlimit,
    rolvaliduntil, rolconfig FROM pg_catalog.pg_roles) r
UNION ALL SELECT 'member', m::text FROM (SELECT roleid::regrole, member::regrole,
    grantor::regrole, admin_option, inherit_option, set_option
    FROM pg_catalog.pg_auth_members) m
UNION ALL SELECT 'acl', 'database ' || coalesce(datacl::text, 'default')
    FROM pg_catalog.pg_database WHERE datname = current_database()
UNION ALL SELECT 'acl', 'schema ' || nspname || ' ' || coalesce(nspacl::text, '-')
    FROM pg_catalog.pg_namespace WHERE nspname IN ('public', 'app')
UNION ALL SELECT 'acl', 'ledger ' || coalesce(relacl::text, 'default')
    FROM pg_catalog.pg_class WHERE oid = to_regclass('public.schema_migrations')
UNION ALL SELECT 'defacl', defaclrole::regrole::text FROM pg_catalog.pg_default_acl
UNION ALL SELECT 'defacl_key', d::text FROM (SELECT defaclrole::regrole,
    defaclnamespace, defaclobjtype FROM pg_catalog.pg_default_acl) d
UNION ALL SELECT 'lo', m.oid || ' ' || m.lomowner::regrole || ' '
    || coalesce(m.lomacl::text, '-') || ' ' || md5(lo_get(m.oid))
    FROM pg_catalog.pg_largeobject_metadata m
UNION ALL SELECT 'ledger', l::text FROM public.schema_migrations l
UNION ALL SELECT 'db_setting', s::text FROM pg_catalog.pg_db_role_setting s
UNION ALL SELECT 'public', o.what || ' ' || a.privilege_type FROM (
    SELECT 'database', coalesce(datacl, acldefault('d', datdba))
        FROM pg_catalog.pg_database WHERE datname = current_database()
    UNION ALL SELECT 'schema ' || nspname, coalesce(nspacl, acldefault('n', nspowner))
        FROM pg_catalog.pg_namespace n WHERE {_USER_NS}
    UNION ALL SELECT 'relation ' || c.oid::regclass, c.relacl
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n
        ON n.oid = c.relnamespace WHERE {_USER_NS}
    UNION ALL SELECT 'column ' || attrelid::regclass || '.' || attname, attacl
        FROM pg_catalog.pg_attribute WHERE attacl IS NOT NULL
    UNION ALL SELECT 'function ' || p.oid::regprocedure,
        coalesce(p.proacl, acldefault('f', p.proowner))
        FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n
        ON n.oid = p.pronamespace WHERE {_USER_NS}
    UNION ALL SELECT 'type ' || t.oid::regtype,
        coalesce(t.typacl, acldefault('T', t.typowner))
        FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n
        ON n.oid = t.typnamespace LEFT JOIN pg_catalog.pg_class c ON c.oid = t.typrelid
        WHERE {_USER_NS} AND t.typcategory <> 'A' AND coalesce(c.relkind, 'c') = 'c'
    UNION ALL SELECT 'default acl ' || defaclrole::regrole || defaclobjtype::text,
        defaclacl FROM pg_catalog.pg_default_acl
    UNION ALL SELECT 'large object ' || oid, lomacl
        FROM pg_catalog.pg_largeobject_metadata
    UNION ALL SELECT 'language ' || lanname, coalesce(lanacl, acldefault('l', lanowner))
        FROM pg_catalog.pg_language
    UNION ALL SELECT 'foreign server ' || srvname, srvacl
        FROM pg_catalog.pg_foreign_server
    UNION ALL SELECT 'fdw ' || fdwname, fdwacl FROM pg_catalog.pg_foreign_data_wrapper
) o(what, acl), aclexplode(o.acl) a WHERE a.grantee = 0
"""


def privilege_state(conn: psycopg.Connection[TupleRow]) -> dict[str, set[str]]:
    state: dict[str, set[str]] = {k: set() for k in _STATE_KINDS}
    for kind, item in conn.execute(_PRIVILEGES_SQL).fetchall():
        state[str(kind)].add(str(item))
    return state


_STATE_KINDS = (
    "role", "member", "acl", "defacl", "defacl_key", "lo", "ledger", "db_setting",
    "public",
)  # fmt: skip


def privilege_violations(
    before: dict[str, set[str]], after: dict[str, set[str]], role: str, bootstrap: bool
) -> list[str]:
    """Refused changes: roles, memberships and the database, public/app schema and
    ledger ACLs (except in 0001); schema_migrations rows, per-database/role settings
    and large objects (always; the runner's own insert comes after this check); any
    new PUBLIC grant; a default ACL not defined by `role`, or removed."""
    kinds = ("lo", "ledger", "db_setting") + (
        () if bootstrap else ("role", "member", "acl")
    )
    problems = [
        f"{kind} changed: {item}"
        for kind in kinds
        for item in sorted(before[kind] ^ after[kind])
    ]
    problems += [
        f"grant to PUBLIC: {g}" for g in sorted(after["public"] - before["public"])
    ]
    problems += [
        f"default ACL defined by {r}" for r in sorted(after["defacl"] - {role})
    ]
    removed = before["defacl_key"] - after["defacl_key"]
    problems += [f"default ACL removed: {k}" for k in sorted(removed)]
    return problems


def _apply(
    conn: psycopg.Connection[TupleRow], migration: Migration, run_as: str | None
) -> None:
    role = run_as if migration.version > BOOTSTRAP_VERSION else None
    try:
        with conn.transaction():
            xact = _scalar(conn, "SELECT pg_current_xact_id()::text")
            session = _scalar(conn, "SELECT session_user::text")
            before = privilege_state(conn)
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
            problems += privilege_violations(
                before,
                privilege_state(conn),
                run_as or MIGRATE_ROLE,
                migration.version == BOOTSTRAP_VERSION,
            )
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
