-- 11_customer_segmentation.sql
-- Business question: Which jurisdictions contribute the highest
-- transaction/risk exposure, and how should entities be segmented into
-- risk tiers for a compliance analyst's worklist? (business question O,
-- plus a general segmentation view)
--
-- Schema used: transactions, alerts
-- Techniques: CTE, NTILE() window function for quartile segmentation,
--             CASE, LEFT JOIN, GROUP BY

-- Part 1: entity segmentation into risk quartiles by total transacted value
WITH entity_value AS (
    SELECT
        source_entity                              AS entity,
        COUNT(*)                                   AS tx_count,
        ROUND(SUM(amount), 2)                       AS total_value
    FROM transactions
    GROUP BY source_entity
),
entity_alert_flags AS (
    SELECT t.source_entity AS entity, COUNT(*) AS alert_count
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
    GROUP BY t.source_entity
)
SELECT
    ev.entity,
    ev.tx_count,
    ev.total_value,
    COALESCE(eaf.alert_count, 0)                              AS alert_count,
    NTILE(4) OVER (ORDER BY ev.total_value)                   AS value_quartile,
    CASE
        WHEN COALESCE(eaf.alert_count, 0) = 0 THEN 'CLEAN'
        WHEN COALESCE(eaf.alert_count, 0) BETWEEN 1 AND 2 THEN 'WATCHLIST'
        ELSE 'ENHANCED_DUE_DILIGENCE'
    END                                                         AS segment
FROM entity_value ev
LEFT JOIN entity_alert_flags eaf ON eaf.entity = ev.entity
ORDER BY ev.total_value DESC;

-- Part 2: jurisdiction-level exposure (source-side)
SELECT
    t.src_jurisdiction                                        AS jurisdiction,
    COUNT(*)                                                  AS tx_count,
    ROUND(SUM(t.amount), 2)                                    AS total_value,
    COUNT(a.alert_id)                                          AS alert_count,
    ROUND(100.0 * COUNT(a.alert_id) / COUNT(*), 2)             AS alert_rate_pct
FROM transactions t
LEFT JOIN alerts a ON a.tx_id = t.tx_id
GROUP BY t.src_jurisdiction
ORDER BY alert_rate_pct DESC, total_value DESC;
