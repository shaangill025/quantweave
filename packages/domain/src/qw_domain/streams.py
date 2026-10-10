"""Prioritized live stream planning under actual entitlements (T022; R014, R054).

Spec §6 "Free monitoring workload": the tracked universe, the scheduled-discovery
universe and the active streamed set differ; owned/risk-sensitive positions and
user-prioritized candidates get explicit slots; a provider cap (e.g. 30 stock
streams) is exposed, never hidden behind rotation that masquerades as continuity.
- Demands are served in a fixed order: class, then rank, then instrument id, so the
  plan is deterministic and independent of input order.
- A feed can stream only if the T021 gate allows ingestion (retention and derived
  data, `ingest.require_ingest`) at the plan time, the instrument is in its entitled
  set, its latency class satisfies the demand and a slot is left. A demand reuses an
  instrument's existing subscription when it suffices; otherwise it takes a slot on
  the weakest sufficient feed (ties by feed id), keeping stronger slots for demands
  that need them. If every sufficient feed is full, a breadth-first search moves
  already-served subscriptions to other sufficient feeds (an augmenting path) to
  free a slot; a served demand is never dropped or moved to a feed that does not
  meet its need.
- Every demand is either served or reported uncovered with each feed's reason; the
  uncovered instruments are the scheduled-discovery set.
- `discovery_jobs` turns the uncovered set into scheduled-research job requests for
  T025 (`adapters.jobs.enqueue`): sorted batches within a per-job quota, an
  idempotency key over (calendar, session date, batch) so re-planning the same day
  does not duplicate work, and the session close as deadline.
LIMITATION: the stream cap, entitled instruments and latency class are a
caller-supplied trust boundary (`StreamFeed`): T021 records no quotas yet.
Stdlib only.
"""

import hashlib
import json
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from qw_domain.calendars import Session
from qw_domain.decimals import safe_repr
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestDenied, IngestRights, require_ingest
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import Priority
from qw_domain.rights import DenyReason, Registry, UseScope
from qw_domain.sources import ID_PATTERN


class StreamError(ValueError):
    pass


class FeedLatency(StrEnum):
    """Latency class, best first (a subset of observation `quality`)."""

    REALTIME_CONSOLIDATED = "realtime_consolidated"
    REALTIME_EXCHANGE_LIMITED = "realtime_exchange_limited"  # e.g. one venue
    DELAYED = "delayed"
    END_OF_DAY = "end_of_day"

    @property
    def rank(self) -> int:
        return list(FeedLatency).index(self)

    def satisfies(self, needed: "FeedLatency") -> bool:
        return self.rank <= needed.rank

    @property
    def real_time(self) -> bool:
        return self.rank <= FeedLatency.REALTIME_EXCHANGE_LIMITED.rank


class DemandClass(StrEnum):
    """Slot priority, highest first."""

    RISK_SENSITIVE = "risk_sensitive_position"
    OWNED = "owned_position"
    USER_PRIORITY = "user_prioritized"
    CANDIDATE = "candidate"
    DISCOVERY = "discovery"


def _id(value: object, what: str) -> None:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise StreamError(f"{what} {safe_repr(value)} is malformed")


def _count(value: object, what: str) -> None:
    if type(value) is not int or value < 0:
        raise StreamError(f"{what} must be a non-negative int")


@dataclass(frozen=True, slots=True)
class WatchDemand:
    instrument_id: InstrumentId
    kind: DemandClass
    rank: int  # within the class, lower first
    reason: str  # why the instrument is watched
    needs: FeedLatency

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not InstrumentId:
            raise StreamError("instrument_id must be an InstrumentId")
        if not isinstance(self.kind, DemandClass):
            raise StreamError("kind must be a DemandClass")
        if not isinstance(self.needs, FeedLatency):
            raise StreamError("needs must be a FeedLatency")
        _count(self.rank, "rank")
        _id(self.reason, "reason")

    @property
    def order(self) -> tuple[int, int, str, int, str]:
        kinds = list(DemandClass)
        key = self.instrument_id.to_wire()
        return kinds.index(self.kind), self.rank, key, self.needs.rank, self.reason


