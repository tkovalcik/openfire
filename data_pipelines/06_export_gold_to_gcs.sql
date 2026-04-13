-- This script exports the final ML-ready Gold table directly to the GCS bucket.
-- It compresses the data into the Parquet format, which is highly optimized 
-- for the Pandas/XGBoost training pipeline.

EXPORT DATA OPTIONS(
  -- Replace 'your-openfire-bucket' with your actual GCS bucket name
  -- gs://openfire/openfire/datasets/gold
  uri='gs://openfire/openfire/datasets/gold/gold_features_*.parquet',
  format='PARQUET',
  compression='SNAPPY',
  overwrite=true
) AS
SELECT * FROM `msds603-mlops-project.openfire_features.gold_features`;