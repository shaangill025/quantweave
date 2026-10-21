"""Personal adaptation: tenant-private alert/explanation preference candidates
(T045 increment 1). All data is SYNTHETIC: tenants, feedback, alert and explanation
cases, approvals. Expected metrics are hand-computed from the fixture timeline below,
not recomputed by the module.
"""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from qw_domain import feedback as fb
from qw_domain import improvement as im
from qw_domain import personal_adaptation as pa
from qw_domain.notifications import Category, Channel, Preferences
from test_improvement import ASSESSOR, EXP, PRODUCER, assess, promo_ledger, proto
from test_trial_ledger import T0, TENANT

C = Category
MIN = timedelta(minutes=1)
BASE = pa.Adaptation()
OTHER = "tenant-synth-b"
OK_SUBJ = fb.Subject(fb.SubjectKind.NOTIFICATION, "ntf-synth-1", 1, "a" * 64)
TH_SUBJ = fb.Subject(fb.SubjectKind.THESIS, "thesis-synth-1", 1, "a" * 64)
APPROVE_AT = T0 + timedelta(days=2)


def alert(n: str, cat: Category, obj: str, minute: int, relevant: bool,
          must: bool = False) -> pa.AlertCase:  # fmt: skip
    return pa.AlertCase(n, cat, obj, T0 + minute * MIN, relevant, must)


# SYNTHETIC timeline. Baseline (no coalescing, nothing muted) delivers all six.
FIXTURE = pa.Fixture(
    "fixture-synth-1",
    (
        alert("a1", C.MONITORING_ALERT, "rule-x", 0, True),
        alert("a2", C.MONITORING_ALERT, "rule-x", 10, False),  # repeat, unhelpful
        alert("a3", C.MONITORING_ALERT, "rule-x", 20, False),  # repeat, unhelpful
        alert("a4", C.MONITORING_ALERT, "rule-y", 25, True, must=True),
        alert("p1", C.RISK_PAUSE, "pause-1", 30, True, must=True),
        alert("a5", C.MONITORING_ALERT, "rule-x", 90, True),
    ),
    (
        pa.ExplanationCase("e1", pa.Verbosity.DETAILED, pa.ExplanationFormat.PROSE),
        pa.ExplanationCase("e2", pa.Verbosity.DETAILED, pa.ExplanationFormat.PROSE),
    ),
)
HELPFUL = pa.Adaptation(coalesce_minutes={C.MONITORING_ALERT: 60},
                        verbosity=pa.Verbosity.DETAILED)  # fmt: skip
MUTES_FLAGGED = pa.Adaptation(mute_external=frozenset({C.MONITORING_ALERT}))
OVER_COALESCED = pa.Adaptation(coalesce_minutes={C.MONITORING_ALERT: 120})


def protocol(**kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "target": im.Target.PERSONAL_ADAPTATION, "scorecard": pa.scorecard("card-pa"),
        "baseline_version": "prefs-v0", "baseline_hash": BASE.content_hash,
        "hypothesis": "SYNTHETIC: coalescing repeats cuts unhelpful alerts",
    } | kw  # fmt: skip
    return proto(**args)


def candidate(a: pa.Adaptation, uses: tuple[fb.Use, ...] = ()) -> im.Candidate:
    return pa.candidate("cand-1", protocol(), "prefs-v0", a, uses,
                        {"producer": "rules-v0"}, T0 + 5 * MIN)  # fmt: skip


def verdict(
    a: pa.Adaptation, art: str | None = None
) -> tuple[im.ExperimentRegistry, im.Verdict]:
    reg = im.ExperimentRegistry(TENANT).register(protocol())
    c = candidate(a) if art is None else replace(candidate(a), artifact_hash=art)
    reg = reg.register_candidate(c)
    metrics = pa.measure(protocol(), reg.candidate("cand-1"), BASE, FIXTURE)
    v = assess(reg, promo_ledger(dict(metrics), c))  # assessor-recorded trial only
    return reg.record_verdict(v), v


def approvals(at: datetime = APPROVE_AT, **ids: str) -> tuple[pa.Approval, ...]:
    ac = im.ApprovalClass
    return (
        pa.Approval(ac.OPERATOR_INSTALL, ids.get("operator", "operator-synth"),
                    "cand-1", at),
        pa.Approval(ac.USER_STRATEGY_ADOPTION, ids.get("user", "user-synth"),
                    "cand-1", at + MIN),
    )  # fmt: skip


