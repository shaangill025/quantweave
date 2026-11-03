"""Apply a verified export to an empty destination tenant (T057 increments 6 and 7;
spec §2 Journey E; R006, R051, R052, R087, R096, R097).

`provision(conn, data, display_name, login, verifier, key, *, claim_installation)`
(owner decision 111) is the import-provisioning path: on a connection that is
exactly qw_app (`check_runtime_role`), after the archive verifies and plans for its
own scope, it creates the tenant with the archive's `scope_id` (an id that exists
is refused), a new owner with a local credential, and one `portability.provisioned`
audit event, as `tenancy.bootstrap_installation` does for a fresh id. With
`claim_installation` (decision 117: a self-hosted install; the caller states it,
nothing infers it from the edition) it also takes the one-time
`installation_bootstrap` marker in the same transaction, so a later `qw-bootstrap`
is refused, and it is refused itself after a bootstrap. It commits before any
`apply_archive`: a failed apply leaves the empty provisioned tenant, which the
operator applies into again (a second provision is refused). It is an operator
path like `qw-bootstrap`, not an HTTP route (its command is not built). No
identity of the source is imported.

`apply_archive(tx, data, principal, key)` runs in the caller's `tenant_transaction`
as the qw_app login (RLS; `policy_version` INSERT and 0060's import function are
granted to qw_app only):
1. `read_archive(data)`: the whole container and manifest re-verification, from
   the bytes, so a manifest or an earlier `ImportReport` is never trusted;
2. `portability_apply.plan`: the exact format, the destination tenant is the
   archive's scope, the record key (`check_key`) and every section applicable,
   all before any write;
3. the principal is this tenant's owner (`apply_principal`). This module does
   not authenticate it: the route must derive it from an authenticated session,
   and must also require a fresh step-up and record the session (deferred);
4. under a tenant import lock, every portable store of the tenant is empty
   (`EMPTY_TABLES`, `destination_not_empty`);
5. in one savepoint: each policy version through `policy_store.propose` (its lock,
   replay and the 0009 guard), created by `principal`; then the stored histories
   must give exactly the archive's rows with no other version (`apply_verify`);
   then the journal through `app.portability_import_journal` (decision 112),
   which checks the lock and the journal's emptiness itself, keeps each
   `account_rev` and `recorded_at`, and recomputes every event id and the chain
   header over the tenant's whole journal in SQL (`destination_not_empty` or
   `apply_verify`); then the other stores must still be empty; then one
   `portability.imported` audit event. Any failure rolls the savepoint back, so a
   caller that catches it still holds no write.
Source facts are kept: policy ids, version numbers, content and hashes, and each
journal event with its revision and knowledge time, as exported. A policy's
`created_at` (the destination's clock) and `created_by` (the importing owner) are
destination provenance, not source facts: neither is in the archive.
Nothing is adopted (no receipt is exported) and no credential is read:
`required_reconnections` is returned for the caller to reconnect.
`apply_archive` sets 0007's `app.balanced` back to DEFERRED after the journal
call, overriding a caller's `SET CONSTRAINTS ... IMMEDIATE` for the rest of the
transaction.
LIMITATIONS: accounts, watchlists and tombstones are applied only while empty
(`portability_apply.APPLIED`); evidence and learned state likewise. Every journal
insert shares the import lock (0060), so no journal writer commits inside an
import; the other stores' writers do not take it yet, and their emptiness is
checked before and after the writes at READ COMMITTED, so the /exports job must
keep the destination closed to them while it runs. The audit event carries no
export id, archive hash or section list: `app.audit_event` has no detail column,
and `correlation_id` does not admit every export id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import policy as dp
from qw_domain import portability as pt
from qw_domain import portability_apply as pa
from qw_domain.retention import RecordKey

from qw_adapters import policy_store as ps
from qw_adapters import tenancy as tn
from qw_adapters.export_archive import DEFAULT_LIMITS, Limits, read_archive

AUDIT_ACTION = "portability.imported"
PROVISION_ACTION = "portability.provisioned"
# 0060's refusals (custom SQLSTATEs). A row the store's own constraints refuse,
# an integrity (23) or data (22) error, is also `apply_verify`. A deadlock (40P01)
# can only be with a concurrent writer of this tenant: `destination_not_empty`.
_SQLSTATE = {"QI001": pt.Refusal.DESTINATION_NOT_EMPTY,
             "40P01": pt.Refusal.DESTINATION_NOT_EMPTY,
             "QI003": pt.Refusal.APPLY_VERIFY}  # fmt: skip
_UNITS = frozenset({"position", "unit_clearing", "corporate_action_clearing"})
# Every tenant table an import must find empty: each store a section is exported
# from, and every record bound to the journal, a policy or holdings (source
# observations and import previews, proposals, risk pauses, strategy adoptions,
# calculations, deletion records and legal holds, evidence and claims, feedback,
# virtual portfolios).
EMPTY_TABLES = ("ledger_event", "source_record", "import_preview", "policy_version",
                "policy_adoption", "risk_pause", "proposal_version", "proposal_event",
                "proposal_commitment", "strategy_adoption", "calculation_record",
                "watchlist_revision", "account", "account_revision", "data_source",
                "source_mapping_revision", "deletion_tombstone",
                "deletion_tombstone_checkpoint", "legal_hold_event",
                "evidence_source", "evidence_record",
                "evidence_event", "claim", "claim_evidence", "claim_resolution",
                "feedback", "feedback_consent", "simulation")  # fmt: skip
# Covered through a foreign key to their parent (empty parent, empty child); the
# test checks each key exists.
CHILDREN = {"posting": "ledger_event", "unit_posting": "ledger_event",
            "risk_pause_resume": "risk_pause", "evidence_content": "evidence_source",
            "feedback_content": "feedback", "feedback_tombstone": "feedback",
            "feedback_consent_revocation": "feedback_consent",
            "simulation_entry": "simulation", "simulation_order": "simulation",
            "simulation_fill": "simulation_order",
            "simulation_digest": "simulation"}  # fmt: skip
# Tenant tables deliberately not checked, and why. The test fails on any tenant
# table in none of the three lists, so this cannot go stale silently. Tables
# without `tenant_id` (strategy manifests, the rights registry, releases and
# optimizer records) are installation-wide and not part of a tenant.
_IDENTITY = "identity of the importing owner, which must exist"
_INFRA = "request and job infrastructure (the import job itself runs here)"
_NOTIFY = "notification delivery state and preferences, not portfolio state"
_BUDGET = "the tenant's cost ledger and reservations, kept as-is"
_RESEARCH = "improvement-loop research, assessor and optimizer records, not exported"
_RIGHTS = "an operator-recorded rights entitlement, never portable (T021)"
EXCLUDED = {
    "app_user": _IDENTITY, "membership": _IDENTITY, "session": _IDENTITY,
    "local_credential": _IDENTITY,
    "audit_event": "append-only destination provenance; the import appends to it",
    "idempotency_record": _INFRA, "installation_bootstrap": _INFRA, "job": _INFRA,
    "outbox": _INFRA, "inbox": _INFRA,
    "notification": _NOTIFY, "notification_delivery": _NOTIFY,
    "notification_delivery_event": _NOTIFY, "notification_seen": _NOTIFY,
    "notification_preference": _NOTIFY,
    "budget_authorization": _BUDGET, "budget_book": _BUDGET,
    "assessment_request": _RESEARCH, "assessor_candidate": _RESEARCH,
    "assessor_protocol": _RESEARCH, "evaluation_protocol": _RESEARCH,
    "evaluation_receipt": _RESEARCH, "experiment_registry_entry": _RESEARCH,
    "experiment_revision": _RESEARCH, "holdout_access": _RESEARCH,
    "holdout_run": _RESEARCH, "holdout_run_outcome": _RESEARCH,
    "holdout_series": _RESEARCH, "research_dataset": _RESEARCH,
    "strategy_search_space": _RESEARCH, "rights_entitlement": _RIGHTS,
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Applied:
    export_id: str
    scope_id: str
    sections: tuple[str, ...]
    policy_versions: int
    journal_events: int
    omissions: tuple[pt.Omission, ...]
    required_reconnections: tuple[str, ...]  # the caller reconnects; none imported


def _occupied(tx: tn.TenantTx, skip: frozenset[str] = frozenset()) -> str | None:
    for table in EMPTY_TABLES:
        if table in skip:
            continue
        row = tx.conn.execute(
            f"SELECT EXISTS (SELECT 1 FROM app.{table} WHERE tenant_id = %s)",
            (tx.tenant_id,),
        ).fetchone()
        if row is None or row[0]:
            return table
    return None


def _stored(tx: tn.TenantTx, plan: pa.ApplyPlan) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid in sorted({r.policy_id for r in plan.policies}):
        history = ps.load(tx, pid)
        if history is None:
            raise pt.PortabilityError(pt.Refusal.APPLY_VERIFY, str(pid))
        rows += [
            {
                "policy_id": str(pid),
                "version": v.version,
                "content_hash": v.content_hash,
                "label": v.draft.label,
                "content": dp.draft_to_json(v.draft),
            }
            for v in history.versions
        ]
    return rows


def _order(rows: Any) -> list[bytes]:
    return sorted(pt.canonical(r) for r in rows)


def journal_args(body: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    """The verified `financial/journal.json` body in 0060's store form: each event
    with its `account_rev` (its position in its account), store kind, stamps as
    given, terms and lines (money `[account, currency, wire, instrument | None]`,
    units `[account, instrument, wire]`, in wire order), and the chains as given.
    A mapping only: the import function and the store's constraints check it."""
    events: list[dict[str, Any]] = []
    revs: dict[str, int] = {}
    for r in body["events"]:
        money: list[list[str | None]] = []
        units: list[list[str]] = []
        for p in r["postings"]:
            base, _, sub = p["book_account"].partition(":")
            if base in _UNITS:
                units.append([base, p["commodity"], p["amount"]])
            else:
                money.append([base, p["commodity"], p["amount"], sub or None])
        notes = dict(n.partition(":")[::2] for n in r["economic_annotations"])
        terms: dict[str, Any] = {}
        if "fee" in notes:
            amount, _, currency = notes["fee"].partition(" ")
            terms["fee"] = {"amount": amount, "currency": currency}
        revs[r["account_id"]] = revs.get(r["account_id"], 0) + 1
        events.append({
            "account_id": r["account_id"], "event_id": r["id"],
            "account_rev": revs[r["account_id"]],
            "kind": "reversal" if r["kind"] == "correction" else r["kind"],
            "effective_at": r["effective_at"], "recorded_at": r["recorded_at"],
            "source_id": r["source_id"], "source_record_id": r["source_record_id"],
            "reverses": r["reverses_event_id"], "supersedes": notes.get("supersedes"),
            "lot_policy": notes.get("lot_policy"), "terms": terms,
            "money": money, "units": units})  # fmt: skip
    return events, list(body["chains"])


