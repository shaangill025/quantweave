"""Option contract identity and OCC symbols (T011; R017, R089, spec 09, F-14, C-24).

Terms are explicit. A missing multiplier is `None` and missing deliverables are an
empty tuple; nothing defaults to 100 shares. Unknown or unverified terms keep the
contract representable but make live sizing unavailable (`sizing_block_reasons`).
The OCC symbol identifies root, expiry, right and strike only. It never determines the
multiplier or deliverable, so adjusted roots such as `BRKB1` parse without terms.
After a corporate action, components whose value is not yet known are explicit
(`CashInLieu`, `UnknownDeliverable`); they block sizing and cannot be verified.
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from fractions import Fraction
from typing import Self

from qw_domain.calendars import SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, safe_repr
from qw_domain.identity import IdentityError, InstrumentId
from qw_domain.instants import require_date


class OptionRight(Enum):
    CALL = "call"
    PUT = "put"


class ExerciseStyle(Enum):
    AMERICAN = "american"
    EUROPEAN = "european"
    UNKNOWN = "unknown"


class Settlement(Enum):
    PHYSICAL = "physical"
    CASH = "cash"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UnitDeliverable:
    instrument_id: InstrumentId
    quantity: PositiveQuantity


@dataclass(frozen=True, slots=True)
class CashDeliverable:
    amount: Money


@dataclass(frozen=True, slots=True)
class CashInLieu:
    """Cash for a fraction (0 < units < 1) of `instrument_id` arising from corporate
    action `event_id`. The amount is unknown until the adjusted terms are verified."""

    instrument_id: InstrumentId
    units: Fraction
    event_id: str

    def __post_init__(self) -> None:
        if type(self.units) is not Fraction or not 0 < self.units < 1:
            raise ValueError(f"cash-in-lieu units {safe_repr(self.units)}")
        if not self.event_id:
            raise ValueError("cash in lieu needs its corporate-action event id")


@dataclass(frozen=True, slots=True)
class UnknownDeliverable:
    """A component added by corporate action `event_id` whose terms are unknown."""

    event_id: str

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("unknown deliverable needs its corporate-action event id")


type Deliverable = UnitDeliverable | CashDeliverable | CashInLieu | UnknownDeliverable


def component_key(d: Deliverable) -> tuple[object, ...]:
    """Two components with one key would be one component listed twice."""
    match d:
        case UnitDeliverable():
            return ("units", d.instrument_id)
        case CashDeliverable():
            return ("cash", d.amount.currency)
        case CashInLieu():
            return ("in_lieu", d.instrument_id, d.event_id)
        case UnknownDeliverable():
            return ("unknown", d.event_id)


_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


@dataclass(frozen=True, slots=True)
class OptionContract:
    """One version (`terms_version`) of a listed option contract's terms.

    `applied_actions` lists the corporate actions, as (event_id, version), absorbed
    into these terms since the base terms; an event id appears at most once."""

    contract_id: InstrumentId
    underlying_id: InstrumentId
    expiry: SessionRef
    strike: Price
    strike_currency: str
    right: OptionRight
    style: ExerciseStyle
    settlement: Settlement
    multiplier: Multiplier | None
    deliverables: tuple[Deliverable, ...]
    adjusted: bool
    terms_verified: bool
    terms_version: int
    applied_actions: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        applied = self.applied_actions
        if type(applied) is not tuple or any(
            type(a) is not tuple
            or len(a) != 2
            or not isinstance(a[0], str)
            or not a[0]
            or type(a[1]) is not int
            or a[1] < 1
            for a in applied
        ):
            raise ValueError("applied_actions must be (event_id, version) tuples")
        if len({event_id for event_id, _ in applied}) != len(applied):
            raise ValueError("a corporate action is applied at most once")
        if _CURRENCY.fullmatch(self.strike_currency) is None:
            raise ValueError(f"strike currency {safe_repr(self.strike_currency)}")
        if self.strike.value <= 0:
            raise ValueError("strike must be positive")
        if type(self.terms_version) is not int or self.terms_version < 1:
            raise ValueError("terms_version must be an integer >= 1")
        if self.underlying_id == self.contract_id:
            raise ValueError("a contract cannot be its own underlying")
        if type(self.deliverables) is not tuple:
            raise TypeError("deliverables must be a tuple")
        if len({component_key(d) for d in self.deliverables}) != len(self.deliverables):
            raise ValueError("duplicate deliverable component")
        if any(
            isinstance(d, UnitDeliverable) and d.instrument_id == self.contract_id
            for d in self.deliverables
        ):
            raise ValueError("a contract cannot deliver itself")
        if self.terms_verified and self._unknown_terms():
            raise ValueError(f"verified terms are incomplete: {self._unknown_terms()}")

    def _unknown_terms(self) -> tuple[str, ...]:
        checks = (
            (self.multiplier is None, "multiplier_unknown"),
            (not self.deliverables, "deliverables_unknown"),
            (self._has(CashInLieu), "cash_in_lieu_unknown"),
            (self._has(UnknownDeliverable), "deliverable_terms_unknown"),
            (self.style is ExerciseStyle.UNKNOWN, "exercise_style_unknown"),
            (self.settlement is Settlement.UNKNOWN, "settlement_unknown"),
        )
        return tuple(code for failed, code in checks if failed)

    def _has(self, kind: type[CashInLieu | UnknownDeliverable]) -> bool:
        return any(isinstance(d, kind) for d in self.deliverables)

    def sizing_block_reasons(self) -> tuple[str, ...]:
        """Reason codes that make live sizing and payoffs unavailable; empty if none.
        Adjusted terms are usable only once verified (F-14)."""
        unverified = () if self.terms_verified else ("terms_unverified",)
        return unverified + self._unknown_terms()

    @property
    def sizing_allowed(self) -> bool:
        return not self.sizing_block_reasons()


_OCC = re.compile(r"([A-Z0-9 ]{6})([0-9]{2})([0-9]{4})([CP])([0-9]{8})", re.ASCII)
_ROOT = re.compile(r"[A-Z0-9]{1,6}", re.ASCII)


@dataclass(frozen=True, slots=True)
class OccSymbol:
    """21-character OCC option symbol: root padded to 6, YYMMDD (20YY), C/P, strike
    in thousandths as 8 digits."""

    root: str
    expiry: date
    right: OptionRight
    strike: Price

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or _ROOT.fullmatch(self.root) is None:
            raise IdentityError(f"OCC root {safe_repr(self.root)} is malformed")
        require_date(self.expiry, "OCC expiry")
        if not 2000 <= self.expiry.year <= 2099:
            raise IdentityError(f"OCC expiry year {self.expiry.year} not representable")
        mills = self.strike.value.scaleb(3)
        if not (0 < mills < 10**8 and mills == mills.to_integral_value()):
            raise IdentityError(f"OCC strike {self.strike!r} not representable")

    @classmethod
    def parse(cls, text: str) -> Self:
        match = _OCC.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            raise IdentityError(f"OCC symbol {safe_repr(text)} is malformed")
        root, yy, mmdd, cp, mills = match.groups()
        try:
            expiry = date.fromisoformat(f"20{yy}-{mmdd[:2]}-{mmdd[2:]}")
            strike = Price(Decimal(int(mills)).scaleb(-3))
        except ValueError:
            raise IdentityError(f"OCC symbol {safe_repr(text)} is invalid") from None
        right = OptionRight.CALL if cp == "C" else OptionRight.PUT
        symbol = cls(root.rstrip(" "), expiry, right, strike)
        if symbol.format() != text:  # e.g. an embedded space in the root
            raise IdentityError(f"OCC symbol {safe_repr(text)} is not canonical")
        return symbol

    def format(self) -> str:
        mills = int(self.strike.value.scaleb(3))
        cp = "C" if self.right is OptionRight.CALL else "P"
        return f"{self.root:<6}{self.expiry:%y%m%d}{cp}{mills:08d}"
