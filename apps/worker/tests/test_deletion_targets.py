"""The store deletion targets (T058; `qw_adapters.deletion_targets`, migration 0063)
and the `retention.delete` job over the registered one (simulation; the evidence
functions are tested directly and are NOT registered until owner decision 124's
passage-reading redaction lands), on a real PostgreSQL 16 as the
non-owner, NOBYPASSRLS qw_worker login. qw_app saves the SYNTHETIC simulations and
evidence through their own stores and places holds; the superuser `conn` only
observes, or plays a tampering owner where a test says so. Tenants, simulations,
evidence texts, holds, principals, receipts and the record key are SYNTHETIC."""

from __future__ import annotations

import importlib.util
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from ingest_rights_helper import KEEP, REGION, qualified_registry
from psycopg import errors
from qw_adapters import deletion_targets as dt
from qw_adapters import evidence_store as es
from qw_adapters import legal_hold_store as hs
from qw_adapters import simulation_store as ss
from qw_adapters import tenancy as tn
from qw_adapters import tombstone_store as ts
from qw_adapters.jobs import ClaimedJob
from qw_domain import retention as rt
from qw_domain import simulation as sim
from qw_domain.decimals import Money
from qw_domain.evidence import EvidenceStore, Reading, SourceKind
from qw_domain.filings import Period
from qw_domain.identity import InstrumentId
from qw_domain.ingest import IngestRights
from qw_domain.jobs import Priority
from qw_domain.rights import UseScope
from qw_domain.risk import Side
from qw_domain.simulation import OrderStyle
from qw_worker import deletion_job as dj
from qw_worker.loop import HandlerFailed
from test_simulation import LATE, T0, at, order, run

_path = Path(__file__).parents[3] / "packages" / "adapters" / "tests" / "conftest.py"
_spec = importlib.util.spec_from_file_location("qw_pg_harness_targets", _path)
assert _spec is not None and _spec.loader is not None
_harness: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
pg_admin_url, database_url = _harness.pg_admin_url, _harness.database_url
conn, runtime_urls = _harness.conn, _harness.runtime_urls
app_conn, worker_conn = _harness.app_conn, _harness.worker_conn

Conn = psycopg.Connection[Any]
KEY = rt.RecordKey(b"SYNTHETIC-test-record-key-targets-0123456789")  # test only
C, R = rt.RetentionClass, rt.HoldRole
POLICY = rt.owner_defaults()
SIMS = ("SIM-1", "SIM-2")
TEXTS = {"s-one": "SYNTHETIC filing: revenue 100 USD.",
         "s-two": "SYNTHETIC release: revenue 120 USD."}  # fmt: skip
EXACT = datetime(2026, 10, 1, 12, 0, 0, 123457, tzinfo=UTC)
pytestmark = pytest.mark.db


def simulation(sid: str) -> ss.Simulation:
    """One market buy: rows in all five simulation tables."""
    p = sim.VirtualPortfolio.open(sid, Money.of("100000", "USD"), T0)
    s = ss.Simulation("SYNTHETIC sim", "exec-synth-1", "bench-synth-1", p)
    return s.run(run(p, order("o1", Side.BUY, "100", OrderStyle.MARKET,
                              at(13, 29))), LATE)  # fmt: skip


def evidence(app: Conn, t: uuid.UUID) -> None:
    now = app.execute("SELECT clock_timestamp()").fetchone()
    assert now is not None
    t0 = now[0].astimezone(UTC).replace(microsecond=0) - timedelta(days=30)
    reg = qualified_registry(("feed-a",), None, str(t), t0 + timedelta(days=3650))
    store = EvidenceStore()
    for sid, text in TEXTS.items():
        rights = IngestRights(reg, str(t), "feed-a", t0, UseScope.PERSONAL, REGION,
                              KEEP)  # fmt: skip
        store.add_source(rights, sid, SourceKind.PRIMARY, text, t0)
    value = Reading(InstrumentId(uuid.UUID(int=41)), "us-gaap:Revenues",
                    Period(None, t0.date()), "USD", Decimal("100"))  # fmt: skip
    store.add_passage(str(t), "p-one", "s-one", TEXTS["s-one"].index("revenue"),
                      "revenue 100 USD", value, t0)  # fmt: skip
    with tn.tenant_transaction(app, t) as tx:
        es.save(tx, store)


