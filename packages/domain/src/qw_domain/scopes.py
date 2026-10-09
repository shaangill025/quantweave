"""Portfolio scopes, sleeves and account-specific spendability (T013).

Spec §2 ONB01, ONB13 and ONB20; spec §4 "Cash and commitments" and "Options and
outside-coverage holdings". `apply_scope` selects accounts from a `Consolidation`
(`qw_domain.sources`) and reports exposure per (instrument, currency) with no FX.
Exposure is informative only. Cash and sleeves stay per account and currency: there is
no cross-account or cross-currency spendability (R043), and a sleeve allocates a
purpose budget, never custody (R010). Completeness is a result field and unknown
outside exposure is never zero (R042). Stdlib only.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from qw_domain.decimals import Money, Quantity, safe_repr
from qw_domain.sources import (
    ID_PATTERN,
    AccountView,
    Consolidation,
    ScopeError,
    UnitKey,
    check_text,
    item_order,
)


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
        check_text(ID_PATTERN, self.sleeve_id, "sleeve_id")
        check_text(ID_PATTERN, self.account_id, "account_id")
        check_text(ID_PATTERN, self.purpose, "purpose")
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
    gaps += [
        f"{k}: {c.source_id} (stale, higher authority) last reported {c.value}"
        f" for {item_order(x.item)[0] or 'cash'} {x.item[1]}"
        for k, v in accounts.items()
        for x in v.conflicts
        for c in x.candidates
        if c.stale
    ]
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
