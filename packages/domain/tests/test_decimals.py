"""Decimal value types and wire format (T008 review §3.2, ADR-013).

Expected values are hand-computed.
"""

import re
from decimal import Decimal, FloatOperation, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.decimals import (
    DOMAIN_CONTEXT,
    BoundedDecimal,
    CurrencyMismatchError,
    DecimalValueError,
    FxRate,
    Money,
    MoneyAmount,
    Multiplier,
    PositiveQuantity,
    Price,
    Quantity,
    Ratio,
    Rounding,
    UsdBudget,
    quantize,
)

PROFILE = settings(derandomize=True, database=None, max_examples=300)
BOUNDS: dict[type[BoundedDecimal], tuple[int, int, bool]] = {
    # class: (max integer digits, scale, positive-only), from review §3.2
    MoneyAmount: (26, 12, False), Price: (26, 12, False), Quantity: (26, 12, False),
    PositiveQuantity: (26, 12, True), Multiplier: (12, 12, True),
    FxRate: (12, 18, True), Ratio: (12, 18, False), UsdBudget: (14, 6, False),
}  # fmt: skip
ALL_CLASSES = list(BOUNDS)


def test_class_bounds_match_review() -> None:
    for cls, bounds in BOUNDS.items():
        assert bounds == (cls.INT_DIGITS, cls.SCALE, cls.POSITIVE)


def test_money_amount_pattern_is_review_example_with_ascii_digits() -> None:
    review = r"^(?!-0(\.0+)?$)-?(0|[1-9]\d{0,25})(\.\d{1,12})?$"
    assert MoneyAmount.wire_pattern() == review.replace(r"\d", "[0-9]")


@pytest.mark.parametrize("text", ["0", "1", "-1", "0.5", "-0.000000000001", "9" * 26])
def test_wire_accepts(text: str) -> None:
    assert MoneyAmount.from_wire(text).value == Decimal(text)


@pytest.mark.parametrize(
    "text",
    [
        "-0", "-0.0", "-0.000", "9" * 27, "1." + "0" * 13, "01", "1e5", "1E5", "+1",
        "1.", ".5", "", "-", " 1", "1 ", "1\n", "\u0661", "1_000", "NaN", "Infinity",
        "-Infinity", "sNaN",
    ],
)  # fmt: skip
def test_wire_rejects(text: str) -> None:
    with pytest.raises(DecimalValueError):
        MoneyAmount.from_wire(text)


def test_construction_types() -> None:
    assert MoneyAmount("1.25").value == Decimal("1.25")
    assert MoneyAmount(7).value == Decimal(7)
    assert MoneyAmount(Decimal("-3.5")).value == Decimal("-3.5")
    for bad in (0.1, 1.0, True, None, 1j, b"1"):
        with pytest.raises(TypeError):
            MoneyAmount(bad)  # type: ignore[arg-type]
    for json_number in (1, 1.5, True, None):  # the wire takes strings only
        with pytest.raises(TypeError):
            MoneyAmount.from_wire(json_number)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("NaN", "decimal_not_finite"), ("sNaN", "decimal_not_finite"),
        ("Infinity", "decimal_not_finite"), ("-Infinity", "decimal_not_finite"),
        ("-0", "decimal_negative_zero"), ("-0.00", "decimal_negative_zero"),
        ("1.0000000000000", "decimal_scale_exceeded"),
        ("0E-13", "decimal_scale_exceeded"), ("1E+26", "decimal_out_of_range"),
    ],
)  # fmt: skip
def test_decimal_construction_rejects(value: str, code: str) -> None:
    with pytest.raises(DecimalValueError) as info:
        MoneyAmount(Decimal(value))
    assert info.value.code == code


def test_per_class_scale_and_sign() -> None:
    assert FxRate("1.123456789012345678").to_wire() == "1.123456789012345678"
    assert UsdBudget("100.000001").to_wire() == "100.000001"
    assert Ratio("-0.5").to_wire() == "-0.5"
    bad_values: list[tuple[type[BoundedDecimal], str]] = [(FxRate, "1." + "1" * 19)]
    bad_values += [(UsdBudget, "0.0000001"), (Multiplier, "1" + "0" * 12)]
    for positive in (PositiveQuantity, Multiplier, FxRate):
        bad_values += [(positive, "0"), (positive, "0.00"), (positive, "-1")]
    for cls, text in bad_values:
        with pytest.raises(DecimalValueError):
            cls(text)


