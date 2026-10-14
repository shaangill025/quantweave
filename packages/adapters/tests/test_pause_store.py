"""0016 and qw_adapters.pause_store as qw_app (the superuser `conn` observes or
tampers). SYNTHETIC drafts of test_policy_store; the oracle is an in-memory
`PauseBook`; times are relative to the DB clock. Cross-tenant: test_pauses_api."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import pause_store as pz
from qw_adapters import policy_store as ps
from qw_adapters import tenancy as tn
from qw_domain.risk_pauses import (
    Pause,
    PauseBook,
    PauseError,
    PauseScope,
    PauseTrigger,
    Resume,
    ScopeKind,
)

Conn = psycopg.Connection[TupleRow]


def _module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pol = _module("qw_policy_store_tests", Path(__file__).with_name("test_policy_store.py"))
ctx, propose, Ctx = pol.ctx, pol.propose, pol.Ctx
WINDOW = timedelta(minutes=10)
ACCOUNT = "acct-syn-1"  # the only account of the SYNTHETIC draft


def pause(conn: Conn, c: Any, who: uuid.UUID | None = None) -> pz.Pauses:
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return pz.pause(tx, c.policy, principal=who or c.other, reason="SYNTHETIC",
                        text="SYNTHETIC stop new risk")  # fmt: skip


def current_tag(tx: tn.TenantTx, c: Any) -> str:
    found = ps.load(tx, c.policy)
    version = found.versions[-1].version if found else 1
    return pz.tag(version, pz.load(tx).active_for(c.policy))


def resume(conn: Conn, c: Any, etag: str | None = None, **kw: Any) -> pz.Pauses:
    args: dict[str, Any] = {"principal": c.owner, "session_id": c.session,
                            "window": WINDOW, "reason": "SYNTHETIC reviewed",
                            "text": "SYNTHETIC I resume"} | kw  # fmt: skip
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return pz.resume(tx, c.policy, etag or current_tag(tx, c), **args)


def load(conn: Conn, tenant: uuid.UUID) -> pz.Pauses:
    with tn.tenant_transaction(conn, tenant) as tx:
        return pz.load(tx)


def rows(conn: Conn, table: str) -> int:
    found = conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
    return int(found[0]) if found else -1


def db_now(conn: Conn) -> Any:
    found = conn.execute("SELECT clock_timestamp()").fetchone()
    assert found is not None
    return found[0]


def step_up_at(conn: Conn, session: uuid.UUID) -> Any:
    found = conn.execute("SELECT step_up_at FROM app.session WHERE id = %s", (session,))
    return found.fetchone()[0]  # type: ignore[index]


def raw_pause(tx: tn.TenantTx, c: Any, kind: ScopeKind, scope: str,
              trig: PauseTrigger, **kw: Any) -> Pause:  # fmt: skip
    """Insert a pause around `pz.pause` (at the DB clock unless `at` is given)."""
    at = kw.pop("at", None) or db_now(tx.conn)
    p = Pause(str(uuid.uuid4()), PauseScope(kind, scope), trig, at, "ev:syn", **kw)
    pz._insert_pause(tx, c.policy, 1, c.other, "r", "t", p)
    return p


def raw_resume(tx: tn.TenantTx, pause_id: str, who: uuid.UUID, session: uuid.UUID,
               at: Any, receipt: str | None = None) -> None:  # fmt: skip
    receipt = receipt or ps.step_up_receipt_id(session, at)
    r = Resume(pause_id, str(who), "x", receipt, db_now(tx.conn), None)
    pz._insert_resume(tx, who, session, at, "t", r)


@pytest.mark.db
def test_round_trip_replays_through_the_domain(
    app_conn: Conn, conn: Conn, ctx: Any
) -> None:
    propose(app_conn, ctx)
    before = db_now(conn)
    paused = pause(app_conn, ctx)
    [p] = paused.active_for(ctx.policy)
    assert (p.scope, p.trigger, p.material) == (
        PauseScope(ScopeKind.ACCOUNT, ACCOUNT), PauseTrigger.USER, False)  # fmt: skip
    assert before <= p.triggered_at <= db_now(conn)  # the DB clock, not the caller's
    assert p.evidence_ref == f"user:{ctx.other}"
    assert load(app_conn, ctx.tenant) == paused
    assert pause(app_conn, ctx).book == paused.book  # already paused: nothing added
    assert rows(conn, "risk_pause") == 1
    resumed = resume(app_conn, ctx)
    [r] = [e for e in resumed.book.events if isinstance(e, Resume)]
    receipt = ps.step_up_receipt_id(ctx.session, step_up_at(conn, ctx.session))
    resumption = Resume(p.pause_id, str(ctx.owner), "SYNTHETIC reviewed", receipt,
                        r.resumed_at, None)  # fmt: skip
    oracle = PauseBook(str(ctx.tenant), (p,)).resume(resumption)
    assert resumed.book == oracle and resumed.active_for(ctx.policy) == ()
    assert load(app_conn, ctx.tenant) == resumed
    assert p.triggered_at <= r.resumed_at <= db_now(conn)
    again = pause(app_conn, ctx)  # a resumed scope can be paused again
    assert len(again.active_for(ctx.policy)) == 1 and rows(conn, "risk_pause") == 2


@pytest.mark.db
def test_tables_are_append_only_and_times_follow_the_db_clock(
    app_conn: Conn, conn: Conn, ctx: Any
) -> None:
    propose(app_conn, ctx)
    pause(app_conn, ctx)
    resume(app_conn, ctx)
    for table in ("risk_pause", "risk_pause_resume"):
        for statement in (f"UPDATE app.{table} SET tenant_id = tenant_id",
                          f"DELETE FROM app.{table}"):  # fmt: skip
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                app_conn.execute(statement)
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(statement)  # the superuser too
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # server-assigned
        app_conn.execute("INSERT INTO app.risk_pause (recorded_at) VALUES (now())")
    for shift in (timedelta(seconds=-61), timedelta(seconds=5)):
        with (
            pytest.raises(psycopg.errors.CheckViolation, match="database clock"),
            tn.tenant_transaction(app_conn, ctx.tenant) as tx,
        ):
            raw_pause(tx, ctx, ScopeKind.ACCOUNT, ACCOUNT, PauseTrigger.USER,
                      material=False, at=db_now(tx.conn) + shift)  # fmt: skip
    assert (rows(conn, "risk_pause"), rows(conn, "risk_pause_resume")) == (1, 1)


@pytest.mark.db
def test_resume_needs_the_owners_step_up_and_refusals_write_nothing(
    app_conn: Conn, conn: Conn, ctx: Any
) -> None:
    def refused(code: str, etag: str | None = None, **kw: Any) -> None:
        with pytest.raises((pz.PauseStoreError, PauseError)) as caught:
            resume(app_conn, ctx, etag, **kw)
        assert getattr(caught.value, "code", None) == code

    refused("not_found")
    propose(app_conn, ctx)
    refused("not_paused")
    pause(app_conn, ctx)
    refused("step_up_required", session_id=ctx.other_session)  # another user's
    refused("step_up_required", window=timedelta(microseconds=1))  # expired
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
        fresh, _ = tn.create_session(tx, ctx.owner, timedelta(hours=1))
    refused("step_up_required", session_id=fresh)  # never stepped up
    refused("forbidden", principal=ctx.other, session_id=ctx.other_session)  # member
    refused("reason", reason="  ")  # the domain's rule
    refused("stale", etag='"1"')  # read before the pause: it never saw it
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
        seen = current_tag(tx, ctx)
    propose(app_conn, ctx, pol.draft(ctx.tenant, ("income",)))  # version 2
    refused("stale", etag=seen)
    assert rows(conn, "risk_pause_resume") == 0
    # The database refuses a member's resume and a forged step-up from qw_app too.
    paused = load(app_conn, ctx.tenant).active_for(ctx.policy)[0]
    for who, session, receipt in (
        (ctx.other, ctx.other_session, None),  # a member, own step-up
        (ctx.owner, ctx.session, "forged"),  # receipt id not bound
    ):
        with (
            pytest.raises(psycopg.errors.RaiseException, match="step-up"),
            tn.tenant_transaction(app_conn, ctx.tenant) as tx,
        ):
            at = step_up_at(conn, session)
            raw_resume(tx, paused.pause_id, who, session, at, receipt)
    assert rows(conn, "risk_pause_resume") == 0


@pytest.mark.db
def test_a_pause_needing_reconciled_state_blocks_the_whole_resume(
    app_conn: Conn, conn: Conn, ctx: Any
) -> None:
    propose(app_conn, ctx)
    pause(app_conn, ctx)
    with tn.tenant_transaction(app_conn, ctx.tenant) as tx:  # a stale-data pause
        p = raw_pause(tx, ctx, ScopeKind.TENANT, str(ctx.tenant), PauseTrigger.STALE,
                      material=False)  # fmt: skip
    with pytest.raises(PauseError, match="reconciled_state_required"):
        resume(app_conn, ctx)
    assert rows(conn, "risk_pause_resume") == 0  # the user pause stays active too
    assert len(load(app_conn, ctx.tenant).active_for(ctx.policy)) == 2
    with (  # nor can qw_app write that resume around the store
        pytest.raises(psycopg.errors.RaiseException, match="reconciled"),
        tn.tenant_transaction(app_conn, ctx.tenant) as tx,
    ):
        raw_resume(tx, p.pause_id, ctx.owner, ctx.session,
                   step_up_at(conn, ctx.session))  # fmt: skip


@pytest.mark.db
def test_tampering_fails_load_closed(app_conn: Conn, conn: Conn, ctx: Any) -> None:
    propose(app_conn, ctx)
    pause(app_conn, ctx)
    resume(app_conn, ctx)
    conn.execute("SET session_replication_role = replica")  # a tampering owner
    conn.execute("UPDATE app.risk_pause SET scope_id = 'acct-syn-2'")
    with pytest.raises(pz.PauseStoreError, match="integrity"):
        load(app_conn, ctx.tenant)
    conn.execute("UPDATE app.risk_pause SET scope_id = %s", (ACCOUNT,))
    assert load(app_conn, ctx.tenant).active_for(ctx.policy) == ()  # restored
    conn.execute("UPDATE app.risk_pause_resume SET resumed_at = resumed_at "
                 "- interval '1 day'")  # fmt: skip
    with pytest.raises(pz.PauseStoreError, match="integrity"):
        load(app_conn, ctx.tenant)
    conn.execute("DELETE FROM app.risk_pause_resume")
    conn.execute("RESET session_replication_role")
    # A hash-consistent resume of a material pause, written with triggers off: the
    # domain replay (reconciled revision required) refuses it.
    conn.execute("SET session_replication_role = replica")
    with tn.tenant_transaction(conn, ctx.tenant) as tx:
        p = raw_pause(tx, ctx, ScopeKind.ACCOUNT, ACCOUNT, PauseTrigger.USER,
                      material=True)  # fmt: skip
        raw_resume(tx, p.pause_id, ctx.owner, ctx.session,
                   step_up_at(conn, ctx.session))  # fmt: skip
    with pytest.raises(pz.PauseStoreError, match="integrity"):
        load(app_conn, ctx.tenant)


@pytest.mark.db
def test_pause_never_waits_on_policy_journal_or_rights_locks(
    runtime_urls: dict[str, str], app_conn: Conn, ctx: Any
) -> None:
    """Pause takes only the pause lock; resume takes the policy lock first."""
    propose(app_conn, ctx)
    held = (f"policy/{ctx.tenant}/{ctx.policy}", f"ledger:{ctx.tenant}:{ACCOUNT}",
            "rights")  # fmt: skip
    with (
        tn.runtime_connection(runtime_urls["qw_app"]) as other,
        tn.tenant_transaction(other, ctx.tenant),
    ):
        for key in held:
            other.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                          (key,))  # fmt: skip
        with tn.tenant_transaction(app_conn, ctx.tenant) as tx:
            tx.conn.execute("SET LOCAL lock_timeout = '200ms'")
            pz.pause(tx, ctx.policy, principal=ctx.other, reason="", text="t")
        with (
            pytest.raises(psycopg.errors.LockNotAvailable),
            tn.tenant_transaction(app_conn, ctx.tenant) as tx,
        ):
            tx.conn.execute("SET LOCAL lock_timeout = '200ms'")
            pz.resume(tx, ctx.policy, '"1"', principal=ctx.owner,
                      session_id=ctx.session, window=WINDOW, reason="r",
                      text="t")  # fmt: skip
    assert len(load(app_conn, ctx.tenant).active_for(ctx.policy)) == 1


@pytest.mark.db
def test_a_pause_covers_every_version_and_later_accounts(
    app_conn: Conn, ctx: Any
) -> None:
    """Reviewer S1: v1 names acct-syn-1, v2 only acct-syn-2; both are paused. An
    account a later version adds is covered for this policy's actions only."""
    propose(app_conn, ctx)
    propose(app_conn, ctx, pol.draft(ctx.tenant, acct="acct-syn-2"))
    held = pause(app_conn, ctx)
    assert sorted(p.scope.scope_id for p in held.active_for(ctx.policy)) == [
        ACCOUNT, "acct-syn-2"]  # fmt: skip
    propose(app_conn, ctx, pol.draft(ctx.tenant, acct="acct-syn-3"))
    held, t = load(app_conn, ctx.tenant), str(ctx.tenant)
    assert held.covering(ctx.policy, "acct-syn-3").blocking(t, "acct-syn-3", None, "s")
    assert not held.covering(uuid.uuid4(), "acct-syn-3").blocking(
        t, "acct-syn-3", None, "s")  # fmt: skip
