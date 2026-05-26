-- =============================================================================
-- SECTION 0: How to run this file
-- =============================================================================
--
-- This file contains MULTIPLE STANDALONE BigQuery queries.
-- - Highlight and run ONE section at a time in the BigQuery console.
-- - Each section repeats the CTE logic intentionally because BigQuery CTEs
--   only exist within a single statement (they do NOT persist across
--   semicolon boundaries).
-- - All sections are READ-ONLY. No CREATE / REPLACE / INSERT / UPDATE /
--   DELETE / DROP / ALTER / TRUNCATE / MERGE statements are issued
--   anywhere in this file.
-- - This is EXPLORATION SQL, not production pipeline SQL. The team can
--   later promote selected logic into
--   `data_pipelines/05_engineer_gold_features.sql` after review.
--
-- Suggested order:
--   1) Section 1 - smoke test: 1000 sample rows of the candidate output.
--   2) Section 2 - feature distribution summary (one row).
--   3) Section 3 - leakage guardrail summary (one row).
--   4) Section 4 - temporal LAG coverage (one row).
--   5) Section 5 - 100 high-risk rows for manual inspection.
--   Section 6 is comment-only and does not execute any SQL.
--
-- Input table (placeholder - update if your project / dataset differs):
--   `msds603-mlops-project.openfire_features.silver_features_all_years`
--
-- Test scope: every section's BaseRows is filtered to 2019 to keep the
-- exploration query cheap. To explore another year, change the WHERE
-- clause inside BaseRows. To score the whole dataset, drop the WHERE
-- (and the trailing LIMIT in Section 1).
--
-- Leakage rule (binding on every section):
--   * Features may only reference fire dates strictly less than
--     window_start_date.
--   * `burned_in_next_15_days_candidate` is the ONLY column allowed to
--     look forward.
--   * Raw `historical_fire_dates` is per-cell, not per-cell-per-time
--     (~7.2% of fire-date references are future relative to the row
--     timestamp; see `docs/exploration/07_5_fire_date_temporal_check.md`).
--     It is parsed inside CTEs and intentionally NOT projected to the
--     final model-facing output.


-- =============================================================================
-- SECTION 1: Feature candidate sample
-- =============================================================================
-- Purpose: 1000-row sample of the model-facing feature candidate output.
-- Use to eyeball that columns look reasonable before running the
-- aggregations in Sections 2-4.

WITH BaseRows AS (
  SELECT
    timestamp,
    DATE(timestamp) AS window_start_date,
    latitude,
    longitude,
    mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
    gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
    mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE '2019-12-31'
),

FireDateArrays AS (
  -- Parse the comma-delimited fire-date string into a per-row array.
  -- SAFE_CAST keeps malformed entries as NULL instead of failing the query.
  SELECT
    b.timestamp,
    b.window_start_date,
    b.latitude,
    b.longitude,
    ARRAY(
      SELECT SAFE_CAST(TRIM(d) AS DATE)
      FROM UNNEST(SPLIT(IFNULL(b.clean_fire_dates, ''), ',')) AS d
      WHERE TRIM(d) != ''
    ) AS fire_date_array
  FROM BaseRows b
),

FireHistoryAggregates AS (
  -- LEFT JOIN UNNEST so cells with no fire history still survive. Every
  -- FEATURE aggregate gates on fire_date < window_start_date; only the
  -- target candidate is allowed to look forward.
  SELECT
    f.timestamp,
    f.window_start_date,
    f.latitude,
    f.longitude,
    COUNTIF(fire_date IS NOT NULL)                                       AS parsed_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  f.window_start_date)  AS past_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date >= f.window_start_date)  AS future_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  f.window_start_date)  AS prior_fire_count_all_time,
    COUNTIF(
      fire_date IS NOT NULL
      AND fire_date <  f.window_start_date
      AND fire_date >= DATE_SUB(f.window_start_date, INTERVAL 10 YEAR)
    )                                                                    AS prior_fire_count_10yr,
    MAX(IF(fire_date < f.window_start_date, fire_date, NULL))            AS last_past_burn_date,
    LOGICAL_OR(
      fire_date IS NOT NULL
      AND fire_date BETWEEN DATE_ADD(f.window_start_date, INTERVAL  1 DAY)
                        AND DATE_ADD(f.window_start_date, INTERVAL 15 DAY)
    )                                                                    AS burned_in_next_15_days_candidate
  FROM FireDateArrays f
  LEFT JOIN UNNEST(f.fire_date_array) AS fire_date
  GROUP BY f.timestamp, f.window_start_date, f.latitude, f.longitude
),

