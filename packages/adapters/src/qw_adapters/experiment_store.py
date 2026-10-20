"""Improvement experiments (T044 increment 2; `0026_experiments.sql`).
- `record` takes `qw_domain.improvement.record`'s inputs and calls it, so it can
  store nothing else (no record is accepted from the caller); as qw_worker (the
  only INSERT role). Equal to the latest revision it writes nothing, else it must
  `may_follow` it. Tenant scope of this tenant only (`scope`).
- Every read re-hashes each revision, checks the chain (1..n, previous hash, times),
  rebuilds rows through the domain `ExperimentRecord` and `may_follow`, and checks
  a reservation id names one experiment; any failure is `integrity`. Hash-only, no
  C-20 signing (as pause_store): dropping a chain's latest rows is not detected.
- Unknown and foreign ids read as None (forced RLS). Times are the DB clock; lock
  `experiment:<tenant>` (shared to read) comes last. Nothing here promotes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

import psycopg
from qw_domain import improvement as im
from qw_domain.budget import BudgetBook
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.trial_ledger import SplitPlan

from qw_adapters.tenancy import TenantTx

__all__ = ["ExperimentStoreError", "Revision", "load", "page", "record",
           "revision_hash"]  # fmt: skip
_COLUMNS = tuple(f.name for f in fields(im.ExperimentRecord))


class ExperimentStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Revision:
    revision: int
    recorded_at: datetime
    digest: str
    record: im.ExperimentRecord


def revision_hash(tenant: object, revision: int, rec: im.ExperimentRecord,
                  at: datetime, previous: str | None) -> str:  # fmt: skip
    body = ["experiment_revision", str(tenant), revision, rec.wire(),
            format_instant(at), previous]  # fmt: skip
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"experiment:{tx.tenant_id}",))  # fmt: skip


def _decode(row: dict[str, Any]) -> im.ExperimentRecord:
    d = row["decision"]
    return im.ExperimentRecord(**(row | {
        "target": im.Target(row["target"]), "scope": im.Scope(row["scope"]),
        "state": im.RecordState(row["state"]),
        "decision": None if d is None else im.Decision(d),
        "data_snapshot_ids": tuple(row["data_snapshot_ids"]),
        "reasons": tuple(row["reasons"]),
    }))  # fmt: skip


def _chains(tx: TenantTx, eid: str | None) -> dict[str, list[Revision]]:
    """Every chain (or `eid`'s), verified, oldest revision first."""
    where, args = ((" AND experiment_id = %s", (tx.tenant_id, eid)) if eid else
                   ("", (tx.tenant_id,)))  # fmt: skip
    sql = (f"SELECT {', '.join(_COLUMNS)}, revision, recorded_at, previous_hash, "
           f"event_hash FROM app.experiment_revision WHERE tenant_id = %s{where} "
           "ORDER BY experiment_id, revision")  # fmt: skip
    try:
        cur = tx.conn.execute(sql, args)
        names = [c.name for c in cur.description or ()]
        rows = [dict(zip(names, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise ExperimentStoreError("database", type(exc).__name__) from None
    out: dict[str, list[Revision]] = {}
    owner: dict[str, str] = {}
    try:
        for row in rows:
            rev, prev, h = row.pop("revision"), row.pop("previous_hash"), row.pop(
                "event_hash")  # fmt: skip
            at = ensure_aware_utc(row.pop("recorded_at"))
            rec = _decode(row)
            chain = out.setdefault(rec.experiment_id, [])
            last = chain[-1] if chain else None
            ok = (
                rev == len(chain) + 1 and prev == (last.digest if last else None)
                and rec.scope is im.Scope.TENANT
                and rec.scope_id == str(tx.tenant_id)
                and (last is None or (at >= last.recorded_at
                                      and rec.may_follow(last.record)))
                and owner.setdefault(rec.budget_reservation_id,
                                     rec.experiment_id) == rec.experiment_id
                and h == revision_hash(tx.tenant_id, rev, rec, at, prev)
            )  # fmt: skip
            if not ok:
                raise ValueError(f"{rec.experiment_id} revision {rev}")
            chain.append(Revision(rev, at, h, rec))
    except (ValueError, TypeError, KeyError) as exc:  # domain errors included
        raise ExperimentStoreError("integrity", type(exc).__name__) from None
    return out


def load(tx: TenantTx, experiment_id: str) -> Revision | None:
    """The latest revision, verified; None when unknown or another tenant's."""
    _lock(tx, shared=True)
    chain = _chains(tx, experiment_id).get(experiment_id)
    return chain[-1] if chain else None


def page(tx: TenantTx, limit: int, after: str | None) -> list[Revision]:
    """Up to `limit` experiments with ids after `after`, by id; verifies every
    chain of the tenant (linear in its revisions)."""
    _lock(tx, shared=True)
    chains = sorted(_chains(tx, None).items())
    return [c[-1] for e, c in chains if after is None or e > after][:limit]


def record(
    tx: TenantTx, registry: im.ExperimentRegistry, experiment_id: str,
    candidate_id: str, plan: SplitPlan, book: BudgetBook,
    outcome: im.Scheduled | im.Deferred | None = None,
    assessment: im.Assessment | None = None,
) -> tuple[Revision, bool]:  # fmt: skip
    """Derive the record (`improvement.record`) and append it as the experiment's
    next revision; (revision, wrote)."""
    if type(registry) is not im.ExperimentRegistry:
        raise TypeError("the domain registry is required, not a record")
    rec = im.record(registry, experiment_id, candidate_id, plan, book, outcome,
                    assessment)  # fmt: skip
    if rec.scope is not im.Scope.TENANT or rec.scope_id != str(tx.tenant_id):
        raise ExperimentStoreError("scope", "tenant-scope records of this tenant")
    _lock(tx, shared=False)
    chain = _chains(tx, rec.experiment_id).get(rec.experiment_id, [])
    last = chain[-1] if chain else None
    if last is not None and last.record == rec:
        return last, False
    if last is not None and not rec.may_follow(last.record):
        raise ExperimentStoreError("transition", "not a later state of this record")
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    at, n = ensure_aware_utc(row[0]), len(chain) + 1
    previous = last.digest if last else None
    digest = revision_hash(tx.tenant_id, n, rec, at, previous)
    values = {**rec.wire(), "tenant_id": tx.tenant_id, "revision": n,
              "recorded_at": at, "previous_hash": previous,
              "event_hash": digest}  # fmt: skip
    cols = ", ".join(values)  # this module's literal names
    try:
        with tx.conn.transaction():
            tx.conn.execute(f"INSERT INTO app.experiment_revision ({cols}) VALUES "
                            f"({', '.join(['%s'] * len(values))})",
                            tuple(values.values()))  # fmt: skip
    except psycopg.errors.UniqueViolation:
        raise ExperimentStoreError("reservation", "another experiment's") from None
    except psycopg.Error as exc:
        raise ExperimentStoreError("database", type(exc).__name__) from None
    got = _chains(tx, rec.experiment_id)[rec.experiment_id][-1]
    if (got.revision, got.digest) != (n, digest):
        raise ExperimentStoreError("integrity", "the stored revision differs")
    return got, True
