"""Retention classes, legal holds, deletion jobs and restore-time tombstones (T058
increment 1).

Everything is SYNTHETIC: tenants, records, policies, periods, holds and backups. The
policy periods (35 days) are test values, not retention rules: the spec sets none
(§12). Expected outcomes are written from spec §4, §12, §15 and the data dictionary,
not recomputed by the module.
"""

import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from qw_domain import retention as rt

C = rt.RetentionClass
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)
TENANT = "tenant-synth-a"
OTHER = "tenant-synth-b"
REF = "synthetic-owner-config-1"  # SYNTHETIC configuration basis, not counsel advice


def item(rid: str, cls: rt.RetentionClass = C.RESEARCH, tenant: str = TENANT,
         at: datetime = T0) -> rt.Item:  # fmt: skip
    return rt.Item(tenant, rid, cls, at)


def rule(cls: rt.RetentionClass, on: rt.OnRequest = rt.OnRequest.DELETE,
         after: timedelta | None = None) -> rt.Rule:  # fmt: skip
    return rt.Rule(cls, on, after, authority_id="owner-1", basis=REF)


def policy(*rules: rt.Rule) -> rt.Policy:
    return rt.Policy("pol-synth", 1, {r.cls: r for r in rules})


CONFIGURED = policy(
    rule(C.RESEARCH, after=35 * D), rule(C.EVIDENCE), rule(C.IMPROVEMENT),
    rule(C.FINANCIAL, rt.OnRequest.RETAIN), rule(C.BACKUP, rt.OnRequest.RETAIN, 35 * D),
)  # fmt: skip


def request(rid: str = "del-1", classes: frozenset[rt.RetentionClass] | None = None,
            at: datetime = T0 + H, ids: frozenset[str] = frozenset(),
            ack: str = "I understand") -> rt.DeletionRequest:  # fmt: skip
    cls = classes or frozenset({C.RESEARCH})
    return rt.DeletionRequest(rid, TENANT, "scope-1", cls, at, "reauth-1", ack, ids)


def hold(hid: str = "hold-1", tenant: str = TENANT, ids: frozenset[str] = frozenset(),
         at: datetime = T0) -> rt.LegalHold:  # fmt: skip
    return rt.LegalHold(hid, tenant, frozenset({C.RESEARCH}), ids, "litigation",
                        "counsel-1", at)  # fmt: skip


def run(store: rt.Retention, req: rt.DeletionRequest, items: list[rt.Item],
        at: datetime, backups: tuple[rt.Backup, ...] = ()) -> rt.Execution:  # fmt: skip
    job = store.submit(req, items, CONFIGURED).job_id
    return store.execute(job, at, CONFIGURED, backups)


def rest(store: rt.Retention, snap: rt.Snapshot, at: datetime) -> rt.Restored:
    led = store.ledger
    return rt.restore(snap, led.entries, led.checkpoint(), at)


def acts(plan: rt.Plan) -> dict[str, tuple[rt.Action, str]]:
    return {d.record_id: (d.action, d.reason) for d in plan.decisions}


def test_spec_baseline_fails_closed_on_every_class() -> None:
    base = rt.spec_baseline("pol-base")
    assert {r.on_request for r in base.rules.values()} == {rt.OnRequest.UNCONFIGURED}
    assert set(base.rules) == set(C)
    every = frozenset(C) - {C.PRIVACY}
    items = [item(f"r-{c.value}", c) for c in sorted(every)]
    plan = rt.plan(request(classes=every), items, base, rt.Holds())
    assert {d.action for d in plan.decisions} == {rt.Action.RETAIN}
    assert {d.reason for d in plan.decisions} == {"retention_unconfigured"}
    sweep = acts(rt.plan(rt.ExpirySweep("sw-1", TENANT, T0 + 400 * D), items, base,
                         rt.Holds()))  # fmt: skip
    assert sweep["r-research"] == (rt.Action.RETAIN, "retention_period_unconfigured")
    assert sweep["r-financial"] == (rt.Action.RETAIN, "explicit_workflow_only")
    assert rt.backup("bk-1", T0, T0 + H, base).expires_at is None
    assert len(rt.OWNER_DECISIONS) >= 5


