# Low-Count Snapshot Investigation: 2025-12-23 and 2025-12-28

**Backlog item:** issue_log.md #10.0 (read-only investigation)
**Date run:** 2026-05-03
**Status:** complete; ready for repair decision
**Run by:** sebaleksander@gmail.com (read-only via `bq` + `gcloud storage`)

---

## TL;DR

- **2025-12-23 is genuinely broken.** Silver has the canonical 65,687 rows. Gold-features-inference has only 37,098 (56.5%). Predictions-history has **zero rows**. The GCS GeoJSON snapshot reflects gold (37,098 features). The bug is in the **silver → gold materialization** for this single date — about 28,589 rows are dropped between layers.
- **2025-12-28 is healthy now.** Silver, gold, predictions-history, and the GCS snapshot all hold the canonical 65,687 rows / features. The UI exclusion is **stale**: probably added when the date was bad, never removed after repair.
- **Recommendation:** Remove `2025-12-28` from `frontend-socal/config.js` exclusions immediately. Diagnose and repair `2025-12-23` separately (silver→gold pipeline issue).

---

## Row counts (canonical invariant: 65,687)

| Source | 2025-12-23 | 2025-12-28 |
|---|---|---|
| Silver (`openfire_features.silver_features_2025`, `timestamp` IN suspect dates) | **65,687** ✅ | **65,687** ✅ |
| Gold features inference (`openfire_features.gold_features_inference`, `window_start_date` =) | **37,098** ⚠️ (56.5% of canonical) | **65,687** ✅ |
| Predictions history (`openfire_features.predictions_history`, `window_start_date` =) | **0 rows — partition absent** ❌ | **65,687** ✅ |
| GCS full GeoJSON (`gs://openfire/predictions/predictions_<YYYYMMDD>.geojson`) — feature count | **37,098** | **65,687** |
| GCS z8 variant — feature count | 6,183 | 10,948 |
| GCS z8 variant — manifest's `feature_count` field | 6,183 ✅ matches | 10,948 ✅ matches |
| GCS file size (full) | 8.48 MiB | 15.02 MiB |
| GCS object `updated_at` (manifest entry) | 2026-05-03 06:47:02 UTC | 2026-05-03 06:47:04 UTC |
| Model version (manifest) | `05cef3a7c514432db3a4d35f092cbdad` | `05cef3a7c514432db3a4d35f092cbdad` (same) |

Neighborhood scan of `predictions_history` confirms the gap is isolated:

```
2025-12-28  | 65687
2026-01-02  | 65687
```

`2025-12-23` partition **does not exist** in `predictions_history` (it would normally appear here as a separate row).

---

## Root cause analysis

### 2025-12-23 — stale gold partition; silver was backfilled after the MERGE ran

**The MERGE re-run today would produce 65,687 rows.** Confirmed by re-executing the source CTE from `data_pipelines/07_engineer_inference_gold.sql` against current silver:

| Date | Source rows at target | Pass `ndvi_change_60d IS NOT NULL` | Would be dropped |
|---|---|---|---|
| 2025-12-23 (re-run today) | 65,687 | **65,687** | 0 |

The gold partition currently in BQ is frozen at `engineered_at = 2026-04-29 19:47:30 UTC` (stale). 2025-12-28 and 2026-01-02 were re-engineered on 2026-05-02 and reflect the backfilled silver; **2025-12-23 was never re-run**.

Data flow with the failure point marked:
```
silver_features_2025  (65,687 rows, fully backfilled NOW)
    │
    ├─ silver→gold materialization
    │   ⚠️ last run 2026-04-29 against then-partial silver
    │   ⚠️ WHERE ndvi_change_60d IS NOT NULL silently dropped ~28,589 rows
    │   ⚠️ no row-count guard at this boundary
    ▼
gold_features_inference  (37,098, partition not refreshed since)
    │
    ├─ inference: assert_inference_cell_count guard fires → write rejected
    ▼
predictions_history  (0 rows; partition absent)
```

Evidence:
1. Silver has the full 65,687 rows for the target and every prior 5-day window in the 60-day range. No NULL `mean_NDVI`. Cell-key continuity is perfect across all 13 windows in the lag range (verified by counting distinct `(lat_key, lon_key)` per window).
2. **A live re-run of the MERGE source SQL produces 65,687 rows that all pass `ndvi_change_60d IS NOT NULL`.** Zero would be dropped.
3. Gold partition has `engineered_at = 2026-04-29`; neighbors 2025-12-28 and 2026-01-02 have `engineered_at = 2026-05-02` — they were re-engineered after silver was backfilled, 2025-12-23 wasn't.
4. Predictions-history has no partition for 2025-12-23 because `assert_inference_cell_count` (run_inference_pipeline.py:225) correctly rejected the 37,098-row gold. **The guard worked as designed; the upstream gold is the broken layer.**
5. The full GeoJSON (37,098 features, last modified 2026-04-29) was generated from the original partial gold. Manifest variants regenerated on 2026-05-03 still reflect the partial gold.

