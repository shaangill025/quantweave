"""Sandboxed code improvement (T049 increment 1; spec §11, §12; R028, R030, R033, R038,
R039, R076, R086, R092, R099). Domain only, stdlib only. A whole-file `Patch` on a
content-addressed `Snapshot` is bound by a `Manifest`; `check` is a name-list static
gate (protected by default, then denylist and Python rules), bypassable defence in
depth; `evaluate` runs the protected tests via the T007 port and binds every digest;
`package`/`route_for_review` hand evidence to human review, granting nothing.
Increment 2: the stager also attests the rootfs tree digest (checked against the
profile's pin before anything runs), and the in-sandbox harness reports the
interpreter and the counts in one line signed with a per-stage key the candidate's
process never holds. LIMITATIONS: the counts still come from the candidate's process,
so a pass is unverified (`in_process_test_summary`); no persistence, so a stored
`Evaluation` must be `evaluate`'s own output.
"""

import ast
import fnmatch
import hashlib
import hmac
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol as Port

from qw_domain import improvement as im
from qw_domain import sandbox as sb
from qw_domain.decimals import safe_repr
from qw_domain.instants import ensure_aware_utc, format_instant
from qw_domain.research_data import digest

MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_PATH = 20, 64 * 1024, 256 * 1024, 200
_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=]", re.MULTILINE)
HARNESS = "qw-harness/1"  # the signed line of qw_adapters/sandbox_harness.py
COUNTS = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")
_REPORTS = frozenset({"ok", "missing", "multiple", "malformed", "token_mismatch"})
_VERSION = re.compile(r"[0-9]+\.[0-9]+", re.ASCII)
CODE = im.Target.APPLICATION_CODE


class CodeError(im.ImprovementError):
    pass


def _path(p: object) -> str:
    ok = isinstance(p, str) and 0 < len(p) <= MAX_PATH
    parts = str(p).split("/")
    if not ok or not all(_SEGMENT.fullmatch(s) and s not in (".", "..") for s in parts):
        raise CodeError("path", safe_repr(p))
    return str(p)


def _sha(v: object, what: str) -> str:
    if not isinstance(v, str) or not _SHA.fullmatch(v):
        raise CodeError(what, f"{safe_repr(v)} is not a sha256 hex digest")
    return v


@dataclass(frozen=True, slots=True)
class Snapshot:
    files: Mapping[str, str]

    def __post_init__(self) -> None:
        tree = {_path(p): _sha(s, "file_digest") for p, s in dict(self.files).items()}
        dirs = {p.rsplit("/", k)[0] for p in tree for k in range(1, p.count("/") + 1)}
        if clash := sorted(dirs & set(tree)):  # a file where another needs a directory
            raise CodeError("path_conflict", clash[0])
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(tree.items()))))

    @property
    def digest(self) -> str:
        return digest(["qw.code.snapshot/1", sorted(self.files.items())])


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    content: bytes | None  # the whole new file; None deletes it
    base_sha: str | None  # the base file it replaces; None adds a file

    def __post_init__(self) -> None:
        _path(self.path)
        if self.content is not None and type(self.content) is not bytes:
            raise TypeError("content must be bytes")
        if self.content is None and self.base_sha is None:
            raise CodeError("change", f"nothing to change at {self.path}")
        if self.base_sha is not None:
            _sha(self.base_sha, "base_sha")

    @property
    def kind(self) -> str:
        if self.base_sha is None:
            return "add"
        return "delete" if self.content is None else "modify"

    @property
    def new_sha(self) -> str | None:
        c = self.content
        return None if c is None else hashlib.sha256(c).hexdigest()


@dataclass(frozen=True, slots=True)
class Patch:
    changes: tuple[FileChange, ...]

    def __post_init__(self) -> None:
        ch = tuple(sorted(self.changes, key=lambda c: c.path))
        if len({c.path for c in ch}) != len(ch):
            raise CodeError("duplicate_path")
        object.__setattr__(self, "changes", ch)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(c.path for c in self.changes)

    @property
    def digest(self) -> str:
        rows = [[c.path, c.kind, c.base_sha, c.new_sha] for c in self.changes]
        return digest(["qw.code.patch/1", rows])


