"""Append-only journal, idempotency, corrections and materialized positions (T012).

Spec §4 "Idempotency and conflict resolution" and "Corrections and retention", F-05:
- One idempotency key (`JournalEvent.key`) maps to at most one event. Same key and
  content is a no-op; same key with other content is a conflict and never overwrites.
- The original is kept; a reversal negates it and a superseding event reposts.
  `correct` validates both before appending either, so it is all or nothing. Every
  entry keeps its `recorded_at` knowledge time, so earlier knowledge can be replayed.
- `materialize` is a pure fold over postings. Posting types, the cost convention and
  the event constructors are in `qw_domain.postings`.

`Journal.sell` and `Journal.split` derive the held position from the journal itself.
`post` also refuses a sale that would leave a negative position at its effective time;
a raw split event passed to `post` still trusts its caller's snapshot.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from qw_domain.corporate_actions import SourceRef, Split
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.postings import (
    BookAccount,
    EventKind,
    Header,
    Holding,
    JournalError,
    JournalEvent,
    SourceObservation,
    UnitAccount,
    sell,
    split,
)


class Outcome(StrEnum):
    POSTED = "posted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class Entry:
    event: JournalEvent
    recorded_at: datetime  # knowledge time


@dataclass(frozen=True, slots=True)
class Conflict:
    """A rejected record whose key is already held with other content."""

    key: tuple[str, ...]
    existing: str
    rejected: JournalEvent | SourceObservation


class ObservationLog:
    """Immutable source observations keyed by (account, source, record)."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], SourceObservation] = {}
        self.conflicts: list[Conflict] = []

    def observe(self, obs: SourceObservation) -> Outcome:
        if type(obs) is not SourceObservation:
            raise TypeError("observe takes a SourceObservation")
        prior = self._by_key.get(obs.key)
        if prior is None:
            self._by_key[obs.key] = obs
            return Outcome.POSTED
        if prior.fingerprint == obs.fingerprint:
            return Outcome.DUPLICATE
        self.conflicts.append(Conflict(obs.key, prior.fingerprint, obs))
        return Outcome.CONFLICT

    def get(self, key: tuple[str, str, str]) -> SourceObservation | None:
        return self._by_key.get(key)


class Journal:
    """Append-only financial journal. `recorded_at` is non-decreasing."""

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        self._by_key: dict[tuple[str, ...], JournalEvent] = {}
        self._by_id: dict[str, Entry] = {}
        self.conflicts: list[Conflict] = []

    def _check(
        self,
        event: JournalEvent,
        recorded_at: datetime,
        pending: Mapping[str, JournalEvent] | None = None,
    ) -> Outcome:
        """Validate `event` as if `pending` (by id) were already appended. Pure."""
        if type(event) is not JournalEvent:
            raise TypeError("post takes a JournalEvent")
        pending = pending or {}
        keyed = {**self._by_key, **{e.key: e for e in pending.values()}}
        prior = keyed.get(event.key)
        if prior is not None:
            same = prior.event_id == event.event_id
            return Outcome.DUPLICATE if same else Outcome.CONFLICT
        if self._entries and recorded_at < self._entries[-1].recorded_at:
            raise JournalError("recorded_at_order", "knowledge time moved backwards")
        for link in (event.reverses, event.supersedes):
            if link is None:
                continue
            target = pending.get(link)
            if target is None and link in self._by_id:
                target = self._by_id[link].event
            if target is None or target.account_id != event.account_id:
                raise JournalError("link_unknown", f"no event {link} in this account")
        if event.supersedes is not None and (
            ("reversal", event.account_id, event.supersedes) not in keyed
        ):
            raise JournalError("supersede_unreversed", "reverse the original first")
        if event.kind is EventKind.SELL:
            after = [*self.events(), *pending.values(), event]
            pos = materialize(after, effective_as_of=event.effective_at)
            for u in event.units:
                held = pos.holding(event.account_id, u.instrument_id).quantity
                if u.account is UnitAccount.POSITION and held.value < 0:
                    raise JournalError("short_sale_unsupported", "sale exceeds units")
        return Outcome.POSTED

    def _append(self, event: JournalEvent, recorded_at: datetime) -> None:
        entry = Entry(event, recorded_at)
        self._entries.append(entry)
        self._by_key[event.key] = event
        self._by_id[event.event_id] = entry

    def _conflict(self, event: JournalEvent) -> Outcome:
        existing = self._by_key[event.key].event_id
        self.conflicts.append(Conflict(event.key, existing, event))
        return Outcome.CONFLICT

    def post(self, event: JournalEvent, recorded_at: datetime) -> Outcome:
        recorded_at = ensure_aware_utc(recorded_at)
        outcome = self._check(event, recorded_at)
        if outcome is Outcome.CONFLICT:
            return self._conflict(event)
        if outcome is Outcome.POSTED:
            self._append(event, recorded_at)
        return outcome

    def reverse(
        self, event_id: str, source: SourceRef, recorded_at: datetime
    ) -> Outcome:
        entry = self._by_id.get(event_id)
        if entry is None:
            raise JournalError("link_unknown", f"no event {event_id}")
        return self.post(entry.event.reversal(source), recorded_at)

    def correct(self, replacement: JournalEvent, recorded_at: datetime) -> Outcome:
        """Reverse `replacement.supersedes` (sourced to the correction record) and post
        the replacement, atomically: on any error or conflict nothing is appended."""
        recorded_at = ensure_aware_utc(recorded_at)
        original = self._by_id.get(replacement.supersedes or "")
        if original is None:
            raise JournalError("link_unknown", "replacement names no known original")
        rev = original.event.reversal(replacement.source)
        first = self._check(rev, recorded_at)
        if first is Outcome.CONFLICT:
            return self._conflict(rev)
        pending = {rev.event_id: rev} if first is Outcome.POSTED else {}
        second = self._check(replacement, recorded_at, pending)
        if second is Outcome.CONFLICT:
            return self._conflict(replacement)
        for event, outcome in ((rev, first), (replacement, second)):
            if outcome is Outcome.POSTED:
                self._append(event, recorded_at)
        posted = Outcome.POSTED in (first, second)
        return Outcome.POSTED if posted else Outcome.DUPLICATE

    def holding(self, h: Header, instrument_id: InstrumentId) -> Holding:
        """The position at `h.effective_at` from current knowledge, ignoring any event
        already held under the same source record (so a re-import is a no-op)."""
        key = ("record", h.account_id, h.source.source_id, h.source.record_id)
        events = [e for e in self.events() if e.key != key]
        return materialize(events, h.effective_at).holding(h.account_id, instrument_id)

    def sell(
        self,
        h: Header,
        instrument_id: InstrumentId,
        qty: PositiveQuantity,
        price: Price,
        trade_fee: Money,
        recorded_at: datetime,
    ) -> Outcome:
        held = self.holding(h, instrument_id)
        return self.post(sell(h, qty, price, trade_fee, held), recorded_at)

    def split(self, h: Header, action: Split, recorded_at: datetime) -> Outcome:
        held = self.holding(h, action.instrument_id)
        return self.post(split(h, action, held), recorded_at)

    def reversal_of(
        self, event_id: str, known_as_of: datetime | None = None
    ) -> JournalEvent | None:
        """The reversal of `event_id`, if one was known by `known_as_of`."""
        entry = self._by_id.get(event_id)
        if entry is None:
            return None
        rev = self._by_key.get(("reversal", entry.event.account_id, event_id))
        if rev is None or known_as_of is None:
            return rev
        known = self._by_id[rev.event_id].recorded_at <= ensure_aware_utc(known_as_of)
        return rev if known else None

    def entries(self, known_as_of: datetime | None = None) -> tuple[Entry, ...]:
        if known_as_of is None:
            return tuple(self._entries)
        t = ensure_aware_utc(known_as_of)
        return tuple(e for e in self._entries if e.recorded_at <= t)

    def events(self, known_as_of: datetime | None = None) -> tuple[JournalEvent, ...]:
        return tuple(e.event for e in self.entries(known_as_of))


