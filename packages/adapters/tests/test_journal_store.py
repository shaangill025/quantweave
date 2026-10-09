"""Journal persistence (0007_journal, qw_adapters.journal_store) on PostgreSQL 16.

Runtime connections are throwaway non-superuser LOGIN roles in qw_app / qw_worker
(conftest); the superuser `conn` only migrates or plays the owner where a test says
so. Accounts, sources and amounts are SYNTHETIC. Expected numbers come from
docs/spec/tests/fixtures/numerical_oracles.json (NUM01, NUM02) or from the in-memory
domain journal, which is the reference the store must reproduce.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg import errors
from psycopg.rows import TupleRow
from qw_adapters import journal_store as js
from qw_adapters import tenancy as tn
from qw_adapters.tenancy import TenantTx, tenant_transaction
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.journal import Journal, Outcome, materialize
from qw_domain.postings import (
    Header,
    Holding,
    JournalError,
    JournalEvent,
    SourceObservation,
    buy,
    deposit,
    split,
)

Conn = psycopg.Connection[TupleRow]
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
ORACLES = {
    o["id"]: o
    for o in json.loads((FIXTURES / "numerical_oracles.json").read_text())["oracles"]
}
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
ACCT = "SYN-ACCOUNT"
_accounts = itertools.count()
MINUS5 = [("cash", "-5"), ("external_capital", "5")]
CAC = "corporate_action_clearing"


def make_tenant(conn: Conn, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, f"SYNTHETIC {name}")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        return tx.tenant_id, user


def usd(v: str | int | Decimal) -> Money:
    return Money.of(v, "USD")


def hdr(record: str, day: int = 0, account: str = ACCT) -> Header:
    return Header(account, T0 + timedelta(days=day), SourceRef("SYN-BROKER", record))


def in_tx[T](conn: Conn, tenant: uuid.UUID, fn: Callable[[TenantTx], T]) -> T:
    with tenant_transaction(conn, tenant) as tx:
        return fn(tx)


def events(conn: Conn, tenant: uuid.UUID, *args: Any) -> tuple[JournalEvent, ...]:
    return in_tx(conn, tenant, lambda tx: js.load_journal(tx, ACCT, *args).events())


def run(conn: Conn, tenant: uuid.UUID, query: str, *params: object) -> None:
    in_tx(conn, tenant, lambda tx: tx.conn.execute(query, params))


def store_all(conn: Conn, tenant: uuid.UUID, journal: Journal) -> None:
    for event in journal.events():
        with tenant_transaction(conn, tenant) as tx:
            assert js.post(tx, event) is Outcome.POSTED


def num01_journal() -> Journal:
    """NUM01 (deposit, buy 10 @ 50 fee 1, sell 4 @ 60 fee 1) then NUM02 (2:1 split)."""
    inp, j = ORACLES["NUM01"]["inputs"], Journal()
    j.post(deposit(hdr("D1"), usd(inp["deposit"])), T0)
    j.post(buy(hdr("B1", 3), SYN, PositiveQuantity(inp["buy_qty"]),
               Price(inp["buy_price"]), usd(inp["buy_fee"])), T0)  # fmt: skip
    j.sell(hdr("S1", 4), SYN, PositiveQuantity(inp["sell_qty"]),
           Price(inp["sell_price"]), usd(inp["sell_fee"]), T0)  # fmt: skip
    ratio = PositiveRatio.from_decimal(ORACLES["NUM02"]["inputs"]["ratio"])
    action = Split("ca-1", 1, SYN, T0.date(), SourceRef("SYN-CA", "1"), T0, ratio)
    assert j.split(hdr("SPL", 9), action, T0) is Outcome.POSTED
    return j


def raw_event(
    tx: TenantTx, event_id: str = "a" * 64, kind: str = "deposit", **cols: str | None
) -> None:
    """A header written around the adapter (direct INSERT as the runtime role)."""
    tx.conn.execute(
        "INSERT INTO app.ledger_event (tenant_id, account_id, event_id, kind, "
        "effective_at, source_id, source_record_id, reverses, supersedes, "
        "lot_policy, terms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "'economic_average_cost/1', '{}')",
        (tx.tenant_id, cols.get("account", ACCT), event_id, kind,
         T0 + timedelta(days=int(cols.get("day") or 0)),
         cols.get("source", "SYN-RAW"), cols.get("record", event_id[:8]),
         cols.get("reverses"), cols.get("supersedes")),
    )  # fmt: skip


def raw_cash(
    tx: TenantTx, lines: list[tuple[str, str]], event_id: str = "a" * 64, start: int = 0
) -> None:
    """Money lines in USD; an account "u:<unit account>" is a unit line of SYN."""
    for i, (account, wire) in enumerate(lines, start):
        unit = account.startswith("u:")
        cols = "unit_account, instrument_id" if unit else "book_account, currency"
        tx.conn.execute(
            f"INSERT INTO app.{'unit_posting' if unit else 'posting'} (tenant_id, "
            f"event_id, line, wire, {cols}) VALUES (%s, %s, %s, %s, %s, %s)",
            (tx.tenant_id, event_id, i, wire, account.removeprefix("u:"),
             SYN.uuid if unit else "USD"),
        )  # fmt: skip


# --- round trip against the domain journal ---


@pytest.mark.db
def test_num01_num02_round_trip(app_conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    mem = num01_journal()
    store_all(app_conn, tenant, mem)
    loaded = in_tx(app_conn, tenant, lambda tx: js.load_journal(tx, ACCT))
    assert loaded.events() == mem.events()
    pos = materialize(loaded.events())
    assert pos == materialize(mem.events())
    exp, exp2 = ORACLES["NUM01"]["expected"], ORACLES["NUM02"]["expected"]
    held = pos.holding(ACCT, SYN)
    assert pos.cash == {(ACCT, "USD"): usd(exp["cash"])}
    assert pos.realized == {(ACCT, "USD"): usd(exp["realized"])}
    assert held.quantity == Quantity(exp2["quantity"])
    assert held.cost == usd(exp["remaining_cost"])
    assert held.cost.amount.value == Decimal(exp2["cost_per_unit"]) * Decimal(
        exp2["quantity"]
    )


@pytest.mark.db
@settings(max_examples=12, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])  # fmt: skip
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["deposit", "buy", "sell"]),
            st.decimals(min_value=Decimal("0.01"), max_value=Decimal(500), places=2),
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal(9), places=3),
            st.decimals(min_value=Decimal(0), max_value=Decimal(3), places=2),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_generated_journal_round_trips(
    app_conn: Conn, worker_conn: Conn, ops: list[tuple[str, Decimal, Decimal, Decimal]]
) -> None:
    tenant, account = make_tenant(app_conn, "G")[0], f"SYN-G{next(_accounts)}"
    mem = Journal()
    for i, (op, a, q, f) in enumerate(ops):
        h = hdr(f"R{i}", i, account)
        if op == "deposit":
            mem.post(deposit(h, usd(a)), T0)
        elif op == "buy":
            mem.post(buy(h, SYN, PositiveQuantity(q), Price(a), usd(f)), T0)
        held = mem.holding(h, SYN).quantity.value
        if op == "sell" and held > 0:
            mem.sell(h, SYN, PositiveQuantity(min(q, held)), Price(a), usd(f), T0)
    store_all(worker_conn, tenant, mem)
    loaded = in_tx(worker_conn, tenant, lambda tx: js.load_journal(tx, account))
    assert loaded.events() == mem.events()
    assert materialize(loaded.events()) == materialize(mem.events())


# --- database-level invariants, written around the adapter ---


@pytest.mark.db
@pytest.mark.parametrize(
    "lines",
    [
        [("cash", "100"), ("external_capital", "-99")],  # unbalanced
        [("cash", "100")],  # one line
        [],  # no lines
        [("cash", "1.0000000000001"), ("external_capital", "-1.0000000000001")],
        [("cash", "1.50"), ("external_capital", "-1.50")],  # not canonical
        [("cash", "0"), ("external_capital", "0")],
        [("u:position", "10"), ("u:unit_clearing", "-9")],  # units unbalanced
        [("u:position", "1")],  # one unit line
    ],
)
def test_direct_insert_of_a_bad_event_is_refused(
    app_conn: Conn, lines: list[tuple[str, str]]
) -> None:
    tenant, _ = make_tenant(app_conn, "A")

    def write(tx: TenantTx) -> None:
        raw_event(tx)
        raw_cash(tx, lines)

    with pytest.raises(errors.CheckViolation):
        in_tx(app_conn, tenant, write)
    assert events(app_conn, tenant) == ()


@pytest.mark.db
def test_lines_cannot_be_added_to_a_committed_event(app_conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    in_tx(app_conn, tenant, lambda tx: js.post(tx, deposit(hdr("D1"), usd(5))))
    eid = deposit(hdr("D1"), usd(5)).event_id
    with pytest.raises(errors.CheckViolation, match="only with their event"):
        in_tx(app_conn, tenant, lambda tx: raw_cash(tx, MINUS5, eid, 2))


@pytest.mark.db
def test_update_delete_truncate_are_refused(app_conn: Conn, conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    in_tx(app_conn, tenant, lambda tx: js.post(tx, deposit(hdr("D1"), usd(5))))
    for table in ("ledger_event", "posting", "unit_posting", "source_record"):
        for stmt in (
            f"UPDATE app.{table} SET tenant_id = tenant_id",
            f"DELETE FROM app.{table}",
            f"TRUNCATE app.{table} CASCADE",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                run(app_conn, tenant, stmt)
            # Ordinary DML of any role; replica mode or DISABLE TRIGGER would pass.
            with pytest.raises(errors.RaiseException, match="append-only"):
                conn.execute(stmt)  # fmt: skip
    for column in ("recorded_at", "account_rev"):  # server-assigned only
        with pytest.raises(errors.InsufficientPrivilege):
            run(app_conn, tenant, f"INSERT INTO app.ledger_event (tenant_id, {column}) "
                "VALUES (app.current_tenant_id(), NULL)")  # fmt: skip


@pytest.mark.db
def test_tenant_isolation(app_conn: Conn) -> None:
    a, _ = make_tenant(app_conn, "A")
    b, _ = make_tenant(app_conn, "B")
    event = deposit(hdr("D1"), usd(7))
    in_tx(app_conn, a, lambda tx: js.post(tx, event))
    assert events(app_conn, b) == ()
    for table in ("ledger_event", "posting"):
        with tenant_transaction(app_conn, b) as tx:
            row = tx.conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
        assert row == (0,)
    with pytest.raises(errors.InsufficientPrivilege, match="row-level security"):
        run(
            app_conn,
            b,
            "INSERT INTO app.source_record (tenant_id, account_id, "
            "source_id, record_id, observed_at, effective_at, payload, fingerprint) "
            "VALUES (%s, 'X', 'S', 'r', now(), now(), '{}', %s)",
            a,
            "0" * 64,
        )
    with pytest.raises(errors.CheckViolation, match="only with their event"):
        run(app_conn, b, "INSERT INTO app.posting (tenant_id, event_id, line, "
            "book_account, currency, wire) VALUES (%s, %s, 9, 'cash', 'USD', '1')",
            b, event.event_id)  # A's event is invisible  # fmt: skip
    # Another tenant's identical record is its own event.
    assert in_tx(app_conn, b, lambda tx: js.post(tx, event)) is Outcome.POSTED


# --- idempotency, concurrency, knowledge time, corrections ---


@pytest.mark.db
def test_idempotent_post_and_conflict(app_conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    event = deposit(hdr("D1"), usd(10))
    assert in_tx(app_conn, tenant, lambda tx: js.post(tx, event)) is Outcome.POSTED
    assert in_tx(app_conn, tenant, lambda tx: js.post(tx, event)) is Outcome.DUPLICATE
    other = deposit(hdr("D1"), usd(11))
    with pytest.raises(js.JournalConflict) as info:
        in_tx(app_conn, tenant, lambda tx: js.post(tx, other))
    assert info.value.existing == event.event_id
    with pytest.raises(errors.UniqueViolation):  # the key holds below the adapter too
        in_tx(app_conn, tenant, lambda tx: raw_event(
            tx, "b" * 64, source="SYN-BROKER", record="D1"))  # fmt: skip
    with pytest.raises(JournalError, match="short_sale_unsupported"):  # domain rule
        in_tx(app_conn, tenant, lambda tx: js.post(tx, num01_journal().events()[2]))
    obs = SourceObservation(ACCT, SourceRef("SYN-BROKER", "D1"), T0, T0, {"a": "1"})
    assert in_tx(app_conn, tenant, lambda tx: js.observe(tx, obs)) is Outcome.POSTED
    assert in_tx(app_conn, tenant, lambda tx: js.observe(tx, obs)) is (
        Outcome.DUPLICATE)  # fmt: skip
    changed = SourceObservation(ACCT, obs.source, T0, T0, {"a": "2"})
    with pytest.raises(js.JournalConflict):
        in_tx(app_conn, tenant, lambda tx: js.observe(tx, changed))
    assert len(events(app_conn, tenant)) == 1


@pytest.mark.db
def test_concurrent_duplicate_posts_give_one_event(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    event, start = deposit(hdr("D1"), usd(3)), threading.Barrier(2)
    outcomes: list[Outcome] = []

    def poster(c: Conn) -> None:
        start.wait()
        outcomes.append(in_tx(c, tenant, lambda tx: js.post(tx, event)))

    threads = [
        threading.Thread(target=poster, args=(c,)) for c in (app_conn, worker_conn)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(outcomes) == [Outcome.DUPLICATE, Outcome.POSTED]
    assert len(events(app_conn, tenant)) == 1


@pytest.mark.db
def test_correction_by_reversal_and_knowledge_time(app_conn: Conn, conn: Conn) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    original = buy(hdr("B1"), SYN, PositiveQuantity(10), Price(50), usd(1))
    in_tx(app_conn, tenant, lambda tx: js.post(tx, original))
    known = in_tx(app_conn, tenant, lambda tx: js.revision(tx, ACCT))
    corrected = buy(Header(ACCT, T0, SourceRef("SYN-BROKER", "B1-fix")), SYN,
                    PositiveQuantity(10), Price(49), usd(1))  # fmt: skip
    replacement = replace(corrected, supersedes=original.event_id)
    assert in_tx(app_conn, tenant, lambda tx: js.correct(tx, replacement)) is (
        Outcome.POSTED)  # fmt: skip
    mem = Journal()
    mem.post(original, T0)
    mem.correct(replacement, T0)
    now = in_tx(app_conn, tenant, lambda tx: js.load_journal(tx, ACCT))
    then = events(app_conn, tenant, None, known)
    told = events(app_conn, tenant, now.entries()[0].recorded_at)  # informational
    assert now.events() == mem.events() and len(now.events()) == 3
    assert then == told == (original,) and known == 2
    assert materialize(now.events()) == materialize(mem.events())
    assert materialize(now.events()).holding(ACCT, SYN).cost == usd(491)
    stamps = [e.recorded_at for e in now.entries()]
    assert stamps == sorted(stamps)
    # The owner cannot set knowledge time or revision either: the trigger does.
    with conn.transaction():
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant),))
        conn.execute("INSERT INTO app.ledger_event (tenant_id, account_id, event_id, "
                     "kind, effective_at, recorded_at, account_rev, tx_id, source_id, "
                     "source_record_id, lot_policy, terms) VALUES (%s, 'SYN-OWN', %s, "
                     "'deposit', now(), '2000-01-01Z', 99, '1', 'S', 'r', 'x/1', '{}')",
                     (tenant, "9" * 64))  # fmt: skip
        conn.execute("INSERT INTO app.posting (tenant_id, event_id, line, "
                     "book_account, currency, wire) VALUES (%s, %s, 0, 'cash', 'USD', "
                     "'1'), (%s, %s, 1, 'expense', 'USD', '-1')",
                     (tenant, "9" * 64, tenant, "9" * 64))  # fmt: skip
        row = conn.execute("SELECT recorded_at > '2001-01-01Z', account_rev FROM "
                           "app.ledger_event WHERE event_id = %s",
                           ("9" * 64,)).fetchone()  # fmt: skip
        assert row == (True, 1)
    # A balanced row whose id is not its content fingerprint fails the load. Open
    # gate: such a row bricks the account; there is no quarantine yet.
    with tenant_transaction(app_conn, tenant) as tx:
        raw_event(tx, "8" * 64)
        raw_cash(tx, [("cash", "1"), ("expense", "-1")], "8" * 64)
    with pytest.raises(js.JournalIntegrityError):
        events(app_conn, tenant)


@pytest.mark.db
@pytest.mark.parametrize(
    ("target", "extra", "lines", "error"),
    [
        (0, {"supersedes": "y"}, [("cash", "1"), ("expense", "-1")],
         errors.CheckViolation),  # supersede with no reversal
        (0, {}, [("cash", "-4"), ("external_capital", "4")], errors.CheckViolation),
        (0, {}, [*MINUS5, ("expense", "1"), ("expense", "-1")],
         errors.CheckViolation),  # line count differs
        (0, {"day": "1"}, MINUS5, errors.CheckViolation),  # other effective time
        (0, {"account": "SYN-OTHER"}, MINUS5, errors.ForeignKeyViolation),
        (4, {"day": "1"}, [("u:position", "-5"), ("u:" + CAC, "5")],
         errors.CheckViolation),  # split units not negated
        (3, {"day": "1"}, [("u:position", "10"), ("u:" + CAC, "-10")],
         errors.CheckViolation),  # reversal of a reversal
        (2, {"day": "1"}, [("u:position", "-10"), ("u:" + CAC, "10")],
         errors.UniqueViolation),  # second reversal
    ],
)  # fmt: skip
def test_bad_links_are_refused_below_the_adapter(
    app_conn: Conn, target: int, extra: dict[str, str],
    lines: list[tuple[str, str]], error: type[Exception],
) -> None:  # fmt: skip
    tenant, _ = make_tenant(app_conn, "A")
    held = Holding(ACCT, SYN, Quantity(10), usd(501))
    action = Split("ca-1", 1, SYN, T0.date(), SourceRef("SYN-CA", "1"), T0,
                   PositiveRatio.from_decimal("2"))  # fmt: skip
    s1 = split(hdr("S1", 1), action, held)
    events = [deposit(hdr("D9"), usd(5)), buy(hdr("B1"), SYN, PositiveQuantity(10),
              Price(50), usd(1)), s1, s1.reversal(SourceRef("SYN-BROKER", "S1-rev")),
              split(hdr("S2", 1), action, held)]  # fmt: skip
    for event in events:  # includes a unit-only reversal of a split, accepted
        with tenant_transaction(app_conn, tenant) as tx:
            assert js.post(tx, event) is Outcome.POSTED
    field = "supersedes" if "supersedes" in extra else "reverses"
    cols = {**extra, field: events[target].event_id}
    with pytest.raises(error), tenant_transaction(app_conn, tenant) as tx:
        raw_event(tx, "e" * 64, "deposit" if field == "supersedes" else "reversal",
                  **cols)  # fmt: skip
        raw_cash(tx, lines, "e" * 64)


@pytest.mark.db
def test_as_of_revision_is_stable_after_a_later_post(
    app_conn: Conn, worker_conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    with tenant_transaction(worker_conn, tenant) as late:
        late.conn.execute("SELECT now()")  # this transaction starts first
        in_tx(app_conn, tenant, lambda tx: js.post(tx, deposit(hdr("D1"), usd(1))))
        rev = in_tx(app_conn, tenant, lambda tx: js.revision(tx, ACCT))
        before = in_tx(
            app_conn, tenant, lambda tx: js.load_journal(tx, ACCT, None, rev)
        )
        js.post(late, deposit(hdr("D2", 1), usd(2)))
    after = in_tx(app_conn, tenant, lambda tx: js.load_journal(tx, ACCT, None, rev))
    assert after.events() == before.events() and len(before.events()) == 1
    assert (rev, in_tx(app_conn, tenant, lambda tx: js.revision(tx, ACCT))) == (2, 3)


@pytest.mark.db
def test_raw_inserts_replay_in_commit_order_with_gapless_revisions(
    app_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    tenant, _ = make_tenant(app_conn, "A")
    ev = [deposit(hdr(f"X{i}"), usd(i + 1)) for i in range(4)]
    started, go = threading.Event(), threading.Event()

    def early() -> None:  # starts first, inserts while another holds the lock
        with tenant_transaction(worker_conn, tenant) as tx:
            tx.conn.execute("SELECT now()")
            started.set()
            go.wait(10)
            js._insert(tx, ev[2])

    thread = threading.Thread(target=early)
    thread.start()
    started.wait(10)
    with tenant_transaction(app_conn, tenant) as tx:
        js._insert(tx, ev[0])
        go.set()
        deadline = time.monotonic() + 10
        while conn.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
        ).fetchone() == (0,):
            assert time.monotonic() < deadline, "the insert never waited for the lock"
            time.sleep(0.01)  # fmt: skip
        js._insert(tx, ev[1])
    thread.join(30)
    with (
        pytest.raises(errors.CheckViolation),
        tenant_transaction(app_conn, tenant) as tx,
    ):
        raw_event(tx)  # takes revision 4, then rolls back
        raw_cash(tx, MINUS5[:1])
    in_tx(app_conn, tenant, lambda tx: js.post(tx, ev[3]))
    assert events(app_conn, tenant) == tuple(ev)  # commit order, not start order
    revs = conn.execute(
        "SELECT array_agg(account_rev ORDER BY account_rev) FROM "
        "app.ledger_event WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert revs == ([1, 2, 3, 4],)  # fmt: skip


# --- catalogue ---


@pytest.mark.db
def test_journal_catalogue(conn: Conn) -> None:
    from qw_adapters.migrations import migrate

    migrate(conn)
    funcs = conn.execute(
        "SELECT p.proname, p.prosecdef, p.proconfig FROM pg_proc p WHERE "
        "p.pronamespace = 'app'::regnamespace AND p.proname IN ('journal_append_only',"
        " 'ledger_event_stamp', 'posting_same_transaction', 'ledger_event_check') "
        "ORDER BY 1"
    ).fetchall()
    assert len(funcs) == 4
    for _, secdef, config in funcs:  # invoker rights: checks run under RLS
        assert (secdef, config) == (False, ["search_path=pg_catalog"])
    trig = conn.execute(
        "SELECT tgdeferrable, tginitdeferred FROM pg_trigger WHERE tgname = "
        "'balanced' AND tgrelid = 'app.ledger_event'::regclass"
    ).fetchone()
    assert trig == (True, True)
    for role in ("qw_app", "qw_worker", "qw_ingest", "qw_assessor", "qw_release"):
        for table in ("ledger_event", "posting", "unit_posting", "source_record"):
            for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                held = conn.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f"app.{table}", priv),
                ).fetchone()
                assert held == (False,), (role, table, priv)
    tenant = uuid.uuid4()
    conn.execute("INSERT INTO app.tenant (id) VALUES (%s)", (tenant,))
    conn.execute("SET ROLE qw_ingest")  # source records only (T008 section 5)
    obs = SourceObservation(ACCT, SourceRef("SYN-BROKER", "R1"), T0, T0, {"a": "1"})
    assert in_tx(conn, tenant, lambda tx: js.observe(tx, obs)) is Outcome.POSTED
    for table in ("ledger_event", "posting", "unit_posting"):
        with (
            pytest.raises(errors.InsufficientPrivilege),
            tenant_transaction(conn, tenant) as tx,
        ):
            tx.conn.execute(f"SELECT 1 FROM app.{table}")  # fmt: skip
    conn.execute("RESET ROLE")
