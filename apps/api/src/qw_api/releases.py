"""Release and install history (Read B; owner decision 45 of 2026-10-22, with
decision 5 of 2026-10-20): `GET /releases` (listReleases, project contract 0.12.0),
`GET /releases/{release_id}` (readRelease) and `GET /deployment/events`
(listDeploymentEvents, both 0.13.0),
served only because real storage backs it, the operator-recorded deployment and
trusted keys (T051, `qw-release-admin`) and the 0029 ledger.

- The server runs as `Settings.release_deployment_id`. Unset, unrecorded, or a
  session tenant that is not a member of the recorded (frozen, decision 48)
  deployment: the same 404, as Read B and decision 45 say. No session holds an
  installation role yet, so membership is the session tenant's only.
- qw_app holds no privilege on the release tables. `release_store.history` reads
  through 0037's member-only SECURITY DEFINER functions (explicit columns since
  0039), re-hashes every row and
  replays the ledger with full signature checks against the stored keys, at the
  database clock. A test-only key verifies only through the in-process hook
  (`create_app(release_test_verifiers=...)`); without it such a ledger cannot
  replay and is 409 `integrity`, like any stored row that fails a check.
- Items are the installed releases, sorted by (installed_at, id), optionally one
  `component`; C-02 pages with the T010 HMAC cursor (bound to the filter too).
- Tenant members never see identities or installation-scope ids (decision 10,
  Q5; Read B): `experiment_id`, `candidate_id`, `verdict_id` are null, named
  `unavailable:<field>`; the signature's `key_id` is null, named
  `none:signature/key_id`. Owner decision 55 (2026-10-23): `signature_reference`
  is hash-only, `sig-sha256.` and the first 32 hex digits of the signature's
  digest (the last part of the internal `sig.<key_id>.<hash>` reference), so it
  carries no key id. Receipts are not stored, so `receipt_status` is
  `unavailable`, and `approved_at` (decision 58) is null; both are named
  `unavailable:receipts` (decision 45). `verdict_source` (decision 33) and the
  install's `self_approved` (decision 23; kept by decision 56) are served.
  `tenant_adoption` is the caller's own strategy state (`strategy_state`), null
  with `none:tenant_adoption` for other releases.
- readRelease is exactly the release's list item; a release not installed in the
  deployment is the same 404 as an unknown id. Events are the ledger in order
  (seq follows `at`), optionally one component, cursor by seq: install and pause
  name `release_id`, rollback `from_release_id` and `to_release_id`, the others
  null and named `none:<field>`; `schema_after` follows the ledger's forward-only
  rule; `operator_id` and `step_up_receipt_id` are null, named `none:<field>`.
Nothing is written."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response
from qw_adapters import release_store as rs
from qw_adapters import strategy_store
from qw_domain import release as rl
from qw_domain.instants import format_instant, parse_instant
from qw_domain.research_data import digest
from qw_domain.strategy_registry import StrategyRegistry as Strategies

from qw_api import security as sec
from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID, Cursor, Limit, _after, _page
from qw_api.security import Principal

router = APIRouter(prefix="/api/v1")
COMPONENT = r"^[a-z_]{1,32}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Component = Annotated[str | None, Query(pattern=COMPONENT, max_length=161)]
HIDDEN = ["unavailable:experiment_id", "unavailable:candidate_id",
          "unavailable:verdict_id", "none:signature/key_id",
          "unavailable:receipts"]  # fmt: skip
NOT_STRATEGY = ["none:strategy_version", "none:strategy_material_hash",
                "none:tenant_adoption"]  # fmt: skip
ReleaseId = Annotated[str, Path(pattern=ID)]
WHO = ["none:operator_id", "none:step_up_receipt_id"]
NOT_FOUND = ApiError(404, "not_found", "No release history for this session.")


def _state(lg: rl.DeploymentLedger, rid: str, component: str) -> str:
    if any(isinstance(e, rl.Rollback) and e.from_release_id == rid
           for e in lg.entries):  # fmt: skip
        return "withdrawn"
    if any(isinstance(e, rl.Pause) and e.release_id == rid for e in lg.entries):
        return "paused"
    return "head" if lg.head(component) == rid else "installed"


def reference(sr: rl.SignedRelease) -> str:
    """Decision 55: a hash of the signature alone, never the key id."""
    return f"sig-sha256.{digest(sr.signature)[:32]}"


def wire(h: rs.History, i: rl.Install, adoption: rl.TenantStrategy | None,
         ) -> dict[str, Any]:  # fmt: skip
    sr, lg = i.release, h.ledger
    m, c = sr.manifest, sr.manifest.compatibility
    rid = m.release_id
    refused = rl.verify_release(sr, h.keys, h.at, lg.deployment.allows_test_keys)
    blocked = lg.runnable(rid, h.at)
    why = [*HIDDEN, *([] if m.strategy_version else NOT_STRATEGY)]
    if adoption is not None and adoption.version is None:
        why.append("none:tenant_adoption/version")
    return {
        "id": rid, "component": m.component, "target": m.target.value,
        "change_kind": rl.CHANGE_KIND[m.target], "experiment_id": None,
        "candidate_id": None, "verdict_id": None,
        "rollback_version_id": m.rollback_version_id,
        "candidate_hash": m.candidate_hash, "artifact_hash": m.artifact_hash,
        "protocol_hash": m.protocol_hash, "manifest_hash": sr.manifest_hash,
        "required_approvals": list(m.required_approvals),
        "compatibility": {"min_schema": c.min_schema, "max_schema": c.max_schema,
                          "writes_schema": c.writes_schema},
        "strategy_version": m.strategy_version,
        "strategy_material_hash": m.strategy_material_hash,
        "verdict_source": sr.verdict_source.value,
        "signature": {"key_id": None, "algorithm": sr.algorithm,
                      "signature_reference": reference(sr),
                      "verification": "refused" if refused else "verified",
                      "reasons": refused},
        "install": {"state": _state(lg, rid, m.component),
                    "installed_at": format_instant(i.at), "runnable": not blocked,
                    "self_approved": i.self_approved, "reasons": blocked},
        "receipt_status": "unavailable", "approved_at": None,
        "user_adoption_required": m.user_adoption_required,
        "tenant_adoption": None if adoption is None else {
            "state": adoption.state.value, "version": adoption.version,
            "reasons": list(adoption.reasons)},
        "coverage": {"status": "partial", "reasons": sorted(why)},
    }  # fmt: skip


def _history(request: Request, principal: Principal,
             ) -> tuple[rs.History, Strategies]:  # fmt: skip
    """The member's verified history and its tenant's strategies, or the 404."""
    deployment = sec.settings_of(request).release_deployment_id
    if deployment is None:
        raise NOT_FOUND
    hook = request.app.state.release_test_verifiers
    with principal.tx() as tx:
        try:
            h = rs.history(tx, deployment, hook)
        except rs.ReleaseStoreError as exc:
            if exc.code != "integrity":
                raise  # database: a crash (500), never a client answer
            raise ApiError(409, "integrity", "Stored releases failed checks.") from None
        if h is None:
            raise NOT_FOUND
        return h, strategy_store.load(tx)


def _item(h: rs.History, i: rl.Install, strategies: Strategies,
          tenant: str) -> dict[str, Any]:  # fmt: skip
    sid = i.release.manifest.strategy_id
    adoption = None if sid is None else rl.strategy_state(
        h.ledger, strategies, tenant, sid, h.at)  # fmt: skip
    return wire(h, i, adoption)


def _paged(items: list[dict[str, Any]]) -> list[str]:
    return [f"{r.split(':', 1)[0]}:{x['id']}/{r.split(':', 1)[1]}"
            for x in items for r in x["coverage"]["reasons"]]  # fmt: skip


@router.get("/releases")
def list_releases(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None,
    component: Component = None,
) -> Response:  # fmt: skip
    sort = f"installed_at,id;component={component or ''}"
    after = _after(request, principal, sort, cursor)
    h, strategies = _history(request, principal)
    tenant = str(principal.session.tenant_id)
    installs = sorted((e for e in h.ledger.entries if isinstance(e, rl.Install)
                       and component in (None, e.release.manifest.component)),
                      key=lambda e: (e.at, e.release.manifest.release_id))  # fmt: skip
    if after is not None:
        at, _, rid = after.partition("|")
        start = (parse_instant(at), rid)
        installs = [e for e in installs
                    if (e.at, e.release.manifest.release_id) > start]  # fmt: skip
    shown, more = installs[:limit], len(installs) > limit
    items = [_item(h, i, strategies, tenant) for i in shown]
    why = _paged(items)
    last = shown[-1] if more else None
    nxt = None if last is None else (
        f"{format_instant(last.at)}|{last.release.manifest.release_id}")  # fmt: skip
    return _page(request, principal, sort, items, more, why, nxt)


@router.get("/releases/{release_id}")
def read_release(
    request: Request, principal: Reader, release_id: ReleaseId
) -> Response:  # fmt: skip
    """Exactly the release's listReleases item; not installed here is unknown."""
    h, strategies = _history(request, principal)
    found = [e for e in h.ledger.entries
             if isinstance(e, rl.Install) and e.release.manifest.release_id
             == release_id]  # fmt: skip
    if not found:
        raise NOT_FOUND  # the same body as a non-member: no membership oracle
    body = _item(h, found[0], strategies, str(principal.session.tenant_id))
    return Response(json.dumps(body).encode(), 200, media_type="application/json")


