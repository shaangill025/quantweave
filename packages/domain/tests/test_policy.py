"""Draft policy inference, versions, adoption receipts and the sizing gate (T019).

SYNTHETIC inputs only: hand-built responses and account facts with made-up ids. The
catalogue is docs/spec/config/onboarding_questions.json. Hashes are compared for
change or equality, never against a copy of the implementation.
"""

import contextlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.decimals import Ratio
from qw_domain.instants import InstantError
from qw_domain.onboarding import Catalogue, LimitMetric, Responses, load_catalogue
from qw_domain.policy import (
    REQUIRED_METRICS,
    AccountFacts,
    AdoptionConsent,
    ConflictCode,
    FeasibilityBound,
    OptionPermission,
    PolicyError,
    PolicyHistory,
    SizingBlock,
    SizingRequest,
    build_draft,
    check_sizing,
)
from qw_domain.scopes import ScopeKind

CONFIG = Path(__file__).resolve().parents[3] / "docs/spec/config"
AS_OF = date(2026, 10, 9)
AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
TENANT = "tenant-synth"


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    return load_catalogue(
        json.loads((CONFIG / "onboarding_questions.json").read_text())
    )


def limit(metric: str, value: str = "0.05", **kw: str) -> dict[str, str]:
    base = {"metric": metric, "value": value, "unit": "ratio", "window": "rolling_30d"}
    scope = {"denominator": "account_nav", "account_id": "acct-1", "currency": "CAD"}
    return {**base, **scope, **kw}


ALL_LIMITS = [limit(m) for m in sorted(REQUIRED_METRICS)]
ALLOC = [{"account_id": "acct-1", "currency": "CAD", "amount": "5000"}]
BASE: dict[str, Any] = {
    "ONB01": "selected_accounts",
    "ONB03": "CAD",
    "ONB04": ["growth", "learning"],
    "ONB05": "gt_7y",
    "ONB06": "none_known",
    "ONB07": "financially_manageable",
    "ONB10": "weekly",
    "ONB11": ["stocks", "etfs"],
    "ONB12": ["long_term"],
    "ONB13": ALLOC,
    "ONB14": ALL_LIMITS,
    "ONB16": "rules_only",
}
FACTS = (AccountFacts("acct-1", OptionPermission.GRANTED, has_short_option=False),)


def answers(cat: Catalogue, **changes: Any) -> Responses:
    raw = {**BASE, **changes}
    return cat.validate({k: v for k, v in raw.items() if v is not None})


def consent(*acks: ConflictCode, who: str = "user-1") -> AdoptionConsent:
    return AdoptionConsent(who, "stepup-1", AT, frozenset(acks), "I adopt this")


def adopted(cat: Catalogue, **changes: Any) -> PolicyHistory:
    draft = build_draft(TENANT, answers(cat, **changes), FACTS, as_of=AS_OF)
    return PolicyHistory.new("pol-1", TENANT).propose(draft).adopt(1, consent())


def codes(draft: Any) -> set[ConflictCode]:
    return {c.code for c in draft.conflicts}


def test_required_metrics_match_risk_policy_draft() -> None:
    draft = json.loads((CONFIG / "risk_policy_draft.json").read_text())
    assert set(draft["limits"]) == set(REQUIRED_METRICS)
    assert all(v is None for v in draft["limits"].values())  # the template is null


# --- 2. inference with provenance; limits stay null -----------------------------


def test_inferred_fields_record_supporting_responses(cat: Catalogue) -> None:
    draft = build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    fields = {f.name: f for f in draft.fields}
    assert fields["horizons"].value == ("long_term",)
    assert fields["horizons"].sources == ("ONB12", "ONB10")
    assert fields["instruments"].sources == ("ONB11",)
    assert fields["reporting_currency"].value == "CAD"
    assert all(f.sources for f in draft.fields)


def test_limits_null_until_user_enters_them(cat: Catalogue) -> None:
    draft = build_draft(TENANT, answers(cat, ONB14=None), FACTS, as_of=AS_OF)
    assert draft.limits == ()  # no template fills them, ONB07 notwithstanding
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    with pytest.raises(PolicyError, match="limits_required"):
        history.adopt(1, consent())


# --- 3. contradiction rules ------------------------------------------------------

NEAR_NEED = {"amount": "20000", "currency": "CAD", "date": "2027-03-01"}


