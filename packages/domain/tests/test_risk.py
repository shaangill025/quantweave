"""Pre-trade risk evaluation, feasible sizes and scoped pauses (T020).

SYNTHETIC inputs only: made-up ids, prices and balances. Expected numbers are worked
by hand in the comments, or come from docs/spec/tests/fixtures/numerical_oracles.json.
Base case: NAV 20000 CAD, cash 6000, allocation 5000 with 3000 used, issuer 500,
sector 2000, another holding 14000 with a -5% stress shock, buy at 50 with a fixed
cost of 2 and a -20% shock. Limits: reserve 1000, issuer 10%, sector 25%, planned
loss 250, stress 5% (ratios of account_nav).
"""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.decimals import FxRate, Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.onboarding import Catalogue, load_catalogue
from qw_domain.policy import (
    AccountFacts,
    AdoptionConsent,
    ConflictCode,
    OptionPermission,
    PolicyHistory,
    build_draft,
)
from qw_domain.risk import (
    LIMIT_CODES,
    Outcome,
    ProposedAction,
    RiskCode,
    RiskEvaluation,
    RiskInputs,
    Side,
    evaluate,
)
from qw_domain.risk_pauses import Pause, PauseBook, PauseScope, PauseTrigger, ScopeKind
from qw_domain.valuation import FxQuote, Mark, MarkKind, Unavailable

SPEC = Path(__file__).resolve().parents[3] / "docs/spec"
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
TENANT = "tenant-synth"
IID, OTHER = InstrumentId(UUID(int=1)), InstrumentId(UUID(int=2))


def cad(x: str) -> Money:
    return Money.of(x, "CAD")


def lim(metric: str, value: str, unit: str = "ratio") -> dict[str, str]:
    return {
        "metric": metric, "value": value, "unit": unit, "denominator": "account_nav",
        "window": "per_trade", "account_id": "acct-1", "currency": "CAD",
    }  # fmt: skip


LIMITS = [
    lim("cash_reserve", "1000", "amount"),
    lim("issuer_concentration", "0.10"),
    lim("sector_concentration", "0.25"),
    lim("planned_trade_loss", "250", "amount"),
    lim("stress_loss", "0.05"),
    lim("loss_pause", "0.10"),
]
ANSWERS: dict[str, Any] = {
    "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": ["growth"],
    "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
    "ONB10": "weekly", "ONB11": ["stocks", "etfs"], "ONB12": ["long_term"],
    "ONB13": [{"account_id": "acct-1", "currency": "CAD", "amount": "5000"}],
    "ONB14": LIMITS, "ONB16": "rules_only",
}  # fmt: skip


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    raw = (SPEC / "config/onboarding_questions.json").read_text()
    return load_catalogue(json.loads(raw))


def adopted(
    cat: Catalogue, *acks: ConflictCode, synthetic: bool = False, **changes: Any
) -> PolicyHistory:
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, False),)
    responses = cat.validate({**ANSWERS, **changes})
    draft = build_draft(
        TENANT, responses, facts, as_of=date(2026, 10, 9), synthetic=synthetic
    )
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    consent = AdoptionConsent("user-1", "stepup-1", AT, frozenset(acks), "")
    return history.adopt(1, consent)


def mark(price: str, currency: str = "CAD", age: int = 1) -> Mark:
    when = AT - timedelta(seconds=age)
    return Mark(IID, Price(price), currency, MarkKind.ASK, when, "feed-synth")


