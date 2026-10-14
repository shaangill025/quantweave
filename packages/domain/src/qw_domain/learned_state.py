"""Learned-state classification, lineage invalidation and safe export/import (T052
increment 1; spec §11, §12; R031, R039, R051, R052, R087, R096). Domain, stdlib only.
- Classification. Each learned or derived item is a lineage node: T043 records and
  consent grants, T044 candidates and verdicts, T045 personal adaptation versions and
  T051 shared releases. A node carries its origin (private tenant, shared, synthetic),
  consent basis (own data, consent, none), evidence purpose and its input nodes.
- Invalidation (spec §12). A record deleted or a consent revoked by `at` invalidates
  every node downstream of it; an invalidated node is never reproducible and names
  its root causes. A missing input fails closed (`lineage_missing`). Guards and
  exports read every deletion and revocation recorded, whatever time they assert;
  the adopt guard checks the candidate's content hash against its registry. Actions:
  - the active personal adaptation must be rebuilt or rolled back;
  - other derived items are blocked: `adopt` and `rollback` here refuse them
    (`PrivateState.adopt`/`rollback` themselves are unguarded: a tested gate);
  - a shared release is flagged `retire_or_rebuild`: a derivative that cannot be
    selectively unlearned is retired or rebuilt, or kept only under a lawful disclosed
    exception (an owner decision). Nothing is uninstalled or switched automatically,
    and no tenant is moved to another version (T051).
- Export. A tenant bundle holds its live records (final-holdout cases withheld),
  consent grants, personal adaptation versions (each `inactive`), its tenant-scope
  candidates and verdicts (configuration payloads only for personal adaptation) and
  its lineage, as canonical JSON files addressed by an `export_manifest` manifest. A
  lineage not built from this store and state is refused (`lineage_stale`); after an
  erasure no configuration payload is exported. No approvals, step-up receipts, keys,
  credentials, executable payloads, shared releases or other tenants' data: a key
  allowlist, fragment, tenant-id and lineage-node scan refuses the bundle otherwise.
- Import checks the manifest shape, exactly five paths, hashes, sizes and classes,
  parses strict JSON (no floats, duplicate keys or deep nesting), rescans every file
  and strictly re-parses each configuration. Every failure is a typed refusal. The
  result is inert: it becomes active only as a new candidate through assessment and
  the normal adoption approvals.
LIMITATIONS: in memory; T036 evidence sources are not yet lineage roots; import does
not re-ingest records (their per-tenant clock) or re-verify exported lineage; a bundle
hash is integrity, not authenticity; free text is not scanned for pasted secrets.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, StrEnum
from types import MappingProxyType

from qw_domain import feedback as fb
from qw_domain import improvement as im
from qw_domain import personal_adaptation as pa
from qw_domain.decimals import safe_repr
from qw_domain.feedback import CaseOrigin, EvaluationCase, ImprovementEvidence, Purpose
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.release import ReleaseManifest
from qw_domain.sources import ID_PATTERN

FORMAT = "qw-learned-state-1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 16  # an export nests at most 6 levels
_ALL = datetime.max.replace(tzinfo=UTC)  # every deletion and revocation recorded
PATHS: Mapping[str, str] = MappingProxyType({  # path -> ManifestFile content_class
    "learning/records.json": "private_learning",
    "learning/consents.json": "audit",
    "learning/adaptation.json": "private_learning",
    "learning/candidates.json": "private_learning",
    "learning/lineage.json": "metadata",
})  # fmt: skip
# Key fragments that mark credentials, authority receipts or activation state.
_FORBIDDEN = ("secret", "password", "passphrase", "credential", "token", "api_key",
              "apikey", "private_key", "signing", "key_material", "step_up", "stepup",
              "session", "cookie", "oauth", "activ", "install", "adopt", "approv",
              "entrypoint", "executable", "script", "command", "source_code",
              "bytecode", "signature")  # fmt: skip
_MANIFEST = frozenset({"id", "format_version", "created_at", "scope_id", "files",
                       "omissions", "contains_credentials", "contains_executable_code",
                       "required_reconnections", "compatibility_version",
                       "content_hash"})  # fmt: skip
_ENTRY = frozenset({"path", "sha256", "bytes", "content_class"})
_CLASSES = frozenset({"approval", "shared_release", "evaluation_case", "record",
                      "lineage_edge", "candidate_payload"})  # fmt: skip
_REASON = re.compile(r"authority_not_portable|installation_scope|erased"
                     r"|(final_holdout|deleted|other_principal):[0-9]{1,9}"
                     r"|(other_scope|not_configuration):[A-Za-z0-9_.:/-]{1,400}",
                     re.ASCII)  # fmt: skip
_HEX64 = re.compile(r"[0-9a-f]{64}", re.ASCII)
# Every key an export writes (one allowlist for all five files, not per file).
_KEYS = frozenset(
    {f.name for t in (fb.Feedback, fb.ErrorRecord, fb.Outcome, EvaluationCase,
                      fb.Subject, fb.FactRef, fb.ConsentGrant) for f in fields(t)}
    | fb.CASE_FIELDS | pa.ADAPTABLE
    | {"record_kind", "grant", "revoked_at", "number", "source", "adaptation_hash",
       "actor_ids", "at", "adaptation", "status", "candidate_id", "target",
       "experiment_id", "parent_id", "artifact_hash", "payload", "created_at",
       "verdicts", "verdict_id", "decision", "reasons", "limitations", "scores",
       "assessed_at", "node", "kind", "scope_id", "origin", "basis", "purpose",
       "inputs", "reproducible", "invalidated", "action"})  # fmt: skip


class LearnedStateError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise LearnedStateError(what, f"{safe_repr(value)} is malformed")
    return value


class Kind(StrEnum):
    RECORD = "record"
    CONSENT = "consent"
    CANDIDATE = "candidate"
    VERDICT = "verdict"
    ADAPTATION = "adaptation"
    RELEASE = "release"


class Origin(StrEnum):
    PRIVATE_TENANT = "private_tenant"
    SHARED = "shared"  # installation scope: built from consented contributions
    SYNTHETIC = "synthetic"


class Basis(StrEnum):  # why the data may be used
    OWN_DATA = "own_data"  # the tenant's own personal adaptation: no consent needed
    CONSENT = "consent"  # shared improvement under explicit grants
    NONE = "none"  # no personal data


class Action(StrEnum):
    NONE = "none"
    BLOCKED = "blocked"  # may not be adopted, released or restored
    REBUILD_OR_ROLLBACK = "rebuild_or_rollback"  # the active private configuration
    RETIRE_OR_REBUILD = "retire_or_rebuild"  # a shared release


def node(kind: Kind, scope_id: str, ident: str) -> str:
    return f"{kind.value}/{scope_id}/{ident}"  # "/" is outside ID_PATTERN


@dataclass(frozen=True, slots=True)
class Item:
    node: str
    kind: Kind
    scope_id: str
    origin: Origin
    basis: Basis
    purpose: Purpose | None
    inputs: tuple[str, ...]
    content_hash: str | None = None  # a candidate's T044 content hash


def _item(kind: Kind, scope: str, ident: str, origin: Origin, basis: Basis,
          purpose: Purpose | None, inputs: tuple[str, ...] = ()) -> Item:  # fmt: skip
    return Item(node(kind, scope, ident), kind, scope, origin, basis, purpose, inputs)


@dataclass(frozen=True, slots=True)
class Status:
    node: str
    reproducible: bool
    invalidated: bool
    reasons: tuple[str, ...]
    action: Action


class Lineage:
    """The lineage graph over one evidence store and the derived state given.
    Derived state is a snapshot; deletions and revocations are read at query time."""

    def __init__(
        self, evidence: ImprovementEvidence, *,
        registries: Sequence[im.ExperimentRegistry] = (),
        states: Sequence[pa.PrivateState] = (),
        releases: Sequence[ReleaseManifest] = (),
    ) -> None:  # fmt: skip
        self._ev = evidence
        self._items: dict[str, Item] = {}
        self._active: set[str] = set()
        self._erased: set[str] = set()
        self._tenants: set[str] = set()
        for reg in registries:
            self._registry(reg)
        for s in states:
            self._state(s)
        for m in releases:
            self._release(m, registries)
        for t in sorted(self._tenants):
            self._records(t)

    @property
    def items(self) -> Mapping[str, Item]:
        return MappingProxyType(self._items)

    def _add(self, it: Item) -> None:
        if self._items.setdefault(it.node, it) != it:
            raise LearnedStateError("duplicate_node", it.node)
        self._tenants.add(it.scope_id)

    def _registry(self, reg: im.ExperimentRegistry) -> None:
        s = reg.scope_id
        for e in reg.entries:
            if isinstance(e, im.Candidate):
                p, uses, ins = reg.protocol(e.experiment_id), e.evidence_uses, []
                for u in uses:
                    self._tenants.add(u.tenant_id)
                    ins += [node(Kind.RECORD, u.tenant_id, r) for r in u.record_ids]
                    ins += [node(Kind.CONSENT, u.tenant_id, c) for c in u.consent_ids]
                if e.parent_id != p.baseline_version:
                    ins.append(node(Kind.CANDIDATE, s, e.parent_id))
                shared = any(u.purpose is Purpose.SHARED_IMPROVEMENT for u in uses)
                basis = (Basis.CONSENT if shared else
                         Basis.OWN_DATA if uses else Basis.NONE)  # fmt: skip
                origin = (Origin.PRIVATE_TENANT if p.scope is im.Scope.TENANT
                          else Origin.SHARED)  # fmt: skip
                ins = list(dict.fromkeys(ins))
                it = _item(Kind.CANDIDATE, s, e.candidate_id, origin, basis,
                           p.evidence_purpose, tuple(ins))  # fmt: skip
                self._add(replace(it, content_hash=e.content_hash))
            elif isinstance(e, im.Verdict):
                c = self._items[node(Kind.CANDIDATE, s, e.candidate_id)]
                self._add(_item(Kind.VERDICT, s, e.verdict_id, c.origin, c.basis,
                                c.purpose, (c.node,)))  # fmt: skip

    def _state(self, s: pa.PrivateState) -> None:
        t = s.tenant_id
        for v in s.versions:
            kind, _, ref = v.source.partition(":")
            up = {"candidate": Kind.CANDIDATE, "rollback": Kind.ADAPTATION}.get(kind)
            ins = () if up is None else (node(up, t, ref),)
            it = _item(Kind.ADAPTATION, t, str(v.number), Origin.PRIVATE_TENANT,
                       Basis.OWN_DATA, Purpose.PERSONAL_ADAPTATION, ins)  # fmt: skip
            self._add(it)
            if v.adaptation is None:
                self._erased.add(it.node)
        self._active.add(node(Kind.ADAPTATION, t, str(s.versions[-1].number)))

    def _release(self, m: ReleaseManifest,
                 registries: Sequence[im.ExperimentRegistry]) -> None:  # fmt: skip
        """Bind the release to the registry with its exact protocol and candidate."""
        for reg in registries:
            try:
                p, c = reg.protocol(m.experiment_id), reg.candidate(m.candidate_id)
            except im.ImprovementError:
                continue
            if (p.content_hash, c.content_hash) == (m.protocol_hash, m.candidate_hash):
                cn = self._items[node(Kind.CANDIDATE, reg.scope_id, m.candidate_id)]
                rel = _item(Kind.RELEASE, reg.scope_id, m.release_id, Origin.SHARED,
                            cn.basis, cn.purpose, (cn.node,))  # fmt: skip
                self._add(rel)
                return
        raise LearnedStateError("release_unbound", m.release_id)

    def _records(self, t: str) -> None:
        live, held = self._ev.export_view(t)
        mine, own = Origin.PRIVATE_TENANT, Basis.OWN_DATA
        for r in live:
            synth = isinstance(r, EvaluationCase) and r.origin is CaseOrigin.SYNTHETIC
            self._add(_item(Kind.RECORD, t, r.record_id,
                            Origin.SYNTHETIC if synth else mine,
                            Basis.NONE if synth else own, None))  # fmt: skip
        gone = [s.record_id for s in self._ev.tombstones(t)]
        for rid in (*held, *gone):  # content unread or dropped: assume private
            self._add(_item(Kind.RECORD, t, rid, mine, own, None))
        for g, _ in self._ev.consents(t):
            self._add(_item(Kind.CONSENT, t, g.consent_id, mine, Basis.CONSENT,
                            g.purpose))  # fmt: skip

    def fresh(self, evidence: ImprovementEvidence, state: pa.PrivateState | None,
              registries: Sequence[im.ExperimentRegistry]) -> bool:  # fmt: skip
        """Built from this store, and still matches the state and registries."""
        if evidence is not self._ev:
            return False
        for reg in registries:
            for c in (e for e in reg.entries if isinstance(e, im.Candidate)):
                it = self._items.get(node(Kind.CANDIDATE, reg.scope_id, c.candidate_id))
                if it is None or it.content_hash != c.content_hash:
                    return False
        if state is None:
            return True
        t, vs = state.tenant_id, state.versions
        mine = [i for i in self._items.values()
                if (i.kind, i.scope_id) == (Kind.ADAPTATION, t)]  # fmt: skip
        return len(mine) == len(vs) and all(
            (n := node(Kind.ADAPTATION, t, str(v.number))) in self._items
            and (n in self._erased) == (v.adaptation is None) for v in vs)  # fmt: skip

    def statuses(self, at: datetime) -> Mapping[str, Status]:
        """Every node as known at `at`: deletions and revocations up to `at` count."""
        at, dead = ensure_aware_utc(at), {}
        for t in self._tenants:
            for s in self._ev.tombstones(t):
                if s.deleted_at <= at:
                    rid = s.record_id
                    dead[node(Kind.RECORD, t, rid)] = f"record_deleted:{rid}"
            for g, revoked in self._ev.consents(t):
                if revoked is not None and revoked <= at:
                    cid = g.consent_id
                    dead[node(Kind.CONSENT, t, cid)] = f"consent_revoked:{cid}"
        causes: dict[str, frozenset[str]] = {}
        limit = len(self._items) + sum(len(x.inputs) for x in self._items.values())

        def visit(root: str) -> frozenset[str]:  # iterative: deep chains are fine
            stack = [root]
            while stack:
                n = stack[-1]
                it = self._items.get(n)
                todo = [] if it is None else [i for i in it.inputs if i not in causes]
                if n in causes:
                    stack.pop()
                elif todo:
                    if len(stack) > limit:
                        raise LearnedStateError("lineage_cycle", n)
                    stack += todo
                else:
                    own = frozenset({dead[n]} if n in dead else ())
                    up = () if it is None else it.inputs
                    causes[n] = (frozenset({f"lineage_missing:{n}"}) if it is None
                                 else own.union(*(causes[i] for i in up)))  # fmt: skip
                    stack.pop()
            return causes[root]

        out = {}
        for n, it in self._items.items():
            why, erased = visit(n), n in self._erased
            out[n] = Status(n, not why and not erased, bool(why),
                            (*sorted(why), *(("erased",) if erased else ())),
                            _action(it, bool(why), n in self._active))  # fmt: skip
        return MappingProxyType(out)


def _action(it: Item, bad: bool, active: bool) -> Action:
    if not bad or it.kind in (Kind.RECORD, Kind.CONSENT):
        return Action.NONE
    if it.kind is Kind.RELEASE:
        return Action.RETIRE_OR_REBUILD
    if it.kind is Kind.ADAPTATION and active:
        return Action.REBUILD_OR_ROLLBACK
    return Action.BLOCKED


def _require_valid(lineage: Lineage, n: str) -> None:
    """Every deletion and revocation already recorded counts, even one after the
    act's own time: a backdated act cannot slip past it (fail closed)."""
    s = lineage.statuses(_ALL).get(n)
    if s is None or s.invalidated:
        raise LearnedStateError("invalidated", n if s is None else ",".join(s.reasons))