def test_preservation_near_term_need_and_intraday_is_blocking(cat: Catalogue) -> None:
    r = answers(
        cat,
        ONB04=["capital_preservation", "active_trading"],
        ONB06="known_dated_need",
        **{"ONB06.need": NEAR_NEED},
        ONB07="essential_spending_affected",
        ONB10="regular_session",
        ONB12=["long_term", "same_day"],
    )
    draft = build_draft(TENANT, r, FACTS, as_of=AS_OF)
    (conflict,) = draft.conflicts
    assert conflict.code is ConflictCode.PRESERVATION_VS_INTRADAY and conflict.blocking
    assert "ONB07" in conflict.sources and "ONB06.need" in conflict.sources
    # Not averaged: the declared objectives and horizons are kept as answered.
    assert {f.name: f.value for f in draft.fields}["horizons"] == (
        "long_term",
        "intraday",
    )
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    with pytest.raises(PolicyError, match="conflict_requires_resolution"):
        history.adopt(1, consent(ConflictCode.PRESERVATION_VS_INTRADAY))
    far = {**NEAR_NEED, "date": "2031-01-01"}
    later = answers(
        cat,
        ONB04=["capital_preservation"],
        ONB06="known_dated_need",
        **{"ONB06.need": far},
        ONB07="essential_spending_affected",
        ONB12=["long_term", "same_day"],
        ONB10="regular_session",
    )
    assert codes(build_draft(TENANT, later, FACTS, as_of=AS_OF)) == set()


def test_monitoring_excludes_intraday(cat: Catalogue) -> None:
    draft = build_draft(
        TENANT, answers(cat, ONB12=["long_term", "same_day"]), FACTS, as_of=AS_OF
    )
    assert codes(draft) == {ConflictCode.MONITORING_EXCLUDES_INTRADAY}
    assert [e.subject for e in draft.exclusions] == ["horizon:intraday"]
    assert {f.name: f.value for f in draft.fields}["horizons"] == ("long_term",)
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    with pytest.raises(PolicyError, match="unacknowledged_conflict"):
        history.adopt(1, consent())
    done = history.adopt(1, consent(ConflictCode.MONITORING_EXCLUDES_INTRADAY))
    assert done.adopted is not None
    receipt = done.adopted[1]
    assert receipt.acknowledged == (ConflictCode.MONITORING_EXCLUDES_INTRADAY,)
    assert receipt.exclusions == draft.exclusions


def test_options_off_with_existing_short_option_keeps_warnings(cat: Catalogue) -> None:
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, has_short_option=True),)
    draft = build_draft(TENANT, answers(cat), facts, as_of=AS_OF)
    assert codes(draft) == {ConflictCode.OPTIONS_OFF_EXISTING_SHORT}
    assert "new_option_ideas" in {e.subject for e in draft.exclusions}
    assert draft.retained_warnings == ("option_risk_and_expiry:acct-1",)


def test_unknown_option_permission_blocks_option_proposals(cat: Catalogue) -> None:
    facts = (
        AccountFacts("acct-1", OptionPermission.UNKNOWN, has_short_option=False),
        AccountFacts("acct-2", OptionPermission.GRANTED, has_short_option=False),
    )
    r = answers(cat, ONB11=["stocks", "options"])
    draft = build_draft(TENANT, r, facts, as_of=AS_OF)
    assert codes(draft) == {ConflictCode.OPTION_PERMISSION_UNKNOWN}
    assert [e.subject for e in draft.exclusions] == ["option_proposals:acct-1"]


def test_profit_goal_feasibility_warning(cat: Catalogue) -> None:
    r = answers(cat, **{"ONB04.target_return": "0.40"})
    bound = FeasibilityBound("synthetic-method-v1", Ratio("0.12"))
    infeasible = build_draft(TENANT, r, FACTS, as_of=AS_OF, feasibility=bound)
    (conflict,) = infeasible.conflicts
    assert conflict.code is ConflictCode.PROFIT_GOAL_INFEASIBLE
    assert not conflict.blocking and "synthetic-method-v1" in conflict.sources
    assert (
        infeasible.limits
        == build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF).limits
    )
    unassessed = build_draft(TENANT, r, FACTS, as_of=AS_OF)
    assert codes(unassessed) == {ConflictCode.PROFIT_GOAL_UNASSESSED}
    ok = answers(cat, **{"ONB04.target_return": "0.10"})
    assert (
        codes(build_draft(TENANT, ok, FACTS, as_of=AS_OF, feasibility=bound)) == set()
    )


# --- 4. versioning and synthetic policies ----------------------------------------


