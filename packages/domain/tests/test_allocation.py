"""Cash-first allocation reference STR-ALLOC-001 (T028 increment 1).

SYNTHETIC accounts, instruments, marks, targets and costs only; no market data. The
numeric expectations are hand-computed in the comments (and NUM06/NUM18 come from
docs/spec/tests/fixtures/numerical_oracles.json). The property's oracle recomputes
each invariant from the inputs, never from the allocator's internals.
"""

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import rights as rt
from qw_domain.allocation import (
    METHOD,
    AllocationError,
    AllocationInputs,
    AllocationResult,
    Code,
    Target,
    TargetSet,
    Tradability,
    allocate,
    allocation_manifest,
)
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.proposals import cash_requirement
from qw_domain.risk import Side
from qw_domain.sources import AccountStatus, AccountView, CashBasis, UnderlyingAccount
from qw_domain.strategy_gate import GateCode, GateInputs, actionable
from qw_domain.strategy_registry import Family, Horizon, StrategyError, StrategyRegistry
from qw_domain.valuation import Mark, MarkKind

FIXTURE = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = json.loads((FIXTURE / "numerical_oracles.json").read_text())["oracles"]
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
CCY, TENANT = "CAD", "tenant-synth-a"
MANIFEST = allocation_manifest("feed-synth-eod")


def iid(n: int) -> InstrumentId:
    return InstrumentId(UUID(int=n))


A, B, C = iid(1), iid(2), iid(3)


def view(
    acct: str,
    cash: str,
    units: dict[InstrumentId, str] | None = None,
    status: str = "reconciled",
    basis: str = "broker_available",
    ccy: str = CCY,
) -> AccountView:
    owner = UnderlyingAccount(acct, TENANT, "inst-synth", "taxable", CCY)
    held = {(i, ccy): Quantity(q) for i, q in (units or {}).items()}
    money = {ccy: Money.of(cash, ccy)}
    basis_of = cast(dict[str, CashBasis], {ccy: basis})
    state = cast(AccountStatus, status)
    return AccountView(owner, state, held, money, basis_of, ("src-synth",), ())


def mark(
    i: InstrumentId, price: str, age: int = 60, ccy: str = CCY,
    kind: MarkKind = MarkKind.LAST,
) -> Mark:  # fmt: skip
    when = AT - timedelta(seconds=age)
    return Mark(i, Price(price), ccy, kind, when, "SYNTHETIC")


def config(band: str) -> dict[str, object]:
    return {"drift_band": Decimal(band)}


def trade(
    acct: str, i: InstrumentId, lot: str = "1", minimum: str = "0",
    fixed: str = "0", unit: str = "0",
) -> Tradability:  # fmt: skip
    m = Money.of
    return Tradability(
        acct, i, PositiveQuantity(lot), m(minimum, CCY), m(fixed, CCY), m(unit, CCY)
    )


def targets(*pairs: tuple[InstrumentId, str], adopted: bool = True) -> TargetSet:
    when = AT - timedelta(days=1) if adopted else None
    chosen = tuple(Target(i, Ratio(w)) for i, w in pairs)
    return TargetSet("targets-synth-1", 1, when, chosen)


def inputs(**kw: Any) -> AllocationInputs:
    base: dict[str, Any] = {
        "currency": CCY, "as_of": AT, "accounts": {}, "account_observed_at": {},
        "account_max_age": timedelta(seconds=300), "reserves": {},
        "targets": targets(), "marks": {}, "mark_max_age": timedelta(minutes=15),
        "tradability": (),
    }  # fmt: skip
    band = kw.pop("drift_band", Ratio("0"))
    base.update(kw)
    base.setdefault("config", config(band.to_wire()))
    if "config_hash" not in base:
        base["config_hash"] = MANIFEST.config_hash(base["config"])
    base.setdefault("unit_priced", frozenset(base["marks"]))
    for acct in base["accounts"]:
        base["account_observed_at"].setdefault(acct, AT - timedelta(seconds=30))
        base["reserves"].setdefault(acct, Money.of(0, CCY))
    return AllocationInputs(**base)


