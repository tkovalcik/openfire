"""Write inference outputs: BQ predictions table, GeoJSON snapshot, manifest.

Three sinks, all idempotent at the per-window level:

1. ``write_predictions_to_bq`` — partition-scoped WRITE_TRUNCATE load into
   ``predictions_history``. Re-running the same window replaces only that
   window's rows; other partitions are untouched. We use partition-scoped
   truncate (``predictions_history$YYYYMMDD``) instead of MERGE because
   load jobs are far cheaper than DML jobs for million-row payloads, and
   the per-window primary-key invariant gives us "one row per (cell,
   window)" without needing a MERGE statement.

2. ``write_geojson_snapshot`` — a date-stamped FeatureCollection at
   ``gs://openfire/predictions/predictions_<YYYYMMDD>.geojson``. Coordinate
   precision is capped (default 5 decimal places ≈ 1 m, well below the
   1 km grid resolution).

3. ``update_manifest`` — overwrites ``manifest.json`` with a pointer to
   the most recent snapshot. GCS object writes are atomic at the object
   level (a single PUT replaces atomically; readers see either the old or
   the new version, never a partial), so the spec's "write-temp-then-rename"
   guidance from POSIX filesystems doesn't apply on GCS.

All three are independently testable with mocked clients; ``process_window``
in the orchestrator wires them in the order BQ → GeoJSON → manifest, so a
partial failure leaves the system in a recoverable state (a re-run
idempotently re-writes the BQ partition and re-attempts the snapshot).
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import pandas as pd

try:
    from src.common.storage import StorageClient
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.storage import StorageClient


LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
PREDICTIONS_TABLE = f"{PROJECT}.{DATASET}.predictions_history"

DEFAULT_GCS_PREFIX = "gs://openfire/predictions"
MANIFEST_FILENAME = "manifest.json"
DEFAULT_COORD_DECIMALS = 5
DEFAULT_PROB_DECIMALS = 5

PREDICTION_COLUMNS = [
    "latitude", "longitude", "window_start_date",
    "risk_probability", "predicted_label",
    "model_name", "model_version", "inference_run_at",
]


# ── BigQuery write ───────────────────────────────────────────────────────────

def _partition_decorator(target_date: date) -> str:
    return target_date.strftime("%Y%m%d")


def write_predictions_to_bq(
    bq_client: Any,
    predictions: pd.DataFrame,
    *,
    target_date: date,
    table: str = PREDICTIONS_TABLE,
) -> int:
    """Load `predictions` into the target_date partition of predictions_history.

    Idempotent per window: WRITE_TRUNCATE on the partition decorator replaces
    only that window's rows; other partitions are untouched. The DataFrame
    must contain exactly PREDICTION_COLUMNS and every row must have
    ``window_start_date == target_date``. Returns the row count loaded.
    """
    from google.cloud import bigquery

    if predictions.empty:
        LOGGER.warning("write_predictions_to_bq: empty DataFrame for window %s; skipping load.", target_date)
        return 0

    missing = [c for c in PREDICTION_COLUMNS if c not in predictions.columns]
    if missing:
        raise ValueError(f"predictions is missing required columns: {missing}")

    bad_window = predictions["window_start_date"].apply(lambda d: d != target_date)
    if bad_window.any():
        raise ValueError(
            f"predictions contains rows whose window_start_date != target_date "
            f"({target_date}); refusing to write to the wrong partition."
        )

    destination = f"{table}${_partition_decorator(target_date)}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=[
            bigquery.SchemaField("latitude",          "FLOAT64",   mode="REQUIRED"),
            bigquery.SchemaField("longitude",         "FLOAT64",   mode="REQUIRED"),
            bigquery.SchemaField("window_start_date", "DATE",      mode="REQUIRED"),
            bigquery.SchemaField("risk_probability",  "FLOAT64",   mode="REQUIRED"),
            bigquery.SchemaField("predicted_label",   "INT64",     mode="REQUIRED"),
            bigquery.SchemaField("model_name",        "STRING",    mode="REQUIRED"),
            bigquery.SchemaField("model_version",     "STRING",    mode="REQUIRED"),
            bigquery.SchemaField("inference_run_at",  "TIMESTAMP", mode="REQUIRED"),
        ],
    )
    payload = predictions[PREDICTION_COLUMNS]
    job = bq_client.load_table_from_dataframe(payload, destination, job_config=job_config)
    job.result()
    rows = len(payload)
    LOGGER.info(
        "Loaded %s prediction rows into %s (job_id=%s)",
        f"{rows:,}", destination, getattr(job, "job_id", "n/a"),
    )
    return rows


# ── GeoJSON snapshot ─────────────────────────────────────────────────────────

def build_geojson_payload(
    predictions: pd.DataFrame,
    *,
    coord_decimals: int = DEFAULT_COORD_DECIMALS,
    prob_decimals: int = DEFAULT_PROB_DECIMALS,
) -> dict[str, Any]:
    """Pure function: turn a predictions DataFrame into a GeoJSON dict."""
    features = []
    for row in predictions.itertuples(index=False):
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    round(float(row.longitude), coord_decimals),
                    round(float(row.latitude), coord_decimals),
                ],
            },
            "properties": {
                "risk_probability": round(float(row.risk_probability), prob_decimals),
                "predicted_label": int(row.predicted_label),
                "window_start_date": (
                    row.window_start_date.isoformat()
                    if hasattr(row.window_start_date, "isoformat")
                    else str(row.window_start_date)
                ),
                "model_version": str(row.model_version),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def snapshot_uri(target_date: date, *, gcs_prefix: str = DEFAULT_GCS_PREFIX) -> str:
    return f"{gcs_prefix.rstrip('/')}/predictions_{target_date.strftime('%Y%m%d')}.geojson"


def manifest_uri(*, gcs_prefix: str = DEFAULT_GCS_PREFIX) -> str:
    return f"{gcs_prefix.rstrip('/')}/{MANIFEST_FILENAME}"


def write_geojson_snapshot(
    predictions: pd.DataFrame,
    *,
    target_date: date,
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    coord_decimals: int = DEFAULT_COORD_DECIMALS,
) -> str:
    """Write the date-stamped GeoJSON FeatureCollection. Returns the written URI."""
    uri = snapshot_uri(target_date, gcs_prefix=gcs_prefix)
    payload = build_geojson_payload(predictions, coord_decimals=coord_decimals)
    body = json.dumps(payload, separators=(",", ":"))  # compact: snapshots can be large
    storage.write_text(uri, body, content_type="application/geo+json")
    LOGGER.info(
        "Wrote GeoJSON snapshot to %s (%d features)", uri, len(payload["features"])
    )
    return uri


# ── manifest ─────────────────────────────────────────────────────────────────

def update_manifest(
    *,
    latest_window_start_date: date,
    latest_geojson_uri: str,
    model_version: str,
    updated_at,  # datetime  # noqa: ANN001 — explicit type below
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
) -> str:
    """Overwrite manifest.json with a pointer to the most recent snapshot.

    GCS object PUTs are atomic; the UI sees either the old manifest or the
    new one, never a partial. No temp-then-rename dance required.
    """
    uri = manifest_uri(gcs_prefix=gcs_prefix)
    payload = {
        "latest_window_start_date": latest_window_start_date.isoformat(),
        "latest_geojson_uri": latest_geojson_uri,
        "model_version": str(model_version),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
    }
    storage.write_json(uri, payload)
    LOGGER.info("Updated manifest at %s → %s", uri, latest_geojson_uri)
    return uri


__all__ = [
    "PREDICTIONS_TABLE",
    "PREDICTION_COLUMNS",
    "DEFAULT_GCS_PREFIX",
    "MANIFEST_FILENAME",
    "build_geojson_payload",
    "snapshot_uri",
    "manifest_uri",
    "write_predictions_to_bq",
    "write_geojson_snapshot",
    "update_manifest",
]
