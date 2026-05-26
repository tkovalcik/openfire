-- Checkpoint 04
-- Purpose: inspect the target-label distribution in the gold feature table
--          before training a binary fire-risk classifier.
-- Scope: run after `data_pipelines/05_engineer_gold_features.sql` finishes.
--
-- Why this matters:
--   * Wildfire-occurrence labels are highly imbalanced (positives are a small
--     minority of cell-date rows). Knowing the exact ratio drives decisions
--     around class weighting, resampling, and the choice of evaluation metric
--     (PR-AUC vs ROC-AUC, threshold tuning, etc.).
--   * A sudden swing in this ratio between runs is a red flag — usually it
--     means the label window changed, the FRAP join broke, or a year was
--     dropped from the silver merge.
--
-- IMPORTANT: this query assumes the target column is named `target_burned`.
-- The current pipeline writes the label as `burned_in_next_15_days` (see
-- `data_pipelines/05_engineer_gold_features.sql`). If your gold table uses
-- a different name, edit the column name in BOTH places below.
--
-- Replace `PROJECT_ID.DATASET_ID.gold_features_2024` with your project /
-- dataset before running.

SELECT
  target_burned AS class_label,
  COUNT(*) AS row_count,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 4) AS percent_of_rows
FROM `PROJECT_ID.DATASET_ID.gold_features_2024`
GROUP BY target_burned
ORDER BY class_label;
