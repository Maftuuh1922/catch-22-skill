import json
import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "c22.py")
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import c22  # noqa: E402


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(textwrap.dedent(text))
    return p


def run(root, *args):
    p = subprocess.run([sys.executable, SCRIPT, *args], cwd=root, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def git_repo(root):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=root, check=True, env=env)


# ---------------------------------------------------------------- extraction

def test_extract_python_asserts_and_raises():
    src = textwrap.dedent('''
        import pytest
        from m import f

        def test_a():
            assert f(2) == 4
            assert 4 == f(3)
            assert f(1) != 0
            assert not f(0)
            with pytest.raises(ValueError):
                f(-1)

        class TestB:
            def test_b(self):
                self.assertEqual(f(5), 25)
                self.assertTrue(f(1))
    ''')
    got, err = c22.extract("tests/test_m.py", src)
    assert err is None
    kinds = sorted((a.kind, a.subject) for a in got)
    assert ("eq", "f(2)") in kinds
    assert ("eq", "f(3)") in kinds  # literal on the left is swapped
    assert ("ne", "f(1)") in kinds
    assert ("truth", "f(0)") in kinds
    assert ("raises", "f(-1)") in kinds
    assert ("eq", "f(5)") in kinds
    assert all(a.pure for a in got if a.subject.startswith("f("))


def test_extract_js_matchers():
    src = textwrap.dedent('''
        import { describe, it, expect } from "vitest";
        import { add } from "../src/add.js";
        describe("add", () => {
          it("adds", () => {
            expect(add(2, 2)).toBe(4);
            expect(add(1, 1)).not.toEqual(3);
            expect(() => add(null, 1)).toThrow(TypeError);
            expect(add(0, 0)).toBeFalsy();
            expect(add([1, 2], {a: 'x'})).toStrictEqual([1, 2, "x"]);
          });
        });
    ''')
    got, _ = c22.extract("tests/add.test.js", src)
    by = {(a.kind, a.subject) for a in got}
    assert ("eq", "add(2, 2)") in by
    assert ("ne", "add(1, 1)") in by
    assert ("raises", "add(None, 1)") in by
    assert ("truth", "add(0, 0)") in by
    assert ("eq", "add([1, 2], {'a': 'x'})") in by
    assert all(a.test == "add > adds" for a in got)


def test_js_literal_parser():
    assert c22.js_literal("[1, 'a', {b: true, 'c': null}]") == [1, "a", {"b": True, "c": None}]
    assert c22.js_literal("-1.5e3") == -1500.0
    assert c22.js_literal("`x${y}`") is c22.MISSING
    assert c22.js_literal("someVar") is c22.MISSING


def test_method_calls_on_local_objects_are_not_pure():
    src = textwrap.dedent('''
        def test_q(user):
            assert user.has_perm("a") is True
            assert user.has_perm("a") is False
    ''')
    got, _ = c22.extract("tests/test_q.py", src)
    assert not any(a.pure for a in got)
    cons = c22.find_contradictions(got, [])
    assert cons and all(c.strength == "weak" for c in cons)


def test_module_function_calls_are_pure():
    src = "import mathlib\n\ndef test_x():\n    assert mathlib.sq(3) == 9\n    assert mathlib.sq(3) == 10\n"
    got, _ = c22.extract("tests/test_x.py", src)
    cons = c22.find_contradictions(got, [])
    assert cons and cons[0].strength == "strong"


def test_dataflow_binding_resolves_result_variable():
    src = textwrap.dedent('''
        def check(candidate):
            assert candidate(1, 2) is None
            result = candidate(1, 2)
            assert result is not None
    ''')
    got, _ = c22.extract("t.py", src, {"candidate": "solve"})
    cons = c22.find_contradictions(got, [])
    assert cons and cons[0].strength == "strong"
    assert cons[0].a.subject == "solve(1, 2)"


def test_binding_dropped_after_mutation():
    src = textwrap.dedent('''
        def test_x():
            r = make(1)
            assert r == [1]
            r.append(2)
            assert r == [1, 2]
    ''')
    got, _ = c22.extract("tests/test_x.py", src)
    assert not [c for c in c22.find_contradictions(got, []) if c.strength == "strong"]


# ---------------------------------------------------------------- contradictions

