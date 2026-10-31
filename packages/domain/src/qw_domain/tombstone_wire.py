"""The `audit/tombstones.json` section (T057 increment 5): the T058 tombstone ledger
(0057, `qw_adapters.tombstone_store.export_tombstones`) as stored, and its import
re-verification, which never needs or carries the record key (owner decision 104).

The body is `{ledger, tombstones, checkpoints}`, exact keys at every level:
- `tombstones`: one record per stored row in `seq` order, exactly the domain
  `Tombstone` (its `backups_unknown` as 0059's `residual_backups_unknown`, owner
  decision 114: a JSON boolean; true lists no backup and no expiry) plus
  `recorded_at` (the database's stamp) and `row_hash` (its seal,
  `account_wire.seal` over `entry_hash`). The record is named only by its keyed
  pseudonym, never its id.
- `checkpoints`: the stored `LedgerCheckpoint` rows (length, head, stamp, seal).
- `ledger`: a closed header `{tenant_id, length, head, hash_scheme, key_id}`,
  recomputed from `tombstones` on import (`header`). The contract manifest is
  `additionalProperties: false`, so the key a destination needs is declared here:
  it compares `key_id` (a one-way MAC, not the key) with its own key before it
  applies anything (`check_key`). An empty ledger names no scheme and no key.
`parse_section` checks, without the key: every row the tenant's (`scope`);
canonical instants, ID_PATTERN ids and the 0057 column CHECKs (`malformed`); one
known `hash_scheme` (`scheme`) and one `key_id` (`key`) per chain; each
`entry_hash` recomputed with the domain digest (`entry`); `seq` from 0 and
`prev_hash` linking (`chain`), deletion times in order; one tombstone per record
hash (`duplicate`, 0057's UNIQUE); `deleted_at` not after `recorded_at` (`time`, as
0057 stamps); each seal and its stamp order (`seal`); every checkpoint the chain's
entry at its length, the latest covering the ledger (`checkpoint`); then the header
(`header`). Hashes are
integrity, not authenticity: whoever rewrites every row and recomputes every hash
verifies, and so does a dropped tail with its checkpoint (signing is T060).
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qw_domain.account_wire import seal
from qw_domain.instants import InstantError, format_instant, parse_instant
from qw_domain.retention import (
    GENESIS,
    HASH_SCHEMES,
    LedgerCheckpoint,
    RecordKey,
    RetentionClass,
    RetentionError,
    Tombstone,
    entry_hash,
    verify,
)
from qw_domain.sources import ID_PATTERN

TOMBSTONE_KEYS = frozenset({"tenant_id", "seq", "record_hash", "cls", "job_id",
                            "requested_at", "deleted_at", "residual_backups",
                            "backup_expiry_at", "hash_scheme", "key_id",
                            "prev_hash", "entry_hash", "recorded_at",
                            "row_hash", "residual_backups_unknown"})  # fmt: skip
CHECKPOINT_KEYS = frozenset({"tenant_id", "length", "head", "recorded_at",
                             "row_hash"})  # fmt: skip
HEADER_KEYS = frozenset({"tenant_id", "length", "head", "hash_scheme", "key_id"})
MAX_SEQ, MAX_BACKUPS = 2147483646, 1000  # 0057's CHECKs
_HEX64 = re.compile(r"[a-f0-9]{64}", re.ASCII)
_HEX32 = re.compile(r"[a-f0-9]{32}", re.ASCII)


class WireError(ValueError):
    """`code`: malformed, scope, scheme, key, entry, chain, duplicate, time, seal,
    checkpoint, header; `check_key` adds argument, key_unavailable and
    key_mismatch."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Parsed:
    entries: tuple[Tombstone, ...]  # seq order, verified
    checkpoint: LedgerCheckpoint  # the latest (length 0: none)
    hash_scheme: str | None  # None: an empty ledger
    key_id: str | None


