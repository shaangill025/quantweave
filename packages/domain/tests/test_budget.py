"""Budget pools and reservations (T037). SYNTHETIC scopes, ids and amounts only.

Expected amounts are hand-computed in the comments, not derived from the module.
"""

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain.budget import (
    BudgetBook,
    BudgetCaps,
    BudgetError,
    Category,
    Refusal,
    Status,
    apply,
    period_of,
)
from qw_domain.decimals import UsdBudget as Usd
from qw_domain.instants import InstantError

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
CONFIG = {"pilot_monthly_usd": "100.00", "improvement_monthly_usd_max": "20.00"}
CAPS = BudgetCaps.from_config(CONFIG, authorization_ref="owner-auth-synth-1")
SCOPE, ACCT, WAIT = "tenant-synth-a", "provider-acct-synth", timedelta(minutes=5)
IMP, M = Category.IMPROVEMENT, Category.MONITORING


def book(at: datetime = AT, caps: BudgetCaps = CAPS) -> BudgetBook:
    return BudgetBook.open(SCOPE, at, caps)


def reserve(
    b: BudgetBook, rid: str, amount: str, cat: Category = Category.RESEARCH,
    at: datetime = AT,
) -> tuple[BudgetBook, Refusal | None]:  # fmt: skip
    return b.reserve(rid, cat, Usd(amount), ACCT, 1000, at, at + WAIT)


def code(result: tuple[BudgetBook, Refusal | None]) -> str | None:
    return None if result[1] is None else result[1].code


def test_caps_come_from_config_and_need_an_owner_authorization() -> None:
    assert (CAPS.inference, CAPS.improvement) == (Usd("100"), Usd("20"))
    unauthorized = BudgetCaps.from_config(CONFIG, authorization_ref=None)
    assert code(reserve(book(caps=unauthorized), "r-1", "0.01")) == (
        "spending_not_authorized"
    )
    for bad in (
        {**CONFIG, "pilot_monthly_usd": 100},  # a JSON number, not a decimal string
        {**CONFIG, "improvement_monthly_usd_max": "120.00"},  # above inference
        {**CONFIG, "improvement_monthly_usd_max": "-1"},
        {"pilot_monthly_usd": "100.00"},
    ):
        with pytest.raises((BudgetError, TypeError)):
            BudgetCaps.from_config(bad, authorization_ref="owner-auth-synth-1")


def test_reservations_stop_at_the_inference_cap() -> None:
    b, _ = reserve(book(), "r-1", "60")
    b, why = reserve(b, "r-2", "40")  # 60 + 40 = 100: exactly the cap
    assert why is None
    after, why = reserve(b, "r-3", "0.000001")
    assert why == Refusal("inference_cap", "0") and after is b and b.version == 2
    s = b.summary()
    assert (s.cap, s.reserved, s.spent, s.available) == (
        Usd("100"), Usd("100"), Usd("0"), Usd("0"))  # fmt: skip


def test_improvement_has_its_own_cap_inside_the_inference_cap() -> None:
    b, _ = reserve(book(), "r-1", "20", IMP)
    assert code(reserve(b, "r-2", "0.01", IMP)) == "improvement_cap"
    assert code(reserve(b, "r-3", "80", M)) is None  # monitoring still available
    b2, _ = reserve(book(), "m-1", "90", M)
    assert code(reserve(b2, "i-1", "15", IMP)) == "inference_cap"  # 90 + 15 > 100
    assert b2.summary(IMP).available == Usd("10")  # min(20 - 0, 100 - 90)


def test_settle_charges_actual_and_releases_the_remainder() -> None:
    b, _ = reserve(book(), "r-1", "100")
    b = b.dispatched("r-1", AT).settle("r-1", Usd("12.345678"), AT + WAIT)
    r = b.get("r-1")
    assert r.status is Status.SETTLED and r.actual == Usd("12.345678")
    s = b.summary()  # 100 - 12.345678 = 87.654322
    assert (s.spent, s.reserved, s.available) == (
        Usd("12.345678"), Usd("0"), Usd("87.654322"))  # fmt: skip
    assert code(reserve(b, "r-2", "87.654322")) is None
    with pytest.raises(BudgetError, match="not_reserved"):
        b.settle("r-1", Usd("1"), AT + WAIT)  # settlement is final


def test_an_overrun_is_recorded_not_hidden_and_blocks_new_reservations() -> None:
    b, _ = reserve(book(), "r-1", "100")
    b = b.dispatched("r-1", AT).settle("r-1", Usd("100.5"), AT + WAIT)
    assert b.summary().available == Usd("-0.5")
    assert code(reserve(b, "r-2", "0.000001")) == "inference_cap"


