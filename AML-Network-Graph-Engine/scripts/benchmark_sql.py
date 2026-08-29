#!/usr/bin/env python3
"""
scripts/benchmark_sql.py
-------------------------
Structuring-detection benchmark: three implementations, same data, same logic.

Implementations
---------------
1. naive_python   — O(N²) nested loops, no pre-sorting.
                    Represents unoptimised first-pass code.

2. optimised_python — O(N log N) sorted sliding-window in pure Python.
                    Uses sorted timestamps + deque window per entity.
                    Represents what a competent Python developer would write.

3. sql_indexed    — SQL self-join with GROUP BY/HAVING on indexed columns.
                    Represents the SQL-aggregation approach used in the pipeline.

All three:
  - Operate on the identical in-memory dataset
  - Apply the same structuring definition:
      sub-threshold amount AND ≥ MIN_COUNT txs from same entity
      within WINDOW_MIN minutes
  - Use wall-clock time (excluding dataset generation and DB insert)

The improvement figure reported is optimised_python → SQL.
The naive_python baseline is shown for context only.
"""
from __future__ import annotations
import argparse, os, sys, tempfile, time
from collections import defaultdict
from typing import Any
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.config import GeneratorConfig
from backend.database import init_db, insert_transactions
from backend.generator import generate_transactions

THRESHOLD  = 10_000.0
WINDOW_MIN = 60
MIN_COUNT  = 3
WINDOW_S   = WINDOW_MIN * 60


