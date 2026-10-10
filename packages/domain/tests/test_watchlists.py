"""Watchlist CRUD, versions, tenancy, audit, deletion isolation and stream demand
(T024; R014, R057, spec §2 Journey C, §13). SYNTHETIC tenants, accounts, instruments.
"""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from qw_domain import watchlists
from qw_domain.decimals import Money, PositiveQuantity, Price
from qw_domain.journal import Journal, materialize
from qw_domain.monitoring import Condition
from qw_domain.postings import buy, deposit
from qw_domain.streams import DemandClass, FeedLatency, plan_streams
from qw_domain.watchlists import Entry, WatchlistError, WatchlistStore, Who
from test_journal import SYN, SYN2, hdr
from test_streams import AT, EXCH, IEX, feed, inst, rights

T, OTHER = "tenant-synth-a", "tenant-synth-b"
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
A, B = Who(T, "user-a", NOW), Who(OTHER, "user-b", NOW)
COND = Condition("price_cross", "gt", ("100",), IEX, "SYNTHETIC breakout")


def entry(iid: object = SYN, priority: int = 50, **kw: object) -> Entry:
    args: dict[str, object] = {
        "instrument_id": iid,
        "thesis": "SYNTHETIC thesis",
        "tags": ("syn",),
        "review_at": NOW + timedelta(days=30),
        "priority": priority,
        "conditions": (COND,),
        "invalidation_text": "SYNTHETIC: thesis invalid below 80",
    }
    args.update(kw)
    return Entry(**args)  # type: ignore[arg-type]


def store_with_list() -> WatchlistStore:
    s = WatchlistStore()
    s.create(A, "wl-1", "Growth", "SYNTHETIC")
    return s


def test_crud_revisions_and_audit_trail() -> None:
    s = store_with_list()
    assert s.get(T, "wl-1").revision == 1
    s.rename(A, "wl-1", 1, "Growth ideas", "renamed")
    s.add_entry(A, "wl-1", 2, entry())
    s.update_entry(A, "wl-1", 3, entry(thesis="revised"))
    s.add_entry(A, "wl-1", 4, entry(SYN2, 10))
    s.remove_entry(A, "wl-1", 5, SYN2)
    wl = s.get(T, "wl-1")
    assert (wl.revision, wl.name, wl.description) == (6, "Growth ideas", "renamed")
    assert [e.thesis for e in wl.entries] == ["revised"]
    assert wl.entries[0].review_at == NOW + timedelta(days=30)
    s.archive(A, "wl-1", 6)
    with pytest.raises(WatchlistError, match="archived"):
        s.add_entry(A, "wl-1", 7, entry(SYN2))
    s.delete(A, "wl-1", 7)
    with pytest.raises(WatchlistError, match="not_found"):
        s.get(T, "wl-1")
    actions = [(a.action, a.revision) for a in s.audit(T)]
    assert actions == [
        ("create", 1), ("rename", 2), ("add_entry", 3), ("update_entry", 4),
        ("add_entry", 5), ("remove_entry", 6), ("archive", 7), ("delete", 7),
    ]  # fmt: skip
    assert all(a.who == A for a in s.audit(T))
    with pytest.raises(WatchlistError, match="id_used"):  # a deleted id is not reused
        s.create(A, "wl-1", "Again", "")


def test_stale_expected_revision_is_a_conflict_and_changes_nothing() -> None:
    s = store_with_list()
    s.rename(A, "wl-1", 1, "First", "")
    other_user = Who(T, "user-c", NOW)
    for stale in (1, 3):
        with pytest.raises(WatchlistError, match="version_conflict"):
            s.rename(other_user, "wl-1", stale, "Second", "")
        with pytest.raises(WatchlistError, match="version_conflict"):
            s.delete(other_user, "wl-1", stale)
    assert (s.get(T, "wl-1").name, s.get(T, "wl-1").revision) == ("First", 2)
    assert len(s.audit(T)) == 2
    s.add_entry(A, "wl-1", 2, entry())
    with pytest.raises(WatchlistError, match="entry_exists"):
        s.add_entry(A, "wl-1", 3, entry())
    with pytest.raises(WatchlistError, match="entry_missing"):
        s.remove_entry(A, "wl-1", 3, SYN2)