@dataclass(frozen=True, slots=True)
class StreamFeed:
    feed_id: str
    latency: FeedLatency
    max_symbols: int  # the entitlement's actual concurrent stream cap
    instruments: frozenset[InstrumentId]  # instruments the entitlement covers

    def __post_init__(self) -> None:
        _id(self.feed_id, "feed")
        _count(self.max_symbols, "max_symbols")
        if not isinstance(self.latency, FeedLatency):
            raise StreamError("latency must be a FeedLatency")
        if type(self.instruments) is not frozenset:
            raise StreamError("instruments must be a frozenset")


@dataclass(frozen=True, slots=True)
class StreamRights:
    """Tenant terms under which streamed content is ingested."""

    registry: Registry
    tenant_id: str
    scope: UseScope
    jurisdiction: str
    retain_for: timedelta

    def for_feed(self, feed_id: str, at: datetime) -> IngestRights:
        reg, t, s, j = self.registry, self.tenant_id, self.scope, self.jurisdiction
        return IngestRights(reg, t, feed_id, at, s, j, self.retain_for)


class UncoveredReason(StrEnum):
    NO_FEED = "no_stream_feed"
    RIGHTS_DENIED = "rights_denied"
    NOT_ENTITLED = "instrument_not_entitled"
    LATENCY = "latency_insufficient"
    CAPACITY = "capacity_exhausted"


@dataclass(frozen=True, slots=True)
class Subscription:
    feed_id: str
    instrument_id: InstrumentId
    latency: FeedLatency
    demands: tuple[WatchDemand, ...]  # in service order


@dataclass(frozen=True, slots=True)
class Uncovered:
    demand: WatchDemand
    reasons: tuple[tuple[str, str], ...]  # (feed id, UncoveredReason value)


@dataclass(frozen=True, slots=True)
class StreamPlan:
    at: datetime
    subscriptions: tuple[Subscription, ...]  # by feed id, then instrument id
    uncovered: tuple[Uncovered, ...]  # in service order
    denied_feeds: Mapping[str, tuple[DenyReason, ...]]  # every T021 deny reason
    feed_hashes: Mapping[str, str]  # feed revision hash of each allowed feed

    @property
    def slots_used(self) -> dict[str, int]:
        used: dict[str, int] = {}
        for s in self.subscriptions:
            used[s.feed_id] = used.get(s.feed_id, 0) + 1
        return used

    @property
    def scheduled_discovery(self) -> tuple[InstrumentId, ...]:
        """Instruments with an uncovered demand: covered by scheduled discovery only."""
        return tuple(sorted({u.demand.instrument_id for u in self.uncovered}))


