"""Persisted policy versions and adoption receipts (T019 increment 2;
`0009_policy.sql`; spec §7 "Policy as a versioned contract"; T008 C-20).

The domain `PolicyHistory` is the model; this module stores and reloads it. Every
function works in the caller's tenant transaction and first takes the policy's
transaction advisory lock (the same key as the 0009 insert trigger), so proposals
and adoptions of one policy serialise.
- `propose` appends a version only when the draft's content hash changed.
- `adopt` verifies, under the lock, that the policy exists in this tenant, that the
  principal's own live session stepped up within `window` (at most 1 hour), that the
  version and content hash are the latest, and then runs the domain adoption (not
  latest, already adopted, conflicts, limits). The receipt is stored in the same
  transaction; `signed_at` is the transaction time, as the database assigns it.
- `load` rebuilds the history and recomputes every content and receipt hash; any
  disagreement with the stored hashes, labels, tenant or version sequence raises
  `PolicyStoreError("integrity")`, so a tampered row never reaches a caller.
Policy ids are server-made UUIDs (C-07). Decimal values stay decimal strings in JSON.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import policy as dp
from qw_domain.decimals import Money, MoneyAmount, Ratio
from qw_domain.instants import ensure_aware_utc
from qw_domain.onboarding import Allocation, Limit, LimitMetric, SizingScope

from qw_adapters.tenancy import TenantTx

MAX_STEP_UP_WINDOW = timedelta(hours=1)  # the 0009 trigger's bound
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PolicyStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def step_up_receipt_id(session_id: uuid.UUID, step_up_at: datetime) -> str:
    micros = (ensure_aware_utc(step_up_at) - _EPOCH) // timedelta(microseconds=1)
    return f"{session_id}.{micros}"


def _scope(s: SizingScope) -> dict[str, str | None]:
    return {
        "account_id": s.account_id,
        "currency": s.currency,
        "sleeve_id": s.sleeve_id,
    }


def _limit(w: Mapping[str, Any]) -> Limit:
    value = w["value"]
    amount = (
        Ratio.from_wire(value) if w["unit"] == "ratio" else MoneyAmount.from_wire(value)
    )
    scope = SizingScope(w["account_id"], w["currency"], w["sleeve_id"])
    return Limit(LimitMetric(w["metric"]), amount, w["unit"], w["denominator"],
                 w["window"], scope)  # fmt: skip


def draft_to_json(d: dp.DraftPolicy) -> dict[str, Any]:
    return {
        "tenant_id": d.tenant_id, "synthetic": d.synthetic,
        "schema": d.response_schema_version,
        "fields": [[f.name, f.value if isinstance(f.value, str) else list(f.value),
                    list(f.sources)] for f in d.fields],
        "limits": [x.to_wire() for x in d.limits],
        "allocations": [_scope(a.scope) | {"amount": a.amount.to_wire()}
                        for a in d.allocations],
        "conflicts": [[c.code.value, list(c.sources), c.blocking, c.detail]
                      for c in d.conflicts],
        "exclusions": [[e.subject, e.reason.value] for e in d.exclusions],
        "warnings": list(d.retained_warnings),
    }  # fmt: skip


def draft_from_json(j: Mapping[str, Any]) -> dp.DraftPolicy:
    return dp.DraftPolicy(
        tenant_id=j["tenant_id"], synthetic=j["synthetic"],
        response_schema_version=j["schema"],
        fields=tuple(dp.InferredField(n, v if isinstance(v, str) else tuple(v),
                                      tuple(s)) for n, v, s in j["fields"]),
        limits=tuple(_limit(w) for w in j["limits"]),
        allocations=tuple(Allocation(
            SizingScope(a["account_id"], a["currency"], a["sleeve_id"]),
            Money.from_wire(a["amount"])) for a in j["allocations"]),
        conflicts=tuple(dp.Conflict(dp.ConflictCode(c), tuple(s), b, t)
                        for c, s, b, t in j["conflicts"]),
        exclusions=tuple(dp.Exclusion(s, dp.ConflictCode(r))
                         for s, r in j["exclusions"]),
        retained_warnings=tuple(j["warnings"]),
    )  # fmt: skip


def _lock(tx: TenantTx, policy_id: uuid.UUID) -> None:
    key = f"policy/{tx.tenant_id}/{policy_id}"
    tx.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))


def _integrity(ok: bool, what: str) -> None:
    if not ok:
        raise PolicyStoreError("integrity", what)


def load(tx: TenantTx, policy_id: uuid.UUID) -> dp.PolicyHistory | None:
    """The tenant's history of `policy_id`, hash-verified; None when absent here."""
    found = tx.conn.execute(
        "SELECT version, content_hash, content, label FROM app.policy_version "
        "WHERE tenant_id = %s AND policy_id = %s ORDER BY version",
        (tx.tenant_id, policy_id),
    ).fetchall()
    if not found:
        return None
    versions = []
    for n, (number, digest, content, label) in enumerate(found, start=1):
        draft = draft_from_json(content)
        _integrity(number == n and draft.tenant_id == str(tx.tenant_id), "sequence")
        _integrity(draft.content_hash == digest and draft.label == label, "version")
        versions.append(dp.PolicyVersion(str(policy_id), n, draft, digest))
    receipts = []
    for row in tx.conn.execute(
        "SELECT version, policy_hash, label, principal_id, step_up_receipt_id, "
        "signed_at, limits, acknowledged, exclusions, acknowledgement_text, "
        "receipt_hash FROM app.policy_adoption "
        "WHERE tenant_id = %s AND policy_id = %s ORDER BY version",
        (tx.tenant_id, policy_id),
    ):
        number, digest, label, who, step_up, signed, limits, acks, excl, text = row[:10]
        receipt = dp.AdoptionReceipt(
            "policy_adoption", str(tx.tenant_id), str(policy_id), number, digest,
            label, str(who), step_up, ensure_aware_utc(signed),
            tuple(_limit(w) for w in limits),
            tuple(dp.ConflictCode(c) for c in acks),
            tuple(dp.Exclusion(s, dp.ConflictCode(r)) for s, r in excl), text,
        )  # fmt: skip
        target = versions[number - 1]
        d = target.draft
        _integrity(
            receipt.receipt_hash == row[10] and digest == target.content_hash
            and receipt.limits == d.limits and receipt.exclusions == d.exclusions
            # the domain's admissibility rules, so hash-consistent forgeries fail too
            and not any(c.blocking for c in d.conflicts) and not dp._limit_gaps(d)
            and set(receipt.acknowledged) == {c.code for c in d.conflicts},
            "receipt",
        )  # fmt: skip
        receipts.append(receipt)
    return dp.PolicyHistory(
        str(policy_id), str(tx.tenant_id), tuple(versions), tuple(receipts)
    )


