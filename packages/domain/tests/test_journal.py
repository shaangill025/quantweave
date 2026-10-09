"""Journal store, idempotency, corrections and positions (T012 PR B; F-03..F-05).

SYNTHETIC inputs: docs/spec/tests/fixtures/canonical_transactions.csv rows and generated
journals. Expected numbers come from numerical_oracles.json (NUM01, NUM02), read from
the file, or from an independent exact `Fraction` model in the test.
"""

import csv
import json
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
from qw_domain.decimals import Money, PositiveQuantity, Price
from qw_domain.decimals import Quantity as Q
from qw_domain.identity import InstrumentId
from qw_domain.instants import parse_instant
from qw_domain.journal import Journal, ObservationLog, Outcome, Positions, materialize
from qw_domain.postings import (
    Header,
    JournalError,
    JournalEvent,
    SourceObservation,
    buy,
    deposit,
    dividend,
    fee,
    interest,
    sell,
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
ACCT = "SYN-ACCOUNT"


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def hdr(record: str, account: str = ACCT, day: int = 0) -> Header:
    return Header(account, T0 + timedelta(days=day), SourceRef("SYN-BROKER", record))


def d(text: str) -> Decimal:
    return Decimal(text)


def split_action(ratio: PositiveRatio, iid: InstrumentId = SYN, i: int = 1) -> Split:
    return Split(f"ca-{i}", 1, iid, T0.date(), SourceRef("SYN-CA", str(i)), T0, ratio)


# ---- Fixture import (SYNTHETIC CSV) and oracles NUM01/NUM02


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
            out.append(journal.post(deposit(h, Money.of(row["gross_cash"], cur)), at))
            continue
        qty, price = PositiveQuantity(row["quantity"]), Price(row["price"])
        sign = -1 if row["kind"] == "buy" else 1
        assert d(row["gross_cash"]) == sign * qty.value * price.value
        if row["kind"] == "buy":
            out.append(journal.post(buy(h, SYN, qty, price, fee_), at))
        else:
            out.append(journal.sell(h, SYN, qty, price, fee_, at))
    return out


def test_num01_and_num02_from_fixture() -> None:
    journal, log = Journal(), ObservationLog()
    assert set(import_rows(journal, log, read_rows(), T0)) == {Outcome.POSTED}
    exp, mark = ORACLES["NUM01"]["expected"], d(ORACLES["NUM01"]["inputs"]["mark"])
    pos = materialize(journal.events())
    h = pos.holding(ACCT, SYN)
    assert pos.cash == {(ACCT, "USD"): usd(exp["cash"])}
    assert h.quantity == Q(exp["remaining_qty"]) and h.cost == usd(
        exp["remaining_cost"]
    )
    assert pos.realized == {(ACCT, "USD"): usd(exp["realized"])}
    assert h.cost is not None
    unrealized = h.quantity.value * mark - h.cost.amount.value
    assert unrealized == d(exp["unrealized"])
    assert pos.cash[ACCT, "USD"].amount.value + h.quantity.value * mark == d(exp["nav"])
    assert d(exp["realized"]) + unrealized == d(exp["total_gain"])
    inp2, exp2 = ORACLES["NUM02"]["inputs"], ORACLES["NUM02"]["expected"]
    ratio = PositiveRatio.from_decimal(inp2["ratio"])
    assert journal.split(hdr("SPL", day=9), split_action(ratio), T0) is Outcome.POSTED
    after = materialize(journal.events()).holding(ACCT, SYN)
    assert after.quantity == Q(exp2["quantity"]) and after.cost == h.cost
    assert after.cost.amount.value == d(exp2["cost_per_unit"]) * after.quantity.value
    assert d(exp2["new_price"]) * after.quantity.value == d(exp2["value"])


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
    kept = log.get((ACCT, "SYN-BROKER", "D1"))
    assert kept is not None and kept.payload["gross_cash"] == "1000"
    assert materialize(journal.events()) == before
    with pytest.raises(TypeError):
        journal.post(kept, T0)  # type: ignore[arg-type]


def test_stale_holding_cannot_double_sell() -> None:
    journal = Journal()
    journal.post(buy(hdr("B"), SYN, PositiveQuantity(10), Price(5), usd(0)), T0)
    stale = journal.holding(hdr("S1"), SYN)
    journal.post(sell(hdr("S1"), PositiveQuantity(10), Price(6), usd(0), stale), T0)
    with pytest.raises(JournalError, match="short_sale_unsupported"):
        journal.post(sell(hdr("S2"), PositiveQuantity(10), Price(6), usd(0), stale), T0)
    with pytest.raises(JournalError, match="short_sale_unsupported"):
        journal.sell(hdr("S2"), SYN, PositiveQuantity(10), Price(6), usd(0), T0)
    again = journal.sell(hdr("S1"), SYN, PositiveQuantity(10), Price(6), usd(0), T0)
    assert again is Outcome.DUPLICATE and len(journal.events()) == 2


def test_accounts_stay_distinct() -> None:
    pos = materialize(
        buy(hdr("B", account=a), SYN, PositiveQuantity(50), Price(1), usd(0))
        for a in ("ACCT-1", "ACCT-2")
    )
    assert {k: h.quantity for k, h in pos.holdings.items()} == {
        ("ACCT-1", SYN): Q(50),
        ("ACCT-2", SYN): Q(50),
    }


# ---- Corrections and knowledge time (F-05, R071)


T1, T2, T3 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)