def test_canonical_wire_output() -> None:
    assert MoneyAmount("1.500").to_wire() == "1.5"
    assert MoneyAmount("0.000").to_wire() == "0"
    assert MoneyAmount(Decimal("1E+3")).to_wire() == "1000"
    biggest = "9" * 26 + "." + "9" * 12
    assert MoneyAmount(biggest).to_wire() == biggest


def test_arithmetic_exact_and_bounded() -> None:
    m = MoneyAmount
    assert m("0.1") + m("0.2") == m("0.3")
    assert m("1") - m("1.000000000001") == m("-0.000000000001")
    assert -m("2.5") == m("-2.5")
    assert (m("1") - m("1")).to_wire() == "0"
    with pytest.raises(DecimalValueError):
        m("9" * 26) + m("1")
    with pytest.raises(DecimalValueError):
        PositiveQuantity("1") - PositiveQuantity("1")
    q = [Quantity("2"), Quantity("-1"), Quantity("0.5")]
    assert sorted(q) == [q[1], q[2], q[0]]


def test_mixed_types_raise() -> None:
    a = MoneyAmount("1")
    for other in (1.0, 0.5, 1, Decimal("1"), Price("1"), None):
        with pytest.raises(TypeError):
            a == other  # noqa: B015
        with pytest.raises(TypeError):
            a < other  # type: ignore[operator]  # noqa: B015
        with pytest.raises(TypeError):
            a >= other  # type: ignore[operator]  # noqa: B015
        with pytest.raises(TypeError):
            a + other  # type: ignore[operator]
        with pytest.raises(TypeError):
            other + a  # type: ignore[operator]


def test_money_currency_rules() -> None:
    usd = Money.of("10.25", "USD")
    assert usd + Money.of("0.75", "USD") == Money.of("11", "USD")
    assert usd - Money.of("0.25", "USD") == Money.of("10", "USD")
    cad = Money.of("1", "CAD")
    with pytest.raises(CurrencyMismatchError):
        usd + cad
    with pytest.raises(CurrencyMismatchError):
        usd < cad  # noqa: B015
    with pytest.raises(CurrencyMismatchError):
        usd >= cad  # noqa: B015
    with pytest.raises(CurrencyMismatchError):
        usd == cad  # noqa: B015
    with pytest.raises(TypeError):
        usd + 1.5  # type: ignore[operator]
    with pytest.raises(TypeError):
        usd == MoneyAmount("10.25")  # noqa: B015
    for bad in ("usd", "US", "USDX", "U5D", ""):
        with pytest.raises(DecimalValueError):
            Money.of("1", bad)


def test_money_wire_object() -> None:
    m = Money.from_wire({"amount": "-12.50", "currency": "EUR"})
    assert m.to_wire() == {"amount": "-12.5", "currency": "EUR"}
    bad: list[dict[str, object]] = [{"amount": 12.5, "currency": "EUR"}]
    bad += [{"amount": "1"}, {"amount": "1", "x": "EUR"}]
    for obj in bad:
        with pytest.raises((TypeError, DecimalValueError)):
            Money.from_wire(obj)


@pytest.mark.parametrize(
    ("value", "quantum", "rounding", "expected"),
    [
        ("2.345", "0.01", "DISPLAY", "2.34"), ("2.355", "0.01", "DISPLAY", "2.36"),
        ("-2.345", "0.01", "DISPLAY", "-2.34"), ("1.001", "0.01", "COST", "1.01"),
        ("-1.001", "0.01", "COST", "-1.01"), ("1.009", "0.01", "PROCEEDS", "1"),
        ("-1.009", "0.01", "PROCEEDS", "-1"), ("7.9", "1", "LOT", "7"),
        ("0.0000000000005", "0.000000000001", "DISPLAY", "0"),
    ],
)  # fmt: skip
def test_quantize_named_rounding(
    value: str, quantum: str, rounding: str, expected: str
) -> None:
    mode = Rounding[rounding]
    q = quantize(MoneyAmount, Decimal(value), quantum=Decimal(quantum), rounding=mode)
    assert q.to_wire() == expected


