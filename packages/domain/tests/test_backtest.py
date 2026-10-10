"""Chronological walk-forward evaluation with explicit costs, frozen baselines, honest
trial reports and scoped claims (T033 increment 1).

All data is SYNTHETIC (datasets and rights from test_research_data, the split plan
from test_trial_ledger); expected values are hand-computed in the comments.
"""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from random import Random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.backtest import (
    Baseline,
    BaselineKind,
    ClaimMetric,
    CostModel,
    Decision,
    DeclaredClaim,
    Direction,
    Evaluation,
    EvaluationProtocol,
    PitContext,
    PriceBar,
    PriceSeries,
    Strategy,
    TrialOutcome,
    claim_for,
    evaluation_receipt,
    run_trial,
    trial_report,
    walk_forward,
)
from qw_domain.decimal_math import CTX
from qw_domain.eval_stats import Z95, summarize
from qw_domain.research_data import EvidenceClass, ResearchError
from qw_domain.strategy_registry import EvidenceState
from qw_domain.trial_ledger import Outcome, Role, Split, TrialLedger
from test_research_data import iid, rights
from test_trial_ledger import (
    CAND,
    DEV,
    DS,
    PROMO,
    T0,
    TENANT,
    TRAIN,
    ask,
    ledger,
    plan,
    trial,
)

D = Decimal
COSTS = CostModel(D("0.001"), D("0.0005"), D("0.0005"))  # 0.002 per unit turnover
DAY0 = datetime(2021, 1, 4, 14, 30, tzinfo=UTC)


def bars(opens: list[str], instrument: int = 1, day0: datetime = DAY0) -> PriceSeries:
    out = []
    for k, o in enumerate(opens):
        start = day0 + timedelta(days=k)
        end = start + timedelta(hours=6, minutes=30)
        out.append(PriceBar(start, end, end, D(o), D(o)))
    return PriceSeries(
        "px-synth-1", TENANT, iid(instrument), EvidenceClass.SYNTHETIC, tuple(out)
    )


# SYNTHETIC opens; open-to-open returns -0.10, 0, +0.10.
PX = bars(["100", "110", "99", "99", "108.9"])
BH = Baseline("bl-hold", BaselineKind.BUY_AND_HOLD, "px-synth-1", D(0))
CASH = Baseline("bl-cash", BaselineKind.CASH, None, D(0))


def protocol(**kw: object) -> EvaluationProtocol:
    args: dict[str, object] = {
        "protocol_id": "proto-synth-1", "plan_id": "plan-synth-1",
        "claim": DeclaredClaim(ClaimMetric.NET_MEAN, Direction.POSITIVE, None),
        "costs": COSTS, "fill_lag_bars": 1, "hac_lags": 1, "z": Z95,
        "baselines": (BH, CASH), "declared_at": T0, "approved_by": "owner-synth",
    } | kw  # fmt: skip
    return EvaluationProtocol(**args)  # type: ignore[arg-type]


PROTO_HASH = protocol().content_hash  # frozen with the plan at registration


def scripted(weights: list[str]) -> Strategy:
    """Weight k is decided at the close of bar k from visible data only."""

    def strategy(ctx: PitContext) -> Decision:
        seen = ctx.bars()
        assert all(b.known_at <= ctx.now for b in seen)
        return Decision(D(weights[len(seen) - 1]), seen[-1].known_at)

    return strategy


def granted(split: Split = Split.TRAIN) -> TrialLedger:
    window = {Split.TRAIN: TRAIN, Split.DEVELOPMENT: DEV}[split]
    return ask(ledger(plan(protocol_hash=PROTO_HASH)), 1, split, window)[0]


def run(weights: list[str], lg: TrialLedger | None = None, **kw: object) -> Evaluation:
    args: dict[str, object] = {
        "ledger": lg or granted(), "dataset": DS, "rights": rights(),
        "access_id": "acc-1", "protocol": protocol(), "series": PX,
        "strategy": scripted(weights), "baseline_series": {"px-synth-1": PX},
    } | kw  # fmt: skip
    return walk_forward(**args)  # type: ignore[arg-type]