def test_answer_change_creates_new_version_not_live_edit(cat: Catalogue) -> None:
    history = adopted(cat)
    version, receipt = history.adopted or pytest.fail("not adopted")
    same = build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    assert history.propose(same) is history  # no semantic change, no new version
    changed = build_draft(
        TENANT,
        answers(
            cat, ONB14=[*ALL_LIMITS[1:], limit(sorted(REQUIRED_METRICS)[0], "0.02")]
        ),
        FACTS,
        as_of=AS_OF,
    )
    newer = history.propose(changed)
    assert [v.version for v in newer.versions] == [1, 2]
    assert newer.adopted == (version, receipt)  # adopted v1 unchanged
    assert newer.status(1) == "adopted" and newer.status(2) == "awaiting_adoption"
    final = newer.adopt(2, consent())
    assert final.status(1) == "superseded" and final.status(2) == "adopted"
    with pytest.raises(PolicyError, match="not_latest"):
        newer.propose(build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)).adopt(
            2, consent()
        )


def test_synthetic_policy_cannot_authorize(cat: Catalogue) -> None:
    history = adopted(cat, ONB01="hypothetical_only")
    assert history.adopted is not None
    version, receipt = history.adopted
    assert version.draft.label == "SYNTHETIC" and receipt.label == "SYNTHETIC"
    decision = check_sizing(history, request())
    assert not decision.allowed
    assert SizingBlock.SYNTHETIC_POLICY in {r.code for r in decision.reasons}
    flagged = build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF, synthetic=True)
    h2 = PolicyHistory.new("pol-2", TENANT).propose(flagged).adopt(1, consent())
    assert not check_sizing(h2, request()).allowed


# --- 5. adoption receipt ------------------------------------------------------------


def test_receipt_is_structured_and_text_never_authorizes(cat: Catalogue) -> None:
    history = adopted(cat)
    assert history.adopted is not None
    version, receipt = history.adopted
    assert (
        receipt.kind == "policy_adoption"
        and receipt.policy_hash == version.content_hash
    )
    assert receipt.principal_id == "user-1" and receipt.signed_at == AT
    assert receipt.limits == version.draft.limits
    first = receipt.limits[0].to_wire()
    assert receipt.to_wire()["limits"] == [x.to_wire() for x in receipt.limits]
    assert first["denominator"] == "account_nav" and first["window"] == "rolling_30d"
    assert first["currency"] == "CAD" and first["value"] == "0.05"
    draft = build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    for bad in [
        AdoptionConsent("", "stepup-1", AT, frozenset(), "yes I accept the plan"),
        AdoptionConsent("user-1", "", AT, frozenset(), "accept recommendations"),
    ]:
        with pytest.raises(PolicyError, match="consent"):
            PolicyHistory.new("pol-1", TENANT).propose(draft).adopt(1, bad)


def test_adoption_requires_limits_for_every_enabled_sizing_scope(
    cat: Catalogue,
) -> None:
    sleeve = {"account_id": "acct-1", "currency": "USD", "sleeve_id": "trading"}
    alloc = [*ALLOC, {**sleeve, "amount": "900"}]
    draft = build_draft(TENANT, answers(cat, ONB13=alloc), FACTS, as_of=AS_OF)
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    with pytest.raises(PolicyError, match="missing_required_limits") as err:
        history.adopt(1, consent())
    assert "acct-1/USD/trading" in str(err.value)
    usd = [
        limit(m, currency="USD", sleeve_id="trading") for m in sorted(REQUIRED_METRICS)
    ]
    ok = build_draft(
        TENANT, answers(cat, ONB13=alloc, ONB14=ALL_LIMITS + usd), FACTS, as_of=AS_OF
    )
    PolicyHistory.new("pol-1", TENANT).propose(ok).adopt(1, consent())


def test_receipt_hash_changes_when_any_limit_changes(cat: Catalogue) -> None:
    base = adopted(cat).adopted
    assert base is not None
    seen = {base[1].receipt_hash, base[0].content_hash}
    for i, field, new in [
        (0, "value", "0.051"),
        (1, "denominator", "sleeve_nav"),
        (2, "window", "rolling_90d"),
        (3, "unit", "amount"),
    ]:
        lims = [dict(x) for x in ALL_LIMITS]
        lims[i][field] = new
        h = adopted(cat, ONB14=lims).adopted
        assert h is not None
        assert h[1].receipt_hash not in seen and h[0].content_hash not in seen
        seen |= {h[1].receipt_hash, h[0].content_hash}
    again = adopted(cat).adopted
    assert again is not None and again[1].receipt_hash == base[1].receipt_hash


# --- 6. sizing gate --------------------------------------------------------------


def request(**kw: str | None) -> SizingRequest:
    args: dict[str, Any] = {
        "tenant_id": TENANT,
        "account_id": "acct-1",
        "currency": "CAD",
        "sleeve_id": None,
        "denominator": "account_nav",
    }
    args.update(kw)
    return SizingRequest(**args)


