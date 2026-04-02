from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

try:
    from common.storage import StorageClient, normalize_storage_uri
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.storage import StorageClient, normalize_storage_uri

from .weather_features import WEATHER_OUTPUT_COLUMNS, WINDOW_END_COLUMN, WINDOW_START_COLUMN


LOGGER = logging.getLogger(__name__)

DATASET_SCHEMA_VERSION = "openfire.dataset.v1"
DEFAULT_EE_FEATURE_VERSION = "openfire.ee.v1"
DEFAULT_LABEL_SCHEMA_VERSION = "openfire.labels.frap.v1"
DEFAULT_WEATHER_SCHEMA_VERSION = "openfire.weather.v1"

TARGET_COLUMN = "target_burned"
OPTIONAL_ID_COLUMNS = ["pixel_id"]
EE_FEATURE_COLUMNS = [
    "NDVI",
    "EVI",
    "NDWI",
    "NBR",
    "B2",
    "B3",
    "B4",
    "B8",
    "B11",
    "B12",
    "elevation",
    "slope",
    "aspect",
    "mean_ndvi_100m",
    "mean_ndvi_500m",
]
WEATHER_NUMERIC_COLUMNS = [
    "precip_7d_sum",
    "precip_30d_sum",
    "temp_7d_mean",
    "temp_30d_mean",
    "humidity_7d_mean",
    "humidity_30d_mean",
    "wind_7d_max",
    "wind_30d_max",
]
WEATHER_METADATA_COLUMNS = [
    "weather_schema_version",
    "weather_source",
    "weather_location_mode",
    "weather_latitude",
    "weather_longitude",
]
OUTPUT_COLUMN_ORDER = [
    "dataset_schema_version",
    "ee_feature_version",
    "label_schema_version",
    "weather_schema_version",
    "weather_source",
    "weather_location_mode",
    *OPTIONAL_ID_COLUMNS,
    "latitude",
    "longitude",
    WINDOW_START_COLUMN,
    WINDOW_END_COLUMN,
    TARGET_COLUMN,
    *EE_FEATURE_COLUMNS,
    "elevation",
    "slope",
    "aspect",
    "mean_ndvi_100m",
    "mean_ndvi_500m",
    "weather_latitude",
    "weather_longitude",
    *WEATHER_NUMERIC_COLUMNS,
]
LABEL_JOIN_CANDIDATES = [
    ["pixel_id", WINDOW_START_COLUMN, WINDOW_END_COLUMN],
    ["latitude", "longitude", WINDOW_START_COLUMN, WINDOW_END_COLUMN],
]
WEATHER_JOIN_CANDIDATES = [
    ["pixel_id", WINDOW_START_COLUMN, WINDOW_END_COLUMN],
    ["latitude", "longitude", WINDOW_START_COLUMN, WINDOW_END_COLUMN],
    [WINDOW_START_COLUMN, WINDOW_END_COLUMN],
]
REQUIRED_FEATURE_COLUMNS = [
    "latitude",
    "longitude",
    WINDOW_START_COLUMN,
    WINDOW_END_COLUMN,
    *EE_FEATURE_COLUMNS,
]
REQUIRED_LABEL_COLUMNS = [TARGET_COLUMN]
REQUIRED_WEATHER_COLUMNS = [WINDOW_START_COLUMN, WINDOW_END_COLUMN, *WEATHER_NUMERIC_COLUMNS]
NDVI_RANGE_COLUMNS = ["NDVI", "mean_ndvi_100m", "mean_ndvi_500m"]
NON_NULL_COLUMNS = ["latitude", "longitude", WINDOW_START_COLUMN, WINDOW_END_COLUMN, TARGET_COLUMN]
NUMERIC_COLUMNS = [
    "latitude",
    "longitude",
    *EE_FEATURE_COLUMNS,
    "weather_latitude",
    "weather_longitude",
    *WEATHER_NUMERIC_COLUMNS,
]
NUMERIC_FILL_DEFAULTS = {
    "NDVI": 0.0,
    "EVI": 0.0,
    "NDWI": 0.0,
    "NBR": 0.0,
    "B2": 0.0,
    "B3": 0.0,
    "B4": 0.0,
    "B8": 0.0,
    "B11": 0.0,
    "B12": 0.0,
    "elevation": 0.0,
    "slope": 0.0,
    "aspect": 0.0,
    "mean_ndvi_100m": 0.0,
    "mean_ndvi_500m": 0.0,
    "weather_latitude": 0.0,
    "weather_longitude": 0.0,
    "precip_7d_sum": 0.0,
    "precip_30d_sum": 0.0,
    "temp_7d_mean": 0.0,
    "temp_30d_mean": 0.0,
    "humidity_7d_mean": 0.0,
    "humidity_30d_mean": 0.0,
    "wind_7d_max": 0.0,
    "wind_30d_max": 0.0,
}


