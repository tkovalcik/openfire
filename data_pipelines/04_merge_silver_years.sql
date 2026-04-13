-- This script dynamically merges all yearly GEE exports into a single Silver table.
-- It uses the '*' wildcard, so as you export new years (2019, 2020), 
-- you just re-run this script and it automatically includes them!

CREATE OR REPLACE TABLE `msds603-mlops-project.openfire_features.silver_features_all_years` AS
SELECT * FROM `msds603-mlops-project.openfire_features.silver_features_*_gee_import`;