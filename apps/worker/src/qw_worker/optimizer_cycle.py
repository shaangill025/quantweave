"""The `optimizer.cycle` job (T050 increment 8; decisions 87, 92, 93, 97): an
experiment-pool worker on qw_worker runs one optimizer cycle on the development
fixture and stores it. `handle`: a job from any tenant but the bootstrap one (or
with no bootstrap row: hosted) is `installation_scope` before any read; then the
strict `CycleJob` payload (the protocol as logged: qw_worker cannot read the
registry); the deployment head (`release_store.load`), from which `run_cycle`
derives `runner_authority` (the baseline until 0050 admits an install); the
cycle at the DB clock; `append_cycle` in the job's transaction, where 0047 and
0051 admit only the logged baseline under the logged protocol. Coded refusals
fail as `optimizer_refused` (the code is logged); `database` retries.
LIMITATIONS: no history (qw_worker cannot read cycles), so patience and the cap
see one cycle; the base, dev fixture (evidence class unchecked) and limits are
the operator's."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from qw_adapters import jobs as qj
from qw_adapters import optimizer_fixture_store as ofs
from qw_adapters import optimizer_store as os_
from qw_adapters import release_store as rs
from qw_adapters.jobs import ClaimedJob
from qw_adapters.tenancy import Conn, TenantTx
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import research_improvement as ri
from qw_domain.decimals import UsdBudget
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import Priority
from qw_domain.research_data import ResearchError, check_id, digest

from qw_worker.loop import Handler, HandlerFailed

log = logging.getLogger("qw_worker.optimizer_cycle")
KIND = "optimizer.cycle"
_KEYS = ("installation_id", "experiment_id", "cycle_id", "deployment_id",
         "protocol", "runner", "base", "dev", "limits")  # fmt: skip
_BAD = (ValueError, TypeError, KeyError, IndexError, ResearchError,
        im.ImprovementError)  # fmt: skip


class CycleRefused(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class CycleJob:
    installation_id: str
    experiment_id: str
    cycle_id: str
    deployment_id: str
    protocol: im.Protocol
    runner: oi.OptimizerConfig
    base: ri.ResearchConfig
    dev: ri.Fixture
    limits: oi.Limits

    def to_payload(self) -> dict[str, object]:
        lim = self.limits
        return dict(zip(_KEYS, (
            self.installation_id, self.experiment_id, self.cycle_id,
            self.deployment_id, self.protocol.wire(),
            [self.runner.version_id, self.runner.to_payload()],
            self.base.to_payload(), ofs.fixture_wire(self.dev),
            [str(lim.max_depth), str(lim.patience), lim.monthly_cap.to_wire(),
             lim.cost_per_proposal.to_wire()]), strict=True))  # fmt: skip

    @classmethod
    def from_payload(cls, w: object) -> CycleJob:
        if not isinstance(w, dict) or sorted(w) != sorted(_KEYS):
            raise CycleRefused("payload_invalid", "exactly " + ", ".join(_KEYS))
        try:
            i, e, c, d = (check_id(w[k], k) for k in _KEYS[:4])
            version, policy = w["runner"]
            depth, patience, cap, cost = w["limits"]
            if not all(type(x) is str and x.isascii() and x.isdigit()
                       for x in (depth, patience)):  # fmt: skip
                raise ValueError("limits")
            job = cls(i, e, c, d, im.Protocol.from_wire(w["protocol"]),
                      oi.OptimizerConfig.from_payload(version, policy),
                      ri.ResearchConfig.from_payload(w["base"]),
                      ofs.fixture_from_wire(w["dev"]),
                      oi.Limits(int(depth), int(patience), UsdBudget.from_wire(cap),
                                UsdBudget.from_wire(cost)))  # fmt: skip
        except (*_BAD, oi.OptimizerError):
            raise CycleRefused("payload_invalid", "fields") from None
        if job.to_payload() != w:
            raise CycleRefused("payload_invalid", "not canonical")
        return job


def submit(tx: TenantTx, j: CycleJob) -> uuid.UUID:
    """Enqueue `j` in `tx`'s tenant (decision 92), idempotent on its payload."""
    body = j.to_payload()
    jid, _ = qj.enqueue(tx, KIND, digest(["optimizer_cycle", body]),
                        Priority.EXPERIMENT, body)  # type: ignore[arg-type]  # fmt: skip
    return jid


def bootstrap_tenant(conn: Conn) -> uuid.UUID | None:
    """The installation's bootstrap tenant (0004; 0051: qw_worker reads the column)."""
    row = conn.execute("SELECT tenant_id FROM app.installation_bootstrap").fetchone()
    return None if row is None else uuid.UUID(str(row[0]))


def run(conn: Conn, j: CycleJob) -> os_.StoredCycle:
    """One cycle of `j`'s runner under the deployment head, stored (qw_worker)."""
    p = j.protocol
    if (p.scope, p.scope_id, p.target, p.experiment_id) != (
        im.Scope.INSTALLATION, j.installation_id, oi.OPT, j.experiment_id
    ):  # fmt: skip
        raise CycleRefused("scope", p.experiment_id)
    try:
        reg = im.ExperimentRegistry(j.installation_id).register(p)
    except im.ImprovementError as exc:
        raise CycleRefused(exc.code, str(exc)) from None
    ledger = rs.load(conn, j.deployment_id)
    if ledger is None:
        raise CycleRefused("deployment_unknown", j.deployment_id)
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    r = oi.run_cycle(j.cycle_id, j.runner, reg, j.experiment_id, ledger, j.base,
                     j.dev, j.limits, (), ensure_aware_utc(row[0]))  # fmt: skip
    if isinstance(r, oi.Blocked):
        raise CycleRefused("cycle_blocked", ",".join(r.reasons))
    return os_.append_cycle(conn, j.installation_id, j.experiment_id, r, p)


def handle(tx: TenantTx, job: ClaimedJob) -> None:
    if bootstrap_tenant(tx.conn) != job.tenant_id:  # decision 93, before any read
        log.warning("job %s: tenant %s is not the installation's operator tenant",
                    job.id, job.tenant_id)  # fmt: skip
        raise HandlerFailed("installation_scope", retryable=False)
    try:
        run(tx.conn, CycleJob.from_payload(job.payload))
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str):
            raise
        if code == "database":
            raise HandlerFailed("database", retryable=True) from None
        log.warning("job %s: optimizer cycle refused: %s", job.id, exc)
        raise HandlerFailed("optimizer_refused", retryable=False) from None


HANDLERS: Mapping[str, Handler] = MappingProxyType({KIND: handle})
