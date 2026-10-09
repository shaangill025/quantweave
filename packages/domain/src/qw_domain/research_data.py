"""Research datasets: rights-checked frozen manifests, point-in-time universe
membership and point-in-time facts (T032 increment 1). Spec §14 "Research datasets"
("Today's survivor list is not a historical universe"), §17, R040, R084.
- Research use is checked through T021 `check_use` for every declared feed as
  retention plus derived data (T021 has no separate research use); any unknown or
  denied right refuses the dataset with every reason.
- A dataset is frozen at `as_of`: later-known records are left out, an undeclared
  feed refuses it, and the manifest hash covers its terms and every record (re-derived
  on construction). Queries past `as_of` are refused, not truncated.
- Membership: per instrument alternating add/remove events with an effective date and
  a knowledge time. Members on a date are those whose latest known event on or before
  it is an add: delisted names stay in earlier universes, nobody is a member before
  listing, and a change is invisible before its knowledge time.
- Facts are read with T023 `FactBook.as_of`, so restatements stay invisible before
  their knowledge time.
LIMITATIONS: no price/bar or corporate-action records yet (`price_mode` is declared
only); no persistence. Stdlib only.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.filings import AsOfView, Fact, FactBook, KnowledgeBasis, known_at
from qw_domain.identity import InstrumentId
from qw_domain.instants import ensure_aware_utc, format_instant, require_date
from qw_domain.rights import DenyReason, Registry, Use, UseScope, check_use
from qw_domain.sources import ID_PATTERN

RESEARCH_USES = (Use.RETENTION, Use.DERIVED_DATA)


class ResearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class DatasetRefused(PermissionError):
    def __init__(self, reasons: tuple[tuple[str, DenyReason], ...]) -> None:
        text = ", ".join(f"{f}:{r.code.value}@{r.subject}" for f, r in reasons)
        super().__init__(f"research use refused: {text}")
        self.reasons = reasons


def check_id(value: object, what: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ResearchError(what, f"{safe_repr(value)} is malformed")
    return value


def digest(body: object) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


class EvidenceClass(StrEnum):  # R040: never interchangeable
    SYNTHETIC = "synthetic"
    HISTORICAL = "historical"
    PROSPECTIVE = "prospective"


class PriceMode(StrEnum):  # §14: tradable vs adjusted research prices
    RAW_TRADABLE = "raw_tradable"
    ADJUSTED_RESEARCH = "adjusted_research"


@dataclass(frozen=True, slots=True)
class ResearchRights:
    """Who uses which feeds for research, when (server time) and under which terms."""

    registry: Registry
    tenant_id: str
    at: datetime
    scope: UseScope
    jurisdiction: str
    retain_for: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_aware_utc(self.at))


def require_research_use(
    rights: ResearchRights, feeds: Iterable[str]
) -> dict[str, str]:
    """Feed id -> feed revision hash, or `DatasetRefused` with every reason."""
    if type(rights) is not ResearchRights:
        raise TypeError("research use needs ResearchRights")
    reasons: list[tuple[str, DenyReason]] = []
    hashes: dict[str, str] = {}
    for feed in sorted(set(feeds)):
        for use in RESEARCH_USES:
            keep = rights.retain_for if use is Use.RETENTION else None
            d = check_use(rights.registry, rights.tenant_id, feed, use, rights.at,
                          scope=rights.scope, jurisdiction=rights.jurisdiction,
                          retain_for=keep)  # fmt: skip
            reasons.extend((feed, r) for r in d.reasons)
            if d.feed_hash is not None:
                hashes[feed] = d.feed_hash
    if reasons or not hashes:
        raise DatasetRefused(tuple(reasons))
    return hashes


@dataclass(frozen=True, slots=True)
class Window:
    """Half-open [start, end) of aware instants."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_aware_utc(self.start))
        object.__setattr__(self, "end", ensure_aware_utc(self.end))
        if self.end <= self.start:
            raise ResearchError("window", "end must follow start")

    def contains(self, t: datetime) -> bool:
        return self.start <= t < self.end

    def covers(self, other: "Window") -> bool:
        return self.start <= other.start and other.end <= self.end

    def to_wire(self) -> list[str]:
        return [format_instant(self.start), format_instant(self.end)]


class Change(StrEnum):
    ADD = "add"  # listed, or added to the research universe
    REMOVE = "remove"  # delisted, or removed


@dataclass(frozen=True, slots=True)
class MembershipEvent:
    instrument_id: InstrumentId
    change: Change
    effective: date
    known_at: datetime
    feed_id: str

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not InstrumentId:
            raise ResearchError("instrument", safe_repr(self.instrument_id))
        if not isinstance(self.change, Change):
            raise ResearchError("change", safe_repr(self.change))
        require_date(self.effective, "effective")
        object.__setattr__(self, "known_at", ensure_aware_utc(self.known_at))
        check_id(self.feed_id, "feed")

    def to_wire(self) -> list[str]:
        e, when = self, format_instant(self.known_at)
        return [e.instrument_id.to_wire(), e.change.value, e.effective.isoformat(),
                when, e.feed_id]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Universe:
    universe_id: str
    events: tuple[MembershipEvent, ...]

    def __post_init__(self) -> None:
        check_id(self.universe_id, "universe")
        object.__setattr__(self, "events", tuple(self.events))
        per: dict[InstrumentId, list[MembershipEvent]] = {}
        for e in self.events:
            if type(e) is not MembershipEvent:
                raise ResearchError("event", "MembershipEvent records only")
            per.setdefault(e.instrument_id, []).append(e)
        for iid, evs in per.items():
            evs.sort(key=lambda e: e.effective)
            days = [e.effective for e in evs]
            alternates = all(e.change is (Change.ADD, Change.REMOVE)[i % 2]
                             for i, e in enumerate(evs))  # fmt: skip
            if len(set(days)) != len(days) or not alternates:
                raise ResearchError("membership_sequence", iid.to_wire())

    def members(self, on: date, known_by: datetime) -> frozenset[InstrumentId]:
        require_date(on, "on")
        known_by = ensure_aware_utc(known_by)
        latest: dict[InstrumentId, MembershipEvent] = {}
        for e in self.events:
            if e.effective <= on and e.known_at <= known_by:
                cur = latest.get(e.instrument_id)
                if cur is None or e.effective > cur.effective:
                    latest[e.instrument_id] = e
        return frozenset(i for i, e in latest.items() if e.change is Change.ADD)


