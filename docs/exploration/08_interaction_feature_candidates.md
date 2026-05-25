# Exploration 08 — Interaction feature candidates

## 1. Purpose of this SQL

`sql/exploration/08_interaction_feature_candidates.sql` is a read-only
BigQuery script that prototypes a model-oriented feature set on top of
the merged silver table. It is meant to be reviewed in the BigQuery
console, not committed into the production pipeline. Once the team
agrees that a candidate is worth keeping, the relevant CTE / expression
can be lifted into `data_pipelines/05_engineer_gold_features.sql`.

The query covers seasonality, weather stress, vegetation/fuel,
fire-history (past-only), interaction terms across those families, and
LAG-based temporal trends — all derived from columns that already exist
on `silver_features_all_years` per the inventory in
`docs/exploration/07_silver_feature_inventory.md`.

## 2. Why these features are meaningful for wildfire prediction

Wildfire ignitions and rapid spread are driven by the simultaneous
presence of three factors: dry/abundant fuel, hot/dry/windy weather,
and an ignition source (often correlated with prior fire activity in
the region). Single-column features partially capture each factor;
**interaction features** are what let a tree model see "hot AND dry AND
windy AND lots of fuel" as a single split.

- `fuel_x_weather_stress` and `fuel_x_wind` capture the standard
  fire-triangle composition: standing biomass × atmospheric demand /
  spread potential.
- `vegetation_dryness_x_heat` and `dryness_x_wind` highlight specific
  bad combinations that pure averages dilute.
- `prior_fire_x_*` capture the well-known recurrence pattern: cells
  that have burned recently tend to burn again.
- `recent_fire_x_dryness` carries an explicit "fresh burn scar in dry
  conditions" signal that's relevant for short-cycle reburn risk.
- LAG-based deltas on NDVI, NBR, precipitation, and temperature give
  the model access to short-term *change* on top of current levels.

## 3. Feature groups

### Seasonality
- `month_of_year` (1–12)
- `is_fire_season` (BOOL, June–November in California)
- `days_since_fire_season_start` (signed; 0 on June 1)
- `days_until_fire_season_end` (signed; 0 on November 30)

Trees can split directly on `month_of_year`; the signed offsets give
non-tree consumers a smooth way to encode proximity to the season
edges.

### Weather stress
- `heat_stress_score` — `gridmet_temp_max / 320.0` (Kelvin scale).
  If your warehouse stores temperature in Celsius, change the divisor.
- `dryness_score` —
  `(1 - humidity_min / 100) * 1 / (1 + precip_sum)`. Bounded ~ (0, 1].
- `wind_stress_score` — `gridmet_wind_max / 20.0` (assumes m/s).
- `weather_stress_score` — equal-weighted average of the three.

These are kept transparent on purpose: each is one expression, easy to
review or tune, and uses `SAFE_DIVIDE` / `COALESCE` so the query never
fails on missing values.

### Vegetation / fuel
- `fuel_load_score` — average of `mean_NDVI` and `mean_EVI`.
- `vegetation_dryness_score` — `mean_NDVI * (1 - mean_NDWI)`. High when
  there is lots of vegetation that is also moisture-stressed.
- `burn_scar_or_low_moisture_signal` — `(-1 * mean_NBR) + (-1 * mean_NDWI)`.
  Higher score = more likely a recent burn scar / dry pixel.

### Pure-feature interactions
- `fuel_x_weather_stress = fuel_load_score * weather_stress_score`
- `fuel_x_wind = fuel_load_score * wind_stress_score`
- `dryness_x_wind = dryness_score * wind_stress_score`
- `vegetation_dryness_x_heat = vegetation_dryness_score * heat_stress_score`
- `nbr_x_weather_stress = burn_scar_or_low_moisture_signal * weather_stress_score`

### Fire-history (past-only)
- `prior_fire_count_all_time`, `prior_fire_count_10yr`
- `has_prior_fire_history` (BOOL)
- `days_since_last_burn_candidate` (NULL if cell never burned before
  `window_start_date`)

