"""The destination's apply plan for a verified export (T057 increment 6; spec §2
Journey E; R006, R051, R052, R087, R096, R097). Domain, stdlib only, no I/O; the
store-side apply is `qw_adapters.destination_import`, which builds the
`ImportReport` from the archive bytes itself (`read_archive`, the full
re-verification), so a manifest is never trusted alone.

`plan` refuses, before anything is written, in this order:
- `apply_format`: anything but the exact format this reader writes (a newer minor,
  an unsupported section or another compatibility version). No `qw-export-1.x`
  migration exists, so nothing is applied that could be read only in part;
- `destination_scope`: the destination tenant is not the archive's scope. Ids and
  hashes are kept exactly (policy content, journal chains and tombstone pseudonyms
  cover the tenant), so the destination tenant has the source's id; no remapping;
- `key_unavailable` / `key_mismatch`: the tombstone ledger's `key_id` against the
  destination's record key (`tombstone_wire.check_key`), even when the ledger
  could not be applied yet, so the check always precedes any write;
- `section_not_applicable`: a non-empty section this increment cannot write
  without re-stamping its source facts (see `APPLIED`);
- `malformed`: a policy id that is not a canonical UUID (the store's key).
The plan then lists each policy version, in archive order, rebuilt as its draft.
It carries nothing adoptable: adoption receipts are never exported, and the
destination re-adopts. Reconnections are listed for the caller; no credential is
read or imported.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from qw_domain import policy as dp
from qw_domain import portability as pt
from qw_domain import tombstone_wire as tw
from qw_domain.retention import RecordKey

R = pt.Refusal
# Sections this increment writes. The others are applied only while empty: their
# stores stamp `recorded_at` (journal, accounts, tombstones) or number revisions
# from 1 under the DB clock (watchlists) by trigger, so writing them would re-stamp
# source facts; that needs an insert path (a migration) not built here. Evidence
# and the T052 learned-state files have no destination store yet.
APPLIED = frozenset({pt.POLICIES_PATH})


@dataclass(frozen=True, slots=True)
class PolicyRow:
    policy_id: uuid.UUID
    version: int
    content_hash: str
    draft: dp.DraftPolicy


@dataclass(frozen=True, slots=True)
class ApplyPlan:
    export_id: str
    scope_id: str
    sections: tuple[str, ...]  # the archive's paths, sorted
    policies: tuple[PolicyRow, ...]
    policy_rows: tuple[Mapping[str, Any], ...]  # as in the archive, for the check
    omissions: tuple[pt.Omission, ...]
    required_reconnections: tuple[str, ...]


def is_empty(path: str, body: Any) -> bool:
    """A verified section with no record (the shapes `check_section` allows)."""
    if path == pt.JOURNAL_PATH:
        return bool(body["events"] == [] and body["chains"] == [])
    if path == pt.ACCOUNTS_PATH:
        return all(body[k] == [] for k in body)
    if path == pt.TOMBSTONES_PATH:
        return bool(body["tombstones"] == [] and body["checkpoints"] == [])
    return bool(body == [])


def _policy_id(v: object) -> uuid.UUID:
    try:
        u = uuid.UUID(v) if isinstance(v, str) else None
    except ValueError:
        u = None
    if u is None or str(u) != v:
        raise pt.PortabilityError(R.MALFORMED, f"{pt.POLICIES_PATH}: policy id")
    return u


def plan(report: pt.ImportReport, destination: str,
         key: RecordKey | None) -> ApplyPlan:  # fmt: skip
    """The writes for `destination` (the tenant id as text), or a refusal."""
    m = report.manifest
    if (m.format_version, m.compatibility_version) != (
        pt.FORMAT_VERSION, pt.COMPATIBILITY_VERSION
    ) or report.compatibility.downgraded or report.unsupported:  # fmt: skip
        raise pt.PortabilityError(R.APPLY_FORMAT, m.format_version)
    if destination != m.scope_id:
        raise pt.PortabilityError(R.DESTINATION_SCOPE, "not the archive's tenant")
    data = report.data
    if pt.TOMBSTONES_PATH in data:
        body: Any = data[pt.TOMBSTONES_PATH]
        try:
            tw.check_key(tw.parse_section(m.scope_id, body), key)
        except tw.WireError as e:
            code = {
                "key_unavailable": R.KEY_UNAVAILABLE,
                "key_mismatch": R.KEY_MISMATCH,
            }.get(e.code, R.MALFORMED)
            raise pt.PortabilityError(code, pt.TOMBSTONES_PATH) from None
    for path in sorted(data):
        if path not in APPLIED and not is_empty(path, data[path]):
            raise pt.PortabilityError(R.SECTION_NOT_APPLICABLE, path)
    rows: list[Any] = list(data.get(pt.POLICIES_PATH, []))  # type: ignore[call-overload]
    policies = tuple(
        PolicyRow(_policy_id(r["policy_id"]), r["version"], r["content_hash"],
                  dp.draft_from_json(r["content"]))
        for r in rows
    )  # fmt: skip
    return ApplyPlan(m.id, m.scope_id, tuple(sorted(data)), policies, tuple(rows),
                     m.omissions, m.required_reconnections)  # fmt: skip
