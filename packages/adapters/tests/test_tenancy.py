"""Tenant isolation (0002_tenancy, qw_adapters.tenancy) against a real PostgreSQL 16.

Every DB test connects through a throwaway LOGIN role that is a member of `qw_app`
or `qw_worker` (non-superuser, non-owner, NOBYPASSRLS; F-09/F-20, spec 12). The
superuser only migrates and, where a test says so, plays the table owner. Tenants,
users and display names are SYNTHETIC.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from datetime import timedelta

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import TupleRow
from qw_adapters.tenancy import (
    MembershipRole,
    TenancyError,
    add_membership,
    append_audit_event,
    check_runtime_role,
    create_session,
    create_user,
    get_membership,
    lookup_session,
    new_session_token,
    provision_tenant,
    revoke_session,
    session_token_hash,
    tenant_transaction,
)

Conn = psycopg.Connection[TupleRow]
TABLES = ("tenant", "app_user", "membership", "session", "audit_event")
# 0003/0004, exercised by the apps/api tests
API_TABLES = ("local_credential", "idempotency_record", "auth_throttle",
              "installation_bootstrap")  # fmt: skip
JOB_TABLES = ("job", "outbox", "inbox")  # 0005, exercised by test_durable_jobs
# 0007, exercised by test_journal_store
JOURNAL_TABLES = ("source_record", "ledger_event", "posting", "unit_posting")
IMPORT_TABLES = ("import_preview",)  # 0008, exercised by test_import_store


def make_tenant(conn: Conn, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """SYNTHETIC tenant with one owner; returns (tenant_id, user_id)."""
    with provision_tenant(conn) as tx:
        user = create_user(tx, f"SYNTHETIC {name}")
        add_membership(tx, user, MembershipRole.TENANT_OWNER)
        append_audit_event(tx, "tenant.created", actor_user_id=user)
        return tx.tenant_id, user


def count(conn: Conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


def set_tenant(conn: Conn, tenant: uuid.UUID) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))


# --- token scheme (no database) ---


def test_session_token_is_32_random_bytes_base64url() -> None:
    tokens = {new_session_token() for _ in range(64)}
    assert len(tokens) == 64
    for token in tokens:
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
        assert len(base64.urlsafe_b64decode(token + "=")) == 32
        # Independent oracle: sha256 over the ASCII token text.
        assert session_token_hash(token) == hashlib.sha256(token.encode()).digest()


@pytest.mark.parametrize(
    "bad", ["", "a" * 42, "a" * 44, "a" * 42 + "=", "a" * 42 + "+", "é" * 43]
)
def test_malformed_session_token_is_refused(bad: str) -> None:
    with pytest.raises(TenancyError):
        session_token_hash(bad)


@pytest.mark.db
def test_tenant_transaction_refuses_non_uuid_tenant(app_conn: Conn) -> None:
    text_id: object = str(uuid.uuid4())
    with pytest.raises(TypeError), tenant_transaction(app_conn, text_id):  # type: ignore[arg-type]
        pass


@pytest.mark.db
def test_tenant_transaction_refuses_open_transaction(database_url: str) -> None:
    with psycopg.connect(database_url) as conn:  # not autocommit
        conn.execute("SELECT 1")
        with pytest.raises(TenancyError), tenant_transaction(conn, uuid.uuid4()):
            pass


# --- runtime roles ---


@pytest.mark.db
def test_runtime_logins_are_non_privileged(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    check_runtime_role(app_conn)
    check_runtime_role(worker_conn)
    with pytest.raises(TenancyError, match="superuser"):
        check_runtime_role(conn)


@pytest.mark.db
def test_tables_have_forced_rls_and_no_public_grants(conn: Conn) -> None:
    from qw_adapters.migrations import migrate

    migrate(conn)
    rows = conn.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
        "c.relowner::regrole::text, coalesce(c.relacl::text, '') "
        "FROM pg_class c WHERE c.relnamespace = 'app'::regnamespace "
        "AND c.relkind = 'r' ORDER BY 1"
    ).fetchall()
    assert sorted(r[0] for r in rows) == sorted(
        TABLES + API_TABLES + JOB_TABLES + JOURNAL_TABLES + IMPORT_TABLES
    )
    for name, rls, forced, owner, acl in rows:
        assert (rls, forced, owner) == (True, True, "qw_migrate"), name
        assert not re.search(r"(^|[{,])=", acl), f"{name} grants to PUBLIC: {acl}"


@pytest.mark.db
@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE app.membership ADD COLUMN x integer",
        "ALTER TABLE app.membership DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE app.membership NO FORCE ROW LEVEL SECURITY",
        "DROP TABLE app.membership",
        "TRUNCATE app.membership",
        "TRUNCATE app.audit_event",
        "CREATE TABLE app.rogue (id integer)",
        "DROP POLICY tenant_isolation ON app.membership",
        "SELECT token_hash FROM app.session",
        "UPDATE app.tenant SET status = 'active'",
        "UPDATE app.session SET expires_at = now()",
        "INSERT INTO app.audit_event (tenant_id, action, occurred_at) "
        "VALUES (app.current_tenant_id(), 'x.y', now() - interval '1 day')",
    ],
)
def test_app_role_cannot_escalate(app_conn: Conn, statement: str) -> None:
    # Privilege errors, not the row-level security error of the same SQLSTATE.
    tenant, _ = make_tenant(app_conn, "A")
    with (
        pytest.raises(errors.InsufficientPrivilege, match=r"permission denied|owner"),
        tenant_transaction(app_conn, tenant),
    ):
        app_conn.execute(statement)


@pytest.mark.db
def test_row_security_off_errors_instead_of_bypassing(app_conn: Conn) -> None:
    make_tenant(app_conn, "A")
    app_conn.execute("SET row_security = off")
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        app_conn.execute("SELECT * FROM app.tenant")


# --- isolation ---


@pytest.mark.db
def test_tenant_context_hides_other_tenant_rows(app_conn: Conn) -> None:
    a, a_user = make_tenant(app_conn, "A")
    b, b_user = make_tenant(app_conn, "B")
    with tenant_transaction(app_conn, b) as tx:
        create_session(tx, b_user, timedelta(hours=1))
    with tenant_transaction(app_conn, a) as tx:
        assert [count(app_conn, t) for t in TABLES] == [1, 1, 1, 0, 1]
        assert get_membership(tx, b_user) is None
        assert (
            app_conn.execute(
                "SELECT id FROM app.app_user WHERE id = %s", (b_user,)
            ).fetchall()
            == []
        )
        updated = app_conn.execute(
            "UPDATE app.app_user SET display_name = 'x' WHERE id = %s", (b_user,)
        )
        assert updated.rowcount == 0
        upd_session = app_conn.execute(
            "UPDATE app.session SET revoked_at = now() WHERE user_id = %s", (b_user,)
        )
        assert upd_session.rowcount == 0
        membership = get_membership(tx, a_user)
        assert membership is not None and membership.tenant_id == a
    with tenant_transaction(app_conn, b) as tx:
        assert [count(app_conn, t) for t in TABLES] == [1, 1, 1, 1, 1]
        assert get_membership(tx, b_user) is not None


@pytest.mark.db
def test_delete_cannot_reach_other_tenant(app_conn: Conn, conn: Conn) -> None:
    # The runtime role has no DELETE grant; the owner (superuser playing qw_migrate)
    # is itself bound by FORCE RLS.
    a, _ = make_tenant(app_conn, "A")
    b, b_user = make_tenant(app_conn, "B")
    with pytest.raises(errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM app.membership")
    with conn.transaction():
        conn.execute("SET LOCAL ROLE qw_migrate")
        set_tenant(conn, a)
        deleted = conn.execute(
            "DELETE FROM app.membership WHERE user_id = %s", (b_user,)
        )
        assert deleted.rowcount == 0
    with tenant_transaction(app_conn, b) as tx:
        assert get_membership(tx, b_user) is not None


@pytest.mark.db
def test_with_check_rejects_rows_for_another_tenant(app_conn: Conn) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, b_user = make_tenant(app_conn, "B")
    new_user = uuid.uuid4()
    for statement, params in [
        (
            "INSERT INTO app.app_user (tenant_id, id, display_name) "
            "VALUES (%s, %s, 'SYNTHETIC x')",
            (b, new_user),
        ),
        ("INSERT INTO app.tenant (id) VALUES (%s)", (uuid.uuid4(),)),
        (
            "INSERT INTO app.audit_event (tenant_id, action) VALUES (%s, 'x.y')",
            (b,),
        ),
        (
            "INSERT INTO app.membership (tenant_id, user_id, role) "
            "VALUES (%s, %s, 'tenant_member')",
            (b, b_user),
        ),
        (
            "INSERT INTO app.session (id, tenant_id, user_id, token_hash, expires_at)"
            " VALUES (gen_random_uuid(), %s, %s, %s, now() + interval '1 hour')",
            (b, b_user, bytes(32)),
        ),
    ]:
        with (
            pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
            tenant_transaction(app_conn, a),
        ):
            app_conn.execute(statement, params)


@pytest.mark.db
@pytest.mark.parametrize("context", [None, ""])
def test_missing_tenant_context_fails_closed(
    app_conn: Conn, worker_conn: Conn, context: str | None
) -> None:
    a, _ = make_tenant(app_conn, "A")
    for c in (app_conn, worker_conn):
        with c.transaction():
            if context is not None:
                c.execute("SELECT set_config('app.tenant_id', %s, true)", (context,))
            visible = [t for t in TABLES if c is app_conn or t != "session"]
            assert [count(c, t) for t in visible] == [0] * len(visible)
    with app_conn.transaction():
        updated = app_conn.execute("UPDATE app.app_user SET display_name = 'x'")
        assert updated.rowcount == 0
    with (
        pytest.raises(errors.InsufficientPrivilege, match="row-level security"),
        app_conn.transaction(),
    ):
        app_conn.execute(
            "INSERT INTO app.audit_event (tenant_id, action) VALUES (%s, 'x.y')",
            (a,),
        )


@pytest.mark.db
def test_malformed_tenant_context_errors(app_conn: Conn) -> None:
    make_tenant(app_conn, "A")
    with pytest.raises(errors.InvalidTextRepresentation), app_conn.transaction():
        app_conn.execute("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        count(app_conn, "tenant")


@pytest.mark.db
def test_composite_foreign_keys_block_cross_tenant_links(app_conn: Conn) -> None:
    a, a_user = make_tenant(app_conn, "A")
    b, b_user = make_tenant(app_conn, "B")
    with tenant_transaction(app_conn, a) as tx:
        other = create_user(tx, "SYNTHETIC A2")
    # B's context, B's tenant_id, A's user: the (tenant_id, user_id) key is absent.
    for statement, params in [
        (
            "INSERT INTO app.membership (tenant_id, user_id, role) "
            "VALUES (%s, %s, 'tenant_member')",
            (b, other),
        ),
        (
            "INSERT INTO app.audit_event (tenant_id, actor_user_id, action) "
            "VALUES (%s, %s, 'x.y')",
            (b, a_user),
        ),
        (
            "INSERT INTO app.session (tenant_id, id, user_id, token_hash, expires_at)"
            " VALUES (%s, gen_random_uuid(), %s, %s, now() + interval '1 hour')",
            (b, a_user, bytes(32)),
        ),
    ]:
        with (
            pytest.raises(errors.ForeignKeyViolation),
            tenant_transaction(app_conn, b),
        ):
            app_conn.execute(statement, params)
    with (
        pytest.raises(errors.ForeignKeyViolation),
        tenant_transaction(app_conn, b) as tx,
    ):
        add_membership(tx, a_user, MembershipRole.TENANT_MEMBER)
    with tenant_transaction(app_conn, b) as tx:
        assert get_membership(tx, b_user) is not None


@pytest.mark.db
def test_worker_role_is_tenant_scoped_and_cannot_touch_sessions(
    app_conn: Conn, worker_conn: Conn
) -> None:
    a, a_user = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    with tenant_transaction(worker_conn, a) as tx:
        assert get_membership(tx, a_user) is not None
        append_audit_event(tx, "job.ran", target_id=uuid.uuid4())
        assert count(worker_conn, "audit_event") == 2
    with tenant_transaction(worker_conn, b):
        assert count(worker_conn, "audit_event") == 1
    for statement in [
        "SELECT id FROM app.session",
        "INSERT INTO app.app_user (tenant_id, id, display_name) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), 'x')",
        "UPDATE app.membership SET role = 'tenant_owner'",
    ]:
        with pytest.raises(errors.InsufficientPrivilege):
            worker_conn.execute(statement)


# --- audit_event is append-only ---


@pytest.mark.db
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_audit_event_is_append_only_even_for_owner(
    app_conn: Conn, worker_conn: Conn, conn: Conn, operation: str
) -> None:
    a, _ = make_tenant(app_conn, "A")
    statement = {
        "UPDATE": "UPDATE app.audit_event SET action = 'x.forged'",
        "DELETE": "DELETE FROM app.audit_event",
        "TRUNCATE": "TRUNCATE app.audit_event",
    }[operation]
    for runtime in (app_conn, worker_conn):
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tenant_transaction(runtime, a),
        ):
            runtime.execute(statement)
    for role in ("qw_migrate", None):  # table owner, then the superuser itself
        with (
            pytest.raises(errors.RaiseException, match="append-only"),
            conn.transaction(),
        ):
            if role:
                conn.execute("SET LOCAL ROLE qw_migrate")
            set_tenant(conn, a)
            conn.execute(statement)
    with tenant_transaction(app_conn, a):
        assert count(app_conn, "audit_event") == 1


# --- sessions ---


@pytest.mark.db
def test_session_lookup_derives_tenant_and_role(app_conn: Conn, conn: Conn) -> None:
    a, a_user = make_tenant(app_conn, "A")
    make_tenant(app_conn, "B")
    with tenant_transaction(app_conn, a) as tx:
        session_id, token = create_session(tx, a_user, timedelta(hours=8))
    found = lookup_session(app_conn, token)
    assert found is not None
    assert (found.session_id, found.tenant_id, found.user_id) == (session_id, a, a_user)
    assert found.role is MembershipRole.TENANT_OWNER
    assert found.expires_at - found.created_at == timedelta(hours=8)
    assert found.expires_at.utcoffset() == timedelta(0) and found.step_up_at is None
    # Stored: the sha256 of the token, never the token (read as the superuser).
    stored = conn.execute(
        "SELECT token_hash FROM app.session WHERE id = %s", (session_id,)
    ).fetchone()
    assert stored == (hashlib.sha256(token.encode()).digest(),)
    assert lookup_session(app_conn, new_session_token()) is None
    assert lookup_session(app_conn, "not a token") is None
    # Lookup leaves no tenant or token setting behind.
    row = app_conn.execute(
        "SELECT coalesce(current_setting('app.tenant_id', true), ''), "
        "coalesce(current_setting('app.session_token_hash', true), '')"
    ).fetchone()
    assert row == ("", "")


@pytest.mark.db
def test_token_path_cannot_list_sessions_inside_a_tenant_context(
    app_conn: Conn,
) -> None:
    a, a_user = make_tenant(app_conn, "A")
    b, b_user = make_tenant(app_conn, "B")
    with tenant_transaction(app_conn, b) as tx:
        _, b_token = create_session(tx, b_user, timedelta(hours=1))
    with tenant_transaction(app_conn, a):
        app_conn.execute(
            "SELECT set_config('app.session_token_hash', %s, true)",
            (session_token_hash(b_token).hex(),),
        )
        assert count(app_conn, "session") == 0
    assert a_user is not None


@pytest.mark.db
def test_expired_or_revoked_session_is_not_returned(app_conn: Conn) -> None:
    a, a_user = make_tenant(app_conn, "A")
    with tenant_transaction(app_conn, a) as tx:
        _, expired = create_session(tx, a_user, timedelta(microseconds=1))
        revoked_id, revoked = create_session(tx, a_user, timedelta(hours=1))
        live_id, live = create_session(tx, a_user, timedelta(hours=1))
    with tenant_transaction(app_conn, a) as tx:
        assert revoke_session(tx, revoked_id) is True
        assert revoke_session(tx, revoked_id) is False
    assert lookup_session(app_conn, expired) is None
    assert lookup_session(app_conn, revoked) is None
    found = lookup_session(app_conn, live)
    assert found is not None and found.session_id == live_id
    with pytest.raises(TenancyError), tenant_transaction(app_conn, a) as tx:
        create_session(tx, a_user, timedelta(0))


@pytest.mark.db
def test_session_requires_membership_in_the_same_tenant(app_conn: Conn) -> None:
    a, _ = make_tenant(app_conn, "A")
    with tenant_transaction(app_conn, a) as tx:
        lonely = create_user(tx, "SYNTHETIC no membership")
    with (
        pytest.raises(errors.ForeignKeyViolation),
        tenant_transaction(app_conn, a) as tx,
    ):
        create_session(tx, lonely, timedelta(hours=1))


@pytest.mark.db
def test_session_of_suspended_tenant_is_not_returned(
    app_conn: Conn, conn: Conn
) -> None:
    a, a_user = make_tenant(app_conn, "A")
    with tenant_transaction(app_conn, a) as tx:
        _, token = create_session(tx, a_user, timedelta(hours=1))
    # The superuser stands in for the (not yet built) operator suspension path.
    conn.execute("UPDATE app.tenant SET status = 'suspended' WHERE id = %s", (a,))
    assert lookup_session(app_conn, token) is None
    conn.execute("UPDATE app.tenant SET status = 'active' WHERE id = %s", (a,))
    assert lookup_session(app_conn, token) is not None


@pytest.mark.db
def test_session_bounds_hold_against_raw_sql(app_conn: Conn, conn: Conn) -> None:
    a, a_user = make_tenant(app_conn, "A")
    insert = (
        "INSERT INTO app.session (id, tenant_id, user_id, token_hash, expires_at) "
        "VALUES (gen_random_uuid(), %s, %s, %s, {})"
    )
    for expires in ("'9999-12-31T00:00:00Z'", "now() + interval '30 days 1 second'"):
        with pytest.raises(errors.CheckViolation), tenant_transaction(app_conn, a):
            app_conn.execute(
                sql.SQL(insert).format(sql.SQL(expires)),
                (a, a_user, hashlib.sha256(expires.encode()).digest()),
            )
    with tenant_transaction(app_conn, a) as tx:
        session_id, token = create_session(tx, a_user, timedelta(hours=1))
    bypasses = [
        "UPDATE app.session SET revoked_at = now() - interval '1 minute'",
        "UPDATE app.session SET revoked_at = created_at - interval '1 day'",
        "UPDATE app.session SET step_up_at = now() - interval '1 hour'",
        "UPDATE app.session SET step_up_at = now() + interval '1 hour'",
    ]
    for statement in bypasses:
        with (
            pytest.raises(errors.RaiseException, match=r"^app\.session: "),
            tenant_transaction(app_conn, a),
        ):
            app_conn.execute(statement)
    with pytest.raises(errors.InsufficientPrivilege), tenant_transaction(app_conn, a):
        app_conn.execute("UPDATE app.session SET expires_at = '9999-12-31T00:00:00Z'")
    with tenant_transaction(app_conn, a) as tx:
        assert revoke_session(tx, session_id) is True
    for statement in [
        "UPDATE app.session SET revoked_at = NULL",  # un-revoke
        "UPDATE app.session SET revoked_at = now()",  # re-revoke at a new time
    ]:
        with (
            pytest.raises(errors.RaiseException, match="set once"),
            tenant_transaction(app_conn, a),
        ):
            app_conn.execute(statement)
    # The owner is bound by the trigger too.
    with (
        pytest.raises(errors.RaiseException, match="only revoked_at"),
        conn.transaction(),
    ):
        conn.execute("SET LOCAL ROLE qw_migrate")
        set_tenant(conn, a)
        conn.execute("UPDATE app.session SET expires_at = expires_at + interval '1 h'")
    assert lookup_session(app_conn, token) is None


@pytest.mark.db
def test_check_runtime_role_requires_runtime_group(
    runtime_urls: dict[str, str], database_url: str, conn: Conn
) -> None:
    login = f"qwtest_nogroup_{uuid.uuid4().hex[:12]}"
    ident = sql.Identifier(login)
    conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS").format(ident))
    try:
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(conn.info.dbname), ident
            )
        )
        with (
            psycopg.connect(make_conninfo(database_url, user=login)) as other,
            pytest.raises(TenancyError, match="not a member"),
        ):
            check_runtime_role(other)
    finally:
        conn.execute(sql.SQL("DROP OWNED BY {}").format(ident))
        conn.execute(sql.SQL("DROP ROLE {}").format(ident))
