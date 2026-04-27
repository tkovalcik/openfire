# Exploration 06 — Fire history feature candidates

## Purpose

Prototype a small set of past-only fire-history features on top of the
merged silver table, so the team can decide which (if any) are worth
promoting into the production gold-feature pipeline
(`data_pipelines/05_engineer_gold_features.sql`).

This file is **read-only exploration SQL**. It does **not** create, replace,
insert into, update, delete from, drop, alter, or truncate any table. It is
safe to copy-paste into the BigQuery console and run without coordinating
with whoever owns the pipeline.

If a candidate looks useful after review, the next step is to lift the
relevant CTE/expression into `05_engineer_gold_features.sql`. Until that
happens, nothing here affects the model.

## Input table

```
msds603-mlops-project.openfire_features.silver_features_all_years
```

Required columns: `timestamp`, `latitude`, `longitude`, `historical_fire_dates`.

Optional pass-through columns referenced in the final `SELECT`:
`mean_NDVI`, `mean_NDWI`, `mean_EVI`, `mean_NBR`, `temperature_2m`,
`precipitation`, `relative_humidity_2m`, `wind_speed_10m`. If your silver
schema uses different names (e.g. `gridmet_temp_max` instead of
`temperature_2m`), edit those column references in the SQL file and the
final `SELECT`.

## Output

One row per `(timestamp, latitude, longitude)` — the same grain as the
input silver row. The query intentionally **does not** fan rows out; the
fire-date `UNNEST` happens inside an inner CTE that is re-aggregated back
to per-row before joining.

### Feature candidate columns

| Column | Type | Description |
| --- | --- | --- |
| `prior_fire_count_all_time` | INT64 | Number of historical fires for this cell strictly before `window_start_date`. |
| `prior_fire_count_5yr` | INT64 | Same, restricted to the last 5 years. |
| `prior_fire_count_10yr` | INT64 | Same, restricted to the last 10 years. |
| `has_prior_fire_history` | BOOL | `prior_fire_count_all_time > 0`. |
| `days_since_last_burn_candidate` | INT64 | Days between window start and the most recent prior burn (NULL if never burned before). |
| `years_since_last_burn_candidate` | FLOAT64 | Same metric in years (`/365.25`). |
| `fire_history_density_score` | FLOAT64 | `prior_fire_count_10yr / 10.0` — fires-per-year over the last decade. |

### Target column (forward-looking, for comparison only)

| Column | Type | Description |
| --- | --- | --- |
| `burned_in_next_15_days_candidate` | BOOL | TRUE if any fire date falls in `[window_start_date + 1 day, window_start_date + 15 days]`. Mirrors the production `burned_in_next_15_days` definition. |

### Sanity-check helper columns

| Column | Type | Description |
| --- | --- | --- |
| `has_any_historical_fire_dates` | BOOL | `historical_fire_dates IS NOT NULL AND != 'None'`. |
| `parsed_fire_date_count` | INT64 | How many fire-date strings parsed cleanly via `SAFE_CAST`. |
| `future_fire_date_count` | INT64 | Parsed dates with `fire_date >= window_start_date`. Only the *target* should depend on these. |
| `past_fire_date_count` | INT64 | Parsed dates with `fire_date < window_start_date`. This is what every feature aggregates over. |

## Why every feature candidate is past-only

Each feature aggregation in the SQL is gated on `fire_date < window_start_date`.
This is the leakage rule: a feature available at prediction time `t` may
only see fire activity observed strictly before `t`. The forward-looking
window (`+1d ... +15d`) is reserved exclusively for the target column.

Two consequences worth knowing:

- A fire on `window_start_date` itself counts as **future** for feature
  purposes. The production target also excludes day 0
  (`BETWEEN start+1d AND start+15d`), so it would not contribute as a
  positive label either. Anyone changing this convention should change it
  in both places.
- Cells with no recorded prior burn return `NULL` for
  `days_since_last_burn_candidate` / `years_since_last_burn_candidate`.
  When promoting these to gold, fill with the same magic value step 05
  already uses (`COALESCE(..., 9999)` for days), or one-hot encode via
  `has_prior_fire_history` instead.

## Which candidates may be useful for tree-based models

XGBoost / Random Forest don't need scaled or transformed inputs, but they
do benefit from features with strong rank-correlation to the target.

- **Strong priors**: `prior_fire_count_5yr`, `prior_fire_count_10yr`,
  `has_prior_fire_history`. Cells that have burned recently tend to keep
  burning.
- **Recency**: `days_since_last_burn_candidate` (already validated by
  step 05 as `days_since_last_burn`). Often non-monotonic — vegetation
  recovery introduces a sweet spot around 5–15 years post-burn.
- **Density**: `fire_history_density_score` is a normalized version of
  `prior_fire_count_10yr` and is mostly redundant with it for a tree
  model; keep one or the other rather than both, to avoid wasting splits.
- **Boolean indicator**: `has_prior_fire_history` is cheap and often a
  strong split feature on its own; worth keeping even alongside the
  numeric counts.

For linear / NN models, prefer the bounded versions (`density_score`,
`years_since_last_burn_candidate`) and drop the raw counts.

## How to interpret the sanity-check columns

After running the query, eyeball these:

- `parsed_fire_date_count` should be ≥ 0 for every row, and equal to
  `past_fire_date_count + future_fire_date_count`. A consistent gap
  means malformed date strings — investigate the upstream
  `historical_fire_dates` build.
- `future_fire_date_count` is **only** allowed to feed the target. If a
  feature you add starts depending on it, that feature is leaking.
- `past_fire_date_count` of 0 with `has_any_historical_fire_dates = TRUE`
  is fine: it means every recorded fire is at or after `window_start_date`
  (e.g. early-window rows for a frequently-burning cell).
- `has_any_historical_fire_dates = FALSE` should give counts of 0 across
  the board.

## What would indicate a problem

- **Row count > input row count.** The query is engineered to preserve
  row grain. If `COUNT(*)` exceeds the silver-table row count for the
  same time/cell range, the join in `FeatureCandidates` is fanning out —
  stop and check whether `silver_features_all_years` has duplicates on
  `(timestamp, latitude, longitude)`.
- **`future_fire_date_count > 0` driving any *feature* column.** Every
  feature is wrapped in `fire_date < window_start_date`; a non-zero
  future count is fine on its own, but if a candidate feature's value
  changes when only `future_fire_date_count` is non-zero, that feature
  has leakage.
- **`prior_fire_count_all_time` consistently zero across the dataset.**
  Either the join key in step 04 broke (no `historical_fire_dates`
  attached) or `fire_history_by_cell` was rebuilt against the wrong
  FRAP vintage.
- **Negative `days_since_last_burn_candidate`.** Should be impossible
  given the `<` filter; if it shows up, the upstream date string
  contains entries that were not properly filtered to past-only.
