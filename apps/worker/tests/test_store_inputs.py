"""Owner decision 70's last step (decisions 67, 71, 73, 75, 77 and 78): the
assessor's default `STORE_SOURCE` rebuilds a stored request with `store_inputs`,
which reads every input the request binds by hash from its store, inside the
qw_assessor transaction that loaded the request. qw_worker records the search
space and evaluation protocol, qw_assessor's bind logs the protocol and
candidate, qw_ingest freezes the dataset, the schema owner records the rights
and the base manifest, and qw_worker submits the request. Real logins
(`runtime_connection(url, role=...)`); the superuser `conn` only observes or
tampers. SYNTHETIC data only (test_assessor_run's world, with its rights and its
fact's feed revision recorded in the stores)."""

from __future__ import annotations

import importlib.util
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from qw_adapters import assessment_request_store as ars
from qw_adapters import dataset_store as dst
from qw_adapters import experiment_registry_store as ers
from qw_adapters import experiment_store as es
from qw_adapters import holdout_series_store as hs
from qw_adapters import rights_store as rs
from qw_adapters import strategy_store as ss
from qw_adapters import study_store as sts
from qw_adapters import tenancy as tn
from qw_domain import improvement as im
from qw_domain.instants import ensure_aware_utc
from qw_domain.jobs import JobState
from qw_domain.rights import (
    EntitlementSource,
    Evidence,
    EvidenceKind,
    FeedStatus,
    UseScope,
)
from qw_domain.strategy_registry import ParamKind
from qw_worker import assessor as qa
from test_improvement import EXP


