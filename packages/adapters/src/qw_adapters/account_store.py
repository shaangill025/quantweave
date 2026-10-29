"""Accounts, sources and source-mapping revisions (T013 increments 2 and 3;
`0055_accounts_sources.sql`, `0056_account_lifecycle.sql`; spec §4; data
dictionary account, source_connection, account_source_binding; contract Account;
owner decisions 101, 102 and 103).

- `account_from_text` is the boundary for caller text: it NFC-normalizes `broker`
  and `account_type` (decision 103) and builds the domain value; codes are the
  domain's (`not_nfc` can then only come from a value built elsewhere).
- `register_account` stores a domain `UnderlyingAccount` (broker = `institution`,
  reporting currency = `base_currency`) with the contract's `name` and
  `analysis_scope`. `add_source` stores a domain `Source` (its `SourceKind`;
  `mirror` marks a Yahoo-style view) with a `provider` label. Both are registered
  once: the same request again returns the stored row (`wrote` False); a
  different one under the same id is `duplicate`. No credential, secret
  reference or connection state is taken or stored.
- `remap` appends the next mapping revision for (source, reference) through
  `SourceMap.remap` (validated before the write: known source and accounts, the
  source's tenant, sorted disjoint intervals). `expected` (an int >= 0, else
  `expected`) is the caller's latest revision, 0 for none: else `stale`, unless
  the latest revision is `expected + 1` with the same intervals (a retry:
  returned, `wrote` False). `intervals` must be `MappedInterval` values
  (`intervals`), at most MAX_INTERVALS (`too_many_intervals`; 0055 holds the same).
- `load(tx, known_as_of)` re-hashes every row, checks each mapping chain
  (1..n, previous hash) and replays everything recorded by `known_as_of` (all
  when None) in knowledge order into a fresh `SourceMap`, failing closed with
  `integrity`. The domain keeps an account distinct from its sources and lets a
  mirror never add to a broker figure (`consolidate`); nothing here re-implements
  or relaxes those rules.
- Knowledge time: 0055 stamps recorded_at (the database clock under the exclusive
  'source_map:<tenant>' lock, never before an earlier row; a caller cannot supply
  it) and seals `row_hash = sha256(content_hash | recorded_at)`, where
  `content_hash` is this module's digest of every other column. Reads take the
  lock shared, so load(T) for a T before the read does not change later, except
  in a snapshot transaction whose snapshot predates a write in flight at T.
  Hash-only, as watchlist_store: removing a chain's latest rows is not detected.
- Lifecycle (owner decision 101): `archive_account` appends the account's next
  revision (0056) with compare-and-set on `expected_revision` (`stale`; the same
  archive at `expected_revision + 1` is a retry, `wrote` False). Archive is the
  only change and is final (`archived`). `load` gives each account's revision and
  lifecycle as of `known_as_of`.
- `read_accounts` derives the contract Account's `status` and `cash_basis` from a
  fresh domain `consolidate` on every read; neither is stored. `status`: archived,
  else conflicted when the consolidation shows a conflict, else active.
  `cash_basis`: the one basis every currency shares, only for a `reconciled`
  account, else `unknown`. No snapshot store exists yet (T014/T017), so callers
  pass the snapshots they hold; with none, cash_basis is `unknown`.
- Errors: `forbidden` (no grant: only qw_app writes, qw_app and qw_worker read),
  `tenant` (a domain value of another tenant), `database`, `integrity`, and the
  domain `ScopeError` codes.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, get_args

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import digest
from qw_domain.sources import (
    AccountView,
    MappedInterval,
    MappingRevision,
    ScopeError,
    Source,
    SourceKind,
    SourceMap,
    SourceSnapshot,
    UnderlyingAccount,
    consolidate,
)

from qw_adapters.tenancy import TenantTx

__all__ = ["MAX_INTERVALS", "AccountRecord", "AccountState", "AccountStoreError",
           "AnalysisScope", "Registry", "SourceRecord", "account_from_text",
           "add_source", "archive_account", "load", "read_accounts",
           "register_account", "remap", "seal"]  # fmt: skip

type AnalysisScope = Literal["complete_declared", "partial_declared", "hypothetical"]
type Lifecycle = Literal["active", "archived"]
type ContractStatus = Literal["active", "archived", "conflicted"]
type ContractCashBasis = Literal[
    "broker_available", "reconciled_ledger", "user_reported", "unknown"
]
MAX_INTERVALS = 1000  # per revision; 0055's CHECK holds the same


class AccountStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class AccountRecord:
    account: UnderlyingAccount
    name: str
    analysis_scope: AnalysisScope
    recorded_at: datetime  # registration (revision 1)
    revision: int = 1  # as of the Registry's known_as_of
    lifecycle: Lifecycle = "active"


@dataclass(frozen=True, slots=True)
class SourceRecord:
    source: Source
    provider: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class Registry:
    """What the tenant had recorded by `known_as_of` (None: everything)."""

    known_as_of: datetime | None
    source_map: SourceMap
    accounts: MappingProxyType[str, AccountRecord]
    sources: MappingProxyType[str, SourceRecord]
    revisions: tuple[MappingRevision, ...]  # knowledge order


@dataclass(frozen=True, slots=True)
class AccountState:
    """The contract Account's derived fields for one account (decision 101)."""

    record: AccountRecord
    status: ContractStatus
    cash_basis: ContractCashBasis
    source_ids: tuple[str, ...]  # sources whose latest revision names the account
    view: AccountView  # the raw consolidation; it knows nothing of the lifecycle

    @property
    def sizing_blocked(self) -> bool:
        """The sizing permission: blocked when the consolidation blocks it or the
        account is archived. Read this, never `view.sizing_blocked` alone (an
        archived account with a fresh complete snapshot has an unblocked view)."""
        return self.view.sizing_blocked or self.record.lifecycle == "archived"


