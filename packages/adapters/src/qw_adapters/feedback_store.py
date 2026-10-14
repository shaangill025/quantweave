"""Private feedback, consent and tombstones (T043 inc. 2; 0019; spec §11, §12).
- `load` re-hashes every row and replays it in time order through
  `ImprovementEvidence` (consent per principal and prospective, tombstones win);
  any failure is `integrity`. Hash-only, no C-20 signing (as pause_store).
- `record` is keyed by (principal, Idempotency-Key), surviving API record purges.
- `delete` (own feedback only) tombstones and DELETEs the content row, its salt
  included; `restore` removes content a restored backup still held (R052).
Times are the DB clock; lock `feedback:<tenant>` is taken last. No external call."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime
from typing import Any

from qw_domain import feedback as fb
from qw_domain.instants import ensure_aware_utc, format_instant

from qw_adapters.tenancy import TenantTx

type _Event = tuple[
    datetime, int, str, fb.Feedback | fb.ConsentGrant | fb.Tombstone | None
]


class FeedbackStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"feedback:{tx.tenant_id}",))  # fmt: skip


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _digest(*parts: object) -> str:
    text = json.dumps([None if p is None else str(p) for p in parts],
                      separators=(",", ":"))  # fmt: skip
    return hashlib.sha256(text.encode()).hexdigest()


def _record_hash(f: fb.Feedback, key: str, salt: str) -> str:
    # S1: the salt is deleted with the content, so no guess can be confirmed
    s = f.subject
    return _digest("feedback", salt, f.tenant_id, f.record_id, f.principal_id, key,
                   s.kind.value, s.object_id, s.version, s.content_hash,
                   f.category.value, f.message, format_instant(f.occurred_at),
                   format_instant(f.recorded_at))  # fmt: skip


def _grant_hash(g: fb.ConsentGrant) -> str:
    end = None if g.expires_at is None else format_instant(g.expires_at)
    return _digest("feedback_consent", g.tenant_id, g.consent_id, g.principal_id,
                   g.purpose.value, g.recipient, sorted(c.value for c in
                   g.data_categories), g.terms_version, format_instant(g.granted_at),
                   end, g.covers_prior)  # fmt: skip


def _revoke_hash(tenant: str, cid: str, who: str, at: datetime) -> str:
    return _digest("feedback_consent_revocation", tenant, cid, who, format_instant(at))


def _stone_hash(s: fb.Tombstone, who: str, record_hash: str) -> str:
    return _digest("feedback_tombstone", s.tenant_id, s.record_id, who, record_hash,
                   format_instant(s.deleted_at))  # fmt: skip


def _integrity(ok: bool, what: str) -> None:
    if not ok:
        raise FeedbackStoreError("integrity", what)


def _feedback_events(tx: TenantTx, t: str) -> list[_Event]:
    out: list[_Event] = []
    stones = {r[0]: r[1:] for r in tx.conn.execute(
        "SELECT record_id, principal_id, deleted_at, event_hash FROM "
        "app.feedback_tombstone WHERE tenant_id = %s", (tx.tenant_id,))}  # fmt: skip
    for row in tx.conn.execute(
        "SELECT f.record_id, f.principal_id, f.idempotency_key, f.recorded_at, "
        "f.record_hash, c.record_id, c.subject_kind, c.subject_id, "
        "c.subject_version, c.subject_hash, c.category, c.message, c.occurred_at, "
        "c.salt "
        "FROM app.feedback f LEFT JOIN app.feedback_content c ON c.tenant_id = "
        "f.tenant_id AND c.record_id = f.record_id WHERE f.tenant_id = %s",
        (tx.tenant_id,),
    ):  # fmt: skip
        rid, who, key, at, digest, held = row[0], str(row[1]), *row[2:6]
        if held is not None:
            f = fb.Feedback(
                tenant_id=t, record_id=rid,
                subject=fb.Subject(fb.SubjectKind(row[6]), row[7], row[8], row[9]),
                occurred_at=row[12], recorded_at=at, principal_id=who,
                category=fb.FeedbackCategory(row[10]), message=row[11],
            )  # fmt: skip
            _integrity(_record_hash(f, key, row[13]) == digest, "feedback")
            out.append((f.recorded_at, 1, rid, f))
        stone = stones.pop(rid, None)
        _integrity(held is not None or stone is not None, "content")
        if stone is not None:
            s = fb.Tombstone(t, rid, ensure_aware_utc(stone[1]))
            _integrity(str(stone[0]) == who and _stone_hash(s, who, digest) ==
                       stone[2], "tombstone")  # fmt: skip
            out.append((s.deleted_at, 3, rid, s))
    _integrity(not stones, "tombstone")
    return out


def _consent_events(tx: TenantTx, t: str) -> list[_Event]:
    out: list[_Event] = []
    owners: dict[str, str] = {}
    for row in tx.conn.execute(
        "SELECT consent_id, principal_id, purpose, recipient, data_categories, "
        "terms_version, granted_at, expires_at, covers_prior, event_hash FROM "
        "app.feedback_consent WHERE tenant_id = %s", (tx.tenant_id,),
    ):  # fmt: skip
        g = fb.ConsentGrant(t, row[0], str(row[1]), fb.Purpose(row[2]), row[3],
                            frozenset(fb.RecordKind(c) for c in row[4]), row[5],
                            row[6], row[7], row[8])  # fmt: skip
        _integrity(_grant_hash(g) == row[9] and g.covers_prior is False, "consent")
        owners[g.consent_id] = g.principal_id
        out.append((g.granted_at, 0, g.consent_id, g))
    for cid, who, at, digest in tx.conn.execute(
        "SELECT consent_id, principal_id, revoked_at, event_hash FROM "
        "app.feedback_consent_revocation WHERE tenant_id = %s", (tx.tenant_id,),
    ):  # fmt: skip
        at = ensure_aware_utc(at)
        _integrity(owners.get(cid) == str(who) and digest == _revoke_hash(
            t, cid, str(who), at), "revocation")  # fmt: skip
        out.append((at, 2, cid, None))  # a revocation
    return out


def _read(tx: TenantTx) -> fb.ImprovementEvidence:
    t, ev = str(tx.tenant_id), fb.ImprovementEvidence()
    try:
        events = _feedback_events(tx, t) + _consent_events(tx, t)
        for at, _, ident, e in sorted(events, key=lambda e: e[:3]):
            if isinstance(e, fb.ConsentGrant):
                ev.grant(e)
            elif isinstance(e, fb.Feedback):
                ev.record(e)
            elif isinstance(e, fb.Tombstone):
                ev.reapply(e)
            else:
                ev.revoke(t, ident, at)
    except (fb.FeedbackError, ValueError, TypeError) as exc:  # a forged history
        raise FeedbackStoreError("integrity", type(exc).__name__) from None
    return ev


def load(tx: TenantTx) -> fb.ImprovementEvidence:
    _lock(tx, shared=True)
    return _read(tx)


def record(
    tx: TenantTx, *, principal: uuid.UUID, key: str, subject: fb.Subject,
    category: fb.FeedbackCategory, message: str,
) -> fb.Feedback:  # fmt: skip
    """Record the principal's private feedback, or return the one this key made."""
    if "\x00" in message:
        raise FeedbackStoreError("message", "contains NUL")
    _lock(tx, shared=False)
    ev, t, who = _read(tx), str(tx.tenant_id), str(principal)
    found = tx.conn.execute(
        "SELECT record_id FROM app.feedback WHERE tenant_id = %s AND principal_id = "
        "%s AND idempotency_key = %s", (tx.tenant_id, principal, key),
    ).fetchone()  # fmt: skip
    if found is not None:
        old = ev.get(t, found[0])  # FeedbackError `deleted` once tombstoned
        assert isinstance(old, fb.Feedback)
        if (old.subject, old.category, old.message) != (subject, category, message):
            raise FeedbackStoreError("conflict", "the key was used for other feedback")
        return old
    now = _now(tx)
    f = ev.record(fb.Feedback(
        tenant_id=t, record_id=str(uuid.uuid4()), subject=subject, occurred_at=now,
        recorded_at=now, principal_id=who, category=category, message=message,
    ))  # fmt: skip
    assert isinstance(f, fb.Feedback)
    s, salt = f.subject, secrets.token_hex(32)
    _insert(tx, "feedback", record_id=f.record_id, principal_id=principal,
            idempotency_key=key, recorded_at=now,
            record_hash=_record_hash(f, key, salt))  # fmt: skip
    _insert(tx, "feedback_content", record_id=f.record_id, subject_kind=s.kind.value,
            subject_id=s.object_id, subject_version=s.version,
            subject_hash=s.content_hash, category=f.category.value,
            message=f.message, occurred_at=f.occurred_at, salt=salt)  # fmt: skip
    return f


