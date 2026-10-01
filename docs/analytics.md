# Financial Analytics Layer

## What problem this layer solves

The core AML engine (graph analytics, Isolation Forest, rule engine,
composite risk scoring) answers *"is this transaction suspicious?"* in
real time, one transaction at a time. It does not answer *"which
entities are our biggest risk exposure this month, and why?"* — that is
a different question, asked after the fact, over the whole dataset, by
a compliance analyst rather than a real-time monitoring system. This
layer exists to answer that second question, without touching or
re-implementing the first.

## How the pieces fit together

```
backend/generator.py          → synthetic transactions (seeded, reproducible)
        ↓
backend/streaming._process_transaction()   → the REAL pipeline: graph engine,
        ↓                                     SQL structuring/velocity scores,
   transactions, alerts                       Isolation Forest, AML rules,
   (SQLite tables)                            composite risk -- run once per
        ↓                                     transaction, same code path the
backend/data_quality.py                       live FastAPI service uses
        ↓ (writes data_quality_results)
sql/analytics/*.sql            → 12 files, 19 result sets, answering the
        ↓                         16 business questions below
scripts/build_analytics_dataset.py  → orchestrates all of the above,
        ↓                              exports Power-BI-ready CSVs
exports/*.csv                  → docs/powerbi.md explains how to load
                                  these into a star-schema Power BI model
```

