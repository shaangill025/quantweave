"""Persisted notifications (T056 increment 2; `0017_notifications.sql`; spec §13).

Stores what `qw_domain.notifications.Center` decided; nothing here sends.
`record` (a repeat returns the stored original and its times), `append` (new
delivery events; a stale prefix is a no-op, else `conflict` or `illegal_event`),
`record_seen` (first open only), `load` and `page` (one user's rows; RLS for the
tenant). On load the id must be `notification_id` of the stored dedupe key, each
row's HMAC seal must match, event positions run 0..n-1 and replay through the
domain transitions in time order; else `integrity` (a page fails whole). Deleting
a delivery's last events while bypassing the triggers is not detected. The record
holds the subject at trigger time; callers resolve the current state at read
time. On any error the caller's transaction must roll back.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from qw_domain import notifications as N
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.notifications import Channel, DeliveryEvent
from qw_domain.notifications import DeliveryStatus as S

from qw_adapters.tenancy import TenantTx

__all__ = ["RANK", "NotificationStoreError", "Row", "Stored", "append", "load",
           "page", "record", "record_seen"]  # fmt: skip
RANK = {Channel.IN_APP: 0, Channel.EMAIL: 1, Channel.BROWSER_PUSH: 2}
_UNKNOWN = frozenset({"unknown", "transport_error", "unrecognised_result"})


class NotificationStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Stored:
    notification: N.Notification  # seen_at is the first open
    deliveries: tuple[N.Delivery, ...]  # external channels, email first


@dataclass(frozen=True, slots=True)
class Row:
    """One channel of one record, as the list serves it."""

    seq: int
    channel: Channel
    item: Stored


def _seal(key: bytes, *parts: object) -> str:
    body = json.dumps([format_instant(p) if isinstance(p, datetime) else p
                       for p in parts])  # fmt: skip
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def _withdrawn(e: DeliveryEvent) -> bool:
    return e.status is S.FAILED and e.code.startswith("withdrawn_") and len(e.code) > 10


def _next_ok(prev: DeliveryEvent, e: DeliveryEvent) -> bool:
    if prev.status is S.QUEUED or (prev.status is S.FAILED and prev.code == "rejected"):
        return (e.status, e.code) == (S.ATTEMPTED, "") or _withdrawn(e)
    if prev.status is S.ATTEMPTED:
        return (e.status, e.code) in ((S.DELIVERED, ""), (S.FAILED, "rejected")) or (
            e.status is S.UNKNOWN and e.code in _UNKNOWN
        )
    return False  # delivered, unknown and withdrawn are final


def _legal(d: N.Delivery, created_at: datetime) -> bool:
    ev = d.events
    if not ev or (ev[0].status, ev[0].code) != (S.QUEUED, "") or ev[0].at < created_at:
        return False
    pairs = itertools.pairwise(ev)
    return d.not_before >= created_at and all(
        b.at >= a.at and _next_ok(a, b) for a, b in pairs
    )


def _rows(tx: TenantTx, sql: str, *args: object) -> list[tuple[Any, ...]]:
    rows = tx.conn.execute(sql, args).fetchall()
    return [tuple(r) for r in rows]


_RECORD = ("SELECT id, user_id, category, object_id, version, trigger_at, "
           "created_at, listed, seal FROM app.notification WHERE tenant_id = %s "
           "AND id = %s")  # fmt: skip


def _build(tx: TenantTx, key: bytes, row: tuple[Any, ...]) -> Stored:
    nid, user, cat, obj, ver, trig, created, listed, seal = row
    tenant, user = str(tx.tenant_id), str(user)
    trig, created = ensure_aware_utc(trig), ensure_aware_utc(created)
    subject = N.Subject(N.Category(cat), obj, ver)
    if nid != N.notification_id(key, tenant, user, cat, obj, ver) or seal != _seal(
        key, "notification", tenant, nid, user, cat, obj, ver, trig, created, listed
    ):
        raise ValueError(f"{nid}: id or seal")
    events: dict[str, list[DeliveryEvent]] = {}
    for ch, pos, status, at, code, s in _rows(
        tx, "SELECT channel, position, status, at, code, seal FROM "
        "app.notification_delivery_event WHERE tenant_id = %s AND notification_id = "
        "%s ORDER BY channel, position", tx.tenant_id, nid,
    ):  # fmt: skip
        mine = events.setdefault(ch, [])
        e = DeliveryEvent(S(status), ensure_aware_utc(at), code)
        if pos != len(mine) or s != _seal(key, "event", tenant, nid, ch, pos,
                                          e.status, e.at, code):  # fmt: skip
            raise ValueError(f"{nid}/{ch}: event {pos}")
        mine.append(e)
    deliveries = []
    for ch, not_before, s in _rows(
        tx, "SELECT channel, not_before, seal FROM app.notification_delivery WHERE "
        "tenant_id = %s AND notification_id = %s", tx.tenant_id, nid,
    ):  # fmt: skip
        nb = ensure_aware_utc(not_before)
        d = N.Delivery(nid, Channel(ch), nb, tuple(events.pop(ch, ())))
        if s != _seal(key, "delivery", tenant, nid, ch, nb) or not _legal(d, created):
            raise ValueError(f"{nid}/{ch}: delivery")
        deliveries.append(d)
    seen = None
    for at, s in _rows(
        tx, "SELECT seen_at, seal FROM app.notification_seen WHERE tenant_id = %s "
        "AND notification_id = %s", tx.tenant_id, nid,
    ):  # fmt: skip
        seen = ensure_aware_utc(at)
        if s != _seal(key, "seen", tenant, nid, seen) or seen < created:
            raise ValueError(f"{nid}: seen")
    if events or N.Channel.IN_APP.value in {d.channel for d in deliveries}:
        raise ValueError(f"{nid}: events without a delivery")
    n = N.Notification(nid, tenant, user, subject, trig, created, listed, seen)
    return Stored(n, tuple(sorted(deliveries, key=lambda d: RANK[d.channel])))


def _verified(tx: TenantTx, key: bytes, rows: list[tuple[Any, ...]]) -> list[Stored]:
    try:
        return [_build(tx, key, r) for r in rows]
    except psycopg.Error as exc:
        raise NotificationStoreError("database", type(exc).__name__) from None
    except (ValueError, KeyError, TypeError) as exc:  # NotificationError too
        raise NotificationStoreError("integrity", str(exc)) from None


def _get(tx: TenantTx, key: bytes, nid: str, user: str | None) -> Stored | None:
    try:
        rows = _rows(tx, _RECORD, tx.tenant_id, nid)
    except psycopg.Error as exc:
        raise NotificationStoreError("database", type(exc).__name__) from None
    found = _verified(tx, key, rows)
    if not found or (user is not None and found[0].notification.user_id != user):
        return None
    return found[0]


def load(tx: TenantTx, key: bytes, user_id: str, nid: str) -> Stored | None:
    """The user's record, verified; None when absent or another user's."""
    return _get(tx, key, nid, user_id)


_PAGE = """SELECT n.seq, c.rank, n.id, n.user_id, n.category, n.object_id, n.version,
    n.trigger_at, n.created_at, n.listed, n.seal FROM app.notification n
