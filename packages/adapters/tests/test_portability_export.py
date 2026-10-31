"""The journal, policy, watchlist, account and tombstone export functions (T057
increments 2 to 5; tombstones are appended as the qw_worker login) on PostgreSQL
16: a SYNTHETIC tenant is exported as the non-superuser qw_app login (conftest),
under RLS in a snapshot transaction, then built, archived, read back and verified.
The superuser `conn` only plays a tampering owner where a test says so. The oracle
for each row is the stored history the stores already verify (`load_journal`,
`revision`, `load`, `page`, `account_store.load`).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import account_store as st
from qw_adapters import journal_store as js
from qw_adapters import policy_store as ps
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as tb
from qw_adapters import watchlist_store as ws
from qw_adapters.export_archive import read_archive, write_archive
from qw_domain import account_wire as aw
from qw_domain import ledger_wire as lw
from qw_domain import portability as pt
from qw_domain import retention as rt
from qw_domain import tombstone_wire as tw
from qw_domain import watchlists as wl
from qw_domain.corporate_actions import PositiveRatio, SourceRef, Split
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import format_instant, parse_instant
from qw_domain.monitoring import Condition
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
from qw_domain.research_data import digest
from qw_domain.simulation import VirtualPortfolio
from qw_domain.sources import (
    HoldingLine,
    MappedInterval,
    Source,
    SourceKind,
    SourceSnapshot,
    consolidate,
)
from test_portability_tenant_sections import draft  # SYNTHETIC answers

Conn = psycopg.Connection[TupleRow]
J, P, W, A = pt.JOURNAL_PATH, pt.POLICIES_PATH, pt.WATCHLISTS_PATH, pt.ACCOUNTS_PATH
T = pt.TOMBSTONES_PATH
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
ACK = "SYNTHETIC: I adopt this"
COND = Condition("price_cross", "gt", ("100.25",), "feed-synth-iex", "SYNTHETIC")
V0 = datetime(2026, 1, 1, tzinfo=UTC)  # valid times; knowledge times are the server's
V1 = datetime(2026, 6, 1, tzinfo=UTC)
AT = datetime(2026, 10, 1, 15, tzinfo=UTC)
NFD = unicodedata.normalize("NFD", "Crédit Agricole")
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    owner: uuid.UUID
    session: uuid.UUID
    policy: uuid.UUID
    events: tuple[str, ...]
    token: str = ""
    lists: tuple[str, str] = ("", "")  # live, deleted


def seed(app_conn: Conn, record: str) -> Tenant:
    """Two journal accounts (a deposit, a buy and its reversal; a deposit), one
    adopted then revised policy, a virtual portfolio, a live watchlist with two
    entries and a deleted one, all SYNTHETIC."""
    with tn.provision_tenant(app_conn) as tx:
        owner = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, owner, tn.MembershipRole.TENANT_OWNER)
        session, token = tn.create_session(tx, owner, timedelta(hours=1))
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
        key = f"synthetic-key-{record}-"
        live = ws.create(tx, owner, session, f"{key}1", "SYNTHETIC growth",
                         "SYNTHETIC notes")[0].watchlist.watchlist_id  # fmt: skip
        for n in (2, 1):  # stored in instrument order
            iid = InstrumentId(uuid.UUID(f"00000000-0000-4000-8000-00000000006{n}"))
            e = wl.Entry(iid, f"SYNTHETIC thesis {n}", ("syn",), now[0], 10 * n,
                         (COND,), "SYNTHETIC invalidation")  # fmt: skip
            ws.add_entry(tx, live, owner, session, f"{key}e{n}", None, e)
        gone = ws.create(tx, owner, session, f"{key}2", "SYNTHETIC gone",
                         "SYNTHETIC deleted text")[0]  # fmt: skip
        ws.delete(tx, gone.watchlist.watchlist_id, owner, session, f"{key}3", 1)
    with tn.tenant_transaction(app_conn, tenant) as tx:  # accounts (T013)
        for aid, broker, kind, ccy in (
            ("SYN-A", NFD, "TFSA - Self-directed", "CAD"),
            ("SYN-B", "Interactive Brokers", "margin", "USD"),
            ("SYN-OLD", "Société Générale", "cash", "USD"),
        ):
            a = st.account_from_text(tx, aid, broker, kind, ccy)  # NFC from here
            st.register_account(tx, a, f"SYNTHETIC {aid}", "complete_declared")
        st.add_source(tx, Source("SYN-BRK", str(tenant), SourceKind.BROKER_EXPORT),
                      "syn-broker")  # fmt: skip
        st.add_source(tx, Source("SYN-YAHOO", str(tenant), SourceKind.MIRROR), "yahoo")
        for src, ref, aid in (("SYN-BRK", "1001", "SYN-A"),
                              ("SYN-YAHOO", "pf-1", "SYN-A"),
                              ("SYN-BRK", "1002", "SYN-OLD")):  # fmt: skip
            st.remap(tx, src, ref, (MappedInterval(aid, V0),), 0)
        st.remap(tx, "SYN-BRK", "1002", (MappedInterval("SYN-OLD", V0, V1),
                                         MappedInterval("SYN-B", V1)), 1)  # fmt: skip
        st.archive_account(tx, "SYN-OLD", 1)
    return Tenant(tenant, owner, session, policy, tuple(e.event_id for e in events),
                  token, (live, gone.watchlist.watchlist_id))  # fmt: skip


def export(app_conn: Conn, t: Tenant, page: int = 500) -> dict[str, Any]:
    with tn.tenant_transaction(app_conn, t.id, snapshot=True) as tx:
        return {J: js.export_journal(tx, page), P: ps.export_policies(tx, page),
                W: ws.export_watchlists(tx), A: st.export_accounts(tx),
                T: tb.export_tombstones(tx)}  # fmt: skip


@pytest.mark.db
def test_round_trip_through_the_archive(app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    sections = export(app_conn, a)
    assert sections == export(app_conn, a, page=1)  # keyset pages agree
    journal, policies = sections[J]["events"], sections[P]
    assert [r["id"] for r in journal] == list(a.events)  # account, then revision
    assert [(r["kind"], r["status"]) for r in journal] == [
        ("deposit", "accepted"), ("buy", "reversed"), ("correction", "accepted"),
        ("deposit", "accepted")]  # fmt: skip
    with tn.tenant_transaction(app_conn, a.id) as tx:
        history = ps.load(tx, a.policy)
        stored = [e.recorded_at for e in js.load_journal(tx, "SYN-ACCT").entries()]
        revs = {acct: js.revision(tx, acct) for acct in ("SYN-ACCT", "SYN-ACCT-2")}
        lists = ws.page(tx, 10, None)
    assert [
        (c["account_id"], c["revision"], c["event_count"])
        for c in sections[J]["chains"]
    ] == [("SYN-ACCT", revs["SYN-ACCT"], 3), ("SYN-ACCT-2", revs["SYN-ACCT-2"], 1)]
    assert [w["id"] for w in sections[W]] == [a.lists[0]]  # the deleted one is not
    assert [(w["revision"], w["name"], w["description"],
             [wl.entry_from_json(e) for e in w["entries"]]) for w in sections[W]] == [
        (r.revision, r.watchlist.name, r.watchlist.description,
         list(r.watchlist.entries)) for r in lists]  # fmt: skip
    assert history is not None
    assert [(r["version"], r["content_hash"]) for r in policies] == [
        (v.version, v.content_hash) for v in history.versions
    ]
    assert [r["recorded_at"] for r in journal[:3]] == [
        format_instant(t) for t in stored
    ]  # knowledge times as stored
    bundle = pt.build_export("export-synth-1", str(a.id), parse_instant(
        journal[-1]["recorded_at"]) + timedelta(seconds=1), sections)  # fmt: skip
    archive = write_archive(bundle)
    report = read_archive(archive)
    assert report.data == sections and report.omissions == pt.ALWAYS_OMITTED
    assert report.pending_reconnections == ()  # no broker connection is stored
    raw = b"".join(bundle.files.values()).decode()
    receipt = history.receipts[0]
    assert a.token and a.token not in raw  # the session bearer secret
    with tn.tenant_transaction(app_conn, a.id) as tx:
        keys = [r[0] for r in tx.conn.execute(
            "SELECT idempotency_key FROM app.watchlist_revision")]  # fmt: skip
    for secret in (str(a.owner), str(a.session), receipt.step_up_receipt_id, ACK,
                   receipt.receipt_hash, "SIM-SYN", a.lists[1],
                   "SYNTHETIC deleted text", *keys):  # fmt: skip
        assert secret not in raw  # approval, identity, deleted and virtual material
        assert secret.encode() not in archive  # nor anywhere in the archive bytes
    pt.scan([pt.parse_json(b, p) for p, b in bundle.files.items()])  # no secret


@pytest.mark.db
def test_another_tenants_rows_never_appear(app_conn: Conn) -> None:
    a, b = seed(app_conn, "a"), seed(app_conn, "b")  # same account names in both
    sa, sb = export(app_conn, a), export(app_conn, b)
    assert {r["tenant_id"] for r in sa[J]["events"]} == {str(a.id)}
    assert {r["content"]["tenant_id"] for r in sa[P]} == {str(a.id)}
    assert {r["tenant_id"] for r in sa[W]} == {str(a.id)}
    assert {r["id"] for r in sb[J]["events"]} == set(b.events)
    assert [r["id"] for r in sb[W]] == [b.lists[0]]
    text = pt.canonical(sa).decode()
    for foreign in (str(b.id), str(b.policy), *b.events, *b.lists):
        assert foreign not in text
    # the same account names, but each chain starts from its own tenant
    assert [c["head"] for c in sa[J]["chains"]] != [c["head"] for c in sb[J]["chains"]]
    later = parse_instant(sa[J]["events"][-1]["recorded_at"]) + timedelta(days=1)
    assert {r["tenant_id"] for rows in sa[A].values() for r in rows} == {str(a.id)}
    assert sa[A]["accounts"] != sb[A]["accounts"]  # the same ids, other hashes
    for path in (J, W, A, T):  # b's sections under a's scope
        with pytest.raises(pt.PortabilityError) as e:
            pt.build_export("export-synth-1", str(a.id), later, {path: sb[path]})
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
    conn.execute(
        "UPDATE app.ledger_event SET effective_at = effective_at - interval "
        "'1 second' WHERE event_id = %s",
        (a.events[0],),
    )
    assert export(app_conn, a)[J]  # restored
    conn.execute("UPDATE app.watchlist_revision SET name = 'SYNTHETIC renamed' "
                 "WHERE watchlist_id = %s AND revision = 1", (a.lists[0],))  # fmt: skip
    with pytest.raises(ws.WatchlistStoreError, match="integrity"):
        export(app_conn, a)


@pytest.mark.db
def test_a_revision_gap_is_never_exported(app_conn: Conn, conn: Conn) -> None:
    """The chain binds row n to `account_rev` n, so the export refuses an account
    whose revisions are not 1..n, even when what is left still replays."""
    a = seed(app_conn, "a")
    t0 = parse_instant(export(app_conn, a)[J]["events"][0]["effective_at"])
    with tn.tenant_transaction(app_conn, a.id) as tx:
        for n in range(2):
            src = SourceRef("SYN-BROKER", f"gap-{n}")
            js.post(tx, deposit(Header("SYN-GAP", t0, src), Money.of("5", "USD")))
        first = js.load_journal(tx, "SYN-GAP").entries()[0].event.event_id
    assert len(export(app_conn, a)[J]["chains"]) == 3
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    for table in ("posting", "ledger_event"):
        conn.execute(f"DELETE FROM app.{table} WHERE event_id = %s", (first,))
    with tn.tenant_transaction(app_conn, a.id) as tx:
        assert len(js.load_journal(tx, "SYN-GAP").entries()) == 1  # still replays
    with pytest.raises(js.JournalIntegrityError, match="gaps"):
        export(app_conn, a)


@pytest.mark.db
def test_exports_need_a_read_only_snapshot(app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    for fn in (js.export_journal, ps.export_policies, ws.export_watchlists,
               st.export_accounts, tb.export_tombstones):  # fmt: skip
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
    """In the store, the event id covers source content, not knowledge time: a
    tampered but still ordered `recorded_at` is exported as stored (the chain is
    then computed over it); only an order break fails the replay. Recorded in the
    receipt as a limitation of the store, not of the archive."""
    a = seed(app_conn, "a")
    before = export(app_conn, a)[J]["events"]
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute(
        "UPDATE app.ledger_event SET recorded_at = recorded_at - interval '1 day' "
        "WHERE event_id = %s",
        (a.events[0],),
    )
    after = export(app_conn, a)[J]["events"]
    assert after[0]["recorded_at"] != before[0]["recorded_at"]  # not detected
    assert after[1:] == before[1:]
    conn.execute(
        "UPDATE app.ledger_event SET recorded_at = recorded_at + interval '10 days' "
        "WHERE event_id = %s",
        (a.events[1],),
    )
    with pytest.raises(JournalError, match="recorded_at_order"):  # replay refuses
        export(app_conn, a)


def _reimport(
    bundle: pt.ExportBundle, body: dict[str, Any], path: str = J
) -> pt.Refusal | None:
    """An archive edit: `body` as the `path` file, under a manifest whose file
    hash and size are recomputed (no signature exists to stop that). None when the
    edited archive verifies."""
    raw = pt.canonical(body)
    files = {**bundle.files, path: raw}
    m = bundle.manifest
    entries = tuple(pt.FileEntry(f.path, pt.sha256(files[f.path]),
                                 len(files[f.path]), f.content_class)
                    for f in m.files)  # fmt: skip
    edited = pt.ExportManifest(m.id, m.format_version, m.created_at, m.scope_id,
                               entries, m.omissions, m.required_reconnections,
                               m.compatibility_version)  # fmt: skip
    try:
        pt.verify_import(pt.canonical(edited.to_wire()), files)
    except pt.PortabilityError as e:
        return e.code
    return None


@pytest.mark.db
def test_archive_edits_of_knowledge_time_order_and_tail_are_refused(
    app_conn: Conn,
) -> None:
    """Increment 2's gap: each edit below replays, so only the chain header
    catches it (`journal_chain`). A rewriter who also recomputes the header is not
    caught: that needs a signature."""
    a, tid = seed(app_conn, "a"), ""
    t0 = parse_instant(export(app_conn, a)[J]["events"][0]["effective_at"])
    with tn.tenant_transaction(app_conn, a.id) as tx:
        tid = str(tx.tenant_id)
        heads = [Header("SYN-ACCT-2", t0, SourceRef("SYN-BROKER", f"z{n}"))
                 for n in range(2)]  # fmt: skip
        js.post_many(tx, [deposit(h, Money.of("7", "CAD")) for h in heads])
    sections = export(app_conn, a)
    events, chains = sections[J]["events"], sections[J]["chains"]
    assert [r["account_id"] for r in events] == ["SYN-ACCT"] * 3 + ["SYN-ACCT-2"] * 3
    later = parse_instant(events[-1]["recorded_at"]) + timedelta(seconds=1)
    bundle = pt.build_export("export-synth-1", tid, later, sections)
    assert read_archive(write_archive(bundle)).data == sections
    assert _reimport(bundle, sections[J]) is None  # the unedited body verifies
    backdated = [dict(r) for r in events]
    t1, t2 = (parse_instant(backdated[n]["recorded_at"]) for n in (0, 1))
    backdated[1]["recorded_at"] = format_instant(t1 + (t2 - t1) / 2)  # order holds
    for rows in (backdated, events[:-1], events[:3]):  # backdate, tail, account
        assert lw.parse_section(rows)  # the rows alone replay
        body = {"chains": chains, "events": rows}
        assert _reimport(bundle, body) is pt.Refusal.JOURNAL_CHAIN
    tied = [dict(r) for r in events]  # equal knowledge times in the store's form
    tied[5]["recorded_at"] = tied[4]["recorded_at"]
    tie = pt.build_export("export-synth-2", tid, later, {J: lw.section(tid, tied)})
    swapped = [*tied[:4], tied[5], tied[4]]
    assert lw.parse_section(swapped)
    body = {"chains": lw.chains(tid, tied), "events": swapped}
    assert _reimport(tie, body) is pt.Refusal.JOURNAL_CHAIN
    rewritten = lw.section(tid, events[:-1])  # rows and header rewritten together
    assert _reimport(bundle, rewritten) is None  # not detectable without a signature


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
    rows = [r for r in export(app_conn, a)[J]["events"] if r["account_id"] == acct]
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


def _snap(src: str, ref: str, qty: str) -> SourceSnapshot:  # SYNTHETIC holdings
    return SourceSnapshot(src, ref, AT, (HoldingLine(SYN, "USD", Quantity(qty)),),
                          (), True)  # fmt: skip


@pytest.mark.db
def test_accounts_round_trip_and_the_mirror_still_counts_once(app_conn: Conn) -> None:
    """Accounts, archive, sources, kinds and mapping chains come back exactly
    through the archive, and consolidating the imported map gives what the store's
    map gives: the broker and its Yahoo mirror of SYN-A count once, SYN-B stays
    distinct."""
    a = seed(app_conn, "a")
    sections = export(app_conn, a)
    later = parse_instant(sections[J]["events"][-1]["recorded_at"]) + timedelta(1)
    report = read_archive(write_archive(pt.build_export("export-synth-1", str(a.id),
                                                        later, sections)))  # fmt: skip
    assert report.data == sections and report.pending_reconnections == ()
    got = aw.parse_section(str(a.id), report.data[A])  # type: ignore[arg-type]
    snaps = [_snap("SYN-BRK", "1001", "50"), _snap("SYN-YAHOO", "pf-1", "50"),
             _snap("SYN-BRK", "1002", "50")]  # fmt: skip
    with tn.tenant_transaction(app_conn, a.id) as tx:
        reg = st.load(tx)
        states = st.read_accounts(tx, snaps, AT, timedelta(days=1))
    assert {k: (r.account, r.name, r.analysis_scope, r.recorded_at, r.revision,
                r.lifecycle) for k, r in got.accounts.items()} == {
        k: (r.account, r.name, r.analysis_scope, r.recorded_at, r.revision,
            r.lifecycle) for k, r in reg.accounts.items()}  # fmt: skip
    assert got.accounts["SYN-OLD"].lifecycle == "archived"
    assert got.accounts["SYN-A"].account.institution == "Crédit Agricole" != NFD
    assert {k: (s.source, s.provider) for k, s in reg.sources.items()} == dict(
        got.sources)  # fmt: skip
    assert got.sources["SYN-YAHOO"][0].kind is SourceKind.MIRROR  # never upgraded
    assert got.revisions == reg.revisions and len(got.revisions) == 4
    views = consolidate(got.source_map, str(a.id), snaps, AT, timedelta(1)).accounts
    for aid, state in states.items():
        v = views[aid]
        assert (v.status, dict(v.units), dict(v.cash), v.sources) == (
            state.view.status, dict(state.view.units), dict(state.view.cash),
            state.view.sources)  # fmt: skip
    units = [v.units.get((SYN, "USD")) for v in views.values()]
    assert units.count(Quantity("50")) == 2 and units.count(None) == 1  # 100, once
    assert views["SYN-A"].sources == ("SYN-BRK", "SYN-YAHOO")
    # source_ids: the latest revision per (source, reference), any interval dates,
    # text ids as stored (SYN-OLD's interval has ended, and it still counts)
    named = aw.source_ids(got.revisions)
    assert named == {k: s.source_ids for k, s in states.items() if s.source_ids}
    assert named["SYN-OLD"] == ("SYN-BRK",)


DERIVED = {"status", "cash_basis", "sizing_blocked", "data_state", "source_ids",
           "view", "health", "conflicts"}  # fmt: skip


def _keys(x: object) -> set[str]:
    if isinstance(x, dict):
        return set(x).union(*(_keys(v) for v in x.values()))
    return set().union(*(_keys(v) for v in x)) if isinstance(x, list) else set()


@pytest.mark.db
def test_no_account_is_presented_as_healthy(app_conn: Conn) -> None:
    """Gate (a) of T013: the store's `status` reads `active` for an account with no
    snapshot. The section carries no derived field at all, refuses one, and the
    imported map consolidates to `no_data` and sizing-blocked for every account."""
    a = seed(app_conn, "a")
    body = export(app_conn, a)[A]
    with tn.tenant_transaction(app_conn, a.id) as tx:
        states = st.read_accounts(tx, (), AT, timedelta(days=1))
    assert states["SYN-A"].status == "active" and states["SYN-A"].sizing_blocked
    assert not _keys(body) & DERIVED
    secretish = ("secret", "token", "credential", "password", "connection", "key")
    assert not [k for k in _keys(body) if any(w in k for w in secretish)]
    got = aw.parse_section(str(a.id), body)
    views = consolidate(got.source_map, str(a.id), (), AT, timedelta(1)).accounts
    assert {(v.status, v.sizing_blocked) for v in views.values()} == {
        ("no_data", True)}  # fmt: skip
    for k, v in (("status", "active"), ("cash_basis", "broker_available"),
                 ("sizing_blocked", False)):  # fmt: skip
        bad = copy.deepcopy(body)
        bad["accounts"][0][k] = v
        with pytest.raises(pt.PortabilityError) as e:
            pt.build_export("export-synth-1", str(a.id), AT, {A: bad})
        assert e.value.code is pt.Refusal.UNKNOWN_FIELD


def _reseal(row: dict[str, Any], content: str) -> None:
    row["content_hash"] = content
    row["row_hash"] = aw.seal(content, parse_instant(row["recorded_at"]))


def _account_digest(t: str, r: dict[str, Any]) -> str:  # without the domain check
    return digest(["account", t, r["account_id"], r["name"], r["broker"],
                   r["account_type"], r["reporting_currency"],
                   r["analysis_scope"]])  # fmt: skip


def _mapping_digest(t: str, r: dict[str, Any]) -> str:  # as revision_content
    return digest(["source_mapping_revision", t, r["source_id"], r["account_ref"],
                   r["revision"], r["intervals"], r["previous_hash"]])  # fmt: skip


@pytest.mark.db
def test_account_tampering_is_refused_on_export_and_import(
    app_conn: Conn, conn: Conn
) -> None:
    a, b = seed(app_conn, "a"), seed(app_conn, "b")
    t, R = str(a.id), pt.Refusal
    good, foreign = export(app_conn, a)[A], export(app_conn, b)[A]
    bundle = pt.build_export("export-synth-1", t, AT, {A: good})
    assert _reimport(bundle, good, A) is None
    acc = {r["account_id"]: n for n, r in enumerate(good["accounts"])}
    maps = {(r["source_id"], r["account_ref"], r["revision"]): n
            for n, r in enumerate(good["mappings"])}  # fmt: skip
    yahoo = [r["source_id"] for r in good["sources"]].index("SYN-YAHOO")

    def case(edit: Any, want: pt.Refusal | None) -> None:
        body = copy.deepcopy(good)
        edit(body)
        assert _reimport(bundle, body, A) is want  # import
        try:
            pt.build_export("export-synth-2", t, AT, {A: body})
            got = None
        except pt.PortabilityError as e:
            got = e.code
        assert got is want  # export

    def flip(body: dict[str, Any], reseal: bool) -> None:
        r = body["sources"][yahoo]
        r["kind"] = "broker_export"
        if reseal:
            s = Source(r["source_id"], t, SourceKind.BROKER_EXPORT)
            _reseal(r, aw.source_content(t, s, r["provider"]))

    case(lambda x: flip(x, False), R.ACCOUNT_SEAL)  # kind flip
    case(lambda x: flip(x, True), None)  # a full rewrite needs a signature (T060)

    def interval(body: dict[str, Any], key: tuple[str, str, int], ivs: Any,
                 reseal: bool) -> None:  # fmt: skip
        r = body["mappings"][maps[key]]
        r["intervals"] = ivs
        if reseal:
            _reseal(r, _mapping_digest(t, r))

    old = good["mappings"][maps[("SYN-BRK", "1002", 1)]]["intervals"]
    moved = [{**old[0], "valid_from": format_instant(V1)}]
    one = ("SYN-YAHOO", "pf-1", 1)
    v0, v1 = format_instant(V0), format_instant(V1)
    plus = v0.replace("Z", "+00:00")
    for key, ivs, reseal, want in (
        (("SYN-BRK", "1002", 1), moved, False, R.ACCOUNT_SEAL),  # interval edit
        (("SYN-BRK", "1002", 1), moved, True, R.ACCOUNT_CHAIN),  # revision 2 names it
        (one, [{"account_id": "SYN-A", "valid_from": v1, "valid_to": v0}], True,
         R.MAPPING_REPLAY),  # empty interval
        (one, [{"account_id": "SYN-A", "valid_from": v0, "valid_to": None},
               {"account_id": "SYN-B", "valid_from": v1, "valid_to": None}], True,
         R.MAPPING_REPLAY),  # overlap
        (one, [{"account_id": "SYN-NOPE", "valid_from": v0, "valid_to": None}],
         True, R.MAPPING_REPLAY),  # an account the section does not hold
        (one, [{"account_id": "SYN-A", "valid_from": plus, "valid_to": None}], True,
         R.MALFORMED),  # not canonical
        (one, [{**old[0], "note": "x"}], False, R.UNKNOWN_FIELD),
        (("SYN-BRK", "1002", 2), good["mappings"][maps[("SYN-BRK", "1002", 2)]][
            "intervals"][::-1], True, R.MAPPING_REPLAY),  # not in valid-time order
    ):  # fmt: skip
        case(lambda x, k=key, i=ivs, s=reseal: interval(x, k, i, s), want)

    def chain(body: dict[str, Any]) -> None:
        r = body["mappings"][maps[("SYN-BRK", "1002", 2)]]
        r["previous_hash"] = "0" * 64
        _reseal(r, _mapping_digest(t, r))

    case(chain, R.ACCOUNT_CHAIN)  # broken chain

    def bounds(body: dict[str, Any], revision: int, many: int) -> None:
        r = body["mappings"][maps[one]]  # the database's bounds, validly sealed
        r["revision"] = revision
        r["intervals"] = [
            {
                "account_id": "SYN-A",
                "valid_from": format_instant(V0 + n * DAY),
                "valid_to": format_instant(V0 + (n + 1) * DAY),
            }
            for n in range(many)
        ]
        _reseal(r, _mapping_digest(t, r))

    case(lambda x: bounds(x, 1, aw.MAX_INTERVALS + 1), R.MALFORMED)
    case(lambda x: bounds(x, aw.MAX_REVISION + 1, 1), R.MALFORMED)
    assert st.MAX_INTERVALS is aw.MAX_INTERVALS  # one definition
    with pytest.raises(pt.PortabilityError) as e:
        pt.check_section(A, good, None)
    assert e.value.code is R.SCOPE_MISMATCH
    case(lambda x: x["mappings"].pop(maps[("SYN-BRK", "1002", 1)]), R.ACCOUNT_CHAIN)

    def forged(body: dict[str, Any]) -> None:
        r = body["accounts"][acc["SYN-B"]]
        later = parse_instant(r["recorded_at"]) + timedelta(microseconds=1)
        r["row_hash"] = aw.seal(r["content_hash"], later)

    case(forged, R.ACCOUNT_SEAL)  # forged seal
    case(lambda x: x["accounts"][acc["SYN-B"]].update(content_hash="0" * 64),
         R.ACCOUNT_SEAL)  # the stored digest alone  # fmt: skip
    plus_at = good["sources"][yahoo]["recorded_at"].replace("Z", "+00:00")
    # the same instant, so the seal (over the canonical form) still matches
    case(lambda x: x["sources"][yahoo].update(recorded_at=plus_at), R.MALFORMED)

    def relinked(body: dict[str, Any]) -> None:
        r = body["account_revisions"][0]
        r["previous_hash"] = body["accounts"][acc["SYN-A"]]["row_hash"]
        _reseal(r, aw.lifecycle_content(t, "SYN-OLD", 2, "archived",
                                        r["previous_hash"]))  # fmt: skip

    case(relinked, R.ACCOUNT_CHAIN)  # an archive naming another account's row
    case(lambda x: x["accounts"][acc["SYN-B"]].update(
        recorded_at=format_instant(V0)), R.ACCOUNT_SEAL)  # backdated  # fmt: skip

    def decomposed(body: dict[str, Any]) -> None:
        r = body["accounts"][acc["SYN-A"]]
        r["broker"] = NFD
        _reseal(r, _account_digest(t, r))

    case(decomposed, R.ACCOUNT_TEXT)  # NFC-decomposed text, validly sealed

    def renamed(body: dict[str, Any], name: str) -> None:
        r = body["accounts"][acc["SYN-B"]]
        r["name"] = name
        _reseal(r, _account_digest(t, r))

    case(lambda x: renamed(x, ""), R.MALFORMED)  # validly sealed, 1 to 100
    case(lambda x: renamed(x, "x" * 101), R.MALFORMED)

    def before(body: dict[str, Any], name: str, n: int) -> None:
        r = body[name][n]  # known before the rows it depends on, validly sealed
        r["recorded_at"] = format_instant(V0)
        _reseal(r, r["content_hash"])

    case(lambda x: before(x, "mappings", maps[one]), R.MAPPING_REPLAY)
    case(lambda x: before(x, "account_revisions", 0), R.ACCOUNT_CHAIN)

    def lifecycle(body: dict[str, Any], life: str, rev: int) -> None:
        archive = body["account_revisions"][0]
        assert (archive["account_id"], archive["revision"]) == ("SYN-OLD", 2)
        r = archive
        if rev == 3:  # after the archive, chained to it
            r = {**archive, "revision": 3, "previous_hash": archive["row_hash"]}
            body["account_revisions"].append(r)
        r["lifecycle"] = life
        _reseal(r, aw.lifecycle_content(t, "SYN-OLD", rev, life,
                                        r["previous_hash"]))  # fmt: skip

    for life, rev in (("active", 3), ("archived", 3), ("active", 2)):
        case(lambda x, y=life, n=rev: lifecycle(x, y, n), R.ACCOUNT_LIFECYCLE)
    retagged = [{**r, "tenant_id": t} for r in foreign["sources"]]  # b's, as a's
    for edit, want in (
        (lambda x: x["accounts"][0].update(status="active"), R.UNKNOWN_FIELD),
        (lambda x: x.update(connections=[]), R.UNKNOWN_FIELD),
        (lambda x: x["sources"][0].pop("row_hash"), R.FIELD_MISSING),
        (lambda x: x["accounts"].reverse(), R.MALFORMED),  # not in key order
        (lambda x: x["mappings"][0].update(revision=True), R.MALFORMED),
        (lambda x: x.update(sources=foreign["sources"]), R.SCOPE_MISMATCH),
        (lambda x: x.update(sources=retagged), R.ACCOUNT_SEAL),
    ):
        case(edit, want)
    # the store refuses to export a tampered row (`load` re-verifies every seal)
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute("UPDATE app.data_source SET kind = 'broker_api' WHERE tenant_id = %s "
                 "AND source_id = 'SYN-YAHOO'", (a.id,))  # fmt: skip
    with pytest.raises(st.AccountStoreError, match="integrity"):
        export(app_conn, a)


# --- the `audit/tombstones.json` section (T057 increment 5): deletions appended
# as qw_worker with a SYNTHETIC key (decision 104), exported as qw_app, then
# re-verified without the key.
RAW = b"SYNTHETIC-test-record-key-0123456789"  # test only
KEY = rt.RecordKey(RAW)
SECRET = "SYNTHETIC-private-note-content"  # the deletion acknowledgement

R = pt.Refusal
IDS = ("acct-004217", "acct-000815")  # guessable, as decision 104 assumes
OTHER = rt.RecordKey(b"SYNTHETIC-another-installation-key-0123")
Body = dict[str, Any]


def clock(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return cast(datetime, row[0])


def deleted(worker_conn: Conn, t: uuid.UUID) -> tuple[rt.Plan, rt.Backup]:
    """A SYNTHETIC deletion of the two research records IDS (and a retained
    financial one), with one residual backup."""
    now = clock(worker_conn)
    bk = rt.backup("bk-synth-1", now - DAY * 2, now - DAY * 2)
    research, fin = rt.RetentionClass.RESEARCH, rt.RetentionClass.FINANCIAL
    items = [rt.Item(str(t), r, research, now - DAY * 9) for r in IDS]
    items.append(rt.Item(str(t), "rec-fin", fin, now - DAY * 9))
    req = rt.DeletionRequest("del-synth-1", str(t), "scope-1", frozenset(
        {research, fin}), now - timedelta(seconds=1), "reauth-1", SECRET)  # fmt: skip
    p = rt.plan(req, items, rt.owner_defaults(), rt.Holds(), KEY)
    for d in p.decisions:
        if d.action is rt.Action.DELETE:
            with tn.tenant_transaction(worker_conn, t) as tx:
                assert tb.append(tx, KEY, d, p, (bk,))[1]
    return p, bk


def load(app_conn: Conn, t: uuid.UUID) -> tb.StoredLedger:
    with tn.tenant_transaction(app_conn, t) as tx:
        return tb.load(tx)


def bundle_of(t: Tenant, sections: dict[str, Any]) -> pt.ExportBundle:
    stamps = [parse_instant(r["recorded_at"]) for r in sections[T]["tombstones"]]
    at = max([*stamps, *(parse_instant(r["recorded_at"])
                         for r in sections[pt.JOURNAL_PATH]["events"])])  # fmt: skip
    return pt.build_export("export-synth-1", str(t.id), at + timedelta(seconds=1),
                           sections)  # fmt: skip


def _tomb(r: Body) -> rt.Tombstone:  # unvalidated, to recompute a tampered hash
    exp = r["backup_expiry_at"]
    return rt.Tombstone(r["seq"], r["tenant_id"], r["record_hash"],
                        cast(Any, r["cls"]), r["job_id"],
                        parse_instant(r["requested_at"]),
                        parse_instant(r["deleted_at"]),
                        tuple(r["residual_backups"]),
                        None if exp is None else parse_instant(exp),
                        r["hash_scheme"], r["key_id"], r["prev_hash"],
                        "")  # fmt: skip


def fix(b: Body, rehash: bool = True, rechain: bool = False,
        heads: bool = True) -> Body:  # fmt: skip
    """Recomputes what a forger could: entry hashes (`rehash`), links (`rechain`),
    seals, checkpoint heads (`heads`) and the header."""
    rows, prev = b["tombstones"], rt.GENESIS
    for r in rows:
        r["prev_hash"] = prev if rechain else r["prev_hash"]
        r["entry_hash"] = rt.entry_hash(_tomb(r)) if rehash else r["entry_hash"]
        r["row_hash"] = tb.seal(r["entry_hash"], parse_instant(r["recorded_at"]))
        prev = r["entry_hash"]
    for c in b["checkpoints"]:
        if heads and 0 < c["length"] <= len(rows):
            c["head"] = rows[c["length"] - 1]["entry_hash"]
        c["row_hash"] = tb.seal(c["head"], parse_instant(c["recorded_at"]))
    b["ledger"] = tw.header(b["ledger"]["tenant_id"], rows)
    return b


def _set(path: str, value: object, **how: bool) -> Callable[[Body], Body]:
    def edit(b: Body) -> Body:
        *parents, leaf = path.split(".")
        obj: Any = b
        for p in parents:
            obj = obj[int(p)] if p.lstrip("-").isdigit() else obj[p]
        if value is KeyError:
            del obj[leaf]
        else:
            obj[leaf] = value(b) if callable(value) else value
        return fix(b, **how) if how else b

    return edit


def _drop(name: str, n: int, **how: bool) -> Callable[[Body], Body]:
    def edit(b: Body) -> Body:
        del b[name][n]
        return fix(b, **how)

    return edit


def _earlier(b: Body) -> str:
    at = parse_instant(b["tombstones"][0]["deleted_at"]) - timedelta(microseconds=1)
    return format_instant(at)


def _after(b: Body) -> str:
    at = parse_instant(b["tombstones"][1]["recorded_at"]) + timedelta(microseconds=1)
    return format_instant(at)


def _scheme(b: Body) -> Body:
    for r in b["tombstones"]:
        r["hash_scheme"] = "qw-record-hmac-9"
    return fix(b, rechain=True)


def _beyond(b: Body) -> Body:
    last = b["checkpoints"][-1]
    b["checkpoints"].append({**last, "length": last["length"] + 1})
    return fix(b, heads=False)


NO = {"rehash": False}
CASES: list[tuple[str, Callable[[Body], Body], R]] = [
    ("forged entry hash", lambda b: _set("tombstones.1.prev_hash", "ab" * 32, **NO)(
        _set("tombstones.0.entry_hash", "ab" * 32)(b)), R.TOMBSTONE_ENTRY),
    ("record hash, entry not rehashed", _set("tombstones.0.record_hash", "cd" * 32,
                                             **NO), R.TOMBSTONE_ENTRY),
    ("job id, entry not rehashed", _set("tombstones.0.job_id", "del-other", **NO),
     R.TOMBSTONE_ENTRY),
    ("broken link", _set("tombstones.1.prev_hash", "cd" * 32, rehash=True),
     R.TOMBSTONE_CHAIN),
    ("record hash rehashed, not relinked", _set("tombstones.0.record_hash",
                                                "cd" * 32, rehash=True),
     R.TOMBSTONE_CHAIN),
    ("renumbered", _set("tombstones.1.seq", 2, rehash=True), R.TOMBSTONE_CHAIN),
    ("first dropped", _drop("tombstones", 0), R.TOMBSTONE_CHAIN),
    ("deletion time backwards", _set("tombstones.1.deleted_at", _earlier,
                                     rechain=True), R.TOMBSTONE_CHAIN),
    ("checkpoint not the head", _set("checkpoints.0.head",
                                     lambda b: b["tombstones"][1]["entry_hash"],
                                     heads=False), R.TOMBSTONE_CHECKPOINT),
    ("latest checkpoint dropped", _drop("checkpoints", -1), R.TOMBSTONE_CHECKPOINT),
    ("checkpoint past the end", _beyond, R.TOMBSTONE_CHECKPOINT),
    ("checkpoint before its entry", _set("checkpoints.0.recorded_at",
                                         "2020-01-01T00:00:00Z", rechain=True),
     R.TOMBSTONE_CHECKPOINT),
    ("mixed key id", _set("tombstones.1.key_id", OTHER.key_id, rechain=True),
     R.TOMBSTONE_KEY),
    ("unknown scheme", _scheme, R.TOMBSTONE_SCHEME),
    ("seal", _set("tombstones.0.row_hash", "ef" * 32), R.TOMBSTONE_SEAL),
    ("stamp order", _set("tombstones.0.recorded_at", lambda b: format_instant(
        parse_instant(b["tombstones"][1]["recorded_at"]) + DAY), rechain=True),
     R.TOMBSTONE_SEAL),
    ("header head", _set("ledger.head", rt.GENESIS), R.TOMBSTONE_HEADER),
    ("header key", _set("ledger.key_id", OTHER.key_id), R.TOMBSTONE_HEADER),
    ("header length true for 1", lambda b: _set("ledger.length", True)(_drop(
        "checkpoints", -1)(_drop("tombstones", -1)(b))), R.TOMBSTONE_HEADER),
    ("extra tombstone key", _set("tombstones.0.record_id", IDS[0]), R.UNKNOWN_FIELD),
    ("extra header key", _set("ledger.key", RAW.hex()), R.UNKNOWN_FIELD),
    ("extra checkpoint key", _set("checkpoints.0.key_id", KEY.key_id),
     R.UNKNOWN_FIELD),
    ("extra section key", _set("key", RAW.hex()), R.UNKNOWN_FIELD),
    ("missing key", _set("tombstones.0.row_hash", KeyError), R.FIELD_MISSING),
    ("foreign row", _set("tombstones.0.tenant_id", str(uuid.uuid4())),
     R.SCOPE_MISMATCH),
    ("seq true", _set("tombstones.0.seq", True), R.MALFORMED),
    ("seq true for 1, recomputed", _set("tombstones.1.seq", True, rechain=True),
     R.MALFORMED),
    ("checkpoint length true for 1", _set("checkpoints.0.length", True),
     R.MALFORMED),
    ("record hash repeated", _set("tombstones.1.record_hash", lambda b: b[
        "tombstones"][0]["record_hash"], rechain=True), R.TOMBSTONE_DUPLICATE),
    ("deleted after recorded", _set("tombstones.1.deleted_at", _after,
                                    rechain=True), R.TOMBSTONE_TIME),
    ("privacy class", _set("tombstones.0.cls", "privacy", rechain=True),
     R.MALFORMED),
    ("backups unsorted", _set("tombstones.0.residual_backups", ["bk-z", "bk-a"],
                              rechain=True), R.MALFORMED),
    ("backups repeated", _set("tombstones.0.residual_backups", ["bk-a", "bk-a"],
                              rechain=True), R.MALFORMED),
    ("instant not canonical", _set("tombstones.0.deleted_at", lambda b: b[
        "tombstones"][0]["deleted_at"].replace("Z", "+00:00")), R.MALFORMED),
    ("key id not hex", _set("tombstones.0.key_id", "K" * 32), R.MALFORMED),
]  # fmt: skip


@pytest.mark.db
def test_round_trip_of_every_store_section(worker_conn: Conn, app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    deleted(worker_conn, a.id)
    sections = export(app_conn, a)
    assert set(sections) == {pt.JOURNAL_PATH, pt.POLICIES_PATH, pt.WATCHLISTS_PATH,
                             pt.ACCOUNTS_PATH, T}  # fmt: skip
    bundle = bundle_of(a, sections)
    report = read_archive(write_archive(bundle))
    assert report.data == sections and report.pending_reconnections == ()
    got = tw.parse_section(str(a.id), cast(Body, report.data[T]))  # no key
    stored = load(app_conn, a.id)
    assert (got.entries, got.checkpoint) == (stored.entries, stored.checkpoint)
    assert (got.hash_scheme, got.key_id) == (rt.HASH_SCHEME, KEY.key_id)
    assert sections[T]["ledger"] == {
        "tenant_id": str(a.id), "length": 2, "head": stored.checkpoint.head,
        "hash_scheme": rt.HASH_SCHEME, "key_id": KEY.key_id}  # fmt: skip
    assert {e.residual_backups for e in got.entries} == {("bk-synth-1",)}


@pytest.mark.db
def test_the_destination_needs_the_same_key(worker_conn: Conn,
                                            app_conn: Conn) -> None:  # fmt: skip
    a = seed(app_conn, "a")
    p, bk = deleted(worker_conn, a.id)
    body = read_archive(write_archive(bundle_of(a, export(app_conn, a)))).data[T]
    got = tw.parse_section(str(a.id), cast(Body, body))
    tw.check_key(got, KEY)
    for ledger, key, why in ((got, OTHER, "key_mismatch"),
                             (got, None, "key_unavailable"),
                             (cast(Body, body)["ledger"], KEY, "argument"),
                             ({}, KEY, "argument")):  # fmt: skip
        with pytest.raises(tw.WireError) as e:
            tw.check_key(cast(tw.Parsed, ledger), key)
        assert e.value.code == why  # detected up front, from the header
    created = p.decisions[0].created_at
    items = tuple(rt.Item(str(a.id), r, rt.RetentionClass.RESEARCH, created)
                  for r in (*IDS, "acct-live"))  # fmt: skip
    snap = rt.Snapshot(bk, items, ())
    at = clock(worker_conn)
    back = rt.restore(snap, got.entries, got.checkpoint, at, key=KEY)
    assert [i.record_id for i in back.items] == ["acct-live"]  # carried key
    with pytest.raises(rt.RetentionError) as err:  # never a silent miss
        rt.restore(snap, got.entries, got.checkpoint, at, key=OTHER)
    assert err.value.code == "key_mismatch"


@pytest.mark.db
def test_archive_tampering_is_refused(worker_conn: Conn, app_conn: Conn) -> None:
    a = seed(app_conn, "a")
    deleted(worker_conn, a.id)
    sections = export(app_conn, a)
    bundle = bundle_of(a, sections)
    at = bundle.manifest.created_at
    for name, edit, want in CASES:
        body = edit(copy.deepcopy(sections[T]))
        with pytest.raises(pt.PortabilityError) as e:
            pt.build_export("export-synth-1", str(a.id), at, {T: body})
        assert (name, e.value.code) == (name, want)
        assert (name, _reimport(bundle, body, T)) == (name, want)
    # integrity, not authenticity (T060): a full rewrite, or a dropped tail with
    # its checkpoint, verifies
    for edit in (
        _set("tombstones.0.record_hash", "cd" * 32, rechain=True),
        lambda b: _drop("checkpoints", -1)(_drop("tombstones", -1)(b)),
    ):
        assert _reimport(bundle, edit(copy.deepcopy(sections[T])), T) is None


@pytest.mark.db
def test_the_archive_holds_no_key_and_no_record_id(worker_conn: Conn,
                                                   app_conn: Conn) -> None:  # fmt: skip
    a = seed(app_conn, "a")
    p, _ = deleted(worker_conn, a.id)
    archive = write_archive(bundle_of(a, export(app_conn, a)))
    for secret in (RAW, RAW.hex().encode(), base64.b64encode(RAW), SECRET.encode(),
                   *(i.encode() for i in IDS), b"scope-1", b"reauth-1"):  # fmt: skip
        assert secret not in archive
    body = cast(Body, read_archive(archive).data[T])
    names = {r["record_hash"] for r in body["tombstones"]}
    created, tenant = format_instant(p.decisions[0].created_at), str(a.id)

    def guesses(rid: str) -> list[str]:  # what a reader without the key can try
        forms = ([tenant, "research", rid], [rt.HASH_SCHEME, tenant, "research", rid,
                                             created])  # fmt: skip
        texts = [json.dumps(f, separators=(",", ":")).encode() for f in forms]
        return [*(hashlib.sha256(x).hexdigest() for x in texts),
                *(OTHER.mac(x) for x in texts),
                *(hmac.new(b"", x, "sha256").hexdigest() for x in texts)]  # fmt: skip

    candidates = [f"acct-{n:06d}" for n in range(20000)]
    assert IDS[0] in candidates and IDS[1] in candidates
    assert not [c for c in candidates if names & set(guesses(c))]  # nothing
    with_key = {rt.record_hash(KEY, tenant, rt.RetentionClass.RESEARCH, c,
                               p.decisions[0].created_at): c
                for c in candidates}  # fmt: skip
    assert sorted(with_key[n] for n in names) == sorted(IDS)  # the key recovers


@pytest.mark.db
def test_tenants_are_isolated(worker_conn: Conn, app_conn: Conn) -> None:
    a, b, c = seed(app_conn, "a"), seed(app_conn, "b"), seed(app_conn, "c")
    deleted(worker_conn, a.id)
    deleted(worker_conn, b.id)  # the same record ids in both tenants
    sa, sb, sc = (export(app_conn, t)[T] for t in (a, b, c))
    assert sc == {"ledger": tw.header(str(c.id), []), "tombstones": [],
                  "checkpoints": []}  # fmt: skip
    tw.check_key(tw.parse_section(str(c.id), sc), None)  # nothing to match
    assert not {r["record_hash"] for r in sa["tombstones"]} & {
        r["record_hash"] for r in sb["tombstones"]}  # fmt: skip
    text = pt.canonical(sb).decode()
    for r in sa["tombstones"]:
        assert r["record_hash"] not in text and r["entry_hash"] not in text
    assert str(a.id) not in text
    at = clock(worker_conn) + timedelta(seconds=1)
    for body, scope, want in (
        (sa, str(b.id), R.SCOPE_MISMATCH),  # a's section under b's scope
        (sc, str(b.id), R.SCOPE_MISMATCH),  # c's empty one
        (_retag(sa, b.id, False), str(b.id), R.TOMBSTONE_ENTRY),  # entries bind a
        (sb, None, R.SCOPE_MISMATCH),  # the entry hashes need the tenant
    ):  # fmt: skip
        with pytest.raises(pt.PortabilityError) as e:
            pt.check_section(T, body, scope)
        assert e.value.code is want
    # integrity, not authenticity: a full retag verifies, but its pseudonyms are
    # keyed by a's tenant, so they match none of b's records
    # parse_section checks the scope itself, even when every hash is recomputed
    retagged = _retag(sa, b.id, True)
    only_cp = copy.deepcopy(sa)
    only_cp["checkpoints"][0]["tenant_id"] = str(b.id)
    for body in (retagged, only_cp, {"tombstones": None}):
        with pytest.raises(tw.WireError) as w:
            tw.parse_section(str(a.id), cast(Body, body))
        assert w.value.code == ("malformed" if body.get("tombstones") is None
                                else "scope")  # fmt: skip
    forged = _retag(sa, b.id, True)
    pt.build_export("export-synth-1", str(b.id), at, {T: forged})
    assert not {r["record_hash"] for r in forged["tombstones"]} & {
        r["record_hash"] for r in sb["tombstones"]}  # fmt: skip


def _retag(body: Body, tenant: uuid.UUID, rehash: bool) -> Body:
    b = copy.deepcopy(body)
    for r in (b["ledger"], *b["tombstones"], *b["checkpoints"]):
        r["tenant_id"] = str(tenant)
    return fix(b, rehash=rehash, rechain=rehash)
