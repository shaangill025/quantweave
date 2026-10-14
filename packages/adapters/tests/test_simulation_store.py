"""Virtual portfolios and simulations (0014_simulation, qw_adapters.simulation_store)
on PostgreSQL 16, as the non-superuser qw_app login; the superuser `conn` only
observes or plays a tampering owner where a test says so. SYNTHETIC accounts, bars
and actions from the T041 domain tests (2026-10-12 session); nothing here compares
a fixed date with the database clock. Hand-computed oracle: a market buy of 100
fills at 10 + 0.05 = 10.05 with fee 1 + 100 x 0.01 = 2 (cash 100000 - 1005 - 2 =
98993); the dividend pays 100 x 0.25 = 25 (cash 99018).
Dates: `opened()` and `ran()` are the domain fixtures shifted by whole days so the
ledger ends one to two days before the database clock (`SHIFT`, set per test from
`SELECT now()`), because a save refuses a simulation that ends after that clock."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import calculation_store as cs
from qw_adapters import journal_store as js
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_domain import simulation as sim
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, Price
from qw_domain.postings import Header, deposit
from qw_domain.risk import Side
from qw_domain.simulation import OrderStatus, OrderStyle
from test_simulation import CAL, LATE, D, at, held, order, pf, run
from test_simulation_events import CALL, DIV, write

Conn = psycopg.Connection[TupleRow]
JOURNAL = ("source_record", "ledger_event", "posting", "unit_posting")


def tenant(c: Conn) -> uuid.UUID:
    with tn.provision_tenant(c) as tx:
        return tx.tenant_id


SHIFT = timedelta(0)


def shifted(v: Any, by: timedelta) -> Any:
    """`v` with every datetime inside it moved by `by`."""
    if isinstance(v, datetime):
        return v + by
    if isinstance(v, tuple):
        return tuple(shifted(x, by) for x in v)
    if is_dataclass(v) and not isinstance(v, type):
        return replace(v, **{f.name: shifted(getattr(v, f.name), by)
                             for f in fields(v)})  # fmt: skip
    return v


def shift_for(c: Conn) -> timedelta:
    row = c.execute("SELECT now()").fetchone()
    assert row is not None
    return timedelta(days=(row[0] - ss.as_of(_ran())).days - 1)


@pytest.fixture(autouse=True)
def _db_clock(conn: Conn) -> None:
    global SHIFT
    SHIFT = shift_for(conn)


def _opened(name: str = "SYNTHETIC sim") -> ss.Simulation:
    return ss.Simulation(name, "exec-synth-1", "bench-synth-1", pf())


def opened(name: str = "SYNTHETIC sim") -> ss.Simulation:
    return shifted(_opened(name), SHIFT)  # type: ignore[no-any-return]


def ran() -> ss.Simulation:
    return shifted(_ran(), SHIFT)  # type: ignore[no-any-return]


def _ran() -> ss.Simulation:
    """o1 fills 100 @ 10.05; o2 (limit 5) expires unfilled at 15:00; o3 is a
    stop-limit (unsupported); then the dividend credits 25."""
    o1 = order("o1", Side.BUY, "100", OrderStyle.MARKET, at(13, 29))
    o2 = order("o2", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
               limit_price=Price("5"), expires_at=at(15, 0))  # fmt: skip
    o3 = order("o3", Side.BUY, "1", OrderStyle.STOP_LIMIT, at(13, 29),
               limit_price=Price("10"), stop_price=Price("10"))  # fmt: skip
    s = _opened().run(run(pf(), o1, o2, o3), LATE)
    p = sim.apply_dividend(s.portfolio, DIV, CAL, at(12, 0, 20))
    assert isinstance(p, sim.VirtualPortfolio)
    return replace(s, portfolio=p)


def save(c: Conn, t: uuid.UUID, s: ss.Simulation) -> None:
    with tn.tenant_transaction(c, t) as tx:
        ss.save(tx, s)


def load(c: Conn, t: uuid.UUID, sid: str = "SIM-1") -> ss.Simulation | None:
    with tn.tenant_transaction(c, t) as tx:
        return ss.load(tx, sid)


def count(c: Conn, table: str) -> int:
    row = c.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_an_empty_tenant_has_no_simulations(app_conn: Conn) -> None:
    a = tenant(app_conn)
    assert load(app_conn, a) is None
    with tn.tenant_transaction(app_conn, a) as tx:
        assert ss.page(tx, 10, None) == []
    for table in ss.TABLES:  # the migration seeds nothing
        assert count(app_conn, table) == 0


@pytest.mark.db
def test_round_trip_replays_the_same_simulation(app_conn: Conn, conn: Conn) -> None:
    a, s = tenant(app_conn), ran()
    save(app_conn, a, opened())
    save(app_conn, a, s)  # extends the stored opening: appends only
    got = load(app_conn, a)
    assert got == s
    assert got is not None and got.portfolio.virtual is True
    p = got.portfolio
    assert (p.cash(), p.income(), p.fees()) == (D("99018"), D("25"), D("2"))
    by_id = {o.result.order.order_id: o.result for o in got.outcomes}
    assert [by_id[k].status for k in ("o1", "o2", "o3")] == [
        OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.UNSUPPORTED]  # fmt: skip
    (f,) = got.fills()
    assert (f.price, f.quantity, f.fees, f.actual_execution) == (
        D("10.05"), D("100"), D("2"), False)  # fmt: skip
    rows = [count(conn, t) for t in ss.TABLES]
    assert rows == [1, 3, 3, 1, 2]  # capital, fill, dividend; 3 orders; 1 fill; 2 saves
    save(app_conn, a, s)  # an identical save writes nothing
    assert [count(conn, t) for t in ss.TABLES] == rows
    with tn.tenant_transaction(app_conn, a) as tx:
        assert ss.page(tx, 10, None) == [s]


@pytest.mark.db
def test_tenants_are_isolated_even_without_rls(app_conn: Conn, conn: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    save(app_conn, a, ran())
    assert load(app_conn, b) is None  # another tenant's id is unknown there
    with tn.tenant_transaction(app_conn, b) as tx:
        assert ss.page(tx, 10, None) == []
    save(app_conn, b, opened("SYNTHETIC other"))  # the same id, independently
    # The superuser bypasses RLS; the tenant filter still holds.
    assert load(conn, a) == ran() and load(conn, b) == opened("SYNTHETIC other")
    with tn.tenant_transaction(conn, b) as tx:
        assert [x.name for x in ss.page(tx, 10, None)] == ["SYNTHETIC other"]


@pytest.mark.db
def test_stored_history_is_append_only(app_conn: Conn, conn: Conn) -> None:
    a = tenant(app_conn)
    save(app_conn, a, ran())
    other = order("o9", Side.BUY, "1", OrderStyle.MARKET, at(13, 29))
    for rewrite in (
        opened(),  # drops stored entries and outcomes
        replace(ran(), name="SYNTHETIC renamed"),
        opened().run(run(pf(), other), LATE),  # a different history
    ):
        with pytest.raises(ss.SimulationStoreError, match="append_only"):
            save(app_conn, a, rewrite)
    for table in ss.TABLES:
        for sql in (f"UPDATE app.{table} SET tenant_id = tenant_id",
                    f"DELETE FROM app.{table}", f"TRUNCATE app.{table}"):  # fmt: skip
            with pytest.raises(errors.InsufficientPrivilege), \
                    tn.tenant_transaction(app_conn, a) as tx:  # fmt: skip
                tx.conn.execute(sql)
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(f"DELETE FROM app.{table}")  # not even the owner
    assert load(app_conn, a) == ran()


FORGERIES = [
    "UPDATE app.simulation_fill SET price = 9.05",
    "UPDATE app.simulation_entry SET cash = cash + 1, capital = capital + 1 "
    "WHERE kind = 'capital'",
    "UPDATE app.simulation_entry SET cash = cash - 1, fees = fees + 1 "
    "WHERE kind = 'fill'",
    "UPDATE app.simulation_entry SET income = 26 WHERE kind = 'dividend'",
    "UPDATE app.simulation_entry SET effective_at = '2026-10-12 13:00Z' "
    "WHERE kind = 'dividend'",
    "UPDATE app.simulation_order SET status = 'filled', remaining = 0 "
    "WHERE order_id = 'o2'",
    "UPDATE app.simulation_order SET quantity = 90 WHERE order_id = 'o1'",
    "UPDATE app.simulation_order SET status = 'submitted' WHERE order_id = 'o2'",
    "DELETE FROM app.simulation_fill; UPDATE app.simulation_order "
    "SET status = 'submitted', remaining = 100 WHERE order_id = 'o1'",
    "UPDATE app.simulation_order SET evaluated_at = '2026-10-12 13:00Z'",
    "UPDATE app.simulation_entry SET position = 5 WHERE kind = 'dividend'",
    "DELETE FROM app.simulation_fill",
    "UPDATE app.simulation SET mode = 'approved_policy_shadow'",
    "UPDATE app.simulation SET capital = 99999",
    # Covered by the digest only (review round 1, S2):
    "UPDATE app.simulation SET currency = 'EUR'",
    "UPDATE app.simulation_fill SET assumptions = '[\"forged\"]'",
    "UPDATE app.simulation_fill SET ambiguous = true",
    "UPDATE app.simulation_fill SET bar_start = '2026-10-12 00:00Z'",
    "UPDATE app.simulation_order SET notes = '[\"forged\"]' WHERE order_id = 'o1'",
    "UPDATE app.simulation_order SET status = 'cancelled' WHERE order_id = 'o2'",
    "UPDATE app.simulation_order SET limit_price = 50 WHERE order_id = 'o2'",
    "UPDATE app.simulation_order SET style = 'market' WHERE order_id = 'o2'",
    "UPDATE app.simulation_order SET oco_group = 'x' WHERE order_id = 'o1'",
    "UPDATE app.simulation_order SET submitted_at = '2026-10-12 00:00Z' "
    "WHERE order_id = 'o1'",
    "UPDATE app.simulation_entry SET ref = 'zzz' WHERE kind = 'dividend'",
    "DELETE FROM app.simulation_entry WHERE kind = 'dividend'",  # the tail
    "DELETE FROM app.simulation_digest",
]


@pytest.mark.db
@pytest.mark.parametrize("forge", FORGERIES)
def test_tampered_rows_fail_closed(app_conn: Conn, conn: Conn, forge: str) -> None:
    a = tenant(app_conn)
    save(app_conn, a, ran())
    conn.execute("SET session_replication_role = replica")  # a tampering superuser
    conn.execute(forge)
    with pytest.raises(ss.SimulationStoreError, match="integrity"):
        load(app_conn, a)
    with pytest.raises(ss.SimulationStoreError, match="integrity"), \
            tn.tenant_transaction(app_conn, a) as tx:  # fmt: skip
        ss.page(tx, 10, None)


@pytest.mark.db
def test_real_and_virtual_records_never_mix(app_conn: Conn, conn: Conn) -> None:
    a = tenant(app_conn)
    hdr = Header("acct-syn-1", at(13, 0), SourceRef("csv-syn", "D1"))
    with tn.tenant_transaction(app_conn, a) as tx:
        js.post_many(tx, [deposit(hdr, Money.of("10", "USD"))])
    real = [count(conn, t) for t in JOURNAL]
    clash = ss.Simulation(
        "SYNTHETIC",
        "exec-synth-1",
        "bench-synth-1",
        sim.VirtualPortfolio.open("acct-syn-1", Money.of("1", "USD"), at(13, 0)),
    )
    with pytest.raises(ss.SimulationStoreError, match="account_conflict"):
        save(app_conn, a, clash)
    save(app_conn, a, ran())
    assert [count(conn, t) for t in JOURNAL] == real  # the journal is untouched
    with tn.tenant_transaction(app_conn, a) as tx:
        assert [r[0] for r in js.list_accounts(tx, 10)] == ["acct-syn-1"]
        assert not js.load_journal(tx, "SIM-1").entries()
    into_sim = Header("SIM-1", at(13, 0), SourceRef("csv-syn", "D2"))
    with pytest.raises(errors.CheckViolation, match="virtual account"), \
            tn.tenant_transaction(app_conn, a) as tx:  # fmt: skip
        js.post_many(tx, [deposit(into_sim, Money.of("10", "USD"))])
    assert [count(conn, t) for t in JOURNAL] == real
    for forge in (
        "UPDATE app.simulation_entry SET virtual = false",
        "UPDATE app.simulation_order SET broker_submission = true",
        "UPDATE app.simulation_fill SET actual_execution = true",
        "UPDATE app.simulation SET separate_from_actual = false",
    ):
        conn.execute("SET session_replication_role = replica")
        with pytest.raises(errors.CheckViolation):
            conn.execute(forge)
    options = held(pf(), "100", "1005")
    written = write(options, CALL, "0.30")
    assert isinstance(written, sim.VirtualPortfolio) and written.options
    with pytest.raises(ss.SimulationStoreError, match="unsupported"):
        save(app_conn, a, replace(opened(), portfolio=written))
    shadow = replace(opened(), mode=ss.Mode.SHADOW)
    with pytest.raises(ss.SimulationStoreError, match="unsupported"):
        save(app_conn, a, shadow)


@pytest.mark.db
def test_an_unbacked_append_without_its_digest_fails_closed(app_conn: Conn) -> None:
    """qw_app may insert rows directly; a dividend appended without a matching
    digest row no longer loads."""
    a = tenant(app_conn)
    save(app_conn, a, ran())
    with tn.tenant_transaction(app_conn, a) as tx:
        tx.conn.execute(
            "INSERT INTO app.simulation_entry (tenant_id, simulation_id, position, "
            "entry_id, kind, effective_at, cash, units, cost, realized, income, "
            "fees, capital, ref) VALUES (%s, 'SIM-1', 3, 'div:forged', 'dividend', "
            "%s, 50, 0, 0, 0, 50, 0, 0, '')",
            (a, ss.as_of(ran()) + timedelta(hours=1)))  # fmt: skip
    with pytest.raises(ss.SimulationStoreError, match="integrity"):
        load(app_conn, a)


@pytest.mark.db
def test_a_concurrent_journal_post_and_simulation_cannot_both_commit(
    app_conn: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """Both directions take the journal's per-account lock: the post waits for the
    simulation's transaction, then sees it and is refused."""
    a = tenant(app_conn)
    hdr = Header("SIM-1", at(13, 0), SourceRef("csv-syn", "D1"))
    outcome: list[BaseException | None] = []

    def post() -> None:
        try:
            with psycopg.connect(runtime_urls["qw_app"], autocommit=True) as c2, \
                    tn.tenant_transaction(c2, a) as tx:  # fmt: skip
                js.post_many(tx, [deposit(hdr, Money.of("10", "USD"))])
            outcome.append(None)
        except psycopg.Error as exc:
            outcome.append(exc)

    with tn.tenant_transaction(app_conn, a) as tx:
        ss.save(tx, ran())
        other = threading.Thread(target=post)
        other.start()
        for _ in range(200):  # until the post waits on the account lock
            row = conn.execute("SELECT count(*) FROM pg_locks WHERE NOT granted "
                               "AND locktype = 'advisory'").fetchone()  # fmt: skip
            if row == (1,):
                break
            time.sleep(0.02)
        assert row == (1,) and not outcome
    other.join(10)
    assert len(outcome) == 1 and isinstance(outcome[0], errors.CheckViolation)
    assert load(app_conn, a) == ran() and count(conn, "ledger_event") == 0


