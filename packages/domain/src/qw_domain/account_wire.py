"""The `financial/accounts.json` section (T057 increment 4) and the content hashes
of the account store (`qw_adapters.account_store`, 0055 and 0056; moved here
unchanged so the export and its import verify the same digests).

The section is the stored history, lossless and nothing else:
`{accounts, account_revisions, sources, mappings}`, one record per stored row, with
exact keys at every level. Each row carries its `tenant_id`, `recorded_at` (the
database's knowledge time), `content_hash` and `row_hash` (the 0055 seal).
- Only stored facts. Contract `status`, `cash_basis`, the consolidation's data state
  and sizing permission are derived (owner decision 101) from snapshots, and no
  snapshot store exists (T014/T017): every account would read `no_data` and
  sizing-blocked, which would go stale at once. The destination re-derives them
  from its own snapshots; an extra key such as `status` is refused, so no row can
  present an account as healthy.
- `source_ids` (contract Account) are not a field. They derive from `mappings` with
  `source_ids`: the latest revision per (source, reference), whatever its interval
  dates (`read_accounts` uses the same function). Ids stay the store's text ids
  (`ID_PATTERN`), never converted to the dictionary's `uuid`.
- No credential, token, secret reference or connection state is stored, so none is
  exported.
`parse_section` rebuilds everything through the domain: each row's content hash
and seal, both chains (revision n + 1 naming the previous row hash), the lifecycle
(archive is the only change and is final), the display text (NFC), kinds verbatim,
and a replay of every row in one knowledge order into a fresh `SourceMap` that must
reproduce each mapping revision. Hashes are integrity, not authenticity: whoever
rewrites a row and its hashes, and every row chained after it, is not detected,
and neither is a dropped chain tail (as in the store).
"""

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

from qw_domain.instants import InstantError, format_instant, parse_instant
from qw_domain.research_data import digest
from qw_domain.sources import (
    ID_PATTERN,
    MappedInterval,
    MappingRevision,
    ScopeError,
    Source,
    SourceKind,
    SourceMap,
    UnderlyingAccount,
)

ACCOUNT_KEYS = frozenset({"account_id", "tenant_id", "name", "broker", "account_type",
                          "reporting_currency", "analysis_scope", "recorded_at",
                          "content_hash", "row_hash"})  # fmt: skip
REVISION_KEYS = frozenset({"account_id", "tenant_id", "revision", "lifecycle",
                           "previous_hash", "recorded_at", "content_hash",
                           "row_hash"})  # fmt: skip
SOURCE_KEYS = frozenset({"source_id", "tenant_id", "kind", "provider", "recorded_at",
                         "content_hash", "row_hash"})  # fmt: skip
MAPPING_KEYS = frozenset({"source_id", "tenant_id", "account_ref", "revision",
                          "intervals", "previous_hash", "recorded_at", "content_hash",
                          "row_hash"})  # fmt: skip
INTERVAL_KEYS = frozenset({"account_id", "valid_from", "valid_to"})
ANALYSIS_SCOPES = ("complete_declared", "partial_declared", "hypothetical")
MAX_INTERVALS = 1000  # per mapping revision; 0055's CHECK holds the same
MAX_REVISION = 2147483646  # 0055's and 0056's revision CHECKs
# the section's lists and each one's order: the store's primary key order
ORDER: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "accounts": ("account_id",), "account_revisions": ("account_id", "revision"),
    "sources": ("source_id",), "mappings": ("source_id", "account_ref", "revision"),
})  # fmt: skip


class WireError(ValueError):
    """`code`: malformed, seal, chain, lifecycle, text or replay."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def intervals_json(ivs: Sequence[MappedInterval]) -> list[dict[str, Any]]:
    return [{"account_id": i.account_id, "valid_from": format_instant(i.valid_from),
             "valid_to": None if i.valid_to is None else format_instant(i.valid_to)}
            for i in ivs]  # fmt: skip


def seal(content_hash: str, recorded_at: datetime) -> str:
    """0055's row_hash: sha256 over `<content_hash>|<format_instant(recorded_at)>`."""
    text = f"{content_hash}|{format_instant(recorded_at)}"
    return hashlib.sha256(text.encode()).hexdigest()