@dataclass(frozen=True, slots=True)
class Provenance:
    producer: im.Party  # the generator's actor, route, family and version
    task_digest: str  # sha256 of the task text it was given
    generated_at: datetime

    def __post_init__(self) -> None:
        _sha(self.task_digest, "task_digest")
        object.__setattr__(self, "generated_at", ensure_aware_utc(self.generated_at))


@dataclass(frozen=True, slots=True)
class Manifest:
    base_digest: str
    patch_digest: str
    touched: frozenset[str]  # declared by the generator; the gate checks it
    provenance: Provenance

    def __post_init__(self) -> None:
        _sha(self.base_digest, "base_digest")
        _sha(self.patch_digest, "patch_digest")
        object.__setattr__(self, "touched", frozenset(_path(p) for p in self.touched))

    @property
    def digest(self) -> str:
        pv, pr = self.provenance, self.provenance.producer
        return digest(["qw.code.manifest/1", self.base_digest, self.patch_digest,
                       sorted(self.touched),
                       [pr.actor_id, pr.route_id, pr.family_id, pr.version],
                       pv.task_digest, format_instant(pv.generated_at)])  # fmt: skip


@dataclass(frozen=True, slots=True)
class CodeCandidate:
    base: Snapshot
    patch: Patch
    manifest: Manifest

    def __post_init__(self) -> None:
        m = self.manifest
        if (m.base_digest, m.patch_digest) != (self.base.digest, self.patch.digest):
            raise CodeError("manifest_mismatch", "manifest names another base or patch")

    @property
    def digest(self) -> str:
        return self.manifest.digest


def candidate(base: Snapshot, patch: Patch, touched: frozenset[str],
              provenance: Provenance) -> CodeCandidate:  # fmt: skip
    m = Manifest(base.digest, patch.digest, touched, provenance)
    return CodeCandidate(base, patch, m)


@dataclass(frozen=True, slots=True)
class Proposal:  # what a generator returns: untrusted until the gate has run
    changes: tuple[FileChange, ...]
    touched: frozenset[str]
    producer: im.Party


class Generator(Port):
    def propose(self, base: Snapshot, task: str) -> Proposal: ...


def generate(gen: Generator, base: Snapshot, task: str, at: datetime) -> CodeCandidate:
    p = gen.propose(base, task)
    prov = Provenance(p.producer, hashlib.sha256(task.encode()).hexdigest(), at)
    return candidate(base, Patch(tuple(p.changes)), frozenset(p.touched), prov)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# (reason, glob) over "/" + lowercased path; a policy may add rules, never drop one.