def provision(
    conn: tn.Conn, data: bytes, display_name: str, login: str, verifier: str,
    key: RecordKey | None = None, limits: Limits = DEFAULT_LIMITS, *,
    claim_installation: bool,
) -> tuple[uuid.UUID, uuid.UUID]:  # fmt: skip
    """Decision 111: the empty destination tenant with the archive's id and a new
    owner; with `claim_installation` (decision 117) also the bootstrap marker.
    Refused (`PortabilityError`) for an archive `apply_archive` would refuse before
    its principal check, and (`TenancyError`) for an id that exists, a claimed
    marker, or a connection that is not exactly qw_app. Returns (tenant, owner)."""
    if type(data) is not bytes:
        raise TypeError("provision takes the archive bytes")
    tn.check_runtime_role(conn, "qw_app")
    report = read_archive(data, limits)
    scope = report.manifest.scope_id
    try:
        tenant: uuid.UUID | None = uuid.UUID(scope)
    except ValueError:
        tenant = None
    if tenant is None or str(tenant) != scope:
        raise pt.PortabilityError(pt.Refusal.DESTINATION_SCOPE, "not a tenant id")
    pa.plan(report, scope, key)
    with tn.provision_tenant(conn, tenant) as tx:
        if claim_installation and conn.execute(  # as bootstrap_installation does
            "INSERT INTO app.installation_bootstrap (tenant_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING RETURNING tenant_id", (tenant,)
        ).fetchone() is None:  # fmt: skip
            raise tn.TenancyError("installation is already bootstrapped")
        user = tn.create_user(tx, display_name)
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        tn.set_local_credential(tx, user, login, verifier)
        tn.append_audit_event(tx, PROVISION_ACTION, actor_user_id=user,
                              target_id=tenant)  # fmt: skip
    return tenant, user


