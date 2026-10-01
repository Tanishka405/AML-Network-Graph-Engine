#!/usr/bin/env python3
"""
scripts/verify_reproducibility.py

Runs `scripts/build_analytics_dataset.py` N times from a completely clean
state (fresh DB each time) and checks:

  1. Every alert-level DB aggregate (count, HIGH/CRITICAL/MEDIUM/LOW
     counts, avg risk score, avg anomaly score) is identical across runs.
  2. Every exports/*.csv file is byte-identical across runs, EXCEPT the
     two files that intentionally carry a wall-clock provenance
     timestamp (kpi_summary.csv's `generated_at_utc` row and
     data_quality_run_history.csv's `run_timestamp` column) -- for those
     two files, every field OTHER than the timestamp must still match
     exactly, run to run.

This distinguishes genuine nondeterminism (a bug) from an intentional
"when was this built" timestamp (not a bug, and not something this
script papers over by deleting the field -- it explicitly checks that
the timestamp is the ONLY thing that differs, not just ignores the
whole file).

Usage:
    python scripts/verify_reproducibility.py --runs 5 --transactions 5000
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files where a wall-clock timestamp is expected to differ run-to-run;
# every OTHER byte in these files must still match.
TIMESTAMPED_FILES = {"kpi_summary.csv", "kpi_summary.json", "data_quality_run_history.csv"}


def _clean():
    for p in (os.path.join(ROOT, "data", "aml.db"),
              os.path.join(ROOT, "data_quality_report.json")):
        if os.path.exists(p):
            os.remove(p)
    exp = os.path.join(ROOT, "exports")
    if os.path.exists(exp):
        shutil.rmtree(exp)


def _run_build(n_transactions: int, seed: int) -> None:
    subprocess.run(
        [sys.executable, "scripts/build_analytics_dataset.py",
         "--transactions", str(n_transactions), "--seed", str(seed)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )


def _db_summary() -> dict:
    conn = sqlite3.connect(os.path.join(ROOT, "data", "aml.db"))
    tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    row = conn.execute("""
        SELECT COUNT(*),
               SUM(risk_label='HIGH'), SUM(risk_label='CRITICAL'),
               SUM(risk_label='MEDIUM'), SUM(risk_label='LOW'),
               ROUND(AVG(risk_score), 8), ROUND(AVG(anomaly_score), 8)
        FROM alerts
    """).fetchone()
    conn.close()
    return dict(transactions=tx, alerts=row[0], high=row[1], critical=row[2],
                medium=row[3], low=row[4], avg_risk=row[5], avg_anomaly=row[6])


def _export_snapshot() -> dict:
    """Return {relative_path: sha256} for every exports/*.csv, plus the
    non-timestamp content of the two timestamped files, hashed separately
    with the timestamp field stripped so we can tell 'only the timestamp
    differs' apart from 'something else silently changed too'."""
    snap = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "exports", "*"))):
        name = os.path.basename(f)
        raw = open(f, "rb").read()
        if name == "kpi_summary.csv":
            lines = raw.decode().splitlines()
            stripped = "\n".join(l for l in lines if not l.startswith("generated_at_utc"))
            snap[name + ":non_ts"] = hashlib.sha256(stripped.encode()).hexdigest()
        elif name == "kpi_summary.json":
            d = json.loads(raw)
            d.pop("generated_at_utc", None)
            snap[name + ":non_ts"] = hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
        elif name == "data_quality_run_history.csv":
            lines = raw.decode().splitlines()
            # drop the first CSV field (run_timestamp) of every data row
            stripped = "\n".join(
                (",".join(l.split(",")[1:]) if i > 0 else l)
                for i, l in enumerate(lines)
            )
            snap[name + ":non_ts"] = hashlib.sha256(stripped.encode()).hexdigest()
        else:
            snap[name] = hashlib.sha256(raw).hexdigest()
    return snap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--transactions", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    db_summaries = []
    export_snapshots = []
    for i in range(args.runs):
        print(f"[{i+1}/{args.runs}] clean build (transactions={args.transactions}, seed={args.seed})...")
        _clean()
        _run_build(args.transactions, args.seed)
        db_summaries.append(_db_summary())
        export_snapshots.append(_export_snapshot())
        print(f"  {json.dumps(db_summaries[-1])}")

    print("\n=== DB-level determinism (transactions/alerts/labels/avg scores) ===")
    db_ok = all(s == db_summaries[0] for s in db_summaries[1:])
    print("IDENTICAL across all runs:" , db_ok)
    if not db_ok:
        for i, s in enumerate(db_summaries):
            print(f"  run {i+1}: {s}")

    print("\n=== Export-level determinism (content hashes, timestamps excluded) ===")
    keys = export_snapshots[0].keys()
    all_ok = True
    for k in sorted(keys):
        vals = {snap[k] for snap in export_snapshots}
        ok = len(vals) == 1
        all_ok &= ok
        print(f"  {'OK  ' if ok else 'DIFF'}  {k}  ({len(vals)} distinct hash(es))")
    print("\nALL EXPORTS IDENTICAL (excluding provenance timestamps):", all_ok)

    if not (db_ok and all_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