def adopt(state: pa.PrivateState, registry: im.ExperimentRegistry, lineage: Lineage,
          candidate_id: str, approvals: Sequence[pa.Approval],
          at: datetime) -> pa.Version:  # fmt: skip
    """T045 adoption, refused for a candidate whose lineage is invalidated or was
    built from another registry's candidate under the same id."""
    n = node(Kind.CANDIDATE, registry.scope_id, candidate_id)
    it = lineage.items.get(n)
    want = registry.candidate(candidate_id).content_hash
    if it is not None and it.content_hash != want:
        raise LearnedStateError("lineage_registry", candidate_id)
    _require_valid(lineage, n)
    return state.adopt(registry, candidate_id, approvals, at)


def rollback(state: pa.PrivateState, lineage: Lineage, number: int, actor_id: str,
             at: datetime) -> pa.Version:  # fmt: skip
    """T045 rollback, refused to a version whose lineage is invalidated."""
    _require_valid(lineage, node(Kind.ADAPTATION, state.tenant_id, str(number)))
    return state.rollback(number, actor_id, at)


def _wire(v: object) -> object:
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        return format_instant(v)
    if isinstance(v, Decimal):
        return str(v)
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if is_dataclass(v) and not isinstance(v, type):
        return _fields(v)
    if isinstance(v, Mapping):
        return {str(k): _wire(x) for k, x in v.items()}
    if isinstance(v, (set, frozenset)):
        return sorted(str(_wire(x)) for x in v)
    if isinstance(v, (tuple, list)):
        return [_wire(x) for x in v]
    raise TypeError(f"{type(v).__name__} is not exportable")