def test_only_closed_preference_knobs_and_widening_is_refused() -> None:
    knobs = {
        "coalesce_minutes.monitoring_alert", "coalesce_minutes.proposal",
        "coalesce_minutes.job_outcome", "mute_external.monitoring_alert",
        "mute_external.proposal", "mute_external.job_outcome",
        "explanation_verbosity", "explanation_format",
    }  # fmt: skip
    assert knobs == pa.ADAPTABLE
    forbidden = ("investment_policy", "max_position_pct", "risk_limits", "sizing",
                 "position_size", "mode", "execution_mode", "approvals", "leverage",
                 "margin_allowed", "order_type", "broker_account", "strategy_adoption",
                 "allowed_instruments", "options_level", "max_drawdown", "stop_loss",
                 "allocation_targets", "budget_usd", "consent_scope", "share_with",
                 "export_examples", "assessor", "scorecard_thresholds",
                 "Risk-Limits", "MODE")  # fmt: skip
    for key in forbidden:
        with pytest.raises(pa.AdaptationError, match="widens_authority"):
            pa.Adaptation.from_payload({**HELPFUL.to_payload(), key: "1"})
    with pytest.raises(pa.AdaptationError, match="not_adaptable"):
        pa.Adaptation.from_payload({"theme_color": "blue"})
    for key in ("coalesce_minutes.risk_pause", "mute_external.risk_pause"):
        with pytest.raises(pa.AdaptationError, match="widens_authority"):
            pa.Adaptation.from_payload({key: "1"})
    with pytest.raises(pa.AdaptationError, match="safety_category"):
        pa.Adaptation(coalesce_minutes={C.RISK_PAUSE: 5})
    with pytest.raises(pa.AdaptationError, match="safety_category"):
        pa.Adaptation(mute_external=frozenset({C.RISK_PAUSE}))
    for value in ("-1", "1441", "1.5", " 5", "x"):
        with pytest.raises(pa.AdaptationError, match="value"):
            pa.Adaptation.from_payload({"coalesce_minutes.proposal": value})
    with pytest.raises(pa.AdaptationError, match="value"):
        pa.Adaptation.from_payload({"mute_external.proposal": "yes"})
    with pytest.raises(TypeError):
        pa.Adaptation(coalesce_minutes={C.PROPOSAL: 1.5})  # type: ignore[dict-item]
    assert pa.Adaptation.from_payload(HELPFUL.to_payload()) == HELPFUL
    assert HELPFUL.content_hash != BASE.content_hash


def test_protocol_must_be_private_personal_and_hold_the_safety_control() -> None:
    pa.check_protocol(protocol())
    refusals = {
        "not_private": (protocol(scope=im.Scope.INSTALLATION),
                        protocol(evidence_purpose=fb.Purpose.SHARED_IMPROVEMENT),
                        proto()),  # a strategy protocol
    }  # fmt: skip
    for code, protos in refusals.items():
        for p in protos:
            with pytest.raises(pa.AdaptationError, match=code):
                pa.check_protocol(p)
    hard = tuple(im.Threshold(m, im.Direction.AT_MOST, Decimal(0), True)
                 for m in im.HARD_CONTROLS[im.Target.PERSONAL_ADAPTATION])  # fmt: skip
    goal = im.Threshold("redundant_alerts_removed", im.Direction.AT_LEAST,
                        Decimal(1), False)  # fmt: skip
    weak = im.Scorecard("card-weak", im.Target.PERSONAL_ADAPTATION, (*hard, goal))
    with pytest.raises(pa.AdaptationError, match="safety_control_missing"):
        pa.check_protocol(protocol(scorecard=weak))
    with pytest.raises(pa.AdaptationError, match="not_private"):
        pa.candidate("cand-1", protocol(scope=im.Scope.INSTALLATION), "prefs-v0",
                     HELPFUL, (), {}, T0 + MIN)  # fmt: skip


