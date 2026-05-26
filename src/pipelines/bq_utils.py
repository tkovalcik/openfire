"""BigQuery helpers used by the training pipeline orchestrator.

Factored out so each step in the orchestrator is one readable line
(`exec_sql_file(client, path)`) instead of repeated boilerplate.
Phase 4 inference will reuse these.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from google.cloud import bigquery
from google.cloud.exceptions import NotFound


LOGGER = logging.getLogger(__name__)


def get_client(project: str) -> bigquery.Client:
    return bigquery.Client(project=project)


def exec_sql_file(client: bigquery.Client, sql_path: Path) -> bigquery.QueryJob:
    """Run a SQL file against BigQuery; log job_id, bytes billed, elapsed time."""
    if not sql_path.exists():
        raise FileNotFoundError(f"SQL file not found: {sql_path}")
    sql = sql_path.read_text()
    LOGGER.info("Executing %s (%d chars)", sql_path.name, len(sql))

    start = time.time()
    job = client.query(sql)
    job.result()  # blocks until BigQuery finishes
    elapsed = time.time() - start

    bytes_billed = job.total_bytes_billed
    bytes_str = f"{bytes_billed:,}" if bytes_billed is not None else "n/a"
    LOGGER.info(
        "Completed %s in %.1fs (job_id=%s, bytes_billed=%s)",
        sql_path.name,
        elapsed,
        job.job_id,
        bytes_str,
    )
    return job


def table_exists(client: bigquery.Client, table_ref: str) -> bool:
    try:
        client.get_table(table_ref)
        return True
    except NotFound:
        return False


def get_row_count(client: bigquery.Client, table_ref: str) -> int:
    """Read num_rows from table metadata. Cheap — no scan."""
    table = client.get_table(table_ref)
    return table.num_rows
