"""Bounded decimal value types, money and the decimal-string wire format.

T008 review §3.2. Values are built only from `str`, `int` or `Decimal`; floats raise
`TypeError`. Each class has a maximum number of integer digits and a maximum scale.
Out-of-bounds input is rejected and never rounded. Rounding happens only in
`quantize`, which names the rounding mode.
"""

import decimal
import numbers
import re
from collections.abc import Mapping
from decimal import Context, Decimal, InvalidOperation, localcontext, setcontext
from enum import Enum
from functools import total_ordering
from typing import ClassVar, Self

# ADR-013: precision above the 38-digit storage, so nothing rounds before quantize.
DOMAIN_CONTEXT = Context(
    prec=50,
    rounding=decimal.ROUND_HALF_EVEN,
    traps=[
        InvalidOperation,
        decimal.DivisionByZero,
        decimal.Overflow,
        decimal.FloatOperation,
    ],
)


def install_decimal_context() -> None:
    """Install the project decimal context; call once in each process entry point."""
    setcontext(DOMAIN_CONTEXT.copy())


class DecimalValueError(ValueError):
    """A decimal outside its class bounds. `code` is the problem code for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CurrencyMismatchError(ValueError):
    """Arithmetic or comparison between amounts in different currencies."""


class Rounding(Enum):
    """Named rounding policies (review §3.2). Distinct members, so the policy applied
    is recorded even when two policies share a `decimal` mode (`.mode`)."""

    COST = "cost"  # costs, commitments, reservations: magnitude rounds up
    PROCEEDS = "proceeds"  # planned proceeds: magnitude rounds down
    LOT = "lot"  # sized quantities round down (power-of-ten increments only)
    DISPLAY = "display"  # display only: banker's rounding

    @property
    def mode(self) -> str:
        return _ROUNDING_MODES[self]


_ROUNDING_MODES = {
    Rounding.COST: decimal.ROUND_UP,
    Rounding.PROCEEDS: decimal.ROUND_DOWN,
    Rounding.LOT: decimal.ROUND_DOWN,
    Rounding.DISPLAY: decimal.ROUND_HALF_EVEN,
}


def safe_repr(value: object, limit: int = 64) -> str:
    """`repr` capped for error messages, so hostile input cannot flood logs."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _numeric_like(value: object) -> bool:
    """Types whose comparison with a value type is a likely unit or float bug."""
    return isinstance(value, numbers.Number | str | bytes | BoundedDecimal | Money)


