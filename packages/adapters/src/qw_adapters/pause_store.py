"""Persisted risk pauses and resumes (T020 increment 2; `0016_risk_pause.sql`).

- `load` re-hashes every row and replays the chain through `PauseBook`; a mismatch
  is `integrity` (a qw_app that recomputes hashes is not stopped: no C-20 signing).
- `pause` pauses each account any version of the policy names, unless already
  paused by it; `covering` extends an active policy pause, at read time, to the
  account an action of the policy names. It reads no step-up, revision,
  freshness, journal or adoption state, so stale or missing data never stops it.
- `resume` needs If-Match = `tag` (the version plus a digest of the active pause
  ids), the owner's live session stepped up within `window` (at most 1 h), and an
  active pause. It resumes all of the policy's pauses or none: one that needs a
  reconciled revision refuses it (no route can present one, T017). No risk
  condition is re-evaluated (no risk inputs are persisted).
Times are the DB clock. Lock `risk_pause:<tenant>` (shared to load) is the last in
the order evidence, rights, simulation, journal account, policy, pause; `pause`
takes only it. Nothing here touches broker state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from qw_domain import policy as dp
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.risk_pauses import (
    Pause,
    PauseBook,
    PauseError,
    PauseScope,
    PauseTrigger,
    Resume,
    ScopeKind,
)

from qw_adapters import policy_store as ps
from qw_adapters.tenancy import TenantTx

MAX_STEP_UP_WINDOW = ps.MAX_STEP_UP_WINDOW


class PauseStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Pauses:
    book: PauseBook
    policy_of: Mapping[str, uuid.UUID]  # pause id -> the policy it was recorded under

    def active_for(self, policy_id: uuid.UUID) -> tuple[Pause, ...]:
        return tuple(p for p in self.book.active() if self.policy_of[p.pause_id] ==
                     policy_id)  # fmt: skip

    def covering(self, policy_id: uuid.UUID, account_id: str) -> PauseBook:
        """The book plus, if this policy is paused but not on `account_id`, one
        copy of its first active pause scoped to that account."""
        on, scope = (
            self.active_for(policy_id),
            PauseScope(ScopeKind.ACCOUNT, account_id),
        )
        if not on or any(p.scope == scope for p in on):
            return self.book
        return self.book.pause(replace(on[0], pause_id=f"{on[0].pause_id}.c",
                                       scope=scope))  # fmt: skip


def tag(version: int, active: tuple[Pause, ...]) -> str:
    """The policy ETag: `"N"`, or `"N-<digest of the active pause ids>"`."""
    ids = ",".join(sorted(p.pause_id for p in active)).encode()
    return f'"{version}-{hashlib.sha256(ids).hexdigest()[:16]}"' if active else (
        f'"{version}"')  # fmt: skip


def policy_accounts(history: dp.PolicyHistory, every: bool = False) -> list[str]:
    """The accounts the latest version (or `every` version) covers."""
    ds = [v.draft for v in history.versions[0 if every else -1 :]]
    return sorted({a.scope.account_id for d in ds for a in d.allocations}
                  | {x.scope.account_id for d in ds for x in d.limits})  # fmt: skip


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"risk_pause:{tx.tenant_id}",))  # fmt: skip


def _digest(*parts: object) -> str:
    text = json.dumps([None if p is None else str(p) for p in parts],
                      separators=(",", ":"))  # fmt: skip
    return hashlib.sha256(text.encode()).hexdigest()


def _pause_hash(tenant: uuid.UUID, policy: uuid.UUID, version: int, who: uuid.UUID,
                reason: str, text: str, p: Pause) -> str:  # fmt: skip
    return _digest("risk_pause", tenant, p.pause_id, policy, version, p.scope.kind,
                   p.scope.scope_id, p.trigger, p.evidence_ref, p.material, who,
                   reason, text, format_instant(p.triggered_at))  # fmt: skip


def _resume_hash(tenant: uuid.UUID, session: uuid.UUID, step_up_at: datetime,
                 text: str, r: Resume) -> str:  # fmt: skip
    return _digest("risk_pause_resume", tenant, r.pause_id, r.principal_id, session,
                   format_instant(step_up_at), r.step_up_receipt_id, r.reason, text,
                   r.reconciled_revision, format_instant(r.resumed_at))  # fmt: skip


def _read(tx: TenantTx) -> Pauses:
    t = tx.tenant_id
    events: list[tuple[datetime, int, Pause | Resume]] = []
    policy_of: dict[str, uuid.UUID] = {}
    try:
        for row in tx.conn.execute(
            "SELECT pause_id, policy_id, policy_version, scope_kind, scope_id, "
            "trigger_kind, evidence_ref, material, principal_id, reason, "
            "acknowledgement_text, triggered_at, event_hash FROM app.risk_pause "
            "WHERE tenant_id = %s", (t,),
        ):  # fmt: skip
            pid, policy, version, kind, scope, trig, ref, material = row[:8]
            scope_of, at = PauseScope(ScopeKind(kind), scope), ensure_aware_utc(row[11])
            p = Pause(str(pid), scope_of, PauseTrigger(trig), at, ref, material)
            ok = _pause_hash(t, policy, version, row[8], row[9], row[10], p) == row[12]
            _integrity(ok and type(material) is bool, "pause")
            policy_of[p.pause_id] = policy
            events.append((p.triggered_at, 0, p))
        for row in tx.conn.execute(
            "SELECT pause_id, principal_id, session_id, step_up_at, "
            "step_up_receipt_id, reason, acknowledgement_text, reconciled_revision, "
            "resumed_at, event_hash FROM app.risk_pause_resume WHERE tenant_id = %s",
            (t,),
        ):
            pid, who, session, step_up_at, receipt, reason, text, rec = row[:8]
            r = Resume(str(pid), str(who), reason, receipt, ensure_aware_utc(row[8]),
                       rec)  # fmt: skip
            ok = _resume_hash(t, session, step_up_at, text, r) == row[9]
            _integrity(ok and receipt == ps.step_up_receipt_id(session, step_up_at),
                       "resume")  # fmt: skip
            events.append((r.resumed_at, 1, r))
        events.sort(key=lambda e: (e[0], e[1], e[2].pause_id))
        book = PauseBook(str(t), tuple(e for _, _, e in events))
    except (PauseError, ValueError, TypeError) as exc:  # a forged chain
        raise PauseStoreError("integrity", str(exc)) from None
    return Pauses(book, policy_of)


def _integrity(ok: bool, what: str) -> None:
    if not ok:
        raise PauseStoreError("integrity", what)


def load(tx: TenantTx) -> Pauses:
    """The tenant's pause chain, hash-verified and replayed."""
    _lock(tx, shared=True)
    return _read(tx)


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def pause(
    tx: TenantTx, policy_id: uuid.UUID, *, principal: uuid.UUID, reason: str,
    text: str,
) -> Pauses:  # fmt: skip
    """Pause the policy's accounts; already-paused ones are left as they are."""
    history = ps.load(tx, policy_id)  # takes no lock; an integrity error is raised
    if history is None:
        raise PauseStoreError("not_found", "no such policy")
    version = history.versions[-1].version
    _lock(tx, shared=False)
    current = _read(tx)
    held = {p.scope.scope_id for p in current.active_for(policy_id)
            if p.scope.kind is ScopeKind.ACCOUNT}  # fmt: skip
    book, policy_of, now = current.book, dict(current.policy_of), _now(tx)
    for account in policy_accounts(history, every=True):
        if account in held:
            continue
        p = Pause(str(uuid.uuid4()), PauseScope(ScopeKind.ACCOUNT, account),
                  PauseTrigger.USER, now, f"user:{principal}", False)  # fmt: skip
        book, policy_of[p.pause_id] = book.pause(p), policy_id
        _insert_pause(tx, policy_id, version, principal, reason, text, p)
    return Pauses(book, policy_of)