EnrichedRows AS (
  -- Identifiers + raw silver columns + seasonality / weather-stress /
  -- vegetation-fuel scores. Joined back to per-row fire-history aggregates.
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    b.mean_NDVI, b.mean_EVI, b.mean_NDWI, b.mean_NBR,
    b.gridmet_temp_max, b.gridmet_humidity_min, b.gridmet_precip_sum, b.gridmet_wind_max,
    b.mean_elevation, b.mean_slope, b.mean_cos_aspect, b.mean_sin_aspect,

    EXTRACT(MONTH FROM b.window_start_date) AS month_of_year,
    EXTRACT(MONTH FROM b.window_start_date) BETWEEN 6 AND 11 AS is_fire_season,
    DATE_DIFF(b.window_start_date,
              DATE(EXTRACT(YEAR FROM b.window_start_date), 6,  1), DAY) AS days_since_fire_season_start,
    DATE_DIFF(DATE(EXTRACT(YEAR FROM b.window_start_date), 11, 30),
              b.window_start_date, DAY) AS days_until_fire_season_end,

    SAFE_DIVIDE(b.gridmet_temp_max, 320.0) AS heat_stress_score,
    (1.0 - SAFE_DIVIDE(COALESCE(b.gridmet_humidity_min, 50.0), 100.0))
      * SAFE_DIVIDE(1.0, 1.0 + COALESCE(b.gridmet_precip_sum, 0.0)) AS dryness_score,
    SAFE_DIVIDE(COALESCE(b.gridmet_wind_max, 0.0), 20.0) AS wind_stress_score,
    (
      SAFE_DIVIDE(b.gridmet_temp_max, 320.0)
      + (1.0 - SAFE_DIVIDE(COALESCE(b.gridmet_humidity_min, 50.0), 100.0))
        * SAFE_DIVIDE(1.0, 1.0 + COALESCE(b.gridmet_precip_sum, 0.0))
      + SAFE_DIVIDE(COALESCE(b.gridmet_wind_max, 0.0), 20.0)
    ) / 3.0 AS weather_stress_score,

    SAFE_DIVIDE(COALESCE(b.mean_NDVI, 0.0) + COALESCE(b.mean_EVI, 0.0), 2.0) AS fuel_load_score,
    COALESCE(b.mean_NDVI, 0.0) * (1.0 - COALESCE(b.mean_NDWI, 0.0))          AS vegetation_dryness_score,
    (-1.0 * COALESCE(b.mean_NBR, 0.0)) + (-1.0 * COALESCE(b.mean_NDWI, 0.0)) AS burn_scar_or_low_moisture_signal,

    COALESCE(a.parsed_fire_date_count,    0)   AS parsed_fire_date_count,
    COALESCE(a.past_fire_date_count,      0)   AS past_fire_date_count,
    COALESCE(a.future_fire_date_count,    0)   AS future_fire_date_count,
    COALESCE(a.prior_fire_count_all_time, 0)   AS prior_fire_count_all_time,
    COALESCE(a.prior_fire_count_10yr,     0)   AS prior_fire_count_10yr,
    (COALESCE(a.prior_fire_count_all_time, 0) > 0) AS has_prior_fire_history,
    CASE WHEN a.last_past_burn_date IS NULL THEN NULL
         ELSE DATE_DIFF(b.window_start_date, a.last_past_burn_date, DAY) END
      AS days_since_last_burn_candidate,
    COALESCE(a.burned_in_next_15_days_candidate, FALSE) AS burned_in_next_15_days_candidate
  FROM BaseRows b
  LEFT JOIN FireHistoryAggregates a
    USING (timestamp, window_start_date, latitude, longitude)
),

