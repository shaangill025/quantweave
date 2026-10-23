"""The assessor-run strategy promotion (owner decision 39) with decision 42's
write-ahead read. `promotion_run` takes two idle autocommit logins (decision 40:
the assessor process holds a qw_assessor and a qw_worker login):
- phases 0 and 1, one qw_assessor transaction under the `experiment:<tenant>`
  lock: load the frozen protocol (`protocol_not_frozen` if none is stored,
  `protocol_frozen` if it differs), its lineage (`assessor_store.bind`) and the
  plan's protected `HoldoutData` (`holdout_series_store`); run every pre-read
  refusal (`strategy_improvement.holdout_read`, then what `experiment_store.record`
  would refuse, by the same derive on the unread ledger: `transition` and derive's
  own); then `assessor_store.reserve_read` records the read and the attempt, and
  the transaction commits. Every refusal comes first and rolls back, so the stored
  ledger is unchanged.
- phase 2: `holdout_run` evaluates on the reserved read (it must re-derive the
  same read, else `read_mismatch`), and `experiment_store.record` stores the
  trial's verdict as a receipt (qw_assessor) and the revision (qw_worker).
Decision 42: if the process dies after the reservation, the same request re-runs
once (attempt 2) under the recorded read: same read, candidate, protocol and
inputs (`inputs_digest`; any difference is `rerun_mismatch`), no new read. If the
first attempt's receipt was already stored, a re-run with a different verdict is
`verdict_conflict` (`store_verdict`). A further re-run is `rerun_used`, and a
recorded run is `holdout_used`. The caller keeps the request (and its pre-read
ledger) durably until a run returns, and persists the returned ledger.
LIMITATIONS: the ledger and budget book are still the caller's (decision 43's
marks are not added here); the strategy code (`build`) is not in the input digest
(it is manifest-registered and trusted, decision 49), so a re-run only proves it
determinate when the first receipt was stored; a crash before the receipt leaves
no earlier result to compare."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.backtest import Strategy, TrialOutcome
from qw_domain.budget import BudgetBook
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    ResearchDataset,
    ResearchError,
    ResearchRights,
    digest,
)
from qw_domain.strategy_registry import ParamValue, StrategyRegistry
from qw_domain.trial_ledger import Access, TrialLedger

from qw_adapters import assessor_store as ast
from qw_adapters import experiment_store as es
from qw_adapters import holdout_series_store as hs
from qw_adapters.assessor_store import ExperimentStoreError
from qw_adapters.tenancy import Conn, TenantTx, tenant_transaction


@dataclass(frozen=True, slots=True)
class Request:
    """One promotion run; a re-run repeats it exactly."""

    study: si.StrategyStudy
    registry: im.ExperimentRegistry
    strategies: StrategyRegistry
    ledger: TrialLedger  # before the read
    candidate_id: str
    dataset: ResearchDataset
    rights: ResearchRights
    instrument: InstrumentId  # the plan's holdout series to evaluate on
    build: Callable[[Mapping[str, ParamValue]], Strategy]
    book: BudgetBook
    assessor: im.Party
    at: datetime  # the read, the trial and the verdict
    access_id: str
    trial_id: str
    verdict_id: str


@dataclass(frozen=True, slots=True)
class Run:
    revision: es.Revision
    wrote: bool
    attempt: int  # 1, or 2 for decision 42's re-run
    ledger: TrialLedger  # after the read and the trial: the caller persists it
    outcome: TrialOutcome


def _inputs(r: Request, p: im.Protocol, a: Access, data: si.HoldoutData) -> str:
    """The digest of everything the run and its verdict depend on, but the code."""
    n = len(r.ledger.entries)
    c = r.registry.candidate(r.candidate_id)
    res = r.book.find(p.budget_reservation_id)
    series = sorted([k, s.series_id, s.content_hash]
                    for k, s in data.baseline_series.items())  # fmt: skip
    return digest([
        "holdout_run_inputs", p.content_hash, c.candidate_id, c.content_hash,
        r.verdict_id, format_instant(r.at), n, r.ledger.checkpoint(n), a.to_wire(),
        [data.series.series_id, data.series.content_hash], series,
        r.dataset.manifest.content_hash, r.study.evaluation.content_hash,
        [r.rights.tenant_id, format_instant(r.rights.at)],
        None if res is None else res.to_wire(), r.book.scope_id,
    ])  # fmt: skip


def _recorded(tx: TenantTx, eid: str, vid: str) -> bool:
    row = tx.conn.execute("SELECT count(*) FROM app.experiment_revision WHERE "
                          "tenant_id = %s AND experiment_id = %s AND verdict_id = %s",
                          (tx.tenant_id, eid, vid)).fetchone()  # fmt: skip
    return row is not None and row[0] > 0


type Reserved = tuple[
    int, Access, si.HoldoutData, si.StrategyStudy, im.ExperimentRegistry
]


def _reserve(tx: TenantTx, r: Request) -> Reserved:
    """Phases 0 and 1: load, refuse, then record the read (decision 42). The run
    uses the stored protocol (hash-equal to the study's, as `bind` checks)."""
    p = r.study.protocol
    stored = ast.load_protocol(tx, p.experiment_id)
    if stored is None:
        raise ExperimentStoreError("protocol_not_frozen", p.experiment_id)
    if stored.content_hash != p.content_hash:
        raise ExperimentStoreError("protocol_frozen", p.experiment_id)
    reg = ast.bind(tx, r.registry, p.experiment_id, r.candidate_id,
                   r.ledger.plan(p.plan_id))  # fmt: skip
    study = replace(r.study, protocol=reg.protocol(p.experiment_id))
    try:
        data = hs.holdout_data(tx, p.plan_id, r.instrument)
    except hs.HoldoutSeriesError as exc:
        raise ExperimentStoreError(exc.code, str(exc)) from None
    if _recorded(tx, p.experiment_id, r.verdict_id):
        raise ExperimentStoreError("holdout_used", "the run is recorded")
    _, a = si.holdout_read(study, reg, r.strategies, r.ledger, r.candidate_id,
                           r.assessor, r.at, r.access_id, r.trial_id)  # fmt: skip
    # what `experiment_store.record` would refuse after the read, refused now: the
    # same derive on the unread ledger (its state has the same predecessors)
    dry = im.Assessment(r.ledger, r.assessor, r.at, r.verdict_id,
                        evaluation=study.evaluation)  # fmt: skip
    rec, _ = im.derive(reg, p.experiment_id, r.candidate_id, r.ledger.plan(p.plan_id),
                       r.book, None, dry)  # fmt: skip
    es.check_next(tx, rec)
    attempt = ast.reserve_read(tx, p, a, r.candidate_id, r.verdict_id,
                               _inputs(r, p, a, data))  # fmt: skip
    return attempt, a, data, study, reg


def promotion_run(worker: Conn, assessor: Conn, tenant_id: uuid.UUID,
                  r: Request) -> Run:  # fmt: skip
    """Run `r` on the plan's one holdout read, recorded before it is opened."""
    p = r.study.protocol
    if p.scope is not im.Scope.TENANT or p.scope_id != str(tenant_id):
        raise ExperimentStoreError("scope", "tenant-scope runs of this tenant")
    try:
        r.ledger.plan(p.plan_id)
    except ResearchError as exc:
        raise ExperimentStoreError("test_access_changed", str(exc)) from None
    with tenant_transaction(assessor, tenant_id) as tx:  # phases 0 and 1
        tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"experiment:{tenant_id}",))  # fmt: skip
        attempt, a, data, study, reg = _reserve(tx, r)
    # phase 2: the read is recorded; only now is the holdout data opened
    lg, out = si.holdout_run(study, reg, r.strategies, r.ledger, r.candidate_id,
                             r.dataset, r.rights, data, r.build, r.assessor, r.at,
                             r.access_id, r.trial_id)  # fmt: skip
    if lg.access(r.access_id).to_wire() != a.to_wire():
        raise ExperimentStoreError("read_mismatch", r.access_id)
    run = im.Assessment(lg, r.assessor, ensure_aware_utc(r.at), r.verdict_id,
                        evaluation=r.study.evaluation)  # fmt: skip
    rev, wrote = es.record(worker, assessor, tenant_id, r.registry, p.experiment_id,
                           r.candidate_id, lg.plan(p.plan_id), r.book, None,
                           run)  # fmt: skip
    return Run(rev, wrote, attempt, lg, out)
