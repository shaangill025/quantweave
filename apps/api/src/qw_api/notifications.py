"""`GET /notifications` (listNotifications; T056 increment 2) over
qw_adapters.notification_store, like the T053 reads: membership, session tenant
(RLS) and session user, C-02 keyset pages with the qw_api.reads HMAC cursor, and
nothing written (listing is not opening). A record failing verification is a 500.
Rows are the contract `Notification`, one per channel: the in-app row (id = record
id; `delivered` = listed, never a read) when listed, then each external channel
(`<id>:<channel>`) at its latest event. No transport exists, so nothing is sent.
`current_object_state` is resolved at read time, never the trigger-time version:
proposals with readProposal's state (`reads.proposal`: recorded resolution plus
covering stored pauses; live states name NOT_RECHECKED),
risk pauses as `paused`/`resumed`; alerts, jobs (no store qw_app may read), missing
objects and resolver errors read `unavailable`, named in coverage.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Request, Response
from qw_adapters import journal_store as js
from qw_adapters import notification_store as ns
from qw_adapters import pause_store, policy_store
from qw_adapters import proposal_store as pst
from qw_adapters.tenancy import TenantTx
from qw_domain import notifications as dn
from qw_domain.notifications import Category, Channel
from qw_domain.proposals import TERMINAL, ProposalState

from qw_api import reads
from qw_api import security as sec
from qw_api.app import ApiError, Settings
from qw_api.policies import Reader
from qw_api.reads import NOT_RECHECKED, Cursor, Limit, _after, _cursor, _now

router = APIRouter(prefix="/api/v1")
SORT = "seq_channel"


def id_key(settings: Settings) -> bytes:
    return sec.mac(settings, "notification-id")


def _resolve(
    tx: TenantTx, n: dn.Notification, now: datetime, held: pause_store.Pauses | None
) -> dn.CurrentView | None:
    s = n.subject
    if held is None:  # the stored pauses failed their checks: nothing is current
        return None
    if s.category is Category.PROPOSAL:
        p, policy = pst.ProposalPublisher(tx).load(s.object_id), pst.policy_of(
            tx, s.object_id)  # fmt: skip
        if p is None or policy is None:
            return None
        history = policy_store.load(tx, policy)
        accounts = sorted({k for k, _ in p.latest.binding.account_revisions})
        revisions = {k: js.revision(tx, k) for k in accounts}
        book = held.covering(policy, p.latest.action.account_id)

        def state_at(at: datetime) -> ProposalState:  # exactly readProposal's state
            view, _ = reads.proposal(p, history, revisions, at, book)
            return ProposalState(view["state"])

        return dn.recorded_proposal_resolver(p, state_at)(n, max(now, p.events[-1].at))
    if s.category is Category.RISK_PAUSE:
        if s.object_id not in held.policy_of:
            return None
        active = any(x.pause_id == s.object_id for x in held.book.active())
        state = "paused" if active else "resumed"
        return dn.CurrentView(state, False, state)
    return None  # monitoring_alert, job_outcome: no readable current-state store


def _status(item: ns.Stored, ch: Channel) -> str:
    if ch is Channel.IN_APP:
        return dn.DeliveryStatus.DELIVERED.value
    return next(d for d in item.deliveries if d.channel is ch).status.value


@router.get("/notifications")
def list_notifications(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    raw = _after(request, principal, SORT, cursor)
    try:
        seq, rank = (int(x) for x in raw.split(":")) if raw else (None, 0)
    except ValueError:  # the MAC held, so only a key we never issue lands here
        raise ApiError(400, "invalid_cursor", "The cursor is not valid here.") from None
    key, user = id_key(sec.settings_of(request)), str(principal.session.user_id)
    with principal.tx() as tx:
        try:
            rows = ns.page(tx, key, user, limit + 1,
                           None if seq is None else (seq, rank))  # fmt: skip
            now, views = _now(tx), dict[str, dn.CurrentView | None]()
            try:
                held: pause_store.Pauses | None = pause_store.load(tx)  # once
            except pause_store.PauseStoreError:
                held = None
            for r in rows[:limit]:
                n = r.item.notification
                if n.id not in views:
                    try:
                        views[n.id] = _resolve(tx, n, now, held)
                    except (dn.NotificationError, pst.ProposalStoreError,
                            pause_store.PauseStoreError):  # fmt: skip
                        views[n.id] = None  # never the stored, stale version
        except ns.NotificationStoreError as exc:
            raise ApiError(500, "integrity", "A stored notification failed its "
                           "checks.") from exc  # fmt: skip
    items, reasons = [], list[str]()
    for r in rows[:limit]:
        n = r.item.notification
        rid = n.id if r.channel is Channel.IN_APP else f"{n.id}:{r.channel.value}"
        view = views[n.id]
        if view is None:
            reasons.append(f"unavailable:{rid}/current_object_state")
        elif n.subject.category is Category.PROPOSAL and view.state not in TERMINAL:
            reasons.append(NOT_RECHECKED)
        state = view.state if view else "unavailable"
        items.append({"id": rid, "object_id": n.subject.object_id,
                      "channel": r.channel.value, "status": _status(r.item, r.channel),
                      "current_object_state": state})  # fmt: skip
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = _cursor(request, principal, SORT, f"{last.seq}:{ns.RANK[last.channel]}")
    reasons = list(dict.fromkeys(reasons))
    coverage = {"status": "partial" if reasons else "complete", "reasons": reasons}
    body = {"items": items, "next_cursor": nxt, "coverage": coverage}
    return Response(json.dumps(body).encode(), 200, media_type="application/json")
