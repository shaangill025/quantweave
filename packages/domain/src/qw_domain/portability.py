"""Hosted/self-hosted portability (T057 increment 1; spec §2 Journey E, §6, §12; T008
C-10, F-25; R006, R013, R051, R052, R087, R096, R097). Domain, stdlib only, no I/O;
the ZIP container is `qw_adapters.export_archive`.
- `ExportManifest` is exactly `export_manifest.schema.json`: no edition, brand or
  installation field (one format for both editions), canonical and content-hashed.
- Versions `qw-export-<major>.<minor>`; a newer minor is a downgrade: unknown sections
  are hash-checked (`.json` ones also scanned) and reported, never loaded.
- Open sections have a structure: the T052 learned-state files (closed key sets per
  record type, `unknown_field`) and `research/evidence.json` (contract Evidence).
- Tenant sections (increment 2) are exact key sets (`unknown_field`,
  `field_missing`), scanned first (`float`), every row the manifest's scope
  (`scope_mismatch`) and re-verified (`malformed`):
  - `financial/journal.json`: `{chains, events}` (increment 3). `events` are
    contract LedgerEvent rows (`qw_domain.ledger_wire`), rebuilt, re-encoded
    exactly and replayed per account. No observation, claimed execution,
    prediction or virtual record has a field there. `chains` is a closed header
    record per account (revision, count, running head over position, id and
    `recorded_at`) that must equal the one recomputed from `events`
    (`journal_chain`): a backdated `recorded_at`, a dropped tail or reordered rows
    are refused. The contract manifest has no field for it, so it is in the section;
  - `policy/policies.json`: each stored version (`policy_id`, `version`,
    `content_hash`, `label` and the draft `content`, `qw_domain.policy`), its hash
    recomputed and versions replayed. Adoption receipts, acceptance and step-up
    material are never exported (the `approval` omission);
  - `research/watchlists.json` (increment 3): each live list as the contract
    Watchlist plus `tenant_id` and `description`, entries in stored form (plus
    `needs`), each entry rebuilt and re-encoded exactly, in instrument order.
  - `financial/accounts.json` (increment 4): the stored accounts, account
    revisions, sources and mapping revisions (`qw_domain.account_wire`), each
    re-sealed and rebuilt through the domain, both chains checked and the map
    replayed (`account_seal`, `account_chain`, `account_lifecycle`,
    `account_text`, `mapping_replay`). Stored facts only: no derived status,
    cash basis, data state or sizing field, and no credential.
  - `audit/tombstones.json` (increment 5): the T058 ledger (0057,
    `qw_adapters.tombstone_store.export_tombstones`) as `{ledger, tombstones,
    checkpoints}` (`qw_domain.tombstone_wire`). Without the key, each entry hash
    is recomputed, the chain, seals and checkpoints are checked, one known
    `hash_scheme` and one `key_id` per chain (`tombstone_*` codes), and the
    closed `ledger` header (where the contract manifest has no field) declares
    the key a destination needs to match tombstones to re-imported records.
  Every section is open.
- The value scan refuses high-confidence secret formats only (defence in depth).
- Only `build_export` and `licensed_evidence` make an `ExportBundle` or a
  `LicensedDecision`; their constructors raise. A decision records each item's T021
  `check_use(user_export)` result with the feed and entitlement hashes, under one
  hash. `build_export` refuses a decision for another scope, after `created_at` or
  older than `DECISION_MAX_AGE`, evidence not permitted or whose `rights_profile_id`
  is not its decided feed, and a denied id in any string value (`_mentions`).
- GATE for the export route/job: `at` and `created_at` come from the server clock.
- Import results are inert: nothing is adopted, activated or reauthorized.
- Destination apply (increment 6): `qw_domain.portability_apply.plan` and
  `qw_adapters.destination_import`; `verify_import` itself stays inert.
LIMITATIONS: other stores are not exported; only policy versions are applied (the
other sections only while empty); no account mapping, format migration or
reconnection; hashes are integrity, not authenticity; free-text fields can carry
what the scan misses; T052 field values are checked by the T052 importer; source
observations (`source_record`) are not a section, so they are not exported.
"""

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum
from numbers import Real
from types import MappingProxyType
from typing import Any, NoReturn, cast

from qw_domain import account_wire as aw
from qw_domain import feedback as fb
from qw_domain import learned_state as ls
from qw_domain import ledger_wire as lw
from qw_domain import personal_adaptation as pa
from qw_domain import policy as dp
from qw_domain import rights
from qw_domain import tombstone_wire as tw
from qw_domain import watchlists as wl
from qw_domain.decimals import safe_repr
from qw_domain.instants import InstantError, format_instant, parse_instant
from qw_domain.sources import ID_PATTERN

FORMAT_VERSION = COMPATIBILITY_VERSION = "qw-export-1.0"
READER = (1, 0)
MANIFEST_PATH = "manifest.json"
EVIDENCE_PATH = "research/evidence.json"
JOURNAL_PATH, POLICIES_PATH = "financial/journal.json", "policy/policies.json"
WATCHLISTS_PATH = "research/watchlists.json"
ACCOUNTS_PATH = "financial/accounts.json"
TOMBSTONES_PATH = "audit/tombstones.json"
MAX_PATH, MAX_DEPTH = 255, 32
DECISION_MAX_AGE = timedelta(minutes=10)
CLASSES = frozenset({"financial", "policy", "research", "private_learning", "audit",
                     "metadata"})  # fmt: skip
SECTIONS: Mapping[str, str] = MappingProxyType({  # path -> ManifestFile content_class
    JOURNAL_PATH: "financial",
    ACCOUNTS_PATH: "financial",
    POLICIES_PATH: "policy",
    WATCHLISTS_PATH: "research",
    EVIDENCE_PATH: "research",
    TOMBSTONES_PATH: "audit",
    **ls.PATHS,  # the T052 learned-state files
})  # fmt: skip
OMISSION_CLASSES = frozenset({"licensed_evidence", "credential", "approval",
                              "shared_release", "evaluation_case", "record",
                              "lineage_edge", "candidate_payload"})  # fmt: skip

