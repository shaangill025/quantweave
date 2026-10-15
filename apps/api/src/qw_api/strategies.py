"""Read-only strategy routes (T027): `GET /strategies` (listStrategies) and
`GET /strategies/{strategy_id}` (readStrategy), over qw_adapters.strategy_store.

Like the T053 reads: membership reads, problem+json errors, C-02 keyset pages with
the HMAC cursor of qw_api.reads, nothing written. Manifests are installation facts,
so every tenant sees the same registry; an unknown id is a 404. A stored registry
that fails its checks is a 500, never an answer. Each item is the latest registered
version of a strategy (spec strategy_version schema, no amendment):
- `configuration_hash` is the default configuration's (`default:` in coverage).
- `operational_status`/`evidence_status` come from `strategy_registry.status` at the
  database clock. Qualification steps and family policies are not persisted
  (`unavailable:qualification`), so they read `not_tested`/`none`.
- `action_eligible` is always false: `strategy_gate.actionable` needs persisted
  qualification, evidence, capabilities and the adopted policy, none of which is
  wired here (`unavailable:action_gate`). Adoption alone never makes it true.
- `approved_by` is null (no release approval is modelled); `source_decisions` is
  empty (`unavailable:source_decisions`). `policy_requirements` are the manifest
  facts the gate checks against the adopted investment policy.
- A page names every family missing from the installed registry as
  `none:family/<family>` (the six are first-release scope; `qw-seed-strategies`
  records them, T027 owner decision 10, so a seeded install names none).
POST /strategies/{id}/adopt is not served (step-up and If-Match design pending).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from qw_adapters import strategy_store as ss
from qw_domain import strategy_registry as sr

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID, Cursor, Limit, _after, _page

router = APIRouter(prefix="/api/v1")
StrategyId = Annotated[str, Path(pattern=ID)]
GAPS = ["default:configuration_hash", "unavailable:qualification",
        "unavailable:action_gate", "unavailable:source_decisions"]  # fmt: skip


def requirements(m: sr.StrategyManifest) -> list[str]:
    out = [f"instrument:{a.value}" for a in m.asset_classes]
    out.append(f"horizon:{m.horizon.value}")
    if m.family is sr.Family.AI_THESIS:
        out.append("ai_mode:ai_enabled")
    if m.regular_session_only:
        out.append("session:regular")
    return sorted(out)


def view(snap: ss.Snapshot, tenant: str, m: sr.StrategyManifest) -> dict[str, Any]:
    s = sr.status(snap.registry, tenant, m.strategy_id, m.version, {}, snap.at)
    return {
        "id": m.strategy_id, "family": m.family.value, "version": m.version,
        "implementation_hash": m.implementation_hash,
        "configuration_hash": s.config_hash,
        "required_feed_spec_ids": sorted({d.feed_id for d in m.data}),
        "policy_requirements": requirements(m),
        "research_protocol_id": m.research_protocol_id,
        "operational_status": s.operational.value, "evidence_status": s.evidence.value,
        "action_eligible": False, "approved_by": None, "source_decisions": [],
    }  # fmt: skip


def _snapshot(principal: Reader) -> ss.Snapshot:
    with principal.tx() as tx:
        return ss.snapshot(tx)


@router.get("/strategies")
def list_strategies(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, "strategy_id", cursor)
    snap, tenant = _snapshot(principal), str(principal.session.tenant_id)
    rest = [m for m in snap.latest if after is None or m.strategy_id > after]
    items = [view(snap, tenant, m) for m in rest[:limit]]
    held = {m.family for m in snap.latest}
    missing = [f"none:family/{f.value}" for f in sr.Family if f not in held]
    reasons = GAPS * bool(items) + missing
    return _page(request, principal, "strategy_id", items, len(rest) > limit, reasons)


@router.get("/strategies/{strategy_id}")
def read_strategy(principal: Reader, strategy_id: StrategyId) -> Response:
    snap = _snapshot(principal)
    found = [m for m in snap.latest if m.strategy_id == strategy_id]
    if not found:
        raise ApiError(404, "not_found", "No such strategy.")
    body = view(snap, str(principal.session.tenant_id), found[0])
    return Response(json.dumps(body).encode(), 200, media_type="application/json")