def corrected(original: JournalEvent, record: str = "B1-corr") -> JournalEvent:
    h = Header(ACCT, T0, SourceRef("SYN-BROKER", record))
    event = buy(h, SYN, PositiveQuantity(8), Price(50), usd(1))
    return replace(event, supersedes=original.event_id)


def test_correction_keeps_original_and_knowledge_time() -> None:
    journal = Journal()
    original = buy(hdr("B1"), SYN, PositiveQuantity(10), Price(50), usd(1))
    journal.post(original, T1)
    fixed = corrected(original)
    assert journal.correct(fixed, T3) is Outcome.POSTED
    assert journal.events()[0] is original and journal.entries()[0].recorded_at == T1
    then = materialize(journal.events(known_as_of=T2)).holding(ACCT, SYN)
    now = materialize(journal.events()).holding(ACCT, SYN)
    assert (then.quantity, then.cost) == (Q(10), usd(501))
    assert (now.quantity, now.cost) == (Q(8), usd(401))
    assert journal.reversal_of(original.event_id, known_as_of=T2) is None
    rev = journal.reversal_of(original.event_id)
    assert rev is not None and rev.effective_at == original.effective_at
    assert journal.correct(fixed, T3) is Outcome.DUPLICATE
    other = original.reversal(SourceRef("SYN-BROKER", "other"))
    assert journal.post(other, T3) is Outcome.CONFLICT
    with pytest.raises(JournalError, match="recorded_at_order"):
        journal.post(deposit(hdr("late"), usd(1)), T2)
    with pytest.raises(JournalError, match="link_unknown"):
        journal.reverse("missing", SourceRef("S", "x"), T3)


def test_correction_is_atomic() -> None:
    journal = Journal()
    original = buy(hdr("B1"), SYN, PositiveQuantity(10), Price(50), usd(1))
    journal.post(original, T1)
    journal.post(deposit(hdr("taken"), usd(5)), T1)
    snapshot = journal.entries()
    wrong_account = replace(corrected(original), account_id="OTHER")
    with pytest.raises(JournalError, match="link_unknown"):
        journal.correct(wrong_account, T3)
    assert journal.entries() == snapshot
    key_taken = corrected(original, record="taken")
    assert journal.correct(key_taken, T3) is Outcome.CONFLICT
    assert (
        journal.entries() == snapshot and journal.reversal_of(original.event_id) is None
    )
    assert journal.correct(corrected(original), T3) is Outcome.POSTED


def test_reversal_key_cannot_be_forged() -> None:
    journal = Journal()
    original = deposit(hdr("D"), usd(10))
    journal.post(original, T1)
    forged = deposit(Header(ACCT, T0, SourceRef("reversal", original.event_id)), usd(1))
    assert journal.post(forged, T1) is Outcome.POSTED
    assert journal.reversal_of(original.event_id) is None
    assert journal.reverse(original.event_id, SourceRef("C", "1"), T2) is Outcome.POSTED
    assert materialize(journal.events()).cash == {(ACCT, "USD"): usd(1)}


# ---- Properties over generated SYNTHETIC journals