def _load(name: str, path: Path) -> Any:
    """`path` as module `name`, executed once per session."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


proc = _load("qw_assessor_process_tests", Path(__file__).with_name(
    "test_assessor_process.py"))  # fmt: skip
_tests = Path(__file__).parents[3] / "packages" / "adapters" / "tests"
dsets = _load("qw_dataset_store_tests", _tests / "test_dataset_store.py")
pg_admin_url, database_url = proc.pg_admin_url, proc.database_url
conn, runtime_urls, app_conn = proc.conn, proc.runtime_urls, proc.app_conn
logins, owner, ing = proc.logins, proc.owner, proc.ing
run_tests, fx = proc.run_tests, proc.run_tests.fx
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
TERMS, KEEP = run_tests.TERMS, run_tests.KEEP
JOB = qa.AssessmentJob("v-p", EXP, "cand-1")  # the request id: its verdict
CAND2 = fx.cand(run_tests.si.derive(fx.BASE, fx.SPACE, {"lookback": 80}, "cand-2"))


def stores(logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
           skip: str = "") -> Any:  # fmt: skip
    """Every input stored by its own writer, except `skip` (the rights by
    test_assessor_run's `stored_world`)."""
    wrk, asr = logins
    with tn.provision_tenant(app_conn) as tx:
        t = tx.tenant_id
    w = run_tests.stored_world(owner, t)
    if skip != "manifest":
        ss.record_manifest(owner, fx.BASE)  # the schema owner only (0015)
    with tn.tenant_transaction(wrk, t) as tx:  # decision 72
        if skip != "space":
            sts.record_space(tx, fx.SPACE)
        if skip != "evaluation":
            sts.record_evaluation(tx, fx.EVAL)
    if skip != "dataset":
        with tn.tenant_transaction(ing, t) as tx:  # decisions 73 and 83
            assert dst.record(tx, w.ds, TERMS) is True
    with tn.tenant_transaction(asr, t) as tx:
        hs.preregister(tx, w.plan.plan_id, "holdout", w.holdout.series)
        hs.preregister(tx, w.plan.plan_id, "px-synth-1", w.holdout.series)
    if skip != "registry":  # bind logs the protocol and candidate (decision 81)
        es.record(wrk, asr, t, w.reg, EXP, "cand-1", w.plan, w.book,
                  im.Scheduled(EXP, "bud-exp-1"))  # fmt: skip
    return w


def submit(wrk: Conn, w: Any, terms: dst.DatasetTerms = TERMS, **kw: Any) -> uuid.UUID:
    """qw_worker submits with the rights it reads at the database clock."""
    with tn.tenant_transaction(wrk, w.t) as tx:
        rights = dst.research_rights(tx, terms)
        return qa.submit_job(tx, run_tests.request(w, rights=rights, **kw))


def refused(asr: Conn, w: Any, job: qa.AssessmentJob = JOB) -> Any:
    with pytest.raises(Exception) as e:
        qa.STORE_SOURCE(asr, w.t, job)
    assert isinstance(getattr(e.value, "code", None), str), e.value
    return e.value


def test_the_default_source_rebuilds_the_request_from_the_stores_and_runs_it(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    jid = submit(wrk, w)
    with tn.tenant_transaction(asr, w.t) as tx:  # decision 71: logged after submit
        ers.append(tx, CAND2)
    before = run_tests.counts(conn, w.t)
    r = qa.STORE_SOURCE(asr, w.t, JOB)  # `check`ed against the stored request
    assert ars.study_hash(r.study) == ars.study_hash(w.study)
    assert ars.registry_hash(r.registry) == ars.registry_hash(w.reg)  # as of submit
    with pytest.raises(im.ImprovementError, match="unknown_candidate"):
        r.registry.candidate("cand-2")
    m = r.strategies.manifest(fx.BASE.strategy_id, "cand-1")  # decision 78
    assert m.material_hash == fx.DERIVED.material_hash and m == fx.DERIVED
    assert r.dataset == w.ds and r.rights.at > w.rights.at  # the DB clock (77)
    assert (r.ledger, r.book, r.candidate_id) == (w.ledger, w.book, "cand-1")
    assert run_tests.counts(conn, w.t) == before  # rebuilding reads no holdout
    lp = qa.AssessorLoop(wrk, asr, proc.SETTINGS, strategies=run_tests.REG)
    assert lp.source is qa.STORE_SOURCE
    run_tests.crash(monkeypatch, run_tests.si, "holdout_run")  # after the read
    assert lp.run_one() == (jid, JobState.QUEUED)
    monkeypatch.undo()
    assert lp.run_one() == (jid, JobState.SUCCEEDED)  # rebuilt again: attempt 2
    assert [a for a, *_ in run_tests.attempts(conn, w.t)] == [1, 2]
    assert [v.verdict_id for v, *_ in run_tests.stored(asr, w)] == ["v-p"]


@pytest.mark.parametrize(("skip", "want"), [
    ("registry", "registry_missing"), ("space", "search_space_missing"),
    ("evaluation", "evaluation_protocol_missing"), ("manifest", "strategy_missing"),
    ("dataset", "dataset_missing"),
])  # fmt: skip
def test_a_missing_input_fails_the_job_under_its_code(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
    skip: str, want: str,
) -> None:  # fmt: skip
    wrk, _ = logins
    w = stores(logins, owner, ing, app_conn, skip)
    jid = submit(wrk, w)
    before = run_tests.counts(conn, w.t)
    lp = qa.AssessorLoop(*logins, proc.SETTINGS, strategies=run_tests.REG)
    assert lp.run_one() == (jid, JobState.FAILED)  # a refusal: no retry
    assert proc.state(wrk, w.t, jid) == (JobState.FAILED, want)
    assert run_tests.counts(conn, w.t) == before


@pytest.mark.parametrize(
    "key", ["study", "registry", "strategy_manifest", "dataset", "rights"])  # fmt: skip
def test_a_bound_hash_that_differs_is_refused(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
    key: str,
) -> None:  # fmt: skip
    """The stored request re-hashed around another input hash (a forgery that
    passes `load`): the true stores no longer match it."""
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    row = conn.execute("SELECT body, recorded_at FROM app.assessment_request "
                       "WHERE tenant_id = %s", (w.t,)).fetchone()  # fmt: skip
    assert row is not None
    body, at = row
    body["inputs"][key] = "0" * 64
    h = ars._hash(tn.TenantTx(conn, w.t), body, at)
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE app.assessment_request SET body = %s, request_hash = %s "
                     "WHERE tenant_id = %s", (Jsonb(body), h, w.t))  # fmt: skip
    err = refused(asr, w)
    assert (err.code, f"inputs.{key}" in str(err)) == ("request_mismatch", True)


def test_inputs_that_changed_after_submit_are_refused(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    other = w.reg.register_candidate(CAND2)  # never logged
    submit(wrk, w, registry=other, verdict_id="v-a")
    err = refused(asr, w, replace(JOB, request_id="v-a"))
    assert (err.code, "inputs.registry" in str(err)) == ("request_mismatch", True)
    submit(wrk, w)
    entitle(owner, w)  # a new entitlement revision, still granted
    err = refused(asr, w)  # but not the decisions the request was bound to
    assert (err.code, "inputs.rights" in str(err)) == ("request_mismatch", True)


@pytest.mark.parametrize("terms", [
    replace(TERMS, scope=UseScope.COMMERCIAL), replace(TERMS, jurisdiction="QC"),
    replace(TERMS, retain_for=KEEP // 2),
])  # fmt: skip
def test_submitted_rights_under_other_terms_than_the_stored_dataset_are_refused(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
    terms: dst.DatasetTerms,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w, terms)
    err = refused(asr, w)  # the provider takes the terms stored with the dataset
    assert (err.code, "inputs.rights" in str(err)) == ("request_mismatch", True)


def entitle(owner: Conn, w: Any, active: bool = True,
            until: datetime | None = None) -> None:  # fmt: skip
    """A new entitlement revision now (grants expiring at `until`)."""
    at = run_tests.clock(owner)
    ent = run_tests.entitlement(w.t, w.rights.at).revise(
        EntitlementSource.INSTALLATION_LICENSE, active, run_tests.profile(at, until),
        None, at)  # fmt: skip
    with tn.tenant_transaction(owner, w.t) as tx:
        rs.record_entitlement(tx, ent)


def test_an_entitlement_revoked_after_submit_is_refused(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    entitle(owner, w, active=False)  # decisions 75 and 77: revoked after submit
    assert refused(asr, w).code == "rights_refused"


def test_a_retired_feed_is_refused_at_the_database_clock(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    at = run_tests.clock(owner)
    retire = Evidence(EvidenceKind.AUTH, "synthetic-retirement", at)
    retired = run_tests.feed(w.rights.at).transition(
        FeedStatus.RETIRED, "operator-synth", (retire,), at)  # fmt: skip
    rs.record_feed(owner, retired)
    assert refused(asr, w).code == "rights_refused"  # decision 75: even a re-run


def test_an_expired_dataset_is_refused_at_the_database_clock(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    e = stores(logins, owner, ing, app_conn, "dataset")
    submit(wrk, e)
    short = replace(TERMS, retain_for=timedelta(days=365))  # held since 2020
    dsets.raw_insert(conn, e.t, e.ds, short)  # stored past the store's own check
    assert refused(asr, e).code == "retention_expired"


def test_an_entitlement_that_expires_by_time_alone_is_refused(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)  # the slow setup, under long grants
    until = db_now(owner) + timedelta(seconds=60)
    entitle(owner, w, until=until)  # then grants that expire in 60 s
    submit(wrk, w)
    r = qa.STORE_SOURCE(asr, w.t, JOB)  # still granted: rebuilt
    assert r.rights.at < until
    deadline = time.monotonic() + 120  # bounded: the clock must pass `until`
    while db_now(owner) <= until:
        assert time.monotonic() < deadline, "the database clock did not advance"
        time.sleep(0.5)  # nothing is recorded: only the database clock moves
    assert refused(asr, w).code == "rights_refused"


def db_now(c: Conn) -> datetime:
    row = c.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return ensure_aware_utc(row[0])


def test_rights_revoked_after_the_rebuild_are_refused_before_the_read(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    r = qa.STORE_SOURCE(asr, w.t, JOB)  # rights granted at the load
    entitle(owner, w, active=False)
    before = run_tests.counts(conn, w.t)
    with pytest.raises(es.ExperimentStoreError) as e:
        run_tests.ar.promotion_run(wrk, asr, w.t, r, run_tests.REG)
    assert e.value.code == "rights_refused"  # re-checked at the read (77)
    assert run_tests.counts(conn, w.t) == before  # the read was not spent


def test_a_grant_changed_after_the_rebuild_is_a_mismatch_before_the_read(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn, conn: Conn,
) -> None:  # fmt: skip
    wrk, asr = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    r = qa.STORE_SOURCE(asr, w.t, JOB)
    entitle(owner, w)  # revised, still granted: not the grant the request binds
    before = run_tests.counts(conn, w.t)
    with pytest.raises(es.ExperimentStoreError) as e:
        run_tests.ar.promotion_run(wrk, asr, w.t, r, run_tests.REG)
    assert (e.value.code, "inputs.rights" in str(e.value)) == (
        "request_mismatch", True)  # decision 85  # fmt: skip
    assert run_tests.counts(conn, w.t) == before  # the read was not spent


def test_a_store_database_fault_is_retried(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    wrk, _ = logins
    w = stores(logins, owner, ing, app_conn)
    jid = submit(wrk, w)

    def faulty(tx: tn.TenantTx, content_hash: str) -> None:
        raise sts.StudyStoreError("database", "OperationalError")

    monkeypatch.setattr(sts, "space", faulty)  # the module qa reads
    lp = qa.AssessorLoop(*logins, proc.SETTINGS, strategies=run_tests.REG)
    assert lp.run_one() == (jid, JobState.QUEUED)  # retryable, under its code
    assert proc.state(wrk, w.t, jid) == (JobState.QUEUED, "database")
    monkeypatch.undo()
    assert lp.run_one() == (jid, JobState.SUCCEEDED)


def test_the_provider_reads_one_read_only_snapshot(
    logins: tuple[Conn, Conn], owner: Conn, ing: Conn, app_conn: Conn,
    runtime_urls: dict[str, str],
) -> None:  # fmt: skip
    """An entry committed by another login while the provider reads is not seen,
    and the provider can write nothing."""
    wrk, _ = logins
    w = stores(logins, owner, ing, app_conn)
    submit(wrk, w)
    seen: list[object] = []
    q = "SELECT count(*) FROM app.experiment_registry_entry"

    def reading(tx: tn.TenantTx, stored: ars.StoredRequest) -> qa.BoundInputs:
        seen.append(tx.conn.execute(q).fetchone())
        with tn.runtime_connection(runtime_urls["qw_assessor"],
                                   role="qw_assessor") as other:  # fmt: skip
            with tn.tenant_transaction(other, w.t) as otx:
                ers.append(otx, CAND2)  # committed meanwhile
            with tn.tenant_transaction(other, w.t) as otx:
                seen.append(otx.conn.execute(q).fetchone())
        seen.append(tx.conn.execute(q).fetchone())
        seen.append(tx.conn.execute("SHOW transaction_isolation").fetchone())
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction), \
                tx.conn.transaction():  # fmt: skip
            tx.conn.execute("CREATE TEMP TABLE qw_probe (x int)")
        return qa.store_inputs(tx, stored)

    with tn.runtime_connection(runtime_urls["qw_assessor"], role="qw_assessor") as a:
        qa.store_source(reading)(a, w.t, JOB)
    assert seen == [(2,), (3,), (2,), ("repeatable read",)]


def test_a_candidate_manifest_is_re_derived_exactly() -> None:
    """Decision 78: from the base, the search space and the logged parameters."""
    c = fx.CAND
    assert qa.candidate_manifest(fx.BASE, fx.SPACE, c) == fx.DERIVED
    bad = [replace(c, payload=dict(c.payload) | {"lookback": "64"}),
           replace(c, payload=dict(c.payload) | {"lookback": "063"}),
           replace(c, payload=dict(c.payload) | {"extra": "1"}),
           replace(c, payload={k: v for k, v in c.payload.items() if k != "review"}),
           replace(c, artifact_hash="e" * 64)]  # fmt: skip
    for b in bad:
        with pytest.raises(qa.Refused) as e:
            qa.candidate_manifest(fx.BASE, fx.SPACE, b)
        assert e.value.code == "candidate_mismatch", b.payload
    # the parameter wire forms (`_wire`): booleans are lower case, numbers canonical
    got = [qa._param(k, v) for k, v in [(ParamKind.BOOLEAN, "true"),
           (ParamKind.BOOLEAN, "false"), (ParamKind.INTEGER, "63"),
           (ParamKind.DECIMAL, "0.05"), (ParamKind.CHOICE, "weekly")]]  # fmt: skip
    assert got == [True, False, 63, Decimal("0.05"), "weekly"]
    assert [type(v) for v in got[:3]] == [bool, bool, int]
    with pytest.raises(KeyError):
        qa._param(ParamKind.BOOLEAN, "True")