def record(strategy: Strategy) -> tuple[TrialLedger, TrialOutcome]:
    when = T0 + timedelta(hours=2)
    return run_trial(granted(), "trial-1", "SYNTHETIC: scripted", when, DS, rights(),
                     "acc-1", protocol(), PX, strategy, {"px-synth-1": PX})  # fmt: skip


def test_hand_computed_backtest_net_of_costs_with_baselines() -> None:
    ev = run(["1", "0", "1", "0", "0"])
    # Decision at bar k fills at the open of bar k+1; periods open1->open2 .. open3->4.
    # weights 1, 0, 1; turnover 1 each (cost 0.002); gross -0.10, 0, +0.10
    assert [p.net for p in ev.periods] == [D("-0.102"), D("-0.002"), D("0.098")]
    assert [p.gross for p in ev.periods] == [D("-0.1"), D("0"), D("0.1")]
    assert ev.summary.mean == D("-0.002")  # -0.006 / 3: a losing strategy
    assert ev.total_cost == D("0.006")
    hold, cash = ev.baselines
    # Buy and hold: buy once (cost 0.002), then drift at weight 1: -0.102, 0, 0.1
    assert [p.net for p in hold.periods] == [D("-0.102"), D("0"), D("0.1")]
    # Excess over buy and hold: 0, -0.002, -0.002 -> mean -0.004 / 3
    assert abs(hold.excess.mean * 3 + D("0.004")) < D("1e-30")
    assert hold.excess.interval is not None
    assert cash.excess.mean == ev.summary.mean  # cash earns the declared 0


def test_fractional_weight_drifts_and_rebalancing_costs_the_drift() -> None:
    px = bars(["90", "100", "110", "110"])
    ev = run(["0.5"] * 4, series=px, baseline_series={"px-synth-1": px})
    # Period 1 (100 -> 110): gross 0.05, weight drifts to 0.55 / 1.05 = 11/21
    # Period 2: turnover |0.5 - 11/21| = 1/42; cost 0.002 / 42
    first, second = ev.periods
    assert first.turnover == D("0.5") and first.net == D("0.049")
    assert abs(second.turnover * 42 - 1) < D("1e-30")
    assert abs(second.net + D("0.002") / 42) < D("1e-30")


def test_look_ahead_is_refused_and_recorded_as_a_failed_trial() -> None:
    def peeks(ctx: PitContext) -> Decision:
        ctx.facts_at(ctx.now + timedelta(days=1))
        raise AssertionError("unreachable")

    def claims_future(ctx: PitContext) -> Decision:
        return Decision(D(1), ctx.now + timedelta(seconds=1))

    for strategy in (peeks, claims_future):
        with pytest.raises(ResearchError, match="look_ahead"):
            run([], strategy=strategy)
    lg, outcome = record(claims_future)
    assert outcome.evaluation is None and outcome.reason is not None
    assert outcome.trial.outcome is Outcome.FAILED and lg.trial_count(CAND.family) == 1


def test_bars_past_the_access_window_are_never_used() -> None:
    # Five daily bars from 2021-12-29; the train window ends 2022-01-01T00:00Z, so
    # only the three December bars are visible: two decisions' worth, one period.
    px_end = bars(["100", "100", "100", "50", "50"], day0=datetime(2021, 12, 29,
                   14, 30, tzinfo=UTC))  # fmt: skip
    ev = run(["1"] * 5, series=px_end, baseline_series={"px-synth-1": px_end})
    assert len(ev.periods) == 1 and ev.periods[0].end < TRAIN.end
    assert ev.summary.max_drawdown == D("-0.002")  # only the entry cost


