"""`qw-migrate` on a real PostgreSQL 16 (T060; owner decision 108). The db-init job
is emulated as the harness superuser: it applies only 0001 and creates a
non-superuser qw_migrate login whose generated SYNTHETIC password is in a 0600
passfile. Against a password-authenticating server (see the T060 receipt) the
passfile is what authenticates; under the harness's trust auth it is checked but
unused."""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import TupleRow
from qw_adapters import migrate_cli, migrations

Conn = psycopg.Connection[TupleRow]
Login = Callable[..., str]
Cap = pytest.CaptureFixture[str]
pytestmark = pytest.mark.db
CANARY = "SYNTHETIC-canary-5d1e7a9c3b0f2e4d6c8a1b3d5f7e9a0c"


def bootstrap_only(conn: Conn, tmp_path: Path) -> None:
    """The db-init job's migration step: 0001 alone, as the superuser."""
    only = tmp_path / "bootstrap"
    only.mkdir()
    shutil.copy(migrations.MIGRATIONS_DIR / "0001_roles_and_baseline.sql", only)
    assert migrations.migrate(conn, only) == [1]


@pytest.fixture
def make_login(database_url: str, pg_admin_url: str, tmp_path: Path) -> Iterator[Login]:
    """Creates LOGIN roles with a generated password in a 0600 passfile and yields
    their passfile-only conninfo; drops them afterwards."""
    made: list[str] = []
    base = {k: v for k, v in conninfo_to_dict(database_url).items() if k != "password"}

    def make(*, group: str | None = "qw_migrate", superuser: bool = False) -> str:
        login, password = f"qwtest_mig_{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
        with psycopg.connect(pg_admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN {} PASSWORD {}{}").format(
                    sql.Identifier(login),
                    sql.SQL("SUPERUSER" if superuser else "NOSUPERUSER NOBYPASSRLS"),
                    sql.Literal(password),
                    sql.SQL(" IN ROLE {}").format(sql.Identifier(group))
                    if group
                    else sql.SQL(""),
                )
            )
        made.append(login)
        passfile = tmp_path / f"{login}.pgpass"
        passfile.write_text(f"*:*:*:{login}:{password}\n")
        passfile.chmod(0o600)
        return make_conninfo("", **{**base, "user": login, "passfile": str(passfile)})

    yield make
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        for login in made:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE usename = %s",
                (login,),
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.fixture
def initialised(conn: Conn, make_login: Login, tmp_path: Path) -> str:
    """0001 applied by the db-init emulation; a qw_migrate login's conninfo."""
    bootstrap_only(conn, tmp_path)
    return make_login()


def run(conninfo: str, capsys: Cap) -> tuple[int, str]:
    code = migrate_cli.main([], {migrate_cli.URL_VARIABLE: conninfo})
    out = capsys.readouterr()
    return code, out.out + out.err


def test_applies_pending_migrations_as_the_qw_migrate_login(
    conn: Conn, initialised: str, capsys: Cap
) -> None:
    url, total = initialised, len(migrations.load_migrations())
    code, out = run(url, capsys)
    assert (code, out) == (0, f"migrated: {total - 1} applied (versions 2-{total})\n")
    applied = migrations.applied_migrations(conn)
    assert sorted(applied) == list(range(1, total + 1))
    owners = conn.execute(
        "SELECT DISTINCT relowner::regrole::text FROM pg_class "
        "WHERE relnamespace = 'app'::regnamespace"
    ).fetchall()
    assert owners == [("qw_migrate",)]
    assert run(url, capsys) == (0, "migrated: 0 applied\n")  # idempotent


def test_refuses_an_uninitialised_database(
    conn: Conn, make_login: Login, capsys: Cap
) -> None:
    url = make_login(group=None)  # roles of 0001 may not exist yet
    code, out = run(url, capsys)
    assert code == 1 and "db-init" in out and "not been initialised" in out
    assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() \
        == (None,)  # fmt: skip
    with psycopg.connect(url, autocommit=True) as c, pytest.raises(
        migrate_cli.NotInitialised
    ):  # fmt: skip
        migrate_cli.preflight(c)


def test_refuses_a_ledger_without_bootstrap_or_grant(
    conn: Conn, initialised: str, capsys: Cap
) -> None:
    url = initialised
    conn.execute("REVOKE INSERT ON public.schema_migrations FROM qw_migrate")
    assert "predates the qw_migrate grant" in run(url, capsys)[1]  # SELECT alone
    conn.execute("GRANT INSERT ON public.schema_migrations TO qw_migrate")
    conn.execute("DELETE FROM public.schema_migrations WHERE version = 1")
    code, out = run(url, capsys)
    assert code == 1 and "not been initialised" in out and "predates" not in out
    assert migrations.applied_migrations(conn) == {}


