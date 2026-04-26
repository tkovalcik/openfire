# OpenFire Training Data Pipeline

This pipeline produces the ML-ready `gold_features` table used to train the
wildfire-risk model. It glues together CalFire fire perimeters, Sentinel-2
imagery, GRIDMET weather, and USGS topography into one row per
`(latitude, longitude, 5-day window)` over four California counties.

**Output:** `msds603-mlops-project.openfire_features.gold_features` —
~38.9M rows × 38 columns, also exported to
`gs://openfire/openfire/datasets/gold/gold_features_*.parquet` as 51 Parquet
shards.

**Coverage:** Kern, Los Angeles, San Luis Obispo, Santa Barbara counties.
Time range 2017‑09‑01 → 2025‑01‑01 at 5-day cadence on a 1 km grid.

**Target column:** `burned_in_next_15_days` (boolean) — true if any CalFire
perimeter for that cell ignited within 1–15 days after the window start.
Class imbalance is severe (~0.044% positive); plan for `scale_pos_weight`
or focal loss in training.

---

## How the data flows

```
                       ┌──────────────────────────────────────┐
                       │  CalFire FRAP (statewide perimeters) │
                       │   downloaded from Azure CDN          │
                       └──────────────┬───────────────────────┘
                                      │ 01_stage_calfire_data.py
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  GCS bronze + silver shapefile       │
                       │  gs://openfire/.../cal-frap-...      │
                       └──────────────┬───────────────────────┘
                                      │ 02_ingest_calfire_gee.py
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  GEE asset: calfire_frap_perimeters_*│
                       └──────────────┬───────────────────────┘
                                      │ 03b_extract_gee_land_weather.py
                                      │ (S2 + GRIDMET + topo per 5d window,
                                      │  one BigQuery table per year)
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  silver_features_YYYY  (BQ, per yr)  │
                       └──────────────┬───────────────────────┘
                                      │ 04_merge_silver_years.sql
                                      │ (merge + dedup + LEFT JOIN labels)
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  silver_features_all_years  (BQ)     │
                       └──────────────┬───────────────────────┘
                                      │ 05_engineer_gold_features.sql
                                      │ (target + lag deltas +
                                      │  days_since_last_burn)
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  gold_features  (BQ)                 │
                       └──────────────┬───────────────────────┘
                                      │ 06_export_gold_to_gcs.sql
                                      ▼
                       ┌──────────────────────────────────────┐
                       │  gs://openfire/.../gold/*.parquet    │
                       └──────────────────────────────────────┘
```

**Out-of-band dependency:** `fire_history_by_cell` — a per-cell roll-up of
historical fire dates used by step 04 to attach labels. It's currently
maintained by a teammate outside this pipeline. If regenerating from
scratch, ensure that table exists before running step 04.

---

## File-by-file guide

Steps live in [`data_pipelines/`](.) (numbered prefix = run order). The
orchestrator at [`src/pipelines/run_training_pipeline.py`](../src/pipelines/run_training_pipeline.py)
runs steps 04–06 by default; 01–03b are pre-staged ingestion that you
typically only re-run when adding a new year of data.

### `01_stage_calfire_data.py`
**Stages CalFire FRAP fire perimeters into GCS.**

- Downloads the CalFire FRAP geodatabase (zipped) from CalFire's Azure CDN.
- Uploads the raw zip and unzipped GDB to `gs://openfire/.../bronze/`.
- Converts the perimeter layer (whichever layer matches `firep`) to
  EPSG:4326 shapefiles and uploads to `gs://openfire/.../silver/`.
- Prints the GCS URIs you need for step 02.

Run when: a new annual CalFire FRAP release is published (typically
once a year — they version the file like `fire24`, `fire25`, …).

### `02_ingest_calfire_gee.py`
**Loads the staged shapefile into a GEE FeatureCollection asset.**

- Scans the silver GCS prefix for the 4 required shapefile sidecar files
  (`.shp`, `.shx`, `.dbf`, `.prj`) matching `TARGET_FILE_IDENTIFIER`.
- Submits a `startTableIngestion` task that creates the EE asset
  `calfire_frap_perimeters_<ASSET_YEAR_SUFFIX>`.
- The task is async — monitor it in the GEE Code Editor's Tasks tab.

Run when: you've staged a new FRAP release in step 01. **Edit
`TARGET_FILE_IDENTIFIER` and `ASSET_YEAR_SUFFIX` near the top before
running.**

### `03_extract_gee_features.js`
**Legacy: GEE Code Editor JavaScript variant of feature extraction.**

