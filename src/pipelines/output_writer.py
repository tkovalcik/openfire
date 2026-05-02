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

from src.common.storage import StorageError

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
            bigquery.SchemaField("lat_bin",           "INT64",     mode="REQUIRED"),
            bigquery.SchemaField("lon_bin",           "INT64",     mode="REQUIRED"),
        ],
    )
    payload = predictions[PREDICTION_COLUMNS].copy()
    payload["lat_bin"] = (payload["latitude"] * 10).round().astype("int64")
    payload["lon_bin"] = (payload["longitude"] * 10).round().astype("int64")
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

def _read_existing_manifest(storage: StorageClient, uri: str) -> dict[str, Any] | None:
    """Return the existing manifest payload, or None if it is absent/unusable."""
    try:
        if not storage.exists(uri):
            return None
        return storage.read_json(uri)
    except (StorageError, ValueError, TypeError) as exc:
        LOGGER.warning("Existing manifest at %s unreadable (%s); will overwrite.", uri, exc)
        return None


def _manifest_window(manifest: dict[str, Any] | None) -> date | None:
    if not manifest:
        return None
    try:
        return date.fromisoformat(str(manifest["latest_window_start_date"]))
    except (KeyError, ValueError, TypeError):
        return None


def _upsert_manifest_window(
    existing: dict[str, Any] | None,
    *,
    window_start_date: date,
    geojson_uri: str,
    model_version: str,
    updated_at,
) -> list[dict[str, str]]:
    by_date: dict[str, dict[str, str]] = {}
    for item in (existing or {}).get("windows", []):
        if not isinstance(item, dict) or "window_start_date" not in item:
            continue
        window_key = str(item["window_start_date"])
        entry = {
            "window_start_date": window_key,
            "geojson_uri": str(item.get("geojson_uri", item.get("latest_geojson_uri", ""))),
            "model_version": str(item.get("model_version", "")),
        }
        if "updated_at" in item:
            entry["updated_at"] = str(item["updated_at"])
        by_date[window_key] = entry

    if existing and not by_date and existing.get("latest_window_start_date") and existing.get("latest_geojson_uri"):
        window_key = str(existing["latest_window_start_date"])
        entry = {
            "window_start_date": window_key,
            "geojson_uri": str(existing["latest_geojson_uri"]),
            "model_version": str(existing.get("model_version", "")),
        }
        if "updated_at" in existing:
            entry["updated_at"] = str(existing["updated_at"])
        by_date[window_key] = entry

    updated_at_text = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    by_date[window_start_date.isoformat()] = {
        "window_start_date": window_start_date.isoformat(),
        "geojson_uri": geojson_uri,
        "model_version": str(model_version),
        "updated_at": updated_at_text,
    }
    return [by_date[key] for key in sorted(by_date)]


def update_manifest(
    *,
    latest_window_start_date: date,
    latest_geojson_uri: str,
    model_version: str,
    updated_at,  # datetime  # noqa: ANN001 — explicit type below
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
) -> str:
    """Monotonically advance manifest.json to point at the most recent snapshot.

    The manifest tracks the *frontier* of inference, not the most-recently-run
    window. Re-running an older window (e.g., a manual single-window rerun
    after a backfill has already advanced the frontier) must not regress the
    pointer — the UI would otherwise show stale predictions. If the existing
    manifest's window is newer than ours, this is a no-op and we return the
    existing URI unchanged. Equal-or-newer windows write through (so model
    version / updated_at refresh on a same-window re-run).

    GCS object PUTs are atomic; the UI sees either the old manifest or the
    new one, never a partial. No temp-then-rename dance required.
    """
    uri = manifest_uri(gcs_prefix=gcs_prefix)
    existing = _read_existing_manifest(storage, uri)
    existing_window = _manifest_window(existing)
    incoming_updated_at = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    windows = _upsert_manifest_window(
        existing,
        window_start_date=latest_window_start_date,
        geojson_uri=latest_geojson_uri,
        model_version=model_version,
        updated_at=updated_at,
    )

    if existing_window is not None and existing_window > latest_window_start_date:
        LOGGER.warning(
            "Skipping manifest update at %s: existing window %s is newer than %s "
            "(refusing to regress the frontier; window index will still be updated).",
            uri, existing_window.isoformat(), latest_window_start_date.isoformat(),
        )
        payload = dict(existing or {})
        payload["windows"] = windows
        storage.write_json(uri, payload)
        return uri

    payload = {
        "latest_window_start_date": latest_window_start_date.isoformat(),
        "latest_geojson_uri": latest_geojson_uri,
        "model_version": str(model_version),
        "updated_at": incoming_updated_at,
        "windows": windows,
    }
    if existing and "aoi_geojson_uri" in existing:
        payload["aoi_geojson_uri"] = existing["aoi_geojson_uri"]
    if existing and "status" in existing:
        payload["status"] = existing["status"]
    if existing and "source" in existing:
        payload["source"] = existing["source"]
    storage.write_json(uri, payload)
    LOGGER.info("Updated manifest at %s → %s", uri, latest_geojson_uri)
    return uri


# ── monitoring index ──────────────────────────────────────────────────────────

DEFAULT_MONITORING_PREFIX = "gs://openfire/monitoring"
MONITORING_INDEX_FILENAME = "index.json"


def monitoring_index_uri(*, monitoring_prefix: str = DEFAULT_MONITORING_PREFIX) -> str:
    return f"{monitoring_prefix.rstrip('/')}/{MONITORING_INDEX_FILENAME}"


def update_monitoring_index(
    entry: dict,
    *,
    storage: StorageClient,
    monitoring_prefix: str = DEFAULT_MONITORING_PREFIX,
) -> str:
    """Upsert a per-window entry into the monitoring index.json.

    The index holds a sorted list of window entries. Re-running the same window
    overwrites that entry (idempotent). The list is sorted by window_start_date
    ascending so the dashboard can walk windows in order.
    """
    uri = monitoring_index_uri(monitoring_prefix=monitoring_prefix)

    try:
        windows: list = storage.read_json(uri).get("windows", []) if storage.exists(uri) else []
    except Exception as exc:
        LOGGER.warning(
            "Could not read monitoring index at %s (%s); starting fresh.", uri, exc
        )
        windows = []

    window_date = entry.get("window_start_date")
    windows = [w for w in windows if w.get("window_start_date") != window_date]
    windows.append(entry)
    windows.sort(key=lambda w: w.get("window_start_date", ""))

    storage.write_json(uri, {"windows": windows})
    LOGGER.info("Updated monitoring index at %s (%d entries)", uri, len(windows))
    return uri


__all__ = [
    "PREDICTIONS_TABLE",
    "PREDICTION_COLUMNS",
    "DEFAULT_GCS_PREFIX",
    "MANIFEST_FILENAME",
    "DEFAULT_MONITORING_PREFIX",
    "MONITORING_INDEX_FILENAME",
    "build_geojson_payload",
    "snapshot_uri",
    "manifest_uri",
    "monitoring_index_uri",
    "write_predictions_to_bq",
    "write_geojson_snapshot",
    "update_manifest",
    "update_monitoring_index",
]
