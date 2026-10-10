"""Prospective shadow capture: forward decisions captured when made, bound to a
registered strategy version, its frozen protocol and a recorded trial; scored later
from bars known after each decision; reported apart from backtests (T033 part C).

All records are SYNTHETIC; times are invented; numbers are hand-computed below.
"""

import ast
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from qw_domain import shadow as sh
from qw_domain.backtest import PriceBar, PriceSeries, run_trial
from qw_domain.research_data import EvidenceClass, ResearchError, Window
from qw_domain.shadow import ShadowBook, ShadowDecision, SlotStatus, open_run, score
from qw_domain.strategy_registry import EvidenceState, StrategyError, StrategyRegistry
from qw_domain.trial_ledger import Candidate, Split, TrialLedger
from test_backtest import PX, protocol, scripted
from test_research_data import iid, rights
from test_strategy_registry import manifest
from test_trial_ledger import DS, T0, TENANT, TRAIN, ask, ledger, plan

D = Decimal
M = manifest()
CONFIG: dict[str, object] = {"lookback": 63}
SCAND = Candidate("fam-momentum", M.material_hash, M.config_hash(CONFIG))
REG = StrategyRegistry().register(M)
REGISTERED = T0 + timedelta(hours=3)  # after the trial (T0 + 2h) and the protocol
DAY0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
GRID = tuple(Window(DAY0 + timedelta(days=k), DAY0 + timedelta(days=k, hours=6.5))
             for k in range(5))  # fmt: skip
LATE = DAY0 + timedelta(days=30)  # every bar is known by then


def series(opens: dict[int, str], evidence: EvidenceClass = EvidenceClass.SYNTHETIC,
           known_late: int | None = None) -> PriceSeries:  # fmt: skip
    """SYNTHETIC live bars on GRID; a missing index is a missing bar."""
    out = []
    for k, o in sorted(opens.items()):
        w = GRID[k]
        known = LATE + timedelta(days=1) if k == known_late else w.end
        out.append(PriceBar(w.start, w.end, known, D(o), D(o)))
    return PriceSeries("px-synth-1", TENANT, iid(1), evidence, tuple(out))


# Opens 100, 110, 99, 99, 108.9: open-to-open returns -0.10, 0, +0.10, +0.10.
LIVE = series({0: "100", 1: "110", 2: "99", 3: "99", 4: "108.9"})


def trial_ledger() -> TrialLedger:
    lg = ledger(plan(protocol_hash=protocol().content_hash))
    lg = ask(lg, 1, Split.TRAIN, TRAIN, candidate=SCAND)[0]
    return run_trial(lg, "trial-1", "SYNTHETIC: scripted", T0 + timedelta(hours=2),
                     DS, rights(), "acc-1", protocol(), PX, scripted(["1"] * 5),
                     {"px-synth-1": PX})[0]  # fmt: skip


def run(min_scored: int = 2, **kw: object) -> sh.ShadowRun:
    args: dict[str, object] = {
        "registry": REG, "ledger": trial_ledger(), "run_id": "shadow-synth-1",
        "strategy_id": M.strategy_id, "version": M.version, "config": CONFIG,
        "trial_id": "trial-1", "protocol": protocol(), "instrument": iid(1),
        "min_scored": min_scored, "max_capture_delay": timedelta(minutes=5),
        "grid": GRID, "registered_at": REGISTERED,
    } | kw  # fmt: skip
    return open_run(**args)  # type: ignore[arg-type]


def dec(n: int, slot: int, weight: str | None, *, after: timedelta = timedelta(0),
        **kw: object) -> ShadowDecision:  # fmt: skip
    """Decision n, made one minute after bar `slot` is known, captured 5 s later."""
    at = GRID[slot].end + timedelta(minutes=1) + after
    args: dict[str, object] = {
        "decision_id": f"sd-{n}", "run_id": "shadow-synth-1",
        "protocol_hash": protocol().content_hash, "decided_at": at,
        "inputs_known_at": GRID[slot].end, "captured_at": at + timedelta(seconds=5),
        "target_weight": None if weight is None else D(weight),
    } | kw  # fmt: skip
    return ShadowDecision(**args)  # type: ignore[arg-type]


def sc(
    b: ShadowBook, px: PriceSeries = LIVE, as_of: datetime = LATE,
    base: PriceSeries | None = None, declared: str | None = None,
) -> sh.ShadowReport:  # fmt: skip
    """Score with the declaration hash recorded at registration (the run's own)."""
    held = {"px-synth-1": base or px}
    return score(b, px, as_of, held, declared or b.run.declaration_hash)


def book(r: sh.ShadowRun, *ds: ShadowDecision) -> ShadowBook:
    b = ShadowBook(r)
    for d in ds:
        b = b.capture(d)
    return b


