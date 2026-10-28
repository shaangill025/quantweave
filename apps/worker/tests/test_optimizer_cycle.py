"""The `optimizer.cycle` job and the GATE-LIFTED recursion chain on the real stores
(T050 increments 8 and 9: the job reserves the verdict's cost, decision 98): real
logins, SYNTHETIC data and authorizations, test-only HMAC keys."""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
import test_release as tr
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import budget_store as bs
from qw_adapters import jobs as qj
from qw_adapters import optimizer_fixture_store as ofs
from qw_adapters import optimizer_store as os_
from qw_adapters import release_store as rs
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import release as rl
from qw_domain.jobs import Backoff, JobState, Pool, Priority
from qw_worker import __main__ as entry
from qw_worker import budget_sweep as sweep
from qw_worker import optimizer_cycle as oc
from qw_worker.config import WorkerSettings
from qw_worker.loop import Worker
from test_improvement import ASSESSOR, PRODUCER
from test_optimizer_improvement import (
    EXP,
    LIMITS,
    V0,
    gate_lifted_install,  # noqa: F401  (a pytest fixture, used by name)
)
from test_optimizer_measure import MP, RP, V1R
from test_research_improvement import BASE, DEV, FIXTURE, PLAN
from test_research_improvement import protocol as research_protocol


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ao = _load("qw_assessor_optimizer_tests", Path(__file__).parent /
           "test_assessor_optimizer.py")  # fmt: skip
pg_admin_url, database_url, conn = ao.pg_admin_url, ao.database_url, ao.conn
runtime_urls, app_conn, ot, tenant, state = (ao.runtime_urls, ao.app_conn, ao.ot,
                                             ao.tenant, ao.state)  # fmt: skip
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
INST, now = ot.INST, ot.now
SETTINGS = WorkerSettings("synthetic-cycle", Pool.EXPERIMENT,
                          job_backoff=Backoff(timedelta(0), timedelta(0)))  # fmt: skip
SHOWN = {None, "optimizer_refused", "installation_scope", "database"}
DEP = tr.deployment().deployment


@pytest.fixture
def logins(runtime_urls: dict[str, str]) -> Iterator[tuple[Conn, Conn, Conn]]:
    with (
        tn.runtime_connection(runtime_urls["qw_worker"], role="qw_worker") as w,
        tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as a,
        tn.runtime_connection(runtime_urls["qw_release"], role="qw_release") as r,
    ):
        yield w, a, r


def setup(logins: tuple[Conn, Conn, Conn], p: im.Protocol = MP) -> None:
    """The operator preregisters the fixture, logs the protocol and records the
    deployment (no install can be stored)."""
    _, asr, rel = logins
    assert ofs.preregister(asr, INST, PLAN, FIXTURE)
    os_.append(asr, INST, p)
    assert rs.record_deployment(rel, DEP)


def operator(app_conn: Conn, conn: Conn) -> uuid.UUID:
    """The bootstrap tenant, with the owner's SYNTHETIC spending authorization."""
    t: uuid.UUID = tenant(app_conn, bootstrap=True)
    ot.authorize(conn, t)
    return t


def held(wrk: Conn, t: uuid.UUID) -> bs.StoredBook | None:
    """`t`'s book holding MP's reservation (qw_worker reads its own tenant)."""
    with tn.tenant_transaction(wrk, t) as tx:
        return bs.holding(tx, MP.budget_reservation_id)


def job(cid: str = "cycle-v0-job", **kw: Any) -> oc.CycleJob:
    return replace(oc.CycleJob(INST, EXP, cid, DEP.deployment_id, MP, V0, BASE, DEV,
                               LIMITS), **kw)  # fmt: skip


def submit(wrk: Conn, t: uuid.UUID, j: oc.CycleJob | object) -> uuid.UUID:
    with tn.tenant_transaction(wrk, t) as tx:
        if isinstance(j, oc.CycleJob):
            return oc.submit(tx, j)
        jid, _ = qj.enqueue(tx, oc.KIND, str(uuid.uuid4()), Priority.EXPERIMENT,
                            j)  # type: ignore[arg-type]  # fmt: skip
        return jid


