"""Personal adaptation: tenant-private alert and explanation preference candidates
(T045 increment 1; spec §11 user adaptation, §12 private learning; R028, R031, R033,
R039, R040, R084). Domain only, stdlib only.
- `Adaptation` is the closed set of adaptable knobs: per-category coalescing windows
  and external-channel mutes for non-safety notification categories, explanation
  verbosity and format. Nothing else can be expressed. A payload key outside
  `ADAPTABLE` is refused, never ignored: `widens_authority` when it names investment
  policy, risk, sizing, mode, approvals, budget, sharing or assessment terms, else
  `not_adaptable`. Safety categories cannot be muted or coalesced.
- `propose` (rules v0) takes only the tenant's own records through T043 `select` for
  `personal_adaptation` with development access, so the use is logged. `candidate`
  wraps the result as a T044 candidate under a protocol that `check_protocol`
  accepts: personal target, tenant scope, personal evidence purpose, and a scorecard
  holding the §11 hard controls plus `missed_safety_alerts` and `payload_invalid`
  at zero. Monitoring-alert coalescing is capped at 240 minutes.
- `measure` is the assessor's fixture replay. It re-parses the raw candidate payload
  (a stray key counts `risk_broadened`, a bad value `payload_invalid`), counts
  non-personal evidence uses as `private_export`, and replays alert cases with the
  notification centre's coalescing rule. A fixture with no non-safety must-deliver
  case is refused: it could not detect a suppressed alert. So is one with no
  relevant alert in a category the candidate mutes or coalesces differently. Its
  numbers reach a verdict only via the assessor's T032 trial and T044 `assess`.
- `PrivateState` is one tenant's append-only version list. Adoption needs an
  accept-for-review verdict on the active baseline, an artifact hash equal to
  the payload's configuration hash, and each approval class the
  protocol froze, by actors other than the producer and assessor, operator install
  no later than user adoption. Rollback to any retained version appends a new
  version; history is never edited. `erase` drops all stored configurations.
LIMITATIONS: in memory; times and actor ids are caller-asserted (no step-up
receipt yet); per-category coalescing is not yet wired into `notifications.Center`
and explanation knobs have no renderer yet; fixture cases are not yet T043 records.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from qw_domain import improvement as im
from qw_domain.decimals import safe_repr
from qw_domain.feedback import (
    Access,
    Feedback,
    FeedbackCategory,
    ImprovementEvidence,
    Purpose,
    SubjectKind,
    Use,
)
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.notifications import EXTERNAL, SAFETY, Category, Preferences
from qw_domain.research_data import digest
from qw_domain.sources import ID_PATTERN


class AdaptationError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _actor(value: object) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise AdaptationError("malformed", safe_repr(value))
    return value


class Verbosity(StrEnum):
    BRIEF = "brief"
    STANDARD = "standard"
    DETAILED = "detailed"


class ExplanationFormat(StrEnum):
    PROSE = "prose"
    BULLETS = "bullets"


ADAPTIVE = tuple(c for c in Category if c not in SAFETY)
MAX_COALESCE_MINUTES = 1440
# Orchestrator decision (owner may overrule): repeats of a watchlist condition are
# held back for at most four hours.
COALESCE_CAP: Mapping[Category, int] = MappingProxyType(
    {c: 240 if c is Category.MONITORING_ALERT else MAX_COALESCE_MINUTES
     for c in ADAPTIVE})  # fmt: skip
ADAPTABLE = frozenset(
    {f"{k}.{c.value}" for c in ADAPTIVE for k in ("coalesce_minutes", "mute_external")}
    | {"explanation_verbosity", "explanation_format"}
)
# Fragments marking a key as an attempt to widen what the system may do.
_WIDENING = ("polic", "risk", "limit", "sizing", "position", "mode", "approv",
             "leverage", "margin", "order", "broker", "strateg", "instrument",
             "option", "drawdown", "stop", "alloc", "budget", "consent", "share",
             "export", "scope", "tenant", "assessor", "scorecard", "threshold",
             "holdout", "safety", "exposure", "account")  # fmt: skip
PRIVATE_HARD = ("missed_safety_alerts", "payload_invalid")


def _classify(key: object) -> AdaptationError:
    if not isinstance(key, str):
        return AdaptationError("not_adaptable", safe_repr(key))
    k = key.lower().replace("-", "_")
    code = "widens_authority" if any(w in k for w in _WIDENING) else "not_adaptable"
    return AdaptationError(code, safe_repr(key))


@dataclass(frozen=True, slots=True)
class Adaptation:
    coalesce_minutes: Mapping[Category, int] = field(default_factory=dict)
    mute_external: frozenset[Category] = frozenset()
    verbosity: Verbosity = Verbosity.STANDARD
    fmt: ExplanationFormat = ExplanationFormat.PROSE

    def __post_init__(self) -> None:
        windows = dict(self.coalesce_minutes)
        for c, m in windows.items():
            if type(c) is not Category or type(m) is not int:
                raise TypeError("coalesce_minutes maps Category to int")
            if c in SAFETY:
                raise AdaptationError("safety_category", c.value)
            if not 0 <= m <= COALESCE_CAP[c]:
                raise AdaptationError("value", f"coalesce_minutes.{c.value}")
        mutes = frozenset(self.mute_external)
        if not all(type(c) is Category for c in mutes):
            raise TypeError("mute_external holds Category values")
        if mutes & SAFETY:
            raise AdaptationError("safety_category", "mute_external")
        if type(self.verbosity) is not Verbosity or (
            type(self.fmt) is not ExplanationFormat
        ):  # fmt: skip
            raise TypeError("typed verbosity and format are required")
        kept = {c: windows[c] for c in ADAPTIVE if windows.get(c)}  # 0 is the default
        object.__setattr__(self, "coalesce_minutes", MappingProxyType(kept))
        object.__setattr__(self, "mute_external", mutes)

    def to_payload(self) -> dict[str, str]:
        out = {"explanation_verbosity": self.verbosity.value,
               "explanation_format": self.fmt.value}  # fmt: skip
        for c in ADAPTIVE:
            out[f"coalesce_minutes.{c.value}"] = str(self.coalesce_minutes.get(c, 0))
            out[f"mute_external.{c.value}"] = str(c in self.mute_external).lower()
        return dict(sorted(out.items()))

    @classmethod
    def from_payload(cls, payload: Mapping[str, str]) -> "Adaptation":
        """Strict, canonical parse; absent knobs keep their defaults."""
        for k, val in payload.items():
            if k not in ADAPTABLE:
                raise _classify(k)
            if not isinstance(val, str):
                raise AdaptationError("value", safe_repr(k))
        windows, mutes = {}, set()
        for c in ADAPTIVE:
            w = payload.get(f"coalesce_minutes.{c.value}", "0")
            ok = len(w) <= 4 and w.isascii() and w.isdigit() and str(int(w)) == w
            if not ok or int(w) > COALESCE_CAP[c]:
                raise AdaptationError("value", f"coalesce_minutes.{c.value}")
            windows[c] = int(w)
            m = payload.get(f"mute_external.{c.value}", "false")
            if m not in ("true", "false"):
                raise AdaptationError("value", f"mute_external.{c.value}")
            if m == "true":
                mutes.add(c)
        try:
            v = Verbosity(payload.get("explanation_verbosity", "standard"))
            f = ExplanationFormat(payload.get("explanation_format", "prose"))
        except ValueError:
            raise AdaptationError("value", "explanation") from None
        return cls(windows, frozenset(mutes), v, f)

    @property
    def content_hash(self) -> str:
        return digest(self.to_payload())


def apply(prefs: Preferences, a: Adaptation) -> Preferences:
    """External mutes on top of the user's own preferences; in-app is untouched."""
    muted = {(c, ch) for c in a.mute_external for ch in EXTERNAL}
    return Preferences(prefs.in_app, prefs.email, prefs.browser_push,
                       prefs.quiet_hours_timezone, prefs.quiet_hours,
                       prefs.redact_financial_values,
                       prefs.muted | muted)  # fmt: skip


