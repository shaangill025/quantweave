"""0028, the assessor-role migration and qw_adapters.assessor_store through
experiment_store.record: the frozen protocol, lineage, holdout reads and evaluation
receipts are stored as qw_assessor alone (owner decision 17), then the revision as
qw_worker, and Q7's single holdout read holds across ledgers. qw_worker and qw_app
read no protected data; the superuser `conn` observes or tampers. SYNTHETIC
fixtures (test_improvement)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.conninfo import make_conninfo
from qw_adapters import assessor_store as ast
from qw_adapters import experiment_store as es
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from test_improvement import ASSESSOR, AT, GOOD, cand, promo_ledger
from test_trial_ledger import T0, ledger, plan, trial

_spec = importlib.util.spec_from_file_location(
    "qw_estore_tests", Path(__file__).with_name("test_experiment_store.py")
)
assert _spec is not None and _spec.loader is not None
st = sys.modules.setdefault("qw_estore_tests", importlib.util.module_from_spec(_spec))
if not hasattr(st, "put"):
    _spec.loader.exec_module(st)
EXP, booked, call, copy, put, reg_for = (st.EXP, st.booked, st.call, st.copy, st.put,
                                         st.reg_for)  # fmt: skip
wa = st.wa
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
Pair = tuple[Conn, Conn]  # (qw_worker, qw_assessor), as in test_experiment_store
St = im.RecordState
PROTECTED = ("assessor_protocol", "assessor_candidate", "holdout_access")
APP_RECEIPT = {"tenant_id", "experiment_id", "verdict_id", "candidate_id",
               "candidate_hash", "protocol_hash", "assessor_version", "decision",
               "reasons", "trial_ids", "failed_trial_ids", "limitations",
               "assessed_at", "recorded_at"}  # fmt: skip
PARTIAL = {k: v for k, v in GOOD.items() if k != "unrecorded_trials"}


def lg(t: uuid.UUID, base: tl.TrialLedger) -> tl.TrialLedger:
    return tl.TrialLedger(str(t), base.entries)


def run(t: uuid.UUID, base: tl.TrialLedger, vid: str) -> im.Assessment:
    return im.Assessment(lg(t, base), ASSESSOR, AT + timedelta(days=1), vid)


@pytest.fixture
def a(app_conn: Conn, wa: Pair) -> uuid.UUID:
    """A tenant whose experiment EXP is authorized (its budget is reserved)."""
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    reg = reg_for(t)
    put(wa, t, reg, booked(reg), im.Scheduled(EXP, "bud-exp-1"))
    return t


def assess(wa: Pair, t: uuid.UUID, base: tl.TrialLedger, vid: str,
           eid: str = EXP) -> es.Revision:  # fmt: skip
    reg = reg_for(t, eid, bud="bud-exp-1" if eid == EXP else f"bud-{eid}")
    rev: es.Revision = put(wa, t, reg, booked(reg, eid), None,
                           run(t, base, vid), eid=eid)[0]  # fmt: skip
    return rev


def test_f1_a_fresh_ledger_cannot_reassess(
    wa: Pair, app_conn: Conn, a: uuid.UUID
) -> None:
    # Reviewer F1: an unscored holdout read (another assessor-role actor) is
    # insufficient; a fresh ledger with its own first read must not be scored.
    other = promo_ledger(GOOD, actor="actor-other")
    reg0 = reg_for(a, "exp-0", bud="bud-exp-0")
    put(wa, a, reg0, booked(reg0, "exp-0"), im.Scheduled("exp-0", "bud-exp-0"),
        eid="exp-0")  # fmt: skip
    assess(wa, a, other, "v-0", "exp-0")  # the plan's first read is stored
    with pytest.raises(es.ExperimentStoreError, match="ledger_diverged"):
        assess(wa, a, promo_ledger(GOOD), "v-1")  # the plan's reads differ
    first = assess(wa, a, other, "v-1")
    assert first.record.state is St.INSUFFICIENT_EVIDENCE
    assert "insufficient:not_assessor_run" in first.record.reasons
    with pytest.raises(es.ExperimentStoreError, match="ledger_diverged"):
        assess(wa, a, promo_ledger(GOOD), "v-2")
    assert call(app_conn, a, "load", EXP) == first  # nothing was written


def test_a_used_holdout_read_is_never_reassessed(
    wa: Pair, assessor_conn: Conn, app_conn: Conn, a: uuid.UUID
) -> None:
    scored = assess(wa, a, promo_ledger(PARTIAL), "v-1")
    assert "insufficient:metric_missing:unrecorded_trials" in scored.record.reasons
    for base, vid in ((promo_ledger(GOOD), "v-2"), (ledger(), "v-3")):
        with pytest.raises(es.ExperimentStoreError, match="holdout_used"):
            assess(wa, a, base, vid)
    assert call(app_conn, a, "load", EXP) == scored
    with tn.tenant_transaction(assessor_conn, a) as tx:
        (v, seq, n, digest), = ast.receipts(tx, EXP)  # fmt: skip
    assert (v.verdict_id, v.trial_ids, seq, n) == ("v-1", ("trial-1",), 1, 3)
    assert v.scores == PARTIAL and digest == lg(a, promo_ledger(PARTIAL)).checkpoint(3)


def test_an_extended_ledger_reassesses_while_the_read_is_unused(
    wa: Pair, assessor_conn: Conn, a: uuid.UUID
) -> None:
    thin = assess(wa, a, ledger(), "v-1")  # plan only: nothing read
    moved = plan(registered_at=T0 - timedelta(minutes=1))  # not an extension
    fresh = tl.TrialLedger(str(a), (moved, *promo_ledger(GOOD).entries[1:]))
    with pytest.raises(es.ExperimentStoreError, match="ledger_diverged"):
        assess(wa, a, fresh, "v-2")  # no read stored: the checkpoint holds
    done = assess(wa, a, promo_ledger(GOOD), "v-2")  # the same ledger grown
    assert (thin.record.state, done.record.state) == (
        St.INSUFFICIENT_EVIDENCE, St.AWAITING_REVIEW)  # fmt: skip
    with tn.tenant_transaction(assessor_conn, a) as tx:
        got = ast.receipts(tx, EXP)
        reads = tx.conn.execute("SELECT seq, access_id, actor_id FROM "
                                "app.holdout_access").fetchall()  # fmt: skip
    assert [(v.verdict_id, seq, n) for v, seq, n, _ in got] == [
        ("v-1", None, 1), ("v-2", 1, 3)]  # fmt: skip
    assert reads == [(1, "acc-1", "actor-synth")]


def test_a_ledger_must_extend_the_latest_checkpoint(wa: Pair, a: uuid.UUID) -> None:
    # N3b: two checkpoints (1 and 2 entries); a ledger that keeps the first entry
    # but rewrites the second is refused, though it extends the first checkpoint.
    def dev(trial_id: str) -> tl.Trial:  # a failed trial that never reached data
        return trial(0, tl.Split.DEVELOPMENT, tl.Outcome.FAILED, trial_id=trial_id,
                     family="fam-other", access_id=None)  # fmt: skip

    base, promo = ledger(), promo_ledger(GOOD).entries[1:]
    assess(wa, a, base, "v-1")
    assess(wa, a, base.record(dev("trial-0")), "v-2")
    rewritten = tl.TrialLedger(str(a), (*base.entries, dev("trial-0b"), *promo))
    with pytest.raises(es.ExperimentStoreError, match="ledger_diverged"):
        assess(wa, a, rewritten, "v-3")
    grown = tl.TrialLedger(str(a), (*base.entries, dev("trial-0"), *promo))
    assert assess(wa, a, grown, "v-3").record.state is St.AWAITING_REVIEW


def test_the_stored_protocol_and_lineage_are_frozen(wa: Pair, a: uuid.UUID) -> None:
    reg = reg_for(a)
    changed = replace(reg.protocol(EXP), hypothesis="SYNTHETIC: other")
    other = im.ExperimentRegistry(str(a)).register(changed).register_candidate(cand())
    with pytest.raises(es.ExperimentStoreError, match="protocol_frozen"):
        put(wa, a, other, booked(reg))
    relabel = im.ExperimentRegistry(str(a)).register(reg.protocol(EXP))
    relabel = relabel.register_candidate(cand(payload={"lookback_months": "9"}))
    with pytest.raises(es.ExperimentStoreError, match="lineage"):
        put(wa, a, relabel, booked(reg))


def receipt_copy(conn: Conn, src: str = "scored_seq IS NOT NULL", **over: str) -> str:
    """An INSERT copying the `src` receipt with the named columns replaced."""
    over = {"verdict_id": "'v-x'", "recorded_at": "clock_timestamp()"} | over
    names = conn.execute("SELECT attname FROM pg_attribute WHERE attrelid = "
                         "'app.evaluation_receipt'::regclass AND attnum > 0 "
                         "ORDER BY attnum").fetchall()  # fmt: skip
    cols = ", ".join(over.get(c, c) for (c,) in names)
    return (f"INSERT INTO app.evaluation_receipt SELECT {cols} FROM "
            f"app.evaluation_receipt WHERE {src}")  # fmt: skip


def test_each_database_rule_refuses_a_raw_insert(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    unscored = {"scored_seq": "NULL", "trial_ids": "'{}'"}
    blocked = unscored | {"decision": "'blocked'", "reasons": "'{blocked}'"}
    cases = [  # as the owner, so only the database applies
        receipt_copy(conn, scored_seq="NULL"),  # a scored trial without its read
        receipt_copy(
            conn, **unscored, decision="'reject'", reasons="'{goal:x}'"
        ),  # scores
        receipt_copy(conn, **blocked, assessor_actor_id="'actor-forged'"),  # not frozen
        receipt_copy(conn, **blocked, protocol_hash="repeat('0', 64)"),
        receipt_copy(
            conn, **blocked, recorded_at="clock_timestamp() + interval '2 min'"
        ),
        "INSERT INTO app.holdout_access SELECT tenant_id, plan_id, 3, 'acc-9', "
        "trial_id, family, actor_id, jsonb_set(jsonb_set(body, '{0}', '\"acc-9\"'),"
        " '{12}', '3'), clock_timestamp(), event_hash FROM app.holdout_access",
    ]
    nxt = ("experiment_id", "tenant_id", "scope_id", "revision + 1",
           "budget_reservation_id", "event_hash")  # fmt: skip
    for sql in (
        copy("'exp-n'", "tenant_id", "scope_id", "1", "'bud-n'", "NULL"),  # protocol
        copy(*nxt, state="'insufficient_evidence'",
             decided="'v-ghost', 'insufficient_evidence'"),  # no receipt
    ):  # fmt: skip
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(sql)  # the latest row is authorized
    assess(wa, a, promo_ledger(GOOD), "v-1")
    for sql in cases:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(sql)
    conn.execute(receipt_copy(conn, **blocked))  # an unscored receipt is allowed ...
    for over in ({}, {"decision": "'insufficient_evidence'", "reasons":
                      "'{metric_missing:x}'"}, {"decision": "'reject'",
                      "reasons": "'{goal:x}'"}):  # fmt: skip
        with pytest.raises(psycopg.errors.UniqueViolation):  # ... a second scored one
            conn.execute(receipt_copy(conn, verdict_id="'v-y'", **over))  # never


def test_a_scored_receipt_must_match_its_read(
    wa: Pair, assessor_conn: Conn, a: uuid.UUID
) -> None:
    # N3a: the stored seq-1 read (actor-synth, trial-1, family EXP) is unscored: the
    # budget is not reserved, so the verdict is blocked and no index applies.
    put(wa, a, reg_for(a, "exp-0", bud="bud-exp-0"), st.empty(a), eid="exp-0")
    reg = reg_for(a)
    put(wa, a, reg, st.empty(a), None, run(a, promo_ledger(GOOD), "v-1"))
    scored = {"scored_seq": "1", "trial_ids": "'{trial-1}'", "decision": "'reject'",
              "reasons": "'{goal:x}'", "src": "verdict_id = 'v-1'"}  # fmt: skip
    other = "(SELECT {} FROM app.assessor_{} WHERE experiment_id = 'exp-0')"
    cases = [
        scored | {"experiment_id": "'exp-0'",  # another experiment's read
                  "protocol_hash": other.format("protocol_hash", "protocol"),
                  "candidate_hash": other.format("content_hash", "candidate")},
        scored | {"decision": "'blocked'", "reasons": "'{assessor_not_registered}'",
                  "assessor_actor_id": "'actor-forged'"},  # not the read's actor
        scored | {"trial_ids": "'{trial-9}'"},  # not the read's trial
    ]  # fmt: skip
    for over in cases:
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            tn.tenant_transaction(assessor_conn, a) as tx,
        ):
            tx.conn.execute(receipt_copy(tx.conn, **over))
    with tn.tenant_transaction(
        assessor_conn, a
    ) as tx:  # the control: only these differ
        tx.conn.execute(receipt_copy(tx.conn, **scored))


def test_qw_app_reads_no_protected_data(
    wa: Pair, app_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(GOOD), "v-1")
    rows = conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON "
        "c.oid = a.attrelid WHERE c.relnamespace = 'app'::regnamespace AND "
        "c.relname = ANY (%s) AND a.attnum > 0 AND NOT a.attisdropped AND "
        "has_column_privilege('qw_app', c.oid, a.attnum, 'SELECT')",
        ([*PROTECTED, "evaluation_receipt"],),
    ).fetchall()
    assert {(t, c) for t, c in rows} == {("evaluation_receipt", c) for c in APP_RECEIPT}
    with tn.tenant_transaction(app_conn, a) as tx:
        assert tx.conn.execute("SELECT decision FROM app.evaluation_receipt"
                               ).fetchall() == [("accept_for_review",)]  # fmt: skip
    probes = [f"SELECT * FROM app.{t}" for t in PROTECTED] + [
        f"SELECT {c} FROM app.evaluation_receipt" for c in
        ("scores", "assessor_actor_id", "plan_id", "scored_seq", "ledger_digest")
    ]  # fmt: skip
    for sql in probes:
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            tn.tenant_transaction(app_conn, a) as tx,
        ):
            tx.conn.execute(sql)


def test_forced_rls_append_only_and_domain_rebuilt_reads(
    wa: Pair, assessor_conn: Conn, app_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(PARTIAL), "v-1")
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    for t in (*PROTECTED, "evaluation_receipt"):
        for tenant, n in ((a, 1), (b, 0)):
            with tn.tenant_transaction(assessor_conn, tenant) as tx:
                row = tx.conn.execute(f"SELECT count(*) FROM app.{t}").fetchone()
                assert row == (n,), t
        for sql in (f"UPDATE app.{t} SET recorded_at = recorded_at",
                    f"DELETE FROM app.{t}"):  # fmt: skip
            with pytest.raises(psycopg.Error):
                conn.execute(sql)  # even the owner

    def raw(sql: str) -> None:  # committed, triggers off (replica role)
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(sql)

    reg = reg_for(a)
    tampers = (
        ("assessor_protocol", "body = jsonb_set(body, '{hypothesis}', '\"forged\"')",
         lambda: put(wa, a, reg, booked(reg))),
        ("evaluation_receipt", "scores = '{\"net_return\": \"9\"}'",
         lambda: assess(wa, a, ledger(), "v-2")),
        ("holdout_access", "body = jsonb_set(body, '{9}', '\"2026-01-01T00:00:00Z\"')",
         lambda: ast_reads(assessor_conn, a)),
    )  # fmt: skip
    for table, change, act in tampers:
        conn.execute(f"CREATE TABLE public.keep AS SELECT * FROM app.{table}")
        raw(f"UPDATE app.{table} SET {change}")
        with pytest.raises(es.ExperimentStoreError, match="integrity"):
            act()
        raw(f"DELETE FROM app.{table}")
        raw(f"INSERT INTO app.{table} SELECT * FROM public.keep")
        conn.execute("DROP TABLE public.keep")
    assert ast_reads(assessor_conn, a)[0][0] == "acc-1"  # restored and verified


def ast_reads(assessor_conn: Conn, t: uuid.UUID) -> list[Any]:
    with tn.tenant_transaction(assessor_conn, t) as tx:
        return ast._reads(tx, "plan-synth-1")


ASSESSOR_TABLES = (*PROTECTED, "evaluation_receipt")


def privileges(conn: Conn, role: str, kind: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT c.relname, a.attname FROM pg_attribute a JOIN pg_class c ON "
        "c.oid = a.attrelid WHERE c.relnamespace = 'app'::regnamespace AND "
        "c.relname = ANY (%s) AND a.attnum > 0 AND NOT a.attisdropped AND "
        "has_column_privilege(%s, c.oid, a.attnum, %s)",
        (list(ASSESSOR_TABLES), role, kind),
    ).fetchall()
    return {(t, c) for t, c in rows}


KINDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def held(conn: Conn, role: str) -> set[tuple[str, str]]:
    """(relation, privilege) the role holds on any app relation, table or column."""
    rows = conn.execute(
        "SELECT c.relname, k FROM pg_class c CROSS JOIN unnest(%s::text[]) k "
        "WHERE c.relnamespace = 'app'::regnamespace AND c.relkind IN "
        "('r', 'p', 'v', 'm', 'S', 'f') AND (has_table_privilege(%s, c.oid, k) "
        "OR (k IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') "
        "AND has_any_column_privilege(%s, c.oid, k)))",
        (list(KINDS), role, role),
    ).fetchall()
    return {(t, k) for t, k in rows}


def test_decision_17_only_qw_assessor_writes_and_reads_assessor_tables(
    wa: Pair, worker_conn: Conn, assessor_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(GOOD), "v-1")
    tn.check_runtime_role(assessor_conn)  # a runtime role, like qw_app and qw_worker
    every = privileges(conn, "qw_migrate", "SELECT")
    assert len(every) == 12 + 9 + 10 + 23  # the four tables' columns
    assert privileges(conn, "qw_assessor", "SELECT") == every
    assert privileges(conn, "qw_assessor", "INSERT") == every  # 0028's full lists
    assert held(conn, "qw_assessor") == {(t, "SELECT") for t in (
        *ASSESSOR_TABLES, "experiment_revision")} | {
        (t, "INSERT") for t in ASSESSOR_TABLES}  # nothing else in app  # fmt: skip
    for kind in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
        assert privileges(conn, "qw_worker", kind) == set(), kind
    assert privileges(conn, "qw_app", "INSERT") == set()
    assert privileges(conn, "qw_app", "SELECT") == {
        ("evaluation_receipt", c) for c in APP_RECEIPT}  # fmt: skip
    probes = [f"SELECT * FROM app.{t}" for t in ASSESSOR_TABLES] + [
        f"SELECT {c} FROM app.evaluation_receipt" for c in ("decision", "scores")
    ] + [receipt_copy(conn, verdict_id="'v-w'", scored_seq="NULL",
                      trial_ids="'{}'", decision="'blocked'",
                      reasons="'{blocked}'")]  # fmt: skip
    for sql in probes:
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            tn.tenant_transaction(worker_conn, a) as tx,
        ):
            tx.conn.execute(sql)
    with tn.tenant_transaction(assessor_conn, a) as tx:
        (v, seq, _, _), = ast.receipts(tx, EXP)  # fmt: skip
        assert (v.verdict_id, seq, v.scores) == ("v-1", 1, GOOD)
    # the worker cannot stand in for the assessor: nothing is stored
    reg1 = reg_for(a, "exp-1", bud="bud-exp-1x")
    with pytest.raises(es.ExperimentStoreError, match="database"):
        put((worker_conn, worker_conn), a, reg1, booked(reg1, "exp-1"), eid="exp-1")
    assert conn.execute("SELECT count(*) FROM app.assessor_protocol WHERE "
                        "experiment_id = 'exp-1'").fetchone() == (0,)  # fmt: skip


def test_a_receipt_without_its_revision_spends_the_read_and_a_retry_reuses_it(
    wa: Pair, app_conn: Conn, assessor_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    reg = reg_for(a)
    good = run(a, promo_ledger(GOOD), "v-1")
    with pytest.raises(es.ExperimentStoreError, match="database"):  # phase 2 fails
        put((app_conn, assessor_conn), a, reg, booked(reg), None, good)
    assert call(app_conn, a, "load", EXP).record.state is St.AUTHORIZED
    with tn.tenant_transaction(assessor_conn, a) as tx:
        assert [(v.verdict_id, seq) for v, seq, _, _ in ast.receipts(tx, EXP)] == [
            ("v-1", 1)
        ]  # committed by the assessor phase: the read is spent
    with pytest.raises(es.ExperimentStoreError, match="holdout_used"):
        put(wa, a, reg, booked(reg), None, run(a, promo_ledger(GOOD), "v-2"))
    worse = run(a, promo_ledger(PARTIAL), "v-1")  # the same id, another verdict
    with pytest.raises(es.ExperimentStoreError, match="verdict_conflict"):
        put(wa, a, reg, booked(reg), None, worse)
    grown = tl.TrialLedger(str(a), (*lg(a, promo_ledger(GOOD)).entries, trial(
        9, tl.Split.DEVELOPMENT, tl.Outcome.FAILED, trial_id="trial-0",
        family="fam-other", access_id=None)))  # fmt: skip
    regrown = replace(good, ledger=grown)  # the same verdict on another ledger
    assert (
        im.derive(reg, EXP, "cand-1", plan(), booked(reg), None, regrown)[1]
        == im.derive(reg, EXP, "cand-1", plan(), booked(reg), None, good)[1]
    )
    with pytest.raises(es.ExperimentStoreError, match="verdict_conflict"):
        put(wa, a, reg, booked(reg), None, regrown)
    done, wrote = put(wa, a, reg, booked(reg), None, good)  # the identical retry
    assert wrote and done.record.state is St.AWAITING_REVIEW
    assert put(wa, a, reg, booked(reg), None, good) == (done, False)
    assert conn.execute("SELECT count(*) FROM app.evaluation_receipt").fetchone() == (
        1,)  # fmt: skip


def test_the_two_phases_never_hold_the_experiment_lock_together(
    wa: Pair, app_conn: Conn, conn: Conn
) -> None:
    for c in (*wa, conn):  # a deadlock fails the test instead of hanging it
        c.execute("SET lock_timeout = '3s'; SET statement_timeout = '20s'")
    with tn.provision_tenant(app_conn) as tx:  # not fixture `a`: no wait before this
        a = tx.tenant_id
    reg = reg_for(a)
    put(wa, a, reg, booked(reg), im.Scheduled(EXP, "bud-exp-1"))
    done, _ = put(wa, a, reg, booked(reg), None, run(a, promo_ledger(GOOD), "v-1"))
    assert done.record.state is St.AWAITING_REVIEW
    reg1 = reg_for(a, "exp-1", bud="bud-exp-1x")
    with conn.transaction():  # the control: a held lock does time out
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     (f"experiment:{a}",))  # fmt: skip
        with pytest.raises(psycopg.errors.LockNotAvailable):
            put(wa, a, reg1, booked(reg1, "exp-1"), eid="exp-1")


def test_a_revision_written_between_the_phases_is_rechecked(
    wa: Pair, runtime_urls: dict[str, str], a: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    reg, good = reg_for(a), run(a, promo_ledger(GOOD), "v-1")
    lock, raced = es._lock, list[tuple[es.Revision, bool]]()

    def interleave(tx: tn.TenantTx, shared: bool) -> None:
        if tx.conn is wa[0] and not raced:  # phase 2 starts: another recorder wins
            with (
                psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as w,
                psycopg.connect(runtime_urls["qw_assessor"], autocommit=True) as s,
            ):
                raced.append(put((w, s), a, reg, booked(reg), None, good))
        lock(tx, shared)

    monkeypatch.setattr(es, "_lock", interleave)
    got = put(wa, a, reg, booked(reg), None, good)
    assert raced[0][1] and got == (raced[0][0], False)  # nothing written twice


def test_a_receipt_then_a_refused_transition_in_phase_2(
    wa: Pair, assessor_conn: Conn, runtime_urls: dict[str, str], a: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    # Our insufficient_evidence verdict v-1 (unscored) is committed in phase 1; before
    # phase 2 another recorder decides v-2 (awaiting_review, final), so phase 2 must
    # refuse v-1 as a transition, not leave it to the database.
    reg, lock, raced = reg_for(a), es._lock, list[tuple[es.Revision, bool]]()

    def interleave(tx: tn.TenantTx, shared: bool) -> None:
        if tx.conn is wa[0] and not raced:
            with (
                psycopg.connect(runtime_urls["qw_worker"], autocommit=True) as w,
                psycopg.connect(runtime_urls["qw_assessor"], autocommit=True) as s,
            ):
                raced.append(put((w, s), a, reg, booked(reg), None,
                                 run(a, promo_ledger(GOOD), "v-2")))  # fmt: skip
        lock(tx, shared)

    monkeypatch.setattr(es, "_lock", interleave)
    with pytest.raises(es.ExperimentStoreError, match="transition"):
        put(wa, a, reg, booked(reg), None, run(a, ledger(), "v-1"))
    assert raced[0][0].record.state is St.AWAITING_REVIEW
    with tn.tenant_transaction(assessor_conn, a) as tx:  # v-1: a receipt, no revision
        assert [v.verdict_id for v, *_ in ast.receipts(tx, EXP)] == ["v-1", "v-2"]
    assert call(assessor_conn, a, "load", EXP) == raced[0][0]


@pytest.fixture
def login_in(conn: Conn, database_url: str) -> Iterator[Callable[..., str]]:
    """Throwaway passwordless LOGIN roles in the given groups (trust auth, as in
    conftest); dropped afterwards."""
    names: list[str] = []

    def make(*groups: str) -> str:
        names.append(name := f"qwtest_mixed_{uuid.uuid4().hex[:12]}")
        conn.execute(
            pgsql.SQL("CREATE ROLE {} LOGIN NOBYPASSRLS IN ROLE {}").format(
                pgsql.Identifier(name),
                pgsql.SQL(", ").join(map(pgsql.Identifier, groups)),
            )
        )
        return make_conninfo(database_url, user=name)

    yield make
    for name in names:
        conn.execute(pgsql.SQL("DROP ROLE {}").format(pgsql.Identifier(name)))


def test_check_runtime_role_refuses_mixed_and_undeclared_logins(
    runtime_urls: dict[str, str], login_in: Callable[..., str]
) -> None:
    roles: tuple[tn.RuntimeRole, ...] = ("qw_app", "qw_worker", "qw_assessor")
    for role in roles:  # each login holds exactly one runtime role
        with tn.runtime_connection(runtime_urls[role], role=role):
            pass
        for other in roles:
            if other != role:
                with (
                    pytest.raises(tn.TenancyError, match="exactly"),
                    tn.runtime_connection(runtime_urls[role], role=other),
                ):
                    pass
    assessor: tuple[tn.RuntimeRole | None, ...] = (None, "qw_assessor")
    for group in ("qw_app", "qw_worker", "qw_ingest", "qw_release"):
        url = login_in("qw_assessor", group)
        for declared in assessor:  # decision 17: never a mixed assessor login
            with (
                pytest.raises(tn.TenancyError, match="no other runtime role"),
                tn.runtime_connection(url, role=declared),
            ):
                pass
    both = login_in("qw_app", "qw_worker")
    with tn.runtime_connection(both):  # undeclared: accepted as before
        pass
    for role in roles[:2]:
        with (
            pytest.raises(tn.TenancyError, match="exactly"),
            tn.runtime_connection(both, role=role),
        ):
            pass
    with (
        pytest.raises(tn.TenancyError, match="unknown"),
        tn.runtime_connection(runtime_urls["qw_app"], role="qw_ingest"),  # type: ignore[arg-type]
    ):
        pass
