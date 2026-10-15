"""`PATCH /api/v1/notifications/preferences` (setNotificationPreferences; T056
increment 4) and its effect on `GET /api/v1/notifications`, through TestClient on a
migrated PostgreSQL 16 database as qw_app. SYNTHETIC tenants, users and records
(test_notifications_api.seed); times follow the database clock. Bodies are
validated strictly against the contract `NotificationPreferences`."""

from __future__ import annotations

import importlib.util
import re
import smtplib
import socket
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain import notifications as dn

ROOT = Path(__file__).parents[3]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nt = _module("qw_ntf_api_tests", ROOT / "apps/api/tests/test_notifications_api.py")
api = nt.api
pg_admin_url, database_url, conn = api.pg_admin_url, api.database_url, api.conn
runtime_urls, app_conn, world = api.runtime_urls, api.app_conn, api.world
login, assert_problem, csrf = api.login, api.assert_problem, api.csrf
URL = "/api/v1/notifications/preferences"
SCHEMA = nt.OPENAPI["components"]["schemas"]["NotificationPreferences"]
QUIET = {"in_app": False, "email": False, "browser_push": False,
         "quiet_hours_timezone": "America/Toronto",
         "quiet_hours": ["22:00-07:00", "12:00-13:00"],
         "redact_financial_values": True}  # fmt: skip
DEFAULTS = QUIET | {"in_app": True, "quiet_hours_timezone": "UTC", "quiet_hours": []}


def patch(w: Any, body: Any, match: str | None, key: str | None = None) -> Any:
    headers = {**csrf(w.client, key), **({"If-Match": match} if match else {})}
    return w.client.patch(URL, json=body, headers=headers)


def ok(response: Any, body: dict[str, Any], etag: str) -> None:
    assert response.status_code == 200, response.text
    got = response.json()
    v = Draft202012Validator(SCHEMA)
    assert [e.message for e in v.iter_errors(got)] == []
    assert got == body and response.headers["etag"] == etag


def revisions(w: Any) -> int:
    return int(w.db.execute("SELECT count(*) FROM app.notification_preference")
               .fetchone()[0])  # fmt: skip


@pytest.mark.db
def test_preferences_follow_if_match_and_idempotency(
    world: Any,
) -> None:
    login(world.client, "bob")
    assert_problem(patch(world, QUIET, None), 428, "if_match_required")
    for stale in ('"1"', 'W/"0"', "0", '"00"', "*", '"0", "1"'):
        assert_problem(patch(world, QUIET, stale), 412, "version_mismatch")
    first = patch(world, QUIET, '"0"', "SYNTHETIC-pref-k-01")  # nothing recorded: 0
    ok(first, QUIET, '"1"')
    replay = patch(world, QUIET, '"0"', "SYNTHETIC-pref-k-01")
    ok(replay, QUIET, '"1"')
    assert replay.content == first.content and "idempotent-replayed" in replay.headers
    assert_problem(patch(world, DEFAULTS, '"0"', "SYNTHETIC-pref-k-01"), 409,
                   "idempotency_key_reused")  # fmt: skip
    assert_problem(patch(world, DEFAULTS, '"0"'), 412, "version_mismatch")
    ok(patch(world, DEFAULTS, '"1"', "SYNTHETIC-pref-k-02"), DEFAULTS, '"2"')
    replay = patch(world, QUIET, '"0"', "SYNTHETIC-pref-k-01")  # its own revision
    ok(replay, QUIET, '"1"')
    # Refused, writing nothing: external channels without step-up (owner decision
    # 4), detail exposure, malformed windows and zones, wrong types, unknown fields.
    for name in ("email", "browser_push"):
        got = assert_problem(patch(world, DEFAULTS | {name: True}, '"2"'), 401,
                             "step_up_required")  # fmt: skip
        assert got["detail"] == "Re-authenticate to continue."
    assert_problem(patch(world, DEFAULTS | {"redact_financial_values": False},
                         '"2"'), 422, "detail_exposure_unsupported")  # fmt: skip
    for field, value in (("quiet_hours", ["7:00-8:00"]),
                         ("quiet_hours", ["22:00-22:00"]),
                         ("quiet_hours", ["00:00-12:00", "12:00-00:00"]),
                         ("quiet_hours", ["22:00-07:00"] * 9),
                         ("quiet_hours_timezone", "Mars/Olympus_Mons"),
                         ("quiet_hours_timezone", "../etc/passwd"),
                         ("quiet_hours_timezone", "UTC\n"),
                         ("in_app", "false"), ("email", 0)):  # fmt: skip
        got = assert_problem(patch(world, DEFAULTS | {field: value}, '"2"'), 400,
                             "invalid_request")  # fmt: skip
        assert got["field_errors"][0]["pointer"].startswith(f"/{field}")
    for body in ({**DEFAULTS, "tenant_id": "x"}, {**DEFAULTS, "muted": []},
                 {k: v for k, v in DEFAULTS.items() if k != "in_app"}):  # fmt: skip
        assert_problem(patch(world, body, '"2"'), 400, "invalid_request")
    no_csrf = world.client.patch(URL, json=DEFAULTS, headers={
        "If-Match": '"2"', "Idempotency-Key": "SYNTHETIC-pref-k-09"})  # fmt: skip
    assert_problem(no_csrf, 403, "csrf_failed")
    no_key = {"X-CSRF-Token": csrf(world.client)["X-CSRF-Token"], "If-Match": '"2"'}
    assert_problem(world.client.patch(URL, json=DEFAULTS, headers=no_key), 400,
                   "invalid_request")  # fmt: skip
    # after the idempotency purge (T025) the same request replays: no new audit
    world.db.execute("DELETE FROM app.idempotency_record")
    ok(patch(world, DEFAULTS, '"1"', "SYNTHETIC-pref-k-02"), DEFAULTS, '"2"')
    assert revisions(world) == 2
    audits = world.db.execute("SELECT count(*) FROM app.audit_event WHERE action = "
                              "'notification.preferences_set'").fetchone()  # fmt: skip
    assert audits == (2,)
    # each user has their own revisions; no tenant or user can be named
    login(world.client, "alice")
    assert_problem(patch(world, QUIET, '"2"'), 412, "version_mismatch")
    ok(patch(world, QUIET, '"0"', "SYNTHETIC-pref-k-01"), QUIET, '"1"')
    login(world.client, "carol")
    ok(patch(world, QUIET, '"0"', "SYNTHETIC-pref-k-01"), QUIET, '"1"')
    world.client.cookies.clear()
    assert_problem(world.client.patch(URL, json=QUIET, headers={"If-Match": '"0"'}),
                   401, "unauthenticated")  # fmt: skip
    assert revisions(world) == 4


