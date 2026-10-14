"""Notification preferences (T056 increment 4; `0020_notification_preferences.sql`;
contract setNotificationPreferences).

- `load` re-hashes every revision of the user, checks revisions run 1..n, each
  names the previous hash, times never go back, email and push are off, and builds
  `qw_domain.notifications.Preferences`; any failure is `integrity`. No revision is
  revision 0, the domain defaults (in-app only). Hash-only, no C-20 signing (as
  pause_store), so a writer that recomputes hashes is stopped only by the domain.
- `set_preferences` appends revision `expected + 1` under the user's lock and says
  whether it wrote (False for a key's same-request replay): `stale`
  when `expected` is not the latest; `external_channel_unavailable` for email or
  browser push (an open owner decision: step-up, and no transport exists); a key
  already recorded returns its revision when the request is the same, else
  `idempotency_key_reused` (it survives idempotency-record purges).
- `by_key` is the revision a key recorded (the ETag of a replayed response).
Times are the DB clock. Lock `notification_preference:<tenant>:<user>` (shared to
load) comes after `risk_pause:<tenant>`. Nothing here sends.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from qw_domain import notifications as N
from qw_domain.instants import ensure_aware_utc, format_instant

from qw_adapters.tenancy import TenantTx

__all__ = ["DEFAULT", "Current", "PreferenceStoreError", "by_key", "load",
           "revision_hash", "set_preferences"]  # fmt: skip


class PreferenceStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Current:
    revision: int  # 0: nothing recorded, the domain defaults
    preferences: N.Preferences
    recorded_at: datetime | None


DEFAULT = Current(0, N.Preferences(), None)


def revision_hash(
    tenant: uuid.UUID, user: uuid.UUID, revision: int, session: uuid.UUID, key: str,
    in_app: bool, email: bool, push: bool, timezone: str, quiet: list[str],
    redact: bool, at: datetime, previous: str | None,
) -> str:  # fmt: skip
    text = json.dumps(["notification_preference", str(tenant), str(user), revision,
                       str(session), key, in_app, email, push, timezone, quiet,
                       redact, format_instant(at), previous],
                      separators=(",", ":"))  # fmt: skip
    return hashlib.sha256(text.encode()).hexdigest()


def _lock(tx: TenantTx, user: uuid.UUID, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"notification_preference:{tx.tenant_id}:{user}",))  # fmt: skip


_SELECT = ("SELECT revision, session_id, idempotency_key, in_app, email, "
           "browser_push, quiet_hours_timezone, quiet_hours, redact_financial_values, "
           "recorded_at, previous_hash, event_hash FROM app.notification_preference "
           "WHERE tenant_id = %s AND user_id = %s ORDER BY revision")  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Revision:
    key: str
    digest: str
    current: Current


def _read(tx: TenantTx, user: uuid.UUID) -> list[_Revision]:
    """Every revision, verified, oldest first."""
    try:
        rows = [tuple(r) for r in tx.conn.execute(_SELECT, (tx.tenant_id, user))]
    except psycopg.Error as exc:
        raise PreferenceStoreError("database", type(exc).__name__) from None
    out: list[_Revision] = []
    try:
        for (
            rev,
            session,
            key,
            in_app,
            email,
            push,
            tz,
            quiet,
            redact,
            at,
            prev,
            h,
        ) in rows:
            at, last = ensure_aware_utc(at), out[-1] if out else None
            ok = (
                rev == len(out) + 1
                and prev == (last.digest if last else None)
                and (last is None or at >= (last.current.recorded_at or at))
                and email is False and push is False and type(quiet) is list
                and h == revision_hash(tx.tenant_id, user, rev, session, key, in_app,
                                       email, push, tz, quiet, redact, at, prev)
            )  # fmt: skip
            if not ok:
                raise ValueError(f"revision {rev}")
            p = N.Preferences(in_app, email, push, tz, tuple(quiet), redact)
            out.append(_Revision(key, h, Current(rev, p, at)))
    except (ValueError, TypeError) as exc:  # NotificationError too
        raise PreferenceStoreError("integrity", str(exc)) from None
    return out


def load(tx: TenantTx, user_id: uuid.UUID) -> Current:
    """The user's preferences in force, verified; DEFAULT when none are recorded."""
    _lock(tx, user_id, shared=True)
    found = _read(tx, user_id)
    return found[-1].current if found else DEFAULT


def by_key(tx: TenantTx, user_id: uuid.UUID, key: str) -> Current | None:
    _lock(tx, user_id, shared=True)
    return next((r.current for r in _read(tx, user_id) if r.key == key), None)


def set_preferences(
    tx: TenantTx, user_id: uuid.UUID, session_id: uuid.UUID, key: str,
    expected: int, p: N.Preferences,
) -> tuple[Current, bool]:  # fmt: skip
    """Record `p` as revision `expected + 1` (True), return the key's own earlier
    revision for the same request (False), or refuse and write nothing."""
    if p.email or p.browser_push:
        raise PreferenceStoreError("external_channel_unavailable",
                                   "email and browser push stay off")  # fmt: skip
    _lock(tx, user_id, shared=False)
    found = _read(tx, user_id)
    if (old := next((r.current for r in found if r.key == key), None)) is not None:
        if (old.revision, old.preferences) == (expected + 1, p):
            return old, False
        raise PreferenceStoreError("idempotency_key_reused", "another request")
    latest = found[-1] if found else None
    if expected != (latest.current.revision if latest else 0):
        raise PreferenceStoreError("stale", "not the latest revision")
    now_row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert now_row is not None
    at, rev, quiet = ensure_aware_utc(now_row[0]), expected + 1, list(p.quiet_hours)
    previous = latest.digest if latest else None
    digest = revision_hash(tx.tenant_id, user_id, rev, session_id, key, p.in_app,
                           False, False, p.quiet_hours_timezone, quiet,
                           p.redact_financial_values, at, previous)  # fmt: skip
    args: tuple[Any, ...] = (
        tx.tenant_id, user_id, rev, session_id, key, p.in_app, False, False,
        p.quiet_hours_timezone, quiet, p.redact_financial_values, at, previous,
        digest,
    )  # fmt: skip
    try:
        with tx.conn.transaction():
            tx.conn.execute(
                "INSERT INTO app.notification_preference (tenant_id, user_id, "
                "revision, session_id, idempotency_key, in_app, email, browser_push, "
                "quiet_hours_timezone, quiet_hours, redact_financial_values, "
                "recorded_at, previous_hash, event_hash) VALUES (%s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s)", args,
            )  # fmt: skip
    except psycopg.Error as exc:
        raise PreferenceStoreError("database", type(exc).__name__) from None
    got = _read(tx, user_id)[-1].current
    if got != Current(rev, p, at):
        raise PreferenceStoreError("integrity", "the stored revision differs")
    return got, True
