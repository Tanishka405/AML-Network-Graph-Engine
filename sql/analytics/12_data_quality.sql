-- 12_data_quality.sql
-- Business question: Can this data be trusted for the analytical outputs
-- above? Surfaces the most recent data-quality run for BI consumption.
--
-- Schema used: data_quality_results (populated by backend/data_quality.py
-- -- this file does NOT reimplement the checks in SQL; it reports the
-- results of the Python checks, since the checks themselves already run
-- as SQL against `transactions`/`alerts` inside data_quality.py. See
-- docs/data_quality.md for why the checks live in Python, not here.)

SELECT
    check_name,
    status,
    rows_checked,
    violations,
    ROUND(violation_rate * 100, 4)                AS violation_pct,
    severity,
    description
FROM data_quality_results
WHERE run_timestamp = (SELECT MAX(run_timestamp) FROM data_quality_results)
ORDER BY
    CASE status WHEN 'FAIL' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
    CASE severity WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
    violation_rate DESC;

-- Run-over-run summary (trend of data quality over time, if this has
-- been run more than once)
SELECT
    run_timestamp,
    COUNT(*)                                                AS checks_run,
    SUM(CASE WHEN status = 'PASS' THEN 1 ELSE 0 END)         AS passed,
    SUM(CASE WHEN status = 'WARN' THEN 1 ELSE 0 END)         AS warned,
    SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END)         AS failed,
    SUM(violations)                                          AS total_violations
FROM data_quality_results
GROUP BY run_timestamp
ORDER BY run_timestamp DESC;