# A shape maps each allowed key to the shape of objects under it (directly or in
# lists), or to None for JSON scalars and lists of them. Records are closed sets.
Shape = Mapping[str, "Shape | None"]


def _leaves(*names: str) -> dict[str, Shape | None]:
    return dict.fromkeys(names)


def _of(*types: type) -> dict[str, Shape | None]:
    return _leaves(*(f.name for t in types for f in fields(t)))


_ADAPT = _leaves(*pa.ADAPTABLE)
_NESTED: Mapping[str, Shape] = {"subject": _of(fb.Subject), "fact": _of(fb.FactRef),
                                "inputs": _leaves(*fb.CASE_FIELDS)}  # fmt: skip
_KINDS: Mapping[str, Shape] = {  # learning/records.json, by record_kind
    t.KIND.value: {"record_kind": None, **{k: _NESTED.get(k) for k in _of(t)}}
    for t in (fb.Feedback, fb.ErrorRecord, fb.Outcome, fb.EvaluationCase)
}  # fmt: skip
_VERDICT = _leaves("verdict_id", "decision", "scores", "assessed_at", "reasons",
                   "limitations")  # fmt: skip
ROWS: Mapping[str, Shape] = {  # the open sections other than records and evidence
    "learning/consents.json": {"grant": _of(fb.ConsentGrant), "revoked_at": None},
    "learning/adaptation.json": {
        **_leaves("tenant_id", "number", "source", "adaptation_hash", "actor_ids",
                  "at", "status"), "adaptation": _ADAPT},
    "learning/candidates.json": {
        **_leaves("candidate_id", "target", "experiment_id", "parent_id",
                  "artifact_hash", "created_at"),
        "payload": _ADAPT, "verdicts": _VERDICT},
    "learning/lineage.json": _leaves("node", "kind", "scope_id", "origin", "basis",
                                     "purpose", "inputs", "reproducible",
                                     "invalidated", "reasons", "action"),
}  # fmt: skip
EVIDENCE_KEYS = frozenset({  # contract Evidence + project contract 0.3.0 coverage
    "id", "source_id", "locator", "passage", "content_hash", "published_at",
    "retrieved_at", "rights_profile_id", "availability", "source_relationship_group",
    "retention_class", "instrument_ids", "coverage"})  # fmt: skip
_AVAILABILITY = frozenset({"available", "restricted", "redacted", "deleted",
                           "unavailable"})  # fmt: skip
# Tenant sections: every object has exactly its shape's keys.
_LEDGER: Shape = {**_leaves(*lw.KEYS), "postings": _leaves(*lw.POSTING_KEYS),
                  "instrument_movements": _leaves(*lw.MOVEMENT_KEYS)}  # fmt: skip
_LIMIT = _leaves("metric", "value", "unit", "denominator", "account_id", "currency",
                 "sleeve_id", "window")  # Limit.to_wire  # fmt: skip
_DRAFT: Shape = {  # qw_domain.policy.draft_to_json
    **_leaves("tenant_id", "synthetic", "schema", "fields", "conflicts", "exclusions",
              "warnings"), "limits": _LIMIT,
    "allocations": {**_leaves("account_id", "currency", "sleeve_id"),
                    "amount": _leaves("amount", "currency")}}  # fmt: skip
_VERSION_ROW: Shape = {**_leaves("policy_id", "version", "content_hash", "label"),
                       "content": _DRAFT}  # fmt: skip
_JOURNAL: Shape = {"chains": _leaves(*lw.CHAIN_KEYS), "events": _LEDGER}
_WATCHLIST: Shape = {  # contract Watchlist + tenant_id, description; entry + needs
    **_leaves("id", "tenant_id", "revision", "name", "description"),
    "entries": {**_leaves("instrument_id", "thesis", "tags", "review_at",
                          "monitoring_priority", "invalidation_text", "needs"),
                "conditions": _leaves("metric", "operator", "values",
                                      "data_spec_id", "description")}}  # fmt: skip
_ACCOUNTS: Shape = {
    "accounts": _leaves(*aw.ACCOUNT_KEYS),
    "account_revisions": _leaves(*aw.REVISION_KEYS),
    "sources": _leaves(*aw.SOURCE_KEYS),
    "mappings": {**_leaves(*aw.MAPPING_KEYS), "intervals": _leaves(*aw.INTERVAL_KEYS)},
}  # fmt: skip
_ACCOUNT_CODES = {"seal": "account_seal", "chain": "account_chain",
                  "lifecycle": "account_lifecycle", "text": "account_text",
                  "replay": "mapping_replay", "malformed": "malformed"}  # fmt: skip
_TOMBSTONES: Shape = {"ledger": _leaves(*tw.HEADER_KEYS),
                      "tombstones": _leaves(*tw.TOMBSTONE_KEYS),
                      "checkpoints": _leaves(*tw.CHECKPOINT_KEYS)}  # fmt: skip
TENANT_SECTIONS: Mapping[str, Shape] = {JOURNAL_PATH: _JOURNAL,
                                        POLICIES_PATH: _VERSION_ROW,
                                        WATCHLISTS_PATH: _WATCHLIST}  # fmt: skip
OPEN_SECTIONS = frozenset({*ls.PATHS, EVIDENCE_PATH, *TENANT_SECTIONS,
                           ACCOUNTS_PATH, TOMBSTONES_PATH})  # fmt: skip
# "-----BEGIN" and "-----begin" in base64 at each of the 3 byte offsets
_B64_PEM = ("LS0tLS1CRUdJT", "tLS0tQkVHSU", "LS0tLUJFR0lO", "LS0tLS1iZWdpb",
            "tLS0tYmVnaW", "LS0tLWJlZ2lu")  # fmt: skip
