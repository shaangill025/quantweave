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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from qw_domain.research_data import Window
from test_improvement import ASSESSOR, AT, EVAL, GOOD, cand, failed_dev, promo_ledger
from test_trial_ledger import DS, PROMO, T0, evaluation, ledger, plan, trial

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
    return im.Assessment(lg(t, base), ASSESSOR, AT + timedelta(days=1), vid,
                         evaluation=EVAL)  # fmt: skip


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


# A strategy rejection's adjustment as the adjustment guard requires (the copied
# row's chain count), so raw copies that score a receipt reach the rule they probe.
ADJ = (
    "jsonb_build_object('chain_trials', chain_trials::text, 'z_widened', '2', "
    "'baseline.bl-hold.excess_lower_widened', '-0.001')"
)


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
              "reasons": "'{goal:x}'", "src": "verdict_id = 'v-1'",
              "adjustment": ADJ}  # fmt: skip
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
    assert len(every) == 13 + 9 + 10 + 26  # the four tables' columns
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


# Owner decision 18: a promotion window whose holdout was scored for one plan is
# never scored again for another plan of the same tenant, target and baseline.
EARLY = Window(datetime(2021, 7, 1, tzinfo=UTC), datetime(2022, 1, 1, tzinfo=UTC))
MID = Window(datetime(2022, 3, 1, tzinfo=UTC), datetime(2022, 9, 1, tzinfo=UTC))
RESEARCH, STRAT = im.Target.RESEARCH, im.Target.STRATEGY  # improvement targets
GOOD_R = {"net_return": Decimal("0.02"), "unsupported_material_facts": Decimal(0)}


def other_plan(pid: str, promotion: Window | None = None) -> tl.SplitPlan:
    bound = evaluation(pid).content_hash  # each plan freezes its own T033 protocol
    if promotion is None:  # the same windows as plan-synth-1
        return plan(plan_id=pid, protocol_hash=bound)
    return plan(plan_id=pid, promotion=promotion, protocol_hash=bound,
                train=Window(datetime(2020, 1, 1, tzinfo=UTC),
                             datetime(2021, 1, 1, tzinfo=UTC)),
                development=Window(datetime(2021, 2, 1, tzinfo=UTC),
                                   datetime(2021, 6, 1, tzinfo=UTC)))  # fmt: skip


def reg_on(t: uuid.UUID, eid: str, sp: tl.SplitPlan, baseline: str = "strat-v0",
           retest_of: str | None = None,
           target: im.Target = STRAT) -> im.ExperimentRegistry:  # fmt: skip
    base = reg_for(t, eid, target, f"bud-{eid}").protocol(eid)
    p = replace(base, plan_id=sp.plan_id, plan_digest=im.plan_digest(sp),
                baseline_version=baseline, retest_of=retest_of)  # fmt: skip
    return im.ExperimentRegistry(str(t)).register(p).register_candidate(
        cand(experiment_id=eid, target=target, parent_id=baseline))  # fmt: skip


def read_on(t: uuid.UUID, sp: tl.SplitPlan, eid: str,
            outcome: tl.Outcome = tl.Outcome.COMPLETED,
            metrics: dict[str, Decimal] = GOOD) -> tl.TrialLedger:  # fmt: skip
    """The plan's one assessor read and its promotion trial (family `eid`)."""
    key = cand(experiment_id=eid).trial_key
    lg = tl.TrialLedger(str(t)).register_plan(sp, DS)
    lg, _ = lg.request_access(f"acc-{eid}", sp.plan_id, f"trial-{eid}", key,
                              tl.Split.PROMOTION, sp.promotion, None, "actor-synth",
                              tl.Role.ASSESSOR, T0 + timedelta(hours=1))  # fmt: skip
    return lg.record(trial(1, tl.Split.PROMOTION, outcome, trial_id=f"trial-{eid}",
                           plan_id=sp.plan_id, access_id=f"acc-{eid}", family=eid,
                           material_hash=key.material_hash, metrics=metrics,
                           config_hash=key.config_hash,
                           protocol_hash=sp.protocol_hash))  # fmt: skip


