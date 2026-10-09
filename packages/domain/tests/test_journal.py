"""Journal, postings, idempotency, corrections and positions (T012; F-01..F-05).

SYNTHETIC inputs: docs/spec/tests/fixtures/canonical_transactions.csv rows and generated
events. Expected numbers come from numerical_oracles.json (NUM01, NUM02), read from
the file, or are hand-computed in the test.
"""

import csv
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import DecimalValueError, Money, PositiveQuantity, Price
from qw_domain.decimals import Quantity as Q
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError, parse_instant
from qw_domain.journal import (
    BookAccount,
    EventKind,
    Header,
    Journal,
    JournalError,
    JournalEvent,
    MoneyPosting,
    ObservationLog,
    Outcome,
    Positions,
    SourceObservation,
    UnitAccount,
    UnitPosting,
    buy,
    deposit,
    dividend,
    fee,
    interest,
    materialize,
    sell,
    split,
    withdrawal,
)

PROFILE = settings(derandomize=True, database=None, max_examples=200)
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(UUID("00000000-0000-4000-8000-00000000005a"))
SYN2 = InstrumentId(UUID("00000000-0000-4000-8000-00000000005b"))
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def hdr(record: str, account: str = "SYN-ACCOUNT", day: int = 0) -> Header:
    return Header(account, T0 + timedelta(days=day), SourceRef("SYN-BROKER", record))


def d(text: str) -> Decimal:
    return Decimal(text)


# ---- Fixture import (SYNTHETIC CSV) and oracle NUM01/NUM02


def read_rows() -> list[dict[str, str]]:
    with (FIXTURES / "canonical_transactions.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def import_rows(
    journal: Journal, log: ObservationLog, rows: list[dict[str, str]], at: datetime
) -> list[Outcome]:
    """A minimal SYNTHETIC mapping of fixture rows; the real importer is T014."""
    out = []
    for row in rows:
        h = Header(
            row["account_ref"],
            parse_instant(row["effective_at"]),
            SourceRef(row["source_ref"], row["record_id"]),
        )
        log.observe(SourceObservation(h.account_id, h.source, at, h.effective_at, row))
        cur, fee_ = row["currency"], Money.of(row["fee"], row["currency"])
        if row["kind"] == "deposit":
            event = deposit(h, Money.of(row["gross_cash"], cur))
        else:
            qty, price = PositiveQuantity(row["quantity"]), Price(row["price"])
            sign = -1 if row["kind"] == "buy" else 1
            assert d(row["gross_cash"]) == sign * qty.value * price.value
            if row["kind"] == "buy":
                event = buy(h, SYN, qty, price, fee_)
            else:
                held = materialize(journal.events()).holding(h.account_id, SYN)
                event = sell(h, SYN, qty, price, fee_, held)
        out.append(journal.post(event, at))
    return out


def test_num01_fixture_matches_oracle() -> None:
    journal, log = Journal(), ObservationLog()
    assert set(import_rows(journal, log, read_rows(), T0)) == {Outcome.POSTED}
    exp, mark = ORACLES["NUM01"]["expected"], d(ORACLES["NUM01"]["inputs"]["mark"])
    pos = materialize(journal.events())
    h = pos.holding("SYN-ACCOUNT", SYN)
    assert pos.cash == {("SYN-ACCOUNT", "USD"): usd(exp["cash"])}
    assert h.quantity == Q(exp["remaining_qty"]) and h.cost == usd(
        exp["remaining_cost"]
    )
    assert pos.realized == {("SYN-ACCOUNT", "USD"): usd(exp["realized"])}
    assert h.cost is not None
    unrealized = h.quantity.value * mark - h.cost.amount.value
    assert unrealized == d(exp["unrealized"])
    assert pos.cash["SYN-ACCOUNT", "USD"].amount.value + h.quantity.value * mark == d(
        exp["nav"]
    )
    assert d(exp["realized"]) + unrealized == d(exp["total_gain"])
    sell_event = journal.events()[-1]
    assert sell_event.fee == usd(1)  # raw fee kept beside the postings
    buy_event = journal.events()[1]
    assert MoneyPosting(BookAccount.SECURITY_COST, usd("501"), SYN) in buy_event.money