_SECRET_VALUE = re.compile(
    r"(?i:-----\s*begin)|" + "|".join(_B64_PEM)
    + r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\."
    r"|\b(?:ssh-(?:rsa|dss|ed25519)|ecdsa-sha2-nistp[0-9]+)\s+AAAA"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[opsur]_[A-Za-z0-9]{20,}"
    r"|\bxox[bap]-[A-Za-z0-9-]{10,}|\b[rs]k_live_[A-Za-z0-9]{10,}"
    r"|\bsk-ant-[A-Za-z0-9_-]{10,}|\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"
    r"|\bAIza[0-9A-Za-z_-]{35}|\bnpm_[A-Za-z0-9]{36}"
    r"|https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{8,}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"
    r"|\bBearer\s+(?=[A-Za-z0-9._~+/-]*[0-9])[A-Za-z0-9._~+/-]{16,}",
    re.ASCII)  # fmt: skip
_BASIC = re.compile(r"\bBasic\s+([A-Za-z0-9+/]{8,}={0,2})", re.ASCII)

_VERSION = re.compile(r"qw-export-([1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})", re.ASCII)
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", re.ASCII)
_DRIVE = re.compile(r"[A-Za-z]:", re.ASCII)
_HEX64 = re.compile(r"[a-f0-9]{64}", re.ASCII)
_REASON = re.compile(r"[a-z][a-z_]*(:[A-Za-z0-9][A-Za-z0-9_.:-]*)?", re.ASCII)
_KEYS = sorted(("id", "format_version", "created_at", "scope_id", "files", "omissions",
                "contains_credentials", "contains_executable_code", "content_hash",
                "required_reconnections", "compatibility_version"))  # fmt: skip
_ENTRY_KEYS = ["bytes", "content_class", "path", "sha256"]
_OMIT_KEYS = ["reason", "record_class"]


class Refusal(StrEnum):
    FORMAT_UNKNOWN = "format_unknown"
    MAJOR_UNSUPPORTED = "major_unsupported"
    READER_TOO_OLD = "reader_too_old"
    COMPATIBILITY_INCONSISTENT = "compatibility_inconsistent"
    FORMAT_NOT_WRITABLE = "format_not_writable"
    MANIFEST = "manifest"
    MANIFEST_MISSING = "manifest_missing"
    MANIFEST_HASH = "manifest_hash"
    CREDENTIALS = "credentials"
    EXECUTABLE = "executable"
    PATH = "path"
    PATH_TRAVERSAL = "path_traversal"
    PATH_ABSOLUTE = "path_absolute"
    PATH_DRIVE = "path_drive"
    PATH_BACKSLASH = "path_backslash"
    PATH_CONTROL = "path_control"
    DUPLICATE_PATH = "duplicate_path"
    CASE_COLLISION = "case_collision"
    ENTRY_UNLISTED = "entry_unlisted"
    ENTRY_MISSING = "entry_missing"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    SECTION_UNKNOWN = "section_unknown"
    SECTION_NOT_READY = "section_not_ready"
    CLASS_MISMATCH = "class_mismatch"
    MALFORMED = "malformed"
    FLOAT = "float"
    DUPLICATE_KEY = "duplicate_key"
    TOO_DEEP = "too_deep"
    UNKNOWN_FIELD = "unknown_field"
    FIELD_MISSING = "field_missing"
    SCOPE_MISMATCH = "scope_mismatch"
    JOURNAL_CHAIN = "journal_chain"
    ACCOUNT_SEAL = "account_seal"
    ACCOUNT_CHAIN = "account_chain"
    ACCOUNT_LIFECYCLE = "account_lifecycle"
    ACCOUNT_TEXT = "account_text"
    MAPPING_REPLAY = "mapping_replay"
    TOMBSTONE_SCHEME = "tombstone_scheme"
    TOMBSTONE_KEY = "tombstone_key"
    TOMBSTONE_ENTRY = "tombstone_entry"
    TOMBSTONE_CHAIN = "tombstone_chain"
    TOMBSTONE_DUPLICATE = "tombstone_duplicate"
    TOMBSTONE_TIME = "tombstone_time"
    TOMBSTONE_SEAL = "tombstone_seal"
    TOMBSTONE_CHECKPOINT = "tombstone_checkpoint"
    TOMBSTONE_HEADER = "tombstone_header"
    SECRET_VALUE = "secret_value"
    OMISSION = "omission"
    DECISION = "decision"
    EVIDENCE_UNDECIDED = "evidence_undecided"
    EVIDENCE_OMITTED = "evidence_omitted"
    EVIDENCE_FEED_MISMATCH = "evidence_feed_mismatch"
    # destination apply (qw_domain.portability_apply, qw_adapters.destination_import)
    APPLY_FORMAT = "apply_format"
    DESTINATION_SCOPE = "destination_scope"
    DESTINATION_NOT_EMPTY = "destination_not_empty"
    SECTION_NOT_APPLICABLE = "section_not_applicable"
    KEY_UNAVAILABLE = "key_unavailable"
    KEY_MISMATCH = "key_mismatch"
    APPLY_PRINCIPAL = "apply_principal"
    APPLY_VERIFY = "apply_verify"
    # archive container (qw_adapters.export_archive)
    ARCHIVE_MALFORMED = "archive_malformed"
    ARCHIVE_LAYOUT = "archive_layout"
    ARCHIVE_TOO_LARGE = "archive_too_large"
    TOO_MANY_ENTRIES = "too_many_entries"
    ENTRY_TOO_LARGE = "entry_too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    COMPRESSION_RATIO = "compression_ratio"
    COMPRESSION_UNSUPPORTED = "compression_unsupported"
    ENTRY_ENCRYPTED = "entry_encrypted"
    ENTRY_UNSUPPORTED = "entry_unsupported"
    LOCAL_HEADER_MISMATCH = "local_header_mismatch"
    SYMLINK = "symlink"
    SPECIAL_ENTRY = "special_entry"
    EXECUTABLE_ENTRY = "executable_entry"


class PortabilityError(ValueError):
    def __init__(self, code: Refusal, detail: str) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code


def _refuse(code: Refusal, v: object) -> PortabilityError:
    return PortabilityError(code, safe_repr(v))


def _id(v: object, code: Refusal = Refusal.MANIFEST) -> str:
    if not isinstance(v, str) or ID_PATTERN.fullmatch(v) is None:
        raise _refuse(code, v)
    return v


