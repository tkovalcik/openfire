"""Dry-run alert worker for OpenFire SoCal subscriptions.

This module is the first slice of the outbound-alert path described in
docs/handoff.md. It deliberately separates alert *selection* from alert
delivery so we can test the geospatial threshold logic without SendGrid,
Resend, Twilio, or live BigQuery credentials.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
from typing import Any, Iterable


LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST_URI = "gs://openfire/predictions/manifest.json"
DEFAULT_THRESHOLD = 0.5
EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Subscription:
    email: str
    zip: str
    risk_threshold: float | None = None


@dataclass(frozen=True)
class ZipCentroid:
    zip: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class RiskCell:
    latitude: float
    longitude: float
    risk_probability: float
    predicted_label: int | None
    window_start_date: str
    model_version: str | None


@dataclass(frozen=True)
class AlertCandidate:
    subscription: Subscription
    zip_centroid: ZipCentroid
    risk_cell: RiskCell
    threshold: float
    distance_km: float


def _coerce_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric; got {value!r}") from exc


def load_subscriptions_from_json(path: str | Path) -> list[Subscription]:
    """Load subscriptions from a JSON fixture for local dry-runs and tests."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload["subscriptions"] if isinstance(payload, dict) else payload
    subscriptions: list[Subscription] = []
    for row in rows:
        subscriptions.append(
            Subscription(
                email=str(row["email"]).lower().strip(),
                zip=str(row["zip"]).strip(),
                risk_threshold=(
                    None
                    if row.get("risk_threshold") is None
                    else _coerce_float(row.get("risk_threshold"), field_name="risk_threshold")
                ),
            )
        )
    return subscriptions