def scorecard(scorecard_id: str) -> im.Scorecard:
    """The personal-adaptation scorecard: hard controls at zero, goals vs baseline."""
    t, d, z = im.Threshold, im.Direction, Decimal(0)
    hard = [*im.HARD_CONTROLS[im.Target.PERSONAL_ADAPTATION], *PRIVATE_HARD]
    return im.Scorecard(scorecard_id, im.Target.PERSONAL_ADAPTATION, (
        *(t(m, d.AT_MOST, z, True) for m in hard),
        t("improved", d.AT_LEAST, Decimal(1), False),  # fewer noisy or clearer
        t("redundant_alerts_removed", d.AT_LEAST, z, False),
        t("relevant_alerts_lost", d.AT_MOST, z, False),
        t("explanation_mismatch_change", d.AT_MOST, z, False),
    ))  # fmt: skip


def check_protocol(p: im.Protocol) -> None:
    if (p.target, p.scope, p.evidence_purpose) != (
        im.Target.PERSONAL_ADAPTATION, im.Scope.TENANT, Purpose.PERSONAL_ADAPTATION
    ):  # fmt: skip
        raise AdaptationError("not_private", p.experiment_id)
    held = {t.metric for t in p.scorecard.thresholds
            if t.hard and t.bound == 0
            and t.direction is im.Direction.AT_MOST}  # fmt: skip
    if not held.issuperset(PRIVATE_HARD):
        raise AdaptationError("safety_control_missing", p.experiment_id)


