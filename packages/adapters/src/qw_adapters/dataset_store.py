"""The research dataset store (`0046_research_dataset.sql`; T032; owner
decisions 70, 73 to 77, 83 and 84).

- `record(tx, dataset, terms)` (qw_ingest, decisions 73 and 83): research rights
  for every declared source at the database clock under `terms` (decision 77,
  the domain's `require_research_use` on the stored registry); every fact's
  `feed_hash` and every manifest source hash must be a recorded revision of its
  feed (equality not required); retention not expired. Decision 74: an identical
  re-freeze returns False; other content, body or terms is `dataset_exists`; a
  freeze that raced into the primary key is retried once so the guard decides.
- `load(tx, manifest)` (qw_worker, qw_assessor): rebuilds the row through
  `ResearchDataset.from_wire` (content hash re-derived), re-checks the event hash
  and requires the stored manifest to equal the plan's; anything wrong is
  `integrity`, a missing row None. Then the rights, feed-revision and retention
  checks again at the database clock, so a revoked right or an expired retention
  refuses even a pending re-run (decision 75).
- `research_rights(tx, terms)`: `ResearchRights` at the database clock.
- Retention: `terms.retain_for` (at most 100 years) from the earliest fact receipt,
  universe event knowledge time or freeze: times the records claim, not
  measurements. At or after its end `record` and `load` refuse `retention_expired`.
LIMITATIONS: the database checks neither rights nor retention at insert (a
qw_ingest login with direct SQL can insert any CHECK-valid row), so `load` is the
gate. No purge yet (decision 84: the owner-run purge with a hashed tombstone, after
which loads report `dataset_purged`, is T058 scope). On any error the caller's
transaction must roll back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import (
    DatasetManifest,
    DatasetRefused,
    ResearchDataset,
    ResearchError,
    ResearchRights,
    digest,
    require_research_use,
)
from qw_domain.rights import DenyReason, Registry, UseScope

from qw_adapters import rights_store
from qw_adapters.tenancy import TenantTx

_COLS = ("tenant_id", "dataset_id", "content_hash", "body", "terms", "recorded_at",
         "event_hash")  # fmt: skip
_JURISDICTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}", re.ASCII)
_US = timedelta(microseconds=1)
MAX_RETAIN = timedelta(days=36525)  # 100 years; the SQL CHECK holds the same bound


class DatasetStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.reasons: tuple[tuple[str, DenyReason], ...] = ()  # rights_refused


@dataclass(frozen=True, slots=True)
class DatasetTerms:
    """Decision 77: the research-rights terms a dataset is frozen and used under."""

    scope: UseScope
    jurisdiction: str
    retain_for: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.scope, UseScope):
            raise DatasetStoreError("invalid", "terms need a UseScope")
        if type(self.jurisdiction) is not str or not _JURISDICTION.fullmatch(
            self.jurisdiction
        ):
            raise DatasetStoreError("invalid", "terms need a jurisdiction")
        if type(self.retain_for) is not timedelta or not (
            timedelta(0) < self.retain_for <= MAX_RETAIN
        ):  # fmt: skip
            raise DatasetStoreError("invalid", "retention in (0, 100 years]")

    def to_wire(self) -> dict[str, object]:
        return {"scope": self.scope.value, "jurisdiction": self.jurisdiction,
                "retain_for_us": self.retain_for // _US}  # fmt: skip

    @classmethod
    def from_wire(cls, w: object) -> DatasetTerms:
        if type(w) is not dict or set(w) != {"scope", "jurisdiction", "retain_for_us"}:
            raise DatasetStoreError("integrity", "terms keys")
        us = w["retain_for_us"]
        if type(us) is not int or type(w["scope"]) is not str:
            raise DatasetStoreError("integrity", "terms types")
        try:
            t = cls(UseScope(w["scope"]), w["jurisdiction"], timedelta(microseconds=us))
        except (ValueError, OverflowError, DatasetStoreError):
            raise DatasetStoreError("integrity", "terms") from None
        if t.to_wire() != w:
            raise DatasetStoreError("integrity", "terms not canonical")
        return t


@dataclass(frozen=True, slots=True)
class StoredDataset:
    dataset: ResearchDataset
    terms: DatasetTerms
    rights: ResearchRights  # at the database clock of this load
    recorded_at: datetime


def _now(tx: TenantTx) -> datetime:
    row = tx.conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _hash(tx: TenantTx, ds_id: str, h: str, terms: object, at: datetime) -> str:
    return digest(["research_dataset", str(tx.tenant_id), ds_id, h, terms,
                   format_instant(at)])  # fmt: skip


def _database(exc: psycopg.Error) -> DatasetStoreError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return DatasetStoreError("forbidden", type(exc).__name__)
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return DatasetStoreError("dataset_exists", type(exc).__name__)
    return DatasetStoreError("database", type(exc).__name__)


def research_rights(tx: TenantTx, terms: DatasetTerms) -> ResearchRights:
    """The stored registry and this tenant's entitlements at the database clock."""
    try:
        registry = rights_store.load(tx)
        at = _now(tx)
    except psycopg.Error as exc:
        raise _database(exc) from None
    except rights_store.RightsStoreError as exc:
        denied = exc.code == "database" and "InsufficientPrivilege" in str(exc)
        raise DatasetStoreError("forbidden" if denied else exc.code, "rights") from None
    return ResearchRights(registry, str(tx.tenant_id), at, terms.scope,
                          terms.jurisdiction, terms.retain_for)  # fmt: skip


