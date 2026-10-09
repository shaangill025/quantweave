"""PostgreSQL 16 harness for `@pytest.mark.db` tests: `QW_TEST_DATABASE_URL` (a
superuser URL; CI's postgres:16 service) or else a throwaway cluster from `QW_PG_BIN`
(default /usr/lib/postgresql/16/bin) in a 0700 temporary directory, reachable only
through its unix socket, run as the `postgres` OS user when root. Without either the
tests skip with the reason, or fail when `QW_REQUIRE_DB=1`. One database per test.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow

PG_MAJOR = 16
PG_BIN = Path(os.environ.get("QW_PG_BIN", f"/usr/lib/postgresql/{PG_MAJOR}/bin"))


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("QW_REQUIRE_DB") == "1":
        pytest.fail(f"QW_REQUIRE_DB=1 but no PostgreSQL: {reason}", pytrace=False)
    pytest.skip(f"no PostgreSQL {PG_MAJOR} available: {reason}")


@pytest.fixture(scope="session")
def pg_admin_url() -> Iterator[str]:
    """Superuser conninfo for a PostgreSQL 16 server."""
    url = os.environ.get("QW_TEST_DATABASE_URL")
    if url:
        yield from _checked(url)
        return
    if not (PG_BIN / "initdb").is_file():
        _unavailable(f"QW_TEST_DATABASE_URL unset and no initdb in {PG_BIN}")
    prefix: list[str] = []
    if os.geteuid() == 0:
        runuser = shutil.which("runuser")
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            runuser = None
        if runuser is None:
            _unavailable("running as root needs runuser and a 'postgres' OS user")
        prefix = [runuser, "-u", "postgres", "--"]

    def run(*args: str, check: bool = True) -> None:
        try:
            subprocess.run([*prefix, *args], check=check, capture_output=True)
        except subprocess.CalledProcessError as exc:
            pytest.fail(f"throwaway cluster: {args[0]} failed: {exc.stderr!r}")

    # 0700 directory: only its owner (and root) can reach the unix socket inside.
    base = Path(tempfile.mkdtemp(prefix="qw-pg-"))
    try:
        if prefix:
            shutil.chown(base, "postgres")
        data, pg_ctl = str(base / "data"), str(PG_BIN / "pg_ctl")
        run(str(PG_BIN / "initdb"), "-D", data, "-U", "qw_test_admin",
            "--auth-local=trust", "--auth-host=reject", "--encoding=UTF8",
            "--no-locale", "--no-sync")  # fmt: skip
        try:
            options = f"-c listen_addresses='' -k {base} -p 5432 -c fsync=off"
            run(pg_ctl, "-D", data, "-l", str(base / "server.log"), "-o", options,
                "-w", "start")  # fmt: skip
            url = f"host={base} port=5432 user=qw_test_admin dbname=postgres"
            yield from _checked(url, socket_only=True)
        finally:
            run(pg_ctl, "-D", data, "-m", "fast", "-w", "stop", check=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _checked(url: str, *, socket_only: bool = False) -> Iterator[str]:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT current_setting('server_version_num')::int, rolsuper, "
            "current_setting('listen_addresses'), inet_server_addr() "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row is not None
    version, superuser, listen, addr = row
    if version // 10000 != PG_MAJOR or not superuser:
        pytest.fail(f"need a PostgreSQL {PG_MAJOR} superuser URL, got {version}")
    if socket_only and (listen, addr) != ("", None):
        pytest.fail(f"throwaway cluster must not listen on TCP: {listen!r} {addr}")
    yield url


@pytest.fixture
def database_url(pg_admin_url: str) -> Iterator[str]:
    """A fresh, empty database for one test; dropped (with FORCE) afterwards."""
    name = f"qw_test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'"
            ).format(sql.Identifier(name))
        )
    try:
        yield make_conninfo(pg_admin_url, dbname=name)
    finally:
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection[TupleRow]]:
    with psycopg.connect(database_url, autocommit=True) as connection:
        yield connection
