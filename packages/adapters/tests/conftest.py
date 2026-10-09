"""PostgreSQL 16 test harness for `@pytest.mark.db` tests.

Server, in order of preference:
1. `QW_TEST_DATABASE_URL`: an existing server and a superuser URL (CI sets this for
   its postgres:16 service container).
2. A throwaway cluster from the host binaries in `QW_PG_BIN` (default
   /usr/lib/postgresql/16/bin): initdb into a temporary directory, start on a free
   localhost port, stop and delete at session end. As root, the binaries run as the
   `postgres` OS user because initdb refuses to run as root.
If neither is available the tests are skipped with the reason, or fail when
`QW_REQUIRE_DB=1` (CI). Each test gets its own database, dropped afterwards.
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def pg_admin_url() -> Iterator[str]:
    """Superuser conninfo for a PostgreSQL 16 server."""
    url = os.environ.get("QW_TEST_DATABASE_URL")
    if url:
        yield from _checked(url)
        return
    if not (PG_BIN / "initdb").is_file():
        _unavailable(
            f"QW_TEST_DATABASE_URL unset and no initdb in {PG_BIN} (set QW_PG_BIN)"
        )
    prefix: list[str] = []
    base = Path(tempfile.mkdtemp(prefix="qw-pg-"))
    if os.geteuid() == 0:
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            shutil.rmtree(base)
            _unavailable("running as root and no 'postgres' OS user for initdb")
        shutil.chown(base, "postgres")
        prefix = ["runuser", "-u", "postgres", "--"]
    data, port = base / "data", _free_port()

    def run(*args: str) -> None:
        subprocess.run([*prefix, *args], check=True, capture_output=True, text=True)

    try:
        run(str(PG_BIN / "initdb"), "-D", str(data), "-U", "qw_test_admin",
            "--auth=trust", "--encoding=UTF8", "--no-locale", "--no-sync")  # fmt: skip
        options = f"-p {port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off"
        run(str(PG_BIN / "pg_ctl"), "-D", str(data), "-l", str(base / "server.log"),
            "-o", options, "-w", "start")  # fmt: skip
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(base, ignore_errors=True)
        pytest.fail(f"throwaway cluster failed: {exc.stderr}", pytrace=False)
    try:
        yield from _checked(
            f"host=127.0.0.1 port={port} user=qw_test_admin dbname=postgres"
        )
    finally:
        run(str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop")
        shutil.rmtree(base, ignore_errors=True)


def _checked(url: str) -> Iterator[str]:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT current_setting('server_version_num')::int, rolsuper "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
    assert row is not None
    version, superuser = row
    if version // 10000 != PG_MAJOR or not superuser:
        pytest.fail(f"need a PostgreSQL {PG_MAJOR} superuser URL, got {version}")
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
