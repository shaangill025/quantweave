"""Deterministic watchlist events and the alert lifecycle: dedupe, cooldown, snooze
and stale sources (T024; R009, R014, R020, R047). SYNTHETIC bars, coverage, filings
and tenants. Expected outcomes are hand-computed from the stated closes and volumes.
"""

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.bars import Bar, Ohlc, SessionLabel, Slot
from qw_domain.coverage import Coverage, CoverageState
from qw_domain.decimals import Price, Quantity
from qw_domain.monitoring import (
    AlertBook,
    Condition,
    Decision,
    Evaluation,
    MonitoringError,
    Outcome,
    RuleKey,
    evaluate_market,
    evaluate_review,
)
from test_bars import DAY, IEX, RT, X, utc
from test_watchlists import T

AT = utc(13, 35, 5)
AGE = timedelta(minutes=2)
S = CoverageState
RULE = RuleKey(T, "wl-1", X, "rule-1")


def cov(
    minute: int,
    close: str | None,
    volume: str = "10",
    state: CoverageState = CoverageState.COVERED,
    reason: str | None = None,
) -> Coverage:
    start = utc(13, minute)
    slot = Slot(SessionLabel.REGULAR, DAY, start, start + timedelta(minutes=1))
    bar = None
    if close is not None:
        px = Price(close)
        ohlc = Ohlc(px, px, px, px)
        bar = Bar(X, IEX, RT, slot, ohlc, Quantity(volume), 1, slot.end)
    return Coverage(X, slot.label, slot.start, slot.end, state, reason, bar)


def market(
    metric: str, op: str, values: tuple[str, ...], *recent: Coverage
) -> Evaluation:
    return evaluate_market(
        RULE, Condition(metric, op, values, IEX, "SYN"), recent, AT, AGE
    )


@pytest.mark.parametrize(
    ("metric", "op", "values", "prior", "last", "fires"),
    [
        ("price_cross", "gt", ("100",), "99.50", "100.25", True),
        ("price_cross", "gt", ("100",), "100", "100.01", True),  # from at the level
        ("price_cross", "gt", ("100",), "100.25", "101", False),  # already above
        ("price_cross", "gt", ("100",), "99", "100", False),  # touching is not above
        ("price_cross", "lt", ("100",), "100.50", "99.99", True),
        ("pct_move", "gte", ("2",), "50", "51", True),  # +2% exactly
        ("pct_move", "gt", ("2",), "50", "51", False),
        ("pct_move", "lte", ("-3",), "80", "77.6", True),  # -3% exactly
        ("pct_move", "lte", ("-3",), "80", "77.61", False),  # -2.9875%
        ("pct_move", "between", ("1", "2"), "200", "203", True),  # +1.5%
        ("pct_move", "between", ("1", "2"), "200", "205", False),  # +2.5%
        ("pct_move", "between", ("1", "2"), "200", "202", True),  # +1%: inclusive
    ],
)
def test_price_conditions_hand_values(
    metric: str, op: str, values: tuple[str, ...], prior: str, last: str, fires: bool
) -> None:
    ev = market(metric, op, values, cov(33, prior), cov(34, last))
    assert ev.outcome is (Outcome.TRIGGERED if fires else Outcome.NOT_TRIGGERED)
    assert ev.trigger_id == (utc(13, 34).isoformat() if fires else None)


def test_volume_condition() -> None:
    def vol(op: str, th: str, c: Coverage) -> Outcome:
        return market("volume", op, (th,), c).outcome

    assert vol("gt", "1000", cov(34, "5", "1500")) is Outcome.TRIGGERED
    assert vol("gt", "1500", cov(34, "5", "1500")) is Outcome.NOT_TRIGGERED
    # A covered record without a bar does not name its feed: fail closed, so a
    # no-trade interval cannot currently trigger a volume rule.
    assert vol("lt", "1", cov(34, None)) is Outcome.STALE_SOURCE


def test_silence_of_one_feed_never_fires_a_rule_bound_to_another() -> None:
    # Review probe: `volume lte 0` on feed-other, fed only the IEX feed's silence.
    cond = Condition("volume", "lte", ("0",), "feed-synth-other", "SYN")
    ev = evaluate_market(RULE, cond, (cov(34, None),), AT, AGE)
    assert (ev.outcome, ev.reason) == (Outcome.STALE_SOURCE, "feed_unverifiable")


