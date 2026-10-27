"""`qw-assessor` / `python -m qw_worker.assessor`: the assessor process (owner
decision 40) that runs assessor-run strategy promotions (decision 39) from the
durable job queue, in the foreground (a process supervisor owns restarts).

Logins (decision 40): the process holds both a qw_worker login, QW_DATABASE_URL
(to claim jobs and write the experiment revision), and a qw_assessor login,
QW_ASSESSOR_DATABASE_URL (to read holdout data and write verdicts). Each is
opened with `runtime_connection(url, role=...)`, so each must be exactly its role
and a mixed login is refused at startup; no new database grants. An ordinary
`qw-worker` refuses to start when QW_ASSESSOR_DATABASE_URL is set, so it never
holds the assessor login.

Strategy code (decision 65): `STRATEGIES` is this process's built-in, reviewed
registry. It is empty, so every run is refused
(`strategy_implementation_unavailable`) until a real strategy is reviewed and
registered here with its implementation hash.

Jobs: `AssessorLoop` claims kind `assessment.promotion` from the experiment pool
(priority `experiment`; ordinary workers have no handler for it, so they never
claim it), and kind `assessment.optimizer` (T050): an installation-scope
`assessor_run_optimizer.Request` that `assessor_run_optimizer.trial` (which takes
no port: it ends at decision 62's `sandbox_unqualified`) runs on the qw_assessor
login alone; the job's tenant only routes it (decision 92). Decision 93: a job
whose tenant is not the installation's bootstrap tenant (`bootstrap_tenant`; none
for a hosted edition) is refused before any registry read (`installation_scope`),
and every trial refusal shows the tenant only `optimizer_refused` (the real code
is logged); `database` and uncoded faults still retry. The promotion payload
is `AssessmentJob`: the tenant's request id with the experiment and candidate it
names (the request id is the verdict id), as `submit_job` writes it with the
request in one qw_worker transaction (decision 64). A `RequestSource` turns it
into an `assessor_run.Request`: the default, `STORE_SOURCE`, loads the stored
request (`request_missing` if there is none) and takes the inputs it binds by
hash (decision 67) from an `InputsProvider`, called in the qw_assessor
transaction that loaded the request (REPEATABLE READ, READ ONLY: one snapshot).
The default,
`STORE_INPUTS` (`store_inputs`), reads each from its store (decision 70: the
registry log as of submit, the study inputs, the base manifest with the
candidate's re-derived, the dataset with its rights at the database clock) and
refuses a missing one under its own code; `check` then refuses any bound hash
that differs (`request_mismatch`). Then
`promotion_run` runs,
outside any job transaction (it needs idle connections), and the job is
completed. A refusal (any error with a short `code`: an invalid payload, a
missing request, a decision 62 `blocked`, `rerun_used`, a conflict) fails the
job without retry, under that code. Any other error (a crash-like fault) fails it
as retryable `assessment_failed` (a store's `database` fault keeps its code and
is retryable too), so the queue's retry re-runs the same request
under its recorded read (decisions 42 and 59). A redelivered job whose run is
already recorded (`run_recorded`) is completed. The lease is extended
(`jobs.heartbeat`) after each committed phase of the run; a lost lease stops the
run and records nothing (the reaper requeues it). LIMITATIONS: one phase longer
than the lease (the evaluation) is still reaped and may run twice, bounded by
decision 59; no publish target, purges or reaping here (the standard worker); a
strategy candidate with evidence uses is refused at `bind` (`evidence_not_logged`:
the run passes no evidence store); the registry log is hash-only, so dropping its
latest entries is not detected (experiment_registry_store)."""

from __future__ import annotations

import logging
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from types import FrameType

from qw_adapters import assessment_request_store as ars
from qw_adapters import assessor_run as ar
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import dataset_store, strategy_store, study_store, tenancy
from qw_adapters import experiment_registry_store as ers
from qw_adapters import jobs as qj
from qw_adapters.jobs import ClaimedJob, LeaseLost
from qw_adapters.strategy_implementations import Implementations
from qw_adapters.tenancy import Conn, TenantTx, tenant_transaction
from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.identity import InstrumentId
from qw_domain.instants import parse_instant
from qw_domain.jobs import JobState, Pool, Priority, check_error_code
from qw_domain.research_data import (
    ResearchDataset,
    ResearchError,
    ResearchRights,
    check_id,
    digest,
)
from qw_domain.strategy_registry import (
    ParamKind,
    ParamValue,
    StrategyManifest,
    StrategyRegistry,
)

from qw_worker.config import WorkerSettings, settings_from_env

