"""`GET /jobs/{job_id}` (readJob; T025 increment 6): the contract `job`, for any
member (the spec names no role), under the session tenant (forced RLS).

An id resolves from durable records only, never from the idempotency record (purged
after 24-48 h):
- a canonical uuid of a durable job (`app.job`, T025): the tenant's;
- a canonical uuid of a feedback record: the job its POST /feedback returned, only
  for the member who submitted it (feedback is private), also once tombstoned;
- `<watchlist id>.deleted`: the job deleteWatchlist returned, for the tenant.
Synchronous jobs are rebuilt from their record with the 202's builder, so the body
is byte for byte what the 202 returned, original times included. Unknown, foreign
and other members' ids are the same 404; an id naming two jobs is 409. A durable
job shows no payload, input revision, lease or worker: `attempt` is
`total_attempts` (never reset by a requeue), `result_reference` is null (no result
is stored), `last_error` is a short-code reason, and `dead_letter` (absent from the
schema) reads as `failed` with a blocking `dead_letter` reason."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from qw_adapters import feedback_store as fs
from qw_adapters import jobs as qj
from qw_adapters import watchlist_store as ws
from qw_adapters.tenancy import TenantTx
from qw_domain.instants import format_instant
from qw_domain.jobs import JobState

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID

router = APIRouter(prefix="/api/v1")
JobId = Annotated[str, Path(pattern=ID)]
DELETED = ".deleted"
_ENDED = {JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED, JobState.DEAD_LETTER}


def synchronous(job_id: str, kind: str, at: datetime, key: str,
                result: str) -> dict[str, Any]:  # fmt: skip
    """The `succeeded` job of a write done inside its request."""
    return {"id": job_id, "kind": kind, "state": "succeeded",
            "created_at": format_instant(at), "attempt": 1, "idempotency_key": key,
            "deadline_at": None, "result_reference": result,
            "reasons": []}  # fmt: skip


def _reason(code: str, message: str, blocking: bool) -> dict[str, Any]:
    return {"code": code, "message": message, "affected_ids": [], "blocking": blocking}


def durable(s: qj.JobStatus) -> dict[str, Any]:
    reasons, state = [], s.state.value
    if s.last_error is not None:
        reasons.append(_reason(s.last_error, "The latest attempt ended with this code.",
                               s.state in _ENDED))  # fmt: skip
    if s.state is JobState.DEAD_LETTER:
        state = JobState.FAILED.value
        reasons.append(_reason("dead_letter", "Attempts are exhausted; an operator "
                               "may requeue the job.", True))  # fmt: skip
    deadline = None if s.deadline_at is None else format_instant(s.deadline_at)
    return {"id": str(s.id), "kind": s.kind, "state": state,
            "created_at": format_instant(s.created_at), "attempt": s.total_attempts,
            "idempotency_key": s.idempotency_key, "deadline_at": deadline,
            "result_reference": None, "reasons": reasons}  # fmt: skip


def _canonical(text: str) -> uuid.UUID | None:
    try:
        u = uuid.UUID(text)
    except ValueError:
        return None
    return u if str(u) == text else None


def _resolve(tx: TenantTx, user: uuid.UUID, job_id: str) -> dict[str, Any] | None:
    if job_id.endswith(DELETED):
        wid = job_id.removesuffix(DELETED)
        r = ws.tombstone(tx, wid)
        if r is None:
            return None
        return synchronous(job_id, "watchlist.delete", r.recorded_at, r.key, wid)
    if (u := _canonical(job_id)) is None:
        return None
    found = []
    if (s := qj.status(tx, u)) is not None:
        found.append(durable(s))
    if (f := fs.issued(tx, user, job_id)) is not None:
        found.append(synchronous(job_id, "feedback.record", f[1], f[0], job_id))
    if len(found) > 1:
        raise ApiError(409, "ambiguous_job", "The id names more than one job.")
    return found[0] if found else None


@router.get("/jobs/{job_id}")
def read_job(principal: Reader, job_id: JobId) -> Response:
    try:
        with principal.tx() as tx:
            job = _resolve(tx, principal.session.user_id, job_id)
    except (ws.WatchlistStoreError, fs.FeedbackStoreError) as exc:
        if exc.code != "integrity":
            raise  # database: a crash (500), never a client answer
        raise ApiError(409, "integrity", "Stored records failed checks.") from None
    if job is None:
        raise ApiError(404, "not_found", "No such job.")
    return Response(json.dumps(job).encode(), 200, media_type="application/json")