def bought(r: AllocationResult) -> dict[tuple[str, InstrumentId], tuple[str, str]]:
    """(account, instrument) -> normalized (quantity, cash required)."""
    out = {}
    for c in r.candidates:
        qty, cash = c.quantity.to_wire(), c.cash_required.amount.to_wire()
        out[(c.account_id, c.instrument_id)] = (n(qty), n(cash))
    return out


def codes(reasons: tuple[Any, ...]) -> set[tuple[Code, str]]:
    return {(x.code, x.subject) for x in reasons}


def n(text: str) -> str:
    return format(Decimal(text).normalize(), "f")


# --- hand-computed positive cases ------------------------------------------------


def test_cash_first_largest_relative_underweight_with_costs_and_reserve() -> None:
    # base = cash 1000 + A 10 x 50 = 1500. Targets 0.4 each -> 600 each.
    # A: deficit 100, relative 100/600; B: deficit 600, relative 1 -> B first.
    # Free cash = 1000 - reserve 100 = 900.
    # B: by deficit floor(600/33) = 18; by cash floor((900-1)/33.01) = 27 -> 18.
    #    cash = 1 + 18 x 33.01 = 595.18; free left 304.82.
    # A: by deficit floor(100/50) = 2, notional 100 >= minimum 50;
    #    cash = 1 + 2 x 50.01 = 101.02; free left 203.80; account cash 303.80.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "1000", {A: "10"})},
        reserves={"acct-1": Money.of(100, CCY)},
        targets=targets((A, "0.4"), (B, "0.4")),
        marks={A: mark(A, "50"), B: mark(B, "33")},
        tradability=(
            trade("acct-1", A, minimum="50", fixed="1", unit="0.01"),
            trade("acct-1", B, minimum="50", fixed="1", unit="0.01"),
        ),
        drift_band=Ratio("0.01"),
    )
    r = allocate(ins)
    assert r.method == METHOD and not r.blocked
    assert [c.instrument_id for c in r.candidates] == [B, A]  # allocation order
    assert bought(r) == {
        ("acct-1", B): ("18", "595.18"),
        ("acct-1", A): ("2", "101.02"),
    }
    assert all(c.reason is Code.BUY_UNDERWEIGHT for c in r.candidates)
    assert r.capital_base == Money.of(1500, CCY)
    assert r.cash_after["acct-1"] == Money.of("303.80", CCY)
    # B after = 594 = 0.396 of 1500, within the 0.01 band; A exactly on target.
    assert not r.prerequisites
    dev = {d.instrument_id: d for d in r.deviations}
    assert dev[B].weight_after == Ratio("0.396") and dev[A].weight_after == Ratio("0.4")


def test_order_is_relative_not_absolute_underweight() -> None:
    # base = cash 600 + A 40 x 10 = 1000; free = 600 - reserve 300 = 300.
    # A: target 800, deficit 400, relative 0.5. B: target 200, deficit 200, relative 1.
    # Relative order: B 20 units (200), then A 10 units (100).
    # (Absolute order would buy A 30 units and leave nothing for B.)
    ins = inputs(
        accounts={"acct-1": view("acct-1", "600", {A: "40"})},
        reserves={"acct-1": Money.of(300, CCY)},
        targets=targets((A, "0.8"), (B, "0.2")),
        marks={A: mark(A, "10"), B: mark(B, "10")},
        tradability=(trade("acct-1", A), trade("acct-1", B)),
        drift_band=Ratio("0.25"),
    )
    r = allocate(ins)
    order = [(c.instrument_id, n(c.quantity.to_wire())) for c in r.candidates]
    assert order == [(B, "20"), (A, "10")]
    assert r.cash_after["acct-1"] == Money.of(300, CCY)  # the reserve is untouched
    # A ends at 500 vs 800: short 300 = 0.3 of the base, outside the 0.25 band.
    assert codes(r.prerequisites) == {(Code.FUNDING_REQUIRED, A.to_wire())}


