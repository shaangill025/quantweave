"""Prioritized live stream planning under actual entitlements (T022; R014, R054).

SYNTHETIC feeds, caps, tenants and instruments only; no real provider is named. The
expected plans are worked out by hand from the stated priority order.
"""

import random
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from ingest_rights_helper import KEEP, REGION, TENANT, qualified_registry
from qw_domain.identity import InstrumentId
from qw_domain.rights import Registry, Use, UseScope
from qw_domain.streams import (
    DemandClass,
    FeedLatency,
    StreamError,
    StreamFeed,
    StreamPlan,
    StreamRights,
    Uncovered,
    WatchDemand,
    plan_streams,
)

AT = datetime(2026, 10, 8, 13, 0, tzinfo=UTC)
IEX, SIP = "feed-synth-iex", "feed-synth-sip"
EXCH, CONS = FeedLatency.REALTIME_EXCHANGE_LIMITED, FeedLatency.REALTIME_CONSOLIDATED
REG = qualified_registry((IEX, SIP))


def inst(n: int) -> InstrumentId:
    return InstrumentId(UUID(int=n))


def rights(reg: Registry = REG) -> StreamRights:
    return StreamRights(reg, TENANT, UseScope.PERSONAL, REGION, KEEP)


def demand(
    n: int,
    kind: DemandClass = DemandClass.CANDIDATE,
    rank: int = 0,
    needs: FeedLatency = EXCH,
) -> WatchDemand:
    return WatchDemand(inst(n), kind, rank, f"reason-{kind.value}", needs)


def feed(
    fid: str = IEX, cap: int = 30, n: int = 100, lat: FeedLatency = EXCH
) -> StreamFeed:
    return StreamFeed(fid, lat, cap, frozenset(inst(i) for i in range(1, n + 1)))


def planned(plan: StreamPlan) -> set[int]:
    return {s.instrument_id.uuid.int for s in plan.subscriptions}


def test_thirty_symbol_cap_keeps_priority_order_and_reports_the_rest() -> None:
    # 3 risk-sensitive, 2 owned, 5 user-prioritized, 30 candidates = 40 demands.
    demands = [demand(i, DemandClass.RISK_SENSITIVE) for i in (40, 39, 38)]
    demands += [demand(i, DemandClass.OWNED) for i in (37, 36)]
    demands += [demand(i, DemandClass.USER_PRIORITY, rank=i) for i in range(31, 36)]
    demands += [demand(i) for i in range(1, 31)]  # all rank 0: tie by instrument id
    plan = plan_streams(demands, [feed(cap=30)], rights(), AT)
    # 10 prioritized + candidates 1..20 (ascending id breaks the tie); 21..30 left.
    assert planned(plan) == set(range(31, 41)) | set(range(1, 21))
    assert len(plan.subscriptions) == 30
    assert [u.demand.instrument_id.uuid.int for u in plan.uncovered] == list(
        range(21, 31)
    )
    assert all(u.reasons == ((IEX, "capacity_exhausted"),) for u in plan.uncovered)
    assert plan.scheduled_discovery == tuple(inst(i) for i in range(21, 31))
    assert plan.slots_used == {IEX: 30}


def test_rank_orders_within_a_class() -> None:
    demands = [demand(1, rank=5), demand(2, rank=1), demand(3, rank=3)]
    plan = plan_streams(demands, [feed(cap=2)], rights(), AT)
    assert planned(plan) == {2, 3}
    assert [u.demand.instrument_id for u in plan.uncovered] == [inst(1)]


def test_plan_is_deterministic_under_input_order() -> None:
    demands = [demand(i, DemandClass.OWNED, i % 3) for i in range(1, 50)]
    demands += [demand(i) for i in range(50, 90)]
    feeds = [feed(IEX, 20, 120), feed(SIP, 7, 120, CONS)]
    first = plan_streams(demands, feeds, rights(), AT)
    shuffled = demands[:]
    random.Random(7).shuffle(shuffled)
    assert plan_streams(shuffled, feeds[::-1], rights(), AT) == first


def test_entitlement_instruments_latency_and_rights_reported_not_dropped() -> None:
    outside = demand(500)  # not in the feed's entitled instrument set
    needs_sip = demand(1, needs=CONS)  # only an exchange-limited feed exists
    plan = plan_streams([outside, needs_sip, demand(2)], [feed()], rights(), AT)
    assert planned(plan) == {2}
    by = {u.demand: u for u in plan.uncovered}
    assert by[outside].reasons == ((IEX, "instrument_not_entitled"),)
    assert by[needs_sip].reasons == ((IEX, "latency_insufficient"),)


def test_no_feed_at_all_reports_every_demand() -> None:
    plan = plan_streams([demand(1), demand(2)], [], rights(), AT)
    assert plan.subscriptions == ()
    assert [u.reasons for u in plan.uncovered] == [(("", "no_stream_feed"),)] * 2


@pytest.mark.parametrize("reg", [qualified_registry((SIP,)), Registry()])
def test_feed_without_rights_never_gets_a_stream(reg: Registry) -> None:
    plan = plan_streams([demand(1)], [feed()], rights(reg), AT)
    assert plan.subscriptions == ()
    assert plan.uncovered[0].reasons == ((IEX, "rights_denied"),)
    assert plan.denied_feeds[IEX]  # every deny reason kept


def test_missing_derived_data_right_denies_streaming() -> None:
    reg = qualified_registry((IEX,), missing=Use.DERIVED_DATA)
    plan = plan_streams([demand(1)], [feed()], rights(reg), AT)
    assert plan.subscriptions == () and IEX in plan.denied_feeds


