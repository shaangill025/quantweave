"""`qw-migrate`: the `migrate-once` job of owner decision 108. It applies pending
migrations as the non-superuser qw_migrate login named by QW_MIGRATE_DATABASE_URL, a
libpq connection string whose password is in a passfile (`file_secrets`). It never
runs 0001: a database without it (and the ledger grant the runner makes there) has
not been through the superuser `db-init` job and is refused, as is a superuser, a
login that is not a member of qw_migrate, or one connected under another role.
`--check-config` checks QW_MIGRATE_DATABASE_URL and its passfile as the user it
runs as, without connecting: exit 0 and `config ok: qw-migrate`, or exit 2.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

import psycopg
from psycopg.rows import TupleRow

from qw_adapters import file_secrets, migrations

URL_VARIABLE = "QW_MIGRATE_DATABASE_URL"


class MigrateRefused(Exception):
    """The database or the login is not one qw-migrate may run against."""


class NotInitialised(MigrateRefused):
    def __init__(self, why: str = "") -> None:
        super().__init__(why or (
            "the database has not been initialised: run the db-init job (owner "
            "decision 108) as the PostgreSQL superuser first; qw-migrate never "
            "applies 0001"))  # fmt: skip


class LedgerUnguarded(NotInitialised):
    """0001 was applied by a runner older than the ledger guard (T060)."""

    def __init__(self) -> None:
        super().__init__(
            "the ledger predates the qw_migrate grant and contiguity trigger: run "
            "qw_adapters.migrations.migrate() once as the PostgreSQL superuser (the "
            "db-init job, owner decision 108), which installs them, then rerun "
            "qw-migrate")  # fmt: skip


def preflight(conn: psycopg.Connection[TupleRow]) -> None:
    """Refuse a superuser or a member of one, a login acting as another role, a
    database without 0001 or its ledger guard, and a login that is not a member of
    qw_migrate."""
    row = conn.execute(
        "SELECT r.rolsuper, current_user = session_user, EXISTS (SELECT 1 FROM "
        "pg_catalog.pg_roles s WHERE s.rolsuper AND s.oid <> r.oid AND "
        "pg_has_role(r.oid, s.oid, 'MEMBER')) FROM pg_catalog.pg_roles r "
        "WHERE r.rolname = session_user"
    ).fetchone()  # MEMBER covers USAGE (inherited) and SET ROLE membership
    if row is None or row[0]:
        raise MigrateRefused("the login is a superuser; use the qw_migrate login")
    if row[2]:
        raise MigrateRefused("the login is a member of a superuser role")
    if not row[1]:
        raise MigrateRefused("the connection runs under another role")
    ready = conn.execute(
        "SELECT to_regclass('public.schema_migrations') IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
        (migrations.MIGRATE_ROLE,),
    ).fetchone()
    if ready is None or not ready[0]:
        raise NotInitialised()
    member = conn.execute(
        "SELECT pg_has_role(session_user, %s, 'MEMBER')", (migrations.MIGRATE_ROLE,)
    ).fetchone()
    if member is None or not member[0]:
        raise MigrateRefused("the login is not a member of qw_migrate")
    if not migrations.ledger_guarded(conn):
        raise LedgerUnguarded()
    bootstrap = conn.execute(
        "SELECT 1 FROM public.schema_migrations WHERE version = %s",
        (migrations.BOOTSTRAP_VERSION,),
    ).fetchone()
    if bootstrap is None:
        raise NotInitialised()


def run(conninfo: str) -> list[int]:
    with psycopg.connect(conninfo, autocommit=True) as conn:
        preflight(conn)
        return migrations.migrate(conn)


def main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="qw-migrate", description=__doc__)
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        conninfo = file_secrets.passfile_conninfo(env, URL_VARIABLE)
    except file_secrets.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print("config ok: qw-migrate")
        return 0
    try:
        done = run(conninfo)
    except MigrateRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except (migrations.MigrationError, psycopg.Error) as exc:
        print(f"failed: {exc}", file=sys.stderr)  # the conninfo holds no secret
        return 1
    span = f" (versions {done[0]}-{done[-1]})" if done else ""
    print(f"migrated: {len(done)} applied{span}")
    return 0