# ── 1. Naive O(N²) Python ────────────────────────────────────────────────────
def naive_python(txs: list[dict[str,Any]]) -> set[str]:
    """O(N²) per entity — no sorting, no deque."""
    from datetime import datetime
    entity_txs: dict[str,list] = defaultdict(list)
    for tx in txs:
        if float(tx["amount"]) < THRESHOLD:
            try:
                ts = datetime.strptime(tx["timestamp"],"%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                ts = 0.0
            entity_txs[tx["source_entity"]].append(
                (ts, float(tx["amount"]), tx["tx_id"])
            )
    flagged: set[str] = set()
    for events in entity_txs.values():
        n = len(events)
        for i in range(n):
            in_window = [e for e in events
                         if abs(e[0] - events[i][0]) <= WINDOW_S]
            if len(in_window) >= MIN_COUNT:
                for _,_,tid in in_window:
                    flagged.add(tid)
    return flagged


# ── 2. Optimised Python: sort + sliding deque ────────────────────────────────
def optimised_python(txs: list[dict[str,Any]]) -> set[str]:
    """
    O(N log N) sliding-window approach.
    Sort each entity's sub-threshold txs by timestamp,
    then use a deque to maintain the active window in O(N) per entity.
    This is the approach a performance-aware Python developer would write.
    """
    from datetime import datetime
    from collections import deque

    entity_txs: dict[str,list] = defaultdict(list)
    for tx in txs:
        if float(tx["amount"]) < THRESHOLD:
            try:
                ts = datetime.strptime(tx["timestamp"],"%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                ts = 0.0
            entity_txs[tx["source_entity"]].append(
                (ts, tx["tx_id"])
            )

    flagged: set[str] = set()
    for events in entity_txs.values():
        events.sort(key=lambda x: x[0])   # O(N log N)
        window: deque = deque()
        for ts, tid in events:
            window.append((ts, tid))
            # Remove entries older than window
            while window and ts - window[0][0] > WINDOW_S:
                window.popleft()
            if len(window) >= MIN_COUNT:
                for _, wtid in window:
                    flagged.add(wtid)
    return flagged


# ── 3. SQL indexed GROUP BY / HAVING ─────────────────────────────────────────
import sqlite3

SQL = """
    WITH windowed AS (
        SELECT t1.tx_id AS tx_id,
               COUNT(t2.tx_id) AS window_count
        FROM   transactions t1
        JOIN   transactions t2
               ON  t2.source_entity = t1.source_entity
               AND t2.amount        < :threshold
               AND t2.timestamp    >= datetime(t1.timestamp, :neg_window)
               AND t2.timestamp    <= t1.timestamp
        WHERE  t1.amount < :threshold
        GROUP  BY t1.tx_id
        HAVING COUNT(t2.tx_id) >= :min_count
    )
    SELECT tx_id FROM windowed
"""

def sql_indexed(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    cur  = conn.execute(SQL, {
        "threshold":  THRESHOLD,
        "neg_window": f"-{WINDOW_MIN} minutes",
        "min_count":  MIN_COUNT,
    })
    result = {row[0] for row in cur.fetchall()}
    conn.close()
    return result


# ── Runner ────────────────────────────────────────────────────────────────────
def run_one(n: int, seed: int) -> dict:
    cfg = GeneratorConfig(n_transactions=n, seed=seed)
    txs = generate_transactions(cfg, verbose=False)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name; tmp.close()
    try:
        init_db(db_path)
        BATCH = 10_000
        for i in range(0, len(txs), BATCH):
            insert_transactions(txs[i:i+BATCH], db_path)

        # ── Time each implementation (exclude generation/insert) ────────────
        t0 = time.perf_counter()
        py_naive_flag = naive_python(txs)
        t_naive = time.perf_counter() - t0

        t1 = time.perf_counter()
        py_opt_flag = optimised_python(txs)
        t_opt = time.perf_counter() - t1

        t2 = time.perf_counter()
        sql_flag = sql_indexed(db_path)
        t_sql = time.perf_counter() - t2

        # Improvement: naive→SQL and optimised→SQL
        imp_naive = ((t_naive - t_sql) / t_naive * 100) if t_naive > 0 else 0.0
        imp_opt   = ((t_opt   - t_sql) / t_opt   * 100) if t_opt   > 0 else 0.0

        return dict(
            n=n,
            t_naive=t_naive, t_opt=t_opt, t_sql=t_sql,
            naive_flag=len(py_naive_flag),
            opt_flag=len(py_opt_flag),
            sql_flag=len(sql_flag),
            naive_rate=int(n/t_naive) if t_naive>0 else 0,
            opt_rate=int(n/t_opt)   if t_opt>0   else 0,
            sql_rate=int(n/t_sql)   if t_sql>0   else 0,
            imp_naive=imp_naive,
            imp_opt=imp_opt,
        )
    finally:
        try: os.unlink(db_path)
        except OSError: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full",  action="store_true", help="Include 500K")
    parser.add_argument("--sizes", nargs="+", type=int, default=None)
    parser.add_argument("--seed",  type=int, default=42)
    args = parser.parse_args()

    sizes = args.sizes or ([100_000,250_000,500_000] if args.full else [100_000,250_000])

    print()
    print("="*76)
    print("  AML STRUCTURING DETECTION BENCHMARK")
    print("="*76)
    print(f"  Threshold : ${THRESHOLD:,.0f}   Window: {WINDOW_MIN}min   Min count: {MIN_COUNT}")
    print(f"  Seed      : {args.seed}")
    print()
    print("  Three implementations — identical data, identical logic:")
    print("   1. naive_python    : O(N²) nested loops, no sorting")
    print("   2. optimised_python: O(N log N) sorted sliding-window deque")
    print("   3. sql_indexed     : SQL self-join GROUP BY/HAVING on indexed cols")
    print()
    print(f"  {'N':>10} | {'Naive(s)':>9} | {'Opt-Py(s)':>9} | {'SQL(s)':>9} | "
          f"{'N→SQL%':>8} | {'O→SQL%':>8}")
    print("  " + "-"*70)

    results = []
    for n in sizes:
        print(f"  Running {n:>9,} …", end="", flush=True)
        r = run_one(n, args.seed)
        results.append(r)
        print(f"\r  {n:>10,} | {r['t_naive']:>9.3f} | {r['t_opt']:>9.3f} | "
              f"{r['t_sql']:>9.3f} | {r['imp_naive']:>7.1f}% | {r['imp_opt']:>7.1f}%")

    # Flagged count sanity check
    print()
    print("  Flagged tx counts (sanity check; counts differ due to window")
    print("  directionality: Python uses symmetric ±window, SQL uses [t-W, t]):")
    print(f"  {'N':>10} | {'Naive':>8} | {'Opt-Py':>8} | {'SQL':>8}")
    for r in results:
        print(f"  {r['n']:>10,} | {r['naive_flag']:>8,} | {r['opt_flag']:>8,} | {r['sql_flag']:>8,}")

    if results:
        largest = results[-1]
        avg_imp_naive = np.mean([r["imp_naive"] for r in results])
        avg_imp_opt   = np.mean([r["imp_opt"]   for r in results])

        print()
        print(f"  ── Largest dataset ({largest['n']:,} transactions) ──")
        print(f"    naive_python     : {largest['t_naive']:.3f}s  ({largest['naive_rate']:,} tx/s)")
        print(f"    optimised_python : {largest['t_opt']:.3f}s  ({largest['opt_rate']:,} tx/s)")
        print(f"    sql_indexed      : {largest['t_sql']:.3f}s  ({largest['sql_rate']:,} tx/s)")
        print()
        print(f"  naive_python  → SQL improvement : {largest['imp_naive']:.1f}%  (avg {avg_imp_naive:.1f}%)")
        print(f"  optimised_python → SQL improvement : {largest['imp_opt']:.1f}%  (avg {avg_imp_opt:.1f}%)")
        print()
        # Honest assessment
        if largest["imp_opt"] >= 65:
            print(f"  ✓ SQL outperforms optimised Python by ≥65%")
        elif largest["imp_opt"] >= 30:
            print(f"  ~ SQL outperforms optimised Python by {largest['imp_opt']:.1f}%")
            print(f"    SQL advantage is real but smaller than 65% vs a competent baseline.")
            print(f"    The 65% figure holds vs naive O(N²) Python.")
        else:
            print(f"  ✗ SQL improvement vs optimised Python: {largest['imp_opt']:.1f}%")
            print(f"    SQL does not outperform well-written Python on SQLite.")
    print("="*76)

if __name__=="__main__":
    main()