@pytest.mark.parametrize(
    ("weight", "band", "expect"),
    [
        ("0.85", "0.05", None),  # 900 vs 850: over by 50 = 0.05, on the band
        ("0.85", "0.049", Code.SALE_REQUIRED),
        ("0.95", "0.05", None),  # 900 vs 950: short 50, less than one 100 lot
        ("0.95", "0.049", Code.FUNDING_REQUIRED),
    ],
)
def test_drift_band_is_strict_and_absolute(
    weight: str, band: str, expect: Code | None
) -> None:
    # base = cash 100 + A 9 x 100 = 1000.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100", {A: "9"})},
        targets=targets((A, weight)),
        marks={A: mark(A, "100")},
        tradability=(trade("acct-1", A),),
        drift_band=Ratio(band),
    )
    r = allocate(ins)
    assert not r.candidates
    assert codes(r.prerequisites) == ({(expect, A.to_wire())} if expect else set())


def test_holding_outside_targets_is_unmanaged_not_a_sale() -> None:
    # base = cash 100 + C 9 x 100 = 1000; A 0.1 -> 100 -> 10 units; C untargeted.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100", {C: "9"})},
        targets=targets((A, "0.1")),
        marks={A: mark(A, "10"), C: mark(C, "100")},
        tradability=(trade("acct-1", A),),
    )
    r = allocate(ins)
    assert bought(r) == {("acct-1", A): ("10", "100")}
    assert codes(r.prerequisites) == {(Code.UNMANAGED_HOLDING, C.to_wire())}


def test_fractional_lot_rounds_down_and_never_overspends() -> None:

    # base 100 (cash only), target A 1.0 at price 3, lot 0.001, fixed 0.5.
    # by deficit floor(100/3, 0.001) = 33.333; by cash floor(99.5/3, 0.001) = 33.166
    # cash = 0.5 + 33.166 x 3 = 99.998 <= 100.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100")},
        targets=targets((A, "1")),
        marks={A: mark(A, "3")},
        tradability=(trade("acct-1", A, lot="0.001", fixed="0.5"),),
    )
    r = allocate(ins)
    assert bought(r) == {("acct-1", A): ("33.166", "99.998")}
    assert r.cash_after["acct-1"] == Money.of("0.002", CCY)


def test_num06_two_4000_buys_cannot_jointly_consume_5000() -> None:
    o = next(x for x in ORACLES if x["id"] == "NUM06")
    available = o["inputs"]["available"]
    want = o["inputs"]["proposals"]
    assert o["expected"]["jointly_feasible"] is False
    assert o["expected"]["each_individually_feasible"] is True
    # base = 5000 cash + C 5000 units x 1 = 10000; A and B 0.4 -> 4000 each, C 0.2.
    # A and B tie on relative underweight 1; canonical id breaks it: A first.
    ins = inputs(
        accounts={"acct-1": view("acct-1", available, {C: "5000"})},
        targets=targets((A, "0.4"), (B, "0.4"), (C, "0.2")),
        marks={i: mark(i, "1") for i in (A, B, C)},
        tradability=tuple(trade("acct-1", i) for i in (A, B)),
        drift_band=Ratio("0.05"),
    )
    r = allocate(ins)
    got = bought(r)
    assert got[("acct-1", A)][1] == n(want[0])  # individually feasible
    assert got[("acct-1", B)][1] == "1000"  # only the remainder
    spent = sum(Decimal(c) for _, c in got.values())
    assert spent == Decimal(available)
    # Not cured by cash: B short 3000 (0.3 > 0.05); C over by 3000 (needs a sale).
    assert codes(r.prerequisites) == {
        (Code.FUNDING_REQUIRED, B.to_wire()),
        (Code.SALE_REQUIRED, C.to_wire()),
    }


def test_no_assumed_sale_proceeds_and_overweight_never_bought() -> None:
    # base = cash 300 + A 14 x 50 = 1000; A target 0.3 (300, over by 400), B 0.7.
    # B: floor(300/100) = 3 by cash; no sale of A funds more of B.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "300", {A: "14"})},
        targets=targets((A, "0.3"), (B, "0.7")),
        marks={A: mark(A, "50"), B: mark(B, "100")},
        tradability=(trade("acct-1", A), trade("acct-1", B)),
        drift_band=Ratio("0.05"),
    )
    r = allocate(ins)
    assert {k: q for k, (q, _) in bought(r).items()} == {("acct-1", B): "3"}
    assert codes(r.prerequisites) == {
        (Code.SALE_REQUIRED, A.to_wire()),
        (Code.FUNDING_REQUIRED, B.to_wire()),
    }