def resume(
    tx: TenantTx, policy_id: uuid.UUID, etag: str, *, principal: uuid.UUID,
    session_id: uuid.UUID, window: timedelta, reason: str, text: str,
) -> Pauses:  # fmt: skip
    """Resume every active pause of the policy, or refuse and write nothing."""
    ps._lock(tx, policy_id)  # the order: policy, then pause
    history = ps.load(tx, policy_id)
    if history is None:
        raise PauseStoreError("not_found", "no such policy")
    session = tx.conn.execute(
        "SELECT s.step_up_at, now(), m.role FROM app.session s JOIN app.membership m "
        "USING (tenant_id, user_id) WHERE s.tenant_id = %s AND s.id = %s AND "
        "s.user_id = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
        "FOR SHARE OF s",
        (tx.tenant_id, session_id, principal),
    ).fetchone()
    limit = min(window, MAX_STEP_UP_WINDOW)
    if session is None or session[0] is None or session[1] - session[0] > limit:
        raise PauseStoreError("step_up_required", "no recent step-up")
    if session[2] != "tenant_owner":
        raise PauseStoreError("forbidden", "only the tenant owner resumes")
    step_up_at = session[0]
    _lock(tx, shared=False)
    current = _read(tx)
    active = current.active_for(policy_id)
    if etag != tag(history.versions[-1].version, active):
        raise PauseStoreError("stale", "the version or the active pauses changed")
    if not active:
        raise PauseStoreError("not_paused", "the policy has no active pause")
    book, now = current.book, _now(tx)
    receipt = ps.step_up_receipt_id(session_id, step_up_at)
    resumes = [Resume(p.pause_id, str(principal), reason, receipt, now, None)
               for p in active]  # fmt: skip
    for r in resumes:  # the domain decides; a refusal writes nothing
        book = book.resume(r)
    for r in resumes:
        _insert_resume(tx, principal, session_id, step_up_at, text, r)
    return Pauses(book, current.policy_of)


