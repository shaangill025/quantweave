"""The assessment request store (`0041_assessment_request.sql`;
owner decisions 43, 64, 65 and 67). The caller of an assessor-run promotion keeps
its request durably here until the run returns:
- `describe(request)`: the stored form. The caller-supplied trial ledger and
  budget book in full (decision 67; the domain wire forms), the strategy
  reference: id, version and implementation hash of the manifest the run builds
  (decision 65), and every other input by content hash (decision 67):
  `study` (protocol, search space, evaluation protocol and base manifest
  hashes), `registry` (every experiment-registry entry's wire), `strategy_manifest`
  (that manifest's material hash), `dataset` (its manifest content hash, which
  covers every record) and `rights` (defined here: the rights terms and the
  research-use decision for each dataset feed at `rights.at`, so the registry is
  bound by its effect on this dataset, not by its full body). The ids, time,
  instrument and assessor party are stored as they are.
- `submit(tx, request)`: qw_worker only (decision 64); one request per tenant,
  experiment and verdict id (`request_exists`), the ledger and protocol scoped to
  the session tenant (`tenant_mismatch`).
- `load(tx, experiment_id, verdict_id)`: qw_assessor only (decision 64); re-hashes
  the row and re-parses the ledger and book through their strict wire forms, else
  `integrity`.
- `check(stored, request)`: what the run calls before it acts; any difference is
  `request_mismatch`, naming the fields.
Any other login is `forbidden` (the database refuses it). LIMITATIONS: nothing
calls `load` or `check` yet (the wiring increment); `build` is resolved by the
qw-assessor process's reviewed registry, not built yet (decision 65); decision
43's caller-supplied marks go on the verdict record (decision 66), not here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import improvement as im
from qw_domain import strategy_improvement as si
from qw_domain.budget import BudgetBook
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    RESEARCH_USES,
    ResearchDataset,
    ResearchRights,
    digest,
)
from qw_domain.rights import Use, check_use
from qw_domain.trial_ledger import TrialLedger

from qw_adapters.assessor_run import Request
from qw_adapters.tenancy import TenantTx

_COLS = ("experiment_id", "verdict_id", "body", "recorded_at", "request_hash")


class AssessmentRequestError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StrategyRef:  # decision 65
    strategy_id: str
    version: str
    implementation_hash: str


@dataclass(frozen=True, slots=True)
class StoredRequest:
    experiment_id: str
    verdict_id: str
    ledger: TrialLedger
    book: BudgetBook
    strategy: StrategyRef
    body: Mapping[str, Any]
    recorded_at: datetime
    request_hash: str


def study_hash(s: si.StrategyStudy) -> str:
    return digest(["strategy_study", s.protocol.content_hash, s.space.content_hash,
                   s.evaluation.content_hash, s.base.material_hash])  # fmt: skip


def registry_hash(r: im.ExperimentRegistry) -> str:
    return digest(["experiment_registry", r.scope_id,
                   [[type(e).__name__, e.wire()] for e in r.entries]])  # fmt: skip


def _us(d: timedelta) -> str:
    return str(d // timedelta(microseconds=1))


def rights_hash(r: ResearchRights, ds: ResearchDataset) -> str:
    """The terms, and the decision `require_research_use` takes on each feed."""
    uses = []
    for feed in sorted(ds.manifest.sources):
        for use in RESEARCH_USES:
            keep = r.retain_for if use is Use.RETENTION else None
            d = check_use(r.registry, r.tenant_id, feed, use, r.at, scope=r.scope,
                          jurisdiction=r.jurisdiction, retain_for=keep)  # fmt: skip
            uses.append([feed, use.value, d.allowed,
                         [[x.code.value, x.subject] for x in d.reasons],
                         d.feed_hash, d.entitlement_hash])  # fmt: skip
    return digest(["research_rights", r.tenant_id, format_instant(r.at),
                   r.scope.value, r.jurisdiction, _us(r.retain_for), uses])  # fmt: skip


def describe(r: Request) -> dict[str, Any]:
    """The stored form of `r` (decisions 65 and 67)."""
    try:
        m = r.strategies.manifest(r.study.base.strategy_id, r.candidate_id)
        a = r.assessor
        return {
            "experiment_id": r.study.protocol.experiment_id,
            "verdict_id": r.verdict_id, "candidate_id": r.candidate_id,
            "access_id": r.access_id, "trial_id": r.trial_id,
            "at": format_instant(r.at), "instrument": r.instrument.to_wire(),
            "assessor": [a.actor_id, a.route_id, a.family_id, a.version],
            "ledger": r.ledger.to_wire(), "book": r.book.to_wire(),
            "strategy": [m.strategy_id, m.version, m.implementation_hash],
            "inputs": {"study": study_hash(r.study),
                       "registry": registry_hash(r.registry),
                       "strategy_manifest": m.material_hash,
                       "dataset": r.dataset.manifest.content_hash,
                       "rights": rights_hash(r.rights, r.dataset)},
        }  # fmt: skip
    except (ValueError, TypeError, KeyError) as exc:
        raise AssessmentRequestError("invalid", type(exc).__name__) from None


def _hash(tx: TenantTx, body: Mapping[str, Any], at: datetime) -> str:
    return digest(["assessment_request", str(tx.tenant_id), body["experiment_id"],
                   body["verdict_id"], body, format_instant(at)])  # fmt: skip


def _database(exc: psycopg.Error) -> AssessmentRequestError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return AssessmentRequestError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return AssessmentRequestError("request_exists", type(exc).__name__)
    return AssessmentRequestError("database", type(exc).__name__)


def submit(tx: TenantTx, r: Request) -> str:
    """Store `r` for its run; returns the request hash (qw_worker cannot read)."""
    body = describe(r)
    p, t = r.study.protocol, str(tx.tenant_id)
    if (p.scope, p.scope_id, r.ledger.tenant_id) != (im.Scope.TENANT, t, t):
        raise AssessmentRequestError("tenant_mismatch", "the session tenant's run")
    try:
        with tx.conn.transaction():
            row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
            assert row is not None
            at = ensure_aware_utc(row[0])
            h = _hash(tx, body, at)
            tx.conn.execute(
                f"INSERT INTO app.assessment_request (tenant_id, {', '.join(_COLS)}) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (tx.tenant_id, body["experiment_id"], body["verdict_id"],
                 Jsonb(body), at, h),
            )  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None
    return h


def load(tx: TenantTx, experiment_id: str, verdict_id: str) -> StoredRequest | None:
    """The stored request, re-verified, or None if there is none."""
    try:
        row = tx.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM app.assessment_request WHERE "
            "tenant_id = %s AND experiment_id = %s AND verdict_id = %s",
            (tx.tenant_id, experiment_id, verdict_id),
        ).fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    if row is None:
        return None
    try:
        r = dict(zip(_COLS, row, strict=True))
        body, at = r["body"], ensure_aware_utc(r["recorded_at"])
        ledger = TrialLedger.from_wire(body["ledger"])
        book = BudgetBook.from_wire(body["book"])
        ref = body["strategy"]
        if (
            r["request_hash"] != _hash(tx, body, at)
            or (r["experiment_id"], r["verdict_id"]) != (experiment_id, verdict_id)
            or ledger.tenant_id != str(tx.tenant_id)
            or type(ref) is not list
            or len(ref) != 3
            or not all(type(x) is str for x in ref)
        ):
            raise ValueError("assessment_request")
    except (ValueError, TypeError, KeyError) as exc:  # ResearchError, BudgetError
        raise AssessmentRequestError("integrity", type(exc).__name__) from None
    return StoredRequest(experiment_id, verdict_id, ledger, book, StrategyRef(*ref),
                         body, at, r["request_hash"])  # fmt: skip


def check(stored: StoredRequest, r: Request) -> None:
    """Refuse a run whose inputs are not exactly the stored request's."""
    now = describe(r)
    keys = stored.body.keys() | now.keys()
    diff = sorted(k for k in keys if k != "inputs" and stored.body.get(k) != now.get(k))
    old, new = stored.body.get("inputs", {}), now["inputs"]
    diff += sorted(f"inputs.{k}" for k in old.keys() | new.keys()
                   if old.get(k) != new.get(k))  # fmt: skip
    if diff:
        raise AssessmentRequestError("request_mismatch", ",".join(diff))
