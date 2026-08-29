# Architecture Documentation

## Component Map

```
backend/
├── config.py              All constants, weights, thresholds in one place
├── database.py            SQLite layer + SQL AML aggregation queries
├── generator.py           Reproducible synthetic transaction generator
├── graph_engine.py        Rolling directed graph + NetworkX analytics
├── feature_engineering.py 22-dimensional feature vector construction
├── anomaly_detector.py    Thread-safe Isolation Forest wrapper
├── aml_rules.py           Five explicit AML rules → normalised scores
├── risk_engine.py         Weighted composite score + alert serialisation
├── streaming.py           Central async processing loop + WS broadcast
└── main.py                FastAPI app, lifecycle, WebSocket endpoint
```

---

## config.py

Single source of truth for all tuneable parameters. Every other module imports from here — no magic numbers are scattered across the codebase.

Key dataclasses:
- `GeneratorConfig` — synthetic dataset parameters
- `DatabaseConfig` — SQLite path, structuring window
- `GraphConfig` — rolling window, centrality parameters
- `IFConfig` — Isolation Forest hyperparameters
- `AMLRuleConfig` — rule thresholds and jurisdictions
- `RiskWeights` — composite score component weights (validated to sum to 1.0)

---

## database.py

### Schema

**transactions** — raw transaction store with indexes on `(source_entity, timestamp)`, `amount`, and `pattern_type`.

**alerts** — one row per triggered alert with all component scores and a human-readable reason string.

**evaluation_results** — stores evaluation run metadata for reproducibility tracking.

### SQL Structuring Detection

The key query joins `transactions` to itself on `source_entity` and a time window:

```sql
SELECT t1.tx_id, COUNT(t2.tx_id) AS window_count,
       SUM(t2.amount) AS window_sum, AVG(t2.amount) AS window_avg
FROM   transactions t1
JOIN   transactions t2
       ON  t2.source_entity = t1.source_entity
       AND t2.amount        < :threshold
       AND t2.timestamp    >= datetime(t1.timestamp, :neg_window)
       AND t2.timestamp    <= t1.timestamp
WHERE  t1.amount < :threshold
GROUP  BY t1.tx_id
HAVING COUNT(t2.tx_id) >= :min_count
```

This detects coordinated sub-threshold behaviour — not individual low-value transactions.

---

## graph_engine.py

### TransactionGraph

Maintains a `networkx.DiGraph` over a rolling 1-hour window. Edge attributes:
- `weight` — cumulative transaction amount
- `tx_count` — number of transactions on this edge
- `avg_amount` — mean transaction amount

### Rolling Window Correctness

Each transaction is tracked as an `EdgeInstance` in a per-edge `EdgeData` object. When `_expire_old()` runs, the **full expiry queue is scanned** (not just the front), so out-of-order timestamps (common in batch evaluation) are handled correctly. An edge is removed only when its last active instance expires.

### Centrality Algorithms

| Algorithm | Implementation | Notes |
|---|---|---|
| Betweenness | `nx.betweenness_centrality(G, k=min(50,n))` | k-sample approximation when n>50 |
| Eigenvector | `nx.eigenvector_centrality_numpy(UG)` | Undirected projection; LAPACK-backed, always converges |
| PageRank | `nx.pagerank(G, alpha=0.85)` | Directed, for broadcast network visualisation |
| Communities | `nx.greedy_modularity_communities(UG)` | On undirected projection |
| Cycles | SCC decomposition — nodes in SCCs of size ≥ 2 | Avoids O(V+E+C) `simple_cycles` |

### Performance

The `_batch_mode=True` flag disables per-add expiry checking during bulk ingestion. After bulk ingest, call `graph._recompute()` once to update all centrality metrics. During live streaming, centrality is recomputed every 50 additions.

---

## feature_engineering.py

Builds the 22-dimensional feature vector fed to Isolation Forest. Inputs:
- Raw transaction dict (7 features)
- SQL structuring map (3 features)
- SQL velocity map (2 features)
- Graph node features (10 features)

`pattern_type` is never included. The feature names are exported as `FEATURE_NAMES` for documentation and debugging.

---

## anomaly_detector.py

Thread-safe `AMLAnomalyDetector` wrapping `sklearn.ensemble.IsolationForest`.

- `add_sample(vec)` — adds to rolling training buffer; returns current score (0.0 if not warm)
- `score(vec)` — single-vector scoring: `clip((-raw - 0.1) * 2.0, 0, 1)`
- `score_batch(X)` — vectorised batch scoring (single sklearn call, much faster)
- `train(X)` — fits model on provided matrix (used by evaluation pipeline)
- Automatic retraining every `retrain_interval` samples after warm-up

---

## aml_rules.py

Five rules, each returning a `RuleResult(rule_name, score, reason, triggered)`:

| Rule | Detection logic | Score source |
|---|---|---|
| `rule_structuring` | SQL window_count ≥ 3, amount < threshold | `sql_entity_structuring_score` |
| `rule_velocity` | SQL burst_count ≥ 5 in 300s window | `sql_velocity_score` |
| `rule_circular_flow` | Entity in graph SCC of size ≥ 2 | `graph_feats["in_cycle"]` |
| `rule_layering` | multi_hop_score + betweenness | Graph metrics |
| `rule_high_risk_jx` | Either jurisdiction in configured set | Transaction fields |

---

## streaming.py

Single async task `run_stream_loop()` drives the entire pipeline:

1. `next(stream_transactions())` — yield one transaction
2. `_process_transaction(tx)` — synchronous, runs in executor:
   - Insert to SQLite
   - Update rolling graph
   - Extract graph features
   - Fetch SQL structuring + velocity scores
   - Build 22-dim feature vector
   - Score with Isolation Forest
   - Evaluate AML rules
   - Compute composite risk
   - Persist alert if threshold exceeded
3. `_broadcast(record)` — push JSON payload to every subscribed client queue

Each WebSocket client has a private `asyncio.Queue`. The broadcast pushes to all queues; full queues drop the oldest item.

---

## Synthetic Generator

Patterns and their share of a 500 K dataset (seed=42):

| Pattern | Fraction | Description |
|---|---|---|
| `normal` | ~85% | Log-normal amounts, random entities/jurisdictions |
| `structuring` | ~4% | 3–8 coordinated sub-$10K txs in short windows |
| `velocity_burst` | ~3% | 5–20 rapid txs from one entity in ≤5 minutes |
| `layering` | ~3% | 3–6 hop chain with decreasing amounts |
| `circular_flow` | ~2% | Cycle of 3–5 entities |
| `high_risk_jx` | ~3% | Large amounts through configured jurisdictions |

`inter_arrival_s` is recomputed **after** sorting by timestamp, so values are globally correct regardless of generation order.

Ground-truth `pattern_type` is stored in the DB and used **only** to compute evaluation metrics — never as a model feature.
