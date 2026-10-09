"""Tenant-scoped PostgreSQL access (T010; spec 03 "Storage and access", spec 12).

`tenant_transaction` opens a transaction on an idle autocommit connection and sets
`app.tenant_id` with `set_config(..., true)` (transaction-local), so the setting
ends with the transaction. The tenant id must be a `uuid.UUID` the server derived,
e.g. from `lookup_session`; a client string is refused. Repository functions take
the yielded `TenantTx` and write its tenant id, never a caller-supplied one. Row
level security in `0002_tenancy.sql` is the enforcement; this module only supplies
the context.

Session tokens: 32 bytes from `secrets`, base64url without padding (43 characters).
The database stores sha256 over the token's ASCII text, never the token. Lookup
sets the hex hash as `app.session_token_hash` and lets PostgreSQL match it, so no
secret comparison happens in Python. Expiry and revocation are checked against the
database clock. Connections must log in as a member of `qw_app` or `qw_worker`;
`check_runtime_role` refuses superusers, BYPASSRLS roles and members of qw_migrate.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import psycopg
from psycopg import pq
from psycopg.rows import TupleRow

Conn = psycopg.Connection[TupleRow]
TOKEN_BYTES = 32
MAX_SESSION_TTL = timedelta(days=30)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class TenancyError(Exception):
    """Tenant context, session token or runtime role is unusable."""


class MembershipRole(StrEnum):
    TENANT_OWNER = "tenant_owner"
    TENANT_MEMBER = "tenant_member"


@dataclass(frozen=True, slots=True)
class TenantTx:
    conn: Conn
    tenant_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class Membership:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: MembershipRole
    version: int


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: MembershipRole
    created_at: datetime
    expires_at: datetime
    step_up_at: datetime | None


@contextmanager
def tenant_transaction(conn: Conn, tenant_id: uuid.UUID) -> Iterator[TenantTx]:
    if type(tenant_id) is not uuid.UUID:
        raise TypeError("tenant_id must be a uuid.UUID derived by the server")
    if not conn.autocommit or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        # Inside an outer transaction the setting would outlive this block.
        raise TenancyError("tenant_transaction needs an idle autocommit connection")
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield TenantTx(conn, tenant_id)


@contextmanager
def provision_tenant(conn: Conn) -> Iterator[TenantTx]:
    """Create a tenant with a fresh server-generated id; the caller adds its first
    user and owner membership in the same transaction."""
    with tenant_transaction(conn, uuid.uuid4()) as tx:
        conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tx.tenant_id,))
        yield tx


def create_user(tx: TenantTx, display_name: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    tx.conn.execute(
        "INSERT INTO app.app_user (id, tenant_id, display_name) VALUES (%s, %s, %s)",
        (user_id, tx.tenant_id, display_name),
    )
    return user_id


def add_membership(tx: TenantTx, user_id: uuid.UUID, role: MembershipRole) -> None:
    tx.conn.execute(
        "INSERT INTO app.membership (tenant_id, user_id, role) VALUES (%s, %s, %s)",
        (tx.tenant_id, user_id, MembershipRole(role).value),
    )


def get_membership(tx: TenantTx, user_id: uuid.UUID) -> Membership | None:
    row = tx.conn.execute(
        "SELECT role::text, version FROM app.membership "
        "WHERE tenant_id = %s AND user_id = %s",
        (tx.tenant_id, user_id),
    ).fetchone()
    if row is None:
        return None
    return Membership(tx.tenant_id, user_id, MembershipRole(row[0]), int(row[1]))


def append_audit_event(
    tx: TenantTx,
    action: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> uuid.UUID:
    event_id = uuid.uuid4()
    tx.conn.execute(
        "INSERT INTO app.audit_event (id, tenant_id, actor_user_id, action, "
        "target_id, correlation_id) VALUES (%s, %s, %s, %s, %s, %s)",
        (event_id, tx.tenant_id, actor_user_id, action, target_id, correlation_id),
    )
    return event_id


def new_session_token() -> str:
    return (
        base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode().rstrip("=")
    )


def session_token_hash(token: str) -> bytes:
    if not _TOKEN.fullmatch(token):
        raise TenancyError("malformed session token")
    return hashlib.sha256(token.encode("ascii")).digest()


def create_session(
    tx: TenantTx, user_id: uuid.UUID, ttl: timedelta
) -> tuple[uuid.UUID, str]:
    """Insert a session for a member of the tenant; returns (session id, token).
    The token is returned once and is not recoverable from the database."""
    if not timedelta(0) < ttl <= MAX_SESSION_TTL:
        raise TenancyError(f"session ttl must be in (0, {MAX_SESSION_TTL}]")
    active = tx.conn.execute(
        "SELECT 1 FROM app.tenant WHERE id = %s AND status = 'active'", (tx.tenant_id,)
    ).fetchone()
    if active is None:
        raise TenancyError("tenant is not active")
    session_id, token = uuid.uuid4(), new_session_token()
    tx.conn.execute(
        "INSERT INTO app.session (id, tenant_id, user_id, token_hash, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + %s)",
        (session_id, tx.tenant_id, user_id, session_token_hash(token), ttl),
    )
    return session_id, token


def lookup_session(
    conn: Conn, token: str, idle: timedelta | None = None
) -> SessionPrincipal | None:
    """The live session and membership for a presented token, or None when the token
    is malformed, unknown, expired, revoked or (with `idle`) unused for longer than
    `idle`, the membership is gone or the tenant is not active. With `idle`, use is
    recorded in last_used_at at most every min(60 s, idle // 2)."""
    try:
        digest = session_token_hash(token)
    except TenancyError:
        return None
    if not conn.autocommit or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise TenancyError("lookup_session needs an idle autocommit connection")
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.tenant_id', '', true), "
            "set_config('app.session_token_hash', %s, true)",
            (digest.hex(),),
        )
        found = conn.execute(
            "SELECT id, tenant_id, user_id, created_at, expires_at, step_up_at "
            "FROM app.session WHERE expires_at > now() AND revoked_at IS NULL AND "
            "(%(idle)s::interval IS NULL OR "
            "coalesce(last_used_at, created_at) > now() - %(idle)s::interval)",
            {"idle": idle},
        ).fetchall()
        if len(found) != 1:
            return None
        session_id, tenant_id, user_id, created_at, expires_at, step_up_at = found[0]
        conn.execute(
            "SELECT set_config('app.session_token_hash', '', true), "
            "set_config('app.tenant_id', %s, true)",
            (str(tenant_id),),
        )
        # The tenant must be active: checked in SQL, in the session's own context.
        member = conn.execute(
            "SELECT m.role::text FROM app.membership m "
            "JOIN app.tenant t ON t.id = m.tenant_id AND t.status = 'active' "
            "WHERE m.tenant_id = %s AND m.user_id = %s",
            (tenant_id, user_id),
        ).fetchone()
        if member is not None and idle is not None:
            conn.execute(
                "UPDATE app.session SET last_used_at = now() WHERE id = %s AND "
                "(last_used_at IS NULL OR last_used_at < now() - %s)",
                (session_id, min(timedelta(seconds=60), idle // 2)),
            )
    if member is None:
        return None
    return SessionPrincipal(
        session_id, tenant_id, user_id, MembershipRole(member[0]), created_at,
        expires_at, step_up_at,
    )  # fmt: skip


def revoke_session(tx: TenantTx, session_id: uuid.UUID) -> bool:
    """Revoke a live session of this tenant; False if absent or already revoked."""
    cur = tx.conn.execute(
        "UPDATE app.session SET revoked_at = now() "
        "WHERE tenant_id = %s AND id = %s AND revoked_at IS NULL",
        (tx.tenant_id, session_id),
    )
    return cur.rowcount == 1


def mark_step_up(tx: TenantTx, session_id: uuid.UUID) -> datetime | None:
    """Set step_up_at = now() on a live session after the caller re-verified."""
    row = tx.conn.execute(
        "UPDATE app.session SET step_up_at = now() WHERE tenant_id = %s AND id = %s "
        "AND revoked_at IS NULL AND expires_at > now() RETURNING step_up_at",
        (tx.tenant_id, session_id),
    ).fetchone()
    return None if row is None else row[0]


def list_memberships(
    tx: TenantTx, limit: int, after: uuid.UUID | None = None
) -> list[tuple[Membership, str]]:
    """This tenant's memberships with display names, by user id, after `after`."""
    rows = tx.conn.execute(
        "SELECT m.user_id, m.role::text, m.version, u.display_name "
        "FROM app.membership m JOIN app.app_user u "
        "ON u.tenant_id = m.tenant_id AND u.id = m.user_id WHERE m.tenant_id = %s "
        "AND (%s::uuid IS NULL OR m.user_id > %s) ORDER BY m.user_id LIMIT %s",
        (tx.tenant_id, after, after, limit),
    ).fetchall()
    return [
        (Membership(tx.tenant_id, u, MembershipRole(r), int(v)), str(n))
        for u, r, v, n in rows
    ]


def lock_owners(tx: TenantTx) -> frozenset[uuid.UUID]:
    """The owners, rows locked until commit so concurrent role changes serialise."""
    rows = tx.conn.execute(
        "SELECT user_id FROM app.membership WHERE tenant_id = %s "
        "AND role = 'tenant_owner' ORDER BY user_id FOR UPDATE",
        (tx.tenant_id,),
    ).fetchall()
    return frozenset(r[0] for r in rows)


def set_member_role(
    tx: TenantTx, user_id: uuid.UUID, role: MembershipRole, expected_version: int
) -> Membership | None:
    """Compare-and-set a membership role; None when the version does not match."""
    row = tx.conn.execute(
        "UPDATE app.membership SET role = %s, version = version + 1 "
        "WHERE tenant_id = %s AND user_id = %s AND version = %s RETURNING version",
        (MembershipRole(role).value, tx.tenant_id, user_id, expected_version),
    ).fetchone()
    return None if row is None else Membership(tx.tenant_id, user_id, role, row[0])


def set_local_credential(
    tx: TenantTx, user_id: uuid.UUID, login: str, verifier: str
) -> None:
    tx.conn.execute(
        "INSERT INTO app.local_credential (tenant_id, user_id, login, verifier) "
        "VALUES (%s, %s, %s, %s)",
        (tx.tenant_id, user_id, login, verifier),
    )


def find_credential(conn: Conn, login: str) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """(tenant, user, verifier) for a login, via the pre-tenant login policy."""
    if not conn.autocommit or conn.info.transaction_status != pq.TransactionStatus.IDLE:
        raise TenancyError("find_credential needs an idle autocommit connection")
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.tenant_id', '', true), "
            "set_config('app.login', %s, true)",
            (login,),
        )
        rows = conn.execute(
            "SELECT tenant_id, user_id, verifier FROM app.local_credential"
        ).fetchall()
    return (rows[0][0], rows[0][1], str(rows[0][2])) if len(rows) == 1 else None


def get_verifier(tx: TenantTx, user_id: uuid.UUID) -> str | None:
    row = tx.conn.execute(
        "SELECT verifier FROM app.local_credential WHERE tenant_id = %s "
        "AND user_id = %s",
        (tx.tenant_id, user_id),
    ).fetchone()
    return None if row is None else str(row[0])


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    user_id: uuid.UUID
    method: str
    route: str
    key: str


def claim_idempotency(
    tx: TenantTx, scope: IdempotencyScope
) -> tuple[bytes, int, bytes] | None:
    """Serialise requests with the same scope until this transaction ends, then
    return the stored (request hash, status, body), or None for a first use."""
    tx.conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("\x1f".join((str(tx.tenant_id), str(scope.user_id), scope.method,
                      scope.route, scope.key)),),
    )  # fmt: skip
    row = tx.conn.execute(
        "SELECT request_hash, status_code, response_body FROM app.idempotency_record "
        "WHERE tenant_id = %s AND user_id = %s AND method = %s AND route = %s "
        "AND idem_key = %s",
        (tx.tenant_id, scope.user_id, scope.method, scope.route, scope.key),
    ).fetchone()
    return None if row is None else (bytes(row[0]), int(row[1]), bytes(row[2]))