def test_rule_invariants() -> None:
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: rule(C.PRIVACY), "tombstone_ledger_protected"),
        (lambda: rule(C.PRIVACY, rt.OnRequest.RETAIN, D), "tombstone_ledger_protected"),
        (lambda: rule(C.FINANCIAL, rt.OnRequest.DELETE, D), "financial_no_expiry"),
        (lambda: rule(C.RESEARCH, after=timedelta(0)), "period"),
        (lambda: rt.Rule(C.RESEARCH, rt.OnRequest.DELETE), "authority"),
        (lambda: rt.Rule(C.RESEARCH, expire_after=D, authority_id="o-1"), "authority"),
    ]
    for bad, code in cases:
        with pytest.raises(rt.RetentionError) as exc:
            bad()
        assert exc.value.code == code
    with pytest.raises(rt.RetentionError, match="rule_class"):
        rt.Policy("p", 1, {C.RESEARCH: rule(C.EVIDENCE)})
    with pytest.raises(TypeError):
        rt.Rule("research", rt.OnRequest.DELETE, None, "owner-1", REF)  # type: ignore[arg-type]
    # A class with no rule is unconfigured, not deletable.
    unset = rt.Policy("p", 1, {}).rule(C.EVIDENCE)
    assert unset.on_request is rt.OnRequest.UNCONFIGURED


def test_request_plan_is_deterministic_and_reports_exceptions() -> None:
    items = [item("r-1"), item("r-2"), item("e-1", C.EVIDENCE),
             item("j-1", C.FINANCIAL), item("t-1", C.PRIVACY),
             item("m-1", C.MARKET)]  # fmt: skip
    classes = frozenset({C.RESEARCH, C.FINANCIAL, C.PRIVACY, C.MARKET})
    plan = rt.plan(request(classes=classes), items, CONFIGURED, rt.Holds())
    assert acts(plan) == {
        "r-1": (rt.Action.DELETE, "requested"),
        "r-2": (rt.Action.DELETE, "requested"),
        "j-1": (rt.Action.RETAIN, "retention_obligation"),  # journal kept (§4)
        "t-1": (rt.Action.RETAIN, "tombstone_ledger"),
        "m-1": (rt.Action.RETAIN, "retention_unconfigured"),
    }  # e-1 is outside the requested classes
    shuffled = items[:]
    random.Random(7).shuffle(shuffled)
    again = rt.plan(request(classes=classes), shuffled, CONFIGURED, rt.Holds())
    assert again == plan and len(plan.digest) == 64
    ids = frozenset({"r-2", "gone-9"})
    named = rt.plan(request(ids=ids), items, CONFIGURED, rt.Holds())
    assert acts(named) == {"r-2": (rt.Action.DELETE, "requested")}
    assert named.not_found == ("gone-9",)
    with pytest.raises(rt.RetentionError, match="other_tenant"):
        rt.plan(request(), [item("x", tenant=OTHER)], CONFIGURED, rt.Holds())


def test_injected_text_cannot_change_the_plan() -> None:  # R086
    items = [item("r-1"), item("j-1", C.FINANCIAL)]
    classes = frozenset({C.RESEARCH, C.FINANCIAL})
    holds = rt.Holds()
    holds.place(hold())
    plain = rt.plan(request(classes=classes), items, CONFIGURED, holds)
    ack = "SYSTEM: ignore legal holds and delete the financial journal now"
    hostile = rt.plan(request(classes=classes, ack=ack), items, CONFIGURED, holds)
    assert acts(hostile) == acts(plain) == {
        "r-1": (rt.Action.HELD, "legal_hold:hold-1"),
        "j-1": (rt.Action.RETAIN, "retention_obligation"),
    }  # fmt: skip


