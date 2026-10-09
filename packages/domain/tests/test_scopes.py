"""Source mapping, overlap dedupe, conflicts and portfolio scopes (T013).

SYNTHETIC inputs only: docs/spec/tests/fixtures/overlapping_holdings.csv (expected
consolidated 100, not 150 or 50, per the fixture README and T008 F-04) and hand-built
snapshots. Expected numbers are hand-computed in each test.
"""

import csv
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
from qw_domain.scopes import (
    HoldingLine,
    MappedInterval,
    OutsideContext,
    PortfolioScope,
    ScopeError,
    ScopeKind,
    Sleeve,
    Source,
    SourceKind,
    SourceMap,
    SourceSnapshot,
    UnderlyingAccount,
    apply_scope,
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
            complete=False,  # the fixture lists one symbol, not whole accounts
        )
        for r in rows
    ]


def everything(m: SourceMap) -> PortfolioScope:
    return PortfolioScope(ScopeKind.ALL_DISCLOSED, frozenset(), OutsideContext.NONE)


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
    view = apply_scope(everything(m), c)
    assert view.exposure == {(SYNTH, "USD"): Q(100)}
    assert view.exposure[(SYNTH, "USD")] not in (Q(150), Q(50))


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
    assert apply_scope(everything(m), c).exposure == {(SYNTH, "USD"): Q(100)}


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
    assert c.accounts["SYN-OTHER"].status == "reconciled"
    view = apply_scope(everything(m), c)
    assert view.spendable_cash("SYN-WS", "USD") is None
    assert view.spendable_cash("SYN-OTHER", "USD") == usd(700)
    assert view.analysis_scope == "partial_declared"
    assert "SYN-WS: conflicted" in view.gaps


def test_complete_broker_snapshot_implies_zero_against_a_mirror_extra() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}),
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50", SYNTH2: "5"}, complete=False),
    ]
    ws = consolidate(m, TENANT, snaps, AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "conflicted"
    assert ws.units == {(SYNTH, "USD"): Q(50)}
    assert [x.item for x in ws.conflicts] == [(SYNTH2, "USD")]


def test_incomplete_mirror_silence_is_not_zero() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50", SYNTH2: "5"}),
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, complete=False),
    ]
    ws = consolidate(m, TENANT, snaps, AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "reconciled"
    assert ws.units == {(SYNTH, "USD"): Q(50), (SYNTH2, "USD"): Q(5)}


def test_stale_sources_do_not_vote_and_stale_only_blocks() -> None:
    m = registry()
    old = T0 - timedelta(days=3)
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "70"}, at=old),  # stale, disagrees
        snap("SYN-YAHOO-MIRROR", "SYN-WS", {SYNTH: "50"}, (usd(10),), complete=False),
        snap("SYN-BROKER-2", "SYN-OTHER", {SYNTH: "50"}, at=old),
    ]
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    ws = c.accounts["SYN-WS"]
    assert ws.status == "reconciled" and ws.units == {(SYNTH, "USD"): Q(50)}
    assert ws.sources == ("SYN-YAHOO-MIRROR",)
    # A mirror's cash is user-reported: shown, never claimed as buying power.
    assert ws.cash == {"USD": usd(10)} and ws.cash_basis == {"USD": "user_reported"}
    assert apply_scope(everything(m), c).spendable_cash("SYN-WS", "USD") is None
    other = c.accounts["SYN-OTHER"]
    assert other.status == "stale" and other.sizing_blocked


def test_same_source_latest_snapshot_supersedes_older() -> None:
    m = registry()
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "40"}, at=T0 - timedelta(hours=2)),
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}),
    ]
    ws = consolidate(m, TENANT, snaps, AS_OF, FRESH).accounts["SYN-WS"]
    assert ws.status == "reconciled" and ws.units == {(SYNTH, "USD"): Q(50)}


def test_snapshot_after_as_of_is_not_used() -> None:
    m = registry()
    later = snap("SYN-BROKER", "SYN-WS", {SYNTH: "60"}, at=AS_OF + timedelta(1))
    c = consolidate(
        m, TENANT, [snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}), later], AS_OF, FRESH
    )
    assert c.accounts["SYN-WS"].units == {(SYNTH, "USD"): Q(50)}


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
    view = apply_scope(everything(m), c)
    assert view.exposure == {}
    assert view.analysis_scope == "partial_declared"
    assert "2 source snapshot(s) quarantined" in view.gaps


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


