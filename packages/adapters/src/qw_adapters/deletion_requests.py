"""Deletion requests (T058 increment 9; spec §12; R052, R086-R088; owner decisions
113, 127 and 128): what the request route and the `retention.delete` job share.

- `check(classes, scope_id, policy)`: the classes a request may name. Each must
  be a retention class, named once. Identity (`security`) is refused first: it is
  removed only by a separate account-closure flow (decision 128). Then every
  class without a registered deletion target (`deletion_targets.SUPPORTED`, the
  registry the worker's `TARGETS` is built from) is refused as not yet supported
  (decision 127), so no request is accepted that the job cannot honour; the job's
  own `no_target` stays as a backstop. Then (decision 137) every class the
  retention policy does not delete on request (retained or unconfigured) is
  refused, `retained_by_policy`, so a request that ends done deleted what it
  named; the caller passes the worker's policy (`QW_RETENTION_CONFIG`, else
  `owner_defaults`). The only scope is the session's whole tenant (`tenant`).
  A refusal is `RequestRefused(code, indexes)`.
- `payload` / `submit_request`: the job's payload and its idempotent enqueue
  (revision `request:<request id>`), on qw_app or qw_worker. The payload carries
  the trigger time and never the acknowledgement text.
- `status(tx, job_id)`: a request by the id of the job it first queued: the
  latest job of the request (decision 113's resumes are `request:<id>:r<seq>`,
  same payload) gives `pending` (queued or running, also awaiting a retry),
  `done`, `deletion_suspended` (a legal hold or the policy blocks part of it), or
  `refused` (failed for another reason, with its code). None for an unknown id,
  another tenant's (forced RLS) or a job that is not a request.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from qw_domain import retention as rt
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.jobs import Priority

from qw_adapters.deletion_targets import SUPPORTED
from qw_adapters.jobs import Json, enqueue
from qw_adapters.legal_hold_store import DELETION_KIND as KIND
from qw_adapters.tenancy import TenantTx

__all__ = ["ACK", "IDENTITY", "KIND", "SCOPES", "SUPPORTED", "DeletionStatus",
           "RequestRefused", "check", "payload", "status",
           "submit_request"]  # fmt: skip
IDENTITY = rt.RetentionClass.SECURITY  # decision 128: account closure only
SCOPES = frozenset({"tenant"})  # the session's whole tenant
ACK = "recorded-by-the-request-route"  # the planner never reads the text


class RequestRefused(ValueError):
    """`code` and the indexes of the offending classes (empty: the scope)."""

    def __init__(self, code: str, indexes: tuple[int, ...] = ()) -> None:
        super().__init__(code)
        self.code, self.indexes = code, indexes


def check(classes: Sequence[str], scope_id: str,
          policy: rt.Policy) -> frozenset[rt.RetentionClass]:  # fmt: skip
    known = {c.value for c in rt.RetentionClass}
    if bad := tuple(n for n, c in enumerate(classes) if c not in known):
        raise RequestRefused("unknown_class", bad)
    if twice := tuple(n for n, c in enumerate(classes) if c in classes[:n]):
        raise RequestRefused("duplicate_class", twice)
    named = [rt.RetentionClass(c) for c in classes]
    if not named:
        raise RequestRefused("no_class")
    if found := tuple(n for n, c in enumerate(named) if c is IDENTITY):
        raise RequestRefused("account_closure_required", found)
    if found := tuple(n for n, c in enumerate(named) if c not in SUPPORTED):
        raise RequestRefused("deletion_not_supported", found)
    kept = {c for c in named if policy.rule(c).on_request is not rt.OnRequest.DELETE}
    if found := tuple(n for n, c in enumerate(named) if c in kept):
        raise RequestRefused("retained_by_policy", found)
    if scope_id not in SCOPES:
        raise RequestRefused("deletion_scope_not_supported")
    return frozenset(named)


def payload(trig: rt.Trigger) -> dict[str, Json]:
    if isinstance(trig, rt.ExpirySweep):
        return {"trigger": "sweep", "sweep_id": trig.sweep_id,
                "triggered_at": format_instant(trig.triggered_at)}  # fmt: skip
    return {"trigger": "request", "request_id": trig.request_id,
            "scope_id": trig.scope_id,
            "classes": sorted(c.value for c in trig.classes),
            "record_ids": sorted(trig.record_ids),
            "requested_at": format_instant(trig.requested_at),
            "reauthentication_receipt_id":
                trig.reauthentication_receipt_id}  # fmt: skip


def now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def submit_request(tx: TenantTx, req: rt.DeletionRequest) -> tuple[uuid.UUID, bool]:
    """Enqueue `req` for `tx`'s tenant once: (job id, created)."""
    if type(req) is not rt.DeletionRequest or req.tenant_id != str(tx.tenant_id):
        raise ValueError("a DeletionRequest of this tenant")
    if req.requested_at > now(tx):
        raise ValueError("requested_at is in the future")
    return enqueue(tx, KIND, f"request:{req.request_id}", Priority.SAFETY,
                   payload(req))  # fmt: skip


@dataclass(frozen=True, slots=True)
class DeletionStatus:
    state: str  # pending, done, deletion_suspended or refused
    classes: tuple[str, ...]
    requested_at: datetime
    code: str | None  # the latest job's error code, or None


_FAMILY = (
    "SELECT state::text, last_error FROM app.job WHERE tenant_id = %(t)s AND "
    "kind = %(k)s AND (input_revision = %(r)s OR (left(input_revision, "
    "length(%(r)s) + 2) = %(r)s || ':r' AND substr(input_revision, length(%(r)s) "
    "+ 3) ~ '^[1-9][0-9]*$')) ORDER BY created_at DESC, length(input_revision) "
    "DESC, input_revision DESC LIMIT 1"
)


def status(tx: TenantTx, job_id: uuid.UUID) -> DeletionStatus | None:
    row = tx.conn.execute(
        "SELECT input_revision, payload FROM app.job WHERE tenant_id = %s AND "
        "id = %s AND kind = %s", (tx.tenant_id, job_id, KIND)).fetchone()  # fmt: skip
    if row is None or not isinstance(row[1], dict):
        return None
    p = row[1]
    if p.get("trigger") != "request" or row[0] != f"request:{p.get('request_id')}":
        return None  # a sweep or a resumed copy: not a request's own id
    latest = tx.conn.execute(_FAMILY, {"t": tx.tenant_id, "k": KIND,
                                       "r": row[0]}).fetchone()  # fmt: skip
    assert latest is not None  # the request's own job is one of them
    state, code = latest
    if state in ("queued", "running"):
        shown = "pending"
    elif state == "succeeded":
        shown = "done"
    elif state == "failed" and code == "deletion_suspended":
        shown = "deletion_suspended"
    else:  # failed, cancelled, expired or dead_letter
        shown, code = "refused", code or state
    at = ensure_aware_utc(datetime.fromisoformat(p["requested_at"]))
    return DeletionStatus(shown, tuple(p["classes"]), at, code)
