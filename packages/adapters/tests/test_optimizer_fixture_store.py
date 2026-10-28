"""0051, `optimizer_fixture_store` and the trial's `stored_inputs` (T050 increment
8): real logins, SYNTHETIC data; GATE-LIFTED tests use `lifted_assess`."""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors
from qw_adapters import assessor_run_optimizer as aro
from qw_adapters import optimizer_fixture_store as ofs
from qw_adapters import optimizer_store as os_
from qw_domain import improvement as im
from qw_domain import optimizer_improvement as oi
from qw_domain import research_improvement as ri
from qw_domain.research_data import EvidenceClass
from test_optimizer_improvement import LIMITS
from test_optimizer_measure import MP
from test_research_improvement import BASE, DEV, FIXTURE, PLAN


def _load(name: str, path: Path) -> Any:  # as apps/worker/tests: no package here
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    sys.modules[name] = mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orl = _load("qw_optimizer_run_log_tests",
            Path(__file__).parent / "test_optimizer_run_log.py")  # fmt: skip
pytestmark = pytest.mark.db
Conn = psycopg.Connection[Any]
INST, code, login, now = orl.INST, orl.ot.code, orl.login, orl.now
TABLE = "app.optimizer_promotion_fixture"
ROLES = ("qw_app", "qw_worker", "qw_assessor", "qw_release", "qw_ingest", "public")
DEVELOPMENT: aro.Development = lambda cycle: (BASE, DEV, LIMITS)  # noqa: E731


@pytest.fixture
def asr(runtime_urls: dict[str, str]) -> Iterator[Conn]:
    yield from login(runtime_urls, "qw_assessor")


def world(asr: Conn, conn: Conn, runtime_urls: dict[str, str],
          fixture: bool = True) -> aro.Request:  # fmt: skip
    """The fixture preregistered (or not), MP's reservation in the bootstrap book
    (decision 98), then increment 5's stored world."""
    if fixture:
        assert ofs.preregister(asr, INST, PLAN, FIXTURE)
    for wrk in login(runtime_urls, "qw_worker"):
        orl.operator(conn, wrk)
        r: aro.Request = orl.stored_world(asr, wrk)
    return r


def rows(conn: Conn) -> int:
    row = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()
    assert row is not None
    return int(row[0])


def raw(c: Conn, body: str = "%s", h: str | None = None) -> None:
    """A direct insert past the store (event hash "0" * 64)."""
    w = {"plan": PLAN.to_wire(), "fixture": ofs.fixture_wire(FIXTURE)}
    c.execute(f"INSERT INTO {TABLE} (installation_id, plan_id, fixture_hash, body, "
              f"recorded_at, event_hash) VALUES (%s, %s, %s, {body}, "
              "clock_timestamp(), repeat('0', 64))",
              (INST, PLAN.plan_id, h or FIXTURE.content_hash,
               psycopg.types.json.Jsonb(w)))  # fmt: skip


def test_exact_privileges_guard_and_forbidden_logins(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    held = conn.execute(
        "SELECT r, k FROM unnest(%s::text[]) r CROSS JOIN unnest(ARRAY['SELECT', "
        "'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) k "
        "WHERE has_table_privilege(r, %s, k) OR (k IN ('SELECT', 'INSERT', 'UPDATE', "
        "'REFERENCES') AND has_any_column_privilege(r, %s, k))",
        (list(ROLES), TABLE, TABLE)).fetchall()  # fmt: skip
    assert set(held) == {("qw_assessor", "SELECT"), ("qw_assessor", "INSERT")}
    g = conn.execute(
        "SELECT p.proowner::regrole::text, p.prosecdef, p.proconfig, "
        "has_function_privilege('public', p.oid, 'EXECUTE'), p.prosrc FROM pg_proc p "
        "WHERE p.oid = 'app.optimizer_promotion_fixture_guard()'::regprocedure"
    ).fetchone()  # fmt: skip
    assert g is not None and g[:4] == ("qw_migrate", False, ["search_path=pg_catalog"],
                                       False)  # fmt: skip
    assert "pg_advisory_xact_lock" in g[4]  # the registry's installation lock
    rls = conn.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                       f"WHERE oid = '{TABLE}'::regclass").fetchone()  # fmt: skip
    assert rls == (True, True)
    pol = conn.execute("SELECT polname, polroles::regrole[]::text[] FROM pg_policy "
                       f"WHERE polrelid = '{TABLE}'::regclass").fetchall()  # fmt: skip
    assert pol == [("installation_assessor", ["qw_assessor"])]  # none for the owner
    assert ofs.preregister(asr, INST, PLAN, FIXTURE)
    for role in ("qw_app", "qw_worker", "qw_release"):
        for c in login(runtime_urls, role):
            assert code(partial(ofs.load, c, INST, PLAN.plan_id)) == "forbidden"
            other = replace(PLAN, plan_id="plan-other")
            assert code(partial(ofs.preregister, c, INST, other, FIXTURE)) == (
                "forbidden")  # fmt: skip
    assert rows(conn) == 1