def test_producer_reads_only_its_tenant_through_a_logged_personal_select() -> None:
    ev = fb.ImprovementEvidence()

    def note(tenant: str, n: int, cat: fb.FeedbackCategory, s: fb.Subject) -> None:
        at = T0 + (n - 10) * MIN  # in recording order, before each select
        ev.record(fb.Feedback(tenant_id=tenant, record_id=f"fb-{n}", subject=s,
                              occurred_at=at, recorded_at=at, principal_id="user-synth",
                              category=cat, message="SYNTHETIC"))  # fmt: skip

    for n in (1, 2):
        note(TENANT, n, fb.FeedbackCategory.IRRELEVANT, OK_SUBJ)
    note(TENANT, 3, fb.FeedbackCategory.UNCLEAR, TH_SUBJ)
    for n in (4, 5, 6):
        note(OTHER, n, fb.FeedbackCategory.UNCLEAR, TH_SUBJ)
    proposed, use = pa.propose(ev, TENANT, EXP, BASE, T0 + MIN)
    assert proposed == HELPFUL
    assert use.purpose is fb.Purpose.PERSONAL_ADAPTATION and use.recipient is None
    assert use.access is fb.Access.DEVELOPMENT and use.record_ids == (
        "fb-1", "fb-2", "fb-3")  # fmt: skip
    assert use in ev.uses(TENANT) and not ev.uses(OTHER)
    reg = im.ExperimentRegistry(TENANT).register(protocol())
    reg = reg.register_candidate(candidate(proposed, (use,)), ev)
    assert reg.candidate("cand-1").payload == HELPFUL.to_payload()
    # Other-tenant feedback alone produces nothing for this tenant.
    quiet, _ = pa.propose(fb.ImprovementEvidence(), TENANT, EXP, BASE, T0)
    assert quiet == BASE
    # A missed-risk report stops any coalescing increase.
    note(TENANT, 12, fb.FeedbackCategory.MISSED_RISK, OK_SUBJ)
    safer, _ = pa.propose(ev, TENANT, EXP, BASE, T0 + 3 * MIN)
    assert safer.coalesce_minutes == {}


def test_helpful_candidate_is_accepted_for_review_on_the_fixture() -> None:
    _, v = verdict(HELPFUL)
    # Hand-computed: a2, a3 coalesced (inside 60 min of a1); a5 at 90 min delivered.
    assert dict(v.scores) == {
        "risk_broadened": Decimal(0), "private_export": Decimal(0),
        "missed_safety_alerts": Decimal(0), "payload_invalid": Decimal(0),
        "improved": Decimal(1), "redundant_alerts_removed": Decimal(2),
        "relevant_alerts_lost": Decimal(0), "explanation_mismatch_change": Decimal(-2),
    }  # fmt: skip
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert v.limitations == ("synthetic_only", "no_multiple_testing_adjustment")  # 36
    assert v.requires_human_promotion
    edge = (alert("b1", C.PROPOSAL, "prop-1", 0, True),
            alert("b2", C.PROPOSAL, "prop-1", 60, True))  # fmt: skip
    hour = pa.Adaptation(coalesce_minutes={C.PROPOSAL: 60})
    assert pa.delivered(hour, edge) == {"b1", "b2"}  # window end is exclusive


def test_deliberate_regressions_are_rejected() -> None:
    _, v = verdict(MUTES_FLAGGED)  # a4 is flagged must-deliver; a1, a4, a5 lost
    assert v.decision is im.Decision.REJECT
    assert v.scores["missed_safety_alerts"] == 1
    assert v.scores["relevant_alerts_lost"] == 3
    assert v.reasons == ("hard:missed_safety_alerts", "goal:relevant_alerts_lost")
    _, v = verdict(OVER_COALESCED)  # a5 at 90 min falls inside a1's 120-min window
    assert v.reasons == ("goal:relevant_alerts_lost",)
    assert v.scores["relevant_alerts_lost"] == 1
    # A candidate built around the adapter still meets the independent measurement.
    wide = {**HELPFUL.to_payload(), "max_position_pct": "50"}
    widened = replace(candidate(HELPFUL), payload=wide)
    m = pa.measure(protocol(), widened, BASE, FIXTURE)
    assert m["risk_broadened"] == 1
    shared = fb.Use(TENANT, EXP, fb.Purpose.SHARED_IMPROVEMENT, fb.Access.DEVELOPMENT,
                    "installation:synth-1", ("fb-1",), ("consent-1",), T0)  # fmt: skip
    leaked = replace(candidate(HELPFUL), evidence_uses=(shared,))
    assert pa.measure(protocol(), leaked, BASE, FIXTURE)["private_export"] == 1
    with pytest.raises(pa.AdaptationError, match="baseline_hash"):
        pa.measure(protocol(), candidate(HELPFUL), HELPFUL, FIXTURE)