def tenant(app: Conn, *sims: str, with_evidence: bool = True) -> uuid.UUID:
    with tn.provision_tenant(app) as tx:
        t = tx.tenant_id
    for sid in sims:
        with tn.tenant_transaction(app, t) as tx:
            ss.save(tx, simulation(sid))
    if with_evidence:
        evidence(app, t)
    return t


def rows(su: Conn, t: uuid.UUID) -> dict[str, int]:
    """Rows of each simulation table, held texts and evidence deletions."""
    got = {}
    for table in (*ss.TABLES, "evidence_content", "evidence_source"):
        row = su.execute(f"SELECT count(*) FROM app.{table} WHERE tenant_id = %s",
                         (t,)).fetchone()  # fmt: skip
        assert row is not None
        got[table] = int(row[0])
    row = su.execute("SELECT count(*) FROM app.evidence_event WHERE tenant_id = %s "
                     "AND kind = 'deleted'", (t,)).fetchone()  # fmt: skip
    assert row is not None
    got["deleted"] = int(row[0])
    return got


def listed(c: Conn, t: uuid.UUID) -> dict[str, list[rt.Item]]:
    with tn.tenant_transaction(c, t) as tx:
        return {"simulation": dt.simulation_items(tx, frozenset()),
                "evidence": dt.evidence_items(tx, frozenset())}  # fmt: skip


def job(app: Conn, t: uuid.UUID, classes: frozenset[rt.RetentionClass],
        rid: str = "del-opaque-t1") -> ClaimedJob:  # fmt: skip
    with tn.tenant_transaction(app, t) as tx:
        now = tx.conn.execute("SELECT clock_timestamp()").fetchone()
        assert now is not None
        req = rt.DeletionRequest(rid, str(t), "scope-1", classes,
                                 now[0] - timedelta(seconds=1), "reauth-1",
                                 "SYNTHETIC acknowledgement")  # fmt: skip
        jid, _ = dj.submit_request(tx, req)
    return ClaimedJob(jid, t, dj.KIND, f"request:{rid}", Priority.SAFETY,
                      dj.payload(req), 1, 5, "synthetic:token",
                      datetime(2026, 11, 3, tzinfo=UTC))  # fmt: skip


def handle(c: Conn, j: ClaimedJob) -> str | None:
    """The registered handler in one tenant transaction, as the loop runs it."""
    h = dj.handlers(KEY, POLICY, dj.TARGETS)[dj.KIND]
    try:
        with tn.tenant_transaction(c, j.tenant_id) as tx:
            try:
                h(tx, j)
            except HandlerFailed as exc:
                if not exc.keep_effects:
                    raise
                return exc.code
    except HandlerFailed as exc:
        return exc.code
    return None


def tombs(c: Conn, t: uuid.UUID) -> tuple[rt.Tombstone, ...]:
    with tn.tenant_transaction(c, t) as tx:
        return ts.load(tx).entries


def test_only_the_simulation_target_is_registered() -> None:
    assert set(dj.TARGETS) == {C.SIMULATION}
    assert all(type(t) is dj.Target and t.cls is c for c, t in dj.TARGETS.items())
    # every other class the policy deletes on request still has no store target;
    # evidence among them until decision 124's passage-reading redaction lands
    missing = {c for c in C if c not in dj.TARGETS
               and POLICY.rule(c).on_request is rt.OnRequest.DELETE}  # fmt: skip
    assert {C.EVIDENCE, C.RESEARCH} <= missing and C.FINANCIAL not in missing