def test_test_vs_test_conflict_is_strong():
    src = "def test_a():\n    assert f(2) == 4\n    x = 1\n    assert f(2) == 5\n"
    got, _ = c22.extract("tests/test_f.py", src)
    cons = c22.find_contradictions(got, [])
    assert len(cons) == 1 and cons[0].strength == "strong" and cons[0].kind == "test-vs-test"


def test_raise_vs_return_conflict():
    src = "import pytest\n\ndef test_a():\n    assert clamp(1, 3, 0) == 1\n\ndef test_b():\n    with pytest.raises(ValueError):\n        clamp(1, 3, 0)\n"
    got, _ = c22.extract("tests/test_c.py", src)
    cons = c22.find_contradictions(got, [])
    assert cons and cons[0].strength == "strong"
    assert "raise" in cons[0].reason


def test_no_conflict_for_equal_values_and_approx():
    src = "import pytest\n\ndef test_a():\n    assert f(1) == 1.0\n    assert f(1) == 1\n    assert g(2) == pytest.approx(0.30000000000000004)\n    assert g(2) == 0.3\n"
    got, _ = c22.extract("tests/test_ok.py", src)
    assert c22.find_contradictions(got, []) == []


def test_fixture_params_downgrade_cross_test_conflicts():
    src = "def test_a(tmp_path):\n    assert f(1) == 1\n\ndef test_b():\n    assert f(1) == 2\n"
    got, _ = c22.extract("tests/test_p.py", src)
    cons = c22.find_contradictions(got, [])
    assert cons and cons[0].strength == "weak"


def test_spec_examples_markdown_and_doctest():
    md = textwrap.dedent('''
        # Bug
        - `slugify("Hello World")` returns `"hello-world"`
        - median([1, 2, 3, 4]) -> 2.5
        - `clamp(1, 3, 0)` raises `ValueError`

        >>> to_roman(4)
        'IV'
    ''')
    specs = c22.extract_spec_text("ISSUE.md", md)
    got = {(s.kind, s.subject, repr(s.expected)) for s in specs}
    assert ("eq", "slugify('Hello World')", "'hello-world'") in got
    assert ("eq", "median([1, 2, 3, 4])", "2.5") in got
    assert ("eq", "to_roman(4)", "'IV'") in got
    assert any(s.kind == "raises" and s.subject == "clamp(1, 3, 0)" for s in specs)


def test_spec_vs_test_conflict():
    specs = c22.extract_spec_text("ISSUE.md", "- `slugify(\"Hello World\")` returns `\"hello-world\"`\n")
    tests, _ = c22.extract("tests/test_s.py", "def test_s():\n    assert slugify('Hello World') == 'Hello-World'\n")
    cons = c22.find_contradictions(tests, specs)
    assert cons and cons[0].kind == "spec-vs-test" and cons[0].strength == "strong"


def test_docstring_and_jsdoc_examples():
    py = 'def sq(x):\n    """Square.\n\n    >>> sq(3)\n    9\n    """\n    return x * x\n'
    js = "/**\n * Square.\n * @example\n * sq(3) // => 9\n */\nexport function sq(x) { return x * x; }\n"
    a = c22.extract_spec_from_source("m.py", py)
    b = c22.extract_spec_from_source("m.js", js)
    assert [(s.subject, s.expected) for s in a] == [("sq(3)", 9)]
    assert [(s.subject, s.expected) for s in b] == [("sq(3)", 9)]


def test_repro_script_confirms(tmp_path):
    src = "def test_a():\n    assert f(2) == 4\n\ndef test_b():\n    assert f(2) == 5\n"
    got, _ = c22.extract("tests/test_f.py", src)
    c = c22.find_contradictions(got, [])[0]
    p = tmp_path / "r.py"
    p.write_text(c22.make_repro(c))
    out = subprocess.run([sys.executable, str(p)], capture_output=True, text=True)
    assert out.returncode == 0 and "Contradiction confirmed" in out.stdout


# ---------------------------------------------------------------- CLI: check

def test_check_contradictory_exit_code(tmp_path):
    root = str(tmp_path)
    write(root, "ISSUE.md", "- `add(2, 2)` returns `4`\n")
    write(root, "tests/test_add.py", "from add import add\n\ndef test_add():\n    assert add(2, 2) == 5\n")
    write(root, "add.py", "def add(a, b):\n    return a + b\n")
    code, out = run(root, "check", "--spec", "ISSUE.md", "--no-tamper", "--json")
    rep = json.loads(out)
    assert code == 10 and rep["verdict"] == "contradictory"
    assert rep["contradictions"][0]["repro_exit"] == 0