def test_sizing_gate(cat: Catalogue) -> None:
    history = adopted(cat)
    assert check_sizing(history, request()).allowed
    cases = [
        (None, request(), SizingBlock.NO_ADOPTED_POLICY),
        (history, request(currency=None), SizingBlock.CURRENCY_MISSING),
        (history, request(denominator=None), SizingBlock.DENOMINATOR_MISSING),
        (history, request(currency="USD"), SizingBlock.SCOPE_NOT_ALLOCATED),
        (history, request(tenant_id="tenant-other"), SizingBlock.TENANT_MISMATCH),
    ]
    for hist, req, code in cases:
        decision = check_sizing(hist, req)
        assert not decision.allowed and code in {r.code for r in decision.reasons}
    proposed_only = PolicyHistory.new("pol-1", TENANT).propose(
        build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    )
    assert {r.code for r in check_sizing(proposed_only, request()).reasons} == {
        SizingBlock.NO_ADOPTED_POLICY
    }


def test_null_limits_block_sizing_for_unlimited_scope(cat: Catalogue) -> None:
    # acct-1 limits do not cover an acct-2 allocation.
    alloc = [*ALLOC, {"account_id": "acct-2", "currency": "CAD", "amount": "100"}]
    draft = build_draft(TENANT, answers(cat, ONB13=alloc), FACTS, as_of=AS_OF)
    with pytest.raises(PolicyError, match="missing_required_limits"):
        PolicyHistory.new("pol-1", TENANT).propose(draft).adopt(1, consent())
    history = adopted(cat)
    decision = check_sizing(history, request(account_id="acct-2"))
    blocks = {r.code for r in decision.reasons}
    assert {SizingBlock.SCOPE_NOT_ALLOCATED, SizingBlock.LIMIT_MISSING} <= blocks


# --- 7. property: no risk limit the user did not enter ----------------------------

SCOPES = [("acct-1", "CAD"), ("acct-2", "USD")]
LOSS = "essential_spending_affected goal_delayed financially_manageable uncertain"
METRIC_SETS = st.sets(st.sampled_from(sorted(LimitMetric)))  # often all six
VALUE = st.sampled_from(["0.01", "0.2", "1"])
limits_st = st.builds(
    lambda a, b, v: [
        limit(m, v, account_id=acct, currency=cur)
        for (acct, cur), metrics in zip(SCOPES, (a, b), strict=True)
        for m in sorted(metrics)
    ],
    METRIC_SETS,
    METRIC_SETS,
    VALUE,
)


def key(wire: dict[str, str | None]) -> str:
    return json.dumps({k: v for k, v in wire.items() if v is not None}, sort_keys=True)


@settings(derandomize=True, database=None, max_examples=200, deadline=None)
@given(
    limits=st.one_of(
        st.just("not_sure"),
        limits_st,
    ),
    allocs=st.lists(st.sampled_from(SCOPES), min_size=1, unique=True),
    loss=st.sampled_from([*LOSS.split(), "not_sure"]),
    scope=st.sampled_from(list(ScopeKind)),
    same_day=st.booleans(),
)
def test_property_no_limit_without_user_entry(
    cat: Catalogue,
    limits: Any,
    allocs: list[tuple[str, str]],
    loss: str,
    scope: str,
    same_day: bool,
) -> None:
    raw_allocs = [{"account_id": a, "currency": c, "amount": "100"} for a, c in allocs]
    horizons = ["long_term", "same_day"] if same_day else ["long_term"]
    r = answers(
        cat, ONB14=limits, ONB13=raw_allocs, ONB07=loss, ONB01=scope, ONB12=horizons
    )
    entered = (
        [x for x in limits if isinstance(x, dict)] if isinstance(limits, list) else []
    )
    draft = build_draft(TENANT, r, FACTS, as_of=AS_OF)
    history = PolicyHistory.new("pol-1", TENANT).propose(draft)
    with contextlib.suppress(PolicyError):
        history = history.adopt(1, consent(*{c.code for c in draft.conflicts}))
    # Independent oracle: adoptable iff every allocated scope has all six entered.
    covered = all(
        {d["metric"] for d in entered if (d["account_id"], d["currency"]) == s}
        >= REQUIRED_METRICS
        for s in allocs
    )
    assert (history.adopted is not None) == covered
    allowed = {key(x) for x in entered}
    assert {key(x.to_wire()) for x in draft.limits} <= allowed
    if history.adopted is not None:
        assert {key(x.to_wire()) for x in history.adopted[1].limits} <= allowed


