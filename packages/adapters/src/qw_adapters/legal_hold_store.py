"""Durable legal holds (`0058_legal_hold.sql`; spec §12; owner decisions 9, 22 and
31; qw_domain.retention Holds; R052, R086, R088).

- One append-only event chain per tenant. `place` and `release` run the domain
  `Holds.place` / `Holds.release` on the verified stored holds at the database
  clock under the tenant's 'legal_hold:<tenant>' lock; the guard stamps
  `recorded_at` (the event's time) and seals `row_hash`. The caller (the hold
  route in qw_app, not built yet) must verify roles and step-up receipts; the
  domain enforces only their combination. Placing the same hold again returns
  the stored one with its first time (`wrote` False); a different one is
  `conflicting_duplicate`.
- `load(tx, solo)` takes the lock shared to the end of the transaction (a hold
  placed concurrently waits for a deletion that read the holds) and replays every
  event: entry hash, seal, time order and chain link, then the domain rules, so
  a row the domain refuses is `integrity`. Callers treat any failure as "hold
  state unknown": no deletion runs. One corrupt row blocks every hold action and
  every deletion of its tenant until owner decision 115's quarantine step exists.
- Trust. The entry hash is unkeyed and computed here, so it detects edits only by
  a writer that cannot compute it (the superuser editing rows, storage faults).
  qw_app supplies every hashed input and can append a well-sealed release with
  fabricated step-up receipts; the store checks only the domain's combination
  rules. Verifying step-up receipts is the hold route's job (the trust boundary).
- Owner decision 113: `release` also calls `resume_suspended` in its transaction,
  which enqueues a fresh `retention.delete` job for each request of the tenant
  whose latest job stopped `deletion_suspended` before the latest release:
  revision `request:<id>:r<release seq>`, the same payload, so the original
  request time stands. The deletion job's sweep calls it too (the backstop).
  The operator alert for a request suspended past a stated period is not built
  (no alert channel exists).
- Errors: `forbidden` (only qw_app writes; qw_app and qw_worker read),
  `database`, `integrity`, `isolation` (`load` outside READ COMMITTED: a
  snapshot taken before the lock could miss a committed place), and the domain's
  codes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Any

import psycopg
from qw_domain import retention as rt
from qw_domain.account_wire import seal
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.jobs import Priority
from qw_domain.sources import ID_PATTERN

from qw_adapters import jobs as qj
from qw_adapters.tenancy import TenantTx

__all__ = ["DELETION_KIND", "HoldStoreError", "load", "place", "release",
           "resume_suspended"]  # fmt: skip
DELETION_KIND = "retention.delete"
_SUSPENDED = (
    "WITH rel AS (SELECT seq, recorded_at FROM app.legal_hold_event WHERE "
    "tenant_id = %(t)s AND kind = 'release' ORDER BY seq DESC LIMIT 1), "
    "last AS (SELECT DISTINCT ON (payload ->> 'request_id') payload, state, "
    "last_error, updated_at FROM app.job WHERE tenant_id = %(t)s AND kind = %(k)s "
    "AND payload ->> 'trigger' = 'request' "
    "ORDER BY payload ->> 'request_id', created_at DESC, id) "
    "SELECT rel.seq, last.payload FROM rel, last WHERE last.state = 'failed' "
    "AND last.last_error IN ('deletion_suspended', 'deletion_refused') AND "
    "last.updated_at <= "
    "rel.recorded_at ORDER BY last.payload ->> 'request_id'"
)

_COLS = ("seq", "kind", "hold_id", "reason", "authority_id", "classes",
         "record_ids", "placed_by", "principal_id", "approval_roles",
         "approval_principals", "approval_receipts", "approval_step_ups", "solo",
         "prev_hash", "entry_hash", "recorded_at", "row_hash")  # fmt: skip
_SELECT = (f"SELECT {', '.join(_COLS)} FROM app.legal_hold_event "
           "WHERE tenant_id = %s ORDER BY seq")  # fmt: skip


class HoldStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _database(exc: psycopg.Error) -> HoldStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return HoldStoreError("forbidden", type(exc).__name__)
    return HoldStoreError("database", type(exc).__name__)


def _entry(tenant: str, r: dict[str, Any]) -> str:
    """sha256 over compact JSON of the stored columns in a fixed order."""
    ups = [None if u is None else format_instant(ensure_aware_utc(u))
           for u in r["approval_step_ups"]]  # fmt: skip
    body = ["qw-legal-hold-1", r["seq"], tenant, r["kind"], r["hold_id"],
            r["reason"], r["authority_id"], list(r["classes"]),
            list(r["record_ids"]), r["placed_by"], r["principal_id"],
            list(r["approval_roles"]), list(r["approval_principals"]),
            list(r["approval_receipts"]), ups, r["solo"],
            r["prev_hash"]]  # fmt: skip
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def _event(r: dict[str, Any], tenant: str) -> rt.LegalHold | rt.HoldRelease:
    at = ensure_aware_utc(r["recorded_at"])
    if r["kind"] == "place":
        return rt.LegalHold(r["hold_id"], tenant, frozenset(
            rt.RetentionClass(c) for c in r["classes"]), frozenset(r["record_ids"]),
            r["reason"], r["authority_id"], at, rt.HoldRole(r["placed_by"]),
            r["principal_id"])  # fmt: skip
    approvals = tuple(rt.Approval(rt.HoldRole(role), who, receipt, up)
                      for role, who, receipt, up in zip(
                          r["approval_roles"], r["approval_principals"],
                          r["approval_receipts"], r["approval_step_ups"],
                          strict=True))  # fmt: skip
    return rt.HoldRelease(r["hold_id"], tenant, r["authority_id"], r["reason"], at,
                          approvals, r["solo"])  # fmt: skip


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    try:
        tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                        (f"legal_hold:{tx.tenant_id}",))  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None


def _load(tx: TenantTx, solo: rt.SoloRelease | None) -> tuple[rt.Holds, str, int]:
    """(the replayed holds, the head entry hash, the next seq)."""
    tenant = str(tx.tenant_id)
    try:
        rows = [dict(zip(_COLS, r, strict=True))
                for r in tx.conn.execute(_SELECT, (tx.tenant_id,))]  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None
    holds, prev, last = rt.Holds(solo=solo), rt.GENESIS, None
    try:
        for n, r in enumerate(rows):
            at = ensure_aware_utc(r["recorded_at"])
            if (r["seq"] != n or r["prev_hash"] != prev
                    or r["entry_hash"] != _entry(tenant, r)
                    or r["row_hash"] != seal(r["entry_hash"], at)
                    or (last is not None and at < last)):  # fmt: skip
                raise ValueError(f"event {n}")
            e = _event(r, tenant)
            if isinstance(e, rt.LegalHold):
                holds.place(e)
            else:
                holds.release(e)
            prev, last = r["entry_hash"], at
    except (rt.RetentionError, ValueError, TypeError, KeyError) as exc:
        raise HoldStoreError("integrity", str(exc)) from None
    return holds, prev, len(rows)


def load(tx: TenantTx, solo: rt.SoloRelease | None = None) -> rt.Holds:
    """The tenant's verified holds; `solo` is the deployment's decision-31 opt-in."""
    try:  # a snapshot older than the lock could miss a committed place
        level = tx.conn.execute("SHOW transaction_isolation").fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    if level is None or level[0] != "read committed":
        raise HoldStoreError("isolation", "load needs READ COMMITTED")
    _lock(tx, shared=True)
    return _load(tx, solo)[0]


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _insert(tx: TenantTx, r: dict[str, Any],
            solo: rt.SoloRelease | None) -> rt.LegalHold | rt.HoldRelease:  # fmt: skip
    """Insert one event, then replay the stored chain: the stored event, with the
    database's time."""
    r["entry_hash"] = _entry(str(tx.tenant_id), r)
    cols = ["tenant_id", *r]
    try:
        tx.conn.execute(
            f"INSERT INTO app.legal_hold_event ({', '.join(cols)}) VALUES "
            f"({', '.join(f'%({k})s' for k in cols)})", {"tenant_id": tx.tenant_id,
                                                          **r})  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None
    return _load(tx, solo)[0].history(str(tx.tenant_id))[-1]


