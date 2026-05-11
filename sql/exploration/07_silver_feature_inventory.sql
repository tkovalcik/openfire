-- Exploration 07 - Silver feature inventory
--
-- Purpose: inspect the current `silver_features_all_years` table BEFORE we
-- prototype any new feature SQL. This file answers "what is already in
-- silver, what's the grain, and what feature directions are feasible?"
-- without making changes to the warehouse.
--
-- This file is READ-ONLY. It does NOT issue CREATE / REPLACE / INSERT /
-- UPDATE / DELETE / DROP / ALTER / TRUNCATE statements anywhere. It is
-- safe to copy-paste the whole file into the BigQuery console; you can
-- highlight and run a single section at a time, since each section is a
-- standalone SELECT.
--
-- Input table (placeholder - update if your project / dataset differs):
--   `msds603-mlops-project.openfire_features.silver_features_all_years`
--
-- Medallion context:
--   Bronze = raw ingestion (GEE exports, CalFire FRAP, GRIDMET pulls).
--   Silver = cleaned, conformed, joined per-cell-per-window rows. <-- this file
--   Gold   = model-ready features and target (built by step 05).
--
-- Run order: each numbered section is independent. Run sections 1-2 first
-- to learn the schema, then sections 3-6 to verify grain and coverage,
-- then section 7 for an eyeball sample. Section 8 is comments only.

-- =============================================================================
-- Section 1: Schema inventory
-- =============================================================================
-- What columns exist on `silver_features_all_years`, in declared order.

SELECT
  column_name,
  data_type,
  ordinal_position
FROM `msds603-mlops-project.openfire_features.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'silver_features_all_years'
ORDER BY ordinal_position;


-- =============================================================================
-- Section 2: Feature-family column search
-- =============================================================================
-- Same INFORMATION_SCHEMA source, but tagged into feature families so we
-- can quickly see which families silver currently covers and which are
-- missing. The CASE order matters: a column matches the first family that
-- claims it.

SELECT
  CASE
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(ndvi|ndwi|evi|nbr)')
      THEN '01_vegetation_indices'
    WHEN REGEXP_CONTAINS(column_name, r'^B(0?[2-9]|1[0-2])$')
         OR REGEXP_CONTAINS(LOWER(column_name), r'(sentinel|sr_band|surface_refl)')
      THEN '02_sentinel_bands'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(temp|temperature|tmin|tmax|tmmn|tmmx)')
      THEN '03_weather_temperature'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(precip|pr_|rain|pr$)')
      THEN '04_weather_precipitation'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(humid|rmin|rmax|rh)')
      THEN '05_weather_humidity'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(wind|vs_|gust)')
      THEN '06_weather_wind'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(gridmet|erc|bi_|fm100|fm1000|vpd|fwi)')
      THEN '07_weather_gridmet_other'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(elevation|slope|aspect|terrain|dem|3dep)')
      THEN '08_terrain'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(fire|burn|frap|ignition|historical_fire)')
      THEN '09_fire_history'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(timestamp|^date|window_start|year|month)')
      THEN '10_time'
    WHEN REGEXP_CONTAINS(LOWER(column_name), r'(latitude|longitude|lat$|lon$|geom|cell)')
      THEN '11_location'
    ELSE '99_other'
  END AS feature_family,
  column_name,
  data_type,
  ordinal_position
FROM `msds603-mlops-project.openfire_features.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'silver_features_all_years'
ORDER BY feature_family, ordinal_position;


-- =============================================================================
-- Section 3: Silver row counts and date range
-- =============================================================================
-- Quick profile of the table. APPROX_COUNT_DISTINCT keeps the cost low on
-- a ~38M-row table; we only need order-of-magnitude here.

SELECT
  COUNT(*)                                                             AS total_rows,
  MIN(DATE(timestamp))                                                 AS min_date,
  MAX(DATE(timestamp))                                                 AS max_date,
  COUNT(DISTINCT DATE(timestamp))                                      AS distinct_dates,
  APPROX_COUNT_DISTINCT(CONCAT(CAST(latitude  AS STRING), '|',
                               CAST(longitude AS STRING)))             AS approx_distinct_cells
FROM `msds603-mlops-project.openfire_features.silver_features_all_years`;


-- =============================================================================
-- Section 4: Grain duplicate check
-- =============================================================================
-- Expected silver grain: one row per (DATE(timestamp), latitude, longitude).
-- Anything returned here is a duplicate group; an empty result set is good.