def _fields(v: object) -> dict[str, object]:
    return {f.name: _wire(getattr(v, f.name)) for f in fields(v)}  # type: ignore[arg-type]


def _canon(o: object) -> bytes:
    text = json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _scan(o: object, tenant_id: str) -> None:
    """Allowlisted keys only; no credential, authority or activation key; no other
    tenant's id or lineage node; bounded nesting. Iterative."""
    stack = [(o, 0)]
    while stack:
        x, depth = stack.pop()
        if depth > MAX_DEPTH:
            raise LearnedStateError("malformed", "nesting")
        if isinstance(x, list):
            stack += [(y, depth + 1) for y in x]
        elif isinstance(x, dict):
            for k, v in x.items():
                if k not in _KEYS or any(f in k for f in _FORBIDDEN):
                    raise LearnedStateError("forbidden_field", safe_repr(k))
                if k in ("tenant_id", "scope_id") and v != tenant_id:
                    raise LearnedStateError("foreign_tenant", safe_repr(v))
                if k == "status" and v != "inactive":
                    raise LearnedStateError("activation", safe_repr(v))
                refs = [v] if isinstance(v, str) else v if isinstance(v, list) else []
                if k in ("node", "inputs") and any(
                    not isinstance(r, str) or r.split("/")[1:2] != [tenant_id]
                    for r in refs
                ):  # fmt: skip
                    raise LearnedStateError("foreign_tenant", "lineage node")
                stack.append((v, depth + 1))