# --- review fixes (B1, S1-S3, N1-N4) -------------------------------------------


def test_sizing_denominator_must_match_limits(cat: Catalogue) -> None:
    decision = check_sizing(adopted(cat), request(denominator="portfolio_nav"))
    assert not decision.allowed
    assert {r.code for r in decision.reasons} <= {
        SizingBlock.DENOMINATOR_MISSING,
        SizingBlock.LIMIT_MISSING,
    }
    lims = [
        limit(m, denominator="sleeve_nav") if m == "stress_loss" else limit(m)
        for m in sorted(REQUIRED_METRICS)
    ]
    mixed = check_sizing(adopted(cat, ONB14=lims), request())
    assert not mixed.allowed and "stress_loss" in {r.detail for r in mixed.reasons}


def test_tenant_mismatch_leaks_nothing(cat: Catalogue) -> None:
    for req in (
        request(tenant_id="tenant-other"),
        request(tenant_id="tenant-other", account_id="acct-9", denominator=None),
    ):
        decision = check_sizing(adopted(cat), req)
        assert decision.allowed is False and decision.policy_hash is None
        assert [(r.code, r.detail) for r in decision.reasons] == [
            (SizingBlock.TENANT_MISMATCH, "")
        ]


def test_missing_or_denied_option_permission_excludes_options(cat: Catalogue) -> None:
    on = answers(cat, ONB11=["stocks", "options"])
    unknown = build_draft(TENANT, on, (), as_of=AS_OF)  # no facts for allocated acct-1
    assert codes(unknown) == {ConflictCode.OPTION_PERMISSION_UNKNOWN}
    assert [e.subject for e in unknown.exclusions] == ["option_proposals:acct-1"]
    denied = (AccountFacts("acct-1", OptionPermission.DENIED, has_short_option=False),)
    draft = build_draft(TENANT, on, denied, as_of=AS_OF)
    assert [e.subject for e in draft.exclusions] == ["option_proposals:acct-1"]


def test_adopt_rejects_naive_time_and_bad_text(cat: Catalogue) -> None:
    history = PolicyHistory.new("pol-1", TENANT).propose(
        build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    )
    with pytest.raises(InstantError):
        naive = datetime(2026, 10, 9, 15)  # noqa: DTZ001
        history.adopt(
            1, AdoptionConsent("user-1", "stepup-1", naive, frozenset(), "ok")
        )
    for text in ("lone \ud800 surrogate", 5):
        bad = AdoptionConsent("user-1", "stepup-1", AT, frozenset(), text)  # type: ignore[arg-type]
        with pytest.raises(PolicyError, match="consent"):
            history.adopt(1, bad)


def test_hash_covers_warnings_and_ignores_order(cat: Catalogue) -> None:
    draft = build_draft(TENANT, answers(cat), FACTS, as_of=AS_OF)
    assert replace(draft, retained_warnings=("x",)).content_hash != draft.content_hash
    alloc = [*ALLOC, {"account_id": "acct-1", "currency": "USD", "amount": "9"}]
    history = PolicyHistory.new("pol-1", TENANT).propose(
        build_draft(TENANT, answers(cat, ONB13=alloc), FACTS, as_of=AS_OF)
    )
    flipped = answers(cat, ONB13=alloc[::-1], ONB14=ALL_LIMITS[::-1])
    assert history.propose(build_draft(TENANT, flipped, FACTS, as_of=AS_OF)) is history


def test_build_draft_requires_a_date(cat: Catalogue) -> None:
    with pytest.raises(TypeError, match="date"):
        build_draft(TENANT, answers(cat), FACTS, as_of=AT)


def test_unknown_need_or_capacity_fails_closed_for_intraday(cat: Catalogue) -> None:
    risky = {"ONB04": ["capital_preservation", "active_trading"], "ONB05": "lt_1y"}
    risky |= {"ONB10": "regular_session", "ONB12": ["long_term", "same_day"]}
    risky |= {"ONB06": "known_dated_need", "ONB07": "essential_spending_affected"}
    for unknown in ({"ONB06": "not_sure"}, {"ONB07": "not_sure"}):
        draft = build_draft(
            TENANT, answers(cat, **(risky | unknown)), FACTS, as_of=AS_OF
        )
        (conflict,) = draft.conflicts
        assert conflict.code is ConflictCode.PRESERVATION_NEED_UNKNOWN
        assert not conflict.blocking and set(unknown) <= set(conflict.sources)
        assert [e.subject for e in draft.exclusions] == ["horizon:intraday"]
