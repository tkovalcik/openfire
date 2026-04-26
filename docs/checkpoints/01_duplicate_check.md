# Checkpoint 01 — Duplicate key check (by year)

## Purpose

Confirm the silver feature table has **at most one row** per **(CAST(`timestamp` AS DATE), latitude, longitude)** (“cell-date” grain). Duplicates break time-series `LAG` logic and unique-key assumptions downstream (e.g. gold feature engineering).

## Query

- **File:** `sql/checkpoints/01_duplicate_check_by_year.sql`
- Uses the cell-date grouping pattern; point the `FROM` clause at the shard you are validating (naming may include `_gee_import` depending on export path).

## Completed validation (team run)

Results are **year-dependent**:

| Year table | Cell-date duplicate check `(date, lat, lon)` |
|------------|-----------------------------------------------|
| `silver_features_2024` | **Passed** — no duplicate groups returned. |
| `silver_features_2017` | **Failed** — duplicate groups existed. |

For **2017**, one duplicate group was inspected manually: rows matched on the **same raw `timestamp`**, **same `latitude`**, **same `longitude`** — i.e. a **true duplicate-key** issue at `(timestamp, latitude, longitude)`, not only an artifact of casting date.

**Conclusion:** Silver row grain is **not consistently duplicate-free across years**. Do not assume a single year check (e.g. 2024) generalizes to all shards.

## Follow-up

Dedup work for **2017** is captured in **`sql/checkpoints/02_deduplicate_2017.sql`**, producing **`silver_features_2017_dedup`** with dedup key `(timestamp, latitude, longitude)`. Post-dedup verification reported:

- No remaining duplicate groups at `(timestamp, latitude, longitude)` or at cell-date grain.

**Row counts (2017):**

- Original `silver_features_2017`: **1,707,862** rows  
- `silver_features_2017_dedup`: **1,642,175** rows  

The deduped table remains large relative to some other yearly tables — treat year-to-year row counts as potentially incomparable until export lineage is aligned.

## How to interpret results (general)

- **Empty result set:** no duplicate cell-date groups in the query output (good for that table).
- **Rows returned:** each row is a duplicate group; `row_count` is how many rows share that `(date, lat, lon)`. Investigate upstream exports or re-runs.

## Notes

- Run in BigQuery console or your SQL client against the correct project/dataset.
- Repeat per year by changing the table name in the `FROM` clause.
