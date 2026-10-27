"""Optimizer experiments, cycles and lineage (`0047_optimizer_store.sql`; T050,
owner decision 87: installation tables; qw_assessor writes the registry, qw_worker
writes cycles, only operators read them; decision 90: qw_release reads for
maintainers and operators; decision 91: qw_assessor also reads cycles).
Installation scope: plain connections, no tenant. Every load re-hashes each row
and re-parses the strict domain wire; any mismatch is `integrity`.
- `append(conn, installation_id, entry, evidence)` (qw_assessor): a protocol,
  candidate or verdict of an optimizer experiment, as the next entry of the
  installation's chain. Under the `optimizer:<installation>` lock it replays the
  log through the domain registry and applies the entry; a protocol must be an
  optimizer protocol of this installation (`scope`, then `check_protocol`'s code);
  a domain refusal keeps its code; an entry identical to a logged one writes
  nothing. `load(conn, installation_id, as_of)` (qw_assessor, qw_release): the
  registry of every entry recorded at or before `as_of`, chain re-checked.
- `append_cycle(conn, installation_id, experiment_id, receipt, protocol)` (qw_worker,
  without SELECT; the refusal codes tell it whether a cycle id, receipt,
  time or runner was taken or wrong): the strict `CycleReceipt` wire under a
  store-issued `cycle_ref` and the DB clock. The database refuses a duplicate
  (`duplicate`), a receipt time not
  within 60 s before the recording or before the experiment's latest cycle
  (`back_dated`) and any runner but the logged protocol's frozen baseline
  (`runner_not_baseline`) or a `protocol` it ran under that is not the logged one
  (0051, `protocol_mismatch`). `load_cycles` (qw_release, qw_assessor) returns them
  in recording order, failing closed for the whole experiment (`integrity`) if any
  row does not re-hash or re-parse.
- `append_lineage(conn, installation_id, receipt)` (qw_assessor, no SELECT): only a
  `LineageReceipt` (which only `lineage` and `verify_lineage` make); it must cite
  a logged protocol and verdict (`not_logged`); a second under its id or hash is
  `duplicate`. `load_lineage` (qw_release) returns an `UnverifiedLineage`, never
  a `LineageReceipt`: the caller runs `verify_lineage`.
- `RunLog(conn, installation_id)` (qw_assessor; 0049): the trial's durable run log
  (decisions 42, 59, 60). Writes take the `optimizer:<installation>` lock and
  verify the plan's rows first; 0049's guards give the refusals. `put_outcome`
  keeps the first digest (`verdict_conflict` for another). Reads: `integrity`.
A database refusal of the clock window is `clock`; any other login is `forbidden`.
LIMITATIONS: hash-only, unkeyed and unsigned (dropping the latest rows is not
detected); an installed V1's cycles cannot be stored (0029 refuses an optimizer
release, so no install can be checked); the cycle's experiment is the caller's
(the wire does not carry it, so a baseline cycle can be filed under another
experiment with the same baseline); the cycle writer is the worker, so a
well-formed but false cycle can be stored (`verify_recursion` re-executes)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain.feedback import ImprovementEvidence
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import ResearchError, digest

from qw_adapters.experiment_registry_store import _PARSE, Head, LogEntry, _ident

Conn = psycopg.Connection[Any]
_COLS = ("seq", "kind", "entry_id", "body", "recorded_at", "prev_hash", "event_hash")
_REFUSALS = frozenset({"clock", "registry_moved", "back_dated", "runner_not_baseline",
                       "not_logged", "attempt_moved", "rerun_mismatch", "rerun_used",
                       "not_evaluating", "run_reserved", "run_recorded",
                       "not_reserved", "protocol_mismatch"})  # fmt: skip
MAX_ATTEMPTS = 4  # the reservation and three re-runs (decision 59)
_BAD = (ValueError, TypeError, KeyError, ResearchError)


class OptimizerStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredCycle:
    cycle_ref: uuid.UUID  # issued by this store, never by the cycle's runner
    receipt: oi.CycleReceipt
    recorded_at: datetime  # the database clock


def _database(exc: psycopg.Error) -> OptimizerStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return OptimizerStoreError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return OptimizerStoreError("duplicate", type(exc).__name__)
    msg = (exc.diag.message_primary or "").rpartition(": ")[2]
    if isinstance(exc, psycopg.errors.CheckViolation) and msg in _REFUSALS:
        return OptimizerStoreError(msg)
    return OptimizerStoreError("database", type(exc).__name__)


def _rows(conn: Conn, sql: str, *args: object) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(sql, args)
        names = [c.name for c in cur.description or ()]
        return [dict(zip(names, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise _database(exc) from None


def _now(conn: Conn, lock: str | None = None, shared: bool = False) -> datetime:
    """Take the installation's lock, then read the DB clock."""
    if lock is not None:
        fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
        _rows(conn, f"SELECT {fn}(hashtextextended(%s, 0))", lock)
    return ensure_aware_utc(_rows(conn, "SELECT clock_timestamp() AS t")[0]["t"])