def _domain(exc: rt.RetentionError) -> HoldStoreError:
    return HoldStoreError(exc.code, "refused by the domain")


def place(tx: TenantTx, hold_id: str, classes: Iterable[rt.RetentionClass],
          record_ids: Iterable[str], reason: str, authority_id: str,
          placed_by: rt.HoldRole, principal_id: str,
          solo: rt.SoloRelease | None = None) -> tuple[rt.LegalHold, bool]:  # fmt: skip
    """Place a hold at the database clock: (the stored hold, wrote)."""
    _lock(tx, shared=False)
    holds, prev, seq = _load(tx, solo)
    tenant = str(tx.tenant_id)
    try:
        h = rt.LegalHold(hold_id, tenant, frozenset(classes), frozenset(record_ids),
                         reason, authority_id, _now(tx), placed_by,
                         principal_id)  # fmt: skip
        old = next((e for e in holds.history(tenant) if isinstance(e, rt.LegalHold)
                    and e.hold_id == hold_id), None)  # fmt: skip
        if old is not None:  # a retry keeps the first placing time
            if replace(h, placed_at=old.placed_at) != old:
                raise rt.RetentionError("conflicting_duplicate", hold_id)
            return old, False
        h = holds.place(h)
    except rt.RetentionError as exc:
        raise _domain(exc) from None
    except TypeError:
        raise HoldStoreError("argument", "hold scope") from None
    stored = _insert(tx, {"seq": seq, "kind": "place", "hold_id": h.hold_id,
                 "reason": h.reason, "authority_id": h.authority_id,
                 "classes": sorted(c.value for c in h.classes),
                 "record_ids": sorted(h.record_ids), "placed_by": h.placed_by.value,
                 "principal_id": h.principal_id, "approval_roles": [],
                 "approval_principals": [], "approval_receipts": [],
                 "approval_step_ups": [], "solo": False,
                 "prev_hash": prev}, solo)  # fmt: skip
    assert isinstance(stored, rt.LegalHold)
    return stored, True