This is the original pre‑Python‑API script. It produces the same
features as `03b` plus the unused `neighborhood_*_10km` columns.
Kept for reference / traceability; **`03b` is the canonical path**.
Don't run both — they write to different tables (`*_gee_import`).

### `03b_extract_gee_land_weather.py`
**Extracts Sentinel-2 + GRIDMET + topography into per-year BQ tables.**

- Builds the 1 km covering grid for the 4-county AOI (geometry centroids
  used as join keys downstream).
- For every 5-day window in `[RUN_START, RUN_END]` (snapped to a fixed
  global epoch of 2017-09-01), it:
  - Pulls Sentinel-2 SR Harmonized images, masks clouds via the SCL band,
    computes NDVI/NDWI/NBR/EVI, takes the median across the window.
  - Pulls GRIDMET, takes max temp / min humidity / sum precip / max wind.
  - Adds static elevation/slope/cos(aspect)/sin(aspect) from USGS 3DEP.
  - Reduces to per-cell means (`reduceRegions`) at 1 km scale.
  - Submits a `Export.table.toBigQuery` task that **appends** to
    `silver_features_<year>`.
- Pre-creates each yearly BQ table with an explicit schema so GEE doesn't
  guess types (it would otherwise serialize masked nulls as STRING).
- Pixels masked by clouds are filled with `-9999` and converted back to
  `NULL` downstream via `NULLIF`.

Run when: you want to backfill or extend a year of data. **Edit
`RUN_START` and `RUN_END` for the window you want, then walk through
the `# %%` cells in order** in VS Code or JupyterLab. Cell 8 monitors
task progress; cell 9 retries failures.

Tasks take hours and run in batches of ~50–100 windows; expect to
re-run cell 9 a few times to mop up sporadic GEE failures.

### `04_merge_silver_years.sql`
**Merges yearly silver tables into one and joins fire labels.**

- Wildcard-reads `silver_features_*` (skipping experimental `*_dedup`
  tables), with `EXCEPT(\`system:index\`)` because BigQuery rejects
  flexible column names in wildcard reads.
- Deduplicates on `(CAST(latitude AS STRING), CAST(longitude AS STRING),
  timestamp)` via `QUALIFY ROW_NUMBER()`. Cast is required because BQ
  refuses FLOAT64 partition keys.
- LEFT JOINs the per-cell historical fire dates from
  `fire_history_by_cell` so labels live alongside features.
- Writes `silver_features_all_years`.

Run when: a new yearly silver table has landed, or `fire_history_by_cell`
has been updated.

### `05_engineer_gold_features.sql`
**Builds target, lag deltas, and days-since-last-burn.**

For each `(lat, lon, window_start_date)`:
- `burned_in_next_15_days`: TRUE if any historical fire date is in
  `[start+1d, start+15d]`.
- `days_since_last_burn`: days between window start and the most recent
  fire date strictly before it. Filled with `9999` if the cell never burned.
- 5d/15d/30d/60d **lag deltas** for NDVI, NDWI, max temp (60d only for
  precip starts at 15d). Computed as `value - LAG(value, k)` over the
  cell's time series, ordered by `window_start_date`.
- Filters out the warm-up window where the 60d lag is NULL.
- Final SELECT drops the empty `neighborhood_*_10km` columns from the
  legacy `03_extract_gee_features.js` path.

Output: `gold_features`, the table the model trains on.

### `06_export_gold_to_gcs.sql`
**Exports `gold_features` to GCS as Parquet shards.**

`EXPORT DATA OPTIONS(...)` writes
`gs://openfire/openfire/datasets/gold/gold_features_*.parquet`
(SNAPPY-compressed, ~51 shards). Overwrites the prefix every run, so
training pipelines always read a consistent snapshot.

### Orchestrator: `src/pipelines/run_training_pipeline.py`
**Runs steps 04 → 06 end-to-end with verification.**

- Each step is a `Step` dataclass that knows its SQL file and an
  `expected_table`; after the SQL runs, the orchestrator confirms the
  table exists and logs its row count plus bytes billed.
- Steps 01–03b are registered but skipped by default (they're already-
  materialized GCS / GEE assets); pass `--include-gee-steps` to force,
  but they are no-ops at the moment because the orchestrator doesn't
  shell out to the Python ingestion scripts.

### Helper: `src/pipelines/bq_utils.py`
Tiny module that wraps `google-cloud-bigquery` so each orchestrator
step is a single readable line. Reused by the (future) inference
pipeline.

---

## Output schema (gold_features, 38 columns)

