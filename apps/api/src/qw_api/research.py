"""Read-only research routes for the T054 views: `GET /calculations/{calculation_id}`
(readCalculation) over qw_adapters.calculation_store.

Like the T053 reads: membership reads, the session tenant under RLS (another
tenant's calculation is the same 404 as an unknown one), problem+json errors, and
nothing written. The body is the domain's contract projection
(`CalculationRecord.to_contract`), which fills every field of the spec calculation
schema; what the schema cannot hold (as-of, rounding, coverage, date and reference
inputs, input as-of times, the unavailable code) is named in `warnings`. A stored
record that fails its hash or domain checks is a 500, never an answer.
Claims, evidence, option terms and simulations have contract routes but no store
yet, so they are not served here (docs/receipts/T054-api.md).
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Path, Response
from qw_adapters import calculation_store as store

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
