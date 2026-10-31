"""Apply a verified export to an empty destination tenant (T057 increment 6; spec §2
Journey E; R006, R051, R052, R087, R096, R097).

`apply_archive(tx, data, principal, key)` runs in the caller's `tenant_transaction`
as the qw_app login (RLS; `policy_version` INSERT is granted to qw_app only):
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
   must give exactly the archive's rows with no other version (`apply_verify`),
   and the other stores must still be empty; then one `portability.imported`
   audit event. Any failure rolls the savepoint back, so a caller that catches it
   still holds no write.
Source facts are kept: policy ids, version numbers, content and hashes as
exported. `created_at` (the destination's clock) and `created_by` (the importing
owner) are destination provenance, not source facts: neither is in the archive.
Nothing is adopted (no receipt is exported) and no credential is read:
`required_reconnections` is returned for the caller to reconnect.
LIMITATIONS: the journal, accounts, watchlists and tombstones are applied only
while empty (`portability_apply.APPLIED`); evidence and learned state likewise.
Emptiness is checked, and checked again after the writes, at READ COMMITTED: a
writer that does not take the import lock and commits after the second check is
not excluded, so the /exports job must keep the destination closed while it runs
(owner decision 112 moves this check into SQL import functions). The audit event
carries no export id, archive hash or section list: `app.audit_event` has no
detail column, and `correlation_id` does not admit every export id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from qw_domain import policy as dp
from qw_domain import portability as pt
from qw_domain import portability_apply as pa
from qw_domain.retention import RecordKey

from qw_adapters import policy_store as ps
from qw_adapters import tenancy as tn
from qw_adapters.export_archive import DEFAULT_LIMITS, Limits, read_archive

AUDIT_ACTION = "portability.imported"
# Every tenant table an import must find empty: each store a section is exported
# from, and every record bound to the journal, a policy or holdings (source
# observations and import previews, proposals, risk pauses, strategy adoptions,
# calculations, evidence and claims, feedback, virtual portfolios).
EMPTY_TABLES = ("ledger_event", "source_record", "import_preview", "policy_version",
                "policy_adoption", "risk_pause", "proposal_version", "proposal_event",
                "proposal_commitment", "strategy_adoption", "calculation_record",
                "watchlist_revision", "account", "account_revision", "data_source",
                "source_mapping_revision", "deletion_tombstone",
                "deletion_tombstone_checkpoint", "evidence_source", "evidence_record",
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
    with tx.conn.transaction():  # a savepoint: all of it or none
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
        if table := _occupied(tx, frozenset({"policy_version"})):
            raise pt.PortabilityError(pt.Refusal.DESTINATION_NOT_EMPTY, table)
        tn.append_audit_event(tx, AUDIT_ACTION, actor_user_id=principal,
                              target_id=tx.tenant_id)  # fmt: skip
    return Applied(plan.export_id, plan.scope_id, plan.sections, len(plan.policies),
                   plan.omissions, plan.required_reconnections)  # fmt: skip