def test_adoption_needs_accepted_verdict_and_non_agent_approvals() -> None:
    reg, _ = verdict(HELPFUL)
    state = pa.PrivateState.open(TENANT, BASE, T0)
    for bad, code in (
        (approvals()[:1], "approvals_missing"),
        (approvals(operator=PRODUCER.actor_id), "approver_not_human"),
        (approvals(user=ASSESSOR.actor_id), "approver_not_human"),
        (approvals(at=T0), "approval_before_verdict"),
        ((approvals()[1], replace(approvals()[0], at=APPROVE_AT + 5 * MIN)),
         "install_after_adoption"),
        (tuple(replace(a, candidate_id="cand-9") for a in approvals()),
         "approvals_missing"),
    ):  # fmt: skip
        with pytest.raises(pa.AdaptationError, match=code):
            state.adopt(reg, "cand-1", bad, APPROVE_AT + 10 * MIN)
    rejected, _ = verdict(MUTES_FLAGGED)
    with pytest.raises(pa.AdaptationError, match="not_accepted"):
        state.adopt(rejected, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    unassessed = im.ExperimentRegistry(TENANT).register(protocol())
    unassessed = unassessed.register_candidate(candidate(HELPFUL))
    with pytest.raises(pa.AdaptationError, match="not_accepted"):
        state.adopt(unassessed, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    other = pa.PrivateState.open(OTHER, BASE, T0)
    with pytest.raises(pa.AdaptationError, match="tenant"):
        other.adopt(reg, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    stale = pa.PrivateState.open(TENANT, OVER_COALESCED, T0)
    with pytest.raises(pa.AdaptationError, match="stale_baseline"):
        stale.adopt(reg, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    assert state.active == BASE and len(state.versions) == 1  # refusals left no trace
    # Owner decision (single-owner pilot): one person may give both approvals.
    solo = pa.PrivateState.open(TENANT, BASE, T0)
    both = approvals(operator="owner-synth", user="owner-synth")
    assert solo.adopt(reg, "cand-1", both, APPROVE_AT + 10 * MIN).number == 2
    v2 = state.adopt(reg, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    assert (v2.number, v2.source, state.active) == (2, "candidate:cand-1", HELPFUL)
    assert v2.actor_ids == ("operator-synth", "user-synth")
    with pytest.raises(pa.AdaptationError, match="already_adopted"):
        state.adopt(reg, "cand-1", approvals(), APPROVE_AT + 20 * MIN)
    prefs = pa.apply(Preferences(email=True), MUTES_FLAGGED)
    assert prefs.muted == {(C.MONITORING_ALERT, Channel.EMAIL),
                           (C.MONITORING_ALERT, Channel.BROWSER_PUSH)}  # fmt: skip


def test_rollback_is_always_possible_recorded_and_erase_drops_private_state() -> None:
    reg, _ = verdict(HELPFUL)
    state = pa.PrivateState.open(TENANT, BASE, T0)
    state.adopt(reg, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    with pytest.raises(pa.AdaptationError, match="time_order"):
        state.rollback(1, "user-synth", T0)
    with pytest.raises(pa.AdaptationError, match="already_active"):
        state.rollback(2, "user-synth", APPROVE_AT + 11 * MIN)
    v3 = state.rollback(1, "user-synth", APPROVE_AT + 12 * MIN)
    assert (v3.number, v3.source, state.active) == (3, "rollback:1", BASE)
    v4 = state.rollback(2, "user-synth", APPROVE_AT + 13 * MIN)
    assert state.active == HELPFUL and v4.adaptation_hash == HELPFUL.content_hash
    exported = state.export()
    assert [e["source"] for e in exported] == [
        "baseline", "candidate:cand-1", "rollback:1", "rollback:2"]  # fmt: skip
    assert all(e["tenant_id"] == TENANT for e in exported)  # history kept, not edited
    with pytest.raises(pa.AdaptationError, match="unknown_version"):
        state.rollback(9, "user-synth", APPROVE_AT + 14 * MIN)
    state.erase("user-synth", APPROVE_AT + 15 * MIN)
    assert state.active == pa.Adaptation()
    assert all(v.adaptation is None for v in state.versions[:-1])
    with pytest.raises(pa.AdaptationError, match="erased"):
        state.rollback(2, "user-synth", APPROVE_AT + 16 * MIN)


def test_review_round1_fixture_goals_payload_and_state() -> None:
    # S1: a fixture whose only must-deliver case is a safety alert proves nothing.
    vacuous = pa.Fixture("fixture-synth-2", (
        alert("n1", C.MONITORING_ALERT, "rule-x", 0, False),
        alert("n2", C.RISK_PAUSE, "pause-1", 5, True, must=True)), ())  # fmt: skip
    with pytest.raises(pa.AdaptationError, match="fixture_no_safety_case"):
        pa.measure(protocol(), candidate(MUTES_FLAGGED), BASE, vacuous)
    # N1: a verbosity-only candidate can meet the combined improvement goal.
    _, v = verdict(pa.Adaptation(verbosity=pa.Verbosity.DETAILED))
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert v.scores["improved"] == 1 and v.scores["redundant_alerts_removed"] == 0
    # N5: measurement is bound to the protocol's experiment ...
    elsewhere = replace(candidate(HELPFUL), experiment_id="exp-other")
    with pytest.raises(pa.AdaptationError, match="candidate_experiment"):
        pa.measure(protocol(), elsewhere, BASE, FIXTURE)
    # ... and adoption to an artifact hash that is the payload's configuration.
    reg, v = verdict(HELPFUL, art="b" * 64)
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    state = pa.PrivateState.open(TENANT, BASE, T0)
    with pytest.raises(pa.AdaptationError, match="artifact_mismatch"):
        state.adopt(reg, "cand-1", approvals(), APPROVE_AT + 10 * MIN)
    # N6: a bad value is a hard violation in measurement, never an exception.
    key = "coalesce_minutes.monitoring_alert"
    for bad in ("0060", "-5", "9" * 5000, "x", "241"):
        c = replace(candidate(HELPFUL), payload={**HELPFUL.to_payload(), key: bad})
        assert pa.measure(protocol(), c, BASE, FIXTURE)["payload_invalid"] == 1
    odd: tuple[dict[object, object], ...] = (
        {"coalesce_minutes.proposal": 5},
        {"mute_external.proposal": True},
        {7: "1"},
    )
    for payload in odd:
        with pytest.raises(pa.AdaptationError):
            pa.Adaptation.from_payload(payload)  # type: ignore[arg-type]
    # N9: monitoring alerts coalesce for at most 240 minutes; others up to 1440.
    with pytest.raises(pa.AdaptationError, match="value"):
        pa.Adaptation(coalesce_minutes={C.MONITORING_ALERT: 241})
    with pytest.raises(pa.AdaptationError, match="value"):
        pa.Adaptation.from_payload({key: "241"})
    pa.Adaptation(coalesce_minutes={C.PROPOSAL: 1440, C.MONITORING_ALERT: 240})
    # N7: the version history cannot be changed from outside.
    assert isinstance(state.versions, tuple)
    with pytest.raises(AttributeError):
        state.versions = ()  # type: ignore[misc]


def test_review_round2_changed_categories_must_be_covered_by_the_fixture() -> None:
    # N11: FIXTURE has no proposal or job_outcome alerts, so muting them is unmeasured.
    quiet = pa.Adaptation(mute_external=frozenset({C.PROPOSAL, C.JOB_OUTCOME}),
                          verbosity=pa.Verbosity.DETAILED)  # fmt: skip
    with pytest.raises(pa.AdaptationError, match="fixture_category_uncovered"):
        pa.measure(protocol(), candidate(quiet), BASE, FIXTURE)
    slow = pa.Adaptation(coalesce_minutes={C.JOB_OUTCOME: 30})
    with pytest.raises(pa.AdaptationError, match="fixture_category_uncovered"):
        pa.measure(protocol(), candidate(slow), BASE, FIXTURE)
    covered = replace(FIXTURE, alerts=(
        *FIXTURE.alerts, alert("j1", C.JOB_OUTCOME, "job-1", 40, True)))  # fmt: skip
    assert pa.measure(protocol(), candidate(slow), BASE, covered)["improved"] == 0