def _json(v: object) -> str:  # canonical: `true` is not `1`
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def header(tenant: str, tombstones: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    last = tombstones[-1] if tombstones else None
    return {"tenant_id": tenant, "length": len(tombstones),
            "head": GENESIS if last is None else last["entry_hash"],
            "hash_scheme": None if last is None else last["hash_scheme"],
            "key_id": None if last is None else last["key_id"]}  # fmt: skip


def _instant(v: object, nullable: bool = False) -> datetime | None:
    if v is None and nullable:
        return None
    try:
        if isinstance(v, str) and format_instant(at := parse_instant(v)) == v:
            return at
    except InstantError:
        pass
    raise WireError("malformed", f"instant {v!r}")


def _at(v: object) -> datetime:
    at = _instant(v)
    assert at is not None
    return at


def _int(v: object, low: int, high: int) -> int:
    if type(v) is not int or not low <= v <= high:
        raise WireError("malformed", f"integer {v!r}")
    return v


def _hex(v: object, pattern: re.Pattern[str] = _HEX64) -> str:
    if not isinstance(v, str) or pattern.fullmatch(v) is None:
        raise WireError("malformed", f"hash {v!r}")
    return v


def _id(v: object) -> str:
    if not isinstance(v, str) or ID_PATTERN.fullmatch(v) is None:
        raise WireError("malformed", f"id {v!r}")
    return v


def _tombstone(r: Mapping[str, Any], tenant: str) -> Tombstone:
    seq, kept = _int(r["seq"], 0, MAX_SEQ), r["residual_backups"]
    if not isinstance(kept, list) or len(kept) > MAX_BACKUPS or kept != sorted(
            {_id(b) for b in kept}):  # distinct, in code point order  # fmt: skip
        raise WireError("malformed", f"tombstone {seq}: residual backups")
    try:
        cls = RetentionClass(r["cls"])
    except ValueError:
        raise WireError("malformed", f"tombstone {seq}: class") from None
    req, dele = _at(r["requested_at"]), _at(r["deleted_at"])
    exp = _instant(r["backup_expiry_at"], nullable=True)
    unknown = r["residual_backups_unknown"]  # owner decision 114, 0059
    if cls is RetentionClass.PRIVACY or req > dele or (exp is not None and not kept) \
            or type(unknown) is not bool or (unknown and (kept or exp)):  # fmt: skip
        raise WireError("malformed", f"tombstone {seq}")
    return Tombstone(seq, tenant, _hex(r["record_hash"]), cls, _id(r["job_id"]),
                     req, dele, tuple(kept), exp, str(r["hash_scheme"]),
                     _hex(r["key_id"], _HEX32), _hex(r["prev_hash"]),
                     _hex(r["entry_hash"]), unknown)  # fmt: skip


def parse_section(tenant: str, body: Mapping[str, Any]) -> Parsed:
    """The verified ledger. Needs no key. `portability` checks exact keys first;
    any other shape here is `malformed`."""
    try:
        return _parse(tenant, body)
    except (KeyError, TypeError, AttributeError, IndexError):
        raise WireError("malformed", "the section's shape") from None


def _parse(tenant: str, body: Mapping[str, Any]) -> Parsed:
    rows, cps = body["tombstones"], body["checkpoints"]
    if any(r["tenant_id"] != tenant for r in (*rows, *cps)):
        raise WireError("scope", "a row of another tenant")
    entries: list[Tombstone] = []
    stamps: list[datetime] = []
    seen: set[str] = set()
    for n, r in enumerate(rows):
        t = _tombstone(r, tenant)
        if t.hash_scheme not in HASH_SCHEMES:
            raise WireError("scheme", f"tombstone {n}: {t.hash_scheme!r}")
        if entries and t.key_id != entries[0].key_id:
            raise WireError("key", f"tombstone {n}: one key per chain")
        if t.entry_hash != entry_hash(t):
            raise WireError("entry", f"tombstone {n}")
        prev = entries[-1] if entries else None
        if t.seq != n or t.prev_hash != (GENESIS if prev is None else prev.entry_hash) \
                or (prev is not None and t.deleted_at < prev.deleted_at):  # fmt: skip
            raise WireError("chain", f"tombstone {n}")
        if t.record_hash in seen:  # 0057: UNIQUE (tenant_id, record_hash)
            raise WireError("duplicate", f"tombstone {n}: a repeated record hash")
        seen.add(t.record_hash)
        at = _at(r["recorded_at"])
        if t.deleted_at > at:  # 0057 stamps recorded_at at or after deleted_at
            raise WireError("time", f"tombstone {n}: deleted after it was recorded")
        if r["row_hash"] != seal(t.entry_hash, at) or (stamps and at < stamps[-1]):
            raise WireError("seal", f"tombstone {n}")
        entries.append(t)
        stamps.append(at)
    try:
        verify(entries)  # the domain's own check, again
    except RetentionError as e:
        raise WireError("chain", str(e)) from None
    length, head, last = 0, GENESIS, None
    for c in cps:
        n, h, at = _int(c["length"], 1, MAX_SEQ + 1), _hex(c["head"]), _at(
            c["recorded_at"])  # fmt: skip
        if not length < n <= len(entries) or h != entries[n - 1].entry_hash or (
                c["row_hash"] != seal(h, at) or at < stamps[n - 1]
                or (last is not None and at < last)):  # fmt: skip
            raise WireError("checkpoint", f"checkpoint {n}")
        length, head, last = n, h, at
    if length != len(entries):
        raise WireError("checkpoint", "the latest checkpoint does not cover the ledger")
    if _json(body["ledger"]) != _json(header(tenant, rows)):
        raise WireError("header", "differs from the one the tombstones give")
    first = entries[0] if entries else None
    return Parsed(tuple(entries), LedgerCheckpoint(length, head),
                  None if first is None else first.hash_scheme,
                  None if first is None else first.key_id)  # fmt: skip


def check_key(ledger: Parsed, key: RecordKey | None) -> None:
    """Before a destination applies tombstones: matching a re-imported record needs
    the key that named them. Takes the verified ledger (`parse_section`), whose
    `key_id` is the header's. An empty ledger needs none."""
    if type(ledger) is not Parsed:
        raise WireError("argument", "a verified ledger from parse_section")
    if ledger.key_id is None:
        return
    if type(key) is not RecordKey:
        raise WireError("key_unavailable", "the record key is required")
    if (key.scheme, key.key_id) != (ledger.hash_scheme, ledger.key_id):
        raise WireError("key_mismatch", "the ledger was named under another key")
