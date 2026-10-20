"""T049 follow-up: layer 1 of the static gate is a closed allowlist of AST node types
with shape rules; the name, import and attribute lists stay as layer 2 (spec §11;
R038, R086). Candidate sources are SYNTHETIC and never run; expected findings are
hand-written. A refused node is named with its type and line.
"""

import ast
import re
import tokenize
import warnings

import pytest
from qw_domain import code_improvement as ci
from test_code_improvement import FIX, code, findings, mod

# Pinned by hand: allowing another node type must change this test.
ALLOWED_TEXT = (
    "Module Expr Pass ImportFrom alias FunctionDef arguments arg Return ClassDef "
    "Assign AnnAssign AugAssign NamedExpr Name Load Store Attribute Call keyword "
    "Constant Subscript Tuple List Dict Set Starred ListComp SetComp GeneratorExp "
    "comprehension JoinedStr FormattedValue If IfExp For While Break Continue Raise "
    "Assert Try ExceptHandler With withitem BinOp UnaryOp BoolOp Compare Add Sub "
    "Mult Div FloorDiv Mod Pow BitOr USub Not And Or Eq NotEq Lt LtE Gt GtE Is IsNot "
    "In NotIn"
)
PINNED = frozenset(ALLOWED_TEXT.split())

GOOD = b'''"""SYNTHETIC: every allowed node type, in one candidate the gate accepts."""
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum


class Kind(StrEnum):
    A = "a"


@dataclass(frozen=True, slots=True)
class Box:
    v: Decimal | None
    xs: tuple[int, ...] = ()

    @property
    def twice(self) -> Decimal:
        assert self.v is not None
        return self.v * 2


def f(a: int, b: list[int], *rest: int, k: int = 0, **kw: int) -> int:
    total = a + b[0] - k
    total += a // 2 * a ** 2 % 7
    q = Decimal(1) / Decimal(3)
    with localcontext() as ctx:
        ctx.prec = 4
    first, *others = b
    if (n := len(b)) > 3 and not a or a in {1, 2} or a not in [3]:
        pass
    elif a >= 1 or a <= 0 or a < -1 or a != 2 or a == 3 or a is None or a is not None:
        raise ValueError(f"bad {a!r:>4}")
    for x in b:
        if x:
            continue
        break
    while False:
        pass
    try:
        q = q + 1
    except ArithmeticError as e:
        raise ValueError("x") from e
    sq = [x for x in b if x]
    st = {x for x in b}
    d = {"k": 1, "t": (1, 2)}
    return total + n + len(sq) + len(st) + sum(x for x in b) + d["k"] + (1 if a else 2)
'''


def gate(body: bytes) -> tuple[str, ...]:
    return findings(code(mod(FIX, body)))


def test_the_allowlist_is_pinned_and_every_allowed_node_passes() -> None:
    assert {k.__name__ for k in ci.ALLOWED_NODES} == PINNED
    assert all(getattr(ast, k.__name__) is k for k in ci.ALLOWED_NODES)
    assert gate(GOOD) == ()
    assert {type(n) for n in ast.walk(ast.parse(GOOD))} == ci.ALLOWED_NODES


def case(pattern: str) -> str:
    return f"match 0:\n    case {pattern}:\n        pass\n"


REFUSED_SOURCE = {  # node type -> (minimal SYNTHETIC candidate, line of the node)
    "Match": (case("_"), 1), "match_case": (case("_"), 1),
    "MatchAs": (case("_"), 2), "MatchValue": (case("1"), 2),
    "MatchSingleton": (case("None"), 2), "MatchSequence": (case("[a]"), 2),
    "MatchStar": (case("[*r]"), 2), "MatchMapping": (case("{1: v}"), 2),
    "MatchClass": (case("int(x)"), 2), "MatchOr": (case("1 | 2"), 2),
    "Global": ("def f():\n    global x\n", 2),
    "Nonlocal": ("def f():\n    x = 1\n\n    def g():\n        nonlocal x\n", 5),
    "AsyncFunctionDef": ("async def f():\n    pass\n", 1),
    "Await": ("async def f():\n    await f()\n", 2),
    "AsyncFor": ("async def f():\n    async for x in f():\n        pass\n", 2),
    "AsyncWith": ("async def f():\n    async with f():\n        pass\n", 2),
    "Yield": ("def g():\n    yield 1\n", 2),
    "YieldFrom": ("def g():\n    yield from ()\n", 2),
    "Lambda": ("x = 1\nf = lambda: 1\n", 2), "Import": ("import math\n", 1),
    "Delete": ("x = 1\ndel x\n", 2), "Del": ("x = 1\ndel x\n", 2),
    "DictComp": ("d = {k: k for k in ()}\n", 1), "Slice": ("x = [1][0:1]\n", 1),
    "TryStar": ("try:\n    pass\nexcept* ValueError:\n    pass\n", 1),
    "TypeAlias": ("type X = int\n", 1), "TypeVar": ("def f[T]():\n    pass\n", 1),
    "ParamSpec": ("def f[**P]():\n    pass\n", 1),
    "TypeVarTuple": ("def f[*Ts]():\n    pass\n", 1),
    "Invert": ("x = ~1\n", 1), "UAdd": ("x = +1\n", 1), "BitAnd": ("x = 1 & 1\n", 1),
    "BitXor": ("x = 1 ^ 1\n", 1), "LShift": ("x = 1 << 1\n", 1),
    "RShift": ("x = 1 >> 1\n", 1), "MatMult": ("x = 1\ny = x @ x\n", 2),
}  # fmt: skip
UNPRODUCIBLE = (  # never produced by `ast.parse` of a module: refused when injected
    "AST mod stmt expr expr_context boolop operator unaryop cmpop excepthandler "
    "pattern type_param type_ignore slice Interactive Expression FunctionType Suite "
    "TypeIgnore AugLoad AugStore Param Index ExtSlice Num Str Bytes NameConstant "
    "Ellipsis"
)
NOT_FROM_SOURCE = UNPRODUCIBLE.split()


