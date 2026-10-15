"""Watchlist routes (T024 increments 2 and 3) over qw_adapters.watchlist_store:
`GET /watchlists` (listWatchlists), `POST /watchlists` (createWatchlist),
`GET /watchlists/{id}` (readWatchlist), `PATCH` (updateWatchlist), `DELETE`
(deleteWatchlist), `POST /watchlists/{id}/entries` (addWatchlistEntry), and
`PATCH`/`DELETE /watchlists/{id}/entries/{instrument_id}` (updateWatchlistEntry,
deleteWatchlistEntry).

Any member reads and writes the session tenant's watchlists (RLS); an unknown,
foreign, deleted or non-canonical id is the same 404. Lists are C-02 keyset pages
by id with the qw_api.reads HMAC cursor (tenant, principal, route, sort key; 1 h).
Writes need CSRF and Idempotency-Key (C-03); update and delete need If-Match `"N"`
(T010 form; 428 absent, 412 stale or malformed; the openapi text says 409, as
recorded for T019). Bodies are the contract `CreateWatchlist`, bounded (name 1-200,
description up to 4000 characters, no NUL). Responses are the contract `watchlist`
with ETag `"N"`; it has no description field, so the description is stored, not
served. Delete appends a tombstone revision and answers the 202 `job` as
`succeeded` (synchronous; id `<watchlist id>.deleted`, not pollable: readJob is not
served).

Entry operations need CSRF and Idempotency-Key. Update and delete need If-Match on
the list's revision (428/412). addWatchlistEntry follows its contract, which names
no If-Match: one sent is honoured (412 when stale or malformed); without one the
latest revision is used under the lock, and a key replays on the body alone.
Bodies are the contract request `WatchlistEntry`, closed: `review_at` and `needs`
are refused. A new entry has `review_at` null and the domain's default `needs`; an
update keeps both. Conditions
and the instrument id (a canonical UUID; no instrument registry exists to check
against) must be accepted by the domain, else 422 `invalid_entry`. The path and body
instrument must agree (400 `instrument_mismatch`). Served entries are the contract
response entry (`review_at`, never `needs`). Stored revisions failing checks are
409 `integrity`, never served. Nothing here reads or writes accounts, the journal or
holdings (AT057): an entry is not proof of ownership.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Request, Response
from pydantic import Field
from qw_adapters import watchlist_store as store
from qw_adapters.tenancy import TenantTx
from qw_domain.identity import IdentityError, InstrumentId
from qw_domain.instants import format_instant
from qw_domain.monitoring import Condition, MonitoringError
from qw_domain.watchlists import Entry, WatchlistError

from qw_api import routes
from qw_api import security as sec
from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import ID, Cursor, Limit, _after, _page

router = APIRouter(prefix="/api/v1")
SORT = "watchlist_id"
_IF_MATCH = re.compile(r'"([1-9][0-9]{0,8})"')
_TEXT = r"^[^\x00]*$"
WatchlistId = Annotated[str, Path(pattern=ID)]
InstrumentPath = Annotated[str, Path(pattern=ID)]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
_REFUSED = {"not_found": (404, "No such watchlist."),
            "stale": (412, "The watchlist has changed."),
            "idempotency_key_reused": (409, "Key used elsewhere."),
            "entry_missing": (404, "No such entry."),
            "entry_exists": (409, "The instrument is already on the list."),
            "entry_limit": (409, "The list holds the maximum number of entries."),
            "integrity": (409, "Stored watchlists failed checks.")}  # fmt: skip


class CreateWatchlist(routes.Body):
    name: str = Field(min_length=1, max_length=200, pattern=_TEXT)
    description: str = Field(max_length=4000, pattern=_TEXT)


Text = Annotated[str, Field(max_length=4000, pattern=_TEXT)]
Short = Annotated[str, Field(max_length=128, pattern=_TEXT)]


class WireCondition(routes.Body):
    metric: str = Field(pattern=ID)
    operator: str = Field(pattern="^(gt|gte|lt|lte|eq|between|in|not_in)$")
    values: list[Short] = Field(max_length=8)
    data_spec_id: str = Field(pattern=ID)
    description: Text


class WatchlistEntry(routes.Body):
    instrument_id: str = Field(pattern=ID)
    thesis: Text
    tags: list[Short] = Field(max_length=32)
    monitoring_priority: int = Field(ge=0, le=100)
    conditions: list[WireCondition] = Field(max_length=16)
    invalidation_text: Text

    def domain(self) -> Entry:
        """New entries: `review_at` None, the domain's default `needs`."""
        try:
            return Entry(
                InstrumentId.from_wire(self.instrument_id), self.thesis,
                tuple(self.tags), None, self.monitoring_priority,
                tuple(Condition(c.metric, c.operator, tuple(c.values), c.data_spec_id,
                                c.description) for c in self.conditions),
                self.invalidation_text,
            )  # fmt: skip
        except (IdentityError, MonitoringError, WatchlistError):
            detail = "The domain does not accept this entry."
            raise ApiError(422, "invalid_entry", detail) from None


