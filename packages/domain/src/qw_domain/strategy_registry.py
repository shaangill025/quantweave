"""Strategy manifests, bounded configuration, three qualification axes and adoption.

T027; spec §8 shared execution contract and three-axis qualification, §7 gate order,
§11/§16 adoption, §13 strategy cards, T008 C-20/C-22.
- A manifest version is immutable. `material_hash` covers all strategy semantics
  (code, model config, family, data, instruments, horizon, session, parameter bounds,
  research protocol); only the title and version label are outside it. Axes and
  adoption bind the material hash: a material change restarts every axis and needs
  re-adoption, a display-only change carries both.
- Configuration is bounded and typed; unknown, float, mistyped or out-of-bounds values
  are refused. Every configuration value is investment-affecting (§7, §13), so one
  configuration hash never inherits another's evidence, eligibility or adoption.
- Each axis step names an actor and evidence known at the step. Synthetic-only
  evidence reaches at most `limited`. Evidence steps need a family policy approved
  before them and record it, so only a predeclared policy can permit limited evidence.
- `status` is derived per tenant, configuration and time; no global flag (C-22).
- LIMITATIONS: increment 1. No actionable gate (capabilities, data rights, investment
  policy), persistence or routes. Actor roles and evidence refs are caller-asserted;
  replay cannot re-derive a stored config hash, only its key shape and history rules.
Stdlib only.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.rights import Use
from qw_domain.sources import ID_PATTERN

ParamValue = int | Decimal | str | bool


class StrategyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _check_id(value: object, what: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise StrategyError(what, f"{safe_repr(value)} is malformed")


def _freeze(obj: object, name: str, kind: type) -> None:
    """Copy a collection field so later caller mutation cannot change a hash."""
    value = getattr(obj, name)
    if not isinstance(value, (list, tuple) if kind is tuple else (set, frozenset)):
        raise StrategyError(name, f"expected a {kind.__name__}")
    object.__setattr__(obj, name, kind(value))


def _hash(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class Family(StrEnum):
    ALLOCATION = "allocation"
    QUALITY_VALUE = "quality_value"
    INCOME = "income"
    SWING_MOMENTUM = "swing_momentum"
    OPENING_RANGE_BREAKOUT = "opening_range_breakout"
    AI_THESIS = "ai_thesis"


class Horizon(StrEnum):  # policy `horizons` values (ONB12; same_day -> intraday)
    LONG_TERM = "long_term"
    SWING_POSITION = "swing_position"
    INTRADAY = "intraday"


class AssetClass(StrEnum):  # policy `instruments` values (ONB11)
    STOCKS = "stocks"
    ETFS = "etfs"
    OPTIONS = "options"


_H = Horizon
# Permitted horizons per family (spec §8: ORB is same-session; trend is swing/position;
# allocation and income are long-term reviews; ai_thesis follows its adopted mandate).
FAMILY_HORIZONS: Mapping[Family, frozenset[Horizon]] = MappingProxyType(
    {
        Family.ALLOCATION: frozenset({_H.LONG_TERM}),
        Family.QUALITY_VALUE: frozenset({_H.LONG_TERM, _H.SWING_POSITION}),
        Family.INCOME: frozenset({_H.LONG_TERM}),
        Family.SWING_MOMENTUM: frozenset({_H.SWING_POSITION}),
        Family.OPENING_RANGE_BREAKOUT: frozenset({_H.INTRADAY}),
        Family.AI_THESIS: frozenset(Horizon),
    }
)


class ParamKind(StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    CHOICE = "choice"
    BOOLEAN = "boolean"


_TYPES = {
    ParamKind.INTEGER: int,
    ParamKind.DECIMAL: Decimal,
    ParamKind.CHOICE: str,
    ParamKind.BOOLEAN: bool,
}


def _typed(kind: ParamKind, value: object) -> bool:
    finite = not isinstance(value, Decimal) or value.is_finite()
    return type(value) is _TYPES[kind] and finite


def _wire(value: ParamValue) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")  # 0.050 and 0.05 hash equally
    return str(value).lower() if isinstance(value, bool) else str(value)


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    kind: ParamKind
    default: ParamValue
    minimum: int | Decimal | None = None
    maximum: int | Decimal | None = None
    allowed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "allowed", tuple)
        _check_id(self.name, "parameter")
        if not isinstance(self.kind, ParamKind):
            raise StrategyError("parameter", f"kind {safe_repr(self.kind)}")
        numeric = self.kind in (ParamKind.INTEGER, ParamKind.DECIMAL)
        lo, hi = self.minimum, self.maximum
        if numeric and not (_typed(self.kind, lo) and _typed(self.kind, hi)):
            raise StrategyError("bounded", f"{self.name} needs typed min and max")
        if numeric and lo > hi:  # type: ignore[operator]
            raise StrategyError("bounded", f"{self.name}: min > max")
        if not numeric and (lo, hi) != (None, None):
            raise StrategyError("bounded", f"{self.name}: min/max are numeric-only")
        choice = self.kind is ParamKind.CHOICE
        if choice != bool(self.allowed) or not all(
            type(a) is str for a in self.allowed
        ):
            raise StrategyError("bounded", f"{self.name}: allowed values for choices")
        if self.check(self.default) is not None:
            raise StrategyError("default", f"{self.name} default is out of bounds")

    def check(self, value: object) -> str | None:
        """None when `value` has this parameter's type and lies within its bounds."""
        lo, hi = self.minimum, self.maximum
        if not _typed(self.kind, value):
            return f"{self.name}: expected {self.kind.value}, got {safe_repr(value)}"
        if (self.allowed and value not in self.allowed) or (
            lo is not None and not lo <= value <= hi  # type: ignore[operator]
        ):
            return f"{self.name}: {safe_repr(value)} is out of bounds"
        return None

    def to_wire(self) -> list[object]:
        bounds = [None if b is None else _wire(b) for b in (self.minimum, self.maximum)]
        return [self.name, self.kind.value, _wire(self.default), *bounds, self.allowed]