def propose(
    evidence: ImprovementEvidence, tenant_id: str, experiment_id: str,
    current: Adaptation, at: datetime,
) -> tuple[Adaptation, Use]:  # fmt: skip
    """Rules v0 over the tenant's own feedback: two or more `irrelevant` notification
    reports add 60 minutes of monitoring-alert coalescing unless any `missed_risk`
    report exists; more `unclear` than `clear` explanation reports step verbosity up.
    """
    sel = evidence.select(tenant_id, experiment_id, Purpose.PERSONAL_ADAPTATION,
                          Access.DEVELOPMENT, at)  # fmt: skip
    fb = [r for r in sel.records if isinstance(r, Feedback)]
    notes = [f for f in fb if f.subject.kind is SubjectKind.NOTIFICATION]
    other = [f.category for f in fb if f.subject.kind is not SubjectKind.NOTIFICATION]
    windows = dict(current.coalesce_minutes)
    noisy = sum(f.category is FeedbackCategory.IRRELEVANT for f in notes)
    if noisy >= 2 and not any(f.category is FeedbackCategory.MISSED_RISK for f in fb):
        mon = Category.MONITORING_ALERT
        windows[mon] = min(windows.get(mon, 0) + 60, COALESCE_CAP[mon])
    levels = list(Verbosity)
    v = current.verbosity
    if other.count(FeedbackCategory.UNCLEAR) > other.count(FeedbackCategory.CLEAR):
        v = levels[min(levels.index(v) + 1, len(levels) - 1)]
    out = Adaptation(windows, current.mute_external, v, current.fmt)
    return out, sel.use


def candidate(
    candidate_id: str, protocol: im.Protocol, parent_id: str, a: Adaptation,
    uses: Sequence[Use], runtime_versions: Mapping[str, str], created_at: datetime,
) -> im.Candidate:  # fmt: skip
    check_protocol(protocol)
    return im.Candidate(candidate_id, protocol.experiment_id, protocol.target,
                        parent_id, protocol.producer, a.content_hash, a.to_payload(),
                        runtime_versions, tuple(uses), created_at)  # fmt: skip


@dataclass(frozen=True, slots=True)
class AlertCase:  # a labelled fixture alert: SYNTHETIC unless stated otherwise
    case_id: str
    category: Category
    object_id: str
    at: datetime
    relevant: bool
    must_deliver: bool  # safety category, or flagged by the user as must-not-miss

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_aware_utc(self.at))
        if type(self.relevant) is not bool or type(self.must_deliver) is not bool:
            raise TypeError("relevant and must_deliver are bools")
        if self.category in SAFETY and not self.must_deliver:
            raise AdaptationError("safety_category", self.case_id)


@dataclass(frozen=True, slots=True)
class ExplanationCase:  # the explanation style the user marked as clear
    case_id: str
    verbosity: Verbosity
    fmt: ExplanationFormat


@dataclass(frozen=True, slots=True)
class Fixture:
    fixture_id: str
    alerts: tuple[AlertCase, ...]
    explanations: tuple[ExplanationCase, ...]