def tick(wrk: Conn, n: int = 10) -> list[tuple[uuid.UUID, JobState | None]]:
    s = replace(SETTINGS, jobs_per_tick=n)
    return Worker(wrk, s, handlers=oc.HANDLERS).tick().jobs


def test_the_cycle_job_stores_a_baseline_cycle(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, conn: Conn
) -> None:
    wrk, asr, _ = logins
    setup(logins)
    t = operator(app_conn, conn)
    jid = submit(wrk, t, job())
    assert submit(wrk, t, job()) == jid  # idempotent on the payload
    assert (
        Worker(wrk, replace(SETTINGS, pool=Pool.STANDARD), handlers=oc.HANDLERS)
        .tick()
        .jobs
        == []
    )  # experiment pool only
    assert tick(wrk) == [(jid, JobState.SUCCEEDED)]
    (s,) = os_.load_cycles(asr, INST, EXP)
    r = s.receipt
    assert (r.cycle_id, r.authority, r.runner_hash) == (
        "cycle-v0-job", "baseline:opt-v0", V0.content_hash)  # fmt: skip
    assert (r.base_hash, r.fixture_hash) == (BASE.content_hash, DEV.content_hash)
    assert r.at <= s.recorded_at
    assert oc.CycleJob.from_payload(job().to_payload()).to_payload() == (
        job().to_payload())  # fmt: skip
    first = held(wrk, t)  # decision 98: reserved before the cycle, committed with it
    assert first is not None and first.book.scope_id == str(t)
    res = first.book.get(MP.budget_reservation_id)
    assert (res.category.value, res.reserved, res.provider_account_ref,
            res.max_output_tokens, res.status.value, res.deadline - res.created_at) == (
        "improvement", MP.max_cost, MP.provider_account_ref, MP.max_output_tokens,
        "reserved", MP.max_duration)  # fmt: skip
    assert res.created_at <= r.at
    jid = submit(wrk, t, job("cycle-v0-again"))  # another cycle: the same reservation
    assert tick(wrk) == [(jid, JobState.SUCCEEDED)]
    assert held(wrk, t) == first and len(os_.load_cycles(asr, INST, EXP)) == 2


