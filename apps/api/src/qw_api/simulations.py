"""Read-only simulation routes (T041 increment 3): `GET /simulations`
(listSimulations) and `GET /simulations/{simulation_id}/results`
(readSimulationResults), over qw_adapters.simulation_store.

Like the T053 reads: membership reads, the session tenant under RLS (another
tenant's simulation is the same 404 as an unknown one), C-02 keyset pages with the
HMAC cursor of qw_api.reads, problem+json errors, nothing written. A stored
simulation that fails its replay is a 500, never an answer.
Simulated, never actual: every summary has `separate_from_actual: true` and each
page names `simulated:separate_from_actual` in coverage. There is no
execution-model or benchmark-policy registry, so those ids are declared labels
(`unverified:`). Fills are stored but listVirtualFills is not served yet.
One simulation that fails its checks makes the whole list a 500.
Results: the simulator produces no calculation records or scorecards yet, so the
results page is empty with `unavailable:scorecard` in coverage, never a figure.
The mutating routes (createSimulation, submitVirtualOrder) are not served: they
need a policy and step-up design (docs/receipts/T041.md). Nothing reaches a broker.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from qw_adapters import simulation_store as ss

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID, Cursor, Limit, _after, _cursor

router = APIRouter(prefix="/api/v1")
SimId = Annotated[str, Path(pattern=ID)]
SIMULATED = "simulated:separate_from_actual"


def summary(s: ss.Simulation) -> dict[str, Any]:
    return {"id": s.simulation_id, "name": s.name,
            "execution_model_id": s.execution_model_id,
            "benchmark_policy_id": s.benchmark_policy_id, "mode": s.mode.value,
            "separate_from_actual": True}  # fmt: skip


def _page(items: list[dict[str, Any]], after: str | None, why: list[str]) -> Response:
    coverage = {"status": "partial", "reasons": [SIMULATED, *why]}
    body = {"items": items, "next_cursor": after, "coverage": coverage}
    return Response(json.dumps(body).encode(), 200, media_type="application/json")


def _one(principal: Reader, simulation_id: str) -> ss.Simulation:
    with principal.tx() as tx:
        s = ss.load(tx, simulation_id)
    if s is None:
        raise ApiError(404, "not_found", "No such simulation.")
    return s


@router.get("/simulations")
def list_simulations(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, "simulation_id", cursor)
    with principal.tx() as tx:
        found = ss.page(tx, limit + 1, after)
    items = [summary(s) for s in found[:limit]]
    more = len(found) > limit
    nxt = (
        _cursor(request, principal, "simulation_id", items[-1]["id"]) if more else None
    )
    why = ["unverified:execution_model_id", "unverified:benchmark_policy_id"]
    return _page(items, nxt, why * bool(items))


@router.get("/simulations/{simulation_id}/results")
def read_results(
    request: Request, principal: Reader, simulation_id: SimId, limit: Limit = 50,
    cursor: Cursor = None,
) -> Response:  # fmt: skip
    _after(request, principal, "calculation_id", cursor)  # no page follows
    _one(principal, simulation_id)
    return _page([], None, ["unavailable:scorecard"])