FORBIDDEN: tuple[tuple[str, str], ...] = tuple(
    (reason, g) for reason, globs in (
        ("tests", "*/tests/* */test/* */test_* *_test.py */conftest.py *.feature "
                  "*/fixtures/* */pytest.ini */tox.ini */setup.cfg */noxfile.py"),
        ("dependencies", "*/pyproject.toml *.lock */package.json */pnpm-*.yaml "
                         "*/requirements*.txt"),
        ("ci_config", "/.github/* /.agentic/* /tools/* */.pre-commit-config.yaml"),
        ("governance", "/docs/* */claude.md */agents.md"),
        ("migrations", "*/sql/* */migrations/* */migrations.py */schema_guard.py"),
        ("signing_keys", "*.pem *.key *.p12 */.env* */keys/* *secret*"),
        ("assessor", "*/qw_domain/improvement.py *_improvement.py "
                     "*/qw_domain/trial_ledger.py */qw_domain/feedback.py "
                     "*/qw_domain/personal_adaptation.py"),
        ("release_gates", "*/qw_domain/release.py */qw_domain/capabilities.py"),
        ("risk_thresholds", "*/qw_domain/risk.py */qw_domain/risk_pauses.py "
                            "*/qw_domain/policy.py */qw_domain/strategy_gate.py"),
        ("budget", "*/qw_domain/budget.py */qw_domain/gateway.py"),
        ("verification_rules", "*/qw_domain/evaluator.py */qw_domain/evidence.py "
                               "*/qw_domain/scorecard.py */qw_domain/eval_stats.py"),
        ("authorization", "*/qw_domain/scopes.py */qw_domain/identity.py "
                          "*/qw_domain/rights.py */qw_adapters/tenancy.py"),
        ("sandbox", "*sandbox*"),
    ) for g in globs.split()
)  # fmt: skip
# Protected by default: only these pure analytics modules may change (an interpretation
# of the §11 code target for owner review). The denylist above is a second layer.
_MODS = "returns benchmark option_pricing option_greeks option_scenarios"
IMPROVABLE = tuple(f"packages/domain/src/qw_domain/{m}.py" for m in _MODS.split())
# Importable stdlib: pure computation and no module-valued attributes (no os/sys reach).
_STDLIB_OK = _words("math decimal datetime itertools bisect heapq numbers")
_FROM_NAMES = {  # importable only as `from X import name`, and only these names
    "typing": _words("Any ClassVar Final Generic Iterable Iterator Mapping NamedTuple "
                     "Optional Protocol Self Sequence TypeAlias TypeVar Union"),
    "collections.abc": _words("Callable Collection Iterable Iterator Mapping Sequence"),
    "dataclasses": _words("dataclass field fields replace FrozenInstanceError"),
    "enum": _words("Enum IntEnum StrEnum auto unique"),
}  # fmt: skip
_FROM_ONLY = frozenset(_FROM_NAMES)
_MODULE_NAMES = frozenset(sys.stdlib_module_names) | _words(
    "os sys builtins inspect codecs"
)
_DANGER_ATTRS = _words(  # module attributes refused on any object
    "os sys builtins inspect codecs importlib subprocess socket ctypes types posix "
    "marshal pickle copyreg gc shutil pathlib"
)
_RUNNERS = _words("pytest _pytest hypothesis")  # test-runner imports: test gaming
_DENY_CALLS = _words(
    "eval exec compile __import__ open breakpoint exit quit setattr delattr getattr "
    "globals locals vars input print help license copyright credits dir"
)
_FRAME = re.compile(r"(gi|cr|ag|f|tb|co)_")  # generator/frame/traceback/code reach
_DENY_NAMES = frozenset({"SystemExit", "KeyboardInterrupt", "BaseException"})
_DUNDER_OK = frozenset({"__init__", "__post_init__", "__name__", "__all__"})


@dataclass(frozen=True, slots=True)
class CodePolicy:
    forbidden: tuple[tuple[str, str], ...] = FORBIDDEN
    improvable: tuple[str, ...] = IMPROVABLE
    max_files: int = MAX_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        for n in ("forbidden", "improvable"):
            object.__setattr__(self, n, tuple(getattr(self, n)))
        bounds = ((self.max_files, MAX_FILES), (self.max_file_bytes, MAX_FILE_BYTES),
                  (self.max_total_bytes, MAX_TOTAL_BYTES))  # fmt: skip
        if not (
            set(FORBIDDEN) <= set(self.forbidden)
            and set(self.improvable) <= set(IMPROVABLE)
            and all(type(v) is int and 1 <= v <= cap for v, cap in bounds)
        ):
            raise CodeError("policy_weakened", "a policy can only be stricter")

    @property
    def digest(self) -> str:
        return digest(["qw.code.policy/1", sorted(map(list, self.forbidden)),
                       sorted(self.improvable),
                       self.max_files, self.max_file_bytes,
                       self.max_total_bytes])  # fmt: skip