ACCOUNTS, INSTRUMENTS = ("ACCT-1", "ACCT-2"), (SYN, SYN2)
values = st.decimals(min_value=d("0.01"), max_value=d("10000"), places=2)
units = st.decimals(min_value=d("0.001"), max_value=d("500"), places=3)
SHARES = [Fraction(1), Fraction(1, 2), Fraction(3, 10), Fraction(5, 7)]
CASH_OPS: dict[str, Any] = {
    "dep": (deposit, 1),
    "wd": (withdrawal, -1),
    "fee": (fee, -1),
    "int": (interest, 1),
}
TOL = Fraction(1, 10**12)
type Flat = dict[tuple[str, str, str], Fraction]


def flat(p: Positions) -> Flat:
    out: Flat = {}
    for (a, iid), h in p.holdings.items():
        out["qty", a, str(iid.uuid)] = Fraction(h.quantity.value)
        if h.cost is not None:
            out["cost", a, str(iid.uuid)] = Fraction(h.cost.amount.value)
    for kind, table in (("cash", p.cash), ("real", p.realized)):
        for (a, cur), m in table.items():
            out[kind, a, cur] = Fraction(m.amount.value)
    return out


def delta(after: Flat, before: Flat) -> Flat:
    keys = after.keys() | before.keys()
    out = {k: after.get(k, Fraction(0)) - before.get(k, Fraction(0)) for k in keys}
    return {k: v for k, v in out.items() if v}


def matches(actual: Flat, expected: Flat) -> bool:
    """Exact, except a sale's cost and realised P&L: within 1e-12 of the model."""
    keys = actual.keys() | expected.keys()
    gap = {
        k: abs(actual.get(k, Fraction(0)) - expected.get(k, Fraction(0))) for k in keys
    }
    return all(
        v == 0 or (k[0] in ("cost", "real") and v <= TOL) for k, v in gap.items()
    )


@st.composite
def journals(draw: st.DrawFn) -> tuple[Journal, list[Flat]]:
    """A journal built through its own helpers, with each event's expected effect from
    an independent exact model of the inputs (spec §5)."""
    journal, effects = Journal(), []
    for i in range(draw(st.integers(1, 14))):
        h = hdr(f"R{i}", draw(st.sampled_from(ACCOUNTS)), day=i)
        a, iid = h.account_id, draw(st.sampled_from(INSTRUMENTS))
        i_ = str(iid.uuid)
        held = journal.holding(h, iid)
        q_held, cost_held = Fraction(held.quantity.value), held.cost
        op = draw(st.sampled_from([*CASH_OPS, "div", "buy", "sell", "split"]))
        op = "buy" if op in ("sell", "split") and q_held <= 0 else op
        cur = "CAD" if iid == SYN2 else "USD"
        amt, price = draw(values), draw(values)
        f = draw(values) if draw(st.booleans()) else Decimal(0)
        if op in CASH_OPS:
            make, sign = CASH_OPS[op]
            journal.post(make(h, Money.of(amt, cur)), T0)
            effect = {("cash", a, cur): sign * Fraction(amt)}
        elif op == "div":
            journal.post(dividend(h, iid, Money.of(amt, cur)), T0)
            effect = {("cash", a, cur): Fraction(amt)}
        elif op == "buy":
            q = draw(units)
            journal.post(
                buy(h, iid, PositiveQuantity(q), Price(price), Money.of(f, cur)), T0
            )
            c = Fraction(q) * Fraction(price) + Fraction(f)
            effect = {
                ("cash", a, cur): -c,
                ("qty", a, i_): Fraction(q),
                ("cost", a, i_): c,
            }
        elif op == "sell":
            share = draw(st.sampled_from(SHARES))
            q = (held.quantity.value * share.numerator / share.denominator).quantize(
                d("0.001"), ROUND_DOWN
            ) or held.quantity.value
            journal.sell(
                h, iid, PositiveQuantity(q), Price(price), Money.of(f, cur), T0
            )
            assert cost_held is not None
            alloc = Fraction(cost_held.amount.value) * Fraction(q) / q_held
            net = Fraction(q) * Fraction(price) - Fraction(f)
            effect = {
                ("cash", a, cur): net,
                ("qty", a, i_): -Fraction(q),
                ("cost", a, i_): -alloc,
                ("real", a, cur): net - alloc,
            }
        else:
            ratio = draw(st.sampled_from([PositiveRatio(2, 1), PositiveRatio(3, 1)]))
            journal.split(h, split_action(ratio, iid, i), T0)
            effect = {("qty", a, i_): q_held * (ratio.fraction() - 1)}
        effects.append({k: v for k, v in effect.items() if v})
    return journal, effects


