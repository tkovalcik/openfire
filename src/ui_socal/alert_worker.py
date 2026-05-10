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
import os
from pathlib import Path
from typing import Any, Iterable
import urllib.request


LOGGER = logging.getLogger(__name__)

DEFAULT_MANIFEST_URI = "gs://openfire/predictions/manifest.json"
DEFAULT_THRESHOLD = 0.5
DEFAULT_BQ_PROJECT = "msds603-mlops-project"
DEFAULT_SUBS_BQ_DATASET = "openfire_features"
DEFAULT_SUBS_BQ_TABLE = "ui_subscriptions"
DEFAULT_ZIP_CENTROIDS_BQ_DATASET = "openfire_features"
DEFAULT_ZIP_CENTROIDS_BQ_TABLE = "zip_centroids"
DEFAULT_ALERT_LOG_BQ_DATASET = "openfire_features"
DEFAULT_ALERT_LOG_BQ_TABLE = "ui_alert_deliveries"
DEFAULT_ALERT_COOLDOWN_HOURS = 120
DEFAULT_APP_URL = "https://openfire-ui-socal-deckgl-222683846563.us-central1.run.app"
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


@dataclass(frozen=True)
class EmailAlert:
    to_email: str
    from_email: str
    subject: str
    text_body: str


@dataclass(frozen=True)
class AlertLogKey:
    email: str
    zip: str
    window_start_date: str


def _coerce_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric; got {value!r}") from exc


def _row_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    return row[key]


def load_subscriptions_from_json(path: str | Path) -> list[Subscription]:
    """Load subscriptions from a JSON fixture for local dry-runs and tests."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload["subscriptions"] if isinstance(payload, dict) else payload
    subscriptions: list[Subscription] = []
    for row in rows:
        subscriptions.append(
            Subscription(
                email=str(_row_get(row, "email")).lower().strip(),
                zip=str(_row_get(row, "zip")).strip(),
                risk_threshold=(
                    None
                    if _row_get(row, "risk_threshold") is None
                    else _coerce_float(
                        _row_get(row, "risk_threshold"),
                        field_name="risk_threshold",
                    )
                ),
            )
        )
    return subscriptions


def subscription_table_id(
    *,
    project: str | None = None,
    dataset: str = DEFAULT_SUBS_BQ_DATASET,
    table: str = DEFAULT_SUBS_BQ_TABLE,
) -> str:
    resolved_project = project or DEFAULT_BQ_PROJECT
    return f"{resolved_project}.{dataset}.{table}"


def zip_centroid_table_id(
    *,
    project: str | None = None,
    dataset: str = DEFAULT_ZIP_CENTROIDS_BQ_DATASET,
    table: str = DEFAULT_ZIP_CENTROIDS_BQ_TABLE,
) -> str:
    resolved_project = project or DEFAULT_BQ_PROJECT
    return f"{resolved_project}.{dataset}.{table}"


def alert_log_table_id(
    *,
    project: str | None = None,
    dataset: str = DEFAULT_ALERT_LOG_BQ_DATASET,
    table: str = DEFAULT_ALERT_LOG_BQ_TABLE,
) -> str:
    resolved_project = project or DEFAULT_BQ_PROJECT
    return f"{resolved_project}.{dataset}.{table}"


def subscriptions_query(table_id: str) -> str:
    """Build the BigQuery query for the latest subscription per email and ZIP."""
    escaped_table_id = table_id.replace("`", "")
    return f"""
SELECT
  LOWER(TRIM(email)) AS email,
  TRIM(zip) AS zip,
  risk_threshold
FROM `{escaped_table_id}`
WHERE email IS NOT NULL
  AND zip IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY LOWER(TRIM(email)), TRIM(zip)
  ORDER BY created_at DESC
) = 1
""".strip()


def zip_centroids_query(table_id: str) -> str:
    """Build the BigQuery query for ZIP centroid lookup rows."""
    escaped_table_id = table_id.replace("`", "")
    return f"""
SELECT
  TRIM(zip) AS zip,
  latitude,
  longitude
FROM `{escaped_table_id}`
WHERE zip IS NOT NULL
  AND latitude IS NOT NULL
  AND longitude IS NOT NULL
""".strip()


def recent_alerts_query(table_id: str, *, cooldown_hours: int) -> str:
    """Build the BigQuery query for alert-delivery cooldown keys."""
    escaped_table_id = table_id.replace("`", "")
    hours = max(0, int(cooldown_hours))
    return f"""
SELECT
  LOWER(TRIM(email)) AS email,
  TRIM(zip) AS zip,
  CAST(window_start_date AS STRING) AS window_start_date
FROM `{escaped_table_id}`
WHERE delivered_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR)
  AND email IS NOT NULL
  AND zip IS NOT NULL
  AND window_start_date IS NOT NULL