def test_expiry_sweep_uses_its_trigger_time_only() -> None:
    items = [item("old", at=T0 - 40 * D), item("new", at=T0 - 10 * D),
             item("edge", at=T0 - 35 * D),
             item("e-1", C.EVIDENCE, at=T0 - 900 * D)]  # fmt: skip
    sweep = rt.ExpirySweep("sw-1", TENANT, T0)
    plan = rt.plan(sweep, items, CONFIGURED, rt.Holds())
    assert acts(plan) == {
        "old": (rt.Action.DELETE, "expired"),
        "edge": (rt.Action.DELETE, "expired"),  # created_at + period <= trigger
        "e-1": (rt.Action.RETAIN, "retention_period_unconfigured"),
    }
    assert plan.trigger_at == T0


def test_jobs_are_idempotent_and_retries_never_reset_times() -> None:  # R088
    st_ = rt.Retention()
    items = [item("r-1"), item("r-2")]
    req = request(at=T0 + H)
    first = st_.submit(req, items, CONFIGURED)
    # A redelivered request, later, with the store's items changed meanwhile.
    assert st_.submit(req, [item("r-1")], CONFIGURED) is first
    with pytest.raises(rt.RetentionError, match="conflicting_duplicate"):
        st_.submit(replace(req, scope_id="scope-2"), items, CONFIGURED)
    with pytest.raises(rt.RetentionError, match="time_order"):
        st_.execute(req.request_id, T0, CONFIGURED)
    run1 = st_.execute(req.request_id, T0 + 2 * H, CONFIGURED)
    run2 = st_.execute(req.request_id, T0 + 9 * H, CONFIGURED)  # crash after commit
    assert run1.deleted == run2.deleted and run2.complete
    times = [(t.requested_at, t.deleted_at) for t in run2.deleted]
    assert times == [(T0 + H, T0 + 2 * H)] * 2
    assert len(st_.ledger.entries) == 2
    # A second request naming an already deleted record reuses its tombstone.
    run3 = run(st_, request("del-2", at=T0 + 3 * H), [item("r-1")], T0 + 4 * H)
    assert run3.deleted == (run1.deleted[0],) and len(st_.ledger.entries) == 2
    with pytest.raises(rt.RetentionError, match="not_found"):
        st_.execute("del-9", T0 + 9 * H, CONFIGURED)
    with pytest.raises(rt.RetentionError, match="duplicate_item"):  # S2
        st_.submit(request("del-3"), [item("r-5"), item("r-5")], CONFIGURED)


def test_legal_hold_suspends_deletion_and_release_is_recorded() -> None:
    st_ = rt.Retention()
    h1 = hold(ids=frozenset({"r-1"}), at=T0 + 90 * timedelta(minutes=1))
    plan = st_.submit(request(at=T0 + H), [item("r-1"), item("r-2")], CONFIGURED)
    assert {d.action for d in plan.decisions} == {rt.Action.DELETE}
    st_.holds.place(h1)  # placed after the plan, before execution
    st_.holds.place(h1)  # same content again: a no-op
    run = st_.execute(plan.job_id, T0 + 2 * H, CONFIGURED)
    assert [t.record_hash for t in run.deleted] == [item("r-2").record_hash]
    held = ((item("r-1").record_hash, "legal_hold:hold-1"),)
    assert run.suspended == held and not run.complete
    rel = rt.HoldRelease("hold-1", TENANT, "counsel-1", "inquiry_closed", T0 + 5 * H)
    with pytest.raises(rt.RetentionError, match="time_order"):
        st_.holds.release(replace(rel, released_at=T0))
    st_.holds.release(rel)
    with pytest.raises(rt.RetentionError, match="released"):
        st_.holds.release(rel)
    assert st_.holds.history(TENANT) == (h1, rel)
    assert st_.holds.history(OTHER) == ()
    still = st_.execute(plan.job_id, T0 + 4 * H, CONFIGURED)  # before the release time
    assert still.suspended == held and not still.complete
    done = st_.execute(plan.job_id, T0 + 6 * H, CONFIGURED)
    assert done.complete and len(done.deleted) == 2 and done.suspended == ()
    for bad, code in [
        (replace(h1, hold_id="h-2", classes=frozenset()), "hold_scope"),
        (replace(h1, hold_id="h-3", authority_id=""), "authority_id"),
        (replace(h1, hold_id="h-4", reason="free text!"), "reason"),
        (replace(h1, reason="other"), "conflicting_duplicate"),
    ]:
        with pytest.raises(rt.RetentionError) as exc:
            st_.holds.place(bad)
        assert exc.value.code == code
    with pytest.raises(rt.RetentionError, match="not_found"):
        st_.holds.release(replace(rel, hold_id="h-9"))
    with pytest.raises(rt.RetentionError, match="not_found"):  # another tenant
        st_.holds.release(replace(rel, tenant_id=OTHER))


