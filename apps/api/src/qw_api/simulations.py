"""Read-only simulation routes (T041 increments 3 and 4): `GET /simulations`
(listSimulations), `GET /simulations/{simulation_id}/fills` (listVirtualFills) and
`GET /simulations/{simulation_id}/results` (readSimulationResults), over
qw_adapters.simulation_store.

Like the T053 reads: membership reads, the session tenant under RLS (another
tenant's simulation is the same 404 as an unknown one), C-02 keyset pages with the
HMAC cursor of qw_api.reads, problem+json errors, nothing written. A stored
simulation that fails its replay or digest is a 500, never an answer; so is a
missing or altered result record. One simulation that fails its checks makes the
whole list a 500.
Simulated, never actual: every summary has `separate_from_actual: true`, every fill
`actual_execution: false`, every result record's first warning is
`simulated:separate_from_actual`, and each page names it first in coverage. There
is no execution-model or benchmark-policy registry, so those ids are declared
labels (`unverified:`). Fills name no data record (the bars are not persisted with
the simulation): `data_record_ids` is empty and `unavailable:data_record_ids`.
Results are the calculation records written when the simulation was saved
(simulation_store): ledger figures, and the price-dependent ones as unavailable
records (named in coverage), plus `unavailable:scorecard`; a simulation saved
before results were recorded has none (`unavailable:results_not_recorded`).
Fills and results are sorted by id (byte order) and paged after the cursor's id.
The mutating routes (createSimulation, submitVirtualOrder) are not served: they
need a policy and step-up design (docs/receipts/T041.md). Nothing reaches a broker.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from qw_adapters import simulation_store as ss
from qw_domain import simulation_results as sr
from qw_domain.decimals import Money, Quantity
from qw_domain.instants import format_instant
from qw_domain.simulation import VirtualFill

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


UNVERIFIED_MODEL = "unverified:execution_model_id"
PRICED = [f"unavailable:{n.removeprefix('virtual_')}" for n in sr.PRICED_CODES]


def fill(f: VirtualFill, currency: str) -> dict[str, Any]:
    return {"id": f.fill_id, "simulated_order_id": f.order_id,
            "filled_at": format_instant(f.filled_at),
            "quantity": Quantity(f.quantity).to_wire(),
            "price": Money.of(f.price, currency).to_wire(),
            "fees": Money.of(f.fees, currency).to_wire(), "data_record_ids": [],
            "assumption_ids": list(f.assumptions),
            "execution_model_id": f.execution_model_id,
            "actual_execution": False}  # fmt: skip


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
    why = [UNVERIFIED_MODEL, "unverified:benchmark_policy_id"]
    return _page(items, nxt, why * bool(items))


def _keyset(
    request: Request, principal: Reader, items: list[dict[str, Any]],
    after: str | None, limit: int, why: list[str],
) -> Response:  # fmt: skip
    rest = sorted((i for i in items if after is None or i["id"] > after),
                  key=lambda i: i["id"].encode())  # fmt: skip
    shown = rest[:limit]
    nxt = None
    if len(rest) > limit:
        nxt = _cursor(request, principal, "id", shown[-1]["id"])
    return _page(shown, nxt, why)


@router.get("/simulations/{simulation_id}/fills")
def list_fills(
    request: Request, principal: Reader, simulation_id: SimId, limit: Limit = 50,
    cursor: Cursor = None,
) -> Response:  # fmt: skip
    after = _after(request, principal, "id", cursor)
    s = _one(principal, simulation_id)
    items = [fill(f, s.portfolio.currency) for f in s.fills()]
    why = [UNVERIFIED_MODEL, "unavailable:data_record_ids"] * bool(items)
    return _keyset(request, principal, items, after, limit, why)


@router.get("/simulations/{simulation_id}/results")
def read_results(
    request: Request, principal: Reader, simulation_id: SimId, limit: Limit = 50,
    cursor: Cursor = None,
) -> Response:  # fmt: skip
    after = _after(request, principal, "id", cursor)
    with principal.tx() as tx:
        found = ss.results(tx, simulation_id)
    if found is None:
        raise ApiError(404, "not_found", "No such simulation.")
    items = [r.to_contract() for r in found]
    why = [UNVERIFIED_MODEL, *PRICED, "unavailable:scorecard"]
    if not found:  # saved before results were recorded; a re-save records them
        why = ["unavailable:results_not_recorded"]
    return _keyset(request, principal, items, after, limit, why)