def test_run_binds_a_registered_version_its_frozen_protocol_and_trial() -> None:
    r = run()
    assert (r.candidate, r.trial_id) == (SCAND, "trial-1")
    assert r.protocol_hash == protocol().content_hash
    with pytest.raises(StrategyError, match="unknown_strategy"):
        run(version="9.9.9")
    with pytest.raises(ResearchError, match="candidate_mismatch"):
        run(config={"lookback": 64})  # another configuration's trial
    other = replace(M, version="0.2.0", implementation_hash="c" * 64)
    with pytest.raises(ResearchError, match="candidate_mismatch"):
        run(registry=REG.register(other), version="0.2.0")  # material change
    with pytest.raises(ResearchError, match="protocol_changed"):
        run(protocol=protocol(fill_lag_bars=2))
    with pytest.raises(ResearchError, match="unknown_trial"):
        run(trial_id="trial-9")
    with pytest.raises(ResearchError, match="not_frozen"):
        run(registered_at=T0 + timedelta(hours=1))  # before the trial was recorded
    with pytest.raises(ResearchError, match="min_scored"):
        run(min_scored=0)
    with pytest.raises(ResearchError, match="max_capture_delay"):  # >= bar spacing
        run(max_capture_delay=timedelta(days=1))
    with pytest.raises(ResearchError, match="grid"):
        run(grid=(GRID[1], GRID[0], *GRID[2:]))  # not chronological
    with pytest.raises(ResearchError, match="grid"):
        run(grid=GRID[:2])  # too short for one period at lag 1


def test_the_declaration_is_hashed_and_required_when_scoring() -> None:
    r = run(min_scored=3)
    b = book(r, dec(1, 0, "1"), dec(2, 1, "0"), dec(3, 2, "1"))
    wire = sc(b).to_wire()
    assert wire["declaration_hash"] == r.declaration_hash and "grid_hash" in wire
    # Lowering the minimum after seeing results (3 -> 1) changes the declaration.
    relaxed = ShadowBook(replace(r, min_scored=1), b.entries)
    assert relaxed.run.declaration_hash != r.declaration_hash
    with pytest.raises(ResearchError, match="declaration_mismatch"):
        sc(relaxed, declared=r.declaration_hash)
    # Dropping bar 2 from the grid (merging two days into one slot) does too.
    g2 = tuple(w for k, w in enumerate(GRID) if k != 2)
    with pytest.raises(ResearchError, match="declaration_mismatch"):
        sc(ShadowBook(replace(r, grid=g2), b.entries), declared=r.declaration_hash)


def test_a_decision_captured_after_its_fill_bar_opens_is_never_scored() -> None:
    # Review probe: decided 1 min before bar 1 opens, captured 4 min later (inside
    # the 5-min delay) with weight 0, after the open is known: not scored, and it
    # cannot supersede sd-1 (weight 1, captured in time).
    fill = GRID[1].start
    late = dec(2, 0, "0", decided_at=fill - timedelta(minutes=1),
               captured_at=fill + timedelta(minutes=3))  # fmt: skip
    rep = sc(book(run(), dec(1, 0, "1"), late))
    assert rep.slots[0].decision_id == "sd-1"
    assert rep.slots[0].period is not None and rep.slots[0].period.net == D("-0.102")
    assert (rep.counts["late_captured"], rep.counts["superseded"]) == (1, 0)
    rep = sc(book(run(), late))
    assert rep.slots[0].status is SlotStatus.MISSED


def test_capture_is_point_in_time_append_only_and_idempotent() -> None:
    r = run()
    b = book(r, dec(1, 0, "1"))
    assert b.capture(dec(1, 0, "1")) == b and len(b.entries) == 1  # identical replay
    with pytest.raises(ResearchError, match="conflicting_duplicate"):
        b.capture(dec(1, 0, "0.5"))
    with pytest.raises(ResearchError, match="conflicting_duplicate"):  # retry may not
        b.capture(dec(1, 0, "1", captured_at=GRID[0].end + timedelta(minutes=2)))
    with pytest.raises(ResearchError, match="backfill"):
        b.capture(dec(2, 1, "1", captured_at=GRID[1].end + timedelta(minutes=7)))
    with pytest.raises(ResearchError, match="backfill"):  # captured before decided
        b.capture(dec(2, 1, "1", captured_at=GRID[1].end))
    with pytest.raises(ResearchError, match="look_ahead"):
        b.capture(dec(2, 1, "1", inputs_known_at=GRID[2].end))
    with pytest.raises(ResearchError, match="protocol_changed"):
        b.capture(dec(2, 1, "1", protocol_hash="f" * 64))
    with pytest.raises(ResearchError, match="run_mismatch"):
        b.capture(dec(2, 1, "1", run_id="shadow-synth-2"))
    with pytest.raises(ResearchError, match="weight"):
        b.capture(dec(2, 1, "1.5"))
    early = REGISTERED - timedelta(minutes=1)
    with pytest.raises(ResearchError, match="before_registration"):
        b.capture(dec(2, 1, "1", decided_at=early, inputs_known_at=early,
                      captured_at=early))  # fmt: skip
    b2 = b.capture(dec(2, 1, "0"))
    with pytest.raises(ResearchError, match="time_order"):
        b2.capture(dec(3, 0, "0", after=timedelta(seconds=1)))
    assert ShadowBook(r, b2.entries) == b2  # construction replays every entry
    with pytest.raises(ResearchError, match="duplicate"):
        ShadowBook(r, (*b2.entries, b2.entries[0]))