Every aggregation here filters parsed fire dates to
`fire_date < window_start_date` before counting or taking `MAX`.

### Fire-history interactions
- `prior_fire_x_weather_stress = prior_fire_count_10yr * weather_stress_score`
- `prior_fire_x_fuel_load = prior_fire_count_10yr * fuel_load_score`
- `recent_fire_x_dryness = dryness_score` when
  `days_since_last_burn_candidate <= 3650` (10 years), else 0. Captures
  short-cycle reburn risk.

### Temporal trends (LAG)
Computed with
`PARTITION BY CAST(latitude AS STRING), CAST(longitude AS STRING)
ORDER BY window_start_date` to match the production convention from
`data_pipelines/05_engineer_gold_features.sql` (BigQuery's window
functions in this project always wrap lat/lon in `CAST(... AS STRING)`;
see `data_pipelines/README.md` "Gotchas").

- `ndvi_prev_observation`, `ndvi_change_from_prev`
- `nbr_change_from_prev`
- `precip_prev_observation`, `precip_change_from_prev`
- `temp_change_from_prev`

Because `BaseRows` is filtered to a single year for interactive testing,
the first observation per cell will have NULL LAG values (no prior
window in scope). Drop the year filter when running on the full
dataset.

## 4. Leakage rule

`historical_fire_dates` on silver represents **all known fire dates for
the cell** — past *and* future relative to the row's `timestamp`. The
inspection in `docs/exploration/07_5_fire_date_temporal_check.md`
confirmed that ~7.2% of parsed fire-date references are future relative
to the row.

This file enforces the leakage rule three ways:

1. **Raw `historical_fire_dates` is never projected to the final
   output.** It is parsed inside `BaseRows` / `FireDateArrays` and
   discarded.
2. **Every fire-history feature** in `FireHistoryAggregates` filters on
   `fire_date < window_start_date` before aggregating. There is no
   "all-time, no-filter" count anywhere.
3. **Only `burned_in_next_15_days_candidate`** (the target candidate) is
   allowed to look forward, using the production convention
   `BETWEEN window_start_date + 1 DAY AND window_start_date + 15 DAY`.

If any future feature added here starts depending on
`future_fire_date_count`, that's a leak — refuse to merge it.

## 5. How to run it in BigQuery

The SQL file is now structured as **multiple standalone statements**,
not one big query. Each section is a complete `WITH ... SELECT;`
statement that can be highlighted and run by itself in the BigQuery
console. The CTE chain is intentionally repeated across sections
because BigQuery scopes CTEs to a single statement.

1. Open the BigQuery console for the `${GCP_PROJECT_ID}` project.
2. Open `sql/exploration/08_interaction_feature_candidates.sql` in
   the editor.
3. Read **Section 0** at the top — it explains the run order and
   leakage rule.
4. **Highlight one section at a time** (between two `=====` banner
   comments) and click **Run**. Do not try to execute the whole file
   in one go.
5. The `BaseRows` CTE in every section filters to 2019 by default
   (`WHERE DATE(timestamp) BETWEEN DATE '2019-01-01' AND DATE
   '2019-12-31'`). Change the year for spot checks. To produce the
   full dataset, drop the `WHERE` clause **in every section you run**
   (and drop the trailing `LIMIT` in Section 1 / Section 5).
6. If your `gridmet_temp_max` is in Celsius (not Kelvin) or
   `gridmet_wind_max` is in mph (not m/s), change the divisors in the
   weather-stress block (`/ 320.0` and `/ 20.0`). Tree models will
   still split correctly on un-normalized inputs — the divisors are
   mainly there to keep the blended `weather_stress_score`
   interpretable.

### How to run section-by-section

