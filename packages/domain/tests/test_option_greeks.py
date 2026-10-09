"""BSM marks and Greeks under explicit assumptions (T034; R017, R089). SYNTHETIC
contracts. Oracles: textbook cases (Hull's S=42, K=40: call 4.76, put 0.81) with
values independently computed in IEEE double with erf, compared to 1e-9."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import decimal_math as dm
from qw_domain.calendars import CalendarId, Session, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError
from qw_domain.option_greeks import DayCount, Greeks, Model, ModelInputs, bsm
from qw_domain.options import (
    CashDeliverable,
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.valuation import Mark, MarkKind, Unavailable

D = Decimal
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
CUTOFF = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
CALL = OptionContract(
    contract_id=InstrumentId(UUID("00000000-0000-4000-8000-0000000000c1")),
    underlying_id=UND,
    expiry=SessionRef(CalendarId("XNYS"), date(2026, 11, 20)),
    strike=Price("100"),
    strike_currency="USD",
    right=OptionRight.CALL,
    style=ExerciseStyle.EUROPEAN,
    settlement=Settlement.PHYSICAL,
    multiplier=Multiplier("100"),
    deliverables=(UnitDeliverable(UND, PositiveQuantity("100")),),
    adjusted=False,
    terms_verified=True,
    terms_version=1,
)
PUT = replace(CALL, right=OptionRight.PUT)
# SYNTHETIC expiry session; its close is the exercise cutoff used for T.
OPEN_AT = CUTOFF - timedelta(hours=6, minutes=30)
SESSION = Session(
    CalendarId("XNYS"), "SYNTHETIC", date(2026, 11, 20), OPEN_AT, CUTOFF, False
)


def mark(spot: str, at: datetime) -> Mark:
    return Mark(UND, Price(spot), "USD", MarkKind.MID, at, "SYNTHETIC")


def inputs(
    spot: str, vol: str, rate: str, q: str = "0", hours: int = 8760
) -> ModelInputs:
    at = CUTOFF - timedelta(hours=hours)
    return ModelInputs(
        spot=mark(spot, at),
        vol=Ratio(vol),
        rate=Ratio(rate),
        dividend_yield=Ratio(q),
        valuation_at=at,
        max_age=timedelta(minutes=15),
        expiry_session=SESSION,
        day_count=DayCount.ACT_365F,
    )


def ok(contract: OptionContract, i: ModelInputs) -> Greeks:
    g = bsm(contract, i)
    assert isinstance(g, Greeks), g
    return g


def near(x: Decimal, expected: str, scale: int = 100) -> bool:
    return abs(x - D(expected) * scale) < D("1e-9") * scale


def test_high_precision_reference_point() -> None:
    # Independent reference: erf Maclaurin series and Machin pi at 80 digits
    # (scratchpad script, not this module). S=K=100, T=1 (ACT/365F), r=5%, vol 20%.
    i = inputs("100", "0.2", "0.05")
    c, p = ok(CALL, i), ok(PUT, i)
    ref = {
        c.price: "10.450583572185566781651231209678",
        p.price: "5.573526022256967690793763187644",
        c.delta: "0.636830651175619071223259952428",
        c.gamma: "0.018762017345846893918708493873",
        c.vega: "37.524034691693787837416987745584",
        c.theta: "-6.414027546438195800775436976213",
    }
    for got, want in ref.items():
        assert abs(dm.CTX.subtract(got, D(want).scaleb(2, dm.CTX))) < D("1e-25")
    # The assumption set travels with the result.
    assert c.inputs == i and c.inputs.day_count is DayCount.ACT_365F
    assert (
        c.year_fraction == 1 and c.method == f"closed_form/decimal_prec_{dm.PRECISION}"
    )


@pytest.mark.parametrize(
    ("strike", "case", "call", "put"),
    [
        (
            "100",
            ("100", "0.2", "0.05", "0", 8760),
            "10.450583572185565",
            "5.573526022256971",
        ),
        (
            "95",
            ("100", "0.3", "0.03", "0.02", 2190),
            "8.766250197097662",
            "3.555167485647573",
        ),
    ],
)
def test_price_matches_oracle(
    strike: str, case: tuple[str, str, str, str, int], call: str, put: str
) -> None:
    i = inputs(*case)
    c = ok(replace(CALL, strike=Price(strike)), i)
    p = ok(replace(PUT, strike=Price(strike)), i)
    assert near(c.price, call) and near(p.price, put)
    assert (
        c.model is Model.BSM_EUROPEAN and c.applicable_to_style and c.currency == "USD"
    )


def test_greeks_match_oracle_and_hull() -> None:
    c, p = ok(CALL, inputs("100", "0.2", "0.05")), ok(PUT, inputs("100", "0.2", "0.05"))
    assert near(c.delta, "0.6368306511756191") and near(p.delta, "-0.3631693488243809")
    assert near(c.gamma, "0.018762017345846895") and near(
        p.gamma, "0.018762017345846895"
    )
    assert near(c.vega, "37.52403469169379") and near(c.theta, "-6.414027546438197")
    assert near(p.theta, "-1.657880423934626") and c.year_fraction == 1
    hull = inputs("42", "0.2", "0.1", hours=4380)
    k40 = Price("40")
    hc, hp = ok(replace(CALL, strike=k40), hull), ok(replace(PUT, strike=k40), hull)
    assert round(hc.price, 0) == 476 and round(hp.price, 0) == 81  # 4.76, 0.81 x 100


def test_adjusted_deliverable_uses_effective_strike() -> None:
    # SYNTHETIC adjusted: K=50, M=100 buys 150 units plus USD 12.50 -> K' = 33.25.
    adj = replace(
        CALL,
        strike=Price("50"),
        deliverables=(
            UnitDeliverable(UND, PositiveQuantity("150")),
            CashDeliverable(Money.of("12.50", "USD")),
        ),
        adjusted=True,
        terms_version=2,
    )
    i = inputs("40", "0.35", "0.04", hours=4380)
    assert near(ok(adj, i).price, "1263.9880856722914", 1)
    assert near(
        ok(replace(adj, right=OptionRight.PUT), i).price, "152.7289687897322", 1
    )
    assert ok(adj, i).delta < 150  # units per contract, never a default 100


def test_american_is_labelled_approximation() -> None:
    g = ok(replace(CALL, style=ExerciseStyle.AMERICAN), inputs("100", "0.2", "0.05"))
    assert g.model is Model.BSM_EUROPEAN_APPROX and not g.applicable_to_style


@settings(derandomize=True, database=None, max_examples=120, deadline=None)
@given(
    spot=st.decimals(min_value=1, max_value=500, places=2),
    strike=st.integers(min_value=1, max_value=500),
    vol=st.decimals(min_value=D("0.01"), max_value=3, places=3),
    rate=st.decimals(min_value=D("-0.02"), max_value=D("0.2"), places=4),
    q=st.decimals(min_value=0, max_value=D("0.1"), places=4),
    hours=st.integers(min_value=1, max_value=36_000),
)
def test_parity_delta_bounds_gamma(
    spot: Decimal, strike: int, vol: Decimal, rate: Decimal, q: Decimal, hours: int
) -> None:
    i = inputs(str(spot), str(vol), str(rate), str(q), hours)
    k = Price(strike)
    c, p = ok(replace(CALL, strike=k), i), ok(replace(PUT, strike=k), i)
    t = c.year_fraction
    fwd = spot * dm.exp(-q * t) - strike * dm.exp(-rate * t)
    assert abs(c.price - p.price - 100 * fwd) < D("1e-20") * (1 + spot + strike)
    assert 0 <= c.delta <= 100 and -100 <= p.delta <= 0
    assert abs(c.delta - p.delta - 100 * dm.exp(-q * t)) < D("1e-25")
    assert c.gamma >= 0 and p.gamma >= 0 and c.vega >= 0 and p.vega >= 0


def test_blocked_and_invalid_inputs() -> None:
    i = inputs("100", "0.2", "0.05")
    expired = replace(i, valuation_at=CUTOFF)
    assert bsm(CALL, expired) == Unavailable(
        "contract_expired", "valuation at or after the cutoff"
    )
    # A session other than the contract's expiry session cannot set T.
    for wrong in (
        replace(SESSION, session_date=date(2026, 11, 21)),
        replace(SESSION, calendar_id=CalendarId("XNAS")),
    ):
        r = bsm(CALL, replace(i, expiry_session=wrong))
        assert isinstance(r, Unavailable) and r.code == "expiry_session_mismatch"
    at = i.valuation_at
    for spot, code in (
        (mark("100", at - timedelta(minutes=16)), "spot_stale"),
        (mark("100", at + timedelta(seconds=1)), "spot_after_as_of"),
        (
            replace(mark("100", at), instrument_id=CALL.contract_id),
            "mark_instrument_mismatch",
        ),
        (replace(mark("100", at), currency="CAD"), "currency_mismatch"),
    ):
        r = bsm(CALL, replace(i, spot=spot))
        assert isinstance(r, Unavailable) and r.code == code
    unknown = replace(
        CALL, multiplier=None, style=ExerciseStyle.UNKNOWN, terms_verified=False
    )
    r = bsm(unknown, i)
    assert isinstance(r, Unavailable) and r.code == "terms_unverified"
    assert "multiplier_unknown" in r.reason and "exercise_style_unknown" in r.reason
    for bad, code in (
        (replace(i, vol=Ratio("0")), "vol_not_positive"),
        (replace(i, vol=Ratio("-0.2")), "vol_not_positive"),
        (replace(i, spot=mark("0", at)), "spot_not_positive"),
        (replace(i, spot=mark("-1", at)), "spot_not_positive"),
    ):
        assert isinstance(r := bsm(CALL, bad), Unavailable) and r.code == code
    # Cash component equal to K*M leaves no positive effective strike.
    zero_k = replace(
        CALL,
        deliverables=(
            UnitDeliverable(UND, PositiveQuantity("100")),
            CashDeliverable(Money.of("10000", "USD")),
        ),
        adjusted=True,
    )
    r = bsm(zero_k, i)
    assert isinstance(r, Unavailable) and r.code == "effective_strike_not_positive"
    with pytest.raises(InstantError):
        replace(i, valuation_at=datetime(2026, 1, 2))  # noqa: DTZ001
    with pytest.raises(TypeError):
        replace(i, spot=Price("100"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Price(100.0)  # type: ignore[arg-type]