def release(tx: TenantTx, hold_id: str, authority_id: str, reason: str,
            approvals: tuple[rt.Approval, ...], solo_release: bool = False,
            solo: rt.SoloRelease | None = None) -> rt.HoldRelease:  # fmt: skip
    """Release a hold at the database clock (owner decisions 22 and 31)."""
    _lock(tx, shared=False)
    holds, prev, seq = _load(tx, solo)
    try:
        r = holds.release(rt.HoldRelease(hold_id, str(tx.tenant_id), authority_id,
                                         reason, _now(tx), approvals,
                                         solo_release))  # fmt: skip
    except rt.RetentionError as exc:
        raise _domain(exc) from None
    stored = _insert(tx, {"seq": seq, "kind": "release", "hold_id": hold_id,
                 "reason": reason, "authority_id": authority_id, "classes": [],
                 "record_ids": [], "placed_by": None, "principal_id": None,
                 "approval_roles": [a.role.value for a in r.approvals],
                 "approval_principals": [a.principal_id for a in r.approvals],
                 "approval_receipts": [a.step_up_receipt_id for a in r.approvals],
                 "approval_step_ups": [a.stepped_up_at for a in r.approvals],
                 "solo": r.solo, "prev_hash": prev}, solo)  # fmt: skip
    assert isinstance(stored, rt.HoldRelease)
    resume_suspended(tx)
    return stored


def resume_suspended(tx: TenantTx) -> list[Any]:
    """Owner decision 113: re-queue every suspended deletion request of `tx`'s
    tenant after its latest release; the ids of newly queued jobs. A request that
    ended `deletion_refused` is re-queued too: a refused record outranks a held one
    in the job's outcome, so the held record would otherwise never be erased. The
    re-run re-checks holds and retries refused records. Idempotent per (request,
    release). Takes no lock beyond the job rows it inserts."""
    out = []
    try:
        rows = tx.conn.execute(_SUSPENDED, {"t": tx.tenant_id,
                                            "k": DELETION_KIND}).fetchall()  # fmt: skip
        for seq, body in rows:
            rid = body.get("request_id") if isinstance(body, dict) else None
            if not isinstance(rid, str) or ID_PATTERN.fullmatch(rid) is None:
                continue  # a forged payload: its own job fails payload_invalid
            jid, created = qj.enqueue(tx, DELETION_KIND, f"request:{rid}:r{seq}",
                                      Priority.SAFETY, body)  # fmt: skip
            if created:
                out.append(jid)
    except psycopg.Error as exc:
        raise _database(exc) from None
    except qj.IdempotencyConflict:
        raise HoldStoreError("conflicting_duplicate", "a resumed job") from None
    return out
