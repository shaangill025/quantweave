"""`GET /notifications` (listNotifications; T056 increments 2-3) over
qw_adapters.notification_store, like the T053 reads: membership, session tenant
(RLS) and session user, C-02 keyset pages with the qw_api.reads HMAC cursor, and
nothing written (listing is not opening). Keys are `Settings.notification_keys`
(503 when unset). A record failing verification is left out whole, none of its
content served, and coverage names `unavailable:notification_record/integrity`;
the other records are still served. A database error is a 500.
Rows are the contract `Notification`, one per channel: the in-app row (id = record
id; `delivered` = listed, never a read) when listed, then each external channel
(`<id>:<channel>`) at its latest event. No transport exists, so nothing is sent.
`current_object_state` is resolved at read time, never the trigger-time version:
proposals with readProposal's state (`reads.proposal`: recorded resolution plus
covering stored pauses; live states name NOT_RECHECKED),
risk pauses as `paused`/`resumed`; alerts, jobs (no store qw_app may read), missing
objects and resolver errors read `unavailable`, named in coverage.
With the user's `in_app` preference off, only safety categories (risk pauses) keep
their in-app row; preferences failing verification list as if it were off and
name `unavailable:notification_preferences/integrity` (never more than allowed).
An enabled external channel names `unavailable:delivery/<channel>:no_transport`.

`PATCH /notifications/preferences` (setNotificationPreferences, increments 4-5): any
member's own preferences (the session user; no id names another user), CSRF,
Idempotency-Key (C-03) and If-Match `"N"` (T010 form; 428 absent, 412 otherwise
stale or malformed; the openapi text says 409, as recorded for T019). There is no
read operation: nothing recorded is revision `"0"` (the domain defaults), and each
response, replays too, carries the ETag of the revision its request recorded.
Turning email or browser push on needs the session's step-up within
`Settings.step_up_window` (owner decision 4; the store checks it under the lock),
else 401 `step_up_required` as `security.require_step_up`, writing nothing;
keeping one on or turning it off needs none. Nothing is sent: no transport exists,
and `NotificationPreferences` has no field to say so (the feed's coverage does).
Detail exposure is refused (422). Stored preferences failing checks, the replay
ETag lookup included, are 409 `integrity`; a purged-key replay writes no audit.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from pydantic import Field
from qw_adapters import journal_store as js
from qw_adapters import notification_preference_store as prefs_store
from qw_adapters import notification_store as ns
from qw_adapters import pause_store, policy_store
from qw_adapters import proposal_store as pst
from qw_adapters.tenancy import TenantTx
from qw_domain import notifications as dn
from qw_domain.notifications import Category, Channel
from qw_domain.proposals import TERMINAL, ProposalState

from qw_api import reads, routes
from qw_api import security as sec
from qw_api.app import ApiError, FieldError, Settings
from qw_api.policies import Reader
from qw_api.reads import NOT_RECHECKED, Cursor, Limit, _after, _cursor, _now

router = APIRouter(prefix="/api/v1")
SORT = "seq_channel"


INTEGRITY = "unavailable:notification_record/integrity"
PREFERENCES_INTEGRITY = "unavailable:notification_preferences/integrity"
_IF_MATCH = re.compile(r'"(0|[1-9][0-9]{0,8})"')
_ZONE = r"^[A-Za-z][A-Za-z0-9_+/-]{0,63}$"  # as 0020's CHECK


def keyring(settings: Settings) -> ns.Keyring:
    if settings.notification_keys is None:
        raise ApiError(503, "notifications_unconfigured",
                       "Notification keys are not configured.")  # fmt: skip
    return settings.notification_keys


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
    keys, user = keyring(sec.settings_of(request)), str(principal.session.user_id)
    reasons = list[str]()
    prefs = dn.Preferences(in_app=False)
    with principal.tx() as tx:
        try:
            try:  # lock order: pause, then preference
                held: pause_store.Pauses | None = pause_store.load(tx)  # once
            except pause_store.PauseStoreError:
                held = None
            try:
                prefs = prefs_store.load(tx, principal.session.user_id).preferences
            except prefs_store.PreferenceStoreError as exc:
                if exc.code != "integrity":
                    raise
                # unverifiable: never list more than allowed
                reasons.append(PREFERENCES_INTEGRITY)
            reasons += [f"unavailable:delivery/{ch}:no_transport"
                        for ch in dn.EXTERNAL if prefs.enabled(ch)]  # fmt: skip
            after = None if seq is None else (seq, rank)
            rows = ns.page(tx, keys, user, limit + 1, after, prefs.in_app)
            now, views = _now(tx), dict[str, dn.CurrentView | None]()
            for r in rows[:limit]:
                if r.item is None:
                    continue
                n = r.item.notification
                if n.id not in views:
                    try:
                        views[n.id] = _resolve(tx, n, now, held)
                    except (dn.NotificationError, pst.ProposalStoreError,
                            pause_store.PauseStoreError):  # fmt: skip
                        views[n.id] = None  # never the stored, stale version
        except (ns.NotificationStoreError, prefs_store.PreferenceStoreError) as exc:
            raise ApiError(500, "internal_error", "Notifications could not be "
                           "read.") from exc  # fmt: skip
    items = []
    for r in rows[:limit]:
        if r.item is None:  # never the failing record's id, object or state
            reasons.append(INTEGRITY)
            continue
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


class PreferencesBody(routes.Body):
    """Contract `NotificationPreferences`; windows are checked by the domain."""

    in_app: bool
    email: bool
    browser_push: bool
    quiet_hours_timezone: str = Field(min_length=1, max_length=64, pattern=_ZONE)
    quiet_hours: list[Annotated[str, Field(max_length=11)]] = Field(max_length=8)
    redact_financial_values: bool


_REFUSED = {
    "step_up_required": (401, "Re-authenticate to continue."),
    "detail_exposure_unsupported": (422, "Financial values are always redacted."),
    "stale": (412, "The preferences have changed."),
    "idempotency_key_reused": (409, "Key used elsewhere."),
    "integrity": (409, "Stored notification preferences failed checks."),
}  # fmt: skip


def _refuse(code: str) -> ApiError:
    status, detail = _REFUSED[code]
    return ApiError(status, "version_mismatch" if status == 412 else code, detail)


def _preferences(body: PreferencesBody) -> dn.Preferences:
    def field(pointer: str) -> ApiError:
        error = FieldError(pointer, "invalid", "Not accepted.")
        return ApiError(400, "invalid_request", "Invalid preferences.", False, (error,))

    try:
        dn.Preferences(quiet_hours_timezone=body.quiet_hours_timezone)
    except dn.NotificationError:
        raise field("/quiet_hours_timezone") from None
    try:
        return dn.Preferences(body.in_app, body.email, body.browser_push,
                              body.quiet_hours_timezone, tuple(body.quiet_hours),
                              body.redact_financial_values)  # fmt: skip
    except dn.NotificationError as exc:
        if exc.code in _REFUSED:
            raise _refuse(exc.code) from None
        raise field("/quiet_hours") from None


@router.patch("/notifications/preferences")
def set_notification_preferences(
    body: PreferencesBody, request: Request, principal: Reader, key: routes.IdemKey,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:  # fmt: skip
    session = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        if if_match is None:
            raise ApiError(428, "if_match_required", "Send If-Match with the revision.")
        if (m := _IF_MATCH.fullmatch(if_match)) is None:
            raise _refuse("stale")
        p = _preferences(body)
        try:
            cur, wrote = prefs_store.set_preferences(
                tx, session.user_id, session.session_id, key, int(m[1]), p,
                step_up_window=sec.settings_of(request).step_up_window)  # fmt: skip
        except prefs_store.PreferenceStoreError as exc:
            if exc.code not in _REFUSED:
                raise  # database: a crash (500), never a client answer
            raise _refuse(exc.code) from None
        if wrote:  # a replay after the idempotency purge changed nothing
            routes._audit(tx, request, "notification.preferences_set",
                          session.user_id, session.user_id)  # fmt: skip
        return 200, json.dumps(_wire(cur.preferences)).encode()

    payload = {"if_match": if_match, "body": body.model_dump(mode="json")}
    response = sec.idempotent(principal, request, key, payload, run)
    try:
        with principal.tx() as tx:
            recorded = prefs_store.by_key(tx, session.user_id, key)
    except prefs_store.PreferenceStoreError as exc:
        if exc.code not in _REFUSED:
            raise
        raise _refuse(exc.code) from None
    if recorded is not None:
        response.headers["ETag"] = f'"{recorded.revision}"'
    return response


def _wire(p: dn.Preferences) -> dict[str, object]:
    return {"in_app": p.in_app, "email": p.email, "browser_push": p.browser_push,
            "quiet_hours_timezone": p.quiet_hours_timezone,
            "quiet_hours": list(p.quiet_hours),
            "redact_financial_values": p.redact_financial_values}  # fmt: skip