INPUTS = RiskInputs(
    as_of=AT, market_max_age=timedelta(minutes=15), fx_max_age=timedelta(hours=1),
    account_max_age=timedelta(seconds=300),
    account_observed_at=AT - timedelta(seconds=60), reconciled=True,
    available_cash=cad("6000"), allocation_used=cad("3000"), marks={IID: mark("50")},
    fx={}, denominators={"account_nav": cad("20000")}, held_units={IID: Quantity(0)},
    position_values={OTHER: cad("14000")}, issuer_exposure={"issuer-x": cad("500")},
    sector_exposure={"sector-y": cad("2000")},
    stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05")},
)  # fmt: skip
BUY = ProposedAction(
    tenant_id=TENANT, account_id="acct-1", currency="CAD", sleeve_id=None,
    strategy_id="strat-1", denominator="account_nav", instrument=IID,
    issuer_id="issuer-x", sector_id="sector-y", horizon="long_term", side=Side.BUY,
    quantity=PositiveQuantity(40), lot=PositiveQuantity(1), stop=Price(48),
    fixed_cost=cad("2"), unit_cost=cad("0"), leveraged_or_inverse=False,
)  # fmt: skip
NO_PAUSES = PauseBook(TENANT)


def run(
    history: PolicyHistory | None, qty: str = "40", ins: RiskInputs = INPUTS, **kw: Any
) -> RiskEvaluation:
    action = replace(BUY, quantity=PositiveQuantity(qty), **kw)
    return evaluate(history, action, ins, NO_PAUSES)


def codes(ev: RiskEvaluation) -> set[RiskCode]:
    return {r.code for r in ev.reasons}


def checks(ev: RiskEvaluation) -> dict[str, Any]:
    return {c.metric.value: c for c in ev.limits}


def test_breach_blocks_with_hand_computed_alternative(cat: Catalogue) -> None:
    h = adopted(cat)
    ev = run(h)
    # post-trade NAV 20000 - 2 = 19998. q=40: issuer 500+2000 > 1999.8; stress
    # 700+400 > 999.9; allocation 3000+2002 > 5000. Capacities: cash 4998/50=99.96,
    # issuer 1499.8/50=29.996, sector 2999.5/50=59.99, loss (250-2)/2=124,
    # stress 299.9/10=29.99, allocation 1998/50=39.96 -> floor(29.99) = 29.
    assert ev.outcome is Outcome.BLOCK
    assert codes(ev) == {
        RiskCode.CONCENTRATION_LIMIT, RiskCode.STRESS_LIMIT, RiskCode.ALLOCATION_LIMIT
    }  # fmt: skip
    assert ev.max_feasible == Quantity(29) and ev.alternative == PositiveQuantity(29)
    c = checks(ev)
    assert c["issuer_concentration"].value == cad("2500")
    assert c["issuer_concentration"].threshold == cad("1999.8")
    assert c["issuer_concentration"].denominator_amount == cad("19998")
    assert c["stress_loss"].value == cad("1100")
    assert c["cash_reserve"].value == cad("3998") and c["cash_reserve"].passed
    fine = run(h, lot=PositiveQuantity("0.01"))
    assert fine.max_feasible == Quantity("29.99")  # stress headroom exactly 0
    assert run(h, "29.99", lot=PositiveQuantity("0.01")).outcome is Outcome.WARN
    assert RiskCode.STRESS_LIMIT in codes(run(h, "30", lot=PositiveQuantity("0.01")))


def test_pass_reports_every_limit_with_denominator_and_provenance(
    cat: Catalogue,
) -> None:
    ev = run(adopted(cat), "20")
    assert ev.outcome is Outcome.WARN and ev.reasons == () and ev.alternative is None
    assert "stop_fill_assumed_no_gap_scenario" in ev.warnings
    c = checks(ev)
    expected = {  # (value, threshold) at q=20; costs 1002, NAV after 19998
        "cash_reserve": ("4998", "1000"),
        "issuer_concentration": ("1500", "1999.8"),
        "sector_concentration": ("3000", "4999.5"),
        "planned_trade_loss": ("42", "250"),
        "stress_loss": ("900", "999.9"),
        "allocation": ("4002", "5000"),
    }
    assert {k: (v.value, v.threshold) for k, v in c.items()} == {
        k: (cad(a), cad(b)) for k, (a, b) in expected.items()
    }
    assert c["issuer_concentration"].ratio == Ratio("0.075007500750075008")
    assert c["issuer_concentration"].denominator == "account_nav"
    assert any(p.startswith("mark:feed-synth@") for p in c["stress_loss"].provenance)
    assert "shock:" + str(IID.uuid) + "=-0.2" in c["stress_loss"].provenance


