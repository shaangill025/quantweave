"""Feedback and improvement evidence (T043 increment 1).

Everything is SYNTHETIC: tenants, principals, proposals, reviews, cases and consent
records. Expected outcomes are written from the rules in the task contract and spec
§7, §11, §12 and §14, not recomputed by the module.
"""

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import feedback as fb
from qw_domain.decimals import UsdBudget
from qw_domain.evaluator import Decision, Review
from qw_domain.proposals import dismiss, expire, mark_planned
from qw_domain.researcher import Abstention, Why
from test_proposals import TENANT, active, cur, ver

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
H = timedelta(hours=1)
OTHER = "tenant-synth-b"
HASH = "a" * 64
SUBJ = fb.Subject(fb.SubjectKind.THESIS, "thesis-1", 1, HASH)
PROP = fb.Subject(fb.SubjectKind.PROPOSAL, "prop-a", 1, HASH)
P = fb.Purpose
REC = "installation:synth-1"


def feedback(rid: str = "fb-1", tenant: str = TENANT, at: datetime = AT,
             **kw: Any) -> fb.Feedback:  # fmt: skip
    base: dict[str, Any] = {
        "category": fb.FeedbackCategory.INCORRECT_FACT, "message": "SYNTHETIC note",
        "principal_id": "user-1",
    }  # fmt: skip
    return fb.Feedback(tenant_id=tenant, record_id=rid, subject=SUBJ,
                       occurred_at=at, recorded_at=at, **{**base, **kw})  # fmt: skip


def case(rid: str = "case-1", split: fb.Split = fb.Split.DEVELOPMENT,
         **inputs: str) -> fb.EvaluationCase:  # fmt: skip
    return fb.EvaluationCase(
        tenant_id=TENANT, record_id=rid, subject=SUBJ, occurred_at=AT,
        recorded_at=AT, inputs=inputs or {"attribute": "revenue", "unit": "USD"},
        expected="incorrect_fact", split=split,
        origin=fb.CaseOrigin.PERSONAL_HISTORICAL,
    )  # fmt: skip


def grant(cid: str = "consent-1", at: datetime = AT, **kw: Any) -> fb.ConsentGrant:
    base: dict[str, Any] = {
        "principal_id": "user-1", "purpose": P.SHARED_IMPROVEMENT, "recipient": REC,
        "data_categories": frozenset({fb.RecordKind.FEEDBACK}),
        "terms_version": "terms-v1", "expires_at": None,
    }  # fmt: skip
    return fb.ConsentGrant(TENANT, cid, granted_at=at, **{**base, **kw})


def shared(store: fb.ImprovementEvidence, at: datetime, exp: str = "exp-1",
           tenant: str = TENANT, terms: str = "terms-v1") -> fb.Selection:  # fmt: skip
    return store.select(tenant, exp, P.SHARED_IMPROVEMENT, fb.Access.DEVELOPMENT, at,
                        recipient=REC, terms_version=terms)  # fmt: skip


def why(sel: fb.Selection, rid: str) -> str:
    return dict(sel.excluded)[rid]


# ---- private feedback is not global training consent (R031) -------------------


def test_private_feedback_is_personal_only_by_default() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback())
    mine = s.select(TENANT, "exp-1", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                    AT + H)  # fmt: skip
    assert [r.record_id for r in mine.records] == ["fb-1"] and not mine.excluded
    out = shared(s, AT + 2 * H)
    assert out.records == () and why(out, "fb-1") == "no_consent"
    # Another tenant sees nothing, for either purpose, and cannot read the record.
    assert shared(s, AT + 3 * H, tenant=OTHER).records == ()
    other = s.select(OTHER, "exp-1", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                     AT + 3 * H)  # fmt: skip
    assert other.records == () and other.excluded == ()
    with pytest.raises(fb.FeedbackError, match="not_found"):
        s.get(OTHER, "fb-1")


def test_feedback_record_cannot_carry_consent_and_matches_the_contract() -> None:
    assert {c.value for c in fb.FeedbackCategory} == {
        "incorrect_fact", "irrelevant", "clear", "unclear", "missed_risk", "other",
    }  # fmt: skip
    assert "consent" not in " ".join(fb.Feedback.__dataclass_fields__)
    with pytest.raises(fb.FeedbackError, match="message"):
        feedback(message="x" * 4001)
    with pytest.raises(TypeError):
        feedback(category="incorrect_fact")
    with pytest.raises(fb.FeedbackError, match="time_order"):
        replace(feedback(), recorded_at=AT - H)
    with pytest.raises(ValueError, match="naive"):
        feedback(at=AT.replace(tzinfo=None))


