"""Option deliverable exposure, collateral and spread legging (T034; R017, R043,
R044, R068, R089; spec 09; T008 F-14).

- `contract_terms` reduces a contract to one underlying: N units plus cash C (strike
  currency) per contract against a strike payment K*M. Unverified, adjusted-unverified
  or unknown terms, baskets and foreign-currency cash give `Unavailable`.
- Collateral convention (spec 09, fully secured): paid premiums are spent; received
  premium never funds anything (unsettled). A short put reserves K*M per contract. A
  short call needs N held units per contract (physical settlement) plus C in cash, and
  units cover one call only. A short paired with a same-expiry, same-terms long of the
  same right that is at least as exercisable (American covers either style; European
  covers only European) reserves the strike-payment width, which bounds the spread's
  expiry loss. Among such pairings greedy pairing may over-reserve but never
  under-reserve. Temporary funding after an early assignment of a paired short is not
  yet reserved (increment 2).
- Premiums are counted per call: every paid premium in `legs` is spent, and premium
  received on closing legs is not netted (conservative). `AccountCover.units` must
  already be net of other encumbrances (other proposals, other structures).
  Broker margin formulas are not modelled; broker option permissions are a caller
  input and unknown permissions block.
- Legging evaluates every intermediate state of a leg sequence (net by contract), so
  a short leg entered or left alone is checked as such. Any blocked state blocks.
Cash is one account and currency; another currency cannot fund (R043). Exact
`Fraction`; money rounds up (cost). Analysis only: nothing here submits or exercises.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from math import ceil, floor

from qw_domain.decimals import DOMAIN_CONTEXT, Money, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.options import (
    CashDeliverable,
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.valuation import Unavailable

COLLATERAL_METHOD = "fully_secured_greedy_pairing/1"


@dataclass(frozen=True, slots=True)
class ContractTerms:
    underlying_id: InstrumentId
    units: Fraction  # N underlying units per contract
    cash: Fraction  # C delivered with the units, strike currency
    strike_cash: Fraction  # K*M paid by the call holder / to the put holder
    currency: str

    @property
    def effective_strike(self) -> Fraction:
        x, u = self.strike_cash - self.cash, self.units
        return Fraction(x.numerator * u.denominator, x.denominator * u.numerator)


def contract_terms(contract: OptionContract) -> ContractTerms | Unavailable:
    if reasons := contract.sizing_block_reasons():
        return Unavailable(reasons[0], ", ".join(reasons))
    assert contract.multiplier is not None  # implied by sizing_allowed
    units = [d for d in contract.deliverables if isinstance(d, UnitDeliverable)]
    cash = [d for d in contract.deliverables if isinstance(d, CashDeliverable)]
    if not units:
        return Unavailable("unit_deliverable_missing", "no units of the underlying")
    if len(units) != 1 or units[0].instrument_id != contract.underlying_id:
        return Unavailable("basket_deliverable_unsupported", "not one underlying")
    if any(d.amount.currency != contract.strike_currency for d in cash):
        return Unavailable("deliverable_currency_mismatch", "cash not in strike ccy")
    if any(d.amount.amount.value < 0 for d in cash):
        return Unavailable("cash_deliverable_negative", "negative cash component")
    terms = ContractTerms(
        underlying_id=contract.underlying_id,
        units=Fraction(units[0].quantity.value),
        cash=sum((Fraction(d.amount.amount.value) for d in cash), Fraction()),
        strike_cash=Fraction(contract.strike.value)
        * Fraction(contract.multiplier.value),
        currency=contract.strike_currency,
    )
    if terms.effective_strike <= 0:
        return Unavailable("effective_strike_not_positive", "cash exceeds strike")
    return terms


def _money(value: Fraction, currency: str, *, up: bool = True) -> Money:
    """To 1e-12: costs round up (never under-reserve), gains round down."""
    scaled = (ceil if up else floor)(value * 10**12)
    with localcontext(DOMAIN_CONTEXT):
        return Money.of(Decimal(scaled).scaleb(-12), currency)


def expiry_intrinsic(contract: OptionContract, spot: Price) -> Money | Unavailable:
    """Spec 09 payoff convention per long contract at underlying price `spot`."""
    terms = contract_terms(contract)
    if isinstance(terms, Unavailable):
        return terms
    if spot.value < 0:
        raise ValueError("spot must be non-negative")
    moneyness = terms.units * Fraction(spot.value) + terms.cash - terms.strike_cash
    if contract.right is OptionRight.PUT:
        moneyness = -moneyness
    return _money(max(moneyness, Fraction()), terms.currency)


class Structure(StrEnum):
    LONG = "long"
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    SPREAD = "spread"


@dataclass(frozen=True, slots=True)
class Leg:
    contract: OptionContract
    quantity: Quantity  # contracts: + long, - short (or a close of a long)
    premium: Money  # journal sign: + received, - paid; total for the leg

    def __post_init__(self) -> None:
        if type(self.quantity) is not Quantity or type(self.premium) is not Money:
            raise TypeError("leg quantity must be Quantity and premium Money")
        q = Fraction(self.quantity.value)
        if q == 0 or q.denominator != 1:
            raise ValueError("leg quantity must be a non-zero whole number")


@dataclass(frozen=True, slots=True)
class AccountCover:
    currency: str
    available_cash: Money  # settled, net of commitments, this account only
    units: Mapping[InstrumentId, Quantity]  # reconciled holdings
    permitted: frozenset[Structure] | None  # broker option permissions; None = unknown

    def __post_init__(self) -> None:
        if self.available_cash.currency != self.currency:
            raise ValueError("available cash must be in the account currency")


@dataclass(frozen=True, slots=True)
class CoverCheck:
    blocked: bool
    reasons: tuple[str, ...]
    cash_required: Money
    units_encumbered: tuple[tuple[InstrumentId, Fraction], ...]
    structures: frozenset[Structure]
    method: str = COLLATERAL_METHOD


def _group(c: OptionContract, t: ContractTerms) -> tuple[object, ...]:
    return (c.right, c.expiry, c.settlement, t.underlying_id, t.units, t.cash)


def _can_cover(long: OptionContract, short: OptionContract) -> bool:
    """A long covers a short only if it is at least as exercisable: a European
    long cannot answer an early assignment of an American short."""
    american = ExerciseStyle.AMERICAN
    return long.style is american or short.style is not american


def _width(call: bool, long_k: Fraction, short_k: Fraction) -> Fraction:
    """Per-contract loss bound of a short paired with a long (strike cash)."""
    return max(long_k - short_k if call else short_k - long_k, Fraction())


def check_cover(legs: Sequence[Leg], cover: AccountCover) -> CoverCheck | Unavailable:
    """Collateral for the open state `legs` (netted by contract) in one account."""
    net: dict[InstrumentId, tuple[OptionContract, Fraction]] = {}
    paid = Fraction()
    for leg in legs:
        if leg.premium.currency != cover.currency:
            return Unavailable("currency_mismatch", "premium in another currency")
        paid += max(-Fraction(leg.premium.amount.value), Fraction())
        contract, prior = net.get(leg.contract.contract_id, (leg.contract, Fraction()))
        if contract != leg.contract:
            return Unavailable("contract_terms_changed", "one id, two term sets")
        net[contract.contract_id] = (contract, prior + Fraction(leg.quantity.value))
    longs: dict[tuple[object, ...], list[tuple[OptionContract, list[Fraction]]]] = {}
    shorts: list[tuple[OptionContract, ContractTerms, Fraction]] = []
    for contract, qty in net.values():
        terms = contract_terms(contract)
        if isinstance(terms, Unavailable):
            return terms
        if terms.currency != cover.currency:
            return Unavailable("currency_mismatch", "strike in another currency")
        if qty > 0:
            slot = [terms.strike_cash, qty]
            longs.setdefault(_group(contract, terms), []).append((contract, slot))
        elif qty < 0:
            shorts.append((contract, terms, -qty))
    reserve, structures = paid, set[Structure]()
    if longs:
        structures.add(Structure.LONG)
    reasons: list[str] = []
    held = {i: Fraction(q.value) for i, q in cover.units.items()}
    encumbered: dict[InstrumentId, Fraction] = {}
    for contract, terms, qty in sorted(shorts, key=lambda s: s[1].strike_cash):
        call = contract.right is OptionRight.CALL
        pool = [
            s
            for c, s in longs.get(_group(contract, terms), [])
            if _can_cover(c, contract)
        ]
        for slot in sorted(pool, key=lambda s: _width(call, s[0], terms.strike_cash)):
            take = min(qty, slot[1])
            if take:
                reserve += _width(call, slot[0], terms.strike_cash) * take
                slot[1] -= take
                qty -= take
                structures.add(Structure.SPREAD)
        if not qty:
            continue
        if not call:
            reserve += terms.strike_cash * qty
            structures.add(Structure.CASH_SECURED_PUT)
            continue
        need = terms.units * qty
        have = held.get(terms.underlying_id, Fraction())
        if contract.settlement is not Settlement.PHYSICAL or have < need:
            reasons.append("uncovered_short_call")
            continue
        held[terms.underlying_id] = have - need
        encumbered[terms.underlying_id] = (
            encumbered.get(terms.underlying_id, Fraction()) + need
        )
        reserve += terms.cash * qty
        structures.add(Structure.COVERED_CALL)
    cash = _money(reserve, cover.currency)
    if cash > cover.available_cash:
        reasons.append("insufficient_cash")
    if cover.permitted is None:
        reasons.append("broker_permission_unknown")
    else:
        reasons += [
            f"broker_permission_missing:{s}"
            for s in sorted(structures - cover.permitted)
        ]
    return CoverCheck(
        blocked=bool(reasons),
        reasons=tuple(reasons),
        cash_required=cash,
        units_encumbered=tuple(
            sorted(encumbered.items(), key=lambda e: str(e[0].uuid))
        ),
        structures=frozenset(structures),
    )


@dataclass(frozen=True, slots=True)
class LeggingCheck:
    blocked: bool
    states: tuple[CoverCheck, ...]  # after each leg in order
    worst: int  # index of the first blocked state, else of the largest cash need


def check_legging(
    sequence: Sequence[Leg], cover: AccountCover, existing: Sequence[Leg] = ()
) -> LeggingCheck | Unavailable:
    if not sequence:
        raise ValueError("legging needs at least one leg")
    states: list[CoverCheck] = []
    for i in range(1, len(sequence) + 1):
        state = check_cover([*existing, *sequence[:i]], cover)
        if isinstance(state, Unavailable):
            return state
        states.append(state)
    blocked = [i for i, s in enumerate(states) if s.blocked]
    worst = (
        blocked[0]
        if blocked
        else max(range(len(states)), key=lambda i: states[i].cash_required)
    )
    return LeggingCheck(blocked=bool(blocked), states=tuple(states), worst=worst)


@dataclass(frozen=True, slots=True)
class VerticalBounds:
    max_loss: Money
    max_gain: Money


def vertical_bounds(long: Leg, short: Leg, fees: Money) -> VerticalBounds | Unavailable:
    """Spec 09 idealized expiry bounds for an equal-quantity, same-terms vertical."""
    lt, st = contract_terms(long.contract), contract_terms(short.contract)
    if isinstance(lt, Unavailable):
        return lt
    if isinstance(st, Unavailable):
        return st
    n = Fraction(long.quantity.value)
    if (
        _group(long.contract, lt) != _group(short.contract, st)
        or not _can_cover(long.contract, short.contract)
        or n <= 0
        or Fraction(short.quantity.value) != -n
    ):
        return Unavailable("not_a_vertical", "legs differ in terms or quantity")
    if {long.premium.currency, short.premium.currency, fees.currency} != {lt.currency}:
        return Unavailable("currency_mismatch", "premium or fee not in strike ccy")
    if fees.amount.value < 0:
        return Unavailable("fee_negative", "fees must be >= 0")
    width = abs(lt.strike_cash - st.strike_cash) * n
    net = Fraction((long.premium + short.premium).amount.value)  # + credit, - debit
    fee = Fraction(fees.amount.value)
    # The structure, not the premium sign, decides: a call spread long the lower
    # strike or a put spread long the higher strike is a debit structure.
    call = long.contract.right is OptionRight.CALL
    debit = lt.strike_cash < st.strike_cash if call else lt.strike_cash > st.strike_cash
    if debit:
        loss, gain = -net + fee, width + net - fee
    else:
        loss, gain = width - net + fee, net - fee
    cur = lt.currency
    return VerticalBounds(_money(loss, cur), _money(gain, cur, up=False))
