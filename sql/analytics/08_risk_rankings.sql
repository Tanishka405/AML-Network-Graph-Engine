-- 08_risk_rankings.sql
-- Business question: What are the top entities by composite risk score,
-- and what is the distribution of transaction amounts by risk category?
-- (business questions K, J)
--
-- Schema used: alerts JOIN transactions
-- Techniques: window functions ROW_NUMBER, RANK, DENSE_RANK together
--             (to show the difference), CASE, GROUP BY

-- Part 1: top entities by composite risk, three ranking styles side by side
WITH entity_risk AS (
    SELECT t.source_entity AS entity, MAX(a.risk_score) AS peak_risk_score
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
    GROUP BY t.source_entity
)
SELECT
    entity,
    peak_risk_score,
    ROW_NUMBER() OVER (ORDER BY peak_risk_score DESC)  AS row_number_rank,
    RANK()       OVER (ORDER BY peak_risk_score DESC)  AS rank_with_ties,
    DENSE_RANK() OVER (ORDER BY peak_risk_score DESC)  AS dense_rank_with_ties
FROM entity_risk
ORDER BY peak_risk_score DESC
LIMIT 25;

-- Part 2: transaction amount distribution by risk category
-- (buckets transactions by the risk_label of their alert, or 'UNFLAGGED'
-- if no alert exists for that tx_id)
SELECT
    COALESCE(a.risk_label, 'UNFLAGGED')                     AS risk_category,
    COUNT(*)                                                AS tx_count,
    ROUND(SUM(t.amount), 2)                                 AS total_value,
    ROUND(AVG(t.amount), 2)                                 AS avg_amount,
    ROUND(100.0 * SUM(t.amount) /
          (SELECT SUM(amount) FROM transactions), 2)        AS pct_of_total_value
FROM transactions t
LEFT JOIN alerts a ON a.tx_id = t.tx_id
GROUP BY COALESCE(a.risk_label, 'UNFLAGGED')
ORDER BY total_value DESC;