def _reject_non_decimal(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise TypeError(f"decimal must be str, int or Decimal: {type(value).__name__}")


@total_ordering  # every derived comparison goes through __lt__/__eq__ and their checks
class BoundedDecimal:
    """Base for decimal classes; subclasses set INT_DIGITS, SCALE and POSITIVE."""

    INT_DIGITS: ClassVar[int]
    SCALE: ClassVar[int]
    POSITIVE: ClassVar[bool] = False
    _pattern: ClassVar[re.Pattern[str]]
    __slots__ = ("_value",)
    _value: Decimal

    def __init__(self, value: str | int | Decimal) -> None:
        _reject_non_decimal(value)
        if isinstance(value, str):
            if self._pattern.fullmatch(value) is None:
                raise DecimalValueError(
                    "decimal_format", f"{safe_repr(value)} is malformed"
                )
            value = Decimal(value)
        elif isinstance(value, int):
            value = Decimal(value)
        object.__setattr__(self, "_value", self._checked(value))

    def __init_subclass__(cls) -> None:
        whole = rf"(0|[1-9][0-9]{{0,{cls.INT_DIGITS - 1}}})(\.[0-9]{{1,{cls.SCALE}}})?$"
        sign = r"(?!0(\.0+)?$)" if cls.POSITIVE else r"(?!-0(\.0+)?$)-?"
        cls._pattern = re.compile(f"^{sign}{whole}", re.ASCII)

    @classmethod
    def wire_pattern(cls) -> str:
        """Review §3.2 regex for this class, ASCII digits; use with fullmatch."""
        return cls._pattern.pattern

    @classmethod
    def from_wire(cls, text: str) -> Self:
        """Parse a JSON decimal string. JSON numbers (int, float, bool) are rejected."""
        if not isinstance(text, str):
            raise TypeError(f"wire decimal must be a string: {type(text).__name__}")
        return cls(text)

    def to_wire(self) -> str:
        """Canonical decimal string: no exponent, no trailing fractional zeros."""
        text = format(self._value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @property
    def value(self) -> Decimal:
        return self._value

    @classmethod
    def _checked(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            code = "decimal_not_finite"
        elif value.is_zero() and value.is_signed():
            code = "decimal_negative_zero"
        elif -int(value.as_tuple().exponent) > cls.SCALE:
            code = "decimal_scale_exceeded"
        elif not value.is_zero() and value.adjusted() >= cls.INT_DIGITS:
            code = "decimal_out_of_range"
        elif cls.POSITIVE and value <= 0:
            code = "decimal_not_positive"
        else:
            return value
        raise DecimalValueError(
            code, f"{cls.__name__} {cls.bounds()}: {safe_repr(value)}"
        )

    @classmethod
    def bounds(cls) -> str:
        sign = "> 0" if cls.POSITIVE else "signed"
        return f"({cls.INT_DIGITS} integer digits, scale {cls.SCALE}, {sign})"

    @classmethod
    def _result(cls, value: Decimal) -> Self:
        out = object.__new__(cls)
        object.__setattr__(
            out, "_value", cls._checked(value.copy_abs() if value.is_zero() else value)
        )
        return out

    def _same(self, other: object) -> Decimal | None:
        """The other value if it has the same class; None for unrelated types."""
        if type(other) is type(self):
            return other._value
        if _numeric_like(other):
            raise TypeError(f"cannot combine {self!r} with {type(other).__name__}")
        return None

    def __reduce__(self) -> tuple[type[Self], tuple[str]]:
        # copy, deepcopy and pickle rebuild through the validating constructor.
        return (type(self), (self.to_wire(),))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __add__(self, other: Self) -> Self:
        if (value := self._same(other)) is None:
            return NotImplemented
        with localcontext(DOMAIN_CONTEXT):
            return self._result(self._value + value)

    def __sub__(self, other: Self) -> Self:
        if (value := self._same(other)) is None:
            return NotImplemented
        with localcontext(DOMAIN_CONTEXT):
            return self._result(self._value - value)

    def __neg__(self) -> Self:
        with localcontext(DOMAIN_CONTEXT):
            return self._result(-self._value)

    def __eq__(self, other: object) -> bool:
        value = self._same(other)
        return NotImplemented if value is None else self._value == value

    def __lt__(self, other: Self) -> bool:
        value = self._same(other)
        return NotImplemented if value is None else self._value < value

    def __hash__(self) -> int:
        return hash((type(self).__name__, self._value))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.to_wire()!r})"


class MoneyAmount(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE = 26, 12


class Price(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE = 26, 12


class Quantity(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE = 26, 12


class PositiveQuantity(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE, POSITIVE = 26, 12, True


class Multiplier(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE, POSITIVE = 12, 12, True


class FxRate(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE, POSITIVE = 12, 18, True


class Ratio(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE = 12, 18


class UsdBudget(BoundedDecimal):
    __slots__ = ()
    INT_DIGITS, SCALE = 14, 6


def quantize[T: BoundedDecimal](
    cls: type[T], value: Decimal, *, quantum: Decimal, rounding: Rounding
) -> T:
    """Round `value` to `quantum` (a power of ten) by a named policy; check as `cls`."""
    if not isinstance(value, Decimal) or not isinstance(quantum, Decimal):
        raise TypeError("quantize takes Decimal value and quantum")
    if (
        not quantum.is_finite()
        or quantum.is_signed()
        or quantum.as_tuple().digits != (1,)
    ):
        raise DecimalValueError(
            "decimal_quantum", f"{safe_repr(quantum)} is not a positive power of ten"
        )
    with localcontext(DOMAIN_CONTEXT):
        try:
            rounded = value.quantize(quantum, rounding=rounding.mode)
        except InvalidOperation:
            raise DecimalValueError("decimal_out_of_range", safe_repr(value)) from None
        return cls._result(rounded)


_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)


@total_ordering
class Money:
    """An amount in one ISO 4217 currency. Arithmetic and ordering across currencies
    raise `CurrencyMismatchError`; equality across currencies is False."""

    __slots__ = ("amount", "currency")
    amount: MoneyAmount
    currency: str

    def __init__(self, amount: MoneyAmount, currency: str) -> None:
        if type(amount) is not MoneyAmount:
            raise TypeError(f"amount must be MoneyAmount, not {type(amount).__name__}")
        if not isinstance(currency, str) or _CURRENCY.fullmatch(currency) is None:
            raise DecimalValueError(
                "currency_code", f"{safe_repr(currency)} is not ISO 4217"
            )
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    @classmethod
    def of(cls, amount: str | int | Decimal, currency: str) -> Self:
        return cls(MoneyAmount(amount), currency)

    @classmethod
    def from_wire(cls, obj: Mapping[str, object]) -> Self:
        if set(obj) != {"amount", "currency"}:
            raise DecimalValueError("money_shape", "expected {amount, currency}")
        amount, currency = obj["amount"], obj["currency"]
        if not isinstance(amount, str) or not isinstance(currency, str):
            raise TypeError("money amount and currency must be JSON strings")
        return cls(MoneyAmount.from_wire(amount), currency)

    def to_wire(self) -> dict[str, str]:
        return {"amount": self.amount.to_wire(), "currency": self.currency}

    def _same(self, other: object) -> MoneyAmount | None:
        """Same-currency amount for arithmetic and ordering; None if unrelated."""
        if type(other) is Money:
            if other.currency != self.currency:
                raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")
            return other.amount
        if _numeric_like(other):
            raise TypeError(f"cannot combine Money with {type(other).__name__}")
        return None

    def __reduce__(self) -> tuple[type["Money"], tuple[MoneyAmount, str]]:
        return (Money, (self.amount, self.currency))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Money is immutable")

    def __add__(self, other: "Money") -> "Money":
        if (amount := self._same(other)) is None:
            return NotImplemented
        return Money(self.amount + amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if (amount := self._same(other)) is None:
            return NotImplemented
        return Money(self.amount - amount, self.currency)

    def __eq__(self, other: object) -> bool:
        # Equality needs no FX rate: different currencies are never equal. Returning
        # False (not raising) keeps sets and dicts of mixed currencies total.
        if type(other) is Money:
            return self.currency == other.currency and self.amount == other.amount
        if _numeric_like(other):
            raise TypeError(f"cannot compare Money with {type(other).__name__}")
        return NotImplemented

    def __lt__(self, other: "Money") -> bool:
        amount = self._same(other)
        return NotImplemented if amount is None else self.amount < amount

    def __hash__(self) -> int:
        return hash((self.currency, self.amount))

    def __repr__(self) -> str:
        return f"Money({self.amount.to_wire()!r}, {self.currency!r})"
