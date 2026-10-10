"""Notifications: minimised external payloads, current-state resolution, delivery
versus read state, preferences, quiet hours, dedupe and push qualification (T056).

SYNTHETIC inputs only: made-up tenants, users, addresses, push origins and the T026
test proposal (buy 80 at 50, cash effect 4000 CAD, account acct-1).
"""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from qw_domain import notifications as N
from qw_domain.jobs import JobState
from qw_domain.monitoring import Alert, RuleKey
from qw_domain.notifications import (
    SAFETY,
    Category,
    Center,
    Channel,
    CurrentView,
    DeliveryStatus,
    ExternalMessage,
    Notification,
    NotificationError,
    Preferences,
    PushSubscription,
    Recipient,
    SendOutcome,
    Signal,
    from_alert,
    from_job,
    from_pause,
    from_proposal,
    proposal_resolver,
    push_qualification,
    render,
)
from qw_domain.proposals import Proposal, ProposalState, revise
from qw_domain.risk_pauses import Pause, PauseScope, PauseTrigger, ScopeKind
from test_proposals import AT, BUY, IID, TENANT, active, cur, ver

SPEC = Path(__file__).resolve().parents[3] / "docs/spec"
T = timedelta
USER = "user-1"
ON = Preferences(email=True, browser_push=True)
PUSH = PushSubscription("https://push.example.invalid", "granted", True, None, False)
ME = Recipient(TENANT, USER, ON, "synthetic@example.invalid", True, PUSH)


class Transport:
    """SYNTHETIC in-memory transport: records messages, answers from a script."""

    def __init__(self, *answers: SendOutcome | Exception) -> None:
        self.answers, self.sent = list(answers), list[ExternalMessage]()

    def send(self, message: ExternalMessage, recipient: Recipient) -> SendOutcome:
        self.sent.append(message)
        answer = self.answers.pop(0) if self.answers else SendOutcome.ACCEPTED
        if isinstance(answer, Exception):
            raise answer
        return answer


def center() -> Center:
    return Center(TENANT, T(minutes=10), 3, b"synthetic-id-key-0001")


def nothing(n: Notification, at: datetime) -> CurrentView | None:
    return None


def alert(trigger: str = "2026-10-09T14:59:00+00:00") -> Signal:
    rule = RuleKey(TENANT, "wl-1", IID, "rule-abc")
    return from_alert(Alert(rule, trigger, AT - T(seconds=30), "close 99.5 -> 100.25"))


def test_contract_enums_and_preference_fields() -> None:
    api = yaml.safe_load((SPEC / "contracts/openapi.yaml").read_text())["components"]
    note = api["schemas"]["Notification"]["properties"]
    assert {c.value for c in Channel} == set(note["channel"]["enum"])
    assert {s.value for s in DeliveryStatus} == set(note["status"]["enum"])
    required = set(api["schemas"]["NotificationPreferences"]["required"])
    assert required <= set(Preferences.__dataclass_fields__)
    assert Preferences().redact_financial_values and Preferences().in_app
    assert not Preferences().email and not Preferences().browser_push  # opt-in


def test_external_payload_carries_no_financial_details() -> None:
    p = active(ver())
    pause = Pause(
        "pause-1", PauseScope(ScopeKind.ACCOUNT, "acct-1"), PauseTrigger.DRAWDOWN,
        AT, "breach-77", True,
    )  # fmt: skip
    signals = [from_proposal(p), alert(), from_pause(pause, TENANT)]
    c = center()
    forbidden = ["80", "4000", "50", "acct-1", str(IID.uuid), "prop-a", "wl-1",
                 "rule-abc", "pause-1", "breach-77", "99.5", "100.25", "CAD", "buy",
                 TENANT, USER, "synthetic@"]  # fmt: skip
    for s in signals:
        n = c.submit(s, ME, AT).notification
        for channel in (Channel.EMAIL, Channel.BROWSER_PUSH):
            m = render(n, channel)
            text = f"{m.title} {m.body}".lower()
            assert [f for f in forbidden if f.lower() in text] == []
            assert not re.search(r"[0-9]", m.title + m.body)
            assert m.link == f"/notifications/{n.id}"
            assert re.fullmatch(r"ntf-[0-9a-f]{32}", n.id)
        with pytest.raises(NotificationError, match="in_app_not_external"):
            render(n, Channel.IN_APP)
    with pytest.raises(NotificationError, match="detail_exposure_unsupported"):
        Preferences(redact_financial_values=False)


