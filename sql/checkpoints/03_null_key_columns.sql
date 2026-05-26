-- Checkpoint 03
-- Purpose: count NULLs in key columns of the silver feature table.
-- Scope: run on a single yearly silver shard (template: 2024) before merging
--        years and engineering gold features.
--
-- Why this matters:
--   * `timestamp`, `latitude`, `longitude` form the row grain. NULLs in any of
--     them break dedup, the LAG window functions in
--     `data_pipelines/05_engineer_gold_features.sql`, and downstream joins.
--   * NULLs in the satellite / weather feature columns (NDVI, NDWI, GRIDMET,
--     etc.) are tolerable in small amounts but a large spike usually means the
--     GEE export was incomplete for that shard.
--
-- Expected result:
--   * `null_timestamp`, `null_latitude`, `null_longitude` should all be 0.
--   * The remaining feature counters should be small relative to `total_rows`.
--     A useful rule of thumb is < 1% per column for the core bands and
--     gridmet measures. Anything materially higher is a signal to re-pull
--     that year before training.
--
-- Replace `PROJECT_ID.DATASET_ID.silver_features_2024` with your project /
-- dataset before running. To check another year, swap the suffix in the
-- table name (e.g. silver_features_2017_dedup).

SELECT
  COUNT(*) AS total_rows,

  -- Row-grain keys: must be 0.
  COUNTIF(timestamp IS NULL) AS null_timestamp,
  COUNTIF(latitude  IS NULL) AS null_latitude,
  COUNTIF(longitude IS NULL) AS null_longitude,

  -- Vegetation / moisture indices (Sentinel-2 derived).
  COUNTIF(mean_NDVI IS NULL) AS null_mean_ndvi,
  COUNTIF(mean_NDWI IS NULL) AS null_mean_ndwi,
  COUNTIF(mean_EVI  IS NULL) AS null_mean_evi,
  COUNTIF(mean_NBR  IS NULL) AS null_mean_nbr,

  -- Raw Sentinel-2 bands used downstream.
  COUNTIF(B2  IS NULL) AS null_b2,
  COUNTIF(B3  IS NULL) AS null_b3,
  COUNTIF(B4  IS NULL) AS null_b4,
  COUNTIF(B8  IS NULL) AS null_b8,
  COUNTIF(B11 IS NULL) AS null_b11,
  COUNTIF(B12 IS NULL) AS null_b12,

  -- Terrain features (SRTM).
  COUNTIF(mean_elevation  IS NULL) AS null_mean_elevation,
  COUNTIF(mean_slope      IS NULL) AS null_mean_slope,
  COUNTIF(mean_cos_aspect IS NULL) AS null_mean_cos_aspect,
  COUNTIF(mean_sin_aspect IS NULL) AS null_mean_sin_aspect,

  -- Weather (GRIDMET).
  COUNTIF(gridmet_temp_max     IS NULL) AS null_gridmet_temp_max,
  COUNTIF(gridmet_humidity_min IS NULL) AS null_gridmet_humidity_min,
  COUNTIF(gridmet_precip_sum   IS NULL) AS null_gridmet_precip_sum,
  COUNTIF(gridmet_wind_max     IS NULL) AS null_gridmet_wind_max
FROM `PROJECT_ID.DATASET_ID.silver_features_2024`;
