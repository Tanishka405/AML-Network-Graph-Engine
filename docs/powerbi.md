# Power BI Model

**No `.pbix` file is included in this repository.** Power BI Desktop was
not available in the environment this was built in. What follows is the
complete dataset, star-schema model, DAX, and page specification a Power
BI build would use — stated as a spec, not a claim that a working
`.pbix` already exists. Everything below (table grain, keys, row counts,
FK integrity) was verified programmatically against the actual exported
CSVs — see `tests/test_star_schema.py`.

## 1. Table inventory

### Core star schema (new — the source model this doc is primarily about)

| Table | Kind | Rows (5,000-tx sample) |
|---|---|---|
| `fact_transactions.csv` | Fact | 5,000 |
| `fact_alerts.csv` | Fact | 610 |
| `dim_entity.csv` | Dimension | 200 |
| `dim_date.csv` | Dimension | 190 |
| `dim_currency.csv` | Dimension | 7 |
| `dim_jurisdiction.csv` | Dimension | 14 |
| `dim_risk_category.csv` | Dimension | 5 |
| `dim_transaction_pattern.csv` | Dimension | 6 |

### Analytical exports (existing — pre-aggregated, report-specific; kept as-is)

The 20 files from `sql/analytics/*.sql` (`entity_risk_summary.csv`,
`daily_risk_trends.csv`, `rule_frequency.csv`, `jurisdiction_risk.csv`,
`data_quality_report.csv`, etc.) remain available and are still useful
— several dashboard pages below use them directly instead of
re-deriving the same aggregation in DAX. They are **not** part of the
star schema's relationship graph; each stands alone as the answer to
one specific business question. Full list and business-question mapping
in `docs/analytics.md`.

## 2. Grain of every table

| Table | Grain (what one row represents) |
|---|---|
| `fact_transactions` | One transaction |
| `fact_alerts` | One alert (an alert is generated for at most one transaction; not every transaction has one — see 4) |
| `dim_entity` | One entity that appears as a source or destination in at least one transaction |
| `dim_date` | One calendar date that appears in `transactions.timestamp` |
| `dim_currency` | One currency code that appears in `transactions.currency` |
| `dim_jurisdiction` | One jurisdiction code that appears as a source or destination jurisdiction |
| `dim_risk_category` | One risk label (CRITICAL/HIGH/MEDIUM/LOW/UNFLAGGED) |
| `dim_transaction_pattern` | One pattern_type label the generator produces |

Verified: `fact_transactions` row count == `SELECT COUNT(*) FROM
transactions`; `fact_alerts` row count == `SELECT COUNT(*) FROM
alerts`; `dim_entity` row count == distinct entities across both
source and dest columns. Tested in
`tests/test_star_schema.py::TestGrain`.

## 3 and 4. Primary keys and foreign keys

| Table | Primary key | Foreign keys |
|---|---|---|
| `fact_transactions` | `tx_id` | `date_key` to dim_date; `source_entity_key`/`dest_entity_key` to dim_entity; `currency_key` to dim_currency; `src_jurisdiction_key`/`dst_jurisdiction_key` to dim_jurisdiction; `pattern_type_key` to dim_transaction_pattern |
| `fact_alerts` | `alert_id` | `tx_id` to fact_transactions; `date_key` to dim_date; `risk_category_key` to dim_risk_category; `source_entity_key`/`dest_entity_key` to dim_entity |
| `dim_entity` | `entity_id` | none |
| `dim_date` | `date_key` | none |
| `dim_currency` | `currency_code` | none |
| `dim_jurisdiction` | `jurisdiction_code` | none |
| `dim_risk_category` | `risk_category` | none |
| `dim_transaction_pattern` | `pattern_type` | none |

PK uniqueness and FK integrity (every fact-table key value resolves to
an existing dimension row, zero orphans) are asserted in
`tests/test_star_schema.py::TestPrimaryKeyUniqueness` and
`::TestForeignKeyIntegrity`, and were independently checked against the
real 5,000-row build (0 FK violations found).