Nothing in this layer recomputes graph centrality, retrains the
Isolation Forest, or reimplements an AML rule in SQL. SQL's job here is
aggregation and ranking over what the real pipeline already computed —
matching the same division of labour the original `database.py`
docstring describes ("SQL is a first-class analytical tool ... not just
a store").

## Reproducibility

`scripts/build_analytics_dataset.py` defaults to **5,000 transactions,
seed=42** — not the generator's full 500,000-transaction default. This
was a deliberate choice: running all 5,000 transactions through the real
per-transaction pipeline (graph updates, Isolation Forest retraining
every 200 samples, AML rule evaluation) took **~51 seconds** on the
machine this was built on — see the timing log kept in this repo's build
history. At 500K transactions that would scale to something on the order
of hours, and a multi-hundred-MB database is exactly what the project's
own "do not commit enormous generated datasets" rule exists to prevent.
5,000 transactions is large enough to produce a realistic mix of alert
types (610 alerts, ~12.2% alert rate in the committed sample) while
staying a small, fast, git-friendly fixture. Re-running with
`--transactions 500000` works if you want the larger scale — just don't
commit the resulting database or exports.

### Reproducibility

Earlier builds of this layer showed the same `--transactions 5000
--seed 42` command producing a *different* alert count on every run
(602/606/607/610 observed across runs) — the two root causes, how they
were found, and how they were fixed:

1. **`nx.betweenness_centrality(G, k=k_samples, ...)` in
   `backend/graph_engine.py`.** Once a graph exceeds 50 nodes, NetworkX
   switches to a k-sampled approximation of betweenness centrality,
   which uses `random.sample()` internally to pick the sampled source
   nodes. The call site didn't pass `seed=`, so it drew from Python's
   unseeded global RNG — a different sample, and therefore a different
   betweenness score (and therefore a different graph_score, IsolationForest
   feature vector, and risk score), on every single call, independent of
   any `random_state` set elsewhere. Traced by capturing every
   transaction's feature vector across two same-process runs and finding
   the first index where they diverged (transaction 97 of 1500, well
   before the Isolation Forest was even trained) — the divergent field
   was `graph_score`, which pointed straight at the graph engine.
   **Fix:** added `betweenness_seed: int = 42` to `GraphConfig` and
   passed it as `nx.betweenness_centrality(..., seed=self.cfg.betweenness_seed)`.
2. **`rng.choice(list(cfg.high_risk_jurisdictions))` in
   `backend/generator.py`.** `high_risk_jurisdictions` is a `Set[str]`.
   Converting a set of strings to a list relies on Python's string hash
   order, which is randomised per process by default (`PYTHONHASHSEED`)
   unless explicitly fixed — so the *same* RNG draw index could select a
   *different* jurisdiction in different processes, even with the same
   seeded `np.random.Generator`. Confirmed by hashing the generator's
   full output under three different `PYTHONHASHSEED` values before and
   after the fix. **Fix:** `rng.choice(sorted(cfg.high_risk_jurisdictions))`
   — a deterministic, sorted sequence instead of set-order-dependent one.

Both are covered by `tests/test_reproducibility.py` (10 tests, including
a mutation check confirming the tests fail if either fix is reverted).
`scripts/verify_reproducibility.py --runs 5` runs the full pipeline from
a clean state 5 times and diffs every export; as of this fix, all 5 runs
produce **identical transaction/alert/HIGH/CRITICAL/avg-risk-score
values and byte-identical exports**, with one intentional exception: two
files (`kpi_summary.csv`'s `generated_at_utc`, `data_quality_run_history.csv`'s
`run_timestamp`) carry a wall-clock build timestamp by design, and only
that one field differs between runs in those two files — verified
explicitly, not just excluded from the comparison.

## The 12 SQL analytics files

| File | Business questions answered | Key SQL technique |
|---|---|---|
| `01_transaction_summary.sql` | Overall shape of the data: volume, value, currency/pattern mix | GROUP BY, CASE |
| `02_daily_risk_trends.sql` | Is suspicious activity rising or falling? | Rolling `AVG() OVER`, `LAG()` |
| `03_entity_activity.sql` | Which entities have the highest volume/value? | UNION ALL, `RANK()`, `DENSE_RANK()` |
| `04_high_risk_entities.sql` | Which entities have the highest AML risk? | CTE, HAVING, `RANK()` |
| `05_transaction_velocity.sql` | Which entities show unusual transaction velocity? | `LAG()`/`LEAD()`, `ROW_NUMBER()` |
| `06_structuring_analysis.sql` | Which entities have repeated structuring alerts? | CASE-based pattern classification |
| `07_network_risk.sql` | Which entities are intermediaries / in circular flows? | UNION ALL, `RANK()` over graph_score |
| `08_risk_rankings.sql` | Top entities by composite risk; amount distribution by risk category | `ROW_NUMBER()` vs `RANK()` vs `DENSE_RANK()` side by side |
| `09_temporal_analysis.sql` | Rolling volume trend; month-over-month entity comparison | Rolling `SUM() OVER`, `LAG()` |
| `10_alert_analysis.sql` | % flagged HIGH/CRITICAL; which rule fires most often | Subquery percentages, UNION ALL |
| `11_customer_segmentation.sql` | Risk tiering; jurisdiction exposure | `NTILE(4)`, CASE segmentation |
| `12_data_quality.sql` | Can this data be trusted? | Reports `data_quality_results`, not a reimplementation |

Every window function required by the brief is used somewhere it
answers a real question, not as a checklist item: `ROW_NUMBER()`,
`RANK()`, `DENSE_RANK()`, `LAG()`, `LEAD()`, `SUM() OVER`, `AVG() OVER`,
and `NTILE()`.

## KPI definitions

Computed in `scripts/build_analytics_dataset.py::compute_kpis()`, each as
a single deterministic SQL aggregate — no number here is invented or
estimated:

| KPI | Definition |
|---|---|
| `total_transactions` | `COUNT(*)` on `transactions` |
| `total_transaction_value` | `SUM(amount)` |
| `average_transaction_value` | `AVG(amount)` |
| `median_transaction_value` | True median via offset-based row selection (SQLite has no `MEDIAN()`) |
| `total_alerts` | `COUNT(*)` on `alerts` |
| `suspicious_transaction_rate_pct` | `total_alerts / total_transactions * 100` |
| `critical_alerts` / `high_alerts` | Count of alerts with that `risk_label` |
| `high_risk_entity_count` | Entities whose alerts (as source or dest) average `risk_score >= 0.60` |
| `flagged_transaction_value` | Sum of `amount` for transactions with at least one alert |
| `flagged_transaction_value_pct` | `flagged_transaction_value / total_transaction_value * 100` |
| `structuring_alert_count` | Alerts where `reason LIKE '%STRUCTURING:%'` (the structuring rule actually triggered — see "Rule trigger frequency vs. raw signal magnitude" below) |
| `circular_flow_entity_count` | Distinct entities (source or dest) on a transaction where `reason LIKE '%CIRCULAR FLOW:%'` |
| `high_risk_jurisdiction_exposure_value` | Value of transactions where `src_jurisdiction` is in the configured high-risk set |
| `average_risk_score` | `AVG(risk_score)` on `alerts` |

## Rule trigger frequency vs. raw signal magnitude

`rule_frequency.csv` originally showed `structuring` at ~100% of alerts
(609 of 610). This was investigated end to end rather than patched
away, and turned out to be a genuine reporting bug (not a real-world
data artifact and not an underlying risk-engine bug) — three distinct
concepts were being conflated:

- **`pattern_type`** — what synthetic scenario the generator produced
  for a transaction (`normal`, `structuring`, `velocity_burst`, ...).
- **A rule trigger** — `backend/aml_rules.py`'s `RuleResult.triggered`,
  a boolean gated by real thresholds (e.g. `rule_structuring()` requires
  `window_count >= 3 AND amount < $10,000 AND score > 0.1`). Only
  triggered rules contribute a sentence to `alerts.reason`
  (`evaluate_all_rules()`: `reasons = [r.reason for r in all_rules if
  r.triggered and r.reason]`) — `reason` is therefore the one place the
  application's own trigger decision is actually persisted.
- **A raw component score** — `alerts.structuring_score`,
  `.velocity_score`, etc. — a continuous value written to every alert
  **regardless of whether the corresponding rule triggered**.

`sql/analytics/10_alert_analysis.sql`, `06_structuring_analysis.sql`,
`07_network_risk.sql`, and
`scripts/build_analytics_dataset.py::compute_kpis()`'s
`structuring_alert_count`/`circular_flow_entity_count` all used
`<component>_score > 0` as a stand-in for "the rule fired." For
`structuring_score` and `velocity_score` — genuinely continuous scores
— this overcounted badly: 609/610 alerts had `structuring_score > 0`,
but only **466** actually had the structuring rule trigger
(`reason LIKE '%STRUCTURING:%'`). `circular_score` and
`high_risk_jx_score` happen to be inherently binary/discrete in the
current implementation, so `score > 0` coincidentally equalled
"triggered" for those two (609/609 and 460/460) — but every occurrence
was still switched to the `reason`-based test, since relying on that
coincidence would silently break again if either score's implementation
ever became continuous.

Two of the "rules" in the original `rule_frequency.csv`
(`graph_centrality`, `isolation_forest_anomaly`) aren't `aml_rules.py`
rules at all — neither has a `RuleResult`/`triggered` concept, a reason
string, or a firing threshold anywhere in the codebase; they contribute
to `composite_risk` purely as continuous, weighted magnitudes. Reporting
them as "rule frequency" was itself the pattern_type/rule/alert
conflation this investigation was asked to check for, so they now have
their own export, `continuous_risk_signal_summary.csv`, reporting
average/max magnitude rather than a trigger count.

**Before vs. after** (canonical 5,000-tx, seed=42 fixture):

| Rule | Before (`score > 0`) | After (`reason`-based, correct) |
|---|---|---|
| structuring | 609 (99.8%) | **466 (76.4%)** |
| velocity | 610 (100%) | **459 (75.2%)** |
| circular_flow | 609 (99.8%) | 609 (99.8%) — unchanged, coincidental binary score |
| high_risk_jurisdiction | 460 (75.4%) | 460 (75.4%) — unchanged, coincidental discrete score |
| layering | not previously reported | **76 (12.5%)** — newly surfaced; see gap below |

Total transactions/alerts/HIGH/CRITICAL/avg risk score were **unchanged
by this fix** — it only corrected how existing alerts were being
counted and labeled, not which transactions get alerted or how
`composite_risk` is computed. Regression tests for this in
`tests/test_sql_analytics.py::TestRuleTriggerVsRawScoreSemantics` (6
tests, including a mutation check confirming they fail if any query
reverts to `score > 0`).

**Known gap: the layering rule.** `rule_layering()` computes a real
score and reason, and its reason text does appear in `alerts.reason`
when triggered — but `risk_engine.py::enrich_transaction()` never
extracts `layering_score` from the rules dict, so it is never included
in the `composite_risk` weighted sum (`RiskWeights` in `config.py` has
no `layering` weight either) and never persisted as its own column on
`alerts`. This means: (a) `avg_score_when_triggered` for layering in
`rule_frequency.csv` is `NULL` by necessity, not a query bug, and (b)
the layering rule currently has zero influence on whether a transaction
becomes an alert at all — it can only ever show up as one line of
explanatory text on an alert some *other* rule already triggered. This
is a real, pre-existing property of the risk engine, left unchanged
here per instructions to fix only the reporting layer — flagged, not
fixed, so it isn't mistaken for something this investigation resolved.

## Business questions → method map

| # | Question | Method |
|---|---|---|
| A | Highest transaction volume entities? | `03_entity_activity.sql` |
| B | Highest total transaction value entities? | `03_entity_activity.sql` |
| C | Highest AML risk entities? | `04_high_risk_entities.sql` |
| D | % of transactions flagged HIGH/CRITICAL? | `10_alert_analysis.sql` part 1 |
| E | How does suspicious activity change over time? | `02_daily_risk_trends.sql` |
| F | Unusual transaction velocity? | `05_transaction_velocity.sql` |
| G | Repeated structuring alerts? | `06_structuring_analysis.sql` |
| H | Circular transaction flows? | `07_network_risk.sql` (circular_flow_alerts column) |
| I | Major intermediaries? | `07_network_risk.sql` (network_role column) |
| J | Amount distribution by risk category? | `08_risk_rankings.sql` part 2 |
| K | Top entities by composite risk score? | `08_risk_rankings.sql` part 1 |
| L | Rolling transaction volume over time? | `09_temporal_analysis.sql` part 1 |
| M | Entity current vs. previous activity? | `09_temporal_analysis.sql` part 2 |
| N | % of total value associated with flagged activity? | `10_alert_analysis.sql` part 1 |
| O | Jurisdiction risk/transaction exposure? | `11_customer_segmentation.sql` part 2 |
| P | Which AML rules contribute most to alerts? | `10_alert_analysis.sql` part 2 |

## How a business analyst would investigate a finding

1. Start on the KPI summary (`exports/kpi_summary.csv` or the Power BI
   Executive page) to see whether the suspicious rate or flagged value
   moved.
2. Drill into `entity_risk_summary.csv` / `04_high_risk_entities.sql` to
   find which entities are driving that movement.
3. Cross-reference `structuring_analysis.csv` and `network_risk.csv` for
   that entity — is the alert a one-off, or a repeated pattern with a
   graph signal behind it?
4. Check `data_quality_report.csv` for that time period before drawing a
   conclusion — if a data quality check failed for that window, the
   finding needs to be caveated, not reported as fact.

This is the WHAT → HOW → WHY chain the project is designed to support end
to end, not just at the transaction-scoring layer.