def test_full_capture_scores_like_the_backtest_and_stays_separate() -> None:
    b = book(run(min_scored=3), dec(1, 0, "1"), dec(2, 1, "0"), dec(3, 2, "1"),
             dec(4, 3, "0"), dec(5, 4, "0"))  # fmt: skip
    rep = sc(b)
    # Slot k fills at the open of bar k+1: weights 1, 0, 1; turnover 1 each, cost
    # 0.002: nets -0.102, -0.002, 0.098 (the hand-computed backtest figures).
    scored = [s.period.net for s in rep.slots if s.period is not None]
    assert scored == [D("-0.102"), D("-0.002"), D("0.098")]
    assert rep.summary is not None and rep.summary.mean == D("-0.002")
    assert rep.summary.interval is not None  # eval_stats Newey-West, lag 1
    # Slot 3 fills at bar 4, but its period ends at the open of bar 5, beyond the
    # declared grid: outcome pending, not zero. Slot 4 fills at bar 5: pending.
    assert rep.slots[3].status is SlotStatus.PENDING_OUTCOME and len(rep.slots) == 4
    assert rep.counts == {"due": 4, "scored": 3, "abstained": 0, "missed": 0,
                          "unavailable": 0, "pending_outcome": 1,
                          "late_decisions": 0, "late_captured": 0, "superseded": 0,
                          "pending": 1, "unscheduled": 0}  # fmt: skip
    hold, cash = rep.baselines
    # Buy and hold on the same slots: -0.102, 0, 0.1; excess 0, -0.002, -0.002.
    assert hold.excess is not None and abs(hold.excess.mean * 3 + D("0.004")) < D(
        "1e-30"
    )
    assert cash.excess is not None and cash.excess.mean == D("-0.002")
    assert rep.evidence_ceiling is EvidenceState.LIMITED  # synthetic bars
    assert "synthetic_only" in rep.limitations and rep.claim_permitted
    wire = rep.to_wire()
    assert wire["record_kind"] == "prospective_shadow"
    assert wire["significance_claimed"] is False and wire["trial_id"] == "trial-1"
    assert wire["protocol_hash"] == protocol().content_hash
    assert "edge" not in json.dumps(wire).lower()
    assert not {"dataset_hash", "split", "window"} & set(wire)  # no backtest figures


def test_abstentions_misses_and_late_decisions_are_counted_not_scored() -> None:
    late = timedelta(hours=18)  # decided during bar 4: too late to fill at its open
    b = book(run(), dec(1, 0, "1"), dec(2, 1, None), dec(3, 2, "0.25"),
             dec(4, 3, "1", after=late))  # fmt: skip
    rep = sc(b)
    assert [s.status.value for s in rep.slots] == [
        "scored", "abstained", "scored", "missed"]  # fmt: skip
    # Slot 2 follows an abstention, so the held weight is unknown: worst-case
    # turnover max(0.25, 0.75) = 0.75, cost 0.0015; gross 0.25 * 0.10 = 0.025.
    assert rep.slots[2].period is not None
    assert rep.slots[2].period.turnover == D("0.75")
    assert rep.slots[2].period.net == D("0.0235")
    assert rep.summary is not None and rep.summary.mean == D("-0.03925")
    assert rep.counts["late_decisions"] == 1 and rep.counts["abstained"] == 1
    assert rep.counts["missed"] == 1 and "gap_turnover_worst_case" in rep.limitations
    # Buy and hold is chained over every due slot (no re-buy on the strategy's gap):
    # 1, abstain, 1 -> baseline slot nets -0.102, 0, 0.10; strategy slot 2 with
    # weight 1 after the gap nets 0.098; common slots 0 and 2: excess 0, -0.002.
    b = book(run(), dec(1, 0, "1"), dec(2, 1, None), dec(3, 2, "1"))
    hold = sc(b).baselines[0]
    assert hold.excess is not None and (hold.common, hold.excess.mean) == (
        2, D("-0.001"))  # fmt: skip
    # A revised decision for slot 0 before the fill replaces the first (counted).
    b = book(run(), dec(1, 0, "1"), dec(2, 0, "0", after=timedelta(minutes=9)))
    rep = sc(b)
    assert rep.slots[0].decision_id == "sd-2" and rep.counts["superseded"] == 1
    assert rep.slots[0].period is not None and rep.slots[0].period.net == D(0)