def test_unanswered_call_keeps_its_reservation_then_expires_at_worst_case() -> None:
    b, _ = reserve(book(), "r-1", "30")
    b = b.dispatched("r-1", AT)
    with pytest.raises(BudgetError, match="dispatched"):
        b.release("r-1")  # a sent request is never silently released
    assert b.expire_due(AT + WAIT - timedelta(microseconds=1)) is b
    b = b.expire_due(AT + WAIT)
    r = b.get("r-1")
    assert (r.status, r.actual, b.summary().spent) == (
        Status.EXPIRED, Usd("30"), Usd("30"))  # fmt: skip
    b = b.settle("r-1", Usd("4"), AT + timedelta(hours=1))  # late usage report
    assert b.get("r-1").status is Status.SETTLED and b.summary().spent == Usd("4")


def test_undispatched_reservation_is_released_without_charge() -> None:
    b, _ = reserve(book(), "r-1", "30")
    with pytest.raises(BudgetError, match="not_dispatched"):
        b.settle("r-1", Usd("1"), AT)
    assert b.expire_due(AT + WAIT).get("r-1").status is Status.RELEASED
    b = b.release("r-1")
    assert b.summary().available == Usd("100")


def test_retry_with_same_id_does_not_reset_the_reservation() -> None:
    b, _ = reserve(book(), "r-1", "30")
    again, why = reserve(b, "r-1", "30", at=AT + timedelta(minutes=1))
    assert why is None and again is b  # created_at and deadline unchanged
    assert code(reserve(b, "r-1", "31")) == "duplicate_reservation"


def test_retry_of_a_closed_or_dispatched_reservation_is_refused() -> None:
    b, _ = reserve(book(), "r-1", "30")
    released = b.release("r-1")  # reviewer probe 1: reserve, release, re-reserve
    assert code(reserve(released, "r-1", "30")) == "reservation_closed"
    sent = b.dispatched("r-1", AT)
    assert code(reserve(sent, "r-1", "30")) == "reservation_closed"
    expired = sent.expire_due(AT + WAIT)  # probe 2: dispatch, expire, re-reserve
    assert code(reserve(expired, "r-1", "30")) == "reservation_closed"
    settled = sent.settle("r-1", Usd("1"), AT + WAIT)
    assert code(reserve(settled, "r-1", "30")) == "reservation_closed"


def test_dispatch_is_refused_at_the_deadline_or_without_authorization() -> None:
    b, _ = reserve(book(), "r-1", "30")
    with pytest.raises(BudgetError, match="deadline_passed"):
        b.dispatched("r-1", AT + WAIT)
    revoked = replace(b, caps=replace(CAPS, authorization_ref=None))
    with pytest.raises(BudgetError, match="spending_not_authorized"):
        revoked.dispatched("r-1", AT)
    with pytest.raises(InstantError):
        b.dispatched("r-1", datetime(2026, 10, 9, 15))  # noqa: DTZ001
    assert (
        b.dispatched("r-1", AT + WAIT - timedelta(microseconds=1)).get("r-1").dispatched
    )


def test_a_report_after_month_end_is_charged_to_the_reservation_month() -> None:
    late = datetime(2026, 10, 31, 23, 59, tzinfo=UTC)
    october, _ = reserve(book(late), "r-1", "30", at=late)
    october = october.dispatched("r-1", late)
    november_report = datetime(2026, 11, 1, 0, 1, tzinfo=UTC)
    october = october.settle("r-1", Usd("2"), november_report)
    assert october.period == "2026-10" and october.summary().spent == Usd("2")
    assert book(november_report).summary().spent == Usd("0")
    sent = reserve(book(late), "r-2", "1", at=late)[0].dispatched("r-2", late)
    with pytest.raises(BudgetError, match="report_before_reservation"):
        sent.settle("r-2", Usd("1"), late - timedelta(seconds=1))


def test_month_boundary_is_utc() -> None:
    assert period_of(datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)) == "2026-10"
    local = datetime(2026, 10, 31, 20, tzinfo=timezone(timedelta(hours=-5)))
    assert period_of(local) == "2026-11"  # 2026-11-01T01:00Z
    october, _ = reserve(book(), "r-1", "100")
    assert code(reserve(october, "r-2", "1", at=local)) == "wrong_period"
    november, why = reserve(book(local), "r-2", "100", at=local)
    assert why is None and november.period == "2026-11"


