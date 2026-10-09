"""PostgreSQL proposal and commitment store with atomic publication (T026 increment
2; `0010_proposals.sql`; spec §3 "Atomic final decision", §7; T008 F-09, F-10).

`ProposalPublisher` works in the caller's `TenantTx`, so a decision, its events, its
commitment changes and its outbox rows commit together or not at all. Every
transition goes through it: it loads the proposal and the scope's live commitments,
runs the domain function (`step`, `revise`, `publish_committed`, `plan_committed`,
`dismiss`, `expire`) and writes the difference, so the commitment rows always follow
`qw_domain.commitments.sync`.
- Locks: the accounts named by the version and its binding, sorted (the journal's
  per-account key, so postings wait), then the policy (the policy store's key). A
  book (account, currency) and a sleeve belong to one account, and every writer of
  them holds that account's lock, so the account lock covers the currency and
  sleeve classes of the T008 order (account -> currency -> sleeve).
- Under the locks the store re-reads the account journal revision and the adopted
  policy and takes the decision time from the server clock (never earlier than the
  proposal's last event). Before each decision it records the expiry of other
  proposals in the book whose expired version still holds a commitment.
- Risk inputs and pauses are built by the caller (`Snapshot`) until feed and pause
  persistence exist; the book's qualified cash is the snapshot's available cash.
- A refused decision is a typed 409-equivalent `Result`; any expiry or invalidation
  the domain recorded while refusing is still committed with its outbox rows.
- Reads recompute each version's content hash and canonical content.
LIMITATIONS: `expire_due` sweeps one tenant (a cross-tenant sweep needs a definer
function); alternatives groups stay in one book, as in the domain.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb
from qw_domain import proposals as dp
from qw_domain.commitments import (
    Commitment,
    CommitmentBook,
    plan_committed,
    publish_committed,
    sync,
)
from qw_domain.decimals import Money, PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
from qw_domain.risk import Outcome, ProposedAction, RiskInputs, Side
from qw_domain.risk_pauses import PauseBook

from qw_adapters import journal_store, policy_store
from qw_adapters.jobs import write_outbox
from qw_adapters.tenancy import TenantTx

EVENT_TYPE = "proposal.transition"
LIVE = [s.value for s in dp.ProposalState if s not in dp.TERMINAL]
type _Out = tuple[dp.Decision, CommitmentBook]
type _Op = Callable[
    [dp.Proposal, dp.Current, CommitmentBook, dict[str, dp.Proposal]], _Out
]


class ProposalStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class Choice(StrEnum):
    PLAN = "plan"
    DISMISS = "dismiss"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Caller-built decision inputs: risk inputs and pauses."""

    inputs: RiskInputs
    pauses: PauseBook


@dataclass(frozen=True, slots=True)
class Result:
    accepted: bool
    proposal: dp.Proposal
    reasons: tuple[dp.Reason, ...] = ()

    @property
    def status(self) -> int:
        """HTTP equivalent: 200, or 409 for a refused decision (F-10, C-04)."""
        return 200 if self.accepted else 409


@dataclass(frozen=True, slots=True)
class _Scope:
    account: str
    currency: str
    policy_id: uuid.UUID
    accounts: tuple[str, ...]  # locked; their revisions are re-read


def _version(j: Any, digest: str) -> dp.ProposalVersion:
    """Rebuild a stored version; refuse it unless it reproduces its content."""
    try:
        a, b = dict(j["action"]), j["binding"]
        a.update(
            instrument=InstrumentId.from_wire(a["instrument"]["uuid"]),
            side=Side(a["side"]), quantity=PositiveQuantity.from_wire(a["quantity"]),
            lot=PositiveQuantity.from_wire(a["lot"]),
            stop=None if a["stop"] is None else Price.from_wire(a["stop"]),
            fixed_cost=Money.from_wire(a["fixed_cost"]),
            unit_cost=Money.from_wire(a["unit_cost"]),
            gap_exits=tuple(Price.from_wire(x) for x in a["gap_exits"]),
        )  # fmt: skip
        observations = tuple(
            dp.Observation(o["key"], parse_instant(o["observed_at"]),
                           timedelta(microseconds=o["max_age"]))
            for o in b["observations"]
        )  # fmt: skip
        binding = dp.Binding(
            b["policy_hash"], tuple((k, r) for k, r in b["account_revisions"]),
            observations, b["risk_digest"], Outcome(b["risk_outcome"]),
        )  # fmt: skip
        times = ("trigger_at", "received_at", "created_at", "expires_at")
        v = dp.ProposalVersion(
            j["proposal_id"], j["version"], ProposedAction(**a), binding,
            Money.from_wire(j["requirement"]),
            **{t: parse_instant(j[t]) for t in times}, mode=dp.Mode(j["mode"]),
            alternatives_group_id=j["alternatives_group_id"],
        )  # fmt: skip
    except (KeyError, TypeError, ValueError) as exc:
        raise ProposalStoreError("integrity", f"stored version: {exc}") from None
    if v.content_hash != digest or v.canonical != j:
        raise ProposalStoreError("integrity", "stored version does not reproduce")
    return v


