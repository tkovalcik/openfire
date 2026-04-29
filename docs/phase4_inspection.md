# Phase 4 — Step 0 Repo Inspection

Read-only audit of existing modules, conventions, and gotchas before any
inference code is written. Findings here drive the implementation choices in
Steps 1–8.

Authored 2026-04-28 from the state of `dev` at commit `639def4`.

---

## 1. Orchestrator pattern (mirror this)

`src/pipelines/run_training_pipeline.py` is the template.

- argparse with `--from-step`, `--to-step`, `--dry-run`, `--project`, plus
  per-pipeline domain flags (training has `--include-gee-steps`).
- A frozen `Step` dataclass: `name` ("04_merge_silver"), `description`,
  `sql_file`, `expected_table`, `default_skip`, `skip_reason`. `prefix`
  property strips the leading "NN_".
- Steps are a module-level `STEPS: list[Step]` with the canonical ordering.
- `resolve_step_index(identifier)` accepts prefix ("04") or full name.
- `run_step(step, *, client, dry_run)` executes the SQL and verifies
  `table_exists` + logs `get_row_count` from `bq_utils`.
- `get_client(project)` is called only after the dry-run early-return — so
  dry-run never opens a BQ session. **The orchestrator dry-run test
  (`test_capstone_pipeline.py::test_orchestrator_dry_run`) patches
  `get_client` and asserts it is never called; mirror this in the inference
  orchestrator's tests.**
- Logging: stdlib `logging.basicConfig` at INFO with a fixed format;
  `LOGGER = logging.getLogger(__name__)` per module.

**Implication:** the inference orchestrator (`src/pipelines/run_inference_pipeline.py`)
keeps the same shape — same flag names where they apply, same dry-run
contract, same Step dataclass-or-equivalent for the per-window pipeline.

## 2. GEE extractor — `data_pipelines/03b_extract_gee_land_weather.py`

Currently a cell-style Jupyter script, not import-safe.

**Hardcoded module-level constants:**
- `EPOCH_START = date(2017, 9, 1)` — never changes.
- `RUN_START = date(2018, 1, 1)`, `RUN_END = date(2018, 12, 31)` — must
  become CLI args. (Local working copy has these bumped to 2020 — exactly
  the friction Step 1 removes.)
- `STEP_DAYS = 5`, `GRID_SCALE = 1000`, `REDUCE_SCALE = 1000`,
  `CLOUD_THRESH = 20`. `STEP_DAYS` and `EPOCH_START` are training
  invariants and should remain module constants. `GRID_SCALE`/`REDUCE_SCALE`
  are tied to the model's resolution (1 km for `openfire-gold v2`); leave
  as constants for now and revisit when stepping down to 250 m.
- `GCP_PROJECT`, `BQ_DATASET`, `BQ_TABLE_PFX = 'silver_features'`. Already
  matches the orchestrator's constants.
- `AOI_COUNTIES = ['Kern', 'Los Angeles', 'San Luis Obispo', 'Santa Barbara']`
  — see AOI section below.

**Side effects at import time:**
- Top-level `ee.Authenticate()` and `ee.Initialize(project=...)`. Fine in
  a notebook, fatal if imported. Refactor: gate behind `if __name__ == "__main__"`
  or move into a `main()` function.
- Top-level `ee.batch.Task.list()` cancellation cell — destructive in an
  inference context (it would cancel a running training extract). Refactor
  this into an opt-in `--cancel-existing` flag, default off.

**BQ table creation:** explicit schema via `bigquery.Client.create_table`
with `silver_features_<year>` table-per-year, schema:
`geo (GEOGRAPHY)`, `latitude/longitude (FLOAT)`, `timestamp (STRING ISO date)`,
`system:index (STRING — flexible-name column auto-injected by GEE)`, plus
18 FLOAT feature columns. **Existing tables are left untouched** — the
script is already idempotent at table-level for inference's purposes. New
years use `CREATE TABLE IF NOT EXISTS` semantics by checking
`bq_client.get_table` and falling through to `create_table` on `NotFound`.

