"""Virtual portfolios and the virtual order/fill policy (T041 increment 1; R063,
R098). SYNTHETIC bars, contracts and accounts only; expected
numbers are hand-computed in the comments. Nothing here can reach a broker.
Model: latency 1 min, participation 10%, lot 1, slippage 0.05/unit, fees USD 1 per
fill + 0.01/unit. Session 2026-10-12 (EDT): 30-minute bars from 13:30Z, known 1 min
after their end."""

import ast
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import simulation as sim
from qw_domain.bars import Bar, BarBook, Ohlc, session_grid
from qw_domain.calendars import load_calendar
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.journal import Journal, Positions
from qw_domain.postings import Header, Holding, deposit
from qw_domain.risk import Side
from qw_domain.simulation import (
    EntryKind,
    ExecutionModel,
    OrderStatus,
    OrderStyle,
    VirtualEntry,
    VirtualOrder,
    VirtualPortfolio,
    simulate,
)
from qw_domain.streams import FeedLatency

FIXTURES = Path(__file__).parent / "fixtures"
CAL = load_calendar((FIXTURES / "xnys_synthetic.json").read_text())
X = InstrumentId(UUID(int=41))
FEED = "feed-synth-30m"
SRC = SourceRef("src-synth-ca", "rec-1")
DAY = date(2026, 10, 12)
SLOTS = session_grid(CAL, DAY, timedelta(minutes=30))
T0 = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)
LATE = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)
D = Decimal
MODEL = ExecutionModel(
    "exec-synth-1", FEED, timedelta(minutes=1), D("0.1"), D("1"), D("0.05"),
    Money.of("1", "USD"), Money.of("0.01", "USD"),
)  # fmt: skip
# (open, high, low, close, volume) per 30-minute slot
BARS = [
    ("10", "10.5", "9.8", "10.2", "5000"),
    ("9.7", "10.0", "9.6", "9.9", "3000"),
    ("10", "11.2", "9.4", "10.5", "4000"),
    ("10.5", "10.6", "10.4", "10.5", "0"),
]


def bar(i: int, o: str, h: str, lo: str, c: str, v: str, known: int = 1) -> Bar:
    s = SLOTS[i]
    ohlc = Ohlc(Price(o), Price(h), Price(lo), Price(c))
    return Bar(X, FEED, FeedLatency.DELAYED, s, ohlc, Quantity(v), None,
               s.end + timedelta(minutes=known))  # fmt: skip


def book(rows: list[tuple[str, str, str, str, str]] = BARS) -> BarBook:
    b = BarBook()
    for i, r in enumerate(rows):
        b = b.record(bar(i, *r))
    return b


def at(h: int, m: int, day: int = 12) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=UTC)


def order(
    oid: str, side: Side, qty: str, style: OrderStyle, sub: datetime, **kw: object
) -> VirtualOrder:
    return VirtualOrder(oid, X, side, PositiveQuantity(qty), style, sub, **kw)  # type: ignore[arg-type]


def pf(capital: str = "100000") -> VirtualPortfolio:
    return VirtualPortfolio.open("SIM-1", Money.of(capital, "USD"), T0)


def held(
    p: VirtualPortfolio, qty: str, cost: str, when: datetime = T0
) -> VirtualPortfolio:
    """A SYNTHETIC virtual purchase without fees, recorded directly."""
    e = VirtualEntry(
        f"held-{qty}-{when.isoformat()}",
        EntryKind.FILL,
        when,
        X,
        p.account_id,
        cash=-D(cost),
        units=D(qty),
        cost=D(cost),
    )
    return p.record(e)


def run(p: VirtualPortfolio, *orders: VirtualOrder, as_of: datetime = LATE,
        b: BarBook | None = None) -> sim.SimRun:  # fmt: skip
    return simulate(p, orders, b or book(), MODEL, as_of)


