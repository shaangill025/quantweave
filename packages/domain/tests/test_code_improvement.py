"""Sandboxed code-improvement core (T049 increment 1). All snapshots, patches, model
outputs and sandbox results are SYNTHETIC; expected values are hand-written. The
sandbox is a fake runner implementing the T007 port (the real one is gVisor).
Scenarios exercised (SYNTHETIC, not release proof): AT030/AT099 (no self-approval,
typed approvals, never automatic), AT038 (static refusal and fake-port containment),
AT039 (tenant scope refused), AT086/AT092 (protected controls not mutable), AT076
(sandbox gate stays blocked). AT028/AT033 only up to the review hand-off.
"""

import ast
import hashlib
import hmac
import json
from dataclasses import fields, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from qw_domain import code_improvement as ci
from qw_domain import improvement as im
from qw_domain import release as rl
from qw_domain import sandbox as sb
from test_improvement import ASSESSOR, PRODUCER, card, proto
from test_trial_ledger import T0, ledger

Out = ci.Outcome
Run = sb.RunStatus
Hit = sb.LimitHit
D0 = Decimal(0)
SRC = "packages/domain/src/qw_domain/"
FIX, RISK, FREE = SRC + "returns.py", SRC + "risk.py", SRC + "benchmark.py"
TEST = "packages/x/tests/test_fm.py"
BUGGY = b"def add(a: int, b: int) -> int:\n    return a - b\n"  # the seeded defect
FIXED = b"def add(a: int, b: int) -> int:\n    return a + b\n"
TEST_SRC = b"from qw_domain.returns import add\n\n\ndef test_add() -> None:\n"
TEST_SRC += b"    assert add(2, 3) == 5\n"
WEAK_TEST = TEST_SRC.replace(b"== 5", b"== -1")
CONTENT = {FIX: BUGGY, TEST: TEST_SRC, RISK: b"MAX_LOSS = 1\n"}
AT = T0 + timedelta(minutes=5)
INSTALL = "install-synth"
ARGV = ("/usr/local/bin/python3", "-I", "-S", "-B", "/mnt/harness.py", "t.py")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


BASE = ci.Snapshot({p: sha(b) for p, b in CONTENT.items()})
LIMITS = sb.SandboxLimits(wall_millis=60_000)
ROOTFS = "f" * 64  # SYNTHETIC rootfs pin
PY = ("/usr/local/bin/python3", "3.13")
PROFILE = ci.Profile("profile-synth", BASE, ci.CodePolicy(), ARGV, LIMITS, 1, ROOTFS,
                     PY)  # fmt: skip
KEY = b"SYNTHETIC-stage-key-0123456789ab"


def signed(key: bytes = KEY, **over: Any) -> str:
    """A harness line (format of qw_adapters/sandbox_harness.py), written by hand."""
    counts = {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0,
              "xpassed": 0} | over.pop("counts", {})  # fmt: skip
    body = {"executable": PY[0], "version": "3.13.16", "implementation": "cpython",
            "nondumpable": True, "child_exit": 0, "report": "ok",
            "counts": counts} | over  # fmt: skip
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return f"qw-harness/1 {text} {hmac.new(key, text.encode(), 'sha256').hexdigest()}\n"


def mod(path: str, new: bytes) -> ci.FileChange:
    return ci.FileChange(path, new, sha(CONTENT[path]))


def add(path: str, new: bytes = b"x = 1\n") -> ci.FileChange:
    return ci.FileChange(path, new, None)


def code(*changes: ci.FileChange, touched: set[str] | None = None,
         base: ci.Snapshot = BASE) -> ci.CodeCandidate:  # fmt: skip
    paths = {c.path for c in changes} if touched is None else touched
    prov = ci.Provenance(PRODUCER, "e" * 64, AT)
    return ci.candidate(base, ci.Patch(changes), frozenset(paths), prov)


REPAIR = code(mod(FIX, FIXED))


def findings(c: ci.CodeCandidate) -> tuple[str, ...]:
    return ci.check(c, PROFILE.policy)


def stream(text: str) -> sb.StreamCapture:
    b = text.encode()
    return sb.StreamCapture(b, len(b), sha(b))