def test_num02_split_matches_oracle() -> None:
    """NUM02 starts from the NUM01 position (6 units, cost 300.60)."""
    inp, exp = ORACLES["NUM02"]["inputs"], ORACLES["NUM02"]["expected"]
    journal = Journal()
    import_rows(journal, ObservationLog(), read_rows(), T0)
    start = materialize(journal.events()).holding("SYN-ACCOUNT", SYN)
    assert (start.quantity, start.cost) == (
        Q(inp["quantity"]),
        usd(inp["economic_cost"]),
    )
    ratio = PositiveRatio.from_decimal(inp["ratio"])
    action = Split("ca-1", 1, SYN, T0.date(), SourceRef("SYN-CA", "1"), T0, ratio)
    event = split(hdr("SPL", day=9), action, PositiveQuantity(inp["quantity"]))
    assert event.money == () and len(event.units) == 2
    after = materialize([*journal.events(), event]).holding("SYN-ACCOUNT", SYN)
    assert after.quantity == Q(exp["quantity"]) and after.cost == start.cost
    assert after.cost is not None
    assert after.cost.amount.value == d(exp["cost_per_unit"]) * after.quantity.value
    assert d(exp["new_price"]) * after.quantity.value == d(exp["value"])
    assert d(inp["old_price"]) * start.quantity.value == d(exp["value"])


def test_reimport_is_noop_and_changed_record_conflicts() -> None:
    journal, log = Journal(), ObservationLog()
    rows = read_rows()
    import_rows(journal, log, rows, T0)
    before = materialize(journal.events())
    later = T0 + timedelta(days=30)
    assert set(import_rows(journal, log, rows, later)) == {Outcome.DUPLICATE}
    assert materialize(journal.events()) == before and len(journal.events()) == 3
    changed = [dict(rows[0], gross_cash="2000")]
    assert import_rows(journal, log, changed, later) == [Outcome.CONFLICT]
    assert len(log.conflicts) == 1 and len(journal.conflicts) == 1
    kept = log.get(("SYN-ACCOUNT", "SYN-BROKER", "D1"))
    assert kept is not None and kept.payload["gross_cash"] == "1000"
    assert materialize(journal.events()) == before
    with pytest.raises(TypeError):
        kept.payload["gross_cash"] = "5"  # type: ignore[index]


def test_observation_is_not_a_journal_event() -> None:
    obs = SourceObservation("A", SourceRef("S", "1"), T0, T0, {"x": "1"})
    with pytest.raises(TypeError):
        Journal().post(obs, T0)  # type: ignore[arg-type]
    later = SourceObservation(
        "A", SourceRef("S", "1"), T0 + timedelta(1), T0, {"x": "1"}
    )
    assert later.fingerprint == obs.fingerprint  # receipt time is not content


# ---- Balance and type rejection (F-01, F-02)


def cash(v: str, cur: str = "USD") -> MoneyPosting:
    return MoneyPosting(BookAccount.CASH, Money.of(v, cur))


def ext(v: str, cur: str = "USD") -> MoneyPosting:
    return MoneyPosting(BookAccount.EXTERNAL_CAPITAL, Money.of(v, cur))


def ev(
    money: tuple[MoneyPosting, ...] = (), units: tuple[UnitPosting, ...] = ()
) -> JournalEvent:
    return JournalEvent(EventKind.DEPOSIT, "A", T0, SourceRef("S", "1"), money, units)


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (lambda: ev((cash("100"), ext("-99"))), "money_unbalanced"),
        (lambda: ev((cash("100", "USD"), ext("-100", "CAD"))), "money_unbalanced"),
        (lambda: ev((cash("100"),)), "posting_count"),
        (lambda: ev(), "event_empty"),
        (lambda: cash("0"), "posting_zero"),
        (lambda: UnitPosting(UnitAccount.POSITION, SYN, Q("0")), "posting_zero"),
        (lambda: MoneyPosting(BookAccount.SECURITY_COST, usd(1)), "posting_instrument"),
        (
            lambda: ev(
                units=(
                    UnitPosting(UnitAccount.POSITION, SYN, Q("10")),
                    UnitPosting(UnitAccount.UNIT_CLEARING, SYN2, Q("-10")),
                )
            ),
            "units_unbalanced",
        ),
        (lambda: deposit(hdr("x"), usd(0)), "amount_not_positive"),
        (lambda: withdrawal(hdr("x"), usd(-5)), "amount_not_positive"),
        (
            lambda: buy(hdr("x"), SYN, PositiveQuantity(1), Price(1), usd(-1)),
            "fee_negative",
        ),
    ],
)
def test_rejected_events(build: Callable[[], object], code: str) -> None:
    with pytest.raises(JournalError) as err:
        build()
    assert err.value.code == code