def _python(path: str, data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"not_utf8:{path}"]
    if _COOKIE.search("\n".join(text.splitlines()[:2])):
        return [f"encoding_declaration:{path}"]  # the interpreter would honour it
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return [f"syntax_error:{path}"]
    out: list[str] = []
    imported: set[str] = set()
    for n in ast.walk(tree):
        mods: list[str] = []
        if isinstance(n, ast.Import):
            mods = [a.name for a in n.names]
            imported |= {a.asname or a.name.split(".")[0] for a in n.names}
            out += [f"forbidden_import:{m}" for m in mods if m in _FROM_ONLY]
        elif isinstance(n, ast.ImportFrom):
            mods = [n.module or ""] if n.level == 0 else []
            imported |= {a.asname or a.name for a in n.names}
            for a in n.names:  # never a module, a private name or everything
                if a.name == "*":
                    out.append("star_import")
                elif a.name in _MODULE_NAMES or a.name.startswith("_"):
                    out.append(f"module_import:{a.name}")
                elif a.name not in _FROM_NAMES.get(n.module or "", {a.name}):
                    out.append(f"name_not_allowed:{a.name}")  # e.g. get_type_hints
        for m in mods:
            root, ok = m.split(".")[0], m in _STDLIB_OK | _FROM_ONLY
            if root in sys.stdlib_module_names | _RUNNERS and not ok:
                out.append(f"forbidden_import:{root}")
            elif not ok and not root.startswith("qw_"):
                out.append(f"unauthorized_dependency:{root}")
        if isinstance(n, ast.Attribute) and (n.attr in _DANGER_ATTRS or (
            _root(n) in imported and (n.attr.startswith("_") or (
                n.attr in _MODULE_NAMES and n.attr not in _STDLIB_OK)))):  # fmt: skip
            out.append(f"module_attribute:{n.attr}")  # re-exported os/sys and privates
        if isinstance(n, ast.Name) and n.id in _DENY_CALLS:  # called or aliased
            out.append(f"forbidden_call:{n.id}")
        if isinstance(n, ast.Name) and (n.id in _DENY_NAMES or (
            n.id.startswith("__") and n.id.endswith("__") and n.id not in _DUNDER_OK
        )):  # fmt: skip
            out.append(f"forbidden_name:{n.id}")
        if isinstance(n, ast.Attribute) and n.attr.startswith("__") and (
            n.attr not in _DUNDER_OK
        ):  # fmt: skip
            out.append(f"dunder_access:{n.attr}")
        if isinstance(n, ast.Match):  # class patterns read attributes by name
            out.append("match_statement")
        notes = [n.annotation] if isinstance(n, ast.arg | ast.AnnAssign) else []
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
            notes = [n.returns]
        if any(isinstance(c, ast.Constant) and isinstance(c.value, str)
               for a in notes if a is not None for c in ast.walk(a)):  # fmt: skip
            out.append("string_annotation")  # evaluated by get_type_hints and friends
        fn = n.func if isinstance(n, ast.Call) else None
        if isinstance(fn, ast.Name) and fn.id in _FROM_NAMES["enum"] and any(
            isinstance(a, ast.Constant) for a in n.args  # type: ignore[attr-defined]
        ):  # fmt: skip
            out.append("enum_functional_api")
        if isinstance(n, ast.Attribute) and _FRAME.match(n.attr):
            out.append(f"frame_introspection:{n.attr}")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "__" in n.value:
            out.append("dunder_string")  # attrgetter/format-style dunder reach
        targets: list[ast.expr] = []
        if isinstance(n, ast.Assign | ast.Delete):
            targets = list(n.targets)
        elif isinstance(n, ast.AugAssign | ast.AnnAssign):
            targets = [n.target]
        for t in targets:
            if isinstance(t, ast.Attribute | ast.Subscript) and _root(t) in imported:
                out.append(f"module_mutation:{_root(t)}")
    return [f"{f}:{path}" for f in dict.fromkeys(out)]


def _root(e: ast.expr) -> str:
    while isinstance(e, ast.Attribute | ast.Subscript | ast.Call):
        e = e.func if isinstance(e, ast.Call) else e.value
    return e.id if isinstance(e, ast.Name) else ""


def _shadows(p: str, base: Snapshot, policy: CodePolicy) -> list[str]:
    parts, out = p.split("/"), []
    for k in range(1, len(parts)):  # each ancestor dir as a module, protected or real
        stem = "/".join(parts[:k]) + ".py"
        low = "/" + stem.lower()
        if stem in base.files or any(fnmatch.fnmatchcase(low, g)
                                     for _, g in policy.forbidden):  # fmt: skip
            out.append(f"shadows_module:{p}")
            break
    if p.endswith(".py") and any(f.startswith(p[:-3] + "/") for f in base.files):
        out.append(f"shadows_package:{p}")
    return out


