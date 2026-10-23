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
- Owner decision 45: the operator (qw_release, `0035_release_keys.sql`) records the
  deployment and the trusted keys (`record_key`) and revokes one (`revoke_key`,
  at the database clock). `stored_keys` re-hashes them and builds the domain
  `TrustedKey`s; `load`, `install`, `rollback` and `record_release` use them when
  no `keys` are given. Public key material only: a non-test key needs an algorithm
  in `PRODUCTION_ALGORITHMS` (empty until T009) and a verifier from `VERIFIERS`
  (none yet, so a stored public key verifies nothing); a test-only key stores no
  material and is trusted only when the caller's in-process `test_verifiers`
  supplies its verifier. A secret never reaches a table: PEM private keys and
  material on a test key are refused.
- Owner decision 45, the read: `history` serves qw_app, which holds no privilege
  on these tables, through 0037's member-only SECURITY DEFINER reads; the
  deployment's tenant list never leaves the database (its hash is checked there).
LIMITATIONS: verdicts are the caller's registry (T044 N3, marked caller-supplied
per decision 33): the assessor store holds tenant-scope protocols only, and an
installation-scope release cannot cite one. Keys, adoptions (step-up reuse across
acts) and step-up receipts are the caller's. Hash-only, no C-20 signing: dropping
the latest events is not detected.
Nothing here deploys, contacts a network or touches broker state."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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

from qw_adapters.tenancy import TenantTx

Conn = psycopg.Connection[Any]
Row = dict[str, Any]
Keys = Mapping[str, rl.TrustedKey]
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
ALGORITHM = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
# Verifier factories from public key material, per reviewed algorithm (T009).
VERIFIERS: Mapping[str, Callable[[str], rl.Verifier]] = {}


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


def record_deployment(conn: Conn, d: rl.Deployment) -> bool:
    """Store the deployment once (True); the same deployment again writes nothing
    (False)."""
    if type(d) is not rl.Deployment:
        raise TypeError("the domain deployment is required")
    rl.DeploymentLedger(d, {})  # the domain's own checks
    with conn.transaction():
        _lock(conn, d.deployment_id, shared=False)
        stored = _deployment(conn, d.deployment_id)
        if stored is not None:
            if stored != d:
                raise ReleaseStoreError("deployment_frozen", d.deployment_id)
            return False
        at = _now(conn)
        _insert(conn, "release_deployment", {
            "deployment_id": d.deployment_id, "edition": d.edition.value,
            "operators": sorted(d.operators), "tenants": sorted(d.tenants),
            "initial_schema": d.initial_schema,
            "allows_test_keys": d.allows_test_keys, "recorded_at": at,
            "solo_maintainer": d.solo is not None,
            "event_hash": _hash("release_deployment", _deployment_wire(d), at),
        })  # fmt: skip
    return True


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
    return _release_of(rows[0]) if rows else None


def _release_of(r: Row) -> rl.SignedRelease:
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
                   allow_test_keys: bool,
                   keys: Keys | None = None) -> bool:  # fmt: skip
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
        trusted = stored_keys(conn) if keys is None else keys
        if why := rl.verify_release(sr, trusted, _now(conn), allow_test_keys):
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


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """A trusted maintainer key as the operator records it (owner decision 45)."""

    key_id: str
    principal_id: str
    algorithm: str
    valid_from: datetime
    valid_until: datetime | None
    public_key: str | None  # public material only; None exactly for a test key
    test_only: bool = False


class _NoVerifier:
    """A stored public key whose algorithm has no reviewed verifier: never verifies."""

    def verify(self, message: bytes, signature: str) -> bool:
        return False


def _key_wire(k: KeyRecord) -> list[object]:
    until = None if k.valid_until is None else format_instant(k.valid_until)
    return [k.key_id, k.principal_id, k.algorithm, k.public_key, k.test_only,
            format_instant(k.valid_from), until]  # fmt: skip


def _check_key(k: KeyRecord) -> None:
    for name in ("key_id", "principal_id"):
        if not isinstance(v := getattr(k, name), str) or not ID.fullmatch(v):
            raise ReleaseStoreError(name, "not an id")
    if not isinstance(k.algorithm, str) or not ALGORITHM.fullmatch(k.algorithm):
        raise ReleaseStoreError("algorithm", "not an algorithm name")
    if k.valid_until is not None and k.valid_until <= k.valid_from:
        raise ReleaseStoreError("key_window", "valid_until must follow valid_from")
    if k.test_only:
        if k.public_key is not None:  # its verifier is a secret: never stored
            raise ReleaseStoreError("test_key_material", "a test key stores nothing")
        return
    if not k.public_key:
        raise ReleaseStoreError("public_key_required", k.key_id)
    if "PRIVATE KEY" in k.public_key.upper() or len(k.public_key) > 8192:
        raise ReleaseStoreError("private_key_material", "public material only")
    if k.algorithm not in rl.PRODUCTION_ALGORITHMS:  # asymmetric, reviewed (T009)
        raise ReleaseStoreError("algorithm_not_production", k.algorithm)


