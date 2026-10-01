-- 01_transaction_summary.sql
-- Business question: What does the overall transaction population look
-- like -- volume, value, and currency/jurisdiction mix -- as a starting
-- point before drilling into risk?
--
-- Schema used: transactions
-- Techniques: GROUP BY, CASE, aggregate functions
--
-- Overall summary
SELECT
    COUNT(*)                                   AS total_transactions,
    ROUND(SUM(amount), 2)                      AS total_value,
    ROUND(AVG(amount), 2)                      AS avg_amount,
    ROUND(MIN(amount), 2)                      AS min_amount,
    ROUND(MAX(amount), 2)                      AS max_amount,
    COUNT(DISTINCT source_entity)              AS distinct_source_entities,
    COUNT(DISTINCT dest_entity)                AS distinct_dest_entities,
    MIN(timestamp)                             AS earliest_tx,
    MAX(timestamp)                             AS latest_tx,
    SUM(CASE WHEN src_jurisdiction != dst_jurisdiction THEN 1 ELSE 0 END)
                                                AS cross_border_tx_count
FROM transactions;

-- By currency
SELECT
    currency,
    COUNT(*)                                   AS tx_count,
    ROUND(SUM(amount), 2)                      AS total_value,
    ROUND(AVG(amount), 2)                      AS avg_amount,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM transactions), 2)
                                                AS pct_of_all_tx
FROM transactions
GROUP BY currency
ORDER BY total_value DESC;

-- By pattern type (the label the generator/evaluation pipeline assigns;
-- 'normal' dominates by design -- this table is what makes the ~92%
-- normal / ~8% anomalous class imbalance concrete)
SELECT
    pattern_type,
    COUNT(*)                                   AS tx_count,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM transactions), 2)
                                                AS pct_of_all_tx,
    ROUND(SUM(amount), 2)                      AS total_value,
    ROUND(AVG(amount), 2)                      AS avg_amount
FROM transactions
GROUP BY pattern_type
ORDER BY tx_count DESC;