@dataclass(frozen=True, slots=True)
class Bundle:
    manifest: Mapping[str, object]  # export_manifest.schema.json
    files: Mapping[str, bytes]


def _omit(cls: str, reason: str) -> dict[str, str]:
    return {"record_class": cls, "reason": reason}


def _candidates(regs: Sequence[im.ExperimentRegistry], omit: list[dict[str, str]],
                erased: bool) -> list[object]:  # fmt: skip
    """Candidates and verdicts as data. A payload only if it is a configuration and
    the tenant has erased none; reasons and limitations only for configurations."""
    rows: list[object] = []
    for reg in regs:
        for c in (e for e in reg.entries if isinstance(e, im.Candidate)):
            config, payload = c.target is im.Target.PERSONAL_ADAPTATION, None
            try:
                if config and not erased:
                    payload = pa.Adaptation.from_payload(c.payload).to_payload()
            except pa.AdaptationError:
                pass
            why = f"not_configuration:{c.candidate_id}"
            why = "erased" if config and erased else why
            if payload is None and _omit("candidate_payload", why) not in omit:
                omit.append(_omit("candidate_payload", why))
            verdicts = [{"verdict_id": v.verdict_id, "decision": v.decision.value,
                         "scores": sorted([k, str(x)] for k, x in v.scores.items()),
                         "assessed_at": format_instant(v.assessed_at)}
                        | ({"reasons": list(v.reasons),
                            "limitations": list(v.limitations)} if config else {})
                        for v in reg.verdicts(c.candidate_id)]  # fmt: skip
            rows.append({"candidate_id": c.candidate_id, "target": c.target.value,
                         "experiment_id": c.experiment_id, "parent_id": c.parent_id,
                         "artifact_hash": c.artifact_hash, "payload": payload,
                         "created_at": format_instant(c.created_at),
                         "verdicts": verdicts})  # fmt: skip
    return rows


