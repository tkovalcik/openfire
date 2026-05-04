# Data Pipeline Bugs Found During Snapshot Investigation

Companion to [docs/snapshot-investigation-2025-12-23-28.md](snapshot-investigation-2025-12-23-28.md). The investigation surfaced four ingestion-layer bugs in addition to the immediate 2025-12-23 incident. Filed here as backlog candidates.

| # | Severity | Where | Summary |
|---|---|---|---|
| 1 | High | [src/pipelines/engineer_gold.py](../src/pipelines/engineer_gold.py) | No row-count guard at silver→gold boundary (now partially addressed in this branch) |
| 2 | High | Silver→gold pipeline | No staleness detection when silver is backfilled after gold is engineered |
| 3 | Medium | [data_pipelines/07_engineer_inference_gold.sql](../data_pipelines/07_engineer_inference_gold.sql) + [src/pipelines/engineer_gold.py](../src/pipelines/engineer_gold.py) | Silent row-count drops in MERGE source CTE — no log/warn for divergence |
| 4 | Low | [data_pipelines/07_engineer_inference_gold.sql](../data_pipelines/07_engineer_inference_gold.sql) | MERGE has no `WHEN NOT MATCHED BY SOURCE` clause |

---

## Bug 1: No row-count guard at silver→gold boundary

**Status on this branch:** **partially addressed.** `engineer_inference_gold` in [src/pipelines/engineer_gold.py](../src/pipelines/engineer_gold.py) now post-counts the partition and raises if it diverges from the canonical `EXPECTED_INFERENCE_CELL_COUNT = 65_687`. The downstream guard at `run_inference_pipeline.py:225` is preserved.

**Why it matters.** Until this branch, the only row-count assertion lived after gold was read for prediction (`assert_inference_cell_count` in `run_inference_pipeline.py`). By the time it fired, the partial gold partition was already persisted in BigQuery. Result: 2025-12-23's gold partition has been frozen at 37,098 rows since 2026-04-29, even after silver was backfilled to 65,687.

**Remaining work.** The constant `EXPECTED_INFERENCE_CELL_COUNT` is now duplicated in `engineer_gold.py` and `run_inference_pipeline.py`. Extract to a shared module (`src/pipelines/invariants.py` or similar) on next pass.

## Bug 2: No staleness detection for engineered partitions

**Symptom.** When silver is backfilled (rows added or corrected for a window already in gold), nothing detects the divergence. The MERGE is idempotent on re-run but is never re-triggered automatically. Manual re-runs only.

**Concrete evidence from this incident.** `gold_features_inference.engineered_at` for 2025-12-23 is 2026-04-29 19:47:30 UTC. Neighboring partitions 2025-12-28 and 2026-01-02 have `engineered_at` of 2026-05-02 — they were re-engineered after silver was backfilled. 2025-12-23 was missed.

**Fix.** Add a check that compares `MAX(engineered_at)` for a gold partition against the latest silver ingest time covering the relevant 60-day lag range. If silver is newer, mark gold as stale and re-engineer before predict. Sibling concern to backlog #8.6 (alerting).

## Bug 3: Silent row-count drops in the MERGE

**Where.** [data_pipelines/07_engineer_inference_gold.sql](../data_pipelines/07_engineer_inference_gold.sql) ends with:

```sql
WHERE window_start_date = @target_date
  AND ndvi_change_60d IS NOT NULL
```

**Why it matters.** The `IS NOT NULL` filter is the *intended* mechanism for the early-grid edge case where 60-day lag history isn't yet computable. For mature dates with a fully backfilled silver, it should drop nothing. When it does drop rows, today there is no log line, no warning, no metric — the count just shows up as a downstream failure later.

**Fix.** In `engineer_gold.engineer_inference_gold`, log `affected_rows` and the post-MERGE count side-by-side; emit a `WARNING` (or structured event) when the post-MERGE count diverges from the canonical 65,687 — even if you also raise (which Bug 1 now does). Makes the failure visible in logs and dashboards, not just exception traces.

## Bug 4: MERGE lacks `WHEN NOT MATCHED BY SOURCE`

**Where.** Same SQL file as Bug 3.

**Why it matters.** If a cell qualified once (made it through the `IS NOT NULL` filter on the first run) and then stops qualifying on a later run (silver row removed/changed), the stale gold row remains in the table forever. Less likely to bite than Bugs 1–3, but worth noting.

**Fix.** Two options:

- Add `WHEN NOT MATCHED BY SOURCE AND dst.window_start_date = @target_date THEN DELETE` to the MERGE.
- Replace the per-window MERGE with `DELETE FROM … WHERE window_start_date = @target_date; INSERT …`. Simpler, partition-bounded, fully describes the intent. Inference's `gold_features_inference` is not shared with training, so a per-window truncate-and-replace is safe here.

---

## Suggested order to fix

1. **Already done in this branch:** Bug 1 partial fix — post-MERGE guard in `engineer_gold.py`.
2. Extract `EXPECTED_INFERENCE_CELL_COUNT` to a shared module (DRY follow-up for Bug 1).
3. Bug 3 — log the divergence so it's visible in dashboards and Cloud Run logs.
4. Bug 2 — staleness detection. Bigger; can be a small standalone job or a check inside `run_inference_pipeline._step_engineer_gold`.
5. Bug 4 — MERGE clause cleanup. Safe to defer.