CROSS JOIN (VALUES (0, 'in_app'), (1, 'email'), (2, 'browser_push')) AS c (rank, ch)
WHERE n.tenant_id = %(t)s AND n.user_id = %(u)s
  AND CASE WHEN c.rank = 0 THEN n.listed ELSE EXISTS (SELECT 1 FROM
      app.notification_delivery d WHERE d.tenant_id = n.tenant_id
      AND d.notification_id = n.id AND d.channel = c.ch) END
  AND (%(s)s::bigint IS NULL OR n.seq < %(s)s OR (n.seq = %(s)s AND c.rank > %(r)s))
ORDER BY n.seq DESC, c.rank LIMIT %(n)s"""


def page(
    tx: TenantTx, key: bytes, user_id: str, limit: int, after: tuple[int, int] | None
) -> list[Row]:
    """Up to `limit` channel rows, newest record first, after `(seq, rank)`."""
    s, r = after or (None, 0)
    args = {"t": tx.tenant_id, "u": user_id, "s": s, "r": r, "n": limit}
    try:
        rows = [tuple(x) for x in tx.conn.execute(_PAGE, args).fetchall()]
    except psycopg.Error as exc:
        raise NotificationStoreError("database", type(exc).__name__) from None
    by_id = {x.notification.id: x for x in _verified(tx, key, [r[2:] for r in rows])}
    out = []
    for seq, rank, nid, *_ in rows:
        item, ch = by_id[nid], next(c for c, k in RANK.items() if k == rank)
        if not (item.notification.listed if rank == 0 else
                ch in {d.channel for d in item.deliveries}):  # fmt: skip
            raise NotificationStoreError("integrity", f"{nid}/{ch}")
        out.append(Row(int(seq), ch, item))
    return out


def _insert_record(tx: TenantTx, key: bytes, n: N.Notification) -> int:
    s, t = n.subject, str(tx.tenant_id)
    seal = _seal(key, "notification", t, n.id, n.user_id, s.category, s.object_id,
                 s.version, n.trigger_at, n.created_at, n.listed)  # fmt: skip
    cur = tx.conn.execute(
        "INSERT INTO app.notification (tenant_id, id, user_id, category, object_id, "
        "version, trigger_at, created_at, listed, seal) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (tx.tenant_id, n.id, n.user_id, s.category.value, s.object_id, s.version,
         n.trigger_at, n.created_at, n.listed, seal),
    )  # fmt: skip
    return cur.rowcount


def _insert_event(
    tx: TenantTx, key: bytes, nid: str, ch: Channel, pos: int, e: DeliveryEvent
) -> None:
    seal = _seal(key, "event", str(tx.tenant_id), nid, ch, pos, e.status, e.at, e.code)
    tx.conn.execute(
        "INSERT INTO app.notification_delivery_event (tenant_id, notification_id, "
        "channel, position, status, at, code, seal) VALUES (%s, %s, %s, %s, %s, %s, "
        "%s, %s)", (tx.tenant_id, nid, ch.value, pos, e.status.value, e.at, e.code,
                    seal),
    )  # fmt: skip


def _writing(tx: TenantTx, key: bytes, nid: str, fn: Any) -> Stored:
    """Run `fn` in a savepoint, then read back the verified record."""
    try:
        with tx.conn.transaction():
            fn()
    except psycopg.Error as exc:
        raise NotificationStoreError("database", type(exc).__name__) from None
    got = _get(tx, key, nid, None)
    assert got is not None
    return got


def record(tx: TenantTx, key: bytes, s: N.Submitted) -> Stored:
    """Store a submitted record and its queued deliveries; a repeat returns the
    stored original unchanged."""
    n = s.notification
    if n.tenant_id != str(tx.tenant_id):
        raise NotificationStoreError("tenant", "a record of another tenant")
    if (old := _get(tx, key, n.id, None)) is not None:
        return old
    if any(d.notification_id != n.id or len(d.events) != 1
           or not _legal(d, n.created_at) for d in s.deliveries):  # fmt: skip
        raise NotificationStoreError("illegal_event", "deliveries start queued")

    def write() -> None:
        _insert_record(tx, key, n)
        for d in s.deliveries:
            tx.conn.execute(
                "INSERT INTO app.notification_delivery (tenant_id, notification_id, "
                "channel, not_before, seal) VALUES (%s, %s, %s, %s, %s)",
                (tx.tenant_id, n.id, d.channel.value, d.not_before,
                 _seal(key, "delivery", str(tx.tenant_id), n.id, d.channel,
                       d.not_before)),
            )  # fmt: skip
            _insert_event(tx, key, n.id, d.channel, 0, d.events[0])

    got = _writing(tx, key, n.id, write)
    if (got.notification, got.deliveries) != (n, s.deliveries):
        raise NotificationStoreError("integrity", "the stored record differs")
    return got


def append(tx: TenantTx, key: bytes, d: N.Delivery) -> Stored:
    """Store the events `d` gained since the stored delivery."""
    cur = _get(tx, key, d.notification_id, None)
    mine = cur.deliveries if cur else ()
    old = next((x for x in mine if x.channel is d.channel), None)
    if cur is None or old is None:
        raise NotificationStoreError("not_found", "no such delivery")
    if old.not_before == d.not_before and old.events[: len(d.events)] == d.events:
        return cur  # a stale copy
    if old.not_before != d.not_before or d.events[: len(old.events)] != old.events:
        raise NotificationStoreError("conflict", "does not extend the stored events")
    if not _legal(d, cur.notification.created_at):
        raise NotificationStoreError("illegal_event", "not a domain transition")

    def write() -> None:
        for pos in range(len(old.events), len(d.events)):
            _insert_event(tx, key, d.notification_id, d.channel, pos, d.events[pos])

    got = _writing(tx, key, d.notification_id, write)
    if next(x for x in got.deliveries if x.channel is d.channel) != d:
        raise NotificationStoreError("integrity", "the stored delivery differs")
    return got


def record_seen(
    tx: TenantTx, key: bytes, user_id: str, nid: str, at: datetime
) -> Stored:
    """The owner's explicit open at `at` (the database clock); the first one stays."""
    cur, at = _get(tx, key, nid, user_id), ensure_aware_utc(at)
    if cur is None:
        raise NotificationStoreError("not_found", "no such notification")
    if cur.notification.seen_at is not None:
        return cur
    if at < cur.notification.created_at:
        raise NotificationStoreError("time_order", "opened before it was generated")
    seal = _seal(key, "seen", str(tx.tenant_id), nid, at)
    sql = ("INSERT INTO app.notification_seen (tenant_id, notification_id, seen_at, "
           "seal) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING")  # fmt: skip
    args = (tx.tenant_id, nid, at, seal)
    return _writing(tx, key, nid, lambda: tx.conn.execute(sql, args))
