from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

try:
    from common.storage import StorageClient, normalize_storage_uri
    from config.settings import get_settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.storage import StorageClient, normalize_storage_uri
    from src.config.settings import get_settings

from .open_meteo_client import OpenMeteoClient, OpenMeteoRequest


LOGGER = logging.getLogger(__name__)

WEATHER_SCHEMA_VERSION = "openfire.weather.v1"
WEATHER_SOURCE = "open-meteo-archive"
WINDOW_START_COLUMN = "date_window_start"
WINDOW_END_COLUMN = "date_window_end"
WEATHER_OUTPUT_COLUMNS = [
    "weather_schema_version",
    "weather_source",
    "weather_location_mode",
    "weather_latitude",
    "weather_longitude",
    "precip_7d_sum",
    "precip_30d_sum",
    "temp_7d_mean",
    "temp_30d_mean",
    "humidity_7d_mean",
    "humidity_30d_mean",
    "wind_7d_max",
    "wind_30d_max",
]


@dataclass(frozen=True)
class WeatherJoinRequest:
    date_window_start: date
    date_window_end: date
    latitude: float
    longitude: float


def validate_dataset_columns(frame: pd.DataFrame) -> None:
    required = {"latitude", "longitude", WINDOW_START_COLUMN, WINDOW_END_COLUMN}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Dataset is missing required columns: {missing}")


def parse_window_date(value: object, column_name: str) -> date:
    parsed = pd.to_datetime(value, errors="raise")
    if pd.isna(parsed):
        raise ValueError(f"Invalid date value for {column_name}: {value}")
    return parsed.date()


def compute_aoi_centroid(frame: pd.DataFrame) -> tuple[float, float]:
    validate_dataset_columns(frame)
    return float(frame["latitude"].mean()), float(frame["longitude"].mean())


def build_join_requests(
    frame: pd.DataFrame,
    *,
    location_mode: Literal["aoi_centroid"] = "aoi_centroid",
    centroid_override: tuple[float, float] | None = None,
) -> list[WeatherJoinRequest]:
    if location_mode != "aoi_centroid":
        raise ValueError(f"Unsupported location_mode: {location_mode}")

    validate_dataset_columns(frame)
    centroid = centroid_override or compute_aoi_centroid(frame)
    deduped = (
        frame[[WINDOW_START_COLUMN, WINDOW_END_COLUMN]]
        .drop_duplicates()
        .sort_values([WINDOW_START_COLUMN, WINDOW_END_COLUMN])
    )

    requests: list[WeatherJoinRequest] = []
    for row in deduped.itertuples(index=False):
        requests.append(
            WeatherJoinRequest(
                date_window_start=parse_window_date(row.date_window_start, WINDOW_START_COLUMN),
                date_window_end=parse_window_date(row.date_window_end, WINDOW_END_COLUMN),
                latitude=float(centroid[0]),
                longitude=float(centroid[1]),
            )
        )
    return requests


def compute_weather_features(
    client: OpenMeteoClient,
    request: WeatherJoinRequest,
    *,
    location_mode: Literal["aoi_centroid"] = "aoi_centroid",
) -> dict[str, object]:
    archive_request = OpenMeteoRequest(
        latitude=request.latitude,
        longitude=request.longitude,
        start_date=request.date_window_end - timedelta(days=29),
        end_date=request.date_window_end,
    )
    daily = client.fetch_daily_weather(archive_request)
    daily["date"] = pd.to_datetime(daily["date"]).dt.date

    window_7d = daily[daily["date"] >= request.date_window_end - timedelta(days=6)]
    window_30d = daily

    if len(window_7d) < 7:
        raise ValueError(
            f"Expected 7 days of weather data through {request.date_window_end}, got {len(window_7d)}"
        )
    if len(window_30d) < 30:
        raise ValueError(
            f"Expected 30 days of weather data through {request.date_window_end}, got {len(window_30d)}"
        )

    return {
        WINDOW_START_COLUMN: request.date_window_start.isoformat(),
        WINDOW_END_COLUMN: request.date_window_end.isoformat(),
        "weather_schema_version": WEATHER_SCHEMA_VERSION,
        "weather_source": WEATHER_SOURCE,
        "weather_location_mode": location_mode,
        "weather_latitude": request.latitude,
        "weather_longitude": request.longitude,
        "precip_7d_sum": float(window_7d["precipitation_sum"].sum()),
        "precip_30d_sum": float(window_30d["precipitation_sum"].sum()),
        "temp_7d_mean": float(window_7d["temperature_2m_mean"].mean()),
        "temp_30d_mean": float(window_30d["temperature_2m_mean"].mean()),
        "humidity_7d_mean": float(window_7d["relative_humidity_2m_mean"].mean()),
        "humidity_30d_mean": float(window_30d["relative_humidity_2m_mean"].mean()),
        "wind_7d_max": float(window_7d["wind_speed_10m_max"].max()),
        "wind_30d_max": float(window_30d["wind_speed_10m_max"].max()),
    }