class Runner:  # SYNTHETIC SandboxRunner: replays one configured result
    def __init__(self, out: str = signed(), code: int = 0, *,
                 gate: bool = True, status: Run = Run.EXITED, hit: Hit = Hit.NONE,
                 run_id: str = "") -> None:  # fmt: skip
        self.out, self.code, self.gate, self.status = out, code, gate, status
        self.hit, self.run_id, self.requests = hit, run_id, list[sb.SandboxRequest]()

    def probe(self) -> sb.SandboxGate:
        return sb.SandboxGate(self.gate, "fake-gvisor", "" if self.gate else "no_runsc")

    def run(self, request: sb.SandboxRequest) -> sb.SandboxResult:
        self.requests.append(request)
        why = "" if self.status is Run.EXITED else "SYNTHETIC"
        return sb.SandboxResult(self.run_id or request.run_id, self.status, self.hit,
                                self.code, stream(self.out), sb.EMPTY_STREAM,
                                "fake-gvisor", why)  # fmt: skip


class Stager:  # SYNTHETIC: writes the bytes and hashes what it wrote
    def __init__(self, runner: Runner, tamper: bool = False,
                 rootfs: str = ROOTFS) -> None:  # fmt: skip
        self.runner, self.tamper, self.rootfs = runner, tamper, rootfs

    def stage(self, base: ci.Snapshot, patch: ci.Patch) -> ci.Staged:
        tree = CONTENT | {ch.path: ch.content for ch in patch.changes}
        if self.tamper:
            tree[TEST] = b"def test_add() -> None:\n    pass\n"
        written = {p: sha(b) for p, b in tree.items() if b is not None}
        return ci.Staged(ci.Snapshot(written).digest, self.rootfs, self.runner, KEY)


def evaluate(c: ci.CodeCandidate, runner: Runner | None = None,
             attempt: int = 1) -> ci.Evaluation:  # fmt: skip
    return ci.evaluate(c, PROFILE, Stager(runner or Runner()), attempt)


def test_manifest_binding_and_the_seeded_repair_passing_the_gate() -> None:
    m = REPAIR.manifest
    assert (m.base_digest, m.patch_digest) == (BASE.digest, REPAIR.patch.digest)
    assert m.touched == {FIX} and m.provenance.producer == PRODUCER
    other_base = ci.Snapshot({**BASE.files, SRC + "x.py": "a" * 64})
    variants = [
        code(mod(FIX, FIXED + b"\n")),
        code(mod(FIX, FIXED), touched={FIX, RISK}),
        code(mod(FIX, FIXED), base=other_base),
        ci.candidate(BASE, REPAIR.patch, m.touched,
                     replace(m.provenance, task_digest="f" * 64)),
        ci.candidate(BASE, REPAIR.patch, m.touched,
                     replace(m.provenance, generated_at=T0)),
    ]  # fmt: skip
    assert len({v.digest for v in variants} | {REPAIR.digest}) == 6
    assert ci.Snapshot(dict(reversed(list(BASE.files.items())))).digest == BASE.digest
    with pytest.raises(ci.CodeError, match="manifest"):  # a manifest for another base
        ci.CodeCandidate(BASE, REPAIR.patch, replace(m, base_digest="a" * 64))
    for bad in ("../etc/passwd", "/abs.py", "a//b.py", "a/./b.py", "a\\b.py", "é.py"):
        with pytest.raises(ci.CodeError, match="path"):
            add(bad)
        with pytest.raises(ci.CodeError, match="path"):
            ci.Snapshot({bad: "a" * 64})
    with pytest.raises(ci.CodeError, match="duplicate_path"):
        ci.Patch((mod(FIX, FIXED), mod(FIX, FIXED + b"\n")))
    with pytest.raises(ci.CodeError, match="change"):
        ci.FileChange(FIX, None, None)
    kinds = [mod(FIX, FIXED), add(FIX), ci.FileChange(FIX, None, sha(BUGGY))]
    assert [k.kind for k in kinds] == ["modify", "add", "delete"]
    assert findings(REPAIR) == ()  # the seeded repair passes the static gate
    assert ci.applied(BASE, REPAIR.patch).files == {**BASE.files, FIX: sha(FIXED)}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (mod(TEST, WEAK_TEST), "forbidden_path:tests:" + TEST),
        (ci.FileChange(TEST, None, sha(TEST_SRC)), "test_deletion:" + TEST),
        (add(SRC + "conftest.py"), "forbidden_path:tests:"),
        (add("conftest.py"), "forbidden_path:tests:conftest.py"),
        (mod(FIX, b"import pytest\npytest.skip('x', allow_module_level=True)\n"),
         "forbidden_import:pytest"),
        (mod(FIX, b"import os\nos._exit(0)\n"), "forbidden_import:os"),
        (mod(FIX, b"raise SystemExit(0)\n"), "forbidden_name:SystemExit"),
        (mod(FIX, b"print('1 passed')\n"), "forbidden_call:print"),
    ],
)  # fmt: skip
def test_test_gaming_is_refused(change: ci.FileChange, expected: str) -> None:
    got = findings(code(change))
    assert any(f.startswith(expected) for f in got), got
    runner = Runner()
    assert evaluate(code(change), runner).outcome is Out.REFUSED
    assert runner.requests == []