**Composite window:** `[t0, t0 + 5d)` for Sentinel-2 (`S2_SR_HARMONIZED`,
`<20% CLOUDY_PIXEL_PERCENTAGE`, SCL pixel mask) + GRIDMET. Static topo
(`USGS/3DEP/10m`) computed once. Masked pixels exported as `-9999` so
`reduceRegions` always returns FLOAT.

## 3. Silver / gold SQL — what's safe to reuse and what isn't

`data_pipelines/04_merge_silver_years.sql`:
- `CREATE OR REPLACE TABLE silver_features_all_years` — **destructive**.
- Wildcards `silver_features_*` (excludes `*_dedup`), `EXCEPT(\`system:index\`)`.
- Dedupes on `(CAST(latitude AS STRING), CAST(longitude AS STRING), timestamp)`,
  `ORDER BY mean_NDVI` tiebreaker.
- LEFT JOINs `fire_history_by_cell` USING `(latitude, longitude)`.

`data_pipelines/05_engineer_gold_features.sql`:
- `CREATE OR REPLACE TABLE gold_features` — **destructive**. Cannot be run
  as-is during inference (Step 4 must adapt this).
- Materializes `window_start_date = CAST(timestamp AS DATE)` from silver.
- Label `burned_in_next_15_days`: TRUE iff any `historical_fire_dates`
  entry is in `[window_start_date + 1d, window_start_date + 15d]`
  (inclusive of both endpoints, **exclusive of `window_start_date`
  itself**).
- `days_since_last_burn`: `DATE_DIFF(window_start_date, MAX(fire_date), DAY)`
  for fires strictly before `window_start_date`. `COALESCE(..., 9999)`.
- Lags: `LAG(col, N) OVER (PARTITION BY (lat,lon) ORDER BY window_start_date)`
  with `N ∈ {1, 3, 6, 12}` corresponding to `{5d, 15d, 30d, 60d}` (the SQL
  trusts the 5-day grid is intact — there is no `RANGE BETWEEN INTERVAL`
  guard against gaps).
- `WHERE ndvi_change_60d IS NOT NULL` removes the 60-day warmup window per
  cell.
- **Critical for inference:** computing lags for window W requires that
  the same cell's windows at W−5d, W−15d, W−30d, W−60d already exist in
  the silver source. Inference cannot meaningfully run on a single fresh
  window in isolation — the 60d backwards history must be present (and
  during normal steady-state operation it always is, because we extracted
  history through 2024 in training and then the 5-day cadence keeps
  backfilling forward).

## 4. Gold feature semantics & timezone — required disclosure

**`window_start_date` is the start of a 5-day Sentinel-2 / GRIDMET
acquisition window.** Specifically:

| Component | Window relative to `window_start_date = W` |
|---|---|
| Sentinel-2 bands & indices (B2..B12, NDVI, EVI, NDWI, NBR) | `[W, W+5d)` median composite |
| GRIDMET weather (temp_max, humidity_min, precip_sum, wind_max) | `[W, W+5d)` aggregated (max/min/sum/max) |
| Topography (elevation, slope, cos/sin aspect) | static, no window |
| Lag deltas (`*_change_5d` … `*_change_60d`) | `feature[W] − feature[W − Nd]`, computed across consecutive 5-day grid windows for the same `(lat, lon)` cell |
| Label `burned_in_next_15_days` | TRUE iff any historical fire date ∈ `[W+1d, W+15d]` |
| `days_since_last_burn` | `W − max(fire_date < W)`, sentinel `9999` if never burned |