def export_bundle(
    evidence: ImprovementEvidence, lineage: Lineage, tenant_id: str, export_id: str,
    at: datetime, *, state: pa.PrivateState | None = None,
    registries: Sequence[im.ExperimentRegistry] = (), principal_id: str | None = None,
) -> Bundle:  # fmt: skip
    """The tenant's learned state as inert, content-addressed data (spec §12).
    Statuses count every deletion and revocation recorded, whatever `at` says.
    `principal_id` keeps only that principal's feedback and grants."""
    _id(tenant_id, "tenant")
    _id(export_id, "export_id")
    at = ensure_aware_utc(at)
    if (state is not None and state.tenant_id != tenant_id) or any(
        r.scope_id != tenant_id for r in registries
    ):  # fmt: skip
        raise LearnedStateError("foreign_tenant", "state of another scope")
    if not lineage.fresh(evidence, state, registries):
        raise LearnedStateError("lineage_stale", "rebuild it from the current state")
    live, held = evidence.export_view(tenant_id)
    grants = evidence.consents(tenant_id)
    if principal_id is not None:
        n_in = len(live)
        live = tuple(r for r in live if getattr(r, "principal_id", principal_id)
                     == principal_id)  # fmt: skip
        grants = tuple(x for x in grants if x[0].principal_id == principal_id)
        omit_principal = n_in - len(live)
    omit = [_omit("approval", "authority_not_portable"),
            _omit("shared_release", "installation_scope")]  # fmt: skip
    if principal_id is not None and omit_principal:
        omit.append(_omit("record", f"other_principal:{omit_principal}"))
    if held:
        omit.append(_omit("evaluation_case", f"final_holdout:{len(held)}"))
    if gone := evidence.tombstones(tenant_id):
        omit.append(_omit("record", f"deleted:{len(gone)}"))
    st, graph = lineage.statuses(_ALL), []
    for n, it in lineage.items.items():
        if it.scope_id != tenant_id:
            continue
        s, ins = st[n], [i for i in it.inputs if i.split("/")[1] == tenant_id]
        if len(ins) != len(it.inputs):
            omit.append(_omit("lineage_edge", f"other_scope:{n}"))
        graph.append({**{k: _wire(getattr(it, k)) for k in
                         ("node", "kind", "scope_id", "origin", "basis", "purpose")},
                      "inputs": ins, "reproducible": s.reproducible,
                      "invalidated": s.invalidated, "reasons": list(s.reasons),
                      "action": s.action.value})  # fmt: skip
    erased = state is not None and any(v.adaptation is None for v in state.versions)
    bodies: dict[str, object] = {
        "learning/records.json": [{**_fields(r), "record_kind": r.KIND.value}
                                  for r in live],
        "learning/consents.json": [{"grant": _wire(g), "revoked_at": _wire(r)}
                                   for g, r in grants],
        "learning/adaptation.json": [{**v, "status": "inactive"}
                                     for v in (state.export() if state else ())],
        "learning/candidates.json": _candidates(registries, omit, erased),
        "learning/lineage.json": graph,
    }  # fmt: skip
    files = {}
    for path, body in bodies.items():
        _scan(body, tenant_id)
        files[path] = _canon(body)
    head = {
        "id": export_id, "format_version": FORMAT, "created_at": format_instant(at),
        "scope_id": tenant_id,
        "files": [{"path": p, "sha256": _sha(b), "bytes": len(b),
                   "content_class": PATHS[p]} for p, b in sorted(files.items())],
        "omissions": omit, "contains_credentials": False,
        "contains_executable_code": False, "required_reconnections": [],
        "compatibility_version": FORMAT,
    }  # fmt: skip
    return Bundle(MappingProxyType({**head, "content_hash": _sha(_canon(head))}),
                  MappingProxyType(files))  # fmt: skip


