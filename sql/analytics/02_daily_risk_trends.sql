-- 02_daily_risk_trends.sql
-- Business question: How does suspicious activity change over time, and
-- is average risk trending up or down over the last 7 days? (business
-- question E from the spec)
--
-- Schema used: alerts JOIN transactions
-- Techniques: CTE, date truncation, window function (rolling AVG OVER),
--             LAG for day-over-day comparison

WITH daily AS (
    SELECT
        DATE(a.timestamp)                       AS alert_date,
        COUNT(*)                                AS alert_count,
        ROUND(AVG(a.risk_score), 4)             AS avg_risk_score,
        SUM(CASE WHEN a.risk_label IN ('CRITICAL', 'HIGH') THEN 1 ELSE 0 END)
                                                 AS high_or_critical_count,
        ROUND(SUM(t.amount), 2)                 AS flagged_value
    FROM alerts a
    JOIN transactions t ON t.tx_id = a.tx_id
    GROUP BY DATE(a.timestamp)
)
SELECT
    alert_date,
    alert_count,
    avg_risk_score,
    high_or_critical_count,
    flagged_value,
    -- 7-day rolling average risk score, ordered by date
    ROUND(AVG(avg_risk_score) OVER (
        ORDER BY alert_date
        ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
    ), 4)                                        AS rolling_7d_avg_risk,
    -- day-over-day change in alert volume
    alert_count - LAG(alert_count) OVER (ORDER BY alert_date)
                                                 AS alert_count_change_vs_prev_day,
    ROUND(
        avg_risk_score - LAG(avg_risk_score) OVER (ORDER BY alert_date), 4
    )                                            AS avg_risk_change_vs_prev_day
FROM daily
ORDER BY alert_date;
