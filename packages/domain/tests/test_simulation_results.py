"""Ledger-only simulation results (T041 increment 4, T042;
qw_domain.simulation_results).

SYNTHETIC virtual ledger built entry by entry (no bars, no prices). Hand-computed
oracles, from a start of 100000 USD:
- buy 100 X at 10.05, fee 2: cash 100000 - 1005 - 2 = 98993, cost 1005;
- dividend 100 x 0.25 = 25: cash 99018;
- sell 40 X at 11, fee 1.4: average cost out 1005 x 40 / 100 = 402, realized
  440 - 402 = 38, cash 99018 + 440 - 1.4 = 99456.6, units 60;
- buy 3 Y at 7 then sell 3 Y at 6, no fees: realized -3, cash 99453.6, Y units 0.
Totals: fees 3.4, income 25, realized before fees 35.
Realized net (spec 5 §15, spec 4 §17: acquisition fees into lot cost, disposition
fees off proceeds, proportional cost): X pool 1005 + 2 = 1007, 40 of 100 allocate
402.8, net proceeds 440 - 1.4 = 438.6, so 35.8; Y -3; total 32.8.
Through the simulator: the spec oracle (NUM01 in numerical_oracles.json) buy 10 at
50 fee 1, sell 4 at 60 fee 1: realized net 38.6 (before fees 40); and the same
receipt case as above (buy 100 at 10.05 fee 2, sell 40 at 11 fee 1.4): 35.8 (38)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from qw_domain import simulation_results as sr
from qw_domain.calculation import CalcStatus, CalculationRecord
from qw_domain.decimals import Money
from qw_domain.identity import InstrumentId
from qw_domain.risk import Side
from qw_domain.simulation import (
    EntryKind,
    ExecutionModel,
    OrderStyle,
    VirtualEntry,
    VirtualPortfolio,
    simulate,
)
from test_simulation import FEED, LATE, MODEL, at, book, order, pf

D = Decimal
X, Y = InstrumentId(UUID(int=41)), InstrumentId(UUID(int=42))
T0 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)  # never compared with a clock here
DIGEST = "ab" * 32
SID = "SIM-R"


def entry(
    n: int, eid: str, kind: EntryKind, iid: InstrumentId | None, *, cash: str,
    units: str = "0", cost: str = "0", realized: str = "0", income: str = "0",
    fees: str = "0",
) -> VirtualEntry:  # fmt: skip
    return VirtualEntry(eid, kind, T0 + timedelta(hours=n), iid, SID, cash=D(cash),
                        units=D(units), cost=D(cost), realized=D(realized),
                        income=D(income), fees=D(fees))  # fmt: skip


def ledger() -> VirtualPortfolio:
    p = VirtualPortfolio.open(SID, Money.of("100000", "USD"), T0)
    for e in (
        entry(1, "b1", EntryKind.FILL, X, cash="-1007", units="100", cost="1005",
              fees="2"),
        entry(2, "d1", EntryKind.DIVIDEND, X, cash="25", income="25"),
        entry(3, "s1", EntryKind.FILL, X, cash="438.6", units="-40", cost="-402",
              realized="38", fees="1.4"),
        entry(4, "b2", EntryKind.FILL, Y, cash="-21", units="3", cost="21"),
        entry(5, "s2", EntryKind.FILL, Y, cash="18", units="-3", cost="-21",
              realized="-3"),
    ):  # fmt: skip
        p = p.record(e)
    return p


AS_OF = T0 + timedelta(hours=5)


def results(
    p: VirtualPortfolio | None = None, digest: str = DIGEST,
    at: datetime = AS_OF + timedelta(minutes=1),
) -> dict[str, CalculationRecord]:  # fmt: skip
    recs = sr.ledger_results(SID, digest, p or ledger(), as_of=AS_OF, computed_at=at,
                             warnings=("unverified:execution_model_id",))  # fmt: skip
    out: dict[str, CalculationRecord] = {}
    for r in recs:
        key = r.formula_id
        if key == sr.UNITS.partition("/")[0]:
            key += "@" + next(i.value for i in r.inputs if i.name == "instrument")
        assert key not in out
        out[key] = r
    return out


def warnings_of(r: CalculationRecord) -> list[str]:
    w = r.to_contract()["warnings"]
    assert isinstance(w, list)
    return w


def test_ledger_figures_match_hand_computed_oracles() -> None:
    got = {k: (r.result, r.unit, r.status) for k, r in results().items()
           if r.status is CalcStatus.OK}  # fmt: skip
    ok = CalcStatus.OK
    assert got == {
        "virtual_ending_cash": ("99453.6", "USD", ok),
        "virtual_realized_gain_before_fees": ("35", "USD", ok),
        "virtual_realized_net": ("32.8", "USD", ok),
        "virtual_fees": ("3.4", "USD", ok),
        "virtual_income": ("25", "USD", ok),
        f"virtual_position_units@{X.to_wire()}": ("60", "units", ok),
        f"virtual_position_units@{Y.to_wire()}": ("0", "units", ok),
    }


def test_the_short_oracle_from_the_task() -> None:
    p = VirtualPortfolio.open(SID, Money.of("100000", "USD"), T0)
    p = p.record(entry(1, "b1", EntryKind.FILL, X, cash="-1007", units="100",
                       cost="1005", fees="2"))  # fmt: skip
    assert results(p)["virtual_ending_cash"].result == "98993"
    p = p.record(entry(2, "d1", EntryKind.DIVIDEND, X, cash="25", income="25"))
    assert results(p)["virtual_ending_cash"].result == "99018"


def test_price_dependent_figures_are_unavailable_never_guessed() -> None:
    got = results()
    for name, code in sr.PRICED_CODES.items():
        r = got[name]
        assert (r.status, r.result, r.coverage) == (
            CalcStatus.INSUFFICIENT, None, "partial")  # fmt: skip
        assert r.unavailable is not None and r.unavailable.code == code
        assert f"unavailable:{code}" in warnings_of(r)
    assert set(sr.PRICED_CODES) == {"virtual_market_value", "virtual_total_return",
                                    "virtual_benchmark_comparison"}  # fmt: skip


def test_every_record_names_the_simulation_and_its_digest_and_is_simulated() -> None:
    for r in results().values():
        refs = {i.name: (i.value, i.source_record_id) for i in r.inputs}
        assert refs["simulation"] == (SID, SID)
        assert refs["simulation_digest"] == (DIGEST, SID)
        assert refs["ledger_entries"] == ("6", SID)
        assert refs["starting_capital"] == ("100000", SID)
        warnings = warnings_of(r)
        assert warnings[:2] == [sr.SIMULATED, "unverified:execution_model_id"]
        assert f"ref:simulation_digest={DIGEST}" in warnings
        assert r.as_of == AS_OF


def test_recomputing_gives_the_same_ids_and_another_state_does_not() -> None:
    first = [r.content_hash for r in results().values()]
    assert len(set(first)) == len(first) == 10
    later = results(at=AS_OF + timedelta(days=30))  # computed_at is outside the id
    assert [r.content_hash for r in later.values()] == first
    other = results(digest="cd" * 32)
    assert {r.content_hash for r in other.values()}.isdisjoint(first)


def test_inputs_are_checked() -> None:
    for bad in ("AB" * 32, "ab" * 31, ""):
        with pytest.raises(ValueError, match="digest"):
            sr.ledger_results(SID, bad, ledger(), as_of=AS_OF, computed_at=AS_OF)
    with pytest.raises(ValueError, match="simulation"):
        sr.ledger_results("SIM-other", DIGEST, ledger(), as_of=AS_OF,
                          computed_at=AS_OF)  # fmt: skip
    with pytest.raises(ValueError, match="as_of"):
        sr.ledger_results(SID, DIGEST, ledger(), as_of=AS_OF,
                          computed_at=AS_OF - timedelta(seconds=1))  # fmt: skip
    with pytest.raises(ValueError, match="before the ledger"):
        sr.ledger_results(SID, DIGEST, ledger(), as_of=AS_OF - timedelta(hours=1),
                          computed_at=AS_OF)  # fmt: skip


def test_realized_net_matches_the_spec_oracle_through_the_simulator() -> None:
    flat = ExecutionModel("exec-synth-flat", FEED, timedelta(minutes=1), D("1"),
                          D("1"), D("0"), Money.of("1", "USD"),
                          Money.of("0", "USD"))  # fmt: skip
    bars = [("50", "50", "50", "50", "1000"), ("55", "55", "55", "55", "1000"),
            ("60", "60", "60", "60", "1000")]  # fmt: skip
    buy = order("b", Side.BUY, "10", OrderStyle.MARKET, at(13, 29))
    sell = order("s", Side.SELL, "4", OrderStyle.MARKET, at(14, 29))
    r = simulate(pf("1000"), (buy, sell), book(bars), flat, LATE)
    assert [(f.price, f.quantity, f.fees) for x in r.results for f in x.fills] == [
        (D("50"), D("10"), D("1")), (D("60"), D("4"), D("1"))]  # fmt: skip
    p = r.portfolio
    assert (p.cash(), p.units(p.instruments()[0])) == (D("738"), D("6"))  # NUM01
    got = sr.ledger_results("SIM-1", DIGEST, p, as_of=LATE, computed_at=LATE)
    by = {x.formula_id: x.result for x in got}
    assert (by["virtual_realized_net"], by["virtual_realized_gain_before_fees"]) == (
        "38.6", "40")  # fmt: skip


def test_realized_net_of_the_receipt_case_through_the_simulator() -> None:
    bars = [("10", "10.5", "9.8", "10.2", "5000"), ("10", "10", "10", "10", "0"),
            ("11.05", "11.2", "11", "11.1", "4000")]  # fmt: skip
    buy = order("b", Side.BUY, "100", OrderStyle.MARKET, at(13, 29))
    sell = order("s", Side.SELL, "40", OrderStyle.MARKET, at(14, 29))
    r = simulate(pf(), (buy, sell), book(bars), MODEL, LATE)
    assert [(f.price, f.quantity, f.fees) for x in r.results for f in x.fills] == [
        (D("10.05"), D("100"), D("2")), (D("11"), D("40"), D("1.4"))]  # fmt: skip
    got = sr.ledger_results("SIM-1", DIGEST, r.portfolio, as_of=LATE,
                            computed_at=LATE)  # fmt: skip
    by = {x.formula_id: x.result for x in got}
    assert (by["virtual_realized_net"], by["virtual_realized_gain_before_fees"]) == (
        "35.8", "38")  # fmt: skip
    assert by["virtual_ending_cash"] == "99431.6"  # 98993 + 440 - 1.4


def test_realized_net_is_unsupported_for_entries_it_does_not_model() -> None:
    p = ledger().record(entry(5, "opt", EntryKind.OPTION_OPEN, Y, cash="30",
                              cost="-30"))  # fmt: skip
    r = results(p)["virtual_realized_net"]
    assert (r.status, r.result, r.coverage) == (CalcStatus.UNSUPPORTED, None,
                                                "partial")  # fmt: skip
    assert r.unavailable is not None
    assert r.unavailable.code == "unsupported_entry_kind"


def net(p: VirtualPortfolio) -> str:
    got = sr.realized_net(p)
    assert isinstance(got, Money)
    return got.amount.to_wire()


def test_a_split_keeps_the_pool_and_scales_only_units() -> None:
    """NUM01 then NUM02: 6 units costing 300.60 split 2-for-1 are 12 units at
    25.05; selling 6 of them at 30 with fee 1 allocates 150.30 and nets 179, so
    28.7 more: 38.6 + 28.7 = 67.3."""
    p = VirtualPortfolio.open(SID, Money.of("1000", "USD"), T0)
    for e in (
        entry(1, "b", EntryKind.FILL, X, cash="-501", units="10", cost="500",
              fees="1"),
        entry(2, "s", EntryKind.FILL, X, cash="239", units="-4", cost="-200",
              realized="40", fees="1"),
        entry(3, "split", EntryKind.SPLIT, X, cash="0", units="6"),
        entry(4, "s2", EntryKind.FILL, X, cash="179", units="-6", cost="-150",
              realized="30", fees="1"),
    ):  # fmt: skip
        p = p.record(e)
        if e.entry_id == "s":
            assert net(p) == "38.6"
    assert (p.units(X), p.cost(X)) == (D("6"), D("150"))  # ledger cost, fees out
    assert net(p) == "67.3"


def test_allocations_round_half_even_and_a_full_close_takes_the_rest() -> None:
    """Pool 99 + 1 = 100 over 3 units, sold one at a time at 40 without fees:
    100/3 = 33.333333333333(3) -> 33.333333333333; then 66.666666666667/2 =
    33.3333333333335 -> 33.333333333334 (half-even); the close takes the rest,
    33.333333333333. Nets 6.666666666667, 13.333333333333, then 20 = 120 - 100."""
    p = VirtualPortfolio.open(SID, Money.of("1000", "USD"), T0)
    p = p.record(entry(1, "b", EntryKind.FILL, X, cash="-100", units="3",
                       cost="99", fees="1"))  # fmt: skip
    seen = []
    for n in (2, 3, 4):
        p = p.record(entry(n, f"s{n}", EntryKind.FILL, X, cash="40", units="-1",
                           cost="-33", realized="7"))  # fmt: skip
        seen.append(net(p))
    assert seen == ["6.666666666667", "13.333333333333", "20"]
    assert D(seen[-1]) == sum(e.cash for e in p.entries[1:])  # cash in - cash out


def test_a_seed_is_cost_without_a_fee() -> None:
    p = VirtualPortfolio.open(SID, Money.of("1000", "USD"), T0)
    p = p.record(VirtualEntry("seed:x", EntryKind.SEED, T0, X, SID, units=D("10"),
                              cost=D("500"), capital=D("500")))  # fmt: skip
    p = p.record(entry(1, "s", EntryKind.FILL, X, cash="299", units="-5",
                       cost="-250", realized="50", fees="1"))  # fmt: skip
    assert net(p) == "49"  # 300 - 1 - 250
