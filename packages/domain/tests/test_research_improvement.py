"""Research/retrieval improvement (T046 increment 1). All data is SYNTHETIC: documents,
dates, values, answer keys, tenant, feeds, routes, keys and the model. The model is
`Reader`, a deterministic scripted fake behind the T037 gateway adapter interface: it
searches, reads the first hit and states that passage's value. Expected metrics are
hand-computed from the fixture tables below, not recomputed by the module.
"""

import ast
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from ingest_rights_helper import KEEP, REGION
from qw_domain import improvement as im
from qw_domain import release as rl
from qw_domain import research_improvement as ri
from qw_domain import researcher as rs
from qw_domain import trial_ledger as tl
from qw_domain.budget import BudgetBook, BudgetCaps
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.evidence import SourceKind
from qw_domain.feedback import Purpose
from qw_domain.filings import Period
from qw_domain.gateway import (
    GatewayContext,
    ModelRequest,
    ProviderResponse,
    Route,
    UsageReport,
)
from qw_domain.research_data import EvidenceClass
from qw_domain.rights import UseScope
from test_improvement import ASSESSOR, EXP, PRODUCER, assess, proto
from test_release import INST, KEY, NO, approval, deployment
from test_researcher import ACCT, E1, FY25, REG, REV, WORKFLOW, route
from test_trial_ledger import PROMO, T0, TENANT, ask, ledger, plan, trial

FY24 = Period(date(2024, 1, 1), date(2024, 12, 31))
LOADED = datetime(2026, 6, 1, tzinfo=UTC)  # SYNTHETIC archive load time
EVALUATED = datetime(2026, 10, 9, 15, tzinfo=UTC)
D = Decimal


def doc(i: str, kind: SourceKind, period: Period, end: str, pub: str,
        upd: str | None, statement: str, value: str) -> ri.Doc:  # fmt: skip
    head = ["SYNTHETIC document", f"Period-end: {end}", f"Published: {pub}"]
    text = "\n".join([*head, *([f"Updated: {upd}"] if upd else []), statement])
    return ri.Doc(i, "feed-a", kind, E1, REV, period, "USD", text,
                  date.fromisoformat(pub), D(value))  # fmt: skip


P, IND = SourceKind.PRIMARY, SourceKind.INDEPENDENT
DOCS = (  # answer key: Published date and the first stated value
    doc("d1", P, FY25, "2025-12-31", "2026-02-10", "2026-04-20",
        "Revenues were 100 USD, up from 90 USD.", "100"),
    doc("d2", IND, FY25, "2025-12-31", "2026-02-12", None,
        "Revenue reached 100 USD.", "100"),
    doc("d3", P, FY24, "2024-12-31", "2025-02-11", "2026-03-01",
        "Revenues were 90 USD, up from 80 USD.", "90"),
    doc("d4", P, FY25, "2025-12-31", "2026-05-15", None,
        "Revenues were restated to 104 USD from 100 USD.", "104"),
)  # fmt: skip


def case(i: str, period: Period, as_of: date, days: int, answer: str | None,
         *refs: str) -> ri.Case:  # fmt: skip
    at = datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC)
    return ri.Case(i, E1, REV, period, at, timedelta(days=days),
                   None if answer is None else D(answer), frozenset(refs))  # fmt: skip


CASES = (
    case("c1", FY25, date(2026, 3, 15), 120, "100", "d1", "d2"),
    case("c2", FY25, date(2026, 1, 20), 120, None),  # nothing published yet
    case("c3", FY24, date(2026, 3, 15), 400, "90", "d3"),
    case("c4", FY24, date(2026, 2, 20), 400, "90", "d3"),
)
FIXTURE = ri.Fixture("fixture-synth-promo", ri.Split.PROMOTION,
                     EvidenceClass.SYNTHETIC, TENANT, LOADED, EVALUATED, DOCS,
                     CASES)  # fmt: skip
