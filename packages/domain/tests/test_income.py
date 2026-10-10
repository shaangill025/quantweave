"""Distribution sustainability reference STR-INCOME-001 (T029 increment 2).

SYNTHETIC issuers, filings, distributions, prices and classifications only; no
market data. Expected numbers are hand-computed in the comments. The property is
differential: the same run on inputs pre-filtered to what was known at t.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import income as inc
from qw_domain import rights as rt
from qw_domain.decimals import Money, Price
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
from qw_domain.income import (
    DistKind,
    Distribution,
    Flag,
    IncomeInputs,
    Verdict,
    evaluate,
    income_manifest,
)
from qw_domain.quality_value import Classification, IssuerClass
from qw_domain.strategy_gate import GateCode, GateInputs, actionable
from qw_domain.strategy_registry import Family, Horizon, StrategyError, StrategyRegistry
from qw_domain.valuation import Mark, MarkKind

T = datetime(2026, 10, 9, 15, tzinfo=UTC)
M = income_manifest()
GEN = IssuerClass.GENERAL_CORPORATE
_seq = iter(range(1, 10**6))
Year = tuple[int, int, int, int]  # net income, CFO, capex, dividends paid
SOUND: Year = (100, 150, 50, 60)
EX_TRAIL = [date(2025, 11, 15), date(2026, 2, 15), date(2026, 5, 15), date(2026, 8, 15)]
EX_PRIOR = [date(2024, 11, 15), date(2025, 2, 15), date(2025, 5, 15), date(2025, 8, 15)]


def iid(n: int) -> InstrumentId:
    return InstrumentId(UUID(int=n))


def filed(year: int) -> datetime:
    return datetime(year + 1, 2, 20, 21, tzinfo=UTC)


def fact(n: int, concept: str, value: int, year: int, at: datetime) -> Fact:
    period = Period(date(year, 1, 1), date(year, 12, 31))
    return Fact(
        f"{n:010d}", Resolution("unknown"), "us-gaap", concept, parse_unit("USD"),
        period, year, "FY", Decimal(value), f"{n:010d}-26-{next(_seq):06d}", "10-K",
        at.date(), at, PublicationBasis.ACCEPTANCE, at, "feed-synth-sec", "synth-hash",
    )  # fmt: skip


CONCEPTS = (inc.NI, inc.CFO, inc.CAPEX, inc.DIVIDENDS)


def years(
    n: int, rows: Mapping[int, Year], skip: tuple[str, int] | None = None
) -> list[Fact]:
    return [
        fact(n, c, v, y, filed(y))
        for y, row in rows.items()
        for c, v in zip(CONCEPTS, row, strict=True)
        if (c, y) != skip
    ]


def dist(
    n: int, ex: date, amount: str = "0.50", kind: DistKind = DistKind.ORDINARY,
    roc: str | None = "0", known: datetime | None = None, assumed: bool = False,
) -> Distribution:  # fmt: skip
    seen = known or datetime(ex.year, ex.month, 1, tzinfo=UTC)
    rc = None if roc is None else Money.of(roc, "USD")
    return Distribution(
        iid(n), ex, ex + timedelta(days=15), Money.of(amount, "USD"), kind, rc, seen,
        "SYNTHETIC-sponsor", assumed,
    )  # fmt: skip


def quarterly(n: int, trail: str = "0.50", prior: str = "0.50") -> list[Distribution]:
    return [dist(n, d, trail) for d in EX_TRAIL] + [dist(n, d, prior) for d in EX_PRIOR]


def mark(n: int, price: str, at: datetime = T - timedelta(hours=1)) -> Mark:
    return Mark(iid(n), Price(price), "USD", MarkKind.LAST, at, "SYNTHETIC")


CLASSIFIED = filed(2022)


def klass(n: int, ic: IssuerClass = GEN, at: datetime = CLASSIFIED) -> Classification:
    return Classification(iid(n), f"{n:010d}", "sector-ind", ic, "USD", "SYNTH", at)


SOUND3 = {2023: SOUND, 2024: SOUND, 2025: SOUND}


def inputs(
    ns: Sequence[int], *, facts: Sequence[Fact] = (),
    dists: Sequence[Distribution] = (),
    prices: Mapping[int, str] | None = None, at: datetime = T,
    classes: Sequence[Classification] = (), config: Mapping[str, object] | None = None,
    history: Mapping[int, date] | None = None,
) -> IncomeInputs:  # fmt: skip
    cfg = config or {}
    marks = {
        iid(n): mark(n, p, at - timedelta(hours=1)) for n, p in (prices or {}).items()
    }
    hist = history if history is not None else {n: date(2024, 1, 1) for n in ns}
    return IncomeInputs(
        at, KnowledgeBasis.PUBLICATION, FactBook().add(facts),
        tuple(iid(n) for n in ns),
        tuple(classes or [klass(n) for n in ns]), marks, timedelta(days=5),
        tuple(dists), {iid(n): d for n, d in hist.items()}, cfg, M.config_hash(cfg),
    )  # fmt: skip


def base(**kw: object) -> IncomeInputs:
    """Issuer 1: three sound years, eight 0.50 quarters, price 50."""
    args: dict[str, object] = {"facts": years(1, SOUND3), "dists": quarterly(1)}
    return inputs([1], **(args | {"prices": {1: "50"}} | kw))  # type: ignore[arg-type]


def one(ins: IncomeInputs, n: int = 1) -> inc.IncomeResult:
    return next(r for r in evaluate(ins).issuers if r.instrument_id == iid(n))


def D(x: str) -> Decimal:
    return Decimal(x)


def test_sustained_payer_hand_values() -> None:
    r = one(base())
    # Trailing ordinary 4 x 0.50 = 2.00 over price 50 -> 0.04 headline and ordinary.
    assert [r.headline_yield, r.ordinary_yield, r.special_share] == [D("0.04")] * 2 + [
        0
    ]
    # 3 years: NI 300, FCF 3 x (150 - 50) = 300, dividends 180.
    assert r.fcf_coverage == r.earnings_coverage == D("1.666666666666666667")
    assert (r.payout_fcf, r.payout_earnings) == (D("0.6"), D("0.6"))
    assert (r.verdict, r.flags, r.rank) == (
        Verdict.SUSTAINED,
        (),
        1,
    ) and r.roc_share == 0


def test_special_distribution_inflates_the_headline() -> None:
    ds = [*quarterly(1), dist(1, date(2026, 6, 1), "3.00", DistKind.SPECIAL)]
    r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}))
    # Headline (2.00 + 3.00) / 50 = 0.10; ordinary 2.00 / 50 = 0.04; special 3/5.
    assert (r.headline_yield, r.ordinary_yield, r.special_share) == (
        D("0.1"), D("0.04"), D("0.6"),
    )  # fmt: skip
    assert {Flag.SPECIAL_INFLATES_HEADLINE, Flag.HEADLINE_UNSUSTAINABLE} == set(r.flags)


def test_unsustainable_headline_yield_payout_exceeds_earnings_and_fcf() -> None:
    weak = {y: (50, 80, 40, 60) for y in (2023, 2024, 2025)}
    r = one(inputs([1], facts=years(1, weak), dists=quarterly(1), prices={1: "20"}))
    # Headline 2.00 / 20 = 0.10. NI 150, FCF 3 x 40 = 120, dividends 180:
    # FCF coverage 120/180 = 2/3, earnings 150/180 = 5/6; payouts 1.5 and 1.2.
    assert r.headline_yield == D("0.1")
    assert r.fcf_coverage == D("0.666666666666666667")
    assert r.earnings_coverage == D("0.833333333333333333")
    assert (r.payout_fcf, r.payout_earnings) == (D("1.5"), D("1.2"))
    assert r.verdict is Verdict.UNSUSTAINABLE and r.rank is None
    assert set(r.flags) == {
        Flag.FCF_COVERAGE_BELOW_MIN, Flag.EARNINGS_COVERAGE_BELOW_MIN,
        Flag.HEADLINE_UNSUSTAINABLE,
    }  # fmt: skip


def test_fcf_shortfall_alone_is_unsustainable() -> None:
    thin = {y: (100, 80, 40, 60) for y in (2023, 2024, 2025)}
    r = one(inputs([1], facts=years(1, thin), dists=quarterly(1), prices={1: "50"}))
    # NI 300 covers dividends 180 (5/3), FCF 120 does not (2/3).
    assert r.earnings_coverage == D("1.666666666666666667")
    assert r.fcf_coverage == D("0.666666666666666667")
    assert r.verdict is Verdict.UNSUSTAINABLE
    assert set(r.flags) == {Flag.FCF_COVERAGE_BELOW_MIN, Flag.HEADLINE_UNSUSTAINABLE}


def test_negative_earnings_and_fcf_make_payouts_inapplicable() -> None:
    loss = {y: (-20, 30, 60, 60) for y in (2023, 2024, 2025)}
    r = one(inputs([1], facts=years(1, loss), dists=quarterly(1), prices={1: "50"}))
    # NI -60, FCF 3 x -30 = -90, dividends 180: coverage -1/2 and -1/3.
    assert (r.fcf_coverage, r.earnings_coverage) == (
        D("-0.5"),
        D("-0.333333333333333333"),
    )
    assert (r.payout_fcf, r.payout_earnings) == (None, None)
    assert {"fcf_non_positive", "earnings_non_positive"} <= set(r.reasons)
    assert r.verdict is Verdict.UNSUSTAINABLE


def test_negative_capex_is_not_assessable() -> None:
    odd = {**SOUND3, 2024: (100, 150, -50, 60)}
    r = one(inputs([1], facts=years(1, odd), dists=quarterly(1), prices={1: "50"}))
    assert r.verdict is Verdict.NOT_ASSESSABLE and "capex_negative" in r.reasons
    assert r.fcf_coverage is None


def test_cut_is_flagged_and_blocks_candidacy() -> None:
    ds = quarterly(1, trail="0.25", prior="0.50")
    r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}))
    assert r.headline_yield == D("0.02")  # 4 x 0.25 / 50
    assert Flag.DISTRIBUTION_CUT in r.flags and Flag.HEADLINE_UNSUSTAINABLE in r.flags
    assert r.verdict is Verdict.SUSTAINED and r.rank is None


def test_announced_assumed_and_future_known_distributions_stay_out() -> None:
    ds = [
        *quarterly(1),
        dist(1, date(2026, 11, 15)),  # known 2026-11-01: after t, invisible
        dist(1, date(2026, 10, 20), "0.55", known=T - timedelta(days=1)),  # ex after t
        dist(1, date(2026, 10, 1), known=T - timedelta(days=20)),  # ex by t, pays 10-16
        dist(1, date(2027, 2, 15), "0.60", known=T - timedelta(days=1), assumed=True),
    ]
    r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}))
    # Ex-date basis: the pending 10-01 record counts, (4 x 0.50 + 0.50) / 50 = 0.05.
    assert r.headline_yield == D("0.05")
    assert (r.announced_per_share, r.assumed_per_share) == (D("0.55"), D("0.60"))


def test_ex_to_pay_gap_is_not_a_cut_and_the_yield_is_stable() -> None:
    exs = [date(y, m, 15) for y in (2024, 2025, 2026) for m in (2, 5, 8, 11)]
    ds = [dist(1, d) for d in exs if d >= date(2024, 11, 15)]  # paid 15 days later
    for day in (14, 20, 30):  # before, inside and after the 11-15 -> 11-30 gap
        at = datetime(2026, 11, day, 15, tzinfo=UTC)
        r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}, at=at))
        # Four 0.50 ex-dates in each 365-day window: 2.00 / 50, no cut.
        assert (r.headline_yield, r.flags, r.rank) == (D("0.04"), (), 1), day
    # A real cut inside the gap: trailing 3 x 0.50 + 0.25 = 1.75 against a prior
    # window whose 2025-11-15 record was still pending a year earlier (2.00).
    cut = [dist(1, d, "0.25" if d == date(2026, 8, 15) else "0.50") for d in exs[3:]]
    at = datetime(2026, 11, 20, 15, tzinfo=UTC)
    r = one(inputs([1], facts=years(1, SOUND3), dists=cut, prices={1: "50"}, at=at))
    assert Flag.DISTRIBUTION_CUT in r.flags and r.headline_yield == D("0.035")


def test_return_of_capital_share_known_or_unknown() -> None:
    ds = [*quarterly(1)[1:], dist(1, EX_TRAIL[0], roc="0.10")]
    r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}))
    assert r.roc_share == D("0.05") and Flag.RETURN_OF_CAPITAL in r.flags  # 0.10/2.00
    ds = [*quarterly(1)[1:], dist(1, EX_TRAIL[0], roc=None)]
    r = one(inputs([1], facts=years(1, SOUND3), dists=ds, prices={1: "50"}))
    assert r.roc_share is None and "return_of_capital_unknown" in r.reasons


def test_missing_data_is_explicit() -> None:
    gap = years(1, SOUND3, skip=(inc.DIVIDENDS, 2024))
    r = one(inputs([1], facts=gap, dists=quarterly(1), prices={1: "50"}))
    assert r.verdict is Verdict.NOT_ASSESSABLE
    assert "PaymentsOfDividends@2024-12-31" in r.reasons and r.fcf_coverage is None
    two = years(1, {2024: SOUND, 2025: SOUND})
    r = one(inputs([1], facts=two, dists=quarterly(1), prices={1: "50"}))
    assert (r.verdict, r.fcf_coverage) == (Verdict.NOT_ASSESSABLE, None)
    assert "lookback_incomplete" in r.reasons
    r = one(inputs([1], facts=years(1, SOUND3), dists=quarterly(1)))
    assert (r.headline_yield, r.ordinary_yield) == (None, None)
    assert "price_missing" in r.reasons and r.special_share == 0
    late = {1: date(2025, 1, 1)}  # records complete only since 2025: no prior year
    r = one(base(history=late))
    assert "cut_history_incomplete" in r.reasons and r.headline_yield == D("0.04")
    r = one(base(history={}))
    assert r.headline_yield is None and "distribution_history_incomplete" in r.reasons


def test_stale_filings_are_not_assessable() -> None:
    late = datetime(2027, 4, 2, 15, tzinfo=UTC)  # FY2025 end + 457 days
    ins = base(at=late)
    r = one(ins)
    assert r.verdict is Verdict.NOT_ASSESSABLE
    assert "period_end_older_than_456_days" in r.reasons


def test_incompatible_class_and_unknown_classification() -> None:
    classes = [klass(1, IssuerClass.BANK), klass(2, at=T + timedelta(days=1))]
    facts, ds = years(1, SOUND3) + years(2, SOUND3), quarterly(1) + quarterly(2)
    ins = inputs(
        [1, 2], facts=facts, dists=ds, prices={1: "50", 2: "50"}, classes=classes
    )
    bank, unknown = one(ins, 1), one(ins, 2)
    assert (bank.verdict, bank.headline_yield, bank.fcf_coverage) == (
        Verdict.NOT_APPLICABLE, None, None,
    )  # fmt: skip
    assert unknown.verdict is Verdict.NOT_ASSESSABLE
    assert unknown.reasons == ("classification_unknown",)


def test_candidates_rank_by_coverage_never_by_headline_yield() -> None:
    strong = {y: (200, 300, 50, 60) for y in (2023, 2024, 2025)}  # FCF cover 750/180
    facts = years(1, SOUND3) + years(2, strong) + years(3, SOUND3)
    ds = quarterly(1) + quarterly(2) + quarterly(3)
    prices = {1: "10", 2: "90", 3: "50"}
    res = evaluate(inputs([3, 2, 1], facts=facts, dists=ds, prices=prices))
    assert [r.instrument_id for r in res.candidates] == [iid(2), iid(1), iid(3)]
    assert [r.rank for r in res.candidates] == [1, 2, 3] and res.research_only


def test_config_binding_inputs_and_manifest_gate() -> None:
    ins = base()
    assert evaluate(replace(ins, config_hash="0" * 64)).blocked == ("config_mismatch",)
    bad = replace(ins, config={"lookback_years": 0})
    assert evaluate(bad).blocked == ("config_invalid:config",)
    with pytest.raises(StrategyError, match="prices"):
        replace(ins, prices={iid(1): mark(2, "50")})
    with pytest.raises(StrategyError, match="distribution"):
        dist(1, EX_TRAIL[0], roc="0.60")  # return of capital above the amount
    m = income_manifest("feed-synth-sec")
    assert (m.strategy_id, m.family, m.horizon) == (
        "STR-INCOME-001", Family.INCOME, Horizon.LONG_TERM,
    )  # fmt: skip
    gate = GateInputs(
        rt.Registry(), rt.UseScope.PERSONAL, "CA-ON", None, {}, frozenset()
    )
    reg = StrategyRegistry().register(m)
    d = actionable(reg, "tenant-synth-a", m.strategy_id, m.version, {}, T, gate)
    assert not d.allowed
    assert GateCode.OPERATIONAL_NOT_PASSED in {r.code for r in d.reasons}


# --- property --------------------------------------------------------------------

type Pair = tuple[int, int]
type Row = tuple[int, int, int, bool]
days = st.integers(-60, 60)  # around t; positive = not yet known at t


@settings(max_examples=120, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(list(IssuerClass)), days, days,
            st.lists(st.tuples(st.integers(-50, 300), days), min_size=12, max_size=12),
            st.lists(st.tuples(st.integers(-500, 400), st.integers(1, 99), days,
                               st.booleans()), max_size=6),
        ),
        min_size=1, max_size=3,
    ),
    st.sampled_from(list(KnowledgeBasis)),
)  # fmt: skip
def test_nothing_unknown_at_t_is_used(
    issuers: list[tuple[IssuerClass, int, int, list[Pair], list[Row]]],
    basis: KnowledgeBasis,
) -> None:
    facts, classes, marks, ds = [], [], {}, []
    for n, (ic, c_off, p_off, vals, rows) in enumerate(issuers, start=1):
        classes.append(klass(n, ic, at=T + timedelta(days=c_off)))
        marks[iid(n)] = mark(n, "40", at=T + timedelta(days=p_off))
        for k, (v, off) in enumerate(vals):
            y = 2023 + k // 4
            facts.append(fact(n, CONCEPTS[k % 4], v, y, T + timedelta(days=off)))
        for ex_off, cents, k_off, special in rows:
            kind = DistKind.SPECIAL if special else DistKind.ORDINARY
            ex = T.date() + timedelta(days=ex_off)
            known = T + timedelta(days=k_off)
            ds.append(dist(n, ex, f"0.{cents:02d}", kind, known=known))
    ids = tuple(iid(n) for n in range(1, len(issuers) + 1))
    hist = {i: date(2024, 1, 1) for i in ids}
    full = IncomeInputs(
        T, basis, FactBook().add(facts), ids, tuple(classes), marks, timedelta(days=90),
        tuple(ds), hist, {}, M.config_hash({}),
    )  # fmt: skip
    known_only = IncomeInputs(
        T, basis, FactBook().add(f for f in facts if known_at(f, basis) <= T),
        ids[::-1],
        tuple(c for c in classes[::-1] if c.known_at <= T),
        {i: m for i, m in marks.items() if m.observed_at <= T}, timedelta(days=90),
        tuple(d for d in ds[::-1] if d.known_at <= T), hist, {}, M.config_hash({}),
    )  # fmt: skip
    res = evaluate(full)
    assert res == evaluate(known_only)
    for r in res.issuers:
        c = inc.classification_at(classes, r.instrument_id, T)
        if c is not None and c.issuer_class is not GEN:
            assert r.verdict is Verdict.NOT_APPLICABLE and r.headline_yield is None