def test_naive_datetimes_and_float_money_are_rejected() -> None:
    naive = datetime(2026, 10, 9, 15)  # noqa: DTZ001
    with pytest.raises(InstantError):
        book(naive)
    with pytest.raises(InstantError):
        book().reserve("r-1", Category.RESEARCH, Usd("1"), ACCT, 10, naive, AT)
    with pytest.raises(TypeError):
        Usd(1.5)  # type: ignore[arg-type]
    with pytest.raises(BudgetError):
        replace(book(), scope_id="bad scope!")
    one = Decimal("1")
    with pytest.raises(TypeError):
        book().reserve("r-1", M, one, ACCT, 10, AT, AT + WAIT)  # type: ignore[arg-type]
    for amount in ("0", "-1"):
        with pytest.raises(BudgetError):
            reserve(book(), "r-1", amount)


def test_wire_form_matches_the_reservation_schema_fields() -> None:
    b, _ = reserve(book(), "r-1", "2.5")
    assert b.get("r-1").to_wire() == {
        "id": "r-1", "scope_id": SCOPE, "period": "2026-10", "category": "research",
        "currency": "USD", "reserved_amount": "2.5", "actual_amount": None,
        "status": "reserved", "provider_account_reference": ACCT,
        "max_output_tokens": 1000, "created_at": "2026-10-09T15:00:00Z",
    }  # fmt: skip


def race(n: int, checked: bool) -> tuple[BudgetBook, list[str], int]:
    """`n` threads read the same book (barrier), each reserve USD7, then write it
    back through `apply` (compare-and-set) or, as a control, unchecked."""
    lock, barrier = threading.Lock(), threading.Barrier(n)
    state, granted, conflicts = {"book": book()}, list[str](), [0]

    def worker(i: int) -> None:
        first = True
        while True:
            seen = state["book"]
            if first:
                barrier.wait()  # every thread holds the same snapshot
                first = False
            new, why = reserve(seen, f"r-{i}", "7")
            with lock:
                try:
                    state["book"] = apply(state["book"], new, seen.version) if (
                        checked) else new  # fmt: skip
                except BudgetError:
                    conflicts[0] += 1
                    continue  # lost the race: re-read and recompute
                granted.extend([f"r-{i}"] if why is None else [])
                return

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return state["book"], granted, conflicts[0]


def test_concurrent_workers_cannot_reserve_the_same_allowance() -> None:
    final, granted, conflicts = race(20, checked=True)
    assert conflicts >= 1  # the race really happened
    assert len(granted) == 14 == len(final.reservations)  # floor(100 / 7)
    assert final.summary().reserved == Usd("98")


def test_control_without_the_version_check_loses_updates() -> None:
    final, granted, _ = race(20, checked=False)
    assert len(granted) > len(final.reservations)  # grants the book does not hold


def test_apply_refuses_a_stale_version() -> None:
    b0 = book()
    b1, _ = reserve(b0, "r-1", "7")
    assert apply(b0, b1, 0) is b1
    b2, _ = reserve(b0, "r-2", "7")  # computed from the stale b0
    with pytest.raises(BudgetError, match="stale_version"):
        apply(b1, b2, 0)
    with pytest.raises(BudgetError, match="stale_version"):
        apply(b0, b1, 1)


OPS = st.lists(
    st.tuples(
        st.sampled_from(["reserve", "dispatch", "settle", "release", "expire"]),
        st.sampled_from(list(Category)),
        st.integers(1, 3000),  # cents
        st.integers(0, 9),
    ),
    max_size=40,
)


@settings(max_examples=200, deadline=None, derandomize=True, database=None)
@given(OPS)
def test_reserved_plus_spent_never_exceeds_either_cap(
    ops: list[tuple[str, Category, int, int]],
) -> None:
    b, at = book(), AT
    for i, (op, cat, cents, pick) in enumerate(ops):
        at += timedelta(minutes=1)
        ids = [r.reservation_id for r in b.reservations]
        amount = Usd(Decimal(cents).scaleb(-2))
        if op == "reserve" or not ids:
            b, _ = b.reserve(f"r-{i}", cat, amount, ACCT, 10, at, at + WAIT)
            continue
        rid = ids[pick % len(ids)]
        try:
            if op == "dispatch":
                b = b.dispatched(rid, at)
            elif op == "settle":
                b = b.settle(rid, min(amount, b.get(rid).reserved), at)
            else:
                b = b.release(rid) if op == "release" else b.expire_due(at)
        except BudgetError:
            pass
        for s, cap in (
            (b.summary(), CAPS.inference),
            (b.summary(IMP), CAPS.improvement),
        ):
            assert (s.reserved + s.spent).value <= cap.value
        assert all(x.actual is None or x.actual.value <= x.reserved.value
                   for x in b.reservations)  # fmt: skip
