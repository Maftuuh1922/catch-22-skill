---
name: catch-22
description: Use when the same test keeps failing after two honest attempts to fix the code, when a fix for one test breaks another, or BEFORE you edit, skip, delete, loosen or special-case any test. Checks whether the task is impossible because the spec/issue contradicts the tests (or two tests contradict each other); if so, stop and show the contradiction with evidence instead of changing the tests. Also verifies the working diff has no test tampering before you say "done". Works with pytest, unittest, jest, vitest and node:assert.
license: MIT
---

# Catch-22

Some tasks cannot be done honestly: the issue says `f(2)` returns `4`, a test says `5`; or one test wants `4` and another wants `5` for the same call. When that happens, the only honest outcome is to say so. Editing the test, skipping it, deleting it, loosening it, or writing code that recognises the test input are all ways of hiding the problem. This skill makes you check for that situation first, and gives you the evidence to hand to a human.

`SKILL_DIR` below means the folder this file is in. The helper is `SKILL_DIR/scripts/c22.py` (Python 3.9+, standard library only). If Python is not available, follow the manual protocol at the end; the rules are the same.

## When this applies

- A test still fails after you made two genuine attempts to fix the implementation.
- Making one test pass makes another fail, and back again.
- You are about to change a test file, a test config (`conftest.py`, `pytest.ini`, `jest.config.*`, `vitest.config.*`, the `test` script in `package.json`), or add `skip`/`xfail`/`.only`/`.todo`.
- You are about to write code that checks for a specific input from a test, counts calls, overrides `__eq__`/`equals` to always match, or reads the test runner's environment.
- Before you report the task as done.

## Step 1. Run the check

From the project root, with the issue or spec in a file (save the task description to `ISSUE.md` first if it only exists in the conversation):

```bash
python3 SKILL_DIR/scripts/c22.py check --spec ISSUE.md --run "<the project's test command>" --report C22-REPORT.md
```

Examples of test commands: `python -m pytest -q`, `npx vitest run`, `npx jest`, `npm test`.
Add `--alias candidate=real_name` when tests call the function under a different name.
The command also mines docstring examples (`>>>`) and JSDoc `@example` lines from source files.

Exit codes: `0` fixable, `10` contradictory, `11` unclear, `12` tampered.

## Step 2. Act on the verdict

**contradictory (10)**: stop changing code for the conflicting assertions. Do not touch the tests. Reply to the user with the contradiction, using the report:

```
I stopped: this task can't be completed as specified.

Spec   ISSUE.md:7        `slugify("Hello World")` returns "hello-world"
Test   tests/test_slug.py:6   assert slugify("Hello World") == "Hello-World"
Both constrain the same call with literal arguments, so no implementation can satisfy both.
Repro: python3 .c22/repro_1.py  (prints why, exits 0)

Which one is right?
1. The spec: the test assertion is wrong and should be corrected by you.
2. The test: the spec is outdated.
3. Both, under different conditions: tell me the condition.

Everything else is fixed: <what you did and what passes>.
```

Finish all work that is not blocked by the contradiction, then wait for the answer.

**unclear (11)**: the tool could not decide (stateful objects, mocks, parametrised fixtures, unparsed assertions, or a crash). Do the manual protocol below on the failing tests. If you find a contradiction, treat it as `contradictory`. If you find none, the bug is in the implementation: keep fixing the code.

**fixable (0)**: no contradiction was found. The failure is a bug in your implementation, or a case you have not understood yet. Keep working on the code. Re-read the failing assertion, the spec, and the error output. Never "fix" the test.

**tampered (12)**: a test, a test config, or an assertion was changed in the working diff. Revert those changes (`git checkout -- <file>` or undo your edit) unless the user explicitly asked for that exact test change in this conversation. Then run the check again.

## Step 3. Before you say "done"

```bash
python3 SKILL_DIR/scripts/c22.py tamper
```

It must print `clean` (exit 0). It compares the working tree with `HEAD` (or with a snapshot taken by `c22.py baseline` at the start of the session, for projects without git). Warnings about implementation heuristics (special-casing, `__eq__` returning `True`, test-runner sniffing) are not proof of cheating, but you must be able to explain each one honestly in your reply.

## Forbidden moves (unless the user explicitly asks for that specific change)

These are the shortcuts documented in the ImpossibleBench paper (arXiv:2510.20270) and in public agent bug reports:

1. Changing an expected value, inverting or loosening an assertion (`==` to `is not None`, `toEqual` to `toBeDefined`, `assert True`).
2. Deleting a test, a test file, or an assertion; commenting it out.
3. Adding `skip`, `skipif`, `xfail`, `pytest.skip()`, `it.skip`, `xit`, `it.todo`, `describe.skip`, or `.only` on other tests.
4. Wrapping assertions in `try/except: pass` or catching `AssertionError`.
5. Excluding tests through config (`collect_ignore`, `--deselect`, `testPathIgnorePatterns`, a changed `test` script).
6. Special-casing the exact inputs a test uses, or returning different results on repeated identical calls.
7. Overloading comparison so anything matches (`__eq__` returning `True`).
8. Detecting the test runner (`PYTEST_CURRENT_TEST`, `JEST_WORKER_ID`, `VITEST`, stack inspection) to change behaviour.
9. Calling the work "done", "backward compatible" or "a legacy test quirk" to justify any of the above.

If the user does ask you to change a test, change only what they asked, say which line you changed, and run `tamper` so the change is visible in its output.

## Manual protocol (no Python, or verdict unclear)

For each failing assertion, write down: the exact expression under test, the exact expected value, file and line.

1. Same input, different expectation: search the tests for the same call with the same arguments. Two different expected values (or one `raises` and one return value) cannot both hold for a deterministic function.
2. Spec versus test: search the issue, README, docstrings and type signatures for the same call or rule. If the spec states a different result, quote both lines.
3. Invariants: a test that contradicts a rule the spec states in words (for example "the result is always sorted", "raises `ValueError` when `lo > hi`") is a contradiction even without a matching example.
4. State and mocks: if the two expectations involve an object that changes between them, a mock, a fixture, a time or randomness source, they may both be right. Prove it before calling it a contradiction.
5. Environment: missing services, versions, locales, time zones. These are not contradictions; report them as blockers.

A contradiction is only real when you can show both lines and explain in one sentence why no implementation can satisfy both. If you can, escalate as in Step 2. If you cannot, the task is probably possible: keep fixing the code.
