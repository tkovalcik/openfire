-- Checkpoint 01
-- Purpose: verify there are no duplicate rows per latitude/longitude/date
-- Scope: first run on silver_features_2024 as the template year

SELECT
  CAST(timestamp AS DATE) AS window_start_date,
  latitude,
  longitude,
  COUNT(*) AS row_count
FROM `msds603-mlops-project.openfire_features.silver_features_2024`
GROUP BY 1, 2, 3
HAVING COUNT(*) > 1
ORDER BY row_count DESC
LIMIT 50;
