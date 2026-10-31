"""`qw-db-init`: the one-time superuser job of a fresh self-hosted install (T060;
owner decision 108). It applies 0001 only, through `migrations.migrate(...,
through=1)`, which also installs (or, on a database from the older runner,
repairs) the ledger guard; later migrations are left to `qw-migrate`. Then it
creates or updates one non-superuser LOGIN per runtime group.

QW_DBINIT_DATABASE_URL is a passfile-only libpq string for a PostgreSQL superuser
(`file_secrets.passfile_conninfo`). Each login comes from a passfile named by
QW_DBINIT_PASSFILE_<GROUP> (QW_MIGRATE is required; QW_APP, QW_WORKER,
QW_ASSESSOR, QW_RELEASE and QW_INGEST are optional): one line
`*:*:*:<login>:<password>`, with the `*_FILE` file checks. These are copies of the
passfiles the services mount, owned by this job's user. Only the SCRAM-SHA-256
verifier, computed here by libpq, is sent to the server, never the password. A
login is created NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS and
a member of its group alone; an existing login of that group gets the new
verifier (rotation); an existing role that is not exactly that is refused.

Exit 0 on success, 2 for configuration, 1 for a refusal or a database failure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from typing import LiteralString

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from qw_adapters import migrations
from qw_adapters.file_secrets import (
    LIBPQ_INLINE,
    ConfigError,
    check_secret_file,
    passfile_conninfo,
    refuse_inline,
)

URL_VARIABLE = "QW_DBINIT_DATABASE_URL"
PASSFILE_PREFIX = "QW_DBINIT_PASSFILE_"
GROUPS = ("qw_migrate", "qw_app", "qw_worker", "qw_assessor", "qw_release",
          "qw_ingest")  # fmt: skip
REQUIRED = ("qw_migrate",)
MIN_PASSWORD = 16
_ATTRIBUTES: LiteralString = (
    "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
)
_LOGIN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
Conn = psycopg.Connection[TupleRow]


class DbInitRefused(Exception):
    """The connection or an existing role is not what db-init may act on."""


def _fields(line: str) -> list[str]:
    """Split a passfile line on unescaped colons (libpq: `\\:` and `\\\\`)."""
    out: list[str] = []
    cur, escaped = "", False
    for ch in line:
        if escaped:
            cur, escaped = cur + ch, False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            out, cur = [*out, cur], ""
        else:
            cur += ch
    return [*out, cur]


def read_login(environ: Mapping[str, str], group: str) -> tuple[str, str] | None:
    """(login, password) from the group's passfile; None when it is unset."""
    variable = PASSFILE_PREFIX + group.upper()
    path = environ.get(variable)
    if path is None:
        if group in REQUIRED:
            raise ConfigError(variable, "not set")
        return None
    try:
        text = check_secret_file(variable, path).decode("ascii")
    except UnicodeDecodeError:
        raise ConfigError(variable, "the passfile must be ASCII") from None
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    if len(lines) != 1:
        raise ConfigError(variable, "needs exactly one *:*:*:<login>:<password> line")
    fields = _fields(lines[0])
    if len(fields) != 5:
        raise ConfigError(variable, "needs exactly one *:*:*:<login>:<password> line")
    login, password = fields[3], fields[4]
    if not _LOGIN.fullmatch(login) or login in GROUPS or login.startswith("pg_"):
        raise ConfigError(variable, "the login name is not allowed")
    if len(password) < MIN_PASSWORD or not all(" " <= c <= "~" for c in password):
        raise ConfigError(
            variable, f"the password must be {MIN_PASSWORD}+ printable ASCII"
        )
    return login, password


def _ensure_login(conn: Conn, group: str, login: str, password: str) -> str:
    verifier = conn.pgconn.encrypt_password(
        password.encode(), login.encode(), b"scram-sha-256"
    ).decode("ascii")
    row = conn.execute(
        "SELECT r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb "
        "OR r.rolreplication, ARRAY(SELECT g.rolname FROM pg_catalog.pg_auth_members"
        " m JOIN pg_catalog.pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid) "
        "FROM pg_catalog.pg_roles r WHERE r.rolname = %s",
        (login,),
    ).fetchone()
    ident, attrs = sql.Identifier(login), sql.SQL(_ATTRIBUTES)
    if row is None:
        conn.execute(
            sql.SQL("CREATE ROLE {} {} PASSWORD {} IN ROLE {}").format(
                ident, attrs, sql.Literal(verifier), sql.Identifier(group)
            )
        )
        return "created"
    if row[0] or set(row[1]) != {group}:
        raise DbInitRefused(f"role {login} exists and is not exactly a {group} login")
    conn.execute(
        sql.SQL("ALTER ROLE {} {} PASSWORD {}").format(
            ident, attrs, sql.Literal(verifier)
        )
    )
    return "updated"


def run(conn: Conn, logins: Mapping[str, tuple[str, str]]) -> list[str]:
    """Apply 0001 (and the ledger guard), then the logins; returns report lines."""
    su = conn.execute(
        "SELECT rolsuper, current_user = session_user FROM pg_catalog.pg_roles "
        "WHERE rolname = session_user"
    ).fetchone()
    if su is None or not su[0] or not su[1]:
        raise DbInitRefused("db-init needs a PostgreSQL superuser login")
    applied = migrations.migrate(conn, through=migrations.BOOTSTRAP_VERSION)
    report = ["0001 applied" if applied else "0001 already applied"]
    with conn.transaction():
        for group, (login, password) in logins.items():
            report.append(f"{_ensure_login(conn, group, login, password)} {login} "
                          f"({group})")  # fmt: skip
    return report


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    argparse.ArgumentParser(prog="qw-db-init", description=__doc__).parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        refuse_inline(env, LIBPQ_INLINE)
        conninfo = passfile_conninfo(env, URL_VARIABLE)
        logins = {g: lg for g in GROUPS if (lg := read_login(env, g)) is not None}
        if len({lg[0] for lg in logins.values()}) != len(logins):
            raise ConfigError(PASSFILE_PREFIX + "*", "each group needs its own login")
    except ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            report = run(conn, logins)
    except DbInitRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except migrations.MigrationError as exc:  # names a migration, no secret
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:  # no server text: it could echo a statement
        why = exc.sqlstate or type(exc).__name__
        print(f"failed: database error ({why})", file=sys.stderr)
        return 1
    print("db-init: " + "; ".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