def test_explicit_consent_is_scoped_and_revocation_is_prospective() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback())
    s.record(case())
    s.grant(grant(at=AT + H, covers_prior=True))
    first = shared(s, AT + 2 * H)
    assert [r.record_id for r in first.records] == ["fb-1"]
    assert first.consent_ids == ("consent-1",)
    assert why(first, "case-1") == "owner_unknown"  # no principal owns a case
    # Recipient and terms version are part of the consent.
    other_rec = s.select(
        TENANT,
        "exp-2",
        P.SHARED_IMPROVEMENT,
        fb.Access.DEVELOPMENT,
        AT + 2 * H,
        recipient="corpus:x",
        terms_version="terms-v1",
    )
    assert other_rec.records == () and why(other_rec, "fb-1") == "no_consent"
    assert shared(s, AT + 2 * H, exp="exp-3", terms="terms-v2").records == ()
    s.revoke(TENANT, "consent-1", AT + 3 * H)
    assert why(shared(s, AT + 4 * H, exp="exp-4"), "fb-1") == "no_consent"
    # The earlier use stays on record and is flagged for derivative handling.
    flagged = s.affected_uses(TENANT, AT + 4 * H)
    assert [(u.use.experiment_id, u.reason) for u in flagged] == [
        ("exp-1", "consent_revoked")]  # fmt: skip
    assert s.affected_uses(TENANT, AT + 2 * H) == ()
    # Personal adaptation never needed the consent and is unaffected.
    mine = s.select(TENANT, "exp-5", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                    AT + 5 * H)  # fmt: skip
    assert {r.record_id for r in mine.records} == {"fb-1", "case-1"}


def test_consent_window_and_other_tenants_consent_do_not_apply() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback())
    assert shared(s, AT + H - timedelta(microseconds=1)).records == ()
    s.grant(grant(at=AT + H, expires_at=AT + 3 * H, covers_prior=True))
    assert len(shared(s, AT + H, exp="e2").records) == 1
    assert shared(s, AT + 3 * H - timedelta(microseconds=1), exp="e3").records
    assert shared(s, AT + 3 * H, exp="e3").records == ()  # expiry is exclusive
    s.grant(replace(grant("consent-b", at=AT + 4 * H), tenant_id=OTHER))
    assert shared(s, AT + 5 * H, exp="e4").records == ()


def test_consent_and_selection_refusals() -> None:
    s = fb.ImprovementEvidence()
    with pytest.raises(fb.FeedbackError, match="recipient"):
        grant(purpose=P.PERSONAL_ADAPTATION)  # personal use needs no grant
    with pytest.raises(fb.FeedbackError, match="data_categories"):
        grant(data_categories=frozenset())
    with pytest.raises(fb.FeedbackError, match="recipient"):
        s.select(TENANT, "e", P.SHARED_IMPROVEMENT, fb.Access.DEVELOPMENT, AT)
    with pytest.raises(fb.FeedbackError, match="recipient"):
        s.select(TENANT, "e", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT, AT,
                 recipient=REC, terms_version="terms-v1")  # fmt: skip
    s.grant(grant())
    with pytest.raises(fb.FeedbackError, match="exists"):
        s.grant(grant(recipient="corpus:other"))
    s.grant(grant())  # identical retry is a no-op
    with pytest.raises(fb.FeedbackError, match="not_found"):
        s.revoke(OTHER, "consent-1", AT + H)
    with pytest.raises(fb.FeedbackError, match="time_order"):
        s.revoke(TENANT, "consent-1", AT - H)
    s.revoke(TENANT, "consent-1", AT + H)
    with pytest.raises(fb.FeedbackError, match="revoked"):
        s.revoke(TENANT, "consent-1", AT + 2 * H)


def test_grants_are_per_principal_and_prospective_by_default() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback("old"))
    s.record(feedback("other", principal_id="user-2", at=AT + H))
    s.grant(grant(at=AT + H))
    s.record(feedback("new", at=AT + 2 * H))
    sel = shared(s, AT + 3 * H)
    assert [r.record_id for r in sel.records] == ["new"]
    assert why(sel, "old") == "recorded_before_consent"
    assert why(sel, "other") == "no_consent"  # user-1 cannot consent for user-2
    s.grant(grant("consent-2", at=AT + 4 * H, data_categories=frozenset(
        {fb.RecordKind.ERROR}), covers_prior=True))  # fmt: skip
    assert why(shared(s, AT + 5 * H, exp="e2", terms="terms-v1"), "old") == (
        "recorded_before_consent")  # fmt: skip