def test_check_fixable_exit_code(tmp_path):
    root = str(tmp_path)
    write(root, "ISSUE.md", "- `add(2, 2)` returns `4`\n")
    write(root, "tests/test_add.py", "from add import add\n\ndef test_add():\n    assert add(2, 2) == 4\n")
    code, out = run(root, "check", "--spec", "ISSUE.md", "--no-tamper", "-q")
    assert code == 0


def test_check_unclear_for_weak_conflict(tmp_path):
    root = str(tmp_path)
    write(root, "tests/test_q.py", "def test_q(q):\n    assert q.size() == 0\n    assert q.size() == 1\n")
    code, _ = run(root, "check", "--no-tamper", "-q")
    assert code == 11


def test_check_with_run_and_pytest(tmp_path):
    pytest.importorskip("pytest")
    root = str(tmp_path)
    write(root, "add.py", "def add(a, b):\n    return a + b\n")
    write(root, "conftest.py", "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n")
    write(root, "tests/test_add.py", "from add import add\n\ndef test_one():\n    assert add(2, 2) == 4\n\ndef test_two():\n    assert add(2, 2) == 5\n")
    code, out = run(root, "check", "--run", "%s -m pytest -q -p no:cacheprovider" % sys.executable, "--no-tamper", "--json")
    rep = json.loads(out)
    assert code == 10, out
    assert any("test_two" in f for f in rep["failing"])


def test_check_report_file(tmp_path):
    root = str(tmp_path)
    write(root, "tests/test_a.py", "def test_a():\n    assert f(1) == 1\n    assert f(1) == 2\n")
    code, _ = run(root, "check", "--no-tamper", "--report", "C22-REPORT.md", "-q")
    assert code == 10
    text = open(os.path.join(root, "C22-REPORT.md")).read()
    assert "CONTRADICTORY" in text and "Options for the human" in text


# ---------------------------------------------------------------- tamper

PY_TEST = "import pytest\n\nfrom m import f\n\n\ndef test_a():\n    assert f(2) == 4\n    assert f(3) == 9\n\n\ndef test_b():\n    assert f(0) == 0\n"


@pytest.mark.parametrize("edit,code", [
    (lambda s: s.replace("== 9", "== 10"), "expected-changed"),
    (lambda s: s.replace("    assert f(3) == 9\n", ""), "assert-removed"),
    (lambda s: s.replace("def test_b", "@pytest.mark.skip\ndef test_b"), "skip-added"),
    (lambda s: s.replace("def test_b", "@pytest.mark.xfail(reason='x')\ndef test_b"), "skip-added"),
    (lambda s: s[: s.index("\n\ndef test_b")] + "\n", "test-deleted"),
    (lambda s: s.replace("assert f(3) == 9", "assert f(3) is not None"), "assert-loosened"),
    (lambda s: s.replace("assert f(3) == 9", "assert True"), "assert-loosened"),
    (lambda s: s.replace("    assert f(3) == 9", "    # assert f(3) == 9"), "assert-removed"),
    (lambda s: s.replace("    assert f(0) == 0\n", "    try:\n        assert f(0) == 0\n    except AssertionError:\n        pass\n"), "assert-swallowed"),
])
def test_python_tamper_detected(edit, code):
    f = c22.diff_test_file("tests/test_m.py", PY_TEST, edit(PY_TEST))
    assert code in {x.code for x in f}, f


def test_python_honest_edits_not_flagged():
    assert c22.diff_test_file("tests/test_m.py", PY_TEST, PY_TEST + "\n\ndef test_c():\n    assert f(4) == 16\n") == []
    assert c22.diff_test_file("tests/test_m.py", PY_TEST, PY_TEST.replace("== 4", "==  4")) == []
    renamed = PY_TEST.replace("def test_b", "def test_b_zero")
    assert c22.diff_test_file("tests/test_m.py", PY_TEST, renamed) == []


JS_TEST = textwrap.dedent('''
    import { it, expect, describe } from "vitest";
    import { add } from "../src/add.js";
    describe("add", () => {
      it("adds", () => {
        expect(add(2, 2)).toBe(4);
        expect(add(1, 2)).toBe(3);
      });
      it("zero", () => {
        expect(add(0, 0)).toBe(0);
      });
    });
''')