## 5. Relationship cardinalities

```
                          dim_date (1) ----+
                                            | (*)
dim_entity (1) --(*)-- fact_transactions --+-- dim_currency (1)
      |                      |             |
      | (*)                  | (*)         +-- dim_jurisdiction (1)  [x2: src, dst]
      |                      |
      |                fact_alerts (*)--(1) dim_risk_category
      |                      |
      +----------(*)---------+
                                fact_transactions --(*)--(1)-- dim_transaction_pattern
```

Every fact-to-dimension relationship is **many-to-one**, filtering in
the standard single direction (dimension filters fact). `fact_transactions`
has **two** relationships to `dim_entity` (source and dest) and **two**
to `dim_jurisdiction` (source and dest) — in Power BI, only one of each
pair can be the *active* relationship at a time; the others must be
marked inactive and invoked with `USERELATIONSHIP()` in specific
measures (e.g. a "transactions where the destination is high-risk"
measure). This is a real modeling constraint of this data, not
simplified away.

`fact_alerts.tx_id` to `fact_transactions.tx_id` is a **fact-to-fact**
relationship (one row in `fact_transactions` has at most one matching
row in `fact_alerts`, since `alerts.tx_id` has no duplicates — the same
kind of check as the `duplicate_transaction_ids` data-quality check,
applied to alerts). This is why `fact_transactions.is_flagged` is
precomputed as a boolean column: not to avoid an honest 1:1 fact
relationship, but because "does this transaction have an alert" is
exactly the kind of has-a-match check that's cheap to compute once in
SQL and then just filter/sum in DAX, versus repeating a
`COUNTROWS(RELATEDTABLE())` pattern on every visual.

## 6 and 7. Dimensions vs. facts

**Facts** (numeric measures, one row per event): `fact_transactions`,
`fact_alerts`.

**Dimensions** (attributes to filter/group/slice by): `dim_entity`,
`dim_date`, `dim_currency`, `dim_jurisdiction`, `dim_risk_category`,
`dim_transaction_pattern`.

`dim_entity` is a **degenerate dimension** in the strict sense — the
only thing known about an entity in this dataset is its ID string.
There is no entity master table (name, type, onboarding date, etc.)
anywhere in the underlying schema, so none is invented here. `dim_date`
and `dim_jurisdiction` add a small number of genuinely derived or
config-sourced columns (calendar fields computed from the date itself;
`is_high_risk` read directly from
`backend.config.AML_CFG.high_risk_jurisdictions`, the same set the real
AML rule engine uses) — verified against that config in
`tests/test_star_schema.py::TestNoInventedColumns`, not hand-typed to
match. `dim_transaction_pattern.description` is the one column in this
model that is authored text (a human explanation of what each pattern
label means) rather than derived from data — called out here so it's
not mistaken for sourced business data.

## 8. Which analytical exports are optional / report-specific

All 20 files from `sql/analytics/*.sql` are optional relative to the
star schema — none of them are required for the model to work, and none
of them participate in the relationship graph above. They exist because
several of the dashboard pages below are faster and clearer built
directly from a pre-aggregated, single-purpose table (e.g.
`daily_risk_trends.csv` already has the rolling-7-day window computed)
than from re-deriving the same rolling calculation in DAX against
`fact_transactions` every time a visual renders.

## 9 and 10. SQL vs. DAX

**In SQL** (`sql/analytics/*.sql`, run once at build time): anything
involving a window function, multi-row window frame, or ranking —
rolling averages, `RANK()`/`DENSE_RANK()`/`NTILE()`, month-over-month
deltas via `LAG()`. SQLite computes these once, correctly, and they're
imported as plain columns.

**In DAX** (computed live, per filter context): simple aggregations
that need to respond to whatever the user has sliced or filtered
(`SUM`, `AVERAGE`, `DIVIDE`-based rates), and genuine time intelligence
against the `dim_date` table (`TOTALYTD`, `DATEADD`) — because *which*
period counts as "this month" depends on what the user has selected,
which SQL cannot know in advance at build time. See
`docs/dax_measures.md` for the full, current measure list.