def test_error_details_keep_codes_and_redact_text() -> None:
    secret = "AAPL position 1500 shares at account 99-123 cost basis 41234.55"
    ab = Abstention(Why.MODEL_ABSTAINED, (secret, "too_long"))
    (e,) = fb.errors_from(TENANT, SUBJ, ab, AT, AT)
    assert e.detail == "redacted; too_long" and "1500" not in e.detail
    long = Abstention(Why.OUTPUT_INVALID, ("x" * 129,))
    assert fb.errors_from(TENANT, SUBJ, long, AT, AT)[0].detail == "redacted"


# ---- dismissal is not a losing trade; outcome categories (spec §7, §14) ---------


def test_dismissed_or_expired_proposal_is_no_trade_outcome() -> None:
    d = dismiss(active(ver()), 1, cur())
    out = fb.outcome_of(d.proposal, 1, "out-1", AT + H)
    assert out.disposition is fb.Disposition.DISMISSED
    assert out.result is fb.Result.NOT_APPLICABLE and out.basis is fb.Basis.NONE
    assert out.fact is None and out.tenant_id == TENANT
    assert out.subject.kind is fb.SubjectKind.PROPOSAL
    assert out.subject.content_hash == d.proposal.latest.content_hash
    gone = expire(active(ver()), AT + timedelta(hours=2))
    assert fb.outcome_of(gone, 1, "out-2", AT + 3 * H).disposition == "expired"
    for result in (fb.Result.LOSS, fb.Result.GAIN, fb.Result.FLAT, fb.Result.UNKNOWN):
        with pytest.raises(fb.FeedbackError, match="no_execution"):
            replace(out, result=result)


def test_planned_is_not_an_execution_and_carries_no_result() -> None:
    d = mark_planned(active(ver()), 1, cur())
    out = fb.outcome_of(d.proposal, 1, "out-1", AT + H)
    assert out.disposition is fb.Disposition.PLANNED
    assert out.result is fb.Result.NOT_APPLICABLE
    with pytest.raises(fb.FeedbackError, match="live"):
        fb.outcome_of(active(ver()), 1, "out-2", AT + H)  # still open: no outcome
    for n in (0, -1, 2):
        with pytest.raises(fb.FeedbackError, match="version"):
            fb.outcome_of(d.proposal, n, "out-3", AT + H)
    with pytest.raises(fb.FeedbackError, match="subject"):  # facts bind proposals
        replace(
            outcome(fb.Disposition.RECONCILED, fb.Result.UNKNOWN, None), subject=SUBJ
        )


def outcome(disp: fb.Disposition, result: fb.Result,
            fact: fb.FactRef | None) -> fb.Outcome:  # fmt: skip
    return fb.Outcome(
        tenant_id=TENANT, record_id="o", subject=PROP, occurred_at=AT, recorded_at=AT,
        disposition=disp, result=result, fact=fact,
    )  # fmt: skip


def test_results_need_a_recorded_fact_of_the_matching_basis() -> None:
    D, R, K = fb.Disposition, fb.Result, fb.FactKind
    report = fb.FactRef(K.EXECUTION_REPORT, "exec-1")
    journal = fb.FactRef(K.JOURNAL_EVENT, "evt-1")
    virtual = fb.FactRef(K.VIRTUAL_ENTRY, "vfill-1")
    real = outcome(D.USER_REPORTED, R.LOSS, report)
    assert real.basis is fb.Basis.REAL
    assert outcome(D.RECONCILED, R.GAIN, journal).basis is fb.Basis.REAL
    assert outcome(D.VIRTUAL_FILLED, R.LOSS, virtual).basis is fb.Basis.VIRTUAL
    assert outcome(D.USER_REPORTED, R.UNKNOWN, None).fact is None
    # Every other disposition, result and fact combination: the property below.
    with pytest.raises(fb.FeedbackError, match="no_execution"):
        outcome(D.DISMISSED, R.NOT_APPLICABLE, report)
    with pytest.raises(fb.FeedbackError, match="result"):
        outcome(D.RECONCILED, R.NOT_APPLICABLE, journal)