@dataclass(frozen=True, slots=True)
class DataNeed:
    feed_id: str
    uses: frozenset[Use]
    retain_for: timedelta | None = None  # required with, and only with, RETENTION

    def __post_init__(self) -> None:
        _check_id(self.feed_id, "feed")
        _freeze(self, "uses", frozenset)
        if not self.uses or not all(isinstance(u, Use) for u in self.uses):
            raise StrategyError("data", "a non-empty set of Use")
        keep = self.retain_for
        valid = isinstance(keep, timedelta) and keep > timedelta(0)
        if (Use.RETENTION in self.uses) != (keep is not None) or (
            keep is not None and not valid
        ):
            raise StrategyError("data", "retention needs a duration, and only it")

    def to_wire(self) -> list[object]:
        keep = None if self.retain_for is None else str(self.retain_for)
        return [self.feed_id, sorted(u.value for u in self.uses), keep]


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    strategy_id: str
    version: str
    family: Family
    implementation_hash: str  # code artifact hash
    data: tuple[DataNeed, ...]
    asset_classes: frozenset[AssetClass]
    horizon: Horizon
    regular_session_only: bool
    parameters: tuple[Parameter, ...]
    research_protocol_id: str
    model_ref: str | None = None  # model/prompt/tool config; required for ai_thesis
    title: str = ""  # display metadata: the only non-material field

    def __post_init__(self) -> None:
        for name, kind in (("parameters", tuple), ("data", tuple)):
            _freeze(self, name, kind)
        _freeze(self, "asset_classes", frozenset)
        for value, what in ((self.strategy_id, "strategy"), (self.version, "version")):
            _check_id(value, what)
        _check_id(self.research_protocol_id, "protocol")
        if not isinstance(self.family, Family):
            raise StrategyError("family", safe_repr(self.family))
        h = self.implementation_hash
        if not isinstance(h, str) or len(h) != 64 or set(h) - set("0123456789abcdef"):
            raise StrategyError("implementation_hash", "64 lowercase hex characters")
        if self.horizon not in FAMILY_HORIZONS[self.family]:
            raise StrategyError("horizon", f"{self.horizon} for {self.family}")
        if (self.family is Family.AI_THESIS) != (self.model_ref is not None):
            raise StrategyError("model_ref", "required for, and only for, ai_thesis")
        if self.model_ref is not None:
            _check_id(self.model_ref, "model_ref")
        if not self.data or not all(type(d) is DataNeed for d in self.data):
            raise StrategyError("data", "at least one DataNeed")
        if not self.asset_classes or set(self.asset_classes) - set(AssetClass):
            raise StrategyError("asset_classes", "a non-empty set of AssetClass")
        names = [p.name for p in self.parameters if type(p) is Parameter]
        if len(set(names)) != len(self.parameters):
            raise StrategyError("parameter", "unique Parameter declarations")
        if type(self.regular_session_only) is not bool or type(self.title) is not str:
            raise StrategyError("manifest", "regular_session_only/title types")

    @property
    def material_hash(self) -> str:
        return _hash(
            {
                "strategy_id": self.strategy_id,
                "family": self.family.value,
                "implementation_hash": self.implementation_hash,
                "model_ref": self.model_ref,
                "data": sorted((d.to_wire() for d in self.data), key=json.dumps),
                "asset_classes": sorted(a.value for a in self.asset_classes),
                "horizon": self.horizon.value,
                "regular_session_only": self.regular_session_only,
                "parameters": sorted(p.to_wire() for p in self.parameters),
                "research_protocol_id": self.research_protocol_id,
            }
        )

    @property
    def content_hash(self) -> str:
        return _hash([self.material_hash, self.version, self.title])

    def resolve(self, config: Mapping[str, object]) -> dict[str, ParamValue]:
        """Defaults plus `config`, or StrategyError("config") naming every problem."""
        declared = {p.name: p for p in self.parameters}
        problems = [
            f"unknown {safe_repr(k)}" if k not in declared else declared[k].check(v)
            for k, v in config.items()
        ]
        if errors := [p for p in problems if p is not None]:
            raise StrategyError("config", "; ".join(errors))
        out: dict[str, ParamValue] = {p.name: p.default for p in self.parameters}
        out.update(config)  # type: ignore[arg-type]  # each value checked above
        return out

    def config_hash(self, config: Mapping[str, object]) -> str:
        return _hash({k: _wire(v) for k, v in self.resolve(config).items()})