def test_quantize_rejects_bad_inputs() -> None:
    cost = Rounding.COST
    for quantum in ("0.05", "1E-13", "NaN"):
        with pytest.raises(DecimalValueError):
            quantize(MoneyAmount, Decimal(1), quantum=Decimal(quantum), rounding=cost)
    with pytest.raises(DecimalValueError):
        quantize(MoneyAmount, Decimal("1E+60"), quantum=Decimal(1), rounding=cost)
    with pytest.raises(TypeError):
        quantize(MoneyAmount, 1.5, quantum=Decimal(1), rounding=cost)  # type: ignore[arg-type]


def test_domain_context_traps_float_operations() -> None:
    assert DOMAIN_CONTEXT.prec == 50
    with localcontext(DOMAIN_CONTEXT):
        with pytest.raises(FloatOperation):
            Decimal(0.1)  # noqa: RUF032
        with pytest.raises(FloatOperation):
            Decimal("1") < 0.5  # noqa: B015


def wire_strings(cls: type[BoundedDecimal]) -> st.SearchStrategy[str]:
    whole = st.integers(1 if cls.POSITIVE else 0, 10**cls.INT_DIGITS - 1)
    frac = st.none() | st.text(alphabet="0123456789", min_size=1, max_size=cls.SCALE)
    sign = st.just("") if cls.POSITIVE else st.sampled_from(["", "-"])
    text = st.builds(
        lambda s, w, f: f"{s}{w}" + (f".{f}" if f else ""), sign, whole, frac
    )
    return text.filter(lambda t: not re.fullmatch(r"-0(\.0+)?", t))  # -0 is rejected


@PROFILE
@given(
    st.sampled_from(ALL_CLASSES).flatmap(
        lambda c: st.tuples(st.just(c), wire_strings(c))
    )
)
def test_property_wire_round_trip(case: tuple[type[BoundedDecimal], str]) -> None:
    cls, text = case
    parsed = cls.from_wire(text)
    assert parsed.value == Decimal(text)
    canonical = parsed.to_wire()
    assert re.fullmatch(cls.wire_pattern(), canonical)
    assert cls.from_wire(canonical) == parsed
    assert cls.from_wire(canonical).to_wire() == canonical


@PROFILE
@given(st.text(max_size=40))
def test_property_wire_never_crashes(text: str) -> None:
    try:
        parsed = MoneyAmount.from_wire(text)
    except DecimalValueError:
        return
    assert re.fullmatch(MoneyAmount.wire_pattern(), text)
    assert MoneyAmount.from_wire(parsed.to_wire()) == parsed


# -0 is rejected by design (tested above), so it is excluded here.
amounts = st.decimals(-(10**24), 10**24, places=12, allow_nan=False).filter(
    lambda d: not (d.is_zero() and d.is_signed())
)


@PROFILE
@given(amounts, amounts, amounts)
def test_property_addition_associative_within_currency(
    a: Decimal, b: Decimal, c: Decimal
) -> None:
    x, y, z = (Money.of(v, "USD") for v in (a, b, c))
    assert (x + y) + z == x + (y + z)
    assert (x + y) - y == x


@PROFILE
@given(st.sampled_from(ALL_CLASSES), st.floats(allow_nan=True, allow_infinity=True))
def test_property_floats_rejected(cls: type[BoundedDecimal], value: float) -> None:
    with pytest.raises(TypeError):
        cls(value)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        cls.from_wire(value)  # type: ignore[arg-type]


@PROFILE
@given(st.sampled_from(ALL_CLASSES), st.integers(1, 10**6), st.integers(1, 6))
def test_property_excess_scale_rejected(
    cls: type[BoundedDecimal], digits: int, extra: int
) -> None:
    value = Decimal(digits).scaleb(-(cls.SCALE + extra))  # trailing zeros count too
    with pytest.raises(DecimalValueError):
        cls(value)
    with pytest.raises(DecimalValueError):
        cls.from_wire(format(value, "f"))