log = logging.getLogger("qw_worker.assessor")
KIND = "assessment.promotion"
OPT_KIND = "assessment.optimizer"
ASSESSOR_URL = "QW_ASSESSOR_DATABASE_URL"
# Decision 65: the reviewed strategy implementations. Empty: nothing runs yet.
STRATEGIES = Implementations(())
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Refused(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class AssessmentJob:
    """The job payload: which stored request to run (decision 43's store is
    keyed by tenant and request id), and the experiment and candidate it names,
    which the source must match."""

    request_id: str
    experiment_id: str
    candidate_id: str

    @classmethod
    def from_payload(cls, payload: object) -> AssessmentJob:
        keys = ("request_id", "experiment_id", "candidate_id")
        if not isinstance(payload, dict) or sorted(payload) != sorted(keys):
            raise Refused("payload_invalid", "exactly " + ", ".join(keys))
        try:
            for k in keys:
                check_id(payload[k], k)
        except (ResearchError, TypeError):
            raise Refused("payload_invalid", "ids") from None
        return cls(payload["request_id"], payload["experiment_id"],
                   payload["candidate_id"])  # fmt: skip

    def to_payload(self) -> dict[str, str]:
        return {"request_id": self.request_id, "experiment_id": self.experiment_id,
                "candidate_id": self.candidate_id}  # fmt: skip


# (the qw_assessor connection, tenant, job) -> the request to run, which must
# name the job's experiment and candidate.
RequestSource = Callable[[Conn, uuid.UUID, AssessmentJob], ar.Request]


@dataclass(frozen=True, slots=True)
class BoundInputs:
    """What a stored request binds by hash only (decision 67), for the source to
    rebuild the request with; `check` refuses any that differ."""

    study: si.StrategyStudy
    registry: im.ExperimentRegistry
    strategies: StrategyRegistry
    dataset: ResearchDataset
    rights: ResearchRights


# Called inside the qw_assessor transaction that loaded the request, which is
# REPEATABLE READ, READ ONLY: a provider reads its input stores from the request's
# own snapshot and can write nothing.
InputsProvider = Callable[[TenantTx, ars.StoredRequest], BoundInputs]


def _param(kind: ParamKind, wire: str) -> ParamValue:
    if kind is ParamKind.INTEGER:
        return int(wire)
    if kind is ParamKind.DECIMAL:
        return Decimal(wire)
    if kind is ParamKind.BOOLEAN:
        return {"true": True, "false": False}[wire]
    return wire


def candidate_manifest(base: StrategyManifest, space: si.SearchSpace,
                       c: im.Candidate) -> StrategyManifest:  # fmt: skip
    """Decision 78: the candidate's manifest re-derived from the stored base and
    search space and its logged parameters; it must give the candidate's artifact
    and payload hashes (the payload hash covers each parameter's canonical wire
    form, so a non-canonical or extra parameter differs): `candidate_mismatch`."""
    try:
        was = base.resolve({})  # the base defaults; derive takes only changes
        values = {p.name: _param(p.kind, c.payload[p.name]) for p in base.parameters}
        m = si.derive(base, space, {k: v for k, v in values.items() if v != was[k]},
                      c.candidate_id)  # fmt: skip
        if (m.material_hash, m.config_hash({})) != (c.artifact_hash, c.payload_hash):
            raise ValueError("re-derived")
    except (ValueError, TypeError, KeyError, ArithmeticError):  # the domain errors
        raise Refused("candidate_mismatch", c.candidate_id) from None
    return m


def store_inputs(tx: TenantTx, stored: ars.StoredRequest) -> BoundInputs:
    """Every input `stored` binds by hash (decision 67), read from its store in the
    request's own qw_assessor transaction; a missing one is refused under its code:
    - the experiment registry, replayed as of the request's recorded time
      (decision 71), so a later entry is not seen; it must hold the request's
      protocol and candidate (`registry_missing`);
    - the study: the protocol from that registry, its search space by
      `mutation_space_hash` (`search_space_missing`), the evaluation protocol by
      its plan's `protocol_hash` (`evaluation_protocol_missing`), the plan from the
      stored ledger, and the base manifest by the reviewed reference's id and
      version (decision 69; `strategy_missing`);
    - the candidate manifest, re-derived (decision 78, `candidate_manifest`);
    - the dataset the plan names (`dataset_missing`) and its rights, both from the
      dataset store, which re-checks rights and retention at the database clock
      (decisions 75 and 77: `rights_refused`, `retention_expired`); never a
      caller's time. `promotion_run` checks them again at the read.
    A store's or the domain's refusal keeps its code (a `database` fault stays
    retryable). Each is located by what the request binds, never by a newer or a
    similar record, and `store_source` then `check`s every bound hash
    (`request_mismatch`, naming each that differs)."""
    reg = ers.load(tx, str(tx.tenant_id), stored.recorded_at)
    try:
        p = reg.protocol(stored.experiment_id)
        c = reg.candidate(stored.body["candidate_id"])
    except im.ImprovementError as exc:
        raise Refused("registry_missing", exc.code) from None
    plan = stored.ledger.plan(p.plan_id)
    h = p.mutation_space_hash
    space = None if h is None else study_store.space(tx, h)
    if space is None:
        raise Refused("search_space_missing", str(h))
    ev = study_store.evaluation(tx, plan.protocol_hash)
    if ev is None:
        raise Refused("evaluation_protocol_missing", plan.protocol_hash)
    ref = stored.strategy
    base = strategy_store.manifest(tx, ref.strategy_id, ref.version)
    if base is None:
        raise Refused("strategy_missing", f"{ref.strategy_id}@{ref.version}")
    study = si.StrategyStudy(p, space.space, ev.protocol, base)
    m = candidate_manifest(base, space.space, c)
    found = dataset_store.load(tx, plan.dataset)
    if found is None:
        raise Refused("dataset_missing", plan.dataset.dataset_id)
    return BoundInputs(study, reg, StrategyRegistry().register(base).register(m),
                       found.dataset, found.rights)  # fmt: skip


STORE_INPUTS: InputsProvider = store_inputs


def store_source(inputs: InputsProvider = STORE_INPUTS) -> RequestSource:
    """The request as qw_worker submitted it (decisions 43 and 64): loaded from the
    request store as qw_assessor, rebuilt with `inputs` (called in that same
    REPEATABLE READ, READ ONLY transaction, so one snapshot), then `check`ed
    against the stored body and hashes (`request_mismatch` on any difference)."""

    def get(assessor: Conn, tenant_id: uuid.UUID, job: AssessmentJob) -> ar.Request:
        with tenant_transaction(assessor, tenant_id, snapshot=True) as tx:
            stored = ars.load(tx, job.experiment_id, job.request_id)
            if stored is None:
                raise Refused("request_missing", job.request_id)
            i = inputs(tx, stored)  # the same snapshot
        b = stored.body
        r = ar.Request(
            i.study, i.registry, i.strategies, stored.ledger, b["candidate_id"],
            i.dataset, i.rights, InstrumentId.from_wire(b["instrument"]),
            stored.strategy, stored.book, im.Party(*b["assessor"]),
            parse_instant(b["at"]), b["access_id"], b["trial_id"], stored.verdict_id,
        )  # fmt: skip
        ars.check(stored, r)
        return r

    return get


STORE_SOURCE = store_source(STORE_INPUTS)


def submit_job(tx: TenantTx, r: ar.Request) -> uuid.UUID:
    """qw_worker (decision 64): store `r` and enqueue its job in the caller's one
    transaction. The job names the request by its verdict id (the store's key with
    the experiment) and is idempotent on the request hash."""
    h = ars.submit(tx, r)
    job = AssessmentJob(r.verdict_id, r.study.protocol.experiment_id, r.candidate_id)
    jid, _ = qj.enqueue(tx, KIND, h, Priority.EXPERIMENT, job.to_payload())
    return jid


def bootstrap_tenant(assessor: Conn) -> uuid.UUID | None:
    """The installation's bootstrap tenant (0004; 0048 lets qw_assessor read its
    one column): the operator's tenant for self-hosted (decision 93); None when
    there is no row (a hosted edition, until it has its own binding)."""
    row = assessor.execute(
        "SELECT tenant_id FROM app.installation_bootstrap"
    ).fetchone()
    return None if row is None else uuid.UUID(str(row[0]))


# what an optimizer refusal may still show the tenant (decision 93): the retried
# faults, which say nothing about the registry, and the completion of a recorded run
_OPT_PASS = frozenset({"database", "run_recorded"})


def submit_optimizer_job(tx: TenantTx, r: aro.Request) -> uuid.UUID:
    """Enqueue `r` (any login that may enqueue in `tx`'s tenant), idempotent on it."""
    body = r.to_payload()
    jid, _ = qj.enqueue(tx, OPT_KIND, digest(["optimizer_trial", body]),
                        Priority.EXPERIMENT, body)  # type: ignore[arg-type]  # fmt: skip
    return jid


def _refusal(exc: Exception) -> str | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


class AssessorLoop:
    def __init__(self, worker: Conn, assessor: Conn, settings: WorkerSettings, *,
                 source: RequestSource = STORE_SOURCE,
                 strategies: Implementations = STRATEGIES) -> None:  # fmt: skip
        self.worker, self.assessor, self.settings = worker, assessor, settings
        self.source, self.strategies = source, strategies
        self.stopping = False

    def request_stop(self) -> None:
        self.stopping = True

    def run_one(self) -> tuple[uuid.UUID, JobState | None] | None:
        s = self.settings
        job = qj.claim(self.worker, s.worker_name, Pool.EXPERIMENT, s.lease,
                       (KIND, OPT_KIND))  # fmt: skip
        if job is None:
            return None
        code, retryable = "", False
        try:
            self._run(job, partial(qj.heartbeat, self.worker, job, s.lease))
        except LeaseLost:  # another claim may own the job now: record nothing
            log.warning("job %s lost its lease during the run", job.id)
            return job.id, None
        except Exception as exc:
            refused = _refusal(exc)
            if refused == "run_recorded":  # a redelivery of a recorded run
                code = ""
            elif refused == "database":  # a database fault, not a refusal: retry
                code, retryable = refused, True
            elif refused is not None:
                code = refused
            else:
                log.exception("job %s: the assessment failed", job.id)
                code, retryable = "assessment_failed", True
        return job.id, self._finish(job, code, retryable)

    def _run(self, job: ClaimedJob, beat: Callable[[], object]) -> None:
        if job.kind == OPT_KIND:
            self._optimizer(job, beat)
            return
        spec = AssessmentJob.from_payload(job.payload)
        r = self.source(self.assessor, job.tenant_id, spec)
        if (r.study.protocol.experiment_id, r.candidate_id) != (
            spec.experiment_id, spec.candidate_id
        ):  # fmt: skip
            raise Refused("request_mismatch", spec.request_id)
        ar.promotion_run(self.worker, self.assessor, job.tenant_id, r,
                         self.strategies, beat)  # fmt: skip

    def _optimizer(self, job: ClaimedJob, beat: Callable[[], object]) -> None:
        """Decisions 92 and 93: only the bootstrap tenant's job runs, refused before
        any registry read (`installation_scope`); every refusal of the trial shows
        the tenant only `optimizer_refused`, its real code and detail logged here."""
        if bootstrap_tenant(self.assessor) != job.tenant_id:
            log.warning("job %s: tenant %s is not the installation's operator tenant",
                        job.id, job.tenant_id)  # fmt: skip
            raise Refused("installation_scope")
        try:
            aro.trial(self.assessor, aro.Request.from_payload(job.payload), beat)
        except LeaseLost:
            raise
        except Exception as exc:
            code = _refusal(exc)
            if code is None or code in _OPT_PASS:
                raise
            log.warning("job %s: optimizer trial refused: %s", job.id, exc)
            raise Refused("optimizer_refused") from None

    def _finish(self, job: ClaimedJob, code: str, retryable: bool) -> JobState | None:
        try:
            with tenant_transaction(self.worker, job.tenant_id) as tx:
                if not code:
                    qj.complete(tx, job)
                    return JobState.SUCCEEDED
                return qj.fail(tx, job, check_error_code(code), retryable=retryable,
                               backoff=self.settings.job_backoff)  # fmt: skip
        except LeaseLost:
            log.warning("job %s lost its lease before its outcome was recorded", job.id)
            return None

    def run(self, max_ticks: int | None = None) -> int:
        ticks = 0
        while not self.stopping and (max_ticks is None or ticks < max_ticks):
            done = self.run_one()
            ticks += 1
            if done is None and not self.stopping:
                time.sleep(self.settings.idle_wait.total_seconds())
        return ticks


def _stop_on_signals(loop: AssessorLoop) -> None:
    def stop(signum: int, frame: FrameType | None) -> None:
        log.info("signal %s: stopping after the current job", signum)
        loop.request_stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)


def main(environ: Mapping[str, str] = os.environ) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    worker_url, assessor_url = environ.get("QW_DATABASE_URL"), environ.get(ASSESSOR_URL)
    if not worker_url or not assessor_url:
        print(f"need QW_DATABASE_URL and {ASSESSOR_URL}", file=sys.stderr)
        return 2
    try:
        settings = settings_from_env(environ)
    except ValueError as exc:
        print(f"invalid worker settings: {exc}", file=sys.stderr)
        return 2
    try:
        with (
            tenancy.runtime_connection(worker_url, role="qw_worker") as worker,
            tenancy.runtime_connection(assessor_url, role="qw_assessor") as assessor,
        ):
            loop = AssessorLoop(worker, assessor, settings)
            _stop_on_signals(loop)
            loop.run()
    except tenancy.TenancyError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