def enrich_with_weather(
    frame: pd.DataFrame,
    *,
    client: OpenMeteoClient,
    location_mode: Literal["aoi_centroid"] = "aoi_centroid",
    centroid_override: tuple[float, float] | None = None,
) -> pd.DataFrame:
    validate_dataset_columns(frame)

    normalized = frame.copy()
    normalized[WINDOW_START_COLUMN] = pd.to_datetime(normalized[WINDOW_START_COLUMN]).dt.strftime(
        "%Y-%m-%d"
    )
    normalized[WINDOW_END_COLUMN] = pd.to_datetime(normalized[WINDOW_END_COLUMN]).dt.strftime(
        "%Y-%m-%d"
    )

    join_requests = build_join_requests(
        normalized,
        location_mode=location_mode,
        centroid_override=centroid_override,
    )
    LOGGER.info("Building weather features for %s unique date windows", len(join_requests))

    feature_rows = [
        compute_weather_features(client, request, location_mode=location_mode)
        for request in join_requests
    ]
    weather_frame = pd.DataFrame(feature_rows)
    columns = [WINDOW_START_COLUMN, WINDOW_END_COLUMN, *WEATHER_OUTPUT_COLUMNS]
    return normalized.merge(weather_frame[columns], on=[WINDOW_START_COLUMN, WINDOW_END_COLUMN], how="left")


def _build_arg_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Enrich a modeling dataset with Open-Meteo weather features.")
    parser.add_argument(
        "--input-uri",
        "--input",
        dest="input_uri",
        required=True,
        help="CSV storage URI with latitude/longitude/date window columns. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--output-uri",
        "--output",
        dest="output_uri",
        required=True,
        help="Output CSV storage URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--location-mode",
        choices=["aoi_centroid"],
        default="aoi_centroid",
        help="Weather lookup strategy. Baseline uses a single AOI centroid.",
    )
    parser.add_argument("--aoi-latitude", type=float, default=None, help="Optional centroid latitude override.")
    parser.add_argument("--aoi-longitude", type=float, default=None, help="Optional centroid longitude override.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=settings.local_cache_dir / "open_meteo",
        help="Local development cache directory for Open-Meteo responses.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    if (args.aoi_latitude is None) ^ (args.aoi_longitude is None):
        raise SystemExit("Provide both --aoi-latitude and --aoi-longitude, or neither.")

    centroid_override = None
    if args.aoi_latitude is not None and args.aoi_longitude is not None:
        centroid_override = (args.aoi_latitude, args.aoi_longitude)

    storage = StorageClient.from_settings(get_settings())
    input_uri = normalize_storage_uri(args.input_uri)
    output_uri = normalize_storage_uri(args.output_uri)
    if not storage.exists(input_uri):
        raise SystemExit(f"Input dataset not found: {input_uri}")

    frame = storage.read_csv(input_uri)
    client = OpenMeteoClient(
        cache_dir=args.cache_dir,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )

    try:
        enriched = enrich_with_weather(
            frame,
            client=client,
            location_mode=args.location_mode,
            centroid_override=centroid_override,
        )
    except Exception as error:
        LOGGER.exception("Weather enrichment failed")
        raise SystemExit(str(error)) from error

    storage.write_csv(enriched, output_uri, index=False)
    LOGGER.info("Wrote weather-enriched dataset to %s", output_uri)


if __name__ == "__main__":
    main()