def test_late_notification_opens_expired_state_and_cannot_reactivate() -> None:
    p = active(ver())  # expires at AT + 30 min
    c = center()
    n = c.submit(from_proposal(p), ME, AT).notification
    assert n.subject.version == "1:active"
    view = proposal_resolver(p, cur(AT))  # built once; resolves as of each open
    early = c.open(USER, n.id, view, AT + T(minutes=1))
    assert early.view.state == "active" and early.view.actionable
    late_at = AT + T(minutes=31)
    late = c.open(USER, n.id, view, late_at)
    assert late.view.state == ProposalState.EXPIRED and not late.view.actionable
    assert late.view.current_version == "1:expired" and late.changed
    assert late.event_age == T(minutes=31, seconds=0)  # from the trigger (publish) time
    events = p.events
    again = c.open(USER, n.id, view, late_at + T(minutes=14))
    assert p.events == events and not again.view.actionable  # opening never acts


def test_superseded_and_unavailable_subjects_are_not_actionable() -> None:
    p = active(ver())
    c = center()
    n = c.submit(from_proposal(p), ME, AT).notification
    d = revise(p, ver(n=2), cur(AT + T(minutes=2)))
    assert d.accepted
    o = c.open(USER, n.id, proposal_resolver(d.proposal, cur(AT + T(minutes=3))),
               AT + T(minutes=3))  # fmt: skip
    assert o.view.state == ProposalState.SUPERSEDED and not o.view.actionable
    assert o.view.current_version == "2:candidate" and o.changed

    lost = c.open(USER, n.id, nothing, AT + T(minutes=4)).view
    assert lost.state == "unavailable" and not lost.actionable
    for bad in ("3:active", "x:active", ""):
        forged = replace(n, subject=replace(n.subject, version=bad))
        with pytest.raises(NotificationError, match="subject_mismatch"):
            proposal_resolver(d.proposal, cur())(forged, AT)
    other: Proposal = active(ver(pid="prop-b"))
    with pytest.raises(NotificationError, match="subject_mismatch"):
        c.open(USER, n.id, proposal_resolver(other, cur()), AT + T(minutes=4))


def test_sending_is_not_a_read_receipt() -> None:
    c, tx = center(), Transport()
    sub = c.submit(alert(), ME, AT)
    n = sub.notification
    assert {d.channel for d in sub.deliveries} == {Channel.EMAIL, Channel.BROWSER_PUSH}
    for d in sub.deliveries:
        done = c.attempt(n.id, d.channel, tx, ME, AT + T(seconds=5))
        assert done.status is DeliveryStatus.DELIVERED
        assert [e.status for e in done.events] == [
            DeliveryStatus.QUEUED, DeliveryStatus.ATTEMPTED, DeliveryStatus.DELIVERED
        ]  # fmt: skip
    assert c.get(USER, n.id).seen_at is None
    assert [not e.seen for e in c.feed(USER, nothing, AT)] == [True]  # listing
    assert c.get(USER, n.id).seen_at is None  # listing is not reading either
    first = c.open(USER, n.id, nothing, AT + T(minutes=1))
    assert first.notification.seen_at == AT + T(minutes=1)
    later = c.open(USER, n.id, nothing, AT + T(minutes=9))
    assert later.notification.seen_at == AT + T(minutes=1)  # first open is kept
    assert all(d.status is DeliveryStatus.DELIVERED for d in c.deliveries(n.id))
    assert len(tx.sent) == 2


def test_duplicates_and_retries_keep_original_records() -> None:
    c = center()
    first = c.submit(alert(), ME, AT)
    again = c.submit(alert(), ME, AT + T(minutes=1))  # outbox redelivery
    assert first.created and not again.created and again.deliveries == ()
    assert again.notification == first.notification
    assert again.notification.created_at == AT
    assert again.notification.trigger_at == AT - T(seconds=30)
    # A new trigger of the same rule inside the window: new feed item, no new email.
    soon = c.submit(alert("2026-10-09T15:05:00+00:00"), ME, AT + T(minutes=6))
    assert soon.created and soon.deliveries == ()
    assert set(soon.reasons) == {"email:coalesced", "browser_push:coalesced"}
    late = c.submit(alert("2026-10-09T15:11:00+00:00"), ME, AT + T(minutes=10))
    assert len(late.deliveries) == 2  # window measured from the last external send
    assert len(c.feed(USER, nothing, AT)) == 3


