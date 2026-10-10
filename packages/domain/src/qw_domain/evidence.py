"""Evidence store and claim graph (T036 increments 1-2; spec §10, §6, R045/R052/R087).

Sources enter only through the T023 ingest gate and keep feed, hashes, publication,
receipt and retention times and an origin group (normalized-text hash or a declared
`same_origin_as`): copies in one group are one source for corroboration. Groups are
content origin, not publisher independence: two different documents from one
publisher or feed are two groups. Passages are exact spans whose `Reading` value must
appear in the quote as a number token (unit, period and scale words such as "million"
are not read from the text). Atomic `reported_fact` claims have append-only revisions;
a citation must match the current revision's entity, attribute, period, unit and
value or it is refused. `support(at)` counts only citations bound to the revision
current at `at` whose source is then published, undeleted, unexpired, has no
withdrawn copy in its group and keeps `derived_data` rights; nothing leaves history.
Supported claims with one key (entity, attribute, period, unit) and different values
form a contradiction and read `contested` until an append-only resolution, naming a
preferred member, an actor and a time, binds that exact member set (claim, revision);
a new member, a lost member or a revision reopens it. No mutation may be backdated
within a tenant.
LIMITATIONS: in-memory; non-fact claim kinds refused; no T027 binding. Stdlib only.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.filings import Fact, Period
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights, require_ingest
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.rights import Registry, Use, UseScope, check_use
from qw_domain.sources import ID_PATTERN

_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,63}", re.ASCII)
# A number token: plain (100, -12.5) or grouped in threes by one consistent separator
# among comma, space, NBSP, thin space, narrow NBSP or apostrophe (1,000; 12 345.67).
# A token touching another digit group across a separator (1 00, 2025 100) yields
# nothing, so such text cannot support any value.
_SEP = "[, \u00a0\u2009\u202f']"  # comma, space, NBSP, thin, narrow NBSP, '
_NUMBER = re.compile(
    rf"(?<![\w.,])(?<![0-9]{_SEP})[-+]?"
    rf"(?:[0-9]{{1,3}}(?P<sep>{_SEP})[0-9]{{3}}(?:(?P=sep)[0-9]{{3}})*|[0-9]+)"
    rf"(?:\.[0-9]+)?(?![\w]|[.,][0-9]|{_SEP}[0-9])",
    re.ASCII,
)


def _numbers(text: str) -> set[Decimal]:
    found = (re.sub(_SEP, "", m.group()) for m in _NUMBER.finditer(text))
    return {Decimal(t) for t in found}


class EvidenceError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise EvidenceError(what, f"{safe_repr(value)} is malformed")
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class SourceKind(StrEnum):
    PRIMARY = "primary"
    INDEPENDENT = "independent"
    SOCIAL = "social"  # a research lead only (R045)


class ClaimKind(StrEnum):
    REPORTED_FACT = "reported_fact"
    CALCULATION = "calculation"
    ASSUMPTION = "assumption"
    FORECAST = "forecast"
    INTERPRETATION = "interpretation"


class Status(StrEnum):  # subset of claim.schema.json `verification`
    SUPPORTED = "supported"
    CONTESTED = "contested"
    UNSUPPORTED = "unsupported"


class Invalid(StrEnum):
    REVISION_SUPERSEDED = "revision_superseded"
    WITHDRAWN = "withdrawn"
    DELETED = "deleted"
    RETENTION_EXPIRED = "retention_expired"
    RIGHTS_REVOKED = "rights_revoked"
    NOT_YET_PUBLISHED = "not_yet_published"


@dataclass(frozen=True, slots=True)
class Reading:
    """What a passage states, or what a claim asserts."""

    entity: InstrumentId
    attribute: str
    period: Period
    unit: str
    value: Decimal

    def __post_init__(self) -> None:
        if type(self.entity) is not InstrumentId or type(self.period) is not Period:
            raise TypeError("a reading needs an InstrumentId and a Period")
        if type(self.value) is not Decimal or not self.value.is_finite():
            raise TypeError("a reading value must be a finite Decimal")
        _id(self.attribute, "attribute")
        if not isinstance(self.unit, str) or _UNIT.fullmatch(self.unit) is None:
            raise EvidenceError("unit", safe_repr(self.unit))


@dataclass(frozen=True, slots=True)
class Source:
    tenant_id: str
    source_id: str
    kind: SourceKind
    feed_id: str
    feed_hash: str
    content_hash: str
    origin: str
    published_at: datetime
    received_at: datetime
    retain_until: datetime
    scope: UseScope
    jurisdiction: str


@dataclass(frozen=True, slots=True)
class Passage:
    tenant_id: str
    passage_id: str
    source_id: str
    start: int
    end: int
    text_hash: str
    reading: Reading
    known_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimRevision:
    tenant_id: str
    claim_id: str
    revision: int
    kind: ClaimKind
    stated: Reading
    known_at: datetime


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    tenant_id: str
    claim_id: str
    revision: int
    passage_id: str
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class Support:
    claim_id: str
    revision: int
    status: Status
    valid: tuple[str, ...]
    invalid: Mapping[str, Invalid]
    independent_sources: int  # distinct origin groups among valid citations
    corroborated: bool  # at least two independent origin groups
    replay_limited: bool  # cited content was deleted or purged


type ClaimKey = tuple[InstrumentId, str, Period, str]  # entity, attribute, period, unit
type Members = frozenset[tuple[str, int]]  # (claim id, revision)


class ResolutionState(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"


@dataclass(frozen=True, slots=True)
class Resolution:
    tenant_id: str
    members: Members
    preferred: str
    actor_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Contradiction:
    key: ClaimKey
    members: Members
    values: frozenset[Decimal]
    state: ResolutionState
    resolution: Resolution | None


class EvidenceStore:
    """Append-only, tenant-scoped; every lookup is keyed by (tenant, id)."""

    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], Source] = {}
        self._content: dict[tuple[str, str], str] = {}
        self._passages: dict[tuple[str, str], Passage] = {}
        self._claims: dict[tuple[str, str], list[ClaimRevision]] = {}
        self._citations: dict[tuple[str, str], dict[str, Citation]] = {}
        self._withdrawn: dict[tuple[str, str], datetime] = {}  # (tenant, origin)
        self._deleted: dict[tuple[str, str], datetime] = {}
        self._resolutions: dict[str, list[Resolution]] = {}
        self._clock: dict[str, datetime] = {}

    def _time(self, tenant_id: str, at: datetime) -> datetime:
        at = ensure_aware_utc(at)
        last = self._clock.get(_id(tenant_id, "tenant"))
        if last is not None and at < last:
            raise EvidenceError("time_order", f"{format_instant(at)} is backdated")
        return at

    def _get[V](self, table: dict[tuple[str, str], V], tenant_id: str, key: str) -> V:
        found = table.get((tenant_id, key))
        if found is None:
            raise EvidenceError("not_found", f"{safe_repr(key)} in this tenant")
        return found

    def add_source(
        self,
        rights: IngestRights,
        source_id: str,
        kind: SourceKind,
        content: str,
        published_at: datetime,
        same_origin_as: str | None = None,
    ) -> Source:
        if type(rights) is not IngestRights:
            raise TypeError("a source needs IngestRights")
        tenant = rights.tenant_id
        published_at = ensure_aware_utc(published_at)
        old = self._sources.get((tenant, _id(source_id, "source")))
        if old is not None:
            same = (rights.feed_id, kind, published_at) == (
                old.feed_id,
                old.kind,
                old.published_at,
            )
            if same and isinstance(content, str) and _sha(content) == old.content_hash:
                return old  # re-ingest: keeps the first receipt time
            raise EvidenceError("exists", source_id)
        at = self._time(tenant, rights.received_at)
        if not isinstance(kind, SourceKind) or not isinstance(content, str):
            raise TypeError("a source needs a SourceKind and text content")
        if not content.strip():
            raise EvidenceError("content", "empty")
        if same_origin_as is None:
            origin = _sha(" ".join(content.casefold().split()))
        else:
            origin = self._get(self._sources, tenant, same_origin_as).origin
        feed_hash = require_ingest(rights)
        src = Source(
            tenant,
            source_id,
            kind,
            rights.feed_id,
            feed_hash,
            _sha(content),
            origin,
            published_at,
            at,
            at + rights.retain_for,
            rights.scope,
            rights.jurisdiction,
        )
        self._sources[(tenant, source_id)] = src
        self._content[(tenant, source_id)] = content
        self._clock[tenant] = at
        return src

    def add_passage(
        self,
        tenant_id: str,
        passage_id: str,
        source_id: str,
        start: int,
        quote: str,
        reading: Reading,
        at: datetime,
    ) -> Passage:
        """The quote must state the reading's value as a number token."""
        at = self._time(tenant_id, at)
        self._get(self._sources, tenant_id, source_id)
        if (tenant_id, _id(passage_id, "passage")) in self._passages:
            raise EvidenceError("exists", passage_id)
        content = self._content.get((tenant_id, source_id))
        if content is None:
            raise EvidenceError("evidence_unavailable", source_id)
        if (
            type(start) is not int
            or start < 0
            or not isinstance(quote, str)
            or not quote
        ):
            raise EvidenceError("span", "a non-negative offset and non-empty quote")
        end = start + len(quote)
        if content[start:end] != quote:
            raise EvidenceError("span_mismatch", "quote is not the text at the offset")
        if type(reading) is not Reading:
            raise TypeError("a passage needs a Reading")
        if reading.value not in _numbers(quote):
            raise EvidenceError("reading_not_in_quote", str(reading.value))
        p = Passage(
            tenant_id, passage_id, source_id, start, end, _sha(quote), reading, at
        )
        self._passages[(tenant_id, passage_id)] = p
        self._clock[tenant_id] = at
        return p

    def add_fact(self, rights: IngestRights, fact: Fact) -> Passage:
        """A T023 reported fact as a primary source with one whole-record passage.
        `rights` must be the fact's own feed and receipt time."""
        if type(fact) is not Fact or type(rights) is not IngestRights:
            raise TypeError("add_fact needs IngestRights and a Fact")
        iid = fact.instrument.instrument_id
        if fact.instrument.status != "found" or iid is None:
            raise EvidenceError("entity_unresolved", fact.cik)
        if (rights.feed_id, rights.received_at) != (fact.feed_id, fact.received_at):
            raise EvidenceError("fact_rights", "feed or receipt time differs")
        f, start = fact, fact.period.start
        body: list[str | None] = [f.cik, f.taxonomy, f.concept, f.unit.raw]
        body += [str(f.value), f.accession]
        body += [f.form, f.filed.isoformat(), f.period.end.isoformat()]
        body.append(None if start is None else start.isoformat())
        content = json.dumps(body, separators=(",", ":"))
        sid = f"fact-{_sha(json.dumps([fact.feed_id, content]))[:32]}"
        if (old := self._passages.get((rights.tenant_id, f"p-{sid}"))) is not None:
            return old  # the same fact from the same feed: idempotent
        attribute = f"{fact.taxonomy}:{fact.concept}"
        reading = Reading(iid, attribute, fact.period, fact.unit.raw, fact.value)
        self.add_source(rights, sid, SourceKind.PRIMARY, content, fact.published_at)
        pid, at = f"p-{sid}", rights.received_at  # derived reading: no quote check
        p = Passage(
            rights.tenant_id, pid, sid, 0, len(content), _sha(content), reading, at
        )
        self._passages[(rights.tenant_id, pid)] = p
        return p

    def state_claim(
        self,
        tenant_id: str,
        claim_id: str,
        kind: ClaimKind,
        stated: Reading,
        known_at: datetime,
    ) -> ClaimRevision:
        """Create a claim or append a revision; an unchanged reading is a no-op."""
        at = self._time(tenant_id, known_at)
        if kind is not ClaimKind.REPORTED_FACT:
            raise EvidenceError("kind_unsupported", f"{safe_repr(kind)}")
        if type(stated) is not Reading:
            raise TypeError("a claim needs a Reading")
        revs = self._claims.setdefault((tenant_id, _id(claim_id, "claim")), [])
        if revs and revs[-1].stated == stated:
            return revs[-1]
        rev = ClaimRevision(tenant_id, claim_id, len(revs) + 1, kind, stated, at)
        revs.append(rev)
        self._clock[tenant_id] = at
        return rev

    def claim_at(
        self, tenant_id: str, claim_id: str, at: datetime
    ) -> ClaimRevision | None:
        at = ensure_aware_utc(at)
        revs = self._get(self._claims, tenant_id, claim_id)
        known = [r for r in revs if r.known_at <= at]
        return known[-1] if known else None

    def _unusable(self, src: Source, at: datetime) -> Invalid | None:
        key = (src.tenant_id, src.source_id)
        if max(src.published_at, src.received_at) > at:
            return Invalid.NOT_YET_PUBLISHED
        if (gone := self._deleted.get(key)) is not None and gone <= at:
            return Invalid.DELETED
        if at >= src.retain_until:
            return Invalid.RETENTION_EXPIRED
        withdrawn = self._withdrawn.get((src.tenant_id, src.origin))
        if withdrawn is not None and withdrawn <= at:
            return Invalid.WITHDRAWN
        return None

    def cite(
        self, tenant_id: str, claim_id: str, passage_id: str, at: datetime
    ) -> Citation:
        at = self._time(tenant_id, at)
        rev = self.claim_at(tenant_id, claim_id, at)
        if rev is None:
            raise EvidenceError("not_known", claim_id)
        p = self._get(self._passages, tenant_id, passage_id)
        src = self._sources[(tenant_id, p.source_id)]
        if src.kind is SourceKind.SOCIAL:
            raise EvidenceError("social_lead", "a social source is not evidence")
        mine, theirs = rev.stated, p.reading
        for name in ("entity", "attribute", "period", "unit", "value"):
            if getattr(mine, name) != getattr(theirs, name):
                raise EvidenceError(f"{name}_mismatch", passage_id)
        if (why := self._unusable(src, at)) is Invalid.NOT_YET_PUBLISHED:
            raise EvidenceError(why.value, format_instant(src.published_at))
        if why is not None:
            raise EvidenceError("evidence_unavailable", why.value)
        body = [tenant_id, claim_id, rev.revision, passage_id]
        cid = f"cit-{_sha(json.dumps(body))[:32]}"
        table = self._citations.setdefault((tenant_id, claim_id), {})
        if cid not in table:
            table[cid] = Citation(
                cid, tenant_id, claim_id, rev.revision, passage_id, at
            )
        self._clock[tenant_id] = at
        return table[cid]

    def withdraw(self, tenant_id: str, source_id: str, at: datetime) -> None:
        """A publisher retraction or correction: withdraws every copy of the content."""
        at = self._time(tenant_id, at)
        src = self._get(self._sources, tenant_id, source_id)
        self._withdrawn.setdefault((tenant_id, src.origin), at)
        self._clock[tenant_id] = at

    def delete(self, tenant_id: str, source_id: str, at: datetime) -> None:
        """Tombstone one copy and drop its content; hashes and history remain."""
        at = self._time(tenant_id, at)
        self._get(self._sources, tenant_id, source_id)
        self._deleted.setdefault((tenant_id, source_id), at)
        self._content.pop((tenant_id, source_id), None)
        self._clock[tenant_id] = at

    def purge_expired(self, tenant_id: str, at: datetime) -> None:
        at = self._time(tenant_id, at)
        for (tenant, sid), src in self._sources.items():
            if tenant == tenant_id and src.retain_until <= at:
                self._content.pop((tenant, sid), None)
        self._clock[tenant_id] = at

    def passage(self, tenant_id: str, passage_id: str) -> Passage:
        return self._get(self._passages, tenant_id, passage_id)

    def source(self, tenant_id: str, source_id: str) -> Source:
        return self._get(self._sources, tenant_id, source_id)

    def unusable(self, src: Source, at: datetime) -> Invalid | None:
        """Why `src` cannot be used as known at `at`, or None (rights excluded)."""
        return self._unusable(src, ensure_aware_utc(at))

    def passage_text(self, tenant_id: str, passage_id: str) -> str | None:
        p = self._get(self._passages, tenant_id, passage_id)
        content = self._content.get((tenant_id, p.source_id))
        return None if content is None else content[p.start : p.end]

    def support(
        self, tenant_id: str, claim_id: str, at: datetime, rights: Registry
    ) -> Support:
        """Support as known at `at`; rights are read as known at `at` (T021)."""
        rev = self.claim_at(tenant_id, claim_id, at)
        if rev is None:
            raise EvidenceError("not_known", claim_id)
        at = ensure_aware_utc(at)
        valid, invalid, groups, limited = self._check(rev, at, rights)
        status = Status.SUPPORTED if valid else Status.UNSUPPORTED
        if valid and self._contested(rev, at, rights):
            status = Status.CONTESTED
        return Support(
            claim_id,
            rev.revision,
            status,
            tuple(sorted(valid)),
            MappingProxyType(invalid),
            len(groups),
            len(groups) >= 2,
            limited,
        )

    def _check(
        self, rev: ClaimRevision, at: datetime, rights: Registry
    ) -> tuple[list[str], dict[str, Invalid], set[str], bool]:
        tenant_id = rev.tenant_id
        valid: list[str] = []
        invalid: dict[str, Invalid] = {}
        groups: set[str] = set()
        limited = False
        for c in self._citations.get((tenant_id, rev.claim_id), {}).values():
            if c.linked_at > at:
                continue
            p = self._passages[(tenant_id, c.passage_id)]
            src = self._sources[(tenant_id, p.source_id)]
            limited |= (tenant_id, src.source_id) not in self._content
            why = self._unusable(src, at)
            if c.revision != rev.revision:
                why = Invalid.REVISION_SUPERSEDED
            if why is None and not self._derived_ok(rights, src, at):
                why = Invalid.RIGHTS_REVOKED
            if why is None:
                valid.append(c.citation_id)
                groups.add(src.origin)
            else:
                invalid[c.citation_id] = why
        return valid, invalid, groups, limited

    @staticmethod
    def _derived_ok(rights: Registry, src: Source, at: datetime) -> bool:
        decision = check_use(
            rights,
            src.tenant_id,
            src.feed_id,
            Use.DERIVED_DATA,
            at,
            scope=src.scope,
            jurisdiction=src.jurisdiction,
        )
        return decision.allowed

    def contradictions(
        self, tenant_id: str, at: datetime, rights: Registry
    ) -> tuple[Contradiction, ...]:
        """Keys whose evidence-supported claims disagree, as known at `at`."""
        at = ensure_aware_utc(at)
        by_key: dict[ClaimKey, list[ClaimRevision]] = {}
        for tenant, cid in self._claims:
            rev = self.claim_at(tenant, cid, at) if tenant == tenant_id else None
            if rev is not None and self._check(rev, at, rights)[0]:
                s = rev.stated
                key = (s.entity, s.attribute, s.period, s.unit)
                by_key.setdefault(key, []).append(rev)
        out = []
        for key, revs in by_key.items():
            values = frozenset(r.stated.value for r in revs)
            if len(values) < 2:
                continue
            members = frozenset((r.claim_id, r.revision) for r in revs)
            done = [
                r
                for r in self._resolutions.get(tenant_id, [])
                if r.members == members and r.at <= at
            ]
            last = done[-1] if done else None
            state = ResolutionState.RESOLVED if last else ResolutionState.UNRESOLVED
            out.append(Contradiction(key, members, values, state, last))
        return tuple(sorted(out, key=lambda c: sorted(c.members)))

    def _contested(self, rev: ClaimRevision, at: datetime, rights: Registry) -> bool:
        for con in self.contradictions(rev.tenant_id, at, rights):
            if (rev.claim_id, rev.revision) not in con.members:
                continue
            if con.resolution is None:
                return True
            best = self.claim_at(rev.tenant_id, con.resolution.preferred, at)
            return best is None or best.stated.value != rev.stated.value
        return False

    def resolve(
        self,
        tenant_id: str,
        members: Members,
        preferred: str,
        actor_id: str,
        at: datetime,
        rights: Registry,
    ) -> Resolution:
        """Record which member's value is preferred for the current member set."""
        at = self._time(tenant_id, at)
        _id(actor_id, "actor")
        current = self.contradictions(tenant_id, at, rights)
        if not any(c.members == members for c in current):
            raise EvidenceError("stale_contradiction", "members are not current")
        if preferred not in {cid for cid, _ in members}:
            raise EvidenceError("preferred", "must be a member claim")
        record = Resolution(tenant_id, members, preferred, actor_id, at)
        self._resolutions.setdefault(tenant_id, []).append(record)
        self._clock[tenant_id] = at
        return record

    def resolutions(self, tenant_id: str) -> tuple[Resolution, ...]:
        return tuple(self._resolutions.get(tenant_id, ()))