def test_a_request_naming_evidence_fails_no_target_before_anything_runs(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, *SIMS)
    before = rows(conn, t)
    for i, classes in enumerate((frozenset({C.EVIDENCE}),
                                 frozenset({C.SIMULATION, C.EVIDENCE}))):  # fmt: skip
        assert handle(worker_conn, job(app_conn, t, classes,
                                       f"del-opaque-e{i}")) == "no_target"  # fmt: skip
    assert rows(conn, t) == before and before["evidence_content"] == 2
    assert before["simulation"] == 2 and tombs(worker_conn, t) == ()


def test_created_at_round_trips_exactly(app_conn: Conn, worker_conn: Conn,
                                        conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, "SIM-1")
    conn.execute("SET session_replication_role = replica")  # pin a µs instant
    conn.execute("UPDATE app.simulation SET created_at = %s WHERE tenant_id = %s",
                 (EXACT, t))  # fmt: skip
    conn.execute("UPDATE app.evidence_source SET recorded_at = %s + interval "
                 "'1 microsecond' WHERE tenant_id = %s AND id = 's-one'",
                 (EXACT, t))  # fmt: skip
    conn.execute("SET session_replication_role = origin")
    got = listed(worker_conn, t)
    assert got["simulation"] == [rt.Item(str(t), "SIM-1", C.SIMULATION, EXACT)]
    one = rt.Item(str(t), "s-one", C.EVIDENCE, EXACT + timedelta(microseconds=1))
    assert got["evidence"][0] == one and [i.record_id for i in got["evidence"]] == [
        "s-one", "s-two"]  # fmt: skip
    text = conn.execute(
        "SELECT to_char(recorded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') "
        "FROM app.evidence_source WHERE tenant_id = %s AND id = 's-two'",
        (t,),
    ).fetchone()
    assert text is not None
    two = got["evidence"][1].created_at
    assert two.tzinfo is UTC and f"{two:%Y-%m-%d %H:%M:%S.%f}" == text[0]
    # a truncated or shifted instant is another incarnation: never deleted
    us = timedelta(microseconds=1)
    for shift in (-us, us, -EXACT.microsecond * us):  # s-one is EXACT + 1 µs
        with tn.tenant_transaction(worker_conn, t) as tx:
            assert not dt.simulation_delete(
                tx, rt.Item(str(t), "SIM-1", C.SIMULATION, EXACT + shift)
            )
            assert not dt.evidence_delete(
                tx, rt.Item(str(t), "s-one", C.EVIDENCE, one.created_at + shift)
            )
    assert rows(conn, t)["simulation"] == 1 and rows(conn, t)["deleted"] == 0
    with tn.tenant_transaction(worker_conn, t) as tx:
        assert dt.simulation_delete(tx, got["simulation"][0])
        assert dt.evidence_delete(tx, one)
    with tn.tenant_transaction(worker_conn, t) as tx:  # absent now: False
        assert not dt.simulation_delete(tx, got["simulation"][0])
        assert not dt.evidence_delete(tx, one)
    assert rows(conn, t) == {"simulation": 0, "simulation_entry": 0,
                             "simulation_order": 0, "simulation_fill": 0,
                             "simulation_digest": 0, "evidence_content": 1,
                             "evidence_source": 2, "deleted": 1}  # fmt: skip


