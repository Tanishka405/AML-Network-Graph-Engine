-- 04_high_risk_entities.sql
-- Business question: Which entities have the highest AML risk, based on
-- the alerts actually generated against transactions they participated
-- in? (business question C)
--
-- Schema used: alerts JOIN transactions
-- Techniques: CTE, UNION ALL, GROUP BY, HAVING, RANK()
--
-- Note: risk is attached to a transaction (alert), not directly to an
-- entity. An entity's risk exposure here is the aggregate of every alert
-- where that entity appears as either source or destination.

WITH entity_alerts AS (
    SELECT t.source_entity AS entity, a.risk_score, a.risk_label
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
    UNION ALL
    SELECT t.dest_entity AS entity, a.risk_score, a.risk_label
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
)
SELECT
    entity,
    COUNT(*)                                             AS alert_count,
    ROUND(AVG(risk_score), 4)                             AS avg_risk_score,
    ROUND(MAX(risk_score), 4)                             AS max_risk_score,
    SUM(CASE WHEN risk_label = 'CRITICAL' THEN 1 ELSE 0 END) AS critical_alerts,
    SUM(CASE WHEN risk_label = 'HIGH'     THEN 1 ELSE 0 END) AS high_alerts,
    RANK() OVER (ORDER BY AVG(risk_score) DESC)           AS risk_rank
FROM entity_alerts
GROUP BY entity
HAVING COUNT(*) >= 2          -- exclude single-alert noise from the ranking
ORDER BY avg_risk_score DESC
LIMIT 25;