def _intervals_json(ivs: Sequence[MappedInterval]) -> list[dict[str, Any]]:
    return [{"account_id": i.account_id, "valid_from": format_instant(i.valid_from),
             "valid_to": None if i.valid_to is None else format_instant(i.valid_to)}
            for i in ivs]  # fmt: skip


def seal(content_hash: str, recorded_at: datetime) -> str:
    """0055's row_hash: sha256 over `<content_hash>|<format_instant(recorded_at)>`."""
    text = f"{content_hash}|{format_instant(recorded_at)}"
    return hashlib.sha256(text.encode()).hexdigest()


def _account_content(tx: TenantTx, a: UnderlyingAccount, name: str, scope: str) -> str:
    return digest(["account", str(tx.tenant_id), a.account_id, name, a.institution,
                   a.account_type, a.base_currency, scope])  # fmt: skip


def _source_content(tx: TenantTx, s: Source, provider: str) -> str:
    return digest(["data_source", str(tx.tenant_id), s.source_id, s.kind.value,
                   provider])  # fmt: skip


def _revision_content(
    tx: TenantTx, sid: str, ref: str, rev: int, ivs: Sequence[MappedInterval],
    previous: str | None,
) -> str:  # fmt: skip
    return digest(["source_mapping_revision", str(tx.tenant_id), sid, ref, rev,
                   _intervals_json(ivs), previous])  # fmt: skip


def _lifecycle_content(tx: TenantTx, aid: str, rev: int, life: str,
                       previous: str) -> str:  # fmt: skip
    return digest(["account_revision", str(tx.tenant_id), aid, rev, life, previous])


def _lock(tx: TenantTx, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    tx.conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                    (f"source_map:{tx.tenant_id}",))  # fmt: skip


def _database(exc: psycopg.Error) -> AccountStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return AccountStoreError("forbidden", type(exc).__name__)
    return AccountStoreError("database", type(exc).__name__)


def _rows(tx: TenantTx, sql: str) -> list[tuple[Any, ...]]:
    try:
        return [tuple(r) for r in tx.conn.execute(sql, (tx.tenant_id,))]
    except psycopg.Error as exc:
        raise _database(exc) from None


_ACCOUNTS = ("SELECT account_id, name, broker, account_type, reporting_currency, "
             "analysis_scope, recorded_at, content_hash, row_hash FROM app.account "
             "WHERE tenant_id = %s ORDER BY recorded_at, account_id")  # fmt: skip
_SOURCES = ("SELECT source_id, kind, provider, recorded_at, content_hash, row_hash "
            "FROM app.data_source WHERE tenant_id = %s ORDER BY recorded_at, "
            "source_id")  # fmt: skip
_REVISIONS = ("SELECT source_id, account_ref, revision, intervals, recorded_at, "
              "previous_hash, content_hash, row_hash FROM "
              "app.source_mapping_revision WHERE tenant_id = %s "
              "ORDER BY source_id, account_ref, revision")  # fmt: skip
_LIFECYCLE = ("SELECT account_id, revision, lifecycle, recorded_at, previous_hash, "
              "content_hash, row_hash FROM app.account_revision WHERE tenant_id = %s "
              "ORDER BY account_id, revision")  # fmt: skip


def _sealed(content: str, want: str, at: datetime, stored: str) -> bool:
    return content == want and stored == seal(content, at)