class ProposalPublisher:
    def __init__(self, tx: TenantTx) -> None:
        self.tx = tx

    def _q(self, sql: str, *args: object) -> list[tuple[Any, ...]]:
        """Run `sql` with the tenant id as its first parameter."""
        cur = self.tx.conn.execute(sql, (self.tx.tenant_id, *args))
        return cur.fetchall() if cur.description else []

    def _lock(self, key: str) -> None:
        self.tx.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
        )

    def _now(self, *seen: dp.Proposal) -> datetime:
        row = self.tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        return max([ensure_aware_utc(row[0]), *(p.events[-1].at for p in seen)])

    def load(self, pid: str) -> dp.Proposal | None:
        versions = tuple(
            _version(content, digest)
            for content, digest in self._q(
                "SELECT content, content_hash FROM app.proposal_version WHERE "
                "tenant_id = %s AND proposal_id = %s ORDER BY version",
                pid,
            )
        )
        if not versions:
            return None
        if [v.version for v in versions] != list(range(1, len(versions) + 1)):
            raise ProposalStoreError("integrity", "version sequence")
        events = tuple(
            dp.Event(n, dp.ProposalState(st), dp.ExecutionState(ex),
                     ensure_aware_utc(at), None if code is None else dp.Code(code),
                     detail)
            for n, st, ex, at, code, detail in self._q(
                "SELECT version, state::text, execution::text, at, code, detail FROM "
                "app.proposal_event WHERE tenant_id = %s AND proposal_id = %s "
                "ORDER BY seq", pid,
            )
        )  # fmt: skip
        return dp.Proposal(versions, events)

    def _scope(
        self, pid: str, *new: dp.ProposalVersion, policy: uuid.UUID | None = None
    ) -> _Scope:
        """Lock for `pid`'s latest version and any `new` versions: the sorted
        accounts (covering their books and sleeves), then the policy. A given
        `policy` means a new proposal. The latest version is read before locking
        and confirmed after."""
        found = self._q(
            "SELECT version, policy_id, content, content_hash FROM "
            "app.proposal_version WHERE tenant_id = %s AND proposal_id = %s "
            "ORDER BY version DESC LIMIT 1",
            pid,
        )
        if found and policy is not None:
            raise ProposalStoreError("exists", pid)
        if not found and policy is None:
            raise ProposalStoreError("not_found", pid)
        vs = [_version(c, h) for _, _, c, h in found] + list(new)
        top = found[0][0] if found else None
        policy = found[0][1] if found else policy
        assert policy is not None
        a = vs[0].action
        named = {k for v in vs for k, _ in v.binding.account_revisions}
        accounts = tuple(sorted(named | {a.account_id}))
        for account in accounts:
            journal_store.lock_account(self.tx, account)
        self._lock(f"policy/{self.tx.tenant_id}/{policy}")  # policy_store's key
        [(latest,)] = self._q(
            "SELECT max(version) FROM app.proposal_version WHERE tenant_id = %s "
            "AND proposal_id = %s",
            pid,
        )
        if latest != top:
            raise ProposalStoreError("retry" if found else "exists", pid)
        return _Scope(a.account_id, a.currency, policy, accounts)

    def _current(self, s: _Scope, snap: Snapshot, at: datetime) -> dp.Current:
        revs = {a: journal_store.revision(self.tx, a) for a in s.accounts}
        history = policy_store.load(self.tx, s.policy_id)
        return dp.Current(at, history, snap.inputs, revs, snap.pauses)

    def _book(self, account: str, currency: str, cash: Money | None) -> CommitmentBook:
        """The live commitments of one book. `cash` is its qualified available
        cash (zero when unknown: admission then refuses `CASH_UNCONFIRMED`)."""
        held = tuple(
            Commitment(pid, n, group, Money.of(reserved, currency),
                       _version(content, digest).action,
                       None if mark is None else Price.from_wire(mark),
                       state == "planned")
            for pid, n, group, reserved, mark, state, content, digest in self._q(
                "SELECT c.proposal_id, c.version, v.group_id, c.reserved_wire, "
                "c.mark_wire, c.state::text, v.content, v.content_hash FROM "
                "app.proposal_commitment c JOIN app.proposal_version v USING "
                "(tenant_id, proposal_id, version) WHERE c.tenant_id = %s AND "
                "v.account_id = %s AND v.currency = %s AND c.state <> 'released' "
                "ORDER BY 1, 2", account, currency,
            )
        )  # fmt: skip
        if cash is None or cash.currency != currency:
            cash = Money.of(0, currency)
        tenant = str(self.tx.tenant_id)
        return CommitmentBook(tenant, account, currency, cash, held)

    def _save(
        self, old: dp.Proposal | None, new: dp.Proposal, policy: uuid.UUID | None
    ) -> None:
        """Append the new versions and events, each event with an outbox row."""
        nv, ne = (0, 0) if old is None else (len(old.versions), len(old.events))
        if old is not None and new.events[:ne] != old.events:
            raise ProposalStoreError("integrity", "history is append-only")
        for v in new.versions[nv:]:
            a = v.action
            self._q(
                "INSERT INTO app.proposal_version (tenant_id, proposal_id, version, "
                "account_id, currency, sleeve_id, group_id, policy_id, expires_at, "
                "content_hash, content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s)",
                v.proposal_id, v.version, a.account_id, a.currency, a.sleeve_id,
                v.alternatives_group_id, policy, v.expires_at, v.content_hash,
                Jsonb(v.canonical),
            )  # fmt: skip
        pid = new.latest.proposal_id
        for seq, e in enumerate(new.events[ne:], start=ne + 1):
            code = None if e.code is None else e.code.value
            self._q(
                "INSERT INTO app.proposal_event (tenant_id, proposal_id, seq, version, "
                "state, execution, at, code, detail) VALUES (%s, %s, %s, %s, %s, %s, "
                "%s, %s, %s)",
                pid, seq, e.version, e.state.value, e.execution.value, e.at, code,
                e.detail,
            )  # fmt: skip
            write_outbox(self.tx, EVENT_TYPE, {
                "proposal_id": pid, "version": e.version, "state": e.state.value,
                "execution": e.execution.value, "code": code,
                "at": format_instant(e.at),
            })  # fmt: skip

    def _sync(self, old: CommitmentBook, new: CommitmentBook) -> None:
        """Write the difference between two books; releases first (one live row
        per proposal)."""
        was = {(c.proposal_id, c.version): c for c in old.commitments}
        now = {(c.proposal_id, c.version): c for c in new.commitments}
        update = (
            "UPDATE app.proposal_commitment SET state = %s WHERE tenant_id = %s AND "
            "proposal_id = %s AND version = %s"
        )
        for key in sorted(was.keys() - now.keys()):
            self.tx.conn.execute(update, ("released", self.tx.tenant_id, *key))
        for key in sorted(now.keys() - was.keys()):
            c = now[key]
            self._q(
                "INSERT INTO app.proposal_commitment (tenant_id, proposal_id, version, "
                "reserved_wire, mark_wire) VALUES (%s, %s, %s, %s, %s)",
                *key, c.amount.amount.to_wire(),
                None if c.price is None else c.price.to_wire(),
            )  # fmt: skip
        for key in sorted(now):
            if now[key].planned and not (key in was and was[key].planned):
                self.tx.conn.execute(update, ("planned", self.tx.tenant_id, *key))

    def _expire(
        self, account: str, currency: str, pids: list[str], at: datetime
    ) -> list[str]:
        """Record due expiries (`qw_domain.proposals.expire`) and release their
        commitments; its account is locked. Returns the ids that expired."""
        book, done = self._book(account, currency, None), []
        for pid in pids:
            p = self.load(pid)
            assert p is not None
            q = dp.expire(p, max(at, p.events[-1].at))
            if q is not p:
                self._save(p, q, None)
                done.append(q)
        self._sync(book, sync(book, *done))
        return [q.latest.proposal_id for q in done]

    def _siblings(self, p: dp.Proposal) -> dict[str, dp.Proposal]:
        group, found = p.latest.alternatives_group_id, dict[str, dp.Proposal]()
        for (key,) in self._q(
            "SELECT DISTINCT proposal_id FROM app.proposal_version WHERE tenant_id = "
            "%s AND group_id = %s AND proposal_id <> %s ORDER BY 1",
            group, p.latest.proposal_id,
        ) if group is not None else ():  # fmt: skip
            q = self.load(key)
            if q is not None and q.latest.alternatives_group_id == group:
                found[key] = q
        return found

    def _apply(
        self, pid: str, snap: Snapshot, op: _Op, *new: dp.ProposalVersion
    ) -> Result:
        """One decision: locks, the expiry of other stale commitments in the book,
        the domain `op` on current records, then every write in this transaction."""
        s = self._scope(pid, *new)
        at = self._now()
        stale = self._q(
            "SELECT DISTINCT c.proposal_id FROM app.proposal_commitment c JOIN "
            "app.proposal_version v USING (tenant_id, proposal_id, version) WHERE "
            "c.tenant_id = %s AND v.account_id = %s AND v.currency = %s AND "
            "c.state <> 'released' AND v.expires_at <= %s AND c.proposal_id <> %s "
            "ORDER BY 1", s.account, s.currency, at, pid,
        )  # fmt: skip
        if stale:
            self._expire(s.account, s.currency, [r[0] for r in stale], at)
        p = self.load(pid)
        assert p is not None
        book = self._book(s.account, s.currency, snap.inputs.available_cash)
        siblings = self._siblings(p)
        cur = self._current(s, snap, self._now(p, *siblings.values()))
        d, after = op(p, cur, book, siblings)
        self._save(p, d.proposal, s.policy_id)
        for key, sib in d.siblings.items():
            self._save(siblings[key], sib, None)
        self._sync(book, after)
        return Result(d.accepted, d.proposal, d.reasons)

    def submit(
        self, v: dp.ProposalVersion, policy_id: uuid.UUID, snap: Snapshot
    ) -> dp.Proposal:
        """Store version 1 as a candidate; its mode must be the adopted policy's."""
        if v.action.tenant_id != str(self.tx.tenant_id):
            raise ProposalStoreError("tenant", "the version names another tenant")
        s = self._scope(v.proposal_id, v, policy=policy_id)
        p = dp.Proposal.new(v, self._current(s, snap, self._now()))
        self._save(None, p, policy_id)
        return p

    def step(self, pid: str, dst: dp.ProposalState, snap: Snapshot) -> Result:
        def op(p: dp.Proposal, c: dp.Current, b: CommitmentBook, _: Any) -> _Out:
            d = dp.step(p, dst, c)
            return d, sync(b, d.proposal)

        return self._apply(pid, snap, op)

    def revise(self, pid: str, v: dp.ProposalVersion, snap: Snapshot) -> Result:
        def op(p: dp.Proposal, c: dp.Current, b: CommitmentBook, _: Any) -> _Out:
            d = dp.revise(p, v, c)
            return d, sync(b, d.proposal)

        return self._apply(pid, snap, op, v)

    def publish(self, pid: str, n: int, snap: Snapshot) -> Result:
        """Final check and admission against the locked book (F-09, F-10)."""

        def op(p: dp.Proposal, c: dp.Current, b: CommitmentBook, _: Any) -> _Out:
            d, after, _ev = publish_committed(p, n, c, b)
            return d, after

        return self._apply(pid, snap, op)

    def decide(self, pid: str, n: int, choice: Choice, snap: Snapshot) -> Result:
        """The user's plan (closing its alternatives) or a dismissal."""

        def op(
            p: dp.Proposal, c: dp.Current, b: CommitmentBook,
            siblings: dict[str, dp.Proposal],
        ) -> _Out:  # fmt: skip
            if choice is Choice.PLAN:
                return plan_committed(p, n, c, b, siblings)
            d = dp.dismiss(p, n, c)
            return d, sync(b, d.proposal)

        return self._apply(pid, snap, op)

    def expire_due(self, limit: int = 100) -> list[str]:
        """Record the expiry of live proposals past `expires_at` (server clock) and
        release their commitments; returns the proposal ids, sorted."""
        due = self._q(
            "SELECT e.proposal_id, v.account_id, v.currency FROM app.proposal_event e "
            "JOIN app.proposal_version v USING (tenant_id, proposal_id, version) "
            "WHERE e.tenant_id = %s AND v.expires_at <= clock_timestamp() AND "
            "e.state::text = ANY(%s) AND e.seq = (SELECT max(seq) FROM "
            "app.proposal_event x WHERE x.tenant_id = e.tenant_id AND "
            "x.proposal_id = e.proposal_id) ORDER BY 1 LIMIT %s",
            LIVE, limit,
        )  # fmt: skip
        books: dict[tuple[str, str], list[str]] = {}
        for pid, account, currency in due:
            books.setdefault((account, currency), []).append(pid)
        for account in sorted({a for a, _ in books}):
            journal_store.lock_account(self.tx, account)
        at, out = self._now(), list[str]()
        for (account, currency), pids in sorted(books.items()):
            out += self._expire(account, currency, pids, at)
        return sorted(out)
