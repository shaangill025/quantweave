"""Trial ledger and split access: preregistered chronological splits, sealed
promotion holdout, full trial registration and evidence binding (T032 increment 1).

All data is SYNTHETIC (see test_research_data); expected values are hand-written.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from qw_domain.research_data import (
    Change,
    DatasetRefused,
    EvidenceClass,
    ResearchDataset,
    ResearchError,
    Window,
)
from qw_domain.rights import Registry
from qw_domain.strategy_gate import EvidenceRecord
from qw_domain.trial_ledger import (
    Access,
    Candidate,
    Outcome,
    Role,
    Split,
    SplitPlan,
    Trial,
    TrialLedger,
    open_view,
)
from test_research_data import AS_OF, EVENTS, ORIGINAL, event, freeze, iid, rights

TENANT = "tenant-synth-a"
MAT, CFG = "a" * 64, "b" * 64
CAND = Candidate("fam-momentum", MAT, CFG)
TRAIN = Window(datetime(2020, 1, 1, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC))
DEV = Window(datetime(2022, 2, 1, tzinfo=UTC), datetime(2023, 6, 1, tzinfo=UTC))
PROMO = Window(datetime(2023, 7, 1, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC))
T0 = AS_OF + timedelta(days=1)
DS = freeze(
    facts=(ORIGINAL,),
    events=(
        *EVENTS,
        event(3, Change.ADD, date(2020, 1, 2), datetime(2020, 1, 2, tzinfo=UTC)),
    ),
)


# SYNTHETIC content labelled historical only to show the flag follows the manifest.
DS_LABELLED_HISTORICAL = freeze(
    facts=(ORIGINAL,), evidence_class=EvidenceClass.HISTORICAL
)


def plan(**kw: object) -> SplitPlan:
    args: dict[str, object] = {
        "plan_id": "plan-synth-1", "dataset": DS.manifest,
        "train": TRAIN, "development": DEV, "promotion": PROMO,
        "embargo": timedelta(days=30), "holdout_instruments": frozenset({iid(3)}),
        "max_promotion_accesses": 1, "approved_by": "owner-synth", "registered_at": T0,
    } | kw  # fmt: skip
    return SplitPlan(**args)  # type: ignore[arg-type]


def ledger(p: SplitPlan | None = None, ds: ResearchDataset = DS) -> TrialLedger:
    return TrialLedger(TENANT).register_plan(p or plan(), ds)


def ask(
    lg: TrialLedger, n: int, split: Split, window: Window,
    role: Role = Role.RESEARCHER, instruments: frozenset[object] | None = None,
    candidate: Candidate = CAND,
) -> tuple[TrialLedger, Access]:  # fmt: skip
    return lg.request_access(f"acc-{n}", "plan-synth-1", f"trial-{n}", candidate,
                             split, window, instruments,  # type: ignore[arg-type]
                             "actor-synth", role,
                             T0 + timedelta(hours=n))  # fmt: skip


def trial(
    n: int, split: Split, outcome: Outcome = Outcome.COMPLETED, **kw: object
) -> Trial:
    args: dict[str, object] = {
        "trial_id": f"trial-{n}", "plan_id": "plan-synth-1", "family": "fam-momentum",
        "hypothesis": "SYNTHETIC: 12-1 momentum ranks predict next-month returns",
        "material_hash": MAT, "config_hash": CFG, "split": split, "outcome": outcome,
        "dataset_hash": DS.manifest.content_hash,
        "metrics": {"net_return": Decimal("0.0125")},
        "access_id": f"acc-{n}", "recorded_at": T0 + timedelta(hours=n, minutes=30),
    } | kw  # fmt: skip
    return Trial(**args)  # type: ignore[arg-type]


def test_plan_must_be_chronological_with_embargo_and_inside_the_dataset() -> None:
    with pytest.raises(ResearchError, match="chronology"):
        plan(embargo=timedelta(days=40))  # train end + 40d > dev start
    with pytest.raises(ResearchError, match="chronology"):
        plan(train=DEV, development=TRAIN)
    with pytest.raises(ResearchError, match="observed"):
        plan(promotion=Window(PROMO.start, AS_OF + timedelta(days=1)))
    with pytest.raises(ResearchError, match="max_promotion_accesses"):
        plan(max_promotion_accesses=0)


def test_train_and_dev_access_cannot_touch_the_holdout_range_or_names() -> None:
    lg, a = ask(ledger(), 1, Split.TRAIN, TRAIN)
    assert a.granted and a.reasons == ()
    lg, b = ask(
        lg, 2, Split.DEVELOPMENT, Window(DEV.start, PROMO.start + timedelta(days=1))
    )
    assert not b.granted and b.reasons == ("outside_split_window",)
    lg, c = ask(lg, 3, Split.TRAIN, TRAIN, instruments=frozenset({iid(1), iid(3)}))
    assert not c.granted and c.reasons == ("holdout_instrument",)
    assert len(lg.accesses()) == 3  # refusals are recorded too


def test_view_filters_holdout_names_and_bounds_decision_time() -> None:
    lg, _ = ask(ledger(), 1, Split.TRAIN, TRAIN)
    view = open_view(lg, DS, "acc-1", rights())
    assert view.members(date(2021, 6, 1), datetime(2021, 6, 1, tzinfo=UTC)) == {iid(1)}
    with pytest.raises(ResearchError, match="outside_access_window"):
        view.facts_at(DEV.start)  # dev-window time through a train access
    with pytest.raises(ResearchError, match="access_refused"):
        open_view(ask(lg, 2, Split.TRAIN, PROMO)[0], DS, "acc-2", rights())


def test_promotion_holdout_is_assessor_only_and_count_limited() -> None:
    lg, a = ask(ledger(), 1, Split.PROMOTION, PROMO)
    assert not a.granted and a.reasons == ("assessor_only",)
    lg, b = ask(lg, 2, Split.PROMOTION, PROMO, Role.ASSESSOR)
    assert b.granted and b.seq == 1
    lg, c = ask(lg, 3, Split.PROMOTION, PROMO, Role.ASSESSOR)
    assert not c.granted and c.reasons == ("holdout_exhausted",)
    view = open_view(lg, DS, "acc-2", rights())
    assert iid(3) in view.members(date(2024, 1, 2), datetime(2024, 1, 2, tzinfo=UTC))


def test_reused_holdout_trial_cannot_become_evidence() -> None:
    lg = ledger(plan(max_promotion_accesses=2))
    lg, _ = ask(lg, 1, Split.PROMOTION, PROMO, Role.ASSESSOR)
    lg = lg.record(trial(1, Split.PROMOTION))
    lg, second = ask(lg, 2, Split.PROMOTION, PROMO, Role.ASSESSOR)
    assert second.granted and second.seq == 2
    lg = lg.record(trial(2, Split.PROMOTION))
    assert lg.evidence_record("trial-1") == EvidenceRecord(
        "trial-1", MAT, CFG, None, True, T0 + timedelta(hours=1, minutes=30)
    )
    with pytest.raises(ResearchError, match="holdout_reused"):
        lg.evidence_record("trial-2")


def test_development_trials_and_synthetic_data_are_not_qualifying() -> None:
    lg, _ = ask(ledger(), 1, Split.DEVELOPMENT, DEV)
    lg = lg.record(trial(1, Split.DEVELOPMENT))
    with pytest.raises(ResearchError, match="not_promotion"):
        lg.evidence_record("trial-1")


def test_evidence_class_comes_from_the_dataset_manifest() -> None:
    with pytest.raises(TypeError):
        plan(evidence_class=EvidenceClass.HISTORICAL)  # a plan cannot declare one
    lg, _ = ask(ledger(), 1, Split.PROMOTION, PROMO, Role.ASSESSOR)
    assert lg.record(trial(1, Split.PROMOTION)).evidence_record("trial-1").synthetic
    hist = plan(dataset=DS_LABELLED_HISTORICAL.manifest)
    lg, _ = ask(
        ledger(hist, DS_LABELLED_HISTORICAL), 2, Split.PROMOTION, PROMO, Role.ASSESSOR
    )
    h = DS_LABELLED_HISTORICAL.manifest.content_hash
    rec = lg.record(trial(2, Split.PROMOTION, dataset_hash=h)).evidence_record(
        "trial-2"
    )
    assert rec.synthetic is False
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        open_view(lg, DS, "acc-2", rights())  # synthetic data under a historical plan
    relabelled = replace(DS.manifest, evidence_class=EvidenceClass.HISTORICAL)
    assert relabelled.content_hash == DS.manifest.content_hash
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        ledger(
            plan(dataset=relabelled)
        )  # same hash, relabelled: refused at registration
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        ledger(plan(), DS_LABELLED_HISTORICAL)  # plan for another dataset
    stored = TrialLedger(TENANT, (plan(dataset=relabelled),))  # bypasses registration
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        open_view(ask(stored, 3, Split.TRAIN, TRAIN)[0], DS, "acc-3", rights())


def test_trial_must_match_the_candidate_its_access_was_granted_for() -> None:
    lg, _ = ask(ledger(), 1, Split.PROMOTION, PROMO, Role.ASSESSOR)
    changes: list[dict[str, object]] = [
        {"material_hash": "c" * 64},
        {"config_hash": "c" * 64},
        {"family": "fam-value"},
    ]
    for kw in changes:
        with pytest.raises(ResearchError, match="candidate_mismatch"):
            lg.record(trial(1, Split.PROMOTION, Outcome.COMPLETED, **kw))
    assert lg.record(trial(1, Split.PROMOTION)).trial_count("fam-momentum") == 1


def test_granted_accesses_without_a_trial_still_count() -> None:
    lg, a = ask(ledger(), 1, Split.TRAIN, TRAIN)
    lg, _ = ask(lg, 2, Split.TRAIN, PROMO)  # refused: saw no data, not counted
    lg, _ = ask(lg, 3, Split.TRAIN, TRAIN, candidate=Candidate("fam-value", MAT, CFG))
    assert lg.unrecorded_accesses("fam-momentum") == (a,)
    assert (lg.trial_count("fam-momentum"), lg.trial_count("fam-value")) == (1, 1)
    lg = lg.record(trial(1, Split.TRAIN, recorded_at=T0 + timedelta(hours=4)))
    assert lg.unrecorded_accesses("fam-momentum") == ()
    assert lg.trial_count("fam-momentum") == 1


def test_every_trial_is_counted_including_failed_and_abandoned() -> None:
    lg, _ = ask(ledger(), 1, Split.TRAIN, TRAIN)
    lg = lg.record(trial(1, Split.TRAIN))
    lg, _ = ask(lg, 2, Split.TRAIN, TRAIN)
    lg = lg.record(trial(2, Split.TRAIN, Outcome.FAILED, metrics={}))
    lg = lg.record(trial(3, Split.TRAIN, Outcome.ABANDONED, metrics={}, access_id=None))
    abandoned = Outcome.ABANDONED
    lg = lg.record(trial(4, Split.TRAIN, abandoned, family="fam-value", access_id=None))
    assert (lg.trial_count("fam-momentum"), lg.trial_count("fam-value")) == (3, 1)
    assert lg.trials()[0].metrics_wire() == {"net_return": "0.0125"}


def test_trial_binding_rules() -> None:
    lg, _ = ask(ledger(), 1, Split.TRAIN, TRAIN)
    with pytest.raises(ResearchError, match="access_required"):
        lg.record(trial(1, Split.TRAIN, access_id=None))
    with pytest.raises(ResearchError, match="access_mismatch"):
        lg.record(trial(1, Split.DEVELOPMENT))
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        lg.record(trial(1, Split.TRAIN, dataset_hash="c" * 64))
    with pytest.raises(ResearchError, match="metrics"):
        trial(1, Split.TRAIN, metrics={"sharpe": 1})
    lg = lg.record(trial(1, Split.TRAIN))
    with pytest.raises(ResearchError, match="duplicate"):
        lg.record(trial(1, Split.TRAIN, recorded_at=T0 + timedelta(days=1)))
    with pytest.raises(ResearchError, match="time_order"):
        lg.record(trial(5, Split.TRAIN, Outcome.FAILED, access_id=None, recorded_at=T0))


def test_ledger_is_append_only_and_forged_entries_are_refused() -> None:
    lg, a = ask(ledger(), 1, Split.PROMOTION, PROMO)
    forged = replace(a, granted=True, reasons=(), seq=1)
    with pytest.raises(ResearchError, match="integrity"):
        TrialLedger(TENANT, (lg.entries[0], forged))
    with pytest.raises(ResearchError, match="unknown_plan"):
        TrialLedger(TENANT, (forged,))
    lg2, _ = ask(lg, 2, Split.TRAIN, TRAIN)
    assert lg2.entries[: len(lg.entries)] == lg.entries


def test_view_needs_current_rights_and_the_planned_dataset() -> None:
    lg, _ = ask(ledger(), 1, Split.TRAIN, TRAIN)
    with pytest.raises(DatasetRefused):
        open_view(lg, DS, "acc-1", rights(registry=Registry()))
    other = freeze(facts=(ORIGINAL,), events=EVENTS)
    with pytest.raises(ResearchError, match="dataset_mismatch"):
        open_view(lg, other, "acc-1", rights())
