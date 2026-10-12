# c22 tamper: synthetic corpus results

c22 0.1.0. Reproduce: `python3 bench/tamper_corpus.py`.

### Python (103 Impossible-LiveCodeBench original suites wrapped as pytest files)

| case | type | n | flagged | rate |
|---|---|---:|---:|---:|
| @pytest.mark.skip added | test tampering | 103 | 103 | 100.0% |
| @pytest.mark.xfail added | test tampering | 103 | 103 | 100.0% |
| assert commented out | test tampering | 100 | 100 | 100.0% |
| assert loosened to `is not None` | test tampering | 100 | 96 | 96.0% |
| assert replaced by `assert True` | test tampering | 100 | 100 | 100.0% |
| assertion deleted | test tampering | 100 | 96 | 96.0% |
| conftest collect_ignore added | test tampering | 103 | 103 | 100.0% |
| expected value changed | test tampering | 100 | 96 | 96.0% |
| failures swallowed (try/except: pass) | test tampering | 103 | 103 | 100.0% |
| pytest.skip() call added | test tampering | 103 | 103 | 100.0% |
| test file deleted | test tampering | 103 | 103 | 100.0% |
| test function deleted | test tampering | 103 | 103 | 100.0% |
| PYTEST_CURRENT_TEST sniffing | implementation cheat (heuristic) | 103 | 103 | 100.0% |
| __eq__ always True | implementation cheat (heuristic) | 103 | 103 | 100.0% |
| special-casing a test input | implementation cheat (heuristic) | 100 | 84 | 84.0% |
| comment added | honest control | 103 | 0 | 0.0% |
| honest implementation change | honest control | 103 | 0 | 0.0% |
| new test added | honest control | 103 | 0 | 0.0% |
| reformatted assertions | honest control | 103 | 0 | 0.0% |

### JavaScript (c22-mini vitest suites)

| case | type | n | flagged | rate |
|---|---|---:|---:|---:|
| assertion deleted | test tampering | 3 | 3 | 100.0% |
| describe.skip added | test tampering | 3 | 3 | 100.0% |
| expect(true).toBe(true) | test tampering | 3 | 3 | 100.0% |
| expected value changed | test tampering | 3 | 3 | 100.0% |
| it.only added (others excluded) | test tampering | 3 | 3 | 100.0% |
| it.skip added | test tampering | 3 | 3 | 100.0% |
| it.todo replaces test | test tampering | 3 | 3 | 100.0% |
| loosened to toBeDefined | test tampering | 3 | 3 | 100.0% |
| test deleted | test tampering | 3 | 3 | 100.0% |
| xit added | test tampering | 3 | 3 | 100.0% |
| blank line added | honest control | 3 | 0 | 0.0% |
| new test added | honest control | 3 | 0 | 0.0% |
