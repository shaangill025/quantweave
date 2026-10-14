"""Persisted strategy registry (T027; `0015_strategy_registry.sql`; spec §8, §11/§16
adoption, §3 global versus tenant data, T008 C-20/C-22).

The domain `StrategyRegistry` is the model. `load` reads the installation's
manifests (global) and the transaction tenant's adoption receipts (RLS, and an
explicit tenant filter), rebuilds each manifest through the domain constructors
(`StrategyManifest`, `Parameter`, `DataNeed` validation), requires the stored body to
re-encode exactly (keys, numbers as strings, integer form, retention in microseconds)
and the stored family, material and content hashes to match, checks each receipt
hash, and replays the receipts through `StrategyRegistry`. Anything the domain would
refuse, or that does not re-encode, is `StrategyStoreError("integrity")`. The order of
`data` and `parameters` and a Decimal's exponent (0.05 vs 0.050) are free: the hashes
normalise them, so such an edit changes nothing served. An empty store loads as the
empty registry. `snapshot`'s "latest" version is the last registered (`seq`), not the
highest version: recording 0.2.0 then 0.1.1 serves 0.1.1. A version-ordering rule is
needed before release tooling exists.
Rows rewritten together with matching hashes (an owner bypassing triggers) are not
detected; a changed body under the stored hashes is.

Writers go through the domain: `record_manifest` (`register`: a changed manifest
under a stored version is `immutable`, the same content is a no-op) and
`record_adoption` (`adopt`: bounded configuration, registered manifest, receipts in
signing order). Only the schema owner may insert (0015 grants runtime roles SELECT
only): these are for tests and future operator tooling. The adopt route is not
served; it must verify step-up and If-Match before any runtime role may insert.
`signed_at` must be the database clock (the 0015 trigger); a refused insert is
`database`. Reads take a shared advisory lock and writes an exclusive one. Family
policies and qualification steps are not persisted, so `status` reads every axis
at its initial state. On any error the caller's transaction must roll back.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import strategy_registry as sr
from qw_domain.instants import ensure_aware_utc
from qw_domain.rights import Use

from qw_adapters.tenancy import Conn, TenantTx

__all__ = ["Snapshot", "StrategyStoreError", "load", "record_adoption",
           "record_manifest", "snapshot"]  # fmt: skip
TABLES = ("strategy_manifest", "strategy_adoption")
_KEYS = {"strategy_id", "version", "family", "implementation_hash", "data",
         "asset_classes", "horizon", "regular_session_only", "parameters",
         "research_protocol_id", "model_ref", "title"}  # fmt: skip


class StrategyStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Snapshot:
    registry: sr.StrategyRegistry
    latest: tuple[sr.StrategyManifest, ...]  # latest version per strategy, by id
    at: datetime  # the database clock


def _value(v: sr.ParamValue | None) -> object:
    if v is None or isinstance(v, bool):
        return v
    return str(v)


def _parse(kind: sr.ParamKind, v: object) -> Any:
    if v is None or kind is sr.ParamKind.BOOLEAN or kind is sr.ParamKind.CHOICE:
        return v
    if not isinstance(v, str):
        raise ValueError("numbers are strings")
    return int(v) if kind is sr.ParamKind.INTEGER else Decimal(v)


def _micros(keep: timedelta | None) -> str | None:
    return None if keep is None else str(keep // timedelta(microseconds=1))


def body(m: sr.StrategyManifest) -> dict[str, Any]:
    """The canonical stored form; declaration order is kept."""
    return {
        "strategy_id": m.strategy_id, "version": m.version, "family": m.family.value,
        "implementation_hash": m.implementation_hash,
        "data": [[d.feed_id, sorted(u.value for u in d.uses), _micros(d.retain_for)]
                 for d in m.data],
        "asset_classes": sorted(a.value for a in m.asset_classes),
        "horizon": m.horizon.value, "regular_session_only": m.regular_session_only,
        "parameters": [[p.name, p.kind.value, _value(p.default), _value(p.minimum),
                        _value(p.maximum), list(p.allowed)] for p in m.parameters],
        "research_protocol_id": m.research_protocol_id, "model_ref": m.model_ref,
        "title": m.title,
    }  # fmt: skip


def manifest_of(j: Mapping[str, Any]) -> sr.StrategyManifest:
    """A stored `body`; it must rebuild through the domain and be canonical."""
    if set(j) != _KEYS:
        raise ValueError("manifest keys")
    params = []
    for name, kind, default, lo, hi, allowed in j["parameters"]:
        k = sr.ParamKind(kind)
        p = (_parse(k, default), _parse(k, lo), _parse(k, hi))
        params.append(sr.Parameter(name, k, *p, tuple(allowed)))
    data = tuple(
        sr.DataNeed(f, frozenset(Use(u) for u in uses),
                    None if keep is None else timedelta(microseconds=int(keep)))
        for f, uses, keep in j["data"]
    )  # fmt: skip
    m = sr.StrategyManifest(
        j["strategy_id"], j["version"], sr.Family(j["family"]),
        j["implementation_hash"], data,
        frozenset(sr.AssetClass(a) for a in j["asset_classes"]),
        sr.Horizon(j["horizon"]), j["regular_session_only"], tuple(params),
        j["research_protocol_id"], j["model_ref"], j["title"],
    )  # fmt: skip
    if body(m) != dict(j):
        raise ValueError("manifest is not canonical")
    return m


def _rows(conn: Conn, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _read(conn: Conn, tenant: str | None) -> tuple[sr.StrategyRegistry, list[Any]]:
    manifests, order = {}, []
    for sid, ver, family, j, material, content in _rows(
        conn,
        "SELECT strategy_id, version, family, body, material_hash, content_hash "
        "FROM app.strategy_manifest ORDER BY seq",
    ):
        m = manifest_of(j)
        stored = (sid, ver, family, material, content)
        if stored != (m.strategy_id, m.version, m.family.value, m.material_hash,
                      m.content_hash):  # fmt: skip
            raise ValueError(f"{sid}@{ver} does not match its hashes")
        manifests[(sid, ver)] = m
        order.append(m)
    receipts: dict[tuple[str, str], list[sr.StrategyAdoption]] = defaultdict(list)
    if tenant is not None:
        for row in _rows(
            conn,
            "SELECT strategy_id, version, manifest_hash, config_hash, principal_id, "
            "step_up_receipt_id, signed_at, acknowledgement_text, receipt_hash FROM "
            "app.strategy_adoption WHERE tenant_id = %s ORDER BY strategy_id, "
            "position",
            (tenant,),
        ):
            sid, ver, mh, ch, who, step, signed, text, digest = row
            at = ensure_aware_utc(signed)
            r = sr.StrategyAdoption(tenant, sid, ver, mh, ch, who, step, at, text)
            if r.receipt_hash != digest:
                raise ValueError(f"receipt {r.strategy_id} does not match its hash")
            receipts[(tenant, r.strategy_id)].append(r)
    adoptions = {k: tuple(v) for k, v in receipts.items()}
    return sr.StrategyRegistry(manifests, {}, {}, adoptions), order


def _registry(conn: Conn, tenant: str | None) -> tuple[sr.StrategyRegistry, list[Any]]:
    try:
        return _read(conn, tenant)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:  # StrategyError
        raise StrategyStoreError("integrity", str(exc)) from None


def _lock(conn: Conn, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {fn}(hashtextextended('strategy', 0))")


def load(tx: TenantTx) -> sr.StrategyRegistry:
    """The installation's manifests and this tenant's adoptions, re-verified."""
    return snapshot(tx).registry


def snapshot(tx: TenantTx) -> Snapshot:
    """`load` plus the latest version of each strategy and the database clock."""
    try:
        _lock(tx.conn, shared=True)
        reg, order = _registry(tx.conn, str(tx.tenant_id))
        (at,) = _rows(tx.conn, "SELECT clock_timestamp()")[0]
    except psycopg.Error as exc:
        raise StrategyStoreError("database", type(exc).__name__) from None
    latest = {m.strategy_id: m for m in order}  # registration order: last wins
    return Snapshot(reg, tuple(latest[k] for k in sorted(latest)), ensure_aware_utc(at))


@contextmanager
def _writing(conn: Conn, tenant: str | None) -> Iterator[sr.StrategyRegistry]:
    try:
        with conn.transaction():
            _lock(conn, shared=False)
            yield _registry(conn, tenant)[0]
    except psycopg.Error as exc:
        raise StrategyStoreError("database", type(exc).__name__) from None
    except sr.StrategyError as exc:
        raise StrategyStoreError(exc.code, str(exc)) from None


def record_manifest(conn: Conn, m: sr.StrategyManifest) -> None:
    """Register one manifest version (installation-wide)."""
    with _writing(conn, None) as reg:
        key = (m.strategy_id, m.version)
        if reg.register(m) is reg:
            return
        conn.execute(
            "INSERT INTO app.strategy_manifest (strategy_id, version, family, body, "
            "material_hash, content_hash) VALUES (%s, %s, %s, %s, %s, %s)",
            (*key, m.family.value, Jsonb(body(m)), m.material_hash, m.content_hash),
        )
        if _registry(conn, None)[0].manifests.get(key) != m:
            raise StrategyStoreError("integrity", "the stored manifest differs")


def record_adoption(
    tx: TenantTx,
    strategy_id: str,
    version: str,
    config: Mapping[str, object],
    consent: sr.StrategyConsent,
) -> None:
    """Append this tenant's adoption receipt; `consent.signed_at` is the DB clock."""
    tenant = str(tx.tenant_id)
    with _writing(tx.conn, tenant) as reg:
        after = reg.adopt(tenant, strategy_id, version, config, consent)
        held = after.adoptions[(tenant, strategy_id)]
        r = held[-1]
        tx.conn.execute(
            "INSERT INTO app.strategy_adoption (tenant_id, strategy_id, position, "
            "version, manifest_hash, config_hash, principal_id, step_up_receipt_id, "
            "signed_at, acknowledgement_text, receipt_hash) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tx.tenant_id, strategy_id, len(held) - 1, r.version, r.manifest_hash,
             r.config_hash, r.principal_id, r.step_up_receipt_id, r.signed_at,
             r.acknowledgement_text, r.receipt_hash),
        )  # fmt: skip
        if _registry(tx.conn, tenant)[0].adoptions.get((tenant, strategy_id)) != held:
            raise StrategyStoreError("integrity", "the stored receipt differs")