# ---- Materialized positions: a pure fold over postings

type AccountKey = tuple[str, str]  # (account id, currency)


@dataclass(frozen=True, slots=True)
class Positions:
    holdings: dict[tuple[str, InstrumentId], Holding]
    cash: dict[AccountKey, Money]
    realized: dict[AccountKey, Money]  # realised net P&L, gain positive

    def holding(self, account_id: str, instrument_id: InstrumentId) -> Holding:
        flat = Holding(account_id, instrument_id, Quantity(0), None)
        return self.holdings.get((account_id, instrument_id), flat)


def materialize(
    events: Iterable[JournalEvent], effective_as_of: datetime | None = None
) -> Positions:
    """Fold events (optionally only those effective by `effective_as_of`). The fold
    reads postings only, so a reversal undoes its original exactly. Income and expense
    postings (dividends, interest, fees) move cash but are not totalled here."""
    t = None if effective_as_of is None else ensure_aware_utc(effective_as_of)
    qty: dict[tuple[str, InstrumentId], Quantity] = {}
    cost: dict[tuple[str, InstrumentId], Money] = {}
    cash: dict[AccountKey, Money] = {}
    realized: dict[AccountKey, Money] = {}
    for ev in events:
        if t is not None and ev.effective_at > t:
            continue
        acct = ev.account_id
        for u in ev.units:
            if u.account is UnitAccount.POSITION:
                k = (acct, u.instrument_id)
                qty[k] = qty.get(k, Quantity(0)) + u.quantity
        for p in ev.money:
            cur = p.amount.currency
            if p.account is BookAccount.CASH:
                cash[(acct, cur)] = cash.get((acct, cur), Money.of(0, cur)) + p.amount
            elif p.account is BookAccount.REALIZED_PL:
                prior = realized.get((acct, cur), Money.of(0, cur))
                realized[(acct, cur)] = prior - p.amount
            elif p.account is BookAccount.SECURITY_COST and p.instrument_id is not None:
                k = (acct, p.instrument_id)
                cost[k] = cost.get(k, Money.of(0, cur)) + p.amount
    # A net-zero cost (e.g. a buy and its reversal) is no known cost, never zero.
    cost = {k: v for k, v in cost.items() if v.amount.value}
    holdings = {
        k: Holding(k[0], k[1], qty.get(k, Quantity(0)), cost.get(k))
        for k in qty.keys() | cost.keys()
        if qty.get(k, Quantity(0)).value or k in cost
    }
    return Positions(
        holdings,
        {k: v for k, v in cash.items() if v.amount.value},
        {k: v for k, v in realized.items() if v.amount.value},
    )
