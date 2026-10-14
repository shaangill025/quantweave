"""Research/retrieval improvement: extraction, retrieval and prompt candidates with
controlled evaluation (T046 increment 1; spec §11 research/retrieval row, §10, §15;
R028, R029, R033, R040, R084). Domain only, stdlib only.
- `ResearchConfig` is the closed set of research-pipeline knobs: the publication-date
  and value extraction rules, the retrieval match and ranking, `top_k` (1 to 5) and
  the prompt template (from `PROMPTS`, the templates the researcher ships). Nothing
  else can be expressed: the evaluator, risk policy, protocol thresholds, rights and
  freshness gates, budgets, tools and answer keys are not knobs. Any other payload
  key is refused, `widens_authority` when it names one of those, else `not_adaptable`.
- `propose` (rules v0) reads only a development fixture: for each extraction knob it
  keeps the registered rule that reads the most development documents correctly.
- `measure` is the assessor's controlled evaluation on a frozen promotion fixture of
  labelled documents and point-in-time cases. The fixture's hash is the T032 plan's
  `protocol_hash`, frozen in the experiment protocol, so an edited answer key is
  `answer_key_changed`. For the baseline and the candidate it extracts each document,
  ingests it through the T036 evidence store, retrieves passages published inside
  the case window by the extracted date, and runs the real T038 researcher (gateway,
  snapshot, tools, strict claim checks) on them. Against the answer key it counts
  publication dates and values read correctly, correct answers or abstentions, false
  facts (a supported claim whose value is not the case answer), false references
  (a cited passage the key does not list), look-ahead (a retrieved passage truly
  published after the case time), model calls and cost. A payload edited around the
  constructor counts `scope_widening` or `payload_invalid`, and one that is not the
  artifact's full canonical payload `artifact_mismatch`. Failed research runs
  (gateway, output, budget, step limit) are `run_failures`, a hard control: a
  model cannot pass by failing. A fixture must hold look-ahead and wrong-period
  traps that a registered date rule would retrieve. The numbers reach a
  verdict only as the assessor's T032 promotion trial read by T044 `assess`.
- Promotion is T051's: maintainer release, then operator install. `config_of` reads
  the configuration a signed release names and checks it is the assessed artifact.
LIMITATIONS: one prompt template exists, so prompt variation is not yet measurable;
the researcher books evaluation calls under the research budget category, not the
improvement one; documents are a fixed header format; no persistence.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from types import MappingProxyType

from qw_domain import improvement as im
from qw_domain import researcher as rs
from qw_domain.budget import BudgetBook
from qw_domain.decimals import DOMAIN_CONTEXT, UsdBudget, safe_repr
from qw_domain.evidence import (
    _NUMBER,
    _SEP,
    ClaimKind,
    EvidenceStore,
    Reading,
    SourceKind,
)
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights
from qw_domain.instants import ensure_aware_utc
from qw_domain.release import SignedRelease
from qw_domain.research_data import EvidenceClass, digest
from qw_domain.strategy_registry import StrategyManifest
from qw_domain.trial_ledger import SplitPlan


class ResearchImprovementError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


_E = ResearchImprovementError


class DateRule(StrEnum):
    UPDATED_ELSE_PUBLISHED = "updated_else_published"  # v0
    PUBLISHED = "published"
    FIRST_DATE = "first_date"


class ValueRule(StrEnum):
    FIRST_NUMBER = "first_number"  # v0
    LAST_NUMBER = "last_number"


class Match(StrEnum):  # reading fields a passage must share with the question
    ATTRIBUTE_PERIOD = "attribute_period"  # v0
    ATTRIBUTE = "attribute"


class Ranking(StrEnum):
    PRIMARY_FIRST = "primary_first"  # v0: primary sources, then most recent
    RECENCY = "recency"


PROMPTS = frozenset({rs.TEMPLATE})
MAX_TOP_K = 5
_KNOBS = (("extract.publication_date", "date_rule", DateRule),
          ("extract.value", "value_rule", ValueRule),
          ("retrieval.match", "match", Match),
          ("retrieval.ranking", "ranking", Ranking))  # fmt: skip
ADAPTABLE = frozenset(
    {k for k, _, _ in _KNOBS} | {"retrieval.top_k", "prompt.template"}
)
# Fragments marking a key as an attempt to reach a protected control.
_WIDENING = ("evaluat", "assess", "risk", "polic", "threshold", "scorecard", "answer",
             "holdout", "split", "right", "fresh", "max_age", "stale", "budget",
             "cost", "approv", "release", "sign", "tool", "consent", "tenant",
             "scope", "order", "broker", "secret", "key")  # fmt: skip
RESEARCH_HARD = ("false_references", "lookahead_uses", "scope_widening",
                 "payload_invalid", "run_failures", "artifact_mismatch")  # fmt: skip


def _refuse(key: object) -> _E:
    k = key.lower().replace("-", "_") if isinstance(key, str) else ""
    return _E("widens_authority" if any(w in k for w in _WIDENING) else "not_adaptable",
              safe_repr(key))  # fmt: skip


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    date_rule: DateRule = DateRule.UPDATED_ELSE_PUBLISHED
    value_rule: ValueRule = ValueRule.FIRST_NUMBER
    match: Match = Match.ATTRIBUTE_PERIOD
    ranking: Ranking = Ranking.PRIMARY_FIRST
    top_k: int = 2
    prompt: str = rs.TEMPLATE

    def __post_init__(self) -> None:
        for _, name, kind in _KNOBS:
            if type(getattr(self, name)) is not kind:
                raise TypeError(f"{name} must be a {kind.__name__}")
        if type(self.top_k) is not int or not 1 <= self.top_k <= MAX_TOP_K:
            raise _E("value", "retrieval.top_k")
        if self.prompt not in PROMPTS:
            raise _E("value", "prompt.template")

    def to_payload(self) -> dict[str, str]:
        out = {k: getattr(self, n).value for k, n, _ in _KNOBS}
        extra = {"retrieval.top_k": str(self.top_k), "prompt.template": self.prompt}
        return out | extra

    @classmethod
    def from_payload(cls, payload: Mapping[str, str]) -> "ResearchConfig":
        """Strict, canonical parse; absent knobs keep their v0 values."""
        for k, v in payload.items():
            if k not in ADAPTABLE:
                raise _refuse(k)
            if not isinstance(v, str):
                raise _E("value", safe_repr(k))
        args: dict[str, object] = {}
        for key, name, kind in _KNOBS:
            if key in payload:
                if payload[key] not in {e.value for e in kind}:
                    raise _E("value", key)
                args[name] = kind(payload[key])
        if (n := payload.get("retrieval.top_k")) is not None:
            if not (len(n) == 1 and n.isascii() and n.isdigit()):
                raise _E("value", "retrieval.top_k")
            args["top_k"] = int(n)
        if (t := payload.get("prompt.template")) is not None:
            args["prompt"] = t
        return cls(**args)  # type: ignore[arg-type]

    @property
    def content_hash(self) -> str:
        return digest(self.to_payload())


_HEADER = re.compile(r"(?P<label>[A-Za-z-]+): (?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})")


def _header(text: str) -> list[tuple[str, date]]:
    out = []
    for line in text.split("\n"):
        if (m := _HEADER.fullmatch(line)) is not None:
            try:
                out.append((m["label"].lower(), date.fromisoformat(m["day"])))
            except ValueError:
                continue  # not a calendar date: unread, never guessed
    return out


def extract_date(rule: DateRule, text: str) -> date | None:
    """The publication date `rule` reads from the document header, or None."""
    h = _header(text)
    labelled = {label: d for label, d in reversed(h)}  # first occurrence wins
    if rule is DateRule.FIRST_DATE:
        return h[0][1] if h else None
    if rule is DateRule.UPDATED_ELSE_PUBLISHED and "updated" in labelled:
        return labelled["updated"]
    return labelled.get("published")


def extract_value(rule: ValueRule, text: str) -> tuple[str, Decimal] | None:
    """The statement (last non-header line) and the number token `rule` picks."""
    lines = [x for x in text.split("\n") if x.strip() and not _HEADER.fullmatch(x)]
    if not lines:
        return None
    quote = lines[-1]
    tokens = [Decimal(re.sub(_SEP, "", m.group())) for m in _NUMBER.finditer(quote)]
    if not tokens:
        return None
    return quote, tokens[0] if rule is ValueRule.FIRST_NUMBER else tokens[-1]


class Split(StrEnum):
    DEVELOPMENT = "development"  # producers may read these answer keys
    PROMOTION = "promotion"  # the assessor's protected fixture


@dataclass(frozen=True, slots=True)
class Doc:  # a raw fixture document with its answer key; SYNTHETIC unless stated
    doc_id: str
    feed_id: str
    kind: SourceKind
    entity: InstrumentId
    attribute: str
    period: Period
    unit: str
    text: str
    published_on: date  # answer key
    value: Decimal  # answer key

    @property
    def published_at(self) -> datetime:
        return _midnight(self.published_on)


@dataclass(frozen=True, slots=True)
class Case:  # a point-in-time question with its answer key
    case_id: str
    entity: InstrumentId
    attribute: str
    period: Period
    as_of: datetime
    max_age: timedelta
    answer: Decimal | None  # None: abstaining is the correct outcome
    refs: frozenset[str]  # documents that correctly support the answer

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", ensure_aware_utc(self.as_of))
        object.__setattr__(self, "refs", frozenset(self.refs))
        if self.answer is not None and type(self.answer) is not Decimal:
            raise TypeError("answer must be a Decimal or None")


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    split: Split
    evidence_class: EvidenceClass  # R040: never counted as live evidence
    tenant_id: str
    loaded_at: datetime
    evaluated_at: datetime
    docs: tuple[Doc, ...]
    cases: tuple[Case, ...]

    def __post_init__(self) -> None:
        for n in ("loaded_at", "evaluated_at"):
            object.__setattr__(self, n, ensure_aware_utc(getattr(self, n)))
        if not all(type(d.value) is Decimal for d in self.docs):
            raise TypeError("answer-key values must be Decimals")
        if self.loaded_at > self.evaluated_at:
            raise _E("time_order", self.fixture_id)

    @property
    def content_hash(self) -> str:
        def wire(x: object) -> object:
            if isinstance(x, (Doc, Case)):
                return [wire(getattr(x, f.name)) for f in fields(x)]
            if isinstance(x, (tuple, frozenset)):
                items = [wire(i) for i in x]
                return sorted(items, key=str) if isinstance(x, frozenset) else items
            if isinstance(x, InstrumentId):
                return x.to_wire()
            if isinstance(x, Period):
                return [str(x.start), str(x.end)]
            return x if x is None or isinstance(x, (str, int)) else str(x)

        return digest([wire(getattr(self, f.name)) for f in fields(self)])


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def propose(dev: Fixture, current: ResearchConfig) -> ResearchConfig:
    """Rules v0: per extraction knob, the registered rule reading the most
    development documents correctly; ties keep the current rule."""
    if dev.split is not Split.DEVELOPMENT:
        raise _E("not_development", dev.fixture_id)

    def dates(r: DateRule) -> int:
        return sum(extract_date(r, d.text) == d.published_on for d in dev.docs)

    def values(r: ValueRule) -> int:
        return sum((extract_value(r, d.text) or ("", None))[1] == d.value
                   for d in dev.docs)  # fmt: skip

    dr = max(DateRule, key=lambda r: (dates(r), r is current.date_rule))
    vr = max(ValueRule, key=lambda r: (values(r), r is current.value_rule))
    return replace(current, date_rule=dr, value_rule=vr)


def scorecard(scorecard_id: str, max_cost_usd: Decimal, max_calls: int) -> im.Scorecard:
    """Hard controls at zero; goals: more publication dates read correctly, no fewer
    correct answers than the baseline, cost and model-call (latency) bounds."""
    t, d, z = im.Threshold, im.Direction, Decimal(0)
    hard = [*im.HARD_CONTROLS[im.Target.RESEARCH], *RESEARCH_HARD]
    return im.Scorecard(scorecard_id, im.Target.RESEARCH, (
        *(t(m, d.AT_MOST, z, True) for m in hard),
        t("pub_date_gain", d.AT_LEAST, Decimal(1), False),
        t("answer_change", d.AT_LEAST, z, False),
        t("cost_usd", d.AT_MOST, max_cost_usd, False),
        t("model_calls", d.AT_MOST, Decimal(max_calls), False),
    ))  # fmt: skip


def check_protocol(p: im.Protocol) -> None:
    if p.target is not im.Target.RESEARCH:
        raise _E("not_research", p.experiment_id)
    held = {t.metric for t in p.scorecard.thresholds
            if t.hard and t.bound == 0
            and t.direction is im.Direction.AT_MOST}  # fmt: skip
    if not held.issuperset(RESEARCH_HARD):
        raise _E("control_missing", p.experiment_id)


def candidate(
    candidate_id: str, protocol: im.Protocol, parent_id: str, cfg: ResearchConfig,
    runtime_versions: Mapping[str, str], created_at: datetime,
) -> im.Candidate:  # fmt: skip
    check_protocol(protocol)
    return im.Candidate(candidate_id, protocol.experiment_id, protocol.target,
                        parent_id, protocol.producer, cfg.content_hash,
                        cfg.to_payload(), runtime_versions, (), created_at)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Harness:  # the assessor's research wiring: T037 gateway via the T038 engine
    workflow: StrategyManifest  # an approved ai_thesis version
    engine: rs.Engine
    max_cost: UsdBudget  # per case mandate
    retain_for: timedelta


@dataclass(slots=True)
class _Tally:
    counts: dict[str, int] = field(default_factory=dict)
    cost: Decimal = Decimal(0)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


_COUNTS = ("pub_date_correct", "value_correct", "answers_correct",
           "unsupported_material_facts", "false_references", "lookahead_uses",
           "model_calls", "run_failures")  # fmt: skip
_FAILED = frozenset({rs.Why.POLICY_VIOLATION, rs.Why.OUTPUT_INVALID,
                     rs.Why.GATEWAY_REFUSED, rs.Why.BUDGET, rs.Why.MODEL_FAILED,
                     rs.Why.STEP_LIMIT})  # fmt: skip


def _evaluate(
    cfg: ResearchConfig, fx: Fixture, h: Harness, book: BudgetBook, run_id: str
) -> tuple[_Tally, BudgetBook]:
    t = _Tally({n: 0 for n in _COUNTS})
    store, ctx = EvidenceStore(), h.engine.ctx
    dated: dict[str, datetime] = {}
    for d in fx.docs:
        day, got = (
            extract_date(cfg.date_rule, d.text),
            extract_value(cfg.value_rule, d.text),
        )
        t.add("pub_date_correct", day == d.published_on)
        t.add("value_correct", got is not None and got[1] == d.value)
        if day is None or got is None:
            continue  # unread documents are never ingested with a guess
        r = IngestRights(ctx.registry, fx.tenant_id, d.feed_id, fx.loaded_at,
                         ctx.scope, ctx.jurisdiction, h.retain_for)  # fmt: skip
        store.add_source(r, d.doc_id, d.kind, d.text, _midnight(day))
        reading = Reading(d.entity, d.attribute, d.period, d.unit, got[1])
        store.add_passage(
            fx.tenant_id,
            f"p-{d.doc_id}",
            d.doc_id,
            d.text.rindex(got[0]),
            got[0],
            reading,
            fx.loaded_at,
        )
        dated[d.doc_id] = _midnight(day)
    for c in fx.cases:
        lo = c.as_of - c.max_age
        hits = [d for d in fx.docs if d.doc_id in dated
                and (d.entity, d.attribute) == (c.entity, c.attribute)
                and (cfg.match is Match.ATTRIBUTE or d.period == c.period)
                and lo <= dated[d.doc_id] <= c.as_of]  # fmt: skip
        first = cfg.ranking is Ranking.PRIMARY_FIRST
        hits.sort(key=lambda d: (first and d.kind is not SourceKind.PRIMARY,
                                 -dated[d.doc_id].toordinal(), d.doc_id))  # fmt: skip
        top = hits[: cfg.top_k]
        t.add("lookahead_uses", sum(d.published_at > c.as_of for d in top))
        m = rs.Mandate(f"{run_id}-{c.case_id}", fx.tenant_id, h.workflow, c.entity,
                       f"SYNTHETIC fixture case {c.case_id}",
                       (rs.Need(c.entity, c.attribute),),
                       fx.evaluated_at - lo, tuple(f"p-{d.doc_id}" for d in top),
                       frozenset(rs.Tool), 4, h.max_cost)  # fmt: skip
        res = rs.research(m, store, h.engine, book, fx.evaluated_at)
        book = res.book
        t.add("model_calls", len(res.record.runs))
        with localcontext(DOMAIN_CONTEXT):
            t.cost += sum((r.cost.value for r in res.record.runs if r.cost),
                          Decimal(0))  # fmt: skip
        out = res.outcome
        if isinstance(out, rs.Abstention):
            t.add("answers_correct", c.answer is None and out.why not in _FAILED)
            t.add("false_references", out.why is rs.Why.UNSUPPORTED_CLAIMS)
            t.add("run_failures", out.why in _FAILED)
            continue
        facts = [x for x in out.claims if x.kind is ClaimKind.REPORTED_FACT]
        cited = {e.removeprefix("p-") for x in out.claims for e in x.evidence_ids}
        wrong = sum(x.value != c.answer for x in facts)
        bad_refs = len(cited - c.refs)
        t.add("unsupported_material_facts", wrong)
        t.add("false_references", bad_refs)
        t.add("answers_correct", c.answer is not None and not wrong and not bad_refs)
    return t, book


def _check_fixture(fx: Fixture) -> None:
    def same(d: Doc, c: Case) -> bool:
        return (d.entity, d.attribute) == (c.entity, c.attribute)

    def inside(at: datetime, c: Case) -> bool:
        return c.as_of - c.max_age <= at <= c.as_of

    def reachable(d: Doc, c: Case) -> bool:  # some registered rule retrieves it
        days = (extract_date(r, d.text) for r in DateRule)
        return any(x is not None and inside(_midnight(x), c) for x in days)

    pairs = [(d, c) for d in fx.docs for c in fx.cases if same(d, c)]
    if not any(d.period == c.period and d.published_at > c.as_of
               and reachable(d, c) for d, c in pairs):  # fmt: skip
        raise _E("fixture_no_lookahead_trap", fx.fixture_id)
    if not any(d.period != c.period and inside(d.published_at, c)
               for d, c in pairs):  # fmt: skip
        raise _E("fixture_no_reference_trap", fx.fixture_id)


def measure(
    protocol: im.Protocol, plan: SplitPlan, candidate: im.Candidate,
    baseline: ResearchConfig, fixture: Fixture, harness: Harness, book: BudgetBook,
    run_id: str,
) -> tuple[Mapping[str, Decimal], BudgetBook]:  # fmt: skip
    """The assessor's metrics for `candidate` against the frozen baseline."""
    check_protocol(protocol)
    if (candidate.experiment_id, candidate.target) != (
        protocol.experiment_id, protocol.target
    ):  # fmt: skip
        raise _E("candidate_experiment", candidate.candidate_id)
    if im.plan_digest(plan) != protocol.plan_digest:
        raise _E("test_access_changed", plan.plan_id)
    if fixture.content_hash != plan.protocol_hash:
        raise _E("answer_key_changed", fixture.fixture_id)
    if fixture.split is not Split.PROMOTION:
        raise _E("not_promotion", fixture.fixture_id)
    if fixture.evidence_class is not plan.dataset.evidence_class:
        raise _E("evidence_class", fixture.evidence_class.value)
    if baseline.content_hash != protocol.baseline_hash:
        raise _E("baseline_hash", protocol.experiment_id)
    _check_fixture(fixture)
    stray = [k for k in candidate.payload if k not in ADAPTABLE]
    good, invalid = {}, 0
    for k, v in candidate.payload.items():
        if k in ADAPTABLE:
            try:
                ResearchConfig.from_payload({k: v})
                good[k] = v
            except ResearchImprovementError:
                invalid += 1
    cfg = ResearchConfig.from_payload(good)
    # The scored payload must be the artifact's full canonical payload.
    bound = good == cfg.to_payload() and cfg.content_hash == candidate.artifact_hash
    base, book = _evaluate(baseline, fixture, harness, book, f"{run_id}-base")
    new, book = _evaluate(cfg, fixture, harness, book, f"{run_id}-cand")
    n, b = new.counts, base.counts
    out = {k: Decimal(v) for k, v in n.items()} | {
        "scope_widening": Decimal(len(stray)), "payload_invalid": Decimal(invalid),
        "artifact_mismatch": Decimal(int(not bound)),
        "pub_date_gain": Decimal(n["pub_date_correct"] - b["pub_date_correct"]),
        "answer_change": Decimal(n["answers_correct"] - b["answers_correct"]),
        "cost_usd": new.cost,
    }  # fmt: skip
    return MappingProxyType(out), book


def config_of(reg: im.ExperimentRegistry, sr: SignedRelease) -> ResearchConfig:
    """The research configuration a signed T051 release names, checked against the
    assessed candidate's artifact hash. Reading it installs nothing."""
    m = sr.manifest
    if m.target is not im.Target.RESEARCH:
        raise _E("not_research", m.release_id)
    c = reg.candidate(m.candidate_id)
    cfg = ResearchConfig.from_payload(c.payload)
    if not cfg.content_hash == c.artifact_hash == m.artifact_hash:
        raise _E("artifact_mismatch", c.candidate_id)
    return cfg