def load_zip_centroids_from_json(path: str | Path) -> dict[str, ZipCentroid]:
    """Load ZIP centroids from a JSON fixture.

    Accepted shapes:
    - [{"zip": "94110", "latitude": 37.75, "longitude": -122.41}]
    - {"94110": {"latitude": 37.75, "longitude": -122.41}}
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = [
            {"zip": zip_code, **coords}
            for zip_code, coords in payload.items()
        ]
    else:
        rows = payload

    out: dict[str, ZipCentroid] = {}
    for row in rows:
        zip_code = str(row["zip"]).strip()
        out[zip_code] = ZipCentroid(
            zip=zip_code,
            latitude=_coerce_float(row["latitude"], field_name="latitude"),
            longitude=_coerce_float(row["longitude"], field_name="longitude"),
        )
    return out


def normalize_uri(uri: str | Path) -> str:
    text = str(uri)
    if text.startswith(("gs://", "local://")):
        return text
    return f"local://{Path(text).resolve()}"


def read_json_uri(uri: str) -> dict[str, Any]:
    normalized = normalize_uri(uri)
    if normalized.startswith("local://"):
        return json.loads(Path(normalized.removeprefix("local://")).read_text(encoding="utf-8"))
    if normalized.startswith("gs://"):
        from google.cloud import storage

        bucket_name, _, blob_name = normalized.removeprefix("gs://").partition("/")
        if not bucket_name or not blob_name:
            raise ValueError(f"Invalid GCS URI: {uri}")
        blob = storage.Client().bucket(bucket_name).blob(blob_name)
        return json.loads(blob.download_as_bytes().decode("utf-8"))
    raise ValueError(f"Unsupported URI scheme for {uri!r}")


def load_manifest(manifest_uri: str = DEFAULT_MANIFEST_URI) -> dict[str, Any]:
    return read_json_uri(manifest_uri)


def latest_geojson_uri(manifest: dict[str, Any], *, manifest_uri: str) -> str:
    """Return the GeoJSON URI for the latest manifest window."""
    latest_date = manifest.get("latest_window_start_date")
    for window in manifest.get("windows", []):
        if window.get("window_start_date") == latest_date and window.get("geojson_uri"):
            return resolve_manifest_asset_uri(str(window["geojson_uri"]), manifest_uri=manifest_uri)

    latest = manifest.get("latest_geojson_uri")
    if not latest:
        raise ValueError("Manifest does not contain latest_geojson_uri or matching latest window.")
    return resolve_manifest_asset_uri(str(latest), manifest_uri=manifest_uri)


def resolve_manifest_asset_uri(asset_uri: str, *, manifest_uri: str) -> str:
    """Resolve local relative manifest asset paths against the manifest location."""
    if asset_uri.startswith(("gs://", "local://", "http://", "https://")):
        return asset_uri
    normalized_manifest = normalize_uri(manifest_uri)
    if normalized_manifest.startswith("gs://"):
        bucket_name, _, blob_name = normalized_manifest.removeprefix("gs://").partition("/")
        prefix = str(Path(blob_name).parent).strip(".")
        joined = f"{prefix}/{asset_uri}".replace("//", "/").lstrip("/")
        return f"gs://{bucket_name}/{joined}"
    manifest_path = Path(normalized_manifest.removeprefix("local://"))
    if asset_uri.startswith("../frontend-socal/data/"):
        repo_root = manifest_path.parent.parent.parent
        return normalize_uri(repo_root / asset_uri.removeprefix("../"))
    return normalize_uri((manifest_path.parent / asset_uri).resolve())


def load_risk_cells(geojson_uri: str) -> list[RiskCell]:
    payload = read_json_uri(geojson_uri)
    cells: list[RiskCell] = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "Point" or len(coordinates) < 2:
            continue
        cells.append(
            RiskCell(
                longitude=_coerce_float(coordinates[0], field_name="longitude"),
                latitude=_coerce_float(coordinates[1], field_name="latitude"),
                risk_probability=_coerce_float(
                    properties.get("risk_probability"),
                    field_name="risk_probability",
                ),
                predicted_label=(
                    None
                    if properties.get("predicted_label") is None
                    else int(properties["predicted_label"])
                ),
                window_start_date=str(properties.get("window_start_date") or ""),
                model_version=(
                    None
                    if properties.get("model_version") is None
                    else str(properties.get("model_version"))
                ),
            )
        )
    if not cells:
        raise ValueError(f"No point risk cells found in {geojson_uri}")
    return cells


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    lat1 = math.radians(a_lat)
    lat2 = math.radians(b_lat)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def nearest_risk_cell(centroid: ZipCentroid, cells: Iterable[RiskCell]) -> tuple[RiskCell, float]:
    best_cell: RiskCell | None = None
    best_distance = float("inf")
    for cell in cells:
        distance = haversine_km(
            centroid.latitude,
            centroid.longitude,
            cell.latitude,
            cell.longitude,
        )
        if distance < best_distance:
            best_cell = cell
            best_distance = distance
    if best_cell is None:
        raise ValueError("Cannot find nearest risk cell from an empty cell collection.")
    return best_cell, best_distance


def evaluate_alerts(
    subscriptions: Iterable[Subscription],
    zip_centroids: dict[str, ZipCentroid],
    risk_cells: Iterable[RiskCell],
    *,
    default_threshold: float = DEFAULT_THRESHOLD,
) -> list[AlertCandidate]:
    cells = list(risk_cells)
    candidates: list[AlertCandidate] = []
    for subscription in subscriptions:
        centroid = zip_centroids.get(subscription.zip)
        if centroid is None:
            LOGGER.warning("Skipping subscription with unknown ZIP centroid: %s", subscription.zip)
            continue
        threshold = (
            default_threshold
            if subscription.risk_threshold is None
            else subscription.risk_threshold
        )
        cell, distance = nearest_risk_cell(centroid, cells)
        if cell.risk_probability >= threshold:
            candidates.append(
                AlertCandidate(
                    subscription=subscription,
                    zip_centroid=centroid,
                    risk_cell=cell,
                    threshold=threshold,
                    distance_km=distance,
                )
            )
    return candidates


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate OpenFire alert subscriptions.")
    parser.add_argument("--manifest-uri", default=DEFAULT_MANIFEST_URI)
    parser.add_argument("--subscriptions-json", required=True)
    parser.add_argument("--zip-centroids-json", required=True)
    parser.add_argument("--default-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Evaluate and print candidates without sending notifications.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    manifest_uri = normalize_uri(args.manifest_uri)
    manifest = load_manifest(manifest_uri)
    geojson_uri = latest_geojson_uri(manifest, manifest_uri=manifest_uri)
    subscriptions = load_subscriptions_from_json(args.subscriptions_json)
    centroids = load_zip_centroids_from_json(args.zip_centroids_json)
    cells = load_risk_cells(geojson_uri)
    candidates = evaluate_alerts(
        subscriptions,
        centroids,
        cells,
        default_threshold=args.default_threshold,
    )

    print(
        json.dumps(
            {
                "dry_run": True,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "manifest_uri": manifest_uri,
                "geojson_uri": geojson_uri,
                "subscriptions": len(subscriptions),
                "risk_cells": len(cells),
                "alert_candidates": [
                    {
                        "email": candidate.subscription.email,
                        "zip": candidate.subscription.zip,
                        "threshold": candidate.threshold,
                        "risk_probability": candidate.risk_cell.risk_probability,
                        "window_start_date": candidate.risk_cell.window_start_date,
                        "model_version": candidate.risk_cell.model_version,
                        "nearest_cell_distance_km": round(candidate.distance_km, 3),
                    }
                    for candidate in candidates
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