def _insert_pause(
    tx: TenantTx, policy_id: uuid.UUID, version: int, principal: uuid.UUID,
    reason: str, text: str, p: Pause,
) -> None:  # fmt: skip
    digest = _pause_hash(tx.tenant_id, policy_id, version, principal, reason, text, p)
    tx.conn.execute(
        "INSERT INTO app.risk_pause (tenant_id, pause_id, policy_id, policy_version, "
        "scope_kind, scope_id, trigger_kind, evidence_ref, material, principal_id, "
        "reason, acknowledgement_text, triggered_at, event_hash) VALUES (%s, %s, %s, "
        "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (tx.tenant_id, uuid.UUID(p.pause_id), policy_id, version, p.scope.kind.value,
         p.scope.scope_id, p.trigger.value, p.evidence_ref, p.material, principal,
         reason, text, p.triggered_at, digest),
    )  # fmt: skip


def _insert_resume(
    tx: TenantTx, principal: uuid.UUID, session_id: uuid.UUID, step_up_at: datetime,
    text: str, r: Resume,
) -> None:  # fmt: skip
    digest = _resume_hash(tx.tenant_id, session_id, step_up_at, text, r)
    args: tuple[Any, ...] = (
        tx.tenant_id, uuid.UUID(r.pause_id), principal, session_id, step_up_at,
        r.step_up_receipt_id, r.reason, text, r.reconciled_revision, r.resumed_at,
        digest,
    )  # fmt: skip
    tx.conn.execute(
        "INSERT INTO app.risk_pause_resume (tenant_id, pause_id, principal_id, "
        "session_id, step_up_at, step_up_receipt_id, reason, acknowledgement_text, "
        "reconciled_revision, resumed_at, event_hash) VALUES (%s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s)", args,
    )  # fmt: skip