def delivered(a: Adaptation, alerts: Sequence[AlertCase]) -> frozenset[str]:
    """External deliveries under `a`, mirroring `Center.submit`: safety always
    goes; a repeat for the same (category, object) inside the window coalesces and
    does not move the window."""
    last: dict[tuple[Category, str], datetime] = {}
    out = set()
    for c in sorted(alerts, key=lambda c: (c.at, c.case_id)):
        if c.category in SAFETY:
            out.add(c.case_id)
            continue
        if c.category in a.mute_external:
            continue
        key, w = (c.category, c.object_id), a.coalesce_minutes.get(c.category, 0)
        prev = last.get(key)
        if prev is not None and (c.at - prev).total_seconds() < w * 60:
            continue
        last[key] = c.at
        out.add(c.case_id)
    return frozenset(out)


def measure(
    protocol: im.Protocol, c: im.Candidate, baseline: Adaptation, fixture: Fixture
) -> Mapping[str, Decimal]:
    """The assessor's fixture metrics for `c` against the frozen baseline. A bad
    payload key or value is a counted hard violation; the rest is replayed."""
    check_protocol(protocol)
    if baseline.content_hash != protocol.baseline_hash:
        raise AdaptationError("baseline_hash", protocol.experiment_id)
    if (c.experiment_id, c.target) != (protocol.experiment_id, protocol.target):
        raise AdaptationError("candidate_experiment", c.candidate_id)
    if not any(x.must_deliver and x.category not in SAFETY for x in fixture.alerts):
        raise AdaptationError("fixture_no_safety_case", fixture.fixture_id)
    stray = [k for k in c.payload if k not in ADAPTABLE]
    good, invalid = {}, 0
    for k, v in c.payload.items():
        if k in ADAPTABLE:
            try:
                Adaptation.from_payload({k: v})
                good[k] = v
            except AdaptationError:
                invalid += 1
    a = Adaptation.from_payload(good)
    for cat in ADAPTIVE:  # every changed category must be measurable here
        old = (cat in baseline.mute_external, baseline.coalesce_minutes.get(cat, 0))
        if (cat in a.mute_external, a.coalesce_minutes.get(cat, 0)) != old and not any(
            x.relevant and x.category is cat for x in fixture.alerts
        ):
            raise AdaptationError("fixture_category_uncovered", cat.value)
    leaks = [u for u in c.evidence_uses
             if (u.tenant_id, u.purpose, u.recipient, u.consent_ids)
             != (protocol.scope_id, Purpose.PERSONAL_ADAPTATION, None, ())]  # fmt: skip
    base, new = delivered(baseline, fixture.alerts), delivered(a, fixture.alerts)
    noise = {x.case_id for x in fixture.alerts if not x.relevant}
    useful = {x.case_id for x in fixture.alerts if x.relevant}
    must = {x.case_id for x in fixture.alerts if x.must_deliver}

    def misfit(v: Verbosity, f: ExplanationFormat) -> int:
        return sum((e.verbosity, e.fmt) != (v, f) for e in fixture.explanations)

    removed = len(base & noise) - len(new & noise)
    clearer = misfit(a.verbosity, a.fmt) - misfit(baseline.verbosity, baseline.fmt)
    counts = {
        "risk_broadened": len(stray), "private_export": len(leaks),
        "missed_safety_alerts": len(must - new), "payload_invalid": invalid,
        "improved": int(removed >= 1 or clearer <= -1),
        "redundant_alerts_removed": removed,
        "relevant_alerts_lost": len((base & useful) - new),
        "explanation_mismatch_change": clearer,
    }  # fmt: skip
    return MappingProxyType({k: Decimal(v) for k, v in counts.items()})


@dataclass(frozen=True, slots=True)
class Approval:  # a human runtime approval, recorded elsewhere (caller-asserted)
    approval_class: im.ApprovalClass
    actor_id: str
    candidate_id: str
    at: datetime

    def __post_init__(self) -> None:
        if type(self.approval_class) is not im.ApprovalClass:
            raise TypeError("approval_class must be an ApprovalClass")
        _actor(self.actor_id)
        _actor(self.candidate_id)
        object.__setattr__(self, "at", ensure_aware_utc(self.at))


@dataclass(frozen=True, slots=True)
class Version:
    number: int
    adaptation: Adaptation | None  # None once erased
    adaptation_hash: str
    source: str  # baseline | candidate:<id> | rollback:<n> | erased
    actor_ids: tuple[str, ...]
    at: datetime