def test_an_old_runner_ledger_is_repaired_by_a_superuser_run(
    conn: Conn, initialised: str, tmp_path: Path, capsys: Cap,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    url = initialised  # SYNTHETIC: what 0001 left under the pre-guard runner
    conn.execute("DROP FUNCTION public.schema_migrations_contiguous() CASCADE; "
                 "REVOKE ALL ON public.schema_migrations FROM qw_migrate")  # fmt: skip
    assert not migrations.ledger_guarded(conn)
    code, out = run(url, capsys)
    assert code == 1 and "predates the qw_migrate grant and contiguity trigger" in out
    assert "migrations.migrate() once as the PostgreSQL superuser" in out
    with caplog.at_level(logging.WARNING, "qw_adapters.migrations"):
        assert migrations.migrate(conn, tmp_path / "bootstrap") == []
    assert "ledger guard installed" in caplog.text
    assert migrations.ledger_guarded(conn)
    caplog.clear()
    with caplog.at_level(logging.WARNING, "qw_adapters.migrations"):
        migrations.migrate(conn, tmp_path / "bootstrap")  # idempotent, silent
    assert caplog.text == ""
    conn.execute("ALTER TABLE public.schema_migrations DISABLE TRIGGER "
                 "schema_migrations_contiguous")  # fmt: skip
    assert not migrations.ledger_guarded(conn)
    migrations.migrate(conn, tmp_path / "bootstrap")  # re-enables it
    assert migrations.ledger_guarded(conn)
    assert run(url, capsys)[0] == 0
    assert migrations.ledger_guarded(conn)


def test_a_replica_only_trigger_is_no_guard(
    conn: Conn, initialised: str, tmp_path: Path, capsys: Cap
) -> None:
    """tgenabled 'R' fires only under session_replication_role = replica, so in an
    ordinary session the ledger is unguarded; 'A' (always) still guards."""
    url, table = initialised, "ALTER TABLE public.schema_migrations "
    conn.execute(table + "ENABLE REPLICA TRIGGER schema_migrations_contiguous")
    assert not migrations.ledger_guarded(conn)
    code, out = run(url, capsys)
    assert code == 1 and "predates the qw_migrate grant" in out
    assert sorted(migrations.applied_migrations(conn)) == [1]
    migrations.migrate(conn, tmp_path / "bootstrap")  # the superuser repair
    tgenabled = "SELECT tgenabled FROM pg_trigger WHERE tgname = %s"
    row = conn.execute(tgenabled, ("schema_migrations_contiguous",)).fetchone()
    assert row == ("O",) and migrations.ledger_guarded(conn)
    conn.execute(table + "ENABLE ALWAYS TRIGGER schema_migrations_contiguous")
    assert migrations.ledger_guarded(conn)
    assert run(url, capsys)[0] == 0


def test_qw_migrate_can_forge_only_the_next_ledger_row(
    conn: Conn, initialised: str, capsys: Cap
) -> None:
    """Pins the accepted trade-off (T060 receipt): the qw_migrate credential can
    record the next pending migration as applied, so it is skipped, which adds
    nothing to what its DDL as owner can already undo. A gap, an update, a delete
    or turning the guard off is refused."""
    url = initialised
    two, five = migrations.load_migrations()[1], migrations.load_migrations()[4]
    insert = "INSERT INTO public.schema_migrations (version, name, checksum) "
    insert += "VALUES (%s, %s, %s)"
    with psycopg.connect(url, autocommit=True) as forger:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            forger.execute(insert, (5, five.name, five.checksum))
        for stmt in (
            "UPDATE public.schema_migrations SET checksum = repeat('0', 64)",
            "DELETE FROM public.schema_migrations",
            "ALTER TABLE public.schema_migrations DISABLE TRIGGER ALL",
            "DROP TRIGGER schema_migrations_contiguous ON public.schema_migrations",
            "DROP FUNCTION public.schema_migrations_contiguous()",
            "SET session_replication_role = replica",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                forger.execute(stmt)
        assert sorted(migrations.applied_migrations(conn)) == [1]
        forger.execute(insert, (2, two.name, two.checksum))  # the accepted forge
    # 0002 is skipped: the run starts at 0003, which fails without 0002's tables.
    code, out = run(url, capsys)
    assert code == 1 and out.startswith("failed: 0003_")
    assert sorted(migrations.applied_migrations(conn)) == [1, 2]
    tenant = conn.execute("SELECT to_regclass('app.tenant')").fetchone()
    assert tenant == (None,) and migrations.ledger_guarded(conn)


def test_refuses_a_superuser_its_members_and_a_non_member(
    conn: Conn, initialised: str, make_login: Login, pg_admin_url: str, capsys: Cap
) -> None:
    code, out = run(make_login(group=None, superuser=True), capsys)
    assert code == 1 and "superuser" in out
    code, out = run(make_login(group="qw_app"), capsys)
    assert code == 1 and "not a member of qw_migrate" in out
    login, su = conninfo_to_dict(initialised)["user"], f"qwtest_su_{uuid.uuid4().hex}"
    ids = sql.Identifier(su), sql.Identifier(str(login))
    with psycopg.connect(pg_admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN SUPERUSER").format(ids[0]))
        try:  # a member with inherited privileges (USAGE) or SET ROLE only
            for option in ("INHERIT TRUE, SET FALSE", "INHERIT FALSE, SET TRUE"):
                grant = sql.SQL("GRANT {} TO {} WITH {}")
                admin.execute(grant.format(*ids, sql.SQL(option)))
                code, out = run(initialised, capsys)
                assert code == 1 and "member of a superuser role" in out, option
                admin.execute(sql.SQL("REVOKE {} FROM {}").format(*ids))
        finally:
            admin.execute(sql.SQL("DROP ROLE {}").format(ids[0]))
    assert sorted(migrations.applied_migrations(conn)) == [1]


def test_refuses_inline_passwords_before_connecting(
    make_login: Login, capsys: Cap
) -> None:
    url = make_login()
    no_passfile = conninfo_to_dict(url)
    del no_passfile["passfile"]
    for env in (
        {migrate_cli.URL_VARIABLE: f"{url} password={CANARY}"},
        {migrate_cli.URL_VARIABLE: url, "PGPASSWORD": CANARY},
        {migrate_cli.URL_VARIABLE: make_conninfo("", **no_passfile)},
        {},
    ):
        code = migrate_cli.main([], env)
        out = capsys.readouterr()
        assert code == 2 and out.err.startswith("refused: ")
        assert CANARY not in out.out + out.err
