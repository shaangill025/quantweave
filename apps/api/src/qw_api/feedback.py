"""`POST /feedback` (submitFeedback; T043 inc. 2): any member's own private feedback,
CSRF and Idempotency-Key (C-03); a deleted record's key is 409 `deleted`. `object_id`
binds, in the tenant, a proposal's latest version or a simulation (unknown or
foreign: 404; both kinds: 409). The 202 `job` is `succeeded` (synchronous write);
its id is the record id, not pollable (readJob is not served). Never logged or sent
to a model or any external service."""

from __future__ import annotations

import json
import uuid
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import Field
from qw_adapters import feedback_store as store
from qw_adapters import simulation_store as ss
from qw_adapters.proposal_store import ProposalPublisher
from qw_adapters.tenancy import TenantTx
from qw_domain import feedback as fb
from qw_domain.instants import format_instant

from qw_api import routes
from qw_api import security as sec
from qw_api.app import ApiError, FieldError
from qw_api.policies import Reader
from qw_api.reads import ID

router = APIRouter(prefix="/api/v1")
Category = Literal["incorrect_fact", "irrelevant", "clear", "unclear", "missed_risk",
                   "other"]  # fmt: skip
_CONFLICT = {"conflict": ("idempotency_key_reused", "Key used elsewhere."),
             "deleted": ("deleted", "This key's feedback was deleted.")}  # fmt: skip


class FeedbackBody(routes.Body):
    object_id: str = Field(min_length=1, max_length=128, pattern=ID)
    category: Category
    message: str = Field(max_length=fb.MAX_MESSAGE)
    cross_user_contribution_consent: Literal[False]


def _subject(tx: TenantTx, object_id: str) -> fb.Subject:
    found: list[fb.Subject] = []
    p = ProposalPublisher(tx).load(object_id)
    if p is not None:
        found.append(fb.Subject(fb.SubjectKind.PROPOSAL, object_id, len(p.versions),
                                p.versions[-1].content_hash))  # fmt: skip
    s = ss.load(tx, object_id)
    if s is not None:
        found.append(fb.Subject(fb.SubjectKind.SIMULATION, object_id, 1, ss.digest(s)))
    if not found:
        raise ApiError(404, "not_found", "No such object.")
    if len(found) > 1:
        raise ApiError(409, "ambiguous_object", "The id names more than one object.")
    return found[0]


@router.post("/feedback", status_code=202)
def submit_feedback(
    body: FeedbackBody, request: Request, principal: Reader, key: routes.IdemKey
) -> Response:
    if "\x00" in body.message:
        error = FieldError("/message", "invalid", "NUL is not permitted.")
        raise ApiError(400, "invalid_request", "Invalid text.", False, (error,))
    user = principal.session.user_id
    with principal.tx() as tx:  # a replay never revives deleted feedback (N2)
        gone = tx.conn.execute(
            "SELECT 1 FROM app.feedback JOIN app.feedback_tombstone USING (tenant_id, "
            "record_id) WHERE idempotency_key = %s AND feedback.principal_id = %s",
            (key, user)).fetchone()  # fmt: skip
    if gone is not None:
        raise ApiError(409, *_CONFLICT["deleted"])

    def run(tx: TenantTx) -> tuple[int, bytes]:
        subject = _subject(tx, body.object_id)
        try:
            f = store.record(tx, principal=user, key=key, subject=subject,
                             category=fb.FeedbackCategory(body.category),
                             message=body.message)  # fmt: skip
        except (store.FeedbackStoreError, fb.FeedbackError) as exc:
            if exc.code not in _CONFLICT:
                raise  # integrity: a crash (500), never a client answer
            raise ApiError(409, *_CONFLICT[exc.code]) from None
        routes._audit(tx, request, "feedback.recorded", user, uuid.UUID(f.record_id))
        job = {"id": f.record_id, "kind": "feedback.record", "state": "succeeded",
               "created_at": format_instant(f.recorded_at), "attempt": 1,
               "idempotency_key": key, "deadline_at": None,
               "result_reference": f.record_id, "reasons": []}  # fmt: skip
        return 202, json.dumps(job).encode()

    payload = {"body": body.model_dump(mode="json")}
    return sec.idempotent(principal, request, key, payload, run)
