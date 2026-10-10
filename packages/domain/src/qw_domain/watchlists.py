"""Watchlists: tenant-scoped, versioned, audited research lists (T024; R014, R057;
spec §2 Journey C, §4, §13 "Watchlists and research").

- Every read and change names the tenant; another tenant's list is `not_found`.
- Every change names the expected revision (If-Match) and raises it by one; a stale
  revision is a `version_conflict` and changes nothing. Archived lists accept only
  deletion. A deleted id is never reused, so the audit trail stays unambiguous.
- A watchlist is research state only. This module does not import the journal or
  postings: removing an entry or a list cannot change holdings, cash or positions.
- `watch_demands` turns active entries into T022 stream demands: priority 1..100 is
  `user_prioritized` (rank 100 - priority, so higher first), 0 is `candidate`; an
  instrument on several lists keeps its highest priority per latency need.
In-memory; persistence is remaining work. Stdlib only.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from qw_domain.decimals import safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.monitoring import Condition
from qw_domain.sources import ID_PATTERN
from qw_domain.streams import DemandClass, FeedLatency, WatchDemand


class WatchlistError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _id(value: object, what: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise WatchlistError("malformed", f"{what} {safe_repr(value)}")


def _text(value: object, what: str) -> None:
    if not isinstance(value, str):
        raise WatchlistError("malformed", f"{what} must be a string")


@dataclass(frozen=True, slots=True)
class Entry:
    instrument_id: InstrumentId
    thesis: str
    tags: tuple[str, ...]
    review_at: datetime | None
    priority: int  # contract monitoring_priority, 0..100
    conditions: tuple[Condition, ...]
    invalidation_text: str
    needs: FeedLatency = FeedLatency.REALTIME_EXCHANGE_LIMITED  # monitoring mode

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not InstrumentId:
            raise WatchlistError("malformed", "instrument_id must be an InstrumentId")
        _text(self.thesis, "thesis")
        _text(self.invalidation_text, "invalidation_text")
        if type(self.tags) is not tuple or not all(
            isinstance(t, str) for t in self.tags
        ):
            raise WatchlistError("malformed", "tags must be a tuple of strings")
        if type(self.conditions) is not tuple or not all(
            type(c) is Condition for c in self.conditions
        ):
            raise WatchlistError("malformed", "conditions must be Conditions")
        if type(self.priority) is not int or not 0 <= self.priority <= 100:
            raise WatchlistError("malformed", "priority must be an int in 0..100")
        if not isinstance(self.needs, FeedLatency):
            raise WatchlistError("malformed", "needs must be a FeedLatency")
        if self.review_at is not None:
            object.__setattr__(self, "review_at", ensure_aware_utc(self.review_at))


@dataclass(frozen=True, slots=True)
class Watchlist:
    tenant_id: str
    watchlist_id: str
    revision: int
    name: str
    description: str
    entries: tuple[Entry, ...]  # by instrument id
    archived: bool = False

    def entry(self, instrument_id: InstrumentId) -> Entry | None:
        return next((e for e in self.entries if e.instrument_id == instrument_id), None)


@dataclass(frozen=True, slots=True)
class Who:
    """The tenant (from the session), the acting user and the request time."""

    tenant_id: str
    actor: str
    at: datetime

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant")
        _id(self.actor, "actor")
        object.__setattr__(self, "at", ensure_aware_utc(self.at))


@dataclass(frozen=True, slots=True)
class AuditRecord:
    who: Who
    watchlist_id: str
    revision: int  # the revision the action produced (deletion: the deleted one)
    action: str
    instrument_id: InstrumentId | None = None


def _name(name: object, description: object) -> None:
    if not isinstance(name, str) or not name:
        raise WatchlistError("malformed", "name must be a non-empty string")
    _text(description, "description")


class WatchlistStore:
    def __init__(self) -> None:
        self._lists: dict[tuple[str, str], Watchlist] = {}
        self._used: set[tuple[str, str]] = set()
        self._audit: list[AuditRecord] = []

    def get(self, tenant_id: str, watchlist_id: str) -> Watchlist:
        found = self._lists.get((tenant_id, watchlist_id))
        if found is None:  # also for another tenant's list: existence is not leaked
            raise WatchlistError("not_found", safe_repr(watchlist_id))
        return found

    def list(self, tenant_id: str) -> tuple[Watchlist, ...]:
        return tuple(w for (t, _), w in sorted(self._lists.items()) if t == tenant_id)

    def audit(self, tenant_id: str) -> tuple[AuditRecord, ...]:
        return tuple(a for a in self._audit if a.who.tenant_id == tenant_id)

    def create(self, who: Who, wid: str, name: str, description: str) -> Watchlist:
        _id(wid, "watchlist id")
        _name(name, description)
        if (who.tenant_id, wid) in self._used:
            raise WatchlistError("id_used", safe_repr(wid))
        w = Watchlist(who.tenant_id, wid, 1, name, description, ())
        self._lists[(who.tenant_id, wid)] = w
        self._used.add((who.tenant_id, wid))
        self._audit.append(AuditRecord(who, wid, 1, "create"))
        return w

    def _current(self, who: Who, wid: str, expected: int) -> Watchlist:
        current = self.get(who.tenant_id, wid)
        if type(expected) is not int or expected != current.revision:
            raise WatchlistError("version_conflict", f"current {current.revision}")
        return current

    def _change(
        self,
        who: Who,
        wid: str,
        expected: int,
        action: str,
        change: Callable[[Watchlist], Watchlist],
        iid: InstrumentId | None = None,
    ) -> Watchlist:
        current = self._current(who, wid, expected)
        if current.archived:
            raise WatchlistError("archived", safe_repr(wid))
        new = replace(change(current), revision=current.revision + 1)
        self._lists[(who.tenant_id, wid)] = new
        self._audit.append(AuditRecord(who, wid, new.revision, action, iid))
        return new

    def rename(
        self, who: Who, wid: str, expected: int, name: str, description: str
    ) -> Watchlist:
        _name(name, description)
        fn = lambda w: replace(w, name=name, description=description)  # noqa: E731
        return self._change(who, wid, expected, "rename", fn)

    def _put(
        self,
        who: Who,
        wid: str,
        expected: int,
        iid: InstrumentId,
        new: Entry | None,
        exists: bool,
    ) -> Watchlist:
        def fn(w: Watchlist) -> Watchlist:
            if (w.entry(iid) is not None) != exists:
                raise WatchlistError("entry_missing" if exists else "entry_exists")
            kept = [e for e in w.entries if e.instrument_id != iid]
            kept += [new] if new is not None else []
            return replace(
                w, entries=tuple(sorted(kept, key=lambda e: e.instrument_id))
            )

        action = (
            "remove_entry" if new is None else "update_entry" if exists else "add_entry"
        )
        return self._change(who, wid, expected, action, fn, iid)

    def add_entry(self, who: Who, wid: str, expected: int, e: Entry) -> Watchlist:
        return self._put(who, wid, expected, e.instrument_id, e, False)

    def update_entry(self, who: Who, wid: str, expected: int, e: Entry) -> Watchlist:
        return self._put(who, wid, expected, e.instrument_id, e, True)

    def remove_entry(
        self, who: Who, wid: str, expected: int, iid: InstrumentId
    ) -> Watchlist:
        return self._put(who, wid, expected, iid, None, True)

    def archive(self, who: Who, wid: str, expected: int) -> Watchlist:
        fn = lambda w: replace(w, archived=True)  # noqa: E731
        return self._change(who, wid, expected, "archive", fn)

    def delete(self, who: Who, wid: str, expected: int) -> None:
        current = self._current(who, wid, expected)
        self._audit.append(AuditRecord(who, wid, current.revision, "delete"))
        del self._lists[(who.tenant_id, wid)]

    def watch_demands(self, tenant_id: str) -> tuple[WatchDemand, ...]:
        best: dict[tuple[InstrumentId, FeedLatency], int] = {}
        for w in self.list(tenant_id):
            for e in () if w.archived else w.entries:
                key = (e.instrument_id, e.needs)
                best[key] = max(best.get(key, 0), e.priority)
        out = [
            WatchDemand(iid, DemandClass.USER_PRIORITY, 100 - p, "watchlist", needs)
            if p > 0
            else WatchDemand(iid, DemandClass.CANDIDATE, 0, "watchlist", needs)
            for (iid, needs), p in best.items()
        ]
        return tuple(sorted(out, key=lambda d: d.order))
