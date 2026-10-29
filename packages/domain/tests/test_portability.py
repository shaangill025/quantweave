"""Export manifest, structural section allowlist, secret scan, factories,
licensed-evidence binding and import verification (T057 increment 1). Every tenant,
record, feed and evidence item is SYNTHETIC; no real export (T015) is used. Expected
hashes, codes and reasons are written by hand from the module docstring rules; the
hostile cases are the review round 1 and 2 probes.
"""

import base64
import copy
import dataclasses
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ingest_rights_helper import REGION, TENANT, qualified_registry
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain import portability as pt
from qw_domain.rights import Registry, Use, UseScope, check_use
from referencing import Registry as RefRegistry
from referencing import Resource
from test_learned_state import exported

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import project_contract  # noqa: E402  # type: ignore[import-not-found]

R = pt.Refusal
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
V1, EV, LIN = "qw-export-1.0", pt.EVIDENCE_PATH, "learning/lineage.json"
SCHEMAS = ROOT / "docs/spec/contracts/schemas"
KW: dict[str, Any] = {"scope": UseScope.PERSONAL, "jurisdiction": REGION}
ITEMS = [pt.LicensedItem("evd-synth-1", "feed-synth-ok"),
         pt.LicensedItem("evd-synth-2", "feed-synth-unknown")]  # fmt: skip
ROW: dict[str, object] = {
    "node": f"record/{TENANT}/r1", "kind": "record", "scope_id": TENANT,
    "origin": "private_tenant", "basis": "own_data", "purpose": None, "inputs": [],
    "reproducible": True, "invalidated": False, "reasons": [], "action": "none",
}  # fmt: skip


def ev(eid: str = "evd-synth-1", **kw: object) -> dict[str, object]:
    return {"id": eid, "source_id": "src-synth", "locator": "synthetic://filing/1",
            "passage": "SYNTHETIC passage", "content_hash": "a" * 64,
            "published_at": None, "retrieved_at": "2026-10-28T00:00:00Z",
            "rights_profile_id": "feed-synth-ok", "availability": "available",
            "source_relationship_group": "grp-synth", "retention_class": "evidence",
            "instrument_ids": ["inst-synth"],
            "coverage": {"status": "complete", "reasons": []}, **kw}  # fmt: skip


def decision(at: datetime = AT, reg: Registry | None = None) -> pt.LicensedDecision:
    reg = reg or qualified_registry(("feed-synth-ok",))
    return pt.licensed_evidence(reg, TENANT, ITEMS, at, **KW)


def build(sections: dict[str, object], **kw: Any) -> pt.ExportBundle:
    return pt.build_export("export-synth-1", TENANT, AT, sections, **kw)


def refused(fn: object, *args: object, **kw: object) -> pt.Refusal:
    with pytest.raises(pt.PortabilityError) as e:
        fn(*args, **kw)  # type: ignore[operator]
    return e.value.code


