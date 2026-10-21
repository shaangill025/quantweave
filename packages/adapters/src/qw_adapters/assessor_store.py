"""The T044 assessor store (`0028_assessor_ledger.sql`), used only by
`experiment_store.record` in its assessor phase: a qw_assessor connection (owner
decision 17, the assessor-role migration; qw_worker has no privilege here) under
the `experiment:<tenant>` lock; no route reads it. `bind` stores the protocol and
lineage once, then requires the caller's to hash the same (`protocol_frozen`,
`lineage`) and rebuilds the registry from the stored rows; a new protocol whose
plan's promotion window overlaps a window already scored for another plan of the
same tenant and target (any baseline) is refused (`holdout_window_used`, decision
18; `admit` checks it again before scoring), and its `retest_of` must name a
stored experiment of the tenant (`retest_of_unknown`, decision 19). `store_verdict`
accepts the identical receipt of an earlier attempt (a retry whose revision was
not written), refuses another under the same verdict id (`verdict_conflict`),
else runs `admit` and `put_verdict`. `admit` (owner decision Q7) refuses
re-assessment once a receipt scored the holdout read (`holdout_used`) and a ledger
that does not extend the last stored checkpoint or begin with the plan's stored
reads (`ledger_diverged`). `put_verdict` appends new holdout reads, then the
receipt naming the read it scored, with its family's and its retest chain's trial
counts. Reads re-hash every row and rebuild protocols, candidates and verdicts
through the domain (stored reads are compared with the ledger's `Access`);
failures are `integrity`. LIMITATIONS: the ledger itself and the budget book are
the caller's; a raw qw_assessor INSERT that meets the database rules is not
checked for provenance."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
from qw_domain.research_data import ResearchError, Window, digest

from qw_adapters.tenancy import TenantTx

Row = dict[str, Any]


class ExperimentStoreError(Exception):  # re-exported by experiment_store
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _hash(kind: str, tx: TenantTx, body: object, at: datetime) -> str:
    return digest([kind, str(tx.tenant_id), body, format_instant(at)])


def _select(tx: TenantTx, sql: str, *args: object) -> list[Row]:
    try:
        cur = tx.conn.execute(sql, (tx.tenant_id, *args))
        names = [c.name for c in cur.description or ()]
        return [dict(zip(names, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise ExperimentStoreError("database", type(exc).__name__) from None


def _verified[T](rows: list[Row], kind: str, tx: TenantTx,
                 build: Callable[[Row], tuple[T, object]]) -> list[T]:  # fmt: skip
    """Each row rebuilt through the domain and re-hashed; else `integrity`."""
    out = []
    try:
        for row in rows:
            obj, body = build(row)
            at = ensure_aware_utc(row["recorded_at"])
            if row["event_hash"] != _hash(kind, tx, body, at):
                raise ValueError(kind)
            out.append(obj)
    except (ValueError, TypeError, KeyError, ResearchError) as exc:
        raise ExperimentStoreError("integrity", type(exc).__name__) from None
    return out


def _insert(tx: TenantTx, table: str, values: Row, body: object) -> None:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    at = ensure_aware_utc(row[0])
    values |= {"tenant_id": tx.tenant_id, "recorded_at": at,
               "event_hash": _hash(table, tx, body, at)}  # fmt: skip
    cols = ", ".join(values)  # this module's literal names
    try:
        with tx.conn.transaction():
            tx.conn.execute(f"INSERT INTO app.{table} ({cols}) VALUES "
                            f"({', '.join(['%s'] * len(values))})",
                            tuple(values.values()))  # fmt: skip
    except psycopg.errors.UniqueViolation as exc:
        one = exc.diag.constraint_name == "evaluation_receipt_one_scored_read"
        code = "holdout_used" if one else "database"
        raise ExperimentStoreError(code, type(exc).__name__) from None
    except psycopg.Error as exc:
        raise ExperimentStoreError("database", type(exc).__name__) from None


def _protocol(tx: TenantTx, eid: str) -> im.Protocol | None:
    def build(r: Row) -> tuple[im.Protocol, object]:
        p = im.Protocol.from_wire(r["body"])
        a = p.assessor
        if (r["protocol_hash"], r["plan_id"], r["baseline_version"],
                r["assessor_actor_id"], r["assessor_route_id"],
                r["assessor_family_id"], r["assessor_version"],
                r.get("retest_of")) != (
                p.content_hash, p.plan_id, p.baseline_version, a.actor_id,
                a.route_id, a.family_id, a.version, p.retest_of):  # fmt: skip
            raise ValueError("protocol columns")
        return p, p.wire()

    rows = _select(tx, "SELECT * FROM app.assessor_protocol WHERE tenant_id = %s "
                   "AND experiment_id = %s", eid)  # fmt: skip
    got = _verified(rows, "assessor_protocol", tx, build)
    return got[0] if got else None


def _lineage(tx: TenantTx, eid: str) -> list[im.Candidate]:
    def build(r: Row) -> tuple[im.Candidate, object]:
        c = im.Candidate.from_wire(r["body"])
        if (r["content_hash"], r["parent_id"]) != (c.content_hash, c.parent_id):
            raise ValueError("candidate columns")
        return c, [r["depth"], c.wire()]

    rows = _select(tx, "SELECT * FROM app.assessor_candidate WHERE tenant_id = %s "
                   "AND experiment_id = %s ORDER BY depth", eid)  # fmt: skip
    if [r["depth"] for r in rows] != list(range(len(rows))):
        raise ExperimentStoreError("integrity", "lineage depth")
    return _verified(rows, "assessor_candidate", tx, build)


def _window_free(tx: TenantTx, p: im.Protocol, window: Window) -> None:
    """Decision 18: `window` shares no instant with a window scored for another
    plan of this tenant and target, any baseline (the scored reads' windows)."""
    rows = _select(tx, "SELECT h.body FROM app.evaluation_receipt r JOIN "
                   "app.holdout_access h ON (h.tenant_id, h.plan_id, h.seq) = "
                   "(r.tenant_id, r.plan_id, r.scored_seq) JOIN app.assessor_protocol "
                   "u ON (u.tenant_id, u.experiment_id) = (r.tenant_id, "
                   "r.experiment_id) WHERE r.tenant_id = %s AND r.plan_id <> %s AND "
                   "u.body ->> 'target' = %s", p.plan_id, p.target.value)  # fmt: skip
    try:
        used = [Window(parse_instant(r["body"][5][0]), parse_instant(r["body"][5][1]))
                for r in rows]  # fmt: skip
    except (ValueError, TypeError, KeyError, IndexError, ResearchError) as exc:
        raise ExperimentStoreError("integrity", type(exc).__name__) from None
    if any(u.start < window.end and window.start < u.end for u in used):
        raise ExperimentStoreError("holdout_window_used", p.plan_id)


def bind(tx: TenantTx, registry: im.ExperimentRegistry, eid: str, cid: str,
         plan: tl.SplitPlan) -> im.ExperimentRegistry:  # fmt: skip
    """The registry rebuilt from the stored protocol and lineage of `cid`."""
    p, c = registry.protocol(eid), registry.candidate(cid)
    if c.experiment_id != eid:
        raise im.ImprovementError("candidate_experiment", cid)
    stored = _protocol(tx, eid)
    if stored is None:
        if (plan.plan_id, im.plan_digest(plan)) != (p.plan_id, p.plan_digest):
            raise im.ImprovementError("test_access_changed", p.plan_id)
        _window_free(tx, p, plan.promotion)
        if p.retest_of is not None and _protocol(tx, p.retest_of) is None:
            raise ExperimentStoreError("retest_of_unknown", p.retest_of)
        a = p.assessor
        _insert(tx, "assessor_protocol", {
            "experiment_id": eid, "protocol_hash": p.content_hash,
            "plan_id": p.plan_id, "baseline_version": p.baseline_version,
            "assessor_actor_id": a.actor_id, "assessor_route_id": a.route_id,
            "assessor_family_id": a.family_id, "assessor_version": a.version,
            "body": Jsonb(p.wire()), "retest_of": p.retest_of,
        }, p.wire())  # fmt: skip
        stored = p
    elif stored.content_hash != p.content_hash:
        raise ExperimentStoreError("protocol_frozen", eid)
    have = _lineage(tx, eid)
    want = [registry.candidate(x) for x in reversed(registry.lineage(cid)[:-1])]
    for depth, x in enumerate(want):
        if depth < len(have):
            if have[depth].wire() != x.wire():
                raise ExperimentStoreError("lineage", x.candidate_id)
            continue
        _insert(tx, "assessor_candidate", {
            "experiment_id": eid, "candidate_id": x.candidate_id, "depth": depth,
            "parent_id": x.parent_id, "content_hash": x.content_hash,
            "body": Jsonb(x.wire()),
        }, [depth, x.wire()])  # fmt: skip
        have.append(x)
    try:
        return im.ExperimentRegistry(str(tx.tenant_id), (stored, *have))
    except (im.ImprovementError, TypeError) as exc:
        raise ExperimentStoreError("integrity", type(exc).__name__) from None


def _reads(tx: TenantTx, plan_id: str) -> list[object]:
    def build(r: Row) -> tuple[object, object]:
        body = r["body"]
        if (r["plan_id"], r["seq"], r["access_id"]) != (body[1], body[12], body[0]):
            raise ValueError("access columns")
        return body, body

    rows = _select(tx, "SELECT * FROM app.holdout_access WHERE tenant_id = %s "
                   "AND plan_id = %s ORDER BY seq", plan_id)  # fmt: skip
    if [r["seq"] for r in rows] != list(range(1, len(rows) + 1)):
        raise ExperimentStoreError("integrity", "holdout seq")
    return _verified(rows, "holdout_access", tx, build)


type Receipt = tuple[im.Verdict, int | None, int | None, str | None]


def receipts(tx: TenantTx, eid: str) -> list[Receipt]:
    """The experiment's receipts, oldest first: (verdict, scored seq, ledger
    entries, ledger digest)."""

    def build(r: Row) -> tuple[Receipt, object]:
        party = im.Party(r["assessor_actor_id"], r["assessor_route_id"],
                         r["assessor_family_id"], r["assessor_version"])  # fmt: skip
        v = im.Verdict(r["verdict_id"], r["experiment_id"], r["candidate_id"],
                       r["candidate_hash"], r["protocol_hash"], party,
                       im.Decision(r["decision"]), tuple(r["reasons"]),
                       {k: Decimal(x) for k, x in r["scores"].items()},
                       tuple(r["trial_ids"]), tuple(r["failed_trial_ids"]),
                       tuple(r["limitations"]),
                       ensure_aware_utc(r["assessed_at"]))  # fmt: skip
        extra = (r["scored_seq"], r["ledger_entries"], r["ledger_digest"])
        counted = (r["family_trials"], r["chain_trials"])  # None on 0028 rows
        tail = [] if counted == (None, None) else list(counted)
        return (v, *extra), [v.wire(), r["plan_id"], *extra, *tail]

    rows = _select(tx, "SELECT * FROM app.evaluation_receipt WHERE tenant_id = %s "
                   "AND experiment_id = %s ORDER BY recorded_at, verdict_id",
                   eid)  # fmt: skip
    return _verified(rows, "evaluation_receipt", tx, build)


def _frozen_plan(p: im.Protocol, ledger: tl.TrialLedger) -> bool:
    try:
        plan = ledger.plan(p.plan_id)
    except ResearchError:
        return False
    return ledger.tenant_id == p.scope_id and im.plan_digest(plan) == p.plan_digest


def _holdout(p: im.Protocol, ledger: tl.TrialLedger) -> list[tl.Access]:
    return [a for a in ledger.accesses() if a.plan_id == p.plan_id and a.granted
            and a.split is tl.Split.PROMOTION]  # fmt: skip


def admit(tx: TenantTx, p: im.Protocol, ledger: tl.TrialLedger) -> None:
    """Refuse an assessment that would re-use the holdout or rewrite the ledger."""
    done = receipts(tx, p.experiment_id)
    if any(seq is not None for _, seq, _, _ in done):
        raise ExperimentStoreError("holdout_used", p.experiment_id)
    if not _frozen_plan(p, ledger):
        return
    _window_free(tx, p, ledger.plan(p.plan_id).promotion)
    marks = [(n, d) for _, _, n, d in done if n is not None and d is not None]
    if marks:
        n, d = marks[-1]
        if len(ledger.entries) < n or ledger.checkpoint(n) != d:
            raise ExperimentStoreError(
                "ledger_diverged", "not an extension of the stored ledger"
            )
    stored = _reads(tx, p.plan_id)
    mine = [a.to_wire() for a in _holdout(p, ledger)]
    if mine[: len(stored)] != stored:
        raise ExperimentStoreError(
            "ledger_diverged", "the plan's stored holdout reads differ"
        )


def _mark(p: im.Protocol, ledger: tl.TrialLedger) -> tuple[int | None, str | None]:
    """The ledger checkpoint a receipt records: (entries, digest), or none."""
    if not _frozen_plan(p, ledger):
        return None, None
    return len(ledger.entries), ledger.checkpoint(len(ledger.entries))


def store_verdict(tx: TenantTx, p: im.Protocol, v: im.Verdict,
                  ledger: tl.TrialLedger) -> None:  # fmt: skip
    """Store `v`'s receipt once: an identical stored one (same verdict and ledger
    checkpoint) is a retry and is kept as it is; a different one is refused."""
    for got, _, n, d in receipts(tx, p.experiment_id):
        if got.verdict_id == v.verdict_id:
            if got == v and (n, d) == _mark(p, ledger):
                return
            raise ExperimentStoreError("verdict_conflict", v.verdict_id)
    admit(tx, p, ledger)
    put_verdict(tx, p, v, ledger)


def _chain_trials(tx: TenantTx, p: im.Protocol, ledger: tl.TrialLedger) -> int:
    """Decision 19: the retest chain's trials (`improvement.chain_trials`) over the
    stored links, each experiment's largest stored count and the ledger."""
    chain, cur = [p.experiment_id], p
    while cur.retest_of is not None:
        nxt = _protocol(tx, cur.retest_of)
        if nxt is None or nxt.experiment_id in chain or len(chain) >= 10000:
            raise ExperimentStoreError("integrity", "retest chain")
        chain.append(nxt.experiment_id)
        cur = nxt
    rows = _select(tx, "SELECT experiment_id, max(family_trials) AS n FROM "
                   "app.evaluation_receipt WHERE tenant_id = %s AND experiment_id "
                   "= ANY (%s) AND family_trials IS NOT NULL GROUP BY experiment_id",
                   chain)  # fmt: skip
    return im.chain_trials(ledger, chain, {r["experiment_id"]: r["n"] for r in rows})


def put_verdict(tx: TenantTx, p: im.Protocol, v: im.Verdict,
                ledger: tl.TrialLedger) -> None:  # fmt: skip
    """Append the ledger's new holdout reads, then the receipt (after `admit`)."""
    seq = None
    n, d = _mark(p, ledger)
    if n is not None:
        reads, k = _holdout(p, ledger), len(_reads(tx, p.plan_id))
        for a in reads[k:]:
            body = a.to_wire()
            _insert(tx, "holdout_access", {
                "plan_id": a.plan_id, "seq": a.seq, "access_id": a.access_id,
                "trial_id": a.trial_id, "family": a.candidate.family,
                "actor_id": a.actor_id, "body": Jsonb(body),
            }, body)  # fmt: skip
        if v.trial_ids:
            t = next(x for x in ledger.trials() if x.trial_id == v.trial_ids[0])
            seq = next(a.seq for a in reads if a.access_id == t.access_id)
    who, w = v.assessor, v.wire()
    family = ledger.trial_count(p.experiment_id)
    chain = _chain_trials(tx, p, ledger)
    _insert(tx, "evaluation_receipt", {
        "experiment_id": v.experiment_id, "verdict_id": v.verdict_id,
        "candidate_id": v.candidate_id, "candidate_hash": v.candidate_hash,
        "protocol_hash": v.protocol_hash, "assessor_actor_id": who.actor_id,
        "assessor_route_id": who.route_id, "assessor_family_id": who.family_id,
        "assessor_version": who.version, "decision": v.decision.value,
        "reasons": list(v.reasons), "scores": Jsonb(w["scores"]),
        "trial_ids": list(v.trial_ids), "failed_trial_ids": list(v.failed_trial_ids),
        "limitations": list(v.limitations), "assessed_at": v.assessed_at,
        "plan_id": p.plan_id, "scored_seq": seq, "ledger_entries": n,
        "ledger_digest": d, "family_trials": family, "chain_trials": chain,
    }, [w, p.plan_id, seq, n, d, family, chain])  # fmt: skip