def test_append_only_for_every_login_and_the_owner(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    ofs.preregister(asr, INST, PLAN, FIXTURE)
    for stmt in (f"UPDATE {TABLE} SET plan_id = plan_id", f"DELETE FROM {TABLE}",
                 f"TRUNCATE {TABLE}"):  # fmt: skip
        for role in ("qw_assessor", "qw_worker", "qw_release"):
            for c in login(runtime_urls, role):
                with pytest.raises(errors.InsufficientPrivilege):
                    c.execute(stmt)
        with pytest.raises(errors.RaiseException, match="append-only"):
            conn.execute(stmt)
        with conn.transaction(), pytest.raises(errors.RaiseException):
            conn.execute("SET LOCAL ROLE qw_migrate")
            conn.execute(stmt)
    assert rows(conn) == 1


def test_preregistration_refusals(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    dev_split = replace(FIXTURE, split=ri.Split.DEVELOPMENT)
    historical = replace(FIXTURE, evidence_class=EvidenceClass.HISTORICAL)
    cases: list[tuple[Any, str]] = [
        (partial(ofs.preregister, asr, INST, PLAN, dev_split), "not_promotion"),
        (partial(ofs.preregister, asr, INST, PLAN, DEV), "not_promotion"),
        (partial(ofs.preregister, asr, INST, replace(PLAN, protocol_hash="e" * 64),
                 FIXTURE), "answer_key_changed"),
        (partial(ofs.preregister, asr, INST, replace(PLAN, protocol_hash=(
            historical.content_hash)), historical), "evidence_class"),
    ]  # fmt: skip
    for fn, want in cases:
        assert code(fn) == want, want
    real = os_._now
    for shift in (timedelta(minutes=-2), timedelta(minutes=1)):
        with monkeypatch.context() as m:
            m.setattr(os_, "_now", lambda c, lock=None, shared=False, s=shift: (
                real(c, lock, shared) + s))  # fmt: skip
            assert code(partial(ofs.preregister, asr, INST, PLAN, FIXTURE)) == "clock"
    for body in (
        "%s - 'fixture'",
        "%s || '{\"x\": 1}'",
        "jsonb_set(%s, '{fixture,split}', '\"development\"')",
    ):
        with pytest.raises(errors.CheckViolation):
            raw(asr, body)
    with pytest.raises(errors.CheckViolation):
        raw(asr, h="e" * 64)  # not the plan's protocol hash
    assert rows(conn) == 0
    assert ofs.preregister(asr, INST, PLAN, FIXTURE)
    assert not ofs.preregister(asr, INST, PLAN, FIXTURE)  # identical: nothing new
    other = replace(FIXTURE, fixture_id="fixture-synth-promo-2")
    moved = replace(PLAN, protocol_hash=other.content_hash)
    assert code(partial(ofs.preregister, asr, INST, moved, other)) == "fixture_exists"
    # a protocol now names the plan: frozen, through the store and raw
    os_.append(asr, INST, MP)
    assert ofs.preregister(asr, "inst-b", PLAN, FIXTURE)  # another installation's
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute(f"DELETE FROM {TABLE} WHERE installation_id = %s", (INST,))
    assert code(partial(ofs.preregister, asr, INST, PLAN, FIXTURE)) == "plan_frozen"
    with pytest.raises(errors.CheckViolation, match="plan_frozen"):
        raw(asr)


def test_load_rebuilds_and_tampering_is_integrity(asr: Conn, conn: Conn) -> None:
    assert ofs.load(asr, INST, PLAN.plan_id) is None  # never a substitute
    w: dict[str, Any] = ofs.fixture_wire(FIXTURE)
    assert ofs.fixture_from_wire(w) == FIXTURE
    assert ofs.fixture_from_wire(ofs.fixture_wire(DEV)) == DEV
    doc, case = w["docs"][0], w["cases"][0]
    for bad in ([w], w | {"x": 1}, w | {"docs": [[*doc[:9], 50]]},
                w | {"docs": [[*doc[:9], "NaN"]]}, w | {"docs": [doc[:9]]},
                w | {"cases": [[*case[:5], "-1", *case[6:]]]},
                w | {"cases": [[*case[:7], [1]]]}, w | {"split": "holdout"},
                w | {"cases": [[*case[:5], "0" + case[5], *case[6:]]]},
                w | {"loaded_at": w["loaded_at"].removesuffix("Z")}):  # fmt: skip
        with pytest.raises((ValueError, TypeError)):
            ofs.fixture_from_wire(bad)
    for change in ("event_hash = repeat('1', 64)", "installation_id = 'inst-moved'",
                   "recorded_at = recorded_at - interval '1 second'",
                   "body = jsonb_set(body, '{fixture,docs,0,9}', '\"51\"')",
                   "body = jsonb_set(body, '{plan,max_promotion_accesses}', '2')",
                   ):  # fmt: skip
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(f"DELETE FROM {TABLE}")
        assert ofs.preregister(asr, INST, PLAN, FIXTURE)
        assert ofs.load(asr, INST, PLAN.plan_id) == (PLAN, FIXTURE)
        with conn.transaction():
            conn.execute("SET LOCAL session_replication_role = replica")
            conn.execute(f"UPDATE {TABLE} SET {change}")
        inst = "inst-moved" if "inst-moved" in change else INST
        assert code(partial(ofs.load, asr, inst, PLAN.plan_id)) == "integrity", change


def test_the_real_trial_is_blocked_before_reading_the_stored_fixture(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    r, reads = world(asr, conn, runtime_urls), list[str]()
    real = ofs.load

    def spy(*a: Any) -> Any:
        reads.append(a[2])
        return real(*a)

    monkeypatch.setattr(ofs, "load", spy)
    assert code(lambda: aro.trial(asr, r)) == "sandbox_unqualified"
    assert reads == [] and [orl.count(conn, t) for t in orl.TABLES] == [0, 0]
    # GATE-LIFTED: past the gate the real trial reads the fixture, then refuses
    # the development inputs no store holds
    monkeypatch.setattr(aro, "measured_assess", orl.lifted_assess)
    assert code(lambda: aro.trial(asr, r)) == "dev_inputs_unstored"
    assert reads == [MP.plan_id] and [orl.count(conn, t) for t in orl.TABLES] == [0, 0]


def gate_lifted(asr: Conn, r: aro.Request,
                dev: aro.Development = DEVELOPMENT) -> aro.Run:  # fmt: skip
    """GATE-LIFTED: `trial`'s phases with the stored inputs and a test-only
    development source; `trial` itself passes none."""
    reg, p, c, cycle = aro._before_gate(asr, r, orl.lifted_assess)
    return aro._after_gate(asr, r, lambda: None, reg, p, c, cycle,
                           aro.stored_inputs(asr, dev), os_.RunLog(asr, INST),
                           orl.lifted_assess)  # fmt: skip


def test_gate_lifted_trial_reads_the_stored_fixture_and_accepts(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """GATE-LIFTED SYNTHETIC: the promotion fixture comes from 0051; development
    inputs that do not hash to the stored cycle's are refused before any read."""
    r, before = world(asr, conn, runtime_urls), orl.entries(conn)
    other: list[Callable[[oi.CycleReceipt], Any]] = [
        lambda c: (replace(BASE, top_k=1), DEV, LIMITS),
        lambda c: (BASE, replace(DEV, fixture_id="fixture-synth-dev-2"), LIMITS),
    ]
    for dev in other:
        assert code(lambda d=dev: gate_lifted(asr, r, d)) == "dev_inputs_mismatch"
    assert [orl.count(conn, t) for t in orl.TABLES] == [0, 0]
    run = gate_lifted(asr, r)
    assert (run.attempt, run.wrote, run.verdict.decision) == (
        1, True, im.Decision.ACCEPT_FOR_REVIEW)  # fmt: skip
    assert "declared_costs_not_reserved" not in run.verdict.limitations  # 94, 98
    assert os_.RunLog(asr, INST).attempts(MP.plan_id)[0].fixture_hash == (
        FIXTURE.content_hash)  # the stored fixture was the read  # fmt: skip
    assert orl.entries(conn) == before + 1


def test_a_preregistration_waits_for_a_protocol_append_and_is_then_frozen(
    asr: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """Two connections: the raw insert's guard waits on the uncommitted protocol
    append's lock, then sees it committed (READ COMMITTED, which it asserts)."""
    out: list[str] = []

    def second() -> None:
        for c in login(runtime_urls, "qw_assessor"):
            try:
                raw(c)
            except errors.CheckViolation as exc:
                out.append(str(exc))

    with asr.transaction():
        os_.append(asr, INST, MP)  # holds `optimizer:<installation>` until commit
        t = threading.Thread(target=second)
        t.start()
        t.join(2)
        assert t.is_alive()  # the insert waits
    t.join(30)
    assert len(out) == 1 and "plan_frozen" in out[0] and rows(conn) == 0
    for c in login(runtime_urls, "qw_assessor"):
        c.execute("SET default_transaction_isolation = 'repeatable read'")
        with pytest.raises(errors.CheckViolation, match="isolation"):
            raw(c)