**Timezone:** GEE `ee.Date(string)` interprets bare ISO dates as UTC
midnight. GRIDMET and Sentinel-2 SR_HARMONIZED metadata is UTC. BigQuery
`DATE` is timezone-less. Therefore `window_start_date = 2024-06-15` refers
to the period starting `2024-06-15 00:00 UTC`. **In `latest` mode, "today"
must be evaluated in UTC**, not in local time — otherwise a job firing in
California at 11pm on 2026-04-28 PT would compute `today() = 2026-04-29`
and try to predict windows that haven't fully occurred in UTC yet.

## 5. Primary key for `predictions_history`

No `cell_id` exists anywhere in the silver/gold schema. The de-facto key
in both 04 and 05 is `(latitude, longitude, window_start_date)`, with
lat/lon `CAST AS STRING` for any `PARTITION BY` clause to dodge BQ's
FLOAT64-partition-key restriction (see Phase 1 gotchas).

**Decision (subject to Step 0 confirmation):** primary key for
`predictions_history` is `(latitude, longitude, window_start_date)`. The
table itself uses `PARTITION BY window_start_date` (BQ allows `DATE`
partition keys natively — no STRING cast needed for the partition column,
unlike lat/lon).

## 6. BQ table-level partitioning — current state

I inspected the SQL files only (did not query BQ directly). Neither
`04_merge_silver_years.sql` nor `05_engineer_gold_features.sql` includes
a `PARTITION BY` clause on `CREATE OR REPLACE TABLE`, only `OVER
(PARTITION BY ...)` window functions inside CTEs. **Therefore
`gold_features` and `silver_features_all_years` are not table-partitioned**
— they're flat ~39M-row tables. (To verify, run `bq show --schema
msds603-mlops-project:openfire_features.gold_features` or
`SELECT table_name, partition_id FROM \`region-us\`.INFORMATION_SCHEMA.PARTITIONS WHERE table_name = 'gold_features'`.)

For `predictions_history` we should improve on this: `PARTITION BY
window_start_date, CLUSTER BY latitude, longitude`. Predictions are
naturally read by date for the GeoJSON snapshot writer and by
`(lat, lon)` for the parity test, so the partition + clustering choice
matches the access patterns and bounds query cost.

## 7. AOI — canonical location and discrepancy

**The AOI is defined in exactly one place:**
`data_pipelines/03b_extract_gee_land_weather.py:60`

```python
AOI_COUNTIES = ['Kern', 'Los Angeles', 'San Luis Obispo', 'Santa Barbara']
```

Confirmed source of truth: **4 counties** (Kern, Los Angeles, San Luis
Obispo, Santa Barbara). The training data — `gold_features`, ~39M rows —
was extracted from this set, and inference must match. Predictions outside
this AOI are out-of-distribution.

**Step 1 promotion plan:** lift the constant into `src/pipelines/aoi.py`
(or `src/common/aoi.py`) as a named-AOI dict per the spec recommendation:

```python
AOIS: dict[str, list[str]] = {
    "socal_4_county": ["Kern", "Los Angeles", "San Luis Obispo", "Santa Barbara"],
}
DEFAULT_AOI = "socal_4_county"
```

Both the GEE extractor and the inference orchestrator import from this
single location.

## 8. Reuse map for `src/serving/`

| Module | Reuse strategy |
|---|---|
| `src/serving/model_loader.py` | **Reuse `load_registry_model_bundle` as-is.** Already tested, already pulls `openfire-gold` from MLflow registry by stage, already returns a `LoadedModel` with `feature_columns`, `decision_threshold`, and `model_version`. The inference predict module calls this directly. |
| `src/serving/schemas.py::FEATURE_COLUMNS` | **Reuse as-is** — already mirrors `train.py::FEATURE_COLUMNS` (34 cols). |
| `src/serving/schemas.py::FeatureRow` | **Skip for batch inference.** Pydantic per-row validation has unacceptable overhead for millions of rows per window. The inference predict module should `loaded_model.model.predict_proba(df[FEATURE_COLUMNS])` directly on a DataFrame. Validation happens at the gold-table level (BQ schema enforcement) instead. |
| `src/serving/predict.py::PredictionService` | **Skip.** Request-scoped (FastAPI), single-batch synchronous, row-oriented. Wrong shape for window-batch inference. |
| `src/serving/predict.py::predict_geojson` (logic for shaping a GeoJSON FeatureCollection) | **Promote.** Move the FeatureCollection / Feature / Point geometry shaping to a shared module (`src/pipelines/geojson_writer.py` or `src/common/geojson.py`) and have both serving and inference use it. Inference variant operates on a DataFrame, not on a Pydantic model list — parameterize the shape, not the input type. |
| `src/serving/schemas.py::GeoJSONFeature*` | Reuse the Pydantic models for output validation in tests; the inference writer can build the JSON directly without instantiating Pydantic on every row. |

