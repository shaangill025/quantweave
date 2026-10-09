"""Sector-aware quality/value reference STR-QUALITY-001 (T029 increment 1).

SYNTHETIC issuers, CIKs, filings, classifications and market values only; no market
data. Expected numbers are hand-computed in the comments. The property's oracle is
the same run on inputs pre-filtered to what was known at t, never the code's internals.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import quality_value as qv
from qw_domain import rights as rt
from qw_domain.decimals import Money
from qw_domain.filings import (
    Fact,
    FactBook,
    KnowledgeBasis,
    Period,
    PublicationBasis,
    known_at,
    parse_unit,
)
from qw_domain.identity import InstrumentId, Resolution
from qw_domain.quality_value import (
    Classification,
    IssuerClass,
    MarketValue,
    QualityInputs,
    State,
    Status,
    evaluate,
    quality_manifest,
)
from qw_domain.strategy_gate import GateCode, GateInputs, actionable
from qw_domain.strategy_registry import Family, Horizon, StrategyError, StrategyRegistry

T = datetime(2026, 10, 9, 15, tzinfo=UTC)
FILED = datetime(2026, 2, 20, 21, tzinfo=UTC)  # FY2025 10-K publication
FY = date(2025, 12, 31)
PUB = KnowledgeBasis.PUBLICATION
M = quality_manifest()
GEN = IssuerClass.GENERAL_CORPORATE
NAMES = [d.name for d in qv.RECIPE]
_seq = iter(range(1, 10**6))

# (revenue, operating income, net income, CFO, capex, equity, debt, assets, cash, MV)
Row = tuple[int, ...]
A_: Row = (1000, 200, 150, 220, 70, 800, 200, 1500, 100, 2000)
B_: Row = (500, 50, 40, 60, 30, 400, 100, 600, 50, 500)
C_: Row = (2000, 600, 450, 700, 100, 1500, 0, 2000, 500, 6000)
CONCEPTS = (qv.REV, qv.OI, qv.NI, qv.CFO, qv.CAPEX, qv.EQUITY, qv.DEBT, qv.ASSETS,
            qv.CASH)  # fmt: skip
FLOWS = {qv.REV, qv.OI, qv.NI, qv.CFO, qv.CAPEX}
SOON = T - timedelta(hours=1)


def iid(n: int) -> InstrumentId:
    return InstrumentId(UUID(int=n))


def cik(n: int) -> str:
    return f"{n:010d}"


def fact(
    n: int, concept: str, value: int, end: date = FY, published: datetime = FILED
) -> Fact:
    period = Period(date(end.year, 1, 1) if concept in FLOWS else None, end)
    accn = f"{cik(n)}-26-{next(_seq):06d}"
    return Fact(
        cik(n), Resolution("unknown"), "us-gaap", concept, parse_unit("USD"), period,
        end.year, "FY", Decimal(value), accn, "10-K", published.date(), published,
        PublicationBasis.ACCEPTANCE, published, "feed-synth-sec", "synthetic-hash",
    )  # fmt: skip


def facts(
    n: int, row: Row, skip: tuple[str, ...] = (), end: date = FY, at: datetime = FILED
) -> list[Fact]:
    pairs = zip(CONCEPTS, row[:9], strict=True)
    return [fact(n, c, v, end, at) for c, v in pairs if c not in skip]


def klass(
    n: int, ic: IssuerClass = GEN, at: datetime = FILED, sector: str = "sector-ind"
) -> Classification:
    return Classification(iid(n), cik(n), sector, ic, "USD", "SYNTHETIC-gics", at)


def mv(n: int, amount: int, at: datetime = SOON, ccy: str = "USD") -> MarketValue:
    return MarketValue(iid(n), Money.of(amount, ccy), at)


def inputs(
    rows: Mapping[int, Row], *, at: datetime = T, extra: Sequence[Fact] = (),
    classes: Sequence[Classification] = (), config: Mapping[str, object] | None = None,
    mvs: Mapping[int, MarketValue] | None = None,
    skip: Mapping[int, tuple[str, ...]] | None = None,
) -> QualityInputs:  # fmt: skip
    book = [f for n, r in rows.items() for f in facts(n, r, (skip or {}).get(n, ()))]
    cfg = config or {}
    hour = at - timedelta(hours=1)
    vals = {n: mv(n, r[9], hour) for n, r in rows.items()} if mvs is None else mvs
    marks = {iid(n): m for n, m in vals.items()}
    return QualityInputs(
        at, PUB, FactBook().add([*book, *extra]), tuple(iid(n) for n in rows),
        tuple(classes or [klass(n) for n in rows]), marks, timedelta(days=5), cfg,
        M.config_hash(cfg),
    )  # fmt: skip


def by_id(res: qv.QualityResult) -> dict[InstrumentId, qv.IssuerResult]:
    return {r.instrument_id: r for r in res.issuers}


def metric(r: qv.IssuerResult, name: str) -> qv.Metric:
    return next(m for m in r.metrics if m.name == name)


def test_hand_computed_values_scores_and_sector_ranks() -> None:
    res = evaluate(inputs({1: A_, 2: B_, 3: C_}))
    assert res.blocked == () and res.research_only
    a, b, c = (by_id(res)[iid(n)] for n in (1, 2, 3))
    # A: margin 200/1000; ROIC 200/(800+200); conversion (220-70)/150;
    # debt/assets 200/1500; EBIT/EV 200/(2000+200-100); FCF yield 150/2000.
    want = [Fraction(1, 5), Fraction(1, 5), Fraction(1), Fraction(2, 15),
            Fraction(2, 21), Fraction(3, 40)]  # fmt: skip
    for name, w in zip(NAMES, want, strict=True):
        m = metric(a, name)
        assert (m.state, m.period_end) == (State.VALUE, FY)
        assert m.value == qv.to_decimal(w)
    # Every metric orders B < A < C (debt/assets: lower is better, C has 0 debt),
    # so the percentiles over 3 peers are 0, 1/2, 1 and the composites 0, 1/2, 1.
    assert [metric(b, n).score for n in NAMES] == [Decimal(0)] * 6
    assert [metric(c, n).score for n in NAMES] == [Decimal(1)] * 6
    assert [a.composite, b.composite, c.composite] == [Decimal("0.5"), 0, 1]
    assert [r.rank_in_sector for r in res.candidates] == [1, 2, 3]
    assert [r.instrument_id for r in res.candidates] == [iid(3), iid(1), iid(2)]
    assert a.coverage == 1


def test_ties_share_the_mid_rank_and_ties_order_by_instrument_id() -> None:
    res = evaluate(inputs({5: A_, 4: A_, 3: C_}))
    four, five = by_id(res)[iid(4)], by_id(res)[iid(5)]
    # Equal values: worse 0, ties 1 -> (0 + 1) / (2 x 2) = 1/4 on every metric.
    assert four.composite == five.composite == Decimal("0.25")
    assert [r.instrument_id for r in res.candidates] == [iid(3), iid(4), iid(5)]


def test_negative_earnings_are_undefined_never_a_cheap_multiple() -> None:
    # D: operating loss -20 and net loss -50; FCF 10 - 40 = -30.
    d_ = (900, -20, -50, 10, 40, 300, 100, 700, 20, 50)
    base = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_})))
    res = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_, 4: d_})))
    d = res[iid(4)]
    for name, why in (("cash_conversion", "earnings_non_positive"),
                      ("operating_earnings_to_ev", "earnings_non_positive"),
                      ("fcf_yield", "fcf_non_positive")):  # fmt: skip
        m = metric(d, name)
        assert (m.state, m.value, m.score, m.reason) == (
            State.UNDEFINED,
            None,
            None,
            why,
        )
    # A loss is still a real (low) margin: -20/900 is the sector's worst of 4.
    assert metric(d, "operating_margin").value == qv.to_decimal(Fraction(-1, 45))
    assert metric(d, "operating_margin").score == 0
    # Valuation peers stay A, B, C, so their EV-yield scores are unchanged.
    for n in (1, 2, 3):
        name = "operating_earnings_to_ev"
        assert metric(res[iid(n)], name).score == metric(base[iid(n)], name).score


@pytest.mark.parametrize(
    "ic",
    [IssuerClass.BANK, IssuerClass.INSURER, IssuerClass.FUND, IssuerClass.PRE_REVENUE],
)
def test_incompatible_issuer_class_is_not_applicable(ic: IssuerClass) -> None:
    classes = [klass(1), klass(2), klass(3), klass(4, ic)]
    res = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_, 4: A_}, classes=classes)))
    r = res[iid(4)]
    assert (r.status, r.composite, r.coverage, r.rank_in_sector) == (
        Status.NOT_APPLICABLE, None, None, None,
    )  # fmt: skip
    assert {(m.state, m.value, m.score) for m in r.metrics} == {
        (State.NOT_APPLICABLE, None, None)
    }
    assert {m.reason for m in r.metrics} == {f"{ic.value}_unsupported"}
    # It is not a peer: A keeps its 3-peer mid rank.
    assert res[iid(1)].composite == Decimal("0.5")


def test_reclassification_is_point_in_time() -> None:
    later = T + timedelta(days=1)
    classes = [klass(1), klass(1, IssuerClass.BANK, at=later), klass(2), klass(3)]
    ins = inputs({1: A_, 2: B_, 3: C_}, classes=classes)
    assert by_id(evaluate(ins))[iid(1)].status is Status.CANDIDATE
    res = evaluate(replace(ins, as_of=later, market_values={}))
    assert by_id(res)[iid(1)].status is Status.NOT_APPLICABLE
    future = [klass(1, at=T + timedelta(seconds=1)), klass(2), klass(3)]
    r = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_}, classes=future)))[iid(1)]
    assert (r.status, r.metrics) == (Status.CLASSIFICATION_UNKNOWN, ())


def test_stale_filing_by_period_age_and_knowledge_time() -> None:
    rows = {1: A_, 2: B_, 3: C_}
    # FY end 2025-12-31 + 456 days = 2027-04-01: still usable; a day later stale.
    edge = datetime(2027, 4, 1, 15, tzinfo=UTC)
    ok = by_id(evaluate(inputs(rows, at=edge)))[iid(1)]
    assert {m.state for m in ok.metrics} == {State.VALUE}
    late = edge + timedelta(days=1)
    # A FY2026 10-K for A exists but is published after `late`: never read.
    filed = late + timedelta(days=3)
    newer = facts(1, A_, end=date(2026, 12, 31), at=filed)
    a = by_id(evaluate(inputs(rows, at=late, extra=newer)))[iid(1)]
    assert {(m.state, m.reason) for m in a.metrics} == {
        (State.STALE, "period_end_older_than_456_days")
    }
    assert a.status is Status.INSUFFICIENT_COVERAGE and a.composite is None
    after = by_id(evaluate(inputs(rows, at=filed, extra=newer)))[iid(1)]
    assert {(m.state, m.period_end) for m in after.metrics} == {
        (State.VALUE, date(2026, 12, 31))
    }


def test_missing_inputs_reduce_coverage_never_zero() -> None:
    cfg: dict[str, object] = {"min_peers": 2}
    skip = {2: (qv.DEBT,)}
    res = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_}, skip=skip, config=cfg)))
    b, a = res[iid(2)], res[iid(1)]
    for name in ("roic_pretax", "debt_to_assets", "operating_earnings_to_ev"):
        m = metric(b, name)
        assert (m.state, m.value, m.score) == (State.MISSING, None, None)
        assert m.reason == "LongTermDebt@2025-12-31"
    # B: 3 of 6 equal weights scored (all 0); A: 1/2 on B-including metrics, 0 on
    # the three ranked against C alone -> (3 x 1/2) / 6 = 1/4.
    assert (b.coverage, b.composite) == (Decimal("0.5"), Decimal(0))
    assert b.status is Status.CANDIDATE
    assert a.composite == Decimal("0.25")
    strict = {"min_peers": 2, "min_coverage": Decimal("0.6")}
    res = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_}, skip=skip, config=strict)))
    assert res[iid(2)].status is Status.INSUFFICIENT_COVERAGE
    assert res[iid(2)].composite is None


def test_market_value_missing_future_stale_or_other_currency() -> None:
    rows = {1: A_, 2: B_, 3: C_}
    cases = {
        None: (State.MISSING, "market_value"),
        mv(1, 2000, at=T + timedelta(seconds=1)): (State.MISSING, "market_value"),
        mv(1, 2000, at=T - timedelta(days=6)): (State.STALE, "market_value"),
        mv(1, 2000, ccy="CAD"): (State.MISSING, "market_value_currency"),
    }
    for value, want in cases.items():
        mvs = {2: mv(2, 500), 3: mv(3, 6000)} | ({1: value} if value else {})
        a = by_id(evaluate(inputs(rows, mvs=mvs)))[iid(1)]
        for name in ("operating_earnings_to_ev", "fcf_yield"):
            assert (metric(a, name).state, metric(a, name).reason) == want
        assert metric(a, "operating_margin").state is State.VALUE


def test_peer_group_below_minimum_is_unscored() -> None:
    a = by_id(evaluate(inputs({1: A_, 2: B_})))[iid(1)]
    assert {m.reason for m in a.metrics} == {"peer_group_below_min_peers"}
    assert a.status is Status.INSUFFICIENT_COVERAGE


def test_tied_restatement_is_conflicted() -> None:
    clash = fact(1, qv.REV, 999)  # same period and publication time, other value
    a = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_}, extra=[clash])))[iid(1)]
    m = metric(a, "operating_margin")
    assert (m.state, m.value, m.reason) == (
        State.CONFLICTED,
        None,
        "Revenues@2025-12-31",
    )


def test_config_is_bound_to_the_adopted_hash() -> None:
    ins = inputs({1: A_, 2: B_, 3: C_})
    assert evaluate(replace(ins, config_hash="0" * 64)).blocked == ("config_mismatch",)
    bad = replace(ins, config={"min_coverage": Decimal(2)})
    assert evaluate(bad).blocked == ("config_invalid:config",)
    assert evaluate(bad).issuers == ()


def test_manifest_registers_and_the_gate_fails_closed() -> None:
    m = quality_manifest("feed-synth-sec")
    assert (m.strategy_id, m.family, m.horizon) == (
        "STR-QUALITY-001", Family.QUALITY_VALUE, Horizon.LONG_TERM,
    )  # fmt: skip
    reg = StrategyRegistry().register(m)
    gate = GateInputs(
        rt.Registry(), rt.UseScope.PERSONAL, "CA-ON", None, {}, frozenset()
    )
    d = actionable(reg, "tenant-synth-a", m.strategy_id, m.version, {}, T, gate)
    assert not d.allowed  # catalogue: action_eligible false until qualification
    assert {GateCode.CAPABILITY_MISSING, GateCode.OPERATIONAL_NOT_PASSED} <= {
        r.code for r in d.reasons
    }


# --- property --------------------------------------------------------------------

offsets = st.integers(-40, 40)  # days around t; positive = not yet known at t


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(list(IssuerClass)), offsets, offsets,
            st.lists(st.tuples(st.integers(-50, 2000), offsets), min_size=9,
                     max_size=9),
        ),
        min_size=2, max_size=4,
    ),
    st.sampled_from(list(KnowledgeBasis)),
)  # fmt: skip
def test_nothing_unknown_at_t_is_used_and_incompatible_is_not_applicable(
    issuers: list[tuple[IssuerClass, int, int, list[tuple[int, int]]]],
    basis: KnowledgeBasis,
) -> None:
    book, classes, mvs = [], [], {}
    for n, (ic, c_off, mv_off, vals) in enumerate(issuers, start=1):
        classes.append(klass(n, ic, at=T + timedelta(days=c_off)))
        mvs[iid(n)] = mv(n, 1000, at=T + timedelta(days=mv_off))
        for concept, (v, off) in zip(CONCEPTS, vals, strict=True):
            book.append(fact(n, concept, v, published=T + timedelta(days=off)))
    ids = tuple(iid(n) for n in range(1, len(issuers) + 1))
    full = QualityInputs(
        T, basis, FactBook().add(book), ids, tuple(classes), mvs, timedelta(days=60),
        {}, M.config_hash({}),
    )  # fmt: skip
    known = QualityInputs(
        T, basis, FactBook().add(f for f in book if known_at(f, basis) <= T),
        ids[::-1], tuple(c for c in classes[::-1] if c.known_at <= T),
        {i: m for i, m in mvs.items() if m.observed_at <= T}, timedelta(days=60),
        {}, M.config_hash({}),
    )  # fmt: skip
    res = evaluate(full)
    assert res == evaluate(known)  # also order-independent
    for r in res.issuers:
        c = qv.classification_at(full.classifications, r.instrument_id, T)
        if c is not None and c.issuer_class is not GEN:
            assert r.status is Status.NOT_APPLICABLE
            assert {(m.state, m.value) for m in r.metrics} == {
                (State.NOT_APPLICABLE, None)
            }


def test_market_value_must_be_keyed_by_its_own_instrument() -> None:
    ins = inputs({1: A_, 2: B_, 3: C_})
    swapped = {iid(1): mv(2, 500), iid(2): mv(2, 500), iid(3): mv(3, 6000)}
    with pytest.raises(StrategyError, match="market_values"):
        replace(ins, market_values=swapped)


def test_negative_capex_is_undefined_not_extra_cash() -> None:
    d_ = (1000, 200, 150, 220, -70, 800, 200, 1500, 100, 2000)
    d = by_id(evaluate(inputs({1: A_, 2: B_, 3: C_, 4: d_})))[iid(4)]
    for name in ("cash_conversion", "fcf_yield"):
        m = metric(d, name)
        assert (m.state, m.value, m.reason) == (State.UNDEFINED, None, "capex_negative")


def test_all_zero_weights_are_an_invalid_config() -> None:
    cfg: dict[str, object] = {f"weight_{n}": Decimal(0) for n in NAMES}
    res = evaluate(inputs({1: A_, 2: B_, 3: C_}, config=cfg))
    assert (res.blocked, res.issuers) == (("config_invalid:weights",), ())
