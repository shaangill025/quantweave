"""0050 and qw_adapters.release_store for optimizer releases (T050 increment 7;
R076, R092, R099). SYNTHETIC data and test-only HMAC keys only (test_release). The
operator login (qw_release) records a signed optimizer release; qw_worker, under
which the optimizer runs, and qw_assessor cannot write it or any key; every
deployment event naming it is refused (`sandbox_unqualified`) by the domain and by
the database. The superuser `conn` observes and attempts raw writes."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
import test_optimizer_release as tor
import test_release as tr
from qw_adapters import release_store as rs
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
KEYS, NO = tr.KEYS, tr.NO
DEP = rl.Deployment("deploy-synth", rl.Edition.SELF_HOSTED,
                    frozenset({"operator-synth", "maint-synth"}),
                    frozenset({tr.USER_A}), 1, True)  # fmt: skip
RELEASE_COLUMNS = ("release_id, component, target, candidate_id, candidate_hash, "
                   "verdict_id, manifest_hash, approver_id, approved_at, min_schema, "
                   "max_schema, writes_schema, key_id, algorithm, signature, body, "
                   "recorded_at, event_hash, verdict_source")  # fmt: skip


def count(c: Conn, table: str = "deployment_event") -> int:
    row = c.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


def record(c: Conn, sr: rl.SignedRelease) -> bool:  # a T051 strategy release
    return rs.record_release(c, tr.accepted(), sr, tr.M1, keys=KEYS,
                             allow_test_keys=True)  # fmt: skip


def put(c: Conn, sr: rl.SignedRelease | None = None) -> bool:
    return rs.record_release(c, tor.accepted(), sr or tor.signed(), keys=KEYS,
                             allow_test_keys=True)  # fmt: skip


def code(fn: Any) -> str:
    with pytest.raises(rs.ReleaseStoreError) as e:
        fn()
    return e.value.code


def raw_event(rid: str, n: int, kind: str = "install", to: str = "NULL") -> str:
    return ("INSERT INTO app.deployment_event (deployment_id, seq, event_id, kind, "
            "component, release_id, to_release_id, reasons, operator_id, "
            "step_up_receipt_id, at, schema_after, event_hash, self_approved) "
            f"SELECT 'deploy-synth', {n}, 'ev-raw-{n}', '{kind}', component, "
            f"release_id, {to}, '{{}}', 'operator-synth', 'stepup-raw-{n}', "
            "clock_timestamp(), 1, repeat('a', 64), false FROM app.signed_release "
            f"WHERE release_id = '{rid}'")  # fmt: skip


def test_the_operator_records_an_optimizer_release_that_never_installs(
    release_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    rs.record_deployment(release_conn, DEP)
    forged = rl.sign_release(tor.signed().manifest, tr.FORGER)
    assert code(lambda: put(release_conn, forged)) == "bad_signature"
    assert count(conn, "signed_release") == 0  # a forgery never claims the id
    assert put(release_conn) and not put(release_conn)  # written once, then a no-op
    rows = conn.execute("SELECT target, component, approver_id, verdict_source "
                        "FROM app.signed_release").fetchall()  # fmt: skip
    assert rows == [("recursive_optimizer", oi.COMPONENT, "maint-synth",
                     "caller_supplied")]  # decision 33's mark  # fmt: skip
    assert code(lambda: rs.install(worker_conn, DEP.deployment_id, KEYS, "ev-0",
                "rel-opt-1", "operator-synth", "stepup-op-0",
                strategies=NO)) == "sandbox_unqualified"  # the domain  # fmt: skip
    assert count(conn) == 0
    with pytest.raises(psycopg.errors.CheckViolation, match="sandbox_unqualified"):
        conn.execute(raw_event("rel-opt-1", 1))  # the database, even as superuser
    assert record(worker_conn, tr.signed())  # control: a strategy release installs
    conn.execute(raw_event("rel-1", 1))
    for rid, kind, to in (("rel-1", "rollback", "'rel-opt-1'"),
                          ("rel-opt-1", "pause", "NULL")):  # fmt: skip
        gate = pytest.raises(psycopg.errors.CheckViolation, match="sandbox_unqualified")
        with gate, conn.transaction():  # 0029's guard off: the gate alone refuses
            conn.execute("ALTER TABLE app.deployment_event "
                         "DISABLE TRIGGER insert_guard")  # fmt: skip
            conn.execute(raw_event(rid, 2, kind, to))
    assert count(conn) == 1


def test_the_install_gate_fails_closed_on_a_release_it_cannot_read(
    release_conn: Conn, worker_conn: Conn, conn: Conn
) -> None:
    """Review finding 3: as qw_worker, with 0029's guard off, a strategy install
    row lands; hide the release row from qw_worker and the gate refuses it."""
    rs.record_deployment(release_conn, DEP)
    assert record(worker_conn, tr.signed())
    row = ("INSERT INTO app.deployment_event (deployment_id, seq, event_id, kind, "
           "component, release_id, to_release_id, reasons, operator_id, "
           "step_up_receipt_id, at, schema_after, event_hash, self_approved) VALUES "
           f"('deploy-synth', 1, 'ev-raw', 'install', '{tr.COMP}', 'rel-1', NULL, "
           "'{}', 'operator-synth', 'stepup-raw', clock_timestamp(), 1, "
           "repeat('a', 64), false)")  # fmt: skip

    def attempt(hidden: bool) -> None:
        with conn.transaction():
            conn.execute("ALTER TABLE app.deployment_event "
                         "DISABLE TRIGGER insert_guard")  # fmt: skip
            if hidden:
                conn.execute("CREATE POLICY hide ON app.signed_release AS RESTRICTIVE "
                             "FOR SELECT TO qw_worker USING (false)")  # fmt: skip
            conn.execute("SET LOCAL ROLE qw_worker")
            conn.execute(row)
            raise psycopg.Rollback()  # leave nothing behind

    attempt(hidden=False)  # control: the gate admits a readable strategy release
    with pytest.raises(psycopg.errors.CheckViolation, match="sandbox_unqualified"):
        attempt(hidden=True)
    assert count(conn) == 0


def test_only_the_operator_writes_optimizer_releases_and_nobody_else_keys(
    release_conn: Conn, worker_conn: Conn, assessor_conn: Conn, conn: Conn
) -> None:
    rs.record_deployment(release_conn, DEP)
    assert code(lambda: put(worker_conn)) == "database"  # the optimizer's role
    assert code(lambda: put(assessor_conn)) == "database"
    assert code(lambda: record(release_conn, tr.signed())) == "database"  # 0029's
    assert count(conn, "signed_release") == 0
    assert put(release_conn)
    copy = (f"INSERT INTO app.signed_release ({RELEASE_COLUMNS}) SELECT "
            + RELEASE_COLUMNS.replace("release_id, ", "'rel-opt-x', ", 1)
            .replace("recorded_at", "clock_timestamp()")
            + " FROM app.signed_release WHERE release_id = 'rel-opt-1'")  # fmt: skip
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # RLS WITH CHECK
        worker_conn.execute(copy)
    assert record(worker_conn, tr.signed())  # a strategy release, by the worker
    seen = release_conn.execute("SELECT release_id FROM app.signed_release")
    assert seen.fetchall() == [("rel-opt-1",)]  # review finding 2: optimizer rows only
    key = ("INSERT INTO app.release_trusted_key (key_id, principal_id, algorithm, "
           "public_key, test_only, valid_from, recorded_at, event_hash) VALUES "
           "('k-x', 'optimizer-synth', 'hmac-sha256-test', NULL, true, now(), "
           "clock_timestamp(), repeat('a', 64))")  # fmt: skip
    revoke = (
        "INSERT INTO app.release_key_revocation (key_id, reason, revoked_at, "
        "event_hash) VALUES ('k-x', 'x', clock_timestamp(), repeat('a', 64))"
    )
    for c in (worker_conn, assessor_conn):  # R092: no key or revocation
        for sql in (key, revoke):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
    grants = conn.execute(
        "SELECT r, t, has_any_column_privilege(r, 'app.' || t, 'INSERT') FROM unnest("
        "ARRAY['qw_worker', 'qw_assessor', 'qw_app']) r, unnest(ARRAY["
        "'release_trusted_key', 'release_key_revocation']) t ORDER BY 1, 2"
    ).fetchall()  # fmt: skip
    assert {g[2] for g in grants} == {False} and len(grants) == 6
    assert (
        count(conn, "signed_release") == 2 and count(conn, "release_trusted_key") == 0
    )


def test_the_store_never_writes_a_baseline_rollback() -> None:
    e = rl.Rollback("ev-rb", oi.COMPONENT, "rel-opt-1", None, "operator-synth",
                    "stepup-op-rb", tr.INST)  # fmt: skip
    with pytest.raises(rs.ReleaseStoreError, match="baseline_rollback_unstored"):
        rs._event_values("deploy-synth", 2, e, 1)