## 9. GEE data-readiness check — feasibility

The current extractor has **no readiness gate**. If `S2_SR_HARMONIZED`
returns zero scenes for `[t0, t0+5d)`, `build_composite` falls back to
`DUMMY_S2` (all-masked constant) and all S2 features for that window
silently become `-9999`. This is wrong for inference — we'd write
predictions on garbage features.

**Implementation sketch for the readiness gate:**

```python
latest_s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
             .filterBounds(aoi_geom)
             .filterDate(t0.advance(-7, 'day'), t0.advance(5, 'day'))
             .aggregate_max('system:time_start'))
latest_s2_date = ee.Date(latest_s2).format('YYYY-MM-dd').getInfo()
# Compare to t0 + 5d. If the most recent scene is older than t0,
# this window cannot be fully covered yet — skip with a logged warning.
```

**Buffer guess:** Sentinel-2 revisit is 5 days at the equator and ~2–3
days at California latitudes due to overlapping orbits. Surface Reflectance
(L2A) processing latency is typically 1–2 days from acquisition. So a
3-day buffer past `window_start_date + 5d` is a reasonable default; expose
as `--readiness-buffer-days` with default 3 and tune after first
production run.

GRIDMET has its own latency (~1 day for near-real-time, several days for
finalized values); same gating logic applies but on a separate query.

## 10. Open decisions for Step 1 and beyond

- [ ] **Gold table strategy (Step 4)** — recommend Option A
  (`gold_features_inference` separate table, MERGEd by primary key).
  Cleanest separation; existing `gold_features` `CREATE OR REPLACE` stays
  correct for training reruns. Inference table doubles as a serving
  feature lookup.
- [ ] **Lag computation in inference** — implement as a SQL
  `WINDOW`-function CTE that reads the union of (existing inference silver
  rows) ∪ (current-window's freshly extracted silver row) ∪ (~60d of prior
  training silver rows for the cell). Concretely: at inference time, the
  silver-side input for the lag CTE is `silver_features_*` ∪ the
  per-window-appended `silver_features_<year>_gee_import` row, which is
  exactly what the existing 04 SQL already wildcards. No special handling
  needed as long as the inference orchestrator appends to the existing
  per-year silver tables (i.e., do **not** create a separate
  `silver_features_inference` table).
- [ ] **`silver_features_all_years` recreation** — `04_merge_silver_years.sql`
  is `CREATE OR REPLACE`. We can either (a) re-run it as part of inference
  (it's cheap on 39M rows but does briefly take the table offline) or
  (b) skip 04 and have the inference gold-engineering CTE wildcard
  `silver_features_*` directly, bypassing the merged all-years table.
  Default recommendation: **(b)** — fewer moving parts, no shared-table
  contention with future training reruns.

## 11. PR sequencing reaffirmed

The spec's PR sequence (Steps 1, 2, 3+5, 6, 7, 8) is consistent with what
this inspection found. No reordering needed. The only adjustment: **bundle
Step 1 with the AOI consolidation** (creating `src/pipelines/aoi.py` and
re-importing from `03b_extract_gee_land_weather.py`) since they touch the
same file and reviewing them together is cleaner than two back-to-back
PRs that both modify the GEE extractor.