def check_path(path: object) -> str:
    """A relative POSIX path of safe ASCII segments, at most 255 bytes (C-10)."""
    if not isinstance(path, str) or not path:
        raise _refuse(Refusal.PATH, path)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise _refuse(Refusal.PATH_CONTROL, path)
    if "\\" in path:
        raise _refuse(Refusal.PATH_BACKSLASH, path)
    if path.startswith("/"):
        raise _refuse(Refusal.PATH_ABSOLUTE, path)
    if _DRIVE.match(path):
        raise _refuse(Refusal.PATH_DRIVE, path)
    parts = path.split("/")
    if any(p in (".", "..") for p in parts):
        raise _refuse(Refusal.PATH_TRAVERSAL, path)
    if len(path) > MAX_PATH or not all(_SEGMENT.fullmatch(p) for p in parts):
        raise _refuse(Refusal.PATH, path)
    return path


def unique_paths(paths: Sequence[str]) -> None:
    """No duplicate and no case-folded collision (one file on a case-insensitive
    filesystem)."""
    seen: set[str] = set()
    folded: set[str] = set()
    for p in paths:
        if p in seen:
            raise _refuse(Refusal.DUPLICATE_PATH, p)
        if p.casefold() in folded:
            raise _refuse(Refusal.CASE_COLLISION, p)
        seen.add(p)
        folded.add(p.casefold())


def _secret(x: str) -> bool:
    """A high-confidence format, or `Basic` with a base64 `user:password`."""
    if _SECRET_VALUE.search(x):
        return True
    for m in _BASIC.finditer(x):
        try:
            if b":" in base64.b64decode(m[1], validate=True)[1:-1]:
                return True
        except ValueError:
            continue
    return False


def scan(body: object) -> list[str]:
    """JSON values only, bounded depth, UTF-8 strings, no high-confidence secret.
    Returns every string value, for the denied-evidence check."""
    found: list[str] = []
    stack: list[tuple[object, int]] = [(body, 0)]
    while stack:
        x, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise PortabilityError(Refusal.TOO_DEEP, f"deeper than {MAX_DEPTH}")
        if isinstance(x, dict):
            if not all(isinstance(k, str) for k in x):
                raise PortabilityError(Refusal.MALFORMED, "non-string key")
            stack.extend((k, depth + 1) for k in x)
            stack.extend((v, depth + 1) for v in x.values())
        elif isinstance(x, list):
            stack.extend((v, depth + 1) for v in x)
        elif isinstance(x, str):
            try:
                x.encode()
            except UnicodeEncodeError:
                raise PortabilityError(Refusal.MALFORMED, "not UTF-8") from None
            if _secret(x):
                raise PortabilityError(Refusal.SECRET_VALUE, "secret-shaped value")
            found.append(x)
        elif isinstance(x, Real) and not isinstance(x, int):  # binary or other real
            raise PortabilityError(Refusal.FLOAT, "decimal strings only")
        elif x is not None and type(x) not in (bool, int):
            raise PortabilityError(Refusal.MALFORMED, type(x).__name__)
    return found


def _conform(x: object, shape: Shape | None, where: str, exact: bool = False) -> None:
    """Every object under `x` uses only its shape's keys (all of them when `exact`);
    no object under a leaf."""
    stack: list[tuple[object, Shape | None, str]] = [(x, shape, where)]
    while stack:
        v, s, at = stack.pop()
        if isinstance(v, list):
            stack.extend((y, s, at) for y in v)
        elif isinstance(v, dict):
            if s is None or not set(v) <= set(s):
                raise _refuse(Refusal.UNKNOWN_FIELD, f"{at}: {sorted(v)}")
            if exact and set(v) != set(s):
                raise _refuse(Refusal.FIELD_MISSING, f"{at}: {sorted(set(s) - set(v))}")
            stack.extend((y, s[k], f"{at}.{k}") for k, y in v.items())


def _rows(body: object, path: str) -> list[dict[str, Any]]:
    if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
        raise _refuse(Refusal.MALFORMED, f"{path}: a list of objects")
    return body


def _instant(v: object, nullable: bool = False) -> bool:
    if v is None:
        return nullable
    try:
        return isinstance(v, str) and bool(parse_instant(v))
    except InstantError:
        return False


def evidence_ids(body: object) -> list[str]:
    """`research/evidence.json`: contract Evidence records, unique ids."""
    rows = _rows(body, EVIDENCE_PATH)
    for r in rows:
        if extra := sorted(set(r) - EVIDENCE_KEYS):
            raise _refuse(Refusal.UNKNOWN_FIELD, f"{EVIDENCE_PATH}: {extra}")
        cov, ids = r.get("coverage"), r.get("instrument_ids")
        why = cov.get("reasons") if isinstance(cov, dict) else None
        ok = (set(r) == EVIDENCE_KEYS and isinstance(r["locator"], str)
              and all(isinstance(r[k], str) and ID_PATTERN.fullmatch(r[k]) for k in (
                  "id", "source_id", "rights_profile_id", "source_relationship_group",
                  "retention_class"))
              and (r["passage"] is None or isinstance(r["passage"], str))
              and isinstance(r["content_hash"], str)
              and _HEX64.fullmatch(r["content_hash"]) is not None
              and _instant(r["published_at"], True) and _instant(r["retrieved_at"])
              and r["availability"] in _AVAILABILITY and isinstance(ids, list)
              and all(isinstance(i, str) and ID_PATTERN.fullmatch(i) for i in ids)
              and isinstance(cov, dict) and set(cov) == {"status", "reasons"}
              and cov["status"] in ("complete", "partial")
              and isinstance(why, list)
              and all(isinstance(s, str) and len(s) <= 300 for s in why))  # fmt: skip
        if not ok:
            raise _refuse(Refusal.MALFORMED, f"{EVIDENCE_PATH}: {r.get('id')}")
    ids_ = [str(r["id"]) for r in rows]
    if len(set(ids_)) != len(ids_):
        raise PortabilityError(Refusal.MALFORMED, "repeated evidence id")
    return ids_