@pytest.mark.db
def test_the_feed_honours_in_app_and_fails_closed_on_tamper(
    world: Any, app_conn: Any, runtime_urls: dict[str, str]
) -> None:
    ids, *_ = nt.seed(world, app_conn, runtime_urls)
    pause = next(k for k in ids if k not in ("job-syn-1", "prop-syn-n"))
    login(world.client, "alice")  # alice turning the feed off does not touch bob's
    ok(patch(world, QUIET, '"0"'), QUIET, '"1"')
    login(world.client, "bob")
    everything, _ = nt.walk(world.client, 50)
    assert len(everything) == 6
    ok(patch(world, QUIET, '"0"'), QUIET, '"1"')
    for limit in (1, 2, 50):  # the in-app rows go, except the safety category
        items, _ = nt.walk(world.client, limit)
        assert [(i["object_id"], i["channel"]) for i in items] == [
            ("job-syn-1", "email"), (pause, "in_app"), (pause, "email"),
            ("prop-syn-n", "email")]  # fmt: skip
    ok(patch(world, DEFAULTS, '"1"', "SYNTHETIC-pref-k-03"), DEFAULTS, '"2"')
    assert nt.walk(world.client, 50)[0] == everything  # nothing was lost
    nt.tamper(world, "UPDATE app.notification_preference SET quiet_hours_timezone ="
              " 'Europe/Paris' WHERE revision = 2")  # fmt: skip
    body = world.client.get(nt.URL).json()
    nt.conforms(body)
    assert [(i["object_id"], i["channel"]) for i in body["items"]] == [
        ("job-syn-1", "email"), (pause, "in_app"), (pause, "email"),
        ("prop-syn-n", "email")]  # fmt: skip
    assert body["coverage"]["status"] == "partial"
    assert "unavailable:notification_preferences/integrity" in body["coverage"][
        "reasons"]  # fmt: skip
    assert_problem(patch(world, QUIET, '"2"'), 409, "integrity")
    # a stored replay is served, but its ETag lookup fails closed, never a 500
    replay = patch(world, DEFAULTS, '"1"', "SYNTHETIC-pref-k-03")
    assert_problem(replay, 409, "integrity")
    assert revisions(world) == 3
    login(world.client, "alice")  # one user's tampered row does not affect another
    assert world.client.get(nt.URL).json()["coverage"] == {"status": "complete",
                                                           "reasons": []}  # fmt: skip


