"""Learned-state classification, lineage invalidation, safe export and import
(T052 increment 1). All tenants, feedback, cases, consents, candidates, approvals,
releases and keys are SYNTHETIC. Expected lineage, causes and hashes are written by
hand from the rules in the module docstring, not recomputed by the module.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain import feedback as fb
from qw_domain import improvement as im
from qw_domain import learned_state as ls
from qw_domain import personal_adaptation as pa
from qw_domain import release as rl
from referencing import Registry, Resource
from test_improvement import EXP, GOOD, assess, cand, promo_ledger, proto
from test_personal_adaptation import (
    APPROVE_AT,
    BASE,
    FIXTURE,
    HELPFUL,
    MIN,
    OK_SUBJ,
    OTHER,
    TH_SUBJ,
    approvals,
    candidate,
    protocol,
)
from test_release import M1, approval
from test_trial_ledger import T0, TENANT

K, A, OR, B = ls.Kind, ls.Action, ls.Origin, ls.Basis
CAT = fb.FeedbackCategory
DAY3 = T0 + timedelta(days=3)
ADOPT_AT = APPROVE_AT + 2 * MIN
ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "docs/spec/contracts/schemas"
REC, LIN = "learning/records.json", "learning/lineage.json"


def n(kind: ls.Kind, ident: str, scope: str = TENANT) -> str:
    return f"{kind.value}/{scope}/{ident}"


def note(ev: fb.ImprovementEvidence, tenant: str, i: int, cat: fb.FeedbackCategory,
         subj: fb.Subject) -> None:  # fmt: skip
    at = T0 + (i - 10) * MIN
    ev.record(fb.Feedback(tenant_id=tenant, record_id=f"fb-{i}", subject=subj,
                          occurred_at=at, recorded_at=at, principal_id="user-synth",
                          category=cat, message="SYNTHETIC"))  # fmt: skip


def case(rid: str, split: fb.Split, origin: fb.CaseOrigin) -> fb.EvaluationCase:
    at = T0 - 5 * MIN
    return fb.EvaluationCase(tenant_id=TENANT, record_id=rid, subject=TH_SUBJ,
                             occurred_at=at, recorded_at=at, split=split,
                             inputs={"claim_kind": "reported_fact"},
                             expected="supported", origin=origin)  # fmt: skip


type World = tuple[fb.ImprovementEvidence, im.ExperimentRegistry, pa.PrivateState]


def personal_world() -> World:
    """Three own feedback records, a synthetic case and a final-holdout case; one
    logged personal selection; cand-1 assessed and adopted through the guard."""
    ev = fb.ImprovementEvidence()
    for i, cat, subj in ((1, CAT.IRRELEVANT, OK_SUBJ), (2, CAT.IRRELEVANT, OK_SUBJ),
                         (3, CAT.UNCLEAR, TH_SUBJ)):  # fmt: skip
        note(ev, TENANT, i, cat, subj)
    note(ev, OTHER, 4, CAT.UNCLEAR, TH_SUBJ)
    ev.record(case("case-synth-1", fb.Split.DEVELOPMENT, fb.CaseOrigin.SYNTHETIC))
    ev.record(case("case-hold-1", fb.Split.FINAL_HOLDOUT,
                   fb.CaseOrigin.PERSONAL_HISTORICAL))  # fmt: skip
    proposed, use = pa.propose(ev, TENANT, EXP, BASE, T0 + MIN)
    assert proposed == HELPFUL
    c = candidate(proposed, (use,))
    reg = im.ExperimentRegistry(TENANT).register(protocol()).register_candidate(c, ev)
    metrics = pa.measure(protocol(), c, BASE, FIXTURE)
    reg = reg.record_verdict(assess(reg, promo_ledger(dict(metrics), c)))
    state = pa.PrivateState.open(TENANT, BASE, T0)
    lin = ls.Lineage(ev, registries=[reg])
    ls.adopt(state, reg, lin, "cand-1", approvals(), ADOPT_AT)
    return ev, reg, state


def test_learned_state_is_classified_and_linked_to_its_sources() -> None:
    ev, reg, state = personal_world()
    lin = ls.Lineage(ev, registries=[reg], states=[state])
    items = lin.items
    cnode, used = n(K.CANDIDATE, "cand-1"), ("fb-1", "fb-2", "fb-3", "case-synth-1")
    assert items[n(K.RECORD, "fb-1")].origin is OR.PRIVATE_TENANT
    assert items[n(K.RECORD, "fb-1")].basis is B.OWN_DATA
    synth = items[n(K.RECORD, "case-synth-1")]
    assert (synth.origin, synth.basis) == (OR.SYNTHETIC, B.NONE)
    assert items[n(K.RECORD, "case-hold-1")].origin is OR.PRIVATE_TENANT
    c = items[cnode]
    assert (c.origin, c.basis, c.purpose) == (
        OR.PRIVATE_TENANT, B.OWN_DATA, fb.Purpose.PERSONAL_ADAPTATION)  # fmt: skip
    assert c.inputs == tuple(n(K.RECORD, r) for r in used)
    assert items[n(K.VERDICT, "verdict-1")].inputs == (cnode,)
    assert items[n(K.ADAPTATION, "1")].inputs == ()
    assert items[n(K.ADAPTATION, "2")].inputs == (cnode,)
    assert n(K.RECORD, "fb-4", OTHER) not in items  # never read, never linked
    st = lin.statuses(ADOPT_AT)
    assert all(s.reproducible and not s.invalidated and s.action is A.NONE
               for s in st.values())  # fmt: skip
    # An input outside the given state fails closed instead of reading as valid.
    orphan = ls.Lineage(ev, states=[state]).statuses(ADOPT_AT)[n(K.ADAPTATION, "2")]
    assert orphan.invalidated and not orphan.reproducible
    assert orphan.reasons == (f"lineage_missing:{cnode}",)


def test_deleted_evidence_invalidates_derived_state_and_clears_reproducibility(
) -> None:  # fmt: skip
    ev, reg, state = personal_world()
    ev.delete(TENANT, "fb-1", DAY3)
    lin = ls.Lineage(ev, registries=[reg], states=[state])
    st = lin.statuses(DAY3)
    why = ("record_deleted:fb-1",)
    for node, action in ((n(K.CANDIDATE, "cand-1"), A.BLOCKED),
                         (n(K.VERDICT, "verdict-1"), A.BLOCKED),
                         (n(K.ADAPTATION, "2"), A.REBUILD_OR_ROLLBACK)):  # fmt: skip
        s = st[node]
        assert (s.invalidated, s.reproducible, s.reasons, s.action) == (
            True, False, why, action), node  # fmt: skip
    assert st[n(K.RECORD, "fb-1")].reasons == why
    for node in (n(K.RECORD, "fb-2"), n(K.ADAPTATION, "1")):
        assert st[node].reproducible and not st[node].invalidated
    # As known before the deletion, the same graph reads valid.
    assert lin.statuses(DAY3 - MIN)[n(K.ADAPTATION, "2")].reproducible
    # The invalidated candidate cannot be adopted again, even into a fresh state and
    # with an adoption time before the deletion.
    with pytest.raises(ls.LearnedStateError, match="invalidated"):
        ls.adopt(pa.PrivateState.open(TENANT, BASE, T0), reg, lin, "cand-1",
                 approvals(), ADOPT_AT)  # fmt: skip
    # Rollback to the clean baseline is allowed; the poisoned version stays blocked.
    v = ls.rollback(state, lin, 1, "user-synth", DAY3 + MIN)
    assert (v.number, v.source, state.active) == (3, "rollback:1", BASE)
    after = ls.Lineage(ev, registries=[reg], states=[state]).statuses(DAY3 + MIN)
    assert after[n(K.ADAPTATION, "2")].action is A.BLOCKED
    assert after[n(K.ADAPTATION, "3")].action is A.NONE
    assert after[n(K.ADAPTATION, "3")].reproducible
    with pytest.raises(ls.LearnedStateError, match="invalidated"):
        ls.rollback(state, lin, 2, "user-synth", DAY3 + 2 * MIN)
    # Erased configurations are never reproducible, whatever their lineage.
    state.erase("user-synth", DAY3 + 3 * MIN)
    gone = ls.Lineage(ev, registries=[reg], states=[state]).statuses(DAY3 + 3 * MIN)
    assert gone[n(K.ADAPTATION, "1")].reasons == ("erased",)
    assert not gone[n(K.ADAPTATION, "1")].reproducible


def shared_world() -> tuple[fb.ImprovementEvidence, im.ExperimentRegistry,
                            rl.ReleaseManifest]:  # fmt: skip
    """tenant consents to shared improvement; cand-1 uses fb-1; rel-1 is signed off."""
    ev, shared = fb.ImprovementEvidence(), fb.Purpose.SHARED_IMPROVEMENT
    ev.grant(fb.ConsentGrant(TENANT, "consent-1", "user-synth", shared,
                             "installation-synth", frozenset({fb.RecordKind.FEEDBACK}),
                             "terms-1", T0 - 20 * MIN, None))  # fmt: skip
    note(ev, TENANT, 1, CAT.IRRELEVANT, OK_SUBJ)
    sel = ev.select(TENANT, EXP, shared, fb.Access.DEVELOPMENT, T0 + MIN,
                    recipient="installation-synth",
                    terms_version="terms-1")  # fmt: skip
    assert sel.consent_ids == ("consent-1",)
    p = proto(scope=im.Scope.INSTALLATION, evidence_purpose=shared)
    reg = im.ExperimentRegistry(TENANT).register(p)
    reg = reg.register_candidate(cand(evidence_uses=(sel.use,)), ev)
    reg = reg.record_verdict(assess(reg, promo_ledger(GOOD)))
    compat = rl.Compatibility(1, 2, 1)
    return ev, reg, rl.prepare_release(reg, "cand-1", "rel-1", compat, approval(), M1)


def test_revoked_shared_consent_flags_the_release_for_retire_or_rebuild() -> None:
    ev, reg, man = shared_world()
    lin = ls.Lineage(ev, registries=[reg], releases=[man])
    rel, cnode = n(K.RELEASE, "rel-1"), n(K.CANDIDATE, "cand-1")
    r = lin.items[rel]
    assert (r.origin, r.basis, r.inputs) == (OR.SHARED, B.CONSENT, (cnode,))
    assert lin.items[cnode].inputs == (n(K.RECORD, "fb-1"), n(K.CONSENT, "consent-1"))
    assert lin.statuses(DAY3)[rel].reproducible
    ev.revoke(TENANT, "consent-1", DAY3)
    st = lin.statuses(DAY3)
    assert (st[rel].invalidated, st[rel].reproducible, st[rel].reasons,
            st[rel].action) == (True, False, ("consent_revoked:consent-1",),
                                A.RETIRE_OR_REBUILD)  # fmt: skip
    assert st[cnode].action is A.BLOCKED
    assert not st[n(K.RECORD, "fb-1")].invalidated  # the record itself remains
    with pytest.raises(ls.LearnedStateError, match="release_unbound"):
        ls.Lineage(ev, releases=[man])
    b = ls.export_bundle(ev, lin, TENANT, "export-synth-9", DAY3, registries=[reg])
    (row,) = json.loads(b.files["learning/candidates.json"])  # N4/N5: strategy
    assert row["payload"] is None and set(row["verdicts"][0]) == {
        "verdict_id", "decision", "scores", "assessed_at"}  # fmt: skip


def canon(o: object) -> bytes:
    return json.dumps(o, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()  # fmt: skip


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def exported() -> tuple[ls.Bundle, fb.ImprovementEvidence]:
    ev, reg, state = personal_world()
    ev.delete(TENANT, "fb-3", DAY3)
    lin = ls.Lineage(ev, registries=[reg], states=[state])
    out = ls.export_bundle(ev, lin, TENANT, "export-synth-1", DAY3, state=state,
                           registries=[reg])  # fmt: skip
    return out, ev


def test_export_is_content_addressed_and_carries_no_activation_or_secrets() -> None:
    b, ev = exported()
    m = dict(b.manifest)
    common = json.loads((SCHEMAS / "common.schema.json").read_text())
    schema = json.loads((SCHEMAS / "export_manifest.schema.json").read_text())
    refs = Registry().with_resource("common.schema.json",
                                    Resource.from_contents(common))  # fmt: skip
    Draft202012Validator(schema, registry=refs).validate(m)
    body = {k: v for k, v in m.items() if k != "content_hash"}
    assert m["content_hash"] == sha(canon(body))
    assert (m["contains_credentials"], m["contains_executable_code"]) == (False, False)
    entries = {e["path"]: e for e in m["files"]}  # type: ignore[attr-defined]
    assert set(entries) == set(b.files) == {
        "learning/records.json", "learning/consents.json", "learning/adaptation.json",
        "learning/candidates.json", "learning/lineage.json"}  # fmt: skip
    for path, raw in b.files.items():
        assert (entries[path]["sha256"], entries[path]["bytes"]) == (sha(raw), len(raw))
        text = raw.decode().lower()
        assert OTHER not in text and "fb-4" not in text  # no other tenant's data
        for bad in ("secret", "password", "credential", "token", "step_up", "signing",
                    "private_key", "api_key", "install", "adopt", "approval",
                    "session", '"active'):  # fmt: skip
            assert bad not in text, (path, bad)
    recs = json.loads(b.files["learning/records.json"])
    assert [r["record_id"] for r in recs] == ["fb-1", "fb-2", "case-synth-1"]
    held = {"record_class": "evaluation_case", "reason": "final_holdout:1"}
    assert held in m["omissions"]  # type: ignore[operator]
    versions = json.loads(b.files["learning/adaptation.json"])
    assert [(v["number"], v["status"]) for v in versions] == [
        (1, "inactive"), (2, "inactive")]  # fmt: skip
    lineage = {r["node"]: r for r in json.loads(b.files["learning/lineage.json"])}
    assert lineage[n(K.RECORD, "fb-3")]["reasons"] == ["record_deleted:fb-3"]
    assert lineage[n(K.CANDIDATE, "cand-1")]["reproducible"] is False
    assert lineage[n(K.ADAPTATION, "2")]["action"] == "rebuild_or_rollback"
    assert exported()[0] == b  # deterministic
    with pytest.raises(ls.LearnedStateError, match="foreign_tenant"):
        ls.export_bundle(ev, ls.Lineage(ev), OTHER, "export-synth-2", DAY3,
                         state=pa.PrivateState.open(TENANT, BASE, T0))  # fmt: skip
    with pytest.raises(ls.LearnedStateError, match="foreign_tenant"):
        reg = im.ExperimentRegistry(TENANT).register(protocol())
        ls.export_bundle(ev, ls.Lineage(ev), OTHER, "export-synth-3", DAY3,
                         registries=[reg])  # fmt: skip


def sealed(m: dict[str, object], files: dict[str, bytes]) -> ls.Bundle:
    body = {k: v for k, v in m.items() if k != "content_hash"}
    return ls.Bundle({**body, "content_hash": sha(canon(body))}, files)


def rehash(m: dict[str, object], files: dict[str, bytes]) -> ls.Bundle:
    """What an attacker holding the bundle can do: rewrite files and every hash."""
    old: list[dict[str, str]] = m["files"]  # type: ignore[assignment]
    cls = {e["path"]: e["content_class"] for e in old}
    entries = [{"path": p, "sha256": sha(f), "bytes": len(f),
                "content_class": cls.get(p, "private_learning")}
               for p, f in sorted(files.items())]  # fmt: skip
    return sealed({**m, "files": entries}, files)


def edit(b: ls.Bundle, path: str, fn: Callable[[list[dict[str, object]]], None]
         ) -> ls.Bundle:  # fmt: skip
    rows = json.loads(b.files[path])
    fn(rows)
    return rehash(dict(b.manifest), {**b.files, path: canon(rows)})


def test_import_is_inert_and_revalidates_everything() -> None:
    b, _ = exported()
    imp = ls.import_bundle(b, TENANT)
    assert imp.configurations == ((1, BASE), (2, HELPFUL))
    assert imp.manifest_hash == b.manifest["content_hash"]
    # Imported configuration becomes active only through the normal flow: a new
    # candidate still needs an assessment verdict and approvals.
    reg = im.ExperimentRegistry(TENANT).register(protocol())
    reg = reg.register_candidate(candidate(imp.configurations[1][1] or BASE))
    with pytest.raises(pa.AdaptationError, match="not_accepted"):
        pa.PrivateState.open(TENANT, BASE, T0).adopt(reg, "cand-1", approvals(),
                                                     ADOPT_AT)  # fmt: skip
    A_, R_ = "learning/adaptation.json", "learning/records.json"

    def adapt(fn: Callable[[dict[str, object]], None]) -> ls.Bundle:
        return edit(b, A_, lambda rows: fn(rows[1]))

    def payload(row: dict[str, object], key: str, value: str) -> None:
        row["adaptation"] = dict(row["adaptation"], **{key: value})  # type: ignore[call-overload]

    bad_files = {**b.files, "learning/records.json": b.files[R_] + b" "}
    old, files = dict(b.manifest), dict(b.files)
    cases: list[tuple[str, ls.Bundle]] = [
        ("file_hash", ls.Bundle(b.manifest, bad_files)),
        ("manifest_hash", ls.Bundle({**old, "created_at": "2020-01-01T00:00:00Z"},
                                    files)),
        ("path", rehash(old, {**files, "../escape.json": b"[]"})),
        ("executable", rehash({**old, "contains_executable_code": True}, files)),
        ("credentials", rehash({**old, "contains_credentials": True}, files)),
        ("forbidden_field", edit(b, R_, lambda r: r[0].update(api_key="x"))),
        ("forbidden_field", adapt(lambda r: r.update(adopted=True))),
        ("activation", adapt(lambda r: r.update(status="active"))),
        ("foreign_tenant", edit(b, R_, lambda r: r[0].update(tenant_id=OTHER))),
        ("forbidden_field",  # the key allowlist refuses it before T045 re-parsing
         adapt(lambda r: payload(r, "max_position_pct", "5"))),
        ("adaptation_hash",
         adapt(lambda r: payload(r, "coalesce_minutes.monitoring_alert", "30"))),
        ("float", rehash(old, {**files, R_: b"[1.5]"})),
        # Review round 1 (S4, N1, N3): only typed refusals, strict shapes.
        ("path", rehash(old, {k: v for k, v in files.items() if k != A_})),
        ("malformed", rehash(old, {**files, LIN: b'[{"x":' + b"9" * 5000 + b"}]"})),
        ("malformed", rehash(old, {**files, LIN: b"[" * 10**5 + b"]" * 10**5})),
        ("malformed", rehash(old, {**files, LIN: b"[" * 40 + b"]" * 40})),
        ("malformed", rehash(old, {**files, LIN: b"\xef\xbb\xbf[]"})),
        ("duplicate_key", rehash(old, {**files, R_: b'[{"a":"1","a":"2"}]'})),
        ("forbidden_field", adapt(lambda r: r.update({"api\u200b_key": "x"}))),
        ("foreign_tenant", edit(b, LIN, lambda r: r[0].update(
            inputs=[f"record/{OTHER}/fb-9"]))),
        ("manifest", sealed({**old, "created_at": 15}, files)),
        ("manifest", sealed({**old, "omissions": [
            {"record_class": "approval", "reason": "sk-live"}]}, files)),
        ("manifest", sealed({**old, "extra": "x"}, files)),
        ("manifest", sealed({**old, "files": [{"path": ["a"], "sha256": "x",
                                               "bytes": 1, "content_class": "x"}]},
                            files)),
        ("manifest", sealed({**old, "files": [{**e, "api_key": "sk"} for e in
                                              old["files"]]}, files)),  # type: ignore[attr-defined]
        ("adaptation_version", edit(b, A_, lambda r: r.append(dict(r[0])))),
        ("adaptation_version", adapt(lambda r: r.update(number=0))),
        ("adaptation_version", adapt(lambda r: r.update(adaptation_hash=None))),
    ]  # fmt: skip
    for code, bundle in cases:
        with pytest.raises(ls.LearnedStateError, match=rf"^{code}\b"):
            ls.import_bundle(bundle, TENANT)
    missing = {k: v for k, v in b.files.items() if k != R_}
    with pytest.raises(ls.LearnedStateError, match="path"):
        ls.import_bundle(ls.Bundle(b.manifest, missing), TENANT)
    with pytest.raises(ls.LearnedStateError, match="scope_mismatch"):
        ls.import_bundle(b, OTHER)


def test_import_refuses_oversized_files(monkeypatch: pytest.MonkeyPatch) -> None:
    b, _ = exported()
    monkeypatch.setattr(ls, "MAX_FILE_BYTES", 16)
    with pytest.raises(ls.LearnedStateError, match="oversized"):
        ls.import_bundle(b, TENANT)


def test_review_round1_export_reads_all_recorded_state_and_refuses_stale_lineage(
) -> None:  # fmt: skip
    ev, reg, state = personal_world()
    ev.delete(TENANT, "fb-1", DAY3)
    lin = ls.Lineage(ev, registries=[reg], states=[state])
    early = ls.export_bundle(ev, lin, TENANT, "export-synth-4", DAY3 - 5 * MIN,
                             state=state, registries=[reg])  # fmt: skip
    rows = {r["node"]: r for r in json.loads(early.files[LIN])}  # S1: backdated
    assert rows[n(K.CANDIDATE, "cand-1")]["invalidated"] is True
    state.erase("user-synth", DAY3 + MIN)
    for stale in (lin, ls.Lineage(ev, states=[state]),
                  ls.Lineage(fb.ImprovementEvidence(), registries=[reg],
                             states=[state])):  # fmt: skip
        with pytest.raises(ls.LearnedStateError, match="lineage_stale"):
            ls.export_bundle(ev, stale, TENANT, "export-synth-5", DAY3 + MIN,
                             state=state, registries=[reg])  # fmt: skip
    wiped = pa.PrivateState.open(TENANT, BASE, T0)  # two versions, 1 erased
    wiped.erase("user-synth", T0 + MIN)
    two = personal_world()[2]  # two versions, none wiped
    with pytest.raises(ls.LearnedStateError, match="lineage_stale"):
        ls.export_bundle(ev, ls.Lineage(ev, registries=[reg], states=[wiped]),
                         TENANT, "export-synth-8", DAY3, state=two)  # fmt: skip
    reg_o = im.ExperimentRegistry(OTHER).register(protocol(scope_id=OTHER))
    reg_o = reg_o.register_candidate(candidate(HELPFUL))
    lin = ls.Lineage(ev, registries=[reg, reg_o], states=[state])
    assert n(K.CANDIDATE, "cand-1", OTHER) in lin.items
    b = ls.export_bundle(ev, lin, TENANT, "export-synth-6", DAY3 + MIN,
                         state=state, registries=[reg])  # fmt: skip
    assert all(OTHER not in raw.decode() for raw in b.files.values())  # N7
    rows = {r["node"]: r for r in json.loads(b.files[LIN])}
    assert [rows[n(K.ADAPTATION, i)]["reproducible"] for i in "123"] == [
        False, False, True]  # fmt: skip
    cands = json.loads(b.files["learning/candidates.json"])
    assert [c["payload"] for c in cands] == [None]
    erased = {"record_class": "candidate_payload", "reason": "erased"}
    assert erased in b.manifest["omissions"]  # type: ignore[operator]
    mine = ls.export_bundle(ev, lin, TENANT, "export-synth-7", DAY3 + MIN,
                            principal_id="user-other")  # fmt: skip
    assert [r["record_id"] for r in json.loads(mine.files[REC])] == ["case-synth-1"]


def test_review_round1_guards_bind_the_registry_and_are_the_only_path() -> None:
    ev, reg, _ = personal_world()
    lin = ls.Lineage(ev, registries=[reg])
    other = im.ExperimentRegistry(TENANT).register(protocol())
    other = other.register_candidate(candidate(HELPFUL))  # same id, other content
    with pytest.raises(ls.LearnedStateError, match="lineage_registry"):
        ls.adopt(pa.PrivateState.open(TENANT, BASE, T0), other, lin, "cand-1",
                 approvals(), ADOPT_AT)  # fmt: skip
    # Unresolved gate: PrivateState.adopt/rollback are not guarded themselves, so
    # no package code outside the two modules may hold a PrivateState.
    allowed = {"personal_adaptation.py", "learned_state.py"}
    for f in [*ROOT.glob("packages/*/src/**/*.py"), *ROOT.glob("apps/**/*.py")]:
        if f.name not in allowed and "node_modules" not in f.parts:
            assert "PrivateState" not in f.read_text(), f
    assert not [k for k in ls._KEYS if any(f in k for f in ls._FORBIDDEN)]
    # A deep lineage chain is walked without recursion.
    for i in range(3000):  # chain-0 is walked first and is 3000 links deep
        node, up = n(K.CANDIDATE, f"chain-{i}"), n(K.CANDIDATE, f"chain-{i + 1}")
        up = up if i < 2999 else n(K.CANDIDATE, "cand-1")
        lin._items[node] = ls.Item(node, K.CANDIDATE, TENANT, OR.PRIVATE_TENANT,
                                   B.OWN_DATA, None, (up,))  # fmt: skip
    assert lin.statuses(ADOPT_AT)[n(K.CANDIDATE, "chain-0")].reproducible