def test_same_bar_fill_is_refused() -> None:
    with pytest.raises(ResearchError, match="same_bar_fill"):
        protocol(fill_lag_bars=0)
    # Bars known only after the next open: a next-bar fill would precede the decision.
    lag = timedelta(days=1, hours=1)
    shifted = (replace(b, known_at=b.start + lag) for b in PX.bars)
    late = replace(PX, bars=tuple(shifted))
    with pytest.raises(ResearchError, match="same_bar_fill"):
        run(["1"] * 5, series=late, baseline_series={"px-synth-1": late})


def test_holdout_protocol_and_frozen_terms_are_enforced() -> None:
    lg = ask(ledger(plan(protocol_hash=PROTO_HASH)), 1, Split.PROMOTION, PROMO,
             Role.ASSESSOR)[0]  # fmt: skip
    with pytest.raises(ResearchError, match="promotion_protocol"):
        run(["1"] * 5, lg=lg)
    # Review probe: a zero-cost protocol under the same plan and declared_at.
    free = CostModel(D(0), D(0), D(0))
    for changed in (protocol(costs=free), protocol(declared_at=T0 - timedelta(1))):
        with pytest.raises(ResearchError, match="protocol_changed"):
            run(["1", "0", "1", "0", "0"], protocol=changed)
    with pytest.raises(ResearchError, match="holdout_instrument"):
        run(["1"] * 5, series=bars(["1", "2", "3"], instrument=3))
    with pytest.raises(ResearchError, match="baseline_misaligned"):
        run(["1"] * 5, baseline_series={"px-synth-1": bars(["1", "2", "3"])})
    hist = replace(PX, evidence_class=EvidenceClass.HISTORICAL)
    with pytest.raises(ResearchError, match="evidence_class_mismatch"):
        run(["1"] * 5, series=hist)


def test_claims_are_structured_and_costs_bounded() -> None:
    with pytest.raises(ResearchError, match="claim"):
        protocol(claim="SYNTHETIC: this strategy has an edge")  # free text
    with pytest.raises(ResearchError, match="claim"):
        protocol(claim=DeclaredClaim(ClaimMetric.EXCESS_MEAN, Direction.POSITIVE,
                                     "bl-unknown"))  # fmt: skip
    with pytest.raises(ResearchError, match="96"):  # metric keys stay valid ids
        protocol(baselines=(replace(CASH, baseline_id="b" * 97),))
    for bad in ((D(1), D(0), D(0)), (D("0.5"), D("0.25"), D("0.25"))):
        with pytest.raises(ResearchError, match="cost"):
            CostModel(*bad)  # total rate must stay below 1
    # Rate 0.5 and a 99% price fall: net -0.99 - 0.5 <= -1 is refused, not booked.
    dear = protocol(costs=CostModel(D("0.5"), D(0), D(0)))
    lg = ask(ledger(plan(protocol_hash=dear.content_hash)), 1, Split.TRAIN, TRAIN)[0]
    crash = bars(["100", "100", "1", "1"])
    with pytest.raises(ResearchError, match="total_loss"):
        run(["1"] * 4, lg=lg, protocol=dear, series=crash,
            baseline_series={"px-synth-1": crash})  # fmt: skip


