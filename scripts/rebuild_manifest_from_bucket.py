"""Rebuild the prediction manifest window index from existing GeoJSON snapshots.

Example:
    python scripts/rebuild_manifest_from_bucket.py \
      --gcs-prefix gs://openfire/predictions \
      --aoi-geojson-uri /data/aoi_counties.geojson
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from typing import Any

from google.cloud import storage as gcs_storage

from src.common.storage import StorageClient, parse_storage_uri
from src.pipelines.output_writer import manifest_uri


SNAPSHOT_RE = re.compile(r"predictions_(\d{8})\.geojson$")


def _snapshot_date(blob_name: str) -> date | None:
    match = SNAPSHOT_RE.search(blob_name)
    if not match:
        return None
    return date.fromisoformat(
        f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:]}"
    )


def _model_version(payload: dict[str, Any]) -> str:
    for feature in payload.get("features", []):
        value = feature.get("properties", {}).get("model_version")
        if value is not None:
            return str(value)
    return "unknown"


def rebuild_manifest(
    *,
    gcs_prefix: str,
    storage: StorageClient,
    aoi_geojson_uri: str | None = None,
    status: str = "live",
    source: str = "GCS prediction snapshots",
) -> str:
    location = parse_storage_uri(gcs_prefix.rstrip("/"))
    if location.scheme != "gs" or location.bucket is None:
        raise ValueError("gcs_prefix must be a gs:// URI")

    client = gcs_storage.Client()
    prefix = location.path.rstrip("/")
    blob_prefix = f"{prefix}/" if prefix else ""
    windows: list[dict[str, str]] = []
    for blob in client.list_blobs(location.bucket, prefix=blob_prefix):
        window_date = _snapshot_date(blob.name)
        if window_date is None:
            continue
        uri = f"gs://{location.bucket}/{blob.name}"
        payload = json.loads(blob.download_as_text(encoding="utf-8"))
        windows.append(
            {
                "window_start_date": window_date.isoformat(),
                "geojson_uri": uri,
                "model_version": _model_version(payload),
                "updated_at": blob.updated.isoformat() if blob.updated else "",
            }
        )

    if not windows:
        raise RuntimeError(f"No predictions_*.geojson snapshots found under {gcs_prefix}")

    windows.sort(key=lambda item: item["window_start_date"])
    latest = windows[-1]
    manifest = {
        "latest_window_start_date": latest["window_start_date"],
        "latest_geojson_uri": latest["geojson_uri"],
        "model_version": latest["model_version"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows,
        "status": status,
        "source": source,
    }
    if aoi_geojson_uri:
        manifest["aoi_geojson_uri"] = aoi_geojson_uri

    return storage.write_json(manifest_uri(gcs_prefix=gcs_prefix), manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcs-prefix", default="gs://openfire/predictions")
    parser.add_argument("--aoi-geojson-uri", default="/data/aoi_counties.geojson")
    parser.add_argument("--status", default="live")
    parser.add_argument("--source", default="GCS prediction snapshots")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    storage = StorageClient()
    uri = rebuild_manifest(
        gcs_prefix=args.gcs_prefix,
        storage=storage,
        aoi_geojson_uri=args.aoi_geojson_uri,
        status=args.status,
        source=args.source,
    )
    print(uri)


if __name__ == "__main__":
    main()
