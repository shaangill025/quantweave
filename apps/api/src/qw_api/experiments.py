"""Experiment reads (T044 increment 2) over qw_adapters.experiment_store:
`GET /experiments` (listExperiments) and `GET /experiments/{id}` (readExperiment).
No create, cancel or promote operation is served: the worker records experiments.

Any member reads the session tenant's experiments (forced RLS); unknown and foreign
ids are the same 404. Lists are C-02 keyset pages by id with the qw_api.reads HMAC
cursor. Items are the project contract 0.6.0 `experiment`:
`requires_human_promotion` true, `evaluation_receipt_ids` empty (no receipt store),
`next_cycle_run_id` and `release_receipt_id` null (T051), `parent_optimizer_version`
null except for the optimizer (named `none:parent_optimizer_version`). Each item
carries its coverage: the stored `<kind>:<detail>` reasons the schema cannot carry
(for example `blocked:sandbox_unqualified`, `evidence:synthetic_only`), `partial`
when there is any. The page repeats them as `<kind>:<id>/<detail>` (at most 300
characters). readExperiment serves a blocked or incomplete record with its reasons;
records stored under 0.5.0 are served as stored (`failed`,
`unavailable:parent_optimizer_version`). Revisions failing checks are 409
`integrity`."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request, Response
from qw_adapters import experiment_store as store
from qw_domain.improvement import ExperimentRecord

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID, Cursor, Limit, _after, _page

router = APIRouter(prefix="/api/v1")
SORT = "experiment_id"
ExperimentId = Annotated[str, Path(pattern=ID)]
_INTERNAL = ("experiment_id", "verdict_id", "decision", "reasons")


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except store.ExperimentStoreError as exc:
        if exc.code != "integrity":
            raise  # database: a crash (500), never a client answer
        raise ApiError(409, "integrity", "Stored experiments failed checks.") from None


def wire(rec: ExperimentRecord) -> dict[str, Any]:
    body = {k: v for k, v in rec.wire().items() if k not in _INTERNAL}
    return {"id": rec.experiment_id, **body, "evaluation_receipt_ids": [],
            "next_cycle_run_id": None,
            "requires_human_promotion": rec.requires_human_promotion,
            "release_receipt_id": None, "coverage": {
                "status": "partial" if rec.reasons else "complete",
                "reasons": list(rec.reasons)}}  # fmt: skip


def reasons(rec: ExperimentRecord) -> list[str]:
    return [f"{kind}:{rec.experiment_id}/{detail}"
            for kind, detail in (r.split(":", 1) for r in rec.reasons)]  # fmt: skip


@router.get("/experiments")
def list_experiments(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, SORT, cursor)
    with principal.tx() as tx:
        rows = _call(lambda: store.page(tx, limit + 1, after))
    shown, more = [r.record for r in rows[:limit]], len(rows) > limit
    items, why = [wire(r) for r in shown], [x for r in shown for x in reasons(r)]
    return _page(request, principal, SORT, items, more, why,
                 shown[-1].experiment_id if more else None)  # fmt: skip


@router.get("/experiments/{experiment_id}")
def read_experiment(principal: Reader, experiment_id: ExperimentId) -> Response:
    with principal.tx() as tx:
        found = _call(lambda: store.load(tx, experiment_id))
    if found is None:
        raise ApiError(404, "not_found", "No such experiment.")
    body = json.dumps(wire(found.record)).encode()
    return Response(body, 200, media_type="application/json")