def _key_rows(conn: Conn, member_of: str | None = None,
              ) -> tuple[list[Row], dict[str, Row]]:  # fmt: skip
    """The tables (operator and worker), or 0037's member reads (qw_app)."""
    if member_of is None:
        keys = _rows(conn, "SELECT * FROM app.release_trusted_key ORDER BY key_id")
        revoked = _rows(conn, "SELECT * FROM app.release_key_revocation "
                        "ORDER BY key_id")  # fmt: skip
    else:
        keys = _rows(conn, "SELECT * FROM app.release_history_keys(%s)", member_of)
        revoked = _rows(conn, "SELECT * FROM app.release_history_revocations(%s)",
                        member_of)  # fmt: skip
    return keys, {r["key_id"]: r for r in revoked}


def _key_record(r: Row) -> KeyRecord:
    until = r["valid_until"]
    k = KeyRecord(r["key_id"], r["principal_id"], r["algorithm"],
                  ensure_aware_utc(r["valid_from"]),
                  None if until is None else ensure_aware_utc(until),
                  r["public_key"], r["test_only"])  # fmt: skip
    if r["event_hash"] != _hash("release_trusted_key", _key_wire(k),
                                ensure_aware_utc(r["recorded_at"])):  # fmt: skip
        raise ReleaseStoreError("integrity", "release_trusted_key")
    return k


def stored_keys(conn: Conn, test_verifiers: Mapping[str, rl.Verifier] | None = None,
                member_of: str | None = None) -> dict[str, rl.TrustedKey]:  # fmt: skip
    """The operator's trusted keys and revocations, re-hashed; a test key appears
    only with its verifier from `test_verifiers` (in process, never stored).
    `member_of` reads them through 0037 as a member of that deployment."""
    hook = test_verifiers or {}
    rows, revoked = _key_rows(conn, member_of)
    out: dict[str, rl.TrustedKey] = {}
    try:
        for r in rows:
            k, rv = _key_record(r), revoked.get(r["key_id"])
            at = None
            if rv is not None:
                at = ensure_aware_utc(rv["revoked_at"])
                if rv["event_hash"] != _hash("release_key_revocation",
                                             [k.key_id, rv["reason"]], at):  # fmt: skip
                    raise ReleaseStoreError("integrity", "release_key_revocation")
            if k.test_only:
                if k.key_id not in hook:
                    continue  # not trusted in this process
                verifier = hook[k.key_id]
            else:
                factory = VERIFIERS.get(k.algorithm)
                verifier = factory(k.public_key or "") if factory else _NoVerifier()
            out[k.key_id] = rl.TrustedKey(k.key_id, k.algorithm, k.principal_id,
                                          verifier, k.valid_from, at, k.test_only,
                                          k.valid_until)  # fmt: skip
        if set(revoked) - {r["key_id"] for r in rows}:
            raise ReleaseStoreError("integrity", "a revocation of no key")
    except (ValueError, TypeError) as exc:  # ReleaseError included
        raise ReleaseStoreError("integrity", type(exc).__name__) from None
    return out


def record_key(conn: Conn, k: KeyRecord) -> bool:
    """The operator records a trusted key; True when written, False when the same
    key is already stored (another key under its id is `key_conflict`)."""
    _check_key(k)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("release_keys",))  # fmt: skip
        rows = _rows(conn, "SELECT * FROM app.release_trusted_key WHERE key_id = %s",
                     k.key_id)  # fmt: skip
        if rows:
            if _key_record(rows[0]) != k:
                raise ReleaseStoreError("key_conflict", k.key_id)
            return False
        at = _now(conn)
        _insert(conn, "release_trusted_key", {
            "key_id": k.key_id, "principal_id": k.principal_id,
            "algorithm": k.algorithm, "public_key": k.public_key,
            "test_only": k.test_only, "valid_from": k.valid_from,
            "valid_until": k.valid_until, "recorded_at": at,
            "event_hash": _hash("release_trusted_key", _key_wire(k), at),
        })  # fmt: skip
    return True


def revoke_key(conn: Conn, key_id: str, reason: str) -> datetime:
    """Append the key's one revocation at the database clock (never back-dated)."""
    if not isinstance(reason, str) or not 1 <= len(reason) <= 500 or any(
        unicodedata.category(c) == "Cc" for c in reason
    ):  # fmt: skip
        raise ReleaseStoreError("reason", "1 to 500 characters, no control")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("release_keys",))  # fmt: skip
        rows, revoked = _key_rows(conn)
        if key_id not in {r["key_id"] for r in rows}:
            raise ReleaseStoreError("not_found", key_id)
        if key_id in revoked:
            raise ReleaseStoreError("already_revoked", key_id)
        at = _now(conn)
        _insert(conn, "release_key_revocation", {
            "key_id": key_id, "reason": reason, "revoked_at": at,
            "event_hash": _hash("release_key_revocation", [key_id, reason], at),
        })  # fmt: skip
    return at


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

    def rel(rid: str) -> rl.SignedRelease | None:
        if rid not in releases:
            releases[rid] = _release(conn, rid)
        return releases[rid]

    return _replay(d, rows, rel, keys)