def test_num07_planned_stop_loss_is_not_a_guaranteed_maximum(cat: Catalogue) -> None:
    raw = (SPEC / "tests/fixtures/numerical_oracles.json").read_text()
    num07 = next(o for o in json.loads(raw)["oracles"] if o["id"] == "NUM07")
    i, e = num07["inputs"], num07["expected"]
    ins = replace(INPUTS, marks={IID: mark(i["entry"])})
    stop, cost = Price(i["stop"]), cad(i["cost"])
    ev = run(adopted(cat), i["quantity"], ins, stop=stop, fixed_cost=cost)
    loss = checks(ev)["planned_trade_loss"]
    assert loss.value == cad(e["planned_loss"])
    assert e["guaranteed_maximum"] is False and "stop_not_guaranteed" in loss.provenance


def test_stop_gap_scenario_sizes_loss_at_the_gap_price(cat: Catalogue) -> None:
    h = adopted(cat)
    assert run(h, "25").outcome is Outcome.WARN  # at the stop: 2*25+2 = 52
    gap = run(h, "25", gap_exits=(Price(40), Price(49)))  # worst exit 40
    assert codes(gap) == {RiskCode.PLANNED_LOSS_LIMIT}
    assert checks(gap)["planned_trade_loss"].value == cad("252")  # 10*25+2
    assert gap.alternative == PositiveQuantity(24)  # 10*24+2 = 242 <= 250
    assert "stop_fill_assumed_no_gap_scenario" not in gap.warnings


@pytest.mark.parametrize("stop", [None, "50", "51"])
def test_unknown_or_nonpositive_loss_abstains(cat: Catalogue, stop: str | None) -> None:
    ev = run(adopted(cat), "1", stop=None if stop is None else Price(stop))
    assert codes(ev) == {RiskCode.LOSS_INPUT_INVALID} and ev.max_feasible is None


def test_withdrawals_tighten_and_deposits_never_relax(cat: Catalogue) -> None:
    h = adopted(cat)
    out = replace(INPUTS, pending_withdrawals=(cad("4000"),))
    ev = run(h, "20", out)
    # cash 2000-1002 = 998 < 1000; NAV 15998: stress 700+200 > 799.9.
    # Capacities: cash 998/50=19.96, stress 99.9/10=9.99 -> 9.
    assert {RiskCode.CASH_RESERVE_LIMIT, RiskCode.STRESS_LIMIT} <= codes(ev)
    assert ev.alternative == PositiveQuantity(9)
    big = run(h, "1", replace(INPUTS, pending_withdrawals=(cad("5500"),)))
    assert RiskCode.WITHDRAWAL_RESERVE_BREACH in codes(big)
    assert big.max_feasible == Quantity(0) and big.alternative is None
    rich = run(h, ins=replace(INPUTS, pending_deposits=(cad("100000"),)))
    assert rich.max_feasible == Quantity(29) and rich.outcome is Outcome.BLOCK
    assert "pending_deposits_not_spendable" in rich.warnings


def test_num06_alternatives_are_not_jointly_fundable(cat: Catalogue) -> None:
    raw = (SPEC / "tests/fixtures/numerical_oracles.json").read_text()
    num06 = next(o for o in json.loads(raw)["oracles"] if o["id"] == "NUM06")
    avail, (first, second) = num06["inputs"]["available"], num06["inputs"]["proposals"]
    assert first == second == "4000"
    h = adopted(cat)
    ins = replace(
        INPUTS, available_cash=cad(avail), allocation_used=cad("0"),
        denominators={"account_nav": cad("100000")}, issuer_exposure={"issuer-x":
        cad("0")},
    )  # fmt: skip
    # 80 units at 50 = 4000; reserve 1000: 5000-4000 = 1000 >= 1000 passes.
    one = run(h, "80", ins, fixed_cost=cad("0"))
    assert one.outcome is not Outcome.BLOCK
    # The caller deducts the first planning commitment (T017/T026) before the second.
    after = replace(
        ins, available_cash=cad(avail) - cad(first), allocation_used=cad(first)
    )
    two = run(h, "80", after, fixed_cost=cad("0"))
    assert RiskCode.CASH_RESERVE_LIMIT in codes(two) and two.alternative is None
    assert num06["expected"] == {
        "jointly_feasible": False, "each_individually_feasible": True
    }  # fmt: skip


