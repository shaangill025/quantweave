"""The `retention.delete` job (T058 PR B) on a real PostgreSQL 16 as the non-owner,
NOBYPASSRLS qw_worker login; qw_app enqueues, places and releases holds. The
deletable store is a SYNTHETIC test-only table (`app.synthetic_note`, research
class) created by the superuser `conn`, which also observes and tampers. Tenants,
notes, holds, principals, receipts and the record key are SYNTHETIC; times are the
database clock. Also `config.record_key_from_env` (decision 107)."""

from __future__ import annotations

import importlib.util
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from qw_adapters import legal_hold_store as hs
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as ts
from qw_adapters.file_secrets import ConfigError, InlineSecretError
from qw_adapters.jobs import ClaimedJob, IdempotencyConflict
from qw_domain import retention as rt
from qw_domain.account_wire import seal
from qw_domain.jobs import Backoff, JobState, Priority
from qw_worker import __main__ as entry
from qw_worker import deletion_job as dj
from qw_worker.config import WorkerSettings, record_key_from_env
from qw_worker.loop import HandlerFailed, Worker

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness_delete", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn, worker_conn = _harness.app_conn, _harness.worker_conn

Conn = psycopg.Connection[Any]
RAW = b"SYNTHETIC-test-record-key-0123456789"  # decision 104, test only
KEY = rt.RecordKey(RAW)
C, R = rt.RetentionClass, rt.HoldRole
POLICY = rt.owner_defaults()
SETTINGS = WorkerSettings(
    "synthetic-delete", job_backoff=Backoff(timedelta(0), timedelta(0))
)
POISON: dict[str, Exception] = {}  # record id -> what its delete raises


def _items(tx: tn.TenantTx, ids: frozenset[str]) -> list[rt.Item]:
    rows = tx.conn.execute("SELECT id, created_at FROM app.synthetic_note WHERE "
                           "cardinality(%s::text[]) = 0 OR id = ANY(%s)",
                           (sorted(ids), sorted(ids))).fetchall()  # fmt: skip
    return [rt.Item(str(tx.tenant_id), i, C.RESEARCH, at) for i, at in rows]


def _delete(tx: tn.TenantTx, it: rt.Item) -> bool:
    if it.record_id in POISON:
        raise POISON[it.record_id]
    cur = tx.conn.execute("DELETE FROM app.synthetic_note WHERE id = %s AND "
                          "created_at = %s", (it.record_id, it.created_at))  # fmt: skip
    return cur.rowcount == 1


TARGETS = {C.RESEARCH: dj.Target(C.RESEARCH, _items, _delete)}


@pytest.fixture
def notes(conn: Conn, runtime_urls: dict[str, str]) -> None:
    POISON.clear()
    conn.execute("CREATE TABLE app.synthetic_note (tenant_id uuid NOT NULL, id text "
                 "NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY "
                 "(tenant_id, id))")  # fmt: skip
    conn.execute("ALTER TABLE app.synthetic_note ENABLE ROW LEVEL SECURITY, FORCE "
                 "ROW LEVEL SECURITY")  # fmt: skip
    conn.execute("CREATE POLICY tenant_isolation ON app.synthetic_note USING "
                 "(tenant_id = app.current_tenant_id())")  # fmt: skip
    conn.execute("GRANT SELECT, DELETE ON app.synthetic_note TO qw_worker")


def tenant(app: Conn, su: Conn, *ids: str) -> uuid.UUID:
    with tn.provision_tenant(app) as tx:
        t = tx.tenant_id
    for i in ids:
        su.execute("INSERT INTO app.synthetic_note VALUES (%s, %s, clock_timestamp() "
                   "- interval '2 days')", (t, i))  # fmt: skip
    return t


def left(su: Conn, t: uuid.UUID) -> list[str]:
    rows = su.execute("SELECT id FROM app.synthetic_note WHERE tenant_id = %s "
                      "ORDER BY id", (t,)).fetchall()  # fmt: skip
    return [r[0] for r in rows]


def tombs(c: Conn, t: uuid.UUID) -> tuple[rt.Tombstone, ...]:
    with tn.tenant_transaction(c, t) as tx:
        return ts.load(tx).entries


def jobs(su: Conn, t: uuid.UUID) -> list[tuple[str, str, str | None, Any]]:
    return [tuple(r) for r in su.execute(
        "SELECT input_revision, state::text, last_error, payload FROM app.job "
        "WHERE tenant_id = %s AND kind = %s ORDER BY created_at, input_revision",
        (t, dj.KIND))]  # fmt: skip