def test_cash_in_another_account_cannot_fund_a_buy() -> None:
    # R043. acct-1 (no cash) is the only account permitting A; acct-2 has 500.
    # base 500; A, B 0.5 each -> 250. B bought 25 x 10 in acct-2; A cannot be.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "0"), "acct-2": view("acct-2", "500")},
        targets=targets((A, "0.5"), (B, "0.5")),
        marks={A: mark(A, "10"), B: mark(B, "10")},
        tradability=(trade("acct-1", A), trade("acct-2", B)),
    )
    r = allocate(ins)
    assert {k: q for k, (q, _) in bought(r).items()} == {("acct-2", B): "25"}
    assert (Code.INSUFFICIENT_CASH, f"acct-1:{A.to_wire()}") in codes(r.abstentions)
    assert (Code.FUNDING_REQUIRED, A.to_wire()) in codes(r.prerequisites)


def test_minimum_trade_and_sub_lot_deficits_abstain() -> None:
    # base 1000; A held 9 x 100 = 900, target 0.95 -> deficit 50 < one 100 lot.
    # B target 0.05 = 50; price 20 -> 2 units = 40 < minimum 45.
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100", {A: "9"})},
        targets=targets((A, "0.95"), (B, "0.05")),
        marks={A: mark(A, "100"), B: mark(B, "20")},
        tradability=(trade("acct-1", A), trade("acct-1", B, minimum="45")),
        drift_band=Ratio("0.1"),
    )
    r = allocate(ins)
    assert not r.candidates
    assert codes(r.abstentions) == {
        (Code.BELOW_LOT, f"acct-1:{A.to_wire()}"),
        (Code.BELOW_MINIMUM_TRADE, f"acct-1:{B.to_wire()}"),
    }


# --- blocked and abstention cases ------------------------------------------------