def account_content(tenant: str, a: UnderlyingAccount, name: str, scope: str) -> str:
    return digest(["account", tenant, a.account_id, name, a.institution,
                   a.account_type, a.base_currency, scope])  # fmt: skip


def source_content(tenant: str, s: Source, provider: str) -> str:
    return digest(["data_source", tenant, s.source_id, s.kind.value, provider])


def revision_content(
    tenant: str, sid: str, ref: str, rev: int, ivs: Sequence[MappedInterval],
    previous: str | None,
) -> str:  # fmt: skip
    return digest(["source_mapping_revision", tenant, sid, ref, rev,
                   intervals_json(ivs), previous])  # fmt: skip


def lifecycle_content(tenant: str, aid: str, rev: int, life: str,
                      previous: str) -> str:  # fmt: skip
    return digest(["account_revision", tenant, aid, rev, life, previous])


def source_ids(revisions: Sequence[MappingRevision]) -> dict[str, tuple[str, ...]]:
    """Per account, the sources whose latest revision (in knowledge order) of some
    reference names it in any interval, whatever the interval dates."""
    latest: dict[tuple[str, str], MappingRevision] = {}
    for r in revisions:  # knowledge order, so the last one wins
        latest[(r.source_id, r.account_ref)] = r
    named: dict[str, set[str]] = {}
    for r in latest.values():
        for i in r.intervals:
            named.setdefault(i.account_id, set()).add(r.source_id)
    return {a: tuple(sorted(s)) for a, s in named.items()}


@dataclass(frozen=True, slots=True)
class ParsedAccount:
    account: UnderlyingAccount
    name: str
    analysis_scope: str
    recorded_at: datetime
    revision: int
    lifecycle: str  # "active" or "archived"


@dataclass(frozen=True, slots=True)
class Accounts:
    """The section rebuilt through the domain; derived state is the caller's."""

    source_map: SourceMap
    accounts: Mapping[str, ParsedAccount]
    sources: Mapping[str, tuple[Source, str]]  # with its provider label
    revisions: tuple[MappingRevision, ...]  # knowledge order


def _instant(v: object) -> datetime:
    try:
        t = parse_instant(v)  # type: ignore[arg-type]  # a non-str raises
    except (TypeError, InstantError):
        raise WireError("malformed", "an instant") from None
    if format_instant(t) != v:  # one canonical form
        raise WireError("malformed", f"instant {v!r} is not canonical")
    return t


def _checked(row: Mapping[str, Any], content: str, what: str) -> datetime:
    at = _instant(row["recorded_at"])
    if row["content_hash"] != content or row["row_hash"] != seal(content, at):
        raise WireError("seal", what)
    return at


def _ok(cond: object, what: str) -> None:
    if not cond:
        raise WireError("malformed", what)


def _int(v: object) -> bool:
    return type(v) is int and 1 <= v <= MAX_REVISION


def parse_section(tenant: str, body: Mapping[str, Any]) -> Accounts:
    """Rebuild and verify `body` (exact keys already checked) for `tenant`."""
    try:
        return _parse(tenant, body)
    except ScopeError as exc:
        raise WireError("text", exc.code) from None
    except (KeyError, TypeError, AttributeError) as exc:
        raise WireError("malformed", type(exc).__name__) from None