def identity_holds(p: VirtualPortfolio) -> bool:
    cost = sum((p.cost(i) for i in p.instruments()), D(0))
    return p.cash() + cost == p.capital() + p.realized() + p.income() - p.fees()


# ---- fills


def test_market_buy_fills_at_the_next_open_with_slippage_and_fees() -> None:
    r = run(pf(), order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 29)))
    (res,) = r.results
    (f,) = res.fills
    # earliest 13:30 = slot start: open 10 + 0.05 = 10.05; fee 1 + 100*0.01 = 2
    assert (f.price, f.quantity, f.fees) == (D("10.05"), D("100"), D("2"))
    assert f.filled_at == at(13, 30) and f.known_at == at(14, 1)
    assert not f.ambiguous and f.actual_execution is False
    assert res.status is OrderStatus.FILLED
    p = r.portfolio  # 100000 - 1005 - 2
    assert (p.cash(), p.units(X), p.cost(X), p.fees()) == (
        D("98993"), D("100"), D("1005"), D("2"),
    )  # fmt: skip
    assert identity_holds(p)


def test_order_inside_a_bar_resolves_worst_case_and_is_ambiguous() -> None:
    r = run(pf(), order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 45)))
    (f,) = r.results[0].fills
    # earliest 13:46 lies inside [13:30, 14:00): bar high 10.5 + 0.05, at bar end
    assert (f.price, f.filled_at, f.ambiguous) == (D("10.55"), at(14, 0), True)
    assert "submission_inside_bar" in f.assumptions
    # a limit is never filled from the colliding bar: no fill there, flagged
    lim = order("o2", Side.BUY, "100", OrderStyle.LIMIT, at(13, 45),
                limit_price=Price("9.9"))  # fmt: skip
    (g,) = run(pf(), lim).results[0].fills
    assert g.filled_at == at(14, 0) and g.price == D("9.75")  # slot 1 open 9.7+0.05
    assert "submission_inside_bar" in run(pf(), lim).results[0].notes


def test_limit_orders_need_penetration_and_gaps_fill_at_the_open() -> None:
    lim = order("o1", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
                limit_price=Price("9.9"))  # fmt: skip
    (f,) = run(pf(), lim).results[0].fills
    # low 9.8 < 9.9 inside slot 0: at the limit (9.9 + 0.05 capped at 9.9), bar end
    assert (f.price, f.filled_at) == (D("9.9"), at(14, 0))
    touch = replace(lim, limit_price=Price("9.8"))  # low == 9.8 only touches
    (g,) = run(pf(), touch).results[0].fills
    # slot 1 opens 9.7 <= 9.8: open + slippage 9.75, still within the limit
    assert (g.price, g.filled_at) == (D("9.75"), at(14, 0))


def test_stop_orders_trigger_and_gap_through() -> None:
    stop = order("o1", Side.BUY, "100", OrderStyle.STOP, at(13, 29),
                 stop_price=Price("10.4"))  # fmt: skip
    (f,) = run(pf(), stop).results[0].fills
    assert (f.price, f.filled_at) == (D("10.45"), at(14, 0))  # max(10, 10.4) + 0.05
    sell = order("o2", Side.SELL, "100", OrderStyle.STOP, at(13, 59),
                 stop_price=Price("9.9"))  # fmt: skip
    (g,) = run(held(pf(), "100", "1000"), sell).results[0].fills
    # slot 1 gaps to 9.7 below the stop: min(9.7, 9.9) - 0.05 at the open
    assert (g.price, g.filled_at) == (D("9.65"), at(14, 0))