def test_no_authorization_refuses_before_anything_is_measured(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, conn: Conn,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """No owner-recorded authorization: a typed refusal, no book, no cycle; other
    budget refusals (here `deadline_too_far`) roll back the same way."""
    wrk, asr, _ = logins
    setup(logins)
    t, runs = tenant(app_conn, bootstrap=True), list[object]()
    real = oi.run_cycle

    def spy(*a: Any, **k: Any) -> Any:
        runs.append(a)
        return real(*a, **k)

    monkeypatch.setattr(oi, "run_cycle", spy)
    jid = submit(wrk, t, job())
    with caplog.at_level(logging.WARNING, "qw_worker.optimizer_cycle"):
        assert tick(wrk) == [(jid, JobState.FAILED)]
    assert state(wrk, t, jid) == (JobState.FAILED, "optimizer_refused")
    assert "optimizer cycle refused: no_authorization" in caplog.text
    assert (runs, held(wrk, t), os_.load_cycles(asr, INST, EXP)) == ([], None, ())
    ot.authorize(conn, t)
    long = replace(MP, max_duration=timedelta(days=8))  # past MAX_DEADLINE
    jid = submit(wrk, t, job("cycle-v0-long", protocol=long))
    with caplog.at_level(logging.WARNING, "qw_worker.optimizer_cycle"):
        assert tick(wrk) == [(jid, JobState.FAILED)]
    assert "optimizer cycle refused: deadline_too_far" in caplog.text
    assert (runs, held(wrk, t)) == ([], None)


def test_the_entry_point_registers_the_cycle_handler(
    runtime_urls: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Worker] = []
    monkeypatch.setattr(Worker, "run", lambda w, max_ticks=None: started.append(w))
    monkeypatch.setattr(entry, "install_signal_handlers", lambda worker: None)
    monkeypatch.setenv("QW_DATABASE_URL", runtime_urls["qw_worker"])
    monkeypatch.delenv("QW_ASSESSOR_DATABASE_URL", raising=False)
    assert entry.main() == 0 and dict(started[0].handlers) == {
        oc.KIND: oc.handle, sweep.KIND: sweep.handle}  # fmt: skip


def test_another_tenant_or_a_hosted_edition_is_refused_before_any_read(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrk, asr, _ = logins
    setup(logins)
    reads: list[object] = []
    monkeypatch.setattr(oc.CycleJob, "from_payload",
                        classmethod(lambda cls, w: reads.append(w)))  # fmt: skip
    monkeypatch.setattr(oc, "run", lambda *a: reads.append(a))
    other = tenant(app_conn)
    jid = submit(wrk, other, job())
    assert tick(wrk) == [(jid, JobState.FAILED)]  # no bootstrap row: hosted
    assert state(wrk, other, jid) == (JobState.FAILED, "installation_scope")
    tenant(app_conn, bootstrap=True)
    jid = submit(wrk, other, job("cycle-v0-other"))
    assert tick(wrk) == [(jid, JobState.FAILED)]
    assert state(wrk, other, jid) == (JobState.FAILED, "installation_scope")
    assert reads == [] and os_.load_cycles(asr, INST, EXP) == ()


def test_refusals_are_generic_and_faults_retry(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture, conn: Conn,
) -> None:  # fmt: skip
    wrk, asr, _ = logins
    setup(logins)
    t = operator(app_conn, conn)
    v1 = oi.OptimizerConfig.from_payload("opt-v1", V1R.to_payload())
    cases: list[tuple[object, str]] = [
        ({"cycle_id": "x"}, "payload_invalid: exactly"),
        (job().to_payload() | {"limits": ["2", "2", "20", "0.50"]},
         "payload_invalid: not canonical"),
        (job(runner=v1), "cycle_blocked: not_installed"),  # no install: no authority
        (job(deployment_id="deploy-none"), "deployment_unknown: deploy-none"),
        (job(protocol=replace(MP, scope_id="inst-other")), "scope: exp-opt-1"),
        (job(limits=replace(LIMITS, patience=1), dev=replace(DEV, split=FIXTURE.split)),
         "not_development: fixture-synth-dev"),
        # the runner is V0, but another field of the protocol is forged (0051)
        (job(protocol=replace(MP, hypothesis="SYNTHETIC: x")), "protocol_mismatch"),
        (job(protocol=replace(MP, producer=replace(MP.producer, route_id="route-x"))),
         "protocol_mismatch"),
        # forged terms are reserved first, then refused and rolled back with it
        (job(protocol=replace(MP, max_cost=MP.max_cost + MP.max_cost)),
         "protocol_mismatch"),
    ]  # fmt: skip
    for payload, detail in cases:
        caplog.clear()
        jid = submit(wrk, t, payload)
        with caplog.at_level(logging.WARNING, "qw_worker.optimizer_cycle"):
            assert tick(wrk) == [(jid, JobState.FAILED)], detail
        assert state(wrk, t, jid) == (JobState.FAILED, "optimizer_refused"), detail
        assert f"optimizer cycle refused: {detail}" in caplog.text, detail
    assert held(wrk, t) is None  # every refusal rolled its reservation back
    jid = submit(wrk, t, job())
    assert tick(wrk) == [(jid, JobState.SUCCEEDED)]
    stored = held(wrk, t)
    assert stored is not None  # the frozen terms, not a forged job's
    assert stored.book.get(MP.budget_reservation_id).reserved == MP.max_cost
    jid = submit(wrk, t, job(cid=job().cycle_id, limits=replace(LIMITS, max_depth=1)))
    assert tick(wrk) == [(jid, JobState.FAILED)]  # 0047: the cycle id is taken
    assert state(wrk, t, jid) == (JobState.FAILED, "optimizer_refused")

    def fault(*a: Any) -> Any:
        raise os_.OptimizerStoreError("database", "OperationalError")

    monkeypatch.setattr(os_, "append_cycle", fault)
    jid = submit(wrk, t, job("cycle-v0-fault"))
    assert tick(wrk, 1) == [(jid, JobState.QUEUED)]
    assert state(wrk, t, jid) == (JobState.QUEUED, "database")
    with tn.tenant_transaction(wrk, t) as tx:
        shown = {r[0] for r in tx.conn.execute("SELECT last_error FROM app.job")}
    assert shown <= SHOWN and len(os_.load_cycles(asr, INST, EXP)) == 1
    assert held(wrk, t) == stored  # retries and refusals never re-reserve


def test_a_cycle_after_the_reservation_window_is_refused(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, conn: Conn,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Review fix 1: one reservation per experiment (`max_duration` window); a
    cycle after its deadline is refused (`reservation_expired`) before anything
    is measured, and nothing is re-reserved. SYNTHETIC 2-second window."""
    wrk, asr, _ = logins
    short = replace(MP, max_duration=timedelta(seconds=2))
    setup(logins, short)
    t = operator(app_conn, conn)
    jid = submit(wrk, t, job(protocol=short))
    assert tick(wrk) == [(jid, JobState.SUCCEEDED)]
    first = held(wrk, t)
    assert first is not None
    deadline = first.book.get(MP.budget_reservation_id).deadline
    runs = list[object]()
    real = oi.run_cycle

    def spy(*a: Any, **k: Any) -> Any:
        runs.append(a)
        return real(*a, **k)

    monkeypatch.setattr(oi, "run_cycle", spy)
    time.sleep(max(0.0, (deadline - now(wrk)).total_seconds()) + 0.2)
    jid = submit(wrk, t, job("cycle-v0-late", protocol=short))
    with caplog.at_level(logging.WARNING, "qw_worker.optimizer_cycle"):
        assert tick(wrk) == [(jid, JobState.FAILED)]
    assert state(wrk, t, jid) == (JobState.FAILED, "optimizer_refused")
    assert "optimizer cycle refused: reservation_expired" in caplog.text
    assert (runs, held(wrk, t)) == ([], first)
    assert len(os_.load_cycles(asr, INST, EXP)) == 1


@pytest.mark.usefixtures("gate_lifted_install")
def test_gate_lifted_recursion_chain_on_the_stores(
    logins: tuple[Conn, Conn, Conn], app_conn: Conn, conn: Conn
) -> None:
    """GATE-LIFTED SYNTHETIC. Production keeps every gate: the trial's
    `sandbox_unqualified`, 0050's install refusal and 0047's baseline-only cycles."""
    wrk, asr, rel = logins
    setup(logins)
    t = operator(app_conn, conn)
    jid = submit(wrk, t, job())
    assert tick(wrk) == [(jid, JobState.SUCCEEDED)]  # 1. V0's cycle, by the job
    (s0,) = os_.load_cycles(asr, INST, EXP)
    r0 = s0.receipt
    reg = os_.load(asr, INST, now(asr))  # 2. V1, proposed from the stored receipt
    sc = oi.propose_self_change("cand-opt-1", "opt-v1", r0, reg, EXP, RP, LIMITS, (),
                                now(asr))  # fmt: skip
    assert isinstance(sc, oi.SelfChange) and sc.config.content_hash == (
        V1R.content_hash)  # fmt: skip
    os_.append(asr, INST, sc.candidate)
    req = aro.Request(INST, EXP, "cand-opt-1", r0.cycle_id, "verdict-opt-1",
                      ASSESSOR, now(asr))  # fmt: skip
    assert ot.code(lambda: aro.trial(asr, req)) == "sandbox_unqualified"  # real
    book, scope = aro.operator_book(asr, MP)  # the job's reservation (decision 98)
    assert scope == str(t) and book == held(wrk, t).book  # type: ignore[union-attr]
    b, p, c, cyc = aro._before_gate(asr, req, ot.lifted_assess)  # 3. measured
    run = aro._after_gate(asr, req, lambda: None, b, p, c, cyc,
                          aro.stored_inputs(asr, lambda x: (BASE, DEV, LIMITS)),
                          os_.RunLog(asr, INST), ot.lifted_assess)  # fmt: skip
    assert run.verdict.decision is im.Decision.ACCEPT_FOR_REVIEW and run.wrote
    reg = os_.load(asr, INST, now(asr))  # 4. signed, recorded, installed in-test
    m = rl.prepare_release(reg, "cand-opt-1", "rel-opt-1", rl.Compatibility(1, 1, 1),
                           tr.approval(at=now(asr)))  # fmt: skip
    sr = rl.sign_release(m, tr.KEY)
    assert rs.record_release(rel, reg, sr, keys=tr.KEYS, allow_test_keys=True)
    stored_install = partial(rs.install, wrk, DEP.deployment_id, tr.KEYS, "ev-opt-0",
                             "rel-opt-1", "operator-synth", "stepup-op-0",
                             strategies=tr.NO)  # fmt: skip
    with pytest.raises(rs.ReleaseStoreError, match="database: CheckViolation"):
        stored_install()  # the domain gate is lifted; 0050's trigger refuses it
    stored = rs.load(wrk, DEP.deployment_id, tr.KEYS)
    assert stored is not None and stored.entries == ()
    lg = tr.deployment().install("ev-opt-0", sr, "operator-synth", "stepup-op-0",
                                 now(asr), strategies=tr.NO)  # fmt: skip
    r1 = oi.run_cycle(
        "cycle-v1-1",
        sc.config,
        reg,
        EXP,
        lg,
        BASE,
        DEV,
        LIMITS,
        (r0, sc),
        now(asr),
        accept_synthetic_evidence=True,
    )  # 5.
    assert isinstance(r1, oi.CycleReceipt) and r1.authority == "release:rel-opt-1"
    assert r1.selected is not None
    assert ot.code(partial(os_.append_cycle, wrk, INST, EXP, r1, MP)) == (
        "runner_not_baseline")  # 0047 stores V0 cycles only  # fmt: skip
    jid = submit(wrk, t, job("cycle-v1-job", runner=sc.config))
    assert tick(wrk) == [(jid, JobState.FAILED)]  # the stored head: none
    rp = research_protocol(experiment_id="exp-research-2",
                           budget_reservation_id="bud-research-2",
                           producer=replace(PRODUCER, version="opt-v1"))  # fmt: skip
    nxt = oi.cycle_candidate("cand-research-1", r1, rp, BASE, now(asr))
    lin = oi.lineage(
        "lineage-synth-1",
        reg,
        EXP,
        lg,
        r0,
        r1,
        BASE,
        DEV,
        nxt,
        accept_synthetic_evidence=True,
    )  # 6. built and stored
    os_.append_lineage(asr, INST, lin)
    u = os_.load_lineage(rel, INST, "lineage-synth-1")
    assert u is not None and u == lin.claim
    got = oi.verify_lineage(u, os_.load(rel, INST, now(rel)), lg, BASE, DEV, nxt,
                            accept_synthetic_evidence=True)  # fmt: skip
    assert got == lin and got.claim.v0_cycle == r0
    assert got.claim.next_cycle.cycle_id == "cycle-v1-1"
    assert got.claim.verdict_id == "verdict-opt-1"
    assert "declared_costs_not_reserved" not in got.claim.verdict_limitations