| Column | Type | Notes |
| --- | --- | --- |
| `window_start_date` | DATE | Start of the 5-day window |
| `latitude`, `longitude` | FLOAT64 | 1 km grid cell centroid (EPSG:4326) |
| `burned_in_next_15_days` | BOOL | **Target.** Burned within 1–15 days after window start |
| `days_since_last_burn` | INT64 | 9999 if never burned |
| `ndvi_change_5d/15d/30d/60d` | FLOAT64 | NDVI lag deltas |
| `ndwi_change_5d/15d/30d/60d` | FLOAT64 | NDWI lag deltas |
| `temp_change_5d/15d/30d/60d` | FLOAT64 | Max-temp lag deltas |
| `precip_change_15d/30d/60d` | FLOAT64 | Precip-sum lag deltas |
| `mean_elevation`, `mean_slope` | FLOAT64 | USGS 3DEP, static |
| `mean_cos_aspect`, `mean_sin_aspect` | FLOAT64 | Aspect decomposed (avoids the 0°/360° wrap) |
| `B2`, `B3`, `B4`, `B8`, `B11`, `B12` | FLOAT64 | Sentinel-2 SR bands, scaled |
| `mean_NDVI`, `mean_EVI`, `mean_NDWI`, `mean_NBR` | FLOAT64 | S2 indices |
| `gridmet_temp_max`, `gridmet_humidity_min`, `gridmet_precip_sum`, `gridmet_wind_max` | FLOAT64 | GRIDMET weather aggregates |

---

## How to run

Prereqs: GCP project with BigQuery + GCS access, ADC configured
(`gcloud auth application-default login`), and the openfire conda env.

```bash
# Default: run steps 04 -> 06
python -m src.pipelines.run_training_pipeline

# Re-run only step 05 (e.g., after editing the gold SQL)
python -m src.pipelines.run_training_pipeline --from-step 05 --to-step 05

# Dry run — print the plan without executing
python -m src.pipelines.run_training_pipeline --dry-run
```

`--from-step` / `--to-step` accept either the prefix (`04`) or the
full name (`04_merge_silver`).

---

## Adding a new year of data

1. **Stage CalFire** if you need a newer FRAP release:
   - Edit nothing in `01_stage_calfire_data.py` (it always grabs the
     latest from CalFire's CDN).
   - Run `python data_pipelines/01_stage_calfire_data.py`.
2. **Ingest into GEE:** edit `TARGET_FILE_IDENTIFIER` (e.g. `"fire25"`)
   and `ASSET_YEAR_SUFFIX` (e.g. `"2025"`) in
   `02_ingest_calfire_gee.py`, then run it. Wait for the GEE task to
   finish (Code Editor → Tasks).
3. **Update `fire_history_by_cell`** to include the new year's
   perimeters. (This is currently outside this pipeline — coordinate
   with whoever owns it.)
4. **Extract features:** edit `RUN_START` / `RUN_END` in
   `03b_extract_gee_land_weather.py` to cover the new year, then walk
   through cells 1 → 8 in order. Use cell 9 to retry any failures.
5. **Rebuild gold:**
   ```bash
   python -m src.pipelines.run_training_pipeline
   ```
   Step 04 picks the new `silver_features_<year>` table up automatically
   via the `silver_features_*` wildcard. Step 05 rebuilds gold from
   silver. Step 06 re-exports the Parquet shards.

---

## Gotchas worth knowing

- **`PARTITION BY` on FLOAT64 is rejected** by BigQuery — that's why
  every window function in 04 and 05 wraps lat/lon in
  `CAST(... AS STRING)`. Don't undo this.
- **Wildcard reads choke on flexible column names.** GEE auto-injects
  `system:index` (with a colon) into every export. Step 04 uses
  `SELECT * EXCEPT(\`system:index\`)` to drop it before merging.
- **Masked S2 pixels become `-9999`**, not NULL. The GEE export uses a
  float sentinel because `ee.Reducer.mean()` on an untyped null
  serializes to STRING and breaks the BQ schema. Convert back with
  `NULLIF(col, -9999)` if you need it.
- **Dedup tiebreaker is `mean_NDVI`** in step 04 (purely for
  determinism; rows for the same `(lat, lon, timestamp)` are
  byte-identical in practice). It used to use `system:index` but that
  column had to be dropped — see above.
- **The 60d lag warm-up is filtered out** in step 05
  (`WHERE ndvi_change_60d IS NOT NULL`). Expect the first ~12 windows
  per cell to be missing from gold.
- **`days_since_last_burn` defaults to 9999** for cells with no
  recorded prior burn. The model treats this as a magic value; don't
  let downstream feature scaling normalize it.
- **GEE export jobs are flaky.** Expect 1–5% of `03b` window tasks to
  fail with transient errors; cell 9 retries them. For a fresh year
  budget several hours of wall-clock time and a few retry passes.