def save_idempotency(
    tx: TenantTx, scope: IdempotencyScope, request_hash: bytes, status: int, body: bytes
) -> None:
    tx.conn.execute(
        "INSERT INTO app.idempotency_record (tenant_id, user_id, method, route, "
        "idem_key, request_hash, status_code, response_body) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (tx.tenant_id, scope.user_id, scope.method, scope.route, scope.key,
         request_hash, status, body),
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class ThrottlePolicy:
    """max_attempts per window; then a lock of base * 2^n, capped at cap."""

    max_attempts: int
    window: timedelta
    base: timedelta
    cap: timedelta

    def __post_init__(self) -> None:
        zero, day = timedelta(0), timedelta(days=1)
        if not (self.max_attempts >= 1 and zero < self.window <= day
                and zero < self.base <= self.cap <= day):  # fmt: skip
            raise ValueError("throttle needs attempts >= 1, 0 < base <= cap <= 1 day")


def throttle_hit(
    conn: Conn, key_hash: bytes, policy: ThrottlePolicy
) -> tuple[datetime | None, bool]:
    """Count one attempt in its own autocommit statement, so a rolled-back request
    still counts. Returns (lock end or None, whether this call locked the key)."""
    row = conn.execute(
        "SELECT * FROM app.throttle_hit(%s, %s, %s, %s, %s)",
        (key_hash, policy.max_attempts, policy.window, policy.base, policy.cap),
    ).fetchone()
    assert row is not None
    return row[0], bool(row[1])


