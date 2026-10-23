"""Owner decision 42 (the assessor-run promotion, decision 39): qw_adapters.
assessor_run.promotion_run loads the frozen protocol and the plan's protected
holdout series as qw_assessor, records the one promotion read (and attempt 1)
durably before any data is opened, then evaluates through `holdout_run` and
records the trial's verdict. After a crash the same read is re-run once,
deterministically, and never a second fresh read. Real logins
(`runtime_connection(url, role=...)`); the superuser `conn` observes or tampers.
SYNTHETIC data only: the test_strategy_improvement fixtures, scoped to a real
tenant id."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import psycopg
import pytest
import test_strategy_improvement as fx
from ingest_rights_helper import qualified_registry
from psycopg import errors
from qw_adapters import assessor_run as ar
from qw_adapters import assessor_store as ast
from qw_adapters import experiment_store as es
from qw_adapters import holdout_series_store as hs
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.backtest import TrialOutcome
from qw_domain.budget import BudgetBook, Category
from qw_domain.decimals import UsdBudget
from qw_domain.research_data import ResearchDataset, ResearchRights
from qw_domain.trial_ledger import Outcome, Split, SplitPlan, TrialLedger
from test_backtest import scripted
from test_improvement import AT, CAPS, EXP, PRODUCER
from test_research_data import FEED, ORIGINAL, freeze, iid, rights
from test_trial_ledger import DS, T0, TRAIN, plan

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
TABLES = ("assessor_protocol", "assessor_candidate", "holdout_access",
          "holdout_run", "holdout_run_outcome", "evaluation_receipt",
          "experiment_revision")  # fmt: skip


class Crash(RuntimeError):
    """A process death at a phase boundary (nothing after it runs)."""


@dataclass(frozen=True)
class World:  # the SYNTHETIC promotion fixtures of one real tenant
    t: uuid.UUID
    ds: ResearchDataset
    rights: ResearchRights
    plan: SplitPlan
    reg: im.ExperimentRegistry
    study: si.StrategyStudy
    ledger: TrialLedger
    holdout: si.HoldoutData
    book: BudgetBook


def world(t: uuid.UUID) -> World:
    s = str(t)
    rr = replace(rights(registry=qualified_registry((FEED,), tenant=s)), tenant_id=s)
    ds = freeze(facts=(ORIGINAL,), events=DS.universe.events, rights=rr)
    sp = plan(protocol_hash=fx.EVAL.content_hash, dataset=ds.manifest)
    p = fx.iproto(sandbox_profile_id="sbx-synth", scorecard=fx.NET, scope_id=s,
                  budget_reservation_id="bud-exp-1",
                  plan_digest=im.plan_digest(sp))  # fmt: skip
    reg = im.ExperimentRegistry(s).register(p).register_candidate(fx.CAND)
    win = replace(fx.WIN, tenant_id=s)  # k = 1 accepts (test_strategy_improvement)
    book, _ = im.schedule(reg, (EXP,), BudgetBook.open(s, AT, CAPS), AT)
    return World(t, ds, rr, sp, reg, si.StrategyStudy(p, fx.SPACE, fx.EVAL, fx.BASE),
                 TrialLedger(s).register_plan(sp, ds),
                 si.HoldoutData(win, {"px-synth-1": win}), book)  # fmt: skip


def research(w: World, lg: TrialLedger, n: int) -> TrialLedger:
    """A granted TRAIN access acc-n for cand-1 at T0 + n hours (no trial yet)."""
    return si.request_research_access(w.study, lg, fx.CAND, f"acc-{n}", f"trial-{n}",
                                      Split.TRAIN, TRAIN, "res-synth",
                                      T0 + n * fx.H)[0]  # fmt: skip


def request(w: World, **kw: Any) -> ar.Request:
    args: dict[str, Any] = {
        "study": w.study, "registry": w.reg, "strategies": fx.STRATS,
        "ledger": w.ledger, "candidate_id": "cand-1", "dataset": w.ds,
        "rights": w.rights, "instrument": iid(3),
        "build": lambda cfg: scripted(fx.WIN_W), "book": w.book,
        "assessor": w.study.protocol.assessor, "at": fx.AT_P, "access_id": "acc-p",
        "trial_id": "trial-p", "verdict_id": "v-p",
    } | kw  # fmt: skip
    return ar.Request(**args)


def expected(w: World) -> tuple[TrialLedger, TrialOutcome, im.Verdict]:
    """The pure domain oracle: the same run and verdict with no store at all."""
    r = request(w)
    lg, out = si.holdout_run(w.study, w.reg, fx.STRATS, w.ledger, "cand-1", w.ds,
                             w.rights, w.holdout, r.build, r.assessor, fx.AT_P,
                             "acc-p", "trial-p")  # fmt: skip
    run = im.Assessment(lg, r.assessor, fx.AT_P, "v-p", evaluation=fx.EVAL)
    v = im.derive(w.reg, EXP, "cand-1", w.plan, w.book, None, run)[1]
    assert v is not None
    return lg, out, v


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as c:
        yield c


@pytest.fixture
def wrk(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    with tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as c:
        yield c


@pytest.fixture
def w(app_conn: Conn, asr: Conn, wrk: Conn) -> World:
    """Series preregistered, then the experiment scheduled (its protocol frozen)."""
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    w = world(t)
    with tn.tenant_transaction(asr, t) as tx:
        hs.preregister(tx, w.plan.plan_id, "holdout", w.holdout.series)
        hs.preregister(tx, w.plan.plan_id, "px-synth-1", w.holdout.series)
    es.record(wrk, asr, t, w.reg, EXP, "cand-1", w.plan, w.book,
              im.Scheduled(EXP, "bud-exp-1"))  # fmt: skip
    return w


def go(wrk: Conn, asr: Conn, w: World, **kw: Any) -> ar.Run:
    return ar.promotion_run(wrk, asr, w.t, request(w, **kw))


def counts(conn: Conn, t: uuid.UUID) -> dict[str, int]:
    out = {}
    for table in TABLES:
        row = conn.execute(f"SELECT count(*) FROM app.{table} WHERE tenant_id = %s",
                           (t,)).fetchone()  # fmt: skip
        assert row is not None
        out[table] = int(row[0])
    return out


def code(fn: Callable[[], object]) -> str:
    """The refusal code; `fn` runs at once (no late binding)."""
    with pytest.raises((es.ExperimentStoreError, im.ImprovementError)) as e:
        fn()
    err = e.value
    assert isinstance(err, (es.ExperimentStoreError, im.ImprovementError))
    return err.code


def attempts(conn: Conn, t: uuid.UUID) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT attempt, seq, access_id, verdict_id FROM app.holdout_run WHERE "
        "tenant_id = %s ORDER BY attempt", (t,)).fetchall()  # fmt: skip


def stored(asr: Conn, w: World) -> list[ast.Receipt]:
    with tn.tenant_transaction(asr, w.t) as tx:
        return ast.receipts(tx, EXP)


def crash(monkeypatch: pytest.MonkeyPatch, owner: object, name: str) -> None:
    def boom(*args: object, **kw: object) -> None:
        raise Crash(name)

    monkeypatch.setattr(owner, name, boom)


# --- the run ---


def test_decision_42_the_happy_path_reserves_then_records(
    wrk: Conn, asr: Conn, conn: Conn, w: World
) -> None:
    lg, out, v = expected(w)
    run = go(wrk, asr, w)
    assert (run.attempt, run.wrote, run.ledger, run.outcome) == (1, True, lg, out)
    assert out.trial.outcome is Outcome.COMPLETED
    assert run.revision.record.state is im.RecordState.AWAITING_REVIEW
    assert run.revision.record.verdict_id == "v-p"
    (got, seq, n, d), = stored(asr, w)  # fmt: skip
    assert (got, seq, n, d) == (v, 1, len(lg.entries), lg.checkpoint(len(lg.entries)))
    assert v.decision is im.Decision.ACCEPT_FOR_REVIEW
    assert attempts(conn, w.t) == [(1, 1, "acc-p", "v-p")]
    reads = conn.execute("SELECT seq, access_id, body FROM app.holdout_access WHERE "
                         "tenant_id = %s", (w.t,)).fetchall()  # fmt: skip
    assert reads == [(1, "acc-p", lg.access("acc-p").to_wire())]


def test_decision_42_the_read_is_recorded_before_any_data_is_opened(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, int]] = []
    real = si.holdout_run

    def spy(*args: Any, **kw: Any) -> tuple[TrialLedger, TrialOutcome]:
        seen.append(counts(conn, w.t))  # committed before the evaluation starts
        return real(*args, **kw)

    monkeypatch.setattr(si, "holdout_run", spy)
    go(wrk, asr, w)
    assert [(c["holdout_access"], c["holdout_run"], c["evaluation_receipt"])
            for c in seen] == [(1, 1, 0)]  # fmt: skip


def test_decision_42_refusals_before_the_read_change_nothing(
    wrk: Conn, asr: Conn, conn: Conn, w: World, app_conn: Conn
) -> None:
    before = counts(conn, w.t)
    lg = research(w, w.ledger, 2)  # an unrecorded research access (decision 50)
    read, _ = si.holdout_read(w.study, w.reg, fx.STRATS, w.ledger, "cand-1",
                              w.study.protocol.assessor, fx.AT_P, "acc-x",
                              "trial-x")  # fmt: skip
    other = im.ExperimentRegistry(str(w.t)).register(replace(
        w.study.protocol, hypothesis="SYNTHETIC: another")).register_candidate(
        fx.CAND)  # fmt: skip
    cases: list[tuple[dict[str, Any], str]] = [
        ({"registry": other}, "protocol_frozen"),
        ({"assessor": PRODUCER}, "assessor_not_registered"),
        ({"ledger": lg}, "unrecorded_trials"),
        ({"ledger": read}, "holdout_used"),  # the caller's ledger already read it
        ({"at": T0}, "before_freeze"),
        ({"instrument": iid(4)}, "series_missing"),
    ]
    for kw, want in cases:
        assert code(partial(go, wrk, asr, w, **kw)) == want, kw
        assert counts(conn, w.t) == before, kw
    with tn.provision_tenant(app_conn) as tx:  # nothing frozen for this experiment
        bare = world(tx.tenant_id)
    assert code(lambda: go(wrk, asr, bare)) == "protocol_not_frozen"
    assert code(lambda: ar.promotion_run(wrk, asr, bare.t, request(w))) == "scope"
    assert counts(conn, w.t) == before
    run = go(wrk, asr, w)  # the read was kept for the real run
    assert (run.attempt, run.outcome.trial.outcome) == (1, Outcome.COMPLETED)


def test_decision_42_a_record_that_could_not_follow_is_refused_first(
    wrk: Conn, asr: Conn, conn: Conn, app_conn: Conn
) -> None:
    # `experiment_store.record` would refuse the verdict after the read (a
    # proposed experiment cannot take one), so the run refuses before it
    with tn.provision_tenant(app_conn) as tx:
        w = world(tx.tenant_id)
    with tn.tenant_transaction(asr, w.t) as tx:
        hs.preregister(tx, w.plan.plan_id, "holdout", w.holdout.series)
        hs.preregister(tx, w.plan.plan_id, "px-synth-1", w.holdout.series)
    empty = BudgetBook.open(str(w.t), AT, CAPS)
    es.record(wrk, asr, w.t, w.reg, EXP, "cand-1", w.plan, empty)  # proposed
    before = counts(conn, w.t)
    assert code(lambda: go(wrk, asr, w)) == "transition"
    assert counts(conn, w.t) == before


def test_decision_42_a_stored_read_no_run_reserved_is_used(
    wrk: Conn, asr: Conn, conn: Conn, w: World
) -> None:
    _, a = si.holdout_read(w.study, w.reg, fx.STRATS, w.ledger, "cand-1",
                           w.study.protocol.assessor, fx.AT_P, "acc-p",
                           "trial-p")  # fmt: skip
    with tn.tenant_transaction(asr, w.t) as tx:  # a caller-recorded read
        ast.put_read(tx, a)
    before = counts(conn, w.t)
    assert code(lambda: go(wrk, asr, w)) == "holdout_used"
    assert counts(conn, w.t) == before
    raw = ("INSERT INTO app.holdout_run (tenant_id, plan_id, attempt, seq, "
           "experiment_id, candidate_id, verdict_id, access_id, inputs_digest, "
           "recorded_at, event_hash) VALUES (%s, %s, 1, 1, %s, 'cand-1', 'v-p', "
           "'acc-p', repeat('a', 64), clock_timestamp(), repeat('b', 64))")  # fmt: skip
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        tx.conn.execute(raw, (w.t, w.plan.plan_id, EXP))  # an earlier read
    assert counts(conn, w.t) == before


def test_decision_42_only_the_assessors_read_of_this_transaction_is_reserved(
    asr: Conn, conn: Conn, w: World
) -> None:
    _, a = si.holdout_read(w.study, w.reg, fx.STRATS, w.ledger, "cand-1",
                           w.study.protocol.assessor, fx.AT_P, "acc-p",
                           "trial-p")  # fmt: skip
    raw = ("INSERT INTO app.holdout_run (tenant_id, plan_id, attempt, seq, "
           "experiment_id, candidate_id, verdict_id, access_id, inputs_digest, "
           "recorded_at, event_hash) VALUES (%s, %s, 1, 1, %s, 'cand-1', 'v-p', "
           "'acc-p', repeat('a', 64), clock_timestamp(), repeat('b', 64))")  # fmt: skip
    before = counts(conn, w.t)
    with pytest.raises(Crash), tn.tenant_transaction(asr, w.t) as tx:  # control
        ast.put_read(tx, a)
        tx.conn.execute(raw, (w.t, w.plan.plan_id, EXP))  # accepted, rolled back
        raise Crash("rollback")
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        ast.put_read(tx, replace(a, actor_id="actor-other"))  # not the assessor
        tx.conn.execute(raw, (w.t, w.plan.plan_id, EXP))
    assert counts(conn, w.t) == before


# --- crashes at each phase boundary ---


def test_decision_42_a_crash_before_the_reservation_leaves_a_fresh_run(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = counts(conn, w.t)
    crash(monkeypatch, ast, "reserve_read")  # after phase 0 loaded everything
    with pytest.raises(Crash):
        go(wrk, asr, w)
    assert counts(conn, w.t) == before  # the read was never recorded
    monkeypatch.undo()
    assert go(wrk, asr, w).attempt == 1  # so the next run is the first


POINTS = {
    "evaluate": (si, "holdout_run"),
    "outcome": (ast, "record_outcome"),
    "record": (es, "record"),
    "revision": (es, "_append"),
}


@pytest.mark.parametrize("where", list(POINTS))
def test_decision_42_a_crash_after_the_reservation_reruns_the_same_read(
    wrk: Conn,
    asr: Conn,
    conn: Conn,
    w: World,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    lg, out, v = expected(w)
    crash(monkeypatch, *POINTS[where])
    with pytest.raises(Crash):
        go(wrk, asr, w)
    mid = counts(conn, w.t)
    assert (mid["holdout_access"], mid["holdout_run"]) == (1, 1)
    assert mid["holdout_run_outcome"] == (where in ("record", "revision"))
    assert mid["evaluation_receipt"] == (where == "revision")
    monkeypatch.undo()
    run = go(wrk, asr, w)  # the same request: a re-run under the recorded read
    assert (run.attempt, run.wrote, run.ledger, run.outcome) == (2, True, lg, out)
    assert [x[0] for x in stored(asr, w)] == [v]  # one receipt, the oracle's
    after = counts(conn, w.t)
    assert (after["holdout_access"], after["holdout_run_outcome"]) == (1, 1)
    assert attempts(conn, w.t) == [(1, 1, "acc-p", "v-p"), (2, 1, "acc-p", "v-p")]
    assert after["experiment_revision"] == mid["experiment_revision"] + 1
    assert code(lambda: go(wrk, asr, w)) == "holdout_used"  # recorded: done
    assert counts(conn, w.t) == after


def test_decision_59_three_reruns_under_the_same_read(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    lg, out, v = expected(w)
    for where in ("evaluate", "outcome", "record"):  # three different deaths
        crash(monkeypatch, *POINTS[where])
        with pytest.raises(Crash):
            go(wrk, asr, w)
        monkeypatch.undo()
    run = go(wrk, asr, w)  # the third re-run
    assert (run.attempt, run.ledger, run.outcome) == (4, lg, out)
    assert [x[0] for x in stored(asr, w)] == [v]
    assert [a for a, *_ in attempts(conn, w.t)] == [1, 2, 3, 4]
    assert counts(conn, w.t)["holdout_access"] == 1  # still the one read


def test_decision_59_a_fourth_rerun_is_refused(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, si, "holdout_run")
    for _ in range(4):  # the run, then its three re-runs, all die
        with pytest.raises(Crash):
            go(wrk, asr, w)
    monkeypatch.undo()
    before = counts(conn, w.t)
    assert before["holdout_run"] == 4 and before["holdout_access"] == 1
    assert code(lambda: go(wrk, asr, w)) == "rerun_used"
    assert counts(conn, w.t) == before
    fifth = (
        "INSERT INTO app.holdout_run SELECT tenant_id, plan_id, 5, seq, "
        "experiment_id, candidate_id, verdict_id, access_id, inputs_digest, "
        "clock_timestamp(), event_hash FROM app.holdout_run WHERE attempt = 4"
    )
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        tx.conn.execute(fifth)  # the database holds the cap too
    conn.execute("ALTER TABLE app.holdout_run DISABLE TRIGGER append_only")
    conn.execute("DELETE FROM app.holdout_run WHERE attempt = 1")  # tampered
    conn.execute("ALTER TABLE app.holdout_run ENABLE TRIGGER append_only")
    assert code(lambda: go(wrk, asr, w)) == "integrity"  # attempts are 1..n


def test_decision_42_a_rerun_must_be_the_same_read(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, si, "holdout_run")
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    before = counts(conn, w.t)
    for kw in ({"access_id": "acc-q"}, {"trial_id": "trial-q"},  # a fresh read
               {"verdict_id": "v-q"}, {"at": fx.AT_P + fx.H},
               {"ledger": fx.rec(research(w, w.ledger, 2), 2,
                                 dataset_hash=w.ds.manifest.content_hash)},
               {"book": im.schedule(w.reg, (EXP,), BudgetBook.open(
                   str(w.t), AT, CAPS), AT + fx.H)[0]}):  # fmt: skip
        assert code(partial(go, wrk, asr, w, **kw)) == "rerun_mismatch", kw
        assert counts(conn, w.t) == before, kw
    assert go(wrk, asr, w).attempt == 2  # the true re-run is still available


HELD = ["1"] * len(fx.WIN_W)  # other strategy code: always in the market


@pytest.mark.parametrize("where", ["record", "revision"])
def test_decision_60_a_rerun_whose_outcome_differs_is_a_conflict(
    wrk: Conn,
    asr: Conn,
    conn: Conn,
    w: World,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    crash(monkeypatch, *POINTS[where])  # the outcome is recorded first
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    first, before = stored(asr, w), counts(conn, w.t)
    with tn.tenant_transaction(asr, w.t) as tx:
        kept = ast.outcome(tx, w.plan.plan_id)
    held = partial(go, wrk, asr, w, build=lambda cfg: scripted(HELD))
    assert code(held) == "verdict_conflict"  # a non-deterministic strategy
    with tn.tenant_transaction(asr, w.t) as tx:
        assert ast.outcome(tx, w.plan.plan_id) == kept
    assert stored(asr, w) == first  # the first result stands, no receipt added
    assert counts(conn, w.t) == before | {"holdout_run": 2}  # the attempt is spent


@pytest.mark.parametrize("where", ["evaluate", "outcome"])
def test_decision_60_before_an_outcome_there_is_nothing_to_compare(
    wrk: Conn,
    asr: Conn,
    conn: Conn,
    w: World,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    crash(monkeypatch, *POINTS[where])
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    run = go(wrk, asr, w, build=lambda cfg: scripted(HELD))  # no result was kept
    assert run.attempt == 2
    with tn.tenant_transaction(asr, w.t) as tx:  # its outcome is now the one
        assert ast.outcome(tx, w.plan.plan_id) is not None


# --- the database ---


def test_decision_42_the_database_holds_one_rerun_of_one_read(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, si, "holdout_run")
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    copy = ("INSERT INTO app.holdout_run (tenant_id, plan_id, attempt, seq, "
            "experiment_id, candidate_id, verdict_id, access_id, inputs_digest, "
            "recorded_at, event_hash) SELECT tenant_id, plan_id, {a}, seq, "
            "experiment_id, candidate_id, {v}, access_id, {d}, clock_timestamp(), "
            "event_hash FROM app.holdout_run WHERE attempt = 1")  # fmt: skip
    bad = [
        copy.format(a=3, v="verdict_id", d="inputs_digest"),  # skips attempt 2
        copy.format(a=5, v="verdict_id", d="inputs_digest"),  # a fifth attempt
        copy.format(a=2, v="'v-q'", d="inputs_digest"),  # another verdict
        copy.format(a=2, v="verdict_id", d="repeat('0', 64)"),  # other inputs
        copy.format(a=2, v="verdict_id", d="inputs_digest").replace(
            "clock_timestamp(),", "clock_timestamp() - interval '2 minutes',"
        ),  # not the database clock
    ]
    for sql in bad:
        with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(
                errors.CheckViolation):  # fmt: skip
            tx.conn.execute(sql)
    lg, _ = si.holdout_run(w.study, w.reg, fx.STRATS, w.ledger, "cand-1", w.ds,
                           w.rights, w.holdout, lambda cfg: scripted(fx.WIN_W),
                           w.study.protocol.assessor, fx.AT_P, "acc-p",
                           "trial-p")  # fmt: skip
    other = im.Assessment(lg, w.study.protocol.assessor, fx.AT_P, "v-other",
                          evaluation=fx.EVAL)  # fmt: skip
    with pytest.raises(es.ExperimentStoreError, match="database"):
        es.record(wrk, asr, w.t, w.reg, EXP, "cand-1", w.plan, w.book, None, other)
    assert counts(conn, w.t)["evaluation_receipt"] == 0  # only the run may score it
    for sql in (
        "UPDATE app.holdout_run SET verdict_id = 'v-q'",
        "DELETE FROM app.holdout_run",
    ):  # append-only, for the owner too
        with pytest.raises(errors.RaiseException):
            conn.execute(sql)
    assert go(wrk, asr, w).attempt == 2


def test_decision_60_the_database_records_the_outcome_before_the_receipt(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(2):  # attempts 1 and 2, neither recorded an outcome
        crash(monkeypatch, ast, "record_outcome")
        with pytest.raises(Crash):
            go(wrk, asr, w)
        monkeypatch.undo()
    lg, _, _ = expected(w)
    run = im.Assessment(lg, w.study.protocol.assessor, fx.AT_P, "v-p",
                        evaluation=fx.EVAL)  # the run's own verdict  # fmt: skip
    with pytest.raises(es.ExperimentStoreError, match="database"):
        es.record(wrk, asr, w.t, w.reg, EXP, "cand-1", w.plan, w.book, None, run)
    raw = ("INSERT INTO app.holdout_run_outcome (tenant_id, plan_id, attempt, "
           "outcome_digest, recorded_at, event_hash) VALUES (%s, %s, %s, "
           "repeat('a', 64), clock_timestamp(), repeat('b', 64))")  # fmt: skip
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        tx.conn.execute(raw, (w.t, w.plan.plan_id, 1))  # not the latest attempt
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        tx.conn.execute(
            raw.replace(
                "clock_timestamp()", "clock_timestamp() - interval '2 minutes'"
            ),
            (w.t, w.plan.plan_id, 2),
        )
    assert counts(conn, w.t)["evaluation_receipt"] == 0
    assert go(wrk, asr, w).attempt == 3
    with tn.tenant_transaction(asr, w.t) as tx, pytest.raises(errors.CheckViolation):
        tx.conn.execute(raw, (w.t, w.plan.plan_id, 3))  # after the receipt
    for sql in (
        "UPDATE app.holdout_run_outcome SET attempt = 1",
        "DELETE FROM app.holdout_run_outcome",
    ):  # append-only, owner too
        with pytest.raises(errors.RaiseException):
            conn.execute(sql)


def test_decision_60_after_the_outcome_only_the_runs_verdict_is_stored(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, es, "record")  # the read and the outcome are recorded
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    lg, _, _ = expected(w)
    other = im.Assessment(lg, w.study.protocol.assessor, fx.AT_P, "v-other",
                          evaluation=fx.EVAL)  # fmt: skip
    with pytest.raises(es.ExperimentStoreError, match="database"):
        es.record(wrk, asr, w.t, w.reg, EXP, "cand-1", w.plan, w.book, None, other)
    assert counts(conn, w.t)["evaluation_receipt"] == 0
    assert go(wrk, asr, w).attempt == 2


def test_decision_42_a_tampered_reservation_is_integrity(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, si, "holdout_run")
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    conn.execute("ALTER TABLE app.holdout_run DISABLE TRIGGER append_only")
    conn.execute("UPDATE app.holdout_run SET inputs_digest = repeat('0', 64)")
    conn.execute("ALTER TABLE app.holdout_run ENABLE TRIGGER append_only")
    before = counts(conn, w.t)
    assert code(lambda: go(wrk, asr, w)) == "integrity"  # the event hash
    assert counts(conn, w.t) == before


def test_decision_60_a_tampered_outcome_is_integrity(
    wrk: Conn, asr: Conn, conn: Conn, w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash(monkeypatch, es, "record")  # the outcome is recorded
    with pytest.raises(Crash):
        go(wrk, asr, w)
    monkeypatch.undo()
    conn.execute("ALTER TABLE app.holdout_run_outcome DISABLE TRIGGER append_only")
    conn.execute("UPDATE app.holdout_run_outcome SET outcome_digest = repeat('0', 64)")
    conn.execute("ALTER TABLE app.holdout_run_outcome ENABLE TRIGGER append_only")
    assert code(lambda: go(wrk, asr, w)) == "integrity"
    assert counts(conn, w.t)["evaluation_receipt"] == 0


# --- decision 62: a verdict certain to be BLOCKED refuses before the read ---


def test_decision_62_every_certain_blocked_cause_refuses_before_the_read(
    wrk: Conn, asr: Conn, conn: Conn, w: World
) -> None:
    p, s = w.study.protocol, str(w.t)
    open_ = BudgetBook.open(s, AT, CAPS)
    other = open_.reserve(
        p.budget_reservation_id,
        Category.IMPROVEMENT,
        UsdBudget("0.01"),
        p.provider_account_ref,
        p.max_output_tokens,
        AT,
        AT + p.max_duration,
    )[0]
    cases: list[tuple[dict[str, Any], str]] = [
        ({"book": open_}, "budget_not_reserved"),  # no reservation
        ({"book": w.book.release(p.budget_reservation_id)}, "budget_not_reserved"),
        ({"book": BudgetBook.open(str(uuid.uuid4()), AT, CAPS)},
         "budget_not_reserved"),  # another scope
        ({"book": other}, "reservation_terms_mismatch"),
        ({"ledger": TrialLedger("tenant-synth-other", w.ledger.entries)},
         "test_access_changed"),  # the ledger is not the protocol's scope
    ]  # fmt: skip
    before = counts(conn, w.t)
    # a ledger without the plan is refused even before phase 0
    assert code(lambda: go(wrk, asr, w, ledger=TrialLedger(s))) == (
        "test_access_changed")  # fmt: skip
    for kw, reason in cases:
        with pytest.raises(es.ExperimentStoreError) as e:
            go(wrk, asr, w, **kw)
        assert (e.value.code, reason in str(e.value)) == ("blocked", True), kw
        assert counts(conn, w.t) == before, kw
    # the other BLOCKED causes of `assess` are refused earlier still:
    # assessor_not_registered by `holdout_read` (test above), and
    # sandbox_unqualified cannot arise, since a strategy study is a strategy target
    with pytest.raises(im.ImprovementError, match="target"):
        si.StrategyStudy(replace(p, target=im.Target.APPLICATION_CODE), fx.SPACE,
                         fx.EVAL, fx.BASE)  # fmt: skip
    assert go(wrk, asr, w).attempt == 1  # the read was kept


def test_decision_42_other_roles_are_denied(
    wrk: Conn, asr: Conn, conn: Conn, w: World, runtime_urls: dict[str, str]
) -> None:
    before = counts(conn, w.t)
    assert code(lambda: ar.promotion_run(asr, wrk, w.t, request(w))) in (
        "forbidden", "database")  # the logins swapped  # fmt: skip
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_worker", "qw_release")
    for role in roles:
        with tn.runtime_connection(runtime_urls[role], role=role) as c:
            assert code(lambda: ar.promotion_run(wrk, c, w.t, request(w))) in (
                "forbidden", "database"), role  # fmt: skip
            for sql in ("SELECT attempt FROM app.holdout_run",
                        "DELETE FROM app.holdout_run",
                        "SELECT attempt FROM app.holdout_run_outcome",
                        "DELETE FROM app.holdout_run_outcome"):  # fmt: skip
                with tn.tenant_transaction(c, w.t), pytest.raises(
                        errors.InsufficientPrivilege):  # fmt: skip
                    c.execute(sql)
    assert counts(conn, w.t) == before
    held = conn.execute(
        "SELECT t, r, k FROM unnest(ARRAY['app.holdout_run', "
        "'app.holdout_run_outcome']) t CROSS JOIN unnest(ARRAY['qw_app', "
        "'qw_worker', 'qw_release', 'qw_ingest', 'qw_assessor']) r CROSS JOIN "
        "unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', "
        "'REFERENCES', 'TRIGGER']) k WHERE has_table_privilege(r, t, k) OR (k IN "
        "('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') AND "
        "has_any_column_privilege(r, t, k))").fetchall()  # fmt: skip
    assert set(held) == {
        (t, "qw_assessor", k)
        for t in ("app.holdout_run", "app.holdout_run_outcome")
        for k in ("SELECT", "INSERT")
    }
    assert go(wrk, asr, w).attempt == 1