def list_policy_ids(
    tx: TenantTx, limit: int, after: uuid.UUID | None = None
) -> list[uuid.UUID]:
    found = tx.conn.execute(
        "SELECT DISTINCT policy_id FROM app.policy_version WHERE tenant_id = %s "
        "AND (%s::uuid IS NULL OR policy_id > %s) ORDER BY policy_id LIMIT %s",
        (tx.tenant_id, after, after, limit),
    ).fetchall()
    return [row[0] for row in found]


def propose(
    tx: TenantTx, policy_id: uuid.UUID, draft: dp.DraftPolicy, created_by: uuid.UUID
) -> dp.PolicyHistory:
    if draft.tenant_id != str(tx.tenant_id):
        raise PolicyStoreError("tenant", "the draft belongs to another tenant")
    if not draft.allocations and not draft.limits:
        raise PolicyStoreError("scope", "a policy names at least one account")
    _lock(tx, policy_id)
    history = load(tx, policy_id) or dp.PolicyHistory.new(
        str(policy_id), str(tx.tenant_id)
    )
    proposed = history.propose(draft)
    if proposed is not history:
        latest = proposed.versions[-1]
        tx.conn.execute(
            "INSERT INTO app.policy_version (tenant_id, policy_id, version, "
            "content_hash, content, label, created_by) VALUES (%s, %s, %s, %s, %s, "
            "%s, %s)",
            (tx.tenant_id, policy_id, latest.version, latest.content_hash,
             Jsonb(draft_to_json(draft)), draft.label, created_by),
        )  # fmt: skip
    return proposed


def adopt(
    tx: TenantTx, policy_id: uuid.UUID, version: int, content_hash: str, *,
    principal: uuid.UUID, session_id: uuid.UUID, window: timedelta,
    acknowledged: frozenset[dp.ConflictCode], text: str,
) -> dp.PolicyHistory:  # fmt: skip
    _lock(tx, policy_id)
    history = load(tx, policy_id)
    if history is None:
        raise PolicyStoreError("not_found", "no such policy")
    session = tx.conn.execute(
        "SELECT step_up_at, now() FROM app.session WHERE tenant_id = %s AND id = %s "
        "AND user_id = %s AND revoked_at IS NULL AND expires_at > now() FOR SHARE",
        (tx.tenant_id, session_id, principal),
    ).fetchone()
    limit = min(window, MAX_STEP_UP_WINDOW)
    if session is None or session[0] is None or session[1] - session[0] > limit:
        raise PolicyStoreError("step_up_required", "no recent step-up")
    step_up_at, now = session
    latest = history.versions[-1]
    if version == latest.version and content_hash != latest.content_hash:
        raise PolicyStoreError("stale", "the content hash is not the latest")
    consent = dp.AdoptionConsent(
        str(principal), step_up_receipt_id(session_id, step_up_at),
        ensure_aware_utc(now), acknowledged, text,
    )  # fmt: skip
    adopted = history.adopt(version, consent)
    try:
        with tx.conn.transaction():  # savepoint: a trigger refusal is a typed error
            _insert_receipt(tx, policy_id, principal, session_id, step_up_at,
                            adopted.receipts[-1])  # fmt: skip
    except psycopg.errors.RaiseException as exc:
        if "no valid step-up" not in str(exc):
            raise
        raise PolicyStoreError("step_up_required", "no recent step-up") from None
    return adopted


def _insert_receipt(
    tx: TenantTx, policy_id: uuid.UUID, principal: uuid.UUID,
    session_id: uuid.UUID, step_up_at: datetime, r: dp.AdoptionReceipt,
) -> None:  # fmt: skip
    tx.conn.execute(
        "INSERT INTO app.policy_adoption (tenant_id, policy_id, version, policy_hash, "
        "label, principal_id, session_id, step_up_at, step_up_receipt_id, limits, "
        "acknowledged, exclusions, acknowledgement_text, receipt_hash) VALUES "
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (tx.tenant_id, policy_id, r.version, r.policy_hash, r.label, principal,
         session_id, step_up_at, r.step_up_receipt_id,
         Jsonb([x.to_wire() for x in r.limits]),
         Jsonb([c.value for c in r.acknowledged]),
         Jsonb([[e.subject, e.reason.value] for e in r.exclusions]),
         r.acknowledgement_text, r.receipt_hash),
    )  # fmt: skip
