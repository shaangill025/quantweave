"""The experiment registry log (`0044_experiment_registry.sql`; owner decisions 71
and 79 to 81). An append-only,
hash-chained log per tenant of the domain `ExperimentRegistry`'s entries, so the
assessor can replay the registry a request was made against. Every write replays
the log through the domain first, so the log only ever holds a sequence the domain
accepts; an entry identical to a logged one writes nothing.
- `append(tx, entry, evidence)`: qw_assessor, protocols and candidates (decision
  81: `assessor_store.bind` mirrors what it binds). Under the `experiment:<tenant>`
  lock it replays the log and applies `register` or `register_candidate(entry,
  evidence)`; a protocol must be tenant scope of this tenant (`scope`); a domain
  refusal keeps its code (decision 80: `duplicate` for a candidate id another
  experiment of the tenant uses).
- `verdict_head(tx, verdict)` (qw_assessor) and `append_verdict(tx, verdict, head)`
  (qw_worker), decision 79: the assessor-side step replays the log, applies the
  verdict rules and hands over the head (None when the verdict is logged); the
  worker, which cannot read the log, writes the verdict as the next entry after
  that head, and the database refuses any other head (`registry_moved`).
- `load(tx, scope_id, as_of)`: qw_assessor. The registry of every entry recorded
  at or before `as_of` (a DB-clock instant no later than now), each re-hashed and
  its chain (seq, previous hash, time) re-checked, rebuilt through the strict wire
  forms and replayed through the domain; any failure is `integrity`. Entries are
  appended under the lock with the DB clock, so a later entry never changes the
  replay as of an earlier instant.
Any other login or kind is `forbidden` (the database refuses it). LIMITATIONS:
hash-only, unkeyed and unsigned: dropping the latest entries is not detected, and a
qw_worker login can append a verdict the domain would refuse (the assessor's next
replay is then `integrity`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import improvement as im
from qw_domain.feedback import ImprovementEvidence
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import ResearchError, digest

from qw_adapters.tenancy import TenantTx

_COLS = ("seq", "kind", "entry_id", "body", "recorded_at", "prev_hash", "event_hash")
_KINDS: dict[type, tuple[str, str]] = {
    im.Protocol: ("protocol", "experiment_id"),
    im.Candidate: ("candidate", "candidate_id"),
    im.Verdict: ("verdict", "verdict_id"),
}
_PARSE: dict[str, Any] = {"protocol": im.Protocol.from_wire,
                          "candidate": im.Candidate.from_wire,
                          "verdict": im.Verdict.from_wire}  # fmt: skip


class ExperimentRegistryError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class LogEntry:
    seq: int
    kind: str
    entry_id: str
    entry: im.Entry
    recorded_at: datetime
    event_hash: str


def entry_hash(tenant: object, seq: int, kind: str, entry_id: str, body: object,
               at: datetime, prev: str | None) -> str:  # fmt: skip
    return digest(["experiment_registry_entry", str(tenant), seq, kind, entry_id,
                   body, format_instant(at), prev])  # fmt: skip


def _database(exc: psycopg.Error) -> ExperimentRegistryError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return ExperimentRegistryError("forbidden", type(exc).__name__)
    return ExperimentRegistryError("database", type(exc).__name__)


def _now(tx: TenantTx, shared: bool) -> datetime:
    """Take the tenant's experiment lock, then read the DB clock."""
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    try:
        tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                        (f"experiment:{tx.tenant_id}",))  # fmt: skip
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    assert row is not None
    return ensure_aware_utc(row[0])