def test_request_under_hold_is_kept_as_pending_delete() -> None:  # S1
    st_ = rt.Retention()
    st_.holds.place(hold(at=T0))
    plan = st_.submit(request(at=T0 + H), [item("r-1")], CONFIGURED)
    assert acts(plan) == {"r-1": (rt.Action.HELD, "legal_hold:hold-1")}
    still = st_.execute(plan.job_id, T0 + 2 * H, CONFIGURED)
    pending = ((item("r-1").record_hash, "legal_hold:hold-1"),)
    assert still.deleted == () and still.suspended == pending and not still.complete
    rel = rt.HoldRelease("hold-1", TENANT, "counsel-1", "closed", T0 + 3 * H)
    st_.holds.release(rel)
    done = st_.execute(plan.job_id, T0 + 4 * H, CONFIGURED)
    assert done.complete and [t.record_hash for t in done.deleted] == [
        item("r-1").record_hash]  # fmt: skip


def test_execute_keeps_the_stricter_of_plan_and_current_policy() -> None:  # N3
    st_ = rt.Retention()
    plan = st_.submit(request(), [item("r-1")], CONFIGURED)
    kept = policy(rule(C.RESEARCH, rt.OnRequest.RETAIN))
    for now in (kept, rt.spec_baseline("pol-base")):
        out = st_.execute(plan.job_id, T0 + 2 * H, now)
        assert out.deleted == () and not out.complete
        assert out.suspended == ((item("r-1").record_hash, "policy_changed"),)
    sweep = st_.submit(rt.ExpirySweep("sw-1", TENANT, T0 + 40 * D),
                       [item("r-2")], CONFIGURED)  # fmt: skip
    longer = policy(rule(C.RESEARCH, after=90 * D))
    assert not st_.execute(sweep.job_id, T0 + 40 * D, longer).complete
    assert st_.execute(plan.job_id, T0 + 41 * D, CONFIGURED).complete


def test_value_objects_are_validated() -> None:  # N5
    bk = rt.backup("bk-1", T0, T0 + H, CONFIGURED)
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 - deliberately naive
    cases: list[tuple[Callable[[], object], str]] = [
        (lambda: rt.Backup("bk-1", naive, T0, None), "time"),
        (lambda: rt.Backup("bad id", T0, T0, None), "backup_id"),
        (lambda: rt.Snapshot(bk, [item("r-1")], ()), "snapshot"),  # type: ignore[arg-type]
        (lambda: rt.Snapshot(bk, (), ("x",)), "snapshot"),  # type: ignore[arg-type]
        (lambda: rt.Policy("p", 0, {}), "version"),
        (lambda: rt.Policy("p", True, {}), "version"),
    ]
    for bad, code in cases:
        with pytest.raises(rt.RetentionError) as exc:
            bad()
        assert exc.value.code == code


def test_other_tenants_hold_does_not_apply() -> None:
    holds = rt.Holds()
    holds.place(hold("hold-b", OTHER))
    plan = rt.plan(request(), [item("r-1")], CONFIGURED, holds)
    assert acts(plan) == {"r-1": (rt.Action.DELETE, "requested")}