def load(tx: TenantTx, known_as_of: datetime | None = None) -> Registry:
    _lock(tx, shared=True)
    return _load(tx, known_as_of)


def _load(tx: TenantTx, known_as_of: datetime | None) -> Registry:
    known = None if known_as_of is None else ensure_aware_utc(known_as_of)
    tenant = str(tx.tenant_id)
    raw = (_rows(tx, _ACCOUNTS), _rows(tx, _SOURCES), _rows(tx, _REVISIONS),
           _rows(tx, _LIFECYCLE))  # fmt: skip
    heads: dict[str, tuple[int, str, str]] = {}  # account: revision, hash, lifecycle
    m = SourceMap()
    accounts: dict[str, AccountRecord] = {}
    sources: dict[str, SourceRecord] = {}
    revisions: list[tuple[datetime, MappingRevision]] = []
    try:
        for aid, name, broker, atype, ccy, scope, at, c, h in raw[0]:
            if scope not in get_args(AnalysisScope.__value__):
                raise ValueError(f"account {aid} scope")
            a = AccountRecord(UnderlyingAccount(aid, tenant, broker, atype, ccy),
                              name, scope, ensure_aware_utc(at))  # fmt: skip
            content = _account_content(tx, a.account, name, scope)
            if not _sealed(c, content, a.recorded_at, h) or not 1 <= len(name) <= 100:
                raise ValueError(f"account {aid}")
            heads[aid] = (1, h, "active")
            if known is None or a.recorded_at <= known:
                accounts[aid] = a
        for aid, rev, life, at, prev, c, h in raw[3]:
            head = heads.get(aid)
            stamp = ensure_aware_utc(at)
            if (head is None or head[2] != "active" or life != "archived"
                    or (rev, prev) != (head[0] + 1, head[1])
                    or not _sealed(c, _lifecycle_content(tx, aid, rev, life, prev),
                                   stamp, h)):  # fmt: skip
                raise ValueError(f"account {aid} revision {rev}")
            heads[aid] = (rev, h, life)
            if aid in accounts and (known is None or stamp <= known):
                accounts[aid] = replace(accounts[aid], revision=rev, lifecycle=life)
        for sid, kind, provider, at, c, h in raw[1]:
            s = SourceRecord(Source(sid, tenant, SourceKind(kind)), provider,
                             ensure_aware_utc(at))  # fmt: skip
            if not _sealed(c, _source_content(tx, s.source, provider),
                           s.recorded_at, h):  # fmt: skip
                raise ValueError(f"source {sid}")
            if known is None or s.recorded_at <= known:
                sources[sid] = s
        last: tuple[str, str, int, str] | None = None
        for sid, ref, rev, ivs, at, prev, c, h in raw[2]:
            link = (last[2] + 1, last[3]) if last and last[:2] == (sid, ref) else (
                1, None)  # fmt: skip
            intervals = tuple(MappedInterval(i["account_id"],
                                             datetime.fromisoformat(i["valid_from"]),
                                             None if i["valid_to"] is None else
                                             datetime.fromisoformat(i["valid_to"]))
                              for i in ivs)  # fmt: skip
            r = MappingRevision(sid, ref, rev, intervals, ensure_aware_utc(at))
            content = _revision_content(tx, sid, ref, rev, intervals, prev)
            if ((rev, prev) != link or not _sealed(c, content, r.recorded_at, h)
                    or _intervals_json(intervals) != ivs):  # fmt: skip
                raise ValueError(f"mapping {sid} {ref} revision {rev}")
            last = (sid, ref, rev, h)
            if known is None or r.recorded_at <= known:
                revisions.append((r.recorded_at, r))
        for a in accounts.values():
            m.add_account(a.account)
        for s in sources.values():
            m.add_source(s.source)
        revisions.sort(key=lambda x: (x[0], x[1].source_id, x[1].account_ref,
                                      x[1].revision))  # fmt: skip
        for _, r in revisions:
            got = m.remap(r.source_id, r.account_ref, r.intervals, r.recorded_at)
            if got != r:
                raise ValueError(f"mapping {r.source_id} replay")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:  # ScopeError
        raise AccountStoreError("integrity", str(exc)) from None
    return Registry(known, m, MappingProxyType(accounts), MappingProxyType(sources),
                    tuple(r for _, r in revisions))  # fmt: skip


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _insert(tx: TenantTx, table: str, values: dict[str, Any]) -> None:
    sql = (f"INSERT INTO app.{table} ({', '.join(values)}) VALUES "
           f"({', '.join(f'%({k})s' for k in values)})")  # fmt: skip
    try:
        with tx.conn.transaction():
            tx.conn.execute(sql, values)
    except psycopg.Error as exc:
        raise _database(exc) from None