def grant(
    tx: TenantTx, *, principal: uuid.UUID, consent_id: str, recipient: str,
    categories: frozenset[fb.RecordKind], terms_version: str,
    expires_at: datetime | None = None,
) -> fb.ConsentGrant:  # fmt: skip
    _lock(tx, shared=False)
    ev = _read(tx)
    g = ev.grant(fb.ConsentGrant(
        str(tx.tenant_id), consent_id, str(principal), fb.Purpose.SHARED_IMPROVEMENT,
        recipient, categories, terms_version, _now(tx), expires_at, False,
    ))  # fmt: skip
    _insert(tx, "feedback_consent", consent_id=g.consent_id, principal_id=principal,
            purpose=g.purpose.value, recipient=g.recipient,
            data_categories=sorted(c.value for c in g.data_categories),
            terms_version=g.terms_version, granted_at=g.granted_at,
            expires_at=g.expires_at, event_hash=_grant_hash(g))  # fmt: skip
    return g


def revoke(tx: TenantTx, *, principal: uuid.UUID, consent_id: str) -> datetime:
    _lock(tx, shared=False)
    ev, t = _read(tx), str(tx.tenant_id)
    mine = {g.consent_id: g.principal_id for g, _ in ev.consents(t)}
    if consent_id in mine and mine[consent_id] != str(principal):
        raise FeedbackStoreError("forbidden", "only its principal revokes a consent")
    at = _now(tx)
    ev.revoke(t, consent_id, at)  # not_found, revoked
    _insert(tx, "feedback_consent_revocation", consent_id=consent_id,
            principal_id=principal, revoked_at=at,
            event_hash=_revoke_hash(t, consent_id, str(principal), at))  # fmt: skip
    return at


