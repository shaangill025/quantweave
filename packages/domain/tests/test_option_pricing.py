"""Package pricing gate (T035; R017, R065, R089; spec 09 "Pre-expiry analysis";
options_catalogue.json `indicative_feed_execution_qualified: false`). SYNTHETIC
contracts, quotes and feeds. Oracle: hand-computed conservative package premium
for a 50/55 bull call: pay the 3.60 ask x 100, receive the 1.40 bid x 100 = -220."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from typing import Any
from uuid import UUID

import pytest
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.option_packages import OptionPackage, PackageKind, build_package
from qw_domain.option_pricing import (
    FeedAccess,
    LatencyClass,
    OptionQuote,
    PricingDecision,
    PricingUse,
    price_package,
)
from qw_domain.option_risk import AccountCover, Leg, Structure
from qw_domain.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.rights import (
    EntitlementHistory,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsProfile,
    RightState,
    Use,
    UseScope,
)

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
UND = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
TENANT, REGION, FEED, OPERATOR = "tenant-synth-a", "CA-ON", "feed-synth", "op-synth"


def call(strike: str, n: int) -> OptionContract:
    return OptionContract(
        contract_id=InstrumentId(UUID(f"00000000-0000-4000-8000-{n:012d}")),
        underlying_id=UND,
        expiry=SessionRef(CalendarId("XNYS"), date(2026, 11, 20)),
        strike=Price(strike),
        strike_currency="USD",
        right=OptionRight.CALL,
        style=ExerciseStyle.AMERICAN,
        settlement=Settlement.PHYSICAL,
        multiplier=Multiplier("100"),
        deliverables=(UnitDeliverable(UND, PositiveQuantity("100")),),
        adjusted=False,
        terms_verified=True,
        terms_version=1,
    )


C50, C55 = call("50", 1), call("55", 2)
USD0 = Money.of(0, "USD")
# Caller-entered (stale) premiums: debit 2.00; the quotes below give 2.20.
LONG, SHORT = Money.of(-350, "USD"), Money.of(150, "USD")
LEGS = [Leg(C50, Quantity(1), LONG), Leg(C55, Quantity(-1), SHORT)]
ACCOUNT = AccountCover("USD", Money.of(99999, "USD"), {}, frozenset(Structure))
_PKG = build_package(PackageKind.BULL_CALL_DEBIT, LEGS, fees=USD0)
assert isinstance(_PKG, OptionPackage)
PKG: OptionPackage = _PKG


def ev(kind: EvidenceKind, actor: str | None = None) -> Evidence:
    return Evidence(kind, f"synthetic-{kind.value}", NOW - timedelta(hours=2), actor)


PERMIT = ev(EvidenceKind.PERMISSION, "counsel-synth")
GRANT = Grant(RightState.GRANTED, PERMIT, NOW + timedelta(days=365))
USES = {Use.DERIVED_DATA: GRANT, Use.FINANCIAL_RECOMMENDATION: GRANT}
PROFILE = RightsProfile(USES, {UseScope.PERSONAL: GRANT}, {REGION: GRANT})


def access(latency: LatencyClass | None, qualify: bool = True) -> FeedAccess:
    before, connected = NOW - timedelta(days=1), NOW - timedelta(hours=1)
    h = FeedHistory.new(FEED, "provider-synth").revise(
        "synthetic_options", frozenset(USES), PROFILE, before
    )
    h = h.transition(
        FeedStatus.CONNECTED, OPERATOR, (ev(EvidenceKind.AUTH),), connected
    )
    if qualify:
        proof = (ev(EvidenceKind.WORKLOAD), PERMIT)
        h = h.transition(FeedStatus.QUALIFIED, OPERATOR, proof, connected)
    licence = EntitlementSource.INSTALLATION_LICENSE
    ent = EntitlementHistory.new(TENANT, FEED).revise(
        licence, True, PROFILE, None, before
    )
    reg = Registry().with_provider(Provider("provider-synth", "SYNTHETIC provider"))
    reg = reg.with_feed(h).with_entitlement(ent)
    table = {} if latency is None else {FEED: latency}
    return FeedAccess(reg, TENANT, UseScope.PERSONAL, REGION, table)


def quote(c: OptionContract, bid: str, ask: str) -> OptionQuote:
    return OptionQuote(c.contract_id, Price(bid), Price(ask), "USD", NOW, FEED)


QUOTES = {C50.contract_id: quote(C50, "3.40", "3.60")}
QUOTES[C55.contract_id] = quote(C55, "1.40", "1.60")
type Outcome = tuple[bool, tuple[str, ...], Money | None]


def decide(use: PricingUse, quotes: object = QUOTES, **kw: object) -> Outcome:
    d = price(use, quotes, **kw)
    return d.allowed, d.reasons, d.net_premium


def price(use: PricingUse, quotes: object = QUOTES, **kw: object) -> PricingDecision:
    args = {"access": access(LatencyClass.REAL_TIME), "cover": ACCOUNT} | kw
    d = price_package(PKG, quotes, use, NOW, timedelta(seconds=30), **args)  # type: ignore[arg-type]
    assert d.allowed is (not d.reasons) and d.use is use
    assert (d.package is None) is (not d.allowed)
    return d


def test_qualified_real_time_feed_prices_at_the_conservative_side() -> None:
    allowed = (True, (), Money.of(-220, "USD"))
    assert decide(PricingUse.LIVE) == decide(PricingUse.RESEARCH) == allowed


@pytest.mark.parametrize("use", list(PricingUse))
def test_bounds_are_rebuilt_from_the_priced_premiums(use: PricingUse) -> None:
    repriced = price(use).package
    assert repriced is not None and PKG.max_loss == Money.of(200, "USD")
    premiums = [leg.premium for leg in repriced.legs]
    assert premiums == [Money.of(-360, "USD"), Money.of(140, "USD")]
    assert repriced.max_loss == Money.of(220, "USD")  # the priced debit
    assert repriced.max_profit == Money.of(280, "USD")  # 500 width - 220
    assert repriced.breakevens == (Fraction("52.2"),)
    assert repriced.cover is not None and PKG.cover is None


def test_priced_package_that_fails_its_checks_is_blocked() -> None:
    wide = {**QUOTES, C50.contract_id: quote(C50, "6.00", "6.50")}  # 650 - 140 > 500
    assert price(PricingUse.RESEARCH, wide).reasons == ("no_profit_possible",)
    poor = AccountCover("USD", Money.of(100, "USD"), {}, frozenset(Structure))
    assert price(PricingUse.LIVE, cover=poor).reasons == ("coverage_refused",)


@pytest.mark.parametrize("latency", [LatencyClass.INDICATIVE, LatencyClass.DELAYED])
def test_indicative_or_delayed_feed_blocks_live_pricing_only(
    latency: LatencyClass,
) -> None:
    feed = access(latency)
    blocked = (False, ("feed_not_executable",), None)
    assert decide(PricingUse.LIVE, access=feed) == blocked
    assert decide(PricingUse.RESEARCH, access=feed)[0]
    unknown = access(None)
    assert decide(PricingUse.LIVE, access=unknown)[1] == ("feed_latency_unknown",)
    assert decide(PricingUse.RESEARCH, access=unknown)[0]


SECOND = timedelta(seconds=1)
BAD: list[tuple[dict[str, Any], str]] = [
    ({"bid": Price("1.70")}, "quote_crossed"),
    ({"bid": Price("0")}, "quote_zero"),
    ({"ask": Price("0")}, "quote_zero"),
    ({"observed_at": NOW - 31 * SECOND}, "quote_stale"),
    ({"observed_at": NOW + SECOND}, "quote_after_as_of"),
    ({"currency": "CAD"}, "currency_mismatch"),
    ({"contract_id": C50.contract_id}, "quote_contract_mismatch"),
]


@pytest.mark.parametrize("use", list(PricingUse))
def test_missing_crossed_zero_or_stale_quotes_block_every_use(use: PricingUse) -> None:
    missing = {C50.contract_id: QUOTES[C50.contract_id]}
    assert decide(use, missing) == (False, ("quote_missing",), None)
    for change, code in BAD:
        bad = {**QUOTES, C55.contract_id: replace(QUOTES[C55.contract_id], **change)}
        assert decide(use, bad) == (False, (code,), None), code


def test_feed_rights_and_coverage_gate() -> None:
    unqualified = access(LatencyClass.REAL_TIME, qualify=False)
    for use in PricingUse:
        denied = ("feed_rights_denied:not_qualified",)
        assert decide(use, access=unqualified)[:2] == (False, denied)
    live = decide(PricingUse.LIVE, cover=None)
    assert live[1] == ("coverage_not_checked",)
    assert decide(PricingUse.RESEARCH, cover=None)[0]