def _replay(d: rl.Deployment, rows: list[Row],
            release: Callable[[str], rl.SignedRelease | None],
            keys: Keys) -> rl.DeploymentLedger:  # fmt: skip
    """Rebuild, re-hash and replay the stored events; any failure is `integrity`."""
    deployment_id = d.deployment_id

    def rel(rid: str) -> rl.SignedRelease:
        if (sr := release(rid)) is None:
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


def load(conn: Conn, deployment_id: str,
         keys: Keys | None = None) -> rl.DeploymentLedger | None:  # fmt: skip
    """The deployment's ledger, replayed and verified against `keys` or, by
    default, the operator's stored keys; None when unknown."""
    with conn.transaction():
        _lock(conn, deployment_id, shared=True)
        return _ledger(conn, deployment_id,
                       stored_keys(conn) if keys is None else keys)  # fmt: skip


@dataclass(frozen=True, slots=True)
class History:
    """A member's view (0037): the replayed ledger, whose deployment names only the
    caller's tenant, the stored keys it was verified with and the database clock."""

    ledger: rl.DeploymentLedger
    keys: Keys
    at: datetime


def _member_deployment(conn: Conn, deployment_id: str) -> rl.Deployment | None:
    rows = _rows(conn, "SELECT * FROM app.release_history_deployment(%s)",
                 deployment_id)  # fmt: skip
    if not rows:
        return None
    r = rows[0]
    if r["hash_ok"] is not True or list(r["operators"]) != sorted(set(r["operators"])):
        raise ReleaseStoreError("integrity", "release_deployment")
    try:
        ed, ops = rl.Edition(r["edition"]), frozenset(r["operators"])
        return rl.Deployment(r["deployment_id"], ed, ops, frozenset(r["tenants"]),
                             r["initial_schema"], r["allows_test_keys"],
                             rl.SoloMaintainer(ed, ops) if r["solo_maintainer"]
                             else None)  # fmt: skip
    except (ValueError, TypeError) as exc:
        raise ReleaseStoreError("integrity", type(exc).__name__) from None


def history(tx: TenantTx, deployment_id: str,
            test_verifiers: Mapping[str, rl.Verifier] | None = None,
            ) -> History | None:  # fmt: skip
    """The release history the session tenant may read (qw_app, owner decision 45):
    None unless the deployment is recorded and the tenant is one of its members.
    Every row is re-hashed and the ledger replayed with full signature checks
    against the stored keys; a test key verifies only through `test_verifiers`."""
    conn = tx.conn
    _lock(conn, deployment_id, shared=True)  # inside the tenant transaction
    d = _member_deployment(conn, deployment_id)
    if d is None:
        return None
    keys = stored_keys(conn, test_verifiers, member_of=deployment_id)
    rows = _rows(conn, "SELECT * FROM app.release_history_events(%s) ORDER BY seq",
                 deployment_id)  # fmt: skip
    found = _rows(conn, "SELECT * FROM app.release_history_releases(%s)",
                  deployment_id)  # fmt: skip
    stored = {r["release_id"]: r for r in found}

    def rel(rid: str) -> rl.SignedRelease | None:
        return _release_of(stored[rid]) if rid in stored else None

    return History(_replay(d, rows, rel, keys), keys, _now(conn))


def _append(conn: Conn, deployment_id: str, keys: Keys | None,
            act: Callable[[rl.DeploymentLedger, datetime], rl.DeploymentLedger],
            ) -> rl.DeploymentLedger:  # fmt: skip
    with conn.transaction():
        _lock(conn, deployment_id, shared=False)
        keys = stored_keys(conn) if keys is None else keys
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


def install(conn: Conn, deployment_id: str, keys: Keys | None, event_id: str,
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


def rollback(conn: Conn, deployment_id: str, keys: Keys | None, event_id: str,
             component: str, operator_id: str, step_up: str, *,
             strategies: Strategies, to_release_id: str | None = None,
             ) -> rl.DeploymentLedger:  # fmt: skip
    """The domain rollback, or the `Pause` it appends, at the database clock."""
    return _append(conn, deployment_id, keys, lambda lg, at: lg.rollback(
        event_id, component, operator_id, step_up, at, strategies=strategies,
        to_release_id=to_release_id))  # fmt: skip
