"""Watchlists (T024 increments 2 and 3; `0022_watchlists.sql`,
`0023_watchlist_entries.sql`; contract listWatchlists, createWatchlist,
readWatchlist, updateWatchlist, deleteWatchlist, addWatchlistEntry,
updateWatchlistEntry, deleteWatchlistEntry).

- Every read re-hashes each revision, checks revisions run 1..n from the create,
  each names the previous hash, times never go back and nothing follows a delete,
  and replays the chain through `qw_domain.watchlists.WatchlistStore` (create,
  rename, add/update/remove entry, delete; a tombstone repeats the last state).
  Each stored entry is decoded into the domain `Entry` and the replayed state must
  re-encode to exactly the stored snapshot (name, description, entries in domain
  order). Any failure is `integrity`. Hash-only, no C-20 signing (as pause_store):
  removing a chain's latest rows is not detected.
- Entries are stored as the contract response `WatchlistEntry` plus the domain's
  monitoring `needs`. The entry operations take a domain `Entry`; an update keeps
  the stored `review_at` and `needs` (the contract request carries neither).
- Ids are canonical UUID text; unknown, foreign, deleted and other spellings all
  read as absent (`None`, or `not_found` for a change).
- Every write is under the tenant lock and says whether it wrote: a key the
  principal already used for the same action (create: on any list) returns the
  revision it recorded when the request is the same (for an entry: the same
  instrument and editable fields), else
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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import watchlists as W
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.monitoring import Condition
from qw_domain.streams import FeedLatency

from qw_adapters.tenancy import TenantTx

__all__ = ["MAX_ENTRIES", "Revision", "WatchlistStoreError", "add_entry", "create",
           "delete", "entry_json", "load", "page", "remove_entry", "revision_hash",
           "update", "update_entry"]  # fmt: skip

MAX_ENTRIES = 500  # per list; 0023 refuses a larger snapshot
_ENTRY_ACTIONS = ("add_entry", "update_entry", "remove_entry")


class WatchlistStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Revision:
    revision: int
    action: str  # create, update, delete, add_entry, update_entry or remove_entry
    principal: uuid.UUID
    key: str
    recorded_at: datetime
    digest: str
    watchlist: W.Watchlist  # the state it recorded (a tombstone: the last live one)
    instrument: str | None = None  # the entry an entry action changed

    @property
    def deleted(self) -> bool:
        return self.action == "delete"


def revision_hash(
    tenant: uuid.UUID, wid: uuid.UUID, revision: int, action: str,
    principal: uuid.UUID, session: uuid.UUID, key: str, name: str, description: str,
    entries: list[Any], at: datetime, previous: str | None, *,
    instrument: uuid.UUID | None = None,
) -> str:  # fmt: skip
    fields = ["watchlist_revision", str(tenant), str(wid), revision, action,
              str(principal), str(session), key, name, description, entries,
              format_instant(at), previous]  # fmt: skip
    if instrument is not None:  # entry actions (0023); earlier hashes are unchanged
        fields.append(str(instrument))
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def entry_json(e: W.Entry) -> dict[str, Any]:
    """The stored form: the contract response entry plus `needs`."""
    return {"instrument_id": e.instrument_id.to_wire(), "thesis": e.thesis,
            "tags": list(e.tags),
            "review_at": None if e.review_at is None else format_instant(e.review_at),
            "monitoring_priority": e.priority,
            "conditions": [{"metric": c.metric, "operator": c.operator,
                            "values": list(c.values), "data_spec_id": c.data_spec_id,
                            "description": c.description} for c in e.conditions],
            "invalidation_text": e.invalidation_text,
            "needs": e.needs.value}  # fmt: skip


def _entry(d: dict[str, Any]) -> W.Entry:
    """Decode a stored entry; the caller re-encodes it, so lax shapes still fail."""
    at = d["review_at"]
    return W.Entry(
        InstrumentId.from_wire(d["instrument_id"]), d["thesis"], tuple(d["tags"]),
        None if at is None else datetime.fromisoformat(at), d["monitoring_priority"],
        tuple(Condition(c["metric"], c["operator"], tuple(c["values"]),
                        c["data_spec_id"], c["description"])
              for c in d["conditions"]),
        d["invalidation_text"], FeedLatency(d["needs"]),
    )  # fmt: skip


def _editable(e: W.Entry | None) -> tuple[Any, ...] | None:
    """What the contract request sets (not `review_at` or `needs`)."""
    return None if e is None else (e.instrument_id, e.thesis, e.tags, e.priority,
                                   e.conditions, e.invalidation_text)  # fmt: skip


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
           "event_hash, instrument_id FROM app.watchlist_revision WHERE "
           "tenant_id = %s{} ORDER BY watchlist_id, revision")  # fmt: skip


def _chains(
    tx: TenantTx, wid: uuid.UUID | None
) -> tuple[dict[str, list[Revision]], W.WatchlistStore]:
    """Every chain (or `wid`'s), verified, oldest revision first, and the domain
    store they replayed into."""
    where, args = ((" AND watchlist_id = %s", (tx.tenant_id, wid)) if wid else
                   ("", (tx.tenant_id,)))  # fmt: skip
    try:
        rows = [tuple(r) for r in tx.conn.execute(_SELECT.format(where), args)]
    except psycopg.Error as exc:
        raise WatchlistStoreError("database", type(exc).__name__) from None
    domain, out = W.WatchlistStore(), dict[str, list[Revision]]()
    try:
        for (
            w,
            rev,
            action,
            who,
            session,
            key,
            name,
            text,
            entries,
            at,
            prev,
            h,
            i,
        ) in rows:
            chain = out.setdefault(str(w), [])
            last, at = (chain[-1] if chain else None), ensure_aware_utc(at)
            ok = (
                rev == len(chain) + 1 and prev == (last.digest if last else None)
                and (last is None or (at >= last.recorded_at and not last.deleted))
                and (i is not None) == (action in _ENTRY_ACTIONS)
                and h == revision_hash(tx.tenant_id, w, rev, action, who, session, key,
                                       name, text, entries, at, prev, instrument=i)
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
                domain.delete(by, str(w), rev - 1)
            elif action == "remove_entry":
                state = domain.remove_entry(by, str(w), rev - 1, InstrumentId(i))
            elif action in _ENTRY_ACTIONS:
                put = domain.add_entry if action == "add_entry" else domain.update_entry
                stored = {e.instrument_id.uuid: e for e in map(_entry, entries)}
                old = last.watchlist.entry(InstrumentId(i)) if last else None
                new = stored[i]
                if (
                    action == "update_entry"
                    and old is not None
                    and ((new.review_at, new.needs) != (old.review_at, old.needs))
                ):  # the store keeps both on an update
                    raise ValueError(f"{w} revision {rev} kept fields")
                state = put(by, str(w), rev - 1, stored[i])  # a duplicate: snapshot
            else:
                raise ValueError(f"{w} action {action}")
            snapshot = (state.name, state.description, [entry_json(e) for e in
                                                         state.entries])  # fmt: skip
            if snapshot != (name, text, entries):
                raise ValueError(f"{w} snapshot {rev}")
            chain.append(Revision(rev, action, who, key, at, h, state,
                                  None if i is None else str(i)))  # fmt: skip
    except (ValueError, TypeError, KeyError, AttributeError) as exc:  # domain errors
        raise WatchlistStoreError("integrity", str(exc)) from None
    return out, domain


def load(tx: TenantTx, wid: str) -> Revision | None:
    """The live list's latest revision, verified; None when unknown or deleted."""
    if (u := _uuid(wid)) is None:
        return None
    _lock(tx, shared=True)
    chain = _chains(tx, u)[0].get(wid)
    return None if not chain or chain[-1].deleted else chain[-1]


def page(tx: TenantTx, limit: int, after: str | None) -> list[Revision]:
    """Up to `limit` live lists with ids after `after`, by id."""
    _lock(tx, shared=True)
    chains = sorted(_chains(tx, None)[0].items())
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
        first = _chains(tx, row[0])[0][str(row[0])][0]
        if (first.watchlist.name, first.watchlist.description) != (name, description):
            raise WatchlistStoreError("idempotency_key_reused", "another request")
        return first, False
    return _insert(tx, uuid.uuid4(), [], "create", principal, session, key,
                   (name, description, ()), None), True  # fmt: skip


Op = Callable[[W.WatchlistStore, W.Who, str, int], W.Watchlist]


def update(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int, name: str, description: str,
) -> tuple[Revision, bool]:  # fmt: skip
    def op(d: W.WatchlistStore, by: W.Who, w: str, n: int) -> W.Watchlist:
        return d.rename(by, w, n, name, description)

    return _change(tx, wid, "update", principal, session, key, expected, op,
                   lambda old: (old.watchlist.name, old.watchlist.description) == (
                       name, description))  # fmt: skip


def delete(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int,
) -> tuple[Revision, bool]:  # fmt: skip
    """Append the tombstone; the list's revisions stay (no erasure here)."""
    return _change(tx, wid, "delete", principal, session, key, expected,
                   lambda d, by, w, n: d.get(by.tenant_id, w),
                   lambda old: True)  # fmt: skip


def add_entry(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int | None, entry: W.Entry,
) -> tuple[Revision, bool]:  # fmt: skip
    """Add `entry` as given (the routes set `review_at` None, `needs` the default).
    `expected` None: the latest revision under the lock (the contract names no
    If-Match here). A key replays on the request alone, whatever `expected` is."""

    def op(d: W.WatchlistStore, by: W.Who, w: str, n: int) -> W.Watchlist:
        if len(d.get(by.tenant_id, w).entries) >= MAX_ENTRIES:
            raise WatchlistStoreError("entry_limit", f"at most {MAX_ENTRIES}")
        return d.add_entry(by, w, n, entry)

    return _entry_change(tx, wid, "add_entry", principal, session, key, expected,
                         entry, op)  # fmt: skip


def update_entry(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int, entry: W.Entry,
) -> tuple[Revision, bool]:  # fmt: skip
    """Replace the editable fields; the stored `review_at` and `needs` are kept."""

    def op(d: W.WatchlistStore, by: W.Who, w: str, n: int) -> W.Watchlist:
        old = d.get(by.tenant_id, w).entry(entry.instrument_id)
        keep = old or entry  # missing: the domain refuses
        e = replace(entry, review_at=keep.review_at, needs=keep.needs)
        return d.update_entry(by, w, n, e)

    return _entry_change(tx, wid, "update_entry", principal, session, key, expected,
                         entry, op)  # fmt: skip


def remove_entry(
    tx: TenantTx, wid: str, principal: uuid.UUID, session: uuid.UUID, key: str,
    expected: int, instrument: InstrumentId,
) -> tuple[Revision, bool]:  # fmt: skip
    def op(d: W.WatchlistStore, by: W.Who, w: str, n: int) -> W.Watchlist:
        return d.remove_entry(by, w, n, instrument)

    return _change(tx, wid, "remove_entry", principal, session, key, expected, op,
                   lambda old: old.instrument == instrument.to_wire(),
                   instrument.uuid)  # fmt: skip


def _entry_change(
    tx: TenantTx, wid: str, action: str, principal: uuid.UUID, session: uuid.UUID,
    key: str, expected: int | None, entry: W.Entry, op: Op,
) -> tuple[Revision, bool]:  # fmt: skip
    iid = entry.instrument_id

    def same(old: Revision) -> bool:
        return old.instrument == iid.to_wire() and _editable(
            old.watchlist.entry(iid)
        ) == _editable(entry)

    return _change(tx, wid, action, principal, session, key, expected, op, same,
                   iid.uuid)  # fmt: skip


def _change(
    tx: TenantTx, wid: str, action: str, principal: uuid.UUID, session: uuid.UUID,
    key: str, expected: int | None, op: Op, same: Callable[[Revision], bool],
    instrument: uuid.UUID | None = None,
) -> tuple[Revision, bool]:  # fmt: skip
    """`op` applies the change to the replayed domain store; `same` says whether a
    revision recorded under this key answered this request."""
    if (u := _uuid(wid)) is None:
        raise WatchlistStoreError("not_found", "no such watchlist")
    _lock(tx, shared=False)
    chains, domain = _chains(tx, u)
    chain = chains.get(wid)
    if not chain:
        raise WatchlistStoreError("not_found", "no such watchlist")
    mine = (principal, action, key)
    if (old := next((r for r in chain if (r.principal, r.action, r.key) == mine),
                    None)) is not None:  # fmt: skip
        if (action == "add_entry" or old.revision == (expected or 0) + 1) and same(old):
            return old, False
        raise WatchlistStoreError("idempotency_key_reused", "another request")
    last = chain[-1]
    if last.deleted:
        raise WatchlistStoreError("not_found", "no such watchlist")
    if expected is not None and expected != last.revision:
        raise WatchlistStoreError("stale", "not the latest revision")
    at = _now(tx)
    try:
        state = op(domain, W.Who(str(tx.tenant_id), str(principal), at), wid,
                   last.revision)  # fmt: skip
    except W.WatchlistError as exc:  # entry_exists, entry_missing
        raise WatchlistStoreError(exc.code, "refused by the domain") from None
    return _insert(tx, u, chain, action, principal, session, key,
                   (state.name, state.description, state.entries), instrument,
                   at), True  # fmt: skip


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _insert(
    tx: TenantTx, wid: uuid.UUID, chain: list[Revision], action: str,
    principal: uuid.UUID, session: uuid.UUID, key: str,
    snapshot: tuple[str, str, tuple[W.Entry, ...]], instrument: uuid.UUID | None,
    at: datetime | None = None,
) -> Revision:  # fmt: skip
    at, rev = at or _now(tx), len(chain) + 1
    name, description, entries = snapshot
    stored = [entry_json(e) for e in entries]
    previous = chain[-1].digest if chain else None
    digest = revision_hash(tx.tenant_id, wid, rev, action, principal, session, key,
                           name, description, stored, at, previous,
                           instrument=instrument)  # fmt: skip
    args: tuple[Any, ...] = (tx.tenant_id, wid, rev, action, principal, session, key,
                             name, description, Jsonb(stored), at, previous,
                             digest, instrument)  # fmt: skip
    try:
        with tx.conn.transaction():
            tx.conn.execute(
                "INSERT INTO app.watchlist_revision (tenant_id, watchlist_id, "
                "revision, action, principal_id, session_id, idempotency_key, name, "
                "description, entries, recorded_at, previous_hash, event_hash, "
                "instrument_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", args,
            )  # fmt: skip
    except psycopg.Error as exc:
        raise WatchlistStoreError("database", type(exc).__name__) from None
    got = _chains(tx, wid)[0][str(wid)][-1]
    if (got.revision, got.digest) != (rev, digest):
        raise WatchlistStoreError("integrity", "the stored revision differs")
    return got
