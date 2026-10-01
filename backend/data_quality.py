"""
backend/data_quality.py — Deterministic data-quality framework for the
`transactions` table.

Design principles (matching the rest of this repository)
----------------------------------------------------------
* Every check is a pure SQL query against the existing schema — no new
  tables are required to *find* problems, only to *report* them.
* Checks NEVER delete or silently modify data. This module is read-only
  with respect to `transactions`; it only writes to `data_quality_results`
  (see backend/database.py) so a bad record is always explainable and
  reproducible, not disappeared.
* Every check returns a structured `DQCheckResult` with a fixed set of
  fields so results can be exported to CSV/JSON and consumed by Power BI
  without post-processing.
* Severity is assigned by the check itself, based on what the violation
  would mean for the AML pipeline downstream (e.g. a null amount breaks
  every risk calculation for that row -> HIGH; a duplicate transaction
  id is likely a harmless re-run of the generator -> MEDIUM).

Run standalone:
    python -m backend.data_quality
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from backend.database import db_conn, get_db_path
from backend.config import GEN_CFG

log = logging.getLogger(__name__)

VALID_CURRENCIES = set(GEN_CFG.currencies)
VALID_JURISDICTIONS = set(GEN_CFG.all_jurisdictions)
VALID_PATTERN_TYPES = {
    "normal", "structuring", "layering", "circular_flow",
    "velocity_burst", "high_risk_jx",
}
# Confirmed against a live query of generator.py output (`SELECT DISTINCT
# pattern_type FROM transactions`), not assumed from variable names --
# an earlier draft of this set assumed "smurfing"/"circular"/"velocity"
# and would have wrongly flagged ~36% of legitimate rows as invalid.


@dataclass
class DQCheckResult:
    check_name: str
    status: str            # PASS / WARN / FAIL
    rows_checked: int
    violations: int
    violation_rate: float  # violations / rows_checked, 0.0 if rows_checked == 0
    severity: str           # LOW / MEDIUM / HIGH
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _status_for(violations: int, rate: float, warn_rate: float, fail_rate: float) -> str:
    """A check FAILs above fail_rate, WARNs above warn_rate, else PASSes."""
    if violations == 0:
        return "PASS"
    if rate >= fail_rate:
        return "FAIL"
    if rate >= warn_rate:
        return "WARN"
    return "PASS" if rate == 0 else "WARN"


def _scalar(conn, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def _run_check(
    conn,
    check_name: str,
    description: str,
    violation_sql: str,
    total_sql: str = "SELECT COUNT(*) FROM transactions",
    severity_if_violations: str = "MEDIUM",
    warn_rate: float = 0.001,
    fail_rate: float = 0.02,
) -> DQCheckResult:
    total = _scalar(conn, total_sql)
    violations = _scalar(conn, violation_sql)
    rate = round(violations / total, 6) if total else 0.0
    status = _status_for(violations, rate, warn_rate, fail_rate)
    severity = severity_if_violations if violations else "LOW"
    return DQCheckResult(
        check_name=check_name,
        status=status,
        rows_checked=total,
        violations=violations,
        violation_rate=rate,
        severity=severity,
        description=description,
    )


# ---------------------------------------------------------------------------
# Individual checks (14, matching the spec)
# ---------------------------------------------------------------------------
def check_null_required_fields(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "null_required_fields",
        "Rows with a NULL value in any column that every downstream "
        "component (SQL aggregation, graph engine, feature builder) "
        "requires to be present.",
        """SELECT COUNT(*) FROM transactions WHERE
              tx_id IS NULL OR timestamp IS NULL OR source_entity IS NULL
           OR dest_entity IS NULL OR amount IS NULL OR currency IS NULL
           OR src_jurisdiction IS NULL OR dst_jurisdiction IS NULL""",
        severity_if_violations="HIGH",
    )


def check_duplicate_tx_ids(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "duplicate_transaction_ids",
        "tx_id is the primary key; this counts rows beyond the first for "
        "any tx_id that appears more than once. Because tx_id is PRIMARY "
        "KEY, true duplicates cannot exist post-insert -- a non-zero count "
        "here would indicate a schema/constraint failure, so this check "
        "exists as a defensive tripwire, not because duplicates are "
        "expected.",
        """SELECT COALESCE(SUM(c - 1), 0) FROM (
               SELECT COUNT(*) AS c FROM transactions
               GROUP BY tx_id HAVING COUNT(*) > 1
           )""",
        severity_if_violations="HIGH",
    )


def check_duplicate_transaction_records(conn) -> DQCheckResult:
    """Full-row duplicates on the business key (not tx_id): same source,
    dest, amount, currency and timestamp -- i.e. the same economic event
    inserted twice under a different tx_id."""
    return _run_check(
        conn,
        "duplicate_transaction_records",
        "Distinct tx_ids that nonetheless share identical "
        "(source_entity, dest_entity, amount, currency, timestamp) -- "
        "the same economic event recorded twice, which would double-count "
        "volume and value KPIs if not flagged.",
        """SELECT COALESCE(SUM(c - 1), 0) FROM (
               SELECT COUNT(*) AS c
               FROM transactions
               GROUP BY source_entity, dest_entity, amount, currency, timestamp
               HAVING COUNT(*) > 1
           )""",
        severity_if_violations="MEDIUM",
    )


def check_invalid_amounts(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "invalid_amounts",
        "Transactions with amount <= 0. A financial transaction with zero "
        "or negative value is not a valid transfer in this model (refunds/"
        "reversals are out of scope for this dataset) and would distort "
        "every downstream sum/avg/VaR-style aggregate.",
        "SELECT COUNT(*) FROM transactions WHERE amount <= 0",
        severity_if_violations="HIGH",
    )


def check_extreme_amounts(conn) -> DQCheckResult:
    """Statistical outliers, not hard-invalid -- flagged, not rejected."""
    stats = conn.execute("SELECT AVG(amount) AS mu FROM transactions").fetchone()
    mu = stats["mu"] if stats and stats["mu"] is not None else 0.0
    var_row = conn.execute(
        "SELECT AVG((amount - ?) * (amount - ?)) AS var FROM transactions", (mu, mu)
    ).fetchone()
    variance = var_row["var"] if var_row and var_row["var"] is not None else 0.0
    stddev = variance ** 0.5
    cutoff = mu + 6 * stddev
    return _run_check(
        conn,
        "extreme_amount_outliers",
        f"Transactions whose amount exceeds mean + 6 standard deviations "
        f"(cutoff=${cutoff:,.2f}, mean=${mu:,.2f}, std=${stddev:,.2f}). "
        f"These are not necessarily invalid (large legitimate wires exist) "
        f"but are flagged for analyst review since they can dominate "
        f"SUM-based KPIs.",
        f"SELECT COUNT(*) FROM transactions WHERE amount > {cutoff}",
        severity_if_violations="LOW",
        warn_rate=0.0001,
        fail_rate=1.0,  # informational -- never hard-fails the pipeline
    )


def check_invalid_currency(conn) -> DQCheckResult:
    # Values come from internal config, not user input -- safe to inline.
    quoted = ",".join(f"'{c}'" for c in sorted(VALID_CURRENCIES))
    return _run_check(
        conn,
        "invalid_currency",
        f"Currency codes outside the {len(VALID_CURRENCIES)} configured in "
        f"GEN_CFG.currencies: {sorted(VALID_CURRENCIES)}.",
        f"SELECT COUNT(*) FROM transactions WHERE currency NOT IN ({quoted})",
        severity_if_violations="MEDIUM",
    )


def check_invalid_jurisdiction(conn) -> DQCheckResult:
    valid = sorted(VALID_JURISDICTIONS)
    quoted = ",".join(f"'{j}'" for j in valid)
    sql = (
        f"SELECT COUNT(*) FROM transactions WHERE "
        f"src_jurisdiction NOT IN ({quoted}) OR "
        f"dst_jurisdiction NOT IN ({quoted})"
    )
    total = _scalar(conn, "SELECT COUNT(*) FROM transactions")
    violations = _scalar(conn, sql)
    rate = round(violations / total, 6) if total else 0.0
    return DQCheckResult(
        check_name="invalid_jurisdiction",
        status=_status_for(violations, rate, 0.001, 0.02),
        rows_checked=total,
        violations=violations,
        violation_rate=rate,
        severity="MEDIUM" if violations else "LOW",
        description=(
            f"src_jurisdiction or dst_jurisdiction outside the "
            f"{len(valid)} configured jurisdictions."
        ),
    )


def check_self_transactions(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "self_transactions",
        "Transactions where source_entity equals dest_entity. An entity "
        "moving funds to itself is not economically meaningful and is "
        "either a generator defect or a data entry error.",
        "SELECT COUNT(*) FROM transactions WHERE source_entity = dest_entity",
        severity_if_violations="MEDIUM",
    )


def check_invalid_timestamps(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "invalid_timestamps",
        "Timestamps that do not parse as valid SQLite datetime values "
        "(strftime returns NULL for anything unparseable).",
        """SELECT COUNT(*) FROM transactions
           WHERE strftime('%Y-%m-%d %H:%M:%S', timestamp) IS NULL""",
        severity_if_violations="HIGH",
    )


def check_future_timestamps(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "future_timestamps",
        "Transactions timestamped after the current UTC time -- a "
        "transaction cannot be recorded before it happens.",
        "SELECT COUNT(*) FROM transactions WHERE timestamp > datetime('now')",
        severity_if_violations="HIGH",
    )


def check_invalid_pattern_type(conn) -> DQCheckResult:
    valid = sorted(VALID_PATTERN_TYPES)
    quoted = ",".join(f"'{p}'" for p in valid)
    total = _scalar(conn, "SELECT COUNT(*) FROM transactions")
    violations = _scalar(
        conn,
        f"SELECT COUNT(*) FROM transactions WHERE pattern_type NOT IN ({quoted})",
    )
    rate = round(violations / total, 6) if total else 0.0
    return DQCheckResult(
        check_name="invalid_transaction_type",
        status=_status_for(violations, rate, 0.001, 0.02),
        rows_checked=total,
        violations=violations,
        violation_rate=rate,
        severity="MEDIUM" if violations else "LOW",
        description=(
            f"pattern_type outside the {len(valid)} labels the generator "
            f"and evaluation pipeline recognise: {valid}."
        ),
    )


def check_negative_inter_arrival(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "negative_inter_arrival",
        "inter_arrival_s (seconds since the entity's previous transaction) "
        "is negative, which is only possible if rows were inserted "
        "out of generation order.",
        "SELECT COUNT(*) FROM transactions WHERE inter_arrival_s < 0",
        severity_if_violations="LOW",
    )


def check_missing_entities(conn) -> DQCheckResult:
    """Referential-style check: entities referenced in alerts should exist
    in transactions (alerts.tx_id -> transactions.tx_id is an actual FK;
    this checks it holds even though SQLite FK enforcement is only checked
    at insert time with PRAGMA foreign_keys=ON)."""
    return _run_check(
        conn,
        "orphaned_alerts",
        "Alerts whose tx_id has no matching row in transactions -- a "
        "referential-integrity violation that would make an alert "
        "impossible to investigate (no source transaction to inspect).",
        """SELECT COUNT(*) FROM alerts a
           LEFT JOIN transactions t ON t.tx_id = a.tx_id
           WHERE t.tx_id IS NULL""",
        total_sql="SELECT COUNT(*) FROM alerts",
        severity_if_violations="HIGH",
    )


def check_blank_entity_ids(conn) -> DQCheckResult:
    return _run_check(
        conn,
        "blank_entity_ids",
        "source_entity or dest_entity is an empty string or whitespace-only "
        "-- distinct from NULL (covered by null_required_fields) but "
        "equally unusable for graph construction.",
        """SELECT COUNT(*) FROM transactions
           WHERE TRIM(source_entity) = '' OR TRIM(dest_entity) = ''""",
        severity_if_violations="HIGH",
    )


CHECK_FUNCS = [
    check_null_required_fields,
    check_duplicate_tx_ids,
    check_duplicate_transaction_records,
    check_invalid_amounts,
    check_extreme_amounts,
    check_invalid_currency,
    check_invalid_jurisdiction,
    check_self_transactions,
    check_invalid_timestamps,
    check_future_timestamps,
    check_invalid_pattern_type,
    check_negative_inter_arrival,
    check_missing_entities,
    check_blank_entity_ids,
]


def run_all_checks(path: str | None = None) -> List[DQCheckResult]:
    """Run every registered check against the transactions/alerts tables.
    Read-only: never modifies transactions or alerts."""
    with db_conn(path) as conn:
        return [fn(conn) for fn in CHECK_FUNCS]


def summarize(results: List[DQCheckResult]) -> Dict[str, Any]:
    return {
        "total_checks": len(results),
        "passed": sum(1 for r in results if r.status == "PASS"),
        "warned": sum(1 for r in results if r.status == "WARN"),
        "failed": sum(1 for r in results if r.status == "FAIL"),
        "high_severity_violations": sum(
            1 for r in results if r.severity == "HIGH" and r.violations > 0
        ),
    }


def run_and_persist(
    path: str | None = None,
    json_out: str | None = None,
) -> Dict[str, Any]:
    """Run all checks, save every result row to data_quality_results, and
    optionally write a JSON report. Returns the summary dict."""
    from backend.database import save_data_quality_results  # local import: avoid cycle

    results = run_all_checks(path)
    run_ts = datetime.now(timezone.utc).isoformat()
    save_data_quality_results([r.to_dict() for r in results], run_timestamp=run_ts, path=path)

    summary = summarize(results)
    report = {
        "run_timestamp": run_ts,
        "summary": summary,
        "checks": [r.to_dict() for r in results],
    }
    if json_out:
        with open(json_out, "w") as f:
            json.dump(report, f, indent=2)
        log.info("Data quality report written to %s", json_out)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    report = run_and_persist(json_out="data_quality_report.json")
    print(json.dumps(report["summary"], indent=2))
    for c in report["checks"]:
        flag = {"PASS": " ", "WARN": "!", "FAIL": "X"}[c["status"]]
        print(f"[{flag}] {c['check_name']:32s} {c['status']:5s} "
              f"violations={c['violations']:>7d} rate={c['violation_rate']:.4%} "
              f"severity={c['severity']}")
