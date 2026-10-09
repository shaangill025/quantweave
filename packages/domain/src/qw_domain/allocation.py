"""Cash-first allocation/rebalance reference STR-ALLOC-001 (T028 increment 1).

Spec §8 STR-ALLOC-001; §4 cash and commitments; §7 joint feasibility; R043, R049.
- Capital base = every in-scope account's cash plus its held units at fresh marks, in
  one currency, from consolidated `AccountView`s (one figure per underlying account
  and item, so a mirrored source never counts twice). Target value = weight x base.
- Free cash per account = broker-available cash minus the account's declared reserve.
  It is spent only in its own account (no cross-account or cross-currency funding).
- Greedy, one buy per (account, instrument): pick the largest relative underweight
  (target - value) / target, ties by canonical instrument id; size it in the permitted
  account allowing the most units (then the lower cost, then account id); round down
  to the lot, never past the target and never past free cash; recompute after each.
- Cash required = fixed + quantity x (mark + unit cost), rounded up at the money scale
  (as `proposals.cash_requirement`). No sale proceeds are assumed: deviations still
  outside the drift band after cash are `SALE_REQUIRED` / `FUNDING_REQUIRED`
  prerequisites, not candidates; holdings outside the target set are
  `UNMANAGED_HOLDING`. The band comes only from a configuration whose hash matches
  the adopted one (`config_hash`). Buys never wait for a band breach.
- Missing, stale or future account state or marks of held instruments, a short or
  not confirmed unit-priced holding, an unadopted target set, unknown cash or another
  currency block the whole run (the base is unknown); the same problem for an unheld
  target, or no permitting account, abstains only that instrument.
- Candidates are proposal inputs, never orders. Exact arithmetic (`Fraction`).
- LIMITATIONS: one currency (no FX); no sale basket, tax or turnover rules, or issuer
  look-through; the mark is the cost estimate (the proposal gate re-prices).
Stdlib only.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from functools import cache
from pathlib import Path
from types import MappingProxyType

from qw_domain.decimals import (
    Money,
    PositiveQuantity,
    Price,
    Quantity,
    Ratio,
    safe_repr,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.rights import Use
from qw_domain.risk import ProposedAction, Side
from qw_domain.sources import ID_PATTERN, AccountView
from qw_domain.strategy_registry import (
    AssetClass,
    DataNeed,
    Family,
    Horizon,
    Parameter,
    ParamKind,
    StrategyError,
    StrategyManifest,
)
from qw_domain.valuation import Mark, MarkKind

METHOD = "cash_first_underweight/1"
STRATEGY_ID, VERSION = "STR-ALLOC-001", "0.1.0-research"
_MONEY_SCALE, _RATIO_SCALE = 10**12, 10**18


class AllocationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Code(StrEnum):
    BUY_UNDERWEIGHT = "buy_underweight"  # candidate reason
    # run-level blocks: the capital base or targets are unknown
    TARGETS_NOT_ADOPTED = "targets_not_adopted"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    ACCOUNT_STALE = "account_stale"
    CASH_UNKNOWN = "cash_unknown"
    CURRENCY_UNSUPPORTED = "currency_unsupported"
    MARK_MISSING = "mark_missing"
    MARK_STALE = "mark_stale"
    MARK_KIND_UNSUITABLE = "mark_kind_unsuitable"  # bid/reference never price a buy
    CONFIG_INVALID = "config_invalid"
    CONFIG_MISMATCH = "config_mismatch"  # not the adopted configuration
    SHORT_UNSUPPORTED = "short_unsupported"
    NOT_UNIT_PRICED = "not_unit_priced"  # value != units x mark (e.g. an option)
    # abstentions: one account or instrument
    CASH_NOT_SPENDABLE = "cash_not_spendable"
    RESERVE_MISSING = "reserve_missing"
    NO_PERMITTED_ACCOUNT = "no_permitted_account"
    INSUFFICIENT_CASH = "insufficient_cash"
    BELOW_LOT = "below_lot"
    BELOW_MINIMUM_TRADE = "below_minimum_trade"
    # prerequisites left after cash, outside the drift band
    SALE_REQUIRED = "sale_required"
    FUNDING_REQUIRED = "funding_required"
    UNMANAGED_HOLDING = "unmanaged_holding"  # held, not in the target set


@dataclass(frozen=True, slots=True)
class Reason:
    code: Code
    subject: str


@dataclass(frozen=True, slots=True)
class Target:
    instrument_id: InstrumentId
    weight: Ratio  # of the capital base


@dataclass(frozen=True, slots=True)
class TargetSet:
    """Explicit user-approved targets; `adopted_at` None means not adopted."""

    target_set_id: str
    version: int
    adopted_at: datetime | None
    targets: tuple[Target, ...]

    def __post_init__(self) -> None:
        if ID_PATTERN.fullmatch(self.target_set_id) is None or self.version < 1:
            raise AllocationError("targets", safe_repr(self.target_set_id))
        if self.adopted_at is not None:
            object.__setattr__(self, "adopted_at", ensure_aware_utc(self.adopted_at))
        object.__setattr__(self, "targets", tuple(self.targets))
        ids = [t.instrument_id for t in self.targets]
        if len(set(ids)) != len(ids):
            raise AllocationError("targets", "duplicate instrument")
        weights = [t.weight.value for t in self.targets]
        if any(w < 0 for w in weights) or sum(weights, Decimal(0)) > 1:
            raise AllocationError("targets", "weights must be >= 0 and sum to <= 1")


@dataclass(frozen=True, slots=True)
class Tradability:
    """An account permits buying an instrument with these rules and costs. An absent
    (account, instrument) pair is not permitted."""

    account_id: str
    instrument_id: InstrumentId
    lot: PositiveQuantity  # a power of ten: 1 whole units, 0.001 fractional
    min_notional: Money
    fixed_cost: Money
    unit_cost: Money

    def __post_init__(self) -> None:
        if self.lot.value.normalize().as_tuple().digits != (1,):
            raise AllocationError("lot", "must be a power of ten")
        if min(m.amount.value for m in self._money()) < 0:
            raise AllocationError("cost", "minimum and costs must be non-negative")

    def _money(self) -> tuple[Money, Money, Money]:
        return self.min_notional, self.fixed_cost, self.unit_cost


@dataclass(frozen=True, slots=True)
class AllocationInputs:
    currency: str
    as_of: datetime
    accounts: Mapping[str, AccountView]  # every account in the scope
    account_observed_at: Mapping[str, datetime]
    account_max_age: timedelta
    reserves: Mapping[str, Money]  # unspendable liquidity reserve per account
    targets: TargetSet
    marks: Mapping[InstrumentId, Mark]
    mark_max_age: timedelta
    tradability: tuple[Tradability, ...]
    config: Mapping[str, object]  # STR-ALLOC-001 parameters (drift_band)
    config_hash: str  # of the adopted/qualified configuration
    unit_priced: frozenset[InstrumentId]  # confirmed value = units x mark (stock/ETF)

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "as_of", ensure_aware_utc(self.as_of))
        put(self, "tradability", tuple(self.tradability))
        put(self, "unit_priced", frozenset(self.unit_priced))
        for name in ("accounts", "reserves", "marks"):
            put(self, name, MappingProxyType(dict(getattr(self, name))))
        seen = {a: ensure_aware_utc(t) for a, t in self.account_observed_at.items()}
        put(self, "account_observed_at", MappingProxyType(seen))
        if any(m.instrument_id != i for i, m in self.marks.items()):
            raise AllocationError("marks", "keyed by their own instrument")
        if len({v.account.tenant_id for v in self.accounts.values()}) > 1:
            raise AllocationError("tenant", "accounts of one tenant only")
        put(self, "config", MappingProxyType(dict(self.config)))
        money = [m for t in self.tradability for m in t._money()]
        if any(m.currency != self.currency for m in [*money, *self.reserves.values()]):
            raise AllocationError("currency", f"costs and reserves in {self.currency}")
        keys = [(t.account_id, t.instrument_id) for t in self.tradability]
        if len(set(keys)) != len(keys):
            raise AllocationError("tradability", "duplicate account/instrument")


@dataclass(frozen=True, slots=True)
class BuyCandidate:
    """A proposal candidate, not an order."""

    account_id: str
    instrument_id: InstrumentId
    quantity: PositiveQuantity
    lot: PositiveQuantity
    price: Price  # the mark used for the estimate
    mark_observed_at: datetime
    fixed_cost: Money
    unit_cost: Money
    cash_required: Money
    reason: Code = Code.BUY_UNDERWEIGHT

    def to_action(
        self,
        *,
        tenant_id: str,
        strategy_id: str,
        denominator: str,
        issuer_id: str,
        sector_id: str,
        leveraged_or_inverse: bool | None,
        sleeve_id: str | None = None,
    ) -> ProposedAction:
        return ProposedAction(
            tenant_id, self.account_id, self.cash_required.currency, sleeve_id,
            strategy_id, denominator, self.instrument_id, issuer_id, sector_id,
            Horizon.LONG_TERM.value, Side.BUY, self.quantity, self.lot, None,
            self.fixed_cost, self.unit_cost, leveraged_or_inverse,
        )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Deviation:
    instrument_id: InstrumentId
    target_weight: Ratio
    weight_before: Ratio  # of the capital base, half-even at 1e-18 (display)
    weight_after: Ratio


@dataclass(frozen=True, slots=True)
class AllocationResult:
    method: str
    blocked: tuple[Reason, ...]  # non-empty: no candidates at all
    candidates: tuple[BuyCandidate, ...]  # in allocation order
    abstentions: tuple[Reason, ...]
    prerequisites: tuple[Reason, ...]
    capital_base: Money | None
    cash_after: Mapping[str, Money]
    deviations: tuple[Deviation, ...]


def _key(i: InstrumentId) -> str:
    return i.to_wire()


def _frac(x: Money | Price | PositiveQuantity | Ratio | Quantity) -> Fraction:
    return Fraction(x.amount.value if isinstance(x, Money) else x.value)


def _dec(x: Fraction) -> Decimal:
    """Exact decimal of a terminating fraction (sums and products of <= 12-place
    decimals, or a value already rounded to a power of ten)."""
    for k in range(51):
        scale = 10**k
        if scale % x.denominator == 0:
            return Decimal(x.numerator * (scale // x.denominator)).scaleb(-k)
    raise AllocationError("inexact", "not a terminating decimal")


def _ceil12(x: Fraction) -> Fraction:
    """Round up at the money scale (`Rounding.COST`; costs are non-negative)."""
    return Fraction(-((-x * _MONEY_SCALE) // 1), _MONEY_SCALE)


def _floor12(x: Fraction) -> Fraction:
    """Round an asset value down at the money scale (as `valuation`)."""
    return Fraction((x * _MONEY_SCALE) // 1, _MONEY_SCALE)


def _ratio(num: Fraction, den: Fraction) -> Ratio:
    """Display weight num/den, half-even at 1e-18 (`Rounding.DISPLAY`)."""
    if not den:
        return Ratio(0)
    return Ratio(Decimal(round(Fraction(num, den) * _RATIO_SCALE)).scaleb(-18))


def _cost(t: Tradability, qty: Fraction, price: Fraction) -> Fraction:
    unit = price + _frac(t.unit_cost)
    return _ceil12(_frac(t.fixed_cost) + qty * unit)


def _units(
    t: Tradability, price: Fraction, deficit: Fraction, free: Fraction
) -> Fraction:
    """The largest lot multiple within the deficit (never past target) and free cash."""
    lot, fixed = _frac(t.lot), _frac(t.fixed_cost)
    by_deficit = deficit // (price * lot)
    by_cash = max((free - fixed) // ((price + _frac(t.unit_cost)) * lot), 0)
    n = min(by_deficit, by_cash)
    # The exact cost is <= free, and free lies on the 1e-12 grid (cash, reserves and
    # earlier rounded costs all do), so the rounded-up cost is <= free too.
    if n > 0 and _cost(t, n * lot, price) > free:
        raise AllocationError("overspend", "rounded cost exceeds free cash")
    return n * lot


def _why_not(t: Tradability, qty: Fraction, price: Fraction, deficit: Fraction) -> Code:
    if deficit < price * _frac(t.lot):
        return Code.BELOW_LOT
    if qty > 0:
        return Code.BELOW_MINIMUM_TRADE
    return Code.INSUFFICIENT_CASH


def _band(ins: AllocationInputs, out: list[Reason]) -> Fraction:
    m = allocation_manifest("feed-config-check")  # config hashes use parameters only
    try:
        digest, band = m.config_hash(ins.config), m.resolve(ins.config)["drift_band"]
    except StrategyError as exc:
        out.append(Reason(Code.CONFIG_INVALID, exc.code))
        return Fraction(0)
    if digest != ins.config_hash:
        out.append(Reason(Code.CONFIG_MISMATCH, "config"))
    assert isinstance(band, Decimal)  # a DECIMAL parameter, checked by resolve
    return Fraction(band)


def _blocks(
    ins: AllocationInputs,
) -> tuple[list[Reason], dict[InstrumentId, Fraction], Fraction]:
    t, ccy = ins.as_of, ins.currency
    out: list[Reason] = []
    band = _band(ins, out)
    held: dict[InstrumentId, Fraction] = {}
    adopted = ins.targets.adopted_at
    if adopted is None or adopted > t:
        out.append(Reason(Code.TARGETS_NOT_ADOPTED, ins.targets.target_set_id))
    if not ins.accounts:
        out.append(Reason(Code.ACCOUNT_UNAVAILABLE, "none"))
    for a in sorted(ins.accounts):
        v = ins.accounts[a]
        seen = ins.account_observed_at.get(a)
        if v.sizing_blocked or v.account.account_id != a:
            out.append(Reason(Code.ACCOUNT_UNAVAILABLE, a))
        if seen is None or seen > t or t - seen > ins.account_max_age:
            out.append(Reason(Code.ACCOUNT_STALE, a))
        if ccy not in v.cash or v.cash[ccy].amount.value < 0:  # unknown or margin
            out.append(Reason(Code.CASH_UNKNOWN, a))
        other = [c for c, m in v.cash.items() if c != ccy and m.amount.value]
        if other or any(c != ccy for (_, c) in v.units):
            out.append(Reason(Code.CURRENCY_UNSUPPORTED, a))
        for (i, _), q in v.units.items():
            held[i] = held.get(i, Fraction(0)) + _frac(q)
    for i in sorted(held, key=_key):
        if held[i] < 0:
            out.append(Reason(Code.SHORT_UNSUPPORTED, _key(i)))
        if (why := _mark_problem(ins, i)) is not None:
            out.append(Reason(why, _key(i)))
    return out, held, band


def _mark_problem(ins: AllocationInputs, i: InstrumentId) -> Code | None:
    if i not in ins.unit_priced:
        return Code.NOT_UNIT_PRICED
    m = ins.marks.get(i)
    if m is None:
        return Code.MARK_MISSING
    if m.currency != ins.currency:
        return Code.CURRENCY_UNSUPPORTED
    if m.kind in (MarkKind.BID, MarkKind.REFERENCE):
        return Code.MARK_KIND_UNSUITABLE
    if m.observed_at > ins.as_of or ins.as_of - m.observed_at > ins.mark_max_age:
        return Code.MARK_STALE
    return None


def _free_cash(
    ins: AllocationInputs, cash: Mapping[str, Fraction]
) -> tuple[dict[str, Fraction], list[Reason]]:
    free: dict[str, Fraction] = {}
    abstain: list[Reason] = []
    for a in sorted(ins.accounts):
        reserve = ins.reserves.get(a)
        # Broker-available cash only, as `scopes._spendable` (user reports never fund).
        if ins.accounts[a].cash_basis.get(ins.currency) != "broker_available":
            abstain.append(Reason(Code.CASH_NOT_SPENDABLE, a))
        elif reserve is None:
            abstain.append(Reason(Code.RESERVE_MISSING, a))
        else:
            free[a] = max(cash[a] - _frac(reserve), Fraction(0))
    return free, abstain


def allocate(ins: AllocationInputs) -> AllocationResult:
    """Deterministic cash-first allocation to explicit adopted targets."""
    ccy = ins.currency
    blocked, held, band_weight = _blocks(ins)
    if blocked:
        return AllocationResult(METHOD, tuple(blocked), (), (), (), None, {}, ())
    price = {i: _frac(m.price) for i, m in ins.marks.items()}
    cash = {a: _frac(v.cash[ccy]) for a, v in ins.accounts.items()}
    base = sum(cash.values(), Fraction(0))
    base += sum((q * price[i] for i, q in held.items()), Fraction(0))
    goal = {t.instrument_id: _frac(t.weight) * base for t in ins.targets.targets}
    value = {i: Fraction(0) for i in goal}
    value.update({i: q * price[i] for i, q in held.items()})
    before = dict(value)
    free, abstain = _free_cash(ins, cash)
    pairs: dict[InstrumentId, list[Tradability]] = {}
    for i in sorted(goal, key=_key):
        if value[i] >= goal[i]:
            continue
        allowed = [
            t
            for t in sorted(ins.tradability, key=lambda t: t.account_id)
            if t.instrument_id == i and t.account_id in ins.accounts
        ]
        if (why := _mark_problem(ins, i)) is not None:
            abstain.append(Reason(why, _key(i)))
        elif not allowed:
            abstain.append(Reason(Code.NO_PERMITTED_ACCOUNT, _key(i)))
        else:
            pairs[i] = allowed

    def rank(j: InstrumentId) -> tuple[Fraction, str]:
        return -Fraction(goal[j] - value[j], goal[j]), _key(j)

    candidates: list[BuyCandidate] = []
    while live := [i for i, p in pairs.items() if p and value[i] < goal[i]]:
        i = min(live, key=rank)
        deficit, p = goal[i] - value[i], price[i]
        sized = []
        for t in pairs[i]:
            q = _units(t, p, deficit, free.get(t.account_id, Fraction(0)))
            ok = q > 0 and q * p >= _frac(t.min_notional)
            sized.append((not ok, -q, _cost(t, q, p), t.account_id, t, q))
        infeasible, _, cost, acct, t, q = min(sized, key=lambda s: s[:4])
        if infeasible:  # no remaining account can buy any of it
            for *_, u, uq in sized:
                why = _why_not(u, uq, p, deficit)
                abstain.append(Reason(why, f"{u.account_id}:{_key(i)}"))
            pairs[i] = []
            continue
        pairs[i] = [u for u in pairs[i] if u is not t]
        free[acct] -= cost
        cash[acct] -= cost
        value[i] += q * p
        m = ins.marks[i]
        candidates.append(
            BuyCandidate(
                acct, i, PositiveQuantity(_dec(q)), t.lot, m.price, m.observed_at,
                t.fixed_cost, t.unit_cost, Money.of(_dec(cost), ccy),
            )
        )  # fmt: skip
    band = band_weight * base
    prereq: list[Reason] = []
    deviations: list[Deviation] = []
    for i in sorted(value, key=_key):
        target = goal.get(i, Fraction(0))
        if i not in goal:
            if value[i] > band:
                prereq.append(Reason(Code.UNMANAGED_HOLDING, _key(i)))
        elif value[i] - target > band:
            prereq.append(Reason(Code.SALE_REQUIRED, _key(i)))
        elif target - value[i] > band:
            prereq.append(Reason(Code.FUNDING_REQUIRED, _key(i)))
        w = (_ratio(x, base) for x in (target, before[i], value[i]))
        deviations.append(Deviation(i, *w))
    after = {a: Money.of(_dec(cash[a]), ccy) for a in sorted(cash)}
    return AllocationResult(
        METHOD, (), tuple(candidates), tuple(abstain), tuple(prereq),
        Money.of(_dec(_floor12(base)), ccy), MappingProxyType(after), tuple(deviations),
    )  # fmt: skip


@cache
def allocation_manifest(feed_id: str) -> StrategyManifest:
    """The STR-ALLOC-001 reference manifest for `strategy_registry`. Its code hash is
    this module's source, so any edit to it is a material change needing a new
    version, requalification and re-adoption. `drift_band`
    defaults to 0 (no tolerance), never a universal recommended band."""
    band = Parameter(
        "drift_band", ParamKind.DECIMAL, Decimal(0), Decimal(0), Decimal(1)
    )
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return StrategyManifest(
        STRATEGY_ID, VERSION, Family.ALLOCATION, code,
        (DataNeed(feed_id, frozenset({Use.DERIVED_DATA})),),
        frozenset({AssetClass.STOCKS, AssetClass.ETFS}), Horizon.LONG_TERM, False,
        (band,), "protocol-alloc-reference-1", None,
        "Cash-first allocation reference",
    )  # fmt: skip
