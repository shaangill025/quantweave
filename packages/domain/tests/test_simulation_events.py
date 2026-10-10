"""Virtual lifecycle events (T041 increment 2; R063, R098, spec 14): cash dividends,
splits, covered-call / cash-secured-put writing, expiry and assignment. SYNTHETIC
accounts, actions, contracts and marks only; expected numbers are hand-computed in
the comments. October 2026 is EDT (local midnight = 04:00Z); the 2026-11-20 expiry
session closes 16:00 EST = 21:00Z."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from qw_domain import simulation as sim
from qw_domain.calendars import CalendarId, SessionRef
from qw_domain.corporate_actions import CashDividend, PositiveRatio, Split
from qw_domain.decimals import Money, Multiplier, PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.option_lifecycle import State
from qw_domain.options import (
    ExerciseStyle,
    OptionContract,
    OptionRight,
    Settlement,
    UnitDeliverable,
)
from qw_domain.risk import Side
from qw_domain.simulation import OrderStatus, OrderStyle, VirtualPortfolio
from qw_domain.valuation import Mark, MarkKind, Unavailable
from test_simulation import CAL, SRC, D, X, at, held, identity_holds, order, pf, run

DIV = CashDividend("div-1", 1, X, date(2026, 10, 14), SRC, at(22, 0),
                   Money.of("0.25", "USD"), record_date=date(2026, 10, 14),
                   pay_date=date(2026, 10, 20))  # fmt: skip


def split(num: int, den: int, version: int = 1) -> Split:
    known = at(22, 0, 12) + timedelta(hours=version)
    return Split("sp-1", version, X, date(2026, 10, 14), SRC, known,
                 PositiveRatio(num, den))  # fmt: skip


def div_pf() -> VirtualPortfolio:
    p = held(pf(), "100", "1000", at(14, 0, 13))
    return held(p, "50", "500", at(14, 0, 14))  # bought on the ex-date: no dividend


def test_dividend_entitles_pre_ex_holders_and_credits_on_the_pay_date() -> None:
    p = div_pf()
    q = sim.apply_dividend(p, DIV, CAL, at(12, 0, 20))
    assert isinstance(q, VirtualPortfolio)
    # 100 units held before 2026-10-14 04:00Z * 0.25 = 25, at 2026-10-20 04:00Z
    assert (q.cash() - p.cash(), q.income(), q.units(X)) == (D(25), D(25), D(150))
    e = q.entries[-1]
    assert (e.kind, e.effective_at, e.ref) == (
        sim.EntryKind.DIVIDEND, at(4, 0, 20), "div-1:v1",
    )  # fmt: skip
    assert identity_holds(q) and q.virtual is True
    assert sim.apply_dividend(q, DIV, CAL, at(13, 0, 21)) is q  # identical replay


def test_dividend_refusals_and_conflicts() -> None:
    p = div_pf()
    assert sim.apply_dividend(p, DIV, CAL, at(3, 59, 20)) == Unavailable(
        "pay_date_not_reached", "2026-10-20"
    )
    no_pay = replace(DIV, pay_date=None)
    assert sim.apply_dividend(p, no_pay, CAL, at(12, 0, 30)) == Unavailable(
        "pay_date_unknown", "div-1"
    )
    late = replace(DIV, known_at=at(23, 0, 20))
    assert sim.apply_dividend(p, late, CAL, at(12, 0, 20)) == Unavailable(
        "action_not_known", "div-1"
    )
    cad = replace(DIV, amount_per_share=Money.of("0.25", "CAD"))
    assert sim.apply_dividend(p, cad, CAL, at(12, 0, 20)) == Unavailable(
        "currency_mismatch", "CAD"
    )
    assert sim.apply_dividend(pf(), DIV, CAL, at(12, 0, 20)) == Unavailable(
        "no_entitlement", "div-1"
    )
    q = sim.apply_dividend(p, DIV, CAL, at(12, 0, 20))
    assert isinstance(q, VirtualPortfolio)
    fixed = replace(DIV, version=2, known_at=at(1, 0, 15),
                    amount_per_share=Money.of("0.30", "USD"))  # fmt: skip
    with pytest.raises(sim.SimError, match="different payload"):
        sim.apply_dividend(q, fixed, CAL, at(12, 0, 20))
    after = held(p, "1", "10", at(14, 0, 21))  # ledger already past the pay date
    with pytest.raises(sim.SimError, match="time_order"):
        sim.apply_dividend(after, DIV, CAL, at(12, 0, 22))


def test_split_scales_units_keeps_cost_and_refuses_unknown_cash_in_lieu() -> None:
    p = held(pf(), "101", "1010", at(14, 0, 13))
    assert sim.apply_split(p, split(2, 1), CAL, at(3, 59, 14)) == Unavailable(
        "action_not_effective", "sp-1"
    )
    q = sim.apply_split(p, split(2, 1), CAL, at(12, 0, 14))
    assert isinstance(q, VirtualPortfolio)
    # 101 * 2 = 202 units, cost 1010 and cash 100000 - 1010 = 98990 unchanged
    assert (q.units(X), q.cost(X), q.cash()) == (D(202), D(1010), D(98990))
    assert q.entries[-1].effective_at == at(4, 0, 14) and identity_holds(q)
    assert sim.apply_split(q, split(2, 1), CAL, at(9, 0, 15)) is q
    with pytest.raises(sim.SimError, match="different payload"):
        sim.apply_split(q, split(3, 1, version=2), CAL, at(9, 0, 15))
    # 101 * 3/2 = 151.5 and 101 / 10 = 10.1: the remainder is cash in lieu of an
    # unknown amount, so the split is refused rather than invented
    for num, den in ((3, 2), (1, 10)):
        assert sim.apply_split(p, split(num, den), CAL, at(12, 0, 14)) == (
            Unavailable("cash_in_lieu_terms_unknown", "sp-1")
        )
    rev = sim.apply_split(held(pf(), "100", "1000", at(14, 0, 13)), split(1, 10),
                          CAL, at(12, 0, 14))  # fmt: skip
    assert isinstance(rev, VirtualPortfolio) and rev.units(X) == D(10)
    later = held(p, "1", "10", at(15, 0, 14))  # entry after the split cut
    with pytest.raises(sim.SimError, match="time_order"):
        sim.apply_split(later, split(2, 1), CAL, at(12, 0, 15))
    assert sim.apply_split(pf(), split(2, 1), CAL, at(12, 0, 14)) == Unavailable(
        "no_entitlement", "sp-1"
    )


# ---- options

EXPIRY = SessionRef(CalendarId("XNYS"), date(2026, 11, 20))
CALL = OptionContract(
    contract_id=InstrumentId(UUID(int=4101)), underlying_id=X, expiry=EXPIRY,
    strike=Price("11"), strike_currency="USD", right=OptionRight.CALL,
    style=ExerciseStyle.AMERICAN, settlement=Settlement.PHYSICAL,
    multiplier=Multiplier("100"),
    deliverables=(UnitDeliverable(X, PositiveQuantity("100")),), adjusted=False,
    terms_verified=True, terms_version=1,
)  # fmt: skip
PUT = replace(CALL, contract_id=InstrumentId(UUID(int=4102)), right=OptionRight.PUT,
              strike=Price("50"))  # fmt: skip
FEE = Money.of("0.65", "USD")
CLOSE = datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
QUOTED, WRITTEN, AUTO = at(13, 5), at(13, 10), Price("0.01")
VP = VirtualPortfolio | Unavailable


def bid(c: OptionContract, px: str, when: datetime = QUOTED) -> Mark:
    return Mark(c.contract_id, Price(px), "USD", MarkKind.BID, when, "synthetic")


def write(p: VirtualPortfolio, c: OptionContract, px: str, eid: str = "w1",
          n: int = 1, when: datetime = WRITTEN) -> VP:  # fmt: skip
    return sim.write_option(p, c, n, bid(c, px), FEE, CAL, when, eid, HOUR)


def settle(p: VirtualPortfolio, c: OptionContract, px: str,
           observed: datetime = CLOSE, as_of: datetime = CLOSE + HOUR / 2,
           auto: Price | None = AUTO) -> VP:  # fmt: skip
    m = Mark(X, Price(px), "USD", MarkKind.REFERENCE, observed, "synthetic-settle")
    return sim.settle_expiry(p, c.contract_id, m, auto, CAL, as_of, HOUR)


def ok(p: VP) -> VirtualPortfolio:
    assert isinstance(p, VirtualPortfolio), p
    return p


def test_covered_call_reserves_units_and_assignment_delivers_them() -> None:
    p = held(pf("2000"), "100", "1005")  # cash 995
    w = ok(write(p, CALL, "0.30"))
    assert w.cash() == D("1024.35")  # 995 + 0.30 * 100 - 0.65
    assert w.free_units(X) == D(0) and w.option(CALL.contract_id).state is State.OPEN
    assert write(w, CALL, "0.30") is w  # identical replay
    with pytest.raises(sim.SimError, match="different payload"):
        write(w, CALL, "0.35")
    assert write(w, CALL, "0.30", "w2") == Unavailable(
        "position_exists", "one virtual position per contract"
    )
    sale = run(w, order("o1", Side.SELL, "1", OrderStyle.MARKET, at(13, 29)))
    assert not sale.results[0].fills  # all 100 units cover the short call
    assert "insufficient_virtual_units" in sale.results[0].notes
    q = ok(settle(w, CALL, "12"))
    # deliver 100 at 11: cash +1100; share gain 1100 - 1005 = 95; premium 30
    assert (q.cash(), q.units(X), q.realized()) == (D("2124.35"), D(0), D(125))
    assert q.option(CALL.contract_id).state is State.ASSIGNMENT_REPORTED
    assert {e.effective_at for e in q.entries[-2:]} == {CLOSE} and identity_holds(q)
    assert settle(q, CALL, "12", as_of=CLOSE + timedelta(days=3)) is q
    with pytest.raises(sim.SimError, match="different settlement"):
        settle(q, CALL, "12.5")


def test_cash_secured_put_reserves_cash_and_assignment_buys_at_strike() -> None:
    w = ok(write(pf("6000"), PUT, "1.20"))
    assert (w.cash(), w.reserved()) == (D("6119.35"), D(5000))  # 6000 + 120 - 0.65
    assert write(w, PUT, "1.20", "w2", when=at(13, 11)) == Unavailable(
        "position_exists", "one virtual position per contract"
    )
    buy = run(w, order("o1", Side.BUY, "200", OrderStyle.MARKET, at(13, 29)))
    # free 6119.35 - 5000 - fee 1 = 1118.35; floor(1118.35 / (10.05 + 0.01)) = 111
    (f,) = buy.results[0].fills
    assert f.quantity == D(111) and "insufficient_virtual_cash" in buy.results[0].notes
    q = ok(settle(w, PUT, "45"))
    assert (q.cash(), q.units(X), q.cost(X)) == (D("1119.35"), D(100), D(5000))
    assert (q.realized(), q.reserved()) == (D(120), D(0)) and identity_holds(q)
    assert q.option(PUT.contract_id).state is State.ASSIGNMENT_REPORTED


def test_put_cover_needs_free_cash_and_call_cover_needs_free_units() -> None:
    assert write(pf("4999"), PUT, "1.20") == Unavailable(
        "cover_insufficient", "put needs free strike cash"
    )
    two = held(pf(), "150", "1500")
    assert write(two, CALL, "0.30", n=2) == Unavailable(
        "cover_insufficient", "call needs 200 free units"
    )


def test_expiry_worthless_uncertain_and_not_reached() -> None:
    w = ok(write(pf("6000"), PUT, "1.20"))
    q = ok(settle(w, PUT, "55"))
    assert q.option(PUT.contract_id).state is State.EXPIRED
    assert (q.realized(), q.reserved(), q.cash()) == (D(120), D(0), D("6119.35"))
    assert identity_holds(q)
    assert settle(w, PUT, "49.995") == Unavailable(
        "assignment_uncertain", "in the money below the auto-exercise threshold"
    )
    assert settle(w, PUT, "45", as_of=CLOSE - HOUR) == Unavailable(
        "expiry_not_reached", "2026-11-20"
    )
    assert settle(w, PUT, "45", observed=CLOSE - HOUR) == Unavailable(
        "settlement_before_expiry", "settlement observed before the expiry close"
    )
    missing = settle(w, PUT, "45", auto=None)
    assert isinstance(missing, Unavailable)
    assert missing.code == "auto_exercise_rule_missing"
    stale = settle(w, PUT, "45", as_of=CLOSE + 2 * HOUR)
    assert isinstance(stale, Unavailable) and stale.code == "settlement_stale"
    later = held(w, "1", "10", CLOSE + HOUR)  # ledger past the expiry close
    with pytest.raises(sim.SimError, match="time_order"):
        settle(later, PUT, "55", as_of=CLOSE + HOUR)
    with pytest.raises(sim.SimError, match="no virtual option"):
        settle(pf(), PUT, "55")


def test_missing_terms_or_state_block_writing() -> None:
    p = held(pf(), "100", "1000")
    raw = replace(CALL, terms_verified=False)  # unknown terms are never verified
    cases = {
        "terms_unverified": raw,
        "multiplier_unknown": replace(raw, multiplier=None),
        "settlement_unknown": replace(raw, settlement=Settlement.UNKNOWN),
    }
    for code, c in cases.items():
        got = write(p, c, "0.30")
        assert isinstance(got, Unavailable) and got.code == "terms_unverified"
        assert code in got.reason
    cash = write(p, replace(CALL, settlement=Settlement.CASH), "0.30")
    assert isinstance(cash, Unavailable) and cash.code == "settlement_unsupported"
    ask = replace(bid(CALL, "0.30"), kind=MarkKind.ASK)
    assert sim.write_option(p, CALL, 1, ask, FEE, CAL, at(13, 10), "w", HOUR) == (
        Unavailable("premium_not_bid", "a sold option is marked at the bid")
    )
    old = bid(CALL, "0.30", at(11, 0))
    got = sim.write_option(p, CALL, 1, old, FEE, CAL, at(13, 10), "w", HOUR)
    assert isinstance(got, Unavailable) and got.code == "premium_stale"
    gone = sim.write_option(p, CALL, 1, bid(CALL, "0.3", CLOSE), FEE, CAL, CLOSE,
                            "w", HOUR)  # fmt: skip
    assert gone == Unavailable("expiry_passed", "2026-11-20")
    with pytest.raises(sim.SimError, match="time_order"):
        write(held(p, "1", "10", at(14, 0)), CALL, "0.30")


def test_corporate_actions_on_an_optioned_underlying_are_refused() -> None:
    w = ok(write(held(pf(), "100", "1000"), CALL, "0.30"))
    assert sim.apply_split(w, split(2, 1), CAL, at(12, 0, 14)) == Unavailable(
        "option_adjustment_required", "sp-1"
    )
    special = replace(DIV, special=True)
    assert sim.apply_dividend(w, special, CAL, at(12, 0, 20)) == Unavailable(
        "option_adjustment_required", "div-1"
    )
    ordinary = ok(sim.apply_dividend(w, DIV, CAL, at(12, 0, 20)))
    assert ordinary.income() == D(25)  # the covered holder keeps the dividend


def test_orders_on_a_virtual_option_contract_are_unsupported() -> None:
    w = ok(write(held(pf(), "100", "1000"), CALL, "0.30"))
    o = replace(order("o1", Side.BUY, "1", OrderStyle.MARKET, at(13, 29)),
                instrument_id=CALL.contract_id)  # fmt: skip
    (res,) = run(w, o).results
    assert res.status is OrderStatus.UNSUPPORTED and not res.fills
    pos = replace(
        w.option(CALL.contract_id), contract=replace(CALL, terms_verified=False)
    )
    tampered = replace(w, options=(pos,))  # an open option whose terms became unusable
    with pytest.raises(sim.SimError, match="without usable terms"):
        run(tampered, order("o2", Side.SELL, "1", OrderStyle.MARKET, at(13, 29)))


# ---- review round 1


def test_fee_above_premium_cannot_leave_cash_short() -> None:
    # put: free cash 5000 - 5000 collateral = 0; + 0.001 * 100 - 0.65 = -0.55
    assert write(pf("5000"), PUT, "0.001") == Unavailable(
        "fee_exceeds_premium", "the write would leave free cash negative"
    )
    spent = held(pf("1000"), "100", "1000")  # cash 0; 0 + 0.1 - 0.65 = -0.55
    assert write(spent, CALL, "0.001") == Unavailable(
        "fee_exceeds_premium", "the write would leave free cash negative"
    )
    assert ok(write(pf("5000"), PUT, "0.0065")).cash() == D(5000)  # 0.65 - 0.65
    zero = sim.write_option(spent, CALL, 1, bid(CALL, "0"), FEE, CAL, WRITTEN, "w",
                            HOUR)  # fmt: skip
    assert zero == Unavailable("premium_not_bid", "a sold option is marked at the bid")


def test_due_bill_special_dividend_waits_for_the_ex_date() -> None:
    due = replace(DIV, effective=date(2026, 10, 25), record_date=None, special=True)
    p = held(pf(), "100", "1000", at(14, 0, 13))
    assert sim.apply_dividend(p, due, CAL, at(12, 0, 20)) == Unavailable(
        "ex_date_not_reached", "2026-10-25"
    )
    p = held(p, "50", "500", at(14, 0, 22))  # bought before the ex-date: entitled
    q = ok(sim.apply_dividend(p, due, CAL, at(12, 0, 26)))
    # 150 * 0.25 = 37.5, credited once both the pay and ex dates are reached
    assert q.income() == D("37.5") and q.entries[-1].effective_at == at(4, 0, 25)


def test_settlement_needs_a_closing_kind_inside_the_window() -> None:
    w = ok(write(pf("6000"), PUT, "1.20"))
    ask = Mark(X, Price("45"), "USD", MarkKind.ASK, CLOSE, "synthetic-settle")
    assert sim.settle_expiry(w, PUT.contract_id, ask, AUTO, CAL, CLOSE + HOUR,
                             HOUR) == Unavailable(
        "settlement_kind_invalid", "settlement must be a last or reference price"
    )  # fmt: skip
    late = CLOSE + timedelta(days=5)  # after the next open (2026-11-23 14:30Z)
    assert settle(w, PUT, "45", observed=late, as_of=late) == Unavailable(
        "settlement_outside_window", "settlement observed after the next session open"
    )
    last = Mark(X, Price("45"), "USD", MarkKind.LAST, CLOSE, "synthetic-settle")
    got = sim.settle_expiry(w, PUT.contract_id, last, AUTO, CAL, CLOSE + HOUR, HOUR)
    assert isinstance(got, VirtualPortfolio)


def test_identical_replays_win_over_later_option_refusals() -> None:
    p = held(pf(), "100", "1000", at(14, 0, 13))
    q = ok(sim.apply_split(p, split(2, 1), CAL, at(12, 0, 14)))
    special = replace(DIV, special=True, effective=date(2026, 10, 15),
                      record_date=None)  # fmt: skip
    q = ok(sim.apply_dividend(q, special, CAL, at(12, 0, 20)))
    quote = bid(CALL, "0.3", at(14, 30, 21))
    w = ok(sim.write_option(q, CALL, 1, quote, FEE, CAL, at(15, 0, 21), "w1", HOUR))
    assert sim.apply_split(w, split(2, 1), CAL, at(16, 0, 21)) is w
    assert sim.apply_dividend(w, special, CAL, at(16, 0, 21)) is w