def _insert(conn: Conn, table: str, values: dict[str, object]) -> None:
    try:
        with conn.transaction():
            conn.execute(f"INSERT INTO app.{table} ({', '.join(values)}) VALUES "
                         f"({', '.join(['%s'] * len(values))})",
                         tuple(values.values()))  # fmt: skip
    except psycopg.Error as exc:
        raise _database(exc) from None


def entry_hash(installation: str, seq: int, kind: str, entry_id: str, body: object,
               at: datetime, prev: str | None) -> str:  # fmt: skip
    return digest(["optimizer_registry_entry", installation, seq, kind, entry_id,
                   body, format_instant(at), prev])  # fmt: skip


def _log(conn: Conn, inst: str, as_of: datetime) -> tuple[im.ExperimentRegistry,
                                                         list[LogEntry]]:  # fmt: skip
    rows = _rows(conn, f"SELECT {', '.join(_COLS)} FROM app.optimizer_registry_entry "
                 "WHERE installation_id = %s AND recorded_at <= %s ORDER BY seq",
                 inst, as_of)  # fmt: skip
    out: list[LogEntry] = []
    try:
        for r in rows:
            last = out[-1] if out else None
            at = ensure_aware_utc(r["recorded_at"])
            e = _PARSE[r["kind"]](r["body"])
            kind, eid = _ident(e)
            h = entry_hash(inst, r["seq"], kind, eid, e.wire(), at, r["prev_hash"])
            if (
                r["seq"] != len(out) + 1
                or r["prev_hash"] != (last.event_hash if last else None)
                or (last is not None and at < last.recorded_at)
                or (kind, eid) != (r["kind"], r["entry_id"])
                or r["event_hash"] != h
            ):
                raise ValueError(f"entry {r['seq']}")
            out.append(LogEntry(r["seq"], kind, eid, e, at, h))
        reg = im.ExperimentRegistry(inst, tuple(x.entry for x in out))
    except (*_BAD, im.ImprovementError) as exc:
        raise OptimizerStoreError("integrity", type(exc).__name__) from None
    return reg, out


def load(conn: Conn, installation_id: str, as_of: datetime) -> im.ExperimentRegistry:
    """The installation's optimizer registry as of `as_of`, replayed and verified."""
    as_of = ensure_aware_utc(as_of)
    with conn.transaction():
        if as_of > _now(conn, f"optimizer:{installation_id}", shared=True):
            raise OptimizerStoreError("as_of", "not a past instant")
        return _log(conn, installation_id, as_of)[0]


def append(conn: Conn, installation_id: str, e: im.Entry,
           evidence: ImprovementEvidence | None = None,
           ) -> tuple[LogEntry, bool]:  # fmt: skip
    """Append an optimizer protocol, candidate or verdict; (the entry, wrote)."""
    kind, eid = _ident(e)
    if type(e) is im.Protocol:
        if (e.scope, e.scope_id, e.target) != (im.Scope.INSTALLATION,
                                               installation_id, oi.OPT):  # fmt: skip
            raise OptimizerStoreError("scope", "an optimizer protocol of this install")
        try:
            oi.check_protocol(e)
        except oi.OptimizerError as exc:
            raise OptimizerStoreError(exc.code, str(exc)) from None
    with conn.transaction():
        at = _now(conn, f"optimizer:{installation_id}")
        reg, log = _log(conn, installation_id, at)
        for x in log:  # by wire: a read-back scorecard keeps its thresholds sorted
            if (x.kind, x.entry_id, x.entry.wire()) == (kind, eid, e.wire()):
                return x, False
        try:
            if type(e) is im.Protocol:
                reg.register(e)
            elif type(e) is im.Candidate:
                reg.register_candidate(e, evidence)
            else:
                assert type(e) is im.Verdict
                reg.record_verdict(e)
        except im.ImprovementError as exc:
            raise OptimizerStoreError(exc.code, str(exc)) from None
        head = Head(log[-1].seq, log[-1].event_hash) if log else Head(0, None)
        seq, body = head.seq + 1, e.wire()
        h = entry_hash(installation_id, seq, kind, eid, body, at, head.event_hash)
        _insert(conn, "optimizer_registry_entry", {
            "installation_id": installation_id, "seq": seq, "kind": kind,
            "entry_id": eid, "body": Jsonb(body), "recorded_at": at,
            "prev_hash": head.event_hash, "event_hash": h})  # fmt: skip
    return LogEntry(seq, kind, eid, e, at, h), True


