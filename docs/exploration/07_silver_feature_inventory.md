# Exploration 07 — Silver feature inventory

## Purpose

Before we propose any new feature SQL, we need a clear picture of what is
already in the Silver layer: which columns exist, what families they
belong to, what the row grain looks like, and how much fire-history
coverage we have. The companion file
`sql/exploration/07_silver_feature_inventory.sql` is a read-only BigQuery
script that answers those questions section by section.

This file is **read-only**. It does not issue `CREATE`, `REPLACE`,
`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, or `TRUNCATE` statements
anywhere — it is safe to copy-paste into the BigQuery console.

## Why we inspect Silver before writing new features

- New feature candidates depend on which inputs already exist. Writing
  feature SQL against assumed column names is the fastest way to break a
  notebook in front of the class.
- Some features (rolling means, lag deltas, seasonality decompositions)
  only make sense if Silver has enough rows per cell. Section 5 of the
  SQL tells us whether that bar is met before we invest in the SQL.
- Fire-history coverage drives whether fire-history features are worth
  building or whether class imbalance and missing labels will dominate.
  Section 6 quantifies that.
- Verifying the row grain (one row per `DATE(timestamp), latitude,
  longitude`) is a precondition for every windowed / lag feature. Step
  05 of the production pipeline already assumes this; we re-verify it
  here on the merged all-years table.

## How this relates to Bronze / Silver / Gold

- **Bronze** is raw ingestion: GEE exports, CalFire FRAP, GRIDMET pulls.
  No features yet.
- **Silver** (this layer) is cleaned, conformed, joined per cell-window
  rows. Concretely the merged table is
  `${GCP_PROJECT_ID}.openfire_features.silver_features_all_years`
  produced by `data_pipelines/04_merge_silver_years.sql`.
- **Gold** is model-ready: the target (`burned_in_next_15_days`),
  windowed / lag features, and `days_since_last_burn` are added by
  `data_pipelines/05_engineer_gold_features.sql`.

We are not modifying any pipeline code. Instead we use this inventory to
decide what new feature candidates to prototype in subsequent exploration
files (e.g. `sql/exploration/06_fire_history_feature_candidates.sql`).
Once a candidate proves valuable, the team can lift it into step 05.

## How to interpret each section

### Section 1 — Schema inventory
Lists every column on `silver_features_all_years` with its data type and
ordinal position. Use this as the source of truth for column names when
writing new features. If a column you expected does not appear, do not
guess — open Section 2 and search by family.

### Section 2 — Feature-family column search
Same `INFORMATION_SCHEMA.COLUMNS` source, but tagged into 11 feature
families (vegetation indices, Sentinel bands, weather components, terrain,
fire history, time, location, other). Read this top-to-bottom to see
what is and is not present:

- Anything bucketed as `99_other` is unrecognized — either a new column
  the team added or a name that doesn't match the regexes. Worth a glance.
- An empty family (e.g. nothing under `06_weather_wind`) means we cannot
  build features in that family without first extending Silver.

### Section 3 — Row counts and date range
Total rows, observed date range, distinct dates, and an approximate
distinct-cell count. The approximate cell count uses
`APPROX_COUNT_DISTINCT` so the query is cheap on a multi-tens-of-millions
row table. Cross-check against `data_pipelines/README.md` (~38.9M rows,
2017‑09‑01 → 2025‑01‑01).

### Section 4 — Grain duplicate check
Returns `(window_start_date, latitude, longitude)` groups that appear
more than once. **Empty result = good.** Any row returned means two or
more silver records share the same cell-date — which would silently
corrupt LAG / rolling features and inflate the target rate. If this
section returns rows, stop and investigate before adding new features.

### Section 5 — Dates-per-cell distribution
Tells us how many distinct windows we have per `(latitude, longitude)`
cell. Look at the deciles output:

- If the median is ≥ 12, 60-day lag features (which the production
  pipeline already builds) and longer rolling features are feasible.
- If the median is in the 6–11 range, prefer 30-day rolling stats and
  drop the 60-day window for new features.
- If `min_dates_per_cell` is unexpectedly low (e.g. 1), some cells are
  effectively static and need to be filtered out before training so they
  don't dominate windowed features with NULLs.

### Section 6 — Fire-history coverage
Counts how many silver rows have a non-NULL, non-`'None'`
`historical_fire_dates`, and what percent of rows that represents.
California fire history is sparse, so a low absolute percentage is
expected. What matters is:

- The percentage should be stable across runs. A sudden drop usually
  means the `fire_history_by_cell` join in step 04 broke or was rebuilt
  against the wrong FRAP vintage.
- A near-zero percentage means fire-history features will not work and
  you should focus on weather / vegetation / terrain features instead.

### Section 7 — Sample of rows with fire history
20 rows containing fire history, ordered for stability. Use this to
sanity-check the shape of `historical_fire_dates` (comma-delimited date
strings) before writing parsing logic, and to spot obvious format
issues (e.g. mixed delimiters, unexpected literals like `'None,2018'`).

### Section 8 — Comment-only feature plan
A SQL comment block listing candidate feature families and the
leakage rule. No SQL is executed here; the section is intentionally
read-only documentation.

## Observed BigQuery Results

Snapshot from running the SQL on
`${GCP_PROJECT_ID}.openfire_features.silver_features_all_years`.
Re-run periodically; numbers will drift as new years are appended.

### Section 3 — Row counts and date range

| Metric | Value |
| --- | --- |
| Total rows | ~40,003,383 |
| Min `DATE(timestamp)` | 2017-09-01 |
| Max `DATE(timestamp)` | 2025-12-28 |
| Distinct dates | 609 |
| Approx distinct cells (`latitude` + `longitude`) | ~94,879 |

### Section 4 — Grain duplicate check

No rows returned. The table preserves the expected silver grain of
**one row per `(DATE(timestamp), latitude, longitude)`**. Windowed and
LAG-based features can be trusted on this table.

### Section 5 — Dates-per-cell distribution

| Metric | Value |
| --- | --- |
| Min dates per cell | 236 |
| Max dates per cell | 609 |
| Average dates per cell | ~424 |

Every cell has well above the 12-window bar required for the production
60-day lag features, so longer rolling stats and multi-window trend
features are feasible.

### Section 6 — Fire-history coverage

| Metric | Value |
| --- | --- |
| Rows with `historical_fire_dates` populated | ~12,400,000 |
| Share of total rows | ~31% |

A non-trivial fraction of the dataset has fire history attached, so
fire-history features are worth pursuing rather than being dominated by
missingness.

### Sections 1 + 2 — Available column families

The schema covers all of the families we expected:

- **Sentinel-2 bands**: `B2`, `B3`, `B4`, `B8`, `B11`, `B12`.
- **Vegetation / moisture indices**: `mean_NDVI`, `mean_NDWI`,
  `mean_EVI`, `mean_NBR`.
- **GRIDMET weather**: `gridmet_temp_max`, `gridmet_humidity_min`,
  `gridmet_precip_sum`, `gridmet_wind_max`.
- **Terrain**: `mean_elevation`, `mean_slope`, `mean_cos_aspect`,
  `mean_sin_aspect`.
- **Fire history**: `historical_fire_dates` (comma-delimited string).

### Important fire-date interpretation

- `historical_fire_dates` may include fire dates **after** a row's
  `timestamp`. The column is per-cell, not per-cell-per-time.
- This was confirmed by rows such as `timestamp = 2017-09-01` containing
  `historical_fire_dates` entries like `2021-11-11`.
- About **7.2%** of parsed fire-date references are future relative to
  the row's `timestamp` (`pct_future_fire_date_rows` ≈ 7.218% in
  `sql/exploration/07_5_fire_date_temporal_check.sql`).
- This is **acceptable for label construction** — the production target
  `burned_in_next_15_days` deliberately looks forward 1–15 days.
- It must **not** be used directly as a model feature. Feeding raw
  `historical_fire_dates` (or all-time fire counts derived from it
  without a temporal filter) to a model would leak future fires into
  training.
- Feature engineering must filter with `fire_date < window_start_date`
  before counting, aggregating, or interacting with other columns. See
  `docs/exploration/07_5_fire_date_temporal_check.md` for the full
  finding and recommended usage patterns.

## Interpretation

What the observed numbers above tell us about the next round of feature
work:

- **Temporal trend features are feasible.** With an average of ~424
  distinct dates per cell and a minimum of 236, every cell has enough
  history to support multi-window rolling means, rolling z-scores, and
  longer lag deltas than the production 60-day window. We are not
  bottlenecked by sparse cells.
- **Vegetation × weather interaction features are feasible.** Both the
  vegetation/moisture indices (`mean_NDVI`, `mean_NDWI`, `mean_EVI`,
  `mean_NBR`) and the GRIDMET weather aggregates (`gridmet_temp_max`,
  `gridmet_humidity_min`, `gridmet_precip_sum`, `gridmet_wind_max`)
  exist on every silver row, so we can build compound risk features
  like `dryness`, `heat_load`, and `wind_dryness` directly in gold
  without first extending Silver.
- **Fire-history interaction features are feasible.** About 31% of rows
  carry a populated `historical_fire_dates` field. That is more than
  enough signal to justify the fire-history candidates already drafted
  in `sql/exploration/06_fire_history_feature_candidates.sql`, plus
  potential interactions like *fire-history × dry season* or
  *fire-history × NDVI anomaly*.
- **Leakage rule (still binding).** Every fire-history-derived feature
  must reference only `fire_date < window_start_date`. The forward
  window (`window_start_date + 1` … `window_start_date + 15`) is
  reserved for the target. This applies to every interaction feature
  that touches `historical_fire_dates` or any future-looking source.

## What results would enable the next feature SQL

- **Section 4 returns zero rows.** Without this we cannot trust any
  windowed feature.
- **Section 5 median ≥ 6 dates per cell.** Below that, temporal trend
  features are not yet feasible at scale.
- **Section 6 shows a non-trivial fire-history coverage** (e.g.
  ≥ 0.1% of rows, even if absolute counts are small). Below that,
  prefer fuel × weather and seasonality features.
- **Section 1/2 confirms the column names** assumed by the next feature
  file (e.g. `mean_NDVI`, `gridmet_temp_max`, etc.). If those names
  differ, update the next file rather than the pipeline.

If all four conditions hold, we can confidently extend
`sql/exploration/06_fire_history_feature_candidates.sql` (and write new
companions for fuel/weather and seasonality) without surprises.

## Candidate feature groups for Random Forest / XGBoost

Tree-based models do not require feature scaling and tolerate missing
data gracefully, so the priority is signal density over numerical
hygiene.

### A. Fire-history features (past-only)
- `prior_fire_count_5yr`, `prior_fire_count_10yr`, `prior_fire_count_all_time`
- `has_prior_fire_history` (boolean)
- `years_since_last_burn`, `months_since_last_burn`
- Density score: `prior_fire_count_10yr / 10.0`

These are typically the strongest non-weather features for wildfire
risk. Trees split well on the integer counts and the boolean.

### B. Vegetation × Weather interaction features
- `dryness = (1 - mean_NDWI) * gridmet_temp_max`
- `heat_load = gridmet_temp_max * (1 - gridmet_humidity_min / 100)`
- `wind_dryness = gridmet_wind_max * (1 - mean_NDWI)`
- `vegetation_anomaly = mean_NDVI - rolling_mean_NDVI_60d`

Capture compounded fire-weather risk that the raw columns do not
expose linearly. Random Forest / XGBoost will reuse these even if the
raw columns are also present.

### C. Seasonal features
- `EXTRACT(MONTH FROM window_start_date)`
- `EXTRACT(DAYOFYEAR FROM window_start_date)`
- `is_fire_season` boolean (June–October for California)
- `sin(2π * doy / 365)`, `cos(2π * doy / 365)` — only if you also plan
  to feed a linear / NN model from the same gold table.

Tree models can split on raw month directly; sin/cos forms are mostly
useful for non-tree consumers.

### D. Temporal trend features
Predicated on Section 5 having sufficient dates-per-cell.

- Rolling mean / std of NDVI, NDWI, temperature, precipitation over
  3, 6, 12 prior windows.
- `precip_anomaly_30d = precip_30d_sum - rolling_mean_precip_30d`
- `vegetation_drying_rate = (mean_NDVI_now - mean_NDVI_30d_ago) / 30`

These extend the LAG deltas already produced in step 05 and are the
features most prone to leakage if the past-only filter is dropped.

### Leakage rule (applies to every group above)

Every FEATURE may only reference inputs observed strictly *before*
`window_start_date` (or fire dates strictly less than it). Forward
windows are reserved for the target. When in doubt, mirror the
production target convention: features stop at `window_start_date - 1`,
target looks at `[window_start_date + 1, window_start_date + 15]`.