def check(c: CodeCandidate, policy: CodePolicy) -> tuple[str, ...]:
    """The static gate: every finding refuses the candidate before anything runs."""
    changes, touched, paths = c.patch.changes, c.manifest.touched, c.patch.paths
    out = [] if changes else ["empty_patch"]
    out += [f"undeclared_path:{p}" for p in sorted(paths - touched)]
    out += [f"declared_untouched:{p}" for p in sorted(touched - paths)]
    if len(changes) > policy.max_files:
        out.append(f"too_many_files:{len(changes)}>{policy.max_files}")
    total = 0
    for ch in changes:
        p, low = ch.path, "/" + ch.path.lower()
        hits = dict.fromkeys(
            r for r, g in policy.forbidden if fnmatch.fnmatchcase(low, g)
        )
        out += [f"forbidden_path:{r}:{p}" for r in hits]
        if ch.kind == "delete" and "tests" in hits:
            out.append(f"test_deletion:{p}")
        if p not in policy.improvable:  # protected by default
            out.append(f"not_improvable:{p}")
        out += _shadows(p, c.base, policy)
        base = c.base.files.get(p)
        if ch.base_sha is None and base is not None:
            out.append(f"exists_in_base:{p}")
        elif ch.base_sha is not None and base != ch.base_sha:
            out.append(f"stale_base:{p}")
        elif ch.new_sha == base:
            out.append(f"no_op_change:{p}")
        if ch.content is not None:
            total += len(ch.content)
            if len(ch.content) > policy.max_file_bytes:
                out.append(
                    f"file_too_large:{p}:{len(ch.content)}>{policy.max_file_bytes}"
                )
            out += _python(p, ch.content)
    if total > policy.max_total_bytes:
        out.append(f"patch_too_large:{total}>{policy.max_total_bytes}")
    try:
        applied(c.base, c.patch)
    except CodeError as e:  # e.g. a file where the base has a package directory
        out.append(e.code)
    return tuple(out)


def applied(base: Snapshot, patch: Patch) -> Snapshot:
    tree = dict(base.files) | {c.path: c.new_sha for c in patch.changes}
    return Snapshot({p: s for p, s in tree.items() if s is not None})


@dataclass(frozen=True, slots=True)
class Profile:
    profile_id: str
    base: Snapshot
    policy: CodePolicy
    argv: tuple[str, ...]  # the protected test command; never from the candidate
    limits: sb.SandboxLimits
    expected_passed: int  # the protected suite's test count
    rootfs_digest: str  # the pinned rootfs tree sha256 (tools/candidate_rootfs.json)
    interpreter: tuple[str, str]  # guest path and major.minor, e.g. "3.13"

    def __post_init__(self) -> None:
        _sha(self.rootfs_digest, "rootfs_digest")
        exe, ver = tuple(self.interpreter)
        if not exe.startswith("/") or not _VERSION.fullmatch(ver):
            raise CodeError("interpreter", safe_repr(self.interpreter))
        object.__setattr__(self, "interpreter", (exe, ver))
        try:
            sb.SandboxRequest("profile-check", tuple(self.argv), self.limits)
        except sb.SandboxError as e:
            raise CodeError("argv", e.code) from None
        object.__setattr__(self, "argv", tuple(self.argv))
        if type(self.expected_passed) is not int or self.expected_passed < 1:
            raise CodeError("expected_passed", "at least one protected test")

    @property
    def digest(self) -> str:
        lim = [getattr(self.limits, n) for n in sb.CEILINGS]
        return digest(["qw.code.profile/2", self.profile_id, self.base.digest,
                       self.policy.digest, list(self.argv), lim,
                       self.expected_passed, self.rootfs_digest,
                       list(self.interpreter)])  # fmt: skip


def check_protocol(p: im.Protocol, profile: Profile) -> None:
    if p.target is not CODE:
        raise CodeError("target", p.target.value)
    if p.scope is not im.Scope.INSTALLATION:  # R039: shared code is installation-scoped
        raise CodeError("tenant_scope", p.experiment_id)
    if p.baseline_hash != profile.base.digest:
        raise CodeError("baseline_mismatch", p.experiment_id)
    frozen = (p.sandbox_profile_id, p.mutation_space_hash)
    if frozen != (profile.profile_id, profile.digest):
        raise CodeError("profile_mismatch", p.experiment_id)


