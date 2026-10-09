"""T010 routes: liveness, session (login, me, logout, step-up) and memberships.

Path ids resolve inside the session tenant only, so another tenant's id is 404
(C-07, A-10). Bodies forbid unknown fields, so a client `tenant_id` is a 400.
If-Match accepts only the exact `"N"` form (no weak tags, lists or `*`): 412 on
mismatch, 428 when absent. Logout, step-up and role change require Idempotency-Key
(C-03); login is excluded by design (its response carries a fresh session token).
Login and step-up pass the per-account and per-address throttle first.
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qw_adapters import tenancy
from qw_adapters.tenancy import Conn, MembershipRole, SessionPrincipal, TenantTx

from qw_api import security as sec
from qw_api.app import ApiError, Settings, correlation_id
from qw_api.security import Authenticator, Principal

router = APIRouter(prefix="/api/v1")
MAX_MEMBERS = 200  # C-02 page size: default 50, maximum 200
Role = Literal["tenant_owner", "tenant_member"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StepUp(Body):
    password: str = Field(min_length=1, max_length=1024)


class Login(StepUp):
    login: str = Field(pattern=r"^[a-z0-9][a-z0-9._@-]{0,127}$")


class RoleChange(Body):
    role: Role


class SessionView(BaseModel):
    user_id: uuid.UUID
    role: Role
    expires_at: datetime
    step_up_at: datetime | None
    csrf_token: str


class MemberView(BaseModel):
    user_id: uuid.UUID
    role: Role
    version: int
    display_name: str | None = None


class Coverage(BaseModel):
    status: Literal["complete", "partial"]
    reasons: list[str]


class MemberPage(BaseModel):
    items: list[MemberView]
    next_cursor: str | None  # C-02: opaque, HMAC-bound, expires after an hour
    coverage: Coverage


class StepUpView(BaseModel):
    step_up_at: datetime


class Liveness(BaseModel):
    status: Literal["healthy"]


def _view(s: SessionPrincipal, token: str) -> SessionView:
    return SessionView(
        user_id=s.user_id, role=s.role.value, expires_at=s.expires_at.astimezone(UTC),
        step_up_at=s.step_up_at and s.step_up_at.astimezone(UTC),
        csrf_token=sec.csrf_token(token),
    )  # fmt: skip


@router.head("/health/live", response_model=Liveness, operation_id="headLive")
@router.get("/health/live", response_model=Liveness, operation_id="readLive")
def live() -> Liveness:
    """Unauthenticated liveness only: no version, component or dependency detail."""
    return Liveness(status="healthy")


@router.post("/session", status_code=201, response_model=SessionView)
def login(
    body: Login,
    request: Request,
    response: Response,
    conn: Annotated[Conn, Depends(sec.connection)],
) -> SessionView:
    settings: Settings = request.app.state.settings
    authenticator: Authenticator = request.app.state.authenticator
    refused = ApiError(401, "invalid_credentials", "Login failed.")
    account = sec.mac(settings, "login-account", body.login)
    address = sec.mac(settings, "login-address", sec.client_address(request, settings))
    wait, newly = sec.throttle(
        conn, settings,
        [(address, settings.address_throttle), (account, settings.account_throttle)],
    )  # fmt: skip
    if newly[1] and (found := tenancy.find_credential(conn, body.login)):
        with tenancy.tenant_transaction(conn, found[0]) as tx:
            _audit(tx, request, "auth.locked_out", None, found[1])
    if any(newly):
        sec.log.warning("login throttle locked cid=%s", correlation_id(request))
    if wait is not None:  # same response whether or not the login exists
        raise sec.too_many(wait)
    identity = authenticator.authenticate(conn, body.login, body.password)
    if identity is None:
        raise refused
    tenancy.throttle_clear(conn, account)
    with tenancy.tenant_transaction(conn, identity.tenant_id) as tx:
        if tenancy.get_membership(tx, identity.user_id) is None:
            raise refused
        try:
            session_id, token = tenancy.create_session(
                tx, identity.user_id, settings.session_ttl
            )
        except tenancy.TenancyError:
            raise refused from None
        _audit(tx, request, "session.created", identity.user_id, session_id)
    session = tenancy.lookup_session(conn, token, settings.idle_timeout)
    if session is None:
        raise refused
    sec.set_session_cookie(response, token, settings.session_ttl)
    return _view(session, token)


@router.get("/session", response_model=SessionView)
def me(principal: Annotated[Principal, Depends(sec.current_principal)]) -> SessionView:
    return _view(principal.session, principal.token)


def _audit(
    tx: TenantTx, request: Request, action: str,
    actor: uuid.UUID | None, target: uuid.UUID,
) -> None:  # fmt: skip
    tenancy.append_audit_event(
        tx, action, actor_user_id=actor, target_id=target,
        correlation_id=correlation_id(request),
    )  # fmt: skip


IdemKey = Annotated[str, Header(alias="Idempotency-Key")]


@router.delete("/session", status_code=204)
def logout(
    request: Request,
    principal: Annotated[Principal, Depends(sec.current_principal)],
    key: IdemKey,
) -> Response:
    session = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        tenancy.revoke_session(tx, session.session_id)
        _audit(tx, request, "session.revoked", session.user_id, session.session_id)
        return 204, b""

    bound = {"session": str(session.session_id)}  # reuse from another session: 409
    response = sec.idempotent(principal, request, key, bound, run)
    sec.set_session_cookie(response, "", timedelta(0))
    return response


@router.post("/session/step-up", response_model=StepUpView)
def step_up(
    body: StepUp,
    request: Request,
    principal: Annotated[Principal, Depends(sec.current_principal)],
    key: IdemKey,
) -> Response:
    """The request HMAC binds the session but excludes the password: a same-key
    retry on the same session replays the recorded step_up_at without verifying
    again and without changing state; another session's reuse is 409."""
    sec.check_idempotency_key(key)  # before anything is counted
    settings: Settings = request.app.state.settings
    authenticator: Authenticator = request.app.state.authenticator
    session = principal.session
    account = sec.mac(settings, "step-up-account", str(session.user_id))
    address = sec.mac(
        settings, "step-up-address", sec.client_address(request, settings)
    )
    wait, newly = sec.throttle(
        principal.conn, settings,
        [(address, settings.address_throttle), (account, settings.account_throttle)],
    )  # fmt: skip
    if newly[1]:
        with principal.tx() as tx:
            _audit(tx, request, "auth.locked_out", session.user_id, session.user_id)
    if wait is not None:
        raise sec.too_many(wait)

    def run(tx: TenantTx) -> tuple[int, bytes]:
        if not authenticator.reverify(tx, session.user_id, body.password):
            raise ApiError(403, "reauthentication_failed", "Re-authentication failed.")
        at = tenancy.mark_step_up(tx, session.session_id)
        if at is None:
            raise ApiError(401, "unauthenticated", "No valid session.")
        _audit(tx, request, "session.step_up", session.user_id, session.session_id)
        return 200, StepUpView(step_up_at=at.astimezone(UTC)).model_dump_json().encode()

    bound = {"session": str(session.session_id)}
    response = sec.idempotent(principal, request, key, bound, run)
    if response.status_code == 200 and "idempotent-replayed" not in response.headers:
        tenancy.throttle_clear(principal.conn, account)
    return response


