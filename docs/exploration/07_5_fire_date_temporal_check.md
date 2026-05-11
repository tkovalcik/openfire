# Exploration 07.5 — Fire-date temporal sanity check

## 1. Purpose of the check

Confirm whether the `historical_fire_dates` column on
`silver_features_all_years` represents **past** fire history relative to
each row's `timestamp`, or whether it is the cell's **full** fire-date
list (past *and* future relative to the row). The answer matters because
the same column is used both for label construction
(`burned_in_next_15_days` looks forward) and as the input for any
fire-history feature we build (which must look backward only).

The companion read-only SQL is at
`sql/exploration/07_5_fire_date_temporal_check.sql`.

## 2. What we discovered

`historical_fire_dates` can include fire dates that occur **after** the
row's `timestamp`. A concrete example from silver:

- `timestamp = 2017-09-01`
- `historical_fire_dates` includes `2021-11-11`

In other words, the column is the full set of fire dates known for that
grid cell, not a row-aware "history as of this timestamp". The same list
appears on every row for that cell because the join in
`data_pipelines/04_merge_silver_years.sql` uses
`LEFT JOIN fire_history_by_cell USING (latitude, longitude)` — there is
no temporal filter at the join.

## 3. Why this is not automatically a bug

The pipeline needs forward-looking fire dates to construct the supervised
target. The production target,
`burned_in_next_15_days` in
`data_pipelines/05_engineer_gold_features.sql`, is defined as:

> `LOGICAL_OR(fire_date BETWEEN window_start_date + 1 day AND window_start_date + 15 days)`

For that aggregation to work, the input array must contain dates from
**after** `window_start_date`. Filtering `historical_fire_dates` to
past-only at the silver layer would silently zero out the label.

Step 05 also derives `days_since_last_burn` from the same column, but
gates it on `fire_date < window_start_date`, so that feature is safe.

## 4. Why it is a leakage risk

Anywhere we use `historical_fire_dates` (or anything derived from it)
**without** a temporal filter, future fire activity leaks into the
features the model trains on. Examples:

- Passing the raw `historical_fire_dates` string directly to a model,
  e.g. as an embedded list or a count of commas. Each row would carry
  knowledge of fires that haven't happened yet at prediction time.
- Computing `prior_fire_count_all_time` as
  `ARRAY_LENGTH(SPLIT(historical_fire_dates, ','))` without filtering by
  `fire_date < window_start_date`. The model would learn that "this cell
  appears 27 times in the FRAP record" — a perfect proxy for "this cell
  will eventually burn in the prediction window."
- Aggregating with no `<` filter for any rolling / density metric.

The leak is silent: training metrics look great, validation metrics
dip, and production performance collapses because the leak vanishes at
serving time.

## 5. Safe usage

- Use future fire dates **only** in the construction of supervised
  labels (`burned_in_next_15_days`, future incidence indicators, etc.).
- For every model **feature**, parse `historical_fire_dates` first and
  then filter to `fire_date < window_start_date` before counting,
  averaging, or otherwise aggregating.
- The pattern already used by
  `data_pipelines/05_engineer_gold_features.sql` for
  `days_since_last_burn` is correct and worth copying:

```sql
SELECT MAX(SAFE_CAST(TRIM(fire_date) AS DATE))
FROM UNNEST(SPLIT(clean_fire_dates, ',')) AS fire_date
WHERE SAFE_CAST(TRIM(fire_date) AS DATE) < window_start_date
```

## 6. Unsafe usage

- Do **not** pass the raw `historical_fire_dates` string (or any naive
  encoding of it) to the model.
- Do **not** compute "all-time fire counts" with
  `ARRAY_LENGTH(SPLIT(historical_fire_dates, ','))` or equivalent —
  that count includes future dates ~7% of the time.
- Do **not** join `fire_history_by_cell` with no temporal filter and
  then aggregate without `< window_start_date`.
- Do **not** assume that a `JOIN ... USING (latitude, longitude)`
  conveys any time semantics. The join is purely spatial.

## 7. Observed BigQuery result

From running Section 1 of `07_5_fire_date_temporal_check.sql` on
`msds603-mlops-project.openfire_features.silver_features_all_years`:

| Metric | Value |
| --- | --- |
| `parsed_fire_date_rows` | 25,412,083 |
| `past_fire_date_rows` | 23,576,772 |
| `same_day_fire_date_rows` | 966 |
| `future_fire_date_rows` | 1,834,345 |
| `pct_future_fire_date_rows` | ~7.218% |

So roughly **7.2%** of the parsed (silver row, fire_date) pairs reference
a fire that occurs strictly **after** the row's `timestamp`. The
same-day count (966) is negligible but worth noting so we can pick a
consistent convention (we treat `=` as future-side, matching the
production target window which excludes day 0).

## 8. Recommended naming / interpretation

The column name `historical_fire_dates` is misleading. A more accurate
name is something like `all_fire_dates_by_cell` — it is per-cell, not
per-cell-per-time. Until we are willing to touch the production pipeline
SQL, the recommendation is:

- In all new **feature** code, mentally read the column as
  `all_fire_dates_by_cell` and require an explicit
  `fire_date < window_start_date` filter at the call site.
- If we later promote any of this work into the production pipeline,
  consider renaming the column on the gold side (e.g. attach a derived
  `prior_fire_dates_array` that is pre-filtered) and updating the
  documentation in `data_pipelines/README.md` so the next teammate does
  not have to rediscover this.

## 9. Implication for exploration 08

The next exploration file (`08_*` interaction features) **must not**
output raw `historical_fire_dates` as a model feature, and every
fire-history-derived column it produces must filter parsed fire dates
with `fire_date < window_start_date` before any aggregation. This
applies to both standalone fire-history counts and to interaction
features (e.g. `prior_fire_count_5yr × dryness`, `has_prior_fire_history
× is_fire_season`).

The earlier file `06_fire_history_feature_candidates.sql` already
follows this rule for every feature column; treat it as the reference
pattern when writing 08.