@dataclass(frozen=True, slots=True)
class Staged:
    tree_digest: str  # of the tree as written, read back
    rootfs_digest: str  # of the rootfs the runner mounts, computed at staging
    runner: sb.SandboxRunner  # mounts the tree read-only and feeds the key
    channel_key: bytes = field(repr=False)  # per stage; only the harness parent

    def __post_init__(self) -> None:
        _sha(self.tree_digest, "tree_digest")
        _sha(self.rootfs_digest, "rootfs_digest")
        if type(self.channel_key) is not bytes or len(self.channel_key) < 16:
            raise CodeError("channel_key", "at least 16 bytes")


class Stager(Port):  # builds base + patch beside the rootfs; attests both
    def stage(self, base: Snapshot, patch: Patch) -> Staged: ...


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"  # ran and failed, or contained by a limit or runtime error
    BLOCKED = "blocked"  # nothing ran: the sandbox or the image was unusable
    REFUSED = "refused"  # the static gate refused it; nothing ran


@dataclass(frozen=True, slots=True)
class Evaluation:
    candidate_digest: str
    profile_digest: str  # binds the base, policy, argv, limits and expected count
    outcome: Outcome
    reasons: tuple[str, ...]
    findings: tuple[str, ...]
    tree_digest: str | None = None
    run_id: str | None = None
    status: str | None = None  # sandbox RunStatus value
    exit_code: int | None = None
    result_digest: str | None = None
    runtime: str = ""
    rootfs_digest: str | None = None
    interpreter: str | None = None  # "path version", from the signed harness line

    def __post_init__(self) -> None:
        ran = self.result_digest is not None
        ok = {
            Outcome.PASSED: not self.reasons and not self.findings and ran
            and self.status == sb.RunStatus.EXITED.value and self.exit_code == 0
            and self.rootfs_digest is not None and self.interpreter is not None,
            Outcome.FAILED: bool(self.reasons) and ran,
            Outcome.BLOCKED: bool(self.reasons) and self.exit_code is None,
            Outcome.REFUSED: bool(self.findings) and not ran,
        }[self.outcome]  # fmt: skip
        if not ok:
            raise CodeError("evaluation_inconsistent", self.outcome.value)

    @property
    def digest(self) -> str:
        return digest(["qw.code.evaluation/2", self.candidate_digest,
                       self.profile_digest, self.outcome.value,
                       list(self.reasons), list(self.findings), self.tree_digest,
                       self.run_id, self.status, self.exit_code, self.result_digest,
                       self.runtime, self.rootfs_digest,
                       self.interpreter])  # fmt: skip


def _authentic(out: sb.StreamCapture, key: bytes) -> list[str]:
    """Bodies of the stdout lines carrying a valid MAC under the stage key."""
    bodies = []
    for line in out.head.decode("utf-8", "replace").splitlines():
        tag, _, rest = line.partition(" ")
        body, _, mac = rest.rpartition(" ")
        want = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
        if tag == HARNESS and hmac.compare_digest(mac.encode(), want.encode()):
            bodies.append(body)
    return bodies


def harness_report(out: sb.StreamCapture, key: bytes,
                   profile: Profile) -> tuple[list[str], str | None]:  # fmt: skip
    """Reasons the signed harness line refuses a pass, and the attested interpreter.
    Unsigned, wrongly signed or repeated lines never count."""
    if out.truncated:
        return ["summary:truncated"], None
    if len(bodies := _authentic(out, key)) != 1:
        return [f"summary:signed_lines={len(bodies)}"], None
    try:
        b = json.loads(bodies[0])
        exe, ver, report = b["executable"], b["version"], b["report"]
        counts = b["counts"]
        ok = isinstance(exe, str) and isinstance(ver, str) and report in _REPORTS
        ok = ok and type(b["child_exit"]) is int and type(b["nondumpable"]) is bool
        ok = ok and set(counts) == set(COUNTS)
        ok = ok and all(type(counts[k]) is int and counts[k] >= 0 for k in COUNTS)
    except (ValueError, TypeError, KeyError):
        ok = False
    if not ok:
        return ["summary:malformed"], None
    why: list[str] = []
    if (exe, ".".join(ver.split(".")[:2])) != profile.interpreter:
        why.append("interpreter_mismatch")
    if not b["nondumpable"]:
        why.append("harness_dumpable")
    if report != "ok":
        why.append(f"summary:report_{report}")
    if b["child_exit"] != 0:
        why.append(f"summary:child_exit={b['child_exit']}")
    why += [f"summary:{w}" for w in COUNTS[1:] if counts[w]]
    if counts["passed"] != profile.expected_passed:
        why.append(f"summary:passed={counts['passed']}!={profile.expected_passed}")
    return why, f"{exe} {ver}"


