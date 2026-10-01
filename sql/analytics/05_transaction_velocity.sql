-- 05_transaction_velocity.sql
-- Business question: Which entities show unusual transaction velocity --
-- many transactions in a short time relative to their own normal pace?
-- (business question F)
--
-- Schema used: transactions
-- Techniques: window functions LAG/LEAD, PARTITION BY, CTE

WITH ordered AS (
    SELECT
        tx_id,
        source_entity,
        timestamp,
        amount,
        inter_arrival_s,
        -- seconds since this entity's previous transaction, recomputed
        -- directly from timestamps (cross-check against the stored
        -- inter_arrival_s column, which is computed at generation time)
        (JULIANDAY(timestamp) - JULIANDAY(
            LAG(timestamp) OVER (PARTITION BY source_entity ORDER BY timestamp)
        )) * 86400.0                                       AS recomputed_gap_s,
        LEAD(timestamp) OVER (PARTITION BY source_entity ORDER BY timestamp)
                                                             AS next_tx_timestamp,
        ROW_NUMBER() OVER (PARTITION BY source_entity ORDER BY timestamp)
                                                             AS tx_sequence_num
    FROM transactions
),
entity_gap_stats AS (
    SELECT
        source_entity,
        COUNT(*)                                            AS tx_count,
        ROUND(AVG(recomputed_gap_s), 1)                     AS avg_gap_s,
        ROUND(MIN(recomputed_gap_s), 1)                     AS min_gap_s,
        SUM(CASE WHEN recomputed_gap_s < 300 THEN 1 ELSE 0 END)
                                                             AS sub_5min_gaps
    FROM ordered
    WHERE recomputed_gap_s IS NOT NULL
    GROUP BY source_entity
)
SELECT
    source_entity,
    tx_count,
    avg_gap_s,
    min_gap_s,
    sub_5min_gaps,
    ROUND(100.0 * sub_5min_gaps / tx_count, 2)              AS pct_bursty_gaps
FROM entity_gap_stats
WHERE tx_count >= 5
ORDER BY pct_bursty_gaps DESC, tx_count DESC
LIMIT 25;