AD = "packages/adapters/src/qw_adapters/"


DENIED = {  # reason -> SYNTHETIC paths; the last one is case-folded
    "tests": ["packages/a/tests/t.py", SRC + "test_h.py", SRC + "fixtures/o.py",
              "packages/Domain/src/qw_domain/Tests/x.py"],
    "risk_thresholds": [RISK], "budget": [SRC + "budget.py"],
    "verification_rules": [SRC + "evaluator.py"], "release_gates": [SRC + "release.py"],
    "assessor": [SRC + "improvement.py", SRC + "code_improvement.py"],
    "signing_keys": [SRC + "keys/k.py", SRC + "s.pem"], "governance": ["CLAUDE.md"],
    "migrations": [AD + "sql/0099_x.sql", AD + "migrations.py"],
    "ci_config": [".github/workflows/ci.yml", ".agentic/c.json", "tools/x.py"],
    "sandbox": [SRC + "sandbox.py", AD + "gvisor_sandbox.py"],
    "dependencies": ["pyproject.toml", "uv.lock"],
}  # fmt: skip


@pytest.mark.parametrize(
    ("reason", "path"), [(r, p) for r, ps in DENIED.items() for p in ps]
)
def test_forbidden_paths_are_refused(reason: str, path: str) -> None:
    assert f"forbidden_path:{reason}:{path}" in findings(code(add(path)))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"import os\nKEY = os.environ['QW_SIGNING_KEY']\n", "forbidden_import:os"),
        (b"import socket\n", "forbidden_import:socket"),
        (b"import typing\ntyping.sys.modules['os']._exit(0)\n",
         "forbidden_import:typing"),
        (b"from typing import sys\n", "module_import:sys"),
        (b"from typing import *\n", "star_import"),
        (b"import typing as t\nt.sys.exit(0)\n", "module_attribute:sys"),
        (b"import uuid\nuuid.os.system('id')\n", "forbidden_import:uuid"),
        (b"import json\njson.codecs.open('/etc/passwd')\n", "module_attribute:codecs"),
        (b"import dataclasses\ndataclasses.inspect.os\n", "module_attribute:inspect"),
        (b"import random\nrandom._os.system('id')\n", "forbidden_import:random"),
        (b"import types\ntypes.FunctionType\n", "forbidden_import:types"),
        (b"import copy\ncopy.types.CodeType\n", "module_attribute:types"),
        (b"import functools\n", "forbidden_import:functools"),
        (b"import qw_domain.sandbox as s\nx = s._RUN_ID\n", "module_attribute:_RUN_ID"),
        (b"from qw_domain.sandbox import _RUN_ID\n", "module_import:_RUN_ID"),
        (b"from qw_domain import sandbox\nsandbox.hashlib.sha256\n",
         "module_attribute:hashlib"),
        (b"from qw_domain import sandbox as s\nx = s\nx.sb.os\n",
         "module_attribute:os"),
        (b"from _io import open as o\n", "forbidden_import:_io"),
        (b"from operator import attrgetter\n", "forbidden_import:operator"),
        (b"f = open\nf('/tmp/x', 'w')\n", "forbidden_call:open"),
        (b"__import__('os')\n", "forbidden_call:__import__"),
        (b"x = ().__class__.__subclasses__()\n", "dunder_access:__class__"),
        (b"x = __builtins__\n", "forbidden_name:__builtins__"),
        (b"x = '{0.__globals__}'.format(f)\n", "dunder_string"),
        (b"import requests\n", "unauthorized_dependency:requests"),
        (b"from qw_domain import risk\nrisk.MAX_LOSS = 9\n", "module_mutation:risk"),
        (b"import qw_domain.risk as r\nr.MAX_LOSS += 9\n", "module_mutation:r"),
        (b"setattr(object, 'x', 1)\n", "forbidden_call:setattr"),
        (b"def g():\n    yield 1\nb = g().gi_frame.f_builtins\n"
         b"imp = b[chr(95)*2+'import'+chr(95)*2]\nimp('os').system('id')\n",
         "frame_introspection:gi_frame"),
        (b"def g():\n    yield 1\nx = g().gi_frame.f_back.f_globals\n",
         "frame_introspection:f_globals"),
        (b"def f():\n    '1 passed'\nhelp(f)\n", "forbidden_call:help"),
        (b"def g():\n    yield 1\nmatch g():\n    case object(gi_frame=f):\n"
         b"        pass\n", "match_statement"),
        (b"from typing import get_type_hints\n", "name_not_allowed:get_type_hints"),
        (b"def f(x: 'chr(95)') -> None:\n    pass\n", "string_annotation"),
        (b"def f() -> 'chr(95)':\n    pass\n", "string_annotation"),
        (b"from enum import Enum\nE = Enum('E', 'A B')\n", "enum_functional_api"),
        (b"# -*- coding: utf-7 -*-\nx = 1\n", "encoding_declaration"),
        (b"def broken(:\n", "syntax_error"),
        (b"x = '\xff'\n", "not_utf8"),
    ],
)  # fmt: skip
def test_malicious_sources_are_refused(body: bytes, expected: str) -> None:
    got = findings(code(mod(FIX, body)))
    assert any(f.startswith(expected) and f.endswith(FIX) for f in got), got


