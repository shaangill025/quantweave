"""Authentication, sessions, CSRF and membership authorization (T010; spec 12).

The qw_adapters.tenancy session token lives only in the `__Host-pi_session` cookie
(HttpOnly, Secure, SameSite=Strict, Path=/, no Domain); tenant and principal come
only from `lookup_session`. CSRF: besides the Origin allowlist (app.py), a
session's writes must send `X-CSRF-Token` = HMAC-SHA256(session token,
"qw-csrf-v1"), a synchronizer token computed instead of stored; other origins
cannot read the HttpOnly cookie, so they cannot derive it. `Authenticator` is the
issuance seam; `LocalPasswordAuthenticator` (self-hosted) uses argon2id via
argon2-cffi (RFC 9106 low-memory profile by default) and verifies a dummy hash
for unknown logins.
"""

import hashlib
import hmac
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request, Response
from qw_adapters import tenancy
from qw_adapters.tenancy import Conn, MembershipRole, SessionPrincipal, TenantTx

from qw_api.app import SAFE_METHODS, ApiError, Settings

SESSION_COOKIE = "__Host-pi_session"
CSRF_HEADER = "X-CSRF-Token"
RANK = {MembershipRole.TENANT_MEMBER: 1, MembershipRole.TENANT_OWNER: 2}


@dataclass(frozen=True, slots=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


class Authenticator(Protocol):
    def authenticate(self, conn: Conn, login: str, secret: str) -> Identity | None:
        """The identity for valid credentials, else None (no reason given)."""
        ...

    def reverify(self, tx: TenantTx, user_id: uuid.UUID, secret: str) -> bool:
        """Re-check the credential of a user of `tx`'s tenant (step-up)."""
        ...


class LocalPasswordAuthenticator:
    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self.hasher = hasher or PasswordHasher()
        self._dummy = self.hasher.hash(uuid.uuid4().hex)

    def enroll(self, tx: TenantTx, user_id: uuid.UUID, login: str, secret: str) -> None:
        tenancy.set_local_credential(tx, user_id, login, self.hasher.hash(secret))

    def _verify(self, verifier: str | None, secret: str) -> bool:
        # An unknown user still costs one verification, against a dummy hash.
        try:
            ok = self.hasher.verify(verifier or self._dummy, secret)
        except (VerificationError, InvalidHashError):
            return False
        return ok and verifier is not None

    def authenticate(self, conn: Conn, login: str, secret: str) -> Identity | None:
        found = tenancy.find_credential(conn, login)
        if not self._verify(found[2] if found else None, secret) or found is None:
            return None
        return Identity(found[0], found[1])

    def reverify(self, tx: TenantTx, user_id: uuid.UUID, secret: str) -> bool:
        return self._verify(tenancy.get_verifier(tx, user_id), secret)


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"qw-csrf-v1", hashlib.sha256).hexdigest()


def set_session_cookie(response: Response, token: str, max_age: timedelta) -> None:
    """Set the cookie; an empty token with a zero max_age clears it."""
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(max_age.total_seconds()), path="/",
        secure=True, httponly=True, samesite="strict",
    )  # fmt: skip


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def connection(request: Request) -> Iterator[Conn]:
    with request.app.state.connections() as conn:
        yield conn


@dataclass(frozen=True, slots=True)
class Principal:
    session: SessionPrincipal
    token: str
    conn: Conn

    def tx(self) -> AbstractContextManager[TenantTx]:
        return tenancy.tenant_transaction(self.conn, self.session.tenant_id)


def current_principal(
    request: Request, conn: Annotated[Conn, Depends(connection)]
) -> Principal:
    token = request.cookies.get(SESSION_COOKIE, "")
    session = tenancy.lookup_session(conn, token) if token else None
    if session is None:
        raise ApiError(401, "unauthenticated", "No valid session.")
    if request.method not in SAFE_METHODS and not hmac.compare_digest(
        request.headers.get(CSRF_HEADER, "").encode(), csrf_token(token).encode()
    ):
        raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token.")
    return Principal(session, token, conn)


def require_role(role: MembershipRole) -> Callable[[Principal], Principal]:
    def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if RANK[principal.session.role] < RANK[role]:
            raise ApiError(403, "forbidden", f"Requires the {role.value} role.")
        return principal

    return dependency


def require_step_up(role: MembershipRole) -> Callable[..., Principal]:
    """`require_role` plus a re-authentication on this session within
    `Settings.step_up_window`; otherwise 401 step_up_required (RFC 9470 style)."""

    def dependency(
        principal: Annotated[Principal, Depends(require_role(role))],
        settings: Annotated[Settings, Depends(settings_of)],
    ) -> Principal:
        at = principal.session.step_up_at
        if at is None or datetime.now(UTC) - at > settings.step_up_window:
            raise ApiError(401, "step_up_required", "Re-authenticate to continue.")
        return principal

    return dependency
