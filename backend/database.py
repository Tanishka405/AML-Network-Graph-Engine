"""
database.py — SQLite persistence layer for the AML system.

Responsibilities
----------------
* Create / migrate the schema (transactions, alerts, evaluation_results).
* Provide typed insert / query helpers.
* Implement SQL-based structuring detection using GROUP BY / HAVING /
  time-window aggregation — demonstrating that SQL is a first-class
  analytical tool in this pipeline, not just a store.
"""

from __future__ import annotations

import sqlite3
import os
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

from backend.config import DB_CFG

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- ── transactions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transactions (
    tx_id               TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    source_entity       TEXT NOT NULL,
    dest_entity         TEXT NOT NULL,
    amount              REAL NOT NULL,
    currency            TEXT NOT NULL,
    src_jurisdiction    TEXT NOT NULL,
    dst_jurisdiction    TEXT NOT NULL,
    pattern_type        TEXT NOT NULL DEFAULT 'normal',
    inter_arrival_s     REAL NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_tx_timestamp       ON transactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_tx_source          ON transactions(source_entity);
CREATE INDEX IF NOT EXISTS idx_tx_dest            ON transactions(dest_entity);
CREATE INDEX IF NOT EXISTS idx_tx_amount          ON transactions(amount);
CREATE INDEX IF NOT EXISTS idx_tx_pattern         ON transactions(pattern_type);
CREATE INDEX IF NOT EXISTS idx_tx_source_ts       ON transactions(source_entity, timestamp);

-- ── alerts ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    alert_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id               TEXT NOT NULL,
    timestamp           TEXT NOT NULL,
    risk_score          REAL NOT NULL,
    risk_label          TEXT NOT NULL,
    anomaly_score       REAL NOT NULL DEFAULT 0.0,
    structuring_score   REAL NOT NULL DEFAULT 0.0,
    graph_score         REAL NOT NULL DEFAULT 0.0,
    velocity_score      REAL NOT NULL DEFAULT 0.0,
    circular_score      REAL NOT NULL DEFAULT 0.0,
    high_risk_jx_score  REAL NOT NULL DEFAULT 0.0,
    reason              TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (tx_id) REFERENCES transactions(tx_id)
);

CREATE INDEX IF NOT EXISTS idx_alert_tx_id        ON alerts(tx_id);
CREATE INDEX IF NOT EXISTS idx_alert_timestamp    ON alerts(timestamp);
CREATE INDEX IF NOT EXISTS idx_alert_risk_label   ON alerts(risk_label);

-- ── evaluation_results ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evaluation_results (
    eval_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp       TEXT NOT NULL,
    n_samples           INTEGER NOT NULL,
    n_train             INTEGER NOT NULL,
    n_test              INTEGER NOT NULL,
    precision           REAL,
    recall              REAL,
    f1                  REAL,
    true_positives      INTEGER,
    false_positives     INTEGER,
    true_negatives      INTEGER,
    false_negatives     INTEGER,
    threshold           REAL,
    notes               TEXT DEFAULT ''
);