def submit(app: Conn, t: uuid.UUID, rid: str = "del-opaque-1",
           classes: frozenset[rt.RetentionClass] = frozenset({C.RESEARCH, C.FINANCIAL}),
           ago: timedelta = timedelta(seconds=1)) -> ClaimedJob:  # fmt: skip
    with tn.tenant_transaction(app, t) as tx:
        now = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert now is not None
        req = rt.DeletionRequest(rid, str(t), "scope-1", classes, now[0] - ago,
                                 "reauth-1", "SYNTHETIC acknowledgement")  # fmt: skip
        jid, _ = dj.submit_request(tx, req)
    return claimed(jid, t, f"request:{rid}", dj.payload(req))


def claimed(jid: uuid.UUID, t: uuid.UUID, rev: str, p: Any) -> ClaimedJob:
    return ClaimedJob(jid, t, dj.KIND, rev, Priority.SAFETY, p, 1, 5,
                      "synthetic:token",
                      datetime(2026, 10, 31, tzinfo=UTC))  # fmt: skip


def run(c: Conn, job: ClaimedJob, key: rt.RecordKey = KEY, policy: rt.Policy = POLICY,
        snapshot: bool = False) -> str | None:  # fmt: skip
    """The handler in one tenant transaction, like the loop: the failure code or
    None; a failure keeps its effects only when it says so."""
    handle = dj.handlers(key, policy, TARGETS)[dj.KIND]
    try:
        with tn.tenant_transaction(c, job.tenant_id, snapshot=snapshot) as tx:
            try:
                handle(tx, job)
            except HandlerFailed as exc:
                if not exc.keep_effects:
                    raise
                return exc.code
    except HandlerFailed as exc:
        return exc.code
    return None


def hold(app: Conn, t: uuid.UUID, ids: tuple[str, ...], hid: str = "hold-1") -> None:
    with tn.tenant_transaction(app, t) as tx:
        hs.place(tx, hid, {C.RESEARCH}, ids, "litigation", "counsel-1",
                 R.TENANT_OWNER, "owner-synth-1")  # fmt: skip


def release(app: Conn, t: uuid.UUID, hid: str = "hold-1", tag: str = "1") -> None:
    with tn.tenant_transaction(app, t) as tx:
        hs.release(tx, hid, "counsel-1", "inquiry_closed", (
            rt.Approval(R.TENANT_OWNER, "owner-synth-1", f"stepup-o-{tag}"),
            rt.Approval(R.OPERATOR, "operator-synth-1",
                        f"stepup-x-{tag}")))  # fmt: skip


def worker(c: Conn) -> Worker:
    return Worker(c, SETTINGS, handlers=dj.handlers(KEY, POLICY, TARGETS))