def test_a_request_deletes_each_record_with_one_tombstone_and_retries(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, *SIMS)
    items = listed(worker_conn, t)
    j = job(app_conn, t, frozenset({C.SIMULATION}))
    assert handle(worker_conn, j) is None
    left = rows(conn, t)
    assert left["evidence_content"] == 2 and left["deleted"] == 0  # unregistered
    assert all(left[x] == 0 for x in ss.TABLES) and left["evidence_source"] == 2
    got = tombs(worker_conn, t)
    assert [g.cls for g in got] == [C.SIMULATION, C.SIMULATION]
    assert all(g.backups_unknown for g in got)
    with tn.tenant_transaction(worker_conn, t) as tx:  # each exact incarnation
        for it in items["simulation"]:
            assert ts.find(tx, KEY, it) is not None
        assert ss.load(tx, "SIM-1") is None and ss.page(tx, 10, None) == []
        store = es.load(tx, for_write=False)
        assert store.passage_text(str(t), "p-one") is not None  # evidence kept
    assert handle(worker_conn, j) is None and tombs(worker_conn, t) == got  # retry
    assert listed(worker_conn, t) == {"simulation": [],
                                      "evidence": items["evidence"]}  # fmt: skip
    # a re-created SIM-1 is a new incarnation: a new request deletes it anew
    with tn.tenant_transaction(app_conn, t) as tx:
        ss.save(tx, simulation("SIM-1"))
    assert handle(worker_conn, job(app_conn, t, frozenset({C.SIMULATION}),
                                   "del-opaque-t2")) is None  # fmt: skip
    assert len(tombs(worker_conn, t)) == 3 and rows(conn, t)["simulation"] == 0


def test_held_records_are_never_deleted(app_conn: Conn, worker_conn: Conn,
                                        conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, *SIMS)
    with tn.tenant_transaction(app_conn, t) as tx:
        hs.place(tx, "hold-1", {C.SIMULATION}, ("SIM-1",), "litigation",
                 "counsel-1", R.TENANT_OWNER, "owner-synth-1")  # fmt: skip
    with tn.tenant_transaction(app_conn, t) as tx:
        before = ss.load(tx, "SIM-1")
    j = job(app_conn, t, frozenset({C.SIMULATION}))
    assert handle(worker_conn, j) == "deletion_suspended"
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") == before and ss.load(tx, "SIM-2") is None
    left = rows(conn, t)
    assert left["evidence_content"] == 2 and left["deleted"] == 0
    assert [g.cls for g in tombs(worker_conn, t)] == [C.SIMULATION]  # SIM-2 only