USD_NAV = {"account_nav": Money.of("16000", "USD")}
FX = FxQuote("CAD", "USD", FxRate("0.8"), AT - timedelta(minutes=5), "fx-synth")


def test_denominator_in_another_currency_uses_the_fx_quote(cat: Catalogue) -> None:
    ev = run(adopted(cat), ins=replace(INPUTS, denominators=USD_NAV, fx={"USD": FX}))
    assert ev.max_feasible == Quantity(29)  # 16000 USD / 0.8 = 20000 CAD, as base
    assert checks(ev)["stress_loss"].denominator_amount == cad("19998")


MISSING: list[tuple[str, dict[str, Any], RiskCode]] = [
    ("cash", {"available_cash": None}, RiskCode.CASH_UNCONFIRMED),
    ("allocation", {"allocation_used": None}, RiskCode.CASH_UNCONFIRMED),
    ("acct-time", {"account_observed_at": None}, RiskCode.ACCOUNT_STALE),
    # concrete_invariants: price 1 s old, account 360 s old, bound 300 s.
    ("acct-stale", {"account_observed_at": AT - timedelta(seconds=360)},
     RiskCode.ACCOUNT_STALE),
    ("acct-future", {"account_observed_at": AT + timedelta(seconds=1)},
     RiskCode.ACCOUNT_STALE),
    ("unreconciled", {"reconciled": False}, RiskCode.ACCOUNT_CONFLICT),
    ("recon-unknown", {"reconciled": None}, RiskCode.ACCOUNT_CONFLICT),
    ("mark", {"marks": {}}, RiskCode.PRICE_STALE),
    ("mark-stale", {"marks": {IID: mark("50", age=901)}}, RiskCode.PRICE_STALE),
    ("mark-ccy", {"marks": {IID: mark("50", "USD")}}, RiskCode.CURRENCY_MISMATCH),
    ("nav", {"denominators": {}}, RiskCode.DENOMINATOR_UNAVAILABLE),
    ("nav-unavail", {"denominators": {"account_nav": Unavailable("mark_missing", "")}},
     RiskCode.DENOMINATOR_UNAVAILABLE),
    ("nav-zero", {"denominators": {"account_nav": cad("0")}},
     RiskCode.DENOMINATOR_UNAVAILABLE),
    ("fx", {"denominators": USD_NAV}, RiskCode.FX_UNAVAILABLE),
    ("fx-stale", {"denominators": USD_NAV, "fx": {"USD": replace(
        FX, observed_at=AT - timedelta(hours=2))}}, RiskCode.FX_UNAVAILABLE),
    ("issuer", {"issuer_exposure": {}}, RiskCode.EXPOSURE_UNKNOWN),
    ("sector", {"sector_exposure": {}}, RiskCode.EXPOSURE_UNKNOWN),
    ("held", {"held_units": {}}, RiskCode.EXPOSURE_UNKNOWN),
    ("shock-new", {"stress_shocks": {OTHER: Ratio("-0.05")}},
     RiskCode.STRESS_SHOCK_MISSING),
    ("shock-held", {"stress_shocks": {IID: Ratio("-0.2")}},
     RiskCode.STRESS_SHOCK_MISSING),
    ("mark-kind", {"marks": {IID: replace(mark("50"), kind=MarkKind.BID)}},
     RiskCode.MARK_UNSUITABLE),  # a buy needs an ask or last price
    ("issuer-ccy", {"issuer_exposure": {"issuer-x": Money.of("500", "USD")}},
     RiskCode.CURRENCY_MISMATCH),
    ("withdrawal-ccy", {"pending_withdrawals": (Money.of("1", "USD"),)},
     RiskCode.CURRENCY_MISMATCH),
    ("fx-pair", {"denominators": USD_NAV, "fx": {"USD": FxQuote(
        "USD", "CAD", FxRate("1.25"), AT, "fx-synth")}}, RiskCode.FX_UNAVAILABLE),
    ("value-missing", {"held_units": {IID: Quantity(0), OTHER: Quantity(7)},
     "position_values": {}}, RiskCode.EXPOSURE_UNKNOWN),  # held, no value: not 0
    ("value-mismatch", {"held_units": {IID: Quantity(100)},
     "position_values": {IID: cad("4000"), OTHER: cad("14000")}},
     RiskCode.EXPOSURE_UNKNOWN),  # 100 x 50 = 5000, not 4000
]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "change", "code"), MISSING, ids=[m[0] for m in MISSING]
)
def test_missing_or_stale_input_blocks_and_is_never_defaulted(
    cat: Catalogue, name: str, change: dict[str, Any], code: RiskCode
) -> None:
    ev = run(adopted(cat), "1", replace(INPUTS, **change))
    assert ev.outcome is Outcome.BLOCK and code in codes(ev)
    assert ev.limits == () and ev.max_feasible is None and ev.alternative is None