def test_request_deletes_marks_backups_unknown_and_retries_idempotently(
        app_conn: Conn, worker_conn: Conn, conn: Conn, notes: None,
        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    t = tenant(app_conn, conn, "rec-a", "rec-b")
    orig = conn.execute("SELECT created_at FROM app.synthetic_note WHERE id = "
                        "'rec-a'").fetchone()  # fmt: skip
    assert orig is not None
    job = submit(app_conn, t)
    with pytest.raises(IdempotencyConflict):  # a later trigger never replaces it
        submit(app_conn, t, ago=timedelta(0))
    calls: list[str] = []  # PR A review M2: holds (shared) before any tombstone lock
    for mod, name in ((hs, "load"), (ts, "append")):
        real = getattr(mod, name)

        def spy(*a: Any, _r: Any = real, _n: str = name, **k: Any) -> Any:
            calls.append(_n)
            return _r(*a, **k)

        monkeypatch.setattr(mod, name, spy)
    for name in ("place", "release"):  # never in the job's transaction
        monkeypatch.setattr(hs, name, lambda *a, **k: pytest.fail("hold write"))
    assert worker(worker_conn).tick().jobs == [(job.id, JobState.SUCCEEDED)]
    assert calls == ["load", "append", "append"]
    assert left(conn, t) == []
    got = tombs(worker_conn, t)
    trigger = datetime.fromisoformat(job.payload["requested_at"])  # type: ignore
    assert len(got) == 2 and {g.requested_at for g in got} == {trigger}
    # decision 114: no catalog, so every tombstone says so (never a bare [])
    assert all(g.backups_unknown and g.residual_backups == () for g in got)
    assert run(worker_conn, job) is None and tombs(worker_conn, t) == got  # a retry
    conn.execute("INSERT INTO app.synthetic_note VALUES (%s, 'rec-a', %s)",
                 (t, orig[0]))  # a SYNTHETIC restored copy  # fmt: skip
    assert run(worker_conn, job) is None  # deleted again under its first tombstone
    assert left(conn, t) == [] and tombs(worker_conn, t) == got


def test_a_release_requeues_suspended_requests_with_their_first_time(
        app_conn: Conn, worker_conn: Conn, conn: Conn,
        notes: None) -> None:  # owner decision 113  # fmt: skip
    t = tenant(app_conn, conn, "rec-a", "rec-b")
    hold(app_conn, t, ("rec-a",))
    job = submit(app_conn, t)
    w = worker(worker_conn)
    assert w.tick().jobs == [(job.id, JobState.FAILED)]
    assert jobs(conn, t)[0][1:3] == ("failed", "deletion_suspended")
    assert left(conn, t) == ["rec-a"] and len(tombs(worker_conn, t)) == 1
    release(app_conn, t)  # seq 1: a fresh job, the same payload and request time
    assert [(r[0], r[1], r[3]) for r in jobs(conn, t)] == [
        ("request:del-opaque-1", "failed", job.payload),
        ("request:del-opaque-1:r1", "queued", job.payload)]  # fmt: skip
    assert w.tick().jobs[0][1] is JobState.SUCCEEDED
    assert left(conn, t) == [] and len(tombs(worker_conn, t)) == 2
    first = datetime.fromisoformat(job.payload["requested_at"])  # type: ignore
    assert {g.requested_at for g in tombs(worker_conn, t)} == {first}
    hold(app_conn, t, ("rec-z",), "hold-2")
    release(app_conn, t, "hold-2", "2")  # nothing suspended now: nothing queued
    assert len(jobs(conn, t)) == 2
    # suspended after the latest release: a sweep does not re-run it
    conn.execute("INSERT INTO app.synthetic_note VALUES (%s, 'rec-c', "
                 "clock_timestamp() - interval '1 day')", (t,))  # fmt: skip
    hold(app_conn, t, ("rec-c",), "hold-3")
    submit(app_conn, t, "del-opaque-7")
    with tn.tenant_transaction(app_conn, t) as tx:
        dj.submit_sweep(tx)
    assert [s for _, s in w.tick().jobs] == [JobState.FAILED, JobState.SUCCEEDED]
    assert [r[0].split(":")[0] for r in jobs(conn, t)][2:] == ["request", "sweep"]
    assert left(conn, t) == ["rec-c"]  # no request:del-opaque-7:r3 was queued
    # backstop: a release whose resume was lost; the hourly sweep re-queues it
    u = tenant(app_conn, conn, "rec-a")
    hold(app_conn, u, ("rec-a",))
    submit(app_conn, u)
    assert w.tick().jobs[0][1] is JobState.FAILED  # suspended
    release(app_conn, u)
    conn.execute("DELETE FROM app.job WHERE tenant_id = %s AND input_revision LIKE "
                 "'%%:r1'", (u,))  # SYNTHETIC loss  # fmt: skip
    with tn.tenant_transaction(app_conn, u) as tx:
        dj.submit_sweep(tx)
    # one tick: the sweep re-queues the request, which then runs
    assert [s for _, s in w.tick().jobs] == [JobState.SUCCEEDED] * 2
    assert [r[0] for r in jobs(conn, u)][-1] == "request:del-opaque-1:r1"
    assert left(conn, u) == []


def test_a_release_requeues_a_refused_request_that_also_waited(
    app_conn: Conn, worker_conn: Conn, conn: Conn, notes: None
) -> None:  # decision 113: a poisoned record must not strand a held one
    t = tenant(app_conn, conn, "rec-a", "rec-bad")
    hold(app_conn, t, ("rec-a",))
    POISON["rec-bad"] = dj.TargetError("store_refused", retryable=False)
    w = worker(worker_conn)
    job = submit(app_conn, t)
    assert w.tick().jobs == [(job.id, JobState.FAILED)]
    assert jobs(conn, t)[0][1:3] == ("failed", "deletion_refused")
    POISON.clear()
    release(app_conn, t)
    assert [r[0] for r in jobs(conn, t)][-1] == "request:del-opaque-1:r1"
    assert w.tick().jobs[0][1] is JobState.SUCCEEDED
    assert left(conn, t) == [] and len(tombs(worker_conn, t)) == 2


def test_unknown_hold_state_deletes_nothing(app_conn: Conn, worker_conn: Conn,
                                            conn: Conn,
                                            notes: None) -> None:  # fmt: skip
    t = tenant(app_conn, conn, "rec-a")
    hold(app_conn, t, ("rec-z",))  # covers another record only
    job = submit(app_conn, t)
    # a snapshot older than the hold lock could miss a place: refused (PR A M1)
    assert run(worker_conn, job, snapshot=True) == "hold_state_unknown"
    with conn.transaction():  # a tampered hold row
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE app.legal_hold_event SET record_ids = '{}' WHERE "
                     "tenant_id = %s", (t,))  # fmt: skip
    assert run(worker_conn, job) == "hold_state_unknown"
    assert left(conn, t) == ["rec-a"] and tombs(worker_conn, t) == ()
    # a covering hold stamped after the job's clock (SYNTHETIC skew, resealed)
    # still blocks: holds are judged at the latest hold event too
    v = tenant(app_conn, conn, "rec-a")
    hold(app_conn, v, ("rec-a",))
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE app.legal_hold_event SET recorded_at = clock_timestamp() "
                     "+ interval '1 hour' WHERE tenant_id = %s", (v,))  # fmt: skip
        row = conn.execute("SELECT entry_hash, recorded_at FROM app.legal_hold_event "
                           "WHERE tenant_id = %s", (v,)).fetchone()  # fmt: skip
        assert row is not None
        conn.execute("UPDATE app.legal_hold_event SET row_hash = %s WHERE "
                     "tenant_id = %s", (seal(row[0], row[1]), v))  # fmt: skip
    assert run(worker_conn, submit(app_conn, v)) == "deletion_suspended"
    assert left(conn, v) == ["rec-a"] and tombs(worker_conn, v) == ()