API, WK = "apps/api/src/qw_api/", "apps/worker/src/qw_worker/"
PROTECTED_BY_DEFAULT = [SRC + m for m in (
    "research_data.py", "decimals.py", "instants.py", "__init__.py", "__main__.py",
    "sitecustomize.py", "routing.py", "testing.py", "pytest_plugin.py",
    "journal.py")] + [
    API + "security.py", API + "capabilities.py", API + "policies.py",
    API + "bootstrap.py", WK + "config.py", WK + "loop.py", AD + "policy_store.py",
    AD + "rights_store.py", AD + "jobs.py"]  # fmt: skip


def test_every_path_is_protected_unless_listed_as_improvable() -> None:
    for path in PROTECTED_BY_DEFAULT:
        assert f"not_improvable:{path}" in findings(code(add(path))), path
    assert all(findings(code(add(p))) == () for p in ci.IMPROVABLE[1:])
    shadow = ["improvement", "risk", "budget", "release", "trial_ledger", "evaluator",
              "scopes", "rights", "policy", "gateway", "capabilities"]  # fmt: skip
    for stem in shadow:  # a package beats the protected same-named module
        got = findings(code(add(f"{SRC}{stem}/__init__.py")))
        assert f"shadows_module:{SRC}{stem}/__init__.py" in got, got
    assert f"shadows_module:{SRC}improvement/core.py" in findings(
        code(add(SRC + "improvement/core.py")))  # fmt: skip
    pkg = ci.Snapshot({**BASE.files, FREE[:-3] + "/__init__.py": "a" * 64})
    got = ci.check(code(add(FREE), base=pkg), PROFILE.policy)
    assert got == (f"shadows_package:{FREE}",)  # a module next to a same-named package
    sub = code(add(FIX[:-3] + "/core.py"))  # a package for the real returns.py module
    assert f"shadows_module:{FIX[:-3]}/core.py" in findings(sub)