def test_policy_gate_and_eligibility(cat: Catalogue) -> None:
    ev = run(None, "1")
    assert codes(ev) == {RiskCode.POLICY_NOT_ADOPTED} and ev.limits == ()
    synth = run(adopted(cat, synthetic=True), "1")
    assert [r.detail for r in synth.reasons] == ["synthetic_policy"]
    other = run(adopted(cat), "1", account_id="acct-2")
    assert "scope_not_allocated" in {r.detail for r in other.reasons}
    alien = run(adopted(cat), "1", tenant_id="tenant-x")
    assert (RiskCode.POLICY_NOT_ADOPTED, "tenant_mismatch") in {
        (r.code, r.detail) for r in alien.reasons
    }
    for flag in (True, None):  # leveraged/inverse or unknown: no new action (R044)
        ev = run(adopted(cat), "1", leveraged_or_inverse=flag)
        assert codes(ev) == {RiskCode.INSTRUMENT_INELIGIBLE}
    intraday = adopted(
        cat, ConflictCode.MONITORING_EXCLUDES_INTRADAY, ONB12=["long_term", "same_day"]
    )
    ev = run(intraday, "1", horizon="intraday")
    assert [(r.code, r.detail) for r in ev.reasons] == [
        (RiskCode.POLICY_EXCLUSION, "horizon:intraday")
    ]
    assert run(intraday, "1").outcome is Outcome.WARN


HELD = replace(
    INPUTS, held_units={IID: Quantity(100)}, issuer_exposure={"issuer-x": cad("5000")},
    position_values={IID: cad("5000"), OTHER: cad("14000")},
)  # fmt: skip


def test_sells_are_long_only_and_checked_reductions(cat: Catalogue) -> None:
    h = adopted(cat)
    sell = {"side": Side.SELL, "stop": None}
    # Issuer 5000 already breaches 1999.8; selling 10 reduces it, so it passes.
    ev = run(h, "10", HELD, **sell)
    assert ev.outcome is Outcome.PASS and checks(ev)["issuer_concentration"].value == (
        cad("4500")
    )
    assert "planned_trade_loss" not in checks(ev) and "cash_reserve" not in checks(ev)
    short = run(h, "101", HELD, **sell)
    assert codes(short) == {RiskCode.SHORT_NOT_PERMITTED}
    assert short.alternative == PositiveQuantity(100)
    # An existing leveraged position may still be reduced (exposure stays tracked).
    lev = run(h, "10", HELD, leveraged_or_inverse=True, **sell)
    assert lev.outcome is Outcome.PASS


