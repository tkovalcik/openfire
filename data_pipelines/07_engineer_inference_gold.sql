-- Engineer gold features for ONE target window and MERGE into
-- gold_features_inference. Idempotent: re-running for the same @target_date
-- updates rows in place rather than appending. Never destructive against the
-- training `gold_features` table.
--
-- Cost optimization: read only the silver rows whose window_start_date falls
-- in [@target_date - 60d, @target_date]. That window is large enough to
-- compute the 60-day lag (12 prior 5-day windows + the target) for each
-- cell, but small enough to scan ~2.5% of silver instead of all of it.
--
-- @target_date is bound at job submission time as a BigQuery DATE parameter.
-- Caller must ensure @target_date is on the 5-day grid (validated upstream
-- in src/pipelines/run_inference_pipeline.py via is_grid_date).

MERGE `msds603-mlops-project.openfire_features.gold_features_inference` AS dst
USING (
  WITH ParsedSilver AS (
    SELECT
      * EXCEPT(`system:index`),
      CAST(timestamp AS DATE) AS window_start_date,
      NULLIF(historical_fire_dates, 'None') AS clean_fire_dates
    FROM `msds603-mlops-project.openfire_features.silver_features_*` s
    LEFT JOIN `msds603-mlops-project.openfire_features.fire_history_by_cell` f
      USING (latitude, longitude)
    WHERE _TABLE_SUFFIX NOT LIKE '%_dedup'
      AND CAST(s.timestamp AS DATE) BETWEEN DATE_SUB(@target_date, INTERVAL 60 DAY) AND @target_date
  ),

  Deduped AS (
    SELECT *
    FROM ParsedSilver
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING), window_start_date
      ORDER BY mean_NDVI
    ) = 1
  ),

  TargetEngineering AS (
    SELECT
      *,
      COALESCE((
        SELECT LOGICAL_OR(
          SAFE_CAST(TRIM(fire_date) AS DATE) BETWEEN DATE_ADD(window_start_date, INTERVAL 1 DAY)
                                                AND DATE_ADD(window_start_date, INTERVAL 15 DAY)
        )
        FROM UNNEST(SPLIT(clean_fire_dates, ',')) AS fire_date
      ), FALSE) AS burned_in_next_15_days,

      (
        SELECT DATE_DIFF(window_start_date, MAX(SAFE_CAST(TRIM(fire_date) AS DATE)), DAY)
        FROM UNNEST(SPLIT(clean_fire_dates, ',')) AS fire_date
        WHERE SAFE_CAST(TRIM(fire_date) AS DATE) < window_start_date
      ) AS days_since_last_burn_raw
    FROM Deduped
  ),

  LagEngineering AS (
    SELECT
      *,
      mean_NDVI - LAG(mean_NDVI,  1) OVER w AS ndvi_change_5d,
      mean_NDVI - LAG(mean_NDVI,  3) OVER w AS ndvi_change_15d,
      mean_NDVI - LAG(mean_NDVI,  6) OVER w AS ndvi_change_30d,
      mean_NDVI - LAG(mean_NDVI, 12) OVER w AS ndvi_change_60d,

      mean_NDWI - LAG(mean_NDWI,  1) OVER w AS ndwi_change_5d,
      mean_NDWI - LAG(mean_NDWI,  3) OVER w AS ndwi_change_15d,
      mean_NDWI - LAG(mean_NDWI,  6) OVER w AS ndwi_change_30d,
      mean_NDWI - LAG(mean_NDWI, 12) OVER w AS ndwi_change_60d,

      gridmet_temp_max - LAG(gridmet_temp_max,  1) OVER w AS temp_change_5d,
      gridmet_temp_max - LAG(gridmet_temp_max,  3) OVER w AS temp_change_15d,
      gridmet_temp_max - LAG(gridmet_temp_max,  6) OVER w AS temp_change_30d,
      gridmet_temp_max - LAG(gridmet_temp_max, 12) OVER w AS temp_change_60d,

      gridmet_precip_sum - LAG(gridmet_precip_sum,  3) OVER w AS precip_change_15d,
      gridmet_precip_sum - LAG(gridmet_precip_sum,  6) OVER w AS precip_change_30d,
      gridmet_precip_sum - LAG(gridmet_precip_sum, 12) OVER w AS precip_change_60d
    FROM TargetEngineering
    WINDOW w AS (
      PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING)
      ORDER BY window_start_date
    )
  )

  SELECT
    window_start_date,
    latitude,
    longitude,
    burned_in_next_15_days,
    COALESCE(days_since_last_burn_raw, 9999) AS days_since_last_burn,

    ndvi_change_5d, ndvi_change_15d, ndvi_change_30d, ndvi_change_60d,
    ndwi_change_5d, ndwi_change_15d, ndwi_change_30d, ndwi_change_60d,
    temp_change_5d, temp_change_15d, temp_change_30d, temp_change_60d,
    precip_change_15d, precip_change_30d, precip_change_60d,

    mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
    B2, B3, B4, B8, B11, B12,
    mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
    gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,

    CURRENT_TIMESTAMP() AS engineered_at
  FROM LagEngineering
  WHERE window_start_date = @target_date
    AND ndvi_change_60d IS NOT NULL
) AS src
ON dst.latitude = src.latitude
   AND dst.longitude = src.longitude
   AND dst.window_start_date = src.window_start_date
