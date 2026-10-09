"""Underlying accounts, source mappings, consolidation and portfolio scopes (T013).

Spec §4 "Canonical boundaries", "Idempotency and conflict resolution", "Cash and
commitments" and "Options and outside-coverage holdings"; spec §2 Journey A, ONB01 and
ONB13; T008 review F-04.
- An `UnderlyingAccount` is the real custody account. A `Source` is a feed reporting on
  accounts (broker API or export, aggregator, manual report, or a Yahoo-style mirror).
- A `SourceMap` links (source, source account reference) to exactly one account per
  half-open interval `[valid_from, valid_to)`. Every change appends a new revision and
  never rewrites an earlier one, so any earlier knowledge time can be replayed. An
  observation with no mapping in force is quarantined, never guessed.
- `consolidate` gives one figure per (account, item) from the most authoritative fresh
  source (`AUTHORITY`). Sources never add: a mirror of a broker report counts once,
  while two real accounts stay distinct. Any fresh disagreement is an explicit
  `ItemConflict` that marks the account `conflicted` and blocks its sizing; the
  authoritative figure is shown, nothing is averaged or summed. Other accounts continue.
- `apply_scope` selects accounts (ONB01) and reports exposure per (instrument,
  currency) with no FX. Cash and sleeves stay per account and currency: there is no
  cross-account or cross-currency spendability (ONB13, R043), and a sleeve allocates a
  purpose budget, never custody (R010). Completeness is a result field (R042).
Stdlib only.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from qw_domain.decimals import Money, Quantity, safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc

AUTHORITY_POLICY = "source_authority/1"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", re.ASCII)
_REF = re.compile(r"[\x21-\x7e]{1,128}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


class ScopeError(ValueError):
    """A rejected mapping, snapshot or scope; `code` names the reason for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _check(pattern: re.Pattern[str], value: object, what: str) -> None:
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
        _check(_ID, self.account_id, "account_id")
        _check(_ID, self.tenant_id, "tenant_id")
        _check(_REF, self.institution, "institution")
        _check(_REF, self.account_type, "account_type")
        _check(_CURRENCY, self.base_currency, "currency")


@dataclass(frozen=True, slots=True)
class Source:
    source_id: str
    tenant_id: str
    kind: SourceKind

    def __post_init__(self) -> None:
        _check(_ID, self.source_id, "source_id")
        _check(_ID, self.tenant_id, "tenant_id")
        if type(self.kind) is not SourceKind:
            raise TypeError("kind must be a SourceKind")


@dataclass(frozen=True, slots=True)
class MappedInterval:
    """The reference names `account_id` for instants in [valid_from, valid_to)."""

    account_id: str
    valid_from: datetime
    valid_to: datetime | None = None

    def __post_init__(self) -> None:
        _check(_ID, self.account_id, "account_id")
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

    def account(self, account_id: str) -> UnderlyingAccount | None:
        return self._accounts.get(account_id)

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
        _check(_REF, account_ref, "account_ref")
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
        _check(_CURRENCY, self.currency, "currency")


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
        _check(_ID, self.source_id, "source_id")
        _check(_REF, self.account_ref, "account_ref")
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

    @property
    def rank(self) -> tuple[int, datetime, str]:
        return (AUTHORITY[self.kind], self.as_of, self.source_id)


@dataclass(frozen=True, slots=True)
class ItemConflict:
    """Fresh sources disagree on one item. `selected` is the authoritative source
    whose figure is shown; the account is blocked for sizing until resolved."""

    account_id: str
    item: Item
    selected: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class Quarantined:
    snapshot: SourceSnapshot
    reason: Literal["unknown_source", "unmapped"]


type AccountStatus = Literal["reconciled", "conflicted", "stale", "no_data"]