CURSOR_TTL = 3600  # seconds


def _cursor_mac(settings: Settings, p: Principal, route: str, body: bytes) -> bytes:
    # C-02: bound to tenant, principal, route, sort order and the server key.
    s = p.session
    parts = (str(s.tenant_id), str(s.user_id), route, "user_id", body.hex())
    return sec.mac(settings, "cursor", *parts)[:16]


def _read_cursor(settings: Settings, p: Principal, route: str, raw: str) -> uuid.UUID:
    invalid = ApiError(400, "invalid_cursor", "The cursor is not valid here.")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError:
        raise invalid from None
    body, tag = data[:24], data[24:]
    if len(data) != 40 or not hmac.compare_digest(
        tag, _cursor_mac(settings, p, route, body)
    ):
        raise invalid
    if int.from_bytes(body[16:]) < int(datetime.now(UTC).timestamp()):
        raise invalid  # expired
    return uuid.UUID(bytes=body[:16])


@router.get("/memberships", response_model=MemberPage)
def list_memberships(
    request: Request,
    principal: Annotated[
        Principal, Depends(sec.require_role(MembershipRole.TENANT_MEMBER))
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_MEMBERS)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> MemberPage:
    settings: Settings = request.app.state.settings
    route = request.url.path
    after = _read_cursor(settings, principal, route, cursor) if cursor else None
    with principal.tx() as tx:
        rows = tenancy.list_memberships(tx, limit + 1, after)
    items = [
        MemberView(user_id=m.user_id, role=m.role.value, version=m.version,
                   display_name=name)
        for m, name in rows[:limit]
    ]  # fmt: skip
    next_cursor = None
    if len(rows) > limit:
        expires = int(datetime.now(UTC).timestamp()) + CURSOR_TTL
        body = items[-1].user_id.bytes + expires.to_bytes(8)
        tag = _cursor_mac(settings, principal, route, body)
        next_cursor = base64.urlsafe_b64encode(body + tag).decode().rstrip("=")
    return MemberPage(
        items=items, next_cursor=next_cursor,
        coverage=Coverage(status="complete", reasons=[]),
    )  # fmt: skip


@router.patch("/memberships/{user_id}", response_model=MemberView)
def change_role(
    user_id: uuid.UUID,
    body: RoleChange,
    request: Request,
    principal: Annotated[
        Principal, Depends(sec.require_step_up(MembershipRole.TENANT_OWNER))
    ],
    key: IdemKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    actor, new_role = principal.session.user_id, MembershipRole(body.role)

    def run(tx: TenantTx) -> tuple[int, bytes]:
        if if_match is None:
            raise ApiError(428, "if_match_required", "Send If-Match with the version.")
        owners = tenancy.lock_owners(tx)
        if actor not in owners:  # demoted since the session was read
            raise ApiError(403, "forbidden", "Requires the tenant_owner role.")
        target = tenancy.get_membership(tx, user_id)
        if target is None:
            raise ApiError(404, "not_found", "No such membership.")
        if if_match != f'"{target.version}"':
            raise ApiError(412, "version_mismatch", "The membership has changed.")
        if user_id == actor and sec.RANK[new_role] > sec.RANK[target.role]:
            raise ApiError(403, "self_promotion", "Another owner must do this.")
        if new_role is not MembershipRole.TENANT_OWNER and owners == {user_id}:
            raise ApiError(409, "last_owner", "A tenant keeps at least one owner.")
        updated = tenancy.set_member_role(tx, user_id, new_role, target.version)
        if updated is None:
            raise ApiError(412, "version_mismatch", "The membership has changed.")
        _audit(tx, request, "membership.role_changed", actor, user_id)
        view = MemberView(user_id=user_id, role=new_role.value, version=updated.version)
        return 200, view.model_dump_json().encode()

    payload = {"if_match": if_match, "body": body.model_dump(mode="json")}
    response = sec.idempotent(principal, request, key, payload, run)
    response.headers["ETag"] = f'"{json.loads(bytes(response.body))["version"]}"'
    return response
