#!/usr/bin/env python3
"""`c22 check` verdicts on c22-mini, after the reference fix is applied.

This is the state an honest agent reaches: the real bug is fixed, some tests still fail.
Possible tasks should come out "fixable" (tests pass); impossible ones should come out
"contradictory". JS tasks need vitest (pass --node-modules or let the harness install it).

  python3 bench/mini_verdicts.py [--skip-js] [--node-modules DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from agent_harness import MINI, load_tasks, node_modules_dir, prepare  # noqa: E402

C22 = os.path.join(HERE, "..", "scripts", "c22.py")
NAMES = {0: "fixable", 10: "contradictory", 11: "unclear", 12: "tampered"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-js", action="store_true")
    ap.add_argument("--node-modules")
    ap.add_argument("--only", action="append")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    a = ap.parse_args()
    nm = None if a.skip_js else node_modules_dir(a.node_modules)
    rows = []
    for t in load_tasks(MINI, a.only):
        if t["lang"] == "js" and not nm:
            continue
        w = prepare(t, False, nm, solution=True)
        p = subprocess.run([sys.executable, C22, "check", "--spec", "ISSUE.md", "--run", t["test_cmd"], "--json", "--no-tamper"],
                           cwd=w, capture_output=True, text=True, timeout=600)
        verdict = NAMES.get(p.returncode, "error %d" % p.returncode)
        kinds = []
        try:
            kinds = sorted({c["kind"] for c in json.loads(p.stdout)["contradictions"] if c["strength"] == "strong"})
        except Exception:
            pass
        rows.append({"task": t["id"], "variant": t["variant"], "possible": t["possible"], "verdict": verdict, "evidence": kinds})
        print("%-28s %-12s %-14s %s" % (t["id"], t["variant"], verdict, ",".join(kinds)))
        shutil.rmtree(w, ignore_errors=True)
    by = Counter((r["variant"], r["verdict"]) for r in rows)
    print("\n" + "\n".join("%-12s %-14s %d" % (k[0], k[1], v) for k, v in sorted(by.items())))
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "mini_verdicts%s.json" % ("_" + "_".join(a.only) if a.only else ""))
    with open(path, "w") as fh:
        json.dump(rows, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
