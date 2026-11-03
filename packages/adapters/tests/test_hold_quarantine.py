"""Owner decision 115 on PostgreSQL 16: `legal_hold_store.quarantine` (0061) and
`qw-hold-quarantine`. The superuser `conn` corrupts rows (as a storage fault or
an edit would) and runs the repair step; qw_app places and releases holds and
qw_worker loads them. SYNTHETIC tenants, principals, receipts, approvals and
holds; times are the database clock."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.conninfo import make_conninfo
from qw_adapters import hold_quarantine_cli as cli
from qw_adapters import jobs as qj
from qw_adapters import legal_hold_store as hs
from qw_adapters import tenancy as tn
from qw_domain import retention as rt
from qw_domain.jobs import Priority

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
C, R = rt.RetentionClass, rt.HoldRole
WHO = ("owner-synth-1", "approval-synth-1", "storage_fault")


def tenant(app: Conn) -> uuid.UUID:
    with tn.provision_tenant(app) as tx:
        return tx.tenant_id


def call(c: Conn, t: uuid.UUID, fn: str, *args: Any, **kw: Any) -> Any:
    with tn.tenant_transaction(c, t) as tx:
        return getattr(hs, fn)(tx, *args, **kw)


def code(c: Conn, t: uuid.UUID, fn: str, *args: Any, **kw: Any) -> str:
    with pytest.raises(hs.HoldStoreError) as err:
        call(c, t, fn, *args, **kw)
    return err.value.code


def put(app: Conn, t: uuid.UUID, hid: str, ids: tuple[str, ...]) -> rt.LegalHold:
    h, _ = call(app, t, "place", hid, {C.RESEARCH}, ids, "litigation", "counsel-1",
                R.TENANT_OWNER, "owner-synth-1")  # fmt: skip
    assert isinstance(h, rt.LegalHold)
    return h


def both(tag: str) -> tuple[rt.Approval, ...]:
    return (rt.Approval(R.TENANT_OWNER, "owner-synth-1", f"stepup-o-{tag}"),
            rt.Approval(R.OPERATOR, "operator-synth-1", f"stepup-x-{tag}"))  # fmt: skip


def tamper(su: Conn, stmt: str, *args: Any) -> None:
    with su.transaction():
        su.execute("SET LOCAL session_replication_role = replica")
        su.execute(stmt, args)


def held(c: Conn, t: uuid.UUID, rid: str, cls: rt.RetentionClass = C.RESEARCH,
         at: datetime | None = None) -> bool:  # fmt: skip
    holds = call(c, t, "load")
    when = at or datetime(2100, 1, 1, tzinfo=UTC)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    return holds.active(rt.Item(str(t), rid, cls, old), when) is not None


def corrupt(app: Conn, su: Conn) -> uuid.UUID:
    """hold-1 (rec-a) at seq 0, hold-2 (rec-b) at seq 1, hold-1's valid release at
    seq 2; then seq 0's entry hash is overwritten, so seq 0 fails and seq 1's link
    no longer matches. Quarantined, seq 0 lends hold-1 to no release: seq 2 is
    verified and has no effect."""
    t = tenant(app)
    put(app, t, "hold-1", ("rec-a",))
    put(app, t, "hold-2", ("rec-b",))
    call(app, t, "release", "hold-1", "counsel-1", "inquiry_closed", both("r"))
    tamper(su, "UPDATE app.legal_hold_event SET entry_hash = repeat('a', 64) "
           "WHERE tenant_id = %s AND seq = 0", t)  # fmt: skip
    return t


def test_quarantine_defaults_to_the_whole_tenant_until_a_valid_release(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = corrupt(app_conn, conn)
    assert code(worker_conn, t, "load") == "integrity"
    rep = call(conn, t, "quarantine", 0, *WHO)
    assert rep.classes == frozenset(C) and rep.record_ids == frozenset()
    assert (rep.placed_by, rep.principal_id, rep.authority_id) == (
        R.OPERATOR, "owner-synth-1", "approval-synth-1")  # fmt: skip
    audit = conn.execute(
        "SELECT seq, approved_by, approval_ref, reason, quarantined_by = "
        "session_user, quarantined_at <= clock_timestamp(), replacement_hold_id "
        "FROM app.legal_hold_quarantine WHERE tenant_id = %s",
        (t,),
    ).fetchall()
    assert audit == [(0, *WHO, True, True, rep.hold_id)]
    # the tenant loads again; every class and record of it is blocked
    for rid, cls in (("rec-a", C.RESEARCH), ("rec-c", C.FINANCIAL),
                     ("rec-b", C.RESEARCH)):  # fmt: skip
        assert held(worker_conn, t, rid, cls)
    # the quarantined hold's own id can no longer be released through the store
    assert code(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed",
                both("1")) == "not_found"  # fmt: skip
    call(
        app_conn, t, "release", rep.hold_id, "counsel-1", "inquiry_closed", both("2")
    )  # the valid replacement release: elsewhere resumes
    assert not held(worker_conn, t, "rec-c", C.FINANCIAL)
    assert not held(worker_conn, t, "rec-a")
    assert held(worker_conn, t, "rec-b")  # hold-2 is untouched


def test_a_stated_scope_lets_the_rest_of_the_tenant_resume(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = corrupt(app_conn, conn)
    rep = call(conn, t, "quarantine", 0, *WHO, classes={C.RESEARCH},
               record_ids=("rec-a",))  # fmt: skip
    assert held(worker_conn, t, "rec-a") and held(worker_conn, t, "rec-b")
    assert not held(worker_conn, t, "rec-c")
    assert not held(worker_conn, t, "rec-a", C.FINANCIAL)
    put(app_conn, t, "hold-3", ("rec-d",))  # hold actions work again
    assert held(worker_conn, t, "rec-d")
    rows = conn.execute("SELECT classes, record_ids FROM app.legal_hold_event "
                        "WHERE tenant_id = %s AND hold_id = %s",
                        (t, rep.hold_id)).fetchall()  # fmt: skip
    assert rows == [(["research"], ["rec-a"])]


def test_a_quarantined_release_never_unblocks_its_hold(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    """The corrupt row is a release of hold-1 (its approvals edited after the
    fact). Quarantined, it is not replayed, so hold-1 still blocks rec-a, also
    after the replacement is released."""
    t = tenant(app_conn)
    put(app_conn, t, "hold-1", ("rec-a",))
    call(app_conn, t, "release", "hold-1", "counsel-1", "inquiry_closed", both("1"))
    assert not held(worker_conn, t, "rec-a")
    tamper(
        conn,
        "UPDATE app.legal_hold_event SET approval_principals = "
        "'{owner-synth-1,owner-synth-1}' WHERE tenant_id = %s AND seq = 1",
        t,
    )
    assert code(worker_conn, t, "load") == "integrity"
    rep = call(conn, t, "quarantine", 1, *WHO, classes={C.FINANCIAL})
    assert held(worker_conn, t, "rec-a")  # hold-1 is placed and not released
    call(app_conn, t, "release", rep.hold_id, "counsel-1", "inquiry_closed",
         both("2"))  # fmt: skip
    assert held(worker_conn, t, "rec-a") and not held(worker_conn, t, "x", C.FINANCIAL)


def test_quarantine_is_bound_to_the_row_its_scope_and_its_replacement(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = corrupt(app_conn, conn)
    assert code(conn, t, "quarantine", 1, *WHO) == "not_corrupt"  # not the first
    good = tenant(app_conn)
    put(app_conn, good, "hold-1", ("rec-a",))
    assert code(conn, good, "quarantine", 0, *WHO) == "not_corrupt"
    assert code(conn, t, "quarantine", 0, *WHO, classes=()) == "classes"
    rep = call(conn, t, "quarantine", 0, *WHO)
    # the corrupt row edited again after the approval: a fresh approval is needed
    tamper(conn, "UPDATE app.legal_hold_event SET reason = 'edited' WHERE "
           "tenant_id = %s AND seq = 0", t)  # fmt: skip
    assert code(worker_conn, t, "load") == "integrity"
    tamper(conn, "UPDATE app.legal_hold_event SET reason = 'litigation' WHERE "
           "tenant_id = %s AND seq = 0", t)  # fmt: skip
    assert held(worker_conn, t, "rec-c")
    # a quarantine's stated scope narrowed afterwards no longer matches its
    # replacement; a quarantine without its replacement never unblocks
    for col, bad in (("record_ids", ["rec-a"]),
                     ("replacement_hold_id", "quarantine-elsewhere")):  # fmt: skip
        was = conn.execute(f"SELECT {col} FROM app.legal_hold_quarantine WHERE "
                           "tenant_id = %s", (t,)).fetchone()  # fmt: skip
        assert was is not None
        stmt = f"UPDATE app.legal_hold_quarantine SET {col} = %s WHERE tenant_id = %s"
        tamper(conn, stmt, bad, t)
        assert code(worker_conn, t, "load") == "integrity", col
        tamper(conn, stmt, was[0], t)
    assert held(worker_conn, t, "rec-c") and rep.hold_id.startswith("quarantine-0-")


_RAW = (
    "INSERT INTO app.legal_hold_quarantine (tenant_id, seq, row_digest, "
    "next_prev_hash, replacement_hold_id, classes, record_ids, approved_by, "
    "approval_ref, reason) VALUES (%s, 0, repeat('0', 64), repeat('0', 64), "
    "'q-1', '{research}', '{}', 'o-1', 'a-1', 'storage_fault')"
)


def test_only_a_superuser_may_quarantine(app_conn: Conn, worker_conn: Conn,
                                         conn: Conn) -> None:  # fmt: skip
    t = corrupt(app_conn, conn)
    for c in (app_conn, worker_conn):
        assert code(c, t, "quarantine", 0, *WHO) == "forbidden"
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(c, t) as tx:  # fmt: skip
            tx.conn.execute(_RAW, (t,))
    # the owner role, and a superuser that set a role: the guard refuses both
    for how in ("SET LOCAL SESSION AUTHORIZATION qw_migrate",
                "SET LOCAL ROLE qw_migrate"):  # fmt: skip
        with pytest.raises(errors.InsufficientPrivilege, match="superuser only"), \
                conn.transaction():  # fmt: skip
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(t),))
            conn.execute(how)
            conn.execute(_RAW, (t,))
    assert code(worker_conn, t, "load") == "integrity"  # nothing was written
    call(conn, t, "quarantine", 0, *WHO)
    for stmt in (
        "UPDATE app.legal_hold_quarantine SET reason = 'x'",
        "DELETE FROM app.legal_hold_quarantine",
    ):  # append-only
        with pytest.raises(psycopg.Error), conn.transaction():
            conn.execute(stmt)
    other = tenant(app_conn)  # isolation: qw_worker sees no other tenant's rows
    with tn.tenant_transaction(worker_conn, other) as tx:
        n = tx.conn.execute("SELECT count(*) FROM app.legal_hold_quarantine")
        assert n.fetchone() == (0,)


def env(tmp_path: Path, url: str) -> dict[str, str]:
    pw = tmp_path / "quarantine.pgpass"
    pw.write_text("*:*:*:*:SYNTHETIC-unused-under-trust-auth\n")
    pw.chmod(0o600)
    return {cli.URL_VARIABLE: make_conninfo(url, passfile=str(pw))}


def test_the_cli_runs_as_a_superuser_only(
    app_conn: Conn, worker_conn: Conn, conn: Conn, database_url: str,
    runtime_urls: dict[str, str], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    t = corrupt(app_conn, conn)
    args = ["--tenant", str(t), "--seq", "0", "--approved-by", WHO[0],
            "--approval-ref", WHO[1], "--reason", WHO[2]]  # fmt: skip
    assert cli.main(["--check-config"], env(tmp_path, database_url)) == 0
    assert cli.main(args, {}) == 2 and "QW_QUARANTINE" in capsys.readouterr().err
    assert cli.main([*args, "--class", "nonsense"],
                    env(tmp_path, database_url)) == 2  # fmt: skip
    for group in ("qw_app", "qw_worker"):  # a service login is refused
        assert cli.main(args, env(tmp_path, runtime_urls[group])) == 1
        assert "superuser" in capsys.readouterr().err
    assert code(worker_conn, t, "load") == "integrity"
    assert cli.main([*args, "--class", "research", "--record-id", "rec-a"],
                    env(tmp_path, database_url)) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "quarantine-0-" in out and "research" in out
    assert held(worker_conn, t, "rec-a") and not held(worker_conn, t, "rec-c")
    assert cli.main(args, env(tmp_path, database_url)) == 1  # nothing left to do
    assert "not_corrupt" in capsys.readouterr().err


def test_quarantine_requeues_requests_that_failed_on_unknown_hold_state(
        app_conn: Conn, conn: Conn) -> None:  # fmt: skip
    """Decision 115: deletion elsewhere resumes. A request whose latest job failed
    `hold_state_unknown` gets a fresh job with the identical payload."""
    t = corrupt(app_conn, conn)
    body = {"trigger": "request", "request_id": "del-opaque-1"}
    with tn.tenant_transaction(app_conn, t) as tx:
        jid, _ = qj.enqueue(tx, hs.DELETION_KIND, "request:del-opaque-1",
                            Priority.SAFETY, body)  # fmt: skip
    tamper(
        conn,
        "UPDATE app.job SET state = 'failed', last_error = "
        "'hold_state_unknown' WHERE id = %s",
        jid,
    )  # as the loop leaves it
    rep = call(conn, t, "quarantine", 0, *WHO)
    rows = conn.execute(
        "SELECT input_revision, payload FROM app.job WHERE tenant_id = %s ORDER BY "
        "created_at", (t,)).fetchall()  # fmt: skip
    seq = conn.execute("SELECT seq FROM app.legal_hold_event WHERE tenant_id = %s "
                       "AND hold_id = %s", (t, rep.hold_id)).fetchone()  # fmt: skip
    assert seq is not None
    assert rows == [("request:del-opaque-1", body),
                    (f"request:del-opaque-1:q{seq[0]}", body)]  # fmt: skip
