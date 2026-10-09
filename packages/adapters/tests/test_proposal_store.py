"""Proposal store and atomic publication (0010_proposals, qw_adapters.proposal_store)
on PostgreSQL 16, as the non-superuser qw_app logins of the conftest harness; the
superuser `conn` only observes. SYNTHETIC tenant, policy, prices and balances: 5000
CAD qualified cash, buy 80 at an ask of 50 (4000) under a generous adopted policy, as
in packages/domain/tests/test_commitments.py. NUM06 expectations are by hand: two
4000 buys against 5000 cannot both be reserved (8000 > 5000)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow
from qw_adapters import journal_store as js
from qw_adapters import policy_store as ps
from qw_adapters import proposal_store as pst
from qw_adapters import tenancy as tn
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, PositiveQuantity, Price, Quantity, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.onboarding import load_catalogue
from qw_domain.policy import AccountFacts, OptionPermission, PolicyHistory, build_draft
from qw_domain.postings import Header, deposit
from qw_domain.proposals import (
    TRANSITIONS,
    Binding,
    Code,
    ExecutionState,
    Observation,
    ProposalState,
    ProposalVersion,
    cash_requirement,
    policy_mode,
)
from qw_domain.risk import ProposedAction, RiskInputs, Side, evaluate
from qw_domain.risk_pauses import PauseBook
from qw_domain.valuation import Mark, MarkKind

Conn = psycopg.Connection[TupleRow]
S, T = ProposalState, timedelta
CONFIG = Path(__file__).resolve().parents[3] / "docs/spec/config"
CATALOGUE = load_catalogue(
    json.loads((CONFIG / "onboarding_questions.json").read_text())
)
IID, OTHER = InstrumentId(uuid.UUID(int=1)), InstrumentId(uuid.UUID(int=2))
ROUNDS = 12  # concurrent NUM06 repetitions
HOUR = timedelta(hours=1)


def cad(x: str) -> Money:
    return Money.of(x, "CAD")


def lim(metric: str, value: str, unit: str = "ratio") -> dict[str, str]:
    return {
        "metric": metric, "value": value, "unit": unit, "denominator": "account_nav",
        "window": "per_trade", "account_id": "acct-1", "currency": "CAD",
    }  # fmt: skip


ANSWERS: dict[str, Any] = {
    "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": ["growth"],
    "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
    "ONB10": "weekly", "ONB11": ["stocks", "etfs"], "ONB12": ["long_term"],
    "ONB13": [{"account_id": "acct-1", "currency": "CAD", "amount": "100000"}],
    "ONB14": [
        lim("cash_reserve", "1", "amount"), lim("issuer_concentration", "1"),
        lim("sector_concentration", "1"), lim("planned_trade_loss", "100000", "amount"),
        lim("stress_loss", "1"), lim("loss_pause", "0.10"),
    ],
    "ONB16": "rules_only",
}  # fmt: skip


@dataclass(frozen=True)
class Ctx:
    tenant: uuid.UUID
    owner: uuid.UUID
    session: uuid.UUID
    policy: uuid.UUID


def adopt(conn: Conn, c: Ctx, **answers: Any) -> PolicyHistory:
    responses = CATALOGUE.validate({**ANSWERS, **answers})
    facts = (AccountFacts("acct-1", OptionPermission.GRANTED, False),)
    d = build_draft(str(c.tenant), responses, facts, as_of=date(2026, 10, 9))
    with tn.tenant_transaction(conn, c.tenant) as tx:
        h = ps.propose(tx, c.policy, d, c.owner)
        latest = h.versions[-1]
        return ps.adopt(
            tx, c.policy, latest.version, latest.content_hash, principal=c.owner,
            session_id=c.session, window=T(minutes=10), acknowledged=frozenset(),
            text="SYNTHETIC: I adopt this",
        )  # fmt: skip


def make_ctx(conn: Conn) -> Ctx:
    with tn.provision_tenant(conn) as tx:
        user = tn.create_user(tx, "SYNTHETIC owner")
        tn.add_membership(tx, user, tn.MembershipRole.TENANT_OWNER)
        session, _ = tn.create_session(tx, user, T(hours=1))
        tn.mark_step_up(tx, session)
    c = Ctx(tx.tenant_id, user, session, uuid.uuid4())
    adopt(conn, c)
    return c


def db_now(conn: Conn) -> datetime:
    row = conn.execute("SELECT clock_timestamp()").fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def snap(c: Ctx, now: datetime) -> pst.Snapshot:
    mark = Mark(IID, Price("50"), "CAD", MarkKind.ASK, now - T(seconds=1), "SYNTH")
    inputs = RiskInputs(
        as_of=now, market_max_age=T(minutes=15), fx_max_age=T(hours=1),
        account_max_age=T(minutes=15), account_observed_at=now - T(seconds=60),
        reconciled=True, available_cash=cad("5000"), allocation_used=cad("0"),
        marks={IID: mark}, fx={}, denominators={"account_nav": cad("20000")},
        held_units={IID: Quantity(0)}, position_values={OTHER: cad("14000")},
        issuer_exposure={"issuer-x": cad("500")},
        sector_exposure={"sector-y": cad("2000")},
        stress_shocks={IID: Ratio("-0.20"), OTHER: Ratio("-0.05")},
    )  # fmt: skip
    return pst.Snapshot(inputs, PauseBook(str(c.tenant)))


def ver(
    conn: Conn, c: Ctx, s: pst.Snapshot, pid: str, qty: int = 80,
    life: timedelta = HOUR, group: str | None = None,
) -> ProposalVersion:  # fmt: skip
    with tn.tenant_transaction(conn, c.tenant) as tx:
        history = ps.load(tx, c.policy)
    action = ProposedAction(
        tenant_id=str(c.tenant), account_id="acct-1", currency="CAD", sleeve_id=None,
        strategy_id="strat-1", denominator="account_nav", instrument=IID,
        issuer_id="issuer-x", sector_id="sector-y", horizon="long_term",
        side=Side.BUY, quantity=PositiveQuantity(qty), lot=PositiveQuantity(1),
        stop=Price(48), fixed_cost=cad("0"), unit_cost=cad("0"),
        leveraged_or_inverse=False,
    )  # fmt: skip
    now = s.inputs.as_of
    ev = evaluate(history, action, s.inputs, s.pauses)
    obs = (Observation("mark:1", now - T(seconds=1), T(minutes=15)),)
    return ProposalVersion(
        pid, 1, action, Binding.of(ev, {"acct-1": 1}, obs),
        cash_requirement(action, Price("50")), now - T(seconds=3),
        now - T(seconds=2), now - T(seconds=1), now + life, policy_mode(history),
        group,
    )  # fmt: skip


def run(conn: Conn, c: Ctx, op: str, *args: Any) -> Any:
    with tn.tenant_transaction(conn, c.tenant) as tx:
        return getattr(pst.ProposalPublisher(tx), op)(*args)


def ready(conn: Conn, c: Ctx, v: ProposalVersion, s: pst.Snapshot) -> None:
    run(conn, c, "submit", v, c.policy, s)
    for dst in (S.VERIFYING, S.READY_FOR_FINAL_CHECK):
        assert run(conn, c, "step", v.proposal_id, dst, s).accepted


def codes(r: pst.Result) -> set[Code]:
    return {x.code for x in r.reasons}


def live(conn: Conn, c: Ctx) -> dict[str, tuple[Decimal, str]]:
    rows = conn.execute(
        "SELECT proposal_id, reserved, state::text FROM app.proposal_commitment "
        "WHERE tenant_id = %s AND state <> 'released'",
        (c.tenant,),
    ).fetchall()
    return {pid: (amount, state) for pid, amount, state in rows}


def count(conn: Conn, table: str, c: Ctx) -> int:
    row = conn.execute(
        f"SELECT count(*) FROM app.{table} WHERE tenant_id = %s", (c.tenant,)
    ).fetchone()
    return int(row[0]) if row else -1


def race(url: str, c: Ctx, pids: tuple[str, ...], s: pst.Snapshot) -> dict[str, Any]:
    """Publish each proposal from its own thread and connection, released together."""
    gate, results = threading.Barrier(len(pids)), dict[str, pst.Result]()

    def publish(pid: str) -> None:
        with psycopg.connect(url, autocommit=True) as own:
            gate.wait()
            results[pid] = run(own, c, "publish", pid, 1, s)

    threads = [threading.Thread(target=publish, args=(p,)) for p in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def held(
    url: str, observer: Conn, c: Ctx, pids: tuple[str, ...], s: pst.Snapshot
) -> dict[str, Any]:
    """Deterministic interleaving: the first publishes and keeps its transaction
    open while the second publishes; it commits once the second waits on a lock
    (or has finished, when nothing serialises them)."""
    first, second = pids
    results, done, release = (
        dict[str, pst.Result](),
        threading.Event(),
        threading.Event(),
    )

    def hold() -> None:
        with (
            psycopg.connect(url, autocommit=True) as own,
            tn.tenant_transaction(own, c.tenant) as tx,
        ):
            results[first] = pst.ProposalPublisher(tx).publish(first, 1, s)
            done.set()
            release.wait(30)

    def other() -> None:
        with psycopg.connect(url, autocommit=True) as own:
            results[second] = run(own, c, "publish", second, 1, s)

    a, b = threading.Thread(target=hold), threading.Thread(target=other)
    a.start()
    assert done.wait(30)
    b.start()
    waiting = (
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
    )
    while b.is_alive() and observer.execute(waiting).fetchone() == (0,):
        time.sleep(0.01)
    release.set()
    a.join()
    b.join()
    return results


@pytest.mark.db
def test_concurrent_publishes_never_overspend(
    app_conn: Conn, conn: Conn, runtime_urls: dict[str, str]
) -> None:
    """F-09 / NUM06 on separate connections: exactly one of two individually valid
    4000 buys against 5000 publishes; the other is a typed 409. In rounds 2-3, 6-7
    and 10-11 the second proposal is under another adopted policy (no shared
    policy lock), so only the account lock serialises the book."""
    c = make_ctx(app_conn)
    c2 = Ctx(c.tenant, c.owner, c.session, uuid.uuid4())
    adopt(app_conn, c2)
    for i in range(ROUNDS):
        s = snap(c, db_now(app_conn))
        pids = (f"r{i}-a", f"r{i}-b")
        for pid, cp in zip(pids, (c, c2 if i // 2 % 2 else c), strict=True):
            ready(app_conn, cp, ver(app_conn, cp, s, pid), s)
        url = runtime_urls["qw_app"]  # odd rounds: an open first transaction
        results = held(url, conn, c, pids, s) if i % 2 else race(url, c, pids, s)
        won = [p for p in pids if results[p].accepted]
        assert len(won) == 1, results
        [lost] = [results[p] for p in pids if p not in won]
        assert lost.status == 409 and lost.proposal.state(1) is S.READY_FOR_FINAL_CHECK
        assert {Code.CAPITAL_CONFLICT, Code.RISK_BLOCKED} <= codes(lost)
        assert live(conn, c) == {won[0]: (Decimal(4000), "held")}  # <= 5000
        assert run(app_conn, c, "decide", won[0], 1, pst.Choice.DISMISS, s).accepted
        assert live(conn, c) == {}


@pytest.mark.db
def test_publication_is_atomic_with_outbox_and_a_crash_leaves_nothing(
    app_conn: Conn, conn: Conn
) -> None:
    c = make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    ready(app_conn, c, ver(app_conn, c, s, "p1"), s)
    before = {t: count(conn, t, c) for t in ("proposal_event", "outbox")}
    with pytest.raises(RuntimeError), tn.tenant_transaction(app_conn, c.tenant) as tx:
        assert pst.ProposalPublisher(tx).publish("p1", 1, s).accepted
        raise RuntimeError("SYNTHETIC crash before commit")
    assert {t: count(conn, t, c) for t in before} == before
    assert live(conn, c) == {} and count(conn, "proposal_commitment", c) == 0
    r = run(app_conn, c, "publish", "p1", 1, s)
    assert r.accepted and r.status == 200 and r.proposal.state(1) is S.ACTIVE
    assert live(conn, c) == {"p1": (Decimal(4000), "held")}
    assert {t: count(conn, t, c) for t in before} == {
        t: n + 1 for t, n in before.items()
    }
    row = conn.execute(
        "SELECT event_type, payload FROM app.outbox WHERE tenant_id = %s "
        "ORDER BY created_at DESC LIMIT 1",
        (c.tenant,),
    ).fetchone()
    assert row is not None and row[0] == "proposal.transition"
    assert row[1] | {"at": ""} == {
        "proposal_id": "p1", "version": 1, "state": "active", "execution": "none",
        "code": None, "at": "",
    }  # fmt: skip
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        assert pst.ProposalPublisher(tx).load("p1") == r.proposal
    two = replace(ver(app_conn, c, s, "p1", 70), version=2)
    revised = run(app_conn, c, "revise", "p1", two, s)  # supersede releases v1
    assert revised.accepted and revised.proposal.state(1) is S.SUPERSEDED
    assert revised.proposal.state(2) is S.CANDIDATE and live(conn, c) == {}


@pytest.mark.db
def test_stale_account_revision_or_policy_conflicts(app_conn: Conn, conn: Conn) -> None:
    """F-10: the store re-reads the journal revision and the adopted policy under
    its locks; a changed one invalidates (409) and reserves nothing."""
    c = make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    for pid in ("p1", "p2"):
        ready(app_conn, c, ver(app_conn, c, s, pid), s)
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        src = SourceRef("SYN-BROKER", "D1")
        js.post(tx, deposit(Header("acct-1", db_now(app_conn), src), cad("1")))
    r = run(app_conn, c, "publish", "p1", 1, s)
    assert r.status == 409 and Code.ACCOUNT_CHANGED in codes(r)
    assert r.proposal.state(1) is S.INVALIDATED and live(conn, c) == {}
    adopt(app_conn, c, ONB04=["growth", "income"])
    r2 = run(app_conn, c, "publish", "p2", 1, s)
    assert {Code.ACCOUNT_CHANGED, Code.POLICY_CHANGED} <= codes(r2)
    assert r2.proposal.state(1) is S.INVALIDATED and live(conn, c) == {}


@pytest.mark.db
def test_planning_closes_the_alternative_and_releases_it(
    app_conn: Conn, conn: Conn
) -> None:
    c = make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    for pid, group, qty in (("a", "g1", 80), ("b", "g1", 80), ("u", None, 30)):
        ready(app_conn, c, ver(app_conn, c, s, pid, qty, group=group), s)
    assert run(app_conn, c, "publish", "a", 1, s).accepted
    assert run(app_conn, c, "publish", "b", 1, s).accepted  # a group reserves its max
    refused = run(app_conn, c, "publish", "u", 1, s)  # 4000 + 1500 > 5000
    assert refused.status == 409 and Code.CAPITAL_CONFLICT in codes(refused)
    r = run(app_conn, c, "decide", "a", 1, pst.Choice.PLAN, s)
    assert r.accepted and r.proposal.execution(1) is ExecutionState.PLANNED
    assert live(conn, c) == {"a": (Decimal(4000), "planned")}
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        b = pst.ProposalPublisher(tx).load("b")
    assert b is not None and b.state(1) is S.INVALIDATED
    assert b.events[-1].code is Code.ALTERNATIVE_SELECTED
    again = run(app_conn, c, "publish", "u", 1, s)  # the planned 4000 still counts
    assert again.status == 409 and Code.CAPITAL_CONFLICT in codes(again)


@pytest.mark.db
def test_late_decision_after_expiry_is_refused(app_conn: Conn, conn: Conn) -> None:
    c = make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    for pid, qty in (("a", 80), ("b", 10), ("e", 10)):
        ready(app_conn, c, ver(app_conn, c, s, pid, qty, life=T(seconds=5)), s)
    for pid in ("a", "b"):
        assert run(app_conn, c, "publish", pid, 1, s).accepted
    expires = s.inputs.as_of + T(seconds=5)
    while db_now(app_conn) < expires:
        time.sleep(0.1)
    events = count(conn, "outbox", c)
    late = run(app_conn, c, "decide", "a", 1, pst.Choice.PLAN, s)
    assert late.status == 409 and codes(late) == {Code.EXPIRED}
    assert late.proposal.state(1) is S.EXPIRED
    assert live(conn, c) == {}  # a settled, b swept in the same transaction
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        b = pst.ProposalPublisher(tx).load("b")
    assert b is not None and b.state(1) is S.EXPIRED
    assert run(app_conn, c, "expire_due") == ["e"]
    assert run(app_conn, c, "expire_due") == []
    assert count(conn, "outbox", c) == events + 3


@pytest.mark.db
def test_tenants_are_isolated(app_conn: Conn) -> None:
    c, other = make_ctx(app_conn), make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    ready(app_conn, c, ver(app_conn, c, s, "p1"), s)
    assert run(app_conn, c, "publish", "p1", 1, s).accepted
    with tn.tenant_transaction(app_conn, other.tenant) as tx:
        assert pst.ProposalPublisher(tx).load("p1") is None
        for table in ("proposal_version", "proposal_event", "proposal_commitment"):
            row = app_conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
            assert row == (0,), table
        with pytest.raises(pst.ProposalStoreError, match="not_found"):
            pst.ProposalPublisher(tx).publish("p1", 1, snap(other, db_now(app_conn)))
    with pytest.raises(pst.ProposalStoreError, match="tenant"):
        run(app_conn, other, "submit", ver(app_conn, c, s, "x"), other.policy, s)


def refuse(conn: Conn, c: Ctx, sql: str, *args: object) -> None:
    with pytest.raises(psycopg.Error), tn.tenant_transaction(conn, c.tenant) as tx:
        tx.conn.execute(sql, (c.tenant, *args))


@pytest.mark.db
def test_database_refuses_illegal_transitions(app_conn: Conn, conn: Conn) -> None:
    pairs = app_conn.execute(
        "SELECT s::text, d::text, app.proposal_step_ok(s, d) FROM "
        "unnest(enum_range(NULL::app.proposal_state)) s, "
        "unnest(enum_range(NULL::app.proposal_state)) d"
    ).fetchall()
    assert {(a, b) for a, b, ok in pairs if ok} == {
        (a.value, b.value) for a, dst in TRANSITIONS.items() for b in dst
    }
    assert len(pairs) == len(S) ** 2
    c = make_ctx(app_conn)
    s = snap(c, db_now(app_conn))
    ready(app_conn, c, ver(app_conn, c, s, "p1"), s)  # events 1..3, ready
    ev = (
        "INSERT INTO app.proposal_event (tenant_id, proposal_id, seq, version, state, "
        "execution, at, detail) VALUES (%s, 'p1', %s, %s, %s, %s, now(), '')"
    )
    refused = [
        (ev, (4, 1, "dismissed", "none")),  # ready -> dismissed
        (ev, (4, 1, "active", "planned")),  # execution changes with the state
        (ev, (5, 1, "active", "none")),  # gap in the sequence
        (ev, (4, 2, "candidate", "none")),  # new version without supersede
        ("INSERT INTO app.proposal_commitment (tenant_id, proposal_id, version, "
         "reserved_wire) VALUES (%s, 'p1', 1, '4000')", ()),  # not active
        ("UPDATE app.proposal_event SET detail = 'x' WHERE tenant_id = %s", ()),
    ]  # fmt: skip
    for sql, args in refused:
        refuse(app_conn, c, sql, *args)
    assert run(app_conn, c, "publish", "p1", 1, s).accepted
    commitment = "UPDATE app.proposal_commitment SET {} WHERE tenant_id = %s"
    refuse(app_conn, c, commitment.format("state = 'planned'"))  # not planned yet
    refuse(app_conn, c, commitment.format("reserved_wire = '1'"))  # frozen
    with tn.tenant_transaction(app_conn, c.tenant) as tx:
        tx.conn.execute(commitment.format("state = 'released'"), (c.tenant,))
    refuse(app_conn, c, commitment.format("state = 'held'"))  # released is final
    refuse(app_conn, c, "DELETE FROM app.proposal_commitment WHERE tenant_id = %s")
    # A tampered version (owner bypassing the trigger) fails the hash check on read.
    conn.execute("ALTER TABLE app.proposal_version DISABLE TRIGGER append_only")
    conn.execute(
        "UPDATE app.proposal_version SET content = jsonb_set(content, "
        "'{action,quantity}', '\"79\"') WHERE tenant_id = %s",
        (c.tenant,),
    )
    with (
        pytest.raises(pst.ProposalStoreError, match="integrity"),
        tn.tenant_transaction(app_conn, c.tenant) as tx,
    ):
        pst.ProposalPublisher(tx).load("p1")