def test_delivery_state_machine_and_retries() -> None:
    c = center()
    n = c.submit(alert(), replace(ME, push=None), AT).notification
    tx = Transport(SendOutcome.REJECTED, SendOutcome.REJECTED, SendOutcome.ACCEPTED)
    d = c.attempt(n.id, Channel.EMAIL, tx, ME, AT + T(seconds=1))
    assert d.status is DeliveryStatus.FAILED and d.events[-1].code == "rejected"
    d = c.attempt(n.id, Channel.EMAIL, tx, ME, AT + T(seconds=2))
    assert d.status is DeliveryStatus.FAILED and d.events[-1].code == "rejected"
    d = c.attempt(n.id, Channel.EMAIL, tx, ME, AT + T(seconds=3))
    assert d.status is DeliveryStatus.DELIVERED and d.attempts == 3
    assert d.accepted_at == AT + T(seconds=3)
    assert d.accepted_at - c.get(USER, n.id).trigger_at == T(seconds=33)  # R067
    assert c.get(USER, n.id).created_at == AT  # retries never reset it
    with pytest.raises(NotificationError, match="not_retryable"):
        c.attempt(n.id, Channel.EMAIL, tx, ME, AT + T(seconds=4))
    with pytest.raises(NotificationError, match="no_delivery"):
        c.attempt(n.id, Channel.BROWSER_PUSH, tx, ME, AT + T(seconds=4))
    for who in (replace(ME, user_id="user-2"), replace(ME, tenant_id="tenant-x")):
        with pytest.raises(NotificationError, match="no_delivery"):
            c.attempt(n.id, Channel.EMAIL, tx, who, AT + T(seconds=4))

    c2 = center()
    n2 = c2.submit(alert(), replace(ME, push=None), AT).notification
    fail = Transport(*[SendOutcome.REJECTED] * 3)
    for k in range(3):
        c2.attempt(n2.id, Channel.EMAIL, fail, ME, AT + T(seconds=k + 1))
    with pytest.raises(NotificationError, match="attempts_exhausted"):
        c2.attempt(n2.id, Channel.EMAIL, fail, ME, AT + T(seconds=9))
    c3 = center()
    n3 = c3.submit(alert(), replace(ME, push=None), AT).notification
    u = c3.attempt(n3.id, Channel.EMAIL, Transport(SendOutcome.UNKNOWN), ME, AT)
    assert u.status is DeliveryStatus.UNKNOWN  # never blindly resent
    with pytest.raises(NotificationError, match="not_retryable"):
        c3.attempt(n3.id, Channel.EMAIL, Transport(), ME, AT + T(seconds=1))
    # An exception or an unrecognised result may follow a send: unknown, final.
    for answer, code in ((RuntimeError("smtp down"), "transport_error"),
                         ("ok", "unrecognised_result")):  # fmt: skip
        c4 = center()
        n4 = c4.submit(alert(), replace(ME, push=None), AT).notification
        tx4 = Transport(answer)  # type: ignore[arg-type]
        e = c4.attempt(n4.id, Channel.EMAIL, tx4, ME, AT)
        assert (e.status, e.events[-1].code) == (DeliveryStatus.UNKNOWN, code)
        assert "smtp down" not in repr(e)
        with pytest.raises(NotificationError, match="not_retryable"):
            c4.attempt(n4.id, Channel.EMAIL, Transport(), ME, AT + T(seconds=1))
    with pytest.raises(NotificationError, match="time_order"):
        c3.attempt(n3.id, Channel.EMAIL, Transport(), ME, AT - T(seconds=1))