def test_synthetic_evaluation_is_capped_at_limited_and_claims_are_scoped() -> None:
    lg, outcome = record(scripted(["1", "0", "1", "0", "0"]))
    assert outcome.evaluation is not None and outcome.trial.outcome is Outcome.COMPLETED
    assert outcome.trial.protocol_hash == PROTO_HASH
    ev = outcome.evaluation
    claim = claim_for(outcome.evaluation, lg)
    assert claim.evidence_class is EvidenceClass.SYNTHETIC
    assert claim.evidence_ceiling is EvidenceState.LIMITED
    assert "synthetic_only" in claim.limitations
    assert "development_window_not_promotion" in claim.limitations
    assert claim.significance_claimed is False
    wire = claim.to_wire()
    assert wire["dataset_hash"] == DS.manifest.content_hash
    assert wire["hypothesis"] == {"metric": "net_mean_return", "direction": "positive",
                             "baseline_id": None}  # fmt: skip
    assert wire["trial_count"] == 1 and D(str(wire["net_mean"])) == D("-0.002")
    assert wire["costs"] == {"commission_rate": "0.001", "half_spread": "0.0005",
                             "slippage_rate": "0.0005"}  # fmt: skip
    receipt = evaluation_receipt("evr-synth-1", "exp-synth-1", outcome.evaluation,
                                 lg, T0 + timedelta(hours=3))  # fmt: skip
    keys = {"id", "experiment_id", "test_manifest_hash", "environment", "results",
            "executed_at", "status"}  # fmt: skip
    assert set(receipt) == keys and receipt["status"] == "completed"
    assert receipt["executed_at"] == "2026-01-02T03:00:00Z"
    with pytest.raises(ResearchError, match="trial_not_recorded"):
        claim_for(run(["1"] * 5), granted())  # an unregistered run claims nothing
    # Review probe: recorded net mean -0.002, hand-edited evaluation says 0.05.
    forged = replace(ev, summary=replace(ev.summary, mean=D("0.05")))
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(forged, lg)
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        evaluation_receipt("evr-synth-2", "exp-synth-1", forged, lg, T0)
    bent = replace(ev, periods=(replace(ev.periods[0], net=D("0.5")), *ev.periods[1:]))
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(bent, lg)  # periods no longer produce the recorded summary
    nets = [p.net for p in bent.periods]
    redone = replace(bent, summary=summarize(nets, 1, Z95))  # consistent, not recorded
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(redone, lg)
    # Part B follow-up: the recorded buy-and-hold excess mean is -0.004/3 (see the
    # hand-computed test); a forged +0.05 excess, alone or with periods and summaries
    # rebuilt to agree with it, is not the recorded figure.
    hold, cash = ev.baselines
    assert lg.trials()[0].metrics["baseline.bl-hold.excess_mean"] == hold.excess.mean
    fake = replace(hold, excess=replace(hold.excess, mean=D("0.05")))
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(replace(ev, baselines=(fake, cash)), lg)
    worse = tuple(replace(p, net=CTX.subtract(p.net, D("0.06"))) for p in hold.periods)
    pairs = zip(ev.periods, worse, strict=True)
    excess = [CTX.subtract(p.net, q.net) for p, q in pairs]
    rebuilt = replace(hold, periods=worse, summary=summarize([p.net for p in worse], 1,
                      Z95), excess=summarize(excess, 1, Z95))  # fmt: skip
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(replace(ev, baselines=(rebuilt, cash)), lg)
    vol = replace(hold, excess=replace(hold.excess, volatility=D(9)))  # not recorded
    with pytest.raises(ResearchError, match="metrics_mismatch"):
        claim_for(replace(ev, baselines=(vol, cash)), lg)  # but not from the periods
    # Review round 1: truncated baseline periods; a baseline whose first period
    # claims no turnover or cost (gross set to the net -0.102); a strategy period
    # with turnover 0 but cost 0.002; a strategy gross that is not net + cost.
    short = replace(hold, periods=hold.periods[:-1])
    p0 = replace(hold.periods[0], turnover=D(0), cost=D(0), gross=D("-0.102"))
    free = replace(hold, periods=(p0, *hold.periods[1:]))
    q0, q1 = replace(ev.periods[0], turnover=D(0)), replace(ev.periods[0], gross=D(0))
    g0, g1, *rest = ev.periods  # gross +-0.1: same gross mean, net != gross - cost
    swapped = (replace(g0, gross=g0.gross + D("0.1")),
               replace(g1, gross=g1.gross - D("0.1")), *rest)  # fmt: skip
    for bad in (replace(ev, baselines=(short, cash)),
                replace(ev, baselines=(free, cash)),
                replace(ev, periods=(q0, *ev.periods[1:])),
                replace(ev, periods=(q1, *ev.periods[1:])),
                replace(ev, periods=swapped)):  # fmt: skip
        with pytest.raises(ResearchError, match="metrics_mismatch"):
            claim_for(bad, lg)