def test_tally_keeps_virtual_and_real_apart() -> None:
    D, R, K = fb.Disposition, fb.Result, fb.FactKind
    rows = [
        outcome(D.RECONCILED, R.LOSS, fb.FactRef(K.JOURNAL_EVENT, "e1")),
        outcome(D.VIRTUAL_FILLED, R.LOSS, fb.FactRef(K.VIRTUAL_ENTRY, "v1")),
        outcome(D.VIRTUAL_FILLED, R.LOSS, fb.FactRef(K.VIRTUAL_ENTRY, "v2")),
        outcome(D.DISMISSED, R.NOT_APPLICABLE, None),
    ]
    assert dict(fb.tally(rows)) == {
        (fb.Basis.REAL, R.LOSS): 1, (fb.Basis.VIRTUAL, R.LOSS): 2,
        (fb.Basis.NONE, R.NOT_APPLICABLE): 1,
    }  # fmt: skip


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(st.sampled_from(list(fb.Disposition)), st.sampled_from(list(fb.Result)),
       st.sampled_from([None, *fb.FactKind]))  # fmt: skip
def test_no_result_without_a_matching_fact(
    disp: fb.Disposition, result: fb.Result, kind: fb.FactKind | None
) -> None:
    fact = None if kind is None else fb.FactRef(kind, "ref-1")
    allowed = {fb.Disposition.USER_REPORTED: fb.FactKind.EXECUTION_REPORT,
               fb.Disposition.BROKER_CONFIRMED: fb.FactKind.JOURNAL_EVENT,
               fb.Disposition.RECONCILED: fb.FactKind.JOURNAL_EVENT,
               fb.Disposition.VIRTUAL_FILLED: fb.FactKind.VIRTUAL_ENTRY}  # fmt: skip
    if disp not in allowed:
        ok = result is fb.Result.NOT_APPLICABLE and fact is None
    elif result is fb.Result.NOT_APPLICABLE:
        ok = False
    elif result is fb.Result.UNKNOWN:
        ok = fact is None or kind is allowed[disp]
    else:
        ok = kind is allowed[disp]
    try:
        o = outcome(disp, result, fact)
    except fb.FeedbackError:
        assert not ok
    else:
        assert ok
        virtual = disp is fb.Disposition.VIRTUAL_FILLED
        assert (o.basis is fb.Basis.VIRTUAL) is virtual


# ---- errors from the evaluator --------------------------------------------------


def review(**kw: Any) -> Review:
    base: dict[str, Any] = {
        "review_id": "rev-1", "proposal_id": "prop-a", "proposal_version": 1,
        "proposal_hash": HASH, "reviewer_run_id": "r-ev", "researcher_run_id": "r-rs",
        "model_family_distinct": True, "initial_blind_assessment": "SYNTHETIC",
        "decision": Decision.REVISE, "claim_findings": (), "introduced_claim_ids": (),
        "revision_number": 0, "completed_at": AT, "expires_at": AT + H,
        "cost": UsdBudget("0.01"), "rationale": "SYNTHETIC", "alternatives": ("hold",),
        "material": ("conflict:c1:p2", "no_supported_fact"), "disclosures": (),
        "rejected_claims": ("added.x:not_read",),
    }  # fmt: skip
    return Review(**{**base, **kw})


def test_errors_capture_disagreements_rejections_and_abstentions() -> None:
    got = fb.errors_from(TENANT, SUBJ, review(), AT, AT + H)
    assert [(e.error_kind, e.code) for e in got] == [
        (fb.ErrorKind.EVALUATOR_DISAGREEMENT, "conflict"),
        (fb.ErrorKind.EVALUATOR_DISAGREEMENT, "no_supported_fact"),
        (fb.ErrorKind.MODEL_ERROR, "claim_rejected"),
    ]
    assert all(e.occurred_at == AT and e.recorded_at == AT + H for e in got)
    # A retry derives the same ids and keeps the original times.
    again = fb.errors_from(TENANT, SUBJ, review(), AT, AT + 2 * H)
    assert [e.record_id for e in again] == [e.record_id for e in got]
    s = fb.ImprovementEvidence()
    for e in got:
        s.record(e)
    for e in again:
        assert s.record(e).recorded_at == AT + H
    ab = fb.errors_from(TENANT, SUBJ, Abstention(Why.MODEL_FAILED, ("timeout",)), AT,
                        AT)  # fmt: skip
    assert [(e.error_kind, e.code, e.detail) for e in ab] == [
        (fb.ErrorKind.ABSTENTION, "model_failed", "timeout")]  # fmt: skip
    assert fb.errors_from(TENANT, SUBJ, review(material=(), rejected_claims=()), AT,
                          AT) == ()  # fmt: skip