def test_traded_holding_value_comes_from_units_and_fresh_mark(cat: Catalogue) -> None:
    # Held 100 x 50 = 5000 counts in stress whether or not the caller listed it.
    listed = run(adopted(cat), "1", HELD)
    bare = replace(HELD, position_values={OTHER: cad("14000")})
    assert checks(run(adopted(cat), "1", bare))["stress_loss"].value == cad("1710")
    assert checks(listed)["stress_loss"].value == cad("1710")  # 1000+700+10
    near = replace(
        HELD,
        value_tolerance=cad("0.01"),
        position_values={IID: cad("4999.99"), OTHER: cad("14000")},
    )
    assert checks(run(adopted(cat), "1", near))["stress_loss"].value == cad("1710")


def test_sell_that_raises_the_ratio_over_the_limit_blocks(cat: Catalogue) -> None:
    # Issuer 2000 of NAV 20000 = 0.10. Selling 0.01 (0.5) with a fixed cost of 900:
    # 1999.5 / 19100 = 0.104685... > 0.10 and above the pre-trade ratio -> block.
    ins = replace(HELD, issuer_exposure={"issuer-x": cad("2000")})
    kw: dict[str, Any] = {"side": Side.SELL, "stop": None, "fixed_cost": cad("900")}
    ev = run(adopted(cat), "0.01", ins, lot=PositiveQuantity("0.01"), **kw)
    issuer = checks(ev)["issuer_concentration"]
    assert ev.outcome is Outcome.BLOCK and not issuer.passed
    assert RiskCode.CONCENTRATION_LIMIT in codes(ev)
    assert issuer.ratio == Ratio("0.10468586387434555")  # 0.10468586387434554973 up
    # 0.1 x (19100) - (2000 - 50q) >= 0 needs q >= 1.8; stress (pre 0.085) needs
    # q >= 7.65, so a sale of 10 passes as a checked reduction.
    assert run(adopted(cat), "10", ins, **kw).outcome is Outcome.PASS


def test_a_check_the_action_worsens_still_binds(cat: Catalogue) -> None:
    h, hedge = adopted(cat), {IID: Ratio("0.20"), OTHER: Ratio("-0.05")}
    # Selling a hedge: stress loss 1500-1000 = 500, +10 per unit sold; cap 999.9
    # -> at most 49.99 units, so 49.
    ins = replace(HELD, stress_shocks=hedge, position_values={
        IID: cad("5000"), OTHER: cad("30000")})  # fmt: skip
    sold = run(h, "60", ins, side=Side.SELL, stop=None)
    assert codes(sold) == {RiskCode.STRESS_LIMIT} and sold.alternative == (
        PositiveQuantity(49)
    )
    # Buying the hedge while stress (1250) already breaches 999.9: 1250-10q <= 999.9
    # needs q >= 25.01; issuer caps q at 29.996, so the feasible range is [25.01, 29].
    buy_ins = replace(
        INPUTS, stress_shocks=hedge, position_values={OTHER: cad("25000")}
    )
    small = run(h, "20", buy_ins)
    assert codes(small) == {RiskCode.STRESS_LIMIT} and small.max_feasible == Quantity(
        29
    )
    assert run(h, "26", buy_ins).outcome is Outcome.WARN
    tight = replace(buy_ins, issuer_exposure={"issuer-x": cad("800")})  # cap 23.996
    assert run(h, "20", tight).max_feasible == Quantity(0)
    flat = {**hedge, IID: Ratio(0)}  # constant breach: no size can satisfy stress
    none = run(h, "20", replace(buy_ins, stress_shocks=flat))
    assert none.max_feasible == Quantity(0) and none.alternative is None


PRICES = st.decimals(min_value=Decimal(1), max_value=Decimal(500), places=2)
CASH = st.integers(min_value=0, max_value=20000)
LOTS = st.sampled_from(["1", "0.1", "0.01"])


@settings(max_examples=150, deadline=None, derandomize=True)
@given(price=PRICES, cash=CASH, used=CASH, w=st.integers(0, 4000), lot=LOTS,
       unit=st.decimals(min_value=0, max_value=1, places=2),
       issuer=st.integers(0, 2500))  # fmt: skip
