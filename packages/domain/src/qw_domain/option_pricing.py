"""Quote-based pricing gate for a catalogue package (T035; R017, R065; spec 09
"Pre-expiry analysis", spec 05 mark policy; config/options_catalogue.json
`indicative_feed_execution_qualified: false`).

- Every leg needs a quote for its own contract, in the strike currency, observed at
  or before `at` and within `max_age`, with bid and ask > 0 and bid <= ask. A
  missing, crossed, zero, stale or future quote blocks every use.
- Each quote's feed must pass the T021 rights gate (`rights.check_use`) for the
  use: derived data for research, financial recommendation for live use.
- Live use (a proposal or actionable price) also needs a feed whose latency class
  (the feed spec's `latency_class`) is real time. Indicative and delayed feeds are
  research-only; an unknown class blocks live use. The latency table is a
  caller-supplied trust boundary: rights.py does not record it yet. Live use also
  needs an `AccountCover` for the T034 coverage check, and the exchange session
  of the trade (`session`, from the calendar): a leg that expires in that session
  (or earlier) is excluded as zero-day expiry (`excluded_new_proposals`). The
  session must be within one day of the UTC date of `at`.
- Each leg is priced at the conservative side, in journal sign: a long pays the
  ask, a short receives the bid, per unit x multiplier x contracts, rounded toward
  the payer. The package is then rebuilt (`build_package`) with these premiums,
  so the returned bounds and breakevens are those of the priced premium; any
  refusal of the rebuild blocks. It is a quoted price, never a claimed fill.
Analysis only: nothing here submits or amends an order.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction

from qw_domain.calendars import SessionRef
from qw_domain.decimals import Money, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.option_packages import OptionPackage, build_package
from qw_domain.option_risk import AccountCover, _money
from qw_domain.rights import Registry, Use, UseScope, check_use
from qw_domain.valuation import ValuationBasis


class LatencyClass(StrEnum):
    REAL_TIME = "real_time"
    DELAYED = "delayed"
    INDICATIVE = "indicative"


class PricingUse(StrEnum):
    RESEARCH = "research"
    LIVE = "live"  # proposal or actionable use


_RIGHTS_USE = {
    PricingUse.RESEARCH: Use.DERIVED_DATA,
    PricingUse.LIVE: Use.FINANCIAL_RECOMMENDATION,
}


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """Per-unit bid and ask of one contract from one feed."""

    contract_id: InstrumentId
    bid: Price
    ask: Price
    currency: str
    observed_at: datetime
    feed_id: str

    def __post_init__(self) -> None:
        if type(self.bid) is not Price or type(self.ask) is not Price:
            raise TypeError("bid and ask must be Price")
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class FeedAccess:
    rights: Registry
    tenant_id: str
    scope: UseScope
    jurisdiction: str
    latency: Mapping[str, LatencyClass]  # feed_id -> feed spec latency class


@dataclass(frozen=True, slots=True)
class PricingDecision:
    allowed: bool
    use: PricingUse
    reasons: tuple[str, ...]
    net_premium: Money | None  # journal sign; None unless allowed
    package: OptionPackage | None  # rebuilt with the priced premiums, if allowed


def _session_reasons(
    pkg: OptionPackage, session: SessionRef | None, at: datetime
) -> list[str]:
    """Zero-day expiry is excluded from new proposals (options_catalogue.json).
    The caller's session must be within one day of the UTC date of `at`; a leg
    expiring on or before the later of the two dates is zero-day (conservative)."""
    if session is None:
        return ["trade_session_unknown"]
    if abs(session.session_date - at.date()) > timedelta(days=1):
        return ["trade_session_mismatch"]
    today = max(session.session_date, at.date())
    out = []
    for leg in pkg.legs:
        expiry = leg.contract.expiry
        if expiry.calendar_id != session.calendar_id:
            out.append("trade_session_calendar_mismatch")
        elif expiry.session_date <= today:
            out.append("zero_day_expiry")
    return out


def price_package(
    pkg: OptionPackage,
    quotes: Mapping[InstrumentId, OptionQuote],
    use: PricingUse,
    at: datetime,
    max_age: timedelta,
    *,
    access: FeedAccess,
    cover: AccountCover | None = None,
    session: SessionRef | None = None,
) -> PricingDecision:
    at = ensure_aware_utc(at)
    basis = ValuationBasis(pkg.currency, at, max_age)
    reasons: list[str] = []
    priced = []
    for leg in pkg.legs:
        q = quotes.get(leg.contract.contract_id)
        if q is None:
            reasons.append("quote_missing")
            continue
        if q.contract_id != leg.contract.contract_id:
            reasons.append("quote_contract_mismatch")
            continue
        if q.currency != pkg.currency:
            reasons.append("currency_mismatch")
        if q.bid.value <= 0 or q.ask.value <= 0:
            reasons.append("quote_zero")
        elif q.bid.value > q.ask.value:
            reasons.append("quote_crossed")
        if (stale := basis.stale(q.observed_at, "quote")) is not None:
            reasons.append(stale.code)
        decision = check_use(
            access.rights,
            access.tenant_id,
            q.feed_id,
            _RIGHTS_USE[use],
            at,
            scope=access.scope,
            jurisdiction=access.jurisdiction,
        )
        reasons += [f"feed_rights_denied:{r.code}" for r in decision.reasons]
        if use is PricingUse.LIVE:
            latency = access.latency.get(q.feed_id)
            if latency is None:
                reasons.append("feed_latency_unknown")
            elif latency is not LatencyClass.REAL_TIME:
                reasons.append("feed_not_executable")
        assert leg.contract.multiplier is not None  # the package has known terms
        qty = Fraction(leg.quantity.value)
        side = q.ask if qty > 0 else q.bid
        cash = -qty * Fraction(side.value) * Fraction(leg.contract.multiplier.value)
        priced.append(replace(leg, premium=_money(cash, pkg.currency, up=False)))
    if use is PricingUse.LIVE and cover is None:
        reasons.append("coverage_not_checked")
    if use is PricingUse.LIVE:
        reasons += _session_reasons(pkg, session, at)
    rebuilt = None
    if not reasons:
        args = {"fees": pkg.fees, "stock": pkg.stock, "cover": cover}
        result = build_package(pkg.kind, priced, **args)  # type: ignore[arg-type]
        if isinstance(result, OptionPackage):
            rebuilt = result
        else:
            reasons.append(result.code)
    unique = tuple(dict.fromkeys(reasons))
    if rebuilt is None:
        return PricingDecision(False, use, unique, None, None)
    net = sum((leg.premium for leg in rebuilt.legs), Money.of(0, pkg.currency))
    return PricingDecision(True, use, (), net, rebuilt)