FeatureRows AS (
  -- Interactions + LAG-based temporal trends. Window functions wrap
  -- lat/lon in CAST(... AS STRING) to match the production convention
  -- in `data_pipelines/05_engineer_gold_features.sql` (see README
  -- "Gotchas" - PARTITION BY on FLOAT64 is rejected).
  SELECT
    e.*,

    e.fuel_load_score          * e.weather_stress_score AS fuel_x_weather_stress,
    e.fuel_load_score          * e.wind_stress_score    AS fuel_x_wind,
    e.dryness_score            * e.wind_stress_score    AS dryness_x_wind,
    e.vegetation_dryness_score * e.heat_stress_score    AS vegetation_dryness_x_heat,
    e.burn_scar_or_low_moisture_signal * e.weather_stress_score AS nbr_x_weather_stress,

    e.prior_fire_count_10yr * e.weather_stress_score    AS prior_fire_x_weather_stress,
    e.prior_fire_count_10yr * e.fuel_load_score         AS prior_fire_x_fuel_load,
    CASE WHEN e.days_since_last_burn_candidate IS NOT NULL
              AND e.days_since_last_burn_candidate <= 3650
         THEN e.dryness_score
         ELSE 0.0 END AS recent_fire_x_dryness,

    LAG(e.mean_NDVI)           OVER cell_time AS ndvi_prev_observation,
    e.mean_NDVI - LAG(e.mean_NDVI)           OVER cell_time AS ndvi_change_from_prev,
    e.mean_NBR  - LAG(e.mean_NBR)            OVER cell_time AS nbr_change_from_prev,
    LAG(e.gridmet_precip_sum)  OVER cell_time AS precip_prev_observation,
    e.gridmet_precip_sum - LAG(e.gridmet_precip_sum) OVER cell_time AS precip_change_from_prev,
    e.gridmet_temp_max   - LAG(e.gridmet_temp_max)   OVER cell_time AS temp_change_from_prev
  FROM EnrichedRows e
  WINDOW cell_time AS (
    PARTITION BY CAST(e.latitude AS STRING), CAST(e.longitude AS STRING)
    ORDER BY e.window_start_date
  )
)

SELECT
  -- Identifiers.
  timestamp,
  window_start_date,
  latitude,
  longitude,

  -- Original useful columns.
  mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
  gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
  mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,

  -- Seasonality.
  month_of_year, is_fire_season,
  days_since_fire_season_start, days_until_fire_season_end,

  -- Weather stress.
  heat_stress_score, dryness_score, wind_stress_score, weather_stress_score,

  -- Vegetation / fuel.
  fuel_load_score, vegetation_dryness_score, burn_scar_or_low_moisture_signal,

  -- Pure-feature interactions.
  fuel_x_weather_stress, fuel_x_wind, dryness_x_wind,
  vegetation_dryness_x_heat, nbr_x_weather_stress,

  -- Past-only fire-history features.
  prior_fire_count_all_time, prior_fire_count_10yr,
  has_prior_fire_history, days_since_last_burn_candidate,

  -- Fire-history interactions.
  prior_fire_x_weather_stress, prior_fire_x_fuel_load, recent_fire_x_dryness,

  -- Temporal trends.
  ndvi_prev_observation, ndvi_change_from_prev, nbr_change_from_prev,
  precip_prev_observation, precip_change_from_prev, temp_change_from_prev,

  -- Target candidate (forward-looking, comparison only).
  burned_in_next_15_days_candidate,

  -- Sanity helpers (audit only - do not feed to the model).
  parsed_fire_date_count, past_fire_date_count, future_fire_date_count
FROM FeatureRows
ORDER BY window_start_date, latitude, longitude
LIMIT 1000;


-- =============================================================================
-- SECTION 2: Feature distribution summary
-- =============================================================================
-- Purpose: confirm candidate features are non-empty and numerically
-- reasonable. Returns a single summary row.