def _journal(body: object, scope_id: str | None) -> None:
    """Exact keys; the events replay; then the header equals the one they give."""
    if not isinstance(body, dict):
        raise _refuse(Refusal.MALFORMED, f"{JOURNAL_PATH}: an object")
    _conform(body, _JOURNAL, JOURNAL_PATH, exact=True)
    rows = _rows(body["events"], JOURNAL_PATH)
    head = _rows(body["chains"], JOURNAL_PATH)
    if any(r["tenant_id"] != scope_id for r in rows):
        raise _refuse(Refusal.SCOPE_MISMATCH, JOURNAL_PATH)
    try:
        lw.parse_section(rows)
    except lw.WireError as e:
        raise _refuse(Refusal.MALFORMED, f"{JOURNAL_PATH}: {e}") from None
    if canonical(head) != canonical(lw.chains(str(scope_id), rows)):  # True is not 1
        raise _refuse(Refusal.JOURNAL_CHAIN, JOURNAL_PATH)


def _accounts(body: object, scope_id: str | None) -> None:
    """Exact keys; every row the scope's; then rebuilt through the domain."""
    if scope_id is None:  # the content hashes need the tenant
        raise _refuse(Refusal.SCOPE_MISMATCH, f"{ACCOUNTS_PATH}: no scope")
    if not isinstance(body, dict):
        raise _refuse(Refusal.MALFORMED, f"{ACCOUNTS_PATH}: an object")
    _conform(body, _ACCOUNTS, ACCOUNTS_PATH, exact=True)
    for name in _ACCOUNTS:
        if any(r["tenant_id"] != scope_id for r in _rows(body[name], ACCOUNTS_PATH)):
            raise _refuse(Refusal.SCOPE_MISMATCH, f"{ACCOUNTS_PATH}: {name}")
    try:
        aw.parse_section(str(scope_id), body)
    except aw.WireError as e:
        code = Refusal(_ACCOUNT_CODES[e.code])
        raise _refuse(code, f"{ACCOUNTS_PATH}: {e}") from None


def _tombstones(body: object, scope_id: str | None) -> None:
    """Exact keys; every row and the header the scope's; then re-verified without
    the key (`qw_domain.tombstone_wire`)."""
    if scope_id is None:  # the entry hashes cover the tenant
        raise _refuse(Refusal.SCOPE_MISMATCH, f"{TOMBSTONES_PATH}: no scope")
    if not isinstance(body, dict) or not isinstance(body.get("ledger"), dict):
        raise _refuse(Refusal.MALFORMED, f"{TOMBSTONES_PATH}: an object")
    _conform(body, _TOMBSTONES, TOMBSTONES_PATH, exact=True)
    rows = _rows(body["tombstones"], TOMBSTONES_PATH)
    rows = [body["ledger"], *rows, *_rows(body["checkpoints"], TOMBSTONES_PATH)]
    if any(r["tenant_id"] != scope_id for r in rows):
        raise _refuse(Refusal.SCOPE_MISMATCH, TOMBSTONES_PATH)
    try:
        tw.parse_section(scope_id, body)
    except tw.WireError as e:
        plain = {"malformed": Refusal.MALFORMED, "scope": Refusal.SCOPE_MISMATCH}
        code = plain.get(e.code) or Refusal(f"tombstone_{e.code}")
        raise _refuse(code, f"{TOMBSTONES_PATH}: {e}") from None


def _watchlists(rows: list[dict[str, Any]], scope_id: str | None) -> None:
    """Unique ids in order; each entry rebuilds (`qw_domain.watchlists`) and
    re-encodes exactly, entries in strict instrument order."""
    for n, r in enumerate(rows):
        if r["tenant_id"] != scope_id:
            raise _refuse(Refusal.SCOPE_MISMATCH, f"{WATCHLISTS_PATH}: row {n}")
        try:
            entries = [wl.entry_from_json(e) for e in r["entries"]]
            order = [e.instrument_id for e in entries]
            ok = (isinstance(r["id"], str) and ID_PATTERN.fullmatch(r["id"])
                  and type(r["revision"]) is int and r["revision"] >= 1
                  and isinstance(r["name"], str) and r["name"] != ""
                  and isinstance(r["description"], str)
                  and order == sorted(set(order))
                  and canonical([wl.entry_json(e) for e in entries])
                  == canonical(r["entries"]))  # fmt: skip
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            ok = False
        if not ok:
            raise _refuse(Refusal.MALFORMED, f"{WATCHLISTS_PATH}: row {n}")
    ids = [str(r["id"]) for r in rows]
    if ids != sorted(set(ids)):
        raise PortabilityError(Refusal.MALFORMED, "watchlist ids repeat or unsorted")


def _policies(rows: list[dict[str, Any]], scope_id: str | None) -> None:
    """Each version's content rebuilds a draft whose hash, label and wire are the
    row's, and replays as the next version of its policy."""
    seen: dict[str, dp.PolicyHistory] = {}
    for n, r in enumerate(rows):
        content = r["content"]
        if not isinstance(content, dict) or content["tenant_id"] != scope_id:
            raise _refuse(Refusal.SCOPE_MISMATCH, f"{POLICIES_PATH}: row {n}")
        try:
            pid = r["policy_id"]
            d = dp.draft_from_json(content)
            h = seen.get(pid) or dp.PolicyHistory.new(pid, content["tenant_id"])
            seen[pid] = nxt = h.propose(d)
            ok = (nxt is not h and type(r["version"]) is int
                  and type(content["synthetic"]) is bool  # the label hides 1 vs true
                  and nxt.versions[-1].version == r["version"]
                  and d.content_hash == r["content_hash"] and d.label == r["label"]
                  and canonical(dp.draft_to_json(d)) == canonical(content))  # fmt: skip
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            ok = False
        if not ok:
            raise _refuse(Refusal.MALFORMED, f"{POLICIES_PATH}: row {n}")