def _fact_wire(f: Fact) -> list[str]:
    start = "" if f.period.start is None else f.period.start.isoformat()
    return [f.cik, f.taxonomy, f.concept, f.unit.raw, start, f.period.end.isoformat(),
            f.accession, str(f.value), format_instant(f.published_at),
            format_instant(f.received_at), f.feed_id, f.feed_hash]  # fmt: skip


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    dataset_id: str
    tenant_id: str
    sources: Mapping[str, str]  # feed id -> feed revision hash at freeze
    evidence_class: EvidenceClass
    basis: KnowledgeBasis
    price_mode: PriceMode
    observed: Window  # declared observation range; records are not filtered to it
    as_of: datetime  # knowledge cutoff: nothing known later is in the dataset
    frozen_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        check_id(self.dataset_id, "dataset")
        check_id(self.tenant_id, "tenant")
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        if not (
            isinstance(self.evidence_class, EvidenceClass)
            and isinstance(self.basis, KnowledgeBasis)
            and isinstance(self.price_mode, PriceMode)
            and type(self.observed) is Window
        ):
            raise ResearchError("terms", "typed evidence class, basis, prices, range")
        for name in ("as_of", "frozen_at"):
            object.__setattr__(self, name, ensure_aware_utc(getattr(self, name)))

    def body(self) -> dict[str, object]:
        m = self
        return {"dataset_id": m.dataset_id, "tenant_id": m.tenant_id,
                "sources": dict(sorted(m.sources.items())),
                "evidence_class": m.evidence_class.value, "basis": m.basis.value,
                "price_mode": m.price_mode.value, "observed": m.observed.to_wire(),
                "as_of": format_instant(m.as_of),
                "frozen_at": format_instant(m.frozen_at)}  # fmt: skip


@dataclass(frozen=True, slots=True)
class ResearchDataset:
    manifest: DatasetManifest
    facts: FactBook
    universe: Universe

    def __post_init__(self) -> None:
        m = self.manifest
        if m.content_hash != _content_hash(m, self.facts, self.universe):
            raise ResearchError("integrity", "content differs from its manifest hash")

    def _check_time(self, t: datetime) -> datetime:
        t = ensure_aware_utc(t)
        if t > self.manifest.as_of:
            raise ResearchError("after_freeze", f"{format_instant(t)} is past as_of")
        return t

    def facts_at(self, t: datetime) -> AsOfView:
        """Facts as known at decision time `t` (latest version known by then)."""
        return self.facts.as_of(self._check_time(t), self.manifest.basis)

    def members(self, on: date, known_by: datetime) -> frozenset[InstrumentId]:
        return self.universe.members(on, self._check_time(known_by))


def _content_hash(m: DatasetManifest, facts: FactBook, universe: Universe) -> str:
    return digest({
        "manifest": m.body(), "universe_id": universe.universe_id,
        "facts": sorted(_fact_wire(f) for f in facts.facts),
        "events": sorted(e.to_wire() for e in universe.events),
    })  # fmt: skip


def freeze_dataset(
    dataset_id: str, rights: ResearchRights, sources: frozenset[str],
    facts: Iterable[Fact], universe: Universe, *, observed: Window, as_of: datetime,
    basis: KnowledgeBasis, price_mode: PriceMode, evidence_class: EvidenceClass,
) -> ResearchDataset:  # fmt: skip
    """Check research rights for every source at `rights.at` and freeze the records
    known at `as_of` into an immutable, hashed dataset."""
    as_of = ensure_aware_utc(as_of)
    if as_of > rights.at or observed.end > as_of:
        raise ResearchError("as_of", "observed.end <= as_of <= freeze time")
    hashes = require_research_use(rights, sources)
    facts = tuple(facts)
    for feed in {f.feed_id for f in facts} | {e.feed_id for e in universe.events}:
        if feed not in hashes:
            raise ResearchError("undeclared_source", safe_repr(feed))
    book = FactBook().add(f for f in facts if known_at(f, basis) <= as_of)
    kept = tuple(e for e in universe.events if e.known_at <= as_of)
    frozen_universe = Universe(universe.universe_id, kept)
    m = DatasetManifest(dataset_id, rights.tenant_id, hashes, evidence_class, basis,
                        price_mode, observed, as_of, rights.at, "")  # fmt: skip
    h = _content_hash(m, book, frozen_universe)
    return ResearchDataset(replace(m, content_hash=h), book, frozen_universe)