WITH BaseRows AS (
  SELECT
    timestamp,
    DATE(timestamp) AS window_start_date,
    latitude,
    longitude,
    mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
    gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
    mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE '2019-12-31'
),
FireDateArrays AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    ARRAY(
      SELECT SAFE_CAST(TRIM(d) AS DATE)
      FROM UNNEST(SPLIT(IFNULL(b.clean_fire_dates, ''), ',')) AS d
      WHERE TRIM(d) != ''
    ) AS fire_date_array
  FROM BaseRows b
),
FireHistoryAggregates AS (
  SELECT
    f.timestamp, f.window_start_date, f.latitude, f.longitude,
    COUNTIF(fire_date IS NOT NULL)                                       AS parsed_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  f.window_start_date)  AS past_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date >= f.window_start_date)  AS future_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  f.window_start_date)  AS prior_fire_count_all_time,
    COUNTIF(
      fire_date IS NOT NULL
      AND fire_date <  f.window_start_date
      AND fire_date >= DATE_SUB(f.window_start_date, INTERVAL 10 YEAR)
    )                                                                    AS prior_fire_count_10yr,
    MAX(IF(fire_date < f.window_start_date, fire_date, NULL))            AS last_past_burn_date,
    LOGICAL_OR(
      fire_date IS NOT NULL
      AND fire_date BETWEEN DATE_ADD(f.window_start_date, INTERVAL  1 DAY)
                        AND DATE_ADD(f.window_start_date, INTERVAL 15 DAY)
    )                                                                    AS burned_in_next_15_days_candidate
  FROM FireDateArrays f
  LEFT JOIN UNNEST(f.fire_date_array) AS fire_date
  GROUP BY f.timestamp, f.window_start_date, f.latitude, f.longitude
),
EnrichedRows AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    b.mean_NDVI, b.mean_EVI, b.mean_NDWI, b.mean_NBR,
    b.gridmet_temp_max, b.gridmet_humidity_min, b.gridmet_precip_sum, b.gridmet_wind_max,
    b.mean_elevation, b.mean_slope, b.mean_cos_aspect, b.mean_sin_aspect,
    EXTRACT(MONTH FROM b.window_start_date) AS month_of_year,
    EXTRACT(MONTH FROM b.window_start_date) BETWEEN 6 AND 11 AS is_fire_season,
    DATE_DIFF(b.window_start_date,
              DATE(EXTRACT(YEAR FROM b.window_start_date), 6,  1), DAY) AS days_since_fire_season_start,
    DATE_DIFF(DATE(EXTRACT(YEAR FROM b.window_start_date), 11, 30),
              b.window_start_date, DAY) AS days_until_fire_season_end,
    SAFE_DIVIDE(b.gridmet_temp_max, 320.0) AS heat_stress_score,
    (1.0 - SAFE_DIVIDE(COALESCE(b.gridmet_humidity_min, 50.0), 100.0))
      * SAFE_DIVIDE(1.0, 1.0 + COALESCE(b.gridmet_precip_sum, 0.0)) AS dryness_score,
    SAFE_DIVIDE(COALESCE(b.gridmet_wind_max, 0.0), 20.0) AS wind_stress_score,
    (
      SAFE_DIVIDE(b.gridmet_temp_max, 320.0)
      + (1.0 - SAFE_DIVIDE(COALESCE(b.gridmet_humidity_min, 50.0), 100.0))
        * SAFE_DIVIDE(1.0, 1.0 + COALESCE(b.gridmet_precip_sum, 0.0))
      + SAFE_DIVIDE(COALESCE(b.gridmet_wind_max, 0.0), 20.0)
    ) / 3.0 AS weather_stress_score,
    SAFE_DIVIDE(COALESCE(b.mean_NDVI, 0.0) + COALESCE(b.mean_EVI, 0.0), 2.0) AS fuel_load_score,
    COALESCE(b.mean_NDVI, 0.0) * (1.0 - COALESCE(b.mean_NDWI, 0.0))          AS vegetation_dryness_score,
    (-1.0 * COALESCE(b.mean_NBR, 0.0)) + (-1.0 * COALESCE(b.mean_NDWI, 0.0)) AS burn_scar_or_low_moisture_signal,
    COALESCE(a.parsed_fire_date_count,    0)   AS parsed_fire_date_count,
    COALESCE(a.past_fire_date_count,      0)   AS past_fire_date_count,
    COALESCE(a.future_fire_date_count,    0)   AS future_fire_date_count,
    COALESCE(a.prior_fire_count_all_time, 0)   AS prior_fire_count_all_time,
    COALESCE(a.prior_fire_count_10yr,     0)   AS prior_fire_count_10yr,
    (COALESCE(a.prior_fire_count_all_time, 0) > 0) AS has_prior_fire_history,
    CASE WHEN a.last_past_burn_date IS NULL THEN NULL
         ELSE DATE_DIFF(b.window_start_date, a.last_past_burn_date, DAY) END
      AS days_since_last_burn_candidate,
    COALESCE(a.burned_in_next_15_days_candidate, FALSE) AS burned_in_next_15_days_candidate
  FROM BaseRows b
  LEFT JOIN FireHistoryAggregates a
    USING (timestamp, window_start_date, latitude, longitude)
),
FeatureRows AS (
  SELECT
    e.*,
    e.fuel_load_score * e.weather_stress_score AS fuel_x_weather_stress,
    e.fuel_load_score * e.wind_stress_score    AS fuel_x_wind,
    e.dryness_score   * e.wind_stress_score    AS dryness_x_wind,
    e.vegetation_dryness_score * e.heat_stress_score AS vegetation_dryness_x_heat,
    e.burn_scar_or_low_moisture_signal * e.weather_stress_score AS nbr_x_weather_stress,
    e.prior_fire_count_10yr * e.weather_stress_score AS prior_fire_x_weather_stress,
    e.prior_fire_count_10yr * e.fuel_load_score      AS prior_fire_x_fuel_load,
    CASE WHEN e.days_since_last_burn_candidate IS NOT NULL
              AND e.days_since_last_burn_candidate <= 3650
         THEN e.dryness_score ELSE 0.0 END AS recent_fire_x_dryness,
    LAG(e.mean_NDVI)          OVER cell_time AS ndvi_prev_observation,
    e.mean_NDVI - LAG(e.mean_NDVI)          OVER cell_time AS ndvi_change_from_prev,
    e.mean_NBR  - LAG(e.mean_NBR)           OVER cell_time AS nbr_change_from_prev,
    LAG(e.gridmet_precip_sum) OVER cell_time AS precip_prev_observation,
    e.gridmet_precip_sum - LAG(e.gridmet_precip_sum) OVER cell_time AS precip_change_from_prev,
    e.gridmet_temp_max   - LAG(e.gridmet_temp_max)   OVER cell_time AS temp_change_from_prev
  FROM EnrichedRows e
  WINDOW cell_time AS (
    PARTITION BY CAST(e.latitude AS STRING), CAST(e.longitude AS STRING)
    ORDER BY e.window_start_date
  )
)