@dataclass(frozen=True, slots=True)
class Imported:
    """Inert imported data: nothing here is active, installed or adopted."""

    tenant_id: str
    manifest_hash: str
    configurations: tuple[tuple[int, pa.Adaptation | None], ...]
    data: Mapping[str, object]  # parsed files; lineage as exported, not re-verified


def _no_float(text: str) -> object:
    raise LearnedStateError("float", safe_repr(text))


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise LearnedStateError("duplicate_key", "a JSON object repeats a key")
    return out


def _rows(o: object, what: str) -> list[dict[str, object]]:
    if not isinstance(o, list) or not all(isinstance(x, dict) for x in o):
        raise LearnedStateError("malformed", what)
    return o


def _config(payload: object, digest: str | None) -> pa.Adaptation:
    if not isinstance(payload, dict):
        raise LearnedStateError("malformed", "adaptation")
    try:
        a = pa.Adaptation.from_payload(payload)
    except pa.AdaptationError as e:
        raise LearnedStateError("adaptation", e.code) from None
    if digest is not None and a.content_hash != digest:
        raise LearnedStateError("adaptation_hash", safe_repr(digest))
    return a


def _instant(v: object) -> bool:
    try:
        return isinstance(v, str) and datetime.fromisoformat(v).tzinfo is not None
    except ValueError:
        return False


