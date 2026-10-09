"""Sector-aware quality/value research reference STR-QUALITY-001 (T029 increment 1).

Spec §8 STR-QUALITY-001 and shared contract; R059, R072, R084. Research candidates
only: action eligibility stays false until qualification (`strategy_gate`).
- Inputs are point-in-time: reported facts come only from `FactBook.as_of(t, basis)`;
  the issuer classification is the latest one known at or before t; the market value
  must be observed at or before t and within `market_max_age`. Nothing later is read.
- General corporate recipe (`RECIPE`): annual (350-380 day) us-gaap facts in
  the issuer's reporting currency, all read at one period end, the latest annual end
  of the metric's first flow concept. Each metric declares the issuer classes it is
  valid for; this recipe covers `general_corporate` only, so banks, insurers, funds
  and pre-revenue issuers get `not_applicable` and no number (spec: separate recipes).
- A non-positive denominator (negative or zero earnings, FCF, revenue, invested
  capital, assets or EV) or negative capex makes a metric `undefined`, never a cheap
  multiple. A
  missing input is `missing`, a tied restatement `conflicted`, and a period end older
  than `max_fact_age_days` at t is `stale`: none of these is zero or imputed.
- Score: within one sector, among peers with a value, percentile rank
  (worse + ties/2) / (n - 1) in the metric's better direction (ranks need no
  winsorization). Fewer than `min_peers` values leave the metric unscored. The
  composite is the weight-averaged score over scored metrics; coverage = scored
  weight / total weight; below `min_coverage` there is no composite. Candidates rank
  within their sector by composite, ties by canonical instrument id.
- Parameters are unqualified research defaults, read only from a configuration whose
  hash matches the adopted one (all-zero weights are invalid). Exact arithmetic
  (`Fraction`), shown half-even 1e-18.
LIMITATIONS: one currency per issuer (no FX: another market-value currency is
`missing`); EV and invested capital use LongTermDebt only (approximate); no
dimensional/segment facts, NOPAT tax adjustment or ETF fund attributes.
Stdlib only.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from qw_domain.decimals import Money, safe_repr
from qw_domain.filings import AsOfView, FactBook, FactKey, KnowledgeBasis
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.rights import Use
from qw_domain.sources import ID_PATTERN
from qw_domain.strategy_registry import (
    AssetClass,
    DataNeed,
    Family,
    Horizon,
    Parameter,
    ParamKind,
    ParamValue,
    StrategyError,
    StrategyManifest,
)

STRATEGY_ID, VERSION = "STR-QUALITY-001", "0.1.0-research"
METHOD = "sector_rank_quality_value/1"
TAXONOMY = "us-gaap"
FLOW_DAYS = (350, 380)  # an annual duration, including 52/53-week fiscal years
REV, OI, NI = "Revenues", "OperatingIncomeLoss", "NetIncomeLoss"
CFO = "NetCashProvidedByUsedInOperatingActivities"
CAPEX = "PaymentsToAcquirePropertyPlantAndEquipment"
EQUITY, DEBT, ASSETS = "StockholdersEquity", "LongTermDebt", "Assets"
CASH = "CashAndCashEquivalentsAtCarryingValue"


class IssuerClass(StrEnum):
    GENERAL_CORPORATE = "general_corporate"
    BANK = "bank"
    INSURER = "insurer"
    FUND = "fund"
    PRE_REVENUE = "pre_revenue"


class State(StrEnum):
    VALUE = "value"
    NOT_APPLICABLE = "not_applicable"  # the recipe does not cover the issuer class
    UNDEFINED = "undefined"  # non-positive denominator
    MISSING = "missing"  # known missing at t
    STALE = "stale"
    CONFLICTED = "conflicted"


@dataclass(frozen=True, slots=True)
class Classification:
    """A sourced issuer classification, known from `known_at`."""

    instrument_id: InstrumentId
    cik: str
    sector_id: str  # the peer group for ranks
    issuer_class: IssuerClass
    currency: str  # reporting currency of the issuer's filings
    source: str
    known_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", ensure_aware_utc(self.known_at))
        if ID_PATTERN.fullmatch(self.sector_id) is None or not self.source:
            raise StrategyError("classification", safe_repr(self.sector_id))
        if type(self.issuer_class) is not IssuerClass:
            raise StrategyError("classification", safe_repr(self.issuer_class))


def classification_at(
    rows: Sequence[Classification], i: InstrumentId, t: datetime
) -> Classification | None:
    """The latest classification known at t; tied different records are unknown."""
    known = [c for c in rows if c.instrument_id == i and c.known_at <= t]
    if not known:
        return None
    last = max(c.known_at for c in known)
    tied = {c for c in known if c.known_at == last}
    return tied.pop() if len(tied) == 1 else None


def annual_series(
    view: AsOfView, cik: str, concept: str, currency: str, flow: bool
) -> dict[date, Fraction | State]:
    """Values by period end: annual durations (flow) or instants (balance)."""

    def ok(key: FactKey) -> bool:
        c, tax, name, unit, p = key
        if (c, tax, name, unit) != (cik, TAXONOMY, concept, currency):
            return False
        if p.start is None:
            return not flow
        return flow and FLOW_DAYS[0] <= (p.end - p.start).days <= FLOW_DAYS[1]

    out: dict[date, Fraction | State] = {}
    for key in sorted(view.conflicted):
        if ok(key):
            out[key[4].end] = State.CONFLICTED
    for key, fact in view.facts.items():
        if ok(key):
            end = key[4].end
            out[end] = State.CONFLICTED if end in out else Fraction(fact.value)
    return out


def read_at(
    view: AsOfView, cik: str, currency: str, flows: Sequence[str],
    stocks: Sequence[str], end: date,
) -> tuple[State, dict[str, Fraction], str]:  # fmt: skip
    """Every concept at exactly `end`, or the first problem."""
    vals: dict[str, Fraction] = {}
    for concept, flow in [(c, True) for c in flows] + [(c, False) for c in stocks]:
        v = annual_series(view, cik, concept, currency, flow).get(end)
        if not isinstance(v, Fraction):
            return v or State.MISSING, {}, f"{concept}@{end.isoformat()}"
        vals[concept] = v
    return State.VALUE, vals, ""


def to_decimal(x: Fraction) -> Decimal:
    """Display value, half-even at 1e-18."""
    return Decimal(round(x * 10**18)).scaleb(-18)


def resolve_config(
    m: StrategyManifest, config: Mapping[str, object], adopted_hash: str
) -> tuple[dict[str, ParamValue] | None, tuple[str, ...]]:
    try:
        params, digest = m.resolve(config), m.config_hash(config)
    except StrategyError as exc:
        return None, (f"config_invalid:{exc.code}",)
    return (params, ()) if digest == adopted_hash else (None, ("config_mismatch",))


def param_fraction(params: Mapping[str, ParamValue], name: str) -> Fraction:
    v = params[name]
    assert isinstance(v, Decimal)  # a DECIMAL parameter, checked by resolve
    return Fraction(v)


Formula = Callable[[Mapping[str, Fraction], Fraction], Fraction | str]


def _ratio(num: Fraction, den: Fraction, why: str) -> Fraction | str:
    return Fraction(num, den) if den > 0 else why


def _fcf(v: Mapping[str, Fraction]) -> Fraction | str:
    """Negative capex (a net inflow as tagged) would inflate FCF: undefined."""
    return "capex_negative" if v[CAPEX] < 0 else v[CFO] - v[CAPEX]


def _conversion(v: Mapping[str, Fraction], _: Fraction) -> Fraction | str:
    fcf = _fcf(v)
    return fcf if isinstance(fcf, str) else _ratio(fcf, v[NI], "earnings_non_positive")


def _ev_yield(v: Mapping[str, Fraction], mv: Fraction) -> Fraction | str:
    if v[OI] <= 0:
        return "earnings_non_positive"
    return _ratio(v[OI], mv + v[DEBT] - v[CASH], "ev_non_positive")


def _fcf_yield(v: Mapping[str, Fraction], mv: Fraction) -> Fraction | str:
    fcf = _fcf(v)
    if isinstance(fcf, str) or fcf <= 0:
        return fcf if isinstance(fcf, str) else "fcf_non_positive"
    return _ratio(fcf, mv, "market_value_non_positive")


@dataclass(frozen=True, slots=True)
class MetricDef:
    name: str
    flows: tuple[str, ...]  # the first one anchors the period end
    stocks: tuple[str, ...]
    uses_market_value: bool
    higher_is_better: bool
    valid_for: frozenset[IssuerClass]
    formula: Formula


_GEN = frozenset({IssuerClass.GENERAL_CORPORATE})
RECIPE: tuple[MetricDef, ...] = (
    MetricDef("operating_margin", (OI, REV), (), False, True, _GEN,
              lambda v, _: _ratio(v[OI], v[REV], "revenue_non_positive")),
    MetricDef("roic_pretax", (OI,), (EQUITY, DEBT), False, True, _GEN,
              lambda v, _: _ratio(v[OI], v[EQUITY] + v[DEBT],
                                  "invested_capital_non_positive")),
    MetricDef("cash_conversion", (NI, CFO, CAPEX), (), False, True, _GEN,
              _conversion),
    MetricDef("debt_to_assets", (OI,), (DEBT, ASSETS), False, False, _GEN,
              lambda v, _: _ratio(v[DEBT], v[ASSETS], "assets_non_positive")),
    MetricDef("operating_earnings_to_ev", (OI,), (DEBT, CASH), True, True, _GEN,
              _ev_yield),
    MetricDef("fcf_yield", (CFO, CAPEX), (), True, True, _GEN, _fcf_yield),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class MarketValue:
    """Equity market value (price x shares outstanding) observed at `observed_at`."""

    instrument_id: InstrumentId
    amount: Money
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_aware_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class QualityInputs:
    as_of: datetime
    basis: KnowledgeBasis
    book: FactBook
    universe: tuple[InstrumentId, ...]  # point-in-time membership at as_of
    classifications: tuple[Classification, ...]
    market_values: Mapping[InstrumentId, MarketValue]
    market_max_age: timedelta
    config: Mapping[str, object]
    config_hash: str  # of the adopted configuration

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "as_of", ensure_aware_utc(self.as_of))
        put(self, "universe", tuple(dict.fromkeys(self.universe)))
        put(self, "classifications", tuple(self.classifications))
        put(self, "market_values", MappingProxyType(dict(self.market_values)))
        put(self, "config", MappingProxyType(dict(self.config)))
        if any(m.instrument_id != i for i, m in self.market_values.items()):
            raise StrategyError("market_values", "keyed by their own instrument")


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    state: State
    value: Decimal | None
    score: Decimal | None  # within-sector percentile; None when unscored
    period_end: date | None
    reason: str


class Status(StrEnum):
    CANDIDATE = "research_candidate"
    INSUFFICIENT_COVERAGE = "insufficient_coverage"
    NOT_APPLICABLE = "not_applicable"
    CLASSIFICATION_UNKNOWN = "classification_unknown"


@dataclass(frozen=True, slots=True)
class IssuerResult:
    instrument_id: InstrumentId
    sector_id: str | None
    issuer_class: IssuerClass | None
    status: Status
    metrics: tuple[Metric, ...]
    composite: Decimal | None
    coverage: Decimal | None
    rank_in_sector: int | None


@dataclass(frozen=True, slots=True)
class QualityResult:
    method: str
    blocked: tuple[str, ...]
    issuers: tuple[IssuerResult, ...]  # by sector, rank, instrument id
    research_only: ClassVar[bool] = True  # never an order or an actionable proposal

    @property
    def candidates(self) -> tuple[IssuerResult, ...]:
        return tuple(r for r in self.issuers if r.status is Status.CANDIDATE)


_Raw = tuple[State, Fraction | None, date | None, str]


def _measure(
    d: MetricDef, c: Classification, view: AsOfView, ins: QualityInputs, max_age: int
) -> _Raw:
    if c.issuer_class not in d.valid_for:
        return State.NOT_APPLICABLE, None, None, f"{c.issuer_class.value}_unsupported"
    ends = annual_series(view, c.cik, d.flows[0], c.currency, True)
    if not ends:
        return State.MISSING, None, None, d.flows[0]
    end = max(ends)
    if (ins.as_of.date() - end).days > max_age:
        return State.STALE, None, end, f"period_end_older_than_{max_age}_days"
    state, vals, why = read_at(view, c.cik, c.currency, d.flows, d.stocks, end)
    if state is not State.VALUE:
        return state, None, end, why
    mv = Fraction(0)
    if d.uses_market_value:
        m = ins.market_values.get(c.instrument_id)
        if m is None:
            return State.MISSING, None, end, "market_value"
        if m.amount.currency != c.currency:
            return State.MISSING, None, end, "market_value_currency"
        age = ins.as_of - m.observed_at
        if age < timedelta(0):  # not yet observed at t: unknown, as if absent
            return State.MISSING, None, end, "market_value"
        if age > ins.market_max_age:
            return State.STALE, None, end, "market_value"
        mv = Fraction(m.amount.amount.value)
    out = d.formula(vals, mv)
    if isinstance(out, str):
        return State.UNDEFINED, None, end, out
    return State.VALUE, out, end, ""


def _percentiles(
    rows: Mapping[InstrumentId, Fraction], higher: bool
) -> dict[InstrumentId, Fraction]:
    n = len(rows)
    out = {}
    for i, v in rows.items():
        worse = sum(1 for u in rows.values() if (u < v if higher else u > v))
        ties = sum(1 for u in rows.values() if u == v) - 1
        out[i] = Fraction(2 * worse + ties, 2 * (n - 1))
    return out


def _issuer(
    i: InstrumentId, c: Classification, raw: Mapping[str, _Raw],
    score: Mapping[tuple[InstrumentId, str], Fraction],
    weight: Mapping[str, Fraction], min_cov: Fraction,
) -> IssuerResult:  # fmt: skip
    metrics, total, got, acc = [], Fraction(0), Fraction(0), Fraction(0)
    for d in RECIPE:
        st, v, end, why = raw[d.name]
        s = score.get((i, d.name))
        if st is not State.NOT_APPLICABLE:
            total += weight[d.name]
        if s is not None:
            got, acc = got + weight[d.name], acc + weight[d.name] * s
        elif st is State.VALUE:
            why = "peer_group_below_min_peers"
        val, sc = (None if x is None else to_decimal(x) for x in (v, s))
        metrics.append(Metric(d.name, st, val, sc, end, why))
    cov = Fraction(got, total) if total else None
    status, comp = Status.CANDIDATE, None
    if cov is None:
        status = Status.NOT_APPLICABLE
    elif got == 0 or cov < min_cov:
        status = Status.INSUFFICIENT_COVERAGE
    else:
        comp = to_decimal(Fraction(acc, got))
    cov_d = None if cov is None else to_decimal(cov)
    return IssuerResult(
        i, c.sector_id, c.issuer_class, status, tuple(metrics), comp, cov_d, None
    )


def evaluate(ins: QualityInputs) -> QualityResult:
    """Deterministic sector-aware quality/value research ranks."""
    params, blocked = resolve_config(quality_manifest(), ins.config, ins.config_hash)
    if params is None:
        return QualityResult(METHOD, blocked, ())
    max_age, min_peers = int(params["max_fact_age_days"]), int(params["min_peers"])
    weight = {d.name: param_fraction(params, f"weight_{d.name}") for d in RECIPE}
    if not any(weight.values()):
        return QualityResult(METHOD, ("config_invalid:weights",), ())
    view = ins.book.as_of(ins.as_of, ins.basis)
    t, results = ins.as_of, []
    known = {
        i: c
        for i in ins.universe
        if (c := classification_at(ins.classifications, i, t))
    }
    raw = {
        i: {d.name: _measure(d, c, view, ins, max_age) for d in RECIPE}
        for i, c in known.items()
    }
    score: dict[tuple[InstrumentId, str], Fraction] = {}
    for sector in {c.sector_id for c in known.values()}:
        peers = [i for i, c in known.items() if c.sector_id == sector]
        for d in RECIPE:
            vals = {i: v for i in peers if (v := raw[i][d.name][1]) is not None}
            if len(vals) >= min_peers:
                for i, s in _percentiles(vals, d.higher_is_better).items():
                    score[i, d.name] = s
    min_cov = param_fraction(params, "min_coverage")
    for i in ins.universe:
        if (c := known.get(i)) is None:
            unknown = Status.CLASSIFICATION_UNKNOWN
            results.append(IssuerResult(i, None, None, unknown, (), None, None, None))
        else:
            results.append(_issuer(i, c, raw[i], score, weight, min_cov))
    return QualityResult(METHOD, (), _ranked(results))


def _ranked(results: list[IssuerResult]) -> tuple[IssuerResult, ...]:
    def key(r: IssuerResult) -> tuple[str, int, Decimal, str]:
        comp = r.composite
        tail = r.instrument_id.to_wire()
        return r.sector_id or "", comp is None, -(comp or Decimal(0)), tail

    rank: dict[str, int] = {}
    out = []
    for r in sorted(results, key=key):
        if r.status is Status.CANDIDATE and r.sector_id is not None:
            rank[r.sector_id] = rank.get(r.sector_id, 0) + 1
            r = replace(r, rank_in_sector=rank[r.sector_id])
        out.append(r)
    return tuple(out)


@cache
def quality_manifest(feed_id: str = "feed-config-check") -> StrategyManifest:
    """STR-QUALITY-001 for `strategy_registry`; its code hash is this module's source.
    Every default is an unqualified research choice (the spec gives no values)."""
    weights = tuple(
        Parameter(
            f"weight_{d.name}", ParamKind.DECIMAL, Decimal(1), Decimal(0), Decimal(10)
        )
        for d in RECIPE
    )
    params = (
        Parameter("max_fact_age_days", ParamKind.INTEGER, 456, 90, 1095),
        Parameter("min_peers", ParamKind.INTEGER, 3, 2, 1000),
        Parameter(
            "min_coverage", ParamKind.DECIMAL, Decimal("0.5"), Decimal(0), Decimal(1)
        ),
        *weights,
    )
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return StrategyManifest(
        STRATEGY_ID, VERSION, Family.QUALITY_VALUE, code,
        (DataNeed(feed_id, frozenset({Use.DERIVED_DATA})),),
        frozenset({AssetClass.STOCKS}), Horizon.LONG_TERM, False, params,
        "protocol-quality-reference-1", None, "Sector-aware quality/value reference",
    )  # fmt: skip
