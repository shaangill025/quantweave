"""T010 routes: liveness, session (login, me, logout, step-up) and memberships.

Path ids resolve inside the session tenant only, so another tenant's id is 404
(C-07, A-10). Bodies forbid unknown fields, so a client `tenant_id` is a 400.
If-Match accepts only the exact `"N"` form (no weak tags, lists or `*`): 412 on
mismatch, 428 when absent. C-03 Idempotency-Key is T010 increment 3.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from qw_adapters import tenancy
from qw_adapters.tenancy import Conn, MembershipRole, SessionPrincipal

from qw_api import security as sec
from qw_api.app import ApiError, Settings, correlation_id
from qw_api.security import Authenticator, Principal

router = APIRouter(prefix="/api/v1")
MAX_MEMBERS = 200
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
    next_cursor: None = None  # C-02 cursors are later work; the page is capped
    coverage: Coverage


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
    identity = authenticator.authenticate(conn, body.login, body.password)
    if identity is None:
        raise refused
    with tenancy.tenant_transaction(conn, identity.tenant_id) as tx:
        if tenancy.get_membership(tx, identity.user_id) is None:
            raise refused
        try:
            session_id, token = tenancy.create_session(
                tx, identity.user_id, settings.session_ttl
            )
        except tenancy.TenancyError:
            raise refused from None
        tenancy.append_audit_event(
            tx, "session.created", actor_user_id=identity.user_id,
            target_id=session_id, correlation_id=correlation_id(request),
        )  # fmt: skip
    session = tenancy.lookup_session(conn, token)
    if session is None:
        raise refused
    sec.set_session_cookie(response, token, settings.session_ttl)
    return _view(session, token)


@router.get("/session", response_model=SessionView)
def me(principal: Annotated[Principal, Depends(sec.current_principal)]) -> SessionView:
    return _view(principal.session, principal.token)


@router.delete("/session", status_code=204)
def logout(
    request: Request, principal: Annotated[Principal, Depends(sec.current_principal)]
) -> Response:
    with principal.tx() as tx:
        tenancy.revoke_session(tx, principal.session.session_id)
        tenancy.append_audit_event(
            tx, "session.revoked", actor_user_id=principal.session.user_id,
            target_id=principal.session.session_id,
            correlation_id=correlation_id(request),
        )  # fmt: skip
    response = Response(status_code=204)
    sec.set_session_cookie(response, "", timedelta(0))
    return response


@router.post("/session/step-up", response_model=SessionView)
def step_up(
    body: StepUp,
    request: Request,
    principal: Annotated[Principal, Depends(sec.current_principal)],
) -> SessionView:
    authenticator: Authenticator = request.app.state.authenticator
    session = principal.session
    with principal.tx() as tx:
        if not authenticator.reverify(tx, session.user_id, body.password):
            raise ApiError(403, "reauthentication_failed", "Re-authentication failed.")
        at = tenancy.mark_step_up(tx, session.session_id)
        if at is None:
            raise ApiError(401, "unauthenticated", "No valid session.")
        tenancy.append_audit_event(
            tx, "session.step_up", actor_user_id=session.user_id,
            target_id=session.session_id, correlation_id=correlation_id(request),
        )  # fmt: skip
    return _view(replace(session, step_up_at=at), principal.token)


@router.get("/memberships", response_model=MemberPage)
def list_memberships(
    principal: Annotated[
        Principal, Depends(sec.require_role(MembershipRole.TENANT_MEMBER))
    ],
) -> MemberPage:
    with principal.tx() as tx:
        rows = tenancy.list_memberships(tx, MAX_MEMBERS + 1)
    items = [
        MemberView(user_id=m.user_id, role=m.role.value, version=m.version,
                   display_name=name)
        for m, name in rows[:MAX_MEMBERS]
    ]  # fmt: skip
    partial = len(rows) > MAX_MEMBERS  # C-02 coverage: never a silent truncation
    coverage = Coverage(status="partial", reasons=["page_limit"]) if partial else None
    return MemberPage(
        items=items, coverage=coverage or Coverage(status="complete", reasons=[])
    )


@router.patch("/memberships/{user_id}", response_model=MemberView)
def change_role(
    user_id: uuid.UUID,
    body: RoleChange,
    request: Request,
    response: Response,
    principal: Annotated[
        Principal, Depends(sec.require_step_up(MembershipRole.TENANT_OWNER))
    ],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> MemberView:
    actor, new_role = principal.session.user_id, MembershipRole(body.role)
    if if_match is None:
        raise ApiError(428, "if_match_required", "Send If-Match with the version.")
    with principal.tx() as tx:
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
        tenancy.append_audit_event(
            tx, "membership.role_changed", actor_user_id=actor, target_id=user_id,
            correlation_id=correlation_id(request),
        )  # fmt: skip
    response.headers["ETag"] = f'"{updated.version}"'
    return MemberView(user_id=user_id, role=new_role.value, version=updated.version)