def test_record_is_append_only_and_tenant_clocked() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback())
    assert s.record(feedback()) == feedback()
    with pytest.raises(fb.FeedbackError, match="conflicting_duplicate"):
        s.record(feedback(message="changed"))
    with pytest.raises(fb.FeedbackError, match="time_order"):
        s.record(feedback("fb-2", at=AT - H))
    s.record(feedback("fb-2", tenant=OTHER, at=AT - H))  # another tenant's clock
    with pytest.raises(fb.FeedbackError, match="subject"):
        fb.Subject(fb.SubjectKind.THESIS, "t", 0, HASH)
    with pytest.raises(fb.FeedbackError, match="subject"):
        fb.Subject(fb.SubjectKind.THESIS, "t", 1, "nothex")


# ---- private evaluation cases: minimised, frozen, holdout tracked (R040, R084) ---


def test_cases_hold_only_permitted_fields() -> None:
    c = case(attribute="revenue", evidence_digest="b" * 64)
    assert c.case_hash == case(evidence_digest="b" * 64, attribute="revenue").case_hash
    assert c.personal_history
    for key in ("account_id", "quantity", "cash", "price", "message", "prompt"):
        with pytest.raises(fb.FeedbackError, match="field_not_permitted"):
            replace(case(), inputs={key: "1"})
    with pytest.raises(fb.FeedbackError, match="evidence_digest"):
        case(evidence_digest="raw passage text")
    synth = replace(c, origin=fb.CaseOrigin.SYNTHETIC)
    assert not synth.personal_history  # never counts as personal learned history
    with pytest.raises(TypeError):
        c.inputs["attribute"] = "x"  # type: ignore[index]


def test_final_holdout_is_never_development_and_read_once_per_experiment() -> None:
    s = fb.ImprovementEvidence()
    s.record(case("dev-1"))
    s.record(case("final-1", fb.Split.FINAL_HOLDOUT))
    dev = s.select(TENANT, "exp-1", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                   AT + H)  # fmt: skip
    assert [r.record_id for r in dev.records] == ["dev-1"]
    assert why(dev, "final-1") == "final_holdout"
    fin = s.select(TENANT, "exp-1", P.PERSONAL_ADAPTATION, fb.Access.FINAL_ASSESSMENT,
                   AT + 2 * H)  # fmt: skip
    assert [r.record_id for r in fin.records] == ["final-1"]
    assert why(fin, "dev-1") == "not_final_holdout"
    with pytest.raises(fb.FeedbackError, match="final_holdout"):
        s.get(TENANT, "final-1")  # only `select` reads holdout cases, and logs it
    again = s.select(TENANT, "exp-1", P.PERSONAL_ADAPTATION,
                     fb.Access.FINAL_ASSESSMENT, AT + 3 * H)  # fmt: skip
    assert again.records == () and why(again, "final-1") == "holdout_reused"
    assert [u.experiment_id for u in s.uses(TENANT)] == ["exp-1"] * 3


# ---- deletion and tombstones (R052, R087) ----------------------------------------


def test_deletion_drops_content_and_flags_earlier_uses() -> None:
    s = fb.ImprovementEvidence()
    s.record(feedback())
    s.record(feedback("fb-2"))
    used = s.select(TENANT, "exp-1", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                    AT + H)  # fmt: skip
    assert len(used.records) == 2
    s.delete(TENANT, "fb-1", AT + 2 * H)
    with pytest.raises(fb.FeedbackError, match="deleted"):
        s.get(TENANT, "fb-1")
    later = s.select(TENANT, "exp-2", P.PERSONAL_ADAPTATION, fb.Access.DEVELOPMENT,
                     AT + 3 * H)  # fmt: skip
    assert [r.record_id for r in later.records] == ["fb-2"]
    assert [(u.use.experiment_id, u.reason) for u in s.affected_uses(
        TENANT, AT + 3 * H)] == [("exp-1", "record_deleted")]  # fmt: skip
    with pytest.raises(fb.FeedbackError, match="deleted"):
        s.record(feedback())  # a deleted id is never reused
    with pytest.raises(fb.FeedbackError, match="not_found"):
        s.delete(OTHER, "fb-2", AT + 4 * H)


# ---- evidence capture only: no path into live decisions --------------------------


def test_module_has_no_live_decision_path() -> None:
    src = Path(fb.__file__).read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.ImportFrom) for a in n.names}  # fmt: skip
    live = {"step", "publish", "mark_planned", "dismiss", "revise", "settle",
            "final_check", "evaluate", "ProviderAdapter", "GatewayContext"}  # fmt: skip
    assert not names & live
    assert {p.value for p in fb.Purpose} == {"personal_adaptation",
                                             "shared_improvement"}  # fmt: skip
