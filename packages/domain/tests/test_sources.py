"""Source mapping, overlap dedupe and conflicts (T013 PR A).

SYNTHETIC inputs only: docs/spec/tests/fixtures/overlapping_holdings.csv (expected
consolidated 100, not 150 or 50, per the fixture README and T008 F-04) and hand-built
snapshots. Expected numbers are hand-computed in each test.
"""

import csv
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.decimals import Money
from qw_domain.decimals import Quantity as Q
from qw_domain.identity import InstrumentId
from qw_domain.instants import InstantError
from qw_domain.sources import (
    HoldingLine,
    MappedInterval,
    ScopeError,
    Source,
    SourceKind,
    SourceMap,
    SourceSnapshot,
    UnderlyingAccount,
    consolidate,
)

PROFILE = settings(derandomize=True, database=None, max_examples=200)
FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
SYNTH = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
SYNTH2 = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a2"))
SYMBOLS = {"SYNTH": SYNTH}  # identity resolution is T011; the fixture symbol is fixed
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
AS_OF = T0 + timedelta(hours=1)
FRESH = timedelta(days=1)
TENANT = "SYN-TENANT"


def usd(v: str | int) -> Money:
    return Money.of(v, "USD")


def acct(account_id: str, tenant: str = TENANT) -> UnderlyingAccount:
    return UnderlyingAccount(account_id, tenant, "SYN-INSTITUTION", "margin", "USD")


def snap(
    source: str,
    ref: str,
    lines: dict[InstrumentId, str],
    cash: tuple[Money, ...] = (),
    *,
    at: datetime = T0,
    complete: bool = True,
) -> SourceSnapshot:
    holdings = tuple(HoldingLine(i, "USD", Q(q)) for i, q in lines.items())
    return SourceSnapshot(source, ref, at, holdings, cash, complete)


def registry() -> SourceMap:
    """SYN-WS via its broker and a Yahoo-style mirror; SYN-OTHER via a second broker."""
    m = SourceMap()
    m.add_account(acct("SYN-WS"))
    m.add_account(acct("SYN-OTHER"))
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    m.add_source(Source("SYN-YAHOO-MIRROR", TENANT, SourceKind.MIRROR))
    m.add_source(Source("SYN-BROKER-2", TENANT, SourceKind.BROKER_API))
    start = T0 - timedelta(days=30)
    for source, ref in (
        ("SYN-BROKER", "SYN-WS"),
        ("SYN-YAHOO-MIRROR", "SYN-WS"),
        ("SYN-BROKER-2", "SYN-OTHER"),
    ):
        m.remap(source, ref, (MappedInterval(ref, start),), start)
    return m


def fixture_snapshots() -> list[SourceSnapshot]:
    rows = list(csv.DictReader((FIXTURES / "overlapping_holdings.csv").open()))
    assert len(rows) == 3
    return [
        SourceSnapshot(
            r["source"],
            r["underlying_account"],
            T0,
            (HoldingLine(SYMBOLS[r["symbol"]], r["currency"], Q(r["quantity"])),),
            (),
            # Each broker row is the whole synthetic account; the mirror is a subset.
            complete=r["source"] != "SYN-YAHOO-MIRROR",
        )
        for r in rows
    ]


# ---- Overlap fixture (AT058, F-04)


def test_overlap_fixture_consolidates_to_100() -> None:
    m = registry()
    c = consolidate(m, TENANT, fixture_snapshots(), AS_OF, FRESH)
    ws, other = c.accounts["SYN-WS"], c.accounts["SYN-OTHER"]
    assert ws.units == {(SYNTH, "USD"): Q(50)}  # shown twice, counted once
    assert other.units == {(SYNTH, "USD"): Q(50)}
    assert ws.status == other.status == "reconciled"
    assert ws.sources == ("SYN-BROKER", "SYN-YAHOO-MIRROR")
    assert ws.conflicts == () and c.quarantined == ()
    total = sum((v.units[(SYNTH, "USD")] for v in c.accounts.values()), Q(0))
    assert total == Q(100) and total not in (Q(150), Q(50))


def test_genuine_accounts_with_identical_holdings_stay_distinct() -> None:
    m = SourceMap()
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    for a in ("SYN-A", "SYN-B"):
        m.add_account(acct(a))
        m.remap("SYN-BROKER", f"ref-{a}", (MappedInterval(a, T0 - FRESH),), T0)
    # Same source, same instrument, same quantity, same time: two real accounts.
    snaps = [snap("SYN-BROKER", f"ref-{a}", {SYNTH: "50"}) for a in ("SYN-A", "SYN-B")]
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    assert (
        c.accounts["SYN-A"].units
        == c.accounts["SYN-B"].units
        == {(SYNTH, "USD"): Q(50)}
    )


