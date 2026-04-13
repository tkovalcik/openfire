WITH ParsedData AS (
  SELECT 
    *,
    CAST(timestamp AS DATE) AS window_start_date,
    NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
  FROM `msds603-mlops-project.openfire_features.silver_features_all_years`
),

TargetEngineering AS (
  SELECT
    *,
    COALESCE((
      SELECT LOGICAL_OR(SAFE_CAST(TRIM(fire_date) AS DATE) BETWEEN DATE_ADD(window_start_date, INTERVAL 1 DAY) AND DATE_ADD(window_start_date, INTERVAL 15 DAY))
      FROM UNNEST(SPLIT(clean_fire_dates, ',')) AS fire_date
    ), FALSE) AS burned_in_next_15_days,

    (
      SELECT DATE_DIFF(window_start_date, MAX(SAFE_CAST(TRIM(fire_date) AS DATE)), DAY)
      FROM UNNEST(SPLIT(clean_fire_dates, ',')) AS fire_date
      WHERE SAFE_CAST(TRIM(fire_date) AS DATE) < window_start_date
    ) AS days_since_last_burn
  FROM ParsedData
),

LagEngineering AS (
  SELECT
    *,
    -- NDVI (Vegetation) Trends (5d, 15d, 30d, 60d)
    mean_NDVI - LAG(mean_NDVI, 1) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndvi_change_5d,
    mean_NDVI - LAG(mean_NDVI, 3) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndvi_change_15d,
    mean_NDVI - LAG(mean_NDVI, 6) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndvi_change_30d,
    mean_NDVI - LAG(mean_NDVI, 12) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndvi_change_60d,

    -- NDWI (Moisture) Trends
    mean_NDWI - LAG(mean_NDWI, 1) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndwi_change_5d,
    mean_NDWI - LAG(mean_NDWI, 3) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndwi_change_15d,
    mean_NDWI - LAG(mean_NDWI, 6) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndwi_change_30d,
    mean_NDWI - LAG(mean_NDWI, 12) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS ndwi_change_60d,
    
    -- Temperature Trends
    gridmet_temp_max - LAG(gridmet_temp_max, 1) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS temp_change_5d,
    gridmet_temp_max - LAG(gridmet_temp_max, 3) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS temp_change_15d,
    gridmet_temp_max - LAG(gridmet_temp_max, 6) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS temp_change_30d,
    
    -- Precipitation Trends
    gridmet_precip_sum - LAG(gridmet_precip_sum, 3) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS precip_change_15d,
    gridmet_precip_sum - LAG(gridmet_precip_sum, 6) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS precip_change_30d,
    gridmet_precip_sum - LAG(gridmet_precip_sum, 12) OVER(PARTITION BY latitude, longitude ORDER BY window_start_date) AS precip_change_60d

  FROM TargetEngineering
)

SELECT 
  window_start_date,
  latitude,
  longitude,
  burned_in_next_15_days,
  COALESCE(days_since_last_burn, 9999) AS days_since_last_burn, 
  
  ndvi_change_5d, ndvi_change_15d, ndvi_change_30d, ndvi_change_60d,
  ndwi_change_5d, ndwi_change_15d, ndwi_change_30d, ndwi_change_60d,
  temp_change_5d, temp_change_15d, temp_change_30d,
  precip_change_15d, precip_change_30d, precip_change_60d,
  
  mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
  B2, B3, B4, B8, B11, B12,
  mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
  neighborhood_ndvi_10km, neighborhood_ndwi_10km, neighborhood_nbr_10km,
  gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max

FROM LagEngineering
WHERE ndvi_change_60d IS NOT NULL
ORDER BY latitude, longitude, window_start_date;