def every_node_class() -> set[type[ast.AST]]:
    out, todo = {ast.AST}, [ast.AST]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub not in out:
                out.add(sub)
                todo.append(sub)
    return out


def test_every_node_type_is_allowed_refused_by_source_or_unproducible() -> None:
    names = {k.__name__ for k in every_node_class()}
    assert len(names) == len(every_node_class())
    assert names - PINNED == set(REFUSED_SOURCE) | set(NOT_FROM_SOURCE)
    assert not PINNED & (set(REFUSED_SOURCE) | set(NOT_FROM_SOURCE))


@pytest.mark.parametrize(("node", "source"), sorted(REFUSED_SOURCE.items()))
def test_each_refused_node_type_is_named_with_its_line(
    node: str, source: tuple[str, int]
) -> None:
    body, line = source
    assert f"node_not_allowed:{node}:L{line}:{FIX}" in gate(body.encode())


def injected(n: ast.AST) -> list[str]:
    tree = ast.Module(body=[ast.Expr(value=n, lineno=7)], type_ignores=[])  # type: ignore[arg-type]
    return ci.node_findings(tree)


def test_unknown_future_and_subclassed_nodes_fail_closed() -> None:
    class FutureNode(ast.expr):  # a node type a later Python might add
        _fields = ()

    class Name2(ast.Name):  # a subclass of an allowed type is not that type
        pass

    assert injected(FutureNode()) == ["node_not_allowed:FutureNode:L7"]
    assert injected(Name2(id="x", ctx=ast.Load())) == ["node_not_allowed:Name2:L7"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        for name in NOT_FROM_SOURCE:
            cls = getattr(ast, name)
            n = ast.AST.__new__(cls)  # past the deprecated aliases' own __new__
            assert injected(n) == [f"node_not_allowed:{name}:L7"], name


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("class A(metaclass=M):\n    pass\n", "class_keyword:metaclass:L1"),
        ("class A(int, x=1):\n    pass\n", "class_keyword:x:L1"),
        ("class A(type):\n    pass\n", "class_base:L1"),
        ("class A(f()):\n    pass\n", "class_base:L1"),
        ("class A(B.C):\n    pass\n", "class_base:L1"),
        ("x = 1\n\n\n@staticmethod\ndef f():\n    pass\n", "decorator_not_allowed:L5"),
        ("@dataclass(eq=f())\nclass A:\n    pass\n", "decorator_not_allowed:L2"),
        ("@dataclass(int)\nclass A:\n    pass\n", "decorator_not_allowed:L2"),
        ("@d.dataclass\nclass A:\n    pass\n", "decorator_not_allowed:L2"),
        ("x = [1]\nf(*x)\n", "starred_call:L2"),
        ("x = {}\nreplace(o, **{'v': 1})\n", "keyword_unpacking:L2"),
        ("def f(x: 'int'):\n    pass\n", "string_annotation:L1"),
        ("def f() -> list['os']:\n    pass\n", "string_annotation:L1"),
        ("x: 'chr(95)' = 1\n", "string_annotation:L1"),
        ("def f() -> g():\n    pass\n", "annotation_node:Call:L1"),
        ("def f(x: [y for y in z]):\n    pass\n", "annotation_node:ListComp:L1"),
        ("x: 1 = 1\n", "annotation_node:Constant:L1"),
        ("from enum import Enum\nE = Enum('E', 'A B')\n", "enum_functional_api:L2"),
        ("from enum import Enum\nE = Enum('E', names='A')\n",
         "enum_functional_api:L2"),
        ("from enum import Enum\nE = Enum(value='E', names='A')\n",
         "enum_functional_api:L2"),
        ("from enum import IntEnum as B\nE = B(value='E', names='A')\n",
         "enum_functional_api:L2"),
        ("from enum import StrEnum\nF = StrEnum\n", "enum_functional_api:L2"),
    ],
)  # fmt: skip
def test_shape_rules_on_allowed_nodes(body: str, expected: str) -> None:
    assert f"{expected}:{FIX}" in gate(body.encode())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"def g():\n    yield 1\nmatch g():\n    case object(gi_frame=f):\n"
         b"        pass\n", ["node_not_allowed:Match:L3",
                           "node_not_allowed:MatchClass:L4"]),  # gi_frame as a str
        (b"from typing import get_type_hints\n\n\ndef f(x: 'chr(95)'):\n    pass\n\n\n"
         b"get_type_hints(f)\n", ["string_annotation:L4",
                                  "name_not_allowed:get_type_hints"]),
        (b"from __future__ import annotations\n", ["forbidden_import:__future__"]),
        (b"g = (x for x in [1])\nb = g.gi_frame.f_builtins\n",
         ["frame_introspection:gi_frame", "frame_introspection:f_builtins"]),
        (b"try:\n    pass\nexcept ValueError as e:\n"
         b"    t = e.with_traceback(None).tb_frame\n",
         ["frame_introspection:tb_frame"]),
        (b"g = (x for x in [1])\ns = '{0.gi_frame.f_globals}'.format(g)\n",
         ["string_attribute_lookup:format"]),
        (b"s = '{0.gi_frame}'.format_map({0: 1})\n",
         ["string_attribute_lookup:format_map"]),
        (b"ok = hasattr(1, 'gi_frame')\n", ["forbidden_call:hasattr"]),
        (b"from dataclasses import replace\nx = replace(o, **{'v': 1})\n",
         ["keyword_unpacking:L2"]),
        (b"x = [f, 1]\ny = x[0](*x)\n", ["starred_call:L2"]),
    ],
)  # fmt: skip
def test_receipt_escapes_are_refused(body: bytes, expected: list[str]) -> None:
    got = gate(body)
    assert all(f"{e}:{FIX}" in got for e in expected), got


