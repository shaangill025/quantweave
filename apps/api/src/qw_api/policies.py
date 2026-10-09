"""T019 policy routes: `GET /policies` (C-18), `GET /policies/{policy_id}` and
`POST /policies/{policy_id}/adopt`, over qw_adapters.policy_store.

Ids resolve inside the session tenant only: another tenant's policy and an unknown
id get the same 404. Reads need membership; adoption needs the tenant_owner role, a
step-up within `Settings.step_up_window` that the store verifies against the
principal's own session row (and 0009 re-checks), CSRF, Idempotency-Key (C-03) and
If-Match `"N"` on the latest version (T010 form; 428 absent, 412 stale). Body
revision fields name the same resource as If-Match, so a disagreement is 400 (C-04).
`acknowledged_conflicts` extends the spec `AdoptVersion` body: conflicts are
acknowledged by code, and the acknowledgement text never authorises (C-20).
Bodies are `policy.schema.json`; reference ids for freshness and loss-pause policy
name the spec config versions, since no such policy records exist yet.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field
from qw_adapters import policy_store as store
from qw_adapters.tenancy import MembershipRole, TenantTx
from qw_domain.instants import format_instant
from qw_domain.policy import ConflictCode, PolicyError, PolicyHistory

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
_STATUS = {"not_found": (404, "No such policy."),
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


def view(history: PolicyHistory) -> dict[str, Any]:
    """The latest version; status derived by the domain."""
    v = history.versions[-1]
    d, adopted = v.draft, history.adopted
    receipt = adopted[1] if adopted and adopted[0].version == v.version else None
    fields = {f.name: f.value for f in d.fields}
    accounts = {a.scope.account_id for a in d.allocations}
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
        "scope_account_ids": sorted(accounts | {x.scope.account_id for x in d.limits}),
        "status": history.status(v.version),
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


def _json(body: object, version: int) -> Response:
    response = Response(json.dumps(body).encode(), 200, media_type="application/json")
    response.headers["ETag"] = f'"{version}"'
    return response


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
        items = [view(_load(tx, i)) for i in ids[:limit]]
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
    return _json(view(history), history.versions[-1].version)


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
        if if_match != f'"{revision}"' or body.version_id != str(revision):
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
    response = sec.idempotent(principal, request, key, payload, run)
    if response.status_code == 200:
        response.headers["ETag"] = f'"{json.loads(bytes(response.body))["version"]}"'
    return response