def test_declared_paths_size_bounds_and_base_consistency() -> None:
    new, gone = SRC + "a.py", ci.FileChange(FREE, None, "a" * 64)
    cases = [
        (code(mod(FIX, FIXED), touched=set()), "undeclared_path:" + FIX),
        (code(mod(FIX, FIXED), touched={FIX, new}), "declared_untouched:" + new),
        (code(mod(FIX, BUGGY)), "no_op_change:" + FIX),
        (code(ci.FileChange(FIX, FIXED, "a" * 64)), "stale_base:" + FIX),
        (code(add(FIX, FIXED)), "exists_in_base:" + FIX),
        (code(gone), "stale_base:" + FREE),
        (code(), "empty_patch"),
    ]  # fmt: skip
    for c, expected in cases:
        assert findings(c) == (expected,)
    policy = ci.CodePolicy(max_files=2, max_file_bytes=40, max_total_bytes=60)
    many = code(*(add(p) for p in ci.IMPROVABLE[1:4]))
    assert ci.check(many, policy) == ("too_many_files:3>2",)
    big = code(add(FREE, b"x = 1\n" * 7))  # 42 bytes
    assert ci.check(big, policy) == (f"file_too_large:{FREE}:42>40",)
    two = code(add(FREE, b"x = 1\n" * 6), add(ci.IMPROVABLE[2], b"y = 1\n" * 5))
    assert ci.check(two, policy) == ("patch_too_large:66>60",)


def test_policy_and_profile_cannot_weaken_the_gate() -> None:
    p = ci.CodePolicy()
    for weaker in ({"forbidden": p.forbidden[1:]},
                   {"improvable": (*p.improvable, SRC + "risk.py")},
                   {"max_total_bytes": ci.MAX_TOTAL_BYTES + 1}):  # fmt: skip
        with pytest.raises(ci.CodeError, match="policy_weakened"):
            ci.CodePolicy(**weaker)
    stricter = ci.CodePolicy(forbidden=(*p.forbidden, ("extra", "*/extra.py")))
    assert stricter.digest != p.digest
    assert replace(PROFILE, policy=stricter).digest != PROFILE.digest
    with pytest.raises(ci.CodeError, match="argv"):
        replace(PROFILE, argv=("pytest", "-q"))
    with pytest.raises(ci.CodeError, match="expected_passed"):
        replace(PROFILE, expected_passed=0)


def test_evaluation_runs_the_protected_tests_bound_to_the_candidate() -> None:
    runner = Runner()
    e = evaluate(REPAIR, runner)
    assert e.outcome is Out.PASSED and e.reasons == ()
    (req,) = runner.requests
    assert req.argv == ARGV and req.limits == LIMITS  # the profile's, not the patch's
    assert req.run_id == "code-" + REPAIR.digest[:24] + "-a1"
    assert {f.name for f in fields(req)} == {"run_id", "argv", "limits"}  # no env
    assert (e.candidate_digest, e.profile_digest) == (REPAIR.digest, PROFILE.digest)
    assert e.tree_digest == ci.Snapshot({**BASE.files, FIX: sha(FIXED)}).digest
    assert e.result_digest == runner.run(req).digest()
    assert (e.rootfs_digest, e.interpreter) == (ROOTFS, PY[0] + " 3.13.16")
    with pytest.raises(ci.CodeError, match="evaluation_inconsistent"):
        replace(e, interpreter=None)  # a pass always names its attested interpreter
    assert evaluate(REPAIR, Runner(), attempt=2).run_id == req.run_id[:-1] + "2"
    assert evaluate(REPAIR).digest == e.digest  # deterministic


