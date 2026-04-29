-- DDL for inference-side tables. Idempotent (CREATE TABLE IF NOT EXISTS) — safe
-- to re-run. Never use CREATE OR REPLACE / DROP / DELETE here; the inference
-- pipeline must not be able to nuke training tables (`gold_features`,
-- `silver_features_*`, `fire_history_by_cell`) by accident.
--
-- gold_features_inference: per the Phase 4 Step 4 decision (Option A in
-- docs/phase4_inspection.md), inference engineers gold features into its own
-- table rather than sharing with training. Schema mirrors gold_features
-- exactly so any consumer can swap the table reference; partitioned and
-- clustered for the per-window write / per-cell read pattern.

-- predictions_history: one row per (cell, window) per scoring run. Per Step 0
-- decision, primary key is (latitude, longitude, window_start_date) and the
-- table is partitioned + clustered for the per-window write / per-cell read
-- pattern. inference_run_at provides timestamp provenance; model_version
-- pins each row to a specific MLflow registered version so promoting a new
-- model gives clean A/B history.

CREATE TABLE IF NOT EXISTS `msds603-mlops-project.openfire_features.predictions_history`
(
  latitude          FLOAT64   NOT NULL,
  longitude         FLOAT64   NOT NULL,
  window_start_date DATE      NOT NULL,
  risk_probability  FLOAT64   NOT NULL,
  predicted_label   INT64     NOT NULL,
  model_name        STRING    NOT NULL,
  model_version     STRING    NOT NULL,
  inference_run_at  TIMESTAMP NOT NULL
)
PARTITION BY window_start_date;


CREATE TABLE IF NOT EXISTS `msds603-mlops-project.openfire_features.gold_features_inference`
(
  window_start_date     DATE      NOT NULL,
  latitude              FLOAT64   NOT NULL,
  longitude             FLOAT64   NOT NULL,

  -- Target column carried through for parity checks against training (it's
  -- only meaningful for windows old enough to have realized labels).
  burned_in_next_15_days BOOL,
  days_since_last_burn   INT64,

  ndvi_change_5d   FLOAT64, ndvi_change_15d  FLOAT64, ndvi_change_30d  FLOAT64, ndvi_change_60d  FLOAT64,
  ndwi_change_5d   FLOAT64, ndwi_change_15d  FLOAT64, ndwi_change_30d  FLOAT64, ndwi_change_60d  FLOAT64,
  temp_change_5d   FLOAT64, temp_change_15d  FLOAT64, temp_change_30d  FLOAT64, temp_change_60d  FLOAT64,
  precip_change_15d FLOAT64, precip_change_30d FLOAT64, precip_change_60d FLOAT64,

  mean_elevation FLOAT64, mean_slope FLOAT64,
  mean_cos_aspect FLOAT64, mean_sin_aspect FLOAT64,

  B2 FLOAT64, B3 FLOAT64, B4 FLOAT64, B8 FLOAT64, B11 FLOAT64, B12 FLOAT64,
  mean_NDVI FLOAT64, mean_EVI FLOAT64, mean_NDWI FLOAT64, mean_NBR FLOAT64,

  gridmet_temp_max FLOAT64, gridmet_humidity_min FLOAT64,
  gridmet_precip_sum FLOAT64, gridmet_wind_max FLOAT64,

  -- Provenance: when this row was MERGEd into the inference table.
  engineered_at TIMESTAMP NOT NULL
)
PARTITION BY window_start_date;
