-- 09_temporal_analysis.sql
-- Business question: How does each entity's current activity compare
-- with its previous activity, and what is the rolling transaction volume
-- over time? (business questions M, L)
--
-- Schema used: transactions
-- Techniques: CTE, date bucketing, window functions SUM() OVER (rolling),
--             LAG for period-over-period comparison

-- Part 1: rolling 7-day transaction volume and value (portfolio-level)
WITH daily_totals AS (
    SELECT
        DATE(timestamp)              AS tx_date,
        COUNT(*)                     AS daily_tx_count,
        ROUND(SUM(amount), 2)        AS daily_value
    FROM transactions
    GROUP BY DATE(timestamp)
)
SELECT
    tx_date,
    daily_tx_count,
    daily_value,
    SUM(daily_tx_count) OVER (
        ORDER BY tx_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    )                                                        AS rolling_7d_tx_count,
    ROUND(SUM(daily_value) OVER (
        ORDER BY tx_date ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ), 2)                                                    AS rolling_7d_value
FROM daily_totals
ORDER BY tx_date;

-- Part 2: entity month-over-month activity comparison
WITH entity_monthly AS (
    SELECT
        source_entity                                   AS entity,
        strftime('%Y-%m', timestamp)                     AS month,
        COUNT(*)                                         AS tx_count,
        ROUND(SUM(amount), 2)                             AS tx_value
    FROM transactions
    GROUP BY source_entity, strftime('%Y-%m', timestamp)
)
SELECT
    entity,
    month,
    tx_count,
    tx_value,
    LAG(tx_count) OVER (PARTITION BY entity ORDER BY month) AS prev_month_tx_count,
    tx_count - LAG(tx_count) OVER (PARTITION BY entity ORDER BY month)
                                                              AS tx_count_change,
    ROUND(
        100.0 * (tx_value - LAG(tx_value) OVER (PARTITION BY entity ORDER BY month))
        / NULLIF(LAG(tx_value) OVER (PARTITION BY entity ORDER BY month), 0), 2
    )                                                         AS value_pct_change
FROM entity_monthly
ORDER BY entity, month;