## Dashboard pages

### Page 1 -- Executive Risk Overview
- KPIs: Total Transactions, Total Transaction Value, Alert Rate,
  High/Critical Alerts, Flagged Transaction Value (DAX measures — see
  `docs/dax_measures.md`)
- Transaction volume trend: line chart, `fact_transactions` grouped by
  `dim_date`
- Alert trend: line chart, `fact_alerts` grouped by `dim_date`, or
  `daily_risk_trends.csv` directly for the rolling-7-day view
- Risk distribution: bar/donut, `fact_alerts` by `dim_risk_category`
- Transaction value by pattern: bar chart, `fact_transactions` by
  `dim_transaction_pattern`, `SUM(amount)`

### Page 2 -- Transaction & Risk Analytics
- Transaction volume over time / transaction value over time (line
  charts off `fact_transactions` + `dim_date`)
- Risk-category distribution (bar, `fact_alerts` by `dim_risk_category`)
- Currency analysis (bar, `fact_transactions` by `dim_currency`)
- Jurisdiction analysis (bar/map, `fact_transactions` by
  `dim_jurisdiction` — note the src/dst relationship caveat in section 5)
- Filters: date range (`dim_date`), currency (`dim_currency`),
  jurisdiction (`dim_jurisdiction`), pattern (`dim_transaction_pattern`)

### Page 3 -- Entity Risk Investigation
- Highest-risk entities: table, `entity_risk_summary.csv` (pre-ranked
  in SQL) or `fact_alerts` grouped by `source_entity_key`
- Transaction value vs. risk score: scatter, `fact_transactions[amount]`
  (x) vs. average `fact_alerts[risk_score]` for that entity (y)
- Transaction frequency: bar, `COUNTROWS` of `fact_transactions` by
  `dim_entity`
- Drill/filter behavior: clicking an entity in any visual cross-filters
  the whole page via the `dim_entity` relationship
- High-risk entity table: `entity_risk_summary.csv` or a DAX-filtered
  `fact_alerts` table visual

### Page 4 -- AML Investigation
- Alerts by rule (actually triggered, not raw score -- see docs/analytics.md): `rule_frequency.csv` (bar)
- Continuous risk signal magnitude (graph centrality, IF anomaly -- NOT rule counts): `continuous_risk_signal_summary.csv`
- Alerts by risk level: `fact_alerts` by `dim_risk_category`
- Structuring analysis: `structuring_analysis.csv`
- Velocity analysis: `transaction_velocity.csv`
- Network-risk outputs / circular-flow indicators: `network_risk.csv`
  (these come from the graph engine's `graph_score`/`circular_score`
  columns on `fact_alerts`, already present as plain numeric columns —
  no graph computation happens in Power BI)

### Page 5 -- Data Quality & Model Performance
- DQ pass/warn/fail counts and violation counts:
  `data_quality_report.csv`, conditional-formatted table
- Model threshold information: `dim_risk_category.min_score_threshold`
  — the exact thresholds `backend.risk_engine` uses, displayed as a
  reference table, not re-derived
- Precision/recall from `scripts/evaluate_model.py`'s
  `evaluation_results` table, if exported (not currently in `exports/`
  — see Remaining Gaps below)
- Important limitations, stated on the page itself, not just in docs:
  this is a synthetic dataset (see the synthetic-data disclaimer at the
  top of the README); precision/recall figures describe this model's
  performance against the synthetic generator's own labels, not
  real-world production AML performance, and must not be presented as
  such.

## Remaining gaps in this spec

- `evaluate_model.py`'s precision/recall results live in the
  `evaluation_results` SQLite table but are not currently exported to
  `exports/` for Power BI — Page 5 above references them as a planned
  addition, not something already wired up. Adding an
  `evaluation_results.csv` export would close this gap in one small
  change to `build_analytics_dataset.py`.
- No `.pbix` exists (stated above and in the README) — this document is
  the complete spec a Power BI build would follow, not evidence that
  build has happened.
