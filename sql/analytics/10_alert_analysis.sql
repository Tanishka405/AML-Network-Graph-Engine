-- 10_alert_analysis.sql
-- Business questions: What percentage of transactions are flagged HIGH or
-- CRITICAL? What percentage of total transaction value is associated with
-- flagged activity? Which AML rules contribute most frequently to alerts?
-- (business questions D, N, P)
--
-- Schema used: alerts JOIN transactions
-- Techniques: CASE, subquery-based percentages, UNION ALL for rule
--             contribution (a "rule" here is one of the six named
--             component scores that make up the composite risk score,
--             per backend/config.py RiskWeights and backend/aml_rules.py)

-- Part 1: risk label distribution and flagged-value share
SELECT
    risk_label,
    COUNT(*)                                                AS alert_count,
    ROUND(100.0 * COUNT(*) /
          (SELECT COUNT(*) FROM transactions), 2)           AS pct_of_all_tx,
    ROUND(SUM(t.amount), 2)                                 AS flagged_value,
    ROUND(100.0 * SUM(t.amount) /
          (SELECT SUM(amount) FROM transactions), 2)        AS pct_of_total_value
FROM alerts a
JOIN transactions t ON t.tx_id = a.tx_id
GROUP BY risk_label
ORDER BY
    CASE risk_label
        WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
        WHEN 'MEDIUM' THEN 3 ELSE 4
    END;

-- Part 2: AML rule trigger frequency -- how many alerts had each of the
-- five discrete AML rules (backend/aml_rules.py) actually fire.
--
-- IMPORTANT -- this does NOT use `<component>_score > 0`. That was the
-- original version of this query, and it was wrong: structuring_score
-- and velocity_score are RAW, continuous scores stored on every alert
-- regardless of whether the rule fired (RuleResult.triggered is a
-- separate boolean gated by count/amount/score thresholds -- see
-- rule_structuring()/rule_velocity() in aml_rules.py). On the canonical
-- 5,000-tx fixture, `structuring_score > 0` matched 609 of 610 alerts
-- (near-100%, which is what surfaced this investigation) while only 466
-- alerts actually had the rule trigger. circular_score and
-- high_risk_jx_score happen to be inherently binary/discrete in the
-- current implementation, so `score > 0` coincidentally equalled
-- "triggered" for those two -- but relying on that coincidence is
-- fragile, so all five rules below use the same, always-correct test.
--
-- The one place `RuleResult.triggered` is actually persisted is the
-- `reason` string: backend/aml_rules.py::evaluate_all_rules() builds it
-- as `[r.reason for r in all_rules if r.triggered and r.reason]`, and
-- each rule's reason starts with a fixed, unique literal prefix. Using
-- `reason LIKE '%PREFIX%'` is therefore reading the application's own
-- trigger decision back out, not re-deriving rule logic in SQL.
--
-- `layering` has no persisted score column at all (see
-- docs/analytics.md "Known gap: the layering rule" -- its score is
-- computed and contributes to `reason` but is never written to
-- `alerts` and never included in the composite risk calculation), so
-- avg_score_when_triggered is NULL for that row by necessity, not by
-- query error.
SELECT rule_name, alert_contributions, avg_score_when_triggered FROM (
    SELECT 'structuring'  AS rule_name,
           SUM(CASE WHEN reason LIKE '%STRUCTURING:%' THEN 1 ELSE 0 END) AS alert_contributions,
           ROUND(AVG(CASE WHEN reason LIKE '%STRUCTURING:%' THEN structuring_score END), 4) AS avg_score_when_triggered
    FROM alerts
    UNION ALL
    SELECT 'velocity',
           SUM(CASE WHEN reason LIKE '%VELOCITY:%' THEN 1 ELSE 0 END),
           ROUND(AVG(CASE WHEN reason LIKE '%VELOCITY:%' THEN velocity_score END), 4)
    FROM alerts
    UNION ALL
    SELECT 'circular_flow',
           SUM(CASE WHEN reason LIKE '%CIRCULAR FLOW:%' THEN 1 ELSE 0 END),
           ROUND(AVG(CASE WHEN reason LIKE '%CIRCULAR FLOW:%' THEN circular_score END), 4)
    FROM alerts
    UNION ALL
    SELECT 'high_risk_jurisdiction',
           SUM(CASE WHEN reason LIKE '%HIGH-RISK JX:%' THEN 1 ELSE 0 END),
           ROUND(AVG(CASE WHEN reason LIKE '%HIGH-RISK JX:%' THEN high_risk_jx_score END), 4)
    FROM alerts
    UNION ALL
    SELECT 'layering',
           SUM(CASE WHEN reason LIKE '%LAYERING:%' THEN 1 ELSE 0 END),
           NULL  -- layering_score is not persisted to `alerts`; see note above
    FROM alerts
)
ORDER BY alert_contributions DESC;

-- Part 3: continuous risk signal magnitude -- graph_score (topology
-- centrality) and anomaly_score (Isolation Forest) are NOT rules in the
-- aml_rules.py sense: neither has a RuleResult/triggered boolean, a
-- reason-string prefix, or a fixed firing threshold anywhere in the
-- codebase -- they are continuous signals that contribute to
-- composite_risk purely by weighted magnitude (see backend/risk_engine.py
-- RiskWeights). Reporting them as "trigger counts" alongside the five
-- real rules above would be exactly the pattern_type/rule/alert
-- conflation this investigation was asked to check for, so they are
-- reported separately, as magnitude statistics, not frequency counts.
SELECT
    'graph_centrality' AS signal_name,
    ROUND(AVG(graph_score), 4)                               AS avg_magnitude,
    ROUND(MAX(graph_score), 4)                               AS max_magnitude,
    SUM(CASE WHEN graph_score > 0 THEN 1 ELSE 0 END)          AS alerts_with_nonzero_signal
FROM alerts
UNION ALL
SELECT
    'isolation_forest_anomaly',
    ROUND(AVG(anomaly_score), 4),
    ROUND(MAX(anomaly_score), 4),
    SUM(CASE WHEN anomaly_score > 0 THEN 1 ELSE 0 END)
FROM alerts;