def test_quiet_hours_defer_external_channels_only() -> None:
    quiet = replace(ON, quiet_hours_timezone="America/Toronto",
                    quiet_hours=("22:00-07:00", "12:00-13:00"))  # fmt: skip
    me = replace(ME, preferences=quiet)
    c = center()
    at = datetime(2026, 10, 10, 7, tzinfo=UTC)  # 03:00 EDT (UTC-4)
    sub = c.submit(alert(), me, at)
    assert {d.not_before for d in sub.deliveries} == {
        datetime(2026, 10, 10, 11, tzinfo=UTC)
    }
    assert c.feed(USER, nothing, AT)  # the in-app item is immediate
    with pytest.raises(NotificationError, match="quiet_hours"):
        c.attempt(sub.notification.id, Channel.EMAIL, Transport(), me, at + T(hours=3))
    ok = c.attempt(sub.notification.id, Channel.EMAIL, Transport(), me, at + T(hours=4))
    assert ok.status is DeliveryStatus.DELIVERED
    noon = datetime(2026, 10, 10, 16, 30, tzinfo=UTC)  # 12:30 EDT
    assert quiet.quiet_until(noon) == datetime(2026, 10, 10, 17, tzinfo=UTC)
    assert quiet.quiet_until(datetime(2026, 10, 10, 17, tzinfo=UTC)) is None  # end open
    assert quiet.quiet_until(datetime(2026, 10, 11, 2, tzinfo=UTC)) == datetime(
        2026, 10, 11, 11, tzinfo=UTC
    )  # 22:00 EDT starts the window (inclusive)
    chained = replace(quiet, quiet_hours=("22:00-07:00", "07:00-08:00"))
    assert chained.quiet_until(at) == datetime(2026, 10, 10, 12, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 4, tzinfo=UTC)  # 23:00 EST (UTC-5)
    assert quiet.quiet_until(winter) == datetime(2026, 12, 1, 12, tzinfo=UTC)
    bad: list[dict[str, Any]] = [
        {"quiet_hours": ("22:00-22:00",)}, {"quiet_hours": ("7:00-9:00",)},
        {"quiet_hours": ("24:00-01:00",)}, {"quiet_hours_timezone": "Mars/Base"},
        {"quiet_hours": ["22:00-07:00"]},
    ]  # fmt: skip
    for kw in bad:
        with pytest.raises(NotificationError, match="quiet_hours"):
            replace(quiet, **kw)


def test_preferences_mutes_and_safety_in_app() -> None:
    assert Category.RISK_PAUSE in SAFETY
    with pytest.raises(NotificationError, match="safety_in_app"):
        Preferences(muted=frozenset({(Category.RISK_PAUSE, Channel.IN_APP)}))
    mute = frozenset({(Category.MONITORING_ALERT, Channel.EMAIL)})
    prefs = Preferences(in_app=False, email=True, muted=mute)
    me = replace(ME, preferences=prefs)
    c = center()
    sub = c.submit(alert(), me, AT)
    assert sub.deliveries == () and "email:muted" in sub.reasons
    assert "in_app:off" in sub.reasons
    pause = Pause("pause-1", PauseScope(ScopeKind.TENANT, TENANT), PauseTrigger.USER,
                  AT, "flag-1", False)  # fmt: skip
    safe = c.submit(from_pause(pause, TENANT), me, AT)
    assert [e.id for e in c.feed(USER, nothing, AT)] == [safe.notification.id]
    assert [d.channel for d in safe.deliveries] == [Channel.EMAIL]
    # The record behind an unlisted item still resolves, so an email link works.
    assert c.open(USER, sub.notification.id, nothing, AT).view.state


def test_push_and_email_qualification_fail_closed() -> None:
    at = AT
    assert push_qualification(PUSH, at) == ()
    cases = {
        "no_subscription": None,
        "permission_not_granted": replace(PUSH, permission="default"),
        "unsupported_environment": replace(PUSH, environment_supports=False),
        "endpoint_not_https": replace(
            PUSH, endpoint_origin="http://push.example.invalid"
        ),
        "subscription_expired": replace(PUSH, expires_at=at),
        "revoked": replace(PUSH, revoked=True),
    }
    for reason, sub in cases.items():
        assert reason in push_qualification(sub, at)
        c = center()
        got = c.submit(alert(), replace(ME, push=sub), at)
        assert [d.channel for d in got.deliveries] == [Channel.EMAIL]
        assert f"browser_push:{reason}" in got.reasons
    for me in (replace(ME, email_verified=False), replace(ME, email_address=None)):
        got = center().submit(alert(), replace(me, push=None), at)
        assert got.deliveries == () and "email:unverified" in got.reasons


