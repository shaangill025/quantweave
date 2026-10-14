"""Persisted data and rights registry (T021 increment 2; `0013_rights_registry.sql`;
spec §6, §16, §3 global versus tenant data, ADR-004).

The domain `Registry` is the model. `load` reads the installation's providers and
feeds (global) and the transaction tenant's entitlements (RLS), and rebuilds them
through `Registry`, whose construction replays every history from scratch
(hashes, revision numbers, transition and evidence rules). Any row the domain
would refuse, or one that is not canonical, is `RightsStoreError("integrity")`:
a tampered or forged registry never reaches `check_use`. An empty store loads
as the empty registry, which denies every use.

Writers append through the domain too: `record_provider`, `record_feed` and
`record_entitlement` take a record or history, require it to extend what is stored
(`Registry.with_*`: append-only, registered provider and feed, replay), insert only
the new rows and read them back. A refusal keeps the domain's code (append_only,
provider_unregistered, feed_unregistered, integrity); `tenant` when an entitlement
is for another tenant than the transaction's. Only the schema owner may insert
(0013 grants runtime roles SELECT only), so these are for tests and future
operator tooling; no HTTP route records rights. Revocation and expiry are new
revisions or transitions, never updates. Knowledge times must be the database
clock (the 0013 trigger); a refused insert is `RightsStoreError("database")`.
Reads take a shared advisory lock and writes an exclusive one, so a load sees one
state. Lock order: a request takes the evidence lock before this one, and writers
here take no other lock, so the two cannot deadlock. Every load replays the whole
registry. On any error the caller's transaction must roll back.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain.instants import ensure_aware_utc, parse_instant
from qw_domain.rights import (
    EntitlementHistory,
    EntitlementRevision,
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedHistory,
    FeedRevision,
    FeedStatus,
    Grant,
    Provider,
    Registry,
    RightsError,
    RightsProfile,
    RightState,
    Transition,
    Use,
    UseScope,
)

from qw_adapters.tenancy import Conn, TenantTx

__all__ = [
    "RightsStoreError",
    "load",
    "record_entitlement",
    "record_feed",
    "record_provider",
]
TABLES = (
    "rights_provider",
    "rights_feed",
    "rights_feed_revision",
    "rights_feed_transition",
    "rights_entitlement",
)


class RightsStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _evidence(w: list[Any]) -> Evidence:
    kind, ref, when, actor = w
    return Evidence(EvidenceKind(kind), ref, parse_instant(when), actor)


def _grant(w: list[Any]) -> Grant:
    state, ev, expires, keep = w
    if keep is not None and not (isinstance(keep, str) and keep.isdigit()):
        raise ValueError("retention")
    return Grant(
        RightState(state),
        None if ev is None else _evidence(ev),
        None if expires is None else parse_instant(expires),
        None if keep is None else timedelta(microseconds=int(keep)),
    )


def rights_of(j: Mapping[str, Any]) -> RightsProfile:
    """A stored `RightsProfile.to_wire()`; it must be canonical."""
    if set(j) != {"uses", "scopes", "jurisdictions"}:
        raise ValueError("rights keys")
    p = RightsProfile(
        {Use(k): _grant(v) for k, v in j["uses"].items()},
        {UseScope(k): _grant(v) for k, v in j["scopes"].items()},
        {k: _grant(v) for k, v in j["jurisdictions"].items()},
    )
    if p.to_wire() != dict(j):
        raise ValueError("rights are not canonical")
    return p


def _uses(j: list[Any]) -> frozenset[Use]:
    if j != sorted(set(j)):
        raise ValueError("required uses are not canonical")
    return frozenset(Use(u) for u in j)


def _utc(at: datetime) -> datetime:
    return ensure_aware_utc(at)


def _rows(conn: Conn, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _read(conn: Conn, tenant: str | None) -> Registry:
    providers = {
        i: Provider(i, n)
        for i, n in _rows(conn, "SELECT id, name FROM app.rights_provider")
    }
    owner = dict(_rows(conn, "SELECT id, provider_id FROM app.rights_feed"))
    revs: dict[str, list[FeedRevision]] = defaultdict(list)
    for f, n, feed, uses, rights, digest, at in _rows(
        conn,
        "SELECT feed_id, revision, feed, required_uses, rights, content_hash, "
        "recorded_at FROM app.rights_feed_revision ORDER BY feed_id, revision",
    ):
        revs[f].append(
            FeedRevision(
                f, owner[f], n, feed, _uses(uses), rights_of(rights), digest, _utc(at)
            )
        )
    steps: dict[str, list[Transition]] = defaultdict(list)
    for f, status, actor, ev, at, n, digest in _rows(
        conn,
        "SELECT feed_id, status, actor_id, evidence, at, revision, revision_hash "
        "FROM app.rights_feed_transition ORDER BY feed_id, position",
    ):
        evidence = tuple(_evidence(e) for e in ev)
        steps[f].append(
            Transition(FeedStatus(status), actor, evidence, _utc(at), n, digest)
        )
    feeds = {
        f: FeedHistory(f, p, tuple(revs[f]), tuple(steps[f])) for f, p in owner.items()
    }
    if set(revs) - set(owner) or set(steps) - set(owner):
        raise ValueError("orphan rows")
    ents: dict[tuple[str, str], list[EntitlementRevision]] = defaultdict(list)
    if tenant is not None:
        for f, n, src, active, rights, ref, digest, at in _rows(
            conn,
            "SELECT feed_id, revision, source, active, rights, "
            "pooling_consent_ref, content_hash, recorded_at FROM "
            "app.rights_entitlement WHERE tenant_id = %s ORDER BY feed_id, "
            "revision",
            (tenant,),
        ):
            ents[(tenant, f)].append(
                EntitlementRevision(
                    tenant,
                    f,
                    n,
                    EntitlementSource(src),
                    active,
                    rights_of(rights),
                    ref,
                    digest,
                    _utc(at),
                )
            )
    entitlements = {k: EntitlementHistory(*k, tuple(v)) for k, v in ents.items()}
    return Registry(providers, feeds, entitlements)


def _registry(conn: Conn, tenant: str | None) -> Registry:
    try:
        return _read(conn, tenant)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:  # RightsError
        raise RightsStoreError("integrity", str(exc)) from None


def _lock(conn: Conn, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {fn}(hashtextextended('rights', 0))")


def load(tx: TenantTx) -> Registry:
    """The installation's feeds and this tenant's entitlements, re-verified."""
    try:
        _lock(tx.conn, shared=True)
        return _registry(tx.conn, str(tx.tenant_id))
    except psycopg.Error as exc:
        raise RightsStoreError("database", type(exc).__name__) from None


@contextmanager
def _writing(conn: Conn, tenant: str | None) -> Iterator[Registry]:
    try:
        with conn.transaction():
            _lock(conn, shared=False)
            yield _registry(conn, tenant)
    except psycopg.Error as exc:
        raise RightsStoreError("database", type(exc).__name__) from None


def _extend(registry: Registry, step: str, record: object) -> None:
    try:
        getattr(registry, f"with_{step}")(record)
    except RightsError as exc:
        raise RightsStoreError(exc.code, str(exc)) from None


def _insert(conn: Conn, table: str, cols: str, rows: list[tuple[Any, ...]]) -> None:
    marks = ", ".join(["%s"] * (cols.count(",") + 1))
    with conn.cursor() as cur:
        cur.executemany(f"INSERT INTO app.{table} ({cols}) VALUES ({marks})", rows)


def record_provider(conn: Conn, provider: Provider) -> None:
    with _writing(conn, None) as reg:
        _extend(reg, "provider", provider)
        if provider.provider_id not in reg.providers:
            _insert(
                conn,
                "rights_provider",
                "id, name",
                [(provider.provider_id, provider.name)],
            )
        if _registry(conn, None).providers[provider.provider_id] != provider:
            raise RightsStoreError("integrity", "the stored provider differs")


def record_feed(conn: Conn, history: FeedHistory) -> None:
    """Append `history`'s new revisions and transitions (it extends the stored)."""
    with _writing(conn, None) as reg:
        _extend(reg, "feed", history)
        old, fid = reg.feeds.get(history.feed_id), history.feed_id
        if old is None:
            _insert(
                conn, "rights_feed", "id, provider_id", [(fid, history.provider_id)]
            )
        n, m = (0, 0) if old is None else (len(old.revisions), len(old.transitions))
        _insert(
            conn,
            "rights_feed_revision",
            "feed_id, revision, feed, required_uses, rights, content_hash, recorded_at",
            [
                (
                    fid,
                    r.revision,
                    r.feed,
                    Jsonb(sorted(r.required_uses)),
                    Jsonb(r.rights.to_wire()),
                    r.content_hash,
                    r.recorded_at,
                )
                for r in history.revisions[n:]
            ],
        )
        _insert(
            conn,
            "rights_feed_transition",
            "feed_id, position, status, "
            "actor_id, evidence, at, revision, revision_hash",
            [
                (
                    fid,
                    i,
                    t.status.value,
                    t.actor_id,
                    Jsonb([e.to_wire() for e in t.evidence]),
                    t.at,
                    t.revision,
                    t.revision_hash,
                )
                for i, t in enumerate(history.transitions)
                if i >= m
            ],
        )
        if _registry(conn, None).feeds[fid] != history:
            raise RightsStoreError("integrity", "the stored feed differs")


def record_entitlement(tx: TenantTx, history: EntitlementHistory) -> None:
    """Append the new revisions of this tenant's entitlement `history`."""
    tenant = str(tx.tenant_id)
    if history.tenant_id != tenant:
        raise RightsStoreError("tenant", "an entitlement of another tenant")
    with _writing(tx.conn, tenant) as reg:
        _extend(reg, "entitlement", history)
        key = (tenant, history.feed_id)
        old = reg.entitlements.get(key)
        _insert(
            tx.conn,
            "rights_entitlement",
            "tenant_id, feed_id, revision, "
            "source, active, rights, pooling_consent_ref, content_hash, "
            "recorded_at",
            [
                (
                    tx.tenant_id,
                    r.feed_id,
                    r.revision,
                    r.source.value,
                    r.active,
                    Jsonb(r.rights.to_wire()),
                    r.pooling_consent_ref,
                    r.content_hash,
                    r.recorded_at,
                )
                for r in history.revisions[0 if old is None else len(old.revisions) :]
            ],
        )
        if _registry(tx.conn, tenant).entitlements.get(key) != history:
            raise RightsStoreError("integrity", "the stored entitlement differs")