| Section | Returns | When to run |
| --- | --- | --- |
| **0** | Comment header only — no SQL. | Read first. |
| **1 — Feature candidate sample** | 1000 rows of identifiers + every feature candidate + target + sanity counters. | Smoke test. Run first to confirm the query executes and columns look reasonable. |
| **2 — Feature distribution summary** | One row of aggregate stats: `total_rows`, target rate, avg/min/max for `weather_stress_score`, `fuel_load_score`, `fuel_x_weather_stress`, fire-history coverage, LAG coverage, `prior_fire_count_10yr`, `days_since_last_burn_candidate`. | After Section 1, to confirm features are non-empty and numerically reasonable. |
| **3 — Leakage guardrail summary** | One row of fire-date split: total / past / future references, rows with future fire dates, rows with positive target, percentages. | Run whenever you change anything in the fire-history CTEs. Future-fire-date counts are expected here (target-only); model features must not depend on them. |
| **4 — Temporal feature coverage** | One row of LAG coverage: how many rows have `ndvi_prev_observation`, `*_change_from_prev`, plus avg/min/max of each delta. | Run if you want to confirm temporal LAGs are populated. The first window per cell is always NULL by construction. |
| **5 — High-risk sample rows** | 100 rows ordered by `weather_stress_score DESC, fuel_x_weather_stress DESC, prior_fire_x_weather_stress DESC` with the columns most useful for eyeballing. | Run last for a manual gut-check that high-score rows look plausibly fire-prone. |
| **6** | Comment-only feature plan — no SQL. | Read after the team has compared candidates. |

If you accidentally run the whole file at once, BigQuery will execute
each statement sequentially and bill for each one. Nothing destructive
will happen (every section is read-only), but you'll pay 5× the cost
versus running just the section you actually need.

## 6. How to interpret useful results

After running, eyeball the LIMIT 1000 sample and look for:

- **Non-trivial spread** in each new score. A score that's effectively
  constant won't help the model and probably has an arithmetic bug.
- **`weather_stress_score`** higher in summer and lower in winter, with
  obvious peaks around heat-wave dates.
- **`fuel_x_weather_stress`** higher in green, hot, dry conditions.
  This is the headline fire-risk composite.
- **`prior_fire_x_*`** non-zero only on cells that have actually burned
  before. If everything is zero, your join may not be matching cells
  with fire history (cross-check
  `sql/exploration/07_silver_feature_inventory.sql` Section 6).
- **LAG columns** non-NULL after the first ~one window per cell. If
  every LAG column is NULL, the year filter is too narrow or the
  partition keys aren't matching across rows.
- **Sanity counters** (`parsed_fire_date_count`,
  `past_fire_date_count`, `future_fire_date_count`) reasonable for
  cells with a populated `historical_fire_dates`. If
  `future_fire_date_count > 0` rows are co-occurring with non-zero
  feature values that shouldn't depend on future fires, that is a
  leakage bug.

## 7. What features may be promoted later into 05

The likely first promotions, in rough order of value-per-line-of-code:

1. **Fire-history counts** (`prior_fire_count_5yr` /
   `prior_fire_count_10yr`, `has_prior_fire_history`) — strong, cheap,
   already follow the leakage rule, complement the existing
   `days_since_last_burn`.
2. **Seasonality** (`month_of_year`, `is_fire_season`,
   `days_since_fire_season_start`) — trivial to compute, useful even
   for the simplest models.
3. **`fuel_x_weather_stress`** and **`vegetation_dryness_x_heat`** —
   the cleanest of the interaction terms; both reuse columns the
   pipeline already loads.
4. **Temporal `*_change_from_prev` columns** — natural extension of
   the existing 5d/15d/30d/60d `*_change_*` features in step 05; only
   add if a previous-observation delta proves more predictive than the
   fixed-step LAGs already in gold.

Defer until later (or drop entirely if not predictive):

- `burn_scar_or_low_moisture_signal` and `nbr_x_weather_stress` —
  conceptually overlap with NBR / NDWI individually; check feature
  importance before promoting.
- `recent_fire_x_dryness` — useful only if the team can defend the
  10-year "recent" threshold with data, otherwise it adds noise.
- The min/max scaling divisors (320, 20, etc.) need to be confirmed
  against the actual GRIDMET column units in our warehouse before
  promotion. Tree models won't care, but a future linear / NN model
  consuming the same gold table will.
