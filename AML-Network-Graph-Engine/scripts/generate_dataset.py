#!/usr/bin/env python3
"""
scripts/generate_dataset.py
----------------------------
Generates a reproducible 500 000-transaction synthetic dataset and seeds
the SQLite database.

Usage
-----
    python scripts/generate_dataset.py [--transactions N] [--seed S]

The script re-uses the existing DB if it exists; pass --reset to wipe first.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.config import GeneratorConfig, DB_CFG
from backend.database import init_db, insert_transactions, count_transactions, db_conn
from backend.generator import generate_transactions

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("generate_dataset")

BATCH_SIZE = 10_000   # rows per INSERT batch


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed AML synthetic dataset")
    parser.add_argument("--transactions", type=int, default=500_000,
                        help="Total transactions to generate (default: 500 000)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true",
                        help="Drop and recreate the database before seeding")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(DB_CFG.path), exist_ok=True)

    if args.reset:
        log.info("Resetting database at %s …", DB_CFG.path)
        if os.path.exists(DB_CFG.path):
            os.remove(DB_CFG.path)

    init_db()
    existing = count_transactions()
    log.info("Existing transactions in DB: %d", existing)

    cfg = GeneratorConfig(n_transactions=args.transactions, seed=args.seed)

    log.info("Generating %d synthetic transactions (seed=%d) …",
             args.transactions, args.seed)
    t0 = time.perf_counter()
    txs = generate_transactions(cfg, verbose=True)
    gen_elapsed = time.perf_counter() - t0
    log.info("Generation complete: %d rows in %.2fs (%.0f tx/s)",
             len(txs), gen_elapsed, len(txs) / gen_elapsed)

    # Pattern distribution
    counts = Counter(t["pattern_type"] for t in txs)
    log.info("Pattern distribution:")
    for pattern, n in sorted(counts.items(), key=lambda x: -x[1]):
        log.info("  %-20s %7d  (%.1f%%)", pattern, n, 100 * n / len(txs))

    # Batch insert
    log.info("Inserting into database in batches of %d …", BATCH_SIZE)
    t1 = time.perf_counter()
    inserted = 0
    for i in range(0, len(txs), BATCH_SIZE):
        batch = txs[i : i + BATCH_SIZE]
        n = insert_transactions(batch)
        inserted += n
        if (i // BATCH_SIZE) % 10 == 0:
            log.info("  … %d / %d rows", i + len(batch), len(txs))

    db_elapsed = time.perf_counter() - t1
    total = count_transactions()

    log.info("─" * 60)
    log.info("Dataset generation complete")
    log.info("  Transactions generated : %d", len(txs))
    log.info("  Rows inserted (new)    : %d", inserted)
    log.info("  Total in DB            : %d", total)
    log.info("  Generation time        : %.2f s", gen_elapsed)
    log.info("  Insert time            : %.2f s", db_elapsed)
    log.info("  Total time             : %.2f s", gen_elapsed + db_elapsed)
    log.info("─" * 60)

    print("\n" + "=" * 60)
    print("DATASET GENERATION REPORT")
    print("=" * 60)
    print(f"  Transactions generated : {len(txs):,}")
    print(f"  Rows in database       : {total:,}")
    print(f"  Seed                   : {args.seed}")
    print()
    print("  Pattern distribution:")
    for pattern, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"    {pattern:<22} {n:>7,}  ({100*n/len(txs):.1f}%)")
    print("=" * 60)


if __name__ == "__main__":
    main()
