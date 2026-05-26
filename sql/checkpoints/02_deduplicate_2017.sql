-- Checkpoint 02
-- Purpose: create a deduplicated working version of silver_features_2017
-- Dedup key: (timestamp, latitude, longitude)

CREATE OR REPLACE TABLE `msds603-mlops-project.openfire_features.silver_features_2017_dedup` AS
SELECT * EXCEPT(rn)
FROM (
  SELECT
    t.*,
    ROW_NUMBER() OVER (
      PARTITION BY
        timestamp,
        TO_JSON_STRING(STRUCT(latitude, longitude))
      ORDER BY timestamp
    ) AS rn
  FROM `msds603-mlops-project.openfire_features.silver_features_2017` t
)
WHERE rn = 1;

-- Verification 1: exact-key duplicate check
SELECT
  timestamp,
  latitude,
  longitude,
  COUNT(*) AS row_count
FROM `msds603-mlops-project.openfire_features.silver_features_2017_dedup`
GROUP BY 1, 2, 3
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 50;

-- Verification 2: cell-date duplicate check
SELECT
  CAST(timestamp AS DATE) AS window_start_date,
  latitude,
  longitude,
  COUNT(*) AS row_count
FROM `msds603-mlops-project.openfire_features.silver_features_2017_dedup`
GROUP BY 1, 2, 3
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 50;