def cycle_hash(installation: str, experiment: str, ref: uuid.UUID, body: object,
               at: datetime) -> str:  # fmt: skip
    return digest(["optimizer_cycle", installation, experiment, str(ref), body,
                   format_instant(at)])  # fmt: skip


def append_cycle(conn: Conn, installation_id: str, experiment_id: str,
                 receipt: oi.CycleReceipt, protocol: im.Protocol,
                 ) -> StoredCycle:  # fmt: skip
    """Store a cycle receipt (qw_worker, blind) at the database clock, with the
    protocol it ran under (0051 refuses one that is not the logged protocol)."""
    if type(receipt) is not oi.CycleReceipt or type(protocol) is not im.Protocol:
        raise TypeError("a domain CycleReceipt and Protocol are required")
    w, ref = receipt.wire(), uuid.uuid4()
    with conn.transaction():
        at = _now(conn)
        _insert(conn, "optimizer_cycle", {
            "cycle_ref": ref, "installation_id": installation_id,
            "experiment_id": experiment_id, "cycle_id": receipt.cycle_id,
            "runner_hash": receipt.runner_hash, "authority": receipt.authority,
            "body": Jsonb(w), "receipt_hash": w["receipt_hash"], "recorded_at": at,
            "protocol": Jsonb(protocol.wire()),
            "event_hash": cycle_hash(installation_id, experiment_id, ref, w, at),
        })  # fmt: skip
    return StoredCycle(ref, receipt, at)


def load_cycles(conn: Conn, installation_id: str, experiment_id: str,
                ) -> tuple[StoredCycle, ...]:  # fmt: skip
    """The experiment's stored cycles in recording order, each re-verified."""
    rows = _rows(conn, "SELECT * FROM app.optimizer_cycle WHERE installation_id = %s "
                 "AND experiment_id = %s ORDER BY recorded_at, cycle_ref",
                 installation_id, experiment_id)  # fmt: skip
    out = []
    try:
        for r in rows:
            c, at = oi.CycleReceipt.from_wire(r["body"]), ensure_aware_utc(
                r["recorded_at"])  # fmt: skip
            # the columns are the body's (0047 CHECK); the hash binds the rest
            if r["event_hash"] != cycle_hash(installation_id, experiment_id,
                                             r["cycle_ref"], c.wire(), at):  # fmt: skip
                raise ValueError(r["cycle_id"])
            out.append(StoredCycle(r["cycle_ref"], c, at))
    except _BAD as exc:
        raise OptimizerStoreError("integrity", type(exc).__name__) from None
    return tuple(out)


def lineage_hash(installation: str, body: object, at: datetime) -> str:
    return digest(["optimizer_lineage", installation, body, format_instant(at)])


def append_lineage(conn: Conn, installation_id: str, receipt: oi.LineageReceipt,
                   ) -> datetime:  # fmt: skip
    """Store a verified lineage receipt (qw_assessor, blind) at the DB clock."""
    if type(receipt) is not oi.LineageReceipt:
        raise TypeError("a verified LineageReceipt is required")
    w = receipt.wire()
    with conn.transaction():
        at = _now(conn)
        _insert(conn, "optimizer_lineage", {
            "installation_id": installation_id,
            "lineage_id": receipt.claim.lineage_id,
            "experiment_id": receipt.claim.experiment_id, "body": Jsonb(w),
            "lineage_hash": w["lineage_hash"], "recorded_at": at,
            "event_hash": lineage_hash(installation_id, w, at)})  # fmt: skip
    return at


def load_lineage(conn: Conn, installation_id: str, lineage_id: str,
                 ) -> oi.UnverifiedLineage | None:  # fmt: skip
    """The stored lineage, re-hashed and re-parsed: unverified until the caller's
    `verify_lineage` re-derives it. None when no lineage has the id."""
    rows = _rows(conn, "SELECT * FROM app.optimizer_lineage WHERE installation_id = "
                 "%s AND lineage_id = %s", installation_id, lineage_id)  # fmt: skip
    if not rows:
        return None
    r = rows[0]
    try:
        u = oi.UnverifiedLineage.from_wire(r["body"])
        at = ensure_aware_utc(r["recorded_at"])
        if r["event_hash"] != lineage_hash(installation_id, u.wire(), at):
            raise ValueError(lineage_id)  # the columns are the body's (0047 CHECK)
    except _BAD as exc:
        raise OptimizerStoreError("integrity", type(exc).__name__) from None
    return u


