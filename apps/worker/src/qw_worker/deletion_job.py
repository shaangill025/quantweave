"""The `retention.delete` job (T058 PR B; spec §4, §12, §15; R052, R086-R088): one
deletion request or expiry sweep of one tenant, on qw_worker in its transaction.

- `submit_request` enqueues a `DeletionRequest` once per request id; `submit_sweep`
  an `ExpirySweep` once per hour of the database clock. The payload carries the
  trigger time (a retry or a resume never resets it) and never the acknowledgement
  text. No timer submits sweeps yet (a cross-tenant lister is needed).
- `handlers(key, policy, targets, solo)`: bound to the installation's `RecordKey`
  (`config.record_key_from_env`, a file, never the database) and one `Target` per
  class (a store's handler: list records with their exact `created_at`; delete
  one). A request for a class the policy deletes but no target handles is
  `no_target`, before anything runs.
- Lock order (PR A review M2): the first lock is `legal_hold` shared, through
  `legal_hold_store.load` (READ COMMITTED only); every tombstone lock comes after
  it, inside `tombstone_store.append`. The job never places or releases a hold.
  Any failure to load the holds is `hold_state_unknown` and deletes nothing.
- Then the targets list the records (a failure fails the job, nothing deleted),
  `retention.plan` as of the trigger time, and `retention.pending` per intent at
  the database clock or the latest hold event, whichever is later: an unreleased
  covering hold always blocks. Each permitted intent is deleted and tombstoned in
  one savepoint, both or neither; an existing tombstone (a retry, or a restored
  copy) stands and the row is deleted again. Owner decision 114: no backup
  catalog exists, so every tombstone is marked `backups_unknown`.
- A record's failure stays with that record: `TargetError(code, retryable)` from a
  target, a tombstone refusal, or any other exception (`target_failed`). The others
  are still deleted. Logs name the job id and codes only, never a record id, and
  carry no traceback (spec §12: redacted identifiers).
- Outcome: succeeded; `database` (kept, retried) when any failure is retryable,
  else `deletion_refused` (kept); otherwise `deletion_suspended` (kept, not
  retried) while a hold or the policy blocks an intent. Owner decision 113: a
  hold release enqueues a fresh job per suspended request
  (`legal_hold_store.resume_suspended`, revision `request:<id>:r<release seq>`,
  the same payload); each sweep runs it again as the backstop.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from qw_adapters import legal_hold_store as hs
from qw_adapters import tombstone_store as ts
from qw_adapters.jobs import ClaimedJob, Json, enqueue
from qw_adapters.tenancy import TenantTx
from qw_domain import retention as rt
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.jobs import Priority, check_error_code

from qw_worker.loop import Handler, HandlerFailed

log = logging.getLogger("qw_worker.deletion_job")
KIND = hs.DELETION_KIND
ACK = "recorded-by-the-request-route"  # the planner never reads the text
_RETRY = frozenset({"database", "stale_clock", "time_order"})
_REQUEST = frozenset({"trigger", "request_id", "scope_id", "classes", "record_ids",
                      "requested_at", "reauthentication_receipt_id"})  # fmt: skip
_SWEEP = frozenset({"trigger", "sweep_id", "triggered_at"})


class TargetError(Exception):
    """A store handler's refusal of one record or listing: a short error code and
    whether a retry may succeed. The message is the code alone."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(check_error_code(code))
        self.code, self.retryable = code, retryable


@dataclass(frozen=True, slots=True)
class Target:
    """One store's deletion handler for `cls`. `items(tx, ids)`: the tenant's
    records of the class (all when `ids` is empty), each with its immutable,
    microsecond-exact `created_at`; `delete(tx, item)`: remove it (True when a
    row was removed). Either may raise `TargetError`."""

    cls: rt.RetentionClass
    items: Callable[[TenantTx, frozenset[str]], Sequence[rt.Item]]
    delete: Callable[[TenantTx, rt.Item], bool]


# The installation's store handlers. None exists yet (each needs its own task and
# its exact-`created_at` acceptance test), so the entry point registers no handler.
TARGETS: Mapping[rt.RetentionClass, Target] = MappingProxyType({})


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


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


def submit_request(tx: TenantTx, req: rt.DeletionRequest) -> tuple[uuid.UUID, bool]:
    """Enqueue `req` for `tx`'s tenant once: (job id, created)."""
    if type(req) is not rt.DeletionRequest or req.tenant_id != str(tx.tenant_id):
        raise ValueError("a DeletionRequest of this tenant")
    if req.requested_at > _now(tx):
        raise ValueError("requested_at is in the future")
    return enqueue(tx, KIND, f"request:{req.request_id}", Priority.SAFETY,
                   payload(req))  # fmt: skip


def submit_sweep(tx: TenantTx) -> tuple[uuid.UUID, bool]:
    """Enqueue this hour's expiry sweep of `tx`'s tenant once."""
    hour = _now(tx).replace(minute=0, second=0, microsecond=0)
    sweep = rt.ExpirySweep(f"sweep-{hour:%Y%m%dT%H}", str(tx.tenant_id), hour)
    return enqueue(tx, KIND, f"sweep:{sweep.sweep_id}", Priority.SAFETY,
                   payload(sweep))  # fmt: skip


