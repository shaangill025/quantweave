"""Reconciliation, cash states, open orders and risk inputs (T017 increment 1).

SYNTHETIC inputs only: made-up ids, balances and a T+1 lag supplied by the test (not a
regulation). The calendar is the SYNTHETIC XNYS fixture; October 2026 is UTC-4, so
a 16:00 ET close is 20:00 UTC. Expected numbers are hand-computed in comments or read
from docs/spec/tests/fixtures/numerical_oracles.json (NUM01, NUM06).
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.calendars import load_calendar
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.journal import Journal, materialize
from qw_domain.postings import Header, buy, deposit
from qw_domain.reconciliation import (
    CashState,
    DifferenceKind,
    OpenOrder,
    OrderOrigin,
    OrderSide,
    PendingWithdrawal,
    ReconcileError,
    Reconciliation,
    ReconStatus,
    SettlementTerms,
    Severity,
    Tolerance,
    cash_states,
    reconcile,
    risk_account_inputs,
    unit_availability,
    with_account_state,
)
from qw_domain.risk import ProposedAction, RiskCode, RiskInputs, Side, evaluate
from qw_domain.risk_pauses import PauseBook
from qw_domain.sources import (
    HoldingLine,
    MappedInterval,
    Source,
    SourceKind,
    SourceMap,
    SourceSnapshot,
    UnderlyingAccount,
)
from qw_domain.valuation import Mark, MarkKind

PROFILE = settings(derandomize=True, database=None, max_examples=60, deadline=None)
HERE = Path(__file__).parent
SPEC = HERE.parents[2] / "docs/spec"
CAL = load_calendar((HERE / "fixtures/xnys_synthetic.json").read_text())
SYN = InstrumentId(UUID("00000000-0000-4000-8000-0000000000b1"))
SYN2 = InstrumentId(UUID("00000000-0000-4000-8000-0000000000b2"))
TERMS = {SYN: SettlementTerms(CAL, 1), SYN2: SettlementTerms(CAL, 1)}
ACCT, TENANT, REF = "SYN-ACCT", "SYN-TENANT", "SYN-REF"
TOL = Tolerance(Quantity(0), {"USD": Money.of("0.01", "USD")})
DAY = timedelta(days=1)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def num(oracle: str) -> dict[str, dict[str, str]]:
    raw = json.loads((SPEC / "tests/fixtures/numerical_oracles.json").read_text())
    found: dict[str, dict[str, str]] = next(
        o for o in raw["oracles"] if o["id"] == oracle
    )
    return found


def registry() -> SourceMap:
    m = SourceMap()
    m.add_account(UnderlyingAccount(ACCT, TENANT, "SYN-INST", "cash", "USD"))
    for sid, kind in (
        ("SYN-BROKER", SourceKind.BROKER_EXPORT),
        ("SYN-MANUAL", SourceKind.MANUAL),
    ):
        m.add_source(Source(sid, TENANT, kind))
        m.remap(sid, REF, (MappedInterval(ACCT, at(1, 0)),), at(1, 0))
    return m


def hdr(t: datetime, rid: str) -> Header:
    return Header(ACCT, t, SourceRef("SYN-USER", rid))


def num01_journal() -> Journal:
    """NUM01: deposit 1000 Mon 5 Oct; buy 10 @ 50 fee 1 Mon (settles Tue 20:00 UTC);
    sell 4 @ 60 fee 1 Wed 7 Oct (settles Thu 8 Oct 20:00 UTC)."""
    i = num("NUM01")["inputs"]
    j = Journal()
    j.post(deposit(hdr(at(5, 14), "d1"), usd(i["deposit"])), at(5, 14))
    b = buy(hdr(at(5, 15), "b1"), SYN, PositiveQuantity(i["buy_qty"]),
            Price(i["buy_price"]), usd(i["buy_fee"]))  # fmt: skip
    j.post(b, at(5, 15))
    j.sell(hdr(at(7, 15), "s1"), SYN, PositiveQuantity(i["sell_qty"]),
           Price(i["sell_price"]), usd(i["sell_fee"]), at(7, 15))  # fmt: skip
    return j


def snapshot_of(
    j: Journal, t: datetime, source: str = "SYN-BROKER", complete: bool = True
) -> SourceSnapshot:
    pos = materialize(j.events(t), t)
    lines = tuple(
        HoldingLine(k[1], "USD", h.quantity)
        for k, h in sorted(pos.holdings.items(), key=lambda kv: kv[0][1])
        if h.quantity.value
    )
    cash = tuple(v for (a, _), v in sorted(pos.cash.items()) if a == ACCT)
    return SourceSnapshot(source, REF, t, lines, cash, complete)


def run(
    j: Journal, snap: SourceSnapshot, as_of: datetime, **kw: object
) -> Reconciliation:
    return reconcile(j, registry(), snap, ACCT, TOL, as_of, DAY, **kw)  # type: ignore[arg-type]


def usd_state(j: Journal, as_of: datetime, **kw: object) -> CashState:
    return cash_states(j, ACCT, as_of, TERMS, **kw)["USD"]  # type: ignore[arg-type]


def order(
    ref: str, side: OrderSide = OrderSide.BUY, qty: str = "80", **kw: object
) -> OpenOrder:
    base = OpenOrder(
        ref, ACCT, SYN, side, PositiveQuantity(qty), "USD", Price("50"), usd(0),
        OrderOrigin.USER_DECLARED, "SYN-USER", at(5, 14),
    )  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


# ---- cash states


def test_num01_settled_unsettled_and_available_follow_t_plus_1() -> None:
    j, e = num01_journal(), num("NUM01")["expected"]
    mon = usd_state(j, at(5, 21))  # buy unsettled: settled 1000, debit -501
    assert (mon.settled, mon.unsettled_debits, mon.available) == (
        usd(1000), usd(-501), usd(499))  # fmt: skip
    wed = usd_state(j, at(8, 19, 59))  # sale proceeds 240-1 = 239 not yet settled
    assert (wed.settled, wed.unsettled_credits, wed.available) == (
        usd(499), usd(239), usd(499))  # fmt: skip
    # AT049: unsettled sale proceeds are not available cash
    thu = usd_state(j, at(8, 20))
    assert thu.settled == usd(e["cash"]) == thu.available  # 738
    assert thu.unsettled_credits == usd(0) == thu.unsettled_debits
    assert any(p.startswith("event:") for p in thu.provenance["settled"])
    assert thu.included == ("settled", "unsettled_debits", "external_encumbrances")


def test_settlement_skips_the_holiday_and_settles_at_the_early_close() -> None:
    # Buy Wed 25 Nov (11:00 ET); Thu 26 Nov is a holiday, so T+1 is Fri 27 Nov,
    # an early close at 13:00 ET = 18:00 UTC.
    j = Journal()
    j.post(deposit(hdr(at(5, 14), "d1"), usd(1000)), at(5, 14))
    t = datetime(2026, 11, 25, 16, tzinfo=UTC)
    j.post(buy(hdr(t, "b1"), SYN, PositiveQuantity(1), Price(100), usd(0)), t)
    before = usd_state(j, datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
    after = usd_state(j, datetime(2026, 11, 27, 18, tzinfo=UTC))
    assert (before.settled, before.unsettled_debits) == (usd(1000), usd(-100))
    assert (after.settled, after.unsettled_debits) == (usd(900), usd(0))


def test_reversed_trade_leaves_no_unsettled_residue() -> None:
    j = num01_journal()
    buy_id = next(e.event_id for e in j.events() if e.source.record_id == "b1")
    s = usd_state(j, at(5, 21))
    j2 = Journal()
    for e in j.events():
        if e.source.record_id != "s1":
            j2.post(e, at(5, 15))
    j2.reverse(buy_id, SourceRef("SYN-USER", "fix1"), at(5, 16))
    r = usd_state(j2, at(5, 21))
    assert s.unsettled_debits == usd(-501)
    assert (r.settled, r.unsettled_debits, r.available) == (
        usd(1000), usd(0), usd(1000))  # fmt: skip


def test_num06_open_buy_order_encumbers_cash_once() -> None:
    e = num("NUM06")
    j = Journal()
    j.post(deposit(hdr(at(5, 14), "d1"), usd(e["inputs"]["available"])), at(5, 14))
    o = order("o1")  # 80 x 50 + 0 = 4000
    twice = (o, replace(o, origin=OrderOrigin.SNAPSHOT_REPORTED, source_id="SYN-B"))
    s = usd_state(j, at(6, 15), open_orders=twice)
    assert s.external_encumbrances == usd(4000)  # the same order counted once
    assert s.available == usd(1000)  # 5000 - 4000: a second 4000 buy is not fundable
    assert s.settled == usd(5000)
    assert "order:o1" in s.provenance["external_encumbrances"]


def test_open_sell_order_creates_no_cash_but_encumbers_units() -> None:
    j = num01_journal()
    sell = order("o2", OrderSide.SELL, "4", limit_price=Price("70"))
    s = usd_state(j, at(5, 21), open_orders=(sell,))
    assert s.available == usd(499) and s.external_encumbrances == usd(0)
    assert unit_availability(j, ACCT, at(5, 21), (sell,)) == {SYN: Quantity(6)}


def test_reported_hold_and_expiry_and_withdrawals() -> None:
    j = num01_journal()
    held = order("o3", limit_price=None, reported_hold=usd("120.5"))
    gone = order("o4", expires_at=at(5, 20))
    later = order("o5", observed_at=at(6, 0))
    w = PendingWithdrawal("w1", ACCT, usd(200), at(5, 16))
    s = usd_state(j, at(5, 21), open_orders=(held, gone, later), withdrawals=(w,))
    assert s.external_encumbrances == usd("120.5")  # o4 expired, o5 not yet known
    assert s.available == usd("378.5")  # 1000 - 501 - 120.5
    assert s.spendable == usd("178.5")  # minus the 200 withdrawal
    assert s.pending_withdrawals == (usd(200),)


def test_missing_inputs_leave_cash_unknown_never_zero() -> None:
    j, t = num01_journal(), at(5, 21)
    cases = [
        (cash_states(j, ACCT, t, {})["USD"], "settlement_terms_missing"),
        (usd_state(j, t, open_orders=(order("o6", limit_price=None),)),
         "open_order_hold_unknown:o6"),
        (usd_state(j, t, open_orders=(order("o1"), order("o1", qty="81"))),
         "open_order_disagreement:o1"),
    ]  # fmt: skip
    for s, reason in cases:
        assert s.available is None and s.spendable is None and s.settled is None
        assert any(r.startswith(reason) for r in s.reasons)


# ---- reconciliation


def test_snapshot_from_the_journal_reconciles() -> None:
    j = num01_journal()
    r = run(j, snapshot_of(j, at(7, 16)), at(7, 17))
    assert r.status is ReconStatus.RECONCILED and r.differences == ()
    assert not r.sizing_blocked and r.cash_basis == "broker_available"
    assert r.reported_cash == {"USD": usd(738)}  # trade-date cash, NUM01


def test_stale_snapshot_is_reported_stale_and_never_erases_newer_events() -> None:
    j = num01_journal()
    before = j.entries()
    old = snapshot_of(j, at(6, 12))  # broker view before the Wed sale: 10 units
    r = run(j, old, at(7, 17))
    assert r.status is ReconStatus.STALE and r.sizing_blocked
    assert r.differences == ()  # compared as of its own time
    sale = next(e.event_id for e in j.events() if e.source.record_id == "s1")
    assert r.unreconciled_events == (sale,)
    assert j.entries() == before  # append-only journal untouched
    assert materialize(j.events()).holding(ACCT, SYN).quantity == Quantity(6)
    fields = risk_account_inputs(r, usd_state(j, at(7, 17), observation=r))
    assert fields.account_observed_at is None and fields.reconciled is True
    ins = with_account_state(risk_inputs(at(7, 17)), fields)
    assert RiskCode.ACCOUNT_STALE in account_codes(ins)
    assert RiskCode.ACCOUNT_CONFLICT not in account_codes(ins)
    aged = run(j, snapshot_of(j, at(7, 16)), at(9, 17))  # older than max_age
    assert aged.status is ReconStatus.STALE and aged.stale_reasons == ("snapshot_age",)
    by_age = risk_account_inputs(aged, usd_state(j, at(9, 17), observation=aged))
    assert (by_age.account_observed_at, by_age.reconciled) == (at(7, 16), True)
    codes = account_codes(with_account_state(risk_inputs(at(9, 17)), by_age))
    assert RiskCode.ACCOUNT_STALE in codes and RiskCode.ACCOUNT_CONFLICT not in codes


def test_knowledge_time_versus_effective_time() -> None:
    late = num01_journal()  # a buy recorded after the snapshot, effective before it
    snap = snapshot_of(late, at(7, 16))
    b = buy(hdr(at(7, 10), "late"), SYN, PositiveQuantity(1), Price(50), usd(0))
    late.post(b, at(7, 18))
    r = run(late, snap, at(7, 19))
    assert r.status is ReconStatus.MATERIAL_CONFLICT and r.unreconciled_events == ()
    back = num01_journal()  # a reversal posted later, backdated to the sale
    sale = next(e.event_id for e in back.events() if e.source.record_id == "s1")
    back.reverse(sale, SourceRef("SYN-USER", "fix1"), at(7, 18))
    r = run(back, snap, at(7, 19))
    assert r.status is ReconStatus.MATERIAL_CONFLICT and r.unreconciled_events == ()
    edge = num01_journal()  # effective exactly at the snapshot: compared, not newer
    edge.post(deposit(hdr(at(7, 16), "d2"), usd(5)), at(7, 16))
    r = run(edge, snapshot_of(edge, at(7, 16)), at(7, 17))
    assert r.status is ReconStatus.RECONCILED and r.unreconciled_events == ()
    many = num01_journal()  # N = 2 newer events are all listed
    r = run(many, snapshot_of(many, at(5, 14)), at(7, 17))
    ids = tuple(e.event_id for e in many.events() if e.source.record_id in ("b1", "s1"))
    assert r.status is ReconStatus.STALE and r.unreconciled_events == ids
    with pytest.raises(ReconcileError, match="max_age"):
        reconcile(many, registry(), snap, ACCT, TOL, at(7, 17), -DAY)


def test_differences_are_typed_and_classified() -> None:
    j = num01_journal()
    snap = SourceSnapshot(
        "SYN-BROKER", REF, at(7, 16),
        (HoldingLine(SYN, "USD", Quantity(4)),
         HoldingLine(SYN2, "USD", Quantity("0.5"))),
        (usd("738.004"),), True,
    )  # fmt: skip
    tol = Tolerance(Quantity(1), TOL.cash)
    r = reconcile(j, registry(), snap, ACCT, tol, at(7, 17), DAY)
    kinds = {(d.kind, d.severity) for d in r.differences}
    assert kinds == {
        (DifferenceKind.QUANTITY_MISMATCH, Severity.MATERIAL),  # |4 - 6| > 1
        (DifferenceKind.UNKNOWN_INSTRUMENT, Severity.MATERIAL),  # 0.5 <= 1, still
        (DifferenceKind.CASH_MISMATCH, Severity.IMMATERIAL),  # 0.004 <= 0.01
    }
    assert r.status is ReconStatus.MATERIAL_CONFLICT and r.sizing_blocked
    q = next(d for d in r.differences if d.kind is DifferenceKind.QUANTITY_MISMATCH)
    assert (q.journal, q.observed, q.delta) == (Decimal(6), Decimal(4), Decimal(-2))


def test_immaterial_differences_do_not_block() -> None:
    j = num01_journal()
    lines = (HoldingLine(SYN, "USD", Quantity("6.5")),)  # |0.5| <= 1 unit
    snap = replace(snapshot_of(j, at(7, 16)), holdings=lines, cash=(usd("738.01"),))
    tol = Tolerance(Quantity(1), TOL.cash)
    r = reconcile(j, registry(), snap, ACCT, tol, at(7, 17), DAY)
    assert {d.severity for d in r.differences} == {Severity.IMMATERIAL}
    assert len(r.differences) == 2
    assert r.status is ReconStatus.IMMATERIAL_DIFFERENCE and not r.sizing_blocked


def test_missing_position_and_missing_tolerance_are_material() -> None:
    j = num01_journal()
    snap = replace(snapshot_of(j, at(7, 16)), holdings=(), cash=(usd("738.001"),))
    r = reconcile(j, registry(), snap, ACCT, Tolerance(Quantity(1), {}), at(7, 17), DAY)
    d = {x.kind: x for x in r.differences}
    assert d[DifferenceKind.MISSING_POSITION].severity is Severity.MATERIAL  # 6 > 1
    assert d[DifferenceKind.CASH_MISMATCH].note == "no_tolerance"
    assert d[DifferenceKind.CASH_MISMATCH].severity is Severity.MATERIAL
    bare = run(j, replace(snap, cash=()), at(7, 17))  # complete: omitted cash is 0
    c = next(x for x in bare.differences if x.kind is DifferenceKind.CASH_MISMATCH)
    assert (c.journal, c.observed) == (Decimal(738), Decimal(0))
    assert c.severity is Severity.MATERIAL


def test_incomplete_snapshot_compares_only_what_it_lists() -> None:
    j = num01_journal()
    snap = replace(snapshot_of(j, at(7, 16)), holdings=(), cash=(), complete=False)
    r = run(j, snap, at(7, 17))
    assert r.differences == () and r.status is ReconStatus.INCOMPLETE
    assert r.sizing_blocked


def test_reconcile_rejections() -> None:
    j = num01_journal()
    snap = snapshot_of(j, at(7, 16))
    with pytest.raises(ReconcileError, match="snapshot_after_as_of"):
        run(j, snap, at(7, 15))
    with pytest.raises(ReconcileError, match="unmapped"):
        reconcile(j, registry(), snap, "SYN-OTHER", TOL, at(7, 17), DAY)
    with pytest.raises(ReconcileError, match="unknown_source"):
        run(j, replace(snap, source_id="SYN-NOPE"), at(7, 17))
    with pytest.raises(ValueError, match="tolerance"):
        Tolerance(Quantity(-1), {})
    with pytest.raises(ValueError, match="tolerance"):
        Tolerance(Quantity(0), {"CAD": usd(1)})


# ---- risk inputs


def risk_inputs(as_of: datetime, max_age: timedelta = DAY) -> RiskInputs:
    return RiskInputs(
        as_of=as_of, market_max_age=DAY, fx_max_age=DAY, account_max_age=max_age,
        account_observed_at=None, reconciled=None, available_cash=None,
        allocation_used=usd(0),
        marks={SYN: Mark(SYN, Price(60), "USD", MarkKind.ASK, as_of, "SYN-FEED")},
        fx={}, denominators={}, held_units={SYN: Quantity(6)}, position_values={},
        issuer_exposure={}, sector_exposure={}, stress_shocks={SYN: Ratio("-0.2")},
    )  # fmt: skip


GATES = {RiskCode.ACCOUNT_STALE, RiskCode.ACCOUNT_CONFLICT, RiskCode.CASH_UNCONFIRMED}


def account_codes(ins: RiskInputs) -> set[RiskCode]:
    action = ProposedAction(
        TENANT, ACCT, "USD", None, "SYN-STRAT", "account_nav", SYN, "SYN-ISS",
        "SYN-SEC", "long_term", Side.BUY, PositiveQuantity(1), PositiveQuantity(1),
        Price(50), usd(0), usd(0), False,
    )  # fmt: skip
    ev = evaluate(None, action, ins, PauseBook(TENANT))
    return {r.code for r in ev.reasons} & GATES


def test_reconciled_account_feeds_risk_without_account_blocks() -> None:
    j, t = num01_journal(), at(7, 17)
    r = run(j, snapshot_of(j, at(7, 16)), t)
    w = PendingWithdrawal("w1", ACCT, usd(100), at(7, 16))
    s = usd_state(j, t, observation=r, withdrawals=(w,))
    assert s.reported == usd(738) and s.available == usd(499)
    ins = with_account_state(risk_inputs(t), risk_account_inputs(r, s))
    assert ins.account_observed_at == at(7, 16) and ins.reconciled is True
    # risk subtracts pending withdrawals itself, so they are passed, not netted twice
    assert ins.available_cash == usd(499) and ins.pending_withdrawals == (usd(100),)
    assert s.spendable == usd(399)
    assert account_codes(ins) == set()


def test_material_conflict_blocks_sizing() -> None:
    j, t = num01_journal(), at(7, 17)
    seven = (HoldingLine(SYN, "USD", Quantity(7)),)
    r = run(j, replace(snapshot_of(j, at(7, 16)), holdings=seven), t)
    fields = risk_account_inputs(r, usd_state(j, t, observation=r))
    ins = with_account_state(risk_inputs(t), fields)
    assert RiskCode.ACCOUNT_CONFLICT in account_codes(ins)
    assert ins.available_cash is None


def test_at083_five_minute_old_broker_state_is_not_fresh() -> None:
    j, t = num01_journal(), at(7, 17)
    received = t - timedelta(seconds=1)
    r = run(j, snapshot_of(j, t - timedelta(minutes=5)), t, received_at=received)
    assert r.observation_age == timedelta(minutes=5)
    assert r.ingestion_delay == timedelta(minutes=4, seconds=59)
    fields = risk_account_inputs(r, usd_state(j, t, observation=r))
    assert fields.account_observed_at == t - timedelta(minutes=5)  # never receipt time
    ins = with_account_state(risk_inputs(t, timedelta(minutes=1)), fields)
    assert RiskCode.ACCOUNT_STALE in account_codes(ins)


def test_user_reported_or_missing_observation_never_confirms_cash() -> None:
    j, t = num01_journal(), at(7, 17)
    r = run(j, snapshot_of(j, at(7, 16), "SYN-MANUAL"), t)
    assert r.cash_basis == "user_reported"
    fields = risk_account_inputs(r, usd_state(j, t, observation=r))
    assert fields.available_cash is None
    assert "cash_basis_user_reported" in fields.reasons
    none = risk_account_inputs(None, usd_state(j, t))
    assert (none.account_observed_at, none.reconciled, none.available_cash) == (
        None, None, None)  # fmt: skip
    assert account_codes(with_account_state(risk_inputs(t), none)) == GATES


def test_at043_cash_in_another_currency_cannot_fund() -> None:
    j, t = Journal(), at(7, 17)
    j.post(deposit(hdr(at(5, 14), "d1"), Money.of(5000, "CAD")), at(5, 14))
    states = cash_states(j, ACCT, t, TERMS)
    assert set(states) == {"CAD"}
    r = run(j, snapshot_of(j, at(7, 16)), t)
    fields = risk_account_inputs(r, states.get("USD"))
    assert fields.available_cash is None and "cash_state_missing" in fields.reasons
    with pytest.raises(ValueError, match="account"):
        risk_account_inputs(r, replace(states["CAD"], account_id="SYN-OTHER"))


# ---- properties


type Generated = tuple[Journal, list[datetime], dict[InstrumentId, Decimal], Decimal]


@st.composite
def journals(draw: st.DrawFn) -> Generated:
    """Also returns an independent running-sum oracle of units and USD cash."""
    j, times, units = Journal(), [], {SYN: Decimal(0), SYN2: Decimal(0)}
    cash = Decimal(draw(st.integers(1, 10**5)))
    j.post(deposit(hdr(at(5, 13), "d0"), usd(cash)), at(5, 13))
    for n in range(draw(st.integers(1, 6))):
        t = at(5, 14) + timedelta(hours=7 * n)
        q, p = draw(st.integers(1, 20)), draw(st.integers(1, 100))
        iid = draw(st.sampled_from([SYN, SYN2]))
        if draw(st.booleans()) and units[iid]:
            sold = min(Decimal(q), units[iid])
            j.sell(hdr(t, f"x{n}"), iid, PositiveQuantity(sold), Price(p), usd(1), t)
            units[iid], cash = units[iid] - sold, cash + sold * p - 1
        else:
            j.post(buy(hdr(t, f"x{n}"), iid, PositiveQuantity(q), Price(p), usd(1)), t)
            units[iid], cash = units[iid] + q, cash - q * p - 1
        times.append(t)
    return j, times, {k: v for k, v in units.items() if v}, cash


@PROFILE
@given(journals())
def test_property_snapshot_from_journal_has_no_differences(
    jt: Generated,
) -> None:
    j, times, units, cash = jt
    end = times[-1] + timedelta(minutes=1)
    snap = snapshot_of(j, end)
    assert {h.instrument_id: h.quantity.value for h in snap.holdings} == units
    assert snap.cash == ((usd(cash),) if cash else ())
    r = run(j, snap, end + timedelta(minutes=1))
    assert r.differences == () and r.status is ReconStatus.RECONCILED


@PROFILE
@given(journals(), st.integers(0, 5))
def test_property_stale_snapshot_never_changes_journal_state(
    jt: Generated, k: int
) -> None:
    j, times, *_ = jt
    end = times[-1] + timedelta(days=3)
    snap = snapshot_of(j, times[min(k, len(times) - 1)])
    before, cash_before = j.entries(), cash_states(j, ACCT, end, TERMS)
    r = reconcile(j, registry(), snap, ACCT, TOL, end, timedelta(days=30))
    assert j.entries() == before
    with_obs = cash_states(j, ACCT, end, TERMS, observation=r)
    for cur, s in with_obs.items():
        same = replace(s, reported=None, provenance=cash_before[cur].provenance)
        assert same == cash_before[cur]
    stale = snap.as_of < times[-1]
    assert (r.status is ReconStatus.STALE) == stale and r.differences == ()


@PROFILE
@given(journals(), st.lists(st.integers(1, 5000), max_size=3), st.integers(0, 3))
def test_property_available_never_exceeds_settled(
    jt: Generated, holds: list[int], day: int
) -> None:
    j, times, *_ = jt
    orders = tuple(
        order(f"o{i}", limit_price=None, reported_hold=usd(h))
        for i, h in enumerate(holds)
    )
    s = usd_state(j, times[-1] + timedelta(days=day), open_orders=orders)
    assert s.settled is not None and s.available is not None
    assert s.available <= s.settled
    assert s.spendable is not None and s.spendable <= s.available
