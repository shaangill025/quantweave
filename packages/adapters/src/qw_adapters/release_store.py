"""The T051 release ledger (`0029_release_ledger.sql`): signed releases and each
deployment's install, rollback and pause events, written as qw_worker and replayed
through the domain `DeploymentLedger` on every read. Installation scope: no tenant
id and no qw_app privilege (0029); tenant adoptions stay in `strategy_store`.
- `record_deployment` stores a deployment once; a different one under its id is
  `deployment_frozen`. `record_release` re-derives the manifest with
  `prepare_release` from the caller's T044 registry and strategy manifest, so a
  signed manifest that skipped its gates (self-approval, verdict order, tenant
  scope, latest verdict, strategy binding) is refused with the domain's code, and
  `verify_release` must pass at the database clock (first reason as the code); an
  id stored with other content is `release_conflict`.
- `install` and `rollback` take the operator's trusted keys and the strategy
  registry the domain needs, load and replay the ledger, apply the domain act at
  the database clock and append its one new event (a `Pause` when no compatible
  earlier release exists). A domain refusal writes nothing and keeps its code.
- `load` re-hashes every row, rebuilds manifests through the domain and requires
  them to re-encode, checks seq 1..n, then replays all events; any
  failure, including a key revoked before an install it signed, is `integrity`.
  Unknown deployment and release ids read alike: None, or `not_found`.
- Owner decisions 23 and 33: a deployment's `SoloMaintainer` opt-in, each
  install's `self_approved` and each release's `verdict_source` are stored and
  hashed; a NULL (a row recorded before those columns) reads as no opt-in, not
  self-approved and caller-supplied, under its original hash.
LIMITATIONS: verdicts are the caller's registry (T044 N3, marked caller-supplied
per decision 33): the assessor store holds tenant-scope protocols only, and an
installation-scope release cannot cite one. Keys, adoptions (step-up reuse across
acts) and step-up receipts are the caller's. Hash-only, no C-20 signing: dropping
the latest events is not detected.
Nothing here deploys, contacts a network or touches broker state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from qw_domain import improvement as im
from qw_domain import release as rl
from qw_domain.instants import ensure_aware_utc, format_instant, parse_instant
from qw_domain.research_data import digest
from qw_domain.strategy_registry import StrategyManifest
from qw_domain.strategy_registry import StrategyRegistry as Strategies

Conn = psycopg.Connection[Any]
Row = dict[str, Any]
Keys = Mapping[str, rl.TrustedKey]


class ReleaseStoreError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _rows(conn: Conn, sql: str, *args: object) -> list[Row]:
    try:
        cur = conn.execute(sql, args)
        names = [c.name for c in cur.description or ()]
        return [dict(zip(names, r, strict=True)) for r in cur]
    except psycopg.Error as exc:
        raise ReleaseStoreError("database", type(exc).__name__) from None


def _now(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def _hash(table: str, body: object, at: datetime) -> str:
    return digest([table, body, format_instant(at)])


def _insert(conn: Conn, table: str, values: Row) -> None:
    cols = ", ".join(values)  # this module's literal names
    try:
        with conn.transaction():
            conn.execute(f"INSERT INTO app.{table} ({cols}) VALUES "
                         f"({', '.join(['%s'] * len(values))})",
                         tuple(values.values()))  # fmt: skip
    except psycopg.Error as exc:
        raise ReleaseStoreError("database", type(exc).__name__) from None


def _deployment_wire(d: rl.Deployment, legacy: bool = False) -> list[object]:
    base: list[object] = [d.deployment_id, d.edition.value, sorted(d.operators),
                          sorted(d.tenants), d.initial_schema,
                          d.allows_test_keys]  # fmt: skip
    return base if legacy else [*base, d.solo is not None]  # decision 23 is hashed


def _deployment(conn: Conn, deployment_id: str) -> rl.Deployment | None:
    rows = _rows(conn, "SELECT * FROM app.release_deployment WHERE deployment_id = %s",
                 deployment_id)  # fmt: skip
    if not rows:
        return None
    r = rows[0]
    if any(list(r[k]) != sorted(set(r[k])) for k in ("operators", "tenants")):
        raise ReleaseStoreError("integrity", "release_deployment members")
    try:
        ed, ops = rl.Edition(r["edition"]), frozenset(r["operators"])
        d = rl.Deployment(
            r["deployment_id"],
            ed,
            ops,
            frozenset(r["tenants"]),
            r["initial_schema"],
            r["allows_test_keys"],
            rl.SoloMaintainer(ed, ops) if r["solo_maintainer"] else None,
        )
        wire = _deployment_wire(d, legacy=r["solo_maintainer"] is None)
        ok = r["event_hash"] == _hash("release_deployment", wire,
                                      ensure_aware_utc(r["recorded_at"]))  # fmt: skip
    except (ValueError, TypeError) as exc:
        raise ReleaseStoreError("integrity", type(exc).__name__) from None
    if not ok:
        raise ReleaseStoreError("integrity", "release_deployment")
    return d


def record_deployment(conn: Conn, d: rl.Deployment) -> None:
    """Store the deployment once; the same deployment again writes nothing."""
    if type(d) is not rl.Deployment:
        raise TypeError("the domain deployment is required")
    rl.DeploymentLedger(d, {})  # the domain's own checks
    with conn.transaction():
        _lock(conn, d.deployment_id, shared=False)
        stored = _deployment(conn, d.deployment_id)
        if stored is not None:
            if stored != d:
                raise ReleaseStoreError("deployment_frozen", d.deployment_id)
            return
        at = _now(conn)
        _insert(conn, "release_deployment", {
            "deployment_id": d.deployment_id, "edition": d.edition.value,
            "operators": sorted(d.operators), "tenants": sorted(d.tenants),
            "initial_schema": d.initial_schema,
            "allows_test_keys": d.allows_test_keys, "recorded_at": at,
            "solo_maintainer": d.solo is not None,
            "event_hash": _hash("release_deployment", _deployment_wire(d), at),
        })  # fmt: skip


def _release_wire(sr: rl.SignedRelease, legacy: bool = False) -> list[object]:
    base: list[object] = [sr.manifest.wire(), sr.manifest_hash, sr.key_id,
                          sr.algorithm, sr.signature]  # fmt: skip
    return base if legacy else [*base, sr.verdict_source.value]  # decision 33


def _manifest(w: Mapping[str, Any]) -> rl.ReleaseManifest:
    c, (version, material), a = w["compatibility"], w["strategy"], w["approval"]
    m = rl.ReleaseManifest(
        w["release_id"], w["component"], im.Target(w["target"]), w["experiment_id"],
        w["candidate_id"], w["candidate_hash"], w["artifact_hash"],
        w["protocol_hash"], w["verdict_id"], tuple(w["required_approvals"]),
        rl.Compatibility(*c), version, material, w["rollback_version_id"],
        rl.MaintainerApproval(a[0], a[1], parse_instant(a[2])),
    )  # fmt: skip
    if m.wire() != w:
        raise ValueError("manifest does not re-encode")
    return m


def _release(conn: Conn, release_id: str) -> rl.SignedRelease | None:
    rows = _rows(conn, "SELECT * FROM app.signed_release WHERE release_id = %s",
                 release_id)  # fmt: skip
    if not rows:
        return None
    r = rows[0]
    try:
        m = _manifest(r["body"])
        src = r["verdict_source"]  # NULL: recorded before decision 33's mark
        sr = rl.SignedRelease(m, r["manifest_hash"], r["key_id"], r["algorithm"],
                              r["signature"],
                              rl.VerdictSource(src or "caller_supplied"))  # fmt: skip
        ok = (  # 0029 CHECKs bind the other columns to the body
            sr.manifest_hash == m.manifest_hash
            and ensure_aware_utc(r["approved_at"]) == m.approval.approved_at
            and r["event_hash"] == _hash("signed_release",
                                         _release_wire(sr, legacy=src is None),
                                         ensure_aware_utc(r["recorded_at"]))
        )  # fmt: skip
    except (ValueError, TypeError, KeyError) as exc:  # domain errors included
        raise ReleaseStoreError("integrity", type(exc).__name__) from None
    if not ok:
        raise ReleaseStoreError("integrity", "signed_release")
    return sr


def record_release(conn: Conn, registry: im.ExperimentRegistry,
                   sr: rl.SignedRelease, strategy: StrategyManifest | None = None, *,
                   keys: Keys, allow_test_keys: bool) -> bool:  # fmt: skip
    """Store a signed release that `prepare_release` re-derives exactly from the
    registry and that verifies against the operator's trusted keys now (so no
    unverifiable release can claim an id); True when written, False when already
    stored with this content."""
    if type(registry) is not im.ExperimentRegistry or type(sr) is not rl.SignedRelease:
        raise TypeError("the domain registry and signed release are required")
    m = sr.manifest
    try:
        again = rl.prepare_release(registry, m.candidate_id, m.release_id,
                                   m.compatibility, m.approval, strategy)  # fmt: skip
    except rl.ReleaseError as exc:
        raise ReleaseStoreError(exc.code, m.release_id) from None
    if again != m:
        raise ReleaseStoreError("manifest_mismatch", m.release_id)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"release_id:{m.release_id}",))  # fmt: skip
        if why := rl.verify_release(sr, keys, _now(conn), allow_test_keys):
            raise ReleaseStoreError(why[0], ",".join(why))
        stored = _release(conn, m.release_id)
        if stored is not None:
            if stored != sr:
                raise ReleaseStoreError("release_conflict", m.release_id)
            return False
        at, a, c = _now(conn), m.approval, m.compatibility
        _insert(conn, "signed_release", {
            "release_id": m.release_id, "component": m.component,
            "target": m.target.value, "candidate_id": m.candidate_id,
            "candidate_hash": m.candidate_hash, "verdict_id": m.verdict_id,
            "manifest_hash": sr.manifest_hash, "approver_id": a.principal_id,
            "approved_at": a.approved_at, "min_schema": c.min_schema,
            "max_schema": c.max_schema, "writes_schema": c.writes_schema,
            "key_id": sr.key_id, "algorithm": sr.algorithm,
            "signature": sr.signature, "body": Jsonb(m.wire()), "recorded_at": at,
            "verdict_source": sr.verdict_source.value,
            "event_hash": _hash("signed_release", _release_wire(sr), at),
        })  # fmt: skip
    return True


def _lock(conn: Conn, deployment_id: str, shared: bool) -> None:
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))",
                 (f"release:{deployment_id}",))  # fmt: skip


def _event_values(deployment_id: str, seq: int, e: rl.Event, schema: int,
                  legacy: bool = False) -> Row:  # fmt: skip
    v: Row = (
        {}
        if legacy
        else {  # decision 23 is hashed
            "self_approved": isinstance(e, rl.Install) and e.self_approved
        }
    )
    v |= {"deployment_id": deployment_id, "seq": seq, "event_id": e.event_id,
              "to_release_id": None, "reasons": [], "operator_id": e.operator_id,
              "step_up_receipt_id": e.step_up_receipt_id,
              "schema_after": schema}  # fmt: skip
    if isinstance(e, rl.Install):
        m = e.release.manifest
        v |= {"kind": "install", "component": m.component, "release_id": m.release_id}
    elif isinstance(e, rl.Rollback):
        v |= {"kind": "rollback", "component": e.component,
              "release_id": e.from_release_id,
              "to_release_id": e.to_release_id}  # fmt: skip
    else:
        v |= {"kind": "pause", "component": e.component, "release_id": e.release_id,
              "reasons": list(e.reasons)}  # fmt: skip
    return v


def _event_body(v: Row) -> list[object]:
    return [v[k] for k in sorted(v)]


def _ledger(conn: Conn, deployment_id: str, keys: Keys) -> rl.DeploymentLedger | None:
    d = _deployment(conn, deployment_id)
    if d is None:
        return None
    rows = _rows(conn, "SELECT * FROM app.deployment_event WHERE deployment_id = %s "
                 "ORDER BY seq", deployment_id)  # fmt: skip
    releases: dict[str, rl.SignedRelease | None] = {}

    def rel(rid: str) -> rl.SignedRelease:
        if rid not in releases:
            releases[rid] = _release(conn, rid)
        if (sr := releases[rid]) is None:
            raise ValueError("unknown release")
        return sr

    builds: dict[str, Callable[[Row, datetime], rl.Event]] = {
        "install": lambda r, at: rl.Install(r["event_id"], rel(r["release_id"]),
                                            r["operator_id"], r["step_up_receipt_id"],
                                            at, r["self_approved"] is True),
        "rollback": lambda r, at: rl.Rollback(
            r["event_id"], r["component"], r["release_id"], r["to_release_id"],
            r["operator_id"], r["step_up_receipt_id"], at),
        "pause": lambda r, at: rl.Pause(
            r["event_id"], r["component"], r["release_id"], tuple(r["reasons"]),
            r["operator_id"], r["step_up_receipt_id"], at),
    }  # fmt: skip
    try:
        entries = []
        for n, r in enumerate(rows, 1):
            at = ensure_aware_utc(r["at"])
            e = builds[r["kind"]](r, at)
            v = _event_values(deployment_id, n, e, r["schema_after"],
                              r["self_approved"] is None)  # fmt: skip
            if {k: r[k] for k in v} != v or r["event_hash"] != _hash(
                "deployment_event", _event_body(v), at
            ):  # fmt: skip
                raise ValueError(f"event {n}")
            entries.append(e)
        lg = rl.DeploymentLedger(d, keys, tuple(entries))
    except (ValueError, TypeError, KeyError) as exc:  # ReleaseError included
        raise ReleaseStoreError("integrity", type(exc).__name__) from None
    return lg


def load(conn: Conn, deployment_id: str, keys: Keys) -> rl.DeploymentLedger | None:
    """The deployment's ledger, replayed and verified; None when unknown."""
    with conn.transaction():
        _lock(conn, deployment_id, shared=True)
        return _ledger(conn, deployment_id, keys)


