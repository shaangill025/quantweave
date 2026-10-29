"""The contract LedgerEvent wire of a journal entry (`ledger_event.schema.json`,
project contract 0.4.0: `lot_policy_id` nullable). listLedger serves it
(`qw_api.reads`, T053; moved here unchanged) and the T057 export writes it as
`financial/journal.json`. `parse_section` is its inverse.
- A reversal is the spec's `correction` (spec §4); a kind missing from `KINDS` or
  `MAPPED` fails closed (KeyError), never a dropped event.
- Money lines keep their currency as commodity (an instrument sub-ledger is
  `<book_account>:<instrument>`); unit lines use the instrument id as commodity. The
  two sets are never summed together, and the book and unit account names are
  disjoint, so each line decodes to exactly one kind.
- The fee, lot policy and supersede link are `economic_annotations`.
- `effective_at` (the source's time) and `recorded_at` (knowledge time) are kept as
  stored: nothing is re-stamped.
- Only journal events are on this wire: no source observation, claimed execution,
  prediction or virtual (simulation) record has a field here.
`parse_section` rebuilds each event, refuses unless re-encoding it gives the row
exactly (so `id`, the idempotency key, movements, annotations, decimals and `Z`
instants are canonical), replays each account in order like the store's
`load_journal`, and checks `status` against the reversals in the section.
"""

from collections.abc import Mapping, Sequence, Set
from typing import Any

from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, MoneyAmount, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import format_instant, parse_instant
from qw_domain.journal import Entry, Journal, Outcome
from qw_domain.postings import (
    BookAccount,
    EventKind,
    JournalEvent,
    MoneyPosting,
    UnitAccount,
    UnitPosting,
)

# Identity map on purpose: it exists so a new EventKind fails closed.
KINDS = {k.value: k.value for k in EventKind if k is not EventKind.REVERSAL}
MAPPED = {EventKind.REVERSAL.value: "correction"}
KEYS = frozenset({"id", "account_id", "tenant_id", "kind", "effective_at",
                  "recorded_at", "source_id", "source_record_id", "idempotency_key",
                  "status", "reverses_event_id", "postings", "instrument_movements",
                  "lot_policy_id", "economic_annotations"})  # fmt: skip
POSTING_KEYS = frozenset({"book_account", "commodity", "amount"})
MOVEMENT_KEYS = frozenset({"instrument_id", "quantity"})
_UNIT = frozenset(a.value for a in UnitAccount)
_KIND = {v: EventKind(k) for k, v in (KINDS | MAPPED).items()}


class WireError(ValueError):
    """A row that is not exactly the wire of a valid, replayable journal event."""


def _line(account: str, commodity: str, amount: str) -> dict[str, str]:
    return {"book_account": account, "commodity": commodity, "amount": amount}


def item(
    tenant: str, entry: Entry, reversed_ids: Set[str]
) -> tuple[dict[str, Any], list[str]]:
    """One journal event on the wire, and the coverage reasons it adds."""
    e, why = entry.event, []
    kind = KINDS.get(e.kind.value)
    if kind is None:
        kind = MAPPED[e.kind.value]
        why.append(f"mapped:{e.event_id}/kind={e.kind.value}->{kind}")
    postings = [
        _line(p.account.value if p.instrument_id is None
              else f"{p.account.value}:{p.instrument_id.to_wire()}",
              p.amount.currency, p.amount.amount.to_wire())
        for p in e.money
    ] + [_line(u.account.value, u.instrument_id.to_wire(), u.quantity.to_wire())
         for u in e.units]  # fmt: skip
    moves = [{"instrument_id": u.instrument_id.to_wire(),
              "quantity": u.quantity.to_wire()}
             for u in e.units if u.account is UnitAccount.POSITION]  # fmt: skip
    notes = [] if e.fee is None else [f"fee:{e.fee.amount.to_wire()} {e.fee.currency}"]
    notes.append(f"lot_policy:{e.lot_policy}")
    if e.supersedes is not None:
        notes.append(f"supersedes:{e.supersedes}")
    key = (f"reversal/{e.reverses}" if e.reverses is not None
           else f"record/{e.source.source_id}/{e.source.record_id}")  # fmt: skip
    return {
        "id": e.event_id, "account_id": e.account_id, "tenant_id": tenant,
        "kind": kind, "effective_at": format_instant(e.effective_at),
        "recorded_at": format_instant(entry.recorded_at),
        "source_id": e.source.source_id, "source_record_id": e.source.record_id,
        "idempotency_key": key,
        "status": "reversed" if e.event_id in reversed_ids else "accepted",
        "reverses_event_id": e.reverses, "postings": postings,
        "instrument_movements": moves, "lot_policy_id": None,
        "economic_annotations": notes,
    }, why  # fmt: skip


def _event(r: Mapping[str, Any]) -> JournalEvent:
    money: list[MoneyPosting] = []
    units: list[UnitPosting] = []
    for p in r["postings"]:
        acct, commodity, amount = p["book_account"], p["commodity"], p["amount"]
        if acct in _UNIT:
            iid, qty = InstrumentId.from_wire(commodity), Quantity.from_wire(amount)
            units.append(UnitPosting(UnitAccount(acct), iid, qty))
            continue
        base, _, sub = acct.partition(":")
        inst = InstrumentId.from_wire(sub) if sub else None
        value = Money(MoneyAmount.from_wire(amount), commodity)
        money.append(MoneyPosting(BookAccount(base), value, inst))
    fee, lot, sup = None, "", None
    for note in r["economic_annotations"]:
        tag, _, rest = note.partition(":")
        if tag == "fee":
            amount, _, cur = rest.partition(" ")
            fee = Money(MoneyAmount.from_wire(amount), cur)
        elif tag in ("lot_policy", "supersedes"):
            lot, sup = (rest, sup) if tag == "lot_policy" else (lot, rest)
        else:
            raise WireError(f"annotation {tag}")
    return JournalEvent(_KIND[r["kind"]], r["account_id"],
                        parse_instant(r["effective_at"]),
                        SourceRef(r["source_id"], r["source_record_id"]),
                        tuple(money), tuple(units), fee, r["reverses_event_id"], sup,
                        lot)  # fmt: skip


def parse_section(rows: Sequence[Mapping[str, Any]]) -> list[Entry]:
    """The entries of a `financial/journal.json` body; raises `WireError`."""
    journals: dict[str, Journal] = {}
    entries: list[Entry] = []
    for n, r in enumerate(rows):
        try:
            entry = Entry(_event(r), parse_instant(r["recorded_at"]))
            held = {entry.event.event_id} if r["status"] == "reversed" else set()
            same = item(r["tenant_id"], entry, held)[0] == r
            journal = journals.setdefault(entry.event.account_id, Journal())
            posted = same and journal.post(entry.event, entry.recorded_at)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            raise WireError(f"row {n}: not a journal event") from None
        if not same:
            raise WireError(f"row {n}: not canonical")
        if posted is not Outcome.POSTED:
            raise WireError(f"row {n}: does not replay")
        entries.append(entry)
    gone = {e.event.reverses for e in entries if e.event.reverses}
    for n, (r, e) in enumerate(zip(rows, entries, strict=True)):
        if (r["status"] == "reversed") != (e.event.event_id in gone):
            raise WireError(f"row {n}: status")
    return entries