def _revisions(registry: Registry, feed: str) -> frozenset[str]:
    history = registry.feeds.get(feed)
    return frozenset(() if history is None else (
        r.content_hash for r in history.revisions))  # fmt: skip


def _retention_end(ds: ResearchDataset, terms: DatasetTerms) -> datetime:
    held = [ds.manifest.frozen_at, *(f.received_at for f in ds.facts.facts),
            *(e.known_at for e in ds.universe.events)]  # fmt: skip
    try:
        return min(held) + terms.retain_for
    except OverflowError:
        raise DatasetStoreError("invalid", "retention end out of range") from None


def _usable(ds: ResearchDataset, terms: DatasetTerms, rights: ResearchRights) -> None:
    """Rights, feed revisions and retention at `rights.at` (the database clock)."""
    m = ds.manifest
    feeds = {f.feed_id for f in ds.facts.facts} | {
        e.feed_id for e in ds.universe.events
    }
    if not feeds <= m.sources.keys():  # the domain refuses this too
        raise DatasetStoreError("undeclared_source", m.dataset_id)
    if rights.at >= _retention_end(ds, terms):
        raise DatasetStoreError("retention_expired", m.dataset_id)
    try:
        require_research_use(rights, m.sources)
    except DatasetRefused as exc:  # revoked, expired, retired or never granted
        err = DatasetStoreError("rights_refused", str(exc))
        err.reasons = exc.reasons
        raise err from None
    claimed = {(f.feed_id, f.feed_hash) for f in ds.facts.facts}
    claimed |= set(m.sources.items())
    for feed, h in sorted(claimed):
        if h not in _revisions(rights.registry, feed):
            raise DatasetStoreError("feed_revision_unknown", feed)


def record(tx: TenantTx, dataset: ResearchDataset, terms: DatasetTerms) -> bool:
    """Store a frozen dataset; True when stored, False when this exact freeze is
    already stored (decision 74)."""
    if type(dataset) is not ResearchDataset or type(terms) is not DatasetTerms:
        raise DatasetStoreError("invalid", "a ResearchDataset and DatasetTerms")
    m = dataset.manifest
    if m.tenant_id != str(tx.tenant_id):
        raise DatasetStoreError("tenant_mismatch", m.dataset_id)
    rights = research_rights(tx, terms)
    if m.frozen_at > rights.at:
        raise DatasetStoreError("invalid", "frozen after the database clock")
    _usable(dataset, terms, rights)
    wire, tw = dataset.to_wire(), terms.to_wire()
    for attempt in (1, 2):
        try:
            with tx.conn.transaction():
                at = _now(tx)
                event = _hash(tx, m.dataset_id, m.content_hash, tw, at)
                values = (tx.tenant_id, m.dataset_id, m.content_hash, Jsonb(wire),
                          Jsonb(tw), at, event)  # fmt: skip
                cur = tx.conn.execute(
                    f"INSERT INTO app.research_dataset ({', '.join(_COLS)}) "
                    f"VALUES ({', '.join(['%s'] * len(_COLS))})",
                    values,
                )
                return cur.rowcount == 1
        except psycopg.errors.UniqueViolation as exc:
            raced = exc.diag.constraint_name == "research_dataset_pkey"
            if not (raced and attempt == 1):  # retried: the guard re-checks identity
                raise _database(exc) from None
        except psycopg.Error as exc:
            raise _database(exc) from None
    raise AssertionError("unreachable")


def load(tx: TenantTx, manifest: DatasetManifest) -> StoredDataset | None:
    """The stored dataset the plan's `manifest` names, re-verified and usable now."""
    if type(manifest) is not DatasetManifest:
        raise DatasetStoreError("invalid", "a DatasetManifest")
    try:
        row = tx.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM app.research_dataset "
            "WHERE tenant_id = %s AND dataset_id = %s",
            (tx.tenant_id, manifest.dataset_id),
        ).fetchone()
    except psycopg.Error as exc:
        raise _database(exc) from None
    if row is None:
        return None
    ds, terms, at = _verified(tx, dict(zip(_COLS, row, strict=True)), manifest)
    rights = research_rights(tx, terms)
    _usable(ds, terms, rights)
    return StoredDataset(ds, terms, rights, at)


def _verified(
    tx: TenantTx, r: dict[str, Any], manifest: DatasetManifest
) -> tuple[ResearchDataset, DatasetTerms, datetime]:
    try:
        ds = ResearchDataset.from_wire(r["body"])
        terms = DatasetTerms.from_wire(r["terms"])
        at = ensure_aware_utc(r["recorded_at"])
        m = ds.manifest
        if (
            (r["tenant_id"], r["dataset_id"], r["content_hash"])
            != (tx.tenant_id, m.dataset_id, m.content_hash)
            or m.tenant_id != str(tx.tenant_id)
            or m != manifest
            or r["event_hash"]
            != _hash(tx, m.dataset_id, m.content_hash, terms.to_wire(), at)
        ):
            raise ValueError("research_dataset")
    except (ValueError, TypeError, KeyError, ResearchError, DatasetStoreError) as exc:
        raise DatasetStoreError("integrity", type(exc).__name__) from None
    return ds, terms, at