def test_a_poisoned_record_fails_alone_and_logs_no_record_id(
        app_conn: Conn, worker_conn: Conn, conn: Conn, notes: None,
        caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    t = tenant(app_conn, conn, "rec-bad", "rec-boom", "rec-ok")
    POISON["rec-bad"] = dj.TargetError("store_refused", retryable=False)
    POISON["rec-boom"] = RuntimeError("SYNTHETIC failure naming rec-boom")
    caplog.set_level(logging.DEBUG)
    job = submit(app_conn, t)
    assert worker(worker_conn).tick().jobs == [(job.id, JobState.FAILED)]
    assert jobs(conn, t)[0][1:3] == ("failed", "deletion_refused")
    assert left(conn, t) == ["rec-bad", "rec-boom"] and len(tombs(worker_conn, t)) == 1
    POISON.clear()
    POISON["rec-bad"] = dj.TargetError("store_busy", retryable=True)
    assert run(worker_conn, job) == "database"  # retried; rec-boom deleted now
    assert left(conn, t) == ["rec-bad"] and len(tombs(worker_conn, t)) == 2
    for bad, want in (
        (dj.TargetError("store_down", retryable=True), "store_down"),
        (RuntimeError("SYNTHETIC rec-bad"), "target_failed"),
    ):

        def listing(tx: tn.TenantTx, ids: frozenset[str], e: Exception = bad) -> Any:
            raise e

        handle = dj.handlers(KEY, POLICY, {C.RESEARCH: dj.Target(
            C.RESEARCH, listing, _delete)})[dj.KIND]  # fmt: skip
        with pytest.raises(HandlerFailed) as err, \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            handle(tx, job)  # listing fails before anything is deleted
        assert (err.value.code, err.value.keep_effects) == (want, False)
    assert left(conn, t) == ["rec-bad"]
    text = caplog.text + " ".join(str(r.args) for r in caplog.records)
    assert "rec-" not in text and not any(r.exc_info for r in caplog.records)
    assert "store_refused" in text and "target_failed" in text


def test_refusals_delete_nothing(app_conn: Conn, worker_conn: Conn, conn: Conn,
                                 notes: None) -> None:  # fmt: skip
    t = tenant(app_conn, conn, "rec-a")
    other = rt.RecordKey(b"SYNTHETIC-another-record-key-98765432")
    evidence = submit(app_conn, t, "del-opaque-3",
                      classes=frozenset({C.RESEARCH, C.EVIDENCE}))  # fmt: skip
    assert run(worker_conn, evidence) == "no_target"  # evidence: no Target here
    assert run(worker_conn, submit(app_conn, t, "del-opaque-6")) is None
    conn.execute("INSERT INTO app.synthetic_note VALUES (%s, 'rec-b', "
                 "clock_timestamp())", (t,))  # fmt: skip
    # the tombstone append refuses another key: the delete rolls back with it
    assert run(worker_conn, submit(app_conn, t, "del-opaque-2"),
               key=other) == "deletion_refused"  # fmt: skip
    assert left(conn, t) == ["rec-b"] and len(tombs(worker_conn, t)) == 1
    job = submit(app_conn, t, "del-opaque-4")
    base = dict(job.payload)  # type: ignore[arg-type]
    for bad in ({**base, "acknowledgement": "x"},
                {**base, "requested_at": "2026-10-31T00:00:00+00:00"},
                {**base, "classes": ["research", "research"]},
                {**base, "trigger": "sweep"}, None):  # fmt: skip
        assert run(worker_conn, claimed(job.id, t, job.input_revision, bad)) == (
            "payload_invalid"
        )
    assert left(conn, t) == ["rec-b"]
    with tn.tenant_transaction(app_conn, t) as tx, pytest.raises(ValueError):
        dj.submit_request(tx, rt.DeletionRequest(
            "del-opaque-5", str(t), "scope-1", frozenset({C.RESEARCH}),
            datetime(2100, 1, 1, tzinfo=UTC), "reauth-1", "SYNTHETIC"))  # fmt: skip


def test_expiry_sweep_follows_the_class_rule(app_conn: Conn, worker_conn: Conn,
                                             conn: Conn,
                                             notes: None) -> None:  # fmt: skip
    t = tenant(app_conn, conn, "old-a", "old-b")
    conn.execute("INSERT INTO app.synthetic_note VALUES (%s, 'new-a', "
                 "clock_timestamp())", (t,))  # fmt: skip
    with tn.tenant_transaction(app_conn, t) as tx:
        jid, created = dj.submit_sweep(tx)
        again, created2 = dj.submit_sweep(tx)  # once per hour of the clock
    assert created and not created2 and again == jid
    rev, _, _, p = jobs(conn, t)[0]
    job = claimed(jid, t, rev, p)
    assert run(worker_conn, job) is None  # owner_defaults: no age rule for research
    assert left(conn, t) == ["new-a", "old-a", "old-b"]
    rules = dict(rt.owner_defaults().rules)
    rules[C.RESEARCH] = rt.Rule(C.RESEARCH, rt.OnRequest.DELETE, timedelta(days=1),
                                "owner-synth-1", "synthetic-basis-1")  # fmt: skip
    assert run(worker_conn, job, policy=rt.Policy("pol-synth", 2, rules)) is None
    assert left(conn, t) == ["new-a"] and len(tombs(worker_conn, t)) == 2


def test_record_key_is_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "record.key"  # owner decision 107
    f.write_bytes(RAW + b"\n")
    f.chmod(0o600)
    assert record_key_from_env({}) is None
    got = record_key_from_env({"QW_RECORD_KEY_FILE": str(f)})
    assert got == KEY and RAW.decode() not in repr(got)
    with pytest.raises(InlineSecretError):
        record_key_from_env({"QW_RECORD_KEY": RAW.decode(),
                             "QW_RECORD_KEY_FILE": str(f)})  # fmt: skip
    f.chmod(0o640)
    with pytest.raises(ConfigError, match="group or world"):
        record_key_from_env({"QW_RECORD_KEY_FILE": str(f)})
    short = tmp_path / "short.key"
    short.write_bytes(b"SYNTHETIC-too-short")
    short.chmod(0o600)
    with pytest.raises(ConfigError, match="at least 32 bytes") as err:
        record_key_from_env({"QW_RECORD_KEY_FILE": str(short)})
    assert "SYNTHETIC-too-short" not in str(err.value)
    with pytest.raises(ConfigError, match="absolute"):
        record_key_from_env({"QW_RECORD_KEY_FILE": os.path.relpath(f)})
    for name in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE",
                 "QW_ASSESSOR_DATABASE_URL"):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    passfile = tmp_path / "worker.pgpass"
    passfile.write_text("*:*:*:qw_worker_login:SYNTHETIC-pw\n")
    passfile.chmod(0o600)
    monkeypatch.setenv("QW_DATABASE_URL", f"host={tmp_path / 'no-socket'} dbname=qw "
                       f"user=qw_worker_login passfile={passfile}")  # fmt: skip
    monkeypatch.setenv("QW_RECORD_KEY_FILE", str(short))
    assert entry.main(["--check-config"]) == 2  # the key is checked at startup
    out = capsys.readouterr().err
    assert "QW_RECORD_KEY_FILE" in out and "SYNTHETIC-too-short" not in out