class _Bad(Exception):
    pass


def _instant(v: object) -> datetime:
    if not isinstance(v, str):
        raise _Bad
    return ensure_aware_utc(datetime.fromisoformat(v))


def _trigger(p: object, tenant: str) -> rt.Trigger:
    """The payload back as its trigger; it must round-trip exactly."""
    try:
        if not isinstance(p, dict) or set(p) not in (_REQUEST, _SWEEP):
            raise _Bad
        trig: rt.Trigger
        if p["trigger"] == "sweep":
            trig = rt.ExpirySweep(p["sweep_id"], tenant, _instant(p["triggered_at"]))
        elif p["trigger"] == "request" and all(
                type(p[k]) is list for k in ("classes", "record_ids")):  # fmt: skip
            trig = rt.DeletionRequest(
                p["request_id"], tenant, p["scope_id"],
                frozenset(rt.RetentionClass(c) for c in p["classes"]),
                _instant(p["requested_at"]), p["reauthentication_receipt_id"], ACK,
                frozenset(p["record_ids"]))  # fmt: skip
        else:
            raise _Bad
        if payload(trig) != p:
            raise _Bad
    except (_Bad, KeyError, TypeError, ValueError):
        raise HandlerFailed("payload_invalid", retryable=False) from None
    return trig


def _latest(holds: rt.Holds, tenant: str) -> datetime | None:
    times = [e.placed_at if isinstance(e, rt.LegalHold) else e.released_at
             for e in holds.history(tenant)]  # fmt: skip
    return max(times, default=None)


def _code(exc: Exception) -> tuple[str, bool]:
    """(code, retryable) of a failure, naming no record."""
    if isinstance(exc, TargetError):
        return exc.code, exc.retryable
    if isinstance(exc, ts.TombstoneStoreError):
        return exc.code, exc.code in _RETRY
    return "target_failed", False


def handlers(key: rt.RecordKey, policy: rt.Policy,
             targets: Mapping[rt.RetentionClass, Target],
             solo: rt.SoloRelease | None = None) -> Mapping[str, Handler]:  # fmt: skip
    if type(key) is not rt.RecordKey or type(policy) is not rt.Policy:
        raise TypeError("a RecordKey and a Policy")
    if any(type(t) is not Target or t.cls is not c for c, t in targets.items()):
        raise TypeError("targets maps each class to its Target")
    targets = MappingProxyType(dict(targets))

    def handle(tx: TenantTx, job: ClaimedJob) -> None:
        tenant = str(tx.tenant_id)
        trig = _trigger(job.payload, tenant)
        if isinstance(trig, rt.DeletionRequest):
            classes, asked = trig.classes, trig.record_ids
            if any(c not in targets and policy.rule(c).on_request
                   is rt.OnRequest.DELETE for c in classes):  # fmt: skip
                raise HandlerFailed("no_target", retryable=False)
        else:
            classes, asked = frozenset(targets), frozenset()
        try:  # the job's first lock: legal_hold shared, to the end (M2)
            holds = hs.load(tx, solo)
            if isinstance(trig, rt.ExpirySweep):
                hs.resume_suspended(tx)  # decision 113's backstop
        except Exception as exc:  # any failure: the hold state is unknown
            code = exc.code if isinstance(exc, hs.HoldStoreError) else "unexpected"
            log.warning("job %s: hold state unknown: %s", job.id, code)
            raise HandlerFailed("hold_state_unknown",
                                retryable=code == "database") from None  # fmt: skip
        try:
            items = [it for c in sorted(classes & targets.keys())
                     for it in targets[c].items(tx, asked)]  # fmt: skip
            plan = rt.plan(trig, items, policy, holds, key)
        except Exception as exc:  # nothing deleted yet
            code, retry = _code(exc)
            if isinstance(exc, rt.RetentionError):
                code, retry = "deletion_refused", False
            log.warning("job %s: listing or planning failed: %s (%s)", job.id, code,
                        type(exc).__name__)  # fmt: skip
            raise HandlerFailed(code, retryable=retry) from None
        now = _now(tx)
        if now < plan.trigger_at:
            raise HandlerFailed("time_order", retryable=True)
        at = max(now, _latest(holds, tenant) or now)
        suspended, failed = 0, []
        for d in plan.decisions:
            why = rt.pending(trig, policy, holds, d, at)
            if why == "retain":
                continue
            if why is not None:
                suspended += 1
                continue
            item = rt.Item(tenant, d.record_id, d.cls, d.created_at)
            try:
                with tx.conn.transaction():  # the delete and its tombstone, or none
                    targets[d.cls].delete(tx, item)
                    ts.append(tx, key, d, plan, None)  # decision 114: unknown
            except Exception as exc:  # this record only
                failed.append(_code(exc))
                log.warning("job %s: a deletion failed: %s (%s)", job.id,
                            failed[-1][0], type(exc).__name__)  # fmt: skip
        if any(retry for _, retry in failed):
            raise HandlerFailed("database", retryable=True, keep_effects=True)
        if failed:
            raise HandlerFailed("deletion_refused", retryable=False, keep_effects=True)
        if suspended:
            raise HandlerFailed("deletion_suspended", retryable=False,
                                keep_effects=True)  # fmt: skip

    return MappingProxyType({KIND: handle})