def event_wire(e: rl.Event, schema_after: int) -> dict[str, Any]:
    """One ledger event; a field its kind lacks is null, named `none:<field>`, and
    identities are null for tenant members (decision 10)."""
    rid = frm = to = None
    if isinstance(e, rl.Install):
        kind, component, rid = "install", e.release.manifest.component, (
            e.release.manifest.release_id)  # fmt: skip
    elif isinstance(e, rl.Rollback):
        kind, component, frm, to = "rollback", e.component, e.from_release_id, (
            e.to_release_id)  # fmt: skip
    else:
        kind, component, rid = "pause", e.component, e.release_id
    reasons = list(e.reasons) if isinstance(e, rl.Pause) else []
    shown = {"release_id": rid, "from_release_id": frm, "to_release_id": to}
    why = [*WHO, *(f"none:{k}" for k, v in shown.items() if v is None)]
    return {"id": e.event_id, "kind": kind, "component": component,
            "at": format_instant(e.at), **shown, "reasons": reasons,
            "schema_after": schema_after, "operator_id": None,
            "step_up_receipt_id": None,
            "coverage": {"status": "partial", "reasons": sorted(why)}}  # fmt: skip


@router.get("/deployment/events")
def list_deployment_events(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None,
    component: Component = None,
) -> Response:  # fmt: skip
    sort = f"seq;component={component or ''}"
    after = _after(request, principal, sort, cursor)
    h, _ = _history(request, principal)
    schema, rows = h.ledger.deployment.initial_schema, []
    for seq, e in enumerate(h.ledger.entries, 1):  # ledger order: seq follows `at`
        if isinstance(e, rl.Install):  # as 0029 and the domain: only moves forward
            schema = max(schema, e.release.manifest.compatibility.writes_schema)
        item = event_wire(e, schema)
        if component in (None, item["component"]) and (
            after is None or seq > int(after)
        ):  # fmt: skip
            rows.append((seq, item))
    shown, more = rows[:limit], len(rows) > limit
    items = [item for _, item in shown]
    return _page(request, principal, sort, items, more, _paged(items),
                 str(shown[-1][0]) if more else None)  # fmt: skip
