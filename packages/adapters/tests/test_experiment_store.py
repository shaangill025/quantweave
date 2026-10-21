"""0026, 0027 and qw_adapters.experiment_store: qw_worker records through
`improvement.record`, qw_app reads, the superuser `conn` observes or tampers. The
SYNTHETIC test_improvement fixtures are scoped to a real tenant id."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from qw_adapters import experiment_store as es
from qw_adapters import tenancy as tn
from qw_adapters.migrations import MIGRATIONS_DIR, migrate
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from qw_domain.budget import BudgetBook
from test_improvement import ASSESSOR, AT, CAPS, EXP, GOOD, cand, promo_ledger, proto
from test_trial_ledger import ledger, plan

pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
OPT = im.Target.RECURSIVE_OPTIMIZER


def reg_for(t: uuid.UUID, eid: str = EXP, target: im.Target = im.Target.STRATEGY,
            bud: str = "bud-exp-1") -> im.ExperimentRegistry:  # fmt: skip
    base = "opt-v0" if target is OPT else "strat-v0"
    p = proto(scope_id=str(t), experiment_id=eid, target=target, baseline_version=base,
              budget_reservation_id=bud, sandbox_profile_id="sbx-synth")  # fmt: skip
    return im.ExperimentRegistry(str(t)).register(p).register_candidate(
        cand(experiment_id=eid, target=target, parent_id=base))  # fmt: skip


def empty(t: uuid.UUID) -> BudgetBook:
    return BudgetBook.open(str(t), AT, CAPS)


def booked(reg: im.ExperimentRegistry, eid: str = EXP) -> BudgetBook:
    book, _ = im.schedule(reg, (eid,), BudgetBook.open(reg.scope_id, AT, CAPS), AT)
    return book


def run(t: uuid.UUID, metrics: dict[str, Any] = GOOD) -> im.Assessment:
    lg = tl.TrialLedger(str(t), promo_ledger(metrics).entries)
    return im.Assessment(lg, ASSESSOR, AT + timedelta(days=1), "v-1")


def put(conn: Conn, t: uuid.UUID, reg: im.ExperimentRegistry, book: BudgetBook,
        *rest: Any, eid: str = EXP) -> tuple[es.Revision, bool]:  # fmt: skip
    with tn.tenant_transaction(conn, t) as tx:
        return es.record(tx, reg, eid, "cand-1", plan(), book, *rest)


def call(conn: Conn, tenant: uuid.UUID, fn: str, *args: Any) -> Any:
    with tn.tenant_transaction(conn, tenant) as tx:
        return getattr(es, fn)(tx, *args)


@pytest.fixture
def tenants(app_conn: Conn) -> tuple[uuid.UUID, uuid.UUID]:
    ids = []
    for _ in range(2):
        with tn.provision_tenant(app_conn) as tx:
            ids.append(tx.tenant_id)
    return ids[0], ids[1]


def test_revisions_only_move_forward_and_reads_verify(
    worker_conn: Conn, app_conn: Conn, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = tenants
    reg = reg_for(a)
    first, wrote = put(worker_conn, a, reg, empty(a))
    assert (first.revision, wrote) == (1, True)
    assert put(worker_conn, a, reg, empty(a))[1] is False  # the same record
    second, _ = put(worker_conn, a, reg, booked(reg), im.Scheduled(EXP, "bud-exp-1"))
    with pytest.raises(es.ExperimentStoreError, match="transition"):
        put(worker_conn, a, reg, empty(a))  # never back
    third, _ = put(worker_conn, a, reg, booked(reg), None, run(a))
    assert third.revision == 3 and third.recorded_at >= second.recorded_at
    with pytest.raises(es.ExperimentStoreError, match="transition"):  # final
        put(worker_conn, a, reg, booked(reg), None, run(a, GOOD | {
            "lookahead_violations": Decimal(1)}))  # fmt: skip
    got = call(app_conn, a, "load", EXP)
    assert got.record.state is im.RecordState.AWAITING_REVIEW
    assert got.record.verdict_id == "v-1" and got.record.requires_human_promotion
    put(worker_conn, a, reg_for(a, "exp-0", OPT, "bud-0"), empty(a), eid="exp-0")
    assert [r.record.experiment_id for r in call(app_conn, a, "page", 5, None)] == [
        "exp-0", EXP]  # fmt: skip
    assert [r.record for r in call(app_conn, a, "page", 5, "exp-0")] == [got.record]
    assert call(app_conn, a, "load", "exp-0").record.parent_optimizer_version == (
        "opt-v0")  # fmt: skip
    assert call(app_conn, a, "load", "exp-unknown") is None
    with pytest.raises(es.ExperimentStoreError, match="reservation"):
        put(worker_conn, a, reg_for(a, "exp-z"), empty(a), eid="exp-z")


def test_only_derived_records_are_stored(
    worker_conn: Conn, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = tenants
    reg = reg_for(a)
    real = im.record(reg, EXP, "cand-1", plan(), booked(reg), None, run(a))
    forged = replace(real, verdict_id="v-forged", budget_reservation_id="bud-forged")
    stripped = replace(
        real,
        reasons=("none:evaluation_receipt_ids", "unavailable:parent_optimizer_version"),
    )
    for rec in (forged, stripped):  # hand-built: no assess ran on them
        with tn.tenant_transaction(worker_conn, a) as tx, pytest.raises(TypeError):
            es.record(tx, rec, EXP, "cand-1", plan(), booked(reg))  # type: ignore[arg-type]
        with tn.tenant_transaction(worker_conn, a) as tx, pytest.raises(TypeError):
            es.record(tx, rec)  # type: ignore[arg-type, call-arg]
    stored, _ = put(worker_conn, a, reg, booked(reg), None, run(a))
    assert stored.record == real and "evidence:synthetic_only" in stored.record.reasons


def test_tenant_isolation_roles_and_append_only(
    worker_conn: Conn, app_conn: Conn, conn: Conn,
    tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:  # fmt: skip
    a, b = tenants
    put(worker_conn, a, reg_for(a), empty(a))
    assert call(app_conn, b, "load", EXP) is None  # foreign reads as unknown
    assert call(app_conn, b, "page", 5, None) == []
    with tn.tenant_transaction(app_conn, b) as tx:
        row = tx.conn.execute("SELECT count(*) FROM app.experiment_revision").fetchone()
        assert row == (0,)  # forced RLS, not only the store's filter
    with pytest.raises(es.ExperimentStoreError, match="scope"):
        put(worker_conn, a, reg_for(b), empty(b))
    p = replace(reg_for(a).protocol(EXP), scope=im.Scope.INSTALLATION)
    shared = im.ExperimentRegistry(str(a)).register(p).register_candidate(cand())
    with pytest.raises(es.ExperimentStoreError, match="scope"):
        put(worker_conn, a, shared, empty(a))
    with pytest.raises(es.ExperimentStoreError, match="database"):  # qw_app: read only
        put(app_conn, a, reg_for(a, "exp-1", bud="bud-1"), empty(a), eid="exp-1")
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        tn.tenant_transaction(worker_conn, a) as tx,  # another tenant's row
    ):
        tx.conn.execute(copy("experiment_id || 'x'", "%s", "%s", "1", "'bud-x'",
                             "previous_hash"), (b, str(b)))  # fmt: skip
    for sql in ("UPDATE app.experiment_revision SET state = 'failed'",
                "DELETE FROM app.experiment_revision"):  # fmt: skip
        with pytest.raises(psycopg.Error):
            conn.execute(sql)  # even the owner: append-only


def copy(eid: str, tenant: str, scope_id: str, rev: str, bud: str, prev: str,
         state: str = "state", cand_v: str = "candidate_version",
         at: str = "clock_timestamp()", decided: str = "verdict_id, decision",
         target: str = "target") -> str:  # fmt: skip
    """An INSERT copying the latest stored row with the named columns replaced."""
    return (f"INSERT INTO app.experiment_revision SELECT {tenant}, {eid}, {rev}, "
            f"{target}, scope, {scope_id}, {state}, baseline_version, {cand_v}, "
            "hypothesis, protocol_id, protected_assessor_version, data_snapshot_ids, "
            f"{bud}, sandbox_profile_id, candidate_hash, parent_optimizer_version, "
            f"{decided}, reasons, {at}, {prev}, event_hash "
            "FROM app.experiment_revision ORDER BY revision DESC LIMIT 1")  # fmt: skip


def test_each_database_rule_refuses_a_raw_insert(
    worker_conn: Conn, conn: Conn, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = tenants
    reg = reg_for(a)
    put(worker_conn, a, reg, booked(reg), im.Scheduled(EXP, "bud-exp-1"))
    nxt = ("experiment_id", "tenant_id", "scope_id", "revision + 1",
           "budget_reservation_id", "event_hash")  # fmt: skip
    new = ("'exp-n'", "tenant_id", "scope_id", "1", "'bud-n'", "NULL")
    cases = [  # each breaks one rule; as the owner, so only the database applies
        copy(*nxt, state="'proposed'"),  # authorized back to proposed
        copy(*nxt, cand_v="'cand-swap'"),  # identity changed
        copy(*nxt, at="clock_timestamp() + interval '2 min'"),  # ahead of the clock
        copy(*new[:2], "'other'", *new[3:]),  # scope id is not the tenant
        copy(*new, decided="'v-x', 'reject'"),  # a decision without its state
        copy(*new, state="'rejected'"),  # a decided state without a decision
        copy(*new, target="'application_code'"),  # code not blocked
        copy(*new, state="'proposed'", target="'application_code'"),  # 0.5.0 form
        copy(*new, state="'proposed'", target="'recursive_optimizer'"),  # no lineage
        copy(*new, state="'blocked'"),  # blocked with no gate or blocked verdict
        copy(*new, state="'failed'", decided="'v-x', 'insufficient_evidence'"),
        copy(*new, state="'failed'"),  # 0.6.0 never stores failed
        copy(*new, decided="'v-x', 'insufficient_evidence'"),  # not its state
    ]
    for sql in cases:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(sql)
    thin = replace(run(a), ledger=tl.TrialLedger(str(a), ledger().entries))
    put(worker_conn, a, reg, booked(reg), None, thin)  # insufficient_evidence
    for sql in (
        copy(*nxt),  # the same verdict id again
        copy(*nxt, state="'authorized'", decided="NULL, NULL"),
    ):  # back
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(sql)
    put(worker_conn, a, reg, booked(reg), None, replace(run(a), verdict_id="v-2"))
    with pytest.raises(psycopg.errors.CheckViolation):  # a decision ...
        conn.execute(copy(*nxt, decided="'v-3', decision"))  # ... nothing follows
    with pytest.raises(psycopg.errors.CheckViolation):  # not even another decision
        conn.execute(copy(*nxt, state="'rejected'", decided="'v-3', 'reject'"))


def test_tampered_or_missing_revisions_are_integrity(
    worker_conn: Conn, app_conn: Conn, conn: Conn,
    tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:  # fmt: skip
    a, _ = tenants
    reg = reg_for(a)
    put(worker_conn, a, reg, empty(a))
    put(worker_conn, a, reg, booked(reg), im.Scheduled(EXP, "bud-exp-1"))
    expected = call(app_conn, a, "load", EXP).record
    tampers = (
        "UPDATE app.experiment_revision SET hypothesis = 'SYNTHETIC forged'",
        "UPDATE app.experiment_revision SET reasons = '{}' WHERE revision = 2",
        "DELETE FROM app.experiment_revision WHERE revision = 1",
    )
    conn.execute("CREATE TABLE public.keep AS SELECT * FROM app.experiment_revision")

    def raw(*sql: str) -> None:  # committed, triggers off (replica role)
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            for statement in sql:
                conn.execute(statement)

    for sql in tampers:
        raw(sql)
        for fn, args in (("load", (EXP,)), ("page", (5, None))):
            with pytest.raises(es.ExperimentStoreError, match="integrity"):
                call(app_conn, a, fn, *args)
        with pytest.raises(es.ExperimentStoreError, match="integrity"):
            put(worker_conn, a, reg, booked(reg), None, run(a))
        raw(
            "DELETE FROM app.experiment_revision",
            "INSERT INTO app.experiment_revision SELECT * FROM public.keep",
        )
    assert call(app_conn, a, "load", EXP).record == expected


def test_reassessment_appends_a_new_verdict_and_never_resets_the_budget(
    worker_conn: Conn, app_conn: Conn, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, _ = tenants
    reg, b = reg_for(a), None
    b = booked(reg)
    thin = replace(run(a), ledger=tl.TrialLedger(str(a), ledger().entries))
    first, _ = put(worker_conn, a, reg, b, im.Scheduled(EXP, "bud-exp-1"))
    ie, _ = put(worker_conn, a, reg, b, None, thin)
    assert ie.record.state is im.RecordState.INSUFFICIENT_EVIDENCE
    with pytest.raises(es.ExperimentStoreError, match="transition"):  # same id
        put(worker_conn, a, reg, b, None, replace(run(a), verdict_id="v-1"))
    lost = b.release("bud-exp-1")  # a re-assessment never re-reserves
    blocked, _ = put(worker_conn, a, reg, lost, None, replace(run(a), verdict_id="v-2"))
    assert blocked.record.state is im.RecordState.BLOCKED
    assert "blocked:budget_not_reserved" in blocked.record.reasons
    with pytest.raises(es.ExperimentStoreError, match="transition"):  # v-1 again
        put(worker_conn, a, reg, b, None, replace(run(a), verdict_id="v-1"))
    done, _ = put(worker_conn, a, reg, b, None, replace(run(a), verdict_id="v-3"))
    assert done.record.state is im.RecordState.AWAITING_REVIEW
    assert done.revision == 4 and done.record.budget_reservation_id == (
        first.record.budget_reservation_id)  # fmt: skip
    assert call(app_conn, a, "load", EXP) == done


GOLDEN_050 = "1270a372d23a5d526b85845e6bffdbe2eb37de5a5bb22c1f48429d0b3b6b02f9"


def legacy(t: uuid.UUID, eid: str = EXP, target: im.Target = im.Target.STRATEGY,
           bud: str = "bud-exp-1") -> list[im.ExperimentRecord]:  # fmt: skip
    """0.5.0 (PR #126) records of one experiment, as 0026 stored them."""
    reg = reg_for(t, eid, target, bud)
    first = im.record(reg, eid, "cand-1", plan(), empty(t))
    if target is OPT:  # 0.5.0: code and optimizer stayed proposed
        return [replace(first, state=im.RecordState.PROPOSED)]
    old = tuple(sorted({*first.reasons} - {"none:parent_optimizer_version"} | {
        "unavailable:parent_optimizer_version"}))  # fmt: skip
    auth = replace(first, state=im.RecordState.AUTHORIZED, reasons=old)
    failed = replace(auth, state=im.RecordState.FAILED, verdict_id="v-1",
                     decision=im.Decision.INSUFFICIENT, reasons=tuple(sorted({
                         *old, "insufficient:no_qualifying_trial",
                         "none:evaluation_receipt_ids"})))  # fmt: skip
    return [replace(first, reasons=old), auth, failed]


def raw_put(conn: Conn, t: uuid.UUID, n: int, rec: im.ExperimentRecord,
            prev: str | None) -> str:  # fmt: skip
    """Insert `rec` as revision `n` with a correct hash (as the owner)."""
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    h = es.revision_hash(t, n, rec, row[0], prev)
    values = {**rec.wire(), "tenant_id": t, "revision": n, "recorded_at": row[0],
              "previous_hash": prev, "event_hash": h}  # fmt: skip
    conn.execute(f"INSERT INTO app.experiment_revision ({', '.join(values)}) "
                 f"VALUES ({', '.join(['%s'] * len(values))})",
                 tuple(values.values()))  # fmt: skip
    return h


def test_a_0_5_0_row_after_a_0_6_0_row_is_integrity(
    worker_conn: Conn, app_conn: Conn, conn: Conn,
    tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:  # fmt: skip
    a, _ = tenants
    first, _ = put(worker_conn, a, reg_for(a), empty(a))  # 0.6.0 proposed
    old = legacy(a)[1]  # 0.5.0 authorized: the database rules alone accept it
    raw_put(conn, a, 2, old, first.digest)
    for fn, args in (("load", (EXP,)), ("page", (5, None))):
        with pytest.raises(es.ExperimentStoreError, match="integrity"):
            call(app_conn, a, fn, *args)


def test_0026_rows_stay_readable_after_0027(conn: Conn, tmp_path: Any) -> None:
    old = tmp_path / "sql"
    old.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("0*.sql")):
        if f.name < "0027":
            shutil.copy(f, old / f.name)
    migrate(conn, old)
    with tn.provision_tenant(conn) as tx:
        a = tx.tenant_id
    stored = {EXP: legacy(a), "exp-0": legacy(a, "exp-0", OPT, "bud-0")}
    for chain in stored.values():
        prev = None
        for n, rec in enumerate(chain, 1):
            prev = raw_put(conn, a, n, rec, prev)
    assert migrate(conn)[:2] == [27, 28]  # later migrations follow
    got = call(conn, a, "page", 5, None)
    assert [r.record for r in got] == [stored["exp-0"][0], stored[EXP][2]]
    assert got[1].record.state is im.RecordState.FAILED  # served as stored
    golden = uuid.UUID("00000000-0000-4000-8000-000000000001")
    fixed = replace(stored[EXP][2], experiment_id="exp-golden",
                    scope_id=str(golden), hypothesis="SYNTHETIC golden",
                    protocol_id="ab" * 32, candidate_hash="cd" * 32,
                    data_snapshot_ids=("ds-syn",), budget_reservation_id="bud-golden",
                    protected_assessor_version="assessor-v1")  # fmt: skip
    at = datetime(2026, 10, 20, 12, tzinfo=UTC)  # pinned under 0.5.0 (base a6c5283)
    assert es.revision_hash(golden, 2, fixed, at,
                            "ef" * 32) == GOLDEN_050  # fmt: skip
    with pytest.raises(es.ExperimentStoreError, match="transition"):  # failed: final
        put(conn, a, reg_for(a), booked(reg_for(a)), None, run(a))
    reg0 = reg_for(a, "exp-0", OPT, "bud-0")
    nxt, wrote = put(conn, a, reg0, empty(a), eid="exp-0")  # proposed -> blocked
    assert wrote and nxt.revision == 2 and nxt.record.state is im.RecordState.BLOCKED
    with pytest.raises(psycopg.errors.CheckViolation):  # new rows: 0.6.0 only
        conn.execute(copy("'exp-n'", "tenant_id", "scope_id", "1", "'bud-n'", "NULL",
                          state="'proposed'"))  # fmt: skip