def _append(conn: Conn, deployment_id: str, keys: Keys,
            act: Callable[[rl.DeploymentLedger, datetime], rl.DeploymentLedger],
            ) -> rl.DeploymentLedger:  # fmt: skip
    with conn.transaction():
        _lock(conn, deployment_id, shared=False)
        lg = _ledger(conn, deployment_id, keys)
        if lg is None:
            raise ReleaseStoreError("not_found")
        at = _now(conn)
        try:
            new = act(lg, at)
        except rl.ReleaseError as exc:
            raise ReleaseStoreError(exc.code, str(exc)) from None
        e = new.entries[-1]
        v = _event_values(deployment_id, len(new.entries), e, new.schema)
        _insert(conn, "deployment_event", v | {
            "at": e.at, "event_hash": _hash("deployment_event", _event_body(v), e.at),
        })  # fmt: skip
        got = _ledger(conn, deployment_id, keys)
        if got is None or got.entries != new.entries:
            raise ReleaseStoreError("integrity", "the stored ledger differs")
    return got


def install(conn: Conn, deployment_id: str, keys: Keys, event_id: str,
            release_id: str, operator_id: str, step_up: str, *,
            strategies: Strategies) -> rl.DeploymentLedger:  # fmt: skip
    """The operator's install of a stored release, at the database clock."""

    def act(lg: rl.DeploymentLedger, at: datetime) -> rl.DeploymentLedger:
        sr = _release(conn, release_id)
        if sr is None:
            raise ReleaseStoreError("not_found")
        return lg.install(event_id, sr, operator_id, step_up, at,
                          strategies=strategies)  # fmt: skip

    return _append(conn, deployment_id, keys, act)


def rollback(conn: Conn, deployment_id: str, keys: Keys, event_id: str,
             component: str, operator_id: str, step_up: str, *,
             strategies: Strategies, to_release_id: str | None = None,
             ) -> rl.DeploymentLedger:  # fmt: skip
    """The domain rollback, or the `Pause` it appends, at the database clock."""
    return _append(conn, deployment_id, keys, lambda lg, at: lg.rollback(
        event_id, component, operator_id, step_up, at, strategies=strategies,
        to_release_id=to_release_id))  # fmt: skip
