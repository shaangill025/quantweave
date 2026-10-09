"""Authentication, sessions, CSRF and membership authorization (T010; spec 12).

The qw_adapters.tenancy session token lives only in the `__Host-pi_session` cookie
(HttpOnly, Secure, SameSite=Strict, Path=/, no Domain); tenant and principal come
only from `lookup_session`. CSRF: besides the Origin allowlist (app.py), a
session's writes must send `X-CSRF-Token` = HMAC-SHA256(session token,
"qw-csrf-v1"), a synchronizer token computed instead of stored; other origins
cannot read the HttpOnly cookie, so they cannot derive it. `Authenticator` is the
issuance seam; `LocalPasswordAuthenticator` (self-hosted) uses argon2id via
argon2-cffi (RFC 9106 low-memory profile by default) and verifies a dummy hash
for unknown logins. Credential attempts pass a persistent throttle (`throttle`);
mutating commands run through `idempotent` (C-03).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import time
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
_IDEM_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
log = logging.getLogger("qw_api")
_last_purge = [0]


@dataclass(frozen=True, slots=True)
class Identity:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


class Authenticator(Protocol):
    def authenticate(self, conn: Conn, login: str, secret: str) -> Identity | None: ...

    def reverify(self, tx: TenantTx, user_id: uuid.UUID, secret: str) -> bool: ...


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


def _canonical_ip(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return text.strip()


def client_address(request: Request, settings: Settings) -> str:
    """The socket peer, or, when the peer is in a configured trusted-proxy network,
    the rightmost X-Forwarded-For hop that is not itself in one (addresses are
    canonicalised with `ipaddress`; CIDR networks are allowed)."""
    nets = [ipaddress.ip_network(p) for p in settings.trusted_proxies]

    def trusted(addr: str) -> bool:
        try:
            return any(ipaddress.ip_address(addr) in net for net in nets)
        except ValueError:
            return False

    peer = _canonical_ip(request.client.host if request.client else "")
    if not trusted(peer):
        return peer
    hops = [_canonical_ip(h) for v in request.headers.getlist("x-forwarded-for")
            for h in v.split(",")]  # fmt: skip
    return next((h for h in reversed(hops) if not trusted(h)), peer)


def mac(settings: Settings, *parts: str) -> bytes:
    data = "\x1f".join(parts).encode()
    return hmac.new(settings.secret_key, data, hashlib.sha256).digest()


def throttle(
    conn: Conn, settings: Settings, hits: list[tuple[bytes, tenancy.ThrottlePolicy]]
) -> tuple[int | None, list[bool]]:
    """Count one attempt per key in order (outside any request transaction, so a
    failed attempt is never rolled back), stopping at the first locked key: callers
    pass the address key first, so a locked address creates no account rows.
    Returns the seconds until the latest lock ends (None when not locked) and, per
    key, whether this call locked it. Quiet rows are purged at most once a minute
    per process."""
    if time.monotonic_ns() - _last_purge[0] > 60 * 10**9:
        _last_purge[0] = time.monotonic_ns()
        tenancy.throttle_purge(conn)
    results: list[tuple[datetime | None, bool]] = []
    for key, policy in hits:
        results.append(tenancy.throttle_hit(conn, key, policy))
        if results[-1][0] is not None:
            break
    results += [(None, False)] * (len(hits) - len(results))
    ends = [until for until, _ in results if until is not None]
    if not ends:
        return None, [new for _, new in results]
    now = conn.execute("SELECT now()").fetchone()
    assert now is not None
    wait = max(1, math.ceil((max(ends) - now[0]).total_seconds()))
    return wait, [new for _, new in results]


def check_idempotency_key(key: str) -> None:
    if not _IDEM_KEY.fullmatch(key):
        raise ApiError(400, "invalid_idempotency_key", "Use 16-128 of [A-Za-z0-9_-].")


def too_many(wait: int) -> ApiError:
    return ApiError(429, "too_many_attempts", "Try again later.", True, (), wait)


def idempotent(
    principal: "Principal", request: Request, key: str, payload: object,
    run: Callable[[TenantTx], tuple[int, bytes]],
) -> Response:  # fmt: skip
    """C-03: replay the stored 2xx response for the same (tenant, principal, method,
    route, key) and payload HMAC; 409 for another payload. `run` executes in the
    same transaction as the record, so an error stores nothing. Session-scoped
    commands put the session id in `payload`, so reuse from another session is 409."""
    check_idempotency_key(key)
    settings, route = settings_of(request), request.url.path
    scope = tenancy.IdempotencyScope(
        principal.session.user_id, request.method, route, key
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = mac(settings, "idempotency", request.method, route, canonical)
    with principal.tx() as tx:
        stored = tenancy.claim_idempotency(tx, scope)
        if stored is not None:
            if not hmac.compare_digest(stored[0], digest):
                raise ApiError(409, "idempotency_key_reused", "Key used elsewhere.")
            replay = Response(stored[2], stored[1], media_type="application/json")
            replay.headers["Idempotent-Replayed"] = "true"
            return replay
        status, body = run(tx)
        tenancy.save_idempotency(tx, scope, digest, status, body)
    return Response(body, status, media_type="application/json")


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
    token, settings = request.cookies.get(SESSION_COOKIE, ""), settings_of(request)
    session = (
        tenancy.lookup_session(conn, token, settings.idle_timeout) if token else None
    )
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
    """`require_role` plus a step-up within the window, else 401 (RFC 9470 style)."""

    def dependency(
        principal: Annotated[Principal, Depends(require_role(role))],
        settings: Annotated[Settings, Depends(settings_of)],
    ) -> Principal:
        at = principal.session.step_up_at
        if at is None or datetime.now(UTC) - at > settings.step_up_window:
            raise ApiError(401, "step_up_required", "Re-authenticate to continue.")
        return principal

    return dependency