def rehash(m: dict[str, object]) -> bytes:
    """Re-hash an edited manifest as a hostile writer would."""
    body = {k: v for k, v in m.items() if k != "content_hash"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return json.dumps({**body, "content_hash": digest}).encode()


def one_file(path: str, raw: bytes, fmt: str = V1, cls: str = "metadata",
             omit: tuple[pt.Omission, ...] = ()) -> bytes:  # fmt: skip
    entry = pt.FileEntry(path, hashlib.sha256(raw).hexdigest(), len(raw), cls)
    m = pt.ExportManifest("export-synth-2", fmt, AT, TENANT, (entry,), omit, (), V1)
    return pt.canonical(m.to_wire())


def test_manifest_is_the_contract_shape_and_content_addressed() -> None:
    b = build({LIN: [ROW]}, reconnections=["conn-synth-1"])
    wire = b.manifest.to_wire()
    common = json.loads((SCHEMAS / "common.schema.json").read_text())
    schema = json.loads((SCHEMAS / "export_manifest.schema.json").read_text())
    refs = RefRegistry().with_resource("common.schema.json",
                                       Resource.from_contents(common))  # fmt: skip
    Draft202012Validator(schema, registry=refs).validate(wire)
    assert set(wire) == set(schema["properties"])  # no edition, brand or domain field
    raw = b.files[LIN]
    assert raw == pt.canonical([ROW]) and wire["files"] == [{
        "path": LIN, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "content_class": "metadata"}]  # fmt: skip
    assert wire["omissions"] == [
        {"record_class": "credential", "reason": "not_portable"},
        {"record_class": "approval", "reason": "authority_not_portable"}]  # fmt: skip
    assert wire["contains_credentials"] is wire["contains_executable_code"] is False
    assert (wire["format_version"], wire["created_at"]) == (V1, "2026-10-29T12:00:00Z")
    body = {k: v for k, v in wire.items() if k != "content_hash"}
    digest = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert wire["content_hash"] == hashlib.sha256(digest).hexdigest()


def test_round_trip_of_open_sections_is_inert() -> None:
    learned, _ = exported()  # the T052 learned-state files, as T052 writes them
    sections = {p: json.loads(raw) for p, raw in learned.files.items()} | {EV: [ev()]}
    b = build(sections, reconnections=["conn-synth-1"], licensed=decision())
    rep = pt.verify_import(pt.canonical(b.manifest.to_wire()), b.files)
    assert rep.data == sections and rep.unsupported == ()
    assert rep.pending_reconnections == ("conn-synth-1",)
    gone = pt.Omission("licensed_evidence", "rights:not_qualified:evd-synth-2")
    assert rep.omissions == (*pt.ALWAYS_OMITTED, gone)
    Draft202012Validator(project_contract.schema("evidence")).validate(ev())


@pytest.mark.parametrize("path", sorted(set(pt.SECTIONS) - pt.OPEN_SECTIONS))
def test_sections_without_a_structure_are_closed(path: str) -> None:
    assert refused(build, {path: []}) is R.SECTION_NOT_READY
    cls = pt.SECTIONS[path]
    assert refused(pt.verify_import, one_file(path, b"[]", cls=cls), {path: b"[]"}) is (
        R.SECTION_NOT_READY)  # fmt: skip
    assert refused(build, {"secrets/vault.json": []}) is R.SECTION_UNKNOWN


REC = {"record_kind": "error", "tenant_id": TENANT, "record_id": "r1",
       "subject": {"kind": "proposal", "object_id": "p1", "version": 1,
                   "content_hash": "a" * 64}, "occurred_at": "2026-10-28T00:00:00Z",
       "recorded_at": "2026-10-28T00:00:00Z", "error_kind": "validation",
       "code": "input_tokens", "detail": "authorization_ref"}  # fmt: skip


@pytest.mark.parametrize(("path", "body", "expect"), [
    (LIN, [{**ROW, "password": "x"}], R.UNKNOWN_FIELD),
    (LIN, [{**ROW, "inputs": [{"api_key": "x"}]}], R.UNKNOWN_FIELD),  # under a leaf
    (LIN, [{**ROW, "Node": "x"}], R.UNKNOWN_FIELD),
    ("learning/records.json", [{**REC, "subject": {**REC["subject"], "token": "x"}}],  # type: ignore[dict-item]
     R.UNKNOWN_FIELD),
    ("learning/records.json", [{**REC, "record_kind": "credential"}], R.UNKNOWN_FIELD),
    ("learning/records.json", [{**REC, "inputs": {}}], R.UNKNOWN_FIELD),  # not a case
    ("learning/consents.json", [{"grant": {"secret": "x"}, "revoked_at": None}],
     R.UNKNOWN_FIELD),
    ("learning/adaptation.json", [{"adaptation": {"risk_limit": "1"}}],
     R.UNKNOWN_FIELD),
    ("learning/candidates.json", [{"verdicts": [{"approval": "x"}]}], R.UNKNOWN_FIELD),
    (EV, [ev(body="licensed text")], R.UNKNOWN_FIELD),
    (EV, [{k: v for k, v in ev().items() if k != "coverage"}], R.MALFORMED),
    (EV, [ev(id="bad id")], R.MALFORMED),
    (EV, [ev(retrieved_at="yesterday")], R.MALFORMED),
    (EV, [ev(availability="public")], R.MALFORMED),
    (EV, [ev(coverage={"status": "complete"})], R.MALFORMED),
    (EV, [ev(coverage={"status": "done", "reasons": []})], R.MALFORMED),
    (EV, [ev(), ev()], R.MALFORMED), (EV, ev(), R.MALFORMED),
    (LIN, {"node": "x"}, R.MALFORMED), (LIN, [{**ROW, "purpose": 1.5}], R.FLOAT),
])  # fmt: skip
def test_structural_allowlist(path: str, body: object, expect: pt.Refusal) -> None:
    assert refused(build, {path: body}, licensed=decision()) is expect


def test_record_vocabulary_and_prose_pass() -> None:
    prose = ["Basic information about the account", "code review notes",
             "evd-synth-20 and evd-synth-1", "Bearer documentation-collection",
             "Basic 1234567890123456", "sk-short", "x-evd-synth-2 evd-synth-2-copy",
             "ssh-rsa keys are rotated monthly"]  # fmt: skip
    build({"learning/records.json": [REC], LIN: [{**ROW, "reasons": prose}]},
          licensed=decision())  # fmt: skip


def _b64(prefix: bytes) -> str:
    return base64.b64encode(prefix + b"-----BEGIN PRIVATE KEY-----").decode()


@pytest.mark.parametrize("value", [
    "-----BEGIN PRIVATE KEY-----", "-----begin rsa private key-----", _b64(b""),
    _b64(b"x"), _b64(b"xy"), "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",
    "ssh-rsa AAAAB3NzaC1yc2E", "ecdsa-sha2-nistp256 AAAAE2Vj", "AKIAIOSFODNN7EXAMPLE",
    "ghp_" + "a1" * 18, "gho_" + "b2" * 18, "xoxb-123456789012-abc",
    "sk_live_4eC39HqLyjWDarjtT1", "rk_live_4eC39HqLyjWDarjtT1", "sk-ant-api03-abcdefgh",
    "https://user:hunter2@db.example/x", "Authorization: Basic dXNlcjpwYXNzd29yZDE=",
    "Bearer abcdefgh12345678xyz", "sk-" + "a" * 40, "sk-proj-" + "b" * 40,
    "-----\nBEGIN", "AIza" + "c" * 35, "npm_" + "d" * 36, "https://hooks.slack.com/services/T0/B0/xyz",
    "ssh-rsa\tAAAAB3",
])  # fmt: skip
def test_high_confidence_secret_values(value: str) -> None:
    assert refused(build, {LIN: [{**ROW, "reasons": [value]}]}) is R.SECRET_VALUE


def test_only_factories_make_bundles_and_decisions() -> None:
    good, d = build({LIN: [ROW]}), decision()
    manifest = good.manifest
    for make in (lambda: pt.ExportBundle(manifest, good.files),
                 lambda: dataclasses.replace(good, files={LIN: b"[]"}),
                 lambda: copy.replace(good, manifest=manifest),
                 lambda: pt.LicensedDecision(),
                 lambda: dataclasses.replace(d, scope_id="x")):  # fmt: skip
        assert refused(make) is R.FORMAT_NOT_WRITABLE
    for field, value in (("permitted", frozenset({"evd-synth-2"})), ("scope_id", "x"),
                         ("denied", frozenset()), ("revisions", ())):  # fmt: skip
        forged = copy.copy(d)
        object.__setattr__(forged, field, value)  # beyond the API: detected by hash
        assert refused(build, {EV: [ev("evd-synth-2")]}, licensed=forged) is R.DECISION


def test_decision_binds_scope_and_time() -> None:
    late, ok = AT - pt.DECISION_MAX_AGE - timedelta(seconds=1), AT - pt.DECISION_MAX_AGE
    build({EV: [ev()]}, licensed=decision(ok))
    for d in (decision(AT + timedelta(seconds=1)), decision(late)):
        assert refused(build, {EV: [ev()]}, licensed=d) is R.DECISION
    other = pt.licensed_evidence(qualified_registry(("feed-synth-ok",)), "tenant-other",
                                 ITEMS, AT, **KW)  # fmt: skip
    assert refused(build, {EV: [ev()]}, licensed=other) is R.DECISION


def test_licensed_evidence_is_bound_to_every_section() -> None:
    d = decision()
    assert d.permitted == frozenset({"evd-synth-1"})
    assert d.denied == frozenset({"evd-synth-2"})
    use = check_use(qualified_registry(("feed-synth-ok",)), TENANT, "feed-synth-ok",
                    Use.USER_EXPORT, AT, **KW)  # fmt: skip
    assert d.revisions == (("evd-synth-1", use.feed_hash, use.entitlement_hash),
                           ("evd-synth-2", None, None))  # fmt: skip
    empty = object.__new__(pt.LicensedDecision)
    assert refused(build, {}, licensed=empty) is R.DECISION
    assert refused(build, {EV: [ev()]}) is R.EVIDENCE_UNDECIDED  # no decision
    assert refused(build, {EV: [ev("evd-synth-9")]}, licensed=d) is R.EVIDENCE_UNDECIDED
    assert refused(build, {EV: [ev(rights_profile_id="feed-synth-unknown")]},
                   licensed=d) is R.EVIDENCE_FEED_MISMATCH  # fmt: skip
    for text in ("copied text of evd-synth-2.", "ref:evd-synth-2", "evd-synth-2:1",
                 "evidence:evd-synth-2", "evd-synth-2.json",
                 "(evd-synth-2)"):  # fmt: skip
        assert refused(build, {LIN: [{**ROW, "reasons": [text]}]}, licensed=d) is (
            R.EVIDENCE_OMITTED)  # fmt: skip
    for sections in ({EV: [ev("evd-synth-2")]}, {EV: [ev(passage="see evd-synth-2")]}):
        assert refused(build, sections, licensed=d) is R.EVIDENCE_OMITTED
    short = pt.licensed_evidence(Registry(), TENANT, [pt.LicensedItem("a", "f1"),
                                 pt.LicensedItem("ev-2", "f1")], AT, **KW)  # fmt: skip
    build({LIN: [{**ROW, "reasons": ["a thing", "lev-22", f"record/{TENANT}/a"]}]},
          licensed=short)  # fmt: skip
    for text in ("a", "ref:ev-2"):  # a short word id matches only an equal value
        assert refused(build, {LIN: [{**ROW, "reasons": [text]}]}, licensed=short) is (
            R.EVIDENCE_OMITTED)  # fmt: skip
    assert refused(build, {}, licensed=d, omissions=d.omissions) is R.OMISSION
    raw = pt.canonical([{**ROW, "reasons": ["evd-synth-2"]}])
    manifest = one_file(LIN, raw, omit=d.omissions)
    assert refused(pt.verify_import, manifest, {LIN: raw}) is R.EVIDENCE_OMITTED


def test_every_deny_code_is_recorded() -> None:
    reg = qualified_registry(("feed-synth-ok",), missing=Use.USER_EXPORT)
    d = pt.licensed_evidence(reg, TENANT, ITEMS[:1], AT, **KW)
    assert d.omissions[0].reason == "rights:right_unknown:evd-synth-1"
    reg = qualified_registry(("feed-synth-ok",), tenant="tenant-other")
    d = pt.licensed_evidence(reg, TENANT, ITEMS[:1], AT, scope=UseScope.PERSONAL,
                             jurisdiction="US-NY")  # fmt: skip
    assert d.omissions[0].reason == "rights:not_entitled.right_unknown:evd-synth-1"
    assert d.omissions[0].evidence_id == "evd-synth-1"
    assert refused(pt.licensed_evidence, Registry(), TENANT, [ITEMS[0]] * 2, AT,
                   **KW) is R.OMISSION  # fmt: skip
    assert refused(pt.Omission, "licensed_evidence", "free text") is R.OMISSION


@pytest.mark.parametrize(("fmt", "need", "expect"), [
    (V1, V1, pt.Compatibility((1, 0), False)),
    ("qw-export-1.4", V1, pt.Compatibility((1, 4), True)),
    ("qw-export-2.0", V1, R.MAJOR_UNSUPPORTED),
    ("qw-export-1.2", "qw-export-1.1", R.READER_TOO_OLD),
    (V1, "qw-export-1.1", R.COMPATIBILITY_INCONSISTENT),
    ("qw-learned-state-1", "qw-learned-state-1", R.FORMAT_UNKNOWN),
    ("qw-export-01.0", V1, R.FORMAT_UNKNOWN), ("qw-export-1.0\n", V1, R.FORMAT_UNKNOWN),
    ("qw-export-\uff11.0", V1, R.FORMAT_UNKNOWN),
])  # fmt: skip
def test_compatibility(fmt: str, need: str, expect: object) -> None:
    if isinstance(expect, pt.Refusal):
        assert refused(pt.check_compatibility, fmt, need) is expect
    else:
        assert pt.check_compatibility(fmt, need) == expect


def test_unknown_sections_at_a_newer_minor_are_checked_not_loaded() -> None:
    imp, v11, raw = pt.verify_import, "qw-export-1.1", b'{"x":"1"}'
    for path in ("research/future.json", "future/blob.bin"):
        assert refused(imp, one_file(path, raw), {path: raw}) is R.SECTION_UNKNOWN
        rep = imp(one_file(path, raw, v11), {path: raw})
        assert rep.unsupported == (path,) and rep.data == {}
        assert rep.compatibility.downgraded
        assert refused(imp, one_file(path, raw + b" ", v11), {path: raw}) is (
            R.SIZE_MISMATCH)  # fmt: skip
    blob = b"\x00\x01-----BEGIN"
    assert imp(one_file("f/b.bin", blob, v11), {"f/b.bin": blob}).unsupported
    pem, ref, gone = b'["-----BEGIN KEY"]', b'["evd-synth-2"]', decision().omissions
    assert refused(imp, one_file("f/x.json", pem, v11), {"f/x.json": pem}) is (
        R.SECRET_VALUE)  # fmt: skip
    man = one_file("f/x.json", ref, v11, omit=gone)
    assert refused(imp, man, {"f/x.json": ref}) is R.EVIDENCE_OMITTED


def test_import_refuses_tampering_with_typed_codes() -> None:
    b = build({LIN: [ROW]})
    wire = b.manifest.to_wire()
    raw, ok, j = pt.canonical(wire), dict(b.files), b.files[LIN]
    imp = pt.verify_import
    assert refused(imp, raw, {**ok, "research/extra.json": b"[]"}) is R.ENTRY_UNLISTED
    assert refused(imp, raw, {}) is R.ENTRY_MISSING
    assert refused(imp, raw, {LIN: j.replace(b"r1", b"r2")}) is R.HASH_MISMATCH
    assert refused(imp, raw, {LIN: j + b" "}) is R.SIZE_MISMATCH
    assert refused(imp, raw, {LIN: "text"}) is R.SIZE_MISMATCH
    assert refused(imp, pt.canonical({**wire, "omissions": []}), ok) is R.MANIFEST_HASH
    for key, code in (("contains_credentials", R.CREDENTIALS),
                      ("contains_executable_code", R.EXECUTABLE)):  # fmt: skip
        assert refused(imp, rehash({**wire, key: True}), ok) is code
    utc = "2026-10-29T12:00:00+00:00"  # same instant, not canonical
    assert refused(imp, rehash({**wire, "created_at": utc}), ok) is R.MANIFEST
    assert refused(imp, rehash({**wire, "edition": "hosted"}), ok) is R.MANIFEST
    files = [dict(f) for f in wire["files"]]  # type: ignore[attr-defined]
    files[0]["content_class"] = "financial"
    assert refused(imp, rehash({**wire, "files": files}), ok) is R.CLASS_MISMATCH
    files[0] = {**files[0], "content_class": "metadata", "path": "../lineage.json"}
    assert refused(imp, rehash({**wire, "files": files}), ok) is R.PATH_TRAVERSAL
    dup = b'[{"node":"1","node":"2"}]'
    assert refused(imp, one_file(LIN, dup), {LIN: dup}) is R.DUPLICATE_KEY
    assert refused(imp, b'{"id": 1.5}', ok) is R.FLOAT
    assert refused(imp, b"\xef\xbb\xbf{}", ok) is R.MALFORMED


def test_lone_surrogates_are_typed_refusals() -> None:
    wire = build({LIN: [ROW]}).manifest.to_wire()
    raw = json.dumps(wire).replace(TENANT, "\\ud800").encode()
    assert refused(pt.verify_import, raw, {}) is R.MALFORMED
    assert refused(pt.ExportManifest.from_wire, {**wire, "scope_id": "\ud800"}) is (
        R.MALFORMED)  # fmt: skip
    assert refused(pt.canonical, {"a": "\udfff"}) is R.MALFORMED
    body = b'[{"node":"\\ud800"}]'
    assert refused(pt.verify_import, one_file(LIN, body), {LIN: body}) is R.MALFORMED


@pytest.mark.parametrize(("path", "expect"), [
    ("../etc/passwd", R.PATH_TRAVERSAL), ("financial/../../x.json", R.PATH_TRAVERSAL),
    ("financial/./x.json", R.PATH_TRAVERSAL), ("/etc/passwd", R.PATH_ABSOLUTE),
    ("C:/Windows/x.json", R.PATH_DRIVE), ("c:x.json", R.PATH_DRIVE),
    ("financial\\x.json", R.PATH_BACKSLASH), ("a.json\x00.sh", R.PATH_CONTROL),
    ("a\nb.json", R.PATH_CONTROL), ("financial//x.json", R.PATH),
    ("financial/", R.PATH), ("", R.PATH), (".hidden", R.PATH),
    ("caf\u00e9.json", R.PATH), ("a" * 256, R.PATH),
])  # fmt: skip
def test_path_rules(path: str, expect: pt.Refusal) -> None:
    assert refused(pt.check_path, path) is expect


def test_paths_and_manifest_fields() -> None:
    assert pt.check_path("a" * 255) == "a" * 255
    assert refused(pt.unique_paths, ["policy/p.json", "Policy/P.json"]) is (
        R.CASE_COLLISION)  # fmt: skip
    assert refused(pt.unique_paths, ["a.json", "a.json"]) is R.DUPLICATE_PATH
    entry = pt.FileEntry("manifest.json", "0" * 64, 0, "metadata")
    assert refused(pt.ExportManifest, "x1", V1, AT, TENANT, (entry,), (), (), V1) is (
        R.DUPLICATE_PATH)  # fmt: skip
    naive = datetime(2026, 10, 29)  # noqa: DTZ001
    assert refused(pt.ExportManifest, "x1", V1, naive, TENANT, (), (), (), V1) is (
        R.MANIFEST)  # fmt: skip
    assert refused(pt.FileEntry, "a.json", "0" * 64, True, "financial") is R.MANIFEST
    assert refused(pt.FileEntry, "a.json", "0" * 64, 1, "credentials") is R.MANIFEST
    assert refused(build, {}, reconnections=["conn-1", "conn-1"]) is R.MANIFEST
    assert refused(build, {}, reconnections=["../conn"]) is R.MANIFEST
    deep: object = "leaf"
    for _ in range(pt.MAX_DEPTH):
        deep = [deep]
    pt.scan(deep)
    assert refused(pt.scan, [deep]) is R.TOO_DEEP