def test_tenant_isolation() -> None:
    s = store_with_list()
    s.create(B, "wl-1", "Other tenant", "")  # same id, own tenant
    s.add_entry(A, "wl-1", 1, entry())
    assert s.get(OTHER, "wl-1").entries == ()
    c = Who("tenant-synth-c", "user-c", NOW)
    for call in (
        lambda: s.get(c.tenant_id, "wl-1"),
        lambda: s.rename(c, "wl-1", 2, "steal", ""),
        lambda: s.delete(c, "wl-1", 2),
    ):
        with pytest.raises(WatchlistError, match="not_found"):
            call()
    assert [w.name for w in s.list(OTHER)] == ["Other tenant"]
    assert {a.who.tenant_id for a in s.audit(T)} == {T}
    assert s.watch_demands(OTHER) == ()


def test_deleting_entries_and_watchlists_leaves_holdings_and_journal_unchanged() -> (
    None
):
    journal = Journal()
    journal.post(deposit(hdr("D1"), Money.of("1000", "USD")), NOW)
    zero = Money.of(0, "USD")
    journal.post(buy(hdr("B1"), SYN, PositiveQuantity(5), Price(100), zero), NOW)
    before_entries = journal.entries()
    before = materialize(e.event for e in before_entries)
    revision = journal.revision("SYN-ACCOUNT")
    s = store_with_list()
    s.add_entry(A, "wl-1", 1, entry(SYN))
    s.remove_entry(A, "wl-1", 2, SYN)
    s.add_entry(A, "wl-1", 3, entry(SYN))
    s.delete(A, "wl-1", 4)
    assert journal.entries() == before_entries
    assert journal.revision("SYN-ACCOUNT") == revision
    after = materialize(e.event for e in journal.entries())
    assert after == before
    assert after.holding("SYN-ACCOUNT", SYN).quantity.value == 5
    # Structural: neither watchlists.py nor anything it imports, transitively,
    # imports the journal or postings.
    closure = qw_imports("qw_domain.watchlists")
    assert {"qw_domain.monitoring", "qw_domain.streams"} <= closure
    assert not closure & {"qw_domain.journal", "qw_domain.postings"}


def qw_imports(root: str) -> set[str]:
    """Transitive closure of qw_domain modules imported (`import` and `from`)."""
    src, seen, todo = Path(watchlists.__file__).parent, set(), [root]
    while todo:
        tree = ast.parse((src / f"{todo.pop().split('.')[-1]}.py").read_text())
        for n in ast.walk(tree):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            names += [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
            for m in names:
                if m.startswith("qw_domain.") and m not in seen:
                    seen.add(m)
                    todo.append(m)
    return seen


def test_entry_and_request_validation() -> None:
    for bad in (
        {"priority": 101},
        {"priority": -1},
        {"review_at": datetime(2026, 10, 8)},  # noqa: DTZ001 (naive)
        {"tags": ["list"]},
        {"thesis": 3},
    ):
        with pytest.raises((WatchlistError, TypeError, ValueError)):
            entry(**bad)
    with pytest.raises(WatchlistError):
        entry("not-an-instrument")
    with pytest.raises(WatchlistError):
        WatchlistStore().create(A, "bad id!", "n", "")
    with pytest.raises(WatchlistError):
        Who(T, "bad actor!", NOW)


def test_watch_demands_feed_the_stream_plan_by_user_priority() -> None:
    s = store_with_list()
    s.create(A, "wl-2", "Second", "")
    s.add_entry(A, "wl-1", 1, entry(inst(1), 90))
    s.add_entry(A, "wl-1", 2, entry(inst(2), 0))  # unprioritized: candidate
    s.add_entry(A, "wl-1", 3, entry(inst(3), 70))
    s.add_entry(A, "wl-2", 1, entry(inst(3), 40))  # the maximum priority wins
    s.add_entry(A, "wl-2", 2, entry(inst(4), 20, needs=FeedLatency.DELAYED))
    s.create(A, "wl-3", "Archived", "")
    s.add_entry(A, "wl-3", 1, entry(inst(5), 100))
    s.archive(A, "wl-3", 2)
    got = [(d.instrument_id, d.kind, d.rank, d.needs) for d in s.watch_demands(T)]
    U, C = DemandClass.USER_PRIORITY, DemandClass.CANDIDATE
    assert got == [
        (inst(1), U, 10, EXCH), (inst(3), U, 30, EXCH),
        (inst(4), U, 80, FeedLatency.DELAYED), (inst(2), C, 0, EXCH),
    ]  # fmt: skip
    plan = plan_streams(s.watch_demands(T), [feed(cap=2)], rights(), AT)
    assert {x.instrument_id for x in plan.subscriptions} == {inst(1), inst(3)}
    assert plan.scheduled_discovery == (inst(2), inst(4))
    assert {u.reasons for u in plan.uncovered} == {((IEX, "capacity_exhausted"),)}