# ---- Conflicts (spec §4 "Idempotency and conflict resolution")


def test_mirror_disagreement_is_an_explicit_conflict_that_blocks_sizing() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}, (usd(1000),)),
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "40"}, complete=False),
        snap("SYN-BROKER-2", "SYN-OTHER", {SYNTH: "50"}, (usd(700),)),
    ]
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    ws = c.accounts["SYN-WS"]
    assert ws.status == "conflicted" and ws.sizing_blocked
    # The broker-authoritative figure is shown, never 45 (average) or 90 (sum) ...
    assert ws.units == {(SYNTH, "USD"): Q(50)}
    # ... and the disagreement is recorded with every candidate.
    (conflict,) = ws.conflicts
    assert conflict.item == (SYNTH, "USD") and conflict.selected == "SYN-BROKER"
    assert {(x.source_id, x.value) for x in conflict.candidates} == {
        ("SYN-BROKER", Decimal(50)),
        ("SYN-YAHOO-MIRROR", Decimal(40)),
    }
    # The unaffected account continues.
    other = c.accounts["SYN-OTHER"]
    assert other.status == "reconciled" and other.cash == {"USD": usd(700)}


def test_only_a_complete_snapshot_reports_omissions_as_zero() -> None:
    m = registry()
    broker = snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"})
    extra = snap(
        "SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50", SYNTH2: "5"}, complete=False
    )
    ws = consolidate(m, TENANT, [broker, extra], AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "conflicted" and ws.units == {(SYNTH, "USD"): Q(50)}
    assert [x.item for x in ws.conflicts] == [(SYNTH2, "USD")]
    # An incomplete mirror's silence about SYNTH2 is not a zero.
    broker = snap("SYN-BROKER", "SYN-WS", {SYNTH: "50", SYNTH2: "5"})
    subset = snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, complete=False)
    ws = consolidate(m, TENANT, [broker, subset], AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "reconciled"
    assert ws.units == {(SYNTH, "USD"): Q(50), (SYNTH2, "USD"): Q(5)}


def test_stale_authoritative_source_is_never_silently_downgraded() -> None:
    m = registry()
    old = T0 - timedelta(days=3)
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "70"}, at=old),  # stale, disagrees
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, (usd(10),), complete=False),
        snap("SYN-BROKER-2", "SYN-OTHER", {SYNTH: "50"}, at=old),
    ]
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    ws = c.accounts["SYN-WS"]
    assert ws.status == "conflicted" and ws.sizing_blocked
    # The stale complete broker snapshot also said 0 cash against the mirror's 10.
    assert [x.item for x in ws.conflicts] == [(None, "USD"), (SYNTH, "USD")]
    conflict = ws.conflicts[1]
    assert conflict.selected == "SYN-YAHOO-MIRROR"
    assert [(x.source_id, x.value, x.stale) for x in conflict.candidates] == [
        ("SYN-BROKER", Decimal(70), True),
        ("SYN-YAHOO-MIRROR", Decimal(50), False),
    ]
    # A mirror's cash is user-reported: shown, never claimed as buying power.
    assert ws.cash == {"USD": usd(10)} and ws.cash_basis == {"USD": "user_reported"}
    other = c.accounts["SYN-OTHER"]
    assert other.status == "stale" and other.sizing_blocked


def test_incomplete_mirror_alone_is_not_reconciled() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}, at=T0 - timedelta(days=3)),
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, complete=False),
        snap("SYN-BROKER-2", "SYN-OTHER", {SYNTH: "50"}),
    ]
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    ws = c.accounts["SYN-WS"]  # agrees with the stale broker; proves no completeness
    assert ws.status == "incomplete" and ws.sizing_blocked and ws.conflicts == ()