@dataclass(frozen=True, slots=True)
class AccountView:
    account: UnderlyingAccount
    status: AccountStatus
    units: Mapping[UnitKey, Quantity]
    cash: Mapping[str, Money]
    cash_basis: Mapping[str, CashBasis]
    sources: tuple[str, ...]  # sources whose fresh snapshot was considered
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
    `as_of` are not yet known and are ignored. Per source only its latest snapshot
    counts; a snapshot older than `max_age` is stale and does not vote."""
    t = ensure_aware_utc(as_of)
    if max_age < timedelta(0):
        raise ScopeError("max_age", "must not be negative")
    quarantined: list[Quarantined] = []
    latest: dict[str, dict[str, list[SourceSnapshot]]] = {}
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
        held = per_source.get(snap.source_id, [])
        if not held or snap.as_of > held[0].as_of:
            per_source[snap.source_id] = [snap]
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
    per_source: Mapping[str, list[SourceSnapshot]],
    as_of: datetime,
    max_age: timedelta,
) -> AccountView:
    snaps = [s for group in per_source.values() for s in group]
    fresh = [s for s in snaps if as_of - s.as_of <= max_age]
    if not fresh:
        status: AccountStatus = "stale" if snaps else "no_data"
        empty: MappingProxyType[str, Money] = MappingProxyType({})
        return AccountView(account, status, MappingProxyType({}), empty, {}, (), ())
    reported = [(s, source_map.source(s.source_id), s.items()) for s in fresh]
    keys = sorted({k for _, _, items in reported for k in items}, key=_item_order)
    units: dict[UnitKey, Quantity] = {}
    cash: dict[str, Money] = {}
    basis: dict[str, CashBasis] = {}
    conflicts: list[ItemConflict] = []
    for key in keys:
        cands = [
            Candidate(s.source_id, src.kind, s.as_of, items.get(key, Decimal(0)))
            for s, src, items in reported
            if src is not None and (key in items or s.complete)
        ]
        best = max(cands, key=lambda c: c.rank)
        if any(c.value != best.value for c in cands):
            ordered = tuple(sorted(cands, key=lambda c: c.rank, reverse=True))
            conflicts.append(
                ItemConflict(account.account_id, key, best.source_id, ordered)
            )
        instrument, currency = key
        if instrument is None:
            cash[currency] = Money.of(best.value, currency)
            ok = best.kind in _BROKER_CASH
            basis[currency] = "broker_available" if ok else "user_reported"
        elif best.value:
            units[(instrument, currency)] = Quantity(best.value)
    return AccountView(
        account,
        "conflicted" if conflicts else "reconciled",
        MappingProxyType(units),
        MappingProxyType(cash),
        MappingProxyType(basis),
        tuple(sorted({s.source_id for s in fresh})),
        tuple(conflicts),
    )


def _item_order(item: Item) -> tuple[str, str]:
    instrument, currency = item
    return ("" if instrument is None else instrument.to_wire(), currency)


# ---- Scopes and sleeves (ONB01, ONB13)


class ScopeKind(StrEnum):
    SELECTED = "selected_accounts"
    ALL_DISCLOSED = "all_disclosed_accounts"
    HYPOTHETICAL = "hypothetical_only"


class OutsideContext(StrEnum):
    """ONB20 outside context. Unknown outside exposure is never zero (R042)."""

    NONE = "none_declared"
    SUMMARIZED = "summarized_unverified"
    UNKNOWN = "unknown"


_OUTSIDE_GAP = {
    OutsideContext.SUMMARIZED: "outside assets summarized without verification",
    OutsideContext.UNKNOWN: "outside exposure unknown, not zero",
}


@dataclass(frozen=True, slots=True)
class Sleeve:
    """A purpose budget inside one account and currency. Not a transfer or custody."""

    sleeve_id: str
    account_id: str
    currency: str
    purpose: str
    allocation: Money

    def __post_init__(self) -> None:
        _check(_ID, self.sleeve_id, "sleeve_id")
        _check(_ID, self.account_id, "account_id")
        _check(_ID, self.purpose, "purpose")
        if type(self.allocation) is not Money:
            raise TypeError("allocation must be Money")
        if self.allocation.currency != self.currency:
            raise ScopeError("currency", "allocation currency differs from the sleeve")
        if self.allocation.amount.value < 0:
            raise ScopeError("allocation", "must not be negative")


@dataclass(frozen=True, slots=True)
class PortfolioScope:
    kind: ScopeKind
    account_ids: frozenset[str]  # SELECTED only
    outside: OutsideContext
    sleeves: tuple[Sleeve, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is ScopeKind.HYPOTHETICAL and (self.account_ids or self.sleeves):
            raise ScopeError(
                "hypothetical", "a hypothetical scope has no real accounts"
            )
        if (self.kind is ScopeKind.SELECTED) != bool(self.account_ids):
            raise ScopeError("account_ids", "only a selected scope lists accounts")
        ids = [s.sleeve_id for s in self.sleeves]
        if len(set(ids)) != len(ids):
            raise ScopeError("duplicate", "sleeve ids must be unique")
        if self.kind is ScopeKind.SELECTED and any(
            s.account_id not in self.account_ids for s in self.sleeves
        ):
            raise ScopeError(
                "sleeve_account", "a sleeve's account is outside the scope"
            )


type AnalysisScope = Literal["complete_declared", "partial_declared", "hypothetical"]


@dataclass(frozen=True, slots=True)
class ScopeView:
    scope: PortfolioScope
    accounts: Mapping[str, AccountView]
    excluded: tuple[str, ...]
    # Consolidated exposure is informative only; it is never spendable (spec §4).
    exposure: Mapping[UnitKey, Quantity]
    analysis_scope: AnalysisScope  # account.schema.json analysis_scope
    statement: str
    gaps: tuple[str, ...]
    full_situation_claim: bool
    _sleeves: Mapping[str, Sleeve] = field(repr=False)

    def spendable_cash(self, account_id: str, currency: str) -> Money | None:
        """Broker-reported cash of this account in this currency only. None when the
        account is blocked, the figure is user-reported, or none is known."""
        view = self.accounts.get(account_id)
        if view is None:
            raise ScopeError("out_of_scope", f"{safe_repr(account_id)}")
        return _spendable(view, currency)

    def sleeve_available(self, sleeve_id: str) -> Money | None:
        """The sleeve's own allocation, or None when its account's cash is unknown or
        its account and currency are over-allocated. Never another sleeve's money."""
        sleeve = self._sleeves[sleeve_id]
        key = (sleeve.account_id, sleeve.currency)
        if key in _over_allocated(self.accounts, self._sleeves.values()):
            return None
        if self.spendable_cash(*key) is None:
            return None
        return sleeve.allocation