def plan_streams(
    demands: Iterable[WatchDemand],
    feeds: Iterable[StreamFeed],
    rights: StreamRights,
    at: datetime,
) -> StreamPlan:
    at = ensure_aware_utc(at)
    # Weakest latency class first, so a demand takes the least capable slot it can.
    ordered_feeds = sorted(feeds, key=lambda f: (-f.latency.rank, f.feed_id))
    if len({f.feed_id for f in ordered_feeds}) != len(ordered_feeds):
        raise StreamError("feed ids must be unique")
    denied: dict[str, tuple[DenyReason, ...]] = {}
    hashes: dict[str, str] = {}
    for f in ordered_feeds:
        try:
            hashes[f.feed_id] = require_ingest(rights.for_feed(f.feed_id, at))
        except IngestDenied as exc:
            denied[f.feed_id] = exc.reasons
    slots: dict[tuple[str, InstrumentId], list[WatchDemand]] = {}
    used = dict.fromkeys(hashes, 0)
    uncovered: list[Uncovered] = []

    def fits(f: StreamFeed, d: WatchDemand) -> bool:
        ok = f.feed_id not in denied and d.instrument_id in f.instruments
        return ok and f.latency.satisfies(d.needs)

    def move(src: str, inst: InstrumentId, dst: str) -> None:
        group = slots.pop((src, inst))
        used[src] -= 1
        if (dst, inst) not in slots:
            used[dst] += 1
        merged = slots.get((dst, inst), []) + group
        slots[(dst, inst)] = sorted(merged, key=lambda d: d.order)

    def augment(d: WatchDemand) -> str | None:
        """Breadth-first search for a chain of subscription moves, each to a feed
        that serves every demand of the moved subscription, ending in a free slot
        or an existing subscription; frees a slot for `d`. Nothing is dropped."""
        parent: dict[str, tuple[str, InstrumentId] | None] = {
            f.feed_id: None for f in ordered_feeds if fits(f, d)
        }
        queue = deque(parent)
        while queue:
            fid = queue.popleft()
            for src, inst in sorted(k for k in slots if k[0] == fid):
                for g in ordered_feeds:
                    gid = g.feed_id
                    if gid == fid or not all(fits(g, x) for x in slots[(src, inst)]):
                        continue
                    if (gid, inst) not in slots and used[gid] >= g.max_symbols:
                        if gid not in parent:
                            parent[gid] = (fid, inst)
                            queue.append(gid)
                        continue
                    hop: tuple[str, InstrumentId] | None = (fid, inst)
                    while hop is not None:  # unwind: each move fills the freed slot
                        move(hop[0], hop[1], gid)
                        gid, hop = hop[0], parent[hop[0]]
                    return gid
        return None

    for d in sorted(demands, key=lambda d: d.order):
        reasons: list[tuple[str, str]] = []
        chosen: str | None = None
        for f in ordered_feeds:
            why: UncoveredReason | None = None
            if f.feed_id in denied:
                why = UncoveredReason.RIGHTS_DENIED
            elif d.instrument_id not in f.instruments:
                why = UncoveredReason.NOT_ENTITLED
            elif not f.latency.satisfies(d.needs):
                why = UncoveredReason.LATENCY
            elif (f.feed_id, d.instrument_id) in slots:
                chosen = f.feed_id  # already streamed: no new slot
                break
            elif used[f.feed_id] >= f.max_symbols:
                why = UncoveredReason.CAPACITY
            elif chosen is None:
                chosen = f.feed_id
                continue  # keep looking for an existing sufficient subscription
            if why is not None:
                reasons.append((f.feed_id, why.value))
        if chosen is None:
            chosen = augment(d)
        if chosen is None:
            if not ordered_feeds:
                reasons.append(("", UncoveredReason.NO_FEED.value))
            uncovered.append(Uncovered(d, tuple(sorted(reasons))))
            continue
        key = (chosen, d.instrument_id)
        if key not in slots:
            used[chosen] += 1
            slots[key] = []
        slots[key].append(d)
    latency = {f.feed_id: f.latency for f in ordered_feeds}
    subs = tuple(
        Subscription(fid, inst, latency[fid], tuple(ds))
        for (fid, inst), ds in sorted(slots.items())
    )
    return StreamPlan(
        at, subs, tuple(uncovered), MappingProxyType(denied), MappingProxyType(hashes)
    )


@dataclass(frozen=True, slots=True)
class DiscoveryJob:
    """A request for `adapters.jobs.enqueue` (kind, input_revision, priority,
    payload, deadline_at)."""

    kind: str
    input_revision: str
    priority: Priority
    payload: dict[str, str | list[str]]
    deadline_at: datetime


def discovery_jobs(
    plan: StreamPlan, session: Session, batch_size: int
) -> tuple[DiscoveryJob, ...]:
    """Scheduled-discovery jobs for the instruments the plan could not stream."""
    if type(batch_size) is not int or batch_size < 1:
        raise StreamError("batch_size must be a positive int")
    wire = [i.to_wire() for i in plan.scheduled_discovery]
    jobs = []
    for n in range(0, len(wire), batch_size):
        payload: dict[str, str | list[str]] = {
            "calendar_id": session.calendar_id.code,
            "session_date": session.session_date.isoformat(),
            "instruments": wire[n : n + batch_size],
            "reason": "not_streamed",
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(text.encode()).hexdigest()
        kind, prio = "scheduled_discovery", Priority.SCHEDULED_RESEARCH
        jobs.append(DiscoveryJob(kind, key, prio, payload, session.close_at))
    return tuple(jobs)
