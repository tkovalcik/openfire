"""Backfill precomputed low-zoom GeoJSON variants for existing snapshots.

This is intentionally limited to GCS/static-object work. It reads existing
``predictions_YYYYMMDD.geojson`` snapshots, writes missing ``z8``/``z9``
variants, and refreshes ``manifest.json`` so older windows advertise
``geojson_variants``. It does not run GEE, BigQuery, or model inference.

Example:
    python scripts/backfill_snapshot_variants.py \
      --gcs-prefix gs://openfire/predictions \
      --aoi-geojson-uri /data/aoi_counties.geojson
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import storage as gcs_storage

from src.common.storage import StorageClient, parse_storage_uri
from src.pipelines.output_writer import (
    SNAPSHOT_VARIANT_SPECS,
    manifest_uri,
    snapshot_uri,
)


LOGGER = logging.getLogger(__name__)

SNAPSHOT_RE = re.compile(r"predictions_(\d{8})\.geojson$")
VARIANT_RE = re.compile(r"predictions_(\d{8})_(z\d+)\.geojson$")


@dataclass(frozen=True)
class SnapshotBackfillResult:
    window_start_date: date
    source_uri: str
    variants_written: tuple[str, ...]
    variants_skipped: tuple[str, ...]
    dry_run: bool


def _date_from_token(token: str) -> date:
    return date.fromisoformat(f"{token[:4]}-{token[4:6]}-{token[6:]}")


def snapshot_date_from_name(name: str) -> date | None:
    match = SNAPSHOT_RE.search(name)
    if not match:
        return None
    return _date_from_token(match.group(1))


def is_variant_name(name: str) -> bool:
    return VARIANT_RE.search(name) is not None


def _list_snapshot_uris(gcs_prefix: str) -> list[str]:
    location = parse_storage_uri(gcs_prefix.rstrip("/"))
    if location.scheme == "local":
        root = location.local_path
        if not root.exists():
            return []
        return [
            f"local://{path.as_posix()}"
            for path in sorted(root.glob("predictions_*.geojson"))
            if snapshot_date_from_name(path.name) is not None and not is_variant_name(path.name)
        ]

    if location.scheme != "gs" or location.bucket is None:
        raise ValueError("gcs_prefix must be a gs:// or local:// URI")

    client = gcs_storage.Client()
    prefix = location.path.rstrip("/")
    blob_prefix = f"{prefix}/" if prefix else ""
    uris = []
    for blob in client.list_blobs(location.bucket, prefix=blob_prefix):
        if snapshot_date_from_name(blob.name) is None or is_variant_name(blob.name):
            continue
        uris.append(f"gs://{location.bucket}/{blob.name}")
    return sorted(uris)


def _read_geojson(storage: StorageClient, uri: str) -> dict[str, Any]:
    payload = storage.read_bytes(uri)
    try:
        payload = gzip.decompress(payload)
    except gzip.BadGzipFile:
        pass
    return json.loads(payload.decode("utf-8"))


def _write_gzipped_geojson(
    storage: StorageClient,
    uri: str,
    payload: dict[str, Any],
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    storage.write_bytes(
        uri,
        gzip.compress(body),
        content_type="application/geo+json",
        content_encoding="gzip",
    )


def _feature_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[float, float, int]:
    index, feature = item
    coordinates = feature.get("geometry", {}).get("coordinates", [])
    try:
        longitude = float(coordinates[0])
    except (IndexError, TypeError, ValueError):
        longitude = float("inf")
    try:
        latitude = float(coordinates[1])
    except (IndexError, TypeError, ValueError):
        latitude = float("-inf")
    return (-latitude, longitude, index)


def build_variant_payload(
    full_payload: dict[str, Any],
    *,
    sample_stride: int,
    sample_offset: int,
) -> dict[str, Any]:
    """Return a deterministic spatial sample matching the SoCal UI fallback."""
    features = full_payload.get("features", [])
    if not isinstance(features, list):
        raise ValueError("GeoJSON payload must contain a features list")

    stride = max(1, int(sample_stride))
    offset = max(0, min(stride - 1, int(sample_offset)))
    ordered = [feature for _, feature in sorted(enumerate(features), key=_feature_sort_key)]
    sampled = ordered[offset::stride] if stride > 1 else ordered
    variant = dict(full_payload)
    variant["features"] = sampled
    return variant


def _variant_metadata(
    uri: str,
    spec: dict[str, Any],
    feature_count: int | None = None,
) -> dict[str, Any]:
    metadata = {
        "geojson_uri": uri,
        "max_zoom": int(spec["max_zoom"]),
        "sample_stride": int(spec["sample_stride"]),
        "sample_offset": int(spec["sample_offset"]),
    }
    if feature_count is not None:
        metadata["feature_count"] = feature_count
    return metadata


def _model_version(payload: dict[str, Any]) -> str:
    for feature in payload.get("features", []):
        value = feature.get("properties", {}).get("model_version")
        if value is not None:
            return str(value)
    return "unknown"


def rebuild_manifest_from_entries(
    entries: list[dict[str, Any]],
    *,
    gcs_prefix: str,
    storage: StorageClient,
    aoi_geojson_uri: str | None = None,
    status: str = "live",
    source: str = "GCS prediction snapshots",
) -> str:
    if not entries:
        raise RuntimeError(f"No predictions_*.geojson snapshots found under {gcs_prefix}")

    windows = sorted(entries, key=lambda item: item["window_start_date"])
    latest = windows[-1]
    manifest: dict[str, Any] = {
        "latest_window_start_date": latest["window_start_date"],
        "latest_geojson_uri": latest["geojson_uri"],
        "model_version": latest["model_version"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows,
        "status": status,
        "source": source,
    }
    if "geojson_variants" in latest:
        manifest["latest_geojson_variants"] = latest["geojson_variants"]
    if aoi_geojson_uri:
        manifest["aoi_geojson_uri"] = aoi_geojson_uri
    return storage.write_json(manifest_uri(gcs_prefix=gcs_prefix), manifest)


def backfill_snapshot_variants(
    *,
    gcs_prefix: str,
    storage: StorageClient,
    dry_run: bool = False,
    force: bool = False,
    update_manifest: bool = True,
    aoi_geojson_uri: str | None = "/data/aoi_counties.geojson",
    status: str = "live",
    source: str = "GCS prediction snapshots",
) -> list[SnapshotBackfillResult]:
    snapshots = _list_snapshot_uris(gcs_prefix)
    results: list[SnapshotBackfillResult] = []
    manifest_entries: list[dict[str, Any]] = []

    for uri in snapshots:
        window_date = snapshot_date_from_name(Path(uri).name)
        if window_date is None:
            continue

        full_payload: dict[str, Any] | None = None
        written: list[str] = []
        skipped: list[str] = []
        variants: dict[str, Any] = {}

        for key, spec in SNAPSHOT_VARIANT_SPECS.items():
            variant_uri = snapshot_uri(
                window_date,
                gcs_prefix=gcs_prefix,
                variant_suffix=str(spec["suffix"]),
            )
            if not force and storage.exists(variant_uri):
                skipped.append(key)
                variants[key] = _variant_metadata(variant_uri, spec)
                continue

            if full_payload is None:
                full_payload = _read_geojson(storage, uri)
            variant_payload = build_variant_payload(
                full_payload,
                sample_stride=int(spec["sample_stride"]),
                sample_offset=int(spec["sample_offset"]),
            )
            variants[key] = _variant_metadata(
                variant_uri,
                spec,
                feature_count=len(variant_payload["features"]),
            )
            if dry_run:
                skipped.append(key)
                continue

            _write_gzipped_geojson(storage, variant_uri, variant_payload)
            written.append(key)

        if full_payload is None:
            full_payload = _read_geojson(storage, uri)
        manifest_entry: dict[str, Any] = {
            "window_start_date": window_date.isoformat(),
            "geojson_uri": uri,
            "model_version": _model_version(full_payload),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if variants:
            manifest_entry["geojson_variants"] = variants
        manifest_entries.append(manifest_entry)

        results.append(
            SnapshotBackfillResult(
                window_start_date=window_date,
                source_uri=uri,
                variants_written=tuple(written),
                variants_skipped=tuple(skipped),
                dry_run=dry_run,
            )
        )

    if update_manifest and not dry_run:
        rebuild_manifest_from_entries(
            manifest_entries,
            gcs_prefix=gcs_prefix,
            storage=storage,
            aoi_geojson_uri=aoi_geojson_uri,
            status=status,
            source=source,
        )

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcs-prefix", default="gs://openfire/predictions")
    parser.add_argument("--aoi-geojson-uri", default="/data/aoi_counties.geojson")
    parser.add_argument("--status", default="live")
    parser.add_argument("--source", default="GCS prediction snapshots")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rewrite variants even if they already exist.")
    parser.add_argument(
        "--no-manifest-update",
        action="store_true",
        help="Write variants but leave manifest.json untouched.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    results = backfill_snapshot_variants(
        gcs_prefix=args.gcs_prefix,
        storage=StorageClient(),
        dry_run=args.dry_run,
        force=args.force,
        update_manifest=not args.no_manifest_update,
        aoi_geojson_uri=args.aoi_geojson_uri,
        status=args.status,
        source=args.source,
    )
    written = sum(len(result.variants_written) for result in results)
    skipped = sum(len(result.variants_skipped) for result in results)
    LOGGER.info(
        "Processed %d snapshots; variants written=%d skipped=%d dry_run=%s",
        len(results),
        written,
        skipped,
        args.dry_run,
    )


if __name__ == "__main__":
    main()
