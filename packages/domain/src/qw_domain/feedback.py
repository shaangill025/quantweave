"""Feedback and improvement evidence (T043 increment 1; spec §7, §11, §12, §14;
R028, R031, R040, R052, R084, R087). Evidence capture only: nothing here feeds a
live decision, proposal, review or order.
- Records (`Feedback`, `ErrorRecord`, `Outcome`, `EvaluationCase`) are tenant-scoped,
  append-only and bound to the exact object version they concern (`Subject`). A
  retry with identical content returns the stored record and its times; other
  content under the same id is refused.
- An outcome without an execution (dismissed, expired, planned, ...) has no result:
  it is not a losing or winning trade. A result needs a recorded fact of the basis
  its disposition implies (execution report, journal event, or virtual entry), so
  virtual outcomes are always labelled virtual and `tally` never merges bases.
- Consent (R031): personal adaptation within the tenant needs none. Shared use needs
  a separate `ConsentGrant` for the recipient, terms version and record kind, in its
  window and not revoked: default deny. Feedback cannot carry consent (the contract
  fixes it to false). Revocation is prospective; `affected_uses` flags earlier uses.
- `select` is the only way to take records for an improvement purpose; every call is
  logged (R084). Final-holdout cases never reach development and are read at most
  once per experiment. Cases hold only `CASE_FIELDS`: no account ids, amounts or
  free text. Deletion tombstones drop content; restore is deferred to persistence.
Stdlib only; no persistence. Retention periods are not invented here (spec §12).
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

from qw_domain.decimals import safe_repr
from qw_domain.evaluator import Review
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.proposals import ExecutionState, Proposal, ProposalState
from qw_domain.researcher import Abstention
from qw_domain.sources import ID_PATTERN

MAX_MESSAGE = 4000  # openapi.yaml Feedback.message maxLength
MAX_DETAIL = 500
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class FeedbackError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise FeedbackError(what, f"{safe_repr(value)} is malformed")
    return value


def _enum[E: StrEnum](value: object, cls: type[E], what: str) -> E:
    if type(value) is not cls:
        raise TypeError(f"{what} must be {cls.__name__}")
    return value


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FeedbackError(what, "a SHA-256 hex digest, not content")
    return value


def _code(part: str) -> str:  # model text never enters a record (S1)
    return part if ID_PATTERN.fullmatch(part) else "redacted"


def _digest(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class SubjectKind(StrEnum):
    THESIS = "thesis"
    REVIEW = "review"
    PROPOSAL = "proposal"
    NOTIFICATION = "notification"
    CLAIM = "claim"
    MODEL_RUN = "model_run"
    SIMULATION = "simulation"


@dataclass(frozen=True, slots=True)
class Subject:  # the exact object version a record concerns
    kind: SubjectKind
    object_id: str
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        _enum(self.kind, SubjectKind, "kind")
        _id(self.object_id, "subject")
        if type(self.version) is not int or self.version < 1:
            raise FeedbackError("subject", "version is a positive int")
        _sha(self.content_hash, "subject")


class RecordKind(StrEnum):  # consent data categories
    FEEDBACK = "feedback"
    ERROR = "error"
    OUTCOME = "outcome"
    EVALUATION_CASE = "evaluation_case"


@dataclass(frozen=True, slots=True, kw_only=True)
class Record:
    KIND: ClassVar[RecordKind]
    tenant_id: str
    record_id: str
    subject: Subject
    occurred_at: datetime  # source time, never reset on retry
    recorded_at: datetime

    def __post_init__(self) -> None:
        _id(self.tenant_id, "tenant")
        _id(self.record_id, "record_id")
        if type(self.subject) is not Subject:
            raise TypeError("subject must be a Subject")
        for name in ("occurred_at", "recorded_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))
        if self.recorded_at < self.occurred_at:
            raise FeedbackError("time_order", "recorded before it occurred")


class FeedbackCategory(StrEnum):  # openapi.yaml Feedback.category
    INCORRECT_FACT = "incorrect_fact"
    IRRELEVANT = "irrelevant"
    CLEAR = "clear"
    UNCLEAR = "unclear"
    MISSED_RISK = "missed_risk"
    OTHER = "other"


@dataclass(frozen=True, slots=True, kw_only=True)
class Feedback(Record):
    KIND = RecordKind.FEEDBACK
    principal_id: str
    category: FeedbackCategory
    message: str  # private; never logged

    def __post_init__(self) -> None:
        Record.__post_init__(self)
        _id(self.principal_id, "principal_id")
        _enum(self.category, FeedbackCategory, "category")
        if not isinstance(self.message, str) or len(self.message) > MAX_MESSAGE:
            raise FeedbackError("message", f"text of at most {MAX_MESSAGE} characters")


class ErrorKind(StrEnum):
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    SYSTEM_ERROR = "system_error"
    EVALUATOR_DISAGREEMENT = "evaluator_disagreement"
    ABSTENTION = "abstention"


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorRecord(Record):
    KIND = RecordKind.ERROR
    error_kind: ErrorKind
    code: str
    detail: str  # structured codes only: no prompt, response or financial data

    def __post_init__(self) -> None:
        Record.__post_init__(self)
        _enum(self.error_kind, ErrorKind, "error_kind")
        _id(self.code, "code")
        if not isinstance(self.detail, str) or len(self.detail) > MAX_DETAIL:
            raise FeedbackError("detail", f"text of at most {MAX_DETAIL} characters")


def errors_from(
    tenant_id: str, subject: Subject, outcome: Review | Abstention,
    occurred_at: datetime, recorded_at: datetime,
) -> tuple[ErrorRecord, ...]:  # fmt: skip
    """Error records for one evaluator outcome: an abstention, each material ground
    of a review, and each evaluator claim that failed the claim rules. Ids derive
    from content and the source time, so a retry maps to the same records."""
    rows: list[tuple[ErrorKind, str, str]] = []
    if isinstance(outcome, Abstention):
        detail = "; ".join(outcome.details)
        rows.append((ErrorKind.ABSTENTION, outcome.why.value, detail))
    elif isinstance(outcome, Review):
        rows += [(ErrorKind.EVALUATOR_DISAGREEMENT, g.split(":", 1)[0], g)
                 for g in outcome.material]  # fmt: skip
        rows += [(ErrorKind.MODEL_ERROR, "claim_rejected", c)
                 for c in outcome.rejected_claims]  # fmt: skip
    else:
        raise TypeError("outcome is a Review or an Abstention")
    at = format_instant(occurred_at)
    out = []
    rows = [(k, c, "; ".join(_code(x) for x in d.split("; "))) for k, c, d in rows]
    for k, c, d in rows:
        rid = "err-" + _digest([tenant_id, asdict(subject), k, c, d, at])[:32]
        out.append(ErrorRecord(
            tenant_id=tenant_id, record_id=rid, subject=subject,
            occurred_at=occurred_at, recorded_at=recorded_at, error_kind=k, code=c,
            detail=d[:MAX_DETAIL],
        ))  # fmt: skip
    return tuple(out)


class Disposition(StrEnum):
    DISMISSED = "dismissed"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    RESEARCH_ONLY = "research_only"
    PLANNED = "planned"  # an intention, not an execution
    USER_REPORTED = "user_reported"
    BROKER_CONFIRMED = "broker_confirmed"
    RECONCILED = "reconciled"
    VIRTUAL_FILLED = "virtual_filled"


class Result(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # nothing was executed
    UNKNOWN = "unknown"  # executed, no result recorded yet
    GAIN = "gain"
    LOSS = "loss"
    FLAT = "flat"


class Basis(StrEnum):
    NONE = "none"
    REAL = "real"
    VIRTUAL = "virtual"


class FactKind(StrEnum):
    EXECUTION_REPORT = "execution_report"  # user-reported, not broker-confirmed
    JOURNAL_EVENT = "journal_event"
    VIRTUAL_ENTRY = "virtual_entry"  # simulator ledger only


@dataclass(frozen=True, slots=True)
class FactRef:
    kind: FactKind
    ref_id: str

    def __post_init__(self) -> None:
        _enum(self.kind, FactKind, "kind")
        _id(self.ref_id, "ref_id")


D = Disposition
# The recorded fact each executed disposition may rest on, and its basis.
FACT: MappingProxyType[Disposition, tuple[FactKind, Basis]] = MappingProxyType({
    D.USER_REPORTED: (FactKind.EXECUTION_REPORT, Basis.REAL),
    D.BROKER_CONFIRMED: (FactKind.JOURNAL_EVENT, Basis.REAL),
    D.RECONCILED: (FactKind.JOURNAL_EVENT, Basis.REAL),
    D.VIRTUAL_FILLED: (FactKind.VIRTUAL_ENTRY, Basis.VIRTUAL),
})  # fmt: skip


@dataclass(frozen=True, slots=True, kw_only=True)
class Outcome(Record):
    KIND = RecordKind.OUTCOME
    disposition: Disposition
    result: Result
    fact: FactRef | None

    def __post_init__(self) -> None:
        Record.__post_init__(self)
        _enum(self.disposition, Disposition, "disposition")
        _enum(self.result, Result, "result")
        if self.fact is not None and type(self.fact) is not FactRef:
            raise TypeError("fact must be a FactRef")
        allowed = FACT.get(self.disposition)
        if allowed is None:
            if self.result is not Result.NOT_APPLICABLE or self.fact is not None:
                raise FeedbackError("no_execution", f"{self.disposition}: no result")
            return
        if self.subject.kind is not SubjectKind.PROPOSAL:
            raise FeedbackError("subject", "an execution outcome binds a proposal")
        if self.result is Result.NOT_APPLICABLE:
            raise FeedbackError("result", "an execution has a result or unknown")
        if self.fact is not None and self.fact.kind is not allowed[0]:
            raise FeedbackError("fact", f"{self.disposition} rests on {allowed[0]}")
        if self.result is not Result.UNKNOWN and self.fact is None:
            raise FeedbackError("fact", "a result needs a recorded fact")

    @property
    def basis(self) -> Basis:
        allowed = FACT.get(self.disposition)
        return Basis.NONE if allowed is None else allowed[1]


_NAMES = frozenset(d.value for d in D)
_FROM_STATE = {s: Disposition(s.value) for s in ProposalState if s.value in _NAMES}
_FROM_EXECUTION = {e: Disposition(e.value) for e in ExecutionState if e.value in _NAMES}


def outcome_of(p: Proposal, n: int, record_id: str, recorded_at: datetime) -> Outcome:
    """The disposition of proposal version `n` from its own history. No result is
    inferred: an executed disposition reads `unknown` until a fact is recorded."""
    if type(n) is not int or not 1 <= n <= len(p.versions):
        raise FeedbackError("version", f"{safe_repr(n)} is not a version")
    v = p.versions[n - 1]
    state, execution = p.state(n), p.execution(n)
    disp = _FROM_EXECUTION.get(execution) or _FROM_STATE.get(state)
    if disp is None:
        raise FeedbackError("live", f"version {n} is {state} with no outcome yet")
    last = next(e for e in reversed(p.events) if e.version == n)
    return Outcome(
        tenant_id=v.action.tenant_id, record_id=record_id,
        subject=Subject(SubjectKind.PROPOSAL, v.proposal_id, n, v.content_hash),
        occurred_at=last.at, recorded_at=recorded_at, disposition=disp,
        result=Result.NOT_APPLICABLE if disp not in FACT else Result.UNKNOWN,
        fact=None,
    )  # fmt: skip


def tally(outcomes: Iterable[Outcome]) -> Mapping[tuple[Basis, Result], int]:
    return MappingProxyType(Counter((o.basis, o.result) for o in outcomes))


class Split(StrEnum):
    DEVELOPMENT = "development"
    FINAL_HOLDOUT = "final_holdout"


class CaseOrigin(StrEnum):
    PERSONAL_HISTORICAL = "personal_historical"
    PERSONAL_PROSPECTIVE = "personal_prospective"
    SYNTHETIC = "synthetic"  # never personal learned history or live evidence


CASE_FIELDS = frozenset({
    "instrument_id", "attribute", "period", "unit", "claim_kind", "as_of",
    "feedback_category", "error_code", "evidence_digest", "proposal_hash",
})  # fmt: skip
_DIGEST_FIELDS = frozenset({"evidence_digest", "proposal_hash"})


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationCase(Record):
    KIND = RecordKind.EVALUATION_CASE
    inputs: Mapping[str, str]
    expected: str
    split: Split
    origin: CaseOrigin

    def __post_init__(self) -> None:
        Record.__post_init__(self)
        _enum(self.split, Split, "split")
        _enum(self.origin, CaseOrigin, "origin")
        _id(self.expected, "expected")
        if not isinstance(self.inputs, Mapping) or not self.inputs:
            raise FeedbackError("inputs", "a non-empty mapping")
        for key, value in self.inputs.items():
            if key not in CASE_FIELDS:
                raise FeedbackError("field_not_permitted", safe_repr(key))
            (_sha if key in _DIGEST_FIELDS else _id)(value, key)
        frozen = MappingProxyType(dict(sorted(self.inputs.items())))
        object.__setattr__(self, "inputs", frozen)

    @property
    def case_hash(self) -> str:
        return _digest([asdict(self.subject), dict(self.inputs), self.expected,
                        self.split, self.origin])  # fmt: skip

    @property
    def personal_history(self) -> bool:
        return self.origin is not CaseOrigin.SYNTHETIC


type AnyRecord = Feedback | ErrorRecord | Outcome | EvaluationCase
_RECORD_TYPES = (Feedback, ErrorRecord, Outcome, EvaluationCase)


class Purpose(StrEnum):  # improvement purposes only; no live-decision purpose exists
    PERSONAL_ADAPTATION = "personal_adaptation"  # this tenant only
    SHARED_IMPROVEMENT = "shared_improvement"  # leaves the tenant: needs consent


class Access(StrEnum):
    DEVELOPMENT = "development"
    FINAL_ASSESSMENT = "final_assessment"


@dataclass(frozen=True, slots=True)
class ConsentGrant:
    """Explicit, separate consent: purpose, recipient, terms version and record
    kinds (spec §12). Revocable; optional expiry (exclusive)."""

    tenant_id: str
    consent_id: str
    principal_id: str
    purpose: Purpose
    recipient: str
    data_categories: frozenset[RecordKind]
    terms_version: str
    granted_at: datetime
    expires_at: datetime | None
    covers_prior: bool = False  # records made before the grant: only if True

    def __post_init__(self) -> None:
        if type(self.covers_prior) is not bool:
            raise TypeError("covers_prior must be a bool")
        for name in ("tenant_id", "consent_id", "principal_id", "terms_version"):
            _id(getattr(self, name), name)
        if _enum(self.purpose, Purpose, "purpose") is Purpose.PERSONAL_ADAPTATION:
            raise FeedbackError("purpose", "personal use needs no grant or recipient")
        _id(self.recipient, "recipient")
        cats = self.data_categories
        if type(cats) is not frozenset or not cats or not all(
            type(c) is RecordKind for c in cats
        ):  # fmt: skip
            raise FeedbackError("data_categories", "a non-empty set of RecordKind")
        object.__setattr__(self, "granted_at", ensure_aware_utc(self.granted_at))
        if self.expires_at is not None:
            end = ensure_aware_utc(self.expires_at)
            object.__setattr__(self, "expires_at", end)
            if end <= self.granted_at:
                raise FeedbackError("expires_at", "after granted_at")


@dataclass(frozen=True, slots=True)
class Use:  # one logged selection, kept even when empty
    tenant_id: str
    experiment_id: str
    purpose: Purpose
    access: Access
    recipient: str | None
    record_ids: tuple[str, ...]
    consent_ids: tuple[str, ...]
    at: datetime


@dataclass(frozen=True, slots=True)
class Selection:
    records: tuple[AnyRecord, ...]
    excluded: tuple[tuple[str, str], ...]  # (record id, reason)
    consent_ids: tuple[str, ...]
    use: Use


@dataclass(frozen=True, slots=True)
class AffectedUse:
    use: Use
    reason: str  # consent_revoked | record_deleted


@dataclass(frozen=True, slots=True)
class Tombstone:
    tenant_id: str
    record_id: str
    deleted_at: datetime


@dataclass
class ImprovementEvidence:
    """Append-only, tenant-scoped; every lookup is keyed by (tenant, id)."""

    _records: dict[tuple[str, str], AnyRecord] = field(default_factory=dict)
    _grants: dict[tuple[str, str], ConsentGrant] = field(default_factory=dict)
    _revoked: dict[tuple[str, str], datetime] = field(default_factory=dict)
    _deleted: dict[tuple[str, str], Tombstone] = field(default_factory=dict)
    _uses: list[Use] = field(default_factory=list)
    _clock: dict[str, datetime] = field(default_factory=dict)

    def _tick(self, tenant_id: str, at: datetime) -> datetime:
        at = ensure_aware_utc(at)
        last = self._clock.get(_id(tenant_id, "tenant"))
        if last is not None and at < last:
            raise FeedbackError("time_order", f"{format_instant(at)} is backdated")
        self._clock[tenant_id] = at
        return at

    def record(self, rec: AnyRecord) -> AnyRecord:
        if not isinstance(rec, _RECORD_TYPES):
            raise TypeError("not an improvement evidence record")
        key = (rec.tenant_id, rec.record_id)
        if key in self._deleted:
            raise FeedbackError("deleted", f"{safe_repr(rec.record_id)} is tombstoned")
        old = self._records.get(key)
        if old is not None:
            if type(old) is type(rec) and old == replace(
                rec, recorded_at=old.recorded_at
            ):
                return old  # retry: the stored times stand
            raise FeedbackError("conflicting_duplicate", safe_repr(rec.record_id))
        self._tick(rec.tenant_id, rec.recorded_at)
        self._records[key] = rec
        return rec

    def get(self, tenant_id: str, record_id: str) -> AnyRecord:
        """Owner display read. A final-holdout case is read only through `select`."""
        if (tenant_id, record_id) in self._deleted:
            raise FeedbackError("deleted", safe_repr(record_id))
        found = self._records.get((tenant_id, record_id))
        if found is None:
            raise FeedbackError("not_found", f"{safe_repr(record_id)} in this tenant")
        if isinstance(found, EvaluationCase) and found.split is Split.FINAL_HOLDOUT:
            raise FeedbackError("final_holdout", "read through select only")
        return found

    def grant(self, g: ConsentGrant) -> ConsentGrant:
        if type(g) is not ConsentGrant:
            raise TypeError("a ConsentGrant")
        old = self._grants.get((g.tenant_id, g.consent_id))
        if old is not None:
            if old == g:
                return old
            raise FeedbackError("exists", safe_repr(g.consent_id))
        self._tick(g.tenant_id, g.granted_at)
        self._grants[(g.tenant_id, g.consent_id)] = g
        return g

    def revoke(self, tenant_id: str, consent_id: str, at: datetime) -> None:
        key = (tenant_id, consent_id)
        if key not in self._grants:
            raise FeedbackError("not_found", f"{safe_repr(consent_id)} in this tenant")
        if key in self._revoked:
            raise FeedbackError("revoked", safe_repr(consent_id))
        self._revoked[key] = self._tick(tenant_id, at)

    def delete(self, tenant_id: str, record_id: str, at: datetime) -> Tombstone:
        if (tenant_id, record_id) not in self._records:
            self.get(tenant_id, record_id)  # raises deleted or not_found
        stone = Tombstone(tenant_id, record_id, self._tick(tenant_id, at))
        del self._records[(tenant_id, record_id)]
        self._deleted[(tenant_id, record_id)] = stone
        return stone

    def _consents(self, tenant_id: str, recipient: str, terms: str,
                  at: datetime) -> list[ConsentGrant]:  # fmt: skip
        out = []
        for (t, cid), g in self._grants.items():
            revoked = self._revoked.get((t, cid))
            if (
                t == tenant_id and g.recipient == recipient
                and g.terms_version == terms and g.granted_at <= at
                and (g.expires_at is None or at < g.expires_at)
                and (revoked is None or at < revoked)
            ):  # fmt: skip
                out.append(g)
        return out

    def select(
        self, tenant_id: str, experiment_id: str, purpose: Purpose, access: Access,
        at: datetime, *, recipient: str | None = None, terms_version: str | None = None,
    ) -> Selection:  # fmt: skip
        """The tenant's records usable for this purpose at `at`, with a reason for
        each one withheld. Every call is logged as a `Use`."""
        _id(experiment_id, "experiment_id")
        _enum(purpose, Purpose, "purpose")
        _enum(access, Access, "access")
        personal = purpose is Purpose.PERSONAL_ADAPTATION
        if personal != (recipient is None and terms_version is None):
            raise FeedbackError("recipient", "shared use names a recipient and terms")
        if not personal:
            _id(recipient, "recipient")
            _id(terms_version, "terms_version")
        at = self._tick(tenant_id, at)
        grants = [] if personal else self._consents(
            tenant_id, str(recipient), str(terms_version), at)  # fmt: skip
        read = {
            rid for u in self._uses
            if u.tenant_id == tenant_id and u.experiment_id == experiment_id
            and u.access is Access.FINAL_ASSESSMENT for rid in u.record_ids
        }  # fmt: skip
        taken: list[AnyRecord] = []
        excluded: list[tuple[str, str]] = []
        consents: set[str] = set()
        for (t, rid), rec in self._records.items():
            if t != tenant_id:
                continue
            reason = _holdout(rec, access, rid in read)
            if reason is None and not personal:
                reason, cover = _consent(rec, grants)
                consents.update(cover[:1])
            if reason is None:
                taken.append(rec)
            else:
                excluded.append((rid, reason))
        ids = tuple(r.record_id for r in taken)
        use = Use(tenant_id, experiment_id, purpose, access, recipient, ids,
                  tuple(sorted(consents)), at)  # fmt: skip
        self._uses.append(use)
        return Selection(tuple(taken), tuple(excluded), use.consent_ids, use)

    def uses(self, tenant_id: str) -> tuple[Use, ...]:
        return tuple(u for u in self._uses if u.tenant_id == tenant_id)

    def affected_uses(self, tenant_id: str, at: datetime) -> tuple[AffectedUse, ...]:
        """Earlier uses that relied on consent revoked, or a record deleted, by
        `at`: derived state to invalidate or rebuild (spec §12)."""
        at = ensure_aware_utc(at)
        out = []
        for u in self.uses(tenant_id):
            gone = [self._revoked.get((tenant_id, c)) for c in u.consent_ids]
            dead = [self._deleted.get((tenant_id, r)) for r in u.record_ids]
            if any(t is not None and u.at <= t <= at for t in gone):
                out.append(AffectedUse(u, "consent_revoked"))
            elif any(s is not None and u.at <= s.deleted_at <= at for s in dead):
                out.append(AffectedUse(u, "record_deleted"))
        return tuple(out)


def _consent(
    rec: AnyRecord, grants: list[ConsentGrant]
) -> tuple[str | None, list[str]]:
    """Only the feedback's own principal can consent for it. Error, outcome and case
    records have no principal owner yet, so they are never shared (deny, S3)."""
    if not isinstance(rec, Feedback):
        return "owner_unknown", []
    mine = [g for g in grants if g.principal_id == rec.principal_id]
    kind = [g for g in mine if rec.KIND in g.data_categories]
    cover = [g.consent_id for g in kind
             if g.covers_prior or rec.recorded_at >= g.granted_at]  # fmt: skip
    if cover:
        return None, cover
    why = "recorded_before_consent" if kind else "category_not_consented"
    return (why if mine else "no_consent"), []


def _holdout(rec: AnyRecord, access: Access, already_read: bool) -> str | None:
    final = isinstance(rec, EvaluationCase) and rec.split is Split.FINAL_HOLDOUT
    if not final:
        return "not_final_holdout" if access is Access.FINAL_ASSESSMENT else None
    if access is Access.DEVELOPMENT:
        return "final_holdout"
    return "holdout_reused" if already_read else None