def test_tenant_and_user_isolation() -> None:
    c = center()
    n = c.submit(alert(), ME, AT).notification
    other_user = replace(ME, user_id="user-2")
    c.submit(alert(), other_user, AT)
    assert len(c.feed(USER, nothing, AT)) == 1
    assert c.feed("user-3", nothing, AT) == ()
    for who in ("user-2", "user-3"):
        with pytest.raises(NotificationError, match="not_found"):
            c.open(who, n.id, nothing, AT)
        with pytest.raises(NotificationError, match="not_found"):
            c.get(who, n.id)
    foreign = replace(alert(), tenant_id="tenant-other")
    with pytest.raises(NotificationError, match="tenant"):
        c.submit(foreign, ME, AT)
    with pytest.raises(NotificationError, match="tenant"):
        c.submit(alert(), replace(ME, tenant_id="tenant-other"), AT)
    assert n.id != c.submit(alert(), other_user, AT).notification.id


def test_sources_and_time_rules() -> None:
    p = active(ver())
    s = from_proposal(p)
    assert (s.category, s.object_id) == (Category.PROPOSAL, "prop-a")
    assert s.version == "1:active"
    assert s.trigger_at == p.events[-1].at
    j = from_job(TENANT, "job-9", JobState.DEAD_LETTER, AT)
    assert (j.category, j.version) == (Category.JOB_OUTCOME, "dead_letter")
    with pytest.raises(NotificationError, match="job_not_terminal"):
        from_job(TENANT, "job-9", JobState.RUNNING, AT)
    a = alert()
    assert a.category is Category.MONITORING_ALERT
    assert a.trigger_at == AT - T(seconds=30)
    assert a.object_id == alert("2026-10-09T15:30:00+00:00").object_id
    with pytest.raises(NotificationError, match="time_order"):
        center().submit(a, ME, AT - T(minutes=1))  # cannot be generated before trigger
    with pytest.raises(NotificationError, match="malformed"):
        Signal(TENANT, Category.JOB_OUTCOME, "bad id!", "x", AT)
    assert BUY.quantity.value == 80  # the payload test's forbidden values are real


def test_attempt_rechecks_consent_and_qualification() -> None:  # review S1
    revoked = replace(PUSH, revoked=True)
    later: list[tuple[Channel, Recipient, str]] = [
        (Channel.BROWSER_PUSH, replace(ME, push=revoked), "withdrawn_revoked"),
        (Channel.BROWSER_PUSH, replace(ME, preferences=Preferences()), "withdrawn_off"),
        (Channel.EMAIL, replace(ME, email_verified=False), "withdrawn_unverified"),
        (Channel.EMAIL, replace(ME, email_address=None), "withdrawn_unverified"),
        (Channel.EMAIL, replace(ME, preferences=replace(ON, muted=frozenset(
            {(Category.MONITORING_ALERT, Channel.EMAIL)}))), "withdrawn_muted"),
    ]  # fmt: skip
    for ch, who, code in later:
        c, tx = center(), Transport()
        n = c.submit(alert(), ME, AT).notification
        d = c.attempt(n.id, ch, tx, who, AT + T(seconds=1))
        assert (d.status, d.events[-1].code, d.attempts) == (
            DeliveryStatus.FAILED, code, 0
        )  # fmt: skip
        assert tx.sent == []
        with pytest.raises(NotificationError, match="not_retryable"):
            c.attempt(n.id, ch, tx, ME, AT + T(seconds=2))
    expiring = replace(ME, push=replace(PUSH, expires_at=AT + T(seconds=5)))
    c, tx = center(), Transport()
    n = c.submit(alert(), expiring, AT).notification
    d = c.attempt(n.id, Channel.BROWSER_PUSH, tx, expiring, AT + T(seconds=5))
    assert d.events[-1].code == "withdrawn_subscription_expired" and tx.sent == []


def test_resolver_is_bound_to_tenant_category_and_errors() -> None:  # N2, N3, N4
    p = active(ver())
    c = center()
    n = c.submit(from_proposal(p), ME, AT).notification
    job = c.submit(
        Signal(TENANT, Category.JOB_OUTCOME, "prop-a", "1:active", AT), ME, AT
    )
    with pytest.raises(NotificationError, match="subject_mismatch"):
        c.open(USER, job.notification.id, proposal_resolver(p, cur()), AT)
    foreign = replace(n, tenant_id="tenant-other")
    with pytest.raises(NotificationError, match="subject_mismatch"):
        proposal_resolver(p, cur())(foreign, AT)

    def boom(note: Notification, at: datetime) -> CurrentView | None:
        raise RuntimeError("store down")

    assert {e.state for e in c.feed(USER, boom, AT)} == {"unavailable"}
    assert not any(e.actionable for e in c.feed(USER, boom, AT))
    with pytest.raises(RuntimeError):
        c.open(USER, n.id, boom, AT)
    assert c.get(USER, n.id).seen_at is None
    with pytest.raises(NotificationError, match="time_order"):
        c.open(USER, n.id, nothing, AT - T(seconds=1))