def put_on(wa: Pair, t: uuid.UUID, eid: str, sp: tl.SplitPlan, lg: Any = None,
           baseline: str = "strat-v0", vid: str = "v-1",
           retest_of: str | None = None,
           target: im.Target = im.Target.STRATEGY) -> es.Revision:  # fmt: skip
    reg = reg_on(t, eid, sp, baseline, retest_of, target)
    run_ = None if lg is None else im.Assessment(lg, ASSESSOR, AT + timedelta(days=1),
                                                 vid, evaluation=evaluation(
                                                     sp.plan_id))  # fmt: skip
    out = im.Scheduled(eid, f"bud-{eid}") if lg is None else None
    rev: es.Revision = es.record(*wa, t, reg, eid, "cand-1", sp, booked(reg, eid),
                                 out, run_)[0]  # fmt: skip
    return rev


def protocols(conn: Conn, eid: str) -> int:
    row = conn.execute("SELECT count(*) FROM app.assessor_protocol WHERE "
                       "experiment_id = %s", (eid,)).fetchone()  # fmt: skip
    assert row is not None
    return int(row[0])


def test_decision_18_a_scored_window_refuses_a_new_overlapping_plan(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    unscored = put_on(
        wa,
        a,
        "exp-u",
        other_plan("plan-u"),
        read_on(a, other_plan("plan-u"), "exp-u", tl.Outcome.FAILED),
    )
    assert unscored.record.state is St.INSUFFICIENT_EVIDENCE  # read, not scored
    assert assess(wa, a, promo_ledger(GOOD), "v-1").record.state is (
        St.AWAITING_REVIEW)  # PROMO is now used (plan-synth-1)  # fmt: skip
    head = Window(datetime(2023, 1, 1, tzinfo=UTC), PROMO.start + timedelta(days=1))
    for eid, sp, base in (
        ("exp-2", other_plan("plan-2"), "strat-v0"),  # the same window
        ("exp-3", other_plan("plan-3", head), "strat-v0"),  # one shared day
        ("exp-b", other_plan("plan-b"), "strat-v1"),  # a relabelled baseline
    ):  # owner: tenant + target, whatever the baseline
        with pytest.raises(es.ExperimentStoreError, match="holdout_window_used"):
            put_on(wa, a, eid, sp, baseline=base)
        assert protocols(conn, eid) == 0  # nothing of the new plan is stored
    # another target's window is its own; windows are half-open: one ending where
    # PROMO starts shares nothing (both bound, then scored)
    edge = Window(datetime(2023, 1, 1, tzinfo=UTC), PROMO.start)
    for eid, sp, target, metrics in (
        ("exp-t", other_plan("plan-t"), RESEARCH, GOOD_R),
        ("exp-g", other_plan("plan-g", edge), im.Target.STRATEGY, GOOD),
    ):  # fmt: skip
        put_on(wa, a, eid, sp, target=target)
        got = put_on(wa, a, eid, sp, read_on(a, sp, eid, metrics=metrics),
                     target=target)  # fmt: skip
        assert got.record.state is St.AWAITING_REVIEW, eid
    put_on(wa, a, "exp-e", other_plan("plan-e", EARLY))
    done = put_on(
        wa,
        a,
        "exp-e",
        other_plan("plan-e", EARLY),
        read_on(a, other_plan("plan-e", EARLY), "exp-e"),
    )
    assert done.record.state is St.AWAITING_REVIEW


def test_decision_18_a_plan_bound_before_the_scoring_cannot_score_after_it(
    wa: Pair, a: uuid.UUID
) -> None:
    p2 = other_plan("plan-2")
    put_on(wa, a, "exp-2", p2)  # bound and authorized while PROMO is unused
    assess(wa, a, promo_ledger(GOOD), "v-1")  # plan-synth-1 scores PROMO
    with pytest.raises(es.ExperimentStoreError, match="holdout_window_used"):
        put_on(wa, a, "exp-2", p2, read_on(a, p2, "exp-2"))


def test_decision_18_the_database_refuses_a_read_or_score_of_a_used_window(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    p2 = other_plan("plan-2")
    put_on(wa, a, "exp-2", p2)
    put_on(wa, a, "exp-2", p2, read_on(a, p2, "exp-2", tl.Outcome.FAILED))  # unscored
    assess(wa, a, promo_ledger(GOOD), "v-1")  # plan-synth-1 scores PROMO
    score = receipt_copy(conn, src="experiment_id = 'exp-2'", scored_seq="1",
                         trial_ids="'{trial-exp-2}'", decision="'reject'",
                         reasons="'{goal:x}'", adjustment=ADJ)  # fmt: skip
    read = ("INSERT INTO app.holdout_access SELECT tenant_id, plan_id, 2, 'acc-9', "
            "trial_id, family, actor_id, jsonb_set(jsonb_set(body, '{0}', "
            "'\"acc-9\"'), '{12}', '2'), clock_timestamp(), event_hash FROM "
            "app.holdout_access WHERE plan_id = 'plan-2'")  # fmt: skip
    for sql in (score, read):  # as the owner: only the database applies
        with pytest.raises(psycopg.errors.CheckViolation, match="holdout_window_used"):
            conn.execute(sql)
    conn.execute(  # a relabelled baseline on a new plan, past the store's own check
        "INSERT INTO app.assessor_protocol SELECT tenant_id, 'exp-z', protocol_hash, "
        "'plan-z', 'strat-v9', assessor_actor_id, assessor_route_id, "
        "assessor_family_id, assessor_version, body || jsonb_build_object("
        "'experiment_id', 'exp-z', 'plan_id', 'plan-z', 'baseline_version', "
        "'strat-v9'), clock_timestamp(), event_hash, retest_of FROM "
        "app.assessor_protocol WHERE experiment_id = 'exp-2'"
    )
    with pytest.raises(psycopg.errors.CheckViolation, match="holdout_window_used"):
        conn.execute(  # its read of the used window: the target alone decides
            "INSERT INTO app.holdout_access SELECT tenant_id, 'plan-z', 1, 'acc-z', "
            "trial_id, family, actor_id, jsonb_set(body, '{1}', '\"plan-z\"'), "
            "clock_timestamp(), event_hash FROM app.holdout_access "
            "WHERE plan_id = 'plan-2'"
        )
    disjoint = other_plan("plan-e", EARLY)  # the control: another window is free
    put_on(wa, a, "exp-e", disjoint)
    put_on(wa, a, "exp-e", disjoint, read_on(a, disjoint, "exp-e", tl.Outcome.FAILED))
    conn.execute(receipt_copy(conn, src="experiment_id = 'exp-e'", scored_seq="1",
                              trial_ids="'{trial-exp-e}'", decision="'reject'",
                              reasons="'{goal:x}'", adjustment=ADJ))  # fmt: skip
    empty = ("INSERT INTO app.holdout_access SELECT tenant_id, plan_id, 2, 'acc-8', "
             "trial_id, family, actor_id, jsonb_set(jsonb_set(jsonb_set(body, '{0}', "
             "'\"acc-8\"'), '{12}', '2'), '{5}', jsonb_build_array(body -> 5 -> 0, "
             "body -> 5 -> 0)), clock_timestamp(), event_hash FROM "
             "app.holdout_access WHERE plan_id = 'plan-e'")  # fmt: skip
    with pytest.raises(psycopg.errors.CheckViolation, match="empty"):  # [t, t)
        conn.execute(empty)


# Owner decision 19: `retest_of` links an experiment to the one it re-tests; each
# receipt counts the trials of the whole chain.
def dev_trials(lg: tl.TrialLedger, plan_id: str, start: int,
               *families: str) -> tl.TrialLedger:  # fmt: skip
    """Failed development trials, one per family given (counted, never dropped)."""
    for n, family in enumerate(families, start):
        lg = lg.record(trial(n, tl.Split.DEVELOPMENT, tl.Outcome.FAILED,
                             family=family, trial_id=f"trial-d{n}", plan_id=plan_id,
                             access_id=None,
                             protocol_hash=lg.plan(plan_id).protocol_hash))  # fmt: skip
    return lg


def counts(conn: Conn, eid: str) -> list[tuple[int, int]]:
    rows = conn.execute("SELECT family_trials, chain_trials FROM "
                        "app.evaluation_receipt WHERE experiment_id = %s "
                        "ORDER BY recorded_at", (eid,)).fetchall()  # fmt: skip
    return [(f, c) for f, c in rows]


def retest(reg: im.ExperimentRegistry, to: str | None) -> im.ExperimentRegistry:
    p = replace(reg.protocol(EXP), retest_of=to)
    return im.ExperimentRegistry(reg.scope_id).register(p).register_candidate(cand())


def test_decision_19_a_retest_counts_the_trials_of_its_whole_chain(
    wa: Pair, app_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(GOOD), "v-1")  # EXP: one trial in its family
    assert counts(conn, EXP) == [(1, 1)]
    early = other_plan("plan-e", EARLY)
    put_on(wa, a, "exp-r", early, retest_of=EXP)
    lg = dev_trials(read_on(a, early, "exp-r"), "plan-e", 2, "exp-r", "exp-r", EXP, EXP)
    done = put_on(wa, a, "exp-r", early, lg, retest_of=EXP)
    assert done.record.state is St.AWAITING_REVIEW
    # exp-r: 1 promotion + 2 development trials = 3; EXP: max(2 here, 1 stored) = 2
    assert counts(conn, "exp-r") == [(3, 3 + 2)]
    with tn.provision_tenant(app_conn) as tx:
        b = tx.tenant_id
    reg_b = reg_for(b, "exp-b", bud="bud-exp-b")
    put(wa, b, reg_b, booked(reg_b, "exp-b"), eid="exp-b")
    for target in ("exp-none", "exp-b"):  # unknown, or another tenant's
        with pytest.raises(es.ExperimentStoreError, match="retest_of_unknown"):
            put_on(wa, a, "exp-x", other_plan("plan-x", MID), retest_of=target)
        assert protocols(conn, "exp-x") == 0
    with pytest.raises(es.ExperimentStoreError, match="protocol_frozen"):  # a cycle
        put(wa, a, retest(reg_for(a), "exp-r"), booked(reg_for(a)))
    with pytest.raises(es.ExperimentStoreError, match="protocol_frozen"):  # unlinked
        put_on(wa, a, "exp-r", early)


def test_decision_19_the_database_holds_the_link_and_the_chain_count(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(GOOD), "v-1")
    early = other_plan("plan-e", EARLY)
    put_on(wa, a, "exp-r", early, retest_of=EXP)
    put_on(wa, a, "exp-r", early, read_on(a, early, "exp-r", tl.Outcome.FAILED),
           retest_of=EXP)  # unscored; exp-r 1 + EXP 1  # fmt: skip
    assert counts(conn, "exp-r") == [(1, 2)]

    def link(target: str, column: str) -> str:  # a raw copy of exp-r's protocol
        return ("INSERT INTO app.assessor_protocol SELECT tenant_id, 'exp-n', "
                "protocol_hash, plan_id, baseline_version, assessor_actor_id, "
                "assessor_route_id, assessor_family_id, assessor_version, "
                "jsonb_set(body, '{experiment_id}', '\"exp-n\"') || "
                f"jsonb_build_object('retest_of', '{target}'), clock_timestamp(), "
                f"event_hash, {column} FROM app.assessor_protocol "
                "WHERE experiment_id = 'exp-r'")  # fmt: skip

    for sql, err in (
        (link("exp-none", "'exp-none'"), psycopg.errors.ForeignKeyViolation),
        (link("exp-n", "'exp-n'"), psycopg.errors.CheckViolation),  # never itself
        (link("exp-r", "NULL"), psycopg.errors.CheckViolation),  # not the body's
    ):  # fmt: skip
        with pytest.raises(err):
            conn.execute(sql)
    conn.execute(link("exp-r", "'exp-r'"))  # the control: exp-n re-tests exp-r
    low = {"verdict_id": "'v-x'", "decision": "'blocked'", "reasons": "'{blocked}'",
           "src": "experiment_id = 'exp-r'"}  # fmt: skip
    for chain in ("1", "NULL"):  # below exp-r's 1 + EXP's stored 1, or uncounted
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(receipt_copy(conn, chain_trials=chain, **low))
    conn.execute(receipt_copy(conn, chain_trials="2", **low))  # the control


# Owner decisions 35 to 37 through the store: the receipt keeps the adjustment (a
# protected column), and the store's chain count is the k that widens z.
def adjustment(conn: Conn, vid: str) -> dict[str, str] | None:
    row = conn.execute("SELECT adjustment FROM app.evaluation_receipt WHERE "
                       "verdict_id = %s", (vid,)).fetchone()  # fmt: skip
    assert row is not None
    got: dict[str, str] | None = row[0]
    return got


def test_decision_35_the_receipt_keeps_the_widened_bounds(
    wa: Pair, assessor_conn: Conn, app_conn: Conn, conn: Conn, a: uuid.UUID
) -> None:
    done = assess(wa, a, promo_ledger(GOOD), "v-1")
    assert done.record.state is St.AWAITING_REVIEW
    with tn.tenant_transaction(assessor_conn, a) as tx:
        (v, *_), = ast.receipts(tx, EXP)  # fmt: skip
    # k = 1 (one trial in EXP): z' = z, bound 0.02 - 1.959964 * 0.002 = 0.016080072
    assert v.adjustment["chain_trials"] == 1
    bound = v.adjustment["baseline.bl-hold.excess_lower_widened"]
    assert abs(bound - Decimal("0.016080072")) < Decimal("1e-9")
    assert adjustment(conn, "v-1") == {k: str(x) for k, x in v.adjustment.items()}
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        tn.tenant_transaction(app_conn, a) as tx,
    ):
        tx.conn.execute("SELECT adjustment FROM app.evaluation_receipt")


def test_decision_35_the_stored_chain_widens_a_retest(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    # EXP: 1 promotion + 9 failed development trials, family count 10 (it still
    # clears: z' = Phi^-1(1 - 0.05 / 20) ~ 2.807, 0.02 - 0.0056 > 0).
    assert assess(wa, a, failed_dev(promo_ledger(GOOD), 9), "v-1").record.state is (
        St.AWAITING_REVIEW)  # fmt: skip
    assert counts(conn, EXP) == [(10, 10)]
    m = GOOD | {"baseline.bl-hold.excess_mean": Decimal("0.01"),
                "baseline.bl-hold.excess_std_error": Decimal("0.004")}  # fmt: skip
    # Unlinked, on its own plan: k = 1, z' = z: 0.01 - 0.0078399 = 0.0021601 >= 0.
    mid = other_plan("plan-m", MID)
    put_on(wa, a, "exp-m", mid)
    alone = put_on(wa, a, "exp-m", mid, read_on(a, mid, "exp-m", metrics=m))
    assert alone.record.state is St.AWAITING_REVIEW
    # A retest of EXP: k = 1 + 10 = 11, alpha / (2 k) = 0.05 / 22 = 0.0022727;
    # tables Phi(2.83) = 0.997673, Phi(2.84) = 0.997744 interpolate z' ~ 2.8376,
    # so 0.01 - 2.8376 * 0.004 ~ -0.00135 < 0: rejected.
    early = other_plan("plan-e", EARLY)
    put_on(wa, a, "exp-r", early, retest_of=EXP)
    linked = put_on(wa, a, "exp-r", early, read_on(a, early, "exp-r", metrics=m),
                    retest_of=EXP)  # fmt: skip
    assert linked.record.state is St.REJECTED
    assert "rejected:bonferroni:baseline.bl-hold" in linked.record.reasons
    got = adjustment(conn, "v-1") or {}  # EXP's
    adj = {k: Decimal(x) for k, x in (adjustment_of(conn, "exp-r") or {}).items()}
    assert got["chain_trials"] == "10" and adj["chain_trials"] == 11
    assert Decimal("2.837") < adj["z_widened"] < Decimal("2.839")
    assert abs(adj["baseline.bl-hold.excess_lower_widened"] + Decimal("0.00135")) < (
        Decimal("0.00001"))  # fmt: skip
    assert counts(conn, "exp-r") == [(1, 11)]


def adjustment_of(conn: Conn, eid: str) -> dict[str, str] | None:
    row = conn.execute("SELECT adjustment FROM app.evaluation_receipt WHERE "
                       "experiment_id = %s", (eid,)).fetchone()  # fmt: skip
    assert row is not None
    got: dict[str, str] | None = row[0]
    return got


def test_decision_35_the_database_requires_a_consistent_adjustment(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    assess(wa, a, promo_ledger(GOOD), "v-1")  # EXP accepted, k = 1
    t = other_plan("plan-t")  # a research experiment: no adjustment
    put_on(wa, a, "exp-t", t, target=RESEARCH)
    put_on(wa, a, "exp-t", t, read_on(a, t, "exp-t", metrics=GOOD_R), target=RESEARCH)
    lower = "{baseline.bl-hold.excess_lower_widened}"
    for over in (
        {"adjustment": "NULL"},  # a strategy acceptance without its bounds
        {"adjustment": f"jsonb_set(adjustment, '{lower}', '\"-0.001\"')"},  # < 0
        {"adjustment": "jsonb_set(adjustment, '{chain_trials}', '\"2\"')"},  # not k
        {"adjustment": "adjustment - 'z_widened'"},  # no z'
        {
            "adjustment": "adjustment - 'baseline.bl-hold.excess_lower_widened' - "
            "'baseline.bl-cash.excess_lower_widened'"
        },  # no bound
    ):  # as the owner: only the database applies
        with pytest.raises(psycopg.errors.CheckViolation, match="adjustment"):
            conn.execute(receipt_copy(conn, src="verdict_id = 'v-1' AND "
                                      f"experiment_id = '{EXP}'", **over))  # fmt: skip
    with pytest.raises(psycopg.errors.UniqueViolation):  # the control: it passes the
        conn.execute(receipt_copy(conn, src=f"experiment_id = '{EXP}'"))  # rules
    with pytest.raises(psycopg.errors.CheckViolation, match="adjustment"):
        conn.execute(receipt_copy(  # a count-based target carries none
            conn, src="experiment_id = 'exp-t'", scored_seq="NULL", trial_ids="'{}'",
            decision="'blocked'", reasons="'{blocked}'",
            adjustment=f"(SELECT adjustment FROM app.evaluation_receipt "
            f"WHERE experiment_id = '{EXP}')"))  # fmt: skip


def test_review_m1_no_protocol_spends_no_holdout_read(
    wa: Pair, conn: Conn, a: uuid.UUID
) -> None:
    reg = reg_for(a)
    bare = replace(run(a, promo_ledger(GOOD), "v-1"), evaluation=None)
    with pytest.raises(im.ImprovementError, match="evaluation_protocol_missing"):
        put(wa, a, reg, booked(reg), None, bare)
    for table in ("evaluation_receipt", "holdout_access"):  # nothing was stored
        assert conn.execute(f"SELECT count(*) FROM app.{table}").fetchone() == (0,)
    done = assess(wa, a, promo_ledger(GOOD), "v-1")  # the read is still unused
    assert done.record.state is St.AWAITING_REVIEW
