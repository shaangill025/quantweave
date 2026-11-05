"""Privacy deletion requests (T058 increment 9; spec §12; R052, R086-R088; owner
decisions 113, 127, 128 and 135 to 138): `POST /privacy/deletions`
(requestDeletion) and `GET /privacy/deletions/{deletion_id}` (readDeletion,
project contract 0.14.0).

Filing needs the tenant_owner role, CSRF and an Idempotency-Key (C-03: a retry
replays the stored 202; another body under the key is 409). The body is checked
first: the acknowledgement holds at most 2000 characters and 4096 UTF-8 bytes and
no control characters (400). Then, inside the idempotent run after the key's
claim, so a same-key retry of an accepted request replays its 202 even after its
step-up lapsed or the policy narrowed (review finding 2), and every refusal rolls
the claim back:
- a step-up within `Settings.step_up_window` (`security.require_step_up`'s own
  check), else 401 `step_up_required`;
- `qw_adapters.deletion_requests.check` against the worker's registry and the
  worker's retention policy (`Settings.retention_policy`): an unknown or
  repeated class is 400; identity (`security`) is 422 `account_closure_required`
  (decision 128: only a separate account-closure flow, not built here); a class
  with no registered deletion target is 422 `deletion_not_supported`, "not yet
  supported" (decision 127); a class the policy retains or leaves unconfigured is
  422 `retained_by_policy` (decision 137); a scope other than the whole tenant
  (`tenant`) is 422 `deletion_scope_not_supported`. Each names the offending
  field.
An accepted request enqueues one `retention.delete` job (revision `request:<new
uuid>`, the database clock as its trigger time), is audited
`privacy.deletion_requested` (target: the job; detail: the acknowledgement, the
classes and the scope, decision 136, 0067) and returns the spec `job` (202), in
one transaction with the idempotency record.

Decision 135: the body's `reauthentication_receipt_id` is validated as an id and
then ignored until a receipt store exists. The job carries `step-up:<MAC of the
session and its step_up_at>`, a binding to the step-up this request was checked
against, never an authorization token: nothing accepts it as a grant.

Reading needs the tenant_owner role (decision 138): one request by the job id the
202 returned, its state from the request's latest job
(`deletion_requests.status`). Unknown, foreign, malformed and non-request ids are
the same 404; `GET /jobs/{id}` hides these jobs from members the same way.
Nothing reaches a broker.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request, Response
from pydantic import Field
from qw_adapters import deletion_requests as dr
from qw_adapters import jobs as qj
from qw_adapters import tenancy
from qw_adapters.tenancy import MembershipRole, TenantTx
from qw_domain import retention as rt
from qw_domain.instants import format_instant

from qw_api import routes
from qw_api import security as sec
from qw_api.app import ApiError, FieldError, correlation_id
from qw_api.jobs import _canonical, durable
from qw_api.reads import ID
from qw_api.security import Principal

router = APIRouter(prefix="/api/v1")
# The step-up is checked inside the idempotent run (review finding 2), not here.
Filer = Annotated[Principal, Depends(sec.require_role(MembershipRole.TENANT_OWNER))]
Owner = Annotated[Principal, Depends(sec.require_role(MembershipRole.TENANT_OWNER))]
DeletionId = Annotated[str, Path(pattern=ID)]
_REFUSED = {
    "unknown_class": (400, "invalid_request", "Unknown retention class.", "enum"),
    "duplicate_class": (400, "invalid_request", "A class is named twice.",
                        "duplicate"),
    "no_class": (400, "invalid_request", "Name at least one class.", "min_items"),
    "account_closure_required": (
        422, "account_closure_required", "Identity and account data are deleted "
        "only by account closure, never by a class request.",
        "account_closure_required"),
    "deletion_not_supported": (
        422, "deletion_not_supported", "Deletion of these classes is not yet "
        "supported; nothing was requested.", "not_yet_supported"),
    "deletion_scope_not_supported": (
        422, "deletion_scope_not_supported", "Only the whole tenant (scope_id "
        "tenant) can be deleted.", "not_supported"),
    "retained_by_policy": (
        422, "retained_by_policy", "The retention policy keeps these classes "
        "(retained or unconfigured), so they cannot be deleted on request; "
        "nothing was requested.", "retained_by_policy"),
}  # fmt: skip
_MESSAGES = {
    "deletion_suspended": "A legal hold or the retention policy blocks part of this "
    "deletion; it resumes when the hold is released.",
    "refused": "The deletion job ended with this code.",
    "pending": "The latest attempt ended with this code; it will be retried.",
}


class DeleteRequest(routes.Body):
    scope_id: str = Field(pattern=ID)
    classes: list[str] = Field(min_length=1, max_length=len(rt.RetentionClass))
    reauthentication_receipt_id: str = Field(pattern=ID)
    acknowledgement: str = Field(min_length=1, max_length=2000)


def _refusal(exc: dr.RequestRefused, classes: list[str]) -> ApiError:
    status, code, detail, field_code = _REFUSED[exc.code]
    if exc.indexes:
        errors = tuple(
            FieldError(f"/classes/{n}", field_code, f"{classes[n]}: {detail}")
            for n in exc.indexes
        )
    else:
        pointer = "/classes" if exc.code == "no_class" else "/scope_id"
        errors = (FieldError(pointer, field_code, detail),)
    return ApiError(status, code, detail, False, errors)  # fmt: skip


def _receipt(request: Request, principal: Principal) -> str:
    """The step-up this request was checked against, as an opaque id."""
    s = principal.session
    assert s.step_up_at is not None  # checked by the run first
    tag = sec.mac(sec.settings_of(request), "deletion-step-up", str(s.session_id),
                  format_instant(s.step_up_at))  # fmt: skip
    return f"step-up:{tag.hex()[:32]}"


@router.post("/privacy/deletions")
def request_deletion(
    body: DeleteRequest, request: Request, principal: Filer, key: routes.IdemKey
) -> Response:
    text = body.acknowledgement
    if len(text.encode()) > 4096 or any(unicodedata.category(c) == "Cc"
                                        for c in text):  # fmt: skip
        error = FieldError("/acknowledgement", "invalid", "Control characters or "
                           "over 4096 UTF-8 bytes.")  # fmt: skip
        raise ApiError(400, "invalid_request", "Invalid text.", False, (error,))
    settings, user = sec.settings_of(request), principal.session.user_id
    step_up = sec.require_step_up(MembershipRole.TENANT_OWNER)

    def run(tx: TenantTx) -> tuple[int, bytes]:
        step_up(principal, settings)  # 401 step_up_required: rolls back the claim
        try:
            classes = dr.check(body.classes, body.scope_id, settings.retention_policy)
        except dr.RequestRefused as exc:
            raise _refusal(exc, body.classes) from None
        receipt = _receipt(request, principal)
        req = rt.DeletionRequest(str(uuid.uuid4()), str(tx.tenant_id), body.scope_id,
                                 classes, dr.now(tx), receipt, dr.ACK)  # fmt: skip
        job_id, _ = dr.submit_request(tx, req)
        detail: dict[str, str | list[str]] = {
            "acknowledgement": text, "scope_id": body.scope_id,
            "classes": sorted(c.value for c in classes)}  # fmt: skip
        tenancy.append_audit_event(
            tx, "privacy.deletion_requested", actor_user_id=user, target_id=job_id,
            correlation_id=correlation_id(request), detail=detail)  # fmt: skip
        found = qj.status(tx, job_id)
        assert found is not None  # queued in this transaction
        return 202, json.dumps(durable(found)).encode()

    payload = {"body": body.model_dump(mode="json")}
    return sec.idempotent(principal, request, key, payload, run)


def view(deletion_id: str, s: dr.DeletionStatus) -> dict[str, Any]:
    reasons = []
    if s.code is not None:
        message = _MESSAGES[s.state if s.state in _MESSAGES else "refused"]
        reasons.append({"code": s.code, "message": message, "affected_ids": [],
                        "blocking": s.state != "pending"})  # fmt: skip
    return {"id": deletion_id, "state": s.state, "classes": list(s.classes),
            "requested_at": format_instant(s.requested_at),
            "reasons": reasons}  # fmt: skip


@router.get("/privacy/deletions/{deletion_id}")
def read_deletion(principal: Owner, deletion_id: DeletionId) -> Response:
    job_id, found = _canonical(deletion_id), None
    if job_id is not None:
        with principal.tx() as tx:
            found = dr.status(tx, job_id)
    if found is None:
        raise ApiError(404, "not_found", "No such deletion request.")
    body = json.dumps(view(deletion_id, found)).encode()
    return Response(body, 200, media_type="application/json")
