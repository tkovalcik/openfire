# Feature Engineering Handoff

This guide is the short version of "what's in the warehouse, why, and how to
extend it without breaking model training." Read this before adding a new
feature or kicking off a training run.

## Data layers (Bronze / Silver / Gold)

We use the standard medallion layout. Each layer has a different grain and a
different contract.

- **Bronze** — raw, append-only ingestion of source data. Earth Engine
  exports, GRIDMET pulls, FRAP fire perimeters, etc. land here essentially
  unchanged. Bronze is allowed to be messy: schema drift, duplicates, partial
  shards, and source-specific quirks all live here.
- **Silver** — cleaned, conformed, per-year tables (e.g.
  `silver_features_2024`). One row per **(date, latitude, longitude)** cell.
  Bronze sources are joined into a single wide row at this point. This is the
  layer we run duplicate / null checkpoints against.
- **Gold** — modeling-ready tables (e.g. `gold_features`). The merged
  all-years silver is reshaped into the exact columns the model consumes:
  target label, time-lag deltas, days-since-last-burn, etc. Anything written
  to Gold should be safe to feed straight into the trainer.

The current SQL implementations are:

- `data_pipelines/04_merge_silver_years.sql` — wildcard-merges the yearly
  silver tables into `silver_features_all_years`, dedups on
  `(latitude, longitude, timestamp)`, and joins fire history per cell.
- `data_pipelines/05_engineer_gold_features.sql` — produces `gold_features`
  with the target label and lag/rolling deltas.

## Expected grain of the silver feature tables

> One row per **`(CAST(timestamp AS DATE), latitude, longitude)`** cell.

There should be exactly one row for each (cell, date) pair across all silver
shards. This is the assumption every downstream step relies on.

## Why duplicate rows break LAG / rolling features

The gold-feature SQL computes time-series deltas with window functions like:

```sql
mean_NDVI - LAG(mean_NDVI, 6) OVER (
  PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING)
  ORDER BY window_start_date
)
```

If silver contains duplicate rows for the same cell-date:

- `LAG(..., 6)` may now reach back 6 *rows* but only 3 *days*, silently
  producing a feature with the wrong horizon.
- The rolling deltas become non-deterministic because BigQuery is free to
  pick any tie-breaking order.
- The target column (`burned_in_next_15_days`) ends up duplicated, which
  inflates the apparent positive class and biases evaluation metrics.

This is why duplicate detection is **checkpoint 01** and dedup is
**checkpoint 02** — those have to be clean before anything else is trusted.

## How gold features are used for training

The trainer reads `gold_features` (or its exported parquet copy from
`data_pipelines/06_export_gold_to_gcs.sql`) and treats each row as one
training example for the cell on `window_start_date`:

- **Target**: `burned_in_next_15_days` (boolean) — whether the cell burned
  within 1–15 days *after* the window start. The forward-only definition is
  what makes the task a forecast rather than a fit.
- **Features**: terrain, vegetation indices, raw Sentinel-2 bands, GRIDMET
  weather summaries, and the engineered lag deltas (`*_change_5d`,
  `*_change_15d`, `*_change_30d`, `*_change_60d`) plus
  `days_since_last_burn`.
- **Splits**: model code splits by year (`derived_year`) so we do not
  evaluate on dates that overlap training.

## How to safely add a new feature

1. **Decide the layer.** Raw source columns belong in bronze/silver. Anything
   derived from existing silver columns (lag, ratio, rolling stat) belongs
   in gold.
2. **Preserve the silver grain.** Any new join into silver must be unique on
   `(date, latitude, longitude)`. If it is not, aggregate first.
3. **Edit gold engineering only when needed.** Add new derived columns to
   `data_pipelines/05_engineer_gold_features.sql` inside the existing CTEs.
   Use the same `PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING)
   ORDER BY window_start_date` window so lag semantics stay consistent.
4. **No leakage.** A feature available at prediction time `t` may only use
   inputs observed at times `<= t`. Anything that peeks past
   `window_start_date` will leak the label.
5. **Re-run the checkpoints below before training.**
6. **Document the feature** in this file or in the trainer's feature list so
   the serving payload can be updated.

## Checkpoints to run before training

Run all four against the relevant project / dataset. The 03 and 04 templates
ship with `PROJECT_ID.DATASET_ID` placeholders — substitute your own.

1. `sql/checkpoints/01_duplicate_check_by_year.sql` — confirm silver is unique
   on (date, lat, lon) for each year you intend to use.
2. `sql/checkpoints/02_deduplicate_2017.sql` — re-run only if you see
   duplicates in the 2017 shard; produces `silver_features_2017_dedup`.
3. `sql/checkpoints/03_null_key_columns.sql` — verify NULL counts on key
   grain columns and on the main feature columns.
4. `sql/checkpoints/04_target_label_distribution.sql` — sanity-check the
   class balance before training.

If any of these come back unexpectedly hot, fix the data before training.
Don't paper over it in the trainer.

## Common risks

- **Leakage.** Anything that uses a value observed *after*
  `window_start_date` to predict the label for that date is a leak. This
  includes "future" rolling stats and joining in event tables that already
  contain the outcome.
- **Duplicate rows.** Break the LAG / rolling features as described above
  and corrupt the label distribution. Always start from checkpoint 01.
- **NULLs in key columns.** Drop the row from windowed computations entirely
  and silently shrink the dataset. Run checkpoint 03 before assuming row
  counts are stable.
- **Class imbalance.** Burn events are rare. Use PR-AUC, calibrated
  thresholds, and class weighting; do not rely on accuracy. Checkpoint 04
  surfaces this explicitly.
- **Spatial autocorrelation.** Neighboring cells share weather, terrain, and
  fire history, so a naive random split will give optimistically biased
  metrics. Prefer year-based or region-based splits and be careful about
  k-fold CV that mixes adjacent cells across folds.
