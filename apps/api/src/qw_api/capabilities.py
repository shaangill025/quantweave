"""`GET /capabilities` (readCapabilities, T021 increment 3): the session tenant's
qualified and unavailable features (qw_domain.capabilities).

A membership read: the rights registry is loaded per request inside the tenant
transaction (`app.state.rights`, qw_adapters.rights_store by default, so another
tenant's entitlements are never read) and evaluated at the database clock read in
the same transaction. A registry that fails integrity is a 500, never an answer.
`deployment` and the self-hosted jurisdiction come from `Settings`. Nothing is
written.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request, Response
from qw_domain.capabilities import evaluate

from qw_api import security as sec
from qw_api.policies import Reader

router = APIRouter(prefix="/api/v1")


@router.get("/capabilities")
def read_capabilities(principal: Reader, request: Request) -> Response:
    with principal.tx() as tx:
        rights = request.app.state.rights(tx)  # this tenant's, re-verified
        row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    settings = sec.settings_of(request)
    states = evaluate(rights, str(principal.session.tenant_id), row[0],
                      settings.deployment, settings.jurisdiction)  # fmt: skip
    body = {
        "deployment": settings.deployment.value,
        "features": [
            {"id": s.feature_id, "enabled": s.enabled,
             "reason_codes": list(s.reason_codes)}
            for s in states
        ],
    }  # fmt: skip
    return Response(json.dumps(body).encode(), 200, media_type="application/json")