def test_num18_quote_freshness_does_not_refresh_account_state() -> None:
    o = next(x for x in ORACLES if x["id"] == "NUM18")["inputs"]
    age = o["account_age_seconds"]
    ins = inputs(
        accounts={"acct-1": view("acct-1", "1000")},
        account_observed_at={"acct-1": AT - timedelta(seconds=age)},
        account_max_age=timedelta(seconds=o["approved_account_max_age_seconds"]),
        targets=targets((A, "1")),
        marks={A: mark(A, "10", age=o["quote_age_seconds"])},
        tradability=(trade("acct-1", A),),
    )
    r = allocate(ins)
    assert not r.candidates and r.capital_base is None  # sizing_allowed: false
    assert codes(r.blocked) == {(Code.ACCOUNT_STALE, "acct-1")}


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        ({"marks": {}}, (Code.MARK_MISSING, A.to_wire())),
        ({"marks": {A: mark(A, "10", age=3600)}}, (Code.MARK_STALE, A.to_wire())),
        ({"marks": {A: mark(A, "10", age=-5)}}, (Code.MARK_STALE, A.to_wire())),
        ({"marks": {A: mark(A, "10", ccy="USD")}},
         (Code.CURRENCY_UNSUPPORTED, A.to_wire())),
        ({"accounts": {"acct-1": view("acct-1", "100", {A: "1"}, status="conflicted")}},
         (Code.ACCOUNT_UNAVAILABLE, "acct-1")),
        ({"accounts": {"acct-1": view("acct-1", "100", {A: "1"}, ccy="USD")}},
         (Code.CURRENCY_UNSUPPORTED, "acct-1")),
        ({"account_observed_at": {"acct-1": AT + timedelta(seconds=1)}},
         (Code.ACCOUNT_STALE, "acct-1")),
        ({"targets": targets((A, "1"), adopted=False)},
         (Code.TARGETS_NOT_ADOPTED, "targets-synth-1")),
        ({"accounts": {}}, (Code.ACCOUNT_UNAVAILABLE, "none")),
        ({"accounts": {"acct-1": view("acct-1", "-1", {A: "1"})}},
         (Code.CASH_UNKNOWN, "acct-1")),
        ({"marks": {A: mark(A, "10", kind=MarkKind.BID)}},
         (Code.MARK_KIND_UNSUITABLE, A.to_wire())),
        ({"config": config("1.5"), "config_hash": "0" * 64},
         (Code.CONFIG_INVALID, "config")),
        # The caller's band is not the adopted one: refused, never used.
        ({"config": config("0.5"), "config_hash": MANIFEST.config_hash(config("0.05"))},
         (Code.CONFIG_MISMATCH, "config")),
        ({"accounts": {"acct-1": view("acct-1", "100", {A: "-1"})}},
         (Code.SHORT_UNSUPPORTED, A.to_wire())),
        ({"unit_priced": frozenset({B})}, (Code.NOT_UNIT_PRICED, A.to_wire())),
    ],
)  # fmt: skip
def test_unknown_state_blocks_the_run_never_zero(
    change: dict[str, Any], expect: tuple[Code, str]
) -> None:
    kw: dict[str, Any] = {
        "accounts": {"acct-1": view("acct-1", "100", {A: "1"})},
        "targets": targets((A, "0.5"), (B, "0.5")),
        "marks": {A: mark(A, "10"), B: mark(B, "10")},
        "tradability": (trade("acct-1", A), trade("acct-1", B)),
    }
    if "marks" in change:
        change = {"marks": {**change["marks"], B: mark(B, "10")}}
    r = allocate(inputs(**{**kw, "unit_priced": frozenset({A, B}), **change}))
    assert expect in codes(r.blocked)
    assert not r.candidates and r.capital_base is None and not r.deviations


def test_unusable_target_mark_or_permission_abstains_only_that_instrument() -> None:
    # A is not held, so its stale mark does not affect the base: only A abstains.
    # C has no account permitting it; D is not confirmed unit-priced (e.g. an
    # option). B proceeds: base 100 -> target 40 -> 4 units.
    d = iid(4)
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100")},
        targets=targets((A, "0.2"), (B, "0.4"), (C, "0.2"), (d, "0.2")),
        marks={A: mark(A, "10", age=3600), B: mark(B, "10"), C: mark(C, "10"),
               d: mark(d, "10")},
        tradability=(trade("acct-1", A), trade("acct-1", B), trade("acct-1", d)),
        unit_priced=frozenset({A, B, C}),
    )  # fmt: skip
    r = allocate(ins)
    assert {k: q for k, (q, _) in bought(r).items()} == {("acct-1", B): "4"}
    assert codes(r.abstentions) == {
        (Code.MARK_STALE, A.to_wire()),
        (Code.NO_PERMITTED_ACCOUNT, C.to_wire()),
        (Code.NOT_UNIT_PRICED, d.to_wire()),
    }


@pytest.mark.parametrize(
    ("basis", "reserves", "expect"),
    [
        ("user_reported", {"acct-1": Money.of(0, CCY)}, Code.CASH_NOT_SPENDABLE),
        ("broker_available", {}, Code.RESERVE_MISSING),
    ],
)
def test_unspendable_cash_is_not_spent(
    basis: str, reserves: dict[str, Money], expect: Code
) -> None:
    ins = inputs(
        accounts={"acct-1": view("acct-1", "100", basis=basis)},
        targets=targets((A, "1")),
        marks={A: mark(A, "10")},
        tradability=(trade("acct-1", A),),
    )
    r = allocate(replace(ins, reserves=reserves))
    assert not r.candidates and not r.blocked
    assert (expect, "acct-1") in codes(r.abstentions)


