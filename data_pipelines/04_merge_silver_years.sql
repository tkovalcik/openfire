-- Merge yearly silver tables into a single all-years silver, deduplicating
-- on (latitude, longitude, timestamp) and joining in fire labels per cell.
--
-- Wildcard auto-picks up new years; _TABLE_SUFFIX filter drops any *_dedup
-- experimental tables so we don't double-count.
--
-- Dedup strategy: keep one row per (lat, lon, timestamp). For byte-identical
-- duplicates the tiebreaker doesn't matter; we ORDER BY mean_NDVI for stability.
--
-- We EXCEPT(`system:index`) because BigQuery rejects wildcard tables whose
-- schema contains flexible column names (anything with special chars like ":").
-- Downstream feature engineering doesn't need it.

CREATE OR REPLACE TABLE `msds603-mlops-project.openfire_features.silver_features_all_years` AS

WITH MergedSilver AS (
  SELECT * EXCEPT(`system:index`)
  FROM `msds603-mlops-project.openfire_features.silver_features_*`
  WHERE _TABLE_SUFFIX NOT LIKE '%_dedup'
),

DedupedSilver AS (
  SELECT *
  FROM MergedSilver
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING), timestamp
    ORDER BY mean_NDVI
  ) = 1
)

SELECT
  s.*,
  f.historical_fire_dates
FROM DedupedSilver s
LEFT JOIN `msds603-mlops-project.openfire_features.fire_history_by_cell` f
  USING (latitude, longitude);
