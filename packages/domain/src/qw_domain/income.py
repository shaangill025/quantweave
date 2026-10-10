"""Distribution sustainability research reference STR-INCOME-001 (T029 increment 2).

Spec §8 STR-INCOME-001; R059, R072, R084. Research candidates only: action
eligibility stays false until qualification (`strategy_gate`).
- Distribution records known at t (`known_at` <= t) are kept apart, all on an
  ex-date basis: trailing (ex-date in the 365 days to t, paid or pending payment),
  announced (ex-date after t) and assumed (a model assumption, never in a yield).
  Records after t are invisible. The prior window uses the same basis, so an
  ex-to-pay gap is never a cut.
- Headline yield = all trailing per share / price; ordinary yield = ordinary
  only. Any special distribution in the window flags `special_inflates_headline`.
  Yields need a fresh price (observed by t, within `price_max_age`) in the records'
  currency and complete records (`history_start`) for the window; cuts need the
  prior 365 days too. Ordinary trailing < ordinary prior flags `distribution_cut`.
  Return-of-capital share = trailing ROC / trailing total, unknown if any is unknown.
- Coverage uses the last `lookback_years` consecutive annual filings from
  `FactBook.as_of(t)` (shared readers in `quality_value`): sums of net income, FCF
  (CFO - capex) and dividends paid (us-gaap `PaymentsOfDividends`, which includes
  specials). Coverage = earnings or FCF / dividends; below `min_coverage` (default 1:
  payout exceeds the source) is `unsustainable`. Payout = dividends / earnings or FCF
  is inapplicable (None, with a reason) for a non-positive denominator. Missing,
  conflicted, stale or non-consecutive years, negative capex or no dividends in the
  filings make it `not_assessable`; nothing is imputed. Sums over the lookback let
  one strong year offset weak ones (no per-year test).
- `headline_unsustainable` = unsustainable, special-inflated or cut. Candidates are
  sustained, uncut issuers with a known yield, ranked by FCF coverage then instrument
  id, never by headline yield. A special-inflated issuer can still be a candidate:
  callers must show `headline_unsustainable` next to the rank. Only
  `general_corporate` issuers are assessed; banks, insurers, funds and pre-revenue
  issuers are `not_applicable` (separate recipes).
LIMITATIONS: no FX; no debt-obligation or balance-sheet resilience screen; company
dividends are compared without share-class split; ROC character is caller-supplied.
Stdlib only.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from functools import cache
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from qw_domain.decimals import Money
from qw_domain.filings import AsOfView, FactBook, KnowledgeBasis
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.quality_value import (
    CAPEX,
    CFO,
    FLOW_DAYS,
    NI,
    Classification,
    IssuerClass,
    State,
    annual_series,
    classification_at,
    param_fraction,
    read_at,
    resolve_config,
    to_decimal,
)
from qw_domain.rights import Use
from qw_domain.strategy_registry import (
    AssetClass,
    DataNeed,
    Family,
    Horizon,
    Parameter,
    ParamKind,
    StrategyError,
    StrategyManifest,
)
from qw_domain.valuation import Mark

__all__ = ["CAPEX", "CFO", "NI", "classification_at"]
STRATEGY_ID, VERSION = "STR-INCOME-001", "0.1.0-research"
METHOD = "distribution_sustainability/1"
DIVIDENDS = "PaymentsOfDividends"
YEAR = timedelta(days=365)
FLOWS = (NI, CFO, CAPEX, DIVIDENDS)


class DistKind(StrEnum):
    ORDINARY = "ordinary"
    SPECIAL = "special"  # special, extra or one-off


@dataclass(frozen=True, slots=True)
class Distribution:
    instrument_id: InstrumentId
    ex_date: date
    pay_date: date
    per_share: Money
    kind: DistKind
    return_of_capital: Money | None  # None: character not yet known
    known_at: datetime
    source: str
    assumed: bool = False  # a model assumption, not a declared distribution

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", ensure_aware_utc(self.known_at))
        amt, roc = self.per_share, self.return_of_capital
        bad = (
            amt.amount.value <= 0
            or self.pay_date < self.ex_date
            or type(self.kind) is not DistKind
            or not self.source
        )
        if roc is not None:
            bad = bad or roc.currency != amt.currency
            bad = bad or not 0 <= roc.amount.value <= amt.amount.value
        if bad:
            raise StrategyError("distribution", f"{self.instrument_id} {self.ex_date}")


class Verdict(StrEnum):
    SUSTAINED = "sustained"
    UNSUSTAINABLE = "unsustainable"
    NOT_ASSESSABLE = "not_assessable"
    NOT_APPLICABLE = "not_applicable"


class Flag(StrEnum):
    SPECIAL_INFLATES_HEADLINE = "special_inflates_headline"
    FCF_COVERAGE_BELOW_MIN = "fcf_coverage_below_min"
    EARNINGS_COVERAGE_BELOW_MIN = "earnings_coverage_below_min"
    DISTRIBUTION_CUT = "distribution_cut"
    RETURN_OF_CAPITAL = "return_of_capital"
    HEADLINE_UNSUSTAINABLE = "headline_unsustainable"


@dataclass(frozen=True, slots=True)
class IncomeInputs:
    as_of: datetime
    basis: KnowledgeBasis
    book: FactBook
    universe: tuple[InstrumentId, ...]
    classifications: tuple[Classification, ...]
    prices: Mapping[InstrumentId, Mark]
    price_max_age: timedelta
    distributions: tuple[Distribution, ...]
    history_start: Mapping[InstrumentId, date]  # records complete from this ex-date
    config: Mapping[str, object]
    config_hash: str  # of the adopted configuration

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "as_of", ensure_aware_utc(self.as_of))
        put(self, "universe", tuple(dict.fromkeys(self.universe)))
        for name in ("classifications", "distributions"):
            put(self, name, tuple(getattr(self, name)))
        for name in ("prices", "history_start", "config"):
            put(self, name, MappingProxyType(dict(getattr(self, name))))
        if any(m.instrument_id != i for i, m in self.prices.items()):
            raise StrategyError("prices", "keyed by their own instrument")


@dataclass(frozen=True, slots=True)
class IncomeResult:
    instrument_id: InstrumentId
    verdict: Verdict
    headline_yield: Decimal | None = None
    ordinary_yield: Decimal | None = None
    special_share: Decimal | None = None
    roc_share: Decimal | None = None
    announced_per_share: Decimal | None = None
    assumed_per_share: Decimal | None = None
    fcf_coverage: Decimal | None = None
    earnings_coverage: Decimal | None = None
    payout_fcf: Decimal | None = None
    payout_earnings: Decimal | None = None
    flags: tuple[Flag, ...] = ()
    reasons: tuple[str, ...] = ()
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class IncomeRun:
    method: str
    blocked: tuple[str, ...]
    issuers: tuple[IncomeResult, ...]  # by rank, then instrument id
    research_only: ClassVar[bool] = True

    @property
    def candidates(self) -> tuple[IncomeResult, ...]:
        return tuple(r for r in self.issuers if r.rank is not None)


def _dec(x: Fraction | None) -> Decimal | None:
    return None if x is None else to_decimal(x)


def _amt(rows: list[Distribution]) -> Fraction:
    return sum((Fraction(d.per_share.amount.value) for d in rows), Fraction(0))


@dataclass(frozen=True, slots=True)
class _Coverage:
    fcf: Fraction | None = None
    earnings: Fraction | None = None
    payout_fcf: Fraction | None = None
    payout_earnings: Fraction | None = None


def _coverage(
    view: AsOfView, c: Classification, t: date, years: int, max_age: int,
    why: list[str],
) -> _Coverage:  # fmt: skip
    ends = sorted(annual_series(view, c.cik, CFO, c.currency, True), reverse=True)
    if not ends:
        why.append(f"{CFO}:missing")
        return _Coverage()
    if (t - ends[0]).days > max_age:
        why.append(f"period_end_older_than_{max_age}_days")
        return _Coverage()
    ends = ends[:years]
    gaps = [(a - b).days for a, b in pairwise(ends)]
    if len(ends) < years or any(not FLOW_DAYS[0] <= g <= FLOW_DAYS[1] for g in gaps):
        why.append("lookback_incomplete")
        return _Coverage()
    ni = fcf = div = Fraction(0)
    for end in ends:
        state, v, problem = read_at(view, c.cik, c.currency, FLOWS, (), end)
        if state is not State.VALUE:
            why.append(problem)
            return _Coverage()
        if v[CAPEX] < 0:
            why.append("capex_negative")
            return _Coverage()
        ni, fcf, div = ni + v[NI], fcf + v[CFO] - v[CAPEX], div + v[DIVIDENDS]
    if div <= 0:
        why.append("no_dividends_in_filings")
        return _Coverage()
    if fcf <= 0:
        why.append("fcf_non_positive")
    if ni <= 0:
        why.append("earnings_non_positive")
    return _Coverage(
        Fraction(fcf, div), Fraction(ni, div),
        Fraction(div, fcf) if fcf > 0 else None, Fraction(div, ni) if ni > 0 else None,
    )  # fmt: skip


def _issuer(
    ins: IncomeInputs, i: InstrumentId, c: Classification, view: AsOfView,
    years: int, min_cov: Fraction, max_age: int,
) -> IncomeResult:  # fmt: skip
    t, today = ins.as_of, ins.as_of.date()
    why: list[str] = []
    flags: list[Flag] = []
    mine = [d for d in ins.distributions if d.instrument_id == i and d.known_at <= t]
    real = [d for d in mine if not d.assumed]
    trailing = [d for d in real if today - YEAR < d.ex_date <= today]
    prior = [d for d in real if today - 2 * YEAR < d.ex_date <= today - YEAR]
    ordinary = [d for d in trailing if d.kind is DistKind.ORDINARY]
    total, ords = _amt(trailing), _amt(ordinary)
    start = ins.history_start.get(i)
    complete = start is not None and start <= today - YEAR + timedelta(days=1)
    headline = ordinary_y = special = roc = None
    if not complete:
        why.append("distribution_history_incomplete")
    else:
        special = Fraction(total - ords, total) if total else Fraction(0)
        if total > ords:
            flags.append(Flag.SPECIAL_INFLATES_HEADLINE)
        if any(d.return_of_capital is None for d in trailing):
            why.append("return_of_capital_unknown")
        else:
            parts = [d.return_of_capital for d in trailing if d.return_of_capital]
            rocs = sum((Fraction(x.amount.value) for x in parts), Fraction(0))
            roc = Fraction(rocs, total) if total else Fraction(0)
            if rocs:
                flags.append(Flag.RETURN_OF_CAPITAL)
        assert start is not None
        if start > today - 2 * YEAR + timedelta(days=1):
            why.append("cut_history_incomplete")
        elif ords < _amt([d for d in prior if d.kind is DistKind.ORDINARY]):
            flags.append(Flag.DISTRIBUTION_CUT)
        m = ins.prices.get(i)
        if m is None or m.observed_at > t:
            why.append("price_missing")
        elif t - m.observed_at > ins.price_max_age:
            why.append("price_stale")
        elif any(d.per_share.currency != m.currency for d in trailing + prior):
            why.append("currency_mismatch")
        else:
            price = Fraction(m.price.value)
            headline, ordinary_y = Fraction(total, price), Fraction(ords, price)
    cov = _coverage(view, c, today, years, max_age, why)
    verdict = Verdict.NOT_ASSESSABLE
    if cov.fcf is not None and cov.earnings is not None:
        verdict = Verdict.SUSTAINED
        if cov.fcf < min_cov:
            flags.append(Flag.FCF_COVERAGE_BELOW_MIN)
        if cov.earnings < min_cov:
            flags.append(Flag.EARNINGS_COVERAGE_BELOW_MIN)
        if cov.fcf < min_cov or cov.earnings < min_cov:
            verdict = Verdict.UNSUSTAINABLE
    risky = {Flag.SPECIAL_INFLATES_HEADLINE, Flag.DISTRIBUTION_CUT}
    if verdict is Verdict.UNSUSTAINABLE or risky & set(flags):
        flags.append(Flag.HEADLINE_UNSUSTAINABLE)
    announced = [d for d in real if d.ex_date > today]
    assumed = [d for d in mine if d.assumed]
    return IncomeResult(
        i, verdict, _dec(headline), _dec(ordinary_y), _dec(special), _dec(roc),
        _dec(_amt(announced)), _dec(_amt(assumed)), _dec(cov.fcf), _dec(cov.earnings),
        _dec(cov.payout_fcf), _dec(cov.payout_earnings), tuple(flags), tuple(why),
    )  # fmt: skip


def evaluate(ins: IncomeInputs) -> IncomeRun:
    """Deterministic distribution sustainability research."""
    params, blocked = resolve_config(income_manifest(), ins.config, ins.config_hash)
    if params is None:
        return IncomeRun(METHOD, blocked, ())
    years, max_age = int(params["lookback_years"]), int(params["max_fact_age_days"])
    min_cov = param_fraction(params, "min_coverage")
    view = ins.book.as_of(ins.as_of, ins.basis)
    out: list[IncomeResult] = []
    for i in ins.universe:
        c = classification_at(ins.classifications, i, ins.as_of)
        if c is None:
            unknown = ("classification_unknown",)
            out.append(IncomeResult(i, Verdict.NOT_ASSESSABLE, reasons=unknown))
        elif c.issuer_class is not IssuerClass.GENERAL_CORPORATE:
            why = (f"{c.issuer_class.value}_unsupported",)
            out.append(IncomeResult(i, Verdict.NOT_APPLICABLE, reasons=why))
        else:
            out.append(_issuer(ins, i, c, view, years, min_cov, max_age))

    def eligible(r: IncomeResult) -> bool:
        cut = Flag.DISTRIBUTION_CUT in r.flags
        return (
            r.verdict is Verdict.SUSTAINED and not cut and r.headline_yield is not None
        )

    def key(r: IncomeResult) -> tuple[bool, Decimal, str]:
        cov = (
            r.fcf_coverage if eligible(r) and r.fcf_coverage is not None else Decimal(0)
        )
        return not eligible(r), -cov, r.instrument_id.to_wire()

    ranked = sorted(out, key=key)
    n = sum(1 for r in ranked if eligible(r))
    return IncomeRun(
        METHOD, (), tuple(replace(r, rank=k + 1) if k < n else r
                          for k, r in enumerate(ranked))
    )  # fmt: skip


@cache
def income_manifest(feed_id: str = "feed-config-check") -> StrategyManifest:
    """STR-INCOME-001 for `strategy_registry`; its code hash covers this module and
    the shared readers. Defaults are unqualified research choices."""
    params = (
        Parameter("lookback_years", ParamKind.INTEGER, 3, 1, 10),
        Parameter(
            "min_coverage", ParamKind.DECIMAL, Decimal(1), Decimal(0), Decimal(10)
        ),
        Parameter("max_fact_age_days", ParamKind.INTEGER, 456, 90, 1095),
    )
    here = Path(__file__)
    src = here.read_bytes() + here.with_name("quality_value.py").read_bytes()
    return StrategyManifest(
        STRATEGY_ID, VERSION, Family.INCOME, hashlib.sha256(src).hexdigest(),
        (DataNeed(feed_id, frozenset({Use.DERIVED_DATA})),),
        frozenset({AssetClass.STOCKS}), Horizon.LONG_TERM, False, params,
        "protocol-income-reference-1", None, "Distribution sustainability reference",
    )  # fmt: skip