def check_section(path: str, body: object, scope_id: str | None = None) -> list[str]:
    """The structural allowlist for an open section, then the secret scan (a tenant
    section is scanned first and its rows must be `scope_id`'s). Returns its string
    values."""
    if path not in OPEN_SECTIONS:
        raise _refuse(Refusal.SECTION_NOT_READY, path)
    whole = {JOURNAL_PATH: _journal, ACCOUNTS_PATH: _accounts,
             TOMBSTONES_PATH: _tombstones}  # fmt: skip
    if path in whole:
        values = scan(body)
        whole[path](body, scope_id)
        return values
    if path in TENANT_SECTIONS:
        values = scan(body)
        rows = _rows(body, path)
        for r in rows:
            _conform(r, TENANT_SECTIONS[path], path, exact=True)
        (_policies if path == POLICIES_PATH else _watchlists)(rows, scope_id)
        return values
    if path == EVIDENCE_PATH:
        evidence_ids(body)  # exact keys and types
    for r in _rows(body, path) if path != EVIDENCE_PATH else ():
        kind = r.get("record_kind")
        shape = _KINDS.get(str(kind)) if path.endswith("records.json") else ROWS[path]
        _conform(r, shape, path)  # an unknown record_kind has no shape
    return scan(body)


def canonical(body: object) -> bytes:
    try:
        return json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode()  # fmt: skip
    except (ValueError, TypeError, RecursionError):  # incl. lone surrogates
        raise PortabilityError(Refusal.MALFORMED, "not canonical JSON") from None


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _no_float(text: str) -> object:
    raise _refuse(Refusal.FLOAT, text)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise PortabilityError(Refusal.DUPLICATE_KEY, "a JSON object repeats a key")
    return out


def parse_json(raw: bytes, what: str) -> object:
    """Strict UTF-8 JSON: no BOM, float, NaN or duplicate key."""
    if type(raw) is not bytes:
        raise _refuse(Refusal.MALFORMED, what)
    try:
        text = raw.decode()
        return json.loads(text, parse_float=_no_float, parse_constant=_no_float,
                          object_pairs_hook=_pairs)  # fmt: skip
    except PortabilityError:
        raise
    except (ValueError, RecursionError, TypeError):  # incl. bad UTF-8, huge ints
        raise _refuse(Refusal.MALFORMED, what) from None


def _version(v: object) -> tuple[int, int]:
    m = _VERSION.fullmatch(v) if isinstance(v, str) else None
    if m is None:
        raise _refuse(Refusal.FORMAT_UNKNOWN, v)
    return int(m[1]), int(m[2])


@dataclass(frozen=True, slots=True)
class Compatibility:
    format_version: tuple[int, int]
    downgraded: bool  # newer minor: unknown sections are reported, never loaded


def check_compatibility(format_version: str, compatibility_version: str,
                        reader: tuple[int, int] = READER) -> Compatibility:  # fmt: skip
    fmt, need = _version(format_version), _version(compatibility_version)
    if need > fmt:
        raise _refuse(Refusal.COMPATIBILITY_INCONSISTENT, compatibility_version)
    if fmt[0] != reader[0]:
        raise _refuse(Refusal.MAJOR_UNSUPPORTED, format_version)
    if need > reader:
        raise _refuse(Refusal.READER_TOO_OLD, compatibility_version)
    return Compatibility(fmt, fmt > reader)


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    sha256: str
    size: int  # wire `bytes`
    content_class: str

    def __post_init__(self) -> None:
        check_path(self.path)
        if not (isinstance(self.sha256, str) and _HEX64.fullmatch(self.sha256)
                and type(self.size) is int and self.size >= 0
                and self.content_class in CLASSES):  # fmt: skip
            raise _refuse(Refusal.MANIFEST, self.path)


@dataclass(frozen=True, slots=True)
class Omission:
    record_class: str
    reason: str

    def __post_init__(self) -> None:
        if self.record_class not in OMISSION_CLASSES or not (
            isinstance(self.reason, str) and len(self.reason) <= 300
            and _REASON.fullmatch(self.reason)
        ):  # fmt: skip
            raise _refuse(Refusal.OMISSION, self.reason)

    @property
    def evidence_id(self) -> str | None:
        """The id a `licensed_evidence` omission names."""
        parts = self.reason.split(":", 2)
        licensed = self.record_class == "licensed_evidence" and len(parts) == 3
        return parts[2] if licensed and parts[0] == "rights" else None


