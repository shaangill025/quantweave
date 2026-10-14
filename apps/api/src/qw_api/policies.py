"""T019 policy routes: `GET /policies` (C-18), `GET /policies/{policy_id}`,
`POST /policies/{policy_id}/adopt`, and (T020) `/pause` and `/resume`, over
qw_adapters.policy_store and pause_store.

Ids resolve inside the session tenant only: another tenant's policy and an unknown
id get the same 404. Reads need membership; adoption needs the tenant_owner role, a
step-up within `Settings.step_up_window` that the store verifies against the
principal's own session row (and 0009 re-checks), CSRF, Idempotency-Key (C-03) and
If-Match `"N"` on the latest version (T010 form; 428 absent, 412 stale). Body
revision fields name the same resource as If-Match, so a disagreement is 400 (C-04).
`acknowledged_conflicts` extends the spec `AdoptVersion` body: conflicts are
acknowledged by code, and the acknowledgement text never authorises (C-20).
Bodies are `policy.schema.json`; reference ids for freshness and loss-pause policy
name the spec config versions, since no such policy records exist yet. While the
policy has an active pause, `status` is `paused` and the ETag `"N-<digest>"`.
Pause (C-04, no If-Match): any member; `expected_revision` is never compared.
Resume: tenant_owner, step-up, If-Match = the current ETag (so an unseen pause
refuses it). Text with control characters is 400; failed verification of stored
records is 409 `integrity`. Neither route reaches a broker.
"""

from __future__ import annotations

import base64
import json
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field
from qw_adapters import pause_store as pauses
from qw_adapters import policy_store as store
from qw_adapters.tenancy import MembershipRole, TenantTx
from qw_domain.instants import format_instant
from qw_domain.policy import ConflictCode, PolicyError, PolicyHistory
from qw_domain.risk_pauses import PauseError

from qw_api import routes
from qw_api import security as sec
from qw_api.app import ApiError, FieldError
from qw_api.security import Principal

router = APIRouter(prefix="/api/v1")
FRESHNESS_POLICY = "feed_and_freshness_policy:0.1.0-design"
LOSS_PAUSE_POLICY = "risk_policy_draft:0.1.0-template"
Reader = Annotated[Principal, Depends(sec.require_role(MembershipRole.TENANT_MEMBER))]
Adopter = Annotated[
    Principal, Depends(sec.require_step_up(MembershipRole.TENANT_OWNER))
]
Pauser = Annotated[Principal, Depends(sec.require_role(MembershipRole.TENANT_MEMBER))]
_STATUS = {"not_found": (404, "No such policy."),
           "forbidden": (403, "Requires the tenant_owner role."),
           "not_paused": (409, "The policy has no active pause."),
           "reconciled_state_required": (409, "A pause needs reconciled state."),
           "step_up_required": (401, "Re-authenticate to continue."),
           "stale": (412, "The policy has changed."),
           "not_latest": (412, "The policy has changed."),
           "already_adopted": (409, "This version is already adopted.")}  # fmt: skip


