-- 03_entity_activity.sql
-- Business questions: Which entities have the highest transaction volume?
-- Which have the highest total transaction value? (business questions A, B)
--
-- Schema used: transactions
-- Techniques: CTE, UNION ALL (an entity can be source and/or dest),
--             window functions RANK() and DENSE_RANK()

WITH entity_flows AS (
    SELECT source_entity AS entity, amount, 'outgoing' AS direction
    FROM transactions
    UNION ALL
    SELECT dest_entity AS entity, amount, 'incoming' AS direction
    FROM transactions
),
entity_totals AS (
    SELECT
        entity,
        COUNT(*)                                       AS tx_count,
        ROUND(SUM(amount), 2)                          AS total_value,
        ROUND(AVG(amount), 2)                          AS avg_value,
        SUM(CASE WHEN direction = 'outgoing' THEN 1 ELSE 0 END) AS outgoing_count,
        SUM(CASE WHEN direction = 'incoming' THEN 1 ELSE 0 END) AS incoming_count
    FROM entity_flows
    GROUP BY entity
)
SELECT
    entity,
    tx_count,
    total_value,
    avg_value,
    outgoing_count,
    incoming_count,
    RANK()       OVER (ORDER BY tx_count DESC)   AS volume_rank,
    DENSE_RANK() OVER (ORDER BY total_value DESC) AS value_rank
FROM entity_totals
ORDER BY tx_count DESC
LIMIT 25;
