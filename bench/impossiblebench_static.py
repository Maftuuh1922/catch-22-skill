#!/usr/bin/env python3
"""Run Catch-22's static contradiction detector on the public ImpossibleBench datasets.

No model is involved. The question this answers: given only what an agent can see
(the spec / issue text and the test files), how often does `c22 check` say
"contradictory" on the impossible variants, and how often does it wrongly say so on
the original (possible) variants?

Data (public, Hugging Face, by the ImpossibleBench authors):
  fjzzq2002/impossible_livecodebench   103 tasks x {original, oneoff, conflicting}
  fjzzq2002/impossible_swebench        349 tasks x {original, oneoff, conflicting}

For Impossible-SWEbench the full test files are fetched from GitHub at each task's
base_commit and the task's test_patch is applied, so the detector sees the same test
files an agent would see. The issue text is the spec. FAIL_TO_PASS gives the failing
tests (what an agent learns by running the suite once).

Usage:
  python3 bench/impossiblebench_static.py --data bench/.data [--only lcb|swe] [--out bench/results]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import c22  # noqa: E402

DATASETS = {"lcb": "impossible_livecodebench", "swe": "impossible_swebench"}
SIZES = {"lcb": 103, "swe": 349}
SPLITS = ["original", "oneoff", "conflicting"]


def fetch_rows(data: str, short: str, split: str) -> List[Dict[str, Any]]:
    path = os.path.join(data, "%s_%s.jsonl" % (short, split))
    if os.path.exists(path):
        return [json.loads(l) for l in open(path)]
    os.makedirs(data, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    off = 0
    while off < SIZES[short]:
        url = ("https://datasets-server.huggingface.co/rows?dataset=fjzzq2002/%s&config=default&split=%s&offset=%d&length=100"
               % (DATASETS[short], split, off))
        for attempt in range(5):
            try:
                d = json.load(urllib.request.urlopen(url, timeout=60))
                break
            except Exception:
                time.sleep(2 + attempt * 2)
        else:
            raise SystemExit("could not download %s/%s" % (short, split))
        rows += [r["row"] for r in d["rows"]]
        off += 100
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return rows


# ---------------------------------------------------------------------------------- LCB

def eval_lcb(row: Dict[str, Any]) -> Dict[str, Any]:
    al = {"candidate": row["entry_point"]}
    tests, err = c22.extract("test_%s.py" % row["task_id"], row["test"], al)
    specs = c22.extract_spec_from_source("func.py", row["prompt"], al)
    cons = c22.find_contradictions(tests, specs)
    strong = [c for c in cons if c.strength == "strong"]
    weak = [c for c in cons if c.strength == "weak"]
    verdict = "contradictory" if strong else ("unclear" if weak else "fixable")
    ev = strong[0] if strong else None
    return {
        "task_id": row["task_id"], "verdict": verdict, "kinds": sorted({c.kind for c in strong}),
        "assertions": len(tests), "spec_examples": len(specs), "parse_error": err,
        "evidence": None if ev is None else {"a": ev.a.text[:160], "b": ev.b.text[:160], "reason": ev.reason},
    }


# ---------------------------------------------------------------------------------- SWE

def _patch_files(patch: str) -> List[str]:
    return [m.group(2) for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)", patch, re.M)]


def fetch_swe_sources(data: str, rows: List[Dict[str, Any]]) -> None:
    jobs = set()
    for r in rows:
        for p in _patch_files(r["test_patch"]):
            jobs.add((r["repo"], r["base_commit"], p))
    src_root = os.path.join(data, "swe_src")

    def get(job):
        repo, commit, path = job
        out = os.path.join(src_root, repo.replace("/", "__"), commit, path)
        miss = out + ".missing"
        if os.path.exists(out) or os.path.exists(miss):
            return
        url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, commit, path)
        for attempt in range(3):
            try:
                body = urllib.request.urlopen(url, timeout=30).read()
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as fh:
                    fh.write(body)
                return
            except urllib.error.HTTPError as e:
                if e.code == 404:  # file is created by the patch
                    os.makedirs(os.path.dirname(miss), exist_ok=True)
                    open(miss, "w").close()
                    return
                time.sleep(1 + attempt)
            except Exception:
                time.sleep(1 + attempt)

    with cf.ThreadPoolExecutor(16) as ex:
        list(ex.map(get, sorted(jobs)))


def eval_swe(row: Dict[str, Any], data: str) -> Dict[str, Any]:
    files = _patch_files(row["test_patch"])
    tmp = tempfile.mkdtemp(prefix="c22swe_")
    try:
        src_root = os.path.join(data, "swe_src", row["repo"].replace("/", "__"), row["base_commit"])
        for p in files:
            s = os.path.join(src_root, p)
            if os.path.exists(s):
                d = os.path.join(tmp, p)
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy(s, d)
        pf = os.path.join(tmp, ".c22.patch")
        with open(pf, "w") as fh:
            fh.write(row["test_patch"])
        ap = subprocess.run(["git", "apply", "--unsafe-paths", "--whitespace=nowarn", pf], cwd=tmp, capture_output=True, text=True)
        if ap.returncode != 0:
            return {"task_id": row["instance_id"], "verdict": "skipped", "why": "patch did not apply: " + ap.stderr[:120]}
        failing = list(row.get("FAIL_TO_PASS") or [])
        res = c22.analyze(tmp, files, [], {}, failing, scan_docstrings=False,
                          spec_texts=[("ISSUE", row.get("problem_statement") or "")])
        strong = [c for c in res["relevant"] if c.strength == "strong"]
        weak = [c for c in res["relevant"] if c.strength == "weak"]
        verdict = "contradictory" if strong else ("unclear" if weak else "fixable")
        ev = strong[0] if strong else (weak[0] if weak else None)
        return {
            "task_id": row["instance_id"], "verdict": verdict, "kinds": sorted({c.kind for c in strong}),
            "assertions": len(res["tests"]), "spec_examples": len(res["specs"]), "parse_errors": res["parse_errors"][:2],
            "evidence": None if ev is None else {"a": ev.a.text[:160], "b": ev.b.text[:160], "reason": ev.reason, "strength": ev.strength},
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summarize(name: str, split: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    c = Counter(r["verdict"] for r in results)
    n = sum(v for k, v in c.items() if k != "skipped")
    return {"dataset": name, "split": split, "n": n, "skipped": c.get("skipped", 0),
            "contradictory": c.get("contradictory", 0), "unclear": c.get("unclear", 0), "fixable": c.get("fixable", 0),
            "contradictory_rate": round(c.get("contradictory", 0) / n, 4) if n else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, ".data"))
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    ap.add_argument("--only", choices=["lcb", "swe"])
    ap.add_argument("--split", choices=SPLITS, action="append", help="limit to these splits (repeatable)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    summary = []
    for short in (["lcb", "swe"] if not a.only else [a.only]):
        for split in (a.split or SPLITS):
            rows = fetch_rows(a.data, short, split)
            if short == "swe":
                fetch_swe_sources(a.data, rows)
                with cf.ThreadPoolExecutor(8) as ex:
                    results = list(ex.map(lambda r: eval_swe(r, a.data), rows))
            else:
                results = [eval_lcb(r) for r in rows]
            with open(os.path.join(a.out, "static_%s_%s.jsonl" % (short, split)), "w") as fh:
                for r in results:
                    fh.write(json.dumps(r) + "\n")
            s = summarize("Impossible-" + ("LiveCodeBench" if short == "lcb" else "SWEbench"), split, results)
            summary.append(s)
            print("%-28s %-12s n=%-4d contradictory=%-4d (%.1f%%) unclear=%-4d fixable=%-4d skipped=%d" % (
                s["dataset"], split, s["n"], s["contradictory"], 100 * (s["contradictory_rate"] or 0), s["unclear"], s["fixable"], s["skipped"]))
    sp = os.path.join(a.out, "static_summary.json")
    old = json.load(open(sp))["summary"] if os.path.exists(sp) else []
    keep = [o for o in old if (o["dataset"], o["split"]) not in {(x["dataset"], x["split"]) for x in summary}]
    order = {"original": 0, "oneoff": 1, "conflicting": 2}
    merged = sorted(keep + summary, key=lambda x: (x["dataset"], order[x["split"]]))
    with open(sp, "w") as fh:
        json.dump({"c22_version": c22.VERSION, "summary": merged}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