def _log(tx: TenantTx, as_of: datetime) -> tuple[im.ExperimentRegistry, list[LogEntry]]:
    try:
        cur = tx.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM app.experiment_registry_entry "
            "WHERE tenant_id = %s AND recorded_at <= %s ORDER BY seq",
            (tx.tenant_id, as_of),
        )
        rows = [dict(zip(_COLS, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise _database(exc) from None
    out: list[LogEntry] = []
    try:
        for r in rows:
            last = out[-1] if out else None
            at = ensure_aware_utc(r["recorded_at"])
            e = _PARSE[r["kind"]](r["body"])
            kind, eid = _ident(e)
            h = entry_hash(tx.tenant_id, r["seq"], kind, eid, e.wire(), at,
                           r["prev_hash"])  # fmt: skip
            if (
                r["seq"] != len(out) + 1
                or r["prev_hash"] != (last.event_hash if last else None)
                or (last is not None and at < last.recorded_at)
                or (kind, eid) != (r["kind"], r["entry_id"])
                or r["event_hash"] != h
            ):
                raise ValueError(f"entry {r['seq']}")
            out.append(LogEntry(r["seq"], kind, r["entry_id"], e, at,
                                r["event_hash"]))  # fmt: skip
        reg = im.ExperimentRegistry(str(tx.tenant_id), tuple(x.entry for x in out))
    except (ValueError, TypeError, KeyError, ResearchError) as exc:
        raise ExperimentRegistryError("integrity", type(exc).__name__) from None
    return reg, out


def load(tx: TenantTx, scope_id: str, as_of: datetime) -> im.ExperimentRegistry:
    """The tenant's registry as of `as_of`, replayed and verified."""
    if scope_id != str(tx.tenant_id):
        raise ExperimentRegistryError("scope", "tenant scope of this tenant")
    as_of = ensure_aware_utc(as_of)
    if as_of > _now(tx, shared=True):
        raise ExperimentRegistryError("as_of", "not a past instant")
    return _log(tx, as_of)[0]


def _ident(e: object) -> tuple[str, str]:
    """(kind, entry id) of a domain entry."""
    if type(e) not in _KINDS:
        raise TypeError("a domain Protocol, Candidate or Verdict is required")
    kind, key = _KINDS[type(e)]
    return kind, getattr(e, key)


@dataclass(frozen=True, slots=True)
class Head:  # the latest logged entry, handed from the assessor to the worker
    seq: int
    event_hash: str | None


def _replay(tx: TenantTx, e: im.Entry, evidence: ImprovementEvidence | None,
            ) -> tuple[datetime, list[LogEntry], LogEntry | None]:  # fmt: skip
    """Lock, replay and apply `e`: (now, the log, its identical logged copy)."""
    kind, eid = _ident(e)
    at = _now(tx, shared=False)
    reg, log = _log(tx, at)
    for x in log:  # by wire: a read-back scorecard keeps its thresholds sorted
        if (x.kind, x.entry_id, x.entry.wire()) == (kind, eid, e.wire()):
            return at, log, x
    try:
        if type(e) is im.Protocol:
            reg.register(e)
        elif type(e) is im.Candidate:
            reg.register_candidate(e, evidence)
        else:
            assert type(e) is im.Verdict
            reg.record_verdict(e)
    except im.ImprovementError as exc:
        raise ExperimentRegistryError(exc.code, str(exc)) from None
    return at, log, None


def _insert(tx: TenantTx, e: im.Entry, head: Head, at: datetime) -> LogEntry:
    kind, eid = _ident(e)
    seq, body = head.seq + 1, e.wire()
    h = entry_hash(tx.tenant_id, seq, kind, eid, body, at, head.event_hash)
    try:
        with tx.conn.transaction():
            tx.conn.execute(
                f"INSERT INTO app.experiment_registry_entry (tenant_id, "
                f"{', '.join(_COLS)}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (tx.tenant_id, seq, kind, eid, Jsonb(body), at, head.event_hash, h),
            )
    except psycopg.errors.CheckViolation:
        raise ExperimentRegistryError("registry_moved", "not the head") from None
    except psycopg.Error as exc:
        raise _database(exc) from None
    return LogEntry(seq, kind, eid, e, at, h)


def _head(log: list[LogEntry]) -> Head:
    return Head(log[-1].seq, log[-1].event_hash) if log else Head(0, None)


def append(
    tx: TenantTx,
    e: im.Protocol | im.Candidate,
    evidence: ImprovementEvidence | None = None,
) -> tuple[LogEntry, bool]:
    """Append a protocol or candidate as the next entry; (the entry, wrote)."""
    if type(e) not in (im.Protocol, im.Candidate):
        raise TypeError("a domain Protocol or Candidate is required")
    t = str(tx.tenant_id)
    if type(e) is im.Protocol and (e.scope, e.scope_id) != (im.Scope.TENANT, t):
        raise ExperimentRegistryError("scope", "tenant scope of this tenant")
    at, log, same = _replay(tx, e, evidence)
    if same is not None:
        return same, False
    return _insert(tx, e, _head(log), at), True


def verdict_head(tx: TenantTx, v: im.Verdict) -> Head | None:
    """The head the worker appends `v` after, once the domain accepts it; None
    when `v` is logged already."""
    if type(v) is not im.Verdict:
        raise TypeError("a domain Verdict is required")
    _, log, same = _replay(tx, v, None)
    return None if same is not None else _head(log)


def append_verdict(tx: TenantTx, v: im.Verdict, head: Head) -> LogEntry:
    """Write `v` (qw_worker, blind) as the entry after `head`."""
    if type(v) is not im.Verdict or type(head) is not Head:
        raise TypeError("a domain Verdict and its handed-over head are required")
    return _insert(tx, v, head, _now(tx, shared=False))