@dataclass(frozen=True)
class DatasetAssemblyResult:
    dataset_uri: str
    manifest_uri: str
    manifest: dict[str, Any]


def load_csv(storage: StorageClient, uri: str, *, name: str) -> pd.DataFrame:
    if not storage.exists(uri):
        raise FileNotFoundError(f"{name} CSV not found: {uri}")
    frame = storage.read_csv(uri)
    if frame.empty:
        raise ValueError(f"{name} CSV is empty: {uri}")
    return frame


def require_columns(frame: pd.DataFrame, required: list[str], *, name: str) -> None:
    missing = sorted(column for column in required if column not in frame.columns)
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")


def normalize_window_columns(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    for column in [WINDOW_START_COLUMN, WINDOW_END_COLUMN]:
        if column in normalized.columns:
            normalized[column] = pd.to_datetime(normalized[column], errors="raise").dt.strftime(
                "%Y-%m-%d"
            )
    return normalized


def resolve_join_keys(
    left: pd.DataFrame,
    right: pd.DataFrame,
    candidates: list[list[str]],
    *,
    right_name: str,
) -> list[str]:
    for candidate in candidates:
        if all(column in left.columns and column in right.columns for column in candidate):
            duplicate_count = int(right.duplicated(subset=candidate).sum())
            if duplicate_count == 0:
                return candidate
            LOGGER.debug(
                "Skipping %s join keys %s because the right frame has %s duplicates",
                right_name,
                candidate,
                duplicate_count,
            )
    raise ValueError(f"Could not resolve safe join keys for {right_name}")


def prepare_feature_frame(frame: pd.DataFrame, *, ee_feature_version: str) -> pd.DataFrame:
    require_columns(frame, REQUIRED_FEATURE_COLUMNS, name="Earth Engine features")
    if TARGET_COLUMN in frame.columns:
        raise ValueError("Earth Engine feature CSV should not already contain target_burned")
    prepared = normalize_window_columns(frame)
    prepared["dataset_schema_version"] = DATASET_SCHEMA_VERSION
    prepared["ee_feature_version"] = frame.get("ee_feature_version", ee_feature_version)
    return prepared


def prepare_label_frame(frame: pd.DataFrame, *, label_schema_version: str) -> pd.DataFrame:
    require_columns(frame, REQUIRED_LABEL_COLUMNS, name="FRAP labels")
    prepared = normalize_window_columns(frame)
    prepared["label_schema_version"] = frame.get("label_schema_version", label_schema_version)
    return prepared


def prepare_weather_frame(frame: pd.DataFrame, *, weather_schema_version: str) -> pd.DataFrame:
    require_columns(frame, REQUIRED_WEATHER_COLUMNS, name="Weather features")
    prepared = normalize_window_columns(frame)
    if "weather_schema_version" not in prepared.columns:
        prepared["weather_schema_version"] = weather_schema_version
    if "weather_source" not in prepared.columns:
        prepared["weather_source"] = "open-meteo-archive"
    if "weather_location_mode" not in prepared.columns:
        prepared["weather_location_mode"] = "aoi_centroid"
    if "weather_latitude" not in prepared.columns:
        prepared["weather_latitude"] = pd.NA
    if "weather_longitude" not in prepared.columns:
        prepared["weather_longitude"] = pd.NA
    return prepared


def merge_inputs(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    weather: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    label_join_keys = resolve_join_keys(features, labels, LABEL_JOIN_CANDIDATES, right_name="labels")
    weather_join_keys = resolve_join_keys(features, weather, WEATHER_JOIN_CANDIDATES, right_name="weather")

    label_columns = [*label_join_keys, TARGET_COLUMN, "label_schema_version"]
    weather_columns = [*weather_join_keys, *WEATHER_METADATA_COLUMNS, *WEATHER_NUMERIC_COLUMNS]

    merged = features.merge(
        labels[label_columns],
        on=label_join_keys,
        how="left",
        validate="many_to_one",
    ).merge(
        weather[weather_columns],
        on=weather_join_keys,
        how="left",
        validate="many_to_one",
    )

    return merged, {
        "label_join_keys": label_join_keys,
        "weather_join_keys": weather_join_keys,
    }


def determine_identity_columns(frame: pd.DataFrame) -> list[str]:
    for candidate in LABEL_JOIN_CANDIDATES:
        if all(column in frame.columns for column in candidate):
            return candidate
    return ["latitude", "longitude", WINDOW_START_COLUMN, WINDOW_END_COLUMN]


def coerce_numeric_columns(frame: pd.DataFrame) -> pd.DataFrame:
    coerced = frame.copy()
    for column in NUMERIC_COLUMNS:
        if column in coerced.columns:
            coerced[column] = pd.to_numeric(coerced[column], errors="coerce")
    if TARGET_COLUMN in coerced.columns:
        coerced[TARGET_COLUMN] = pd.to_numeric(coerced[TARGET_COLUMN], errors="coerce")
    return coerced


def validate_data_quality(frame: pd.DataFrame) -> dict[str, int]:
    identity_columns = determine_identity_columns(frame)
    duplicate_row_count = int(frame.duplicated(subset=identity_columns).sum())
    duplicate_full_row_count = int(frame.duplicated().sum())
    invalid_ndvi_count = int(
        (
            pd.concat(
                [
                    frame[column].dropna().pipe(lambda values: (values < -1.0) | (values > 1.0))
                    for column in NDVI_RANGE_COLUMNS
                    if column in frame.columns
                ],
                axis=0,
            ).sum()
            if any(column in frame.columns for column in NDVI_RANGE_COLUMNS)
            else 0
        )
    )
    invalid_lat_long_count = int(
        (
            (frame["latitude"] < -90.0)
            | (frame["latitude"] > 90.0)
            | (frame["longitude"] < -180.0)
            | (frame["longitude"] > 180.0)
        ).sum()
    )
    missing_target_count = int(frame[TARGET_COLUMN].isna().sum())

    metrics = {
        "duplicate_identity_rows": duplicate_row_count,
        "duplicate_full_rows": duplicate_full_row_count,
        "out_of_range_ndvi_rows": invalid_ndvi_count,
        "impossible_lat_long_rows": invalid_lat_long_count,
        "missing_target_rows": missing_target_count,
    }
    errors = [name for name, count in metrics.items() if count > 0]
    if errors:
        raise ValueError(
            "Data quality validation failed: "
            + ", ".join(f"{name}={metrics[name]}" for name in errors)
        )
    return metrics


def apply_missing_value_strategy(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    filled = frame.copy()
    report: dict[str, dict[str, Any]] = {}

    for column in NON_NULL_COLUMNS:
        if filled[column].isna().any():
            raise ValueError(f"Required column contains missing values: {column}")

    string_defaults = {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "ee_feature_version": DEFAULT_EE_FEATURE_VERSION,
        "label_schema_version": DEFAULT_LABEL_SCHEMA_VERSION,
        "weather_schema_version": DEFAULT_WEATHER_SCHEMA_VERSION,
        "weather_source": "open-meteo-archive",
        "weather_location_mode": "aoi_centroid",
    }
    for column, default_value in string_defaults.items():
        if column not in filled.columns:
            filled[column] = default_value
        missing_count = int(filled[column].isna().sum())
        if missing_count:
            filled[column] = filled[column].fillna(default_value)
        report[column] = {
            "strategy": "fill_constant",
            "missing_count": missing_count,
            "fill_value": default_value,
        }

    for column in NUMERIC_COLUMNS:
        if column not in filled.columns:
            continue
        missing_count = int(filled[column].isna().sum())
        series = filled[column]
        default_value = NUMERIC_FILL_DEFAULTS.get(column)
        if default_value is None:
            report[column] = {
                "strategy": "no_fill_required",
                "missing_count": missing_count,
                "fill_value": None,
            }
            continue
        median_value = float(series.median()) if series.notna().any() else default_value
        fill_value = median_value if pd.notna(median_value) else default_value
        if missing_count:
            filled[column] = series.fillna(fill_value)
        report[column] = {
            "strategy": "fill_median_or_default",
            "missing_count": missing_count,
            "fill_value": fill_value,
        }

    filled[TARGET_COLUMN] = filled[TARGET_COLUMN].astype(int)
    return filled, report


def enforce_output_schema(frame: pd.DataFrame) -> pd.DataFrame:
    present_optional_ids = [column for column in OPTIONAL_ID_COLUMNS if column in frame.columns]
    ordered_columns = [
        "dataset_schema_version",
        "ee_feature_version",
        "label_schema_version",
        "weather_schema_version",
        "weather_source",
        "weather_location_mode",
        *present_optional_ids,
        "latitude",
        "longitude",
        WINDOW_START_COLUMN,
        WINDOW_END_COLUMN,
        TARGET_COLUMN,
        *EE_FEATURE_COLUMNS,
        "weather_latitude",
        "weather_longitude",
        *WEATHER_NUMERIC_COLUMNS,
    ]
    missing = [column for column in ordered_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Assembled dataset is missing required output columns: {missing}")
    return frame[ordered_columns].copy()


def build_manifest(
    *,
    features_uri: str,
    labels_uri: str,
    weather_uri: str,
    dataset_uri: str,
    manifest_uri: str,
    join_details: dict[str, list[str]],
    frame: pd.DataFrame,
    validation_report: dict[str, int],
    missing_value_report: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "earth_engine_features_csv": features_uri,
            "frap_labels_csv": labels_uri,
            "weather_features_csv": weather_uri,
        },
        "output": {
            "dataset_parquet": dataset_uri,
            "manifest_json": manifest_uri,
            "row_count": int(len(frame)),
            "column_count": int(len(frame.columns)),
            "columns": list(frame.columns),
        },
        "feature_versions": {
            "dataset_schema_version": DATASET_SCHEMA_VERSION,
            "ee_feature_version": sorted(frame["ee_feature_version"].dropna().astype(str).unique().tolist()),
            "label_schema_version": sorted(frame["label_schema_version"].dropna().astype(str).unique().tolist()),
            "weather_schema_version": sorted(
                frame["weather_schema_version"].dropna().astype(str).unique().tolist()
            ),
        },
        "joins": join_details,
        "validation": validation_report,
        "missing_value_strategy": missing_value_report,
        "summary": {
            "rows": int(len(frame)),
            "columns": int(len(frame.columns)),
            "positive_targets": int(frame[TARGET_COLUMN].sum()),
            "negative_targets": int((frame[TARGET_COLUMN] == 0).sum()),
        },
    }


def write_parquet(storage: StorageClient, frame: pd.DataFrame, uri: str) -> None:
    try:
        storage.write_parquet(frame, uri, index=False, engine="pyarrow")
    except ImportError as error:
        raise RuntimeError(
            "Parquet export requires pyarrow. Install pyarrow before running build_dataset."
        ) from error


def assemble_dataset(
    *,
    features_uri: str,
    labels_uri: str,
    weather_uri: str,
    dataset_uri: str,
    manifest_uri: str,
    ee_feature_version: str = DEFAULT_EE_FEATURE_VERSION,
    label_schema_version: str = DEFAULT_LABEL_SCHEMA_VERSION,
    weather_schema_version: str = DEFAULT_WEATHER_SCHEMA_VERSION,
    emit_stdout: bool = True,
    storage: StorageClient | None = None,
) -> DatasetAssemblyResult:
    resolved_storage = storage or StorageClient()
    normalized_features_uri = normalize_storage_uri(features_uri)
    normalized_labels_uri = normalize_storage_uri(labels_uri)
    normalized_weather_uri = normalize_storage_uri(weather_uri)
    normalized_dataset_uri = normalize_storage_uri(dataset_uri)
    normalized_manifest_uri = normalize_storage_uri(manifest_uri)

    features = prepare_feature_frame(
        load_csv(resolved_storage, normalized_features_uri, name="Earth Engine features"),
        ee_feature_version=ee_feature_version,
    )
    labels = prepare_label_frame(
        load_csv(resolved_storage, normalized_labels_uri, name="FRAP labels"),
        label_schema_version=label_schema_version,
    )
    weather = prepare_weather_frame(
        load_csv(resolved_storage, normalized_weather_uri, name="Weather features"),
        weather_schema_version=weather_schema_version,
    )

    merged, join_details = merge_inputs(features, labels, weather)
    merged = coerce_numeric_columns(merged)
    validation_report = validate_data_quality(merged)
    merged, missing_value_report = apply_missing_value_strategy(merged)
    assembled = enforce_output_schema(merged)

    write_parquet(resolved_storage, assembled, normalized_dataset_uri)

    manifest = build_manifest(
        features_uri=normalized_features_uri,
        labels_uri=normalized_labels_uri,
        weather_uri=normalized_weather_uri,
        dataset_uri=normalized_dataset_uri,
        manifest_uri=normalized_manifest_uri,
        join_details=join_details,
        frame=assembled,
        validation_report=validation_report,
        missing_value_report=missing_value_report,
    )
    resolved_storage.write_json(normalized_manifest_uri, manifest)
    if emit_stdout:
        print(json.dumps(manifest["summary"], indent=2))

    return DatasetAssemblyResult(
        dataset_uri=normalized_dataset_uri,
        manifest_uri=normalized_manifest_uri,
        manifest=manifest,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assemble Earth Engine features, FRAP labels, and weather features into a parquet dataset."
    )
    parser.add_argument(
        "--features-uri",
        "--features",
        dest="features_uri",
        required=True,
        help="Earth Engine feature CSV storage URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--labels-uri",
        "--labels",
        dest="labels_uri",
        required=True,
        help="FRAP label CSV storage URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--weather-uri",
        "--weather",
        dest="weather_uri",
        required=True,
        help="Weather feature CSV storage URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--out-parquet-uri",
        "--out-parquet",
        dest="out_parquet_uri",
        required=True,
        help="Output parquet storage URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--out-manifest-uri",
        "--out-manifest",
        dest="out_manifest_uri",
        required=True,
        help="Output JSON manifest storage URI. Supports local:// and gs://.",
    )
    parser.add_argument("--ee-feature-version", default=DEFAULT_EE_FEATURE_VERSION)
    parser.add_argument("--label-schema-version", default=DEFAULT_LABEL_SCHEMA_VERSION)
    parser.add_argument("--weather-schema-version", default=DEFAULT_WEATHER_SCHEMA_VERSION)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    try:
        assemble_dataset(
            features_uri=args.features_uri,
            labels_uri=args.labels_uri,
            weather_uri=args.weather_uri,
            dataset_uri=args.out_parquet_uri,
            manifest_uri=args.out_manifest_uri,
            ee_feature_version=args.ee_feature_version,
            label_schema_version=args.label_schema_version,
            weather_schema_version=args.weather_schema_version,
            emit_stdout=True,
        )
    except Exception as error:
        LOGGER.exception("Dataset assembly failed")
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
