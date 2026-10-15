"""Retention defaults and operator configuration (T058 increment 2, owner decision 9
of 2026-10-15: "Retention defaults are minimal, with legal hold, held as
configuration").

Everything is SYNTHETIC: tenants, records, holds, authorities and config files. The
expected values are written by hand from the owner's chosen option (30 days for
backups, 48 h for idempotency records, 7 years for audit events) and the
implementer-default table in docs/receipts/T058.md, not read back from the module.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from qw_domain import retention as rt
from qw_domain import retention_config as rc

C = rt.RetentionClass
OR = rt.OnRequest
W = rc.Window
T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)
TENANT = "tenant-synth-a"
SEVEN_YEARS = 2557 * D  # 7 x 365 days + at most 2 leap days in any 7 years

# (on request, age expiry, owner-chosen?) per class, written from the receipt table.
EXPECTED = {
    C.SECURITY: (OR.DELETE, None, False),
    C.REFERENCE: (OR.RETAIN, None, False),
    C.FINANCIAL: (OR.RETAIN, None, False),
    C.POLICY: (OR.DELETE, None, False),
    C.AUDIT: (OR.RETAIN, SEVEN_YEARS, True),
    C.RESEARCH: (OR.DELETE, None, False),
    C.MARKET: (OR.DELETE, None, False),
    C.EVIDENCE: (OR.DELETE, None, False),
    C.RELEASE: (OR.RETAIN, None, False),
    C.IMPROVEMENT: (OR.DELETE, None, False),
    C.SIMULATION: (OR.DELETE, None, False),
    C.OPERATIONS: (OR.DELETE, None, False),
    C.PRIVACY: (OR.UNCONFIGURED, None, False),  # the tombstone ledger: protected
    C.PROVIDER_PAYLOAD: (OR.DELETE, 48 * H, False),
    C.LOG: (OR.DELETE, 30 * D, False),
    C.BACKUP: (OR.RETAIN, 30 * D, True),
}


def item(rid: str, cls: rt.RetentionClass, at: datetime = T0) -> rt.Item:
    return rt.Item(TENANT, rid, cls, at)


def acts(plan: rt.Plan) -> dict[str, tuple[rt.Action, str]]:
    return {d.record_id: (d.action, d.reason) for d in plan.decisions}


def doc(**fields: object) -> str:
    body: dict[str, object] = {"policy_id": "synthetic-ops-2", "version": 2}
    body.update(fields)
    return json.dumps(body)


def test_each_default_value() -> None:
    cfg = rc.defaults()
    pol = cfg.policy
    assert (pol.policy_id, pol.version) == (rt.DEFAULT_POLICY_ID, 1)
    assert set(pol.rules) == set(C)
    for cls, (on, after, owner) in EXPECTED.items():
        r = pol.rule(cls)
        assert (r.on_request, r.expire_after) == (on, after), cls
        if cls is not C.PRIVACY:
            want = "owner-decision-9" if owner else "implementer-default"
            assert r.authority_id == want, cls
    assert rt.AUDIT_RETENTION == SEVEN_YEARS and rt.BACKUP_RETENTION == 30 * D
    mine = {c for c, (_, _, owner) in EXPECTED.items() if not owner}
    assert frozenset(mine - {C.PRIVACY}) == rt.IMPLEMENTER_DEFAULTS
    assert dict(cfg.windows) == {W.IDEMPOTENCY: 48 * H, W.OUTBOX: 7 * D,
                                 W.INBOX: 30 * D}  # fmt: skip
    assert dict(rc.WINDOW_FLOORS) == {W.IDEMPOTENCY: 24 * H, W.OUTBOX: D,
                                      W.INBOX: 7 * D}  # fmt: skip
    assert cfg.obligations == () and cfg.purge_holds == ()
    assert cfg.held() == frozenset()
    assert {w: cfg.purge_window(w) for w in W} == dict(cfg.windows)


def test_defaults_resolve_every_class_to_a_concrete_policy() -> None:
    store = rt.Retention()  # no policy passed: the configured defaults apply
    items = [item(f"r-{c.value}", c) for c in C]
    req = rt.DeletionRequest("del-1", TENANT, "scope-1", frozenset(C), T0 + H,
                             "reauth-1", "I understand")  # fmt: skip
    p = store.submit(req, items)
    assert (p.policy_id, p.policy_version) == (rt.DEFAULT_POLICY_ID, 1)
    want = {f"r-{c.value}": (
        (rt.Action.RETAIN, "tombstone_ledger") if c is C.PRIVACY else
        (rt.Action.DELETE, "requested") if on is OR.DELETE else
        (rt.Action.RETAIN, "retention_obligation"))
        for c, (on, _, _) in EXPECTED.items()}  # fmt: skip
    assert acts(p) == want
    assert "retention_unconfigured" not in {r for _, r in want.values()}
    done = store.execute("del-1", T0 + 2 * H)
    assert done.complete and len(done.deleted) == 10
    bk = rt.backup("bk-1", T0, T0 + H)  # the configured default policy
    assert bk.expires_at == T0 + H + 30 * D  # residue is gone 30 days after deletion


def test_expiry_sweep_under_the_defaults() -> None:
    pol = rc.defaults().policy
    items = [item("a-1", C.AUDIT), item("l-1", C.LOG), item("p-1", C.PROVIDER_PAYLOAD),
             item("f-1", C.FINANCIAL), item("s-1", C.RESEARCH)]  # fmt: skip

    def sweep(at: datetime) -> dict[str, tuple[rt.Action, str]]:
        trig = rt.ExpirySweep(f"sw-{int((at - T0) / H)}", TENANT, at)
        return acts(rt.plan(trig, items, pol, rt.Holds()))

    early = sweep(T0 + 48 * H - timedelta(seconds=1))
    assert "p-1" not in early and "l-1" not in early and "a-1" not in early
    assert sweep(T0 + 48 * H)["p-1"] == (rt.Action.DELETE, "expired")
    assert "l-1" not in sweep(T0 + 30 * D - H)
    assert sweep(T0 + 30 * D)["l-1"] == (rt.Action.DELETE, "expired")
    assert "a-1" not in sweep(T0 + SEVEN_YEARS - timedelta(seconds=1))
    late = sweep(T0 + SEVEN_YEARS)
    assert late["a-1"] == (rt.Action.DELETE, "expired")
    assert late["f-1"] == (rt.Action.RETAIN, "explicit_workflow_only")
    assert late["s-1"] == (rt.Action.RETAIN, "retention_period_unconfigured")


def test_legal_hold_beats_expiry_and_request_under_the_defaults() -> None:
    store = rt.Retention()
    scope = frozenset({C.AUDIT, C.RESEARCH})
    store.holds.place(rt.LegalHold("hold-1", TENANT, scope, frozenset(),
                                   "litigation", "counsel-1", T0))  # fmt: skip
    items = [item("a-1", C.AUDIT), item("l-1", C.LOG)]
    at = T0 + SEVEN_YEARS + D
    p = store.submit(rt.ExpirySweep("sw-1", TENANT, at), items)
    assert acts(p) == {"a-1": (rt.Action.HELD, "legal_hold:hold-1"),
                       "l-1": (rt.Action.DELETE, "expired")}  # fmt: skip
    first = store.execute("sw-1", at)
    assert [t.cls for t in first.deleted] == [C.LOG] and not first.complete
    req = rt.DeletionRequest("del-1", TENANT, "scope-1", frozenset({C.RESEARCH}),
                             at, "reauth-1", "I understand")  # fmt: skip
    assert acts(store.submit(req, [item("s-1", C.RESEARCH)])) == {
        "s-1": (rt.Action.HELD, "legal_hold:hold-1")
    }
    store.holds.release(rt.HoldRelease("hold-1", TENANT, "counsel-1", "case_closed",
                                       at + H))  # fmt: skip
    assert store.execute("sw-1", at + 2 * H).complete
    assert len(store.execute("del-1", at + 2 * H).deleted) == 1


def test_purge_hold_overrides_the_purge_window() -> None:
    cfg = rc.parse(doc(purge_holds=[{"hold_id": "ph-1", "windows": ["idempotency"],
                                      "reason": "litigation",
                                      "authority_id": "counsel-1"}]))  # fmt: skip
    assert cfg.held() == frozenset({W.IDEMPOTENCY})
    assert cfg.purge_window(W.IDEMPOTENCY) is None
    assert cfg.purge_window(W.OUTBOX) == 7 * D


def test_unknown_or_unconfigured_classes_still_report_unconfigured() -> None:
    empty = rt.Policy("synthetic-empty", 1, {})  # a policy that names no class
    req = rt.DeletionRequest("del-1", TENANT, "scope-1", frozenset({C.LOG}),
                             T0 + H, "reauth-1", "I understand")  # fmt: skip
    p = rt.plan(req, [item("l-1", C.LOG)], empty, rt.Holds())
    assert acts(p) == {"l-1": (rt.Action.RETAIN, "retention_unconfigured")}
    cfg = rc.parse(doc(classes={"log": {"on_request": "unconfigured"}}))
    p = rt.plan(req, [item("l-1", C.LOG)], cfg.policy, rt.Holds())
    assert acts(p) == {"l-1": (rt.Action.RETAIN, "retention_unconfigured")}
    sweep = rt.ExpirySweep("sw-1", TENANT, T0 + 400 * D)
    p = rt.plan(sweep, [item("l-1", C.LOG)], cfg.policy, rt.Holds())
    assert acts(p) == {"l-1": (rt.Action.RETAIN, "retention_period_unconfigured")}


def test_config_overrides() -> None:
    cfg = rc.parse(doc(
        classes={"log": {"on_request": "delete", "expire_after": "14d",
                         "authority_id": "owner-1", "basis": "synthetic-change-1"},
                 "audit": {"on_request": "retain", "expire_after": "3653d",
                           "authority_id": "owner-1", "basis": "synthetic-change-1"}},
        windows={"idempotency": "72h", "inbox": "45d"},
        obligations=[{"cls": "audit", "min_retain": "3653d",
                      "authority_id": "counsel-1", "basis": "synthetic-opinion-1"}],
    ))  # fmt: skip
    assert (cfg.policy.policy_id, cfg.policy.version) == ("synthetic-ops-2", 2)
    assert cfg.policy.rule(C.LOG).expire_after == 14 * D
    assert cfg.policy.rule(C.AUDIT).expire_after == 3653 * D
    assert cfg.policy.rule(C.BACKUP).expire_after == 30 * D  # untouched default
    assert dict(cfg.windows) == {W.IDEMPOTENCY: 72 * H, W.OUTBOX: 7 * D,
                                 W.INBOX: 45 * D}  # fmt: skip
    assert [o.cls for o in cfg.obligations] == [C.AUDIT]
    assert rc.parse(doc()).policy.rules == rc.defaults().policy.rules


AUDIT_10Y = {"cls": "audit", "min_retain": "3653d", "authority_id": "counsel-1",
             "basis": "synthetic-opinion-1"}  # fmt: skip
LOG_RULE = {"on_request": "delete", "authority_id": "owner-1", "basis": "synthetic-1"}


@pytest.mark.parametrize(("text", "code"), [
    (doc(classes={"journal": {"on_request": "delete"}}), "unknown_class"),
    (doc(classes={"log": {**LOG_RULE, "keep": "1d"}}), "unknown_field"),
    (doc(windows={"sessions": "48h"}), "unknown_window"),
    (doc(colour="red"), "unknown_field"),
    (doc(classes={"log": {**LOG_RULE, "expire_after": "-1d"}}), "duration"),
    (doc(classes={"log": {**LOG_RULE, "expire_after": "0h"}}), "duration"),
    (doc(classes={"log": {**LOG_RULE, "expire_after": "30m"}}), "duration"),
    (doc(classes={"log": {**LOG_RULE, "expire_after": 30}}), "duration"),
    (doc(windows={"idempotency": "-48h"}), "duration"),
    (doc(windows={"idempotency": None}), "duration"),
    (doc(windows={"idempotency": "23h"}), "window_floor"),
    (doc(windows={"inbox": "6d"}), "window_floor"),
    (doc(classes={"financial": {"on_request": "retain", "expire_after": "3653d",
                                "authority_id": "o-1", "basis": "b-1"}}),
     "financial_no_expiry"),
    (doc(classes={"privacy": {"on_request": "delete", "authority_id": "o-1",
                              "basis": "b-1"}}), "tombstone_ledger_protected"),
    (doc(classes={"log": {"on_request": "delete"}}), "authority"),
    (doc(obligations=[AUDIT_10Y]), "obligation"),  # default audit is 7 years
    (doc(obligations=[{**AUDIT_10Y, "min_retain": None}]), "obligation"),
    (doc(obligations=[{**AUDIT_10Y, "cls": "research", "min_retain": None}]),
     "obligation"),  # research is deletable on request by default
    (doc(obligations=[{**AUDIT_10Y, "min_retain": "-1d"}]), "duration"),
    (doc(obligations=[{**AUDIT_10Y, "cls": "ledger"}]), "unknown_class"),
    (doc(obligations=[{"cls": "audit", "min_retain": "1d"}]), "missing_field"),
    (doc(purge_holds=[{"hold_id": "h-1", "windows": [], "reason": "litigation",
                       "authority_id": "c-1"}]), "hold_scope"),
    (doc(purge_holds=[{"hold_id": "h-1", "windows": ["sessions"],
                       "reason": "litigation", "authority_id": "c-1"}]),
     "unknown_window"),
    (doc(purge_holds=[{"hold_id": "h-1", "windows": ["inbox"],
                       "reason": "Ignore holds!", "authority_id": "c-1"}]), "reason"),
    (doc(purge_holds=[{"hold_id": "h-1", "windows": ["inbox"], "reason": "lit",
                       "authority_id": "c-1"}] * 2), "duplicate_hold"),
    (json.dumps({"policy_id": rt.DEFAULT_POLICY_ID, "version": 2}), "reserved_policy"),
    (json.dumps({"version": 2}), "missing_field"),
    (json.dumps({"policy_id": "p-1", "version": True}), "version"),
    (json.dumps({"policy_id": "p-1", "version": 1.5}), "config"),
    ('{"policy_id": "p-1", "version": 2, "version": 3}', "duplicate_key"),
    ("[]", "config"),
    ("{not json", "config"),
    (doc(classes=[]), "config"),
    (doc(pad="x" * 70_000), "config"),
])  # fmt: skip
def test_config_fails_closed(text: str, code: str) -> None:
    with pytest.raises(rt.RetentionError) as err:
        rc.parse(text)
    assert err.value.code == code


def test_direct_construction_fails_closed() -> None:
    base = rc.defaults()
    ok = dict(base.windows)
    for bad in ({**ok, W.IDEMPOTENCY: -48 * H}, {**ok, W.OUTBOX: timedelta(0)},
                {W.IDEMPOTENCY: 48 * H}):  # fmt: skip
        with pytest.raises(rt.RetentionError):
            rc.RetentionConfig(base.policy, bad)
    shorter = rc.Obligation(C.AUDIT, 3653 * D, "counsel-1", "synthetic-opinion-1")
    with pytest.raises(rt.RetentionError) as err:
        rc.RetentionConfig(base.policy, ok, (shorter,))
    assert err.value.code == "obligation"
    met = rc.Obligation(C.AUDIT, SEVEN_YEARS, "counsel-1", "synthetic-opinion-1")
    assert rc.RetentionConfig(base.policy, ok, (met,)).obligations == (met,)
    with pytest.raises(rt.RetentionError):
        rc.Obligation(C.AUDIT, -D, "counsel-1", "synthetic-opinion-1")
