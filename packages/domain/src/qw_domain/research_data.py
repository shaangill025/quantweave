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
- Wire forms (for the later durable store): strict and canonical for every record;
  `ResearchDataset.from_wire` re-derives the content hash and refuses a mismatch. The
  hash covers every fact field (owner decision 76), so a fact cannot move between
  instruments, periods or filings without changing it, and a dataset only holds
  facts whose wire form round-trips.
LIMITATIONS: no price/bar or corporate-action records yet (`price_mode` is declared
only); no persistence; the research-rights terms are not yet on the dataset wire
(decision 77). Stdlib only.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.decimals import safe_repr
from qw_domain.filings import (
    AsOfView,
    Fact,
    FactBook,
    FilingsError,
    KnowledgeBasis,
    known_at,
)
from qw_domain.identity import InstrumentId
from qw_domain.instants import (
    InstantError,
    ensure_aware_utc,
    format_instant,
    parse_instant,
    require_date,
)
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


def canonical(body: object) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(body: object) -> str:
    return hashlib.sha256(canonical(body).encode()).hexdigest()


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

    @classmethod
    def from_wire(cls, w: object) -> "Window":
        """Two canonical UTC `Z` instants; anything else is refused."""
        if type(w) is not list or len(w) != 2 or not all(type(x) is str for x in w):
            raise ResearchError("window_wire", "two instant strings")
        try:
            out = cls(parse_instant(w[0]), parse_instant(w[1]))
        except InstantError as exc:
            raise ResearchError("window_wire", type(exc).__name__) from None
        if out.to_wire() != w:
            raise ResearchError("window_wire", "not the canonical wire form")
        return out


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

    @classmethod
    def from_wire(cls, w: object) -> "MembershipEvent":
        """Five canonical strings: instrument, change, date, UTC `Z` instant, feed."""
        if type(w) is not list or len(w) != 5 or not all(type(x) is str for x in w):
            raise ResearchError("event_wire", "five strings")
        try:
            e = cls(InstrumentId.from_wire(w[0]), Change(w[1]),
                    date.fromisoformat(w[2]), parse_instant(w[3]), w[4])  # fmt: skip
        except ResearchError:
            raise
        except ValueError as exc:  # IdentityError, InstantError and the enum's
            raise ResearchError("event_wire", type(exc).__name__) from None
        if e.to_wire() != w:
            raise ResearchError("event_wire", "not the canonical wire form")
        return e


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

    def to_wire(self) -> dict[str, object]:
        return {"universe_id": self.universe_id,
                "events": [e.to_wire() for e in self.events]}  # fmt: skip

    @classmethod
    def from_wire(cls, w: object) -> "Universe":
        if type(w) is not dict or set(w) != {"universe_id", "events"} or not (
            type(w["universe_id"]) is str and type(w["events"]) is list
        ):  # fmt: skip
            raise ResearchError("universe_wire", "an id and a list of events")
        events = tuple(MembershipEvent.from_wire(e) for e in w["events"])
        return cls(w["universe_id"], events)


def _storable(f: Fact) -> None:
    """A dataset fact must survive its wire form unchanged (the store keeps it)."""
    try:
        same = Fact.from_wire(f.to_wire()) == f
    except FilingsError as exc:
        raise ResearchError("fact_wire", exc.code) from None
    if not same:
        raise ResearchError("fact_wire", "does not round-trip")


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

    def to_wire(self) -> dict[str, object]:
        """The terms and the content hash. The hash covers the records, which are
        not here, so a read cannot re-derive it: a store binds the wire itself."""
        return {**self.body(), "content_hash": self.content_hash}

    @classmethod
    def from_wire(cls, w: object) -> "DatasetManifest":
        keys = {"dataset_id", "tenant_id", "sources", "evidence_class", "basis",
                "price_mode", "observed", "as_of", "frozen_at",
                "content_hash"}  # fmt: skip
        if type(w) is not dict or set(w) != keys or type(w["sources"]) is not dict:
            raise ResearchError("manifest_wire", "an object with the manifest keys")
        if not all(type(w[k]) is str for k in keys - {"sources", "observed"}) or not (
            all(type(v) is str for v in w["sources"].values())
        ):  # fmt: skip
            raise ResearchError("manifest_wire", "string fields are required")
        try:
            m = cls(w["dataset_id"], w["tenant_id"], w["sources"],
                    EvidenceClass(w["evidence_class"]), KnowledgeBasis(w["basis"]),
                    PriceMode(w["price_mode"]), Window.from_wire(w["observed"]),
                    parse_instant(w["as_of"]), parse_instant(w["frozen_at"]),
                    w["content_hash"])  # fmt: skip
        except ResearchError:
            raise
        except ValueError as exc:  # the enums' and InstantError
            raise ResearchError("manifest_wire", type(exc).__name__) from None
        if m.to_wire() != w:
            raise ResearchError("manifest_wire", "not the canonical wire form")
        return m


@dataclass(frozen=True, slots=True)
class ResearchDataset:
    manifest: DatasetManifest
    facts: FactBook
    universe: Universe

    def __post_init__(self) -> None:
        m = self.manifest
        if (type(m), type(self.facts), type(self.universe)) != (
            DatasetManifest, FactBook, Universe
        ):  # fmt: skip
            raise ResearchError("dataset", "a manifest, a FactBook and a Universe")
        feeds = {f.feed_id for f in self.facts.facts}
        feeds |= {e.feed_id for e in self.universe.events}
        if undeclared := feeds - m.sources.keys():  # every feed is rights-checked
            raise ResearchError("undeclared_source", safe_repr(sorted(undeclared)))
        for f in self.facts.facts:
            _storable(f)
        # One stored form per dataset (decision 74 compares bytes): facts in canonical
        # JSON order of their wire, events in wire order. Order carries no meaning.
        facts = sorted(self.facts.facts, key=lambda f: canonical(f.to_wire()))
        events = sorted(self.universe.events, key=lambda e: e.to_wire())
        object.__setattr__(self, "facts", FactBook(tuple(facts)))
        u = Universe(self.universe.universe_id, tuple(events))
        object.__setattr__(self, "universe", u)
        if m.content_hash != _content_hash(m, self.facts, self.universe):
            raise ResearchError("integrity", "content differs from its manifest hash")

    def to_wire(self) -> dict[str, object]:
        return {"manifest": self.manifest.to_wire(), "facts": self.facts.to_wire(),
                "universe": self.universe.to_wire()}  # fmt: skip

    @classmethod
    def from_wire(cls, w: object) -> "ResearchDataset":
        """Canonical parts in canonical order (an unsorted wire is refused); the
        content hash is re-derived on construction and a mismatch is refused
        (`integrity`)."""
        if type(w) is not dict or set(w) != {"manifest", "facts", "universe"}:
            raise ResearchError("dataset_wire", "manifest, facts and universe")
        try:
            facts = FactBook.from_wire(w["facts"])
        except FilingsError as exc:
            raise ResearchError("dataset_wire", exc.code) from None
        m = DatasetManifest.from_wire(w["manifest"])
        ds = cls(m, facts, Universe.from_wire(w["universe"]))
        if ds.to_wire() != w:
            raise ResearchError("dataset_wire", "not the canonical wire form")
        return ds

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
    """Owner decision 76: every fact field, through the complete fact wire."""
    return digest({
        "manifest": m.body(), "universe_id": universe.universe_id,
        "facts": sorted((f.to_wire() for f in facts.facts), key=canonical),
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