def throttle_clear(conn: Conn, key_hash: bytes) -> None:
    conn.execute("SELECT app.throttle_clear(%s)", (key_hash,))


def throttle_purge(conn: Conn) -> int:
    """Delete throttle rows quiet for a day; returns how many."""
    row = conn.execute("SELECT app.throttle_purge()").fetchone()
    return 0 if row is None else int(row[0])


def bootstrap_installation(
    conn: Conn, display_name: str, login: str, verifier: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Once per installation: the first tenant, its owner and local credential.
    Raises TenancyError when the installation was already bootstrapped (a racing
    second run waits on the marker row and is refused too)."""
    with provision_tenant(conn) as tx:
        marker = conn.execute(
            "INSERT INTO app.installation_bootstrap (tenant_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING RETURNING tenant_id",
            (tx.tenant_id,),
        ).fetchone()
        if marker is None:
            raise TenancyError("installation is already bootstrapped")
        user_id = create_user(tx, display_name)
        add_membership(tx, user_id, MembershipRole.TENANT_OWNER)
        set_local_credential(tx, user_id, login, verifier)
        append_audit_event(
            tx, "installation.bootstrapped", actor_user_id=user_id,
            target_id=tx.tenant_id,
        )  # fmt: skip
    return tx.tenant_id, user_id


@contextmanager
def runtime_connection(conninfo: str) -> Iterator[Conn]:
    """An autocommit connection that passed `check_runtime_role`."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        check_runtime_role(conn)
        yield conn


def check_runtime_role(conn: Conn) -> None:
    """Refuse a connection that could bypass row level security or is not a member
    of qw_app or qw_worker."""
    row = conn.execute(
        "SELECT r.rolsuper, r.rolbypassrls, pg_has_role(r.oid, 'qw_migrate', 'MEMBER'),"
        " pg_has_role(r.oid, 'qw_app', 'MEMBER') OR pg_has_role(r.oid, 'qw_worker',"
        " 'MEMBER') FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
    if row is None or row[0]:
        raise TenancyError("runtime connection is a superuser")
    if row[1] or row[2]:
        raise TenancyError("runtime connection can bypass RLS or owns the schema")
    if not row[3]:
        raise TenancyError("runtime connection is not a member of qw_app or qw_worker")