@dataclass(frozen=True, slots=True)
class FamilyPolicy:
    """Human-approved, predeclared family qualification policy (§8)."""

    family: Family
    policy_id: str
    version: int
    approved_by: str
    approved_at: datetime
    permits_limited_evidence: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_at", ensure_aware_utc(self.approved_at))
        _check_id(self.policy_id, "family_policy")
        _check_id(self.approved_by, "actor")
        if (
            not isinstance(self.family, Family)
            or type(self.version) is not int
            or type(self.permits_limited_evidence) is not bool
        ):
            raise StrategyError("family_policy", "family and integer version")

    @property
    def content_hash(self) -> str:
        body = [getattr(self, f.name) for f in fields(self)]
        return _hash([*body[:4], format_instant(self.approved_at), body[5]])


class Axis(StrEnum):
    OPERATIONAL = "operational"
    EVIDENCE = "investment_evidence"
    ELIGIBILITY = "user_eligibility"


class OperationalState(StrEnum):
    NOT_TESTED = "not_tested"
    PASSED = "passed"
    FAILED = "failed"
    SUSPENDED = "suspended"


class EvidenceState(StrEnum):
    NONE = "none"
    LIMITED = "limited"
    HISTORICAL = "historical"
    PROSPECTIVE_LIMITED = "prospective_limited"
    QUALIFIED_FOR_DECLARED_CLAIM = "qualified_for_declared_claim"