SELECT
  COUNT(*) AS total_rows,
  COUNTIF(burned_in_next_15_days_candidate) AS positive_target_rows,
  SAFE_DIVIDE(COUNTIF(burned_in_next_15_days_candidate) * 1.0, COUNT(*)) AS positive_target_rate,

  AVG(weather_stress_score) AS avg_weather_stress_score,
  MIN(weather_stress_score) AS min_weather_stress_score,
  MAX(weather_stress_score) AS max_weather_stress_score,

  AVG(fuel_load_score) AS avg_fuel_load_score,
  MIN(fuel_load_score) AS min_fuel_load_score,
  MAX(fuel_load_score) AS max_fuel_load_score,

  AVG(fuel_x_weather_stress) AS avg_fuel_x_weather_stress,
  MIN(fuel_x_weather_stress) AS min_fuel_x_weather_stress,
  MAX(fuel_x_weather_stress) AS max_fuel_x_weather_stress,

  COUNTIF(has_prior_fire_history) AS rows_with_prior_fire_history,
  SAFE_DIVIDE(COUNTIF(has_prior_fire_history) * 1.0, COUNT(*)) AS prior_fire_history_rate,

  COUNTIF(ndvi_change_from_prev IS NOT NULL)   AS rows_with_ndvi_lag,
  COUNTIF(precip_change_from_prev IS NOT NULL) AS rows_with_precip_lag,

  AVG(prior_fire_count_10yr) AS avg_prior_fire_count_10yr,
  MIN(prior_fire_count_10yr) AS min_prior_fire_count_10yr,
  MAX(prior_fire_count_10yr) AS max_prior_fire_count_10yr,

  AVG(days_since_last_burn_candidate) AS avg_days_since_last_burn_candidate,
  MIN(days_since_last_burn_candidate) AS min_days_since_last_burn_candidate,
  MAX(days_since_last_burn_candidate) AS max_days_since_last_burn_candidate