def test_failed_and_abandoned_trials_are_reported() -> None:
    lg = granted()
    lg = lg.record(trial(1, Split.TRAIN, Outcome.FAILED, metrics={},
                         protocol_hash=PROTO_HASH))  # fmt: skip
    lg = lg.record(trial(2, Split.TRAIN, Outcome.ABANDONED, metrics={}, access_id=None,
                         protocol_hash=PROTO_HASH))  # fmt: skip
    lg = ask(lg, 3, Split.TRAIN, TRAIN)[0]  # saw data, never recorded
    report = trial_report(lg.trials(CAND.family), lg.trial_count(CAND.family))
    assert [r["trial_id"] for r in report.rows] == ["trial-1", "trial-2"]
    assert [r["outcome"] for r in report.rows] == ["failed", "abandoned"]
    assert (report.trial_count, report.unrecorded) == (3, 1)
    with pytest.raises(ResearchError, match="trial_count"):
        trial_report(lg.trials(CAND.family), 1)


@settings(max_examples=25, deadline=None)
@given(st.lists(st.sampled_from(list(Outcome)), max_size=4), st.randoms())
def test_claims_carry_every_ledger_trial_in_any_order(
    outs: list[Outcome], rnd: Random
) -> None:
    # Trial 1 is the evaluated run; trials 2.. have any outcome; one access is never
    # recorded. Claim and receipt read the ledger, so none can be left out.
    lg, outcome = record(scripted(["1", "0", "1", "0", "0"]))
    assert outcome.evaluation is not None
    for n, out in enumerate(outs, start=2):
        acc = None if out is Outcome.ABANDONED else f"acc-{n}"
        if acc is not None:
            lg = ask(lg, n, Split.TRAIN, TRAIN)[0]
        lg = lg.record(trial(n, Split.TRAIN, out, metrics={}, access_id=acc,
                             protocol_hash=PROTO_HASH))  # fmt: skip
    lg = ask(lg, 9, Split.TRAIN, TRAIN)[0]  # saw data, result never recorded
    shuffled = list(lg.trials(CAND.family))
    rnd.shuffle(shuffled)
    count = lg.trial_count(CAND.family)
    assert trial_report(shuffled, count) == trial_report(lg.trials(CAND.family), count)
    receipt = evaluation_receipt("evr-synth-1", "exp-synth-1", outcome.evaluation, lg,
                                 T0 + timedelta(days=1))  # fmt: skip
    results = receipt["results"]
    assert isinstance(results, dict)
    ids = [r["trial_id"] for r in results["trials"]]
    assert ids == sorted(f"trial-{n}" for n in range(1, len(outs) + 2))
    assert results["unrecorded_trials"] == 1
    wire = results["claim"]
    assert wire["trial_count"] == 2 + len(outs)
    assert wire["significance_claimed"] is False
    assert {"ci_lower", "ci_upper", "ci_method"} <= set(wire)
    assert "edge" not in json.dumps(receipt).lower()


def test_long_cash_rate_is_rounded_once_and_still_claims() -> None:
    # a 47-digit cash rate is rounded to the 40-digit context when the periods are
    # built, so the period arithmetic check agrees with it (review N1)
    long_rate = D("0.00001234567890123456789012345678901234567890123")
    cash = replace(CASH, cash_rate=long_rate)
    pr = protocol(baselines=(cash,))
    lg = ask(ledger(plan(protocol_hash=pr.content_hash)), 1, Split.TRAIN, TRAIN)[0]
    lg, out = run_trial(lg, "trial-1", "SYNTHETIC", T0 + timedelta(hours=2), DS,
                        rights(), "acc-1", pr, PX, scripted(["1", "0", "1", "0", "0"]),
                        {"px-synth-1": PX})  # fmt: skip
    assert out.evaluation is not None
    assert claim_for(out.evaluation, lg).trial_count == 1