DEV = ri.Fixture("fixture-synth-dev", ri.Split.DEVELOPMENT, EvidenceClass.SYNTHETIC,
    TENANT, LOADED, EVALUATED, (
        doc("v1", P, FY24, "2024-06-30", "2024-08-05", "2024-09-01",
            "Revenues were 50 USD, up from 40 USD.", "50"),
        doc("v2", P, FY24, "2024-09-30", "2024-11-04", "2025-01-10",
            "Revenues were 60 USD, up from 45 USD.", "60"),
    ), ())  # fmt: skip
BASE = ri.ResearchConfig()  # v0: the Updated date wins when present
FIX = replace(BASE, date_rule=ri.DateRule.PUBLISHED)
LAST = replace(FIX, value_rule=ri.ValueRule.LAST_NUMBER)
ANY_PERIOD = replace(FIX, match=ri.Match.ATTRIBUTE)
FIRST_DATE = replace(BASE, date_rule=ri.DateRule.FIRST_DATE)
PLAN = plan(protocol_hash=FIXTURE.content_hash)


class Reader:
    """SYNTHETIC scripted model: search, read the first hit, state its value."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, req: ModelRequest, r: Route, deadline: datetime) -> (
        ProviderResponse
    ):  # fmt: skip
        self.calls += 1
        lines = req.prompt.split("\n")
        mandate = json.loads(lines[1].removeprefix("MANDATE: "))
        log = json.loads(lines[3].removeprefix(rs.DATA_HEADER))
        reply = {"step": "tool", "tool": "search_evidence", "thesis": "",
                 "query": mandate["needs"][0][1]}  # fmt: skip
        if log and log[-1]["tool"] == "search_evidence":
            first = log[-1]["result"][0]["passage_id"]
            reply |= {"tool": "read_passage", "query": first}
        elif log:
            hit = log[-1]["result"]
            claim = {
                "id": "c-1",
                "kind": "reported_fact",
                "text": "SYNTHETIC",
                "evidence_ids": [hit["passage_id"]],
                "value": hit["value"],
            }
            reply = {"step": "thesis", "tool": "", "query": "", "thesis": json.dumps({
                "instrument_id": mandate["instrument_id"], "summary": "SYNTHETIC",
                "claims": [claim], "alternatives": [], "invalidation": [],
                "uncertainty": "SYNTHETIC"})}  # fmt: skip
        usage = UsageReport(req.request_id, ACCT, "auth-synth", 100, 50)
        return ProviderResponse(json.dumps(reply), usage, "model-ref-synth")


class Garbled(Reader):
    """SYNTHETIC failing model: every reply is unparsable."""

    def complete(self, req: ModelRequest, r: Route, deadline: datetime) -> (
        ProviderResponse
    ):  # fmt: skip
        out = super().complete(req, r, deadline)
        return ProviderResponse("not json at all", out.usage, out.model_returned)


def harness(model: Reader | None = None) -> ri.Harness:
    ticks = iter(EVALUATED + timedelta(seconds=i) for i in range(10_000))
    ctx = GatewayContext(REG, UseScope.PERSONAL, REGION, None, timedelta(hours=1))
    eng = rs.Engine((route(),), ctx, model or Reader(), lambda: next(ticks), 1000,
                    timedelta(seconds=20))  # fmt: skip
    return ri.Harness(WORKFLOW, eng, Usd("1"), KEEP)


def book() -> BudgetBook:
    return BudgetBook.open(TENANT, EVALUATED, BudgetCaps(Usd("100"), Usd("20"), "own"))


def protocol(**kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "target": im.Target.RESEARCH, "scope": im.Scope.INSTALLATION,
        "scorecard": ri.scorecard("card-research", D("0.05"), 30),
        "baseline_version": "research-v0", "baseline_hash": BASE.content_hash,
        "plan_digest": im.plan_digest(PLAN),
        "evidence_purpose": Purpose.SHARED_IMPROVEMENT,
        "hypothesis": "SYNTHETIC: the Published line is the publication date",
    } | kw  # fmt: skip
    return proto(**args)


def candidate(cfg: ri.ResearchConfig) -> im.Candidate:
    return ri.candidate(
        "cand-1",
        protocol(),
        "research-v0",
        cfg,
        {"pipeline": "research-v0"},
        T0 + timedelta(minutes=5),
    )


def measure(c: im.Candidate, fixture: ri.Fixture = FIXTURE, **kw: object) -> (
    dict[str, Decimal]
):  # fmt: skip
    args: dict[str, object] = {"protocol": protocol(), "plan": PLAN, "candidate": c,
        "baseline": BASE, "fixture": fixture, "harness": harness(), "book": book(),
        "run_id": "eval-synth-1"} | kw  # fmt: skip
    metrics, _ = ri.measure(**args)  # type: ignore[arg-type]
    return dict(metrics)


def promo_ledger(metrics: dict[str, Decimal], c: im.Candidate) -> tl.TrialLedger:
    key = c.trial_key  # the assessor records its own promotion trial
    lg, a = ask(ledger(PLAN), 1, tl.Split.PROMOTION, PROMO, tl.Role.ASSESSOR, None, key)
    lg = tl.TrialLedger(lg.tenant_id, (*lg.entries[:-1],
                        replace(a, actor_id=ASSESSOR.actor_id)))  # fmt: skip
    return lg.record(trial(1, tl.Split.PROMOTION, family=EXP, metrics=metrics,
                           material_hash=key.material_hash,
                           config_hash=key.config_hash,
                           protocol_hash=PLAN.protocol_hash))  # fmt: skip


def verdict(
    cfg: ri.ResearchConfig, **kw: object
) -> tuple[im.ExperimentRegistry, im.Verdict]:
    reg = im.ExperimentRegistry(TENANT).register(protocol())
    reg = reg.register_candidate(candidate(cfg))
    m = measure(reg.candidate("cand-1"), **kw)  # type: ignore[arg-type]
    v = assess(reg, promo_ledger(m, candidate(cfg)))
    return reg.record_verdict(v), v


def zero(*names: str) -> dict[str, Decimal]:
    return {n: D(0) for n in names}


HARD = zero("unsupported_material_facts", "false_references", "lookahead_uses",
            "scope_widening", "payload_invalid", "run_failures",
            "artifact_mismatch")  # fmt: skip


def test_only_closed_research_knobs_and_widening_is_refused() -> None:
    knobs = {"extract.publication_date", "extract.value", "retrieval.match",
             "retrieval.ranking", "retrieval.top_k", "prompt.template"}  # fmt: skip
    assert knobs == ri.ADAPTABLE
    assert {rs.TEMPLATE} == ri.PROMPTS  # only templates the researcher ships
    forbidden = (
        "evaluator_route",
        "assessor",
        "risk_policy",
        "acceptance_threshold",
        "scorecard",
        "answer_key",
        "holdout_split",
        "rights_override",
        "freshness_max_age",
        "stale_after",
        "budget_usd",
        "max_cost",
        "approval",
        "release_signing_key",
        "tools",
        "Answer-Key",
    )
    for key in forbidden:
        with pytest.raises(ri.ResearchImprovementError, match="widens_authority"):
            ri.ResearchConfig.from_payload({**FIX.to_payload(), key: "1"})
    with pytest.raises(ri.ResearchImprovementError, match="not_adaptable"):
        ri.ResearchConfig.from_payload({"colour": "blue"})
    for key, bad in (("retrieval.top_k", "0"), ("retrieval.top_k", "6"),
                     ("retrieval.top_k", "02"), ("retrieval.top_k", "x"),
                     ("prompt.template", "my-own-prompt"),
                     ("extract.publication_date", "model_guess"),
                     ("retrieval.ranking", "")):  # fmt: skip
        with pytest.raises(ri.ResearchImprovementError, match="value"):
            ri.ResearchConfig.from_payload({key: bad})
    with pytest.raises(ri.ResearchImprovementError, match="value"):
        ri.ResearchConfig(top_k=9)
    assert ri.ResearchConfig.from_payload(FIX.to_payload()) == FIX
    assert ri.ResearchConfig.from_payload({}) == BASE
    assert len({c.content_hash for c in (BASE, FIX, LAST, ANY_PERIOD)}) == 4


def test_extraction_rules_read_the_header_and_statement() -> None:
    d1, d3 = DOCS[0].text, DOCS[2].text
    R = ri.DateRule
    assert ri.extract_date(R.UPDATED_ELSE_PUBLISHED, d1) == date(2026, 4, 20)
    assert ri.extract_date(R.PUBLISHED, d1) == date(2026, 2, 10)
    assert ri.extract_date(R.FIRST_DATE, d1) == date(2025, 12, 31)
    assert ri.extract_date(R.UPDATED_ELSE_PUBLISHED, DOCS[1].text) == date(2026, 2, 12)
    assert ri.extract_date(R.PUBLISHED, "SYNTHETIC\nno header\n1 USD") is None
    assert ri.extract_value(ri.ValueRule.FIRST_NUMBER, d3) == (
        "Revenues were 90 USD, up from 80 USD.", D(90))  # fmt: skip
    assert ri.extract_value(ri.ValueRule.LAST_NUMBER, d3) == (
        "Revenues were 90 USD, up from 80 USD.", D(80))  # fmt: skip
    assert ri.extract_value(ri.ValueRule.FIRST_NUMBER, "Header: x\nno figure") is None


def test_producer_proposes_from_development_cases_only() -> None:
    assert ri.propose(DEV, BASE) == FIX  # Published fits both dev documents
    assert ri.propose(DEV, FIX) == FIX  # nothing better: unchanged
    with pytest.raises(ri.ResearchImprovementError, match="not_development"):
        ri.propose(FIXTURE, BASE)  # the promotion answer key is not for producers
    c = candidate(ri.propose(DEV, BASE))
    assert c.artifact_hash == FIX.content_hash and c.payload == FIX.to_payload()


def test_publication_date_fix_improves_and_is_accepted_for_review() -> None:
    _, v = verdict(FIX)
    # Hand-computed. Dates: v0 misreads d1 (Updated 04-20) and d3 (Updated 03-01);
    # Published reads all four. Answers: v0 abstains on c4 (d3 looks published
    # 2026-03-01, after 02-20); FIX answers c1 (d1, primary), c3, c4 (d3), abstains
    # on c2. Three model calls per answered case; USD0.00105 per call.
    assert dict(v.scores) == HARD | {
        "pub_date_correct": D(4), "pub_date_gain": D(2), "value_correct": D(4),
        "answers_correct": D(4), "answer_change": D(1), "model_calls": D(9),
        "cost_usd": D("0.00945"), "run_failures": D(0),
    }  # fmt: skip
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert v.limitations == ("synthetic_only",) and v.requires_human_promotion


def test_false_fact_regression_is_rejected_although_dates_improve() -> None:
    _, v = verdict(LAST)  # reads "up from 90" etc.: d1 90, d3 80, d4 100
    assert v.scores["pub_date_gain"] == 2 and v.scores["value_correct"] == 1
    assert v.scores["unsupported_material_facts"] == 3  # c1, c3, c4
    assert v.scores["false_references"] == 0 and v.scores["answer_change"] == -2
    assert v.decision is im.Decision.REJECT
    assert v.reasons == ("hard:unsupported_material_facts", "goal:answer_change")


def test_false_reference_and_lookahead_regressions_are_rejected() -> None:
    _, v = verdict(ANY_PERIOD)  # c3, c4 cite FY2025 d1 for an FY2024 question
    assert (
        v.scores["pub_date_gain"],
        v.scores["unsupported_material_facts"],
        v.scores["false_references"],
        v.scores["lookahead_uses"],
    ) == (D(2), D(2), D(2), D(0))
    assert v.reasons == ("hard:unsupported_material_facts", "hard:false_references",
                         "goal:answer_change")  # fmt: skip
    _, v = verdict(FIRST_DATE)  # period ends look like publication dates
    # Retrieved before publication: d4 (2026-05-15) for c1; d1 (02-10) and d4 for
    # c2 (as of 2026-01-20), which then cites d1 although the answer is "none yet".
    assert (v.scores["lookahead_uses"], v.scores["false_references"],
            v.scores["pub_date_correct"]) == (D(3), D(1), D(0))  # fmt: skip
    assert v.decision is im.Decision.REJECT and v.reasons[:3] == (
        "hard:unsupported_material_facts", "hard:false_references",
        "hard:lookahead_uses")  # fmt: skip


def test_answer_key_test_access_and_payload_tampering_are_caught() -> None:
    edited = replace(FIXTURE, cases=(replace(CASES[0], answer=D(90)), *CASES[1:]))
    with pytest.raises(ri.ResearchImprovementError, match="answer_key_changed"):
        measure(candidate(FIX), edited)
    redated = replace(FIXTURE, docs=(replace(DOCS[0], published_on=date(2026, 1, 1)),
                                     *DOCS[1:]))  # fmt: skip
    with pytest.raises(ri.ResearchImprovementError, match="answer_key_changed"):
        measure(candidate(FIX), redated)
    with pytest.raises(ri.ResearchImprovementError, match="test_access_changed"):
        measure(candidate(FIX), plan=plan())
    hist = replace(FIXTURE, evidence_class=EvidenceClass.HISTORICAL)  # relabelled
    hp = plan(protocol_hash=hist.content_hash)
    with pytest.raises(ri.ResearchImprovementError, match="evidence_class"):
        measure(
            candidate(FIX),
            hist,
            plan=hp,
            protocol=protocol(plan_digest=im.plan_digest(hp)),
        )
    with pytest.raises(ri.ResearchImprovementError, match="baseline_hash"):
        measure(candidate(FIX), baseline=FIX)
    with pytest.raises(ri.ResearchImprovementError, match="not_promotion"):
        measure(candidate(FIX), DEV, plan=plan(protocol_hash=DEV.content_hash),
                protocol=protocol(plan_digest=im.plan_digest(
                    plan(protocol_hash=DEV.content_hash))))  # fmt: skip
    # A fixture that cannot observe look-ahead or a wrong reference proves nothing.
    for name, docs, cases in (
        ("lookahead", DOCS[:3], (CASES[0], CASES[2])),
        ("reference", (DOCS[0], DOCS[1], DOCS[3]), CASES[:2]),
    ):
        f = replace(FIXTURE, docs=docs, cases=cases)
        p = plan(protocol_hash=f.content_hash)
        with pytest.raises(ri.ResearchImprovementError, match=f"fixture_no_{name}"):
            measure(
                candidate(FIX),
                f,
                plan=p,
                protocol=protocol(plan_digest=im.plan_digest(p)),
            )
    weak = im.Scorecard("card-weak", im.Target.RESEARCH, (
        im.Threshold("unsupported_material_facts", im.Direction.AT_MOST, D(0), True),
        im.Threshold("pub_date_gain", im.Direction.AT_LEAST, D(1), False)))  # fmt: skip
    with pytest.raises(ri.ResearchImprovementError, match="control_missing"):
        ri.check_protocol(protocol(scorecard=weak))
    with pytest.raises(ri.ResearchImprovementError, match="not_research"):
        ri.check_protocol(proto())
    # A payload edited around the constructor still meets the assessor.
    c = replace(candidate(FIX), payload={**FIX.to_payload(), "evaluator_route": "x",
                                         "retrieval.top_k": "99"})  # fmt: skip
    m = measure(c)
    assert (m["scope_widening"], m["payload_invalid"]) == (D(1), D(1))
    assert m["artifact_mismatch"] == 1  # the scored payload is not the artifact


def test_promotion_goes_through_release_and_install_never_the_candidate() -> None:
    reg, v = verdict(FIX)
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    compat = rl.Compatibility(1, 1, 1)
    for actor in (PRODUCER.actor_id, ASSESSOR.actor_id):
        with pytest.raises(rl.ReleaseError, match="self_approval"):
            rl.prepare_release(reg, "cand-1", "rel-r1", compat, approval(who=actor))
    man = rl.prepare_release(reg, "cand-1", "rel-r1", compat, approval())
    assert man.component == "research:research" and not man.user_adoption_required
    sr = rl.sign_release(man, KEY)
    lg = deployment().install("ev-0", sr, "operator-synth", "stepup-op-0", INST,
                              strategies=NO)  # fmt: skip
    assert lg.head("research:research") == "rel-r1"
    assert ri.config_of(reg, sr) == FIX
    forged = replace(sr, manifest=replace(man, artifact_hash=LAST.content_hash))
    with pytest.raises(ri.ResearchImprovementError, match="artifact_mismatch"):
        ri.config_of(reg, forged)
    p = protocol(scope=im.Scope.TENANT)  # private research learning is not shared
    r = im.ExperimentRegistry(TENANT).register(p).register_candidate(candidate(FIX))
    r = r.record_verdict(replace(v, protocol_hash=p.content_hash))
    with pytest.raises(rl.ReleaseError, match="tenant_scope"):
        rl.prepare_release(r, "cand-1", "rel-r2", compat, approval())
    src = Path(ri.__file__).read_text()
    names = {n.name for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.FunctionDef)}  # fmt: skip
    assert not {n for n in names if any(w in n for w in (
        "promote", "approve", "install", "adopt", "sign", "release_"))}  # fmt: skip


def test_review_round1_failures_artifact_binding_and_reachable_traps() -> None:
    # S1: a model that fails every call cannot pass by abstaining.
    _, v = verdict(FIX, harness=harness(Garbled()))
    assert v.scores["run_failures"] == 3  # c1, c3, c4 reach the model; c2 does not
    assert v.decision is im.Decision.REJECT and "hard:run_failures" in v.reasons
    # S2: the scored payload must be the full canonical payload of the artifact.
    for c in (replace(candidate(FIX), artifact_hash=LAST.content_hash),
              replace(candidate(FIX), payload={"extract.publication_date":
                                               "published"})):  # fmt: skip
        assert measure(c)["artifact_mismatch"] == 1
    _, v = verdict(FIX)
    assert v.scores["artifact_mismatch"] == 0
    # S3: traps no registered date rule can retrieve do not count.
    old = doc("d9", P, FY24, "2020-12-31", "2021-02-01", None,
              "Revenues were 7 USD.", "7")  # fmt: skip
    late = doc("d8", P, FY24, "2024-12-31", "2030-01-01", None,
               "Revenues were 8 USD.", "8")  # fmt: skip
    # d4 is FY2025: a future document of another period is not a look-ahead trap
    # for the FY2024 case c3, which would never retrieve it.
    for docs, cases, code in (
        ((*DOCS[:3], old, late), (CASES[0], CASES[2]), "lookahead"),
        ((DOCS[2], DOCS[3], DOCS[0]), (CASES[2],), "lookahead"),
        ((DOCS[0], DOCS[2], DOCS[3], old), (CASES[0],), "reference"),
    ):
        f = replace(FIXTURE, docs=docs, cases=cases)
        p = plan(protocol_hash=f.content_hash)
        with pytest.raises(ri.ResearchImprovementError, match=f"fixture_no_{code}"):
            measure(
                candidate(FIX),
                f,
                plan=p,
                protocol=protocol(plan_digest=im.plan_digest(p)),
            )