def _own(tx: TenantTx, tenant_id: str) -> None:
    if tenant_id != str(tx.tenant_id):
        raise AccountStoreError("tenant", "a value of another tenant")


def _nfc(value: str) -> str:  # a non-str is left for the domain to refuse
    return unicodedata.normalize("NFC", value) if isinstance(value, str) else value


def account_from_text(
    tx: TenantTx, account_id: str, broker: str, account_type: str,
    reporting_currency: str,
) -> UnderlyingAccount:  # fmt: skip
    """The store boundary for caller text (decision 103): `broker` and
    `account_type` are normalized to NFC before the domain value is built, so the
    stored bytes and hashes are the NFC form. The domain then applies its rule
    (`not_nfc` stays for values built directly; 0056 refuses raw non-NFC rows)."""
    try:
        return UnderlyingAccount(account_id, str(tx.tenant_id), _nfc(broker),
                                 _nfc(account_type), reporting_currency)  # fmt: skip
    except ScopeError as exc:
        raise AccountStoreError(exc.code, "refused by the domain") from None


def register_account(
    tx: TenantTx, account: UnderlyingAccount, name: str, analysis_scope: AnalysisScope
) -> tuple[AccountRecord, bool]:
    _own(tx, account.tenant_id)
    if not isinstance(name, str) or not 1 <= len(name) <= 100:
        raise AccountStoreError("name", "1 to 100 characters")
    if analysis_scope not in get_args(AnalysisScope.__value__):
        raise AccountStoreError("analysis_scope", "not a contract value")
    _lock(tx, shared=False)
    reg = _load(tx, None)
    old = reg.accounts.get(account.account_id)
    if old is not None:
        if (old.account, old.name, old.analysis_scope) != (
            account,
            name,
            analysis_scope,
        ):
            raise AccountStoreError("duplicate", "another account under this id")
        return old, False
    _insert(tx, "account", {
        "tenant_id": tx.tenant_id, "account_id": account.account_id, "name": name,
        "broker": account.institution, "account_type": account.account_type,
        "reporting_currency": account.base_currency,
        "analysis_scope": analysis_scope,
        "content_hash": _account_content(tx, account, name,
                                         analysis_scope)})  # fmt: skip
    return _load(tx, None).accounts[account.account_id], True


def add_source(
    tx: TenantTx, source: Source, provider: str
) -> tuple[SourceRecord, bool]:
    _own(tx, source.tenant_id)
    _lock(tx, shared=False)
    reg = _load(tx, None)
    old = reg.sources.get(source.source_id)
    if old is not None:
        if (old.source, old.provider) != (source, provider):
            raise AccountStoreError("duplicate", "another source under this id")
        return old, False
    _insert(tx, "data_source", {
        "tenant_id": tx.tenant_id, "source_id": source.source_id,
        "kind": source.kind.value, "provider": provider,
        "content_hash": _source_content(tx, source, provider)})  # fmt: skip
    return _load(tx, None).sources[source.source_id], True


def archive_account(
    tx: TenantTx, account_id: str, expected_revision: int
) -> tuple[AccountRecord, bool]:
    """Archive (decision 101's only lifecycle change, final): the next revision."""
    if type(expected_revision) is not int or expected_revision < 1:
        raise AccountStoreError("expected", "an int >= 1")
    _lock(tx, shared=False)
    old = _load(tx, None).accounts.get(account_id)
    if old is None:
        raise AccountStoreError("unknown_account", "no such account")
    if old.revision == expected_revision + 1 and old.lifecycle == "archived":
        return old, False  # a retry of this archive
    if old.revision != expected_revision:
        raise AccountStoreError("stale", "not the latest revision")
    if old.lifecycle == "archived":
        raise AccountStoreError("archived", "archive is final")
    previous = _account_head(tx, account_id)
    rev = expected_revision + 1
    _insert(tx, "account_revision", {
        "tenant_id": tx.tenant_id, "account_id": account_id, "revision": rev,
        "lifecycle": "archived", "previous_hash": previous,
        "content_hash": _lifecycle_content(tx, account_id, rev, "archived",
                                           previous)})  # fmt: skip
    return _load(tx, None).accounts[account_id], True


