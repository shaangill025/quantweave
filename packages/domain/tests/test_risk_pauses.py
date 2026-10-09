"""Scoped risk pauses, explicit resumption and unitised drawdown (T020; R069, F-13).

SYNTHETIC ids only. Nothing but an explicit, step-up-backed resume clears a pause.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from qw_domain.decimals import Price, Ratio
from qw_domain.instants import InstantError
from qw_domain.risk_pauses import (
    Pause,
    PauseBook,
    PauseError,
    PauseScope,
    PauseTrigger,
    Resume,
    ScopeKind,
    drawdown_breached,
    unitized_drawdown,
)

AT = datetime(2026, 10, 9, 15, tzinfo=UTC)
TENANT = "tenant-synth"
ACCT = PauseScope(ScopeKind.ACCOUNT, "acct-1")
PAUSE = Pause("pause-1", ACCT, PauseTrigger.DRAWDOWN, AT, "breach-receipt-1", True)
RESUME = Resume(
    "pause-1", "user-1", "reviewed recovery", "stepup-9", AT + timedelta(days=2),
    "acct-1:r8",
)  # fmt: skip
PAUSED = PauseBook(TENANT).pause(PAUSE)


def test_blocking_matches_scope_ids_and_ignores_time() -> None:
    book = PAUSED.pause(Pause("pause-2", PauseScope(ScopeKind.SLEEVE, "sleeve-t"),
                              PauseTrigger.USER, AT, "user-req-1", False))  # fmt: skip
    hits = book.blocking(TENANT, "acct-1", "sleeve-t", "strat-1")
    assert [p.pause_id for p in hits] == ["pause-1", "pause-2"]
    assert book.blocking(TENANT, "acct-2", None, "strat-1") == ()
    assert PAUSED.active() == (PAUSE,)  # no expiry exists to consult


def test_resume_is_explicit_and_history_is_append_only() -> None:
    done = PAUSED.resume(RESUME)
    assert done.active() == () and PAUSED.active() == (PAUSE,)
    assert done.events == (PAUSE, RESUME)  # the pause record is kept
    with pytest.raises(PauseError, match="not_active"):
        done.resume(RESUME)
    again = done.pause(replace(PAUSE, pause_id="pause-2"))
    assert [p.pause_id for p in again.active()] == ["pause-2"]
    with pytest.raises(PauseError, match="duplicate"):
        done.pause(PAUSE)
    with pytest.raises(InstantError):
        replace(RESUME, resumed_at=datetime(2026, 10, 11, 9))  # noqa: DTZ001
    # A non-material user pause needs no reconciled revision; stale data always does.
    for trigger, ok in ((PauseTrigger.USER, True), (PauseTrigger.STALE, False)):
        book = PauseBook(TENANT).pause(replace(PAUSE, trigger=trigger, material=False))
        bare = replace(RESUME, reconciled_revision=None)
        if ok:
            assert book.resume(bare).active() == ()
        else:
            with pytest.raises(PauseError, match="reconciled_state_required"):
                book.resume(bare)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"principal_id": ""}, "principal"),
        ({"step_up_receipt_id": "bad id"}, "step_up"),
        ({"reason": " "}, "reason"),
        ({"reconciled_revision": None}, "reconciled_state_required"),
        ({"resumed_at": AT - timedelta(seconds=1)}, "before_trigger"),
        ({"pause_id": "pause-9"}, "not_active"),
    ],
)
def test_resume_rejections(change: dict[str, Any], code: str) -> None:
    with pytest.raises(PauseError, match=code):
        PAUSED.resume(replace(RESUME, **change))


def test_unitized_drawdown_ignores_cash_flows() -> None:
    # Unit values (NAV per unit): high-water 1.25, now 1.10 -> D = 1 - 1.1/1.25 = 0.12.
    d = unitized_drawdown(Price("1.25"), Price("1.10"))
    assert d == Ratio("0.12") and drawdown_breached(d, Ratio("0.10"))
    assert drawdown_breached(d, Ratio("0.12")) and not drawdown_breached(
        d, Ratio("0.15")
    )
    # A deposit buys units at the current unit value, so D is unchanged by it.
    assert unitized_drawdown(Price("1.25"), Price("1.25")) == Ratio(0)
    with pytest.raises(ValueError, match="above"):
        unitized_drawdown(Price("1"), Price("1.5"))


def test_book_validates_the_event_chain_on_construction() -> None:
    bare = replace(RESUME, reconciled_revision=None)
    for events, code in [
        ((PAUSE, bare), "reconciled_state_required"),  # material pause
        ((RESUME,), "not_active"),
        ((PAUSE, PAUSE), "duplicate"),
        ((PAUSE, replace(RESUME, resumed_at=AT - timedelta(1))), "before_trigger"),
        ((replace(PAUSE, scope=PauseScope(ScopeKind.TENANT, "tenant-x")),),
         "tenant_scope"),  # a tenant pause names the book's own tenant
    ]:  # fmt: skip
        with pytest.raises(PauseError, match=code):
            PauseBook(TENANT, events)
    with pytest.raises(PauseError, match="tenant_scope"):
        PAUSED.pause(Pause("p-3", PauseScope(ScopeKind.TENANT, "tenant-x"),
                           PauseTrigger.USER, AT, "user-req-1", False))  # fmt: skip
    assert PauseBook(TENANT, (PAUSE, RESUME)).active() == ()
