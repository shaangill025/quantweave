"""Read-only routes for the T053 web UI: `GET /accounts` (listAccounts),
`GET /accounts/{account_id}/snapshot` (readSnapshot),
`GET /accounts/{account_id}/ledger` (listLedger), `GET /proposals` (listProposals)
and `GET /proposals/{proposal_id}` (readProposal), over qw_adapters.journal_store
and proposal_store.

Like `/policies`: membership reads, the session tenant under RLS (another tenant's
account is the same 404 as an unknown one), C-02 keyset pages with an HMAC cursor
bound to tenant, principal, route and sort key (1 h), problem+json errors. Nothing is
written: no audit row, no recorded expiry, no idempotency record.
Wire rule (project contract 0.2.0, contracts/amendments.json): a field no store
holds is null and named in the C-02 `coverage.reasons` as `unavailable:<field>`
(`<id>/<field>` when it varies by item; `unknown:` for a value outside the
contract, `none:` for a known absence); an enum with an `unknown` member uses it
instead. So `coverage` is `partial` whenever an item is returned. There is no
account registry, freshness policy, reconciliation record, review, qualification
or feed binding store yet.
- Accounts are the tenant's journal accounts; `source_ids` are the sources that
  fed each one (journal events and stored source records), never other accounts.
- The snapshot folds the whole journal (`qw_domain.journal.materialize`). Ledger
  cash per currency is the `ledger_cash` extension: trade-date journal cash, not
  available cash (no settlement terms or open orders are persisted, T017), so
  `available_cash` is null. A cost the journal does not know stays null.
- A proposal's state is resolved at read time from stored records
  (`qw_domain.proposals.resolve_recorded`) and the stored pauses (pause_store, the
  domain's `pause_reasons`): an active pause covering a buy or call-cover sale of
  a live proposal adds `RISK_PAUSED` and reads `invalidated` until it is resumed (a
  read projection; no transition is recorded). Risk inputs are not persisted, so
  the evaluation is not re-run: a live state carries `RISK_NOT_RECHECKED`. Each
  proposal carries its own coverage (project contract 0.4.0), as readProposal
  returns one item.
- The ledger pages the verified journal by account revision (`ledger_item`; the
  wire mapping is in docs/receipts/T053-api.md, reads increment 2). A simulation
  id is never a journal account (0014 guard), so it is the unknown-account 404.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response
from qw_adapters import journal_store as js
from qw_adapters import pause_store, policy_store
from qw_adapters import proposal_store as pst
from qw_domain import ledger_wire as lw
from qw_domain import proposals as dp
from qw_domain.decimals import Money
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.journal import Journal, materialize
from qw_domain.policy import PolicyHistory
from qw_domain.postings import Holding
from qw_domain.risk import Side
from qw_domain.risk_pauses import PauseBook

from qw_api import routes
from qw_api import security as sec
from qw_api.app import ApiError, Settings
from qw_api.policies import Reader
from qw_api.security import Principal

router = APIRouter(prefix="/api/v1")
ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=256)]  # a 128-character id fits
ACCOUNT_UNAVAILABLE = ("name", "broker", "account_type", "reporting_currency",
                       "analysis_scope", "status")  # fmt: skip
PROPOSAL_UNAVAILABLE = (
    "origin", "binding/strategy_version_id", "binding/feed_spec_id",
    "binding/entitlement_revision", "binding/portfolio_snapshot_id", "conditions",
    "invalidation_conditions", "claim_ids", "evidence_ids", "calculation_ids",
    "review_id", "final_check_receipt_id", "qualification", "joint_set_id",
)  # fmt: skip
NOT_RECHECKED = "not_rechecked:risk_evaluation"
SNAPSHOT_REASONS = ["unknown:freshness_status", "unavailable:reconciliation",
                    "unavailable:available_cash", "unknown:cash_basis",
                    "unavailable:unresolved_ids"]  # fmt: skip


def _mac(settings: Settings, p: Principal, route: str, sort: str, body: bytes) -> bytes:
    s = p.session
    parts = (str(s.tenant_id), str(s.user_id), route, sort, body.hex())
    return sec.mac(settings, "cursor", *parts)[:16]


def _cursor(request: Request, p: Principal, sort: str, key: str) -> str:
    expires = int(datetime.now(UTC).timestamp()) + routes.CURSOR_TTL
    body = expires.to_bytes(8) + key.encode()
    tag = _mac(sec.settings_of(request), p, request.url.path, sort, body)
    return base64.urlsafe_b64encode(body + tag).decode().rstrip("=")


def _after(request: Request, p: Principal, sort: str, raw: str | None) -> str | None:
    if not raw:  # absent or empty: the first page, as /policies
        return None
    invalid = ApiError(400, "invalid_cursor", "The cursor is not valid here.")
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = base64.b64decode(padded, altchars=b"-_", validate=True)
    except ValueError:
        raise invalid from None
    body, tag = data[:-16], data[-16:]
    expected = _mac(sec.settings_of(request), p, request.url.path, sort, body)
    if len(body) < 9 or not hmac.compare_digest(tag, expected):
        raise invalid
    if int.from_bytes(body[:8]) < int(datetime.now(UTC).timestamp()):
        raise invalid  # expired
    return body[8:].decode()  # the MAC holds, so these are the bytes we encoded


def _page(
    request: Request, p: Principal, sort: str, items: list[dict[str, Any]],
    more: bool, reasons: list[str], last: str | None = None,
) -> Response:  # fmt: skip
    """A C-02 page; the cursor carries `last` (default: the last item's id)."""
    key = last if last is not None else items[-1]["id"] if more else ""
    after = _cursor(request, p, sort, key) if more else None
    status = "partial" if reasons else "complete"
    page = {"items": items, "next_cursor": after,
            "coverage": {"status": status, "reasons": reasons}}  # fmt: skip
    return Response(json.dumps(page).encode(), 200, media_type="application/json")


@router.get("/accounts")
def list_accounts(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, "account_id", cursor)
    with principal.tx() as tx:
        rows = js.list_accounts(tx, limit + 1, after)
    tenant = str(principal.session.tenant_id)
    items = [
        {"id": a, "owner_tenant_id": tenant, **dict.fromkeys(ACCOUNT_UNAVAILABLE),
         "revision": n, "source_ids": list(s), "cash_basis": "unknown"}
        for a, n, s in rows[:limit]
    ]  # fmt: skip
    reasons = [f"unavailable:{f}" for f in ACCOUNT_UNAVAILABLE] + ["unknown:cash_basis"]
    more = len(rows) > limit
    return _page(request, principal, "account_id", items, more, reasons * bool(items))


def position(h: Holding) -> tuple[dict[str, Any], list[str]]:
    """A journal holding on the wire; an unknown cost is null, never zero."""
    iid, cost = str(h.instrument_id.uuid), h.cost
    view = {"instrument_id": iid, "quantity": h.quantity.to_wire(),
            "economic_cost": None if cost is None else cost.to_wire(),
            "lot_coverage": "unknown" if cost is None else "complete"}  # fmt: skip
    return view, [] if cost is not None else [f"unknown:positions/{iid}/economic_cost"]


def snapshot(account_id: str, journal: Journal) -> dict[str, Any]:
    entries = journal.entries()
    revision = journal.revision(account_id)
    folded = materialize(e.event for e in entries)
    positions, reasons = [], list(SNAPSHOT_REASONS)
    for key in sorted(folded.holdings, key=lambda k: str(k[1].uuid)):
        view, why = position(folded.holdings[key])
        positions.append(view)
        reasons += why
    digest = hashlib.sha256(f"{account_id}\x1f{revision}".encode()).hexdigest()
    return {
        "id": f"snap:{digest[:32]}", "account_id": account_id, "revision": revision,
        "effective_at": format_instant(max(e.event.effective_at for e in entries)),
        "received_at": format_instant(max(e.recorded_at for e in entries)),
        "freshness_status": "unknown", "reconciliation": None, "available_cash": None,
        "cash_basis": "unknown", "positions": positions, "unresolved_ids": None,
        "ledger_cash": [m.to_wire() for _, m in sorted(folded.cash.items())],
        "coverage": {"status": "partial", "reasons": reasons},
    }  # fmt: skip


@router.get("/accounts/{account_id}/snapshot")
def read_snapshot(
    principal: Reader, account_id: Annotated[str, Path(pattern=ID)]
) -> Response:
    with principal.tx() as tx:
        journal = js.load_journal(tx, account_id)
    if not journal.entries():
        raise ApiError(404, "not_found", "No such account.")
    body = json.dumps(snapshot(account_id, journal)).encode()
    return Response(body, 200, media_type="application/json")


HORIZONS = frozenset({"long_term", "swing_position", "intraday"})


def horizon(pid: str, value: str) -> tuple[str | None, list[str]]:
    """The contract horizon; a stored value outside its enum is null with a reason."""
    return (value, []) if value in HORIZONS else (None, [f"unknown:{pid}/horizon"])


def proposal(
    p: dp.Proposal, history: PolicyHistory | None, revisions: dict[str, int],
    at: datetime, pauses: PauseBook,
) -> tuple[dict[str, Any], list[str]]:  # fmt: skip
    """The latest version with its read-time state, and per-item reasons."""
    v, a = p.latest, p.latest.action
    adopted = history.adopted if history else None
    current = adopted[0].content_hash if adopted else None
    state, why = dp.resolve_recorded(p, at, revisions, current)
    if p.live and (paused := dp.pause_reasons(a, pauses)):  # with other reasons too
        state, why = dp.ProposalState.INVALIDATED, (*why, *paused)
    codes = [r.code.value for r in why]
    if state not in dp.TERMINAL:
        codes.append("RISK_NOT_RECHECKED")
    versions = history.versions if history else ()
    match = [x.version for x in versions if x.content_hash == v.binding.policy_hash]
    bound = f"{history.policy_id}:{match[0]}" if history and match else None
    cost = Money.of(-v.requirement.amount.value, v.requirement.currency)
    revision = dict(v.binding.account_revisions).get(a.account_id)
    term, notes = horizon(v.proposal_id, a.horizon)
    if a.sleeve_id is None:
        notes.append(f"none:{v.proposal_id}/sleeve_id")
    b = v.binding
    risk = (f"Risk evaluation at binding: {b.risk_outcome.value} (digest "
            f"{b.risk_digest[:16]}); not re-evaluated at read time.")  # fmt: skip
    times = ("trigger_at", "received_at", "created_at", "expires_at")
    binding: dict[str, Any] = {"account_revision": revision,
                               "policy_version_id": bound}  # fmt: skip
    view: dict[str, Any] = {
        "id": v.proposal_id, "version": v.version, "content_hash": v.content_hash,
        "tenant_id": a.tenant_id, "account_id": a.account_id, "sleeve_id": a.sleeve_id,
        "mode": v.mode.value, "state": state.value, "action": a.side.value,
        "horizon": term, "risk_summary": risk,
        **{t: format_instant(getattr(v, t)) for t in times},
        "binding": binding,
        "legs": [{"instrument_id": str(a.instrument.uuid), "side": a.side.value,
                  "quantity": a.quantity.to_wire(), "quantity_basis": "sized",
                  "estimated_cash_effect":
                      cost.to_wire() if a.side is Side.BUY else None}],
        "alternatives_group_id": v.alternatives_group_id,
        "execution_state": p.execution(v.version).value, "reason_codes": codes,
    }  # fmt: skip
    for field in PROPOSAL_UNAVAILABLE:
        parent, _, name = field.rpartition("/")
        (binding if parent else view)[name] = None
    pairs = (("account_revision", revision), ("policy_version_id", bound))
    missing = [f"binding/{f}" for f, x in pairs if x is None]
    return view, notes + [f"unavailable:{v.proposal_id}/{f}" for f in missing]


def _proposal(
    tx: Any, store: pst.ProposalPublisher, now: datetime, pid: str,
    policy: uuid.UUID, held: pause_store.Pauses,
) -> tuple[dict[str, Any], list[str]]:  # fmt: skip
    """The proposal at the database clock with its own coverage (0.4.0), and the
    item reasons for a page."""
    p = store.load(pid)
    assert p is not None
    history = policy_store.load(tx, policy)
    accounts = {k for k, _ in p.latest.binding.account_revisions}
    revisions = {k: js.revision(tx, k) for k in sorted(accounts)}
    book = held.covering(policy, p.latest.action.account_id)  # every policy account
    view, why = proposal(p, history, revisions, max(now, p.events[-1].at), book)
    own = [f"unavailable:{f}" for f in PROPOSAL_UNAVAILABLE] + why
    if "RISK_NOT_RECHECKED" in view["reason_codes"]:
        own.append(NOT_RECHECKED)
    view["coverage"] = {"status": "partial", "reasons": own}
    return view, why


def _now(tx: Any) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


@router.get("/proposals")
def list_proposals(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, "proposal_id", cursor)
    items, reasons = [], [f"unavailable:{f}" for f in PROPOSAL_UNAVAILABLE]
    with principal.tx() as tx:
        rows = pst.list_proposals(tx, limit + 1, after)
        now, store = _now(tx), pst.ProposalPublisher(tx)
        held = pause_store.load(tx)
        for pid, policy_id in rows[:limit]:
            pol = uuid.UUID(str(policy_id))
            view, why = _proposal(tx, store, now, pid, pol, held)
            items.append(view)
            reasons += why
    if any("RISK_NOT_RECHECKED" in i["reason_codes"] for i in items):
        reasons.append(NOT_RECHECKED)
    more = len(rows) > limit
    return _page(request, principal, "proposal_id", items, more, reasons * bool(items))


@router.get("/proposals/{proposal_id}")
def read_proposal(
    principal: Reader, proposal_id: Annotated[str, Path(pattern=ID)]
) -> Response:
    with principal.tx() as tx:
        policy = pst.policy_of(tx, proposal_id)
        if policy is None:  # unknown, or another tenant's under RLS
            raise ApiError(404, "not_found", "No such proposal.")
        view, _ = _proposal(tx, pst.ProposalPublisher(tx), _now(tx), proposal_id,
                            policy, pause_store.load(tx))  # fmt: skip
    return Response(json.dumps(view).encode(), 200, media_type="application/json")


# Journal kinds on the ledger_event wire (a reversal is the spec's correction) and
# the item itself live in qw_domain.ledger_wire, which the T057 export shares.
KINDS, ledger_item = lw.KINDS, lw.item
LEDGER_REASONS = ["unavailable:lot_policy_id",
                  "unreconciled:status_is_journal_acceptance"]  # fmt: skip


@router.get("/accounts/{account_id}/ledger")
def list_ledger(
    request: Request, principal: Reader, account_id: Annotated[str, Path(pattern=ID)],
    limit: Limit = 50, cursor: Cursor = None,
) -> Response:  # fmt: skip
    after = _after(request, principal, "account_rev", cursor)
    with principal.tx() as tx:
        journal = js.load_journal(tx, account_id)
    entries = journal.entries()  # in account_rev order; revisions are gapless from 1
    if not entries:  # unknown, another tenant's, or a simulation (never a ledger)
        raise ApiError(404, "not_found", "No such account.")
    start = 0 if after is None else int(after)
    reversed_ids = {x.event.reverses for x in entries if x.event.reverses}
    tenant, items, reasons = str(principal.session.tenant_id), [], []
    for entry in entries[start : start + limit]:
        view, why = ledger_item(tenant, entry, reversed_ids)
        items.append(view)
        reasons += why
    more = start + limit < len(entries)
    last = str(start + len(items))
    page_reasons = (LEDGER_REASONS + reasons) * bool(items)
    return _page(request, principal, "account_rev", items, more, page_reasons, last)