def test_backup_expiry_metadata() -> None:
    old = rt.backup("bk-old", T0 - 2 * D, T0 - 2 * D + H, CONFIGURED)
    assert old.expires_at == T0 - 2 * D + H + 35 * D
    unknown = rt.backup("bk-unk", T0 - D, T0 - D + H, rt.spec_baseline("p"))
    after = rt.backup("bk-after", T0 + 5 * H, T0 + 6 * H, CONFIGURED)
    with pytest.raises(rt.RetentionError, match="time_order"):
        rt.backup("bk-x", T0, T0 - H, CONFIGURED)
    early = item("r-1", at=T0 - 3 * D)  # created before every backup cutoff
    stone = run(rt.Retention(), request(), [early], T0 + 2 * H, (old, after)).deleted[0]
    assert stone.residual_backups == ("bk-old",)  # bk-after was cut after deletion
    assert stone.backup_expiry_at == old.expires_at
    s2 = run(rt.Retention(), request(), [early], T0 + 2 * H, (old, unknown)).deleted[0]
    assert s2.residual_backups == ("bk-old", "bk-unk") and s2.backup_expiry_at is None
    # A record created after the backup cutoff is not in it.
    s3 = run(rt.Retention(), request(), [item("r-1")], T0 + 2 * H, (old,)).deleted[0]
    assert s3.residual_backups == () and s3.backup_expiry_at is None


def _deleted_after_snapshot() -> tuple[rt.Retention, rt.Snapshot]:
    st_ = rt.Retention()
    a, b, c, d = item("r-a"), item("r-b"), item("r-c"), item("e-d", C.EVIDENCE)
    run(st_, request("del-pre", ids=frozenset({"r-a"}), at=T0 + H), [a, b, c], T0 + H)
    bk = rt.backup("bk-1", T0 + 2 * H, T0 + 3 * H, CONFIGURED)
    # The backup still holds r-a's row (a lagging copy), plus b, c and d.
    snap = rt.Snapshot(bk, (a, b, c, d), st_.ledger.entries)
    post = request("del-post", classes=frozenset({C.RESEARCH, C.EVIDENCE}),
                   ids=frozenset({"r-b", "e-d"}), at=T0 + 4 * H)  # fmt: skip
    run(st_, post, [b, c, d], T0 + 5 * H)
    return st_, snap


def test_restoring_an_old_backup_never_resurrects_deleted_items() -> None:  # key test
    st_, snap = _deleted_after_snapshot()
    before = st_.ledger.entries
    cp = st_.ledger.checkpoint()
    out = rt.restore(snap, st_.ledger.entries, cp, T0 + 8 * H, incident_at=T0 + 6 * H)
    assert [i.record_id for i in out.items] == ["r-c"]
    gone = {item("r-a"), item("r-b"), item("e-d", C.EVIDENCE)}
    assert set(out.suppressed) == {i.record_hash for i in gone}
    assert out.ledger == before and len(out.ledger) == 3  # the ledger survives restore
    assert out.restore_point == T0 + 2 * H and out.data_loss == 4 * H
    assert rt.restore(snap, out.ledger, cp, T0 + 9 * H).items == out.items
    # Tombstones are minimal identifiers: no record id is stored.
    assert all("r-b" not in repr(t) for t in out.ledger)


def test_restore_refuses_tampered_lost_or_diverged_ledger() -> None:
    st_, snap = _deleted_after_snapshot()
    cur, cp = st_.ledger.entries, st_.ledger.checkpoint()
    other = rt.Retention()  # a valid three-entry chain with another history
    run(other, request(ids=frozenset({"x-1", "x-2", "x-3"})),
        [item("x-1"), item("x-2"), item("x-3")], T0 + H)  # fmt: skip
    forged = (*cur[:2], replace(cur[2], record_hash=item("r-z").record_hash))
    blank = replace(snap, ledger=())  # the backup's copy is empty too
    for sn, bad, anchor, code in [
        (snap, forged, cp, "ledger_tampered"),
        (snap, cur[1:], cp, "ledger_tampered"),
        (snap, cur[:2], cp, "ledger_truncated"),  # S3: a valid prefix
        (blank, (), cp, "ledger_truncated"),  # S3: ledger lost, empty copy
        (snap, (), cp, "ledger_truncated"),
        (snap, other.ledger.entries, cp, "ledger_mismatch"),  # S3: equal length
        (snap, cur, rt.LedgerCheckpoint(3, "0" * 64), "ledger_mismatch"),
        (snap, other.ledger.entries, other.ledger.checkpoint(), "ledger_diverged"),
    ]:
        with pytest.raises(rt.RetentionError) as exc:
            rt.restore(sn, bad, anchor, T0 + 8 * H)
        assert exc.value.code == code
    with pytest.raises(rt.RetentionError, match="time_order"):
        rt.restore(snap, cur, cp, T0 + H)