FROM FeatureRows;


-- =============================================================================
-- SECTION 3: Leakage guardrail summary
-- =============================================================================
-- Purpose: confirm the fire-date temporal split. Future fire dates show
-- up in the parsed-helper counters because `historical_fire_dates` is
-- per-cell, not per-cell-per-time. That is EXPECTED and SAFE because:
--   * `burned_in_next_15_days_candidate` is the only column allowed to
--     consume future dates.
--   * Every model FEATURE in this file gates on
--     fire_date < window_start_date.
-- If you ever see future-fire-date counts driving a non-target column,
-- treat it as a leakage bug.

WITH BaseRows AS (
  SELECT
    timestamp,
    DATE(timestamp) AS window_start_date,
    latitude,
    longitude,
    mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
    gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
    mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE '2019-12-31'
),
FireDateArrays AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    ARRAY(
      SELECT SAFE_CAST(TRIM(d) AS DATE)
      FROM UNNEST(SPLIT(IFNULL(b.clean_fire_dates, ''), ',')) AS d
      WHERE TRIM(d) != ''
    ) AS fire_date_array
  FROM BaseRows b
),
FireHistoryAggregates AS (
  SELECT
    f.timestamp, f.window_start_date, f.latitude, f.longitude,
    COUNTIF(fire_date IS NOT NULL)                                       AS parsed_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  f.window_start_date)  AS past_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date >= f.window_start_date)  AS future_fire_date_count,
    LOGICAL_OR(
      fire_date IS NOT NULL
      AND fire_date BETWEEN DATE_ADD(f.window_start_date, INTERVAL  1 DAY)
                        AND DATE_ADD(f.window_start_date, INTERVAL 15 DAY)
    )                                                                    AS burned_in_next_15_days_candidate
  FROM FireDateArrays f
  LEFT JOIN UNNEST(f.fire_date_array) AS fire_date
  GROUP BY f.timestamp, f.window_start_date, f.latitude, f.longitude
),
PerRow AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    COALESCE(a.parsed_fire_date_count, 0) AS parsed_fire_date_count,
    COALESCE(a.past_fire_date_count,   0) AS past_fire_date_count,
    COALESCE(a.future_fire_date_count, 0) AS future_fire_date_count,
    COALESCE(a.burned_in_next_15_days_candidate, FALSE) AS burned_in_next_15_days_candidate
  FROM BaseRows b
  LEFT JOIN FireHistoryAggregates a
    USING (timestamp, window_start_date, latitude, longitude)
)

SELECT
  COUNT(*)                                          AS total_rows,
  COUNTIF(parsed_fire_date_count > 0)               AS rows_with_any_parsed_fire_dates,
  SUM(parsed_fire_date_count)                       AS total_parsed_fire_date_refs,
  SUM(past_fire_date_count)                         AS total_past_fire_date_refs,
  SUM(future_fire_date_count)                       AS total_future_fire_date_refs,
  COUNTIF(future_fire_date_count > 0)               AS rows_with_future_fire_dates,
  COUNTIF(burned_in_next_15_days_candidate)         AS rows_with_positive_target,
  SAFE_DIVIDE(
    COUNTIF(future_fire_date_count > 0) * 100.0, COUNT(*)
  )                                                 AS pct_rows_with_future_fire_dates,
  SAFE_DIVIDE(
    COUNTIF(burned_in_next_15_days_candidate) * 100.0, COUNT(*)
  )                                                 AS pct_rows_positive_target
FROM PerRow;


-- =============================================================================
-- SECTION 4: Temporal feature coverage
-- =============================================================================
-- Purpose: confirm LAG-based temporal features are populated. The first
-- observation per cell will always have NULL LAG values; we want to see
-- that share *not* dominate the dataset.