def results(c: Conn, t: uuid.UUID, sid: str = "SIM-1") -> list[Any] | None:
    with tn.tenant_transaction(c, t) as tx:
        return ss.results(tx, sid)


@pytest.mark.db
def test_save_records_the_ledger_results_in_its_transaction(
    app_conn: Conn, conn: Conn
) -> None:
    a = tenant(app_conn)
    save(app_conn, a, opened())  # 5 ledger figures, no position, 3 unavailable
    assert count(conn, "calculation_record") == 8
    before = conn.execute("SELECT now()").fetchone()
    save(app_conn, a, ran())
    after = conn.execute("SELECT now()").fetchone()
    assert before is not None and after is not None
    assert count(conn, "calculation_record") == 8 + 9
    got = results(app_conn, a)
    assert got is not None and len(got) == 9
    by = {r.formula_id: r for r in got}
    oracle = {"virtual_ending_cash": "99018", "virtual_fees": "2",
              "virtual_income": "25", "virtual_realized_net": "0",
              "virtual_realized_gain_before_fees": "0",
              "virtual_position_units": "100", "virtual_market_value": None,
              "virtual_total_return": None,
              "virtual_benchmark_comparison": None}  # fmt: skip
    assert {k: r.result for k, r in by.items()} == oracle
    digest = ss.digest(ran())
    for r in got:
        refs = {i.name: i.value for i in r.inputs}
        assert (refs["simulation"], refs["simulation_digest"]) == ("SIM-1", digest)
        assert r.as_of == ss.as_of(ran()) and before[0] <= r.computed_at <= after[0]
        assert r.warnings[0] == "simulated:separate_from_actual"
    save(app_conn, a, ran())  # an identical save records nothing new
    assert count(conn, "calculation_record") == 17
    assert results(app_conn, a) == got  # the first computed_at is kept