def _call[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except store.WatchlistStoreError as exc:
        if exc.code not in _REFUSED:
            raise  # database: a crash (500), never a client answer
        status, detail = _REFUSED[exc.code]
        code = {412: "version_mismatch", 404: "not_found"}.get(status, exc.code)
        raise ApiError(status, code, detail) from None


def _expected(if_match: str | None) -> int:
    if if_match is None:
        raise ApiError(428, "if_match_required", "Send If-Match with the revision.")
    if (m := _IF_MATCH.fullmatch(if_match)) is None:
        raise ApiError(412, "version_mismatch", _REFUSED["stale"][1])
    return int(m[1])


def _wire(r: store.Revision) -> dict[str, Any]:
    w = r.watchlist
    entries = [{k: v for k, v in store.entry_json(e).items() if k != "needs"}
               for e in w.entries]  # fmt: skip
    return {"id": w.watchlist_id, "revision": w.revision, "name": w.name,
            "entries": entries}  # fmt: skip


def _tagged(response: Response) -> Response:
    revision = json.loads(bytes(response.body))["revision"]
    response.headers["ETag"] = f'"{revision}"'
    return response


@router.get("/watchlists")
def list_watchlists(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, SORT, cursor)
    with principal.tx() as tx:
        rows = _call(lambda: store.page(tx, limit + 1, after))
    items = [_wire(r) for r in rows[:limit]]
    return _page(request, principal, SORT, items, len(rows) > limit, [])


@router.get("/watchlists/{watchlist_id}")
def read_watchlist(principal: Reader, watchlist_id: WatchlistId) -> Response:
    with principal.tx() as tx:
        found = _call(lambda: store.load(tx, watchlist_id))
    if found is None:
        raise ApiError(404, "not_found", _REFUSED["not_found"][1])
    body = json.dumps(_wire(found)).encode()
    return _tagged(Response(body, 200, media_type="application/json"))


@router.post("/watchlists", status_code=201)
def create_watchlist(
    body: CreateWatchlist, request: Request, principal: Reader, key: routes.IdemKey
) -> Response:
    s = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        r, wrote = _call(
            lambda: store.create(
                tx, s.user_id, s.session_id, key, body.name, body.description
            )
        )
        if wrote:
            routes._audit(tx, request, "watchlist.created", s.user_id,
                          uuid.UUID(r.watchlist.watchlist_id))  # fmt: skip
        return 201, json.dumps(_wire(r)).encode()

    payload = {"body": body.model_dump(mode="json")}
    return _tagged(sec.idempotent(principal, request, key, payload, run))


@router.patch("/watchlists/{watchlist_id}")
def update_watchlist(
    watchlist_id: WatchlistId, body: CreateWatchlist, request: Request,
    principal: Reader, key: routes.IdemKey, if_match: IfMatch = None,
) -> Response:  # fmt: skip
    s = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        expected = _expected(if_match)
        r, wrote = _call(lambda: store.update(tx, watchlist_id, s.user_id,
                                              s.session_id, key, expected, body.name,
                                              body.description))  # fmt: skip
        if wrote:
            routes._audit(tx, request, "watchlist.updated", s.user_id,
                          uuid.UUID(watchlist_id))  # fmt: skip
        return 200, json.dumps(_wire(r)).encode()

    payload = {"if_match": if_match, "body": body.model_dump(mode="json")}
    return _tagged(sec.idempotent(principal, request, key, payload, run))


@router.delete("/watchlists/{watchlist_id}", status_code=202)
def delete_watchlist(
    watchlist_id: WatchlistId, request: Request, principal: Reader,
    key: routes.IdemKey, if_match: IfMatch = None,
) -> Response:  # fmt: skip
    s = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        expected = _expected(if_match)
        r, wrote = _call(
            lambda: store.delete(
                tx, watchlist_id, s.user_id, s.session_id, key, expected
            )
        )
        if wrote:
            routes._audit(tx, request, "watchlist.deleted", s.user_id,
                          uuid.UUID(watchlist_id))  # fmt: skip
        job = {"id": f"{watchlist_id}.deleted", "kind": "watchlist.delete",
               "state": "succeeded", "created_at": format_instant(r.recorded_at),
               "attempt": 1, "idempotency_key": key, "deadline_at": None,
               "result_reference": watchlist_id, "reasons": []}  # fmt: skip
        return 202, json.dumps(job).encode()

    return sec.idempotent(principal, request, key, {"if_match": if_match}, run)


def _optional(if_match: str | None) -> int | None:
    """addWatchlistEntry: the contract names no If-Match; one sent is honoured."""
    return None if if_match is None else _expected(if_match)


def _entry_write[N](
    request: Request, principal: Reader, key: str, if_match: str | None,
    event: str, watchlist_id: str, payload: dict[str, Any],
    expected_of: Callable[[str | None], N],
    change: Callable[[TenantTx, N], tuple[store.Revision, bool]],
) -> Response:  # fmt: skip
    s = principal.session

    def run(tx: TenantTx) -> tuple[int, bytes]:
        expected = expected_of(if_match)
        r, wrote = _call(lambda: change(tx, expected))
        if wrote:
            routes._audit(tx, request, event, s.user_id,
                          uuid.UUID(watchlist_id))  # fmt: skip
        return 200, json.dumps(_wire(r)).encode()

    return _tagged(sec.idempotent(principal, request, key, payload, run))


@router.post("/watchlists/{watchlist_id}/entries")
def add_watchlist_entry(
    watchlist_id: WatchlistId, body: WatchlistEntry, request: Request,
    principal: Reader, key: routes.IdemKey, if_match: IfMatch = None,
) -> Response:  # fmt: skip
    s = principal.session

    def change(tx: TenantTx, expected: int | None) -> tuple[store.Revision, bool]:
        return store.add_entry(tx, watchlist_id, s.user_id, s.session_id, key,
                               expected, body.domain())  # fmt: skip

    return _entry_write(request, principal, key, if_match, "watchlist.entry_added",
                        watchlist_id, {"body": body.model_dump(mode="json")},
                        _optional, change)  # fmt: skip


@router.patch("/watchlists/{watchlist_id}/entries/{instrument_id}")
def update_watchlist_entry(
    watchlist_id: WatchlistId, instrument_id: InstrumentPath, body: WatchlistEntry,
    request: Request, principal: Reader, key: routes.IdemKey,
    if_match: IfMatch = None,
) -> Response:  # fmt: skip
    s = principal.session

    def change(tx: TenantTx, expected: int) -> tuple[store.Revision, bool]:
        if body.instrument_id != instrument_id:
            raise ApiError(400, "instrument_mismatch",
                           "The path and body name different instruments.")  # fmt: skip
        return store.update_entry(tx, watchlist_id, s.user_id, s.session_id, key,
                                  expected, body.domain())  # fmt: skip

    return _entry_write(request, principal, key, if_match, "watchlist.entry_updated",
                        watchlist_id, {"if_match": if_match,
                                       "body": body.model_dump(mode="json")},
                        _expected, change)  # fmt: skip


@router.delete("/watchlists/{watchlist_id}/entries/{instrument_id}")
def delete_watchlist_entry(
    watchlist_id: WatchlistId, instrument_id: InstrumentPath, request: Request,
    principal: Reader, key: routes.IdemKey, if_match: IfMatch = None,
) -> Response:  # fmt: skip
    s = principal.session

    def change(tx: TenantTx, expected: int) -> tuple[store.Revision, bool]:
        try:
            iid = InstrumentId.from_wire(instrument_id)
        except IdentityError:  # another spelling: no such entry
            raise ApiError(404, "not_found", _REFUSED["entry_missing"][1]) from None
        return store.remove_entry(tx, watchlist_id, s.user_id, s.session_id, key,
                                  expected, iid)  # fmt: skip

    return _entry_write(request, principal, key, if_match, "watchlist.entry_removed",
                        watchlist_id, {"if_match": if_match}, _expected,
                        change)  # fmt: skip