WITH BaseRows AS (
  SELECT
    timestamp,
    DATE(timestamp) AS window_start_date,
    latitude,
    longitude,
    mean_NDVI, mean_NBR, gridmet_precip_sum, gridmet_temp_max
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE '2019-12-31'
),
LagRows AS (
  SELECT
    b.window_start_date, b.latitude, b.longitude,
    LAG(b.mean_NDVI)          OVER cell_time AS ndvi_prev_observation,
    b.mean_NDVI - LAG(b.mean_NDVI)          OVER cell_time AS ndvi_change_from_prev,
    b.mean_NBR  - LAG(b.mean_NBR)           OVER cell_time AS nbr_change_from_prev,
    b.gridmet_precip_sum - LAG(b.gridmet_precip_sum) OVER cell_time AS precip_change_from_prev,
    b.gridmet_temp_max   - LAG(b.gridmet_temp_max)   OVER cell_time AS temp_change_from_prev
  FROM BaseRows b
  WINDOW cell_time AS (
    PARTITION BY CAST(b.latitude AS STRING), CAST(b.longitude AS STRING)
    ORDER BY b.window_start_date
  )
)

SELECT
  COUNT(*)                                          AS total_rows,

  COUNTIF(ndvi_prev_observation IS NOT NULL)        AS rows_with_ndvi_prev_observation,
  SAFE_DIVIDE(
    COUNTIF(ndvi_prev_observation IS NOT NULL) * 100.0, COUNT(*)
  )                                                 AS pct_with_ndvi_prev_observation,

  COUNTIF(ndvi_change_from_prev IS NOT NULL)        AS rows_with_ndvi_change,
  COUNTIF(nbr_change_from_prev IS NOT NULL)         AS rows_with_nbr_change,
  COUNTIF(precip_change_from_prev IS NOT NULL)      AS rows_with_precip_change,
  COUNTIF(temp_change_from_prev IS NOT NULL)        AS rows_with_temp_change,

  AVG(ndvi_change_from_prev) AS avg_ndvi_change_from_prev,
  MIN(ndvi_change_from_prev) AS min_ndvi_change_from_prev,
  MAX(ndvi_change_from_prev) AS max_ndvi_change_from_prev,

  AVG(nbr_change_from_prev) AS avg_nbr_change_from_prev,
  MIN(nbr_change_from_prev) AS min_nbr_change_from_prev,
  MAX(nbr_change_from_prev) AS max_nbr_change_from_prev,

  AVG(precip_change_from_prev) AS avg_precip_change_from_prev,
  MIN(precip_change_from_prev) AS min_precip_change_from_prev,
  MAX(precip_change_from_prev) AS max_precip_change_from_prev,

  AVG(temp_change_from_prev) AS avg_temp_change_from_prev,
  MIN(temp_change_from_prev) AS min_temp_change_from_prev,
  MAX(temp_change_from_prev) AS max_temp_change_from_prev
FROM LagRows;


-- =============================================================================
-- SECTION 5: High-risk sample rows
-- =============================================================================
-- Purpose: 100-row sample of the top candidate-risk rows for manual
-- inspection. Useful for sanity-checking that high scores correspond to
-- plausibly fire-prone cells / dates.