def test_targets_see_and_delete_only_their_own_tenant(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    a = tenant(app_conn, "SIM-1")
    b = tenant(app_conn, with_evidence=False)
    a_items = listed(worker_conn, a)
    assert listed(worker_conn, b) == {"simulation": [], "evidence": []}
    with tn.tenant_transaction(worker_conn, b) as tx:
        assert not dt.simulation_delete(tx, a_items["simulation"][0])
        assert not dt.evidence_delete(tx, a_items["evidence"][0])
        for table in (*dt.DELETE_ORDER, "simulation"):  # RLS: none of A's rows
            assert tx.conn.execute(f"DELETE FROM app.{table}").rowcount == 0
    assert handle(worker_conn, job(app_conn, b, frozenset({C.SIMULATION}))) is None
    assert rows(conn, a)["simulation"] == 1 and rows(conn, a)["evidence_content"] == 2
    assert tombs(worker_conn, b) == () and tombs(worker_conn, a) == ()


def test_only_the_worker_may_delete_and_only_whole_simulations(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, "SIM-1", with_evidence=False)
    for table in ss.TABLES:
        for role, may in (("qw_app", False), ("qw_worker", True),
                          ("qw_assessor", False), ("qw_release", False)):  # fmt: skip
            row = conn.execute("SELECT has_table_privilege(%s, %s, 'DELETE')",
                               (role, f"app.{table}")).fetchone()  # fmt: skip
            assert row == (may,), (role, table)
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(app_conn, t) as tx:  # fmt: skip
            tx.conn.execute(f"DELETE FROM app.{table}")
        with pytest.raises(errors.InsufficientPrivilege), \
                tn.tenant_transaction(app_conn, t) as tx:  # fmt: skip
            tx.conn.execute(f"UPDATE app.{table} SET tenant_id = tenant_id")
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(f"DELETE FROM app.{table}")  # not a superuser
        for op in ("DELETE FROM", "UPDATE", "TRUNCATE"):  # the owner: the triggers
            # TRUNCATE CASCADE: a referenced table's FK check precedes its trigger
            tail = {"UPDATE": " SET tenant_id = tenant_id", "TRUNCATE": " CASCADE"}
            stmt = f"{op} app.{table}" + tail.get(op, "")
            with pytest.raises(errors.RaiseException,
                               match=f"append-only: {op.split()[0]} refused"), \
                    conn.transaction():  # fmt: skip
                conn.execute("SET LOCAL SESSION AUTHORIZATION qw_migrate")
                conn.execute(stmt)
    for partial in ("DELETE FROM app.simulation_digest",
                    "DELETE FROM app.simulation_fill"):  # fmt: skip
        with pytest.raises(errors.RaiseException, match="whole simulation"), \
                tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
            tx.conn.execute(partial)
    with pytest.raises(errors.ForeignKeyViolation), \
            tn.tenant_transaction(worker_conn, t) as tx:  # fmt: skip
        tx.conn.execute("DELETE FROM app.simulation")  # children first
    with tn.tenant_transaction(app_conn, t) as tx:
        assert ss.load(tx, "SIM-1") == simulation("SIM-1")


def test_a_corrupt_evidence_store_fails_only_its_own_deletions(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, "SIM-1")
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute("UPDATE app.evidence_content SET content = 'SYNTHETIC forged' "
                 "WHERE tenant_id = %s AND source_id = 's-two'", (t,))  # fmt: skip
    conn.execute("SET session_replication_role = origin")
    with tn.tenant_transaction(worker_conn, t) as tx:  # listed; verified per delete
        for it in dt.evidence_items(tx, frozenset()):
            with pytest.raises(dt.TargetStoreError, match="integrity"):
                dt.evidence_delete(tx, it)
    assert handle(worker_conn, job(app_conn, t, frozenset({C.SIMULATION}))) is None
    left = rows(conn, t)
    assert left["simulation"] == 0 and left["evidence_content"] == 2
    assert [g.cls for g in tombs(worker_conn, t)] == [C.SIMULATION]
    with tn.tenant_transaction(worker_conn, t) as tx:  # the backstop still runs
        jid, _ = dj.submit_sweep(tx)
        payload = tx.conn.execute("SELECT input_revision, payload FROM app.job "
                                  "WHERE id = %s", (jid,)).fetchone()  # fmt: skip
    assert payload is not None
    lease = datetime(2026, 11, 3, tzinfo=UTC)
    sweep = ClaimedJob(jid, t, dj.KIND, payload[0], Priority.SAFETY, payload[1], 1,
                       5, "synthetic:token", lease)  # fmt: skip
    assert handle(worker_conn, sweep) is None


def test_listing_holds_each_store_lock_before_any_tombstone_lock(
        app_conn: Conn, worker_conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn, "SIM-1")

    def free(name: str) -> bool:  # another session: is the store lock free?
        with app_conn.transaction():
            row = app_conn.execute("SELECT pg_try_advisory_xact_lock_shared("
                                   "hashtextextended(%s, 0))",
                                   (f"{name}:{t}",)).fetchone()  # fmt: skip
        assert row is not None
        return bool(row[0])

    with tn.tenant_transaction(worker_conn, t) as tx:
        nothing = frozenset({"SIM-none"})  # no candidate: no lock at all
        assert dt.simulation_items(tx, nothing) == dt.evidence_items(tx, nothing) == []
        assert free("evidence") and free("simulation")
        dt.evidence_items(tx, frozenset())
        assert not free("evidence") and free("simulation")
        dt.simulation_items(tx, frozenset())
        assert not free("evidence") and not free("simulation")
        assert free("deletion_tombstone")  # none taken yet
    assert free("evidence") and free("simulation")
    with tn.tenant_transaction(worker_conn, t) as tx:  # the registered path
        for c in sorted(dj.TARGETS):
            dj.TARGETS[c].items(tx, frozenset())
        assert free("evidence") and not free("simulation")
        assert free("deletion_tombstone")


def test_two_jobs_of_one_tenant_do_not_deadlock(
        app_conn: Conn, worker_conn: Conn, conn: Conn,
        runtime_urls: dict[str, str]) -> None:  # fmt: skip
    t = tenant(app_conn, *SIMS)
    sims = frozenset({C.SIMULATION})
    jobs = [job(app_conn, t, sims, "del-opaque-c1"), job(app_conn, t, sims,
                                                         "del-opaque-c2")]  # fmt: skip
    got: dict[int, str | None] = {}
    start = threading.Barrier(2)
    with psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as other:
        for c in (worker_conn, other):
            c.execute("SET lock_timeout = '20s'")  # bounded: a hang fails, not waits

        def go(i: int, c: Conn) -> None:
            start.wait(timeout=10)
            got[i] = handle(c, jobs[i])

        runs = [threading.Thread(target=go, args=(i, c))
                for i, c in enumerate((worker_conn, other))]  # fmt: skip
        for r in runs:
            r.start()
        for r in runs:
            r.join(timeout=60)
        assert not any(r.is_alive() for r in runs)
    assert got == {0: None, 1: None}  # neither deadlocked nor timed out
    assert len(tombs(worker_conn, t)) == 2  # each record once
    assert rows(conn, t)["simulation"] == 0 and rows(conn, t)["evidence_content"] == 2


def test_a_deleted_record_is_absent_before_the_store_is_verified(
        app_conn: Conn, worker_conn: Conn, conn: Conn) -> None:  # fmt: skip
    t = tenant(app_conn)
    one = listed(worker_conn, t)["evidence"][0]
    with tn.tenant_transaction(worker_conn, t) as tx:
        assert dt.evidence_delete(tx, one)
    conn.execute("SET session_replication_role = replica")  # then a corruption
    conn.execute("UPDATE app.evidence_content SET content = 'SYNTHETIC forged' "
                 "WHERE tenant_id = %s AND source_id = 's-two'", (t,))  # fmt: skip
    conn.execute("SET session_replication_role = origin")
    with tn.tenant_transaction(worker_conn, t) as tx:  # the retry: absent, no load
        assert not dt.evidence_delete(tx, one)
        with pytest.raises(dt.TargetStoreError, match="integrity"):
            dt.evidence_delete(tx, listed_one(tx, "s-two"))


def listed_one(tx: tn.TenantTx, sid: str) -> rt.Item:
    (it,) = dt.evidence_items(tx, frozenset({sid}))
    return it


def test_only_transient_database_errors_are_retryable() -> None:
    def fails(exc: BaseException) -> Any:
        def go() -> None:
            raise exc

        try:
            dt._guarded(go)
        except dt.TargetStoreError as err:
            return err.code, err.retryable
        pytest.fail("no error")

    for transient in (errors.SerializationFailure(), errors.DeadlockDetected(),
                      errors.LockNotAvailable(), errors.QueryCanceled(),
                      psycopg.OperationalError()):  # fmt: skip
        assert fails(transient) == ("database", True)
    for lasting in (errors.InsufficientPrivilege(), errors.CheckViolation(),
                    errors.UndefinedTable(), psycopg.ProgrammingError()):  # fmt: skip
        assert fails(lasting) == ("database", False)
    for cause, retry in ((errors.DeadlockDetected(), True),
                         (errors.InsufficientPrivilege(), False)):  # fmt: skip
        wrapped = es.EvidenceStoreError("database", "x")
        wrapped.__context__ = cause  # as `raise ... from None` in the store keeps it
        assert fails(wrapped) == ("database", retry)
    assert fails(es.EvidenceStoreError("integrity")) == ("integrity", False)