def test_same_instrument_shares_one_slot() -> None:
    a = demand(1, DemandClass.OWNED)
    b = demand(1, DemandClass.CANDIDATE)
    plan = plan_streams([a, b, demand(2)], [feed(cap=2)], rights(), AT)
    assert planned(plan) == {1, 2} and plan.uncovered == ()
    (sub,) = [s for s in plan.subscriptions if s.instrument_id == inst(1)]
    assert sub.demands == (a, b)


def test_weakest_sufficient_feed_is_used_first() -> None:
    # The owned demand accepts IEX; taking the single SIP slot would starve the
    # lower-priority demand that needs consolidated data.
    feeds = [feed(IEX, 1), feed(SIP, 1, lat=CONS)]
    first = demand(1, DemandClass.OWNED)
    second = demand(2, needs=CONS)
    plan = plan_streams([first, second], feeds, rights(), AT)
    assert {(s.feed_id, s.instrument_id) for s in plan.subscriptions} == {
        (IEX, inst(1)),
        (SIP, inst(2)),
    }


def test_served_demand_moves_to_free_a_slot_instead_of_starving() -> None:
    # Weakest-first puts A on the delayed feed; only A may use the consolidated
    # one, so A is moved there and B takes the freed delayed slot.
    lag = FeedLatency.DELAYED
    feeds = [
        StreamFeed(IEX, lag, 1, frozenset({inst(1), inst(2)})),
        StreamFeed(SIP, CONS, 1, frozenset({inst(1)})),
    ]
    a, b = demand(1, DemandClass.OWNED, needs=lag), demand(2, needs=lag)
    plan = plan_streams([a, b], feeds, rights(), AT)
    assert plan.uncovered == ()
    assert [(s.feed_id, s.demands) for s in plan.subscriptions] == [
        (IEX, (b,)),
        (SIP, (a,)),
    ]


def test_full_capable_feed_reports_capacity_and_weaker_feed_latency() -> None:
    feeds = [feed(IEX, 5), feed(SIP, 1, lat=CONS)]
    plan = plan_streams(
        [demand(1, needs=CONS), demand(2, needs=CONS)], feeds, rights(), AT
    )
    assert planned(plan) == {1}
    assert plan.uncovered[0].reasons == (
        (IEX, "latency_insufficient"),
        (SIP, "capacity_exhausted"),
    )


def test_invalid_inputs_are_refused() -> None:
    with pytest.raises(StreamError):
        StreamFeed(IEX, EXCH, -1, frozenset())
    with pytest.raises(StreamError):
        StreamFeed(IEX, EXCH, True, frozenset())  # bool is not a count
    with pytest.raises(StreamError):
        WatchDemand(inst(1), DemandClass.OWNED, -1, "r", EXCH)
    with pytest.raises(StreamError):
        WatchDemand(inst(1), DemandClass.OWNED, 0, "bad reason", EXCH)
    with pytest.raises(StreamError):
        plan_streams([], [feed(), feed()], rights(), AT)  # duplicate feed id
    with pytest.raises(ValueError):
        naive = AT.replace(tzinfo=None)
        plan_streams([], [feed()], rights(), naive)


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(
    st.lists(
        st.tuples(
            st.integers(1, 40),
            st.sampled_from(list(DemandClass)),
            st.integers(0, 3),
            st.sampled_from(list(FeedLatency)),
        ),
        max_size=60,
    ),
    st.lists(
        st.tuples(
            st.sampled_from([IEX, SIP, "feed-synth-x"]),
            st.sampled_from(list(FeedLatency)),
            st.integers(0, 30),
            st.sets(st.integers(1, 40)),
        ),
        max_size=3,
        unique_by=lambda f: f[0],
    ),
)
def test_property_caps_entitlements_and_full_accounting(
    raw_demands: list[tuple[int, DemandClass, int, FeedLatency]],
    raw_feeds: list[tuple[str, FeedLatency, int, set[int]]],
) -> None:
    demands = [WatchDemand(inst(n), k, r, "prop", lat) for n, k, r, lat in raw_demands]
    feeds = [
        StreamFeed(fid, lat, cap, frozenset(inst(i) for i in ids))
        for fid, lat, cap, ids in raw_feeds
    ]
    plan = plan_streams(demands, feeds, rights(), AT)  # feed-synth-x has no rights
    caps = {f.feed_id: f for f in feeds}
    for f in feeds:
        subs = [s for s in plan.subscriptions if s.feed_id == f.feed_id]
        assert len(subs) <= f.max_symbols
        assert all(s.instrument_id in f.instruments for s in subs)
        assert f.feed_id not in plan.denied_feeds or not subs
    assert not any(s.feed_id == "feed-synth-x" for s in plan.subscriptions)
    served = [d for s in plan.subscriptions for d in s.demands]
    for s in plan.subscriptions:
        assert all(caps[s.feed_id].latency.satisfies(d.needs) for d in s.demands)
        assert all(d.instrument_id == s.instrument_id for d in s.demands)
    uncovered = [u.demand for u in plan.uncovered]
    assert all(isinstance(u, Uncovered) and u.reasons for u in plan.uncovered)
    assert sorted(served + uncovered, key=repr) == sorted(demands, key=repr)
    # Priority monotonicity: a demand left uncovered for capacity on a feed that
    # could serve it never yields to a subscription held only by lower priority.
    for u in plan.uncovered:
        for fid, why in u.reasons:
            if why != "capacity_exhausted":
                continue
            for s in plan.subscriptions:
                if s.feed_id == fid:
                    assert min(d.order for d in s.demands) < u.demand.order