def test_type_and_scale_rejections() -> None:
    with pytest.raises(TypeError):  # ten shares are not fifty dollars
        MoneyPosting(BookAccount.CASH, Q("10"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        UnitPosting(UnitAccount.POSITION, SYN, usd(10))  # type: ignore[arg-type]
    with pytest.raises(DecimalValueError) as err:
        usd(Decimal("0.0000000000001"))
    assert err.value.code == "decimal_scale_exceeded"
    with pytest.raises(DecimalValueError):  # 12 + 12 fractional digits: never rounded
        buy(hdr("x"), SYN, PositiveQuantity("0.000000000001"), Price("0.5"), usd(0))
    with pytest.raises(InstantError):
        hdr_naive = Header("A", datetime(2026, 1, 2), SourceRef("S", "1"))  # noqa: DTZ001
        deposit(hdr_naive, usd(1))


def test_sell_guards_long_only_and_known_cost() -> None:
    pos = materialize([buy(hdr("B"), SYN, PositiveQuantity(5), Price(10), usd(0))])
    held = pos.holding("SYN-ACCOUNT", SYN)
    with pytest.raises(JournalError, match="short_sale_unsupported"):
        sell(hdr("S"), SYN, PositiveQuantity(6), Price(10), usd(0), held)
    unknown = Positions({}, {}, {}).holding("SYN-ACCOUNT", SYN)
    with pytest.raises(JournalError, match="cost_unknown"):
        sell(hdr("S"), SYN, PositiveQuantity(1), Price(10), usd(0), unknown)
    with pytest.raises(JournalError, match="currency_mismatch"):
        sell(hdr("S"), SYN, PositiveQuantity(1), Price(10), Money.of(0, "CAD"), held)


def test_income_fee_withdrawal_and_split_quarantine() -> None:
    events = [
        deposit(hdr("D"), usd(100)),
        interest(hdr("I"), usd("0.25")),
        dividend(hdr("V"), SYN, usd("3.10")),
        fee(hdr("F"), usd("1.35")),
        withdrawal(hdr("W"), usd(50)),
    ]
    assert materialize(events).cash == {("SYN-ACCOUNT", "USD"): usd("52")}
    income = [p for p in events[2].money if p.account is BookAccount.INCOME]
    assert income == [MoneyPosting(BookAccount.INCOME, usd("-3.10"), SYN)]
    rev = Split(
        "ca-2", 1, SYN, T0.date(), SourceRef("SYN-CA", "2"), T0, PositiveRatio(1, 3)
    )
    with pytest.raises(JournalError, match="split_fraction"):  # 100/3: cash in lieu
        split(hdr("R"), rev, PositiveQuantity(100))
    out = split(hdr("R"), rev, PositiveQuantity(99))
    assert {u.quantity for u in out.units} == {Q(-66), Q(66)}


def test_accounts_stay_distinct() -> None:
    events = [
        buy(hdr("B", account=a), SYN, PositiveQuantity(50), Price(1), usd(0))
        for a in ("ACCT-1", "ACCT-2")
    ]
    pos = materialize(events)
    assert {k: h.quantity for k, h in pos.holdings.items()} == {
        ("ACCT-1", SYN): Q(50),
        ("ACCT-2", SYN): Q(50),
    }


# ---- Corrections and knowledge time (F-05, R071)


def test_correction_keeps_original_and_knowledge_time() -> None:
    journal = Journal()
    t1, t2, t3 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    original = buy(hdr("B1"), SYN, PositiveQuantity(10), Price(50), usd(1))
    journal.post(original, t1)
    redo = Header("SYN-ACCOUNT", T0, SourceRef("SYN-BROKER", "B1-corr"))
    fixed = replace(
        buy(redo, SYN, PositiveQuantity(8), Price(50), usd(1)),
        supersedes=original.event_id,
    )
    assert journal.correct(fixed, t3) is Outcome.POSTED
    assert journal.events()[0] is original and journal.entries()[0].recorded_at == t1
    then = materialize(journal.events(known_as_of=t2)).holding("SYN-ACCOUNT", SYN)
    now = materialize(journal.events()).holding("SYN-ACCOUNT", SYN)
    assert (then.quantity, then.cost) == (Q(10), usd(501))
    assert (now.quantity, now.cost) == (Q(8), usd(401))
    assert journal.reversal_of(original.event_id, known_as_of=t2) is None
    assert journal.reversal_of(original.event_id) is not None
    # replay, a second different reversal, and reversing a reversal
    assert journal.correct(fixed, t3) is Outcome.DUPLICATE
    other = original.reversal(SourceRef("SYN-BROKER", "other"))
    assert journal.post(other, t3) is Outcome.CONFLICT
    with pytest.raises(JournalError, match="reverse_reversal"):
        journal.events()[1].reversal(SourceRef("S", "x"))
    with pytest.raises(JournalError, match="recorded_at_order"):
        journal.post(deposit(hdr("late"), usd(1)), t2)
    with pytest.raises(JournalError, match="link_unknown"):
        journal.reverse("missing", SourceRef("S", "x"), t3)


# ---- Properties over generated SYNTHETIC journals

ACCOUNTS, INSTRUMENTS = ("ACCT-1", "ACCT-2"), (SYN, SYN2)
amounts = st.decimals(min_value=d("0.01"), max_value=d("10000"), places=2)
units_ = st.decimals(min_value=d("0.001"), max_value=d("500"), places=3)
RATIOS = [PositiveRatio(*r) for r in ((2, 1), (3, 2), (1, 2), (1, 10))]
FORWARD = [PositiveRatio(2, 1), PositiveRatio(3, 1)]  # keeps generated scales bounded
CASH_OPS: dict[str, Any] = {
    "dep": deposit, "wd": withdrawal, "fee": fee, "int": interest,
}  # fmt: skip
OPS = [*CASH_OPS, "div", "buy", "sell", "split"]


@st.composite
def journals(draw: st.DrawFn) -> list[JournalEvent]:
    events: list[JournalEvent] = []
    for i in range(draw(st.integers(1, 14))):
        h = hdr(f"R{i}", draw(st.sampled_from(ACCOUNTS)), day=i)
        iid = draw(st.sampled_from(INSTRUMENTS))
        held = materialize(events).holding(h.account_id, iid)
        held_q = held.quantity.value
        op = draw(st.sampled_from(OPS))
        op = "buy" if op in ("sell", "split") and held_q <= 0 else op
        cur = "CAD" if iid == SYN2 else "USD"
        amt, price = Money.of(draw(amounts), cur), Price(draw(amounts))
        zero = Money.of(0, cur)
        if op in CASH_OPS:
            event = CASH_OPS[op](h, amt)
        elif op == "div":
            event = dividend(h, iid, amt)
        elif op == "buy":
            qty = PositiveQuantity(draw(units_))
            event = buy(h, iid, qty, price, amt if draw(st.booleans()) else zero)
        elif op == "sell":
            share = draw(st.sampled_from([d("1"), d("0.5"), d("0.3")]))
            q = (held_q * share).quantize(d("0.001"), ROUND_DOWN) or held_q
            event = sell(h, iid, PositiveQuantity(q), price, zero, held)
        else:
            ratio = draw(st.sampled_from(FORWARD))
            ca = Split(f"ca{i}", 1, iid, T0.date(), SourceRef("C", str(i)), T0, ratio)
            event = split(h, ca, PositiveQuantity(held_q))
        events.append(event)
    return events


def commodity_totals(events: list[JournalEvent]) -> dict[str, Decimal]:
    """Independent re-sum per currency and per instrument (no library helpers)."""
    tot: dict[str, Decimal] = {}
    for e in events:
        for m in e.money:
            tot[m.amount.currency] = (
                tot.get(m.amount.currency, d("0")) + m.amount.amount.value
            )
        for u in e.units:
            k = u.instrument_id.to_wire()
            tot[k] = tot.get(k, d("0")) + u.quantity.value
    return tot


@PROFILE
@given(journals())
def test_every_event_and_the_journal_balance(events: list[JournalEvent]) -> None:
    for e in events:
        assert all(v == 0 for v in commodity_totals([e]).values())
    assert all(v == 0 for v in commodity_totals(events).values())


@PROFILE
@given(journals())
def test_reimport_twice_gives_identical_positions(events: list[JournalEvent]) -> None:
    journal = Journal()
    assert {journal.post(e, T0) for e in events} == {Outcome.POSTED}
    once = materialize(journal.events())
    assert {journal.post(e, T0) for e in events} == {Outcome.DUPLICATE}
    assert materialize(journal.events()) == once


@PROFILE
@given(journals(), st.data())
def test_reversal_restores_positions(
    events: list[JournalEvent], data: st.DataObject
) -> None:
    k = data.draw(st.integers(0, len(events) - 1))
    journal = Journal()
    for e in events:
        journal.post(e, T0)
    journal.reverse(events[k].event_id, SourceRef("SYN-CORR", "1"), T0)
    assert materialize(journal.events()) == materialize(events[:k] + events[k + 1 :])


@PROFILE
@given(units_, amounts, st.sampled_from(RATIOS))
def test_split_preserves_cost_and_value(
    qty: Decimal, price: Decimal, r: PositiveRatio
) -> None:
    pre = [buy(hdr("B"), SYN, PositiveQuantity(qty), Price(price), usd(1))]
    action = Split("ca", 1, SYN, T0.date(), SourceRef("SYN-CA", "1"), T0, r)
    before = materialize(pre).holding("SYN-ACCOUNT", SYN)
    after = materialize([*pre, split(hdr("S"), action, PositiveQuantity(qty))]).holding(
        "SYN-ACCOUNT", SYN
    )
    assert after.cost == before.cost
    new_price = Fraction(price) * r.fraction().denominator / r.fraction().numerator
    assert Fraction(after.quantity.value) * new_price == Fraction(qty) * Fraction(price)
