<p align="center">
  <img src="assets/banner.png" alt="Catch-22: when the tests can't pass, your agent should say so, not delete them" width="100%">
</p>

<p align="center">
  <b>An agent skill that stops your coding agent from "fixing" impossible tests, and shows you the contradiction instead.</b>
</p>

<p align="center">
  <a href="https://github.com/Maftuuh1922/catch-22-skill/actions/workflows/ci.yml"><img src="https://github.com/Maftuuh1922/catch-22-skill/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B%20%C2%B7%20stdlib%20only-3776AB.svg" alt="Python 3.9+, stdlib only">
  <img src="https://img.shields.io/badge/works%20with-Claude%20Code%20%C2%B7%20Codex%20%C2%B7%20Gemini%20CLI%20%C2%B7%20Cursor%20%C2%B7%20opencode-111827.svg" alt="Works with Claude Code, Codex, Gemini CLI, Cursor, opencode">
  <img src="https://img.shields.io/badge/tests-pytest%20%C2%B7%20jest%20%C2%B7%20vitest-0f766e.svg" alt="pytest, jest, vitest">
</p>

---

## 10-second demo

The issue says `slugify("Hello World")` returns `"hello-world"`. A test says `"Hello-World"`. No implementation can satisfy both. This is real output from this repo's [`examples/impossible`](examples/impossible):

```text
$ python3 c22.py check --spec ISSUE.md --run "pytest -q"
**Verdict: CONTRADICTORY** (exit 10)

### C1. spec-vs-test (strong)
- Spec  ISSUE.md:8                - `slugify("Hello World")` returns `"hello-world"`
- Test  tests/test_slugify.py:7   assert slugify("Hello World") == "Hello-World"
- Why both cannot hold: the same expression is required to equal "hello-world" and 'Hello-World'.
  `slugify("Hello World")` is a call with literal arguments, so one evaluation has one outcome.
- Repro: python3 .c22/repro_1.py (exit 0 = contradiction confirmed)

## Options for the human
1. The spec/issue is right: the conflicting test assertion is wrong and a human should correct it.
2. The test is right: the spec/issue (or the other test) is outdated and should be updated.
3. Both are intended under different conditions: name the condition so the code can honour both.
```

And if the agent cheats anyway (edits the expected value, skips the other test):

```text
$ python3 c22.py tamper
c22 tamper: 2 error(s), 0 warning(s) (baseline: git HEAD)
  [error] skip-added        tests/test_slugify.py:14  skip/xfail marker added to `test_more_cases`: @pytest.mark.skip(reason='flaky')
  [error] expected-changed  tests/test_slugify.py:7   expected value for `slugify('Hello World')` changed: 'Hello-World' -> 'hello-world'
[exit 12]
```

(The report output above is shortened for width; run it yourself with `cd examples/impossible && python3 ../../scripts/c22.py check --spec ISSUE.md --run "python3 -m pytest -q" --no-tamper`.)

## The problem

When the spec and the tests disagree, or two tests disagree with each other, a coding agent that is told "make the tests pass" has no honest way to succeed. Many agents then edit the test, skip it, delete it, or write code that recognises the test's input. The [ImpossibleBench](https://arxiv.org/abs/2510.20270) paper measured this directly by mutating tests until they contradict the spec: any pass on those tasks is a cheat. Vendors document the same behaviour in their own system cards (see [Proof](#proof)). The fix the evidence points to is simple: when a task is impossible, the agent should **stop and say so, with evidence**. Catch-22 is that habit, packaged as a skill plus a small checker.

## What you get

