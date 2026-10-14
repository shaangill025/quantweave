"""Watchlists (T024 increment 2; `0022_watchlists.sql`; contract listWatchlists,
createWatchlist, readWatchlist, updateWatchlist, deleteWatchlist).

- Every read re-hashes each revision, checks revisions run 1..n from the create,
  each names the previous hash, times never go back and nothing follows a delete,
  and replays the chain through `qw_domain.watchlists.WatchlistStore` (create,
  rename, delete; a tombstone repeats the last state); any failure is `integrity`.
  Entries must be empty until the entry operations exist. Hash-only, no C-20
  signing (as pause_store): removing a chain's latest rows is not detected.
- Ids are canonical UUID text; unknown, foreign, deleted and other spellings all
  read as absent (`None`, or `not_found` for a change).
- `create`, `update` and `delete` write under the tenant lock and say whether they
  wrote: a key the principal already used for the same action (create: on any
  list) returns the revision it recorded when the request is the same, else
  `idempotency_key_reused` (it survives idempotency-record purges); `stale` when
  `expected` is not the latest revision.
- `page` verifies every chain of the tenant (linear in its revisions) and pages the
  live lists by id.
Times are the DB clock. Lock `watchlist:<tenant>` (shared to read) comes after every
other lock. Research state only: nothing here reads or writes accounts or holdings.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import watchlists as W
from qw_domain.instants import ensure_aware_utc, format_instant

from qw_adapters.tenancy import TenantTx

__all__ = ["Revision", "WatchlistStoreError", "create", "delete", "load", "page",
           "revision_hash", "update"]  # fmt: skip


class WatchlistStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Revision:
    revision: int
    action: str  # create, update or delete
    principal: uuid.UUID
    key: str
    recorded_at: datetime
    digest: str
    watchlist: W.Watchlist  # the state it recorded (a tombstone: the last live one)

    @property
    def deleted(self) -> bool:
        return self.action == "delete"


def revision_hash(
    tenant: uuid.UUID, wid: uuid.UUID, revision: int, action: str,
    principal: uuid.UUID, session: uuid.UUID, key: str, name: str, description: str,
    entries: list[Any], at: datetime, previous: str | None,
) -> str:  # fmt: skip
    text = json.dumps(["watchlist_revision", str(tenant), str(wid), revision, action,
                       str(principal), str(session), key, name, description, entries,
                       format_instant(at), previous],
                      sort_keys=True, separators=(",", ":"))  # fmt: skip
    return hashlib.sha256(text.encode()).hexdigest()


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"watchlist:{tx.tenant_id}",))  # fmt: skip


def _uuid(wid: str) -> uuid.UUID | None:
    try:
        u = uuid.UUID(wid)
    except (ValueError, TypeError, AttributeError):
        return None
    return u if str(u) == wid else None


_SELECT = ("SELECT watchlist_id, revision, action, principal_id, session_id, "
           "idempotency_key, name, description, entries, recorded_at, previous_hash, "
           "event_hash FROM app.watchlist_revision WHERE tenant_id = %s{} "
           "ORDER BY watchlist_id, revision")  # fmt: skip


def _chains(tx: TenantTx, wid: uuid.UUID | None) -> dict[str, list[Revision]]:
    """Every chain (or `wid`'s), verified, oldest revision first."""
    where, args = ((" AND watchlist_id = %s", (tx.tenant_id, wid)) if wid else
                   ("", (tx.tenant_id,)))  # fmt: skip
    try:
        rows = [tuple(r) for r in tx.conn.execute(_SELECT.format(where), args)]
    except psycopg.Error as exc:
        raise WatchlistStoreError("database", type(exc).__name__) from None
    domain, out = W.WatchlistStore(), dict[str, list[Revision]]()
    try:
        for w, rev, action, who, session, key, name, text, entries, at, prev, h in rows:
            chain = out.setdefault(str(w), [])
            last, at = (chain[-1] if chain else None), ensure_aware_utc(at)
            ok = (
                rev == len(chain) + 1 and prev == (last.digest if last else None)
                and (last is None or (at >= last.recorded_at and not last.deleted))
                and entries == [] and h == revision_hash(
                    tx.tenant_id, w, rev, action, who, session, key, name, text,
                    entries, at, prev)
            )  # fmt: skip
            if not ok:
                raise ValueError(f"{w} revision {rev}")
            by = W.Who(str(tx.tenant_id), str(who), at)
            if action == "create":
                state = domain.create(by, str(w), name, text)
            elif action == "update":
                state = domain.rename(by, str(w), rev - 1, name, text)
            elif action == "delete" and last is not None:
                state = last.watchlist
                if (name, text) != (state.name, state.description):
                    raise ValueError(f"{w} tombstone {rev}")
                domain.delete(by, str(w), rev - 1)
            else:
                raise ValueError(f"{w} action {action}")
            chain.append(Revision(rev, action, who, key, at, h, state))
    except (ValueError, TypeError) as exc:  # WatchlistError too
        raise WatchlistStoreError("integrity", str(exc)) from None
    return out


def load(tx: TenantTx, wid: str) -> Revision | None:
    """The live list's latest revision, verified; None when unknown or deleted."""
    if (u := _uuid(wid)) is None:
        return None
    _lock(tx, shared=True)
    chain = _chains(tx, u).get(wid)
    return None if not chain or chain[-1].deleted else chain[-1]


def page(tx: TenantTx, limit: int, after: str | None) -> list[Revision]:
    """Up to `limit` live lists with ids after `after`, by id."""
    _lock(tx, shared=True)
    chains = sorted(_chains(tx, None).items())
    return [c[-1] for w, c in chains
            if not c[-1].deleted and (after is None or w > after)][:limit]  # fmt: skip


def create(
    tx: TenantTx, principal: uuid.UUID, session: uuid.UUID, key: str, name: str,
    description: str,
) -> tuple[Revision, bool]:  # fmt: skip
    _lock(tx, shared=False)
    row = tx.conn.execute(
        "SELECT watchlist_id FROM app.watchlist_revision WHERE tenant_id = %s AND "
        "principal_id = %s AND idempotency_key = %s AND revision = 1",
        (tx.tenant_id, principal, key),
    ).fetchone()
    if row is not None:
        first = _chains(tx, row[0])[str(row[0])][0]
        if (first.watchlist.name, first.watchlist.description) != (name, description):
            raise WatchlistStoreError("idempotency_key_reused", "another request")
        return first, False
    return _insert(tx, uuid.uuid4(), [], "create", principal, session, key, name,
                   description), True  # fmt: skip


def update(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int, name: str, description: str,
) -> tuple[Revision, bool]:  # fmt: skip
    return _change(tx, wid, "update", principal, session, key, expected,
                   (name, description))  # fmt: skip


def delete(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int,
) -> tuple[Revision, bool]:  # fmt: skip
    """Append the tombstone; the list's revisions stay (no erasure here)."""
    return _change(tx, wid, "delete", principal, session, key, expected, None)


def _change(
    tx: TenantTx, wid: str, action: str, principal: uuid.UUID, session: uuid.UUID,
    key: str, expected: int, text: tuple[str, str] | None,
) -> tuple[Revision, bool]:  # fmt: skip
    if (u := _uuid(wid)) is None:
        raise WatchlistStoreError("not_found", "no such watchlist")
    _lock(tx, shared=False)
    chain = _chains(tx, u).get(wid)
    if not chain:
        raise WatchlistStoreError("not_found", "no such watchlist")
    mine = (principal, action, key)
    if (old := next((r for r in chain if (r.principal, r.action, r.key) == mine),
                    None)) is not None:  # fmt: skip
        state = (old.watchlist.name, old.watchlist.description)
        if old.revision == expected + 1 and text in (None, state):
            return old, False
        raise WatchlistStoreError("idempotency_key_reused", "another request")
    last = chain[-1]
    if last.deleted:
        raise WatchlistStoreError("not_found", "no such watchlist")
    if expected != last.revision:
        raise WatchlistStoreError("stale", "not the latest revision")
    name, description = text or (last.watchlist.name, last.watchlist.description)
    return _insert(tx, u, chain, action, principal, session, key, name,
                   description), True  # fmt: skip


def _insert(
    tx: TenantTx, wid: uuid.UUID, chain: list[Revision], action: str,
    principal: uuid.UUID, session: uuid.UUID, key: str, name: str, description: str,
) -> Revision:  # fmt: skip
    now_row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert now_row is not None
    at, rev = ensure_aware_utc(now_row[0]), len(chain) + 1
    previous = chain[-1].digest if chain else None
    digest = revision_hash(tx.tenant_id, wid, rev, action, principal, session, key,
                           name, description, [], at, previous)  # fmt: skip
    args: tuple[Any, ...] = (tx.tenant_id, wid, rev, action, principal, session, key,
                             name, description, Jsonb([]), at, previous,
                             digest)  # fmt: skip
    try:
        with tx.conn.transaction():
            tx.conn.execute(
                "INSERT INTO app.watchlist_revision (tenant_id, watchlist_id, "
                "revision, action, principal_id, session_id, idempotency_key, name, "
                "description, entries, recorded_at, previous_hash, event_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", args,
            )  # fmt: skip
    except psycopg.Error as exc:
        raise WatchlistStoreError("database", type(exc).__name__) from None
    got = _chains(tx, wid)[str(wid)][-1]
    if (got.revision, got.digest) != (rev, digest):
        raise WatchlistStoreError("integrity", "the stored revision differs")
    return got
