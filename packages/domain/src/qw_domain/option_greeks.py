"""Black-Scholes-Merton marks and Greeks under explicit assumptions (T034; spec 09).

Per-contract results follow from the contract's own deliverable (`contract_terms`):
N underlying units plus cash C for a strike payment of K*M is N options on one unit
struck at K' = (K*M - C) / N. Nothing assumes 100. Inputs are the caller's: a spot
`Mark` of the underlying (fresh: observed at or before `valuation_at` and within
`max_age`), volatility, continuous rate and dividend yield, a day count and the
contract's expiry `Session` from its exchange calendar. T runs to that session's
close (half days included); a provider exercise cutoff later than the close is not
used, so T is never inflated. Discrete dividends are not modelled.
`applicable_to_style` is True only when the closed form's exercise assumption
matches the contract (European); it is still a model estimate, not an executable
price or a calibrated probability. American contracts get the European closed form
labelled `bsm_european_approx` and `applicable_to_style=False` (spec 09 "Pre-expiry
analysis"). Units: delta and gamma in underlying units
per contract, vega per 1.00 of volatility, theta per year of the day count, all in
the strike currency. Stdlib Decimal (`decimal_math`), no float.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction

from qw_domain import decimal_math as dm
from qw_domain.calendars import Session
from qw_domain.decimals import Ratio
from qw_domain.instants import ensure_aware_utc
from qw_domain.option_risk import check_mark, contract_terms
from qw_domain.options import ExerciseStyle, OptionContract, OptionRight
from qw_domain.valuation import Mark, Unavailable

NUMERICAL_METHOD = f"closed_form/decimal_prec_{dm.PRECISION}"


class DayCount(StrEnum):
    ACT_365F = "act/365f"
    ACT_360 = "act/360"


_YEAR_DAYS = {DayCount.ACT_365F: 365, DayCount.ACT_360: 360}


class Model(StrEnum):
    BSM_EUROPEAN = "bsm_european"
    BSM_EUROPEAN_APPROX = "bsm_european_approx"


@dataclass(frozen=True, slots=True)
class ModelInputs:
    spot: Mark  # the underlying; its source and time identify the input quote
    vol: Ratio  # annualised
    rate: Ratio  # continuously compounded
    dividend_yield: Ratio  # continuous; discrete dividends are out of model
    valuation_at: datetime  # as_of
    max_age: timedelta  # for the spot mark
    expiry_session: Session  # the contract's expiry session; T ends at its close
    day_count: DayCount

    def __post_init__(self) -> None:
        kinds = (("spot", Mark), ("vol", Ratio), ("rate", Ratio))
        for name, kind in (*kinds, ("dividend_yield", Ratio), ("max_age", timedelta)):
            if type(getattr(self, name)) is not kind:
                raise TypeError(f"{name} must be {kind.__name__}")
        if type(self.day_count) is not DayCount:
            raise TypeError("day_count must be DayCount")
        if type(self.expiry_session) is not Session:
            raise TypeError("expiry_session must be Session")
        ensure_aware_utc(self.valuation_at)


@dataclass(frozen=True, slots=True)
class Greeks:
    price: Decimal
    delta: Decimal
    gamma: Decimal
    vega: Decimal
    theta: Decimal
    currency: str
    model: Model
    applicable_to_style: bool
    year_fraction: Decimal
    inputs: ModelInputs
    method: str = NUMERICAL_METHOD


def _dec(f: Fraction) -> Decimal:
    return dm.div(Decimal(f.numerator), Decimal(f.denominator))


def year_fraction(start: datetime, end: datetime, day_count: DayCount) -> Decimal:
    span = ensure_aware_utc(end) - ensure_aware_utc(start)
    micros = span // timedelta(microseconds=1)
    return dm.div(Decimal(micros), Decimal(_YEAR_DAYS[day_count] * 86_400_000_000))


def bsm(contract: OptionContract, inputs: ModelInputs) -> Greeks | Unavailable:
    terms = contract_terms(contract)
    if isinstance(terms, Unavailable):
        return terms
    session = inputs.expiry_session
    if (session.calendar_id, session.session_date) != (
        contract.expiry.calendar_id,
        contract.expiry.session_date,
    ):
        return Unavailable("expiry_session_mismatch", "not the contract's expiry")
    t = year_fraction(inputs.valuation_at, session.close_at, inputs.day_count)
    if t <= 0:
        return Unavailable("contract_expired", "valuation at or after the cutoff")
    stale = check_mark(
        inputs.spot,
        contract.underlying_id,
        terms.currency,
        inputs.valuation_at,
        inputs.max_age,
        "spot",
    )
    if stale is not None:
        return stale
    s, sigma = inputs.spot.price.value, inputs.vol.value
    if s <= 0:
        return Unavailable("spot_not_positive", "spot must be > 0")
    if sigma <= 0:
        return Unavailable("vol_not_positive", "volatility must be > 0")
    k, n = _dec(terms.effective_strike), _dec(terms.units)
    r, q = inputs.rate.value, inputs.dividend_yield.value
    c = dm.CTX
    sq_t = dm.sqrt(t)
    sig_t = c.multiply(sigma, sq_t)
    drift = c.add(c.subtract(r, q), dm.div(c.multiply(sigma, sigma), Decimal(2)))
    d1 = dm.div(c.add(dm.ln(dm.div(s, k)), c.multiply(drift, t)), sig_t)
    d2 = c.subtract(d1, sig_t)
    sq = c.multiply(s, dm.exp(c.minus(c.multiply(q, t))))  # S e^{-qT}
    kr = c.multiply(k, dm.exp(c.minus(c.multiply(r, t))))  # K e^{-rT}
    pdf = dm.norm_pdf(d1)
    decay = c.minus(dm.div(c.multiply(c.multiply(sq, pdf), sigma), c.multiply(2, sq_t)))
    sign = 1 if contract.right is OptionRight.CALL else -1
    n1, n2 = dm.norm_cdf(c.multiply(sign, d1)), dm.norm_cdf(c.multiply(sign, d2))
    price = c.multiply(sign, c.subtract(c.multiply(sq, n1), c.multiply(kr, n2)))
    delta = c.multiply(sign, dm.div(c.multiply(sq, n1), s))
    carry = c.subtract(
        c.multiply(q, c.multiply(sq, n1)), c.multiply(r, c.multiply(kr, n2))
    )
    theta = c.add(decay, c.multiply(sign, carry))
    american = contract.style is ExerciseStyle.AMERICAN
    return Greeks(
        price=c.multiply(n, price),
        delta=c.multiply(n, delta),
        gamma=c.multiply(
            n, dm.div(c.multiply(dm.div(sq, s), pdf), c.multiply(s, sig_t))
        ),
        vega=c.multiply(n, c.multiply(c.multiply(sq, pdf), sq_t)),
        theta=c.multiply(n, theta),
        currency=terms.currency,
        model=Model.BSM_EUROPEAN_APPROX if american else Model.BSM_EUROPEAN,
        applicable_to_style=not american,
        year_fraction=t,
        inputs=inputs,
    )