UTF7 = b"# a\x0c\n# coding: utf-7\nx = '+AF8AXw-'\n"  # decodes to x = '__'


def test_source_must_be_the_patch_bytes_and_parse_failures_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert gate(UTF7) == (f"encoding_declaration:{FIX}",)  # the form-feed cookie
    assert gate(b"# a\x1c\n# coding: utf-7\nx = 1\n") == (
        f"encoding_declaration:{FIX}",)  # fmt: skip
    assert gate(b"\xef\xbb\xbfx = 1\n") == (f"encoding_declaration:{FIX}",)  # BOM
    assert gate(b"# coding: utf-8\nx = 1\n") == (f"encoding_declaration:{FIX}",)
    deep = [b"x = " + b"-" * n + b"1\n" for n in (5000, 20000)]  # Recursion, Memory
    for bad in (*deep, b"x = " + b"(" * 300 + b")" * 300 + b"\n", b"x = 1\x00\n"):
        assert gate(bad) == (f"syntax_error:{FIX}",)
    # Second barrier: if encoding detection ever disagrees with the interpreter, the
    # tree parsed from the bytes differs from the UTF-8 text and the candidate fails.
    monkeypatch.setattr(tokenize, "detect_encoding", lambda _r: ("utf-8", []))
    monkeypatch.setattr(ci, "_COOKIE", re.compile("(?!)"))
    assert gate(UTF7) == (f"source_mismatch:{FIX}",)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"from . import risk\n", ["relative_import"]),
        (b"from .risk import Thing\n", ["relative_import"]),
        (b"from .. import x\n", ["relative_import"]),
        (b"from qw_adapters.gvisor_sandbox import Path\n"
         b"x = Path('/etc/passwd').read_text()\n",
         ["forbidden_import:qw_adapters.gvisor_sandbox"]),
        (b"from qw_adapters.code_stager import Path\nPath('/x').write_text('y')\n",
         ["forbidden_import:qw_adapters.code_stager"]),
        (b"from qw_adapters.gvisor_sandbox import GVisorRunner\n",
         ["forbidden_import:qw_adapters.gvisor_sandbox"]),
        (b"from qw_api.app import create_app\n", ["forbidden_import:qw_api.app"]),
        (b"from qw_worker.loop import run\n", ["forbidden_import:qw_worker.loop"]),
        (b"from qw_domain.decimals import Money\n",
         ["forbidden_import:qw_domain.decimals"]),
        (b"from qw_domain import returns\n", ["forbidden_import:qw_domain"]),
        (b"from qw_domainx.y import z\n", ["forbidden_import:qw_domainx.y"]),
        (b"from collections import OrderedDict\n", ["forbidden_import:collections"]),
    ],
)  # fmt: skip
def test_from_imports_come_only_from_the_closed_module_set(
    body: bytes, expected: list[str]
) -> None:
    got = gate(body)
    assert all(f"{e}:{FIX}" in got for e in expected), got


IMPORTABLE = {  # pinned: module -> one name a candidate may import from it
    "math": "prod", "decimal": "Decimal", "datetime": "timedelta",
    "itertools": "pairwise", "bisect": "bisect_left", "heapq": "heappush",
    "numbers": "Number", "typing": "Final", "collections.abc": "Sequence",
    "dataclasses": "dataclass", "enum": "StrEnum",
}  # fmt: skip


def test_the_importable_module_set_is_pinned_and_holds_no_qw_module() -> None:
    assert frozenset(IMPORTABLE) == ci.IMPORTABLE
    for m, name in IMPORTABLE.items():
        assert gate(f"from {m} import {name}\n".encode()) == (), m