@pytest.mark.parametrize(
    ("runner", "outcome", "reason"),
    [
        (Runner(gate=False), Out.BLOCKED, "sandbox_blocked:no_runsc"),
        (Runner("1 failed in 0.1s\n", 1), Out.FAILED, "tests_failed:exit=1"),
        (Runner(signed(counts={"skipped": 1})), Out.FAILED, "summary:skipped"),
        (Runner(signed(counts={"xfailed": 1})), Out.FAILED, "summary:xfailed"),
        (Runner(signed(counts={"errors": 1})), Out.FAILED, "summary:errors"),
        (Runner(signed(counts={"passed": 2})), Out.FAILED, "summary:passed=2!=1"),
        (Runner("1 passed in 0.01s\n"), Out.FAILED, "summary:signed_lines=0"),
        (Runner(signed(b"SYNTHETIC-forger-key-0123456789")), Out.FAILED,
         "summary:signed_lines=0"),
        (Runner(signed() + signed()), Out.FAILED, "summary:signed_lines=2"),
        (Runner(signed().replace('"passed":1', '"passed":9')), Out.FAILED,
         "summary:signed_lines=0"),
        (Runner(signed(report="token_mismatch")), Out.FAILED,
         "summary:report_token_mismatch"),
        (Runner(signed(child_exit=1)), Out.FAILED, "summary:child_exit=1"),
        (Runner(signed(nondumpable=False)), Out.FAILED, "harness_dumpable"),
        (Runner(signed(version="3.12.9")), Out.FAILED, "interpreter_mismatch"),
        (Runner(signed(executable="/tmp/python3")), Out.FAILED,
         "interpreter_mismatch"),
        (Runner(signed(counts={"passed": "1"})), Out.FAILED, "summary:malformed"),
        (Runner(signed(report="fine")), Out.FAILED, "summary:malformed"),
        (Runner(status=Run.LIMIT, hit=Hit.WALL_TIME), Out.FAILED,
         "limit_exceeded:wall_time"),
        (Runner(status=Run.ERROR), Out.FAILED, "runtime_error"),
        (Runner(run_id="other-run"), Out.FAILED, "result_unbound"),
    ],
)  # fmt: skip
def test_evaluation_fails_closed(runner: Runner, outcome: Out, reason: str) -> None:
    e = evaluate(REPAIR, runner)
    assert (e.outcome, e.reasons) == (outcome, (reason,))
    assert ci.metrics(e)["tests_passed"] == D0


def test_tampered_image_and_contained_candidates_never_pass() -> None:
    runner = Runner()
    e = ci.evaluate(REPAIR, PROFILE, Stager(runner, tamper=True))
    assert e.outcome is Out.BLOCKED and e.reasons == ("image_mismatch",)
    assert runner.requests == []  # a tree other than base + patch never runs
    e = ci.evaluate(REPAIR, PROFILE, Stager(runner, rootfs="a" * 64))
    assert (e.outcome, e.reasons, e.rootfs_digest) == (
        Out.BLOCKED, ("rootfs_mismatch",), "a" * 64)  # fmt: skip
    assert runner.requests == []  # nothing runs on an unpinned rootfs
    for bad in (dict(rootfs_digest="x"), dict(interpreter=("python3", "3.13")),
                dict(interpreter=(PY[0], "3"))):  # fmt: skip
        with pytest.raises(ci.CodeError):
            replace(PROFILE, **bad)
    with pytest.raises(ci.CodeError, match="channel_key"):
        ci.Staged("a" * 64, ROOTFS, runner, b"short")
    assert replace(PROFILE, rootfs_digest="a" * 64).digest != PROFILE.digest
    spin = code(mod(FIX, b"while True:\n    pass\n"))  # statically clean; contained
    assert findings(spin) == ()
    hit = Runner(status=Run.LIMIT, hit=Hit.CPU_TIME)
    assert evaluate(spin, hit).outcome is Out.FAILED
    with pytest.raises(ci.CodeError, match="base_mismatch"):
        evaluate(code(mod(FIX, FIXED), base=ci.Snapshot({FIX: sha(BUGGY)})))