def test_latest_snapshot_per_source_reference_up_to_as_of_is_used() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "40"}, at=T0 - timedelta(hours=2)),
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}),
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "60"}, at=AS_OF + timedelta(1)),  # later
    ]
    ws = consolidate(m, TENANT, snaps, AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "reconciled" and ws.units == {(SYNTH, "USD"): Q(50)}


def test_two_references_to_one_account_each_keep_their_latest() -> None:
    m = registry()
    m.remap("SYN-BROKER", "SYN-WS-LOGIN-2", (MappedInterval("SYN-WS", T0 - FRESH),), T0)
    snaps = [  # an older report under a second reference still votes, so it conflicts
        snap("SYN-BROKER", "SYN-WS-LOGIN-2", {SYNTH: "40"}, at=T0 - timedelta(hours=1)),
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}),
    ]
    ws = consolidate(m, TENANT, snaps, AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "conflicted" and ws.units == {(SYNTH, "USD"): Q(50)}


# ---- Mapping: quarantine, intervals, versions, tenants (R058, R081)


def test_unmapped_source_reference_is_quarantined_not_guessed() -> None:
    m = registry()
    stray = snap("SYN-BROKER", "SYN-UNKNOWN-REF", {SYNTH: "50"})
    unknown = snap("SYN-NOT-REGISTERED", "SYN-WS", {SYNTH: "50"})
    c = consolidate(m, TENANT, [stray, unknown], AS_OF, FRESH)
    assert [(q.snapshot, q.reason) for q in c.quarantined] == [
        (stray, "unmapped"),
        (unknown, "unknown_source"),
    ]
    assert all(v.status == "no_data" and not v.units for v in c.accounts.values())


def test_expired_mapping_interval_quarantines_later_snapshots() -> None:
    m = SourceMap()
    m.add_account(acct("SYN-WS"))
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    end = T0 - timedelta(hours=1)  # half-open: covers [T0-10d, T0-1h)
    m.remap(
        "SYN-BROKER", "SYN-WS", (MappedInterval("SYN-WS", T0 - timedelta(10), end),), T0
    )
    assert m.resolve("SYN-BROKER", "SYN-WS", end - timedelta(microseconds=1)) == acct(
        "SYN-WS"
    )
    assert m.resolve("SYN-BROKER", "SYN-WS", end) is None
    c = consolidate(
        m, TENANT, [snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"})], AS_OF, FRESH
    )
    assert [q.reason for q in c.quarantined] == ["unmapped"]
    assert c.accounts["SYN-WS"].status == "no_data"


def test_remap_is_versioned_and_never_rewrites_history() -> None:
    m = registry()
    m.add_account(acct("SYN-NEW"))
    switch = T0 - timedelta(hours=6)
    first = m.revisions("SYN-YAHOO-MIRROR", "SYN-WS")
    m.remap(
        "SYN-YAHOO-MIRROR",
        "SYN-WS",
        (
            MappedInterval("SYN-WS", T0 - timedelta(30), switch),
            MappedInterval("SYN-NEW", switch),
        ),
        AS_OF,
    )
    revs = m.revisions("SYN-YAHOO-MIRROR", "SYN-WS")
    assert revs[:1] == first and [r.revision for r in revs] == [1, 2]
    assert m.resolve("SYN-YAHOO-MIRROR", "SYN-WS", T0) == acct("SYN-NEW")
    # Replay with the knowledge held before the remap.
    assert m.resolve("SYN-YAHOO-MIRROR", "SYN-WS", T0, known_as_of=T0) == acct("SYN-WS")
    mirror = snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, complete=False)
    now = consolidate(m, TENANT, [mirror], AS_OF, FRESH)
    then = consolidate(m, TENANT, [mirror], AS_OF, FRESH, known_as_of=T0)
    assert now.accounts["SYN-NEW"].units == then.accounts["SYN-WS"].units
    assert now.accounts["SYN-WS"].units == {} == then.accounts["SYN-NEW"].units


@pytest.mark.parametrize(
    ("intervals", "code"),
    [
        (
            (MappedInterval("SYN-WS", T0), MappedInterval("SYN-OTHER", T0 + FRESH)),
            "overlap",
        ),
        ((MappedInterval("SYN-MISSING", T0),), "unknown_account"),
        ((MappedInterval("SYN-FOREIGN", T0),), "tenant"),
        ((), "empty"),
    ],
)
def test_remap_rejections(intervals: tuple[MappedInterval, ...], code: str) -> None:
    m = registry()
    m.add_account(acct("SYN-FOREIGN", tenant="SYN-OTHER-TENANT"))
    before = m.revisions("SYN-BROKER", "SYN-WS")
    with pytest.raises(ScopeError) as err:
        m.remap("SYN-BROKER", "SYN-WS", intervals, AS_OF)
    assert err.value.code == code
    assert m.revisions("SYN-BROKER", "SYN-WS") == before


def test_registry_rejections() -> None:
    m = registry()
    with pytest.raises(ScopeError, match="recorded_at"):  # registry() recorded T0-30d
        m.remap(
            "SYN-BROKER", "SYN-WS", (MappedInterval("SYN-WS", T0),), T0 - timedelta(31)
        )
    with pytest.raises(ScopeError, match="unknown_source"):
        m.remap("SYN-NOPE", "x", (MappedInterval("SYN-WS", T0),), AS_OF)
    with pytest.raises(ScopeError, match="duplicate"):
        m.add_account(UnderlyingAccount("SYN-WS", TENANT, "OTHER", "cash", "CAD"))
    with pytest.raises(ScopeError, match="interval"):
        MappedInterval("SYN-WS", T0, T0)
    with pytest.raises(ScopeError, match="duplicate_line"):
        snap("SYN-BROKER", "SYN-WS", {}, (usd(1), usd(2)))
    with pytest.raises(InstantError):
        MappedInterval("SYN-WS", T0.replace(tzinfo=None))


def test_foreign_tenant_snapshot_is_refused() -> None:
    m = registry()
    m.add_account(acct("SYN-X", tenant="SYN-T2"))
    m.add_source(Source("SYN-T2-BROKER", "SYN-T2", SourceKind.BROKER_API))
    m.remap("SYN-T2-BROKER", "SYN-X", (MappedInterval("SYN-X", T0 - FRESH),), AS_OF)
    foreign = snap("SYN-T2-BROKER", "SYN-X", {SYNTH: "9"})
    with pytest.raises(ScopeError, match="foreign_tenant"):
        consolidate(m, TENANT, [foreign], AS_OF, FRESH)
    assert "SYN-X" not in consolidate(m, TENANT, [], AS_OF, FRESH).accounts


# ---- Property: extra sources never add; disagreement and gaps never pass (F-04)

_qty = st.integers(0, 10**6).map(lambda n: Q(Decimal(n).scaleb(-2)))
_lines = st.dictionaries(st.sampled_from([SYNTH, SYNTH2]), _qty, min_size=1)
_mirror = st.tuples(
    st.booleans(), st.booleans(), st.integers(0, 5)
)  # complete, off, lag
_account = st.tuples(
    _lines, st.sampled_from(["fresh", "stale", "absent"]), st.lists(_mirror, max_size=3)
)


@PROFILE
@given(st.lists(_account, min_size=1, max_size=3))
def test_mirrors_dedupe_and_never_hide_gaps(
    accounts: list[tuple[dict[InstrumentId, Q], str, list[tuple[bool, bool, int]]]],
) -> None:
    m = SourceMap()
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    snaps, expected = [], {}
    for n, (lines, broker, mirrors) in enumerate(accounts):
        a = f"SYN-{n}"
        m.add_account(acct(a))
        m.remap("SYN-BROKER", f"r{n}", (MappedInterval(a, T0 - timedelta(9)),), T0)
        held = tuple(HoldingLine(i, "USD", q) for i, q in lines.items())
        if broker != "absent":
            at = T0 if broker == "fresh" else T0 - timedelta(days=3)
            snaps.append(SourceSnapshot("SYN-BROKER", f"r{n}", at, held, (), True))
        first = held[0]
        changed = (replace(first, quantity=first.quantity + Q(1)), *held[1:])
        for k, (complete, off, lag) in enumerate(mirrors):
            src = f"SYN-MIRROR-{n}-{k}"
            m.add_source(Source(src, TENANT, SourceKind.MIRROR))
            m.remap(src, "ref", (MappedInterval(a, T0 - FRESH),), T0)
            at = T0 - timedelta(minutes=lag)
            report = changed if off else held
            snaps.append(SourceSnapshot(src, "ref", at, report, (), complete))
        # Independent oracle of the expected status.
        offs = [off for _, off, _ in mirrors]
        if broker == "fresh":
            status = "conflicted" if any(offs) else "reconciled"
        elif not mirrors:
            status = "stale" if broker == "stale" else "no_data"
        elif any(offs) and (broker == "stale" or not all(offs)):
            status = "conflicted"
        else:
            status = "reconciled" if any(c for c, _, _ in mirrors) else "incomplete"
        known = broker == "fresh" or (bool(mirrors) and not any(offs))
        expected[a] = (
            status,
            known,
            {(i, "USD"): q for i, q in lines.items() if q.value},
        )
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    for a, (status, known, units) in expected.items():
        got = c.accounts[a]
        assert got.status == status and got.sizing_blocked == (status != "reconciled")
        assert bool(got.conflicts) == (status == "conflicted")
        if known:
            assert got.units == units  # a mirror never adds to or replaces the broker