# ---- Scopes, sleeves and spendability (ONB01, ONB13, R010, R042, R043)


def multi() -> tuple[SourceMap, list[SourceSnapshot]]:
    m = registry()
    m.add_account(
        UnderlyingAccount("SYN-CAD", TENANT, "SYN-INSTITUTION", "tfsa", "CAD")
    )
    m.add_source(Source("SYN-BROKER-3", TENANT, SourceKind.BROKER_EXPORT))
    m.remap("SYN-BROKER-3", "SYN-CAD", (MappedInterval("SYN-CAD", T0 - FRESH),), T0)
    cad = SourceSnapshot(
        "SYN-BROKER-3",
        "SYN-CAD",
        T0,
        (HoldingLine(SYNTH, "USD", Q(10)),),
        (Money.of(300, "CAD"), usd(20)),
        True,
    )
    snaps = [
        snap("SYN-BROKER", "SYN-WS", {SYNTH: "50"}, (usd(5000),)),
        snap("SYN-BROKER-2", "SYN-OTHER", {SYNTH: "50"}, (Money.of(800, "CAD"),)),
        cad,
    ]
    return m, snaps


def test_scope_excluding_an_account() -> None:
    m, snaps = multi()
    c = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    scope = PortfolioScope(
        ScopeKind.SELECTED, frozenset({"SYN-WS", "SYN-CAD"}), OutsideContext.NONE
    )
    view = apply_scope(scope, c)
    assert set(view.accounts) == {"SYN-WS", "SYN-CAD"}
    assert view.excluded == ("SYN-OTHER",)
    assert view.exposure == {(SYNTH, "USD"): Q(60)}  # 50 + 10, SYN-OTHER excluded
    assert view.analysis_scope == "complete_declared"
    assert view.statement == "complete for selected accounts"
    assert not view.full_situation_claim
    with pytest.raises(ScopeError, match="out_of_scope"):
        view.spendable_cash("SYN-OTHER", "CAD")


def test_no_cross_account_or_cross_currency_spendability() -> None:
    m, snaps = multi()
    view = apply_scope(everything(m), consolidate(m, TENANT, snaps, AS_OF, FRESH))
    assert view.spendable_cash("SYN-WS", "USD") == usd(5000)
    assert view.spendable_cash("SYN-OTHER", "USD") is None  # SYN-WS USD is not B's
    assert view.spendable_cash("SYN-WS", "CAD") is None  # no implicit FX
    assert view.spendable_cash("SYN-CAD", "CAD") == Money.of(300, "CAD")
    assert view.spendable_cash("SYN-CAD", "USD") == usd(20)
    # Cash stays per account and currency; nothing is summed across them.
    assert view.accounts["SYN-OTHER"].cash == {"CAD": Money.of(800, "CAD")}
    assert view.full_situation_claim and view.analysis_scope == "complete_declared"
    assert view.statement == "complete for all disclosed accounts"


def test_sleeves_are_per_account_and_currency_and_do_not_share() -> None:
    m, snaps = multi()
    sleeves = (
        Sleeve("trading", "SYN-WS", "USD", "trading", usd(1000)),
        Sleeve("reserve", "SYN-WS", "USD", "long_term_reserve", usd(4000)),
        Sleeve("cad-invest", "SYN-CAD", "CAD", "investing", Money.of(400, "CAD")),
    )
    scope = PortfolioScope(
        ScopeKind.ALL_DISCLOSED, frozenset(), OutsideContext.NONE, sleeves
    )
    view = apply_scope(scope, consolidate(m, TENANT, snaps, AS_OF, FRESH))
    # A trading sleeve never draws on the reserve (R010) or another account (R043).
    assert view.sleeve_available("trading") == usd(1000)
    assert view.sleeve_available("reserve") == usd(4000)
    # 400 CAD allocated against 300 CAD of cash: over-allocated, nothing available.
    assert view.sleeve_available("cad-invest") is None
    assert "SYN-CAD CAD: sleeves over-allocated" in view.gaps
    with pytest.raises(ScopeError, match="currency"):
        Sleeve("bad", "SYN-WS", "USD", "trading", Money.of(1, "CAD"))
    with pytest.raises(ScopeError, match="sleeve_account"):
        PortfolioScope(
            ScopeKind.SELECTED,
            frozenset({"SYN-WS"}),
            OutsideContext.NONE,
            (Sleeve("x", "SYN-CAD", "CAD", "investing", Money.of(1, "CAD")),),
        )