def test_metrics_count_hard_controls_per_file() -> None:
    hard = ("secret_access_attempts", "host_writes", "unauthorized_dependencies",
            "protected_control_mutations")  # fmt: skip
    assert set(hard) == set(im.HARD_CONTROLS[im.Target.APPLICATION_CODE])
    clean = ci.metrics(evaluate(REPAIR))
    assert clean == {**dict.fromkeys(hard, D0), "tests_passed": Decimal(1)}
    keys = add(SRC + "keys/k.py", b"import requests\nimport os\nimport socket\n")
    bad = code(mod(TEST, WEAK_TEST), add("uv.lock", b"x"), keys)
    got = ci.metrics(evaluate(bad))
    want = [1, 1, 2, 1]  # keys/k.py; keys/k.py; uv.lock + keys/k.py; the test file
    assert got == {**{h: Decimal(n) for h, n in zip(hard, want, strict=True)},
                   "tests_passed": D0}  # fmt: skip


def code_proto(**kw: object) -> im.Protocol:
    args: dict[str, object] = {
        "target": im.Target.APPLICATION_CODE, "scope": im.Scope.INSTALLATION,
        "scope_id": INSTALL, "baseline_version": "app-v0", "baseline_hash": BASE.digest,
        "scorecard": card(im.Target.APPLICATION_CODE),
        "sandbox_profile_id": "profile-synth", "mutation_space_hash": PROFILE.digest,
    } | kw  # fmt: skip
    return proto(**args)


def test_protocol_binds_profile_and_installation_scope() -> None:
    ci.check_protocol(code_proto(), PROFILE)
    opt = im.Target.RECURSIVE_OPTIMIZER
    bad = [
        (code_proto(scope=im.Scope.TENANT), "tenant_scope"),
        (code_proto(baseline_hash="a" * 64), "baseline_mismatch"),
        (code_proto(mutation_space_hash="a" * 64), "profile_mismatch"),
        (code_proto(sandbox_profile_id="other"), "profile_mismatch"),
        (code_proto(target=opt, scorecard=card(opt)), "target"),
    ]
    for p, code_ in bad:
        with pytest.raises(ci.CodeError, match=code_):
            ci.check_protocol(p, PROFILE)


def registered() -> tuple[im.ExperimentRegistry, im.Candidate]:
    c = ci.to_candidate(REPAIR, "code-cand-1", code_proto().experiment_id, "app-v0", AT)
    reg = im.ExperimentRegistry(INSTALL).register(code_proto()).register_candidate(c)
    return reg, c


def routed(e: ci.Evaluation, c: im.Candidate) -> ci.ReviewRoute:
    b = ci.package(code_proto(), PROFILE, REPAIR, e, c)
    return ci.route_for_review(b, code_proto())


def test_handoff_routes_to_human_review_and_cannot_promote() -> None:
    reg, c = registered()
    assert (c.artifact_hash, c.producer) == (REPAIR.digest, PRODUCER)
    assert dict(c.payload) == {"base_digest": BASE.digest,
                               "patch_digest": REPAIR.patch.digest}  # fmt: skip
    e = evaluate(REPAIR)
    b = ci.package(code_proto(), PROFILE, REPAIR, e, c)
    assert b.eligible and b.protocol_hash == code_proto().content_hash
    dirty = replace(b, metrics={**b.metrics, "host_writes": Decimal(1)})
    assert not dirty.eligible and dirty.digest != b.digest  # hard controls hold too
    assert (b.candidate_hash, b.evaluation_digest) == (c.content_hash, e.digest)
    route = ci.route_for_review(b, code_proto())
    assert route.state == "awaiting_human_review" and route.automatic is False
    assert route.required_approvals == {im.ApprovalClass.MAINTAINER_RELEASE,
                                        im.ApprovalClass.OPERATOR_INSTALL}  # fmt: skip
    assert route.blocked_by == ("sandbox_unqualified",)  # T007 not qualified
    names = {f.name for f in fields(route)}
    assert names == {"bundle_digest", "state", "required_approvals", "blocked_by",
                     "limitations"}  # fmt: skip
    assert "in_process_test_summary" in route.limitations  # a pass is not verified
    book: Any = None  # never read: code targets are blocked before the budget check
    v = im.assess(reg, c.experiment_id, c.candidate_id, ledger(), book, ASSESSOR,
                  AT + timedelta(days=1), "verdict-1")  # fmt: skip
    assert v.decision is im.Decision.BLOCKED and v.reasons == ("sandbox_unqualified",)
    ok = rl.MaintainerApproval("maintainer-synth", "stepup-synth", AT + timedelta(2))
    with pytest.raises(rl.ReleaseError, match="sandbox_unqualified"):
        rl.prepare_release(reg, c.candidate_id, "rel-1", rl.Compatibility(1, 1, 1), ok)
    assert routed(evaluate(REPAIR, Runner("1 failed\n", 1)), c).state == "rejected"
    blocked = evaluate(REPAIR, Runner(gate=False))
    assert routed(blocked, c).state == "insufficient_evidence"
    with pytest.raises(ci.CodeError, match="protocol_mismatch"):
        ci.route_for_review(b, code_proto(hypothesis="SYNTHETIC: another"))