def apply_archive(tx: tn.TenantTx, data: bytes, principal: uuid.UUID,
                  key: RecordKey | None = None,
                  limits: Limits = DEFAULT_LIMITS) -> Applied:  # fmt: skip
    """Write the archive into this empty tenant, all or nothing; every refusal is
    a typed `PortabilityError`."""
    if type(data) is not bytes or type(principal) is not uuid.UUID:
        raise TypeError("apply_archive takes the archive bytes and a uuid.UUID")
    plan = pa.plan(read_archive(data, limits), str(tx.tenant_id), key)
    member = tn.get_membership(tx, principal)
    if member is None or member.role is not tn.MembershipRole.TENANT_OWNER:
        raise pt.PortabilityError(pt.Refusal.APPLY_PRINCIPAL, "not this tenant's owner")
    tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"import:{tx.tenant_id}",))  # fmt: skip
    if table := _occupied(tx):
        raise pt.PortabilityError(pt.Refusal.DESTINATION_NOT_EMPTY, table)
    try:
        with tx.conn.transaction():  # a savepoint: all of it or none
            events = _write(tx, plan, principal)
    except psycopg.Error as e:
        state = e.sqlstate or ""
        code = _SQLSTATE.get(state)
        if code is None and state[:2] in ("22", "23"):
            code = pt.Refusal.APPLY_VERIFY
        if code is None:
            raise
        raise pt.PortabilityError(code, pt.JOURNAL_PATH) from None
    return Applied(plan.export_id, plan.scope_id, plan.sections, len(plan.policies),
                   events, plan.omissions, plan.required_reconnections)  # fmt: skip


def _write(tx: tn.TenantTx, plan: pa.ApplyPlan, principal: uuid.UUID) -> int:
    for row in plan.policies:
        ps.propose(tx, row.policy_id, row.draft, principal)
    count = tx.conn.execute(
        "SELECT count(*) FROM app.policy_version WHERE tenant_id = %s",
        (tx.tenant_id,),
    ).fetchone()
    if count != (len(plan.policies),) or _order(_stored(tx, plan)) != _order(
        plan.policy_rows
    ):
        raise pt.PortabilityError(pt.Refusal.APPLY_VERIFY, pt.POLICIES_PATH)
    events, chains = journal_args(plan.journal)
    # every event or a refusal: the chain it checks covers the whole tenant journal
    got = tx.conn.execute("SELECT app.portability_import_journal(%s, %s)",
                          (Jsonb(events), Jsonb(chains))).fetchone()  # fmt: skip
    if table := _occupied(tx, frozenset({"policy_version", "ledger_event"})):
        raise pt.PortabilityError(pt.Refusal.DESTINATION_NOT_EMPTY, table)
    tn.append_audit_event(tx, AUDIT_ACTION, actor_user_id=principal,
                          target_id=tx.tenant_id)  # fmt: skip
    return 0 if got is None else int(got[0])
