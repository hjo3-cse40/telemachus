#!/usr/bin/env python3
"""Measures the Laya memory intake questions on the labelled synthetic set.

Usage (from the repo root):
    python3 tests/benchmarks/laya_memory_calibration.py [--mode auto|english|multilingual]
                                                        [--questions path.json]

Reads tests/fixtures/memory_intake_eval.json, sends the single-author items (own and
pasted) to telemachus/memory/laya_worker.py in the Laya venv, and prints:
- accuracy per question, per language;
- for each threshold, what a gate built on it would do (how many durable facts it
  would wrongly skip, how many pasted texts it would wrongly accept as own words).
The thresholds in telemachus/memory/intake.py come from this output.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "memory_intake_eval.json"
WORKER = ROOT / "telemachus" / "memory" / "laya_worker.py"
LAYA_PYTHON = os.environ.get("LAYA_PYTHON", str(Path.home() / "laya/.venv/bin/python"))


def run_worker(texts, mode, questions_path=None):
    env = dict(os.environ, TELEMACHUS_LAYA_MEMORY_MODEL=mode)
    if questions_path:
        env["TELEMACHUS_LAYA_MEMORY_QUESTIONS"] = str(Path(questions_path).resolve())
    payload = json.dumps({"records": [{"text": t} for t in texts]}) + "\n"
    proc = subprocess.run([LAYA_PYTHON, "-I", str(WORKER)], input=payload, capture_output=True,
                          text=True, env=env, timeout=900)
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    data = json.loads(line) if line else {"error": proc.stderr[-2000:]}
    if "error" in data:
        sys.exit(f"worker error: {data['error']}")
    return data["results"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto")
    ap.add_argument("--questions", default=None)
    ap.add_argument("--json-out", default=None, help="Write per-item results to this file")
    args = ap.parse_args()

    items = json.loads(FIXTURE.read_text(encoding="utf-8"))
    units = [it for it in items if it["author"] in ("own", "pasted")]
    results = run_worker([it["text"] for it in units], args.mode, args.questions)

    rows = []
    for it, res in zip(units, results):
        rows.append({
            "id": it["id"], "lang": it["lang"], "author": it["author"], "durable": it["durable"],
            "model": res["model"],
            "p_own": res["authorship"]["probabilities"]["own_words"],
            "p_durable": res["durability"]["probabilities"]["durable_personal_fact"],
            "latency_ms": res["latency_ms"],
        })
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def acc(subset, pred, truth):
        if not subset:
            return "n/a"
        ok = sum(1 for r in subset if pred(r) == truth(r))
        return f"{ok}/{len(subset)} = {ok / len(subset):.2f}"

    print(f"mode={args.mode} questions={args.questions or 'built-in'} items={len(rows)} "
          f"latency/item={rows[0]['latency_ms']} ms")
    for lang in ("en", "ko", None):
        sub = [r for r in rows if lang is None or r["lang"] == lang]
        own = [r for r in sub if r["author"] == "own"]
        print(f"[{lang or 'all'}] authorship acc {acc(sub, lambda r: r['p_own'] >= 0.5, lambda r: r['author'] == 'own')}"
              f" | durability acc (own only) {acc(own, lambda r: r['p_durable'] >= 0.5, lambda r: r['durable'])}")

    own = [r for r in rows if r["author"] == "own"]
    pasted = [r for r in rows if r["author"] == "pasted"]
    durable = [r for r in own if r["durable"]]
    not_durable = [r for r in own if not r["durable"]]
    print("\nDurability SKIP gate (skip the 9B call when p_durable < t):")
    print("  t     durable facts lost   non-durable calls saved")
    for t in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
        lost = sum(1 for r in durable if r["p_durable"] < t)
        saved = sum(1 for r in not_durable if r["p_durable"] < t)
        print(f"  {t:<5} {lost:>3}/{len(durable):<16} {saved:>3}/{len(not_durable)}")
    print("\nAuthorship OWN gate (treat as own words when p_own >= t):")
    print("  t     pasted wrongly accepted   own correctly accepted")
    for t in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95):
        bad = sum(1 for r in pasted if r["p_own"] >= t)
        good = sum(1 for r in own if r["p_own"] >= t)
        print(f"  {t:<5} {bad:>3}/{len(pasted):<22} {good:>3}/{len(own)}")

    print("\nPer item (sorted by p_durable):")
    for r in sorted(rows, key=lambda r: r["p_durable"]):
        print(f"  {r['id']:<20} {r['model']:<12} own={r['p_own']:.2f} durable={r['p_durable']:.2f}"
              f"  truth={r['author']}/{'D' if r['durable'] else '-'}")


if __name__ == "__main__":
    main()