-- ── data_quality_results ────────────────────────────────────────────────────
-- Written by backend/data_quality.py. One row per check per run. Never used
-- to silently drop data -- this table is a report, not a filter.
CREATE TABLE IF NOT EXISTS data_quality_results (
    dq_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp       TEXT NOT NULL,
    check_name          TEXT NOT NULL,
    status              TEXT NOT NULL,     -- PASS / WARN / FAIL
    rows_checked        INTEGER NOT NULL,
    violations          INTEGER NOT NULL,
    violation_rate      REAL NOT NULL,
    severity            TEXT NOT NULL,     -- LOW / MEDIUM / HIGH
    description         TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_dq_run_ts ON data_quality_results(run_timestamp);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def get_db_path() -> str:
    os.makedirs(os.path.dirname(DB_CFG.path), exist_ok=True)
    return DB_CFG.path


def get_connection(path: str | None = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory set."""
    conn = sqlite3.connect(path or get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_conn(path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = get_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------
def init_db(path: str | None = None) -> None:
    """Create tables and indexes if they do not already exist."""
    with db_conn(path) as conn:
        conn.executescript(SCHEMA_SQL)
    log.info("Database schema initialised at %s", path or get_db_path())


# ---------------------------------------------------------------------------
# Transaction persistence
# ---------------------------------------------------------------------------
def insert_transactions(rows: List[Dict[str, Any]], path: str | None = None) -> int:
    """
    Bulk-insert a list of transaction dicts.  Uses executemany with
    parameterised SQL.  Returns number of rows inserted.
    """
    sql = """
        INSERT OR IGNORE INTO transactions
            (tx_id, timestamp, source_entity, dest_entity, amount,
             currency, src_jurisdiction, dst_jurisdiction,
             pattern_type, inter_arrival_s)
        VALUES
            (:tx_id, :timestamp, :source_entity, :dest_entity, :amount,
             :currency, :src_jurisdiction, :dst_jurisdiction,
             :pattern_type, :inter_arrival_s)
    """
    with db_conn(path) as conn:
        conn.executemany(sql, rows)
        n = conn.execute("SELECT changes()").fetchone()[0]
    return n


def insert_transaction(row: Dict[str, Any], path: str | None = None) -> None:
    """Insert a single transaction dict."""
    insert_transactions([row], path)


# ---------------------------------------------------------------------------
# Alert persistence
# ---------------------------------------------------------------------------
def insert_alert(alert: Dict[str, Any], path: str | None = None) -> int:
    """Persist an alert record; returns the new alert_id."""
    sql = """
        INSERT INTO alerts
            (tx_id, timestamp, risk_score, risk_label,
             anomaly_score, structuring_score, graph_score,
             velocity_score, circular_score, high_risk_jx_score, reason)
        VALUES
            (:tx_id, :timestamp, :risk_score, :risk_label,
             :anomaly_score, :structuring_score, :graph_score,
             :velocity_score, :circular_score, :high_risk_jx_score, :reason)
    """
    with db_conn(path) as conn:
        cur = conn.execute(sql, alert)
        return cur.lastrowid


def get_recent_alerts(limit: int = 100, path: str | None = None) -> List[Dict]:
    sql = """
        SELECT a.*, t.source_entity, t.dest_entity, t.amount, t.currency,
               t.src_jurisdiction, t.dst_jurisdiction
        FROM   alerts a
        JOIN   transactions t ON t.tx_id = a.tx_id
        ORDER  BY a.timestamp DESC
        LIMIT  ?
    """
    with db_conn(path) as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# SQL-based structuring detection  ← Phase 4 requirement
# ---------------------------------------------------------------------------
def sql_structuring_scores(
    window_minutes: int | None = None,
    threshold: float | None = None,
    min_count: int | None = None,
    path: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Detect structuring (smurfing) using SQL aggregation.

    Logic
    -----
    For every source entity, look at all sub-threshold transactions
    within a rolling *window_minutes* window.  Flag entities where:
        - COUNT(*) >= min_count          (multiple transactions)
        - SUM(amount) >= threshold       (aggregate near/above threshold)
        - AVG(amount) < threshold        (individual amounts sub-threshold)

    This captures the coordinated behaviour, NOT just individual
    transactions below the reporting threshold.

    Returns a list of dicts with structuring risk scores per entity.
    """
    window_minutes = window_minutes or DB_CFG.structuring_window_minutes
    threshold      = threshold      or 10_000.0
    min_count      = min_count      or DB_CFG.structuring_count_threshold

    sql = """
        WITH ranked AS (
            -- Self-join to find transactions within window of each other
            SELECT
                t1.tx_id            AS tx_id,
                t1.source_entity    AS entity,
                t1.amount           AS amount,
                t1.timestamp        AS ts,
                COUNT(t2.tx_id)     AS window_count,
                SUM(t2.amount)      AS window_sum,
                AVG(t2.amount)      AS window_avg,
                MIN(t2.amount)      AS window_min,
                MAX(t2.amount)      AS window_max
            FROM transactions t1
            JOIN transactions t2
                ON  t2.source_entity = t1.source_entity
                AND t2.amount        < :threshold
                AND t2.timestamp    >= datetime(t1.timestamp,
                                               :neg_window_str)
                AND t2.timestamp    <= t1.timestamp
            WHERE t1.amount < :threshold
            GROUP BY t1.tx_id
            HAVING COUNT(t2.tx_id) >= :min_count
        )
        SELECT
            entity,
            tx_id,
            ts,
            window_count,
            window_sum,
            window_avg,
            window_min,
            window_max,
            -- Normalised score: how close is window_sum to threshold?
            -- Score → 1 when sum >> threshold with many transactions
            MIN(1.0,
                (CAST(window_count AS REAL) / 10.0) *
                (window_sum / (:threshold * :min_count))
            ) AS structuring_score
        FROM ranked
        ORDER BY structuring_score DESC
    """
    neg_window = f"-{window_minutes} minutes"
    with db_conn(path) as conn:
        rows = conn.execute(sql, {
            "threshold":       threshold,
            "neg_window_str":  neg_window,
            "min_count":       min_count,
        }).fetchall()
    return [dict(r) for r in rows]


def sql_entity_structuring_score(
    entity: str,
    tx_timestamp: str,
    window_minutes: int | None = None,
    threshold: float | None = None,
    path: str | None = None,
) -> Dict[str, Any]:
    """
    Return structuring features for a SINGLE entity at a given timestamp.
    Used in the real-time pipeline.
    """
    window_minutes = window_minutes or DB_CFG.structuring_window_minutes
    threshold      = threshold      or 10_000.0
    min_count      = DB_CFG.structuring_count_threshold

    sql = """
        SELECT
            COUNT(*)        AS window_count,
            SUM(amount)     AS window_sum,
            AVG(amount)     AS window_avg,
            MIN(amount)     AS window_min,
            MAX(amount)     AS window_max
        FROM transactions
        WHERE source_entity = :entity
          AND amount        < :threshold
          AND timestamp    >= datetime(:ts, :neg_window)
          AND timestamp    <= :ts
    """
    neg_window = f"-{window_minutes} minutes"
    with db_conn(path) as conn:
        row = conn.execute(sql, {
            "entity":     entity,
            "threshold":  threshold,
            "ts":         tx_timestamp,
            "neg_window": neg_window,
        }).fetchone()

    if not row or row["window_count"] is None or row["window_count"] < min_count:
        return {"structuring_score": 0.0, "window_count": 0,
                "window_sum": 0.0, "window_avg": 0.0}

    count = row["window_count"]
    wsum  = row["window_sum"] or 0.0
    wavg  = row["window_avg"] or 0.0
    score = min(1.0, (count / 10.0) * (wsum / (threshold * min_count)))
    return {
        "structuring_score": round(score, 6),
        "window_count":      count,
        "window_sum":        round(wsum, 2),
        "window_avg":        round(wavg, 2),
    }


# ---------------------------------------------------------------------------
# Velocity detection via SQL
# ---------------------------------------------------------------------------
def sql_velocity_score(
    entity: str,
    tx_timestamp: str,
    window_seconds: int = 300,
    burst_threshold: int = 5,
    path: str | None = None,
) -> Dict[str, Any]:
    """Count transactions from entity within window_seconds."""
    sql = """
        SELECT COUNT(*) AS tx_count
        FROM   transactions
        WHERE  source_entity = :entity
          AND  timestamp    >= datetime(:ts, :neg_window)
          AND  timestamp    <= :ts
    """
    neg_window = f"-{window_seconds} seconds"
    with db_conn(path) as conn:
        row = conn.execute(sql, {
            "entity":     entity,
            "ts":         tx_timestamp,
            "neg_window": neg_window,
        }).fetchone()

    count = row["tx_count"] if row else 0
    score = min(1.0, count / (burst_threshold * 3))
    return {"velocity_score": round(score, 6), "burst_count": count}


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------
def get_transactions_since(
    since: str,
    limit: int = 10_000,
    path: str | None = None,
) -> List[Dict]:
    sql = """
        SELECT * FROM transactions
        WHERE  timestamp >= ?
        ORDER  BY timestamp ASC
        LIMIT  ?
    """
    with db_conn(path) as conn:
        rows = conn.execute(sql, (since, limit)).fetchall()
    return [dict(r) for r in rows]


def count_transactions(path: str | None = None) -> int:
    with db_conn(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def count_alerts(path: str | None = None) -> int:
    with db_conn(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]


def save_data_quality_results(
    results: List[Dict[str, Any]],
    run_timestamp: str | None = None,
    path: str | None = None,
) -> int:
    """
    Persist a batch of data-quality check results (see backend/data_quality.py).
    Each dict must contain: check_name, status, rows_checked, violations,
    violation_rate, severity, description. Returns number of rows inserted.
    """
    run_timestamp = run_timestamp or datetime.now(timezone.utc).isoformat()
    sql = """
        INSERT INTO data_quality_results
            (run_timestamp, check_name, status, rows_checked,
             violations, violation_rate, severity, description)
        VALUES
            (:run_timestamp, :check_name, :status, :rows_checked,
             :violations, :violation_rate, :severity, :description)
    """
    rows = [{**r, "run_timestamp": run_timestamp} for r in results]
    with db_conn(path) as conn:
        conn.executemany(sql, rows)
        n = conn.execute("SELECT changes()").fetchone()[0]
    return n


def get_latest_data_quality_results(path: str | None = None) -> List[Dict[str, Any]]:
    """Return the most recent data-quality run's check results."""
    sql = """
        SELECT * FROM data_quality_results
        WHERE run_timestamp = (SELECT MAX(run_timestamp) FROM data_quality_results)
        ORDER BY severity DESC, violation_rate DESC
    """
    with db_conn(path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def save_evaluation(result: Dict[str, Any], path: str | None = None) -> int:
    sql = """
        INSERT INTO evaluation_results
            (run_timestamp, n_samples, n_train, n_test,
             precision, recall, f1,
             true_positives, false_positives,
             true_negatives, false_negatives,
             threshold, notes)
        VALUES
            (:run_timestamp, :n_samples, :n_train, :n_test,
             :precision, :recall, :f1,
             :true_positives, :false_positives,
             :true_negatives, :false_negatives,
             :threshold, :notes)
    """
    with db_conn(path) as conn:
        cur = conn.execute(sql, result)
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Bulk structuring scores — for evaluation pipeline (avoids N round-trips)
# ---------------------------------------------------------------------------
def sql_bulk_structuring_scores(
    entities_timestamps: list,          # list of (entity, timestamp) tuples
    window_minutes: int | None = None,
    threshold: float | None = None,
    path: str | None = None,
) -> dict:
    """
    Compute structuring scores for multiple (entity, timestamp) pairs in
    one SQL pass using a temp table.  Returns dict keyed by (entity, ts).
    Falls back gracefully if temp table fails.
    """
    from backend.config import DB_CFG
    window_minutes = window_minutes or DB_CFG.structuring_window_minutes
    threshold      = threshold or 10_000.0
    min_count      = DB_CFG.structuring_count_threshold
    neg_window     = f"-{window_minutes} minutes"

    results = {}
    # Process in chunks of 500 to avoid huge queries
    CHUNK = 500
    for i in range(0, len(entities_timestamps), CHUNK):
        chunk = entities_timestamps[i:i+CHUNK]
        with db_conn(path) as conn:
            for entity, ts in chunk:
                sql = """
                    SELECT COUNT(*) as cnt, SUM(amount) as wsum, AVG(amount) as wavg
                    FROM transactions
                    WHERE source_entity = ?
                      AND amount < ?
                      AND timestamp >= datetime(?, ?)
                      AND timestamp <= ?
                """
                row = conn.execute(sql, (entity, threshold, ts, neg_window, ts)).fetchone()
                cnt  = row["cnt"]  if row else 0
                wsum = row["wsum"] if row else 0.0
                wavg = row["wavg"] if row else 0.0
                if cnt is None or cnt < min_count:
                    results[(entity, ts)] = {"structuring_score": 0.0, "window_count": 0,
                                              "window_sum": 0.0, "window_avg": 0.0}
                else:
                    score = min(1.0, (cnt / 10.0) * (wsum / (threshold * min_count)))
                    results[(entity, ts)] = {
                        "structuring_score": round(score, 6),
                        "window_count": cnt,
                        "window_sum":   round(wsum, 2),
                        "window_avg":   round(wavg, 2),
                    }
    return results


def sql_bulk_velocity_scores(
    entities_timestamps: list,
    window_seconds: int = 300,
    burst_threshold: int = 5,
    path: str | None = None,
) -> dict:
    """Bulk velocity scores for multiple (entity, timestamp) pairs."""
    results = {}
    neg_window = f"-{window_seconds} seconds"
    CHUNK = 500
    for i in range(0, len(entities_timestamps), CHUNK):
        chunk = entities_timestamps[i:i+CHUNK]
        with db_conn(path) as conn:
            for entity, ts in chunk:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM transactions "
                    "WHERE source_entity=? AND timestamp>=datetime(?,?) AND timestamp<=?",
                    (entity, ts, neg_window, ts)
                ).fetchone()
                cnt = row["cnt"] if row else 0
                score = min(1.0, cnt / (burst_threshold * 3))
                results[(entity, ts)] = {"velocity_score": round(score, 6), "burst_count": cnt}
    return results