def evaluate(c: CodeCandidate, profile: Profile, stager: Stager,
             attempt: int = 1) -> Evaluation:  # fmt: skip
    """Gate, stage and run the protected tests in the sandbox; bound to `c`."""
    if c.base.digest != profile.base.digest:
        raise CodeError("base_mismatch", "the candidate's base is not the profile's")
    if type(attempt) is not int or not 1 <= attempt <= 99:
        raise CodeError("attempt", safe_repr(attempt))
    found = check(c, profile.policy)
    head = (c.digest, profile.digest)
    if found:
        return Evaluation(*head, Outcome.REFUSED, (), found)
    tree = applied(c.base, c.patch).digest
    st = stager.stage(c.base, c.patch)
    if st.tree_digest != tree:
        return Evaluation(*head, Outcome.BLOCKED, ("image_mismatch",), (), tree)
    if st.rootfs_digest != profile.rootfs_digest:  # nothing runs on another rootfs
        return Evaluation(*head, Outcome.BLOCKED, ("rootfs_mismatch",), (), tree,
                          rootfs_digest=st.rootfs_digest)  # fmt: skip
    run_id = f"code-{c.digest[:24]}-a{attempt}"
    req = sb.SandboxRequest(run_id, profile.argv, profile.limits)
    r = sb.execute(st.runner, req)
    reasons: list[str] = []
    why, interp = harness_report(r.stdout, st.channel_key, profile)
    if r.status is sb.RunStatus.BLOCKED:
        reasons.append(f"sandbox_blocked:{r.reason}")
    elif r.run_id != run_id:
        reasons.append("result_unbound")
    elif r.status is sb.RunStatus.LIMIT:
        reasons.append(f"limit_exceeded:{r.limit_hit.value}")
    elif r.status is sb.RunStatus.ERROR:
        reasons.append("runtime_error")
    elif r.exit_code != 0:
        reasons.append(f"tests_failed:exit={r.exit_code}")
    else:
        reasons += why
    outcome = Outcome.FAILED if reasons else Outcome.PASSED
    if r.status is sb.RunStatus.BLOCKED:
        outcome = Outcome.BLOCKED
    return Evaluation(*head, outcome, tuple(reasons), (), tree, run_id, r.status.value,
                      r.exit_code, r.digest(), r.runtime, st.rootfs_digest,
                      interp)  # fmt: skip


_BUCKETS = (  # finding prefix -> the T044 application_code hard control it counts
    ("forbidden_path:signing_keys:", "secret_access_attempts"),
    ("forbidden_path:dependencies:", "unauthorized_dependencies"),
    ("unauthorized_dependency:", "unauthorized_dependencies"),
    *((pre, "protected_control_mutations")
      for pre in ("forbidden_path:", "test_deletion:", "module_mutation:")),
    *((pre, "host_writes")
      for pre in ("forbidden_import:", "forbidden_call:", "forbidden_name:",
                  "dunder_")),
)  # fmt: skip


def metrics(e: Evaluation) -> dict[str, Decimal]:
    """Hard-control counts (files per control) plus the `tests_passed` goal."""
    seen: dict[str, set[str]] = {m: set() for m in im.HARD_CONTROLS[CODE]}
    for f in e.findings:
        bucket = next((b for pre, b in _BUCKETS if f.startswith(pre)), None)
        if bucket is not None:
            seen[bucket].add(f.rsplit(":", 1)[-1])
    out = {m: Decimal(len(paths)) for m, paths in seen.items()}
    out["tests_passed"] = Decimal(1 if e.outcome is Outcome.PASSED else 0)
    return out


