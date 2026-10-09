"""PostgreSQL store for the financial journal and source records (T012 increment 2;
`0007_journal.sql`; spec §4; T008 C-12, C-13, F-01, F-02, F-05).

The database enforces the invariants (append-only, balance per commodity, exact
reversals, supersede-after-reversal, idempotency keys, decimal scale, server
knowledge time); this module adds the domain's account-level rules by replaying the
account into a `qw_domain.journal.Journal` under the per-account advisory lock the
insert trigger also takes, then validating the new event on it (short-sale guard,
links in the same account, conflicts). Every read recomputes each `event_id` from
the stored content and refuses a mismatch, so a row written around this module (or
altered numerically) cannot load silently.

`post` and `correct` run in the caller's `TenantTx`. A repeat of the same event is
`Outcome.DUPLICATE`; another event under a held key raises `JournalConflict` (the
caller's transaction then rolls back). The trigger assigns `account_rev` (gapless,
visible in assignment order) and `recorded_at`; only `account_rev` is a stable
as-of key (`as_of_rev`, `revision`). `known_as_of` filters by recorded_at, which is
informational: such a read can later gain an event stamped <= t.
LIMITATIONS: each write replays the whole account (O(events)), so importing n events
in one transaction costs O(n^2) (T014); a caller writing several accounts in one
transaction must lock them in sorted order, or opposite orders can deadlock; events
carry a source reference but no foreign key to `source_record` yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from psycopg.types.json import Jsonb
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, MoneyAmount, Quantity
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc
from qw_domain.journal import Journal, Outcome
from qw_domain.postings import (
    BookAccount,
    EventKind,
    JournalEvent,
    MoneyPosting,
    SourceObservation,
    UnitAccount,
    UnitPosting,
)

from qw_adapters.tenancy import TenantTx


class JournalConflict(Exception):
    """The idempotency key is held by a record with other content."""

    def __init__(self, key: tuple[str, ...], existing: str) -> None:
        super().__init__(f"key {key} is held by {existing}")
        self.key, self.existing = key, existing


class JournalIntegrityError(Exception):
    """Stored rows do not reproduce their event id or violate a domain rule."""


def lock_account(tx: TenantTx, account_id: str) -> None:
    """Serialise writers of one account until the transaction ends (same key as the
    `app.ledger_event_stamp` trigger)."""
    tx.conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"ledger:{tx.tenant_id}:{account_id}",),
    )


def load_journal(
    tx: TenantTx,
    account_id: str,
    known_as_of: datetime | None = None,
    as_of_rev: int | None = None,
) -> Journal:
    """The account's journal at revision `as_of_rev` (events with account_rev below
    it; see `revision`) and recorded by `known_as_of`, in revision order, with every
    event id verified."""
    t = None if known_as_of is None else ensure_aware_utc(known_as_of)
    where = "tenant_id = %s AND account_id = %s"
    args: tuple[object, ...] = (tx.tenant_id, account_id)
    headers = tx.conn.execute(
        "SELECT event_id, kind::text, effective_at, recorded_at, source_id, "
        "source_record_id, reverses, supersedes, lot_policy, terms FROM "
        f"app.ledger_event WHERE {where} AND (%s::timestamptz IS NULL OR "
        "recorded_at <= %s) AND (%s::bigint IS NULL OR account_rev < %s) "
        "ORDER BY account_rev",
        (*args, t, t, as_of_rev, as_of_rev),
    ).fetchall()
    money: defaultdict[str, list[MoneyPosting]] = defaultdict(list)
    units: defaultdict[str, list[UnitPosting]] = defaultdict(list)
    ids = [h[0] for h in headers]
    for eid, acct, cur, wire, iid in tx.conn.execute(
        "SELECT event_id, book_account::text, currency, wire, instrument_id FROM "
        "app.posting WHERE tenant_id = %s AND event_id = ANY(%s) ORDER BY event_id, "
        "line",
        (tx.tenant_id, ids),
    ):
        instrument = None if iid is None else InstrumentId(iid)
        money[eid].append(
            MoneyPosting(BookAccount(acct), Money(MoneyAmount(wire), cur), instrument)
        )
    for eid, acct, iid, wire in tx.conn.execute(
        "SELECT event_id, unit_account::text, instrument_id, wire FROM "
        "app.unit_posting WHERE tenant_id = %s AND event_id = ANY(%s) "
        "ORDER BY event_id, line",
        (tx.tenant_id, ids),
    ):
        units[eid].append(
            UnitPosting(UnitAccount(acct), InstrumentId(iid), Quantity(wire))
        )
    journal = Journal()
    for eid, kind, eff, rec, src, rec_id, rev, sup, policy, terms in headers:
        fee = terms.get("fee")
        event = JournalEvent(
            EventKind(kind), account_id, eff, SourceRef(src, rec_id),
            tuple(money[eid]), tuple(units[eid]),
            None if fee is None else Money.from_wire(fee), rev, sup, policy,
        )  # fmt: skip
        if event.event_id != eid:
            raise JournalIntegrityError(f"stored event {eid} does not reproduce")
        if journal.post(event, rec) is not Outcome.POSTED:
            raise JournalIntegrityError(f"stored event {eid} does not replay")
    return journal


def _insert(tx: TenantTx, event: JournalEvent) -> None:
    terms = {} if event.fee is None else {"fee": event.fee.to_wire()}
    tx.conn.execute(
        "INSERT INTO app.ledger_event (tenant_id, account_id, event_id, kind, "
        "effective_at, source_id, source_record_id, reverses, supersedes, "
        "lot_policy, terms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (tx.tenant_id, event.account_id, event.event_id, event.kind.value,
         event.effective_at, event.source.source_id, event.source.record_id,
         event.reverses, event.supersedes, event.lot_policy, Jsonb(terms)),
    )  # fmt: skip
    with tx.conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO app.posting (tenant_id, event_id, line, book_account, "
            "currency, wire, instrument_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    tx.tenant_id,
                    event.event_id,
                    i,
                    p.account.value,
                    p.amount.currency,
                    p.amount.amount.to_wire(),
                    None if p.instrument_id is None else p.instrument_id.uuid,
                )
                for i, p in enumerate(event.money)
            ],
        )
        cur.executemany(
            "INSERT INTO app.unit_posting (tenant_id, event_id, line, unit_account, "
            "instrument_id, wire) VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (
                    tx.tenant_id,
                    event.event_id,
                    i,
                    p.account.value,
                    p.instrument_id.uuid,
                    p.quantity.to_wire(),
                )
                for i, p in enumerate(event.units)
            ],
        )


def _prepare(tx: TenantTx, account_id: str) -> tuple[Journal, datetime]:
    lock_account(tx, account_id)
    journal = load_journal(tx, account_id)
    row = tx.conn.execute(
        "SELECT greatest(clock_timestamp(), max(recorded_at)) FROM app.ledger_event "
        "WHERE tenant_id = %s AND account_id = %s",
        (tx.tenant_id, account_id),
    ).fetchone()
    assert row is not None
    return journal, row[0]


def _raise_conflict(journal: Journal) -> None:
    c = journal.conflicts[-1]
    raise JournalConflict(c.key, c.existing)


def post(tx: TenantTx, event: JournalEvent) -> Outcome:
    """Validate `event` against the stored account and insert it (POSTED), or
    DUPLICATE when the same event is held; raises `JournalConflict` or a domain
    `JournalError`."""
    journal, now = _prepare(tx, event.account_id)
    outcome = journal.post(event, now)
    if outcome is Outcome.CONFLICT:
        _raise_conflict(journal)
    if outcome is Outcome.POSTED:
        _insert(tx, event)
    return outcome


def correct(tx: TenantTx, replacement: JournalEvent) -> Outcome:
    """Reverse `replacement.supersedes` and post the replacement in this transaction
    (the domain's `Journal.correct`): both rows or neither."""
    journal, now = _prepare(tx, replacement.account_id)
    before = {e.event.event_id for e in journal.entries()}
    outcome = journal.correct(replacement, now)
    if outcome is Outcome.CONFLICT:
        _raise_conflict(journal)
    for entry in journal.entries():
        if entry.event.event_id not in before:
            _insert(tx, entry.event)
    return outcome


def observe(tx: TenantTx, obs: SourceObservation) -> Outcome:
    """Store an imported record once: POSTED, DUPLICATE for the same fingerprint, or
    `JournalConflict` for other content under the same (account, source, record)."""
    row = tx.conn.execute(
        "INSERT INTO app.source_record (tenant_id, account_id, source_id, record_id, "
        "observed_at, effective_at, payload, fingerprint) VALUES (%s, %s, %s, %s, "
        "%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING fingerprint",
        (tx.tenant_id, obs.account_id, obs.source.source_id, obs.source.record_id,
         obs.observed_at, obs.effective_at, Jsonb(dict(obs.payload)),
         obs.fingerprint),
    ).fetchone()  # fmt: skip
    if row is not None:
        return Outcome.POSTED
    held = tx.conn.execute(
        "SELECT fingerprint FROM app.source_record WHERE tenant_id = %s AND "
        "account_id = %s AND source_id = %s AND record_id = %s",
        (tx.tenant_id, *obs.key),
    ).fetchone()
    assert held is not None
    if held[0] != obs.fingerprint:
        raise JournalConflict(obs.key, str(held[0]))
    return Outcome.DUPLICATE


def revision(tx: TenantTx, account_id: str) -> int:
    """1 + the account's latest `account_rev`; equals `Journal.revision` of the
    loaded journal (T008 C-04, T014 N3). Lock the account to hold it."""
    row = tx.conn.execute(
        "SELECT coalesce(max(account_rev), 0) + 1 FROM app.ledger_event "
        "WHERE tenant_id = %s AND account_id = %s",
        (tx.tenant_id, account_id),
    ).fetchone()
    assert row is not None
    return int(row[0])