def test_stop_and_target_in_one_bar_takes_the_worse_and_is_ambiguous() -> None:
    kw = {"oco_group": "B1"}
    stop = order("s", Side.SELL, "100", OrderStyle.STOP, at(14, 29),
                 stop_price=Price("9.5"), **kw)  # fmt: skip
    tgt = order("t", Side.SELL, "100", OrderStyle.LIMIT, at(14, 29),
                limit_price=Price("11"), **kw)  # fmt: skip
    r = run(held(pf(), "100", "1000"), tgt, stop)
    rs, rt = r.results[1], r.results[0]
    # slot 2: low 9.4 <= 9.5 and high 11.2 > 11; stop 9.45 is worse than target 11
    (f,) = rs.fills
    assert (f.price, f.ambiguous) == (D("9.45"), True)
    assert "stop_and_target_same_bar" in f.assumptions
    assert rt.status is OrderStatus.CANCELLED and not rt.fills


def test_participation_cap_partial_fill_no_volume_and_no_fill() -> None:
    mkt = order("o1", Side.BUY, "1000", OrderStyle.MARKET, at(13, 29),
                expires_at=at(15, 30))  # fmt: skip
    res = run(pf(), mkt).results[0]
    # 10% of 5000 = 500 @ 10.05 (fee 6), 10% of 3000 = 300 @ 9.75 (fee 4),
    # slot 2 has 4000 volume: 200 @ 10.05 (fee 3); 100% done
    assert [(f.quantity, f.price, f.fees) for f in res.fills] == [
        (D("500"), D("10.05"), D("6")), (D("300"), D("9.75"), D("4")),
        (D("200"), D("10.05"), D("3")),
    ]  # fmt: skip
    rows = [*BARS[:2], (*BARS[2][:4], "0"), BARS[3]]
    part = run(pf(), mkt, b=book(rows)).results[0]
    assert part.status is OrderStatus.PARTIALLY_FILLED and part.remaining == D("200")
    assert "no_volume" in part.notes
    none = order("o2", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
                 limit_price=Price("5"), expires_at=at(15, 0))  # fmt: skip
    nr = run(pf(), none).results[0]
    assert nr.status is OrderStatus.EXPIRED and not nr.fills


def test_cash_and_units_limit_fills() -> None:
    mkt = order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 29))
    res = run(pf("1000"), mkt)
    # floor((1000 - 1) / (10.05 + 0.01)) = 99; 1000 - 994.95 - 1.99 = 3.06
    (f,) = res.results[0].fills
    assert f.quantity == D("99") and res.portfolio.cash() == D("3.06")
    assert "insufficient_virtual_cash" in res.results[0].notes
    short = order("o2", Side.SELL, "10", OrderStyle.MARKET, at(13, 29))
    sr = run(pf(), short).results[0]
    assert not sr.fills and "insufficient_virtual_units" in sr.notes


def test_sell_realizes_average_cost_and_reconciles() -> None:
    p = run(pf(), order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 29))).portfolio
    s = order("o2", Side.SELL, "40", OrderStyle.MARKET, at(14, 29))
    p = run(p, s).portfolio
    # 40 @ 10 - 0.05 = 398, fee 1.4; removed cost 1005*40/100 = 402; realized -4
    assert (p.cash(), p.cost(X), p.realized()) == (D("99389.6"), D("603"), D("-4"))
    assert identity_holds(p)


def test_no_fill_before_the_bar_is_known_or_from_a_later_correction() -> None:
    mkt = order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 29))
    early = run(pf(), mkt, as_of=at(14, 0, 12) + timedelta(seconds=30)).results[0]
    assert early.status is OrderStatus.SUBMITTED and not early.fills
    b = book().record(bar(0, "10.1", "10.5", "9.8", "10.2", "5000", known=300))
    (f,) = run(pf(), mkt, as_of=at(15, 0), b=b).results[0].fills
    assert f.price == D("10.05")  # the correction known at 19:00Z is not used