@pytest.mark.parametrize(
    ("outside", "gap"),
    [
        (OutsideContext.SUMMARIZED, "outside assets summarized without verification"),
        (OutsideContext.UNKNOWN, "outside exposure unknown, not zero"),
    ],
)
def test_outside_context_makes_the_view_partial(
    outside: OutsideContext, gap: str
) -> None:
    m, snaps = multi()
    scope = PortfolioScope(ScopeKind.ALL_DISCLOSED, frozenset(), outside)
    view = apply_scope(scope, consolidate(m, TENANT, snaps, AS_OF, FRESH))
    assert view.analysis_scope == "partial_declared" and not view.full_situation_claim
    assert view.statement == f"partial: {gap}"


def test_hypothetical_scope_uses_no_real_accounts() -> None:
    m, snaps = multi()
    scope = PortfolioScope(ScopeKind.HYPOTHETICAL, frozenset(), OutsideContext.UNKNOWN)
    view = apply_scope(scope, consolidate(m, TENANT, snaps, AS_OF, FRESH))
    assert view.accounts == {} and view.exposure == {}
    assert view.analysis_scope == "hypothetical" and not view.full_situation_claim
    with pytest.raises(ScopeError, match="hypothetical"):
        PortfolioScope(
            ScopeKind.HYPOTHETICAL, frozenset({"SYN-WS"}), OutsideContext.NONE
        )
    with pytest.raises(ScopeError, match="unknown_account"):
        apply_scope(
            PortfolioScope(
                ScopeKind.SELECTED, frozenset({"SYN-NOPE"}), OutsideContext.NONE
            ),
            consolidate(m, TENANT, snaps, AS_OF, FRESH),
        )


# ---- Property: mirrors of a report never change the consolidated result (F-04)

_qty = st.integers(0, 10**6).map(lambda n: Q(Decimal(n).scaleb(-2)))
_lines = st.dictionaries(st.sampled_from([SYNTH, SYNTH2]), _qty, min_size=1)


@PROFILE
@given(
    reports=st.lists(_lines, min_size=1, max_size=3),
    mirrors=st.lists(
        st.tuples(st.integers(0, 2), st.booleans(), st.integers(0, 5)), max_size=6
    ),
)
def test_mirroring_any_report_never_changes_the_total(
    reports: list[dict[InstrumentId, Q]],
    mirrors: list[tuple[int, bool, int]],
) -> None:
    m = SourceMap()
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    snaps = []
    for n, lines in enumerate(reports):
        m.add_account(acct(f"SYN-{n}"))
        m.remap("SYN-BROKER", f"r{n}", (MappedInterval(f"SYN-{n}", T0 - FRESH),), T0)
        held = tuple(HoldingLine(i, "USD", q) for i, q in lines.items())
        snaps.append(SourceSnapshot("SYN-BROKER", f"r{n}", T0, held, (), True))
    base = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    for k, (target, complete, lag) in enumerate(mirrors):
        n = target % len(reports)
        m.add_source(Source(f"SYN-MIRROR-{k}", TENANT, SourceKind.MIRROR))
        m.remap(f"SYN-MIRROR-{k}", "ref", (MappedInterval(f"SYN-{n}", T0 - FRESH),), T0)
        copy = snaps[n]
        at = T0 - timedelta(minutes=lag)
        snaps.append(
            SourceSnapshot(f"SYN-MIRROR-{k}", "ref", at, copy.holdings, (), complete)
        )
    after = consolidate(m, TENANT, snaps, AS_OF, FRESH)
    scope = PortfolioScope(ScopeKind.ALL_DISCLOSED, frozenset(), OutsideContext.NONE)
    expected: dict[tuple[InstrumentId, str], Q] = {}
    for lines in reports:
        for i, q in lines.items():
            if q.value:
                expected[(i, "USD")] = expected.get((i, "USD"), Q(0)) + q
    assert (
        apply_scope(scope, after).exposure
        == apply_scope(scope, base).exposure
        == expected
    )
    for n in range(len(reports)):
        view = after.accounts[f"SYN-{n}"]
        assert view.status == "reconciled" and view.conflicts == ()
        assert view.units == base.accounts[f"SYN-{n}"].units
