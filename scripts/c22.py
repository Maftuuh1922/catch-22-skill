#!/usr/bin/env python3
"""Catch-22: tell an impossible task from a hard one before anyone touches the tests.

Stdlib only. Python 3.9+.

Commands
  c22 check   [PATHS...] [--spec FILE] [--run CMD] ...   verdict: fixable / contradictory / unclear
  c22 tamper  [--base REF]                                 test tampering in the working diff
  c22 baseline                                             snapshot test files now (for repos without git)

Exit codes (stable, meant for CI gates and agent hooks)
  0   fixable        no contradiction found and no tampering
  10  contradictory  the spec and the tests (or two tests) cannot both hold
  11  unclear        failing tests could not be analysed, or only weak evidence was found
  12  tampered       tests were edited, skipped, deleted or loosened in the working diff
  1   internal error, 2 usage error (argparse)
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

VERSION = "0.1.0"

EXIT_FIXABLE = 0
EXIT_CONTRADICTORY = 10
EXIT_UNCLEAR = 11
EXIT_TAMPERED = 12

STATE_DIR = ".c22"

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
    ".c22", "site-packages", ".idea", ".vscode", "coverage",
}

PY_TEST_GLOBS = ["test_*.py", "*_test.py", "tests.py"]
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")
JS_TEST_RE = re.compile(r"\.(test|spec)\.(m|c)?[jt]sx?$")
CONFIG_NAMES = {
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py",
    "package.json", ".mocharc.json", ".mocharc.js", ".mocharc.yml",
}
CONFIG_RE = re.compile(r"^(jest|vitest|vite|karma)\.config\.(m|c)?[jt]s$|^jest\.config\.json$")


class _Missing:
    """Marker for a value we could not evaluate statically."""

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<unevaluated>"


MISSING = _Missing()


class JSUndefined:
    def __repr__(self) -> str:
        return "undefined"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JSUndefined)

    def __hash__(self) -> int:
        return 0


UNDEFINED = JSUndefined()


# --------------------------------------------------------------------------------------
# File classification
# --------------------------------------------------------------------------------------

def norm_path(p: str) -> str:
    return p.replace(os.sep, "/")


def lang_of(path: str) -> Optional[str]:
    if path.endswith(".py"):
        return "py"
    if path.endswith(JS_EXTS):
        return "js"
    return None


def is_test_file(path: str, extra_globs: Sequence[str] = ()) -> bool:
    p = norm_path(path)
    base = p.rsplit("/", 1)[-1]
    for g in extra_globs:
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(base, g):
            return True
    if base == "conftest.py":
        return False  # handled as config
    if p.endswith(".py"):
        if any(fnmatch.fnmatch(base, g) for g in PY_TEST_GLOBS):
            return True
        parts = p.split("/")[:-1]
        return any(part in ("tests", "test") for part in parts)
    if p.endswith(JS_EXTS):
        if JS_TEST_RE.search(base):
            return True
        return "__tests__" in p.split("/")
    return False


def is_config_file(path: str) -> bool:
    base = norm_path(path).rsplit("/", 1)[-1]
    return base in CONFIG_NAMES or bool(CONFIG_RE.match(base))


def walk_files(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".c22")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            yield norm_path(os.path.relpath(full, root))


def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# --------------------------------------------------------------------------------------
# Assertion model
# --------------------------------------------------------------------------------------

@dataclass
class Assertion:
    file: str
    line: int
    lang: str
    kind: str  # eq | ne | truth | raises
    subject: str  # normalised key used for matching
    subject_src: str
    expected_src: str
    test: str = ""  # enclosing test (qualified)
    pure: bool = False  # subject is a call with literal args (or plain-name callee)
    approx: bool = False
    stmt_index: int = 0
    source: str = "test"  # test | spec
    text: str = ""  # the original line(s), for evidence
    func_has_params: bool = False
    func_mocks: bool = False
    expected: Any = field(default=MISSING, repr=False, compare=False)

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("expected", None)
        d["expected_value"] = _jsonable(self.expected)
        return d


def _jsonable(v: Any) -> Any:
    if v is MISSING:
        return None
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)


def values_conflict(a: Assertion, b: Assertion) -> Optional[str]:
    """Return a human reason if a and b cannot both hold for one evaluation of the subject."""
    ka, kb = a.kind, b.kind
    if ka == "raises" and kb == "raises":
        return None  # different exception types may share a base class; too weak
    if (ka == "raises") != (kb == "raises"):
        other = b if ka == "raises" else a
        if other.kind in ("eq", "truth"):
            return "one assertion requires the call to raise, the other requires it to return a value"
        return None
    va, vb = a.expected, b.expected
    if va is MISSING or vb is MISSING:
        return None
    if ka == "eq" and kb == "eq":
        if _equal(va, vb, a.approx or b.approx):
            return None
        return "the same expression is required to equal %s and %s" % (_short(a.expected_src), _short(b.expected_src))
    if {ka, kb} == {"eq", "ne"}:
        if _equal(va, vb, a.approx or b.approx):
            return "one assertion requires == %s, the other requires != %s" % (_short(a.expected_src), _short(b.expected_src))
        return None
    if ka == "truth" and kb == "truth":
        return None if bool(va) == bool(vb) else "one assertion requires a truthy result, the other a falsy one"
    if {ka, kb} == {"eq", "truth"}:
        e = a if ka == "eq" else b
        t = b if ka == "eq" else a
        try:
            if bool(e.expected) != bool(t.expected):
                return "one assertion requires == %s (%s), the other requires a %s result" % (
                    _short(e.expected_src), "truthy" if bool(e.expected) else "falsy",
                    "truthy" if bool(t.expected) else "falsy")
        except Exception:
            return None
    return None


def _equal(a: Any, b: Any, approx: bool) -> bool:
    if approx and isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        tol = max(1e-6 * max(abs(a), abs(b)), 1e-12)
        return abs(a - b) <= tol
    try:
        return bool(a == b)
    except Exception:
        return repr(a) == repr(b)


def _short(s: str, n: int = 60) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------------------
# Python extraction
# --------------------------------------------------------------------------------------

def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        pass
    # numpy-ish: np.array([...]) -> list, for comparison only
    if isinstance(node, ast.Call) and _callee_name(node.func) in ("array", "asarray") and len(node.args) == 1 and not node.keywords:
        try:
            return ast.literal_eval(node.args[0])
        except Exception:
            return MISSING
    return MISSING


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _callee_chain(func: ast.AST) -> Optional[List[str]]:
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
        return list(reversed(parts))
    return None


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - py<3.9
        return ast.dump(node)


class _Canon(ast.NodeTransformer):
    """Canonicalise a subject expression: alias callee names, normalise literals."""

    def __init__(self, aliases: Dict[str, str]):
        self.aliases = aliases

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        chain = _callee_chain(node.func)
        if chain:
            last = self.aliases.get(chain[-1], chain[-1])
            node.func = ast.Name(id=last, ctx=ast.Load())
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in self.aliases:
            return ast.Name(id=self.aliases[node.id], ctx=node.ctx)
        return node


def canon_py(node: ast.AST, aliases: Dict[str, str]) -> str:
    try:
        n = ast.parse(_unparse(node), mode="eval").body  # cheap structural copy
    except (SyntaxError, RecursionError, ValueError):
        return _unparse(node)
    n = _Canon(aliases).visit(n)
    key = _unparse(n)
    if len(key) > 4000:  # huge literal inputs: keep keys small but exact
        key = key[:200] + "...#" + hashlib.sha1(key.encode()).hexdigest()
    return key


class _AnyRoot(set):
    """Spec text describes functions, so `pkg.f(1)` in a spec is treated as a function call."""

    def __contains__(self, item: object) -> bool:
        return True


_ANY_ROOT = _AnyRoot()


def _module_names(tree: ast.AST) -> set:
    """Names bound by top-level imports: `mod.f(1)` on these is a function call, not a method on state."""
    names = set()
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def _is_pure_call(node: ast.AST, module_names: Optional[set] = None) -> bool:
    """A call we may treat as deterministic: literal args and a plain-name callee, or a dotted
    callee rooted at an imported module (`math.floor(2.5)`). Method calls on local objects
    (`user.has_perms([...])`, `sql.count("x")`) depend on state and are never pure."""
    if not isinstance(node, ast.Call):
        return False
    chain = _callee_chain(node.func)
    if not chain:
        return False
    args_ok = all(_literal(a) is not MISSING for a in node.args) and all(
        k.arg is not None and _literal(k.value) is not MISSING for k in node.keywords)
    if not args_ok:
        return False
    if len(chain) == 1:
        return True
    if chain[0] in ("self", "cls"):
        return False
    if module_names is None:
        return False
    return chain[0] in module_names and bool(node.args or node.keywords)


EQ_METHODS = {
    "assertEqual": "eq", "assertEquals": "eq", "assertListEqual": "eq", "assertDictEqual": "eq",
    "assertTupleEqual": "eq", "assertSetEqual": "eq", "assertSequenceEqual": "eq", "assertMultiLineEqual": "eq",
    "assertNotEqual": "ne", "assertNotEquals": "ne", "assertAlmostEqual": "eq~", "assertIs": "eq",
    "assert_equal": "eq", "assert_array_equal": "eq", "assert_allclose": "eq~", "assert_almost_equal": "eq~",
    "assert_array_almost_equal": "eq~",
}
MOCK_HINTS = ("monkeypatch", "mock", "patch", "mocker", "freeze_time", "freezer")


class _PyCollector(ast.NodeVisitor):
    def __init__(self, path: str, src: str, aliases: Dict[str, str], source: str):
        self.path = path
        self.lines = src.splitlines()
        self.aliases = aliases
        self.source = source
        self.out: List[Assertion] = []
        self.stack: List[str] = []
        self.func_params: List[bool] = []
        self.func_mocks: List[bool] = []
        self.stmt_counter = 0
        self.bindings: Dict[str, ast.AST] = {}
        self.module_names: set = set()

    # scope tracking -------------------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_func(self, node: Any) -> None:
        self.stack.append(node.name)
        params = [a.arg for a in node.args.args + node.args.kwonlyargs if a.arg not in ("self", "cls")]
        callable_params = {p for p in params if p in ("candidate", "func", "fn", "solution", "f")}
        has_fixtures = bool(set(params) - callable_params)
        body_src = "\n".join(self.lines[node.lineno - 1: getattr(node, "end_lineno", node.lineno)])
        mocks = any(h in body_src for h in MOCK_HINTS) or any(
            "patch" in _unparse(d) or "parametrize" in _unparse(d) for d in node.decorator_list)
        self.func_params.append(has_fixtures)
        self.func_mocks.append(mocks)
        saved = self.bindings
        self.bindings = {}
        for i, stmt in enumerate(node.body):
            self.stmt_counter = i
            self.visit(stmt)
            # light dataflow: `result = f(1, 2)` followed by `assert result == 3`
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and (isinstance(sub.ctx, (ast.Store, ast.Del)) or not isinstance(stmt, ast.Assert)):
                    self.bindings.pop(sub.id, None)  # any non-assert use may mutate the value
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    and _is_pure_call(stmt.value, self.module_names)):
                self.bindings[stmt.targets[0].id] = stmt.value
        self.bindings = saved
        self.func_params.pop()
        self.func_mocks.pop()
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    # helpers -------------------------------------------------------------------
    def _add(self, node: ast.AST, kind: str, subject: ast.AST, expected: Optional[ast.AST],
             approx: bool = False, expected_value: Any = MISSING, expected_src: str = "") -> None:
        if expected is not None:
            approx_inner = None
            if isinstance(expected, ast.Call) and _callee_name(expected.func) == "approx" and expected.args:
                approx_inner = expected.args[0]
            if approx_inner is not None:
                approx = True
                expected_value = _literal(approx_inner)
            else:
                expected_value = _literal(expected)
            expected_src = expected_src or _unparse(expected)
        ln = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", ln) or ln
        text = "\n".join(self.lines[ln - 1:end]).strip() if ln else ""
        subject_src = _unparse(subject)
        if isinstance(subject, ast.Name) and subject.id in self.bindings:
            subject = self.bindings[subject.id]
            subject_src = "%s (= %s)" % (subject_src, _unparse(subject))
        a = Assertion(
            file=self.path, line=ln, lang="py", kind=kind,
            subject=canon_py(subject, self.aliases), subject_src=subject_src,
            expected_src=expected_src, test=".".join(self.stack), pure=_is_pure_call(subject, self.module_names),
            approx=approx, stmt_index=self.stmt_counter, source=self.source, text=text,
            func_has_params=bool(self.func_params and self.func_params[-1]),
            func_mocks=bool(self.func_mocks and self.func_mocks[-1]),
        )
        a.expected = expected_value
        self.out.append(a)

    # assertions ----------------------------------------------------------------
    def visit_Assert(self, node: ast.Assert) -> None:
        t = node.test
        if isinstance(t, ast.Compare) and len(t.ops) == 1:
            left, right, op = t.left, t.comparators[0], t.ops[0]
            if _literal(left) is not MISSING and _literal(right) is MISSING:
                left, right = right, left
            if isinstance(op, ast.Eq):
                self._add(node, "eq", left, right)
            elif isinstance(op, ast.NotEq):
                self._add(node, "ne", left, right)
            elif isinstance(op, ast.Is) and _literal(right) is not MISSING:
                self._add(node, "eq", left, right)
            elif isinstance(op, ast.IsNot) and _literal(right) is not MISSING:
                self._add(node, "ne", left, right)
        elif isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
            self._add(node, "truth", t.operand, None, expected_value=False, expected_src="falsy")
        elif isinstance(t, (ast.Call, ast.Name, ast.Attribute, ast.Subscript)):
            self._add(node, "truth", t, None, expected_value=True, expected_src="truthy")
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        c = node.value
        if isinstance(c, ast.Call):
            name = _callee_name(c.func)
            mode = EQ_METHODS.get(name)
            if mode and len(c.args) >= 2:
                a0, a1 = c.args[0], c.args[1]
                if _literal(a0) is not MISSING and _literal(a1) is MISSING:
                    a0, a1 = a1, a0
                self._add(node, "ne" if mode == "ne" else "eq", a0, a1, approx=mode.endswith("~"))
            elif name in ("assertTrue", "assertFalse") and c.args:
                self._add(node, "truth", c.args[0], None, expected_value=(name == "assertTrue"),
                          expected_src="truthy" if name == "assertTrue" else "falsy")
            elif name == "assertIsNone" and c.args:
                self._add(node, "eq", c.args[0], ast.Constant(value=None))
            elif name == "assertRaises" and len(c.args) >= 2 and isinstance(c.args[1], (ast.Name, ast.Attribute)):
                fake = ast.Call(func=c.args[1], args=list(c.args[2:]), keywords=list(c.keywords))
                ast.copy_location(fake, c)
                self._add(node, "raises", fake, None, expected_src=_unparse(c.args[0]))
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        exc = None
        for item in node.items:
            ce = item.context_expr
            if isinstance(ce, ast.Call) and _callee_name(ce.func) in ("raises", "assertRaises", "assertRaisesRegex") and ce.args:
                exc = _unparse(ce.args[0])
                for kw in ce.keywords:
                    if kw.arg == "match":
                        exc += " match=" + _unparse(kw.value)
        if exc is not None:
            for stmt in node.body:
                target = None
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Call, ast.Attribute, ast.Subscript)):
                    target = stmt.value
                elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Call, ast.Subscript)):
                    target = stmt.value
                if target is not None:
                    self._add(stmt, "raises", target, None, expected_src=exc)
        self.generic_visit(node)


def extract_py(path: str, src: str, aliases: Optional[Dict[str, str]] = None, source: str = "test") -> Tuple[List[Assertion], Optional[str]]:
    aliases = aliases or {}
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return extract_lines(path, src, "py", aliases, source), "syntax error line %s: %s" % (e.lineno, e.msg)
    col = _PyCollector(path, src, aliases, source)
    col.module_names = _module_names(tree)
    col.visit(tree)
    return col.out, None


_LINE_ASSERT = re.compile(r"^\s*assert\s+(?P<l>.+?)\s*(?P<op>==|!=)\s*(?P<r>.+?)\s*(#.*)?$")


def extract_lines(path: str, src: str, lang: str, aliases: Dict[str, str], source: str) -> List[Assertion]:
    """Fallback for files that do not parse: single-line `assert X == Y` statements."""
    out = []
    if lang != "py":
        return out
    for i, line in enumerate(src.splitlines(), 1):
        m = _LINE_ASSERT.match(line)
        if not m:
            continue
        try:
            l_node = ast.parse(m.group("l"), mode="eval").body
            r_node = ast.parse(m.group("r"), mode="eval").body
        except SyntaxError:
            continue
        if _literal(l_node) is not MISSING and _literal(r_node) is MISSING:
            l_node, r_node = r_node, l_node
        a = Assertion(file=path, line=i, lang="py", kind="eq" if m.group("op") == "==" else "ne",
                      subject=canon_py(l_node, aliases), subject_src=m.group("l"), expected_src=m.group("r"),
                      test="<unparsed>", pure=_is_pure_call(l_node), source=source, text=line.strip())
        a.expected = _literal(r_node)
        out.append(a)
    return out


# --------------------------------------------------------------------------------------
# JavaScript / TypeScript extraction (jest, vitest, node:assert)
# --------------------------------------------------------------------------------------

class _JSLitParser:
    """Tiny parser for JS literals: numbers, strings, arrays, objects, true/false/null/undefined."""

    def __init__(self, s: str):
        self.s = s
        self.i = 0

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def parse(self) -> Any:
        self.ws()
        v = self.value()
        self.ws()
        if self.i != len(self.s):
            raise ValueError("trailing")
        return v

    def value(self) -> Any:
        self.ws()
        if self.i >= len(self.s):
            raise ValueError("eof")
        c = self.s[self.i]
        if c in "\"'`":
            return self.string()
        if c == "[":
            self.i += 1
            arr = []
            while True:
                self.ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
                elif self.s[self.i] != "]":
                    raise ValueError("array")
        if c == "{":
            self.i += 1
            obj = {}
            while True:
                self.ws()
                if self.s[self.i] == "}":
                    self.i += 1
                    return obj
                if self.s[self.i] in "\"'":
                    k = self.string()
                else:
                    m = re.match(r"[A-Za-z_$][\w$]*", self.s[self.i:])
                    if not m:
                        raise ValueError("key")
                    k = m.group(0)
                    self.i += len(k)
                self.ws()
                if self.s[self.i] != ":":
                    raise ValueError("colon")
                self.i += 1
                obj[k] = self.value()
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
                elif self.s[self.i] != "}":
                    raise ValueError("object")
        m = re.match(r"-?(?:\d[\d_]*\.?\d*(?:[eE][+-]?\d+)?|\.\d+)n?", self.s[self.i:])
        if m and m.group(0) not in ("-", ""):
            tok = m.group(0)
            self.i += len(tok)
            tok = tok.replace("_", "").rstrip("n")
            return float(tok) if any(ch in tok for ch in ".eE") else int(tok)
        for word, val in (("true", True), ("false", False), ("null", None), ("undefined", UNDEFINED),
                          ("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))):
            if self.s.startswith(word, self.i) and not re.match(r"[\w$]", self.s[self.i + len(word): self.i + len(word) + 1] or " "):
                self.i += len(word)
                return val
        raise ValueError("unknown token at %d" % self.i)

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                nxt = self.s[self.i + 1: self.i + 2]
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                self.i += 2
                continue
            if q == "`" and c == "$" and self.s[self.i + 1: self.i + 2] == "{":
                raise ValueError("template interpolation")
            if c == q:
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise ValueError("unterminated string")


def js_literal(s: str) -> Any:
    try:
        return _JSLitParser(s.strip()).parse()
    except (ValueError, IndexError):
        return MISSING


def _scan_balanced(s: str, start: int) -> int:
    """s[start] is an opening bracket; return index just past its match. Skips strings and comments."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[s[start]]]
    i = start + 1
    n = len(s)
    while i < n and stack:
        c = s[i]
        if c in "\"'`":
            q = c
            i += 1
            while i < n and s[i] != q:
                if s[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
        i += 1
    return i


def _split_args(s: str) -> List[str]:
    """Split a JS argument list at top-level commas."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in "\"'`":
            q = c
            j = i + 1
            while j < len(s) and s[j] != q:
                if s[j] == "\\":
                    j += 1
                j += 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


_JS_CALL = re.compile(r"^(?P<callee>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\((?P<args>.*)\)$", re.S)
_JS_TESTCALL = re.compile(r"(?<![\w$.])(?P<fn>it|test|describe|xit|xtest|xdescribe|fit|fdescribe)(?P<mods>(?:\s*\.\s*(?:skip|only|todo|each|fails|concurrent|skipIf|runIf|sequential))*)\s*\(")


def _strip_ws_outside_strings(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c in "\"'`":
            j = i + 1
            while j < len(s) and s[j] != c:
                if s[j] == "\\":
                    j += 1
                j += 1
            out.append(s[i:j + 1])
            i = j + 1
            continue
        if not c.isspace():
            out.append(c)
        i += 1
    return "".join(out)


def js_module_names(src: str) -> set:
    names = set(re.findall(r"import\s+\*\s+as\s+([A-Za-z_$][\w$]*)", src))
    names |= set(re.findall(r"import\s+([A-Za-z_$][\w$]*)\s*(?:,|\s+from)", src))
    names |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(", src))
    names |= {"Math", "JSON", "Number", "String", "Object", "Array"}
    return names


def canon_js(expr: str, aliases: Dict[str, str], module_names: Optional[set] = None) -> Tuple[str, bool]:
    e = expr.strip()
    e = re.sub(r"^await\s+", "", e)
    m = _JS_CALL.match(e)
    if m and _scan_balanced(e, e.index("(")) == len(e):
        chain = [p.strip() for p in m.group("callee").split(".")]
        args = _split_args(m.group("args"))
        vals = [js_literal(a) for a in args]
        literal = all(v is not MISSING for v in vals)
        name = aliases.get(chain[-1], chain[-1])
        if literal:
            key = "%s(%s)" % (name, ", ".join(repr(_py_from_js(v)) for v in vals))
        else:
            key = "%s(%s)" % (name, ", ".join(_strip_ws_outside_strings(a) for a in args))
        pure = literal and (len(chain) == 1 or (module_names is not None and chain[0] in module_names and bool(args)))
        return key, pure
    return _strip_ws_outside_strings(e), False


def _py_from_js(v: Any) -> Any:
    if isinstance(v, list):
        return [_py_from_js(x) for x in v]
    if isinstance(v, dict):
        return {k: _py_from_js(x) for k, x in v.items()}
    return v


_JS_MATCHERS_EQ = ("toBe", "toEqual", "toStrictEqual")


def extract_js(path: str, src: str, aliases: Optional[Dict[str, str]] = None, source: str = "test") -> Tuple[List[Assertion], Optional[str]]:
    aliases = aliases or {}
    out: List[Assertion] = []
    line_starts = [0]
    for m in re.finditer(r"\n", src):
        line_starts.append(m.end())

    def line_of(pos: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    # test spans
    spans: List[Tuple[int, int, str]] = []
    for m in _JS_TESTCALL.finditer(src):
        open_i = m.end() - 1
        end = _scan_balanced(src, open_i)
        args = _split_args(src[open_i + 1:end - 1])
        title = js_literal(args[0]) if args else MISSING
        spans.append((m.start(), end, title if isinstance(title, str) else "<dynamic>"))

    def test_of(pos: int) -> str:
        names = [(s, t) for (s, e, t) in spans if s <= pos < e]
        names.sort()
        return " > ".join(t for _, t in names)

    lines = src.splitlines()

    mods = js_module_names(src)

    def add(pos: int, end_pos: int, kind: str, subject_src: str, expected_src: str, expected: Any, approx: bool = False) -> None:
        key, pure = canon_js(subject_src, aliases, mods)
        ln = line_of(pos)
        ln_end = line_of(max(pos, end_pos - 1))
        a = Assertion(file=path, line=ln, lang="js", kind=kind, subject=key, subject_src=subject_src.strip(),
                      expected_src=expected_src.strip(), test=test_of(pos), pure=pure, approx=approx,
                      stmt_index=ln, source=source, text="\n".join(lines[ln - 1:ln_end]).strip())
        func_src = src[max(0, pos - 2000):pos]
        a.func_mocks = bool(re.search(r"\b(vi|jest)\.(mock|spyOn|fn|useFakeTimers)\b", func_src))
        a.expected = expected
        out.append(a)

    # expect(...).matcher(...)
    for m in re.finditer(r"(?<![\w$.])expect\s*\(", src):
        open_i = m.end() - 1
        close = _scan_balanced(src, open_i)
        subject = src[open_i + 1:close - 1]
        rest = src[close:]
        mm = re.match(r"\s*((?:\.\s*(?:not|resolves|rejects)\s*)*)\.\s*([A-Za-z]+)\s*\(", rest)
        if not mm:
            continue
        mods = mm.group(1).replace(" ", "")
        matcher = mm.group(2)
        m_open = close + mm.end() - 1
        m_close = _scan_balanced(src, m_open)
        arg = src[m_open + 1:m_close - 1].strip()
        negated = ".not" in mods
        subj_args = _split_args(subject)
        subj = subj_args[0] if subj_args else subject
        if matcher in ("toThrow", "toThrowError"):
            fm = re.match(r"^\s*(?:async\s*)?\(\s*\)\s*=>\s*(.*)$", subj, re.S) or re.match(r"^\s*(?:async\s+)?function\s*\(\s*\)\s*\{\s*(?:return\s+)?(.*?);?\s*\}\s*$", subj, re.S)
            if fm and not negated:
                body = fm.group(1).strip()
                if body.startswith("{"):
                    inner = body[1:-1].strip().rstrip(";")
                    inner = re.sub(r"^return\s+", "", inner)
                    body = inner
                add(m.start(), m_close, "raises", body, arg or "Error", MISSING)
            continue
        if matcher in _JS_MATCHERS_EQ or matcher == "toBeCloseTo":
            val = js_literal(arg) if arg else MISSING
            add(m.start(), m_close, "ne" if negated else "eq", subj, arg, val, approx=(matcher == "toBeCloseTo"))
        elif matcher in ("toBeTruthy", "toBeFalsy"):
            truth = (matcher == "toBeTruthy") != negated
            add(m.start(), m_close, "truth", subj, "truthy" if truth else "falsy", truth)
        elif matcher in ("toBeNull", "toBeUndefined"):
            val = None if matcher == "toBeNull" else UNDEFINED
            add(m.start(), m_close, "ne" if negated else "eq", subj, matcher[4:].lower(), val)

    # node:assert style
    for m in re.finditer(r"(?<![\w$])assert\s*\.\s*(equal|strictEqual|deepEqual|deepStrictEqual|notEqual|notStrictEqual|throws|ok)\s*\(", src):
        open_i = m.end() - 1
        close = _scan_balanced(src, open_i)
        args = _split_args(src[open_i + 1:close - 1])
        fn = m.group(1)
        if fn == "throws" and args:
            fm = re.match(r"^\s*\(\s*\)\s*=>\s*(.*)$", args[0], re.S)
            if fm:
                add(m.start(), close, "raises", fm.group(1).strip().strip("{}").strip().rstrip(";"), args[1] if len(args) > 1 else "Error", MISSING)
        elif fn == "ok" and args:
            add(m.start(), close, "truth", args[0], "truthy", True)
        elif len(args) >= 2:
            a0, a1 = args[0], args[1]
            if js_literal(a0) is not MISSING and js_literal(a1) is MISSING:
                a0, a1 = a1, a0
            add(m.start(), close, "ne" if fn.startswith("not") else "eq", a0, a1, js_literal(a1))
    return out, None


def extract(path: str, src: str, aliases: Optional[Dict[str, str]] = None, source: str = "test") -> Tuple[List[Assertion], Optional[str]]:
    lang = lang_of(path)
    if lang == "py":
        return extract_py(path, src, aliases, source)
    if lang == "js":
        return extract_js(path, src, aliases, source)
    return [], None


# --------------------------------------------------------------------------------------
# Spec extraction (issue text, README, docstrings, JSDoc)
# --------------------------------------------------------------------------------------

_SPEC_ARROW = re.compile(
    r"(?P<call>[A-Za-z_][\w.]*\s*\((?:[^()]|\([^()]*\))*\))\s*"
    r"(?:==|===|->|=>|→|⇒|returns|should\s+return|should\s+be|gives|yields|evaluates\s+to|is)\s+"
    r"(?P<val>.+?)\s*[.,;]?\s*$")
_SPEC_RAISE = re.compile(
    r"(?P<call>[A-Za-z_][\w.]*\s*\((?:[^()]|\([^()]*\))*\))\s*(?:should\s+)?(?:raises?|throws?)\s+(?:an?\s+)?`?(?P<exc>[A-Za-z_][\w.]*)")


_CURRENT_BEHAVIOUR = re.compile(
    r"\b(currently|right now|at the moment|actual(ly)?|instead|incorrectly|wrongly|unfortunately|observed|"
    r"but (it )?(got|gets|returns|gives|yields)|now (returns|gives|yields)|used to|before (this|the) (fix|change)|regression)\b", re.I)


def _spec_value(val: str) -> Tuple[Any, str]:
    v = val.strip().strip("`").strip()
    v = re.sub(r"\s*(#|//).*$", "", v)
    try:
        return ast.literal_eval(v), v
    except Exception:
        pass
    j = js_literal(v)
    if j is not MISSING:
        return _py_from_js(j), v
    return MISSING, v


def _spec_subject(call: str, aliases: Dict[str, str]) -> Optional[Tuple[str, bool, str]]:
    call = call.strip().strip("`")
    try:
        node = ast.parse(call, mode="eval").body
        return canon_py(node, aliases), _is_pure_call(node, _ANY_ROOT), call
    except SyntaxError:
        key, pure = canon_js(call, aliases, _ANY_ROOT)
        if "(" in key:
            return key, pure, call
    return None


def extract_spec_text(path: str, text: str, aliases: Optional[Dict[str, str]] = None, line_offset: int = 0) -> List[Assertion]:
    aliases = aliases or {}
    out: List[Assertion] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip().strip("`").strip()
        if not line.startswith(">>>"):
            line = re.sub(r"^(?:[-*+]|>|\d+[.)])\s+", "", line)
        ln = i + 1 + line_offset
        if line.startswith(">>>"):
            expr = line[3:].strip()
            j = i + 1
            outs = []
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("..."):
                    expr += " " + nxt[3:].strip()
                    j += 1
                    continue
                break
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith(">>>") and not lines[j].strip().startswith("```"):
                outs.append(lines[j].strip())
                j += 1
            subj = _spec_subject(expr, aliases)
            if subj and outs:
                key, pure, src_call = subj
                out_text = " ".join(outs)
                tb = re.match(r"Traceback|(?P<exc>[A-Za-z_]\w*(?:Error|Exception))\b", out_text)
                if out_text.startswith("Traceback"):
                    excm = re.search(r"([A-Za-z_]\w*(?:Error|Exception))", " ".join(outs[1:]) or out_text)
                    a = Assertion(file=path, line=ln, lang="spec", kind="raises", subject=key, subject_src=src_call,
                                  expected_src=excm.group(1) if excm else "Exception", pure=pure, source="spec",
                                  text=(raw.strip() + " / " + out_text)[:200])
                    out.append(a)
                elif not (tb and tb.group("exc")):
                    val, vsrc = _spec_value(out_text)
                    a = Assertion(file=path, line=ln, lang="spec", kind="eq", subject=key, subject_src=src_call,
                                  expected_src=vsrc, pure=pure, source="spec", text=(raw.strip() + " -> " + out_text)[:200])
                    a.expected = val
                    out.append(a)
            i = j
            continue
        line = line.replace("`", "")  # markdown inline code around calls and values
        if _CURRENT_BEHAVIOUR.search(line):
            i += 1  # describes today's (buggy) behaviour, not the spec
            continue
        m = _SPEC_RAISE.search(line)
        if m:
            subj = _spec_subject(m.group("call"), aliases)
            if subj:
                key, pure, src_call = subj
                out.append(Assertion(file=path, line=ln, lang="spec", kind="raises", subject=key, subject_src=src_call,
                                     expected_src=m.group("exc"), pure=pure, source="spec", text=raw.strip()[:200]))
            i += 1
            continue
        m = _SPEC_ARROW.search(line)
        if m:
            subj = _spec_subject(m.group("call"), aliases)
            val, vsrc = _spec_value(m.group("val"))
            if subj and val is not MISSING:
                key, pure, src_call = subj
                a = Assertion(file=path, line=ln, lang="spec", kind="eq", subject=key, subject_src=src_call,
                              expected_src=vsrc, pure=pure, source="spec", text=raw.strip()[:200])
                a.expected = val
                out.append(a)
        i += 1
    return out


def extract_spec_from_source(path: str, src: str, aliases: Optional[Dict[str, str]] = None) -> List[Assertion]:
    """Docstring doctests (Python) and JSDoc @example lines (JS/TS)."""
    aliases = aliases or {}
    out: List[Assertion] = []
    if path.endswith(".py"):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return out
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                doc = ast.get_docstring(node, clean=False)
                if doc and ">>>" in doc:
                    body = getattr(node, "body", [])
                    first = body[0].lineno if body else 1
                    out.extend(extract_spec_text(path, doc, aliases, line_offset=first - 1))
    elif path.endswith(JS_EXTS):
        for m in re.finditer(r"/\*\*(.*?)\*/", src, re.S):
            block = m.group(1)
            if "@example" not in block and "=>" not in block:
                continue
            start_line = src.count("\n", 0, m.start())
            cleaned = "\n".join(re.sub(r"^\s*\*\s?", "", l) for l in block.splitlines())
            cleaned = re.sub(r"//\s*=>", "=>", cleaned)
            out.extend(extract_spec_text(path, cleaned, aliases, line_offset=start_line))
    return out


# --------------------------------------------------------------------------------------
# Contradiction analysis
# --------------------------------------------------------------------------------------

@dataclass
class Contradiction:
    kind: str  # test-vs-test | spec-vs-test
    strength: str  # strong | weak
    a: Assertion
    b: Assertion
    reason: str
    repro: str = ""

    def public(self) -> Dict[str, Any]:
        return {"kind": self.kind, "strength": self.strength, "reason": self.reason,
                "a": self.a.public(), "b": self.b.public(), "repro": self.repro}


def _pair_strength(a: Assertion, b: Assertion) -> str:
    if a.kind == "raises" or b.kind == "raises":
        # call-must-raise vs call-must-return is strong only for deterministic calls
        pass
    if not (a.pure and b.pure):
        return "weak"
    if a.source == "spec" or b.source == "spec":
        return "strong"
    if a.test == b.test:
        return "weak" if a.func_mocks else "strong"
    # different tests: fixtures, parametrisation or mocks could legitimately change behaviour
    if a.func_has_params or b.func_has_params or a.func_mocks or b.func_mocks:
        return "weak"
    return "strong"


def find_contradictions(tests: List[Assertion], specs: List[Assertion]) -> List[Contradiction]:
    out: List[Contradiction] = []
    by_subject: Dict[str, List[Assertion]] = {}
    for a in tests:
        by_subject.setdefault(a.subject, []).append(a)
    seen = set()
    for subj, group in by_subject.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                reason = values_conflict(a, b)
                if not reason:
                    continue
                key = (a.subject, a.expected_src, b.expected_src, a.kind, b.kind)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Contradiction("test-vs-test", _pair_strength(a, b), a, b, reason))
    for s in specs:
        for t in by_subject.get(s.subject, []):
            reason = values_conflict(s, t)
            if reason:
                key = ("spec", s.file, s.line, t.file, t.line)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Contradiction("spec-vs-test", _pair_strength(s, t), s, t, reason))
    out.sort(key=lambda c: (c.strength != "strong", c.kind != "spec-vs-test", c.b.file, c.b.line))
    return out


def make_repro(c: Contradiction) -> str:
    """A minimal, runnable demonstration that both expectations cannot hold.
    It does not import project code: it shows that the two required outcomes are distinct."""
    a, b = c.a, c.b

    def desc(x: Assertion) -> str:
        where = "%s:%d" % (x.file, x.line)
        return "%s  [%s]  %s" % (where, x.source, _short(x.text or x.subject_src, 110))

    lines = [
        "# Catch-22 minimal repro (generated). Runs with plain python3, imports nothing from the project.",
        "# A: " + desc(a),
        "# B: " + desc(b),
        "# Both constrain the same expression: %s" % _short(a.subject_src, 100),
    ]
    if a.kind == "raises" or b.kind == "raises":
        r = a if a.kind == "raises" else b
        o = b if r is a else a
        lines += [
            "outcome_a = %r" % ("raises " + r.expected_src if r is a else "returns " + o.expected_src),
            "outcome_b = %r" % ("raises " + r.expected_src if r is b else "returns " + o.expected_src),
            "print('A requires:', outcome_a)",
            "print('B requires:', outcome_b)",
            "print('One call cannot both raise and return. Contradiction confirmed.')",
            "raise SystemExit(0)",
        ]
    else:
        def lit(x: Assertion) -> str:
            if x.kind == "truth":
                return "True" if x.expected else "False"
            return repr(x.expected)

        cmp_a = {"eq": "result == A", "ne": "result != A", "truth": "bool(result) == A"}[a.kind]
        cmp_b = {"eq": "result == B", "ne": "result != B", "truth": "bool(result) == B"}[b.kind]
        lines += [
            "A = %s" % lit(a),
            "B = %s" % lit(b),
            "def satisfies_both(result):",
            "    return (%s) and (%s)" % (cmp_a, cmp_b),
            "candidates = [A, B]",
            "ok = [r for r in candidates if satisfies_both(r)]",
            "print('A requires', %r, repr(A))" % a.kind,
            "print('B requires', %r, repr(B))" % b.kind,
            "print('values satisfying both among the two required outcomes:', ok)",
            "if ok:",
            "    print('Not a contradiction after all.'); raise SystemExit(1)",
            "print('No single return value satisfies both. Contradiction confirmed.')",
            "raise SystemExit(0)",
        ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Running the test command
# --------------------------------------------------------------------------------------

_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(?P<id>\S+?)(?:\s+-\s+.*)?$", re.M)
_JEST_BULLET = re.compile(r"^\s*●\s+(?P<name>.+?)\s*$", re.M)
_VITEST_FAIL = re.compile(r"^\s*(?:FAIL|×|✕|✗)\s+(?P<name>.+?)\s*(?:\d+\s*ms)?\s*$", re.M)


def run_tests(cmd: str, root: str, timeout: int) -> Tuple[int, str, List[str]]:
    try:
        p = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        out = str(e.stdout or "") + "\nTIMEOUT"
        rc = 124
    failing = parse_failing(out)
    return rc, out, failing


def parse_failing(out: str) -> List[str]:
    names: List[str] = []
    clean = re.sub(r"\x1b\[[0-9;]*m", "", out)
    for m in _PYTEST_FAIL.finditer(clean):
        names.append(m.group("id"))
    for m in _JEST_BULLET.finditer(clean):
        n = m.group("name")
        if not n.lower().startswith(("console.", "test suite failed")):
            names.append(n)
    for m in _VITEST_FAIL.finditer(clean):
        names.append(m.group("name"))
    seen, res = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res


def _test_matches_failing(test: str, file: str, failing: Sequence[str]) -> bool:
    if not failing:
        return False
    tparts = [p for p in re.split(r"[.\s>›:]+", test) if p]
    tlast = test.split(".")[-1].split(" > ")[-1].strip()
    for f in failing:
        fid = re.sub(r"\[.*\]$", "", f)
        if "::" in fid:
            fpath, _, fname = fid.partition("::")
            fname_last = fname.split("::")[-1]
            if fname_last == tlast and (norm_path(fpath).endswith(norm_path(file)) or norm_path(file).endswith(norm_path(fpath))):
                return True
            continue
        fl = fid.strip()
        if tlast and (fl == tlast or fl.endswith(" " + tlast) or fl.endswith("> " + tlast) or fl.endswith("› " + tlast) or tlast in fl.split(" › ")):
            return True
        if tparts and all(p in fl for p in tparts[-1:]):
            return True
    return False


# --------------------------------------------------------------------------------------
# Tamper detection
# --------------------------------------------------------------------------------------

@dataclass
class Finding:
    severity: str  # error | warn
    code: str
    file: str
    line: int
    message: str
    before: str = ""
    after: str = ""

    def public(self) -> Dict[str, Any]:
        return asdict(self)


SKIP_PY = re.compile(r"(pytest\.mark\.(skip|skipif|xfail)|unittest\.(skip|skipIf|skipUnless|expectedFailure)|@skip\b|pytest\.(skip|xfail|importorskip)\s*\(|self\.skipTest\s*\(|raise\s+(unittest\.)?SkipTest)")
SKIP_JS = re.compile(r"(?<![\w$])(?:(?:it|test|describe)\s*\.\s*(?:skip|todo|fails|skipIf|only)\b|xit\s*\(|xtest\s*\(|xdescribe\s*\(|fit\s*\(|fdescribe\s*\()")


def _py_tests(src: str) -> Optional[Dict[str, Dict[str, Any]]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    lines = src.splitlines()
    res: Dict[str, Dict[str, Any]] = {}

    def visit(body: List[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.name.startswith("test") or node.name == "check"):
                end = getattr(node, "end_lineno", node.lineno)
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                seg = "\n".join(lines[start - 1:end])
                res[prefix + node.name] = {
                    "line": node.lineno,
                    "decorators": [_unparse(d) for d in node.decorator_list],
                    "src": seg,
                    "body_dump": ast.dump(ast.Module(body=node.body, type_ignores=[])),
                    "swallow": _has_swallow(node),
                }
    visit(tree.body, "")
    return res


def _has_swallow(node: ast.AST) -> int:
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Try):
            for h in sub.handlers:
                broad = h.type is None or _unparse(h.type) in ("Exception", "BaseException", "AssertionError")
                if broad and all(isinstance(s, (ast.Pass, ast.Continue)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in h.body):
                    n += 1
    return n


def _js_tests(src: str) -> Dict[str, Dict[str, Any]]:
    res: Dict[str, Dict[str, Any]] = {}
    for m in _JS_TESTCALL.finditer(src):
        if m.group("fn") in ("describe", "xdescribe", "fdescribe"):
            continue
        open_i = m.end() - 1
        end = _scan_balanced(src, open_i)
        args = _split_args(src[open_i + 1:end - 1])
        title = js_literal(args[0]) if args else MISSING
        name = title if isinstance(title, str) else "<dynamic@%d>" % m.start()
        res[name] = {"line": src.count("\n", 0, m.start()) + 1, "mods": m.group("mods").replace(" ", ""),
                     "fn": m.group("fn"), "src": src[m.start():end]}
    return res


def _assert_index(asserts: List[Assertion]) -> Dict[Tuple[str, str], List[Assertion]]:
    idx: Dict[Tuple[str, str], List[Assertion]] = {}
    for a in asserts:
        idx.setdefault((a.test, a.subject), []).append(a)
    return idx


def _norm_expr(s: str) -> str:
    return re.sub(r"\s+", "", s).replace('"', "'")


def diff_test_file(path: str, before: Optional[str], after: Optional[str]) -> List[Finding]:
    """Compare one test file between base and working tree."""
    out: List[Finding] = []
    if before is None:
        return out  # new test file: adding tests is fine
    if after is None:
        n = len(_py_tests(before) or {}) if path.endswith(".py") else len(_js_tests(before))
        out.append(Finding("error", "test-file-deleted", path, 0, "test file deleted (%d tests removed)" % n))
        return out
    if before == after:
        return out
    lang = lang_of(path)
    # 1. tests removed / skipped
    if lang == "py":
        tb, ta = _py_tests(before), _py_tests(after)
        if tb is not None and ta is not None:
            after_bodies = {v["body_dump"]: k for k, v in ta.items()}
            for name, info in tb.items():
                if name not in ta:
                    if info["body_dump"] in after_bodies:
                        continue  # renamed, same body
                    out.append(Finding("error", "test-deleted", path, info["line"], "test `%s` was removed" % name, before=_short(info["src"], 200)))
                    continue
                new = ta[name]
                added_dec = [d for d in new["decorators"] if d not in info["decorators"] and SKIP_PY.search("@" + d)]
                for d in added_dec:
                    out.append(Finding("error", "skip-added", path, new["line"], "skip/xfail marker added to `%s`: @%s" % (name, d), after="@" + d))
                if SKIP_PY.search(new["src"]) and not SKIP_PY.search(info["src"]) and not added_dec:
                    out.append(Finding("error", "skip-added", path, new["line"], "skip/xfail call added inside `%s`" % name))
                if new["swallow"] > info["swallow"]:
                    out.append(Finding("error", "assert-swallowed", path, new["line"], "`%s` now catches and ignores exceptions/assertion errors (try/except: pass)" % name))
        mod_b = re.findall(r"^pytestmark\s*=.*$", before, re.M)
        mod_a = re.findall(r"^pytestmark\s*=.*$", after, re.M)
        for line in mod_a:
            if line not in mod_b and SKIP_PY.search(line):
                out.append(Finding("error", "skip-added", path, 0, "module-level skip added: " + line.strip(), after=line.strip()))
    elif lang == "js":
        tb, ta = _js_tests(before), _js_tests(after)
        for name, info in tb.items():
            if name not in ta:
                out.append(Finding("error", "test-deleted", path, info["line"], "test `%s` was removed" % name, before=_short(info["src"], 200)))
                continue
            new = ta[name]
            if (new["mods"] != info["mods"] and re.search(r"\.(skip|todo|fails|skipIf|only)", new["mods"])) or (new["fn"] != info["fn"] and new["fn"] in ("xit", "xtest", "fit")):
                out.append(Finding("error", "skip-added", path, new["line"], "test `%s` changed to %s%s" % (name, new["fn"], new["mods"]), after=new["fn"] + new["mods"]))
        # describe-level skip / only
        for m in re.finditer(r"(?<![\w$])(?:describe\s*\.\s*(?:skip|only|todo)|xdescribe|fdescribe)\s*\(", after):
            seg = after[m.start():m.start() + 80]
            if seg.split("(")[0] not in before:
                out.append(Finding("error", "skip-added", path, after.count("\n", 0, m.start()) + 1, "suite-level skip/only added: " + seg.split("(")[0].strip()))
    # 2. assertions changed, removed or loosened
    ab, _ = extract(path, before)
    aa, _ = extract(path, after)
    ib, ia = _assert_index(ab), _assert_index(aa)
    for key, olds in ib.items():
        news = ia.get(key, [])
        old_set = [(_norm_expr(o.expected_src), o.kind) for o in olds]
        new_set = [(_norm_expr(n.expected_src), n.kind) for n in news]
        if not news:
            test_alive = any(k[0] == key[0] for k in ia) or not key[0]
            if test_alive:
                o = olds[0]
                out.append(Finding("error", "assert-removed", path, o.line,
                                   "assertion on `%s` removed from `%s`" % (_short(o.subject_src), o.test or "<module>"), before=o.text))
            continue
        for o in olds:
            if (_norm_expr(o.expected_src), o.kind) in new_set:
                continue
            changed = [n for n in news if (_norm_expr(n.expected_src), n.kind) not in old_set]
            if changed:
                n = changed[0]
                if n.kind != o.kind:
                    out.append(Finding("error", "assert-loosened" if n.kind in ("truth", "ne") else "assert-edited", path, n.line,
                                       "assertion on `%s` changed from %s to %s" % (_short(o.subject_src), o.kind, n.kind), before=o.text, after=n.text))
                else:
                    out.append(Finding("error", "expected-changed", path, n.line,
                                       "expected value for `%s` changed: %s -> %s" % (_short(o.subject_src), _short(o.expected_src), _short(n.expected_src)),
                                       before=o.text, after=n.text))
            elif len(news) < len(olds):
                out.append(Finding("error", "assert-removed", path, o.line,
                                   "assertion `%s` removed" % _short(o.text or o.subject_src), before=o.text))
    # 3. raw line-level safety net: assertion lines commented out or replaced by trivial ones
    triv_py = re.compile(r"^\s*assert\s+(True|1|not\s+False)\s*(#.*)?$")
    triv_js = re.compile(r"expect\s*\(\s*(true|1)\s*\)\s*\.\s*toBe\s*\(\s*(true|1)\s*\)")
    b_lines, a_lines = set(before.splitlines()), after.splitlines()
    for i, l in enumerate(a_lines, 1):
        if l in b_lines:
            continue
        s = l.strip()
        if triv_py.match(l) or triv_js.search(l):
            out.append(Finding("error", "assert-loosened", path, i, "trivially-true assertion added: " + s, after=s))
        elif re.match(r"^(#|//)\s*(assert\b|expect\s*\(|self\.assert)", s):
            uncommented = re.sub(r"^(#|//)\s*", "", s)
            if any(uncommented in bl for bl in b_lines):
                out.append(Finding("error", "assert-removed", path, i, "assertion commented out: " + s, after=s))
    # de-duplicate
    uniq, seen = [], set()
    for f in out:
        k = (f.code, f.file, f.line, f.message)
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return uniq


def diff_config_file(path: str, before: Optional[str], after: Optional[str]) -> List[Finding]:
    if before == after or before is None and after is None:
        return []
    if before is None:
        if norm_path(path).endswith("conftest.py") and after and re.search(r"collect_ignore|pytest_collection_modifyitems|skip", after):
            return [Finding("error", "test-config-changed", path, 0, "new conftest.py that ignores or skips tests")]
        return []
    if after is None:
        return [Finding("warn", "test-config-changed", path, 0, "test configuration file deleted")]
    b, a = before.splitlines(), after.splitlines()
    added = [l for l in a if l not in set(b)]
    removed = [l for l in b if l not in set(a)]
    base = norm_path(path).rsplit("/", 1)[-1]
    interesting = re.compile(r"collect_ignore|testpaths|addopts|--ignore|--deselect|-k\s|norecursedirs|python_files|testMatch|testPathIgnorePatterns|exclude|include|skip|xfail|\"test\"\s*:|passWithNoTests|bail|deselect|pytest_collection_modifyitems")
    hits = [l for l in added + removed if interesting.search(l)]
    if base == "package.json" or base in ("pyproject.toml", "setup.cfg", "tox.ini"):
        if not hits:
            return []
    if not added and not removed:
        return []
    sev = "error" if hits else "warn"
    msg = "test configuration changed" + (": " + "; ".join(_short(h.strip(), 80) for h in hits[:3]) if hits else "")
    return [Finding(sev, "test-config-changed", path, 0, msg, before="\n".join(removed[:5]), after="\n".join(added[:5]))]


RED_FLAGS = [
    (re.compile(r"PYTEST_CURRENT_TEST|['\"]pytest['\"]\s+in\s+sys\.modules|JEST_WORKER_ID|process\.env\.VITEST\b|sys\._getframe|inspect\.stack\(\)"),
     "test-env-sniffing", "implementation inspects the test runner or call stack"),
]


def scan_impl_file(path: str, before: Optional[str], after: Optional[str], test_asserts: List[Assertion]) -> List[Finding]:
    """Heuristics for the non-test cheats ImpossibleBench documents (operator overloading,
    special-casing, test sniffing). Warnings, never proof."""
    if after is None:
        return []
    b_lines = set((before or "").splitlines())
    added = [(i, l) for i, l in enumerate(after.splitlines(), 1) if l not in b_lines]
    if not added:
        return []
    out: List[Finding] = []
    for i, l in added:
        for rx, code, msg in RED_FLAGS:
            if rx.search(l):
                out.append(Finding("warn", code, path, i, msg + ": " + l.strip(), after=l.strip()))
    # __eq__ that always returns True
    if path.endswith(".py"):
        try:
            tree = ast.parse(after)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "__eq__":
                    rets = [r for r in ast.walk(node) if isinstance(r, ast.Return)]
                    if any(isinstance(r.value, ast.Constant) and r.value.value is True for r in rets):
                        seg = "\n".join(after.splitlines()[node.lineno - 1:getattr(node, "end_lineno", node.lineno)])
                        if seg not in (before or ""):
                            out.append(Finding("warn", "eq-overload", path, node.lineno, "__eq__ can return True unconditionally (comparison-operator overloading)"))
        except SyntaxError:
            pass
    elif re.search(r"\.(m|c)?[jt]sx?$", path):
        for i, l in added:
            if re.search(r"(equals|valueOf|\[Symbol\.toPrimitive\])\s*\([^)]*\)\s*\{?\s*return\s+true", l):
                out.append(Finding("warn", "eq-overload", path, i, "equality hook returns true unconditionally: " + l.strip()))
    # special-casing: an added branch that contains the literal arguments of a test assertion
    probes = []
    for a in test_asserts:
        if not a.pure:
            continue
        m = re.match(r"^[^(]+\((.*)\)$", a.subject, re.S)
        if not m:
            continue
        args = [x.strip() for x in _split_args(m.group(1)) if x.strip()]
        sig = [x for x in args if x not in ("0", "1", "True", "False", "None", "''", '""', "[]", "{}", "-1", "true", "false")]
        if sig and sum(len(x) for x in sig) >= 3:
            probes.append((a, sig))
    for i, l in added:
        if not re.search(r"\b(if|elif|case|when|switch)\b|\?\s*|==", l):
            continue
        compact = _norm_expr(l)
        for a, sig in probes:
            if all(_norm_expr(s) in compact for s in sig):
                out.append(Finding("warn", "special-casing", path, i,
                                   "added branch matches the literal inputs of a test (%s:%d): %s" % (a.file, a.line, l.strip()), after=l.strip()))
                break
    return out


# --------------------------------------------------------------------------------------
# Git / baseline plumbing
# --------------------------------------------------------------------------------------

def _git(root: str, *args: str) -> Optional[str]:
    try:
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    return p.stdout


def in_git_repo(root: str) -> bool:
    out = _git(root, "rev-parse", "--is-inside-work-tree")
    return bool(out and out.strip() == "true")


def git_toplevel(root: str) -> str:
    out = _git(root, "rev-parse", "--show-toplevel")
    return out.strip() if out else root


class Baseline:
    """Where the "before" version of a file comes from: a c22 snapshot, or a git ref."""

    def __init__(self, root: str, ref: Optional[str] = None, force_git: bool = False):
        self.root = root
        self.snapshot = os.path.join(root, STATE_DIR, "baseline")
        self.manifest_path = os.path.join(self.snapshot, "manifest.json")
        self.ref = ref or "HEAD"
        self.mode = "none"
        if not force_git and not ref and os.path.exists(self.manifest_path):
            self.mode = "snapshot"
            with open(self.manifest_path) as fh:
                self.manifest = json.load(fh)
        elif in_git_repo(root) and _git(root, "rev-parse", "--verify", "--quiet", self.ref + "^{commit}") is not None:
            self.mode = "git"
            self.top = git_toplevel(root)
            self.prefix = norm_path(os.path.relpath(os.path.abspath(root), self.top))
            if self.prefix == ".":
                self.prefix = ""

    def describe(self) -> str:
        if self.mode == "snapshot":
            return "c22 baseline snapshot (%s)" % self.manifest.get("created", "?")
        if self.mode == "git":
            return "git %s" % self.ref
        return "none"

    def changed_files(self) -> List[Tuple[str, str]]:
        """List of (status, relpath) where status is A, M or D, relative to root."""
        res: List[Tuple[str, str]] = []
        if self.mode == "snapshot":
            files = self.manifest.get("files", {})
            current = {}
            for rel in walk_files(self.root):
                if is_test_file(rel) or is_config_file(rel) or lang_of(rel):
                    current[rel] = True
            for rel, digest in files.items():
                full = os.path.join(self.root, rel)
                if not os.path.exists(full):
                    res.append(("D", rel))
                else:
                    with open(full, "rb") as fh:
                        if hashlib.sha256(fh.read()).hexdigest() != digest:
                            res.append(("M", rel))
            for rel in current:
                if rel not in files:
                    res.append(("A", rel))
            return res
        if self.mode == "git":
            out = _git(self.root, "diff", "--name-status", "--no-renames", self.ref, "--", ".") or ""
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    st = parts[0][0]
                    rel = self._rel(parts[-1])
                    if rel is not None:
                        res.append(("A" if st == "A" else "D" if st == "D" else "M", rel))
            untracked = _git(self.root, "ls-files", "--others", "--exclude-standard", "--", ".") or ""
            for line in untracked.splitlines():
                rel = norm_path(line.strip())
                if rel and not rel.startswith(STATE_DIR + "/"):
                    res.append(("A", rel))
        return res

    def _rel(self, top_rel: str) -> Optional[str]:
        p = norm_path(top_rel)
        if self.prefix:
            if not p.startswith(self.prefix + "/"):
                return None
            return p[len(self.prefix) + 1:]
        return p

    def before(self, rel: str) -> Optional[str]:
        if self.mode == "snapshot":
            p = os.path.join(self.snapshot, "files", rel)
            return read_text(p) if os.path.exists(p) else None
        if self.mode == "git":
            spec = "%s:%s" % (self.ref, (self.prefix + "/" if self.prefix else "") + rel)
            try:
                p = subprocess.run(["git", "show", spec], cwd=self.root, capture_output=True)
            except OSError:
                return None
            if p.returncode != 0:
                return None
            return p.stdout.decode("utf-8", errors="replace")
        return None


def cmd_baseline(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.root)
    snap = os.path.join(root, STATE_DIR, "baseline")
    if os.path.exists(snap):
        shutil.rmtree(snap)
    os.makedirs(os.path.join(snap, "files"), exist_ok=True)
    with open(os.path.join(root, STATE_DIR, ".gitignore"), "w") as fh:
        fh.write("*\n")
    files: Dict[str, str] = {}
    for rel in walk_files(root):
        if not (is_test_file(rel, args.tests) or is_config_file(rel) or lang_of(rel)):
            continue
        full = os.path.join(root, rel)
        try:
            data = open(full, "rb").read()
        except OSError:
            continue
        if len(data) > 2_000_000:
            continue
        files[rel] = hashlib.sha256(data).hexdigest()
        dst = os.path.join(snap, "files", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as fh:
            fh.write(data)
    import datetime

    manifest = {"created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "version": VERSION, "files": files}
    with open(os.path.join(snap, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)
    n_tests = sum(1 for r in files if is_test_file(r, args.tests))
    print("c22: baseline saved to %s (%d files, %d test files)" % (os.path.relpath(snap, root), len(files), n_tests))
    return 0


def collect_tamper(root: str, ref: Optional[str], extra_globs: Sequence[str], use_git: bool = False) -> Tuple[List[Finding], str]:
    base = Baseline(root, ref, force_git=use_git)
    if base.mode == "none":
        return [], "none"
    findings: List[Finding] = []
    changed = base.changed_files()
    test_asserts_now: List[Assertion] = []
    for st, rel in changed:
        if is_test_file(rel, extra_globs) and st != "D":
            src = read_text(os.path.join(root, rel))
            if src:
                test_asserts_now.extend(extract(rel, src)[0])
    for st, rel in changed:
        full = os.path.join(root, rel)
        after = read_text(full) if st != "D" and os.path.exists(full) else None
        if is_test_file(rel, extra_globs):
            before = base.before(rel) if st != "A" else None
            findings.extend(diff_test_file(rel, before, after))
        elif is_config_file(rel):
            before = base.before(rel) if st != "A" else None
            findings.extend(diff_config_file(rel, before, after))
        elif lang_of(rel):
            before = base.before(rel) if st != "A" else None
            findings.extend(scan_impl_file(rel, before, after, test_asserts_now))
    return findings, base.describe()


# --------------------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------------------

def parse_aliases(items: Sequence[str]) -> Dict[str, str]:
    res = {}
    for it in items or []:
        if "=" in it:
            k, v = it.split("=", 1)
            res[k.strip()] = v.strip()
    return res


def gather_test_files(root: str, paths: Sequence[str], extra_globs: Sequence[str]) -> List[str]:
    files: List[str] = []
    if paths:
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(root, p)
            if os.path.isdir(full):
                for rel in walk_files(full):
                    if lang_of(rel) and (is_test_file(rel, extra_globs) or extra_globs == ["*"]):
                        files.append(norm_path(os.path.relpath(os.path.join(full, rel), root)))
            elif os.path.exists(full):
                files.append(norm_path(os.path.relpath(full, root)))
    else:
        for rel in walk_files(root):
            if lang_of(rel) and is_test_file(rel, extra_globs):
                files.append(rel)
    return sorted(set(files))


def analyze(root: str, test_files: Sequence[str], spec_files: Sequence[str], aliases: Dict[str, str],
            failing: Sequence[str], scan_docstrings: bool = True, spec_texts: Sequence[Tuple[str, str]] = ()) -> Dict[str, Any]:
    tests: List[Assertion] = []
    parse_errors: List[str] = []
    for rel in test_files:
        src = read_text(os.path.join(root, rel))
        if src is None:
            continue
        got, err = extract(rel, src, aliases)
        tests.extend(got)
        if err:
            parse_errors.append("%s: %s" % (rel, err))
    specs: List[Assertion] = []
    for sp in spec_files:
        full = sp if os.path.isabs(sp) else os.path.join(root, sp)
        txt = read_text(full)
        if txt is None:
            parse_errors.append("%s: spec file not readable" % sp)
            continue
        rel = norm_path(os.path.relpath(full, root)) if not os.path.isabs(sp) else sp
        if full.endswith((".py",) + JS_EXTS):
            specs.extend(extract_spec_from_source(rel, txt, aliases))
        else:
            specs.extend(extract_spec_text(rel, txt, aliases))
    for name, txt in spec_texts:
        specs.extend(extract_spec_text(name, txt, aliases))
    if scan_docstrings:
        tset = set(test_files)
        for rel in walk_files(root):
            if rel in tset or is_test_file(rel) or not lang_of(rel):
                continue
            full = os.path.join(root, rel)
            try:
                if os.path.getsize(full) > 500_000:
                    continue
            except OSError:
                continue
            txt = read_text(full)
            if txt and (">>>" in txt or "@example" in txt):
                specs.extend(extract_spec_from_source(rel, txt, aliases))
    contradictions = find_contradictions(tests, specs)
    if failing:
        relevant = [c for c in contradictions if c.b.source == "spec" or _test_matches_failing(c.b.test, c.b.file, failing)
                    or (c.a.source == "test" and _test_matches_failing(c.a.test, c.a.file, failing))]
    else:
        relevant = contradictions
    return {"tests": tests, "specs": specs, "contradictions": contradictions, "relevant": relevant, "parse_errors": parse_errors}


def decide(result: Dict[str, Any], failing: Sequence[str], ran: bool, run_rc: Optional[int], tamper: List[Finding]) -> Tuple[str, int, List[str]]:
    notes: List[str] = []
    errors = [f for f in tamper if f.severity == "error"]
    strong = [c for c in result["relevant"] if c.strength == "strong"]
    weak = [c for c in result["relevant"] if c.strength == "weak"]
    if errors:
        notes.append("%d tampering finding(s) in the working diff; a tampered suite cannot be trusted." % len(errors))
        return "tampered", EXIT_TAMPERED, notes
    if ran and run_rc == 0:
        notes.append("the test command passed; nothing to escalate.")
        if strong:
            notes.append("%d static conflict(s) were found anyway (listed below); since the suite passes, they probably involve state the analysis cannot see, or a spec line describing old behaviour. Worth a look, not a blocker." % len(strong))
        return "fixable", EXIT_FIXABLE, notes
    if strong:
        return "contradictory", EXIT_CONTRADICTORY, notes
    if weak:
        notes.append("%d possible contradiction(s) found, but the calls involved are stateful, mocked or parametrised, so they need a human look." % len(weak))
        return "unclear", EXIT_UNCLEAR, notes
    if ran and run_rc not in (0, None) and not failing:
        notes.append("the test command failed but no failing test names could be parsed from its output (collection error, crash or unknown runner).")
        return "unclear", EXIT_UNCLEAR, notes
    if failing:
        covered = [f for f in failing if any(_test_matches_failing(a.test, a.file, [f]) for a in result["tests"])]
        if not covered:
            notes.append("none of the failing tests contain assertions c22 can parse; analyse them by hand (see SKILL.md, manual protocol).")
            return "unclear", EXIT_UNCLEAR, notes
    if result["parse_errors"] and not result["tests"]:
        notes.append("test files could not be parsed.")
        return "unclear", EXIT_UNCLEAR, notes
    notes.append("no contradiction found between the spec and the tests: treat the failure as a bug in the implementation and keep fixing the code, not the tests.")
    return "fixable", EXIT_FIXABLE, notes


def render_markdown(rep: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("# Catch-22 report")
    L.append("")
    L.append("**Verdict: %s** (exit %d)" % (rep["verdict"].upper(), rep["exit_code"]))
    L.append("")
    for n in rep["notes"]:
        L.append("- " + n)
    if rep["notes"]:
        L.append("")
    cons = rep["contradictions"]
    if cons:
        L.append("## Contradictions")
        L.append("")
        for i, c in enumerate(cons, 1):
            a, b = c["a"], c["b"]
            L.append("### C%d. %s (%s)" % (i, c["kind"], c["strength"]))
            L.append("")
            L.append("- **%s** `%s:%s`%s" % ("Spec" if a["source"] == "spec" else "Test A", a["file"], a["line"],
                                            (" in `%s`" % a["test"]) if a["test"] else ""))
            L.append("  ```")
            L.append("  " + (a["text"] or a["subject_src"]).replace("\n", "\n  "))
            L.append("  ```")
            L.append("- **%s** `%s:%s`%s" % ("Test" if a["source"] == "spec" else "Test B", b["file"], b["line"],
                                            (" in `%s`" % b["test"]) if b["test"] else ""))
            L.append("  ```")
            L.append("  " + (b["text"] or b["subject_src"]).replace("\n", "\n  "))
            L.append("  ```")
            L.append("- **Why both cannot hold:** %s. `%s` %s." % (
                c["reason"], _short(a["subject_src"], 80),
                "is a call with literal arguments, so one evaluation has one outcome" if c["strength"] == "strong"
                else "may depend on state, mocks or parameters, so a human should confirm"))
            if c.get("repro_path"):
                L.append("- **Repro:** `python3 %s` (exit 0 = contradiction confirmed)" % c["repro_path"])
            L.append("")
        L.append("## Options for the human")
        L.append("")
        L.append("1. The spec/issue is right: the conflicting test assertion is wrong and a human should correct it.")
        L.append("2. The test is right: the spec/issue (or the other test) is outdated and should be updated.")
        L.append("3. Both are intended under different conditions: name the condition so the code can honour both.")
        L.append("")
        L.append("Until a human picks one, the agent does not edit, skip, delete or loosen any test, and does not special-case inputs.")
        L.append("")
    if rep["tamper"]:
        L.append("## Tampering in the working diff (baseline: %s)" % rep.get("baseline", "?"))
        L.append("")
        for f in rep["tamper"]:
            loc = "%s:%s" % (f["file"], f["line"]) if f["line"] else f["file"]
            L.append("- [%s] `%s` %s: %s" % (f["severity"], f["code"], loc, f["message"]))
        L.append("")
    s = rep["summary"]
    L.append("## Coverage")
    L.append("")
    L.append("- test files: %d, assertions parsed: %d, spec examples: %d" % (s["test_files"], s["assertions"], s["spec_examples"]))
    if rep.get("failing"):
        L.append("- failing tests reported by the runner: %d" % len(rep["failing"]))
    for e in rep.get("parse_errors", []):
        L.append("- parse issue: " + e)
    L.append("")
    L.append("_Generated by Catch-22 %s. Static analysis finds contradictions between literal expectations; it cannot prove that a task is possible._" % VERSION)
    return "\n".join(L) + "\n"


def cmd_check(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.root)
    aliases = parse_aliases(args.alias)
    test_files = gather_test_files(root, args.paths, args.tests)
    failing = list(args.failing or [])
    ran, rc, run_out = False, None, ""
    if args.run:
        ran = True
        rc, run_out, parsed = run_tests(args.run, root, args.timeout)
        failing.extend(parsed)
    res = analyze(root, test_files, args.spec or [], aliases, failing, scan_docstrings=not args.no_docstrings)
    tamper: List[Finding] = []
    baseline_desc = "skipped"
    if not args.no_tamper:
        tamper, baseline_desc = collect_tamper(root, args.base, args.tests)
    verdict, code, notes = decide(res, failing, ran, rc, tamper)
    if baseline_desc == "none" and not args.no_tamper:
        notes.append("no baseline for tamper detection (not a git repo and no `c22 baseline` snapshot).")
    cons_public = []
    shown = res["relevant"] if verdict in ("contradictory", "unclear", "tampered") else res["relevant"]
    state = os.path.join(root, STATE_DIR)
    for i, c in enumerate(shown[: args.max], 1):
        d = c.public()
        if c.strength == "strong" and not args.no_repro:
            c.repro = make_repro(c)
            os.makedirs(state, exist_ok=True)
            gi = os.path.join(state, ".gitignore")
            if not os.path.exists(gi):
                with open(gi, "w") as fh:
                    fh.write("*\n")
            rp = os.path.join(state, "repro_%d.py" % i)
            with open(rp, "w") as fh:
                fh.write(c.repro)
            d["repro"] = c.repro
            d["repro_path"] = norm_path(os.path.relpath(rp, root))
            try:
                p = subprocess.run([sys.executable, rp], capture_output=True, text=True, timeout=20)
                d["repro_exit"] = p.returncode
                d["repro_output"] = p.stdout.strip()
            except Exception as e:  # pragma: no cover
                d["repro_exit"] = None
                d["repro_output"] = str(e)
        cons_public.append(d)
    rep = {
        "tool": "catch-22", "version": VERSION, "root": root, "verdict": verdict, "exit_code": code, "notes": notes,
        "contradictions": cons_public,
        "tamper": [f.public() for f in tamper], "baseline": baseline_desc,
        "failing": failing, "parse_errors": res["parse_errors"],
        "summary": {"test_files": len(test_files), "assertions": len(res["tests"]), "spec_examples": len(res["specs"]),
                    "contradictions_total": len(res["contradictions"]), "contradictions_relevant": len(res["relevant"])},
    }
    md = render_markdown(rep)
    if args.report:
        rp = args.report if os.path.isabs(args.report) else os.path.join(root, args.report)
        with open(rp, "w") as fh:
            fh.write(md)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    elif not args.quiet:
        print(md)
    return code


def cmd_tamper(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.root)
    findings, desc = collect_tamper(root, args.base, args.tests, use_git=args.git)
    if desc == "none":
        msg = "c22: no baseline. Run `c22 baseline` at the start of the session, or use a git repository."
        if args.json:
            print(json.dumps({"error": msg}))
        else:
            print(msg, file=sys.stderr)
        return 1
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]
    code = EXIT_TAMPERED if errors or (args.strict and warns) else 0
    if args.json:
        print(json.dumps({"baseline": desc, "exit_code": code, "findings": [f.public() for f in findings]}, indent=2))
    else:
        if not findings:
            print("c22 tamper: clean (baseline: %s)" % desc)
        else:
            print("c22 tamper: %d error(s), %d warning(s) (baseline: %s)" % (len(errors), len(warns), desc))
            for f in findings:
                loc = "%s:%s" % (f.file, f.line) if f.line else f.file
                print("  [%s] %-20s %s  %s" % (f.severity, f.code, loc, f.message))
                if f.before:
                    print("      before: " + _short(f.before, 120))
                if f.after:
                    print("      after:  " + _short(f.after, 120))
    return code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="c22", description="Catch-22: detect impossible tasks (spec vs tests) and test tampering.")
    p.add_argument("--version", action="version", version="catch-22 " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=".", help="project root (default: .)")
        sp.add_argument("--tests", action="append", default=[], metavar="GLOB", help="extra glob that marks test files (repeatable)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")

    c = sub.add_parser("check", help="verdict: fixable / contradictory / unclear (+ tampering)")
    common(c)
    c.add_argument("paths", nargs="*", help="test files or directories (default: discover)")
    c.add_argument("--spec", action="append", default=[], metavar="FILE", help="issue/spec/README file with examples (repeatable)")
    c.add_argument("--run", metavar="CMD", help="run the test command and focus on the tests that fail")
    c.add_argument("--failing", action="append", metavar="TEST", help="name of a failing test (repeatable), instead of --run")
    c.add_argument("--alias", action="append", default=[], metavar="A=B", help="treat callee A in tests as B in the spec (e.g. candidate=solve)")
    c.add_argument("--base", metavar="REF", help="git ref for tamper detection (default: c22 snapshot, else HEAD)")
    c.add_argument("--report", metavar="FILE", help="also write the markdown report here")
    c.add_argument("--timeout", type=int, default=600, help="timeout for --run in seconds")
    c.add_argument("--max", type=int, default=20, help="max contradictions to report")
    c.add_argument("--no-tamper", action="store_true", help="skip tamper detection")
    c.add_argument("--no-docstrings", action="store_true", help="do not mine docstrings/JSDoc in source files for examples")
    c.add_argument("--no-repro", action="store_true", help="do not write repro scripts to .c22/")
    c.add_argument("-q", "--quiet", action="store_true", help="no output, exit code only")
    c.set_defaults(func=cmd_check)

    t = sub.add_parser("tamper", help="test tampering in the working diff")
    common(t)
    t.add_argument("--base", metavar="REF", help="git ref to compare against (default: c22 snapshot, else HEAD)")
    t.add_argument("--git", action="store_true", help="ignore any c22 snapshot and use git")
    t.add_argument("--strict", action="store_true", help="warnings (implementation heuristics, config edits) also fail")
    t.set_defaults(func=cmd_tamper)

    b = sub.add_parser("baseline", help="snapshot test, config and source files now")
    b.add_argument("--root", default=".")
    b.add_argument("--tests", action="append", default=[], metavar="GLOB")
    b.set_defaults(func=cmd_baseline)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":
    sys.exit(main())
