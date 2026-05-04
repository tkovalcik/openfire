"""Engineer gold features for one inference window.

Wraps the two SQL files in `data_pipelines/`:
- `07_create_inference_tables.sql` — DDL, idempotent CREATE TABLE IF NOT EXISTS.
- `07_engineer_inference_gold.sql` — parameterized MERGE keyed on
  (latitude, longitude, window_start_date) for one @target_date.

Phase 4 Step 4 decision (per docs/phase4_inspection.md): inference engineers
into its own table `gold_features_inference` rather than sharing
`gold_features` with training. The training table stays read-only from the
inference path's perspective; the inference path uses MERGE so re-running
the same window is a no-op (or an UPDATE if the silver inputs changed).

Output table: `msds603-mlops-project.openfire_features.gold_features_inference`
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from google.cloud import bigquery


LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
INFERENCE_GOLD_TABLE = f"{PROJECT}.{DATASET}.gold_features_inference"

PIPELINE_ROOT = Path(__file__).resolve().parents[2] / "data_pipelines"
DDL_FILE = PIPELINE_ROOT / "07_create_inference_tables.sql"
MERGE_FILE = PIPELINE_ROOT / "07_engineer_inference_gold.sql"

# Canonical SoCal grid cell count. Duplicated in run_inference_pipeline.py;
# extract to a shared module on next pass.
EXPECTED_INFERENCE_CELL_COUNT = 65_687


def ensure_inference_tables(client: bigquery.Client) -> None:
    """Run the idempotent DDL. Safe to call before every window."""
    sql = DDL_FILE.read_text()
    job = client.query(sql)
    job.result()
    LOGGER.info("Ensured inference tables exist (job_id=%s)", job.job_id)


def _count_gold_rows(client: bigquery.Client, target_date: date) -> int:
    sql = (
        f"SELECT COUNT(*) AS n FROM `{INFERENCE_GOLD_TABLE}` "
        f"WHERE window_start_date = @target_date"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_date", "DATE", target_date),
        ]
    )
    row = next(iter(client.query(sql, job_config=job_config).result()))
    return int(row["n"])


def engineer_inference_gold(
    client: bigquery.Client,
    *,
    target_date: date,
) -> int:
    """MERGE the engineered gold features for one window into gold_features_inference.

    Returns the post-MERGE row count for the partition. Idempotent: re-running
    for the same target_date overwrites in place.

    Raises RuntimeError if the resulting partition's row count diverges from
    EXPECTED_INFERENCE_CELL_COUNT. The downstream guard in
    run_inference_pipeline.py catches the same condition, but only after gold
    is read for prediction; checking here surfaces the failure at the
    silver→gold boundary so partial gold partitions can't silently persist
    after a silver backfill (see docs/snapshot-investigation-2025-12-23-28.md).
    """
    sql = MERGE_FILE.read_text()
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_date", "DATE", target_date),
        ]
    )
    job = client.query(sql, job_config=job_config)
    job.result()
    affected = job.num_dml_affected_rows or 0
    LOGGER.info(
        "Engineered gold for window=%s; rows affected=%s, bytes_billed=%s, job_id=%s",
        target_date.isoformat(),
        f"{affected:,}",
        f"{job.total_bytes_billed:,}" if job.total_bytes_billed else "n/a",
        job.job_id,
    )

    actual = _count_gold_rows(client, target_date)
    if actual != EXPECTED_INFERENCE_CELL_COUNT:
        raise RuntimeError(
            f"gold_features_inference row-count invariant failed for window "
            f"{target_date.isoformat()}: expected "
            f"{EXPECTED_INFERENCE_CELL_COUNT:,}, got {actual:,}. The MERGE "
            f"silently drops cells whose 60-day NDVI lag is unavailable "
            f"(WHERE ndvi_change_60d IS NOT NULL in "
            f"07_engineer_inference_gold.sql). If silver was recently "
            f"backfilled for this window, re-running this step will resolve "
            f"it; otherwise upstream silver is incomplete."
        )

    return actual


__all__ = [
    "INFERENCE_GOLD_TABLE",
    "DDL_FILE",
    "MERGE_FILE",
    "EXPECTED_INFERENCE_CELL_COUNT",
    "ensure_inference_tables",
    "engineer_inference_gold",
]