WHEN MATCHED THEN UPDATE SET
  burned_in_next_15_days = src.burned_in_next_15_days,
  days_since_last_burn   = src.days_since_last_burn,
  ndvi_change_5d  = src.ndvi_change_5d,  ndvi_change_15d  = src.ndvi_change_15d,
  ndvi_change_30d = src.ndvi_change_30d, ndvi_change_60d  = src.ndvi_change_60d,
  ndwi_change_5d  = src.ndwi_change_5d,  ndwi_change_15d  = src.ndwi_change_15d,
  ndwi_change_30d = src.ndwi_change_30d, ndwi_change_60d  = src.ndwi_change_60d,
  temp_change_5d  = src.temp_change_5d,  temp_change_15d  = src.temp_change_15d,
  temp_change_30d = src.temp_change_30d, temp_change_60d  = src.temp_change_60d,
  precip_change_15d = src.precip_change_15d,
  precip_change_30d = src.precip_change_30d,
  precip_change_60d = src.precip_change_60d,
  mean_elevation = src.mean_elevation, mean_slope = src.mean_slope,
  mean_cos_aspect = src.mean_cos_aspect, mean_sin_aspect = src.mean_sin_aspect,
  B2 = src.B2, B3 = src.B3, B4 = src.B4, B8 = src.B8, B11 = src.B11, B12 = src.B12,
  mean_NDVI = src.mean_NDVI, mean_EVI = src.mean_EVI,
  mean_NDWI = src.mean_NDWI, mean_NBR = src.mean_NBR,
  gridmet_temp_max = src.gridmet_temp_max, gridmet_humidity_min = src.gridmet_humidity_min,
  gridmet_precip_sum = src.gridmet_precip_sum, gridmet_wind_max = src.gridmet_wind_max,
  engineered_at = src.engineered_at
WHEN NOT MATCHED THEN INSERT (
  window_start_date, latitude, longitude,
  burned_in_next_15_days, days_since_last_burn,
  ndvi_change_5d, ndvi_change_15d, ndvi_change_30d, ndvi_change_60d,
  ndwi_change_5d, ndwi_change_15d, ndwi_change_30d, ndwi_change_60d,
  temp_change_5d, temp_change_15d, temp_change_30d, temp_change_60d,
  precip_change_15d, precip_change_30d, precip_change_60d,
  mean_elevation, mean_slope, mean_cos_aspect, mean_sin_aspect,
  B2, B3, B4, B8, B11, B12,
  mean_NDVI, mean_EVI, mean_NDWI, mean_NBR,
  gridmet_temp_max, gridmet_humidity_min, gridmet_precip_sum, gridmet_wind_max,
  engineered_at
) VALUES (
  src.window_start_date, src.latitude, src.longitude,
  src.burned_in_next_15_days, src.days_since_last_burn,
  src.ndvi_change_5d, src.ndvi_change_15d, src.ndvi_change_30d, src.ndvi_change_60d,
  src.ndwi_change_5d, src.ndwi_change_15d, src.ndwi_change_30d, src.ndwi_change_60d,
  src.temp_change_5d, src.temp_change_15d, src.temp_change_30d, src.temp_change_60d,
  src.precip_change_15d, src.precip_change_30d, src.precip_change_60d,
  src.mean_elevation, src.mean_slope, src.mean_cos_aspect, src.mean_sin_aspect,
  src.B2, src.B3, src.B4, src.B8, src.B11, src.B12,
  src.mean_NDVI, src.mean_EVI, src.mean_NDWI, src.mean_NBR,
  src.gridmet_temp_max, src.gridmet_humidity_min, src.gridmet_precip_sum, src.gridmet_wind_max,
  src.engineered_at
);