def _shape_ok(m: dict[str, object]) -> bool:
    """The export_manifest shape, with the omission vocabulary this module writes."""
    files, omissions = m["files"], m["omissions"]
    return (isinstance(m["id"], str) and ID_PATTERN.fullmatch(m["id"]) is not None
            and _instant(m["created_at"]) and m["required_reconnections"] == []
            and isinstance(m["content_hash"], str)
            and _HEX64.fullmatch(m["content_hash"]) is not None
            and isinstance(files, list) and isinstance(omissions, list)
            and all(isinstance(e, dict) and set(e) == _ENTRY
                    and all(isinstance(e[k], str) for k in ("path", "sha256",
                                                            "content_class"))
                    and type(e["bytes"]) is int for e in files)
            and all(isinstance(o, dict) and set(o) == {"record_class", "reason"}
                    and o["record_class"] in _CLASSES and isinstance(o["reason"], str)
                    and _REASON.fullmatch(o["reason"]) is not None
                    for o in omissions))  # fmt: skip


def import_bundle(b: Bundle, tenant_id: str) -> Imported:
    """Verify and parse a bundle for its own tenant. Every failure is a typed
    `LearnedStateError` refusal."""
    m = dict(b.manifest)
    if set(m) != _MANIFEST:
        raise LearnedStateError("manifest", "fields differ from export_manifest")
    if m["contains_credentials"] is not False:
        raise LearnedStateError("credentials", "a bundle never carries credentials")
    if m["contains_executable_code"] is not False:
        raise LearnedStateError("executable", "a bundle never carries executable code")
    if (m["format_version"], m["compatibility_version"]) != (FORMAT, FORMAT):
        raise LearnedStateError("incompatible", safe_repr(m["format_version"]))
    if m["scope_id"] != _id(tenant_id, "tenant"):
        raise LearnedStateError("scope_mismatch", safe_repr(m["scope_id"]))
    if not _shape_ok(m):
        raise LearnedStateError("manifest", "does not match export_manifest")
    body = {k: v for k, v in m.items() if k != "content_hash"}
    if _sha(_canon(body)) != m["content_hash"]:
        raise LearnedStateError("manifest_hash", "manifest altered")
    entries = _rows(m["files"], "files")
    if sorted(str(e["path"]) for e in entries) != sorted(PATHS) or (
        set(b.files) != PATHS.keys()
    ):  # fmt: skip
        raise LearnedStateError("path", "exactly the five learned-state files")
    data: dict[str, object] = {}
    for e in entries:
        path, size = str(e["path"]), e["bytes"]
        raw = b.files[path]
        if type(size) is not int or max(size, len(raw)) > MAX_FILE_BYTES:
            raise LearnedStateError("oversized", path)
        if (e["sha256"], size) != (_sha(raw), len(raw)):
            raise LearnedStateError("file_hash", path)
        if e["content_class"] != PATHS[path]:
            raise LearnedStateError("classification", path)
        try:
            text = raw.decode()  # strict UTF-8; a BOM then fails JSON parsing
            obj = json.loads(text, parse_float=_no_float, parse_constant=_no_float,
                             object_pairs_hook=_pairs)  # fmt: skip
        except LearnedStateError:
            raise
        except (ValueError, RecursionError, TypeError):  # incl. JSON and huge ints
            raise LearnedStateError("malformed", path) from None
        _scan(obj, tenant_id)
        data[path] = obj
    configs, seen = [], set()
    for row in _rows(data["learning/adaptation.json"], "adaptation"):
        number, payload, digest = (row.get("number"), row.get("adaptation"),
                                   row.get("adaptation_hash"))  # fmt: skip
        if type(number) is not int or number < 1 or number in seen or not (
            isinstance(digest, str)
        ):  # fmt: skip
            raise LearnedStateError("adaptation_version", safe_repr(number))
        seen.add(number)
        configs.append((number, None if payload is None else _config(payload, digest)))
    for row in _rows(data["learning/candidates.json"], "candidates"):
        if row.get("payload") is not None:
            _config(row["payload"], None)
    return Imported(tenant_id, str(m["content_hash"]), tuple(configs),
                    MappingProxyType(data))  # fmt: skip