def test_feasible_size_is_maximal_and_monotone(
    cat: Catalogue, price: Decimal, cash: int, used: int, w: int, lot: str,
    unit: Decimal, issuer: int,
) -> None:  # fmt: skip
    h = adopted(cat)
    ins = replace(
        INPUTS, marks={IID: mark(str(price))}, available_cash=cad(str(cash)),
        allocation_used=cad(str(min(used, 5000))), pending_withdrawals=(cad(str(w)),),
        issuer_exposure={"issuer-x": cad(str(issuer))},
    )  # fmt: skip
    kw: dict[str, Any] = {
        "lot": PositiveQuantity(lot), "stop": Price(price - Decimal("0.5")),
        "unit_cost": cad(str(unit)),
    }  # fmt: skip
    ev = run(h, "1", ins, **kw)
    best = ev.max_feasible
    assert best is not None
    step = Decimal(lot)
    if best.value >= step:
        fit = run(h, best.to_wire(), ins, **kw)
        assert fit.outcome is not Outcome.BLOCK, fit.reasons
    over = run(h, str(best.value + step), ins, **kw)
    assert over.outcome is Outcome.BLOCK and codes(over) & LIMIT_CODES
    richer = run(h, "1", replace(ins, available_cash=cad(str(cash + 1000))), **kw)
    assert richer.max_feasible is not None and richer.max_feasible.value >= best.value
    dearer = run(h, "1", replace(ins, marks={IID: mark(str(price + 1))}),
                 **{**kw, "stop": Price(price + Decimal("0.5"))})  # fmt: skip
    assert dearer.max_feasible is not None and dearer.max_feasible.value <= best.value


# Scoped pauses (R069): new risk blocked in scope; checked reductions continue.
ACCT = PauseScope(ScopeKind.ACCOUNT, "acct-1")
PAUSE = Pause("pause-1", ACCT, PauseTrigger.DRAWDOWN, AT, "breach-receipt-1", True)
PAUSED = PauseBook(TENANT).pause(PAUSE)
ONE = replace(BUY, quantity=PositiveQuantity(1))


@pytest.mark.parametrize(
    ("kind", "scope_id", "blocked"),
    [
        (ScopeKind.TENANT, TENANT, True), (ScopeKind.ACCOUNT, "acct-1", True),
        (ScopeKind.ACCOUNT, "acct-2", False), (ScopeKind.STRATEGY, "strat-1", True),
        (ScopeKind.STRATEGY, "strat-2", False),
        (ScopeKind.SLEEVE, "sleeve-t", False),  # the action has no sleeve
    ],
)  # fmt: skip
def test_pause_blocks_new_risk_in_its_scope_only(
    cat: Catalogue, kind: ScopeKind, scope_id: str, blocked: bool
) -> None:
    book = PauseBook(TENANT).pause(replace(PAUSE, scope=PauseScope(kind, scope_id)))
    ev = evaluate(adopted(cat), ONE, INPUTS, book)
    assert ev.outcome is (Outcome.BLOCK if blocked else Outcome.WARN)
    assert (RiskCode.RISK_PAUSED in codes(ev)) is blocked
    alien = evaluate(adopted(cat), ONE, INPUTS, PauseBook("tenant-other"))
    assert RiskCode.RISK_PAUSED in codes(alien)


def test_pause_allows_checked_reductions_and_survives_deposits_and_midnight(
    cat: Catalogue,
) -> None:
    sell = replace(BUY, side=Side.SELL, stop=None, quantity=PositiveQuantity(10))
    assert evaluate(adopted(cat), sell, HELD, PAUSED).outcome is Outcome.PASS
    cover = replace(sell, covers_short_call=True)  # not assumed risk-reducing
    assert RiskCode.RISK_PAUSED in codes(evaluate(adopted(cat), cover, HELD, PAUSED))
    later = replace(
        INPUTS, as_of=AT + timedelta(days=1), pending_deposits=(cad("1000000"),)
    )
    assert RiskCode.RISK_PAUSED in codes(evaluate(adopted(cat), ONE, later, PAUSED))
    assert PAUSED.active() == (PAUSE,)
