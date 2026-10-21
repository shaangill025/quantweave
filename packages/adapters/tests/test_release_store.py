"""0029 and qw_adapters.release_store (qw_worker): signed releases and a
deployment's install, rollback and pause events persist, replay through the domain
`DeploymentLedger` and fail closed. The superuser `conn` observes or tampers.
SYNTHETIC fixtures and test-only HMAC keys (test_release); no real key or deploy."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
import pytest
import test_release as tr
from qw_adapters import release_store as rs
from qw_domain import release as rl
from test_improvement import proto

COMP, KEY, KEYS, M1, M2, M3, NO, REL = (tr.COMP, tr.KEY, tr.KEYS, tr.M1, tr.M2,
                                        tr.M3, tr.NO, tr.REL)  # fmt: skip
USER_A, USER_B, accepted, approval, signed = (tr.USER_A, tr.USER_B, tr.accepted,
                                              tr.approval, tr.signed)  # fmt: skip
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
DEP = rl.Deployment("deploy-synth", rl.Edition.SELF_HOSTED,
                    frozenset({"operator-synth", "maint-synth"}),
                    frozenset({USER_A, USER_B}), 1, True)  # fmt: skip
RELEASE_COLUMNS = ("release_id, component, target, candidate_id, candidate_hash, "
                   "verdict_id, manifest_hash, approver_id, approved_at, min_schema, "
                   "max_schema, writes_schema, key_id, algorithm, signature, body, "
                   "recorded_at, event_hash")  # fmt: skip
TABLES = ("release_deployment", "signed_release", "deployment_event")
STRAT = {"rel-1": M1, "rel-2": M2, "rel-3": M3}


def record(w: Conn, sr: rl.SignedRelease, reg: Any = None, keys: Any = KEYS) -> bool:
    return rs.record_release(w, reg or accepted(), sr, STRAT[sr.manifest.release_id],
                             keys=keys, allow_test_keys=True)  # fmt: skip


def setup(w: Conn, *rels: rl.SignedRelease) -> None:
    rs.record_deployment(w, DEP)
    for sr in rels:
        assert record(w, sr)


def install(w: Conn, rid: str, n: int = 0, **kw: Any) -> rl.DeploymentLedger:
    args = {"operator_id": "operator-synth", "step_up": f"stepup-op-{n}"} | kw
    return rs.install(w, DEP.deployment_id, KEYS, f"ev-{n}", rid, strategies=NO,
                      **args)  # fmt: skip


def rollback(w: Conn, **kw: Any) -> rl.DeploymentLedger:
    return rs.rollback(w, DEP.deployment_id, KEYS, "ev-rb", COMP, "operator-synth",
                       "stepup-op-rb", strategies=NO, **kw)  # fmt: skip


def count(c: Conn, table: str = "deployment_event") -> int:
    row = c.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    assert row is not None
    return int(row[0])


def test_compatible_rollback_persists_and_replays(worker_conn: Conn) -> None:
    one, two = signed(), signed("rel-2", (1, 2, 2))
    setup(worker_conn, one, two)
    empty = rs.load(worker_conn, DEP.deployment_id, KEYS)
    assert empty is not None and (empty.deployment, empty.entries) == (DEP, ())
    install(worker_conn, "rel-1")
    lg = install(worker_conn, "rel-2", 1)
    back = rollback(worker_conn)
    assert back.entries[:2] == lg.entries  # nothing earlier is rewritten
    rb = back.entries[-1]
    assert isinstance(rb, rl.Rollback) and (rb.from_release_id, rb.to_release_id) == (
        "rel-2", "rel-1")  # fmt: skip
    got = rs.load(worker_conn, DEP.deployment_id, KEYS)
    assert got is not None and got.entries == back.entries
    assert (got.head(COMP), got.schema) == ("rel-1", 2)  # never downgraded
    assert [e.at for e in got.entries] == sorted(e.at for e in got.entries)
    assert got.entries[0].at > REL  # the database clock, not the caller's


def test_unsupported_rollback_pauses_and_the_pause_persists(worker_conn: Conn) -> None:
    setup(worker_conn, signed(compat=(1, 1, 1)), signed("rel-2", (1, 2, 2)))
    install(worker_conn, "rel-1")
    install(worker_conn, "rel-2", 1)
    p = rollback(worker_conn).entries[-1]
    assert isinstance(p, rl.Pause) and p.reasons == ("rel-1:schema_incompatible:2",)
    got = rs.load(worker_conn, DEP.deployment_id, KEYS)
    assert got is not None and got.entries[-1] == p and got.head(COMP) == "rel-2"
    assert got.runnable("rel-2", p.at) == ["rel-1:schema_incompatible:2"]
    with pytest.raises(rs.ReleaseStoreError, match="rollback_target"):
        rollback(worker_conn, to_release_id="rel-1")


def test_a_release_is_rederived_from_the_registry(worker_conn: Conn) -> None:
    sr = signed()
    m = sr.manifest
    reg = accepted()
    forged = {  # each re-signed with the trusted test key: only re-derivation stops it
        "manifest_mismatch": replace(m, rollback_version_id="strat-v9"),
        "self_approval": replace(m, approval=approval(who="optimizer-synth")),
        "time_order": replace(m, approval=approval(at=REL - timedelta(days=2))),
    }
    for code, bad in forged.items():
        with pytest.raises(rs.ReleaseStoreError, match=code):
            record(worker_conn, rl.sign_release(bad, KEY), reg)
    with pytest.raises(rs.ReleaseStoreError, match="tenant_scope"):
        record(worker_conn, sr, accepted(proto()))  # R039
    with pytest.raises(rs.ReleaseStoreError, match="manifest_hash"):
        record(worker_conn, replace(sr, manifest_hash="0" * 64), reg)
    for code, bad_sr, keys in (
        ("bad_signature", replace(sr, signature="0" * 64), KEYS),
        ("untrusted_key", sr, {}),
    ):  # review F1
        with pytest.raises(rs.ReleaseStoreError) as refused:
            record(worker_conn, bad_sr, reg, keys)  # never squats the release id
        assert refused.value.code == code
    assert count(worker_conn, "signed_release") == 0
    assert record(worker_conn, sr, reg)
    assert not record(worker_conn, sr, reg)  # the same: no-op
    other = rl.sign_release(replace(m, compatibility=rl.Compatibility(1, 3, 1)), KEY)
    with pytest.raises(rs.ReleaseStoreError, match="release_conflict"):
        record(worker_conn, other, reg)


def test_the_deployment_is_frozen(worker_conn: Conn, conn: Conn) -> None:
    rs.record_deployment(worker_conn, DEP)
    raw = ("INSERT INTO app.release_deployment SELECT 'deploy-{}', edition, '{}', "
           "tenants, initial_schema, allows_test_keys, clock_timestamp(), event_hash "
           "FROM app.release_deployment")  # fmt: skip
    for ops in ("{op-b,op-a}", "{op-a,op-a}"):  # review F4: sorted and unique
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(raw.format(1, ops))
    conn.execute(raw.format(2, "{op-a,op-b}"))  # the control
    rs.record_deployment(worker_conn, DEP)  # the same: no-op
    for bad in (replace(DEP, operators=frozenset({"operator-other"})),
                replace(DEP, allows_test_keys=False)):  # fmt: skip
        with pytest.raises(rs.ReleaseStoreError, match="deployment_frozen"):
            rs.record_deployment(worker_conn, bad)
    assert count(worker_conn, "release_deployment") == 2


def test_unknown_ids_and_refused_acts_write_nothing(worker_conn: Conn) -> None:
    assert rs.load(worker_conn, "deploy-none", KEYS) is None
    with pytest.raises(rs.ReleaseStoreError, match="not_found") as unknown_dep:
        rs.install(worker_conn, "deploy-none", KEYS, "ev-0", "rel-1",
                   "operator-synth", "stepup-op-0", strategies=NO)  # fmt: skip
    setup(worker_conn, signed())
    with pytest.raises(rs.ReleaseStoreError, match="not_found") as unknown_rel:
        install(worker_conn, "rel-9")
    assert str(unknown_dep.value) == str(unknown_rel.value)  # the same answer
    refusals: dict[str, dict[str, Any]] = {"not_operator": {"operator_id": USER_A},
                "step_up_reused": {"step_up": "stepup-maint-rel-1"},
                "separation_of_duties": {"operator_id": "maint-synth"}}  # fmt: skip
    for code, kw in refusals.items():
        with pytest.raises(rs.ReleaseStoreError) as refused:
            install(worker_conn, "rel-1", **kw)
        assert refused.value.code == code  # the domain's code is kept
    with pytest.raises(rs.ReleaseStoreError, match="unverified"):
        rs.install(worker_conn, DEP.deployment_id, {}, "ev-0", "rel-1",
                   "operator-synth", "stepup-op-0", strategies=NO)  # fmt: skip
    with pytest.raises(rs.ReleaseStoreError, match="nothing_installed"):
        rollback(worker_conn)
    assert count(worker_conn) == 0
    install(worker_conn, "rel-1")
    with pytest.raises(rs.ReleaseStoreError, match="release_conflict"):
        install(worker_conn, "rel-1", 1)
    assert count(worker_conn) == 1


def copy_event(src: str, **over: str) -> str:
    """An INSERT copying the latest event at `src` with the named columns replaced."""
    cols = ["deployment_id", "seq", "event_id", "kind", "component", "release_id",
            "to_release_id", "reasons", "operator_id", "step_up_receipt_id", "at",
            "schema_after", "event_hash"]  # fmt: skip
    over = {"seq": "seq + 1", "event_id": "'ev-x'", "step_up_receipt_id": "'su-x'",
            "at": "clock_timestamp()"} | over  # fmt: skip
    return (f"INSERT INTO app.deployment_event ({', '.join(cols)}) SELECT "
            f"{', '.join(over.get(c, c) for c in cols)} FROM app.deployment_event "
            f"WHERE {src}")  # fmt: skip


def test_each_database_rule_refuses_a_raw_insert(worker_conn: Conn, conn: Conn) -> None:
    setup(worker_conn, signed(compat=(1, 1, 1)), signed("rel-2", (1, 2, 2)),
          signed("rel-3", (2, 2, 2)))  # fmt: skip
    install(worker_conn, "rel-1")

    def refused(*sqls: str, error: type[Exception] = psycopg.errors.CheckViolation
                ) -> None:  # fmt: skip
        for sql in sqls:  # as the owner, so only the database applies
            with pytest.raises(error):
                conn.execute(sql)

    refused(copy_event("seq = 1", release_id="'rel-3'", schema_after="2"))  # min 2
    install(worker_conn, "rel-2", 1)
    last = "seq = 2"

    def rb(**kw: str) -> dict[str, str]:
        return {"kind": "'rollback'", "to_release_id": "'rel-1'"} | kw

    refused(
        copy_event(last, seq="seq + 2"),  # a gap in the sequence
        copy_event(last, at="clock_timestamp() + interval '2 min'"),  # not the clock
        copy_event(last, at="clock_timestamp() - interval '2 min'"),
        copy_event(last, release_id="'rel-3'", at="at - interval '1 ms'"),  # order
        copy_event(last, release_id="'rel-3'", schema_after="1"),  # downgrade
        copy_event(last, release_id="'rel-3'", operator_id="'operator-other'"),
        copy_event(last, release_id="'rel-3'", component="'strategy:other'"),
        copy_event(last, release_id="'rel-3'", operator_id="'maint-synth'"),  # signer
        copy_event(last, **rb()),  # rel-1 cannot run on schema 2
        copy_event(last, **rb(release_id="'rel-1'", to_release_id="'rel-2'")),
        copy_event(last, **rb(to_release_id="'rel-3'")),  # never installed
        copy_event(last, **rb(to_release_id="'rel-2'")),  # to itself
        copy_event(last, kind="'pause'", reasons="'{why}'", release_id="'rel-1'"),
        copy_event(last, kind="'pause'"),  # a pause without reasons
        copy_event(last, reasons="'{why}'"),  # an install with reasons
    )
    refused(copy_event(last),  # rel-2 installed twice
            copy_event(last, release_id="'rel-3'", step_up_receipt_id="'stepup-op-0'"),
            error=psycopg.errors.UniqueViolation)  # fmt: skip

    def copy_release(rid: str, patch: str, approved: str = "approved_at") -> str:
        cols = RELEASE_COLUMNS.replace("release_id, ", f"'{rid}', ", 1).replace(
            "body", f"body || {patch}").replace("approved_at", approved).replace(
            "recorded_at", "clock_timestamp()")  # fmt: skip
        return (f"INSERT INTO app.signed_release ({RELEASE_COLUMNS}) SELECT {cols} "
                "FROM app.signed_release WHERE release_id = 'rel-2'")  # fmt: skip

    fresh = ("jsonb_build_object('release_id', 'rel-z', 'approval', jsonb_set(body "
             "-> 'approval', '{1}', '\"stepup-maint-rel-z\"'))")  # fmt: skip
    late = copy_release("rel-z", fresh, "clock_timestamp()")
    no_maintainer = "'required_approvals', '[]'::jsonb, 'approval'"
    refused(late.replace("'approval'", no_maintainer, 1))
    conn.execute(late)  # control: approved just now
    for rid, step in (("rel-q", "stepup-op-0"), ("rel-r", "stepup-maint-rel-1")):
        conn.execute(copy_release(rid, f"jsonb_build_object('release_id', '{rid}', "
                     f"'approval', jsonb_set(body -> 'approval', '{{1}}', "
                     f"'\"{step}\"'))"))  # fmt: skip
    refused(  # review F2: each step-up authorises one act, maintainer's included
        copy_event(last, release_id="'rel-q'"),  # its approval is an event's step-up
        copy_event(last, release_id="'rel-r'"),  # or another release's approval
        *(copy_event(last, release_id="'rel-3'", step_up_receipt_id=f"'{s}'")
          for s in ("stepup-maint-rel-1", "stepup-maint-rel-3")),  # signers' steps
    )  # fmt: skip
    refused(copy_event(last, release_id="'rel-z'", at="at"))  # before its approval
    conn.execute(copy_event(last, release_id="'rel-3'"))  # control: an install
    back = rb(event_id="'ev-y'", step_up_receipt_id="'su-y'", to_release_id="'rel-2'")
    refused(copy_event("seq = 3", **back | {"schema_after": "1"}))  # downgrade
    conn.execute(copy_event("seq = 3", **back))  # control: rel-3 is withdrawn
    refused(copy_event("seq = 4", **rb(event_id="'ev-z'", step_up_receipt_id="'su-z'",
                       release_id="'rel-2'", to_release_id="'rel-3'")))  # fmt: skip
    conn.execute(copy_event("seq = 4", kind="'pause'", reasons="'{why}'",
                            to_release_id="NULL", release_id="'rel-2'",
                            event_id="'ev-w'",
                            step_up_receipt_id="'su-w'"))  # fmt: skip
    body = (
        "body || jsonb_build_object('release_id', 'rel-x', 'target', "
        "'application_code', 'component', 'application_code:application_code')"
    )
    rest = RELEASE_COLUMNS.split(", ", 3)[3].replace("body", body)
    refused(  # R076: never a code or optimizer release
        f"INSERT INTO app.signed_release ({RELEASE_COLUMNS}) SELECT 'rel-x', "
        "'application_code:application_code', 'application_code', "
        f"{rest.replace('recorded_at', 'clock_timestamp()')} "
        "FROM app.signed_release WHERE release_id = 'rel-1'"
    )
    assert count(conn) == 5


def test_installation_scope_rls_append_only_and_verified_reads(
    worker_conn: Conn, app_conn: Conn, conn: Conn
) -> None:
    setup(worker_conn, signed(), signed("rel-2", (1, 2, 2)))
    install(worker_conn, "rel-1")
    rows = conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity, has_table_privilege("
        "'qw_app', oid, 'SELECT, INSERT'), has_any_column_privilege('qw_app', oid, "
        "'SELECT') FROM pg_class WHERE relnamespace = 'app'::regnamespace AND "
        "relname = ANY (%s)", (list(TABLES),)
    ).fetchall()  # fmt: skip
    assert sorted(rows) == sorted((t, True, True, False, False) for t in TABLES)
    for t in TABLES:  # tenants see no installation's adoption or tenant list
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app_conn.execute(f"SELECT 1 FROM app.{t}")
        for sql in (f"UPDATE app.{t} SET recorded_at = recorded_at"
                    if t != "deployment_event" else f"UPDATE app.{t} SET at = at",
                    f"DELETE FROM app.{t}"):  # fmt: skip
            with pytest.raises(psycopg.Error):
                conn.execute(sql)  # even the owner

    def raw(sql: str) -> None:  # committed, triggers off (replica role)
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(sql)

    tampers = (
        ("release_deployment", "initial_schema = 2"),
        ("release_deployment", "operators = '{operator-synth}'"),
        ("release_deployment", "operators = operators || operators"),  # duplicates
        ("signed_release", "signature = repeat('0', 64)"),
        ("signed_release", "approved_at = approved_at - interval '1 day'"),
        ("signed_release", "body = body || '{\"extra\": 1}'"),  # not canonical
        ("signed_release", "body = jsonb_set(body, '{rollback_version_id}', '\"x\"')"),
        ("deployment_event", "operator_id = 'operator-other'"),
        ("deployment_event", "seq = 5"),
    )
    for table, change in tampers:
        conn.execute(f"CREATE TABLE public.keep AS SELECT * FROM app.{table}")
        raw(f"UPDATE app.{table} SET {change}")
        with pytest.raises(rs.ReleaseStoreError, match="integrity"):
            rs.load(worker_conn, DEP.deployment_id, KEYS)
        raw(f"DELETE FROM app.{table}")
        raw(f"INSERT INTO app.{table} SELECT * FROM public.keep")
        conn.execute("DROP TABLE public.keep")
    revoked = {KEY.key_id: replace(KEYS[KEY.key_id], revoked_at=REL)}
    with pytest.raises(rs.ReleaseStoreError, match="integrity"):
        rs.load(worker_conn, DEP.deployment_id, revoked)  # replay fails closed (N6)
    got = rs.load(worker_conn, DEP.deployment_id, KEYS)
    assert got is not None and got.head(COMP) == "rel-1"  # restored and verified


def test_concurrent_acts_queue_on_the_deployment_lock(
    worker_conn: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    # Review F3: both installs read the ledger only after the lock, so both land
    # (seq 1 and 2); without the store's lock the second would fail in the database.
    setup(worker_conn, signed(), signed("rel-2", (1, 2, 2)))
    key = "hashtextextended('release:deploy-synth', 0)"
    conn.execute(f"SELECT pg_advisory_lock({key})")  # held: both acts must queue
    got: dict[str, object] = {}

    def act(rid: str, n: int) -> None:
        with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as c:
            try:
                got[rid] = install(c, rid, n).head(COMP)
            except rs.ReleaseStoreError as exc:
                got[rid] = exc.code

    threads = [threading.Thread(target=act, args=(r, n))
               for n, r in enumerate(("rel-1", "rel-2"))]  # fmt: skip
    for t in threads:
        t.start()
    deadline = time.monotonic() + 30
    waiting = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    while (row := conn.execute(waiting).fetchone()) is not None and row[0] < 2:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    conn.execute(f"SELECT pg_advisory_unlock({key})")
    for t in threads:
        t.join(30)
    assert sorted(map(str, got.values())) == sorted({"rel-1", "rel-2"})
    lg = rs.load(worker_conn, DEP.deployment_id, KEYS)
    assert lg is not None and len(lg.entries) == 2