def test_caps_use_the_ledger_as_of_fill_time_and_time_order_holds() -> None:
    later = held(pf(), "100", "1000", at(15, 0))  # bought at 15:00
    sell = order("o1", Side.SELL, "100", OrderStyle.MARKET, at(13, 20))
    res = run(later, sell)  # would fill at 13:30, before the units exist
    assert not res.results[0].fills and res.portfolio.units(X, at=at(13, 30)) == 0
    assert "insufficient_virtual_units" in res.results[0].notes
    with pytest.raises(sim.SimError, match="time_order"):
        held(later, "1", "10", at(14, 0))
    poor = held(pf("1000"), "50", "999", at(15, 0))  # cash 1 from 15:00 only
    with pytest.raises(sim.SimError, match="time_order"):  # fill at 13:30 < 15:00
        run(poor, order("o2", Side.BUY, "10", OrderStyle.MARKET, at(13, 29)))
    assert poor.cash(at=at(13, 30)) == D("1000") and poor.cash() == D("1")


def test_no_fill_stamped_after_expiry() -> None:
    lim = order("o1", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
                limit_price=Price("9.9"), expires_at=at(13, 45))  # fmt: skip
    res = run(pf(), lim).results[0]  # slot 0 penetrates 9.9, stamp 14:00 > 13:45
    assert not res.fills and res.status is OrderStatus.EXPIRED
    gap = order("o2", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
                limit_price=Price("10"), expires_at=at(13, 45))  # fmt: skip
    (f,) = run(pf(), gap).results[0].fills  # open 10 <= 10 at 13:30 < 13:45
    assert (f.filled_at, f.price) == (at(13, 30), D("10"))
    edge = replace(lim, expires_at=at(14, 0))  # bar-end stamp 14:00 == expiry
    res = run(pf(), edge).results[0]
    assert not res.fills and "expired_in_bar" in res.notes


def test_unsupported_orders_are_not_filled() -> None:
    sl = order("o1", Side.BUY, "1", OrderStyle.STOP_LIMIT, at(13, 29),
               stop_price=Price("10"), limit_price=Price("10.1"))  # fmt: skip
    nolim = order("o2", Side.BUY, "1", OrderStyle.LIMIT, at(13, 29))
    for res in run(pf(), sl, nolim).results:
        assert res.status is OrderStatus.UNSUPPORTED and not res.fills


# ---- segregation


def test_virtual_records_cannot_enter_the_real_journal() -> None:
    p = pf()
    with pytest.raises(TypeError):
        Journal().post(p.entries[0], T0)  # type: ignore[arg-type]
    real = deposit(Header("REAL-1", T0, SRC), Money.of("10", "USD"))
    with pytest.raises(TypeError):
        p.record(real)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="virtual"):
        replace(p.entries[0], virtual=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="broker"):
        order("o", Side.BUY, "1", OrderStyle.MARKET, T0, broker_submission=True)
    other = VirtualPortfolio.open("SIM-2", Money.of("1", "USD"), T0)
    with pytest.raises(ValueError, match="account"):
        p.record(replace(other.entries[0], entry_id="x", account_id="SIM-2"))