def age_step_up(w: Any, user: str, by: timedelta) -> None:
    nt.tamper(w, f"UPDATE app.session SET created_at = least(created_at, now() - "
              f"interval '{by.total_seconds()} seconds'), step_up_at = now() - "
              f"interval '{by.total_seconds()} seconds' WHERE user_id = "
              f"'{w.users[user]}'")  # fmt: skip


@pytest.mark.db
def test_external_channels_need_step_up_and_nothing_is_sent(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 4: enabling email or push needs a step-up within the window;
    keeping or disabling one does not. No transport exists, nothing dispatches."""
    calls: list[str] = []

    def record(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"{name} invoked")

        return call

    monkeypatch.setattr(dn.Center, "attempt", record("Center.attempt"))
    monkeypatch.setattr(smtplib, "SMTP", record("smtplib.SMTP"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", record("smtplib.SMTP_SSL"))
    monkeypatch.setattr(socket, "create_connection", record("socket"))
    monkeypatch.setattr(socket.socket, "connect", record("socket.connect"))
    nt.keys(world, nt.KEY)
    login(world.client, "bob")
    email = DEFAULTS | {"email": True}
    both = email | {"browser_push": True}
    got = assert_problem(patch(world, email, '"0"', "SYNTHETIC-pref-k-11"), 401,
                         "step_up_required")  # fmt: skip
    assert got["detail"] == "Re-authenticate to continue." and revisions(world) == 0
    assert api.step_up(world.client).status_code == 200
    first = patch(world, email, '"0"', "SYNTHETIC-pref-k-11")  # the same key retried
    ok(first, email, '"1"')
    replay = patch(world, email, '"0"', "SYNTHETIC-pref-k-11")
    assert replay.content == first.content and replay.headers["etag"] == '"1"'
    stored = world.db.execute("SELECT step_up_at FROM app.notification_preference "
                              "WHERE revision = 1").fetchone()  # fmt: skip
    assert stored is not None and stored[0] is not None
    body = world.client.get(nt.URL).json()
    assert body["coverage"] == {"status": "partial", "reasons": [
        "unavailable:delivery/email:no_transport"]}  # fmt: skip
    age_step_up(world, "bob", timedelta(hours=2))  # expired
    assert_problem(patch(world, both, '"1"'), 401, "step_up_required")
    with api.client_for(world.url, step_up_window=timedelta(microseconds=1)) as strict:
        login(strict, "bob")  # the API's window, narrower than the database's
        assert api.step_up(strict).status_code == 200
        headers = {**api.csrf(strict), "If-Match": '"1"'}
        assert_problem(strict.patch(URL, json=both, headers=headers), 401,
                       "step_up_required")  # fmt: skip
    assert revisions(world) == 1
    # keeping email on and turning it off need no step-up
    keep = email | {"quiet_hours": ["22:00-07:00"]}
    ok(patch(world, keep, '"1"', "SYNTHETIC-pref-k-12"), keep, '"2"')
    ok(patch(world, DEFAULTS, '"2"', "SYNTHETIC-pref-k-13"), DEFAULTS, '"3"')
    assert world.client.get(nt.URL).json()["coverage"]["status"] == "complete"
    assert_problem(patch(world, both, '"3"'), 401, "step_up_required")
    assert api.step_up(world.client).status_code == 200
    ok(patch(world, both, '"3"', "SYNTHETIC-pref-k-14"), both, '"4"')
    assert world.client.get(nt.URL).json()["coverage"]["reasons"] == [
        "unavailable:delivery/email:no_transport",
        "unavailable:delivery/browser_push:no_transport"]  # fmt: skip
    assert revisions(world) == 4 and calls == []


def test_no_source_module_can_dispatch_externally() -> None:
    """No transport exists (adding one needs owner authorisation): outside the
    domain's `Transport` protocol and `Center.attempt`, no source names them, and
    none imports a mail or push client."""
    sources = sorted([*ROOT.glob("apps/*/src/**/*.py"),
                      *ROOT.glob("packages/*/src/**/*.py")])  # fmt: skip
    assert len(sources) > 50
    domain = ROOT / "packages/domain/src/qw_domain/notifications.py"
    pattern = re.compile(r"\b(smtplib|aiosmtplib|webpush|pywebpush|Transport)\b"
                         r"|\.attempt\(|\.send\(")  # fmt: skip
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in sources if p != domain
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if pattern.search(line)]  # fmt: skip
    assert hits == []