def to_candidate(c: CodeCandidate, candidate_id: str, experiment_id: str,
                 parent_id: str, created_at: datetime) -> im.Candidate:  # fmt: skip
    """The T044 lineage record; its artifact hash is the manifest digest."""
    payload = {"base_digest": c.base.digest, "patch_digest": c.patch.digest}
    return im.Candidate(candidate_id, experiment_id, CODE, parent_id,
                        c.manifest.provenance.producer, c.digest, payload,
                        {"code_manifest": "qw.code.manifest/1"}, (),
                        created_at)  # fmt: skip


LIMITATIONS = ("in_process_test_summary", "static_gate_best_effort")


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    experiment_id: str
    candidate_id: str
    candidate_hash: str  # T044 Candidate.content_hash
    protocol_hash: str
    evaluation_digest: str  # binds the manifest, profile, gate report and result
    outcome: Outcome
    metrics: Mapping[str, Decimal]
    limitations: tuple[str, ...] = LIMITATIONS

    @property
    def eligible(self) -> bool:
        clean = all(self.metrics[m] == 0 for m in im.HARD_CONTROLS[CODE])
        return self.outcome is Outcome.PASSED and clean

    @property
    def digest(self) -> str:
        return digest(["qw.code.bundle/1", self.experiment_id, self.candidate_id,
                       self.candidate_hash, self.protocol_hash,
                       self.evaluation_digest, self.outcome.value,
                       {k: str(v) for k, v in self.metrics.items()},
                       list(self.limitations)])  # fmt: skip


def package(p: im.Protocol, profile: Profile, c: CodeCandidate, e: Evaluation,
            cand: im.Candidate) -> EvidenceBundle:  # fmt: skip
    """The evidence the protected assessor and the human reviewer receive."""
    check_protocol(p, profile)
    if (cand.artifact_hash, cand.experiment_id, cand.target, cand.producer) != (
        c.digest, p.experiment_id, p.target, p.producer
    ):  # fmt: skip
        raise CodeError("candidate_unbound", cand.candidate_id)
    if (e.candidate_digest, e.profile_digest, e.findings) != (
        c.digest, profile.digest, check(c, profile.policy)
    ):  # fmt: skip
        raise CodeError("evaluation_unbound", cand.candidate_id)
    if e.outcome is Outcome.PASSED:  # the pass ran on the pinned rootfs and interpreter
        exe, _, ver = (e.interpreter or "").rpartition(" ")
        got = (e.rootfs_digest, exe, ".".join(ver.split(".")[:2]))
        if got != (profile.rootfs_digest, *profile.interpreter):
            raise CodeError("evaluation_unbound", cand.candidate_id)
    return EvidenceBundle(p.experiment_id, cand.candidate_id, cand.content_hash,
                          p.content_hash, e.digest, e.outcome,
                          MappingProxyType(metrics(e)))  # fmt: skip


@dataclass(frozen=True, slots=True)
class ReviewRoute:
    bundle_digest: str
    state: str  # spec §11 candidate state
    required_approvals: frozenset[im.ApprovalClass]  # typed acts, granted elsewhere
    blocked_by: tuple[str, ...]
    limitations: tuple[str, ...]  # e.g. a pass read from an in-process test summary

    @property
    def automatic(self) -> bool:
        return False  # a human release decision is always required


def route_for_review(b: EvidenceBundle, p: im.Protocol) -> ReviewRoute:
    if b.protocol_hash != p.content_hash:
        raise CodeError("protocol_mismatch", b.experiment_id)
    state = "rejected"
    if b.eligible:
        state = "awaiting_human_review"
    elif b.outcome is Outcome.BLOCKED:
        state = "insufficient_evidence"
    gates = ("sandbox_unqualified",) if p.target in im.SANDBOX_TARGETS else ()
    return ReviewRoute(b.digest, state, frozenset(p.approvals), gates, b.limitations)
