-- Exploration 07.5 - Fire-date temporal sanity check
--
-- Purpose: confirm whether `historical_fire_dates` on
-- `silver_features_all_years` actually represents PAST history relative to
-- each row's `timestamp`, or whether it is the cell's full fire-date list
-- (past AND future relative to the row).
--
-- This file is READ-ONLY. It does NOT issue CREATE / REPLACE / INSERT /
-- UPDATE / DELETE / DROP / ALTER / TRUNCATE statements. Safe to copy-paste
-- into the BigQuery console; each section is a standalone SELECT and can
-- be highlighted/run on its own.
--
-- Input table (placeholder - update if your project / dataset differs):
--   `msds603-mlops-project.openfire_features.silver_features_all_years`
--
-- Background: a row with timestamp = 2017-09-01 was observed to contain
-- `historical_fire_dates` entries like 2021-11-11. That implies the column
-- is "all known fire dates per cell", not "past history as of this row".
-- That's fine for label construction (we need future fires to build
-- `burned_in_next_15_days`), but it makes raw `historical_fire_dates` and
-- naive all-time fire counts unsafe to feed directly to the model.


-- =============================================================================
-- Section 1: Past / same-day / future fire-date counts
-- =============================================================================
-- Each "row" here is one (silver row, fire_date) pair after parsing. We
-- classify it relative to the silver row's window_start_date and report
-- the percent that fall in the future (which would be label-leak material
-- if used as a feature).

WITH ParsedRows AS (
  SELECT
    timestamp,
    CAST(timestamp AS DATE) AS window_start_date,
    latitude,
    longitude,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
),

UnnestedFireDates AS (
  SELECT
    window_start_date,
    SAFE_CAST(TRIM(d) AS DATE) AS fire_date
  FROM ParsedRows,
  UNNEST(SPLIT(IFNULL(clean_fire_dates, ''), ',')) AS d
  WHERE TRIM(d) != ''
)

SELECT
  COUNT(*)                                                          AS parsed_fire_date_rows,
  COUNTIF(fire_date IS NOT NULL AND fire_date <  window_start_date) AS past_fire_date_rows,
  COUNTIF(fire_date IS NOT NULL AND fire_date =  window_start_date) AS same_day_fire_date_rows,
  COUNTIF(fire_date IS NOT NULL AND fire_date >  window_start_date) AS future_fire_date_rows,
  SAFE_DIVIDE(
    COUNTIF(fire_date IS NOT NULL AND fire_date > window_start_date) * 100.0,
    COUNT(*)
  )                                                                 AS pct_future_fire_date_rows
FROM UnnestedFireDates;


-- =============================================================================
-- Section 2: Sample rows where fire_date > window_start_date
-- =============================================================================
-- 50-row eyeball sample so teammates can confirm the finding directly. The
-- combination of (timestamp, window_start_date, latitude, longitude,
-- fire_date, historical_fire_dates) makes the leakage risk concrete.

WITH ParsedRows AS (
  SELECT
    timestamp,
    CAST(timestamp AS DATE) AS window_start_date,
    latitude,
    longitude,
    historical_fire_dates,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
),

UnnestedFireDates AS (
  SELECT
    timestamp,
    window_start_date,
    latitude,
    longitude,
    historical_fire_dates,
    SAFE_CAST(TRIM(d) AS DATE) AS fire_date
  FROM ParsedRows,
  UNNEST(SPLIT(IFNULL(clean_fire_dates, ''), ',')) AS d
  WHERE TRIM(d) != ''
)

SELECT
  timestamp,
  window_start_date,
  latitude,
  longitude,
  fire_date,
  historical_fire_dates
FROM UnnestedFireDates
WHERE fire_date IS NOT NULL
  AND fire_date > window_start_date
ORDER BY window_start_date, latitude, longitude
LIMIT 50;
