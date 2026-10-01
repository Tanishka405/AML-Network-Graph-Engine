#!/usr/bin/env python3
"""
scripts/build_analytics_dataset.py
------------------------------------
Builds the full financial-analytics / BI layer on top of the existing AML
engine, end to end:

  1. Initialise a fresh SQLite database.
  2. Generate a reproducible batch of synthetic transactions
     (backend.generator, seeded).
  3. Run every transaction through the REAL processing pipeline
     (backend.streaming._process_transaction) -- the same function the
     live FastAPI/WebSocket service uses. This is intentional: it means
     `alerts` is populated by the actual graph engine, SQL structuring/
     velocity scores, Isolation Forest, AML rules and composite risk
     scorer, not a shortcut or a mock.
  4. Run the deterministic data-quality framework (backend.data_quality)
     and persist results to data_quality_results.
  5. Execute every query in sql/analytics/*.sql against the populated
     database and export each result set as a Power-BI-ready CSV under
     exports/.
  6. Compute the documented KPI layer (see docs/analytics.md) and export
     it as both CSV and JSON.

Reproducibility
----------------
Everything here is seeded (default seed=42) and the transaction count is
deliberately moderate (default 5,000) so the full run completes in about
a minute and the exported dataset stays small enough to commit to Git as
a fixture, per the project's own "do not commit enormous generated
datasets" rule. Re-running this script with the same seed and count
reproduces byte-identical transaction generation (see
tests/test_generator.py) and the same alerts, modulo the Isolation
Forest's own documented retrain schedule.

Usage
-----
    python scripts/build_analytics_dataset.py
    python scripts/build_analytics_dataset.py --transactions 5000 --seed 42
    python scripts/build_analytics_dataset.py --keep-existing-db
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.config import GeneratorConfig, DB_CFG
from backend.database import (
    init_db, db_conn, count_transactions, count_alerts, get_db_path,
)
from backend.generator import generate_transactions
from backend import data_quality as dq

log = logging.getLogger("build_analytics_dataset")

EXPORTS_DIR = os.path.join(ROOT, "exports")
SQL_DIR = os.path.join(ROOT, "sql", "analytics")


# ---------------------------------------------------------------------------
# Step 1-3: data generation through the real pipeline
# ---------------------------------------------------------------------------
def build_dataset(n_transactions: int, seed: int, keep_existing_db: bool) -> None:
    db_path = get_db_path()
    if not keep_existing_db and os.path.exists(db_path):
        os.remove(db_path)
        log.info("Removed existing database at %s for a clean, reproducible run", db_path)

    init_db()

    # Import here (not at module top) so a fresh DB file always exists
    # before streaming's module-level graph singleton is touched.
    from backend.streaming import _process_transaction

    cfg = GeneratorConfig(n_transactions=n_transactions, seed=seed)
    log.info("Generating %d transactions (seed=%d)...", n_transactions, seed)
    txs = generate_transactions(cfg, verbose=False)

    log.info("Running all %d transactions through the real processing "
              "pipeline (graph engine, SQL scores, Isolation Forest, AML "
              "rules, composite risk)...", len(txs))
    t0 = time.perf_counter()
    for i, tx in enumerate(txs):
        _process_transaction(tx)
        if (i + 1) % 1000 == 0:
            log.info("  ...%d / %d processed (%.1fs elapsed)",
                      i + 1, len(txs), time.perf_counter() - t0)
    elapsed = time.perf_counter() - t0

    n_tx = count_transactions()
    n_al = count_alerts()
    log.info("Pipeline run complete in %.1fs: %d transactions, %d alerts "
              "(%.2f%% alert rate)", elapsed, n_tx, n_al,
              100.0 * n_al / n_tx if n_tx else 0.0)


# ---------------------------------------------------------------------------
# Step 4: data quality
# ---------------------------------------------------------------------------
def run_data_quality() -> dict:
    log.info("Running data-quality framework (14 checks)...")
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    report = dq.run_and_persist(
        json_out=os.path.join(ROOT, "data_quality_report.json")
    )
    log.info("Data quality: %d passed, %d warned, %d failed (of %d checks)",
              report["summary"]["passed"], report["summary"]["warned"],
              report["summary"]["failed"], report["summary"]["total_checks"])
    return report


# ---------------------------------------------------------------------------
# Step 5: run every analytics SQL file, export each result set as CSV
# ---------------------------------------------------------------------------
def _strip_sql_comments(sql: str) -> str:
    lines = []
    for line in sql.split("\n"):
        idx = line.find("--")
        lines.append(line[:idx] if idx != -1 else line)
    return "\n".join(lines)


# Maps (sql_file_stem, statement_index) -> output CSV name. Files with one
# statement need no suffix; multi-statement files get one name per result
# set, chosen to match the business question each part answers.
EXPORT_NAME_MAP = {
    ("01_transaction_summary", 0): "transaction_summary_overall.csv",
    ("01_transaction_summary", 1): "transaction_summary_by_currency.csv",
    ("01_transaction_summary", 2): "transaction_summary_by_pattern.csv",
    ("02_daily_risk_trends", 0): "daily_risk_trends.csv",
    ("03_entity_activity", 0): "entity_activity.csv",
    ("04_high_risk_entities", 0): "entity_risk_summary.csv",
    ("05_transaction_velocity", 0): "transaction_velocity.csv",
    ("06_structuring_analysis", 0): "structuring_analysis.csv",
    ("07_network_risk", 0): "network_risk.csv",
    ("08_risk_rankings", 0): "risk_rankings_top_entities.csv",
    ("08_risk_rankings", 1): "risk_distribution_by_category.csv",
    ("09_temporal_analysis", 0): "rolling_volume_trend.csv",
    ("09_temporal_analysis", 1): "entity_month_over_month.csv",
    ("10_alert_analysis", 0): "aml_alert_summary.csv",
    ("10_alert_analysis", 1): "rule_frequency.csv",
    ("10_alert_analysis", 2): "continuous_risk_signal_summary.csv",
    ("11_customer_segmentation", 0): "entity_segmentation.csv",
    ("11_customer_segmentation", 1): "jurisdiction_risk.csv",
    ("12_data_quality", 0): "data_quality_report.csv",
    ("12_data_quality", 1): "data_quality_run_history.csv",
}


def run_analytics_sql() -> dict:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    results_summary = {}
    files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    with db_conn() as conn:
        conn.row_factory = None  # use tuples + description for generic CSV export
        import sqlite3
        conn.row_factory = sqlite3.Row
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            cleaned = _strip_sql_comments(open(f).read())
            statements = [s.strip() for s in cleaned.split(";") if s.strip()]
            for i, stmt in enumerate(statements):
                out_name = EXPORT_NAME_MAP.get((stem, i), f"{stem}_{i}.csv")
                rows = conn.execute(stmt).fetchall()
                out_path = os.path.join(EXPORTS_DIR, out_name)
                if rows:
                    cols = rows[0].keys()
                else:
                    cols = [d[0] for d in conn.execute(stmt + " LIMIT 0").description]
                with open(out_path, "w", newline="") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(cols)
                    for r in rows:
                        writer.writerow([r[c] for c in cols])
                results_summary[out_name] = len(rows)
                log.info("  %-42s %5d rows -> exports/%s", stem + f"[{i}]", len(rows), out_name)
    return results_summary


# ---------------------------------------------------------------------------
# Step 5b: star-schema source model (fact/dim tables) for Power BI
# ---------------------------------------------------------------------------
def export_star_schema() -> dict:
    """
    Exports a genuine fact/dimension model on top of the real
    transactions/alerts tables, in addition to (not instead of) the 19
    pre-aggregated analytical exports above.

    Every column here is either copied directly from transactions/alerts,
    or derived from a value that's already there (e.g. calendar fields
    computed from a date, or a risk threshold read from
    backend.config.RISK_THRESHOLDS -- the same constants the risk engine
    itself uses to assign risk_label, not a number invented for this
    export). No column represents data that doesn't exist in the
    pipeline. See docs/powerbi.md for the full grain/key documentation.
    """
    from backend.config import RISK_THRESHOLDS, AML_CFG

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    counts = {}
    with db_conn() as conn:
        import sqlite3
        conn.row_factory = sqlite3.Row

        # ── dim_entity: one row per entity that appears as a source or dest ──
        rows = conn.execute("""
            SELECT entity FROM (
                SELECT source_entity AS entity FROM transactions
                UNION
                SELECT dest_entity FROM transactions
            ) ORDER BY entity
        """).fetchall()
        _write_csv("dim_entity.csv", ["entity_id"], [[r["entity"]] for r in rows])
        counts["dim_entity.csv"] = len(rows)

        # ── dim_date: one row per date that appears in transactions.timestamp ──
        rows = conn.execute("""
            SELECT DISTINCT DATE(timestamp) AS d FROM transactions ORDER BY d
        """).fetchall()
        date_rows = []
        for r in rows:
            import datetime as _dt
            d = _dt.date.fromisoformat(r["d"])
            date_rows.append([
                r["d"], d.year, d.month, d.day,
                (d.month - 1) // 3 + 1,          # quarter, derived from month
                d.isoweekday(),                   # 1=Mon .. 7=Sun
                d.strftime("%A"),
                1 if d.isoweekday() >= 6 else 0,  # is_weekend, derived
            ])
        _write_csv("dim_date.csv",
                    ["date_key", "year", "month", "day", "quarter",
                     "day_of_week", "day_name", "is_weekend"], date_rows)
        counts["dim_date.csv"] = len(date_rows)

        # ── dim_currency: one row per currency code actually used ──────────
        rows = conn.execute("SELECT DISTINCT currency FROM transactions ORDER BY currency").fetchall()
        _write_csv("dim_currency.csv", ["currency_code"], [[r["currency"]] for r in rows])
        counts["dim_currency.csv"] = len(rows)

        # ── dim_jurisdiction: one row per jurisdiction code (src or dst) ───
        rows = conn.execute("""
            SELECT j FROM (
                SELECT src_jurisdiction AS j FROM transactions
                UNION
                SELECT dst_jurisdiction FROM transactions
            ) ORDER BY j
        """).fetchall()
        jx_rows = [[r["j"], 1 if r["j"] in AML_CFG.high_risk_jurisdictions else 0] for r in rows]
        _write_csv("dim_jurisdiction.csv", ["jurisdiction_code", "is_high_risk"], jx_rows)
        counts["dim_jurisdiction.csv"] = len(jx_rows)

        # ── dim_risk_category: the risk_label taxonomy + real thresholds ───
        # RISK_THRESHOLDS is the exact dict backend.risk_engine uses to
        # assign risk_label from risk_score -- not re-derived or guessed.
        cat_rows = [[label, thresh] for label, thresh in RISK_THRESHOLDS.items()]
        cat_rows.append(["UNFLAGGED", None])  # transactions with no alert row at all
        _write_csv("dim_risk_category.csv", ["risk_category", "min_score_threshold"], cat_rows)
        counts["dim_risk_category.csv"] = len(cat_rows)

        # ── dim_transaction_pattern: the pattern_type labels the generator
        # produces (see backend/data_quality.py VALID_PATTERN_TYPES, verified
        # against live data, not assumed). Descriptions are authored
        # documentation of what each label means in backend/generator.py and
        # backend/aml_rules.py, not sourced data -- flagged as such.
        pattern_descriptions = {
            "normal": "Ordinary transaction; no synthetic anomaly pattern applied.",
            "structuring": "Series of sub-threshold transactions from one source, generated to test structuring detection.",
            "layering": "Multi-hop chain of transactions moving funds through several entities.",
            "circular_flow": "Transactions forming a closed loop back to the originating entity.",
            "velocity_burst": "Many transactions from one source in a short time window.",
            "high_risk_jx": "Transaction generated with a source or destination in a configured high-risk jurisdiction.",
        }
        rows = conn.execute("SELECT DISTINCT pattern_type FROM transactions ORDER BY pattern_type").fetchall()
        pat_rows = [[r["pattern_type"], pattern_descriptions.get(r["pattern_type"], "")] for r in rows]
        _write_csv("dim_transaction_pattern.csv", ["pattern_type", "description"], pat_rows)
        counts["dim_transaction_pattern.csv"] = len(pat_rows)

        # ── fact_transactions: one row per transaction ──────────────────────
        rows = conn.execute("""
            SELECT tx_id, DATE(timestamp) AS date_key, timestamp,
                   source_entity, dest_entity, amount, currency,
                   src_jurisdiction, dst_jurisdiction, pattern_type,
                   inter_arrival_s,
                   EXISTS(SELECT 1 FROM alerts a WHERE a.tx_id = transactions.tx_id) AS is_flagged
            FROM transactions
            ORDER BY tx_id
        """).fetchall()
        _write_csv(
            "fact_transactions.csv",
            ["tx_id", "date_key", "timestamp", "source_entity_key", "dest_entity_key",
             "amount", "currency_key", "src_jurisdiction_key", "dst_jurisdiction_key",
             "pattern_type_key", "inter_arrival_s", "is_flagged"],
            [[r["tx_id"], r["date_key"], r["timestamp"], r["source_entity"], r["dest_entity"],
              r["amount"], r["currency"], r["src_jurisdiction"], r["dst_jurisdiction"],
              r["pattern_type"], r["inter_arrival_s"], r["is_flagged"]] for r in rows],
        )
        counts["fact_transactions.csv"] = len(rows)

        # ── fact_alerts: one row per alert ──────────────────────────────────
        rows = conn.execute("""
            SELECT a.alert_id, a.tx_id, DATE(a.timestamp) AS date_key, a.timestamp,
                   a.risk_score, a.risk_label, a.anomaly_score, a.structuring_score,
                   a.graph_score, a.velocity_score, a.circular_score,
                   a.high_risk_jx_score, a.reason,
                   t.source_entity, t.dest_entity
            FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
            ORDER BY a.alert_id
        """).fetchall()
        _write_csv(
            "fact_alerts.csv",
            ["alert_id", "tx_id", "date_key", "timestamp", "risk_score",
             "risk_category_key", "anomaly_score", "structuring_score", "graph_score",
             "velocity_score", "circular_score", "high_risk_jx_score", "reason",
             "source_entity_key", "dest_entity_key"],
            [[r["alert_id"], r["tx_id"], r["date_key"], r["timestamp"], r["risk_score"],
              r["risk_label"], r["anomaly_score"], r["structuring_score"], r["graph_score"],
              r["velocity_score"], r["circular_score"], r["high_risk_jx_score"], r["reason"],
              r["source_entity"], r["dest_entity"]] for r in rows],
        )
        counts["fact_alerts.csv"] = len(rows)

    for name, n in counts.items():
        log.info("  %-28s %6d rows -> exports/%s", "star_schema", n, name)
    return counts


def _write_csv(filename: str, header: list, rows: list) -> None:
    path = os.path.join(EXPORTS_DIR, filename)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Step 6: KPI layer
# ---------------------------------------------------------------------------
def compute_kpis() -> dict:
    """
    Every KPI here is a single deterministic SQL aggregate against the
    live database -- no numbers are invented. See docs/analytics.md for
    the definition of each.
    """
    with db_conn() as conn:
        total_tx = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        total_value = conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions").fetchone()[0]
        avg_value = conn.execute("SELECT COALESCE(AVG(amount),0) FROM transactions").fetchone()[0]

        # Median via a standard two-side rank comparison (SQLite has no MEDIAN()).
        median_row = conn.execute("""
            SELECT AVG(amount) FROM (
                SELECT amount FROM transactions ORDER BY amount
                LIMIT 2 - (SELECT COUNT(*) FROM transactions) % 2
                OFFSET (SELECT (COUNT(*) - 1) / 2 FROM transactions)
            )
        """).fetchone()
        median_value = median_row[0] if median_row and median_row[0] is not None else 0.0

        total_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        critical_alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE risk_label='CRITICAL'").fetchone()[0]
        high_alerts = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE risk_label='HIGH'").fetchone()[0]

        flagged_value = conn.execute("""
            SELECT COALESCE(SUM(t.amount),0) FROM alerts a
            JOIN transactions t ON t.tx_id = a.tx_id
        """).fetchone()[0]

        structuring_alert_count = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE reason LIKE '%STRUCTURING:%'").fetchone()[0]
        circular_flow_entities = conn.execute("""
            SELECT COUNT(DISTINCT entity) FROM (
                SELECT t.source_entity AS entity FROM alerts a
                JOIN transactions t ON t.tx_id=a.tx_id WHERE a.reason LIKE '%CIRCULAR FLOW:%'
                UNION
                SELECT t.dest_entity FROM alerts a
                JOIN transactions t ON t.tx_id=a.tx_id WHERE a.reason LIKE '%CIRCULAR FLOW:%'
            )
        """).fetchone()[0]

        high_risk_jx_value = conn.execute("""
            SELECT COALESCE(SUM(t.amount),0) FROM transactions t
            WHERE t.src_jurisdiction IN (
                SELECT DISTINCT src_jurisdiction FROM transactions
                WHERE src_jurisdiction IN ('AE','KY','VG','PA','MT','CY','LI')
            )
        """).fetchone()[0]

        avg_risk_score = conn.execute(
            "SELECT COALESCE(AVG(risk_score),0) FROM alerts").fetchone()[0]

        high_risk_entity_count = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT entity FROM (
                    SELECT t.source_entity AS entity, a.risk_score FROM alerts a
                    JOIN transactions t ON t.tx_id=a.tx_id
                    UNION ALL
                    SELECT t.dest_entity, a.risk_score FROM alerts a
                    JOIN transactions t ON t.tx_id=a.tx_id
                )
                GROUP BY entity
                HAVING AVG(risk_score) >= 0.60
            )
        """).fetchone()[0]

    kpis = {
        "total_transactions": total_tx,
        "total_transaction_value": round(total_value, 2),
        "average_transaction_value": round(avg_value, 2),
        "median_transaction_value": round(median_value, 2),
        "total_alerts": total_alerts,
        "suspicious_transaction_rate_pct": round(100.0 * total_alerts / total_tx, 4) if total_tx else 0.0,
        "critical_alerts": critical_alerts,
        "high_alerts": high_alerts,
        "high_risk_entity_count": high_risk_entity_count,
        "flagged_transaction_value": round(flagged_value, 2),
        "flagged_transaction_value_pct": round(100.0 * flagged_value / total_value, 4) if total_value else 0.0,
        "structuring_alert_count": structuring_alert_count,
        "circular_flow_entity_count": circular_flow_entities,
        "high_risk_jurisdiction_exposure_value": round(high_risk_jx_value, 2),
        "average_risk_score": round(avg_risk_score, 4),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return kpis


def export_kpis(kpis: dict) -> None:
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    csv_path = os.path.join(EXPORTS_DIR, "kpi_summary.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["kpi", "value"])
        for k, v in kpis.items():
            writer.writerow([k, v])
    json_path = os.path.join(EXPORTS_DIR, "kpi_summary.json")
    with open(json_path, "w") as f:
        json.dump(kpis, f, indent=2)
    log.info("KPI layer exported: exports/kpi_summary.csv, exports/kpi_summary.json")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--transactions", type=int, default=5000,
                         help="Number of transactions to generate (default: 5000, "
                              "chosen so the full run completes in ~1 minute; see "
                              "docs/analytics.md for why this is not the full 500K "
                              "generator default).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--keep-existing-db", action="store_true",
                         help="Do not wipe data/aml.db first (append instead of a fresh run).")
    args = parser.parse_args()

    t0 = time.perf_counter()
    build_dataset(args.transactions, args.seed, args.keep_existing_db)
    dq_report = run_data_quality()
    export_summary = run_analytics_sql()
    star_schema_summary = export_star_schema()
    kpis = compute_kpis()
    export_kpis(kpis)

    total_elapsed = time.perf_counter() - t0
    log.info("=" * 70)
    log.info("BUILD COMPLETE in %.1fs", total_elapsed)
    log.info("Transactions: %d | Alerts: %d", count_transactions(), count_alerts())
    log.info("Data quality: %d/%d checks passed", dq_report["summary"]["passed"],
              dq_report["summary"]["total_checks"])
    log.info("Exported %d analytical CSV files + %d star-schema files to exports/",
              len(export_summary), len(star_schema_summary))
    log.info("KPIs: %d metrics -> exports/kpi_summary.csv", len(kpis))
    log.info("=" * 70)


if __name__ == "__main__":
    main()