@PROFILE
@given(journals())
def test_each_event_has_its_economic_effect(built: tuple[Journal, list[Flat]]) -> None:
    journal, effects = built
    evs = journal.events()
    assert len(evs) == len(effects)
    for k, expected in enumerate(effects):
        actual = delta(flat(materialize(evs[: k + 1])), flat(materialize(evs[:k])))
        assert matches(actual, expected), (evs[k].kind, actual, expected)


@PROFILE
@given(journals(), st.data())
def test_reversal_undoes_exactly_its_event(
    built: tuple[Journal, list[Flat]], data: st.DataObject
) -> None:
    journal, effects = built
    evs = journal.events()
    k = data.draw(st.integers(0, len(evs) - 1))
    before = flat(materialize(evs))
    journal.reverse(evs[k].event_id, SourceRef("SYN-CORR", "1"), T0)
    undone = delta(flat(materialize(journal.events())), before)
    assert matches({key: -v for key, v in undone.items()}, effects[k])
    assert journal.events()[: len(evs)] == evs  # the original is kept
    rev = journal.reversal_of(evs[k].event_id)
    assert rev is not None and rev.effective_at == evs[k].effective_at
    if k == len(evs) - 1:
        assert flat(materialize(journal.events())) == flat(materialize(evs[:k]))


@PROFILE
@given(journals())
def test_reimport_twice_and_journal_balance(built: tuple[Journal, list[Flat]]) -> None:
    journal, _ = built
    evs, once = journal.events(), materialize(journal.events())
    assert {journal.post(e, T0) for e in evs} == {Outcome.DUPLICATE}
    assert materialize(journal.events()) == once
    tot: dict[str, Decimal] = {}
    for e in evs:
        for m in e.money:
            tot[m.amount.currency] = (
                tot.get(m.amount.currency, d("0")) + m.amount.amount.value
            )
        for u in e.units:
            tot[str(u.instrument_id.uuid)] = (
                tot.get(str(u.instrument_id.uuid), d("0")) + u.quantity.value
            )
    assert set(tot.values()) <= {d("0")}


@PROFILE
@given(
    st.lists(
        st.tuples(st.booleans(), units, values, values, st.sampled_from(SHARES)),
        min_size=1,
        max_size=12,
    )
)
def test_average_cost_against_exact_model(
    steps: list[tuple[bool, Decimal, Decimal, Decimal, Fraction]],
) -> None:
    """Exact Fraction average cost with nonzero fees. The implementation's remaining
    cost rounds up, never by more than 1e-12 per sale; what it keeps in cost it takes
    from realised P&L, so remaining cost - realised P&L is exact."""
    journal = Journal()
    qty, cost, realized, sales = Fraction(0), Fraction(0), Fraction(0), 0
    for i, (is_buy, q, price, f, share) in enumerate(steps):
        h = hdr(f"R{i}", day=i)
        if is_buy or qty == 0:
            journal.post(buy(h, SYN, PositiveQuantity(q), Price(price), usd(f)), T0)
            qty, cost = (
                qty + Fraction(q),
                cost + Fraction(q) * Fraction(price) + Fraction(f),
            )
        else:
            sold = Fraction(math_floor_milli(qty * share)) or qty
            journal.sell(
                h,
                SYN,
                PositiveQuantity(Decimal(sold.numerator) / sold.denominator),
                Price(price),
                usd(f),
                T0,
            )
            alloc = cost * sold / qty
            realized += sold * Fraction(price) - Fraction(f) - alloc
            qty, cost, sales = qty - sold, cost - alloc, sales + 1
        pos = materialize(journal.events())
        h_ = pos.holding(ACCT, SYN)
        impl_cost = Fraction(h_.cost.amount.value) if h_.cost else Fraction(0)
        real = pos.realized.get((ACCT, "USD"), usd(0))
        impl_real = Fraction(real.amount.value)
        assert Fraction(h_.quantity.value) == qty
        assert 0 <= impl_cost - cost <= sales * TOL
        assert impl_cost - impl_real == cost - realized  # sum of allocations agrees
        if qty == 0:
            assert impl_cost == 0 and impl_real == realized


def math_floor_milli(x: Fraction) -> Fraction:
    return Fraction(x.numerator * 1000 // x.denominator, 1000)
