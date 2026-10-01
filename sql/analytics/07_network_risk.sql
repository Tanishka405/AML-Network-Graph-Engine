-- 07_network_risk.sql
-- Business questions: Which entities participate in circular transaction
-- flows? Which act as major intermediaries? (business questions H, I)
--
-- Schema used: alerts JOIN transactions
-- Techniques: CTE, UNION ALL, GROUP BY, CASE, window function RANK()
--
-- graph_score and circular_score are written by backend/graph_engine.py
-- (betweenness/eigenvector centrality and SCC-cycle membership
-- respectively) at alert time -- this query surfaces them per entity
-- without recomputing graph topology in SQL, since that's NetworkX's job,
-- not SQL's. This is intentionally a reporting layer over the graph
-- engine's output, not a reimplementation of it.
--
-- circular_flow_alerts counts via `a.reason LIKE '%CIRCULAR FLOW:%'`,
-- not `circular_score > 0`. For this specific rule the two happen to
-- give the same count today, because rule_circular_flow() sets
-- circular_score to the graph engine's already-binary `in_cycle` value
-- (0.0 or 1.0) -- but relying on that coincidence would silently break
-- if circular_score ever became a continuous score like
-- structuring_score and velocity_score are (see
-- 06_structuring_analysis.sql and docs/analytics.md for the
-- investigation that found exactly this bug for those two rules). The
-- reason-text test is the one definition of "did the rule fire" that
-- is correct regardless of the scoring implementation.

WITH entity_graph_signals AS (
    SELECT t.source_entity AS entity, a.graph_score,
           a.reason LIKE '%CIRCULAR FLOW:%' AS circular_triggered
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
    UNION ALL
    SELECT t.dest_entity AS entity, a.graph_score,
           a.reason LIKE '%CIRCULAR FLOW:%'
    FROM alerts a JOIN transactions t ON t.tx_id = a.tx_id
)
SELECT
    entity,
    COUNT(*)                                              AS alert_count,
    ROUND(AVG(graph_score), 4)                             AS avg_graph_score,
    ROUND(MAX(graph_score), 4)                             AS max_graph_score,
    SUM(CASE WHEN circular_triggered THEN 1 ELSE 0 END)     AS circular_flow_alerts,
    CASE
        WHEN AVG(graph_score) >= 0.5 THEN 'LIKELY_INTERMEDIARY'
        WHEN AVG(graph_score) >= 0.25 THEN 'POSSIBLE_INTERMEDIARY'
        ELSE 'PERIPHERAL'
    END                                                     AS network_role,
    RANK() OVER (ORDER BY AVG(graph_score) DESC)            AS intermediary_rank
FROM entity_graph_signals
GROUP BY entity
HAVING COUNT(*) >= 2
ORDER BY avg_graph_score DESC
LIMIT 25;