@pytest.mark.parametrize("edit,code", [
    (lambda s: s.replace("toBe(3)", "toBe(4)"), "expected-changed"),
    (lambda s: s.replace('it("zero"', 'it.skip("zero"'), "skip-added"),
    (lambda s: s.replace('it("zero"', 'xit("zero"'), "skip-added"),
    (lambda s: s.replace('it("adds"', 'it.only("adds"'), "skip-added"),
    (lambda s: s.replace("describe(", "describe.skip("), "skip-added"),
    (lambda s: s.replace("    expect(add(1, 2)).toBe(3);\n", ""), "assert-removed"),
    (lambda s: s.replace("expect(add(1, 2)).toBe(3)", "expect(add(1, 2)).toBeTruthy()"), "assert-loosened"),
    (lambda s: s.replace('  it("zero", () => {\n    expect(add(0, 0)).toBe(0);\n  });\n', ""), "test-deleted"),
])
def test_js_tamper_detected(edit, code):
    after = edit(JS_TEST)
    assert after != JS_TEST
    f = c22.diff_test_file("tests/add.test.js", JS_TEST, after)
    assert code in {x.code for x in f}, f


def test_impl_heuristics():
    tests, _ = c22.extract("tests/test_m.py", "def test_a():\n    assert solve(1029, 77) == 5\n")
    before = "def solve(a, b):\n    return a % b\n"
    special = "def solve(a, b):\n    if (a, b) == (1029, 77):\n        return 5\n    return a % b\n"
    eq = "class X:\n    def __eq__(self, o):\n        return True\n" + before
    sniff = "import os\nT = 'PYTEST_CURRENT_TEST' in os.environ\n" + before
    assert {f.code for f in c22.scan_impl_file("m.py", before, special, tests)} == {"special-casing"}
    assert "eq-overload" in {f.code for f in c22.scan_impl_file("m.py", before, eq, tests)}
    assert "test-env-sniffing" in {f.code for f in c22.scan_impl_file("m.py", before, sniff, tests)}
    honest = "def solve(a, b):\n    return (a % b) if b else 0\n"
    assert c22.scan_impl_file("m.py", before, honest, tests) == []


def test_tamper_cli_git(tmp_path):
    root = str(tmp_path)
    write(root, "m.py", "def f(x):\n    return x * x\n")
    write(root, "tests/test_m.py", PY_TEST)
    git_repo(root)
    code, out = run(root, "tamper")
    assert code == 0 and "clean" in out
    write(root, "tests/test_m.py", PY_TEST.replace("== 9", "== 10"))
    code, out = run(root, "tamper")
    assert code == 12 and "expected-changed" in out
    os.remove(os.path.join(root, "tests/test_m.py"))
    code, out = run(root, "tamper", "--json")
    assert code == 12 and "test-file-deleted" in out


def test_tamper_cli_snapshot_without_git(tmp_path):
    root = str(tmp_path)
    write(root, "tests/test_m.py", PY_TEST)
    code, out = run(root, "baseline")
    assert code == 0
    assert os.path.exists(os.path.join(root, ".c22", "baseline", "manifest.json"))
    code, out = run(root, "tamper")
    assert code == 0
    write(root, "conftest.py", "collect_ignore = ['tests/test_m.py']\n")
    code, out = run(root, "tamper")
    assert code == 12 and "test-config-changed" in out


def test_check_reports_tampering_first(tmp_path):
    root = str(tmp_path)
    write(root, "tests/test_m.py", PY_TEST)
    git_repo(root)
    write(root, "tests/test_m.py", PY_TEST.replace("def test_b", "@pytest.mark.skip\ndef test_b"))
    code, _ = run(root, "check", "-q")
    assert code == 12


def test_no_baseline_message(tmp_path):
    code, out = run(str(tmp_path), "tamper")
    assert code == 1 and "no baseline" in out


# ---------------------------------------------------------------- runner output parsing

def test_parse_failing_pytest_and_vitest_and_jest():
    out = textwrap.dedent('''
        FAILED tests/test_a.py::test_two - assert 4 == 5
        ERROR tests/test_b.py::test_x
         FAIL  tests/add.test.js > add > adds
        ● add › zero
    ''')
    names = c22.parse_failing(out)
    assert "tests/test_a.py::test_two" in names
    assert "tests/test_b.py::test_x" in names
    assert any("add > adds" in n for n in names)
    assert "add › zero" in names


def test_version_flag():
    p = subprocess.run([sys.executable, SCRIPT, "--version"], capture_output=True, text=True)
    assert c22.VERSION in p.stdout
