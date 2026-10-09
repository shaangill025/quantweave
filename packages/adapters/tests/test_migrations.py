"""Migration runner and 0001 baseline against a real PostgreSQL 16 (CHK-DB-INTEGRATION).

Extra migrations are SYNTHETIC fixtures written to a temporary directory next to a
copy of the real 0001.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters.migrations import (
    LOCK_KEY,
    MIGRATIONS_DIR,
    MigrationError,
    applied_migrations,
    load_migrations,
    migrate,
)
from qw_adapters.schema_guard import column_type_violations

pytestmark = pytest.mark.db
Conn = psycopg.Connection[TupleRow]
ROLES = ("qw_app", "qw_assessor", "qw_ingest", "qw_migrate", "qw_release", "qw_worker")
BASELINE = "0001_roles_and_baseline.sql"


@pytest.fixture
def mdir(tmp_path: Path) -> Path:
    shutil.copy(MIGRATIONS_DIR / BASELINE, tmp_path / BASELINE)
    return tmp_path


def add(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(f"-- SYNTHETIC test migration\n{body}\n")


def versions(conn: Conn) -> list[int]:
    return sorted(applied_migrations(conn))


def table_exists(conn: Conn, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def test_real_migration_set_applies_and_is_idempotent(conn: Conn) -> None:
    files = load_migrations()
    assert files[0].name == BASELINE
    assert migrate(conn) == [m.version for m in files]
    first = conn.execute("SELECT * FROM public.schema_migrations").fetchall()
    assert [(r[0], r[1], r[2]) for r in first] == [
        (m.version, m.name, m.checksum) for m in files
    ]
    assert all(r[3].utcoffset() is not None for r in first)
    assert migrate(conn) == []
    assert conn.execute("SELECT * FROM public.schema_migrations").fetchall() == first


def test_baseline_roles_and_privileges(conn: Conn) -> None:
    migrate(conn)
    rows = conn.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, "
        "rolcreatedb, rolreplication, rolinherit FROM pg_roles "
        "WHERE rolname LIKE 'qw\\_%' AND rolname <> 'qw_test_admin' ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == list(ROLES)  # exactly the T008 section 5 set
    assert all(r[1:] == (False,) * 6 + (True,) for r in rows)
    owner = conn.execute(
        "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'app'"
    ).fetchone()
    assert owner == ("qw_migrate",)
    checks = conn.execute(
        "SELECT has_schema_privilege('qw_app', 'app', 'USAGE'), "
        "has_schema_privilege('qw_app', 'app', 'CREATE'), "
        "has_schema_privilege('qw_app', 'public', 'CREATE'), "
        "has_database_privilege('qw_app', current_database(), 'CONNECT'), "
        "has_database_privilege('qw_app', current_database(), 'TEMPORARY')"
    ).fetchone()
    assert checks == (True, False, False, True, False)
    public = conn.execute(  # the PUBLIC pseudo-role
        "SELECT has_database_privilege('public', current_database(), 'CONNECT'), "
        "has_schema_privilege('public', 'app', 'USAGE'), "
        "has_schema_privilege('public', 'public', 'CREATE')"
    ).fetchone()
    assert public == (False, False, False)


def test_baseline_reapplies_in_second_database_and_resets_role_attributes(
    conn: Conn, pg_admin_url: str
) -> None:
    migrate(conn)
    conn.execute("ALTER ROLE qw_app LOGIN CREATEDB")  # drifted attributes
    conn.execute(
        "CREATE DATABASE qw_test_second TEMPLATE template0 ENCODING 'UTF8' LOCALE 'C'"
    )
    try:
        with psycopg.connect(
            psycopg.conninfo.make_conninfo(pg_admin_url, dbname="qw_test_second"),
            autocommit=True,
        ) as second:
            assert migrate(second) == [1]
        attrs = conn.execute(
            "SELECT rolcanlogin, rolcreatedb FROM pg_roles WHERE rolname = 'qw_app'"
        ).fetchone()
        assert attrs == (False, False)
    finally:
        conn.execute("DROP DATABASE qw_test_second WITH (FORCE)")


def test_applies_in_order_as_qw_migrate(conn: Conn, mdir: Path) -> None:
    add(mdir, "0002_create_t.sql", "CREATE TABLE app.t (id integer PRIMARY KEY);")
    add(
        mdir,
        "0003_use_t.sql",
        "INSERT INTO app.t VALUES (1);\n"
        "CREATE FUNCTION app.f() RETURNS integer LANGUAGE sql AS 'SELECT 1';\n"
        "CREATE TYPE app.k AS ENUM ('a');",
    )
    assert migrate(conn, mdir) == [1, 2, 3]
    owners = conn.execute(
        "SELECT relowner::regrole::text FROM pg_class WHERE oid = 'app.t'::regclass "
        "UNION ALL SELECT proowner::regrole::text FROM pg_proc "
        "WHERE oid = 'app.f'::regproc"
    ).fetchall()
    assert owners == [("qw_migrate",), ("qw_migrate",)]
    usable = conn.execute(
        "SELECT has_function_privilege('qw_app', 'app.f()', 'EXECUTE'), "
        "has_type_privilege('qw_app', 'app.k', 'USAGE')"
    ).fetchone()
    assert usable == (False, False)  # default privileges revoked from PUBLIC


def test_changed_applied_file_is_refused(conn: Conn, mdir: Path) -> None:
    add(mdir, "0002_create_t.sql", "CREATE TABLE app.t (id integer);")
    migrate(conn, mdir)
    add(mdir, "0002_create_t.sql", "CREATE TABLE app.t (id bigint);")
    add(mdir, "0003_next.sql", "CREATE TABLE app.u (id integer);")
    with pytest.raises(MigrationError, match=r"0002_create_t\.sql: checksum changed"):
        migrate(conn, mdir)
    assert versions(conn) == [1, 2]
    assert not table_exists(conn, "app.u")
    (mdir / "0003_next.sql").unlink()
    add(mdir, "0002_create_t.sql", "CREATE TABLE app.t (id integer);")
    (mdir / "0002_create_t.sql").rename(mdir / "0002_renamed.sql")
    with pytest.raises(MigrationError, match=r"applied as 0002_create_t\.sql"):
        migrate(conn, mdir)
    (mdir / "0002_renamed.sql").unlink()
    with pytest.raises(MigrationError, match=r"applied migration 2 .* has no file"):
        migrate(conn, mdir)


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["0003_c.sql"], r"contiguous .* missing \[2\]"),
        (["0002_b.sql", "0002_c.sql"], "duplicate version 2"),
        (["2_b.sql"], "not a regular NNNN_lower_snake.sql"),
        (["0002_Bad-Name.sql"], "not a regular NNNN_lower_snake.sql"),
        (["0002_upper.SQL"], "not a regular NNNN_lower_snake.sql"),
    ],
)
def test_bad_file_sets_are_refused_before_any_change(
    conn: Conn, mdir: Path, names: list[str], message: str
) -> None:
    for name in names:
        add(mdir, name, "CREATE TABLE app.x (id integer);")
    with pytest.raises(MigrationError, match=message):
        migrate(conn, mdir)
    assert not table_exists(conn, "public.schema_migrations")


def test_out_of_order_history_is_refused(conn: Conn, mdir: Path) -> None:
    for n in (2, 3):
        add(mdir, f"000{n}_t{n}.sql", f"CREATE TABLE app.t{n} (id integer);")
    migrate(conn, mdir)
    conn.execute("DELETE FROM public.schema_migrations WHERE version = 2")
    with pytest.raises(MigrationError, match=r"0002_t2\.sql is pending but newer"):
        migrate(conn, mdir)


def test_failing_migration_rolls_back(conn: Conn, mdir: Path) -> None:
    add(mdir, "0002_ok.sql", "CREATE TABLE app.ok (id integer);")
    add(mdir, "0003_fails.sql", "CREATE TABLE app.partial (id integer);\nSELECT 1/0;")
    add(mdir, "0004_never.sql", "CREATE TABLE app.never (id integer);")
    with pytest.raises(MigrationError, match=r"0003_fails.sql rolled back: .*zero"):
        migrate(conn, mdir)
    assert versions(conn) == [1, 2]
    assert table_exists(conn, "app.ok")
    assert not table_exists(conn, "app.partial")
    assert not table_exists(conn, "app.never")


@pytest.mark.parametrize(
    "body",
    [
        "CREATE TABLE app.c (id integer);\nCOMMIT;",
        "RESET ROLE; CREATE TABLE app.t (id int); SET LOCAL ROLE qw_migrate;",
        "SET SESSION AUTHORIZATION DEFAULT; CREATE TABLE app.t (id int);",
        "set local role qw_app;", "Set Session Role qw_app;", "RESET ALL;",
        "begin;", "select 1; End", "SAVEPOINT s;", "ROLLBACK TO s;", "abort;",
        "START TRANSACTION;", "RELEASE SAVEPOINT s;", "/* c */ commit;",
        "SELECT 1; -- note\nPREPARE TRANSACTION 'x';", "DISCARD ALL;",
        "CREATE FUNCTION app.f() RETURNS int LANGUAGE sql BEGIN ATOMIC SELECT 1; END;",
    ],
)  # fmt: skip
def test_forbidden_statements_are_refused_before_execution(
    mdir: Path, body: str
) -> None:
    add(mdir, "0002_forbidden.sql", body)
    with pytest.raises(MigrationError, match="forbidden statement"):
        load_migrations(mdir)


@pytest.mark.parametrize(
    "body",
    [
        "SELECT 'begin; commit';", "SELECT E'it\\'s; commit';",
        "SELECT 1 AS \"x;commit\";", "SELECT 'a''; commit';",
        "DO $$ BEGIN PERFORM 1; END $$;", "DO $b$ BEGIN RAISE NOTICE '$$'; END $b$;",
        "CREATE FUNCTION app.g() RETURNS int LANGUAGE plpgsql\n"
        "AS 'BEGIN RETURN 1; END';",
        "SET search_path = app; SET LOCAL statement_timeout = 0;",
        "-- COMMIT;\n/* ROLLBACK; */ SELECT 1;",
    ],
)  # fmt: skip
def test_scanner_ignores_quoted_and_commented_text(mdir: Path, body: str) -> None:
    add(mdir, "0002_fine.sql", body)
    assert [m.version for m in load_migrations(mdir)] == [1, 2]


@pytest.mark.parametrize(
    "body",
    ["SELECT 'open;", "/* open", "DO $$ BEGIN", 'SELECT "x;', "SELECT E'\\'",
     "/* nested /* */ */ commit;"],
)  # fmt: skip
def test_unscannable_file_is_refused(mdir: Path, body: str) -> None:
    add(mdir, "0002_open.sql", body)
    with pytest.raises(MigrationError, match="cannot scan: unterminated"):
        load_migrations(mdir)


def test_symlinked_migration_is_refused(conn: Conn, mdir: Path) -> None:
    add(mdir.parent, "elsewhere.sql", "CREATE TABLE app.x (id integer);")
    (mdir / "0002_link.sql").symlink_to(mdir.parent / "elsewhere.sql")
    with pytest.raises(MigrationError, match=r"0002_link\.sql: not a regular"):
        migrate(conn, mdir)


@pytest.mark.parametrize(
    "body",
    [
        "DO $$ BEGIN EXECUTE 'RESET ROLE'; EXECUTE 'CREATE TABLE app.t (id int)'; "
        "EXECUTE 'SET LOCAL ROLE qw_migrate'; END $$;",
        "DO $$ BEGIN EXECUTE 'SET SESSION AUTHORIZATION DEFAULT'; "
        "EXECUTE 'CREATE TABLE app.t (id int)'; "
        "EXECUTE 'SET LOCAL ROLE qw_migrate'; END $$;",
        "DO $$ BEGIN EXECUTE 'RESET ROLE'; EXECUTE 'CREATE SCHEMA rogue'; "
        "EXECUTE 'SET LOCAL ROLE qw_migrate'; END $$;",
    ],
)
def test_dynamic_role_escape_is_caught_by_ownership_check(
    conn: Conn, mdir: Path, body: str
) -> None:
    # The scanner cannot see inside dollar quotes; the ownership check must.
    add(mdir, "0002_escape.sql", body)
    with pytest.raises(MigrationError, match=r"ownership: pg_(class app\.t|namespace)"):
        migrate(conn, mdir)
    assert versions(conn) == [1]
    assert not table_exists(conn, "app.t")
    gone = conn.execute("SELECT to_regnamespace('rogue') IS NULL").fetchone()
    assert gone == (True,)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("DO $$ BEGIN EXECUTE 'RESET ROLE'; END $$;", "changed the current role"),
        ("DO $$ BEGIN EXECUTE 'SET SESSION AUTHORIZATION qw_app'; END $$;",
         "changed the session authorization"),
    ],
)  # fmt: skip
def test_dynamic_role_change_is_rolled_back(
    conn: Conn, mdir: Path, body: str, message: str
) -> None:
    add(mdir, "0002_escape.sql", body)
    with pytest.raises(MigrationError, match=message):
        migrate(conn, mdir)
    assert versions(conn) == [1]
    assert conn.execute("SELECT session_user = current_user").fetchone() == (True,)


def test_session_settings_do_not_leak(conn: Conn, mdir: Path) -> None:
    default = conn.execute("SHOW search_path").fetchone()
    add(mdir, "0002_path.sql", "SET search_path = app;\nSET statement_timeout = 7;\n"
        "CREATE TABLE t2 (id integer);")  # fmt: skip
    assert migrate(conn, mdir) == [1, 2]
    assert table_exists(conn, "app.t2")
    assert conn.execute("SHOW search_path").fetchone() == default
    assert conn.execute("SHOW statement_timeout").fetchone() == ("0",)


def test_unlock_failure_does_not_mask_the_migration_error(
    tmp_path: Path, conn: Conn
) -> None:
    add(tmp_path, "0001_kill.sql", "SELECT pg_terminate_backend(pg_backend_pid());")
    with pytest.raises(MigrationError, match=r"0001_kill\.sql rolled back") as info:
        migrate(conn, tmp_path)
    assert any("advisory unlock also failed" in n for n in info.value.__notes__)


def test_requires_autocommit_connection(database_url: str) -> None:
    with (
        psycopg.connect(database_url) as plain,
        pytest.raises(MigrationError, match="autocommit"),
    ):
        migrate(plain)


def test_non_utf8_database_is_refused(conn: Conn, pg_admin_url: str) -> None:
    conn.execute(
        "CREATE DATABASE qw_test_ascii TEMPLATE template0 ENCODING 'SQL_ASCII' "
        "LOCALE 'C'"
    )
    try:
        url = psycopg.conninfo.make_conninfo(pg_admin_url, dbname="qw_test_ascii")
        with (
            psycopg.connect(url, autocommit=True) as ascii_db,
            pytest.raises(MigrationError, match="encoding must be UTF8"),
        ):
            migrate(ascii_db)
        with (
            psycopg.connect(url, autocommit=True) as ascii_db,
            pytest.raises(RuntimeError, match="UTF8"),
        ):
            column_type_violations(ascii_db)
    finally:
        conn.execute("DROP DATABASE qw_test_ascii WITH (FORCE)")


def test_concurrent_runners_serialize_on_advisory_lock(
    conn: Conn, database_url: str, mdir: Path
) -> None:
    # Without the lock both runners would try 0002 and one would fail on CREATE TABLE.
    add(mdir, "0002_slow.sql", "CREATE TABLE app.once (id integer);\n"
        "SELECT pg_sleep(0.2);")  # fmt: skip
    results: dict[str, list[int] | BaseException] = {}

    def runner(label: str) -> None:
        try:
            with psycopg.connect(database_url, autocommit=True) as c:
                results[label] = migrate(c, mdir)
        except BaseException as exc:  # reported through the assertion below
            results[label] = exc

    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    threads = [threading.Thread(target=runner, args=(x,)) for x in "ab"]
    for thread in threads:
        thread.start()
    waiting = 0
    for _ in range(200):  # both runners must be blocked on the lock we hold
        row = conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
            "AND NOT granted AND database = (SELECT oid FROM pg_database "
            "WHERE datname = current_database())"
        ).fetchone()
        waiting = int(row[0]) if row else 0
        if waiting == 2:
            break
        time.sleep(0.05)
    conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
    for thread in threads:
        thread.join(timeout=30)
    assert waiting == 2
    assert sorted(map(str, results.values())) == ["[1, 2]", "[]"]
    assert versions(conn) == [1, 2]