@pytest.mark.parametrize(
    "bad",
    [
        lambda: targets((A, "0.6"), (B, "0.5")),  # sum > 1
        lambda: targets((A, "-0.1")),
        lambda: targets((A, "0.5"), (A, "0.2")),  # duplicate
        lambda: trade("acct-1", A, lot="0.5"),  # lot not a power of ten
        lambda: trade("acct-1", A, fixed="-1"),
        lambda: inputs(tradability=(trade("acct-1", A), trade("acct-1", A))),
        lambda: inputs(
            reserves={"acct-1": Money.of(1, "USD")},
            accounts={"acct-1": view("acct-1", "1")},
        ),
    ],
)
def test_invalid_inputs_are_refused(bad: Any) -> None:
    with pytest.raises(AllocationError):
        bad()


# --- proposal feed and registry --------------------------------------------------


def test_candidate_feeds_a_proposed_action_with_matching_cash() -> None:
    ins = inputs(
        accounts={"acct-1": view("acct-1", "1000")},
        targets=targets((A, "1")),
        marks={A: mark(A, "33")},
        tradability=(trade("acct-1", A, fixed="1", unit="0.01"),),
    )
    c = allocate(ins).candidates[0]
    action = c.to_action(
        tenant_id=TENANT, strategy_id="STR-ALLOC-001", denominator="account_nav",
        issuer_id="issuer-synth", sector_id="sector-synth", leveraged_or_inverse=False,
    )  # fmt: skip
    assert action.side is Side.BUY and action.horizon == Horizon.LONG_TERM.value
    assert action.quantity == c.quantity and action.stop is None
    # 30 units: 1 + 30 x 33.01 = 991.30
    assert c.cash_required == Money.of("991.30", CCY)
    assert cash_requirement(action, c.price) == c.cash_required


def test_manifest_registers_and_the_gate_fails_closed() -> None:
    m = allocation_manifest("feed-synth-eod")
    assert (m.strategy_id, m.family, m.horizon) == (
        "STR-ALLOC-001",
        Family.ALLOCATION,
        Horizon.LONG_TERM,
    )
    reg = StrategyRegistry().register(m)
    assert m.resolve({}) == config("0") and m is MANIFEST
    with pytest.raises(StrategyError, match="config"):
        m.resolve({"drift_band": Decimal("1.5")})
    scope = rt.UseScope.PERSONAL
    gate = GateInputs(rt.Registry(), scope, "CA-ON", None, {}, frozenset())
    d = actionable(reg, TENANT, m.strategy_id, m.version, {}, AT, gate)
    got = {r.code for r in d.reasons}
    assert not d.allowed  # catalogue: action_eligible false until qualification
    assert {GateCode.CAPABILITY_MISSING, GateCode.OPERATIONAL_NOT_PASSED} <= got


# --- property --------------------------------------------------------------------

IDS = [iid(k) for k in range(1, 5)]
money2 = st.integers(0, 500_000).map(lambda c: Decimal(c).scaleb(-2))


