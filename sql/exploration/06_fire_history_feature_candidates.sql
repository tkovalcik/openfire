-- Exploration 06 - Fire history feature candidates
--
-- This is EXPLORATION SQL, not production pipeline SQL. It is safe to run
-- because it is read-only: no CREATE / REPLACE / INSERT / UPDATE / DELETE /
-- DROP / ALTER / TRUNCATE statements are issued anywhere in this file.
--
-- Goal: prototype a handful of past-only fire-history feature candidates on
-- top of the merged silver table. The current production pipeline
-- (`data_pipelines/05_engineer_gold_features.sql`) already produces the
-- target `burned_in_next_15_days` and the past-only `days_since_last_burn`
-- feature; this file proposes a few more candidates the team can review
-- before deciding whether to promote any of them into step 05.
--
-- Leakage rule (applies to every candidate FEATURE below):
--   * Features may only reference fire_date < window_start_date.
--   * Anything looking >= window_start_date is reserved for the TARGET.
--
-- Input table (placeholder - update if your project / dataset differs):
--   `msds603-mlops-project.openfire_features.silver_features_all_years`
--
-- Assumed columns: timestamp, latitude, longitude, historical_fire_dates
-- (comma-delimited string, possibly NULL or the literal 'None').
--
-- This minimal version intentionally excludes optional satellite/weather
-- pass-through columns so it can run even when schema names differ. After
-- confirming the fire-history logic works, teammates can add real feature
-- columns back based on the actual BigQuery schema.

WITH ParsedRows AS (
  -- One row per (timestamp, latitude, longitude). Normalize the fire-date
  -- string and derive the window_start_date used by the leakage filter.
  SELECT
    timestamp,
    CAST(timestamp AS DATE) AS window_start_date,
    latitude,
    longitude,
    historical_fire_dates,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
    -- Optional satellite / weather pass-through columns are intentionally
    -- omitted in this minimal version. To add them back, append entries
    -- such as `mean_NDVI`, `mean_NDWI`, `mean_EVI`, `mean_NBR`,
    -- `temperature_2m`, `precipitation`, `relative_humidity_2m`, or
    -- `wind_speed_10m` here once the real silver schema is confirmed.
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
),

FireDateArrays AS (
  -- Split the comma-delimited string into an array of safely-cast dates.
  -- SAFE_CAST keeps malformed entries as NULL instead of failing the query.
  SELECT
    p.*,
    ARRAY(
      SELECT SAFE_CAST(TRIM(d) AS DATE)
      FROM UNNEST(SPLIT(IFNULL(p.clean_fire_dates, ''), ',')) AS d
      WHERE TRIM(d) != ''
    ) AS fire_date_array
  FROM ParsedRows p
),

UnnestedFireDates AS (
  -- Long-format helper: one row per (silver row, fire_date). Used by the
  -- aggregations below. We keep window_start_date alongside so the
  -- past/future split can happen inside the aggregate.
  SELECT
    f.timestamp,
    f.window_start_date,
    f.latitude,
    f.longitude,
    fire_date
  FROM FireDateArrays f,
  UNNEST(f.fire_date_array) AS fire_date
),

FireHistoryAggregates AS (
  -- Re-collapse to one row per (timestamp, latitude, longitude) with the
  -- fire-history aggregates we care about. Every feature here uses
  -- fire_date < window_start_date so it is strictly past-only.
  SELECT
    timestamp,
    window_start_date,
    latitude,
    longitude,

    -- Sanity-check helpers (do not feed these to the model directly).
    COUNTIF(fire_date IS NOT NULL) AS parsed_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date >= window_start_date) AS future_fire_date_count,
    COUNTIF(fire_date IS NOT NULL AND fire_date <  window_start_date) AS past_fire_date_count,

    -- Past-only feature candidates.
    COUNTIF(fire_date IS NOT NULL AND fire_date < window_start_date) AS prior_fire_count_all_time,
    COUNTIF(
      fire_date IS NOT NULL
      AND fire_date <  window_start_date
      AND fire_date >= DATE_SUB(window_start_date, INTERVAL  5 YEAR)
    ) AS prior_fire_count_5yr,
    COUNTIF(
      fire_date IS NOT NULL
      AND fire_date <  window_start_date
      AND fire_date >= DATE_SUB(window_start_date, INTERVAL 10 YEAR)
    ) AS prior_fire_count_10yr,

    -- Most recent past burn date (NULL if cell never burned before now).
    MAX(IF(fire_date < window_start_date, fire_date, NULL)) AS last_past_burn_date,

    -- Target candidate (allowed to look forward).
    LOGICAL_OR(
      fire_date IS NOT NULL
      AND fire_date BETWEEN DATE_ADD(window_start_date, INTERVAL  1 DAY)
                        AND DATE_ADD(window_start_date, INTERVAL 15 DAY)
    ) AS burned_in_next_15_days_candidate
  FROM UnnestedFireDates
  GROUP BY timestamp, window_start_date, latitude, longitude
),

FeatureCandidates AS (
  -- Join the aggregates back to the parsed silver row so the final output
  -- still has one row per (timestamp, latitude, longitude). Optional
  -- satellite / weather pass-through columns are intentionally omitted in
  -- this minimal version; add them back here (and in ParsedRows) once the
  -- real silver schema is confirmed.
  SELECT
    f.timestamp,
    f.window_start_date,
    f.latitude,
    f.longitude,

    -- Sanity-check helpers.
    (f.clean_fire_dates IS NOT NULL) AS has_any_historical_fire_dates,
    COALESCE(a.parsed_fire_date_count, 0) AS parsed_fire_date_count,
    COALESCE(a.future_fire_date_count, 0) AS future_fire_date_count,
    COALESCE(a.past_fire_date_count,   0) AS past_fire_date_count,

    -- Past-only feature candidates.
    COALESCE(a.prior_fire_count_all_time, 0) AS prior_fire_count_all_time,
    COALESCE(a.prior_fire_count_5yr,      0) AS prior_fire_count_5yr,
    COALESCE(a.prior_fire_count_10yr,     0) AS prior_fire_count_10yr,
    (COALESCE(a.prior_fire_count_all_time, 0) > 0) AS has_prior_fire_history,

    -- Days / years since last past burn. NULL if the cell never burned
    -- before window_start_date - downstream consumers can fill with a
    -- magic value (e.g. 9999) the same way step 05 already does.
    CASE
      WHEN a.last_past_burn_date IS NULL THEN NULL
      ELSE DATE_DIFF(f.window_start_date, a.last_past_burn_date, DAY)
    END AS days_since_last_burn_candidate,
    CASE
      WHEN a.last_past_burn_date IS NULL THEN NULL
      ELSE DATE_DIFF(f.window_start_date, a.last_past_burn_date, DAY) / 365.25
    END AS years_since_last_burn_candidate,

    -- Simple density: fires-per-year averaged over the last decade.
    COALESCE(a.prior_fire_count_10yr, 0) / 10.0 AS fire_history_density_score,

    -- Target candidate (forward-looking on purpose).
    COALESCE(a.burned_in_next_15_days_candidate, FALSE) AS burned_in_next_15_days_candidate
  FROM FireDateArrays f
  LEFT JOIN FireHistoryAggregates a
    USING (timestamp, window_start_date, latitude, longitude)
)

-- Final sample: one row per (timestamp, latitude, longitude). Bounded so it
-- is cheap to run interactively in the BigQuery console.
SELECT *
FROM FeatureCandidates
ORDER BY window_start_date, latitude, longitude
LIMIT 1000;