def _parse(tenant: str, body: Mapping[str, Any]) -> Accounts:
    for name, keys in ORDER.items():
        rows = body[name]
        _ok(isinstance(rows, list), name)
        order = [tuple(r[k] for k in keys) for r in rows]
        _ok(order == sorted(set(order)), f"{name} repeat or are unsorted")
    # (knowledge time, table rank, action): one order for every row, as 0055 stamps
    steps: list[tuple[datetime, int, int, Any]] = []
    heads: dict[str, list[Any]] = {}  # account: [revision, row hash, lifecycle, at]
    accounts: dict[str, ParsedAccount] = {}
    for n, r in enumerate(body["accounts"]):
        _ok(isinstance(r["name"], str) and 1 <= len(r["name"]) <= 100
            and r["analysis_scope"] in ANALYSIS_SCOPES, f"account {n}")  # fmt: skip
        a = UnderlyingAccount(r["account_id"], tenant, r["broker"], r["account_type"],
                              r["reporting_currency"])  # fmt: skip
        content = account_content(tenant, a, r["name"], r["analysis_scope"])
        at = _checked(r, content, f"account {a.account_id}")
        heads[a.account_id] = [1, r["row_hash"], "active", at]
        accounts[a.account_id] = ParsedAccount(a, r["name"], r["analysis_scope"], at,
                                               1, "active")  # fmt: skip
        steps.append((at, 0, n, a))
    for r in body["account_revisions"]:
        aid, rev, life, prev = (r[k] for k in ("account_id", "revision", "lifecycle",
                                               "previous_hash"))  # fmt: skip
        head = heads.get(aid)
        _ok(_int(rev), f"account {aid!r} revision")
        if head is None:
            raise WireError("chain", f"no account {aid!r}")
        if head[2] != "active" or life != "archived":  # archive only, and final
            raise WireError("lifecycle", f"account {aid} revision {rev}")
        at = _checked(r, lifecycle_content(tenant, aid, rev, life, prev),
                      f"account {aid} revision {rev}")  # fmt: skip
        if (rev, prev) != (head[0] + 1, head[1]) or at < head[3]:
            raise WireError("chain", f"account {aid} revision {rev}")
        heads[aid] = [rev, r["row_hash"], life, at]
    sources: dict[str, tuple[Source, str]] = {}
    for n, r in enumerate(body["sources"]):
        _ok(r["kind"] in {k.value for k in SourceKind} and isinstance(r["provider"],
            str) and ID_PATTERN.fullmatch(r["provider"]), f"source {n}")  # fmt: skip
        s = Source(r["source_id"], tenant, SourceKind(r["kind"]))  # kept verbatim
        at = _checked(r, source_content(tenant, s, r["provider"]),
                      f"source {s.source_id}")  # fmt: skip
        sources[s.source_id] = (s, r["provider"])
        steps.append((at, 1, n, s))
    last: tuple[str, str, int, str] | None = None
    for n, r in enumerate(body["mappings"]):
        sid, ref, rev, prev = (r[k] for k in ("source_id", "account_ref", "revision",
                                              "previous_hash"))  # fmt: skip
        ivs = r["intervals"]
        _ok(_int(rev) and isinstance(ivs, list) and len(ivs) <= MAX_INTERVALS and all(
            isinstance(i, dict) and set(i) == INTERVAL_KEYS for i in ivs),
            f"mapping {n}")  # fmt: skip
        try:  # the domain's half-open intervals
            intervals = tuple(MappedInterval(
                i["account_id"], _instant(i["valid_from"]),
                None if i["valid_to"] is None else _instant(i["valid_to"]))
                for i in ivs)  # fmt: skip
        except ScopeError as exc:
            raise WireError("replay", f"mapping {n} {exc.code}") from None
        _ok(intervals_json(intervals) == ivs, f"mapping {n} intervals")
        content = revision_content(tenant, sid, ref, rev, intervals, prev)
        at = _checked(r, content, f"mapping {sid} {ref} revision {rev}")
        link = (last[2] + 1, last[3]) if last and last[:2] == (sid, ref) else (1, None)
        if (rev, prev) != link:
            raise WireError("chain", f"mapping {sid} {ref} revision {rev}")
        last = (sid, ref, rev, r["row_hash"])
        steps.append((at, 2, n, MappingRevision(sid, ref, rev, intervals, at)))
    m, revisions = SourceMap(), []
    for _, rank, _, x in sorted(steps, key=lambda s: s[:3]):
        if rank == 0:
            m.add_account(x)
        elif rank == 1:
            m.add_source(x)
        else:
            try:
                got = m.remap(x.source_id, x.account_ref, x.intervals, x.recorded_at)
            except ScopeError as exc:
                raise WireError("replay", f"{x.source_id} {exc.code}") from None
            if got != x:
                raise WireError("replay", f"{x.source_id} {x.account_ref}")
            revisions.append(got)
    for aid, (rev, _, life, _) in heads.items():
        p = accounts[aid]
        accounts[aid] = ParsedAccount(p.account, p.name, p.analysis_scope,
                                      p.recorded_at, rev, life)  # fmt: skip
    return Accounts(m, MappingProxyType(accounts), MappingProxyType(sources),
                    tuple(revisions))  # fmt: skip
