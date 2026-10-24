"""The assessor-run strategy promotion (owner decision 39) with decision 42's
write-ahead read. `promotion_run` takes two idle autocommit logins (decision 40:
the assessor process holds a qw_assessor and a qw_worker login):
- phases 0 and 1, one qw_assessor transaction under the `experiment:<tenant>`
  lock: load the frozen protocol (`protocol_not_frozen` if none is stored,
  `protocol_frozen` if it differs), its lineage (`assessor_store.bind`) and the
  plan's protected `HoldoutData` (`holdout_series_store`); run every pre-read
  refusal (`strategy_improvement.holdout_read`, then what `experiment_store.record`
  would refuse, by the same derive on the unread ledger: `transition`, derive's
  own, and decision 62's `blocked` for a verdict certain to be BLOCKED); then
  `assessor_store.reserve_read` records the read and the attempt, and the
  transaction commits. Every refusal comes first and rolls back, so the stored
  ledger is unchanged.
- phase 2: `holdout_run` evaluates on the reserved read (it must re-derive the
  same read, else `read_mismatch`).
- phase 3 (decision 60), its own qw_assessor transaction: the digest of the trial
  outcome (the ledger after the read and trial, and the refusal reason) is
  recorded once per plan, before any receipt; a later attempt whose outcome
  differs is `verdict_conflict`.
- phase 4: `experiment_store.record` stores the trial's verdict as a receipt
  (qw_assessor) and the revision (qw_worker).
Decisions 42 and 59: if the process dies after the reservation, the same request
re-runs under the recorded read, at most three times (attempts 2 to 4): same
read, candidate, protocol and inputs (`inputs_digest`; any difference is
`rerun_mismatch`), no new read. A different result is `verdict_conflict` once an
outcome is recorded (decision 60; also `store_verdict` once a receipt is). A
fifth attempt is `rerun_used`, and a recorded run is `run_recorded`. Decision 61:
loading and verifying the stored series in phase 0 is not opening it; opening is
the evaluation. Decision 65: the request names its strategy code by `StrategyRef`,
resolved through the process's reviewed registry before phase 0 (unregistered,
changed or another strategy's code is refused), and the reference with its
implementation hash is in the input digest, so changed code is `rerun_mismatch`.
Decisions 75, 77 and 85: the rights are in the digest by their grant (terms and
each feed's decision), not their check time; phase 1 re-loads the stored dataset
and re-checks its rights, feed revisions and retention under its stored terms at
the database clock just before the read is recorded (`rights_refused`,
`retention_expired`, and `request_mismatch` for a changed grant, each refused
before the read is spent), and the evaluation uses those rights. A reservation
digested before this has the time in it, so its re-run is `rerun_mismatch`.
The caller keeps the request (and its pre-read ledger) durably until a run
returns, and persists the returned ledger.
LIMITATIONS: the ledger and budget book are still the caller's (decision 43's
marks are not added here); a crash after the evaluation and before the outcome is
recorded leaves no earlier result to compare. A reservation made before decision
65 (0038 and 0040 rows) carries a digest without the reference, so its re-run is
`rerun_mismatch`: refused, never misread; no stored row changes."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.backtest import TrialOutcome
from qw_domain.budget import BudgetBook
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    RESEARCH_USES,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    digest,
)
from qw_domain.rights import Use, check_use
from qw_domain.strategy_registry import StrategyRegistry
from qw_domain.trial_ledger import Access, TrialLedger

from qw_adapters import assessor_store as ast
from qw_adapters import dataset_store as dst
from qw_adapters import experiment_store as es
from qw_adapters import holdout_series_store as hs
from qw_adapters.assessor_store import ExperimentStoreError
from qw_adapters.strategy_implementations import Implementations, StrategyRef
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
    strategy: StrategyRef  # resolved through the reviewed registry (decision 65)
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


def _us(d: timedelta) -> str:
    return str(d // timedelta(microseconds=1))


def rights_hash(r: ResearchRights, ds: ResearchDataset) -> str:
    """Decision 85: the tenant, the terms and the decision `require_research_use`
    takes on each feed at `r.at` (feed and entitlement hashes); not `r.at` itself
    (decision 77: each run checks rights at its own time of use)."""
    uses = []
    for feed in sorted(ds.manifest.sources):
        for use in RESEARCH_USES:
            keep = r.retain_for if use is Use.RETENTION else None
            d = check_use(r.registry, r.tenant_id, feed, use, r.at, scope=r.scope,
                          jurisdiction=r.jurisdiction, retain_for=keep)  # fmt: skip
            uses.append([feed, use.value, d.allowed,
                         [[x.code.value, x.subject] for x in d.reasons],
                         d.feed_hash, d.entitlement_hash])  # fmt: skip
    return digest(["research_rights", r.tenant_id, r.scope.value, r.jurisdiction,
                   _us(r.retain_for), uses])  # fmt: skip


def _inputs(r: Request, p: im.Protocol, a: Access, data: si.HoldoutData) -> str:
    """The digest of everything the run and its verdict depend on; the code by
    its reviewed implementation hash (decision 65); the rights by `rights_hash`
    (decision 85: their grant, not their check time)."""
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
        rights_hash(r.rights, r.dataset),
        None if res is None else res.to_wire(), r.book.scope_id,
        [r.strategy.strategy_id, r.strategy.version, r.strategy.implementation_hash],
    ])  # fmt: skip


def _recorded(tx: TenantTx, eid: str, vid: str, cid: str) -> bool:
    """A revision records this verdict for this candidate (`candidate_version`
    holds the candidate id): the run is done. Another candidate's revision under
    the verdict id is not this run."""
    row = tx.conn.execute("SELECT count(*) FROM app.experiment_revision WHERE "
                          "tenant_id = %s AND experiment_id = %s AND verdict_id = %s "
                          "AND candidate_version = %s",
                          (tx.tenant_id, eid, vid, cid)).fetchone()  # fmt: skip
    return row is not None and row[0] > 0


type Reserved = tuple[int, Access, si.HoldoutData, si.StrategyStudy,
                      im.ExperimentRegistry, ResearchRights]  # fmt: skip


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
    if _recorded(tx, p.experiment_id, r.verdict_id, r.candidate_id):
        raise ExperimentStoreError("run_recorded", r.verdict_id)
    _, a = si.holdout_read(study, reg, r.strategies, r.ledger, r.candidate_id,
                           r.assessor, r.at, r.access_id, r.trial_id)  # fmt: skip
    # what `experiment_store.record` would refuse after the read, refused now: the
    # same derive on the unread ledger (its state has the same predecessors)
    dry = im.Assessment(r.ledger, r.assessor, r.at, r.verdict_id,
                        evaluation=study.evaluation)  # fmt: skip
    rec, v = im.derive(reg, p.experiment_id, r.candidate_id, r.ledger.plan(p.plan_id),
                       r.book, None, dry)  # fmt: skip
    if v is not None and v.decision is im.Decision.BLOCKED:  # decision 62
        raise ExperimentStoreError("blocked", ",".join(v.reasons))
    es.check_next(tx, rec)
    rights = _rights_now(tx, r)  # decisions 75 and 77, at the read
    attempt = ast.reserve_read(tx, p, a, r.candidate_id, r.verdict_id,
                               _inputs(r, p, a, data))  # fmt: skip
    return attempt, a, data, study, reg, rights


def _rights_now(tx: TenantTx, r: Request) -> ResearchRights:
    """The dataset's rights re-checked at the database clock, under the terms
    stored with it (decision 77), in the transaction that records the read: a
    revocation or expiry since the request was built is refused here
    (`rights_refused`, `retention_expired`; `dataset_missing` with no stored
    dataset), and so is a grant that changed but still allows the use, whose
    `rights_hash` is no longer the one the request binds (decision 85:
    `request_mismatch`), all before the read is spent. The evaluation uses them."""
    try:
        found = dst.load(tx, r.dataset.manifest)
    except dst.DatasetStoreError as exc:
        raise ExperimentStoreError(exc.code, str(exc)) from None
    if found is None:
        raise ExperimentStoreError("dataset_missing", r.dataset.manifest.dataset_id)
    if rights_hash(found.rights, r.dataset) != rights_hash(r.rights, r.dataset):
        raise ExperimentStoreError("request_mismatch", "inputs.rights")
    return found.rights


def promotion_run(worker: Conn, assessor: Conn, tenant_id: uuid.UUID, r: Request,
                  strategies: Implementations,
                  beat: Callable[[], object] = lambda: None) -> Run:  # fmt: skip
    """Run `r` on the plan's one holdout read, recorded before it is opened, with
    the strategy code `strategies` (the process's reviewed registry) resolves.
    `beat` (the job's lease heartbeat) runs after each committed phase; whatever
    it raises stops the run there."""
    p = r.study.protocol
    if p.scope is not im.Scope.TENANT or p.scope_id != str(tenant_id):
        raise ExperimentStoreError("scope", "tenant-scope runs of this tenant")
    base = r.study.base
    if (r.strategy.strategy_id, r.strategy.version) != (base.strategy_id,
                                                        base.version):  # fmt: skip
        raise ExperimentStoreError("strategy_mismatch", r.strategy.strategy_id)
    build = strategies.resolve(r.strategy).build
    try:
        r.ledger.plan(p.plan_id)
    except ResearchError as exc:
        raise ExperimentStoreError("test_access_changed", str(exc)) from None
    with tenant_transaction(assessor, tenant_id) as tx:  # phases 0 and 1
        tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"experiment:{tenant_id}",))  # fmt: skip
        attempt, a, data, study, reg, rights = _reserve(tx, r)
    beat()
    # phase 2: the read is recorded; only now is the holdout data opened
    lg, out = si.holdout_run(study, reg, r.strategies, r.ledger, r.candidate_id,
                             r.dataset, rights, data, build, r.assessor, r.at,
                             r.access_id, r.trial_id)  # fmt: skip
    if lg.access(r.access_id).to_wire() != a.to_wire():
        raise ExperimentStoreError("read_mismatch", r.access_id)
    beat()
    n = len(lg.entries)  # the read and the trial, metrics included
    done = digest(["holdout_run_outcome", n, lg.checkpoint(n), out.reason])
    with tenant_transaction(assessor, tenant_id) as tx:  # phase 3 (decision 60)
        ast.record_outcome(tx, p.plan_id, attempt, done)
    beat()
    run = im.Assessment(lg, r.assessor, ensure_aware_utc(r.at), r.verdict_id,
                        evaluation=r.study.evaluation)  # fmt: skip
    rev, wrote = es.record(worker, assessor, tenant_id, r.registry, p.experiment_id,
                           r.candidate_id, lg.plan(p.plan_id), r.book, None,
                           run)  # fmt: skip
    return Run(rev, wrote, attempt, lg, out)