@pytest.mark.db
def test_results_reproduce_and_stay_in_their_tenant(app_conn: Conn) -> None:
    a, b = tenant(app_conn), tenant(app_conn)
    save(app_conn, a, ran())
    assert results(app_conn, b) is None
    ids = sorted(cs.PREFIX + r.content_hash for r in results(app_conn, a) or [])
    with tn.tenant_transaction(app_conn, b) as tx:
        assert [cs.load(tx, i) for i in ids] == [None] * 9  # unknown in b
    save(app_conn, b, ran())  # the same simulation, recomputed in b: same ids
    again = sorted(cs.PREFIX + r.content_hash for r in results(app_conn, b) or [])
    assert again == ids


@pytest.mark.db
@pytest.mark.parametrize("forge", [
    FORGERIES[0],  # a simulation row the replay re-check catches
    "UPDATE app.simulation_fill SET assumptions = '[\"forged\"]'",  # digest only
    "DELETE FROM app.calculation_record WHERE record->>'formula_id' = "
    "'virtual_fees'",
    "UPDATE app.calculation_record SET record = jsonb_set(record, '{result}', "
    "'\"99999\"') WHERE record->>'formula_id' = 'virtual_ending_cash'",
])  # fmt: skip
def test_tampered_results_are_never_served(
    app_conn: Conn, conn: Conn, forge: str
) -> None:
    a = tenant(app_conn)
    save(app_conn, a, ran())
    conn.execute("SET session_replication_role = replica")
    conn.execute(forge)
    with pytest.raises(ss.SimulationStoreError, match="integrity"):
        results(app_conn, a)


