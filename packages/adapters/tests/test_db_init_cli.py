"""`qw-db-init` then `qw-migrate` on a real PostgreSQL 16 (T060; owner decision 108).
Every password is a generated SYNTHETIC value in a 0600 file under tmp_path. The
harness uses trust auth, so the passfiles are checked but do not authenticate; the
stored verifier is checked instead."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from qw_adapters import db_init_cli, migrate_cli, migrations, tenancy

Conn = psycopg.Connection[TupleRow]
Cap = pytest.CaptureFixture[str]
pytestmark = pytest.mark.db


@pytest.fixture
def names(pg_admin_url: str) -> Iterator[dict[str, str]]:
    """Unique login names per group; the roles are dropped afterwards."""
    suffix = uuid.uuid4().hex[:10]
    made = {g: f"qwtest_{g[3:]}_{suffix}" for g in ("qw_migrate", "qw_app")}
    yield made
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        for login in made.values():
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE usename = %s", (login,))  # fmt: skip
            admin.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login))
            )


def secret(tmp_path: Path, name: str, text: str, mode: int = 0o600) -> str:
    path = tmp_path / name
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def env_for(url: str, tmp_path: Path, names: dict[str, str]) -> dict[str, str]:
    admin = conninfo_to_dict(url)["user"]
    su = secret(tmp_path, "su.pgpass", f"*:*:*:{admin}:SYNTHETIC-unused\n")
    env = {db_init_cli.URL_VARIABLE: make_conninfo(url, passfile=su)}
    for group, login in names.items():
        line = f"*:*:*:{login}:SYNTHETIC-{uuid.uuid4().hex}\n"
        env[db_init_cli.PASSFILE_PREFIX + group.upper()] = secret(
            tmp_path, f"{group}.pgpass", line
        )
    return env


def run(env: dict[str, str], capsys: Cap) -> tuple[int, str]:
    code = db_init_cli.main([], env)
    out = capsys.readouterr()
    return code, out.out + out.err


def test_db_init_then_qw_migrate_then_the_app_login(
    conn: Conn, database_url: str, tmp_path: Path, names: dict[str, str], capsys: Cap
) -> None:
    env = env_for(database_url, tmp_path, names)
    mig, app = names["qw_migrate"], names["qw_app"]
    code, out = run(env, capsys)
    assert (code, out) == (0, f"db-init: 0001 applied; created {mig} (qw_migrate); "
                              f"created {app} (qw_app)\n")  # fmt: skip
    assert sorted(migrations.applied_migrations(conn)) == [1]
    assert migrations.ledger_guarded(conn)
    for group, login in names.items():
        row = conn.execute(
            "SELECT rolcanlogin, rolsuper OR rolbypassrls OR rolcreaterole OR "
            "rolcreatedb OR rolreplication, rolpassword, ARRAY(SELECT roleid::regrole"
            "::text FROM pg_auth_members WHERE member = a.oid) FROM pg_authid a "
            "WHERE rolname = %s", (login,),
        ).fetchone()  # fmt: skip
        assert row is not None and row[0] is True and row[1] is False
        assert row[2].startswith("SCRAM-SHA-256$") and "SYNTHETIC" not in row[2]
        assert row[3] == [group]
    # migrate-once: the later migrations as the qw_migrate login
    passfile = env["QW_DBINIT_PASSFILE_QW_MIGRATE"]
    mig_url = make_conninfo(database_url, user=mig, passfile=passfile)
    assert migrate_cli.main([], {migrate_cli.URL_VARIABLE: mig_url}) == 0
    total = len(migrations.load_migrations())
    assert sorted(migrations.applied_migrations(conn)) == list(range(1, total + 1))
    with psycopg.connect(make_conninfo(database_url, user=app)) as runtime:
        tenancy.check_runtime_role(runtime, "qw_app")
    capsys.readouterr()
    # a rerun after the later migrations: nothing applied, verifiers rotated
    before = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                          (app,)).fetchone()  # fmt: skip
    env = env_for(database_url, tmp_path, names)
    code, out = run(env, capsys)
    assert code == 0 and out.startswith("db-init: 0001 already applied; updated ")
    after = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                         (app,)).fetchone()  # fmt: skip
    assert before != after
    assert sorted(migrations.applied_migrations(conn)) == list(range(1, total + 1))


OK = "SYNTHETIC-0123456789abcdef"


@pytest.mark.parametrize(
    ("group", "text", "mode", "reason"),
    [
        ("qw_migrate", None, 0o600, "QW_DBINIT_PASSFILE_QW_MIGRATE: not set"),
        ("qw_app", "*:*:*:{login}:" + OK + "\n", 0o640, "group or world"),
        ("qw_app", "*:*:*:{login}\n", 0o600, "exactly one"),
        ("qw_app", "a\nb\n", 0o600, "exactly one"),
        ("qw_app", "*:*:*:{login}:short\n", 0o600, "16+ printable"),
        ("qw_app", "*:*:*:qw_app:" + OK + "\n", 0o600, "not allowed"),
        ("qw_app", "*:*:*:pg_x:" + OK + "\n", 0o600, "not allowed"),
        ("qw_app", "*:*:*:{other}:" + OK + "\n", 0o600, "own login"),
    ],
)  # fmt: skip
def test_configuration_is_refused_before_connecting(
    database_url: str, conn: Conn, tmp_path: Path, names: dict[str, str], capsys: Cap,
    group: str, text: str | None, mode: int, reason: str,
) -> None:  # fmt: skip
    env = env_for(database_url, tmp_path, names)
    variable = db_init_cli.PASSFILE_PREFIX + group.upper()
    if text is None:
        del env[variable]
    else:
        body = text.format(login=names[group], other=names["qw_migrate"])
        env[variable] = secret(tmp_path, "bad.pgpass", body, mode)
    code, out = run(env, capsys)
    assert code == 2 and reason in out and "SYNTHETIC" not in out
    assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() \
        == (None,)  # fmt: skip


def test_refuses_inline_secrets_a_non_superuser_and_a_foreign_role(
    database_url: str, conn: Conn, tmp_path: Path, names: dict[str, str], capsys: Cap
) -> None:
    env = env_for(database_url, tmp_path, names)
    assert run({**env, "PGPASSWORD": "SYNTHETIC-x"}, capsys)[0] == 2
    url = make_conninfo(env[db_init_cli.URL_VARIABLE], password="SYNTHETIC-x")
    code, out = run({**env, db_init_cli.URL_VARIABLE: url}, capsys)
    assert code == 2 and "password=" in out and "SYNTHETIC" not in out
    plain = names["qw_app"]  # a non-superuser connecting as db-init
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(plain)))
    nosu = make_conninfo(env[db_init_cli.URL_VARIABLE], user=plain)
    code, out = run({**env, db_init_cli.URL_VARIABLE: nosu}, capsys)
    assert code == 1 and "needs a PostgreSQL superuser" in out
    # an existing role of that name that is not exactly a qw_app login
    conn.execute(sql.SQL("ALTER ROLE {} SUPERUSER").format(sql.Identifier(plain)))
    code, out = run(env, capsys)
    assert code == 1 and f"role {plain} exists and is not exactly a qw_app" in out
    row = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = %s", (plain,))
    assert row.fetchone() == (True,)  # unchanged
    login = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s",
                         (names["qw_migrate"],)).fetchone()  # fmt: skip
    assert login is None  # the logins are created in one transaction


def test_check_config_checks_db_init_and_qw_migrate_without_connecting(
    tmp_path: Path, capsys: Cap
) -> None:
    """`--check-config` (T060): the configuration checks of each command, exit 0 or
    2, and no connection (no server is at this host)."""
    host = f"host={tmp_path / 'no-socket'} dbname=qw"
    su = secret(tmp_path, "su.pgpass", "*:*:*:postgres:SYNTHETIC-unused\n")
    line = f"*:*:*:qwtest_migrate_cc:SYNTHETIC-{uuid.uuid4().hex}\n"
    mig = secret(tmp_path, "qw_migrate.pgpass", line)
    init = {db_init_cli.URL_VARIABLE: f"{host} user=postgres passfile={su}",
            db_init_cli.PASSFILE_PREFIX + "QW_MIGRATE": mig}  # fmt: skip
    later = {migrate_cli.URL_VARIABLE: f"{host} user=qwtest_migrate_cc passfile={mig}"}
    for main, env, name in ((db_init_cli.main, init, "qw-db-init"),
                            (migrate_cli.main, later, "qw-migrate")):  # fmt: skip
        assert main(["--check-config"], env) == 0
        assert capsys.readouterr().out == f"config ok: {name}\n"
        assert main(["--check-config"], {**env, "PGPASSWORD": "SYNTHETIC-x"}) == 2
        out = capsys.readouterr()
        assert out.err.startswith("refused: PGPASSWORD") and "SYNTHETIC" not in out.err
    Path(mig).chmod(0o640)
    for main, env in ((db_init_cli.main, init), (migrate_cli.main, later)):
        assert main(["--check-config"], env) == 2
        assert "group or world" in capsys.readouterr().err
    Path(mig).chmod(0o600)
    missing = {k: v for k, v in init.items() if k != db_init_cli.URL_VARIABLE}
    assert db_init_cli.main(["--check-config"], missing) == 2