C33, C34, RETRO = cov(33, "99"), cov(34, "101"), S.RETROSPECTIVE


def gap(minute: int, state: CoverageState, why: str) -> Coverage:
    return cov(minute, "101", state=state, reason=why)


@pytest.mark.parametrize(
    ("recent", "reason"),
    [
        ((C33, gap(34, S.MISSING, "sequence_gap")), "coverage_missing:sequence_gap"),
        ((C33, gap(34, S.DELAYED, "not_streamed")), "coverage_delayed:not_streamed"),
        ((gap(33, RETRO, "trailing_loss"), C34), f"coverage_{RETRO}:trailing_loss"),
        ((cov(31, "99"), cov(32, "101")), "source_age_exceeded"),  # 13:33 + 2m < t
        ((cov(34, "101"),), "insufficient_history"),
        ((cov(32, "99"), cov(34, "101")), "non_contiguous"),
        ((cov(33, None), cov(34, "101")), "feed_unverifiable"),
    ],
)  # fmt: skip
def test_stale_or_incomplete_input_never_fires(
    recent: tuple[Coverage, ...], reason: str
) -> None:
    ev = market("price_cross", "gt", ("100",), *recent)
    assert (ev.outcome, ev.reason, ev.trigger_id) == (S_OUT, reason, None)


S_OUT = Outcome.STALE_SOURCE


def test_unconfigured_freshness_feed_mismatch_and_look_ahead() -> None:
    cond = Condition("price_cross", "gt", ("100",), IEX, "SYN")
    recent = (cov(33, "99"), cov(34, "101"))
    ev = evaluate_market(RULE, cond, recent, AT, None)
    assert (ev.outcome, ev.reason) == (Outcome.STALE_SOURCE, "freshness_unconfigured")
    other = replace(cond, data_spec_id="feed-synth-other")
    ev = evaluate_market(RULE, other, recent, AT, AGE)
    assert (ev.outcome, ev.reason) == (Outcome.STALE_SOURCE, "feed_mismatch")
    with pytest.raises(MonitoringError, match="look_ahead"):  # interval open at t
        evaluate_market(RULE, cond, recent, utc(13, 34, 30), AGE)
    late = recent[1].bar
    assert late is not None
    known_late = replace(recent[1], bar=replace(late, knowledge_at=utc(13, 35, 30)))
    with pytest.raises(MonitoringError, match="look_ahead"):  # closed, known after t
        evaluate_market(RULE, cond, (recent[0], known_late), AT, AGE)


def test_condition_validation() -> None:
    for bad in (
        ("rsi", "gt", ("1",)),
        ("price_cross", "gte", ("1",)),
        ("pct_move", "between", ("2", "1")),
        ("volume", "gt", ("1", "2")),
        ("volume", "gt", ("NaN",)),
        ("volume", "gt", ("1.0e400000",)),
    ):
        with pytest.raises(MonitoringError):
            Condition(bad[0], bad[1], bad[2], IEX, "SYN")


def test_review_date_reached() -> None:
    due = utc(14, 0)
    assert evaluate_review(RULE, due, utc(13, 59)).outcome is Outcome.NOT_TRIGGERED
    ev = evaluate_review(RULE, due, due)
    assert (ev.outcome, ev.trigger_id) == (Outcome.TRIGGERED, due.isoformat())
    assert evaluate_review(RULE, None, due).outcome is Outcome.NOT_TRIGGERED


def ev(
    at: datetime, trigger: str | None, outcome: Outcome = Outcome.TRIGGERED
) -> Evaluation:
    return Evaluation(RULE, at, outcome, trigger, "SYN")


