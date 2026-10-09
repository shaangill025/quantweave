"""Portfolio scopes, sleeves, spendability and completeness (T013 PR B).

SYNTHETIC inputs only: hand-built snapshots and docs/spec/tests/fixtures/
overlapping_holdings.csv (consolidated 100, not 150 or 50; T008 F-04). Expected
numbers are hand-computed in each test.
"""

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from qw_domain.decimals import Money
from qw_domain.decimals import Quantity as Q
from qw_domain.identity import InstrumentId
from qw_domain.scopes import (
    OutsideContext,
    PortfolioScope,
    ScopeKind,
    Sleeve,
    apply_scope,
)
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

FIXTURES = Path(__file__).resolve().parents[3] / "docs/spec/tests/fixtures"
SYNTH = InstrumentId(UUID("00000000-0000-4000-8000-0000000000a1"))
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
AS_OF = T0 + timedelta(hours=1)
FRESH = timedelta(days=1)
TENANT = "SYN-TENANT"


def usd(v: str | int) -> Money:
    return Money.of(v, "USD")


def snap(
    source: str, ref: str, qty: str, cash: tuple[Money, ...] = (), **kw: object
) -> SourceSnapshot:
    at = kw.get("at", T0)
    assert isinstance(at, datetime)
    lines = (HoldingLine(SYNTH, "USD", Q(qty)),)
    return SourceSnapshot(
        source, ref, at, lines, cash, kw.get("complete", True) is True
    )


def registry() -> SourceMap:
    """SYN-WS via its broker and a mirror; SYN-OTHER via a second broker."""
    m = SourceMap()
    for a in ("SYN-WS", "SYN-OTHER"):
        m.add_account(UnderlyingAccount(a, TENANT, "SYN-INSTITUTION", "margin", "USD"))
    m.add_source(Source("SYN-BROKER", TENANT, SourceKind.BROKER_API))
    m.add_source(Source("SYN-YAHOO-MIRROR", TENANT, SourceKind.MIRROR))
    m.add_source(Source("SYN-BROKER-2", TENANT, SourceKind.BROKER_API))
    for source, ref in (
        ("SYN-BROKER", "SYN-WS"),
        ("SYN-YAHOO-MIRROR", "SYN-WS"),
        ("SYN-BROKER-2", "SYN-OTHER"),
    ):
        m.remap(source, ref, (MappedInterval(ref, T0 - timedelta(30)),), T0)
    return m


def everything(m: SourceMap) -> PortfolioScope:
    return PortfolioScope(ScopeKind.ALL_DISCLOSED, frozenset(), OutsideContext.NONE)


def test_overlap_fixture_exposure_is_100() -> None:
    rows = list(csv.DictReader((FIXTURES / "overlapping_holdings.csv").open()))
    snaps = [
        snap(r["source"], r["underlying_account"], r["quantity"],
             complete=r["source"] != "SYN-YAHOO-MIRROR")
        for r in rows
    ]  # fmt: skip
    m = registry()
    view = apply_scope(everything(m), consolidate(m, TENANT, snaps, AS_OF, FRESH))
    assert view.exposure == {(SYNTH, "USD"): Q(100)}
    assert view.full_situation_claim and view.gaps == ()


@pytest.mark.parametrize(
    ("snaps", "gaps"),
    [
        (  # broker 50 vs mirror 40
            [snap("SYN-BROKER", "SYN-WS", "50", (usd(1000),)),
             snap("SYN-YAHOO-MIRROR", "SYN-WS", "40", complete=False)],
            ["SYN-WS: conflicted"],
        ),
        (  # stale broker said 70; only a fresh incomplete mirror says 50
            [snap("SYN-BROKER", "SYN-WS", "70", at=T0 - timedelta(days=3)),
             snap("SYN-YAHOO-MIRROR", "SYN-WS", "50", complete=False)],
            ["SYN-WS: conflicted", "SYN-WS: SYN-BROKER (stale, higher authority) "
             f"last reported 70 for {SYNTH.to_wire()} USD"],
        ),
        (  # incomplete mirror agreeing with a stale broker
            [snap("SYN-BROKER", "SYN-WS", "50", at=T0 - timedelta(days=3)),
             snap("SYN-YAHOO-MIRROR", "SYN-WS", "50", complete=False)],
            ["SYN-WS: incomplete"],
        ),
        (  # unmapped reference
            [snap("SYN-BROKER", "SYN-WS", "50"), snap("SYN-BROKER", "SYN-X", "1")],
            ["1 source snapshot(s) quarantined"],
        ),
    ],
)  # fmt: skip
def test_blocked_accounts_make_the_view_partial(
    snaps: list[SourceSnapshot], gaps: list[str]
) -> None:
    m = registry()
    other = snap("SYN-BROKER-2", "SYN-OTHER", "50", (usd(700),))
    view = apply_scope(
        everything(m), consolidate(m, TENANT, [*snaps, other], AS_OF, FRESH)
    )
    assert list(view.gaps) == gaps
    assert view.analysis_scope == "partial_declared" and not view.full_situation_claim
    assert view.statement == "partial: " + "; ".join(gaps)
    assert view.spendable_cash("SYN-WS", "USD") is None
    assert view.spendable_cash("SYN-OTHER", "USD") == usd(700)  # unaffected


# ---- Scopes, sleeves and spendability (ONB01, ONB13, R010, R042, R043)


def multi() -> tuple[SourceMap, list[SourceSnapshot]]:
    m = registry()
    m.add_account(UnderlyingAccount("SYN-CAD", TENANT, "SYN-INST", "tfsa", "CAD"))
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
        snap("SYN-BROKER", "SYN-WS", "50", (usd(5000),)),
        snap("SYN-BROKER-2", "SYN-OTHER", "50", (Money.of(800, "CAD"),)),
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