@dataclass(frozen=True, slots=True)
class ExportManifest:
    """`export_manifest.schema.json`, field for field."""

    id: str
    format_version: str
    created_at: datetime
    scope_id: str
    files: tuple[FileEntry, ...]
    omissions: tuple[Omission, ...]
    required_reconnections: tuple[str, ...]
    compatibility_version: str

    def __post_init__(self) -> None:
        _id(self.id)
        _id(self.scope_id)
        _version(self.format_version)
        _version(self.compatibility_version)
        try:
            format_instant(self.created_at)
        except (TypeError, InstantError):
            raise _refuse(Refusal.MANIFEST, "created_at") from None
        if not all(type(f) is FileEntry for f in self.files) or not all(
            type(o) is Omission for o in self.omissions
        ):  # fmt: skip
            raise PortabilityError(Refusal.MANIFEST, "entries")
        unique_paths([MANIFEST_PATH, *(f.path for f in self.files)])
        recon = self.required_reconnections
        for r in recon:
            _id(r)
        if len(set(recon)) != len(recon):
            raise PortabilityError(Refusal.MANIFEST, "repeated reconnection")

    def body(self) -> dict[str, object]:
        return {
            "id": self.id, "format_version": self.format_version,
            "created_at": format_instant(self.created_at), "scope_id": self.scope_id,
            "files": [{"path": f.path, "sha256": f.sha256, "bytes": f.size,
                       "content_class": f.content_class} for f in self.files],
            "omissions": [{"record_class": o.record_class, "reason": o.reason}
                          for o in self.omissions],
            "contains_credentials": False, "contains_executable_code": False,
            "required_reconnections": list(self.required_reconnections),
            "compatibility_version": self.compatibility_version,
        }  # fmt: skip

    def to_wire(self) -> dict[str, object]:
        return {**self.body(), "content_hash": sha256(canonical(self.body()))}

    @classmethod
    def from_wire(cls, obj: object) -> "ExportManifest":
        """Strict: exact contract keys, credential/code flags false, hash over the
        received fields, and the received form must be canonical."""
        if not isinstance(obj, dict) or not all(isinstance(k, str) for k in obj) or (
            sorted(obj) != _KEYS
        ):  # fmt: skip
            raise PortabilityError(Refusal.MANIFEST, "fields differ from contract")
        if obj["contains_credentials"] is not False:
            raise PortabilityError(Refusal.CREDENTIALS, "never carried")
        if obj["contains_executable_code"] is not False:
            raise PortabilityError(Refusal.EXECUTABLE, "never carried")
        body = {k: v for k, v in obj.items() if k != "content_hash"}
        if obj["content_hash"] != sha256(canonical(body)):
            raise PortabilityError(Refusal.MANIFEST_HASH, "manifest altered")
        fs, om, rc = obj["files"], obj["omissions"], obj["required_reconnections"]
        if not (isinstance(fs, list) and isinstance(om, list) and isinstance(rc, list)
                and all(isinstance(f, dict) and sorted(f) == _ENTRY_KEYS for f in fs)
                and all(isinstance(o, dict) and sorted(o) == _OMIT_KEYS for o in om)
                and isinstance(obj["created_at"], str)):  # fmt: skip
            raise PortabilityError(Refusal.MANIFEST, "shape")
        try:
            created = parse_instant(obj["created_at"])
        except InstantError:
            raise PortabilityError(Refusal.MANIFEST, "created_at") from None
        m = cls(obj["id"], obj["format_version"], created, obj["scope_id"],
                tuple(FileEntry(f["path"], f["sha256"], f["bytes"], f["content_class"])
                      for f in fs),
                tuple(Omission(o["record_class"], o["reason"]) for o in om),
                tuple(rc), obj["compatibility_version"])  # fmt: skip
        if m.to_wire() != obj:
            raise PortabilityError(Refusal.MANIFEST, "not canonical")
        return m


def _sealed(*_: object, **__: object) -> NoReturn:
    raise PortabilityError(Refusal.FORMAT_NOT_WRITABLE, "made only by its factory")


def _make[T](cls: type[T], **values: object) -> T:
    obj: T = object.__new__(cls)
    for k, v in values.items():
        object.__setattr__(obj, k, v)
    return obj


@dataclass(frozen=True, slots=True)
class LicensedItem:
    evidence_id: str
    feed_id: str


@dataclass(frozen=True, slots=True, init=False)
class LicensedDecision:
    """Made only by `licensed_evidence`."""

    scope_id: str
    at: datetime
    use_scope: rights.UseScope
    jurisdiction: str
    items: tuple[LicensedItem, ...]
    permitted: frozenset[str]
    denied: frozenset[str]
    omissions: tuple[Omission, ...]  # one per denied id
    revisions: tuple[tuple[str, str | None, str | None], ...]  # id, feed, entitlement
    decision_hash: str  # over every field above

    __init__ = _sealed


def _decision_hash(v: Mapping[str, Any]) -> str:
    return sha256(canonical([
        v["scope_id"], format_instant(v["at"]), str(v["use_scope"]), v["jurisdiction"],
        sorted([i.evidence_id, i.feed_id] for i in v["items"]), sorted(v["permitted"]),
        sorted(v["denied"]), [o.reason for o in v["omissions"]],
        sorted(list(r) for r in v["revisions"])]))  # fmt: skip


def licensed_evidence(
    registry: rights.Registry, tenant_id: str, items: Sequence[LicensedItem],
    at: datetime, *, scope: rights.UseScope, jurisdiction: str,
) -> LicensedDecision:  # fmt: skip
    """Which evidence `user_export` permits; one omission per item it does not
    (spec §6 rights-exclusion report). `at` is the export time (server clock)."""
    if len({i.evidence_id for i in items}) != len(items):
        raise PortabilityError(Refusal.OMISSION, "repeated evidence id")
    keep: set[str] = set()
    omit: list[Omission] = []
    revs: list[tuple[str, str | None, str | None]] = []
    for it in items:
        _id(it.evidence_id, Refusal.DECISION)
        _id(it.feed_id, Refusal.DECISION)
        d = rights.check_use(registry, tenant_id, it.feed_id, rights.Use.USER_EXPORT,
                             at, scope=scope, jurisdiction=jurisdiction)  # fmt: skip
        revs.append((it.evidence_id, d.feed_hash, d.entitlement_hash))
        if d.allowed:
            keep.add(it.evidence_id)
            continue
        codes = ".".join(sorted({r.code.value for r in d.reasons}))
        omit.append(Omission("licensed_evidence", f"rights:{codes}:{it.evidence_id}"))
    v: dict[str, Any] = {
        "scope_id": tenant_id, "at": at, "use_scope": scope, "items": tuple(items),
        "jurisdiction": jurisdiction, "permitted": frozenset(keep),
        "denied": frozenset(i.evidence_id for i in items) - keep,
        "omissions": tuple(omit), "revisions": tuple(revs)}  # fmt: skip
    made = _make(LicensedDecision, **v, decision_hash=_decision_hash(v))
    return cast(LicensedDecision, made)


def _mentions(values: Sequence[str], ids: frozenset[str]) -> str | None:
    """The first denied id in any string value, case-sensitive and unnormalised.
    A token id (6 or more characters, or 3 or more mixing letters with digits or
    punctuation) matches when not preceded or followed by a letter, digit, `_` or
    `-`: `ref:evd-2`, `evd-2:1` and `evd-2.json` match, `evd-20` does not. Other ids
    (the contract sets no minimum length) match only an exactly equal value."""
    long_ = [i for i in ids if len(i) >= 6 or (
        len(i) >= 3 and not i.isalpha() and any(c.isalpha() for c in i))]  # fmt: skip
    short = ids - set(long_)
    alt = "|".join(re.escape(i) for i in sorted(long_, key=len, reverse=True))
    token = re.compile(rf"(?<![A-Za-z0-9_-])({alt})(?![A-Za-z0-9_-])") if alt else None
    for v in values:
        if v in short:
            return v
        if token and (m := token.search(v)):
            return m[1]
    return None