def test_missing_or_not_yet_known_bars_are_unavailable_never_zero() -> None:
    holed = series({0: "100", 1: "110", 3: "99", 4: "108.9"})  # bar 2 missing
    b = book(run(), dec(1, 0, "1"), dec(2, 1, "0"), dec(3, 2, "1"))
    rep = sc(b, holed)
    assert [s.status for s in rep.slots[:3]] == [
        SlotStatus.UNAVAILABLE,
        SlotStatus.UNAVAILABLE,
        SlotStatus.SCORED,
    ]
    # Slot 2 restarts after a gap: worst-case turnover 1, net 0.10 - 0.002.
    assert rep.slots[2].period is not None and rep.slots[2].period.net == D("0.098")
    pending = series({0: "100", 1: "110", 2: "99", 3: "99", 4: "108.9"}, known_late=4)
    rep = sc(b, pending)
    assert rep.slots[2].status is SlotStatus.UNAVAILABLE  # bar 4 known after as_of
    assert rep.counts["scored"] == 2 and rep.claim_permitted  # the minimum is 2
    # One scored slot by this as_of: below the declared minimum, no claim.
    early = sc(b, as_of=GRID[2].end + timedelta(hours=1))
    # Due: slots 0 and 1 (slot 2 fills at bar 3, not started); slot 1's period
    # ends at the open of bar 3, which has not ended by as_of: outcome pending.
    assert (early.counts["due"], early.counts["scored"]) == (2, 1)
    assert (early.counts["pending_outcome"], early.counts["pending"]) == (1, 1)
    assert early.evidence_ceiling is EvidenceState.NONE and not early.claim_permitted
    assert "below_declared_minimum" in early.limitations
    sooner = GRID[2].end + timedelta(seconds=30)  # sd-3 not yet captured: unseen
    assert sc(b, as_of=sooner).counts["pending"] == 0


def test_prospective_ceiling_and_series_checks() -> None:
    live = series({0: "100", 1: "110", 2: "99", 3: "99", 4: "108.9"},
                  EvidenceClass.PROSPECTIVE)  # fmt: skip
    b = book(run(), dec(1, 0, "1"), dec(2, 1, "0"), dec(3, 2, "1"))
    rep = sc(b, live)
    assert rep.evidence_ceiling is EvidenceState.PROSPECTIVE_LIMITED
    assert "synthetic_only" not in rep.limitations
    hist = replace(live, evidence_class=EvidenceClass.HISTORICAL)
    with pytest.raises(ResearchError, match="not_prospective"):
        sc(b, hist)  # replayed history
    with pytest.raises(ResearchError, match="series"):
        sc(b, replace(live, instrument=iid(2)), base=live)
    stray = PriceBar(LATE, LATE + timedelta(hours=1), LATE + timedelta(hours=1),
                     D(1), D(1))  # fmt: skip
    with pytest.raises(ResearchError, match="off_grid"):
        sc(b, replace(live, bars=(*live.bars, stray)), base=live)
    with pytest.raises(ResearchError, match="baseline_missing"):
        score(b, live, LATE, {}, b.run.declaration_hash)
    with pytest.raises(ResearchError, match="evidence_class_mismatch"):
        sc(b, live, base=LIVE)  # synthetic baseline bars


def test_no_order_or_broker_code_is_reachable() -> None:
    root, seen, todo = Path(sh.__file__).parent, set[str](), ["qw_domain.shadow"]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        tree = ast.parse((root / f"{mod.removeprefix('qw_domain.')}.py").read_text())
        for n in ast.walk(tree):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            names += [n.module] if isinstance(n, ast.ImportFrom) and n.module else []
            for name in names:
                assert "broker" not in name and "order" not in name, (mod, name)
                if name.startswith("qw_domain."):
                    todo.append(name)
                else:
                    assert name.split(".")[0] in sys.stdlib_module_names, (mod, name)
    assert {"qw_domain.backtest", "qw_domain.trial_ledger"} <= seen
    banned = {"journal", "postings", "proposals", "commitments", "simulation"}
    assert not {m.split(".")[-1] for m in seen} & banned
    assert not [n for n in dir(sh) if "order" in n.lower() or "submit" in n.lower()]