def _spendable(view: AccountView, currency: str) -> Money | None:
    if view.sizing_blocked or view.cash_basis.get(currency) != "broker_available":
        return None
    return view.cash.get(currency)


def _over_allocated(
    accounts: Mapping[str, AccountView], sleeves: Iterable[Sleeve]
) -> list[tuple[str, str]]:
    total: dict[tuple[str, str], Money] = {}
    for s in sleeves:
        key = (s.account_id, s.currency)
        total[key] = total.get(key, Money.of(0, s.currency)) + s.allocation
    over = []
    for (account_id, currency), allocated in sorted(total.items()):
        cash = _spendable(accounts[account_id], currency)
        if cash is not None and cash < allocated:
            over.append((account_id, currency))
    return over


def apply_scope(scope: PortfolioScope, consolidation: Consolidation) -> ScopeView:
    every = consolidation.accounts
    if scope.kind is ScopeKind.HYPOTHETICAL:
        return ScopeView(
            scope, {}, tuple(sorted(every)), {}, "hypothetical",
            "hypothetical only: no real account data", (), False, {},
        )  # fmt: skip
    chosen = set(every) if scope.kind is ScopeKind.ALL_DISCLOSED else scope.account_ids
    missing = sorted(chosen - set(every))
    if missing or any(s.account_id not in chosen for s in scope.sleeves):
        raise ScopeError("unknown_account", f"{missing or 'sleeve account'}")
    accounts = {k: every[k] for k in sorted(chosen)}
    exposure: dict[UnitKey, Quantity] = {}
    for view in accounts.values():
        for key, qty in view.units.items():
            exposure[key] = exposure.get(key, Quantity(0)) + qty
    gaps = [f"{k}: {v.status}" for k, v in accounts.items() if v.sizing_blocked]
    if consolidation.quarantined:
        gaps.append(f"{len(consolidation.quarantined)} source snapshot(s) quarantined")
    over = _over_allocated(accounts, scope.sleeves)
    gaps += [f"{a} {c}: sleeves over-allocated" for a, c in over]
    outside = _OUTSIDE_GAP.get(scope.outside)
    complete = not gaps and outside is None
    selected = scope.kind is ScopeKind.SELECTED
    which = "selected accounts" if selected else "all disclosed accounts"
    statement = f"complete for {which}"
    if not complete:
        statement = "partial: " + "; ".join([*gaps, *([outside] if outside else [])])
    return ScopeView(
        scope,
        MappingProxyType(accounts),
        tuple(sorted(set(every) - chosen)),
        MappingProxyType({k: v for k, v in exposure.items() if v.value}),
        "complete_declared" if complete else "partial_declared",
        statement,
        tuple(gaps),
        complete and not selected,
        MappingProxyType({s.sleeve_id: s for s in scope.sleeves}),
    )
