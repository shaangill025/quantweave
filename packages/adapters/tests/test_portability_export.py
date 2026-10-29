"""The journal and policy export functions (T057 increment 2) on PostgreSQL 16: a
SYNTHETIC tenant is exported as the non-superuser qw_app login (conftest), under
RLS in a snapshot transaction, then built, archived, read back and verified. The
superuser `conn` only plays a tampering owner where a test says so. The oracle for
each row is the stored history the stores already verify (`load_journal`, `load`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import journal_store as js
from qw_adapters import policy_store as ps
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_adapters.export_archive import read_archive, write_archive
from qw_domain import ledger_wire as lw
from qw_domain import portability as pt
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import Money, PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import format_instant, parse_instant
from qw_domain.postings import (
    Header,
    JournalError,
    JournalEvent,
    buy,
    deposit,
    dividend,
    sell,
    split,
)
from qw_domain.simulation import VirtualPortfolio
from test_portability_tenant_sections import draft  # SYNTHETIC answers

Conn = psycopg.Connection[TupleRow]
J, P = pt.JOURNAL_PATH, pt.POLICIES_PATH
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
ACK = "SYNTHETIC: I adopt this"


@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    owner: uuid.UUID
    session: uuid.UUID
    policy: uuid.UUID
    events: tuple[str, ...]


def seed(app_conn: Conn, record: str) -> Tenant:
    """Two journal accounts (a deposit, a buy and its reversal; a deposit), one
    adopted then revised policy, and a virtual portfolio, all SYNTHETIC."""
    with tn.provision_tenant(app_conn) as tx:
        owner = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, owner, tn.MembershipRole.TENANT_OWNER)
        session, _ = tn.create_session(tx, owner, timedelta(hours=1))
        tn.mark_step_up(tx, session)
        now = tx.conn.execute("SELECT now()").fetchone()
    assert now is not None
    t0, tenant, policy = now[0] - timedelta(days=3), tx.tenant_id, uuid.uuid4()

    def h(acct: str, n: int) -> Header:
        source = SourceRef("SYN-BROKER", f"{record}-{n}")
        return Header(acct, t0 + timedelta(hours=n), source)

    bought = buy(h("SYN-ACCT", 1), SYN, PositiveQuantity("2"), Price("100.25"),
                 Money.of("1", "USD"))  # fmt: skip
    events = (deposit(h("SYN-ACCT", 0), Money.of("1000", "USD")), bought,
              bought.reversal(SourceRef("SYN-BROKER", f"{record}-2")),
              deposit(h("SYN-ACCT-2", 3), Money.of("50", "CAD")))  # fmt: skip
    with tn.tenant_transaction(app_conn, tenant) as tx:
        js.post_many(tx, events)
        first = ps.propose(tx, policy, draft(tenant=str(tenant)), owner).versions[0]
        ps.adopt(tx, policy, 1, first.content_hash,
                 principal=owner, session_id=session, window=timedelta(minutes=10),
                 acknowledged=frozenset(), text=ACK)  # fmt: skip
        ps.propose(tx, policy, draft("income", str(tenant)), owner)
        sim = VirtualPortfolio.open("SIM-SYN", Money.of("500", "USD"), now[0])
        ss.save(tx, ss.Simulation("SYNTHETIC", "exec-synth-1", "bench-synth-1", sim))
    return Tenant(tenant, owner, session, policy, tuple(e.event_id for e in events))


def export(app_conn: Conn, t: Tenant, page: int = 500) -> dict[str, Any]:
    with tn.tenant_transaction(app_conn, t.id, snapshot=True) as tx:
        return {J: js.export_journal(tx, page), P: ps.export_policies(tx, page)}


@pytest.mark.db
def test_round_trip_through_the_archive(app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    sections = export(app_conn, a)
    assert sections == export(app_conn, a, page=1)  # keyset pages agree
    journal, policies = sections[J], sections[P]
    assert [r["id"] for r in journal] == list(a.events)  # account, then revision
    assert [(r["kind"], r["status"]) for r in journal] == [
        ("deposit", "accepted"), ("buy", "reversed"), ("correction", "accepted"),
        ("deposit", "accepted")]  # fmt: skip
    with tn.tenant_transaction(app_conn, a.id) as tx:
        history = ps.load(tx, a.policy)
        stored = [e.recorded_at for e in js.load_journal(tx, "SYN-ACCT").entries()]
    assert history is not None
    assert [(r["version"], r["content_hash"]) for r in policies] == [
        (v.version, v.content_hash) for v in history.versions
    ]
    assert [r["recorded_at"] for r in journal[:3]] == [
        format_instant(t) for t in stored
    ]  # knowledge times as stored
    bundle = pt.build_export("export-synth-1", str(a.id), parse_instant(
        journal[-1]["recorded_at"]) + timedelta(seconds=1), sections)  # fmt: skip
    report = read_archive(write_archive(bundle))
    assert report.data == sections and report.omissions == pt.ALWAYS_OMITTED
    raw = b"".join(bundle.files.values()).decode()
    receipt = history.receipts[0]
    for secret in (str(a.owner), str(a.session), receipt.step_up_receipt_id, ACK,
                   receipt.receipt_hash, "SIM-SYN"):  # fmt: skip
        assert secret not in raw  # approval, identity and virtual material


@pytest.mark.db
def test_another_tenants_rows_never_appear(app_conn: Conn) -> None:
    a, b = seed(app_conn, "a"), seed(app_conn, "b")  # same account names in both
    sa, sb = export(app_conn, a), export(app_conn, b)
    assert {r["tenant_id"] for r in sa[J]} == {str(a.id)}
    assert {r["content"]["tenant_id"] for r in sa[P]} == {str(a.id)}
    assert {r["id"] for r in sb[J]} == set(b.events)
    text = pt.canonical(sa).decode()
    for foreign in (str(b.id), str(b.policy), *b.events):
        assert foreign not in text
    with pytest.raises(pt.PortabilityError) as e:  # b's rows under a's scope
        pt.build_export("export-synth-1", str(a.id), parse_instant(
            sa[J][-1]["recorded_at"]) + timedelta(days=1), {J: sb[J]})  # fmt: skip
    assert e.value.code is pt.Refusal.SCOPE_MISMATCH


@pytest.mark.db
def test_tampered_rows_are_never_exported(app_conn: Conn, conn: Conn) -> None:
    a = seed(app_conn, "a")
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(
        "UPDATE app.policy_version SET content = jsonb_set(content, "
        "'{limits,0,value}', '\"0.5\"') WHERE version = 2"
    )
    with pytest.raises(ps.PolicyStoreError, match="integrity"):
        export(app_conn, a)
    conn.execute(
        "UPDATE app.policy_version SET content = jsonb_set(content, "
        "'{limits,0,value}', '\"0.05\"') WHERE version = 2"
    )
    assert export(app_conn, a)[P]  # restored
    conn.execute(
        "UPDATE app.ledger_event SET effective_at = effective_at + interval "
        "'1 second' WHERE event_id = %s",
        (a.events[0],),
    )
    with pytest.raises(js.JournalIntegrityError):
        export(app_conn, a)


@pytest.mark.db
def test_exports_need_a_read_only_snapshot(app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    for fn in (js.export_journal, ps.export_policies):
        with tn.tenant_transaction(app_conn, a.id) as tx, pytest.raises(
            tn.SnapshotRequired):  # fmt: skip
            fn(tx)
        with tn.tenant_transaction(app_conn, a.id) as tx, pytest.raises(
            tn.SnapshotRequired):  # fmt: skip
            tx.conn.execute("SET TRANSACTION READ ONLY")  # still READ COMMITTED
            fn(tx)
        with app_conn.transaction(), pytest.raises(tn.SnapshotRequired):
            app_conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            app_conn.execute("SELECT set_config('app.tenant_id', %s, true)",
                             (str(a.id),))  # fmt: skip
            fn(tn.TenantTx(app_conn, a.id))  # writable


@pytest.mark.db
def test_recorded_at_is_trusted_from_the_store(app_conn: Conn, conn: Conn) -> None:
    """The event id covers source content, not knowledge time: a tampered but
    still ordered `recorded_at` is exported as stored; only an order break fails
    the replay. Recorded in the receipt as a limitation."""
    a = seed(app_conn, "a")
    before = export(app_conn, a)[J]
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(
        "UPDATE app.ledger_event SET recorded_at = recorded_at - interval '1 day' "
        "WHERE event_id = %s",
        (a.events[0],),
    )
    after = export(app_conn, a)[J]
    assert after[0]["recorded_at"] != before[0]["recorded_at"]  # not detected
    assert after[1:] == before[1:]
    conn.execute(
        "UPDATE app.ledger_event SET recorded_at = recorded_at + interval '10 days' "
        "WHERE event_id = %s",
        (a.events[1],),
    )
    with pytest.raises(JournalError, match="recorded_at_order"):  # replay refuses
        export(app_conn, a)


@pytest.mark.db
def test_rich_journal_round_trips_exactly(app_conn: Conn) -> None:
    """USD and CAD, fractional quantities, a fee, a dividend, a sale, a 2:1 split,
    and a superseded buy with its reversal (the reviewer's probe data)."""
    a = seed(app_conn, "a")
    i2 = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005b"))
    acct, usd, cad = "SYN-RICH", "USD", "CAD"
    with tn.tenant_transaction(app_conn, a.id) as tx:
        now = tx.conn.execute("SELECT now()").fetchone()
        assert now is not None

        def h(n: int, record: str) -> Header:
            when = now[0] - timedelta(days=2) + timedelta(hours=n)
            return Header(acct, when, SourceRef("SYN-BROKER", record))

        for e in (deposit(h(0, "d1"), Money.of("10000.50", usd)),
                  deposit(h(0, "d2"), Money.of("5000", cad)),
                  buy(h(1, "b1"), SYN, PositiveQuantity("10.5"),
                      Price("100.123456"), Money.of("1.25", usd)),
                  dividend(h(2, "v1"), SYN, Money.of("3.33", usd))):  # fmt: skip
            js.post(tx, e)
        held = js.load_journal(tx, acct).holding(h(3, "s1"), SYN)
        js.post(tx, sell(h(3, "s1"), PositiveQuantity("4"), Price("110"),
                         Money.of("1", usd), held))  # fmt: skip
        held = js.load_journal(tx, acct).holding(h(4, "sp"), SYN)
        ca = SourceRef("SYN-CA", "ca")
        action = Split("ca-1", 1, SYN, h(4, "sp").effective_at.date(), ca, now[0],
                       PositiveRatio(2, 1))  # fmt: skip
        js.post(tx, split(h(4, "sp"), action, held))
        old = buy(h(5, "b2"), i2, PositiveQuantity("3"), Price("50"),
                  Money.of("2", cad))  # fmt: skip
        js.post(tx, old)
        c1 = SourceRef("SYN-BROKER", "c1")
        new = JournalEvent(old.kind, acct, old.effective_at, c1, old.money, old.units,
                           old.fee, None, old.event_id)  # supersedes `old`  # fmt: skip
        js.correct(tx, new)
        stored = js.load_journal(tx, acct).entries()
    rows = [r for r in export(app_conn, a)[J] if r["account_id"] == acct]
    assert [r["kind"] for r in rows] == [
        "deposit",
        "deposit",
        "buy",
        "dividend",
        "sell",
        "split",
        "buy",
        "correction",
        "buy",
    ]
    assert tuple(lw.parse_section(rows)) == tuple(stored)  # the domain's entries
    sections = export(app_conn, a)
    bundle = pt.build_export("export-synth-1", str(a.id), now[0] + timedelta(1),
                             sections)  # fmt: skip
    assert read_archive(write_archive(bundle)).data == sections