@st.composite
def worlds(draw: st.DrawFn) -> AllocationInputs:
    k = draw(st.integers(1, 4))
    ids = IDS[:k]
    accts = [f"acct-{j}" for j in range(draw(st.integers(1, 2)))]
    prices = {i: Decimal(draw(st.integers(1, 50_000))).scaleb(-2) for i in ids}
    accounts = {
        a: view(a, str(draw(money2)),
                {i: str(draw(st.integers(0, 200))) for i in ids if draw(st.booleans())})
        for a in accts
    }  # fmt: skip
    pct = draw(st.lists(st.integers(0, 100), min_size=k, max_size=k))
    total = sum(pct) or 1
    weights = [Decimal(p * 100 // total).scaleb(-2) for p in pct]  # sum <= 1
    trades = tuple(
        trade(a, i, lot=draw(st.sampled_from(["1", "0.1", "0.001"])),
              minimum=str(draw(st.integers(0, 50))), fixed=str(draw(st.integers(0, 5))),
              unit=str(Decimal(draw(st.integers(0, 10))).scaleb(-2)))
        for a in accts for i in ids if draw(st.booleans())
    )  # fmt: skip
    return inputs(
        accounts=accounts,
        reserves={a: Money.of(draw(st.integers(0, 100)), CCY) for a in accts},
        targets=targets(*zip(ids, map(str, weights), strict=True)),
        marks={i: mark(i, str(p)) for i, p in prices.items()},
        tradability=trades,
        drift_band=Ratio(Decimal(draw(st.integers(0, 10))).scaleb(-2)),
    )


def _shuffled(ins: AllocationInputs, seed: int) -> AllocationInputs:
    rnd = random.Random(seed)

    def mix(d: Any) -> Any:
        items = list(d.items())
        rnd.shuffle(items)
        return dict(items)

    ts, tr = list(ins.targets.targets), list(ins.tradability)
    rnd.shuffle(ts)
    rnd.shuffle(tr)
    return replace(
        ins, accounts=mix(ins.accounts), marks=mix(ins.marks),
        reserves=mix(ins.reserves), account_observed_at=mix(ins.account_observed_at),
        targets=replace(ins.targets, targets=tuple(ts)), tradability=tuple(tr),
    )  # fmt: skip


@settings(max_examples=300, deadline=None)
@given(worlds(), st.integers(0, 2**32))
def test_property_cash_never_overspent_and_weights_move_toward_target(
    ins: AllocationInputs, seed: int
) -> None:
    r = allocate(ins)
    assert not r.blocked
    price = {i: m.price.value for i, m in ins.marks.items()}
    held = {i: Decimal(0) for i in price}
    for v in ins.accounts.values():
        for (i, _), units in v.units.items():
            held[i] += units.value
    base = sum(v.cash[CCY].amount.value for v in ins.accounts.values()) + sum(
        held[i] * price[i] for i in price
    )
    assert r.capital_base == Money.of(base, CCY)
    goal = {t.instrument_id: t.weight.value * base for t in ins.targets.targets}
    trades = {(t.account_id, t.instrument_id): t for t in ins.tradability}
    spent = {a: Decimal(0) for a in ins.accounts}
    after = dict(held)
    for c in r.candidates:
        t = trades[(c.account_id, c.instrument_id)]
        q = c.quantity.value
        p = price[c.instrument_id]
        assert q > 0 and q % t.lot.value == 0
        assert q * p >= t.min_notional.amount.value
        exact = t.fixed_cost.amount.value + q * (p + t.unit_cost.amount.value)
        assert c.cash_required.amount.value >= exact  # rounded up, never down
        assert c.cash_required.amount.value - exact < Decimal("1e-12")
        assert held[c.instrument_id] * price[c.instrument_id] < goal[c.instrument_id]
        spent[c.account_id] += c.cash_required.amount.value
        after[c.instrument_id] += q
    left = {}
    for a, v in ins.accounts.items():
        free = max(v.cash[CCY].amount.value - ins.reserves[a].amount.value, Decimal(0))
        assert spent[a] <= free
        left[a] = free - spent[a]
        assert r.cash_after[a] == Money.of(v.cash[CCY].amount.value - spent[a], CCY)
    for i, g in goal.items():
        before_gap = abs(held[i] * price[i] - g)
        after_value = after[i] * price[i]
        assert abs(after_value - g) <= before_gap
        if after[i] != held[i]:
            assert after_value <= g  # no buy overshoots its target
    # Maximality: an underweight with no candidate was not buyable anywhere, since
    # free cash only falls and its deficit is unchanged; and it carries a reason.
    without = set(goal) - {c.instrument_id for c in r.candidates}
    for i in sorted(without, key=lambda j: j.to_wire()):
        deficit, p = goal[i] - held[i] * price[i], price[i]
        pairs = [t for t in ins.tradability if t.instrument_id == i]
        if deficit <= 0 or not pairs:
            continue
        assert any(x.subject.endswith(i.to_wire()) for x in r.abstentions)
        for t in pairs:
            lot_value = t.lot.value * p
            one_lot = t.fixed_cost.amount.value + t.lot.value * (
                p + t.unit_cost.amount.value
            )
            assert not (
                deficit >= lot_value >= t.min_notional.amount.value
                and left[t.account_id] >= one_lot
            )
    assert allocate(_shuffled(ins, seed)) == r
