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

import gzip
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
SNAPSHOT_VARIANT_SPECS = {
    "low": {
        "suffix": "z8",
        "max_zoom": 8,
        "sample_stride": 6,
        "sample_offset": 2,
    },
    "medium": {
        "suffix": "z9",
        "max_zoom": 9,
        "sample_stride": 3,
        "sample_offset": 1,
    },
}

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
    sample_stride: int = 1,
    sample_offset: int = 0,
) -> dict[str, Any]:
    """Pure function: turn a predictions DataFrame into a GeoJSON dict."""
    if sample_stride > 1:
        predictions = _spatially_sample_predictions(
            predictions,
            sample_stride=sample_stride,
            sample_offset=sample_offset,
        )

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


def _spatially_sample_predictions(
    predictions: pd.DataFrame,
    *,
    sample_stride: int,
    sample_offset: int,
) -> pd.DataFrame:
    """Return a deterministic lat/lon-ordered sample matching the SoCal UI tiers."""
    if sample_stride <= 1:
        return predictions
    offset = max(0, min(sample_stride - 1, sample_offset))
    ordered = predictions.sort_values(
        by=["latitude", "longitude"],
        ascending=[False, True],
        kind="mergesort",
    )
    return ordered.iloc[offset::sample_stride]


def snapshot_uri(
    target_date: date,
    *,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    variant_suffix: str | None = None,
) -> str:
    suffix = f"_{variant_suffix}" if variant_suffix else ""
    return f"{gcs_prefix.rstrip('/')}/predictions_{target_date.strftime('%Y%m%d')}{suffix}.geojson"


def manifest_uri(*, gcs_prefix: str = DEFAULT_GCS_PREFIX) -> str:
    return f"{gcs_prefix.rstrip('/')}/{MANIFEST_FILENAME}"


def _write_geojson_payload(
    uri: str,
    payload: dict[str, Any],
    *,
    storage: StorageClient,
    gzip_output: bool,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if gzip_output:
        storage.write_bytes(
            uri,
            gzip.compress(body),
            content_type="application/geo+json",
            content_encoding="gzip",
        )
        return
    storage.write_bytes(uri, body, content_type="application/geo+json")


def write_geojson_snapshot(
    predictions: pd.DataFrame,
    *,
    target_date: date,
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    coord_decimals: int = DEFAULT_COORD_DECIMALS,
    gzip_output: bool = True,
) -> str:
    """Write the date-stamped GeoJSON FeatureCollection. Returns the written URI."""
    uri = snapshot_uri(target_date, gcs_prefix=gcs_prefix)
    payload = build_geojson_payload(predictions, coord_decimals=coord_decimals)
    _write_geojson_payload(uri, payload, storage=storage, gzip_output=gzip_output)
    LOGGER.info(
        "Wrote GeoJSON snapshot to %s (%d features)", uri, len(payload["features"])
    )
    return uri


def write_geojson_snapshot_variants(
    predictions: pd.DataFrame,
    *,
    target_date: date,
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    coord_decimals: int = DEFAULT_COORD_DECIMALS,
    gzip_output: bool = True,
) -> dict[str, dict[str, Any]]:
    """Write pre-sampled GeoJSON snapshots for low-zoom SoCal UI rendering."""
    variants: dict[str, dict[str, Any]] = {}
    for key, spec in SNAPSHOT_VARIANT_SPECS.items():
        uri = snapshot_uri(
            target_date,
            gcs_prefix=gcs_prefix,
            variant_suffix=str(spec["suffix"]),
        )
        payload = build_geojson_payload(
            predictions,
            coord_decimals=coord_decimals,
            sample_stride=int(spec["sample_stride"]),
            sample_offset=int(spec["sample_offset"]),
        )
        _write_geojson_payload(uri, payload, storage=storage, gzip_output=gzip_output)
        variants[key] = {
            "geojson_uri": uri,
            "feature_count": len(payload["features"]),
            "max_zoom": int(spec["max_zoom"]),
            "sample_stride": int(spec["sample_stride"]),
            "sample_offset": int(spec["sample_offset"]),
        }
        LOGGER.info(
            "Wrote GeoJSON %s variant to %s (%d features)",
            key, uri, len(payload["features"])
        )
    return variants


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
    geojson_variants: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
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
        if isinstance(item.get("geojson_variants"), dict):
            entry["geojson_variants"] = item["geojson_variants"]
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
        if isinstance(existing.get("latest_geojson_variants"), dict):
            entry["geojson_variants"] = existing["latest_geojson_variants"]
        by_date[window_key] = entry

    updated_at_text = updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at)
    current_entry: dict[str, Any] = {
        "window_start_date": window_start_date.isoformat(),
        "geojson_uri": geojson_uri,
        "model_version": str(model_version),
        "updated_at": updated_at_text,
    }
    if geojson_variants:
        current_entry["geojson_variants"] = geojson_variants
    by_date[window_start_date.isoformat()] = current_entry
    return [by_date[key] for key in sorted(by_date)]


def update_manifest(
    *,
    latest_window_start_date: date,
    latest_geojson_uri: str,
    model_version: str,
    updated_at,  # datetime  # noqa: ANN001 — explicit type below
    storage: StorageClient,
    gcs_prefix: str = DEFAULT_GCS_PREFIX,
    latest_geojson_variants: dict[str, dict[str, Any]] | None = None,
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
        geojson_variants=latest_geojson_variants,
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
    if latest_geojson_variants:
        payload["latest_geojson_variants"] = latest_geojson_variants
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
    "SNAPSHOT_VARIANT_SPECS",
    "DEFAULT_MONITORING_PREFIX",
    "MONITORING_INDEX_FILENAME",
    "build_geojson_payload",
    "snapshot_uri",
    "manifest_uri",
    "monitoring_index_uri",
    "write_predictions_to_bq",
    "write_geojson_snapshot",
    "write_geojson_snapshot_variants",
    "update_manifest",
    "update_monitoring_index",
]