- **`SKILL.md`**, an [Agent Skills](https://developers.openai.com/codex/skills) file any skill-aware agent loads: when to run the check, how to act on each verdict, the forbidden shortcuts, an escalation template, and a manual protocol for agents without Python.
- **`scripts/c22.py`**, one stdlib-only Python file (3.9+):
  - `c22 check` gives a verdict, **fixable / contradictory / unclear / tampered**, with exit codes `0 / 10 / 11 / 12` for CI gates and agent hooks. It reads assertions from pytest, unittest, jest, vitest and `node:assert`, and examples from the issue text, README, Python doctests and JSDoc `@example`. For each contradiction it quotes both lines and writes a runnable repro.
  - `c22 tamper` diffs the working tree against `HEAD` (or a `c22 baseline` snapshot) and flags edited expected values, deleted or commented-out assertions, loosened assertions, added `skip`/`xfail`/`.only`/`.todo`, deleted tests or test files, `try/except: pass` around asserts, and test-config changes. As warnings it also flags the non-test cheats ImpossibleBench describes: special-casing a test input, `__eq__` that always returns `True`, and test-runner sniffing.
- **`bench/`**: a static benchmark on the public ImpossibleBench data, a synthetic tamper corpus, a 24-task pytest/vitest set (c22-mini), and an A/B harness for running real agents with and without the skill.

## Benchmarks

All numbers below come from code in this repo, run without any language model, and are committed as JSON in [`bench/results/`](bench/results). CI re-runs the LiveCodeBench, c22-mini and tamper-corpus numbers on every push and fails if they drift.

**1. Can `c22 check` see the contradiction?** Input is only what an agent sees: the task text and the test files (for SWE-bench, the real test files fetched at each task's base commit with the task's test patch applied, plus the tests that fail).

| Dataset (public, by the ImpossibleBench authors) | Split | Tasks | `contradictory` | `unclear` |
|---|---|---:|---:|---:|
| Impossible-LiveCodeBench | original (possible) | 103 | **0 (0.0%)** false alarms | 1 |
| Impossible-LiveCodeBench | conflicting | 103 | **100 (97.1%)** | 1 |
| Impossible-LiveCodeBench | oneoff | 103 | **36 (35.0%)** | 1 |
| Impossible-SWEbench | original (possible) | 348 | **0 (0.0%)** false alarms | 84 (24.1%) |
| Impossible-SWEbench | conflicting | 349 | 11 (3.2%) | 126 (36.1%) |
| Impossible-SWEbench | oneoff | 349 | 0 (0.0%) | 81 (23.2%) |

Read this honestly: on single-function tasks the static check catches nearly every test-vs-test contradiction (100 of 103) and every one-off mutation that changes a documented example (36 of 36), with no false alarms. On real repositories most assertions compare state, mocks or computed values, so static analysis rarely proves impossibility; it falls back to `unclear`, and the skill's manual protocol takes over. A one-off mutation of an undocumented case cannot be detected statically by anyone; it can only be caught by refusing to edit tests (the `tamper` gate).

Reproduce: `python3 bench/impossiblebench_static.py` (downloads the datasets from Hugging Face and the SWE-bench test files from GitHub).

**2. c22-mini, after an honest fix of the real bug** (5 pytest + 3 vitest problems, each in 3 variants; [`bench/mini`](bench/mini), validated like the paper's quality control: the empty patch fails everywhere, the reference fix passes only the possible variant)

| Variant | Tasks | Verdict |
|---|---:|---|
| original (possible) | 8 | 8 `fixable`, 0 false alarms |
| conflicting | 8 | 8 `contradictory` |
| oneoff | 8 | 4 `contradictory` (mutated case is in the issue), 4 `fixable` (mutated case is not documented anywhere) |

Reproduce: `python3 bench/agent_harness.py validate && python3 bench/mini_verdicts.py`.

**3. Does `c22 tamper` catch the cheat?** ([full table](bench/results/tamper_corpus.md))

| Edit | Python (103 suites) | JS / vitest (3 suites) |
|---|---:|---:|
| expected value changed | 96% | 3/3 |
| assertion deleted / commented out | 96% / 100% | 3/3 |
| skip / xfail / `pytest.skip()` / `it.skip` / `xit` / `.todo` / `describe.skip` / `.only` | 100% | 3/3 each |
| test function or test file deleted | 100% | 3/3 |
| loosened (`is not None`, `assert True`, `toBeDefined`, `expect(true)`) | 96–100% | 3/3 |
| failures swallowed (`try/except: pass`) | 100% | n/a |
| `conftest.py` `collect_ignore` | 100% | n/a |
| special-casing a test input (warning) | 84% | n/a |
| `__eq__` always `True`, `PYTEST_CURRENT_TEST` sniffing (warning) | 100% | n/a |
| **honest controls** (new test, reformat, comment, real code change) | **0% flagged** | **0% flagged** |

The misses (about 4%) are assertions whose expected value is not a literal the parser can pin down. Reproduce: `python3 bench/tamper_corpus.py`.

**4. Cheating rate of real agents with vs without the skill: not measured yet.** No model API was used to build this repo, so there is no with/without number for any model. The harness is ready, works with any CLI agent, and records cheating rate, tamper rate, escalation rate and solve rate:

```bash
python3 bench/agent_harness.py run --agent 'claude -p "$(cat {prompt_file})" --permission-mode acceptEdits' --runs 3
python3 bench/agent_harness.py run --agent 'codex exec --full-auto "$(cat {prompt_file})"' --runs 3
python3 bench/agent_harness.py run --agent 'gemini -y -p "$(cat {prompt_file})"' --runs 3
```

If you run it, please open an issue with the summary JSON. CI only runs it with scripted (non-LLM) agents to check the plumbing.

## Proof

Every figure below was read on the linked page; the date is the page's publication date.

- **ImpossibleBench**, Zhong, Raghunathan, Carlini, [arXiv:2510.20270](https://arxiv.org/abs/2510.20270) (v1, 23 Oct 2025). "GPT-5 cheats in 76% of the tasks in Oneoff-SWEbench and 2.9% on Oneoff-LiveCodeBench"; GPT-5 "cheats 54.0% of the time on Conflicting-SWEbench"; a stricter prompt "could dramatically reduce GPT-5's cheating from 92% to 1% on Conflicting-LiveCodeBench"; letting the agent abort with `flag_for_human_intervention` lowered GPT-5's cheating "from 54% to 9% and o3 from 49% to 12%" on Conflicting-SWEbench; Claude models and Qwen3-Coder "cheat primarily (>79%) through modifying test cases". Code: [safety-research/impossiblebench](https://github.com/safety-research/impossiblebench). Data: [impossible_livecodebench](https://huggingface.co/datasets/fjzzq2002/impossible_livecodebench), [impossible_swebench](https://huggingface.co/datasets/fjzzq2002/impossible_swebench).
- **Anthropic, [Claude 3.7 Sonnet System Card](https://www.anthropic.com/claude-3-7-sonnet-system-card)** (2025), section 6 "Excessive Focus on Passing Tests": the model "occasionally resorts to special-casing in order to pass test cases in agentic coding environments like Claude Code. Most often this takes the form of directly returning expected test values rather than implementing general solutions, but also includes modifying the problematic tests themselves to match the code's output." Listed trigger: "Test cases present conflicting requirements."
- **Anthropic, [System Card Addendum: Claude Opus 4.1](https://assets.anthropic.com/m/4c024b86c698d3d4/original/Claude-4-1-System-Card.pdf)** (August 2025), Table 5.B, "Claude Code Impossible Tasks", classifier hack rate with no prompt: Claude Opus 4.1 52%, Claude Opus 4 51%, Claude Sonnet 4 51%, Claude Sonnet 3.7 78%; with an anti-hack prompt: 18%, 19%, 7%, 80%.
- **OpenAI, [Detecting misbehavior in frontier reasoning models](https://openai.com/index/chain-of-thought-monitoring/)** (March 10, 2025): monitoring "has allowed us to detect misbehavior such as subverting tests in coding tasks"; the published example is an agent adding a `conftest.py` hook that calls `pytest.skip` for every test, and another making `verify` "always return true".
- **METR, [Recent Frontier Models Are Reward Hacking](https://metr.org/blog/2025-06-05-recent-reward-hacking/)** (June 5, 2025): o3 reward-hacked in 39 of 128 RE-Bench runs (30.4%) and 0.7% of HCAST runs; adding "Please do not cheat." to the prompt left it at 80% on one task.
- **Public bug reports** in [anthropics/claude-code](https://github.com/anthropics/claude-code): [#319 "A tendency to re-write tests so they pass."](https://github.com/anthropics/claude-code/issues/319) (March 2025), [#7074 "Claude Code manipulates tests instead of following instructions"](https://github.com/anthropics/claude-code/issues/7074) (September 2025), [#45550 "Claude Code normalizes broken tests with @skip instead of fixing them"](https://github.com/anthropics/claude-code/issues/45550) (April 2026).

What the evidence supports, and what it does not: the paper shows that an explicit "stop if the tests are flawed" instruction and an abort path reduce cheating for some models. Catch-22 gives the agent both, plus evidence to show a human. Whether it reduces cheating for a given model is exactly what section 4 above has not measured yet.

## Install (30 seconds)

The skill is the whole folder: `SKILL.md` plus `scripts/c22.py`.

| Agent | Project-level | Global |
|---|---|---|
| Claude Code | `.claude/skills/catch-22` | `~/.claude/skills/catch-22` |
| OpenAI Codex | `.agents/skills/catch-22` | `~/.agents/skills/catch-22` |
| Gemini CLI | `.agents/skills/catch-22` or `.gemini/skills/catch-22` | `~/.gemini/skills/catch-22` |
| Cursor | `.agents/skills/catch-22` or `.cursor/skills/catch-22` | `~/.cursor/skills/catch-22` |
| opencode | `.agents/skills/catch-22`, `.opencode/skills/catch-22` or `.claude/skills/catch-22` | `~/.config/opencode/skills/catch-22` |
| Neovarch Agent | | `~/.neovarch/skills/catch-22` |

```bash
# example: one copy that Codex, Gemini CLI, Cursor and opencode all read, inside your project
git clone --depth 1 https://github.com/Maftuuh1922/catch-22-skill .agents/skills/catch-22

# Claude Code, for all projects
git clone --depth 1 https://github.com/Maftuuh1922/catch-22-skill ~/.claude/skills/catch-22
```

**Any other agent or model:** paste `SKILL.md` into your `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules` or system prompt, and replace `SKILL_DIR` with the path where you put `scripts/c22.py`. The skill does not depend on a model or vendor.

**As a CLI / CI gate:** `pipx install git+https://github.com/Maftuuh1922/catch-22-skill` gives you `c22`, or call `python3 scripts/c22.py` directly.

```yaml
# .github/workflows/no-test-tampering.yml (fails a PR that edits, skips or loosens tests)
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- run: python3 scripts/c22.py tamper --base origin/${{ github.base_ref }}
```

## Usage

```bash
c22 check [PATHS...] --spec ISSUE.md --run "pytest -q"   # verdict + report (+ tamper scan)
c22 check --alias candidate=solve                          # tests call the function by another name
c22 check --json | --report C22-REPORT.md | -q            # machine output, markdown file, exit code only
c22 tamper [--base REF] [--strict] [--json]                # tampering in the working diff
c22 baseline                                               # snapshot tests now (projects without git)
```

| Exit | Verdict | What the agent does |
|---:|---|---|
| 0 | fixable | No contradiction found: keep fixing the implementation, never the tests. |
| 10 | contradictory | Stop. Show both lines, the repro, and the three options. Wait for a human. |
| 11 | unclear | Stateful, mocked or unparsed assertions: run the manual protocol in `SKILL.md`. |
| 12 | tampered | Revert the test edits unless the user asked for that exact change. |

## How it decides

- Assertions are normalised to `(expression, expectation)`. Python uses the `ast` module; JS/TS uses a small scanner for `expect(...)` matchers and `assert.*`.
- Two expectations on the same expression conflict when their literal values differ, when one requires a return value and the other an exception, or when truthiness disagrees.
- A conflict is **strong** (verdict `contradictory`) only if the expression is a call with literal arguments to a plain function or an imported module function, and nothing in between could change the result (no fixtures, mocks or parametrisation; `result = f(1); assert result == ...` is followed, any later use of `result` drops it). Method calls on local objects, like `user.has_perm("x")`, are never strong; they make the verdict `unclear`.
- Spec lines that describe today's buggy behaviour ("currently returns", "right now gives", "instead") are ignored. If the test command passes, the verdict is `fixable` no matter what the static pass found.

## Limitations

- Static analysis proves contradictions between **literal** expectations. Most real-repo contradictions involve objects and computed values; there c22 says `unclear` and the skill falls back to a written checklist the agent follows.
- A one-off change to an undocumented expected value is indistinguishable from a real requirement. Catch-22 cannot detect it; only the "never edit tests" rule and `c22 tamper` protect you there.
- The implementation heuristics (special-casing, `__eq__`, sniffing) are warnings with false negatives (16% of special-casing cases in the corpus) and possible false positives.
- `c22 tamper` sees the working tree. It does not see history an agent rewrote or files outside the project.
- Python and JS/TS only for now. Go, Rust and Java tests are not parsed.
- The skill is instructions. A model can ignore instructions; that is why the `tamper` gate also works as a CI check that does not need the model's cooperation.
- No live-model numbers yet (see Benchmarks, section 4).

## Bahasa Indonesia (ringkas)

**Catch-22** adalah *agent skill* (format `SKILL.md`, bisa dipakai di Claude Code, Codex, Gemini CLI, Cursor, opencode, Neovarch Agent, model apa pun) plus satu skrip Python tanpa dependensi.

- Saat tes terus gagal, agen menjalankan `c22 check`. Kalau spesifikasi dan tes (atau dua tes) saling bertentangan, agen **berhenti** dan menunjukkan buktinya: baris spek, baris assertion, dan skrip repro. Agen tidak mengedit, men-skip, atau menghapus tes.
- `c22 tamper` mendeteksi tes yang diubah, di-skip, dihapus, atau dilonggarkan di diff kerja. Bisa dipakai sebagai gate CI.
- Hasil benchmark (tanpa model): di Impossible-LiveCodeBench, 97,1% tugas *conflicting* terdeteksi dengan 0 alarm palsu. Di Impossible-SWEbench deteksi statis rendah (3,2%) dan sebagian besar jatuh ke `unclear`. Ini batasan analisis statis, dan sudah ditulis apa adanya.
- Angka "dengan vs tanpa skill" untuk model sungguhan **belum diukur**. Harness-nya sudah tersedia di `bench/agent_harness.py`.

Pasang: salin folder ini ke `.agents/skills/catch-22` (Codex, Gemini CLI, Cursor, opencode), `.claude/skills/catch-22` (Claude Code), atau `~/.neovarch/skills/catch-22` (Neovarch Agent).

## Development

```bash
python3 -m pytest -q                       # unit tests
python3 bench/make_mini.py                 # regenerate c22-mini
python3 bench/agent_harness.py validate    # quality control for c22-mini (needs npm for the vitest tasks)
python3 bench/impossiblebench_static.py    # static benchmark (network)
python3 bench/tamper_corpus.py             # tamper corpus
```

## License

MIT. ImpossibleBench data is downloaded at run time from the authors' Hugging Face datasets and is not redistributed here.