### 2025-12-28 — already healthy; UI exclusion is stale

All four data sources match at 65,687. The same model version produced both dates' snapshots (`05cef3a7…`), and the snapshots were updated within 2 seconds of each other on 2026-05-03. There is no current data deficit for this date. The UI exclusion in `frontend-socal/config.js` is no longer warranted — it was likely added when the date was bad and never removed after repair.

---

## Recommendations

### Immediate (low risk, high value)

1. **Remove `2025-12-28` from `frontend-socal/config.js` exclusions.** It's healthy. The exclusion is hiding a perfectly good window from the UI.
2. **Keep `2025-12-23` excluded for now.** The underlying gold partition is broken; it has 37,098 rows backing it and no `predictions_history` row. Showing a 56.5% snapshot is worse than hiding it.

### Repair (separate work item)

3. **Re-run `engineer_inference_gold` for 2025-12-23.** The MERGE is idempotent and the source SQL re-executed against current silver produces 65,687 rows. After it succeeds, the gold partition will hold the canonical row count.
4. **Re-run inference for the 2025-12-23 window.** With clean gold, `assert_inference_cell_count` will pass and `predictions_history` + a fresh GeoJSON snapshot will be written.
5. **Then remove `2025-12-23` from `frontend-socal/config.js` exclusions.**

### Code-level bugs found in ingestion (separate from this incident; ship as new backlog items)

6. **No row-count guard at the silver→gold boundary.**
   - `data_pipelines/07_engineer_inference_gold.sql` ends with `WHERE window_start_date = @target_date AND ndvi_change_60d IS NOT NULL`. Any cell whose 60-day lag isn't computable is silently dropped, with no warning, log, or assertion.
   - `src/pipelines/engineer_gold.py` reports `num_dml_affected_rows` to the logger but does not assert against the canonical 65,687 invariant. The downstream guard at `src/pipelines/run_inference_pipeline.py:225` only fires when gold is read for prediction — by then the partial partition is already persisted in BQ.
   - **Fix:** in `engineer_inference_gold(...)`, after the MERGE, query gold for `target_date` and call `assert_inference_cell_count(...)`. Raise on mismatch and roll back / mark the partition stale.

7. **No staleness detection for engineered partitions.**
   - The MERGE is idempotent on re-run, but nothing detects "silver was updated after gold was last engineered" and triggers a re-engineering.
   - **Fix:** add a check that compares `MAX(engineered_at)` in `gold_features_inference` for a window against the latest silver `loaded_at` / silver max ingest time for the relevant 60-day range. If silver is newer, mark gold as stale and re-engineer before predict.

8. **Silent row-count drop is hidden by SQL filter, not surfaced in code.**
   - The `IS NOT NULL` filter is the *intended* mechanism for partial-window edge cases (early dates with insufficient lag history). For mature dates it should never drop anything; today it does, silently. There's no metric / log line saying "MERGE produced N rows out of expected canonical_count."
   - **Fix:** in `engineer_gold.py`, log `affected_rows` *and* compare to the expected 65,687 with a WARNING when it diverges (even if you don't raise — at least make it visible).

9. **MERGE has no `WHEN NOT MATCHED BY SOURCE` clause.**
   - If a cell qualified once but stops qualifying on a later run (silver row removed/changed), the stale gold row remains. Less likely to bite, but worth noting.
   - **Fix:** consider `WHEN NOT MATCHED BY SOURCE THEN DELETE` for the target window only, OR use `DELETE … ; INSERT …` instead of MERGE for inference (since training stays in `gold_features`, not this table).

These four code-level findings are not in `docs/issue_log.md` today and are good candidates to add to the backlog as siblings of `#9.0` (training/inference parity).

---

## Reproduction (read-only)

```bash
PROJECT=msds603-mlops-project
DATES="('2025-12-23','2025-12-28')"

# BQ row counts
for table in silver_features_2025 gold_features_inference predictions_history; do
  echo "=== $table ==="
  if [ "$table" = "silver_features_2025" ]; then
    bq query --project_id=$PROJECT --use_legacy_sql=false --format=pretty \
      "SELECT timestamp AS d, COUNT(*) AS n FROM \`${PROJECT}.openfire_features.${table}\` WHERE timestamp IN $DATES GROUP BY 1 ORDER BY 1"
  else
    bq query --project_id=$PROJECT --use_legacy_sql=false --format=pretty \
      "SELECT window_start_date AS d, COUNT(*) AS n FROM \`${PROJECT}.openfire_features.${table}\` WHERE window_start_date IN $DATES GROUP BY 1 ORDER BY 1"
  fi
done

# GCS feature counts
for d in 20251223 20251228; do
  for v in "" _z8 _z9; do
    gcloud storage cp "gs://openfire/predictions/predictions_${d}${v}.geojson" "/tmp/p.geojson"
    python -c "import json; print('${d}${v}:', len(json.load(open('/tmp/p.geojson'))['features']))"
  done
done

# Manifest entries
gcloud storage cat gs://openfire/predictions/manifest.json | python -m json.tool | less
```
