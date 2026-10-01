# Data Quality Framework

## Why this exists

Every KPI, SQL query and Power BI visual in this project reads from
`transactions` and `alerts`. If those tables can silently contain nulls,
negative amounts, or invalid currency codes, every number built on top of
them is untrustworthy. `backend/data_quality.py` makes that trust
explicit and checkable instead of assumed.

## Design principles

1. **Read-only.** No check ever deletes or modifies a row in
   `transactions` or `alerts`. A bad record stays in place so it can be
   investigated — this module only *reports*, via the
   `data_quality_results` table (see `backend/database.py`).
2. **Deterministic SQL, not heuristics.** Every check is a plain SQL
   query against the real schema. There is no sampling and no
   approximation — a check either finds a violation or it doesn't.
3. **Structured, not free-text.** Every check returns the same shape:
   `check_name`, `status`, `rows_checked`, `violations`,
   `violation_rate`, `severity`, `description`. That structure is what
   lets `sql/analytics/12_data_quality.sql` and the Power BI "Data
   Quality" page consume the results without special-casing anything.

## The 14 checks

| # | Check | What it catches | Severity if violated |
|---|---|---|---|
| 1 | `null_required_fields` | NULL in any column every downstream component needs | HIGH |
| 2 | `duplicate_transaction_ids` | tx_id appearing more than once (defensive — PK should prevent this) | HIGH |
| 3 | `duplicate_transaction_records` | Same source/dest/amount/currency/timestamp under different tx_ids | MEDIUM |
| 4 | `invalid_amounts` | amount ≤ 0 | HIGH |
| 5 | `extreme_amount_outliers` | amount > mean + 6σ (flagged, not rejected) | LOW |
| 6 | `invalid_currency` | Currency code outside `GEN_CFG.currencies` | MEDIUM |
| 7 | `invalid_jurisdiction` | Jurisdiction outside `GEN_CFG.all_jurisdictions` | MEDIUM |
| 8 | `self_transactions` | source_entity == dest_entity | MEDIUM |
| 9 | `invalid_timestamps` | Timestamp doesn't parse as a valid SQLite datetime | HIGH |
| 10 | `future_timestamps` | Timestamp after `datetime('now')` | HIGH |
| 11 | `invalid_transaction_type` | pattern_type outside the 6 labels the generator produces | MEDIUM |
| 12 | `negative_inter_arrival` | inter_arrival_s < 0 | LOW |
| 13 | `orphaned_alerts` | alert.tx_id with no matching transaction | HIGH |
| 14 | `blank_entity_ids` | source_entity/dest_entity is empty or whitespace | HIGH |

## A real bug this framework caught during development

`VALID_PATTERN_TYPES` was originally written from the *variable names* in
`backend/generator.py` (`smurf_fraction`, `circular_fraction`,
`velocity_fraction`), not from the actual string literals the generator
writes to the `pattern_type` column. Running the check against a real
5,000-row dataset immediately failed at a 35.75% violation rate. A quick
`SELECT DISTINCT pattern_type FROM transactions` showed the real values
are `structuring`, `circular_flow`, and `velocity_burst` — not
`smurfing`, `circular`, `velocity`. The set was corrected and pinned with
a regression test
(`tests/test_data_quality.py::TestPatternTypeValidation::test_every_real_generator_pattern_type_is_accepted`)
that runs every real pattern type through the check. This is the kind of
mistake this framework exists to catch — and it caught one on itself.

## Status thresholds

A check is `WARN` above a 0.1% violation rate and `FAIL` above 2%
(`extreme_amount_outliers` is exempted from FAIL since it's informational
by design — large legitimate wires exist). These thresholds are
deliberately loose defaults, not tuned against this dataset; treat them
as a starting point, not a claim of statistical rigor.

## Running it

```bash
python -m backend.data_quality
# writes data_quality_report.json and data_quality_results rows
```

Or as part of the full pipeline:

```bash
python scripts/build_analytics_dataset.py
```

## Reproducing the sample run in this repo

On the 5,000-transaction seeded sample (`seed=42`) committed as a
fixture: **13 of 14 checks pass**; `extreme_amount_outliers` warns at an
0.36% rate (18 of 5,000 transactions), which is expected — the generator
deliberately creates a small number of large layering-pattern
transactions (up to $500,000) as part of its designed anomaly mix, and
those are exactly what this check is supposed to surface for analyst
review, not silently hide.