def _account_head(tx: TenantTx, account_id: str) -> str:
    row = tx.conn.execute(
        "SELECT row_hash FROM (SELECT row_hash, 1 AS revision FROM app.account "
        "WHERE tenant_id = %(t)s AND account_id = %(a)s UNION ALL SELECT row_hash, "
        "revision FROM app.account_revision WHERE tenant_id = %(t)s AND "
        "account_id = %(a)s) x ORDER BY revision DESC LIMIT 1",
        {"t": tx.tenant_id, "a": account_id},
    ).fetchone()
    assert row is not None  # _load just verified the account
    return str(row[0])


def read_accounts(
    tx: TenantTx, snapshots: Iterable[SourceSnapshot], as_of: datetime,
    max_age: timedelta, known_as_of: datetime | None = None,
) -> MappingProxyType[str, AccountState]:  # fmt: skip
    """Each account recorded by `known_as_of` with its contract `status` and
    `cash_basis` derived from `consolidate` on the stored map (decision 101)."""
    reg = load(tx, known_as_of)
    try:
        views = consolidate(reg.source_map, str(tx.tenant_id), snapshots, as_of,
                            max_age).accounts  # fmt: skip
    except ScopeError as exc:
        raise AccountStoreError(exc.code, "refused by the domain") from None
    latest: dict[tuple[str, str], MappingRevision] = {}
    for r in reg.revisions:  # knowledge order, so the last one wins
        latest[(r.source_id, r.account_ref)] = r
    named: dict[str, set[str]] = {}
    for r in latest.values():
        for i in r.intervals:
            named.setdefault(i.account_id, set()).add(r.source_id)
    out: dict[str, AccountState] = {}
    for aid, rec in sorted(reg.accounts.items()):
        view = views[aid]
        status: ContractStatus = (
            "archived" if rec.lifecycle == "archived"
            else "conflicted" if view.status == "conflicted" else "active"
        )  # fmt: skip
        bases = set(view.cash_basis.values())
        basis: ContractCashBasis = (
            bases.pop() if view.status == "reconciled" and len(bases) == 1
            else "unknown"
        )  # fmt: skip
        out[aid] = AccountState(rec, status, basis,
                                tuple(sorted(named.get(aid, ()))), view)  # fmt: skip
    return MappingProxyType(out)


def remap(
    tx: TenantTx, source_id: str, account_ref: str,
    intervals: Sequence[MappedInterval], expected: int,
) -> tuple[MappingRevision, bool]:  # fmt: skip
    if type(expected) is not int or expected < 0:
        raise AccountStoreError("expected", "an int >= 0")
    if isinstance(intervals, (str, bytes)) or not isinstance(intervals, Sequence) \
            or not all(type(i) is MappedInterval for i in intervals):  # fmt: skip
        raise AccountStoreError("intervals", "a sequence of MappedInterval")
    if len(intervals) > MAX_INTERVALS:
        raise AccountStoreError("too_many_intervals", f"at most {MAX_INTERVALS}")
    _lock(tx, shared=False)
    reg = _load(tx, None)
    history = reg.source_map.revisions(source_id, account_ref)
    sorted_ivs = tuple(sorted(intervals, key=lambda x: x.valid_from))
    if len(history) == expected + 1 and history[-1].intervals == sorted_ivs:
        return history[-1], False
    if len(history) != expected:
        raise AccountStoreError("stale", "not the latest revision")
    previous = _hash_of(tx, source_id, account_ref, expected)
    try:  # validates everything first; the in-memory map is discarded on failure
        rev = reg.source_map.remap(source_id, account_ref, intervals, _now(tx))
    except ScopeError as exc:
        raise AccountStoreError(exc.code, "refused by the domain") from None
    _insert(tx, "source_mapping_revision", {
        "tenant_id": tx.tenant_id, "source_id": source_id,
        "account_ref": account_ref, "revision": rev.revision,
        "intervals": Jsonb(_intervals_json(rev.intervals)),
        "previous_hash": previous,
        "content_hash": _revision_content(tx, source_id, account_ref, rev.revision,
                                          rev.intervals, previous)})  # fmt: skip
    got = _load(tx, None).source_map.revisions(source_id, account_ref)[-1]
    if got != replace(rev, recorded_at=got.recorded_at):  # 0055 stamped the time
        raise AccountStoreError("integrity", "the stored revision differs")
    return got, True


def _hash_of(tx: TenantTx, source_id: str, ref: str, revision: int) -> str | None:
    if revision == 0:
        return None
    row = tx.conn.execute(
        "SELECT row_hash FROM app.source_mapping_revision WHERE tenant_id = %s AND "
        "source_id = %s AND account_ref = %s AND revision = %s",
        (tx.tenant_id, source_id, ref, revision),
    ).fetchone()
    assert row is not None  # _load just verified the chain
    return str(row[0])