""".strip()


def load_subscriptions_from_bigquery(
    table_id: str,
    *,
    client: Any | None = None,
) -> list[Subscription]:
    """Load the current subscription set from BigQuery."""
    if client is None:
        from google.cloud import bigquery

        project = table_id.split(".", 1)[0] if "." in table_id else None
        client = bigquery.Client(project=project)

    subscriptions: list[Subscription] = []
    for row in client.query(subscriptions_query(table_id)).result():
        subscriptions.append(
            Subscription(
                email=str(_row_get(row, "email")).lower().strip(),
                zip=str(_row_get(row, "zip")).strip(),
                risk_threshold=(
                    None
                    if _row_get(row, "risk_threshold") is None
                    else _coerce_float(
                        _row_get(row, "risk_threshold"),
                        field_name="risk_threshold",
                    )
                ),
            )
        )
    return subscriptions


def load_zip_centroids_from_bigquery(
    table_id: str,
    *,
    client: Any | None = None,
) -> dict[str, ZipCentroid]:
    """Load ZIP centroid lookup rows from BigQuery."""
    if client is None:
        from google.cloud import bigquery

        project = table_id.split(".", 1)[0] if "." in table_id else None
        client = bigquery.Client(project=project)

    centroids: dict[str, ZipCentroid] = {}
    for row in client.query(zip_centroids_query(table_id)).result():
        zip_code = str(_row_get(row, "zip")).strip()
        centroids[zip_code] = ZipCentroid(
            zip=zip_code,
            latitude=_coerce_float(_row_get(row, "latitude"), field_name="latitude"),
            longitude=_coerce_float(_row_get(row, "longitude"), field_name="longitude"),
        )
    return centroids


def alert_log_schema():
    from google.cloud import bigquery

    return [
        bigquery.SchemaField("delivered_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("email", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("zip", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("window_start_date", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("risk_probability", "FLOAT64"),
        bigquery.SchemaField("threshold", "FLOAT64"),
        bigquery.SchemaField("provider", "STRING"),
        bigquery.SchemaField("model_version", "STRING"),
    ]


def ensure_alert_log_table(client: Any, table_id: str) -> str:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    schema = alert_log_schema()
    try:
        table = client.get_table(table_id)
    except NotFound:
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(field="delivered_at")
        table.clustering_fields = ["zip", "email", "window_start_date"]
        client.create_table(table, exists_ok=True)
    else:
        existing_fields = {field.name for field in table.schema}
        missing_fields = [field for field in schema if field.name not in existing_fields]
        if missing_fields:
            table.schema = list(table.schema) + missing_fields
            client.update_table(table, ["schema"])
    return table_id


def load_recent_alert_keys_from_bigquery(
    table_id: str,
    *,
    cooldown_hours: int = DEFAULT_ALERT_COOLDOWN_HOURS,
    client: Any | None = None,
) -> set[AlertLogKey]:
    """Load recently delivered alert keys so repeat worker runs do not resend."""
    if client is None:
        from google.cloud import bigquery

        project = table_id.split(".", 1)[0] if "." in table_id else None
        client = bigquery.Client(project=project)

    ensure_alert_log_table(client, table_id)
    return {
        AlertLogKey(
            email=str(_row_get(row, "email")).lower().strip(),
            zip=str(_row_get(row, "zip")).strip(),
            window_start_date=str(_row_get(row, "window_start_date")).strip(),
        )
        for row in client.query(
            recent_alerts_query(table_id, cooldown_hours=cooldown_hours)
        ).result()
    }


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
        zip_code = str(_row_get(row, "zip")).strip()
        out[zip_code] = ZipCentroid(
            zip=zip_code,
            latitude=_coerce_float(_row_get(row, "latitude"), field_name="latitude"),
            longitude=_coerce_float(_row_get(row, "longitude"), field_name="longitude"),
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


def alert_candidate_key(candidate: AlertCandidate) -> AlertLogKey:
    return AlertLogKey(
        email=candidate.subscription.email.lower().strip(),
        zip=candidate.subscription.zip.strip(),
        window_start_date=candidate.risk_cell.window_start_date,
    )


def filter_recent_alerts(
    candidates: Iterable[AlertCandidate],
    recent_keys: set[AlertLogKey],
) -> list[AlertCandidate]:
    return [
        candidate
        for candidate in candidates
        if alert_candidate_key(candidate) not in recent_keys
    ]


def alert_delivery_rows(
    candidates: Iterable[AlertCandidate],
    *,
    provider: str,
) -> list[dict[str, object]]:
    delivered_at = datetime.now(timezone.utc).isoformat()
    return [
        {
            "delivered_at": delivered_at,
            "email": candidate.subscription.email.lower().strip(),
            "zip": candidate.subscription.zip.strip(),
            "window_start_date": candidate.risk_cell.window_start_date,
            "risk_probability": candidate.risk_cell.risk_probability,
            "threshold": candidate.threshold,
            "provider": provider,
            "model_version": candidate.risk_cell.model_version,
        }
        for candidate in candidates
    ]


def record_alert_deliveries_in_bigquery(
    candidates: Iterable[AlertCandidate],
    *,
    table_id: str,
    provider: str,
    client: Any | None = None,
) -> int:
    rows = alert_delivery_rows(candidates, provider=provider)
    if not rows:
        return 0
    if client is None:
        from google.cloud import bigquery

        project = table_id.split(".", 1)[0] if "." in table_id else None
        client = bigquery.Client(project=project)
    ensure_alert_log_table(client, table_id)
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        raise RuntimeError(f"Failed to record alert deliveries: {errors}")
    return len(rows)


def render_alert_email(
    candidate: AlertCandidate,
    *,
    from_email: str,
    app_url: str = DEFAULT_APP_URL,
) -> EmailAlert:
    risk_percent = candidate.risk_cell.risk_probability * 100
    threshold_percent = candidate.threshold * 100
    subject = f"OpenFire wildfire risk alert for ZIP {candidate.subscription.zip}"
    text_body = "\n".join(
        [
            f"OpenFire detected elevated wildfire risk near ZIP {candidate.subscription.zip}.",
            "",
            f"Latest risk probability: {risk_percent:.1f}%",
            f"Your alert threshold: {threshold_percent:.1f}%",
            f"Forecast window start: {candidate.risk_cell.window_start_date or 'unknown'}",
            f"Nearest modeled cell: {candidate.distance_km:.1f} km from ZIP centroid",
            "",
            f"View the map: {app_url}",
        ]
    )
    return EmailAlert(
        to_email=candidate.subscription.email,
        from_email=from_email,
        subject=subject,
        text_body=text_body,
    )


def send_email_via_sendgrid(
    message: EmailAlert,
    *,
    api_key: str,
    urlopen: Any = urllib.request.urlopen,
) -> None:
    payload = {
        "personalizations": [{"to": [{"email": message.to_email}]}],
        "from": {"email": message.from_email},
        "subject": message.subject,
        "content": [{"type": "text/plain", "value": message.text_body}],
    }
    request = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        status = getattr(response, "status", 202)
        if status >= 400:
            raise RuntimeError(f"SendGrid returned HTTP {status}")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate OpenFire alert subscriptions.")
    parser.add_argument("--manifest-uri", default=DEFAULT_MANIFEST_URI)
    parser.add_argument("--subscriptions-json")
    parser.add_argument(
        "--subscriptions-table",
        help=(
            "Fully-qualified BigQuery table id. Defaults can be composed with "
            "--subscriptions-project/--subscriptions-dataset/--subscriptions-table-name."
        ),
    )
    parser.add_argument("--subscriptions-project", default=DEFAULT_BQ_PROJECT)
    parser.add_argument("--subscriptions-dataset", default=DEFAULT_SUBS_BQ_DATASET)
    parser.add_argument("--subscriptions-table-name", default=DEFAULT_SUBS_BQ_TABLE)
    parser.add_argument("--zip-centroids-json")
    parser.add_argument(
        "--zip-centroids-table",
        help=(
            "Fully-qualified BigQuery table id with zip, latitude, and longitude columns. "
            "Defaults can be composed with --zip-centroids-project/"
            "--zip-centroids-dataset/--zip-centroids-table-name."
        ),
    )
    parser.add_argument("--zip-centroids-project", default=DEFAULT_BQ_PROJECT)
    parser.add_argument("--zip-centroids-dataset", default=DEFAULT_ZIP_CENTROIDS_BQ_DATASET)
    parser.add_argument("--zip-centroids-table-name", default=DEFAULT_ZIP_CENTROIDS_BQ_TABLE)
    parser.add_argument("--default-threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--app-url", default=os.getenv("OPENFIRE_ALERT_APP_URL", DEFAULT_APP_URL))
    parser.add_argument("--alert-from-email", default=os.getenv("OPENFIRE_ALERT_FROM_EMAIL"))
    parser.add_argument("--sendgrid-api-key", default=os.getenv("SENDGRID_API_KEY"))
    parser.add_argument("--alert-log-table")
    parser.add_argument("--alert-log-project", default=DEFAULT_BQ_PROJECT)
    parser.add_argument("--alert-log-dataset", default=DEFAULT_ALERT_LOG_BQ_DATASET)
    parser.add_argument("--alert-log-table-name", default=DEFAULT_ALERT_LOG_BQ_TABLE)
    parser.add_argument("--alert-cooldown-hours", type=int, default=DEFAULT_ALERT_COOLDOWN_HOURS)
    parser.add_argument(
        "--disable-alert-log",
        action="store_true",
        help="Do not check or write BigQuery alert delivery history.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send real email alerts through SendGrid. Dry-run is the default.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Evaluate and print candidates without sending notifications.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.subscriptions_json and args.subscriptions_table:
        raise SystemExit("--subscriptions-json and --subscriptions-table cannot both be set.")
    if args.zip_centroids_json and args.zip_centroids_table:
        raise SystemExit("--zip-centroids-json and --zip-centroids-table cannot both be set.")
    manifest_uri = normalize_uri(args.manifest_uri)
    manifest = load_manifest(manifest_uri)
    geojson_uri = latest_geojson_uri(manifest, manifest_uri=manifest_uri)
    if args.subscriptions_json:
        subscriptions = load_subscriptions_from_json(args.subscriptions_json)
        subscriptions_source = normalize_uri(args.subscriptions_json)
    else:
        table_id = args.subscriptions_table or subscription_table_id(
            project=args.subscriptions_project,
            dataset=args.subscriptions_dataset,
            table=args.subscriptions_table_name,
        )
        subscriptions = load_subscriptions_from_bigquery(table_id)
        subscriptions_source = table_id
    if args.zip_centroids_json:
        centroids = load_zip_centroids_from_json(args.zip_centroids_json)
        zip_centroids_source = normalize_uri(args.zip_centroids_json)
    else:
        table_id = args.zip_centroids_table or zip_centroid_table_id(
            project=args.zip_centroids_project,
            dataset=args.zip_centroids_dataset,
            table=args.zip_centroids_table_name,
        )
        centroids = load_zip_centroids_from_bigquery(table_id)
        zip_centroids_source = table_id
    cells = load_risk_cells(geojson_uri)
    candidates = evaluate_alerts(
        subscriptions,
        centroids,
        cells,
        default_threshold=args.default_threshold,
    )
    dry_run = not args.send
    alert_log_table = args.alert_log_table or alert_log_table_id(
        project=args.alert_log_project,
        dataset=args.alert_log_dataset,
        table=args.alert_log_table_name,
    )
    recent_alert_keys: set[AlertLogKey] = set()
    if args.send and not args.disable_alert_log and args.alert_cooldown_hours > 0:
        recent_alert_keys = load_recent_alert_keys_from_bigquery(
            alert_log_table,
            cooldown_hours=args.alert_cooldown_hours,
        )
    eligible_candidates = filter_recent_alerts(candidates, recent_alert_keys)
    from_email = args.alert_from_email or "alerts@openfire.local"
    email_alerts = [
        render_alert_email(candidate, from_email=from_email, app_url=args.app_url)
        for candidate in eligible_candidates
    ]
    sent_count = 0
    recorded_count = 0
    sent_candidates: list[AlertCandidate] = []
    if args.send:
        if not args.alert_from_email:
            raise SystemExit("--alert-from-email or OPENFIRE_ALERT_FROM_EMAIL is required with --send.")
        if not args.sendgrid_api_key:
            raise SystemExit("--sendgrid-api-key or SENDGRID_API_KEY is required with --send.")
        for candidate, email_alert in zip(eligible_candidates, email_alerts, strict=True):
            send_email_via_sendgrid(email_alert, api_key=args.sendgrid_api_key)
            sent_count += 1
            sent_candidates.append(candidate)
        if not args.disable_alert_log:
            recorded_count = record_alert_deliveries_in_bigquery(
                sent_candidates,
                table_id=alert_log_table,
                provider="sendgrid",
            )

    print(
        json.dumps(
            {
                "dry_run": dry_run,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "manifest_uri": manifest_uri,
                "geojson_uri": geojson_uri,
                "subscriptions_source": subscriptions_source,
                "subscriptions": len(subscriptions),
                "zip_centroids_source": zip_centroids_source,
                "zip_centroids": len(centroids),
                "risk_cells": len(cells),
                "delivery": {
                    "provider": "sendgrid",
                    "attempted": len(email_alerts) if args.send else 0,
                    "sent": sent_count,
                    "recorded": recorded_count,
                    "suppressed_by_cooldown": len(candidates) - len(eligible_candidates),
                    "alert_log_table": None if args.disable_alert_log else alert_log_table,
                    "cooldown_hours": args.alert_cooldown_hours,
                },
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
                "email_alerts": [
                    {
                        "to_email": email_alert.to_email,
                        "from_email": email_alert.from_email,
                        "subject": email_alert.subject,
                        "text_body": email_alert.text_body,
                    }
                    for email_alert in email_alerts
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
