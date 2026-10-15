"""Read-only research routes for the T054 views: `GET /calculations/{calculation_id}`
(readCalculation) over qw_adapters.calculation_store, and `GET /claims/{claim_id}`
(readClaim) and `GET /evidence/{evidence_id}` (readEvidence) over
qw_adapters.evidence_store (T036 increment 4).

Like the T053 reads: membership reads, the session tenant under RLS (another
tenant's calculation is the same 404 as an unknown one), problem+json errors, and
nothing written. The body is the domain's contract projection
(`CalculationRecord.to_contract`), which fills every field of the spec calculation
schema; what the schema cannot hold (as-of, rounding, coverage, date and reference
inputs, input as-of times, the unavailable code) is named in `warnings`. A stored
record that fails its hash or domain checks is a 500, never an answer.

Claims and evidence (project contract 0.3.0: both gain `coverage`; claim
`material` is null, named `unknown:material`) are evaluated now, by the database
clock, under the `app.state.rights` registry, loaded in the request's tenant
transaction (qw_adapters.rights_store by default; a tampered registry is a 500).
Evidence text is served only when
the source is usable and display is granted; otherwise `passage` is null,
`availability` is deleted, unavailable (withdrawn, expired, purged, unpublished)
or restricted, and the reason is in coverage. A claim's `verification` is the
domain support status (`unavailable` when nothing counts and rights are a
reason); evidence and counterevidence ids are the passages that count for it and
for disagreeing members of its contradiction; invalid citations are in coverage.
A claim restates what its evidence says, so its text is served only when display
is granted for every source it cites for the current revision (counted or not;
superseded citations excepted); otherwise `text` is "" (the contract's text is a
required string) and coverage names `withheld:<claim_id>/text:rights` (owner
decision 2026-10-15). Status, ids and support are unchanged.
Option terms have a contract route but no store yet, so they are not served here
(docs/receipts/T054-api.md); simulations are read in qw_api.simulations (T041).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from qw_adapters import calculation_store as store
from qw_adapters import evidence_store
from qw_domain.evidence import EvidenceError, EvidenceStore, Invalid, Status
from qw_domain.instants import format_instant
from qw_domain.rights import Registry, Use, check_use

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID

router = APIRouter(prefix="/api/v1")


@router.get("/calculations/{calculation_id}")
def read_calculation(
    principal: Reader, calculation_id: Annotated[str, Path(pattern=ID)]
) -> Response:
    with principal.tx() as tx:
        rec = store.load(tx, calculation_id)
    if rec is None:
        raise ApiError(404, "not_found", "No such calculation.")
    body = json.dumps(rec.to_contract()).encode()
    return Response(body, 200, media_type="application/json")


type Opened = tuple[EvidenceStore, datetime, Registry]


def _json(body: dict[str, Any]) -> Response:
    return Response(json.dumps(body).encode(), 200, media_type="application/json")


def _open(principal: Reader, request: Request) -> Opened:
    with principal.tx() as tx:
        es = evidence_store.load(tx, for_write=False)  # read-only: shared lock
        rights = request.app.state.rights(tx)  # this tenant's, re-verified
        # "now" is read once the locks are held, in the same transaction.
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return es, row[0], rights


def _coverage(reasons: list[str]) -> dict[str, object]:
    return {"status": "partial" if reasons else "complete", "reasons": reasons}


@router.get("/evidence/{evidence_id}")
def read_evidence(
    principal: Reader, request: Request, evidence_id: Annotated[str, Path(pattern=ID)]
) -> Response:
    es, now, rights = _open(principal, request)
    tenant = str(principal.session.tenant_id)
    try:
        p = es.passage(tenant, evidence_id)
    except EvidenceError:
        raise ApiError(404, "not_found", "No such evidence.") from None
    src, text = es.source(tenant, p.source_id), es.passage_text(tenant, evidence_id)
    why, reasons = es.unusable(src, now), []
    shown = check_use(rights, tenant, src.feed_id, Use.DISPLAY, now,
                      scope=src.scope, jurisdiction=src.jurisdiction)  # fmt: skip
    if why is Invalid.DELETED:
        availability = "deleted"
    elif why is not None or text is None:
        availability = "unavailable"
    elif not shown.allowed:
        availability = "restricted"
        reasons += sorted({f"restricted:{d.code.value}" for d in shown.reasons})
    else:
        availability = "available"
    if why is not None:
        reasons.append(why.value)
    elif text is None:
        reasons.append("content_removed")
    return _json({
        "id": p.passage_id, "source_id": src.source_id,
        "locator": f"chars:{p.start}-{p.end}",
        "passage": text if availability == "available" else None,
        "content_hash": p.text_hash,
        "published_at": format_instant(src.published_at),
        "retrieved_at": format_instant(src.received_at),
        "rights_profile_id": src.feed_id, "availability": availability,
        "source_relationship_group": src.origin, "retention_class": "evidence",
        "instrument_ids": [p.reading.entity.to_wire()],
        "coverage": _coverage(reasons),
    })  # fmt: skip


def _displayable(
    es: EvidenceStore, tenant: str, now: datetime, rights: Registry, cited: list[str]
) -> bool:
    """True when display is granted for the source of every passage `cited`."""
    for pid in cited:
        src = es.source(tenant, es.passage(tenant, pid).source_id)
        shown = check_use(rights, tenant, src.feed_id, Use.DISPLAY, now,
                          scope=src.scope, jurisdiction=src.jurisdiction)  # fmt: skip
        if not shown.allowed:
            return False
    return True


@router.get("/claims/{claim_id}")
def read_claim(
    principal: Reader, request: Request, claim_id: Annotated[str, Path(pattern=ID)]
) -> Response:
    es, now, rights = _open(principal, request)
    tenant = str(principal.session.tenant_id)
    try:
        rev = es.claim_at(tenant, claim_id, now)
    except EvidenceError:
        rev = None
    if rev is None:
        raise ApiError(404, "not_found", "No such claim.")
    passage = {c.citation_id: c.passage_id for c in es.snapshot(tenant).citations}
    sup = es.support(tenant, claim_id, now, rights)
    verification = sup.status.value
    if sup.status is Status.UNSUPPORTED and Invalid.RIGHTS_REVOKED in set(
        sup.invalid.values()
    ):
        verification = "unavailable"
    against: set[str] = set()
    for con in es.contradictions(tenant, now, rights):
        if (claim_id, rev.revision) in con.members:
            for other, _ in con.members:
                o = es.claim_at(tenant, other, now)
                if o is not None and o.stated.value != rev.stated.value:
                    valid = es.support(tenant, other, now, rights).valid
                    against |= {passage[c] for c in valid}
    reasons = ["unknown:material"]
    reasons += [f"invalid:{passage[c]}={why.value}"
                for c, why in sorted(sup.invalid.items())]  # fmt: skip
    if sup.replay_limited:
        reasons.append("replay_limited")
    s, start = rev.stated, rev.stated.period.start
    period = s.period.end.isoformat()
    if start is not None:
        period = f"{start.isoformat()}/{period}"
    text = f"{s.attribute} of {s.entity.to_wire()} for {period}: {s.value} {s.unit}"
    if not _displayable(es, tenant, now, rights, [passage[c] for c in (
            *sup.valid, *(c for c, why in sup.invalid.items()
                          if why is not Invalid.REVISION_SUPERSEDED))]):  # fmt: skip
        text = ""
        reasons.append(f"withheld:{claim_id}/text:rights")
    return _json({
        "id": claim_id, "kind": rev.kind.value, "text": text,
        "instrument_ids": [s.entity.to_wire()],
        "evidence_ids": sorted({passage[c] for c in sup.valid}),
        "calculation_ids": [], "material": None, "verification": verification,
        "counterevidence_ids": sorted(against), "valid_at": format_instant(now),
        "coverage": _coverage(reasons),
    })  # fmt: skip
