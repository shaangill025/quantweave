"""0026 and qw_adapters.experiment_store: qw_worker records through
`improvement.record`, qw_app reads, the superuser `conn` observes or tampers. The
SYNTHETIC test_improvement fixtures are scoped to a real tenant id."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from qw_adapters import experiment_store as es
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import trial_ledger as tl
from qw_domain.budget import BudgetBook
from test_improvement import ASSESSOR, AT, CAPS, EXP, GOOD, cand, promo_ledger, proto
from test_trial_ledger import plan

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
        copy(*new, target="'application_code'"),  # code past proposed
        copy(*new, state="'proposed'", target="'recursive_optimizer'"),  # no lineage
    ]
    for sql in cases:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(sql)
    put(worker_conn, a, reg, booked(reg), None, run(a))  # a decision ...
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(copy(*nxt))  # ... and nothing follows it


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
