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
    session_id, token = uuid.uuid4(), new_session_token()
    tx.conn.execute(
        "INSERT INTO app.session (id, tenant_id, user_id, token_hash, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + %s)",
        (session_id, tx.tenant_id, user_id, session_token_hash(token), ttl),
    )
    return session_id, token


def lookup_session(conn: Conn, token: str) -> SessionPrincipal | None:
    """The live session and membership for a presented token, or None when the token
    is malformed, unknown, expired or revoked, or the membership is gone."""
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
            "FROM app.session WHERE expires_at > now() AND revoked_at IS NULL"
        ).fetchall()
        if len(found) != 1:
            return None
        session_id, tenant_id, user_id, created_at, expires_at, step_up_at = found[0]
        conn.execute(
            "SELECT set_config('app.session_token_hash', '', true), "
            "set_config('app.tenant_id', %s, true)",
            (str(tenant_id),),
        )
        membership = get_membership(TenantTx(conn, tenant_id), user_id)
    if membership is None:
        return None
    return SessionPrincipal(
        session_id, tenant_id, user_id, membership.role, created_at, expires_at,
        step_up_at,
    )  # fmt: skip


def revoke_session(tx: TenantTx, session_id: uuid.UUID) -> bool:
    """Revoke a live session of this tenant; False if absent or already revoked."""
    cur = tx.conn.execute(
        "UPDATE app.session SET revoked_at = now() "
        "WHERE tenant_id = %s AND id = %s AND revoked_at IS NULL",
        (tx.tenant_id, session_id),
    )
    return cur.rowcount == 1


def check_runtime_role(conn: Conn) -> None:
    """Refuse a connection whose role could bypass row level security."""
    row = conn.execute(
        "SELECT r.rolsuper, r.rolbypassrls, pg_has_role(r.oid, 'qw_migrate', 'MEMBER')"
        " FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
    ).fetchone()
    if row is None or row[0]:
        raise TenancyError("runtime connection is a superuser")
    if row[1] or row[2]:
        raise TenancyError("runtime connection can bypass RLS or owns the schema")