WITH BaseRows AS (
  SELECT
    timestamp,
    DATE(timestamp) AS window_start_date,
    latitude,
    longitude,
    mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
    gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
    mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
  WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE '2019-12-31'
),
FireDateArrays AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    ARRAY(
      SELECT SAFE_CAST(TRIM(d) AS DATE)
      FROM UNNEST(SPLIT(IFNULL(b.clean_fire_dates, ''), ',')) AS d
      WHERE TRIM(d) != ''
    ) AS fire_date_array
  FROM BaseRows b
),
FireHistoryAggregates AS (
  SELECT
    f.timestamp, f.window_start_date, f.latitude, f.longitude,
    COUNTIF(
      fire_date IS NOT NULL
      AND fire_date <  f.window_start_date
      AND fire_date >= DATE_SUB(f.window_start_date, INTERVAL 10 YEAR)
    ) AS prior_fire_count_10yr,
    MAX(IF(fire_date < f.window_start_date, fire_date, NULL)) AS last_past_burn_date,
    LOGICAL_OR(
      fire_date IS NOT NULL
      AND fire_date BETWEEN DATE_ADD(f.window_start_date, INTERVAL  1 DAY)
                        AND DATE_ADD(f.window_start_date, INTERVAL 15 DAY)
    ) AS burned_in_next_15_days_candidate
  FROM FireDateArrays f
  LEFT JOIN UNNEST(f.fire_date_array) AS fire_date
  GROUP BY f.timestamp, f.window_start_date, f.latitude, f.longitude
),
Scored AS (
  SELECT
    b.timestamp, b.window_start_date, b.latitude, b.longitude,
    b.mean_NDVI, b.mean_NDWI, b.mean_NBR,
    b.gridmet_temp_max, b.gridmet_humidity_min, b.gridmet_precip_sum, b.gridmet_wind_max,

    (
      SAFE_DIVIDE(b.gridmet_temp_max, 320.0)
      + (1.0 - SAFE_DIVIDE(COALESCE(b.gridmet_humidity_min, 50.0), 100.0))
        * SAFE_DIVIDE(1.0, 1.0 + COALESCE(b.gridmet_precip_sum, 0.0))
      + SAFE_DIVIDE(COALESCE(b.gridmet_wind_max, 0.0), 20.0)
    ) / 3.0 AS weather_stress_score,

    SAFE_DIVIDE(COALESCE(b.mean_NDVI, 0.0) + COALESCE(b.mean_EVI, 0.0), 2.0) AS fuel_load_score,

    COALESCE(a.prior_fire_count_10yr, 0) AS prior_fire_count_10yr,
    CASE WHEN a.last_past_burn_date IS NULL THEN NULL
         ELSE DATE_DIFF(b.window_start_date, a.last_past_burn_date, DAY) END
      AS days_since_last_burn_candidate,
    COALESCE(a.burned_in_next_15_days_candidate, FALSE) AS burned_in_next_15_days_candidate
  FROM BaseRows b
  LEFT JOIN FireHistoryAggregates a
    USING (timestamp, window_start_date, latitude, longitude)
)

SELECT
  timestamp, window_start_date, latitude, longitude,
  mean_NDVI, mean_NDWI, mean_NBR,
  gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
  weather_stress_score,
  fuel_load_score,
  fuel_load_score * weather_stress_score AS fuel_x_weather_stress,
  prior_fire_count_10yr,
  days_since_last_burn_candidate,
  prior_fire_count_10yr * weather_stress_score AS prior_fire_x_weather_stress,
  burned_in_next_15_days_candidate
FROM Scored
ORDER BY
  weather_stress_score DESC,
  fuel_load_score * weather_stress_score DESC,
  prior_fire_count_10yr * weather_stress_score DESC
LIMIT 100;


-- =============================================================================
-- SECTION 6: Feature plan notes (comment-only - executes nothing)
-- =============================================================================
--
-- After running Sections 1-5, the team can use these notes when deciding
-- which candidates are worth promoting into
-- `data_pipelines/05_engineer_gold_features.sql`.
--
-- LIKELY FIRST PROMOTIONS (cheap, follow leakage rule, complement
-- existing gold columns):
--   * weather_stress_score
--   * fuel_load_score
--   * fuel_x_weather_stress
--   * prior_fire_count_10yr
--   * days_since_last_burn_candidate (already promoted as
--     `days_since_last_burn` in step 05; the candidate version exists
--     here for cross-checking with the production calculation)
--
-- USEFUL BUT NEED EVALUATION (review with feature-importance metrics
-- from Random Forest / XGBoost training before promoting):
--   * Temporal LAG features
--     (ndvi_change_from_prev, nbr_change_from_prev,
--      precip_change_from_prev, temp_change_from_prev)
--     - overlap with the existing 5d/15d/30d/60d *_change_* columns in
--       step 05; promote only if the previous-observation deltas beat
--       the fixed-step LAGs.
--   * Fire-history interactions
--     (prior_fire_x_weather_stress, prior_fire_x_fuel_load,
--      recent_fire_x_dryness)
--     - powerful in theory, but check correlation with the standalone
--       prior_fire_count_10yr and weather_stress_score before paying
--       for an extra column in gold.
--
-- DO NOT PROMOTE:
--   * Raw `historical_fire_dates` - it includes future fire dates
--     relative to each row (~7.2% of references). Feeding it to the
--     model in any form (raw string, array_length, naive count) is a
--     leak. See `docs/exploration/07_5_fire_date_temporal_check.md`.
--   * `parsed_fire_date_count`, `past_fire_date_count`,
--     `future_fire_date_count` - these are audit helpers, not features.
--     Keep them in exploration, drop them from anything fed to the
--     trainer.