@dataclass(frozen=True, slots=True, init=False)
class ExportBundle:
    """Made only by `build_export`: current format, open sections, each checked
    as on import."""

    manifest: ExportManifest
    files: Mapping[str, bytes]

    __init__ = _sealed


ALWAYS_OMITTED = (Omission("credential", "not_portable"),
                  Omission("approval", "authority_not_portable"))  # fmt: skip


def _check_decision(d: LicensedDecision | None, scope_id: str, at: datetime) -> None:
    if d is None:
        return
    try:
        made = d.decision_hash == _decision_hash({f.name: getattr(d, f.name)
                                                  for f in fields(d)})  # fmt: skip
    except (AttributeError, TypeError, ValueError, PortabilityError):
        made = False
    if type(d) is not LicensedDecision or not made:
        raise PortabilityError(Refusal.DECISION, "not made by licensed_evidence")
    if d.scope_id != scope_id:
        raise _refuse(Refusal.DECISION, f"scope {d.scope_id}")
    if not timedelta(0) <= at - d.at <= DECISION_MAX_AGE:
        raise _refuse(Refusal.DECISION, f"made at {format_instant(d.at)}")


def build_export(
    export_id: str, scope_id: str, created_at: datetime,
    sections: Mapping[str, object], *, omissions: Sequence[Omission] = (),
    reconnections: Sequence[str] = (), licensed: LicensedDecision | None = None,
) -> ExportBundle:  # fmt: skip
    """Open, structurally checked sections as canonical JSON with their manifest.
    Evidence needs `licensed`, whose omissions are the only licensed ones."""
    if any(o.record_class == "licensed_evidence" for o in omissions):
        raise PortabilityError(Refusal.OMISSION, "licensed omissions come from rights")
    _check_decision(licensed, scope_id, created_at)
    files: dict[str, bytes] = {}
    values: list[str] = []
    for path, body in sections.items():
        if path not in SECTIONS:
            raise _refuse(Refusal.SECTION_UNKNOWN, path)
        values += check_section(path, body, scope_id)
        files[path] = canonical(body)
    if hit := _mentions(values, licensed.denied if licensed else frozenset()):
        raise _refuse(Refusal.EVIDENCE_OMITTED, hit)
    feeds = {i.evidence_id: i.feed_id for i in licensed.items} if licensed else {}
    permitted = licensed.permitted if licensed else frozenset()
    evidence_ids(sections.get(EVIDENCE_PATH, []))
    for r in sections.get(EVIDENCE_PATH, []):  # type: ignore[attr-defined]
        if r["id"] not in permitted:
            raise _refuse(Refusal.EVIDENCE_UNDECIDED, r["id"])
        if r["rights_profile_id"] != feeds[r["id"]]:  # T036 serves the feed id there
            raise _refuse(Refusal.EVIDENCE_FEED_MISMATCH, r["id"])
    entries = tuple(FileEntry(p, sha256(b), len(b), SECTIONS[p])
                    for p, b in sorted(files.items()))  # fmt: skip
    omits = (*ALWAYS_OMITTED, *(licensed.omissions if licensed else ()), *omissions)
    manifest = ExportManifest(export_id, FORMAT_VERSION, created_at, scope_id, entries,
                              omits, tuple(reconnections),
                              COMPATIBILITY_VERSION)  # fmt: skip
    files_ = MappingProxyType(files)
    return cast(ExportBundle, _make(ExportBundle, manifest=manifest, files=files_))


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Verified, inert data. Nothing is adopted, activated or reauthorized."""

    manifest: ExportManifest
    compatibility: Compatibility
    data: Mapping[str, object]  # loaded sections, parsed
    unsupported: tuple[str, ...]  # checked, not loaded (newer minor)
    omissions: tuple[Omission, ...]
    pending_reconnections: tuple[str, ...]


def verify_import(manifest_raw: bytes, files: Mapping[str, bytes]) -> ImportReport:
    """Verify an export against its manifest; every failure is a typed refusal."""
    m = ExportManifest.from_wire(parse_json(manifest_raw, MANIFEST_PATH))
    compat = check_compatibility(m.format_version, m.compatibility_version)
    listed = {f.path for f in m.files}
    if unlisted := sorted(str(p) for p in set(files) - listed):
        raise _refuse(Refusal.ENTRY_UNLISTED, unlisted[0])
    data: dict[str, object] = {}
    unsupported: list[str] = []
    values: list[str] = []
    for f in m.files:
        raw = files.get(f.path)
        if raw is None:
            raise _refuse(Refusal.ENTRY_MISSING, f.path)
        if type(raw) is not bytes or len(raw) != f.size:
            raise _refuse(Refusal.SIZE_MISMATCH, f.path)
        if sha256(raw) != f.sha256:
            raise _refuse(Refusal.HASH_MISMATCH, f.path)
        known = SECTIONS.get(f.path)
        if known is None and not compat.downgraded:
            raise _refuse(Refusal.SECTION_UNKNOWN, f.path)
        if known is None:
            if f.path.endswith(".json"):
                values += scan(parse_json(raw, f.path))
            unsupported.append(f.path)  # checked, never loaded
            continue
        if f.content_class != known:
            raise _refuse(Refusal.CLASS_MISMATCH, f.path)
        data[f.path] = body = parse_json(raw, f.path)
        values += check_section(f.path, body, m.scope_id)
    omitted = frozenset(o.evidence_id for o in m.omissions if o.evidence_id)
    if hit := _mentions(values, omitted):
        raise _refuse(Refusal.EVIDENCE_OMITTED, hit)
    return ImportReport(m, compat, MappingProxyType(data), tuple(unsupported),
                        m.omissions, m.required_reconnections)  # fmt: skip