class AdoptVersion(routes.Body):
    version_id: str = Field(pattern=r"^[1-9][0-9]{0,8}$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1, lt=2**31)
    acknowledgement: str = Field(min_length=1, max_length=2000)
    acknowledged_conflicts: list[str] = Field(default_factory=list, max_length=16)


class RevisionAction(routes.Body):
    expected_revision: int = Field(ge=1, lt=2**31)
    acknowledgement: str = Field(min_length=1, max_length=2000)
    reason: str = Field(max_length=2000)


def view(history: PolicyHistory, paused: bool = False) -> dict[str, Any]:
    """The latest version; status derived by the domain, or `paused`."""
    v = history.versions[-1]
    d, adopted = v.draft, history.adopted
    receipt = adopted[1] if adopted and adopted[0].version == v.version else None
    fields = {f.name: f.value for f in d.fields}
    limits = [
        {"metric": x.metric.value, "value": x.value.to_wire(),
         "unit": "ratio" if x.unit == "ratio" else x.scope.currency,
         "denominator": x.denominator,
         "scope_id": ":".join(p for p in (x.scope.account_id, x.scope.currency,
                                          x.scope.sleeve_id) if p)}
        for x in d.limits
    ]  # fmt: skip
    return {
        "id": history.policy_id, "version": v.version, "tenant_id": history.tenant_id,
        "scope_account_ids": pauses.policy_accounts(history),
        "status": "paused" if paused else history.status(v.version),
        "goal": ",".join(fields.get("objectives", ())),
        "horizons": list(fields.get("horizons", ())),
        "permitted_catalogue_ids": [], "numerical_limits": limits,
        "adopted_strategy_version_ids": [],
        "data_freshness_policy_id": FRESHNESS_POLICY,
        "loss_pause_policy_id": LOSS_PAUSE_POLICY,
        "accepted_by": receipt.principal_id if receipt else None,
        "accepted_at": format_instant(receipt.signed_at) if receipt else None,
        "content_hash": v.content_hash,
    }  # fmt: skip


def _version(if_match: str) -> str:
    return if_match.strip('"').split("-")[0]  # `"N"` or `"N-<pause digest>"`


def _load(tx: TenantTx, policy_id: uuid.UUID) -> PolicyHistory:
    history = store.load(tx, policy_id)
    if history is None:
        raise ApiError(404, "not_found", _STATUS["not_found"][1])
    return history


@router.get("/policies")
def list_policies(
    request: Request,
    principal: Reader,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
) -> Response:
    settings, route = sec.settings_of(request), request.url.path
    after = routes._read_cursor(settings, principal, route, cursor) if cursor else None
    with principal.tx() as tx:
        ids = store.list_policy_ids(tx, limit + 1, after)
        held = pauses.load(tx)
        items = [view(_load(tx, i), bool(held.active_for(i))) for i in ids[:limit]]
    next_cursor = None
    if len(ids) > limit:
        expires = int(datetime.now(UTC).timestamp()) + routes.CURSOR_TTL
        body = ids[limit - 1].bytes + expires.to_bytes(8)
        tag = routes._cursor_mac(settings, principal, route, body)
        next_cursor = base64.urlsafe_b64encode(body + tag).decode().rstrip("=")
    page = {"items": items, "next_cursor": next_cursor,
            "coverage": {"status": "complete", "reasons": []}}  # fmt: skip
    return Response(json.dumps(page).encode(), 200, media_type="application/json")


@router.get("/policies/{policy_id}")
def read_policy(policy_id: uuid.UUID, principal: Reader) -> Response:
    with principal.tx() as tx:
        history = _load(tx, policy_id)
        active = pauses.load(tx).active_for(policy_id)
    body = json.dumps(view(history, bool(active))).encode()
    response = Response(body, 200, media_type="application/json")
    response.headers["ETag"] = pauses.tag(history.versions[-1].version, active)
    return response


@router.post("/policies/{policy_id}/adopt")
def adopt_policy(
    policy_id: uuid.UUID,
    body: AdoptVersion,
    request: Request,
    principal: Adopter,
    key: routes.IdemKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    session, window = principal.session, sec.settings_of(request).step_up_window
    if len(body.acknowledgement.encode()) > 4096:  # the 0009 CHECK counts bytes
        error = FieldError("/acknowledgement", "too_long", "At most 4096 UTF-8 bytes.")
        raise ApiError(400, "invalid_request", "Too long.", False, (error,))
    known = {c.value for c in ConflictCode}
    if bad := [n for n, c in enumerate(body.acknowledged_conflicts) if c not in known]:
        errors = tuple(FieldError(f"/acknowledged_conflicts/{n}", "enum",
                                  "Unknown conflict code.") for n in bad)  # fmt: skip
        raise ApiError(400, "invalid_request", "Unknown conflict code.", False, errors)

    def run(tx: TenantTx) -> tuple[int, bytes]:
        if if_match is None:
            raise ApiError(428, "if_match_required", "Send If-Match with the version.")
        revision = body.expected_revision
        if _version(if_match) != str(revision) or body.version_id != str(revision):
            raise ApiError(400, "revision_mismatch", "If-Match and body disagree.")
        try:
            history = store.adopt(
                tx, policy_id, revision, body.content_hash,
                principal=session.user_id, session_id=session.session_id,
                window=window, text=body.acknowledgement,
                acknowledged=frozenset(map(ConflictCode, body.acknowledged_conflicts)),
            )  # fmt: skip
        except (store.PolicyStoreError, PolicyError) as exc:
            if isinstance(exc, store.PolicyStoreError) and exc.code not in _STATUS:
                raise  # integrity: a crash (500), never a client answer
            if exc.code in _STATUS:
                status, detail = _STATUS[exc.code]
                code = "version_mismatch" if status == 412 else exc.code
                raise ApiError(status, code, detail) from None
            raise ApiError(422, exc.code, str(exc)) from None
        routes._audit(tx, request, "policy.adopted", session.user_id, policy_id)
        return 200, json.dumps(view(history)).encode()

    payload = {"if_match": if_match, "body": body.model_dump(mode="json")}
    return _respond(principal, policy_id, sec.idempotent(principal, request, key,
                                                         payload, run))  # fmt: skip


def _pause_errors(exc: Exception) -> ApiError:
    code = getattr(exc, "code", "")
    if isinstance(exc, PauseError) and code not in _STATUS:
        return ApiError(422, code, str(exc))
    if code == "integrity":  # stored data failed verification: typed, not a 500
        return ApiError(409, code, "Stored policy or pause records failed checks.")
    if code not in _STATUS:
        raise exc
    status, detail = _STATUS[code]
    return ApiError(status, "version_mismatch" if status == 412 else code, detail)


def _text(body: RevisionAction) -> None:
    for name in ("acknowledgement", "reason"):  # the 0016 CHECKs count bytes
        text = getattr(body, name)
        if len(text.encode()) > 4096 or any(unicodedata.category(c) == "Cc"
                                            for c in text):  # fmt: skip
            error = FieldError(f"/{name}", "invalid", "Control characters or over "
                               "4096 UTF-8 bytes.")  # fmt: skip
            raise ApiError(400, "invalid_request", "Invalid text.", False, (error,))


def _respond(principal: Principal, policy_id: uuid.UUID, r: Response) -> Response:
    """A success, replays too, carries the current ETag (`pause_store.tag`)."""
    if r.status_code == 200:
        with principal.tx() as tx:
            version = _load(tx, policy_id).versions[-1].version
            r.headers["ETag"] = pauses.tag(version, pauses.load(tx).active_for(
                policy_id))  # fmt: skip
    return r


@router.post("/policies/{policy_id}/pause")
def pause_policy(
    policy_id: uuid.UUID, body: RevisionAction, request: Request, principal: Pauser,
    key: routes.IdemKey,
) -> Response:  # fmt: skip
    _text(body)
    user = principal.session.user_id

    def run(tx: TenantTx) -> tuple[int, bytes]:
        try:
            held = pauses.pause(tx, policy_id, principal=user, reason=body.reason,
                                text=body.acknowledgement)  # fmt: skip
        except (pauses.PauseStoreError, store.PolicyStoreError) as exc:
            raise _pause_errors(exc) from None
        routes._audit(tx, request, "policy.paused", user, policy_id)
        history = _load(tx, policy_id)
        return 200, json.dumps(view(history, bool(held.active_for(policy_id)))).encode()

    payload = {"body": body.model_dump(mode="json")}
    response = sec.idempotent(principal, request, key, payload, run)
    return _respond(principal, policy_id, response)


@router.post("/policies/{policy_id}/resume")
def resume_policy(
    policy_id: uuid.UUID, body: RevisionAction, request: Request, principal: Adopter,
    key: routes.IdemKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:  # fmt: skip
    _text(body)
    session, window = principal.session, sec.settings_of(request).step_up_window

    def run(tx: TenantTx) -> tuple[int, bytes]:
        if if_match is None:
            raise ApiError(428, "if_match_required", "Send If-Match with the version.")
        if _version(if_match) != str(body.expected_revision):
            raise ApiError(400, "revision_mismatch", "If-Match and body disagree.")
        try:
            held = pauses.resume(
                tx, policy_id, if_match, principal=session.user_id,
                session_id=session.session_id, window=window, reason=body.reason,
                text=body.acknowledgement,
            )  # fmt: skip
        except (pauses.PauseStoreError, PauseError, store.PolicyStoreError) as exc:
            raise _pause_errors(exc) from None
        routes._audit(tx, request, "policy.resumed", session.user_id, policy_id)
        history = _load(tx, policy_id)
        return 200, json.dumps(view(history, bool(held.active_for(policy_id)))).encode()

    payload = {"if_match": if_match, "body": body.model_dump(mode="json")}
    response = sec.idempotent(principal, request, key, payload, run)
    return _respond(principal, policy_id, response)