def delete(tx: TenantTx, *, principal: uuid.UUID, record_id: str) -> fb.Tombstone:
    _lock(tx, shared=False)
    ev, t = _read(tx), str(tx.tenant_id)
    found = tx.conn.execute(
        "SELECT principal_id, record_hash FROM app.feedback WHERE tenant_id = %s AND "
        "record_id = %s", (tx.tenant_id, record_id),
    ).fetchone()  # fmt: skip
    if found is not None and found[0] != principal:
        raise FeedbackStoreError("forbidden", "only its principal deletes feedback")
    stone = ev.delete(t, record_id, _now(tx))  # not_found, deleted
    assert found is not None
    _insert_tombstone(tx, principal, stone, found[1])
    tx.conn.execute("DELETE FROM app.feedback_content WHERE tenant_id = %s AND "
                    "record_id = %s", (tx.tenant_id, record_id))  # fmt: skip
    return stone


def restore(tx: TenantTx) -> list[str]:
    """After a restore from backup: remove content any tombstone covers (R052)."""
    _lock(tx, shared=False)
    rows: list[Any] = tx.conn.execute(
        "DELETE FROM app.feedback_content c USING app.feedback_tombstone t WHERE "
        "c.tenant_id = %s AND t.tenant_id = c.tenant_id AND t.record_id = "
        "c.record_id RETURNING c.record_id", (tx.tenant_id,),
    ).fetchall()  # fmt: skip
    return sorted(r[0] for r in rows)


def _insert(tx: TenantTx, table: str, **cols: object) -> None:
    names = ", ".join(["tenant_id", *cols])  # the names are this module's literals
    marks = ", ".join(["%s"] * (len(cols) + 1))
    tx.conn.execute(f"INSERT INTO app.{table} ({names}) VALUES ({marks})",
                    (tx.tenant_id, *cols.values()))  # fmt: skip


def _insert_tombstone(
    tx: TenantTx, principal: uuid.UUID, s: fb.Tombstone, record_hash: str
) -> None:
    _insert(tx, "feedback_tombstone", record_id=s.record_id, principal_id=principal,
            deleted_at=s.deleted_at,
            event_hash=_stone_hash(s, str(principal), record_hash))  # fmt: skip