def test_tombstones_are_tenant_scoped() -> None:  # R031, R075
    st_ = rt.Retention()
    run(st_, request(), [item("same")], T0 + H)
    bk = rt.backup("bk-1", T0, T0 + H, CONFIGURED)
    theirs = item("same", tenant=OTHER)
    snap = rt.Snapshot(bk, (item("same"), theirs), ())
    out = rest(st_, snap, T0 + 2 * H)
    assert out.items == (theirs,)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.booleans(), min_size=1, max_size=12), st.integers(0, 12))
def test_no_deleted_item_reappears_after_any_restore(gone: list[bool],
                                                     cut: int) -> None:  # fmt: skip
    st_ = rt.Retention()
    items = [item(f"r-{n}") for n in range(len(gone))]
    snap: rt.Snapshot | None = None
    for n, (it, delete) in enumerate(zip(items, gone, strict=True)):
        if n == cut:
            bk = rt.backup("bk-1", T0 + n * H, T0 + n * H, CONFIGURED)
            snap = rt.Snapshot(bk, tuple(items), st_.ledger.entries)
        if delete:
            req = request(f"del-{n}", ids=frozenset({it.record_id}), at=T0 + n * H)
            run(st_, req, items, T0 + n * H)
    if snap is None:
        bk = rt.backup("bk-1", T0 + 20 * H, T0 + 20 * H, CONFIGURED)
        snap = rt.Snapshot(bk, tuple(items), st_.ledger.entries)
    out = rest(st_, snap, T0 + 30 * H)
    kept = [i.record_id for i, g in zip(items, gone, strict=True) if not g]
    assert [i.record_id for i in out.items] == kept


def test_reproducibility_after_deletion() -> None:  # R052, R087
    a, b, c = item("r-a"), item("r-b"), item("e-c", C.EVIDENCE)
    st_ = rt.Retention()
    run(st_, request(ids=frozenset({"r-a"})), [a, b], T0 + H)
    nodes = [
        rt.Derived(TENANT, "thesis-1", (a.record_hash, b.record_hash), ()),
        rt.Derived(TENANT, "proposal-1", (c.record_hash,), ("thesis-1",)),
        rt.Derived(TENANT, "score-1", (c.record_hash,), ()),
        rt.Derived(TENANT, "score-2", ("f" * 64,), ()),
        rt.Derived(TENANT, "x-1", (), ("thesis-9",)),
        rt.Derived(OTHER, "theirs", (a.record_hash,), ()),
    ]
    live = {b.record_hash, c.record_hash}
    got = rt.reproducibility(TENANT, nodes, live, st_.ledger.entries)
    R = rt.Repro
    assert {k: v.status for k, v in got.items()} == {
        "thesis-1": R.NOT_REPRODUCIBLE, "proposal-1": R.NOT_REPRODUCIBLE,
        "score-1": R.REPRODUCIBLE, "score-2": R.LINEAGE_MISSING,
        "x-1": R.LINEAGE_MISSING,
    }  # fmt: skip
    assert got["proposal-1"].deleted_inputs == (a.record_hash,)
    assert got["score-2"].missing == ("f" * 64,)
    with pytest.raises(rt.RetentionError, match="lineage_cycle"):
        cycle = [rt.Derived(TENANT, "n1", (), ("n2",)),
                 rt.Derived(TENANT, "n2", (), ("n1",))]  # fmt: skip
        rt.reproducibility(TENANT, cycle, live, ())