def test_seed_from_a_real_snapshot_never_mutates_it() -> None:
    cost = Money.of("1001", "USD")
    snap = Positions({("REAL-1", X): Holding("REAL-1", X, Quantity("10"), cost),
                      ("REAL-2", X): Holding("REAL-2", X, Quantity("5"), cost)},
                     {}, {})  # fmt: skip
    before = deepcopy(snap)
    p = VirtualPortfolio.seed("SIM-1", Money.of("2000", "USD"), snap, "REAL-1", T0)
    assert snap == before
    assert (p.units(X), p.cost(X), p.cash(), p.capital()) == (
        D("10"), D("1001"), D("2000"), D("3001"),
    )  # fmt: skip
    assert all(e.virtual for e in p.entries) and p.entries[-1].kind is EntryKind.SEED
    p2 = run(p, order("o1", Side.SELL, "10", OrderStyle.MARKET, at(13, 29))).portfolio
    assert p2.units(X) == 0 and snap == before


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module == "qw_domain":
            found |= {f"qw_domain.{a.name}" for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_no_broker_or_third_party_module_is_reachable() -> None:
    root = Path(sim.__file__).parent
    seen, todo = set[str](), ["qw_domain.simulation"]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        for name in _imports(root / f"{mod.removeprefix('qw_domain.')}.py"):
            assert "broker" not in name.lower(), (mod, name)
            if name.startswith("qw_domain."):
                todo.append(name)
            else:
                assert name.split(".")[0] in sys.stdlib_module_names, (mod, name)
    assert "qw_domain.bars" in seen and len(seen) > 5
    assert not [n for n in dir(sim) if "broker" in n.lower() or "submit" in n.lower()]


# ---- property


PX = st.integers(90, 110).map(lambda n: D(n) / 10)


@st.composite
def scenario(draw: st.DrawFn) -> tuple[list[tuple[str, str, str, str, str]],
                                       list[VirtualOrder], datetime]:  # fmt: skip
    rows = []
    for _ in range(draw(st.integers(1, 5))):
        a, b = draw(PX), draw(PX)
        lo, hi = min(a, b) - D("0.2"), max(a, b) + D("0.1")
        rows.append((str(a), str(hi), str(lo), str(b), str(draw(st.integers(0, 3000)))))
    orders = []
    for k in range(draw(st.integers(1, 4))):
        style = draw(st.sampled_from([OrderStyle.MARKET, OrderStyle.LIMIT,
                                      OrderStyle.STOP]))  # fmt: skip
        px = Price(str(draw(PX)))
        sub = at(13, 20) + timedelta(minutes=draw(st.integers(0, 150)))
        life = draw(st.sampled_from([None, 15, 40, 75]))
        orders.append(order(
            f"o{k}", draw(st.sampled_from([Side.BUY, Side.SELL])),
            str(draw(st.integers(1, 400))), style,
            sub, limit_price=px if style is OrderStyle.LIMIT else None,
            stop_price=px if style is OrderStyle.STOP else None,
            expires_at=None if life is None else sub + timedelta(minutes=life),
        ))  # fmt: skip
    return rows, orders, at(13, 30) + timedelta(minutes=draw(st.integers(0, 200)))


@settings(max_examples=150, deadline=None)
@given(scenario())
def test_property_ledger_reconciles_and_fills_respect_time(
    s: tuple[list[tuple[str, str, str, str, str]], list[VirtualOrder], datetime],
) -> None:
    rows, orders, as_of = s
    start = held(pf("3000"), "50", "500")
    b = book(rows)
    r = simulate(start, orders, b, MODEL, as_of)
    flows = []  # independent oracle from the fill records
    for res in r.results:
        o = res.order
        for f in res.fills:
            assert f.filled_at >= o.submitted_at + MODEL.latency
            assert o.expires_at is None or f.filled_at <= o.expires_at
            src = [x for x in b.bars if x.start == f.bar_start]
            assert src and max(x.knowledge_at for x in src) <= as_of
            assert f.known_at <= as_of and f.filled_at >= f.bar_start
            lo = min(x.ohlc.low.value for x in src) - MODEL.slippage
            hi = max(x.ohlc.high.value for x in src) + MODEL.slippage
            assert lo <= f.price <= hi
            sign = 1 if o.side is Side.BUY else -1
            flows.append((f.filled_at, -sign * f.quantity * f.price - f.fees,
                          sign * f.quantity))  # fmt: skip
    cash, units = D(2500), D(50)
    for t, _, _ in flows:  # never short of units or cash at any fill time
        assert D(2500) + sum((c for u, c, _ in flows if u <= t), D(0)) >= 0
        assert D(50) + sum((q for u, _, q in flows if u <= t), D(0)) >= 0
    cash += sum((c for _, c, _ in flows), D(0))
    units += sum((q for _, _, q in flows), D(0))
    p = r.portfolio
    assert (p.cash(), p.units(X)) == (cash, units) and units >= 0 and cash >= 0
    assert p.cash() + p.cost(X) == D(3000) + p.realized() - p.fees()
