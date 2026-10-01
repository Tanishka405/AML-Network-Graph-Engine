# DAX Measures

Written against the star schema in `docs/powerbi.md`
(`fact_transactions`, `fact_alerts`, and the six `dim_*` tables), plus
the pre-aggregated analytical exports where noted. As in the previous
version of this document: ranking, rolling windows, and segmentation
are computed once in SQL (`sql/analytics/*.sql`) and imported as plain
columns — DAX here does filter-context aggregation, ratios, and time
intelligence, the things DAX is actually for.

## Volume and value

```dax
Total Transactions =
COUNTROWS ( fact_transactions )
```

```dax
Total Transaction Value =
SUM ( fact_transactions[amount] )
```

```dax
Average Transaction Value =
AVERAGE ( fact_transactions[amount] )
```

```dax
Distinct Entities =
DISTINCTCOUNT ( fact_transactions[source_entity_key] )
```

```dax
Transactions per Entity =
DIVIDE ( [Total Transactions], [Distinct Entities] )
```

## Alerts and risk

```dax
Alert Count =
COUNTROWS ( fact_alerts )
```

```dax
Alert Rate =
DIVIDE ( [Alert Count], [Total Transactions] )
```

```dax
High Risk Alerts =
CALCULATE ( [Alert Count], fact_alerts[risk_category_key] = "HIGH" )
```

```dax
Critical Alerts =
CALCULATE ( [Alert Count], fact_alerts[risk_category_key] = "CRITICAL" )
```

```dax
High Risk Alert Rate =
DIVIDE (
    CALCULATE ( [Alert Count], fact_alerts[risk_category_key] IN { "HIGH", "CRITICAL" } ),
    [Total Transactions]
)
```

```dax
Average Risk Score =
AVERAGE ( fact_alerts[risk_score] )
```

```dax
High Risk Entities =
CALCULATE (
    DISTINCTCOUNT ( fact_alerts[source_entity_key] ),
    fact_alerts[risk_category_key] IN { "HIGH", "CRITICAL" }
)
```

## Flagged value

```dax
Flagged Transaction Value =
CALCULATE (
    SUM ( fact_transactions[amount] ),
    fact_transactions[is_flagged] = TRUE
)
```

```dax
Flagged Value Rate =
DIVIDE ( [Flagged Transaction Value], [Total Transaction Value] )
```

## Time intelligence
*(requires `dim_date` marked as the model's Date table, related to
`fact_transactions[date_key]` and `fact_alerts[date_key]`)*

```dax
Transaction Value MoM % =
VAR CurrentValue = [Total Transaction Value]
VAR PriorValue =
    CALCULATE ( [Total Transaction Value], DATEADD ( dim_date[date_key], -1, MONTH ) )
RETURN
    DIVIDE ( CurrentValue - PriorValue, PriorValue )
```

```dax
Transaction Volume MoM % =
VAR CurrentValue = [Total Transactions]
VAR PriorValue =
    CALCULATE ( [Total Transactions], DATEADD ( dim_date[date_key], -1, MONTH ) )
RETURN
    DIVIDE ( CurrentValue - PriorValue, PriorValue )
```

```dax
YTD Transaction Value =
TOTALYTD ( [Total Transaction Value], dim_date[date_key] )
```

```dax
YTD Alert Count =
TOTALYTD ( [Alert Count], dim_date[date_key] )
```

## From the pre-aggregated analytical exports
*(these read a SQL-computed column directly rather than recomputing
the aggregation in DAX — see docs/powerbi.md section 9/10)*

```dax
Rolling 7-Day Avg Risk =
AVERAGE ( daily_risk_trends[rolling_7d_avg_risk] )
```

```dax
Alert Rate by Jurisdiction =
DIVIDE ( SUM ( jurisdiction_risk[alert_count] ), SUM ( jurisdiction_risk[tx_count] ) )
```

```dax
Top Rule by Contribution =
CALCULATE (
    SELECTEDVALUE ( rule_frequency[rule_name] ),
    TOPN ( 1, rule_frequency, rule_frequency[alert_contributions], DESC )
)
```
*(`rule_frequency[alert_contributions]` counts alerts where that rule's
`RuleResult.triggered` was actually True — read back from `alerts.reason`,
not from a raw component score. See docs/analytics.md "Rule trigger
frequency vs. raw signal magnitude" for the investigation that fixed
this. `graph_score` and `anomaly_score` are not rules and are not in this
table — see `continuous_risk_signal_summary.csv` for those.)*

## Deliberately not included

No DAX measure recomputes `RANK()`/`DENSE_RANK()`/`NTILE()` — those
already exist as columns from `sql/analytics/08_risk_rankings.sql` and
`sql/analytics/11_customer_segmentation.sql`. No measure references
`dim_jurisdiction` as if it had a single unambiguous relationship to
`fact_transactions` — it has two (source and destination; see
`docs/powerbi.md` section 5), so any jurisdiction-based measure must
either pick one relationship explicitly or use `USERELATIONSHIP()`; the
measures above avoid the ambiguity by working from `jurisdiction_risk.csv`
(already resolved to source-side in SQL) instead of guessing. No
Sharpe-ratio, VaR, or portfolio-style measure appears here — this is the
AML project, not the separate Portfolio Risk Analytics project.