def test_dedupe_cooldown_and_tenant_check() -> None:
    book = AlertBook(T, timedelta(minutes=10))
    assert book.offer(ev(utc(14, 0), "a")).decision is Decision.ALERTED
    assert book.offer(ev(utc(14, 30), "a")).decision is Decision.DUPLICATE
    assert book.offer(ev(utc(14, 31), "b")).decision is Decision.ALERTED
    assert book.offer(ev(utc(14, 40, 59), "c")).decision is Decision.COOLDOWN
    assert book.offer(ev(utc(14, 41), "c")).decision is Decision.DUPLICATE  # consumed
    assert book.offer(ev(utc(14, 41), "d")).decision is Decision.ALERTED  # 10m after b
    assert [a.trigger_id for a in book.alerts] == ["a", "b", "d"]
    assert len(book.log) == 6
    with pytest.raises(MonitoringError, match="tenant"):
        AlertBook(OTHER_TENANT, timedelta(0)).offer(ev(utc(14, 0), "a"))
    with pytest.raises(MonitoringError, match="time_order"):
        book.offer(ev(utc(14, 0), "e"))


OTHER_TENANT = "tenant-synth-b"


def test_snooze_is_never_extended_silently_and_stale_is_recorded() -> None:
    book = AlertBook(T, timedelta(0))
    book.snooze(RULE, utc(15, 0), utc(14, 0))
    with pytest.raises(MonitoringError, match="snooze_active"):
        book.snooze(RULE, utc(16, 0), utc(14, 10))
    assert book.snoozed_until(RULE) == utc(15, 0)
    assert book.offer(ev(utc(14, 59), "a")).decision is Decision.SNOOZED
    book.snooze(RULE, utc(15, 30), utc(14, 59), extend=True)  # explicit, audited
    assert [(s.until, s.extended) for s in book.snoozes] == [
        (utc(15, 0), False),
        (utc(15, 30), True),
    ]
    stale = ev(utc(15, 31), None, Outcome.STALE_SOURCE)
    assert book.offer(stale).decision is Decision.STALE_SOURCE
    assert book.offer(ev(utc(15, 31), "b")).decision is Decision.ALERTED
    with pytest.raises(MonitoringError):
        book.snooze(RULE, utc(15, 0), utc(15, 31))  # until in the past
    with pytest.raises(MonitoringError):
        Evaluation(RULE, utc(15, 0), Outcome.STALE_SOURCE, "x", "SYN")


STEP = st.tuples(
    st.integers(0, 15),  # minutes to advance
    st.sampled_from(list(Outcome)),
    st.integers(0, 4),  # trigger instance
    st.none() | st.tuples(st.integers(1, 40), st.booleans()),  # snooze request
)


@settings(max_examples=300, derandomize=True, database=None, deadline=None)
@given(st.lists(STEP, max_size=40), st.integers(0, 20))
def test_never_alerts_in_cooldown_snooze_or_on_stale_input(
    steps: list[tuple[int, Outcome, int, tuple[int, bool] | None]], cool: int
) -> None:
    cooldown = timedelta(minutes=cool)
    book, at = AlertBook(T, cooldown), utc(14, 0)
    until: datetime | None = None
    last_alert: datetime | None = None
    seen: set[str] = set()
    for dt, outcome, k, snooze in steps:
        at += timedelta(minutes=dt)
        if snooze is not None:
            active = until is not None and at < until
            if active and not snooze[1]:
                with pytest.raises(MonitoringError):
                    book.snooze(RULE, at + timedelta(minutes=snooze[0]), at)
            else:
                book.snooze(RULE, at + timedelta(minutes=snooze[0]), at, snooze[1])
                until = at + timedelta(minutes=snooze[0])
        trig = f"t{k}" if outcome is Outcome.TRIGGERED else None
        got = book.offer(Evaluation(RULE, at, outcome, trig, "SYN")).decision
        snoozed = until is not None and at < until
        cooling = last_alert is not None and at < last_alert + cooldown
        if got is Decision.ALERTED:
            assert trig is not None and trig not in seen
            assert not snoozed and not cooling
            last_alert = at
        elif trig is not None and trig not in seen and not snoozed and not cooling:
            pytest.fail(f"a fresh, due trigger was suppressed: {got}")
        if outcome is Outcome.STALE_SOURCE:
            assert got is Decision.STALE_SOURCE
        if trig is not None:
            seen.add(trig)
    assert [r.decision for r in book.log].count(Decision.ALERTED) == len(book.alerts)