def test_package_refuses_unbound_evidence() -> None:
    _, c = registered()
    other = code(mod(FIX, b"def add(a: int, b: int) -> int:\n    return b + a\n"))
    with pytest.raises(ci.CodeError, match="evaluation_unbound"):
        ci.package(code_proto(), PROFILE, REPAIR, evaluate(other), c)
    failed = evaluate(REPAIR, Runner("1 failed\n", 1))
    with pytest.raises(ci.CodeError, match="evaluation_inconsistent"):  # a forged pass
        replace(failed, outcome=Out.PASSED, reasons=())
    with pytest.raises(ci.CodeError, match="candidate_unbound"):
        ci.package(code_proto(), PROFILE, REPAIR, evaluate(REPAIR),
                   replace(c, artifact_hash="a" * 64))  # fmt: skip
    ok = evaluate(REPAIR)
    for forged in (replace(ok, rootfs_digest="a" * 64),
                   replace(ok, interpreter="/x/py 3.13.1"),
                   replace(ok, interpreter=PY[0] + " 3.12.9")):  # fmt: skip
        with pytest.raises(ci.CodeError, match="evaluation_unbound"):  # not the pin
            ci.package(code_proto(), PROFILE, REPAIR, forged, c)


def test_truncated_output_conflicting_paths_and_key_repr() -> None:
    one = signed().encode()
    cut = sb.StreamCapture(one, len(one) + 1, sha(one + b"x"))  # a valid line, cut
    assert ci.harness_report(cut, KEY, PROFILE) == (["summary:truncated"], None)
    assert ci.harness_report(stream(signed()), KEY, PROFILE) == ([], PY[0] + " 3.13.16")
    with pytest.raises(ci.CodeError, match="path_conflict"):
        ci.Snapshot({"a/b.py": "a" * 64, "a": "b" * 64})
    assert "path_conflict" in findings(code(add(SRC[:-1])))  # a file over a package
    staged = ci.Staged("a" * 64, ROOTFS, Runner(), KEY)
    assert KEY.hex() not in repr(staged) and repr(KEY) not in repr(staged)


class SyntheticGenerator:  # deterministic SYNTHETIC double; no model call
    def __init__(self, touched: frozenset[str] = frozenset({FIX})) -> None:
        self.touched = touched

    def propose(self, base: ci.Snapshot, task: str) -> ci.Proposal:
        assert base.files[FIX] == sha(BUGGY) and "add" in task
        return ci.Proposal((mod(FIX, FIXED),), self.touched, PRODUCER)


def test_generator_port_is_deterministic_and_its_output_is_untrusted() -> None:
    task = "SYNTHETIC: repair add() so test_add passes"
    a = ci.generate(SyntheticGenerator(), BASE, task, AT)
    assert a.digest == ci.generate(SyntheticGenerator(), BASE, task, AT).digest
    assert a.manifest.provenance.task_digest == sha(task.encode())
    assert a.patch == REPAIR.patch and findings(a) == ()
    lying = ci.generate(SyntheticGenerator(frozenset()), BASE, task, AT)
    assert findings(lying) == ("undeclared_path:" + FIX,)


def test_module_has_no_promotion_or_approval_path() -> None:
    tree = ast.parse(Path(ci.__file__).read_text())
    names = {n.name.lower() for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef | ast.ClassDef)}  # fmt: skip
    for verb in ("promote", "approve", "install", "adopt", "sign", "grant"):
        assert not any(verb in n for n in names), verb