@pytest.mark.db
def test_a_simulation_ending_after_the_database_clock_is_refused(
    app_conn: Conn, conn: Conn
) -> None:
    a = tenant(app_conn)
    late = shifted(_ran(), SHIFT + timedelta(days=3))
    with pytest.raises(ss.SimulationStoreError, match="clock"):
        save(app_conn, a, late)
    assert [count(conn, t) for t in (*ss.TABLES, "calculation_record")] == [0] * 6


@pytest.mark.db
def test_results_are_as_of_the_latest_evaluation_when_no_entry_follows(
    app_conn: Conn,
) -> None:
    a = tenant(app_conn)
    o2 = order("o2", Side.BUY, "100", OrderStyle.LIMIT, at(13, 29),
               limit_price=Price("5"), expires_at=at(15, 0))  # fmt: skip
    s = shifted(_opened().run(run(pf(), o2), LATE), SHIFT)
    assert len(s.portfolio.entries) == 1  # only the capital: no fill
    save(app_conn, a, s)
    got = results(app_conn, a)
    assert got is not None and {r.as_of for r in got} == {LATE + SHIFT}


@pytest.mark.db
def test_unrecorded_results_are_named_and_a_resave_restores_them(
    app_conn: Conn, conn: Conn
) -> None:
    """A clean simulation with no result rows at all (saved before increment 4) is
    `[]`, not an error; an identical re-save records them."""
    a = tenant(app_conn)
    save(app_conn, a, ran())
    full = results(app_conn, a)
    conn.execute("SET session_replication_role = replica")
    conn.execute("DELETE FROM app.calculation_record")
    assert results(app_conn, a) == []
    save(app_conn, a, ran())
    again = results(app_conn, a)
    assert again is not None and full is not None
    assert [r.canonical() for r in again] == [r.canonical() for r in full]


@pytest.mark.db
def test_a_failing_result_write_rolls_back_the_whole_save(
    app_conn: Conn, conn: Conn
) -> None:
    a, s = tenant(app_conn), ran()
    clash = ss._figures(s, ss.as_of(s))[0]  # a row under its id with another body
    with tn.tenant_transaction(app_conn, a) as tx:
        tx.conn.execute(
            "INSERT INTO app.calculation_record (tenant_id, id, content_hash, "
            "record, computed_at) VALUES (%s, %s, %s, '{}', now())",
            (a, cs.PREFIX + clash.content_hash, clash.content_hash))  # fmt: skip
    with pytest.raises(ss.SimulationStoreError, match="integrity"):
        save(app_conn, a, s)
    assert [count(conn, t) for t in ss.TABLES] == [0] * 5
    assert count(conn, "calculation_record") == 1  # only the planted row
