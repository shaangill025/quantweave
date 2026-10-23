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
claim it). The payload is `AssessmentJob`: the tenant's request id with the
experiment and candidate it names (the request id is the verdict id), as
`submit_job` writes it with the request in one qw_worker transaction (decision
64). A `RequestSource` turns it into an `assessor_run.Request`: the default,
`STORE_SOURCE`, loads the stored request (`request_missing` if there is none) and
needs the inputs it binds by hash (decision 67) from an `InputsProvider`, called
in the qw_assessor transaction that loaded the request; none of
them has a store yet, so the default provider refuses (`inputs_unavailable`) and
every run fails closed until they do. Then `promotion_run` runs,
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
decision 59; no publish target, purges or reaping here (the standard worker)."""

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
from functools import partial
from types import FrameType

from qw_adapters import assessment_request_store as ars
from qw_adapters import assessor_run as ar
from qw_adapters import jobs as qj
from qw_adapters import tenancy
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
)
from qw_domain.strategy_registry import StrategyRegistry

from qw_worker.config import WorkerSettings, settings_from_env

log = logging.getLogger("qw_worker.assessor")
KIND = "assessment.promotion"
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


# Called inside the qw_assessor transaction that loaded the request, so a
# provider reads its input stores from the same snapshot.
InputsProvider = Callable[[TenantTx, ars.StoredRequest], BoundInputs]


def no_inputs(tx: TenantTx, stored: ars.StoredRequest) -> BoundInputs:
    """No store holds these inputs yet (T032 and the research-data, search-space
    and evaluation-protocol stores), so the assessor cannot obtain them: refuse."""
    raise Refused("inputs_unavailable", "study, registry, strategies, dataset, rights")


def store_source(inputs: InputsProvider = no_inputs) -> RequestSource:
    """The request as qw_worker submitted it (decisions 43 and 64): loaded from the
    request store as qw_assessor, rebuilt with `inputs` (called in that same
    transaction), then `check`ed against the stored body and hashes
    (`request_mismatch` on any difference)."""

    def get(assessor: Conn, tenant_id: uuid.UUID, job: AssessmentJob) -> ar.Request:
        with tenant_transaction(assessor, tenant_id) as tx:
            stored = ars.load(tx, job.experiment_id, job.request_id)
            if stored is None:
                raise Refused("request_missing", job.request_id)
            i = inputs(tx, stored)  # the same transaction and snapshot
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


STORE_SOURCE = store_source()


def submit_job(tx: TenantTx, r: ar.Request) -> uuid.UUID:
    """qw_worker (decision 64): store `r` and enqueue its job in the caller's one
    transaction. The job names the request by its verdict id (the store's key with
    the experiment) and is idempotent on the request hash."""
    h = ars.submit(tx, r)
    job = AssessmentJob(r.verdict_id, r.study.protocol.experiment_id, r.candidate_id)
    jid, _ = qj.enqueue(tx, KIND, h, Priority.EXPERIMENT, job.to_payload())
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
        job = qj.claim(self.worker, s.worker_name, Pool.EXPERIMENT, s.lease, (KIND,))
        if job is None:
            return None
        code, retryable = "", False
        try:
            spec = AssessmentJob.from_payload(job.payload)
            r = self.source(self.assessor, job.tenant_id, spec)
            if (r.study.protocol.experiment_id, r.candidate_id) != (
                spec.experiment_id, spec.candidate_id
            ):  # fmt: skip
                raise Refused("request_mismatch", spec.request_id)
            beat = partial(qj.heartbeat, self.worker, job, s.lease)
            ar.promotion_run(self.worker, self.assessor, job.tenant_id, r,
                             self.strategies, beat)  # fmt: skip
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