@dataclass(frozen=True, slots=True)
class Reservation:  # decision 42's write-ahead record of the plan's one read
    experiment_id: str
    candidate_id: str
    verdict_id: str
    fixture_hash: str  # the read: the plan's promotion fixture
    inputs: str  # the digest of every input the trial and verdict depend on


def run_hash(installation: str, plan_id: str, attempt: int, r: Reservation,
             at: datetime) -> str:  # fmt: skip
    return digest(["optimizer_run", installation, plan_id, attempt, r.experiment_id,
                   r.candidate_id, r.verdict_id, r.fixture_hash, r.inputs,
                   format_instant(at)])  # fmt: skip


def outcome_hash(installation: str, plan_id: str, attempt: int, d: str,
                 at: datetime) -> str:  # fmt: skip
    return digest(["optimizer_run_outcome", installation, plan_id, attempt, d,
                   format_instant(at)])  # fmt: skip


@dataclass(frozen=True, slots=True)
class RunLog:
    """The installation's durable optimizer run log (qw_assessor); each call is
    one transaction, and each write re-checks under the installation lock."""

    conn: Conn
    installation_id: str

    def attempts(self, plan_id: str) -> tuple[Reservation, ...]:
        rows = _rows(self.conn, "SELECT * FROM app.optimizer_run WHERE "
                     "installation_id = %s AND plan_id = %s ORDER BY attempt",
                     self.installation_id, plan_id)  # fmt: skip
        out = []
        try:
            for n, r in enumerate(rows, 1):  # attempts 1..n, each hash-bound
                x = Reservation(r["experiment_id"], r["candidate_id"],
                                r["verdict_id"], r["fixture_hash"],
                                r["inputs_digest"])  # fmt: skip
                at = ensure_aware_utc(r["recorded_at"])
                if r["attempt"] != n or r["event_hash"] != run_hash(
                    self.installation_id, plan_id, n, x, at
                ):  # fmt: skip
                    raise ValueError(f"attempt {r['attempt']}")
                out.append(x)
        except _BAD as exc:
            raise OptimizerStoreError("integrity", type(exc).__name__) from None
        return tuple(out)

    def reserve(self, plan_id: str, attempt: int, r: Reservation) -> None:
        """The guard (0049) refuses all but the next attempt repeating the first;
        the log it extends is verified first, under the same lock."""
        inst = self.installation_id
        with self.conn.transaction():
            at = _now(self.conn, f"optimizer:{inst}")
            self.attempts(plan_id)
            _insert(self.conn, "optimizer_run", {
                "installation_id": inst, "plan_id": plan_id, "attempt": attempt,
                "experiment_id": r.experiment_id, "candidate_id": r.candidate_id,
                "verdict_id": r.verdict_id, "fixture_hash": r.fixture_hash,
                "inputs_digest": r.inputs, "recorded_at": at,
                "event_hash": run_hash(inst, plan_id, attempt, r, at)})  # fmt: skip

    def outcome(self, plan_id: str) -> str | None:
        rows = _rows(self.conn, "SELECT * FROM app.optimizer_run_outcome WHERE "
                     "installation_id = %s AND plan_id = %s",
                     self.installation_id, plan_id)  # fmt: skip
        if not rows:
            return None
        r = rows[0]
        try:
            at = ensure_aware_utc(r["recorded_at"])
            if r["event_hash"] != outcome_hash(self.installation_id, plan_id,
                                               r["attempt"], r["outcome_digest"],
                                               at):  # fmt: skip
                raise ValueError(plan_id)
        except _BAD as exc:
            raise OptimizerStoreError("integrity", type(exc).__name__) from None
        return str(r["outcome_digest"])

    def put_outcome(self, plan_id: str, attempt: int, digest_: str) -> None:
        inst = self.installation_id
        with self.conn.transaction():
            at = _now(self.conn, f"optimizer:{inst}")
            got = self.outcome(plan_id)
            if got is not None:
                if got != digest_:
                    raise OptimizerStoreError("verdict_conflict", plan_id)
                return
            _insert(self.conn, "optimizer_run_outcome", {
                "installation_id": inst, "plan_id": plan_id, "attempt": attempt,
                "outcome_digest": digest_, "recorded_at": at,
                "event_hash": outcome_hash(inst, plan_id, attempt, digest_, at),
            })  # fmt: skip