class EligibilityState(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


AxisState = OperationalState | EvidenceState | EligibilityState
_O, _V, _U = OperationalState, EvidenceState, EligibilityState
_STATES = {Axis.OPERATIONAL: _O, Axis.EVIDENCE: _V, Axis.ELIGIBILITY: _U}
_INITIAL: Mapping[Axis, AxisState] = {
    Axis.OPERATIONAL: _O.NOT_TESTED,
    Axis.EVIDENCE: _V.NONE,
    Axis.ELIGIBILITY: _U.NOT_EVALUATED,
}
_EV = list(EvidenceState)
# Re-assessment to the same state is allowed; FAILED is final for a material hash.
# Evidence rises at most one state per step (none may go to limited or historical;
# qualified needs prospective_limited first) and may be downgraded to any state.
TRANSITIONS: Mapping[AxisState, frozenset[AxisState]] = MappingProxyType(
    {
        _O.NOT_TESTED: frozenset({_O.PASSED, _O.FAILED}),
        _O.PASSED: frozenset({_O.PASSED, _O.SUSPENDED, _O.FAILED}),
        _O.SUSPENDED: frozenset({_O.PASSED, _O.FAILED}),
        _O.FAILED: frozenset(),
        **{s: frozenset(_EV[: i + 2]) for i, s in enumerate(_EV)},
        _V.NONE: frozenset({_V.NONE, _V.LIMITED, _V.HISTORICAL}),
        _U.NOT_EVALUATED: frozenset({_U.ELIGIBLE, _U.INELIGIBLE}),
        _U.ELIGIBLE: frozenset(EligibilityState),
        _U.INELIGIBLE: frozenset(EligibilityState),
    }
)


@dataclass(frozen=True, slots=True)
class QualEvidence:
    ref_id: str
    recorded_at: datetime
    synthetic: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", ensure_aware_utc(self.recorded_at))
        _check_id(self.ref_id, "evidence")
        if type(self.synthetic) is not bool:
            raise StrategyError("evidence", "synthetic flag is required")


@dataclass(frozen=True, slots=True)
class AxisStep:
    state: AxisState
    actor_id: str
    evidence: tuple[QualEvidence, ...]
    at: datetime
    family_policy_hash: str | None  # evidence axis: the policy it was assessed under

    @property
    def synthetic_only(self) -> bool:
        return all(e.synthetic for e in self.evidence)


QualKey = tuple[str, ...]  # (axis, material hash[, config hash[, tenant]])


@dataclass(frozen=True, slots=True)
class StrategyConsent:
    principal_id: str
    step_up_receipt_id: str
    signed_at: datetime
    acknowledgement_text: str  # recorded for UX; never authorises


@dataclass(frozen=True, slots=True)
class StrategyAdoption:
    """C-20 `user_strategy_adoption`; an adapter signs `receipt_hash`."""

    tenant_id: str
    strategy_id: str
    version: str
    manifest_hash: str  # material hash
    config_hash: str
    principal_id: str
    step_up_receipt_id: str
    signed_at: datetime
    acknowledgement_text: str
    kind: str = "user_strategy_adoption"

    @property
    def receipt_hash(self) -> str:
        body = {f.name: getattr(self, f.name) for f in fields(self)}
        return _hash(body | {"signed_at": format_instant(self.signed_at)})


def _key(axis: Axis, m: StrategyManifest, *rest: str | None) -> QualKey:
    return tuple(p for p in (axis.value, m.material_hash, *rest) if p is not None)


@dataclass(frozen=True, slots=True)
class StrategyRegistry:
    """Immutable and append-only; every change returns a new registry. Construction
    replays every history through the same rules as the methods (as `rights.Registry`
    does), so forged or reordered records are refused with `integrity`.

    PRECONDITIONS for persistence and routes: step `at`, `approved_at` and `signed_at`
    come from the server clock (a future-dated step would block later steps through
    time order); the adopt route verifies the step-up receipt against T010 records for
    this tenant and principal, and refuses its reuse."""

    manifests: Mapping[tuple[str, str], StrategyManifest] = field(default_factory=dict)
    family_policies: Mapping[Family, tuple[FamilyPolicy, ...]] = field(
        default_factory=dict
    )
    qualifications: Mapping[QualKey, tuple[AxisStep, ...]] = field(default_factory=dict)
    adoptions: Mapping[tuple[str, str], tuple[StrategyAdoption, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        for f in fields(self):
            table = dict(getattr(self, f.name))
            object.__setattr__(self, f.name, MappingProxyType(table))
        try:
            self._replay()
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise StrategyError("integrity", f"does not replay: {exc}") from None

    def _replay(self) -> None:
        for key, m in self.manifests.items():
            if type(m) is not StrategyManifest or key != (m.strategy_id, m.version):
                raise StrategyError("manifest", safe_repr(key))
        lists = (self.family_policies, self.qualifications, self.adoptions)
        if not all(type(v) is tuple for t in lists for v in t.values()):
            raise StrategyError("history", "histories are tuples")
        for family, policies in self.family_policies.items():
            for i, p in enumerate(policies):
                self._check_policy(family, policies[:i], p)
        by_hash = {m.material_hash: m for m in self.manifests.values()}
        for qkey, steps in self.qualifications.items():
            for i, s in enumerate(steps):
                self._check_step(by_hash[qkey[1]], qkey, steps[:i], s)
        for pair, receipts in self.adoptions.items():
            for i, r in enumerate(receipts):
                self._check_receipt(pair, receipts[:i], r)

    def _check_policy(
        self, family: Family, prior: tuple[FamilyPolicy, ...], p: object
    ) -> None:
        if type(p) is not FamilyPolicy or p.family is not family:
            raise StrategyError("family_policy", "a FamilyPolicy of this family")
        last = prior[-1] if prior else None
        if last and (p.version <= last.version or p.approved_at < last.approved_at):
            raise StrategyError("family_policy", "versions are appended in order")

    def _check_step(
        self, m: StrategyManifest, key: QualKey, prior: tuple[AxisStep, ...], s: object
    ) -> None:
        axis = Axis(key[0])
        size = {Axis.OPERATIONAL: 2, Axis.EVIDENCE: 3, Axis.ELIGIBILITY: 4}[axis]
        if len(key) != size or type(s) is not AxisStep:
            raise StrategyError("axis", f"{axis} key or step malformed")
        if not isinstance(s.state, _STATES[axis]):
            raise StrategyError("axis", f"{safe_repr(s.state)} is not a {axis} state")
        if size == 4:
            _check_id(key[3], "tenant")
        _check_id(s.actor_id, "actor")
        at = ensure_aware_utc(s.at)
        if not s.evidence or not all(
            type(e) is QualEvidence and e.recorded_at <= at for e in s.evidence
        ):
            raise StrategyError("evidence", "evidence known at the step is required")
        current = prior[-1].state if prior else _INITIAL[axis]
        if s.state not in TRANSITIONS[current]:
            raise StrategyError("illegal_transition", f"{current} -> {s.state}")
        if prior and at < prior[-1].at:
            raise StrategyError("time_order", "steps are recorded in time order")
        if axis is not Axis.EVIDENCE or s.state is _V.NONE:
            if s.family_policy_hash is not None:
                raise StrategyError("axis", "only evidence steps name a policy")
            return
        if s.synthetic_only and s.state is not _V.LIMITED:
            raise StrategyError("synthetic", "synthetic-only evidence is limited")
        known = [
            p for p in self.family_policies.get(m.family, ()) if p.approved_at <= at
        ]
        fp = next((p for p in known if p.content_hash == s.family_policy_hash), None)
        if fp is None:
            raise StrategyError("predeclared", "no family policy approved before")
        if s.state is _V.LIMITED and not fp.permits_limited_evidence:
            raise StrategyError("limited_not_permitted", fp.policy_id)

    def _check_receipt(
        self, pair: tuple[str, str], prior: tuple[StrategyAdoption, ...], r: object
    ) -> None:
        if type(r) is not StrategyAdoption or (r.tenant_id, r.strategy_id) != pair:
            raise StrategyError("adoption", f"receipt does not match {pair}")
        m = self.manifest(r.strategy_id, r.version)
        if r.kind != "user_strategy_adoption" or r.manifest_hash != m.material_hash:
            raise StrategyError("adoption", "kind or manifest hash mismatch")
        for value in (r.tenant_id, r.principal_id, r.step_up_receipt_id):
            _check_id(value, "consent")
        if not isinstance(r.acknowledgement_text, str):
            raise StrategyError("consent", "acknowledgement text must be a string")
        if prior and ensure_aware_utc(r.signed_at) < prior[-1].signed_at:
            raise StrategyError("time_order", "adoptions are signed in time order")

    def _with(self, name: str, key: object, value: object) -> "StrategyRegistry":
        parts = {f.name: getattr(self, f.name) for f in fields(self)}
        parts[name] = {**parts[name], key: value}
        return StrategyRegistry(**parts)

    def manifest(self, strategy_id: str, version: str) -> StrategyManifest:
        if (found := self.manifests.get((strategy_id, version))) is None:
            raise StrategyError("unknown_strategy", f"{strategy_id}@{version}")
        return found

    def register(self, m: StrategyManifest) -> "StrategyRegistry":
        if type(m) is not StrategyManifest:
            raise StrategyError("manifest", "a StrategyManifest is required")
        key = (m.strategy_id, m.version)
        if (old := self.manifests.get(key)) is not None:
            if old.content_hash != m.content_hash:
                raise StrategyError("immutable", f"{key} is already registered")
            return self
        return self._with("manifests", key, m)

    def with_family_policy(self, p: FamilyPolicy) -> "StrategyRegistry":
        if type(p) is not FamilyPolicy:
            raise StrategyError("family_policy", "a FamilyPolicy is required")
        prior = self.family_policies.get(p.family, ())
        self._check_policy(p.family, prior, p)
        return self._with("family_policies", p.family, (*prior, p))

    def family_policy_at(self, family: Family, at: datetime) -> FamilyPolicy | None:
        known = [p for p in self.family_policies.get(family, ()) if p.approved_at <= at]
        return known[-1] if known else None

    def qualify(
        self,
        axis: Axis,
        strategy_id: str,
        version: str,
        to: AxisState,
        actor_id: str,
        evidence: tuple[QualEvidence, ...],
        at: datetime,
        *,
        config: Mapping[str, object] | None = None,
        tenant: str | None = None,
    ) -> "StrategyRegistry":
        """Append one step. Operational steps key on the material hash, evidence on
        material and configuration hashes, eligibility also on the tenant. An evidence
        step records the family policy in force at `at` (server clock)."""
        at = ensure_aware_utc(at)
        m = self.manifest(strategy_id, version)
        if (config is None) != (axis is Axis.OPERATIONAL) or (
            (tenant is None) != (axis is not Axis.ELIGIBILITY)
        ):
            raise StrategyError("axis", f"{axis} key: config/tenant mismatch")
        key = _key(axis, m, None if config is None else m.config_hash(config), tenant)
        steps = self.qualifications.get(key, ())
        fp = self.family_policy_at(m.family, at)
        evidence_step = axis is Axis.EVIDENCE and to is not _V.NONE
        basis = fp.content_hash if fp and evidence_step else None
        step = AxisStep(to, actor_id, tuple(evidence), at, basis)
        self._check_step(m, key, steps, step)
        return self._with("qualifications", key, (*steps, step))

    def adopt(
        self,
        tenant_id: str,
        strategy_id: str,
        version: str,
        config: Mapping[str, object],
        consent: StrategyConsent,
    ) -> "StrategyRegistry":
        """Append a receipt. `consent.signed_at` is set by the server clock."""
        m = self.manifest(strategy_id, version)
        c = consent
        if type(c) is not StrategyConsent:
            raise StrategyError("consent", "a StrategyConsent is required")
        receipt = StrategyAdoption(
            tenant_id,
            strategy_id,
            version,
            m.material_hash,
            m.config_hash(config),
            c.principal_id,
            c.step_up_receipt_id,
            ensure_aware_utc(c.signed_at),
            c.acknowledgement_text,
        )
        prior = self.adoptions.get((tenant_id, strategy_id), ())
        self._check_receipt((tenant_id, strategy_id), prior, receipt)
        return self._with("adoptions", (tenant_id, strategy_id), (*prior, receipt))


class AdoptionState(StrEnum):
    NONE = "none"
    CURRENT = "current"
    STALE = "stale"  # the latest receipt binds other material or configuration hashes


@dataclass(frozen=True, slots=True)
class StrategyStatus:
    """The strategy card's separate fields (§13) for one tenant and configuration.
    It is not an eligibility decision: the actionable gate (T027 increment 2) also
    needs family capabilities, data rights and the adopted investment policy."""

    manifest_hash: str
    config_hash: str
    operational: OperationalState
    evidence: EvidenceState
    evidence_synthetic_only: bool
    limited_evidence_permitted: bool  # by the family policy the step was assessed under
    eligibility: EligibilityState
    adoption: AdoptionState
    adoption_hash: str | None


def status(
    reg: StrategyRegistry,
    tenant_id: str,
    strategy_id: str,
    version: str,
    config: Mapping[str, object],
    at: datetime,
) -> StrategyStatus:
    """Axis states and adoption known at `at` (server clock). A material or
    configuration change reads as not tested, no evidence, not evaluated and stale."""
    at = ensure_aware_utc(at)
    m = reg.manifest(strategy_id, version)
    cfg = m.config_hash(config)

    def state(axis: Axis, *rest: str) -> AxisStep | None:
        known = [
            s for s in reg.qualifications.get(_key(axis, m, *rest), ()) if s.at <= at
        ]
        return known[-1] if known else None

    ops, ev = state(Axis.OPERATIONAL), state(Axis.EVIDENCE, cfg)
    user = state(Axis.ELIGIBILITY, cfg, tenant_id)
    permits = ev is not None and any(
        p.permits_limited_evidence and p.content_hash == ev.family_policy_hash
        for p in reg.family_policies.get(m.family, ())
    )
    signed = reg.adoptions.get((tenant_id, strategy_id), ())
    known = [r for r in signed if r.signed_at <= at]
    receipt = max(reversed(known), key=lambda r: r.signed_at) if known else None
    adoption = AdoptionState.NONE
    if receipt is not None:
        bound = (receipt.manifest_hash, receipt.config_hash) == (m.material_hash, cfg)
        adoption = AdoptionState.CURRENT if bound else AdoptionState.STALE
    return StrategyStatus(
        m.material_hash,
        cfg,
        _O(ops.state) if ops else _O.NOT_TESTED,
        _V(ev.state) if ev else _V.NONE,
        ev is not None and ev.synthetic_only,
        permits,
        _U(user.state) if user else _U.NOT_EVALUATED,
        adoption,
        receipt.receipt_hash if receipt else None,
    )