def test_recipient_and_preference_validation() -> None:  # S3, S6, N1, N5
    bad_addresses = ["a@b.invalid\r\nBcc: evil@x.invalid", "a b@x.invalid",
                     "a@b@x.invalid", "a@localhost", "x" * 65 + "@b.invalid",
                     "a@" + "b" * 250 + ".invalid", "é@b.invalid"]  # fmt: skip
    for a in bad_addresses:
        with pytest.raises(NotificationError, match="email_address"):
            replace(ME, email_address=a)
    long_url = "https://x.invalid/" + "a" * 2048
    for url in ["https://evil\r\n", "https://", "https://a b", "ftp://x.invalid",
                "https://x.invalid/\x7f", long_url]:  # fmt: skip
        with pytest.raises(NotificationError, match="endpoint"):
            replace(PUSH, endpoint_origin=url)
    bad: list[dict[str, Any]] = [
        {"in_app": "no"}, {"email": "false"}, {"browser_push": 1},
        {"muted": [(Category.PROPOSAL, Channel.EMAIL)]},
        {"muted": frozenset({("proposal", "email")})},
    ]  # fmt: skip
    for kw in bad:
        with pytest.raises(NotificationError, match="malformed"):
            Preferences(**kw)
    with pytest.raises(NotificationError, match="malformed"):
        replace(PUSH, permission="yes")
    with pytest.raises(NotificationError, match="malformed"):
        replace(ME, email_verified="true")  # type: ignore[arg-type]
    with pytest.raises(NotificationError, match="whole day"):
        Preferences(quiet_hours=("00:00-12:00", "12:00-00:00"))
    with pytest.raises(NotificationError, match="id_key"):
        Center(TENANT, T(minutes=10), 3, b"short")
    other = Center(TENANT, T(minutes=10), 3, b"synthetic-id-key-0002")
    assert other.submit(alert(), ME, AT).notification.id != (
        center().submit(alert(), ME, AT).notification.id
    )


def test_submit_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:  # review S4
    c, calls = center(), [0]
    real = N._refusal

    def flaky(ch: Channel, cat: Category, r: Recipient, at: datetime) -> str:
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("mid-loop")
        return real(ch, cat, r, at)

    monkeypatch.setattr(N, "_refusal", flaky)
    with pytest.raises(RuntimeError):
        c.submit(alert(), ME, AT)
    assert c.feed(USER, nothing, AT) == ()
    retry = c.submit(alert(), ME, AT + T(seconds=1))
    assert retry.created and len(retry.deliveries) == 2


def test_safety_external_bypasses_quiet_hours_and_coalescing() -> None:  # D1
    quiet = replace(ON, quiet_hours_timezone="America/Toronto",
                    quiet_hours=("22:00-07:00",))  # fmt: skip
    me, at = replace(ME, preferences=quiet), datetime(2026, 10, 10, 7, tzinfo=UTC)
    scope = PauseScope(ScopeKind.TENANT, TENANT)
    c = center()
    for k, minutes in enumerate((0, 2)):
        pause = Pause(f"pause-{k}", scope, PauseTrigger.USER, at, "flag-1", False)
        sub = c.submit(from_pause(pause, TENANT), me, at + T(minutes=minutes))
        assert {d.not_before for d in sub.deliveries} == {at + T(minutes=minutes)}
        assert len(sub.deliveries) == 2
    same = Pause("pause-0", scope, PauseTrigger.USER, at, "flag-1", False)
    resent = replace(from_pause(same, TENANT), version="resumed")
    assert len(c.submit(resent, me, at + T(minutes=3)).deliveries) == 2
    plain = c.submit(alert(), me, at)  # a non-safety item still waits
    assert {d.not_before for d in plain.deliveries} == {at + T(hours=4)}
