-- 06_structuring_analysis.sql
-- Business question: Which entities have repeated structuring alerts --
-- i.e. this isn't a one-off, it's a pattern? (business question G)
--
-- Schema used: alerts JOIN transactions
-- Techniques: CTE, GROUP BY, HAVING, CASE
--
-- IMPORTANT: this filters on `a.reason LIKE '%STRUCTURING:%'`, not
-- `a.structuring_score > 0`. structuring_score is the RAW, continuous
-- score backend/aml_rules.py's rule_structuring() computes -- it is
-- stored on every alert regardless of whether the rule actually fired.
-- Whether the rule fired is a separate boolean (RuleResult.triggered,
-- gated by count>=3 AND amount<threshold AND score>0.1) that is not
-- persisted as its own column; the only place that boolean's outcome is
-- recorded is the `reason` string, which
-- backend/aml_rules.py::evaluate_all_rules() builds ONLY from triggered
-- rules ("reasons = [r.reason for r in all_rules if r.triggered ...]").
-- Using structuring_score > 0 here was found to overcount by ~31% on the
-- canonical 5,000-tx fixture (609 "structuring alerts" by raw score vs.
-- 466 that actually triggered the rule) -- see docs/analytics.md
-- "Rule trigger frequency vs. raw signal magnitude" for the full
-- investigation.

WITH structuring_alerts AS (
    SELECT
        t.source_entity,
        a.tx_id,
        a.timestamp,
        a.structuring_score,
        t.amount
    FROM alerts a
    JOIN transactions t ON t.tx_id = a.tx_id
    WHERE a.reason LIKE '%STRUCTURING:%'
)
SELECT
    source_entity,
    COUNT(*)                                      AS structuring_alert_count,
    ROUND(AVG(structuring_score), 4)              AS avg_structuring_score,
    ROUND(SUM(amount), 2)                          AS total_flagged_value,
    ROUND(AVG(amount), 2)                          AS avg_flagged_tx_amount,
    MIN(timestamp)                                 AS first_structuring_alert,
    MAX(timestamp)                                 AS last_structuring_alert,
    CASE
        WHEN COUNT(*) >= 5 THEN 'REPEATED_PATTERN'
        WHEN COUNT(*) >= 2 THEN 'RECURRING'
        ELSE 'ISOLATED'
    END                                             AS pattern_classification
FROM structuring_alerts
GROUP BY source_entity
HAVING COUNT(*) >= 1
ORDER BY structuring_alert_count DESC, avg_structuring_score DESC
LIMIT 25;