SELECT
  DATE(timestamp) AS window_start_date,
  latitude,
  longitude,
  COUNT(*) AS row_count
FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
GROUP BY 1, 2, 3
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 100;


-- =============================================================================
-- Section 5: Dates-per-cell distribution
-- =============================================================================
-- How many distinct windows do we have per cell? Used to assess whether
-- temporal trend / lag features are feasible (need >= ~12 dates per cell
-- for the 60-day lag to be useful).

WITH PerCell AS (
  SELECT
    latitude,
    longitude,
    COUNT(DISTINCT DATE(timestamp)) AS dates_per_cell
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  GROUP BY latitude, longitude
)
SELECT
  COUNT(*)              AS num_cells,
  MIN(dates_per_cell)   AS min_dates_per_cell,
  MAX(dates_per_cell)   AS max_dates_per_cell,
  AVG(dates_per_cell)   AS avg_dates_per_cell,
  APPROX_QUANTILES(dates_per_cell, 4) AS quartiles_dates_per_cell,
  APPROX_QUANTILES(dates_per_cell, 10) AS deciles_dates_per_cell
FROM PerCell;


-- =============================================================================
-- Section 6: Fire-history coverage
-- =============================================================================
-- Share of silver rows whose joined `historical_fire_dates` is populated.
-- The literal 'None' string is treated as "no history", matching the
-- convention used by `data_pipelines/05_engineer_gold_features.sql`.

SELECT
  COUNT(*)                                                                              AS total_rows,
  COUNTIF(historical_fire_dates IS NOT NULL AND historical_fire_dates != 'None')        AS rows_with_fire_history,
  COUNTIF(historical_fire_dates IS NULL OR historical_fire_dates = 'None')              AS rows_without_fire_history,
  SAFE_DIVIDE(
    COUNTIF(historical_fire_dates IS NOT NULL AND historical_fire_dates != 'None') * 100.0,
    COUNT(*)
  )                                                                                     AS pct_rows_with_fire_history
FROM `msds603-mlops-project.openfire_features.silver_features_all_years`;


-- =============================================================================
-- Section 7: Small sample of rows with fire history
-- =============================================================================
-- Eyeball-able sample to understand how `historical_fire_dates` is shaped
-- for cells that actually have history. Useful before designing fire-
-- history features.

SELECT
  timestamp,
  latitude,
  longitude,
  historical_fire_dates
FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
WHERE historical_fire_dates IS NOT NULL
  AND historical_fire_dates != 'None'
ORDER BY timestamp, latitude, longitude
LIMIT 20;


-- =============================================================================
-- Section 8: Comment-only feature plan (no SQL executed)
-- =============================================================================
-- Possible next feature candidates to prototype in subsequent exploration
-- files (e.g. sql/exploration/06_fire_history_feature_candidates.sql plus
-- new ones to come). All features below MUST be past-only to avoid label
-- leakage; only the target is allowed to look forward.
--
--   A. Fire-history count features (past-only)
--      * prior_fire_count_5yr, prior_fire_count_10yr, all-time
--      * has_prior_fire_history (boolean)
--      * fires_per_year_density = prior_count / years_observed
--      * months_since_last_burn / years_since_last_burn
--      Trees handle these well without scaling.
--
--   B. Fuel-x-weather interaction features
--      * dryness = (1 - mean_NDWI) * gridmet_temp_max
--      * heat_load = gridmet_temp_max * (1 - gridmet_humidity_min/100)
--      * wind_dryness = gridmet_wind_max * (1 - mean_NDWI)
--      Cheap to compute in gold; capture compounded fire-weather risk.
--
--   C. Seasonality features
--      * EXTRACT(MONTH FROM window_start_date) as fire_season_month
--      * sin/cos transforms of day-of-year for periodicity
--      * is_dry_season boolean (e.g. months 6-10 in California)
--      Tree models can use raw month; linear/NN models prefer sin/cos.
--
--   D. Temporal trend features (only if Section 5 confirms enough dates
--      per cell; ~12+ for 60d lag, ~6+ for 30d).
--      * rolling means of NDVI / NDWI over 3, 6, 12 prior windows
--      * relative anomaly = (current - rolling_mean) / rolling_std
--      * extends the LAG features already in step 05.
--
--   Leakage rule (repeat for emphasis):
--      Every FEATURE filter must reference fire_date < window_start_date
--      or only inputs observed at or before window_start_date. Anything
--      using future windows is reserved for the TARGET.