@dataclass
class PrivateState:
    """One tenant's adapted configuration history. Append-only except `erase`."""

    tenant_id: str
    _history: list[Version] = field(repr=False)

    @property
    def versions(self) -> tuple[Version, ...]:
        return tuple(self._history)

    @classmethod
    def open(cls, tenant_id: str, baseline: Adaptation, at: datetime) -> "PrivateState":
        _actor(tenant_id)
        v = Version(1, baseline, baseline.content_hash, "baseline", (),
                    ensure_aware_utc(at))  # fmt: skip
        return cls(tenant_id, [v])

    @property
    def active(self) -> Adaptation:
        a = self.versions[-1].adaptation
        return Adaptation() if a is None else a

    def _push(self, a: Adaptation, source: str, actors: tuple[str, ...],
              at: datetime) -> Version:  # fmt: skip
        at = ensure_aware_utc(at)
        if at < self.versions[-1].at:
            raise AdaptationError("time_order", format_instant(at))
        v = Version(len(self.versions) + 1, a, a.content_hash, source, actors, at)
        self._history.append(v)
        return v

    def adopt(self, registry: im.ExperimentRegistry, candidate_id: str,
              approvals: Sequence[Approval], at: datetime) -> Version:  # fmt: skip
        """Make an assessed candidate active. Every check runs before any change."""
        if registry.scope_id != self.tenant_id:
            raise AdaptationError("tenant", "registry of another scope")
        c = registry.candidate(candidate_id)
        p = registry.protocol(c.experiment_id)
        check_protocol(p)
        if any(v.source == f"candidate:{candidate_id}" for v in self.versions):
            raise AdaptationError("already_adopted", candidate_id)
        verdicts = registry.verdicts(candidate_id)
        if not verdicts or verdicts[-1].decision is not im.Decision.ACCEPT_FOR_REVIEW:
            raise AdaptationError("not_accepted", candidate_id)
        if self.active.content_hash != p.baseline_hash:
            raise AdaptationError("stale_baseline", candidate_id)
        mine = {a.approval_class: a for a in approvals
                if a.candidate_id == candidate_id}  # fmt: skip
        if not p.approvals <= mine.keys():
            raise AdaptationError("approvals_missing", candidate_id)
        agents = {p.producer.actor_id, p.assessor.actor_id}
        needed = [mine[k] for k in sorted(p.approvals)]
        if any(a.actor_id in agents for a in needed):
            raise AdaptationError("approver_not_human", candidate_id)
        if any(not verdicts[-1].assessed_at <= a.at <= at for a in needed):
            raise AdaptationError("approval_before_verdict", candidate_id)
        install = mine[im.ApprovalClass.OPERATOR_INSTALL]
        if install.at > mine[im.ApprovalClass.USER_STRATEGY_ADOPTION].at:
            raise AdaptationError("install_after_adoption", candidate_id)
        a = Adaptation.from_payload(c.payload)
        if a.content_hash != c.artifact_hash:
            raise AdaptationError("artifact_mismatch", candidate_id)
        return self._push(a, f"candidate:{candidate_id}",
                          tuple(x.actor_id for x in needed), at)  # fmt: skip

    def rollback(self, number: int, actor_id: str, at: datetime) -> Version:
        """Restore a retained version as a new one; the record says which."""
        _actor(actor_id)
        if not 1 <= number <= len(self.versions):
            raise AdaptationError("unknown_version", str(number))
        old = self.versions[number - 1].adaptation
        if old is None:
            raise AdaptationError("erased", str(number))
        if old == self.active:
            raise AdaptationError("already_active", str(number))
        return self._push(old, f"rollback:{number}", (actor_id,), at)

    def export(self) -> tuple[dict[str, object], ...]:
        """The owner's own history (data portability); never another tenant's."""
        return tuple({
            "tenant_id": self.tenant_id, "number": v.number, "source": v.source,
            "adaptation_hash": v.adaptation_hash, "actor_ids": list(v.actor_ids),
            "at": format_instant(v.at),
            "adaptation": None if v.adaptation is None else v.adaptation.to_payload(),
        } for v in self.versions)  # fmt: skip

    def erase(self, actor_id: str, at: datetime) -> Version:
        """Delete private learned state (spec §12): stored configurations are
        dropped and the default becomes active; numbers and sources remain."""
        v = self._push(Adaptation(), "erased", (_actor(actor_id),), at)
        self._history[:-1] = [Version(x.number, None, "erased", x.source, x.actor_ids,
                                      x.at) for x in self.versions[:-1]]  # fmt: skip
        return v
