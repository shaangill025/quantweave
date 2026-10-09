"""Underlying accounts, versioned source mapping and overlap consolidation (T013).

Spec §4 "Canonical boundaries" and "Idempotency and conflict resolution"; spec §2
Journey A ("map sources to real accounts"); T008 review F-04.
- An `UnderlyingAccount` is the real custody account. A `Source` is a feed reporting on
  accounts (broker API or export, aggregator, manual report, or a Yahoo-style mirror).
- A `SourceMap` links (source, source account reference) to exactly one account per
  half-open interval `[valid_from, valid_to)`. Every change appends a new revision and
  never rewrites an earlier one, so any earlier knowledge time can be replayed. An
  observation with no mapping in force is quarantined, never guessed.
- `consolidate` gives one figure per (account, item) from the most authoritative fresh
  source (`AUTHORITY`). Sources never add: a mirror of a broker report counts once,
  while two real accounts stay distinct. A fresh disagreement, or a stale source of
  higher authority than the one used that last said otherwise, is an `ItemConflict`
  that marks the account `conflicted` and blocks its sizing; nothing is averaged or
  summed. An account backed by no fresh complete snapshot is `incomplete` and blocked.
  Other accounts continue. Cash stays per account and currency.
- LIMITATIONS: precedence is one ranking per source kind for every item; spec §4's
  field-specific precedence is not implemented. Aggregator cash counts as
  broker-available; that needs per-provider qualification (T005).
Stdlib only.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from qw_domain.decimals import Money, Quantity, safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc

AUTHORITY_POLICY = "source_authority/1"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)
_REF = re.compile(r"[\x21-\x7e]{1,128}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


class ScopeError(ValueError):
    """A rejected mapping, snapshot or scope; `code` names the reason for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def check_text(pattern: re.Pattern[str], value: object, what: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ScopeError(what, f"{safe_repr(value)} is malformed")


class SourceKind(StrEnum):
    BROKER_API = "broker_api"
    BROKER_EXPORT = "broker_export"
    AGGREGATOR = "aggregator"
    MANUAL = "manual"  # a user report
    MIRROR = "mirror"  # e.g. a Yahoo portfolio: report-only


# Higher wins. Broker records are stronger evidence than user reports (spec §4).
AUTHORITY: Mapping[SourceKind, int] = MappingProxyType(
    {
        SourceKind.BROKER_API: 4,
        SourceKind.BROKER_EXPORT: 3,
        SourceKind.AGGREGATOR: 2,
        SourceKind.MANUAL: 1,
        SourceKind.MIRROR: 0,
    }
)
_BROKER_CASH = frozenset(
    {SourceKind.BROKER_API, SourceKind.BROKER_EXPORT, SourceKind.AGGREGATOR}
)

type CashBasis = Literal["broker_available", "user_reported"]
type Item = tuple[InstrumentId | None, str]  # (instrument, or None for cash; currency)
type UnitKey = tuple[InstrumentId, str]


@dataclass(frozen=True, slots=True)
class UnderlyingAccount:
    account_id: str
    tenant_id: str
    institution: str
    account_type: str
    base_currency: str

    def __post_init__(self) -> None:
        check_text(ID_PATTERN, self.account_id, "account_id")
        check_text(ID_PATTERN, self.tenant_id, "tenant_id")
        check_text(_REF, self.institution, "institution")
        check_text(_REF, self.account_type, "account_type")
        check_text(_CURRENCY, self.base_currency, "currency")


@dataclass(frozen=True, slots=True)
class Source:
    source_id: str
    tenant_id: str
    kind: SourceKind

    def __post_init__(self) -> None:
        check_text(ID_PATTERN, self.source_id, "source_id")
        check_text(ID_PATTERN, self.tenant_id, "tenant_id")
        if type(self.kind) is not SourceKind:
            raise TypeError("kind must be a SourceKind")


@dataclass(frozen=True, slots=True)
class MappedInterval:
    """The reference names `account_id` for instants in [valid_from, valid_to)."""

    account_id: str
    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        check_text(ID_PATTERN, self.account_id, "account_id")
        object.__setattr__(self, "valid_from", ensure_aware_utc(self.valid_from))
        if self.valid_to is not None:
            end = ensure_aware_utc(self.valid_to)
            object.__setattr__(self, "valid_to", end)
            if end <= self.valid_from:
                raise ScopeError("interval", f"empty interval {self!r}")

    def covers(self, at: datetime) -> bool:
        return self.valid_from <= at and (self.valid_to is None or at < self.valid_to)

    def overlaps(self, other: "MappedInterval") -> bool:
        return (self.valid_to is None or other.valid_from < self.valid_to) and (
            other.valid_to is None or self.valid_from < other.valid_to
        )


@dataclass(frozen=True, slots=True)
class MappingRevision:
    """The complete interval list for one (source, reference) from `recorded_at`."""

    source_id: str
    account_ref: str
    revision: int
    intervals: tuple[MappedInterval, ...]
    recorded_at: datetime


class SourceMap:
    """In-memory registry of accounts, sources and versioned mappings."""

    def __init__(self) -> None:
        self._accounts: dict[str, UnderlyingAccount] = {}
        self._sources: dict[str, Source] = {}
        self._revisions: dict[tuple[str, str], list[MappingRevision]] = {}
        self._recorded: datetime | None = None

    def add_account(self, account: UnderlyingAccount) -> None:
        if self._accounts.get(account.account_id, account) != account:
            raise ScopeError("duplicate", f"account {account.account_id} differs")
        self._accounts[account.account_id] = account

    def add_source(self, source: Source) -> None:
        if self._sources.get(source.source_id, source) != source:
            raise ScopeError("duplicate", f"source {source.source_id} differs")
        self._sources[source.source_id] = source

    def source(self, source_id: str) -> Source | None:
        return self._sources.get(source_id)

    def accounts(self, tenant_id: str) -> tuple[UnderlyingAccount, ...]:
        found = (a for a in self._accounts.values() if a.tenant_id == tenant_id)
        return tuple(sorted(found, key=lambda a: a.account_id))

    def revisions(
        self, source_id: str, account_ref: str
    ) -> tuple[MappingRevision, ...]:
        return tuple(self._revisions.get((source_id, account_ref), ()))

    def remap(
        self,
        source_id: str,
        account_ref: str,
        intervals: Sequence[MappedInterval],
        recorded_at: datetime,
    ) -> MappingRevision:
        """Append the next revision. Validates everything before changing anything."""
        check_text(_REF, account_ref, "account_ref")
        at = ensure_aware_utc(recorded_at)
        if self._recorded is not None and at < self._recorded:
            raise ScopeError("recorded_at", "mapping knowledge time went backwards")
        source = self._sources.get(source_id)
        if source is None:
            raise ScopeError("unknown_source", f"{safe_repr(source_id)}")
        ivs = tuple(sorted(intervals, key=lambda x: x.valid_from))
        if not ivs:
            raise ScopeError("empty", "a revision needs at least one interval")
        for n, iv in enumerate(ivs):
            target = self._accounts.get(iv.account_id)
            if target is None:
                raise ScopeError("unknown_account", iv.account_id)
            if target.tenant_id != source.tenant_id:
                raise ScopeError("tenant", f"{iv.account_id} is in another tenant")
            if n and ivs[n - 1].overlaps(iv):
                raise ScopeError("overlap", f"{ivs[n - 1]!r} and {iv!r}")
        history = self._revisions.setdefault((source_id, account_ref), [])
        rev = MappingRevision(source_id, account_ref, len(history) + 1, ivs, at)
        history.append(rev)
        self._recorded = at
        return rev

    def resolve(
        self,
        source_id: str,
        account_ref: str,
        at: datetime,
        known_as_of: datetime | None = None,
    ) -> UnderlyingAccount | None:
        """The account the reference names at `at`, by the latest revision recorded
        by `known_as_of` (all revisions when None). None means quarantine."""
        t = ensure_aware_utc(at)
        known = None if known_as_of is None else ensure_aware_utc(known_as_of)
        revs = [
            r
            for r in self._revisions.get((source_id, account_ref), ())
            if known is None or r.recorded_at <= known
        ]
        if not revs:
            return None
        for iv in revs[-1].intervals:
            if iv.covers(t):
                return self._accounts[iv.account_id]
        return None


@dataclass(frozen=True, slots=True)
class HoldingLine:
    instrument_id: InstrumentId
    currency: str
    quantity: Quantity

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not InstrumentId:
            raise TypeError("instrument_id must be an InstrumentId")
        if type(self.quantity) is not Quantity:
            raise TypeError("quantity must be a Quantity")
        check_text(_CURRENCY, self.currency, "currency")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One source's report on one account reference at `as_of`. A `complete` snapshot
    lists the whole account, so an item it omits is reported as zero; an incomplete
    one (e.g. a watch-style mirror) says nothing about items it omits."""

    source_id: str
    account_ref: str
    as_of: datetime
    holdings: tuple[HoldingLine, ...]
    cash: tuple[Money, ...]
    complete: bool

    def __post_init__(self) -> None:
        check_text(ID_PATTERN, self.source_id, "source_id")
        check_text(_REF, self.account_ref, "account_ref")
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        holdings, cash = tuple(self.holdings), tuple(self.cash)
        if not all(type(h) is HoldingLine for h in holdings):
            raise TypeError("holdings must be HoldingLine values")
        if not all(type(m) is Money for m in cash):
            raise TypeError("cash must be Money values")
        keys = [(h.instrument_id, h.currency) for h in holdings]
        if len(set(keys)) != len(keys) or len({m.currency for m in cash}) != len(cash):
            raise ScopeError("duplicate_line", "one line per item and currency")
        object.__setattr__(self, "holdings", holdings)
        object.__setattr__(self, "cash", cash)

    def items(self) -> dict[Item, Decimal]:
        found: dict[Item, Decimal] = {
            (h.instrument_id, h.currency): h.quantity.value for h in self.holdings
        }
        found.update({(None, m.currency): m.amount.value for m in self.cash})
        return found


@dataclass(frozen=True, slots=True)
class Candidate:
    source_id: str
    kind: SourceKind
    as_of: datetime
    value: Decimal
    stale: bool = False

    @property
    def rank(self) -> tuple[int, datetime, str]:
        return (AUTHORITY[self.kind], self.as_of, self.source_id)


@dataclass(frozen=True, slots=True)
class ItemConflict:
    """Sources disagree on one item. `selected` is the fresh source whose figure is
    shown; a `stale` candidate outranks it. The account is blocked until resolved."""

    account_id: str
    item: Item
    selected: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class Quarantined:
    snapshot: SourceSnapshot
    reason: Literal["unknown_source", "unmapped"]


type AccountStatus = Literal[
    "reconciled", "conflicted", "incomplete", "stale", "no_data"
]


@dataclass(frozen=True, slots=True)
class AccountView:
    account: UnderlyingAccount
    status: AccountStatus
    units: Mapping[UnitKey, Quantity]
    cash: Mapping[str, Money]
    cash_basis: Mapping[str, CashBasis]
    sources: tuple[str, ...]  # sources with a fresh snapshot
    conflicts: tuple[ItemConflict, ...]

    @property
    def sizing_blocked(self) -> bool:
        return self.status != "reconciled"


@dataclass(frozen=True, slots=True)
class Consolidation:
    accounts: Mapping[str, AccountView]
    quarantined: tuple[Quarantined, ...]


def consolidate(
    source_map: SourceMap,
    tenant_id: str,
    snapshots: Iterable[SourceSnapshot],
    as_of: datetime,
    max_age: timedelta,
    known_as_of: datetime | None = None,
) -> Consolidation:
    """Per-account holdings and cash for one tenant as of `as_of`. Snapshots after
    `as_of` are not yet known and are ignored. Per (source, reference) only the latest
    snapshot counts; one older than `max_age` is stale: it is not used, but outranks a
    lower-authority figure it disagrees with (a conflict)."""
    t = ensure_aware_utc(as_of)
    if max_age < timedelta(0):
        raise ScopeError("max_age", "must not be negative")
    quarantined: list[Quarantined] = []
    latest: dict[str, dict[tuple[str, str], list[SourceSnapshot]]] = {}
    for snap in snapshots:
        if snap.as_of > t:
            continue
        source = source_map.source(snap.source_id)
        if source is None:
            quarantined.append(Quarantined(snap, "unknown_source"))
            continue
        if source.tenant_id != tenant_id:  # fail closed: never carried into a result
            raise ScopeError("foreign_tenant", f"{snap.source_id} is another tenant's")
        acct = source_map.resolve(
            snap.source_id, snap.account_ref, snap.as_of, known_as_of
        )
        if acct is None:
            quarantined.append(Quarantined(snap, "unmapped"))
            continue
        per_source = latest.setdefault(acct.account_id, {})
        ref = (snap.source_id, snap.account_ref)
        held = per_source.get(ref, [])
        if not held or snap.as_of > held[0].as_of:
            per_source[ref] = [snap]
        elif snap.as_of == held[0].as_of:
            held.append(snap)
    accounts = {
        a.account_id: _account_view(
            source_map, a, latest.get(a.account_id, {}), t, max_age
        )
        for a in source_map.accounts(tenant_id)
    }
    return Consolidation(MappingProxyType(accounts), tuple(quarantined))


def _account_view(
    source_map: SourceMap,
    account: UnderlyingAccount,
    per_source: Mapping[tuple[str, str], list[SourceSnapshot]],
    as_of: datetime,
    max_age: timedelta,
) -> AccountView:
    snaps = [s for group in per_source.values() for s in group]
    fresh = [s for s in snaps if as_of - s.as_of <= max_age]
    if not fresh:
        status: AccountStatus = "stale" if snaps else "no_data"
        empty: MappingProxyType[str, Money] = MappingProxyType({})
        return AccountView(account, status, MappingProxyType({}), empty, {}, (), ())
    stale = [s for s in snaps if as_of - s.as_of > max_age]
    keys = sorted({k for s in snaps for k in s.items()}, key=item_order)
    units: dict[UnitKey, Quantity] = {}
    cash: dict[str, Money] = {}
    basis: dict[str, CashBasis] = {}
    conflicts: list[ItemConflict] = []
    for key in keys:
        cands = _candidates(source_map, fresh, key, False)
        if not cands:
            continue
        best = max(cands, key=lambda c: c.rank)
        outranked = [
            c
            for c in _candidates(source_map, stale, key, True)
            if AUTHORITY[c.kind] > AUTHORITY[best.kind] and c.value != best.value
        ]
        if outranked or any(c.value != best.value for c in cands):
            ordered = sorted([*cands, *outranked], key=lambda c: c.rank, reverse=True)
            conflicts.append(
                ItemConflict(account.account_id, key, best.source_id, tuple(ordered))
            )
        instrument, currency = key
        if instrument is None:
            cash[currency] = Money.of(best.value, currency)
            ok = best.kind in _BROKER_CASH
            basis[currency] = "broker_available" if ok else "user_reported"
        elif best.value:
            units[(instrument, currency)] = Quantity(best.value)
    complete = any(s.complete for s in fresh)
    return AccountView(
        account,
        "conflicted" if conflicts else "reconciled" if complete else "incomplete",
        MappingProxyType(units),
        MappingProxyType(cash),
        MappingProxyType(basis),
        tuple(sorted({s.source_id for s in fresh})),
        tuple(conflicts),
    )


def _candidates(
    source_map: SourceMap, snaps: Iterable[SourceSnapshot], key: Item, stale: bool
) -> list[Candidate]:
    """What each snapshot says about `key`; a complete snapshot's omission is 0."""
    found = []
    for s in snaps:
        items, src = s.items(), source_map.source(s.source_id)
        if src is not None and (key in items or s.complete):
            value = items.get(key, Decimal(0))
            found.append(Candidate(s.source_id, src.kind, s.as_of, value, stale))
    return found


def item_order(item: Item) -> tuple[str, str]:
    instrument, currency = item
    return ("" if instrument is None else instrument.to_wire(), currency)
