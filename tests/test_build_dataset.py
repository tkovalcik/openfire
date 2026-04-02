from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from common.storage import StorageClient
from data_pipeline.build_dataset import DATASET_SCHEMA_VERSION, assemble_dataset
from conftest import FakeGCSClient


def build_feature_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "pixel_id": ["p1", "p2"],
            "latitude": [37.70, 37.71],
            "longitude": [-122.40, -122.41],
            "date_window_start": ["2024-06-01", "2024-06-01"],
            "date_window_end": ["2024-06-30", "2024-06-30"],
            "NDVI": [0.4, 0.5],
            "EVI": [0.3, 0.2],
            "NDWI": [0.1, 0.0],
            "NBR": [0.2, 0.1],
            "B2": [100, 101],
            "B3": [110, 111],
            "B4": [120, 121],
            "B8": [130, 131],
            "B11": [140, 141],
            "B12": [150, None],
            "elevation": [100.0, 120.0],
            "slope": [5.0, 7.0],
            "aspect": [180.0, 190.0],
            "mean_ndvi_100m": [0.42, 0.52],
            "mean_ndvi_500m": [0.39, 0.49],
        }
    )


def build_label_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "pixel_id": ["p1", "p2"],
            "date_window_start": ["2024-06-01", "2024-06-01"],
            "date_window_end": ["2024-06-30", "2024-06-30"],
            "target_burned": [1, 0],
        }
    )


def build_weather_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date_window_start": ["2024-06-01"],
            "date_window_end": ["2024-06-30"],
            "weather_schema_version": ["openfire.weather.v1"],
            "weather_source": ["open-meteo-archive"],
            "weather_location_mode": ["aoi_centroid"],
            "weather_latitude": [37.705],
            "weather_longitude": [-122.405],
            "precip_7d_sum": [2.0],
            "precip_30d_sum": [10.0],
            "temp_7d_mean": [19.5],
            "temp_30d_mean": [18.0],
            "humidity_7d_mean": [55.0],
            "humidity_30d_mean": [60.0],
            "wind_7d_max": [11.0],
            "wind_30d_max": [14.0],
        }
    )


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    features_path = tmp_path / "features.csv"
    labels_path = tmp_path / "labels.csv"
    weather_path = tmp_path / "weather.csv"
    build_feature_frame().to_csv(features_path, index=False)
    build_label_frame().to_csv(labels_path, index=False)
    build_weather_frame().to_csv(weather_path, index=False)
    return features_path, labels_path, weather_path


def test_assemble_dataset_writes_parquet_and_manifest(tmp_path: Path, capsys) -> None:
    features_path, labels_path, weather_path = write_inputs(tmp_path)
    dataset_path = tmp_path / "assembled.parquet"
    manifest_path = tmp_path / "manifest.json"

    result = assemble_dataset(
        features_uri=f"local://{features_path.as_posix()}",
        labels_uri=f"local://{labels_path.as_posix()}",
        weather_uri=f"local://{weather_path.as_posix()}",
        dataset_uri=f"local://{dataset_path.as_posix()}",
        manifest_uri=f"local://{manifest_path.as_posix()}",
    )

    stdout = capsys.readouterr().out
    parquet_frame = pd.read_parquet(dataset_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert dataset_path.exists()
    assert manifest_path.exists()
    assert result.manifest["dataset_schema_version"] == DATASET_SCHEMA_VERSION
    assert result.dataset_uri.endswith("assembled.parquet")
    assert parquet_frame.columns.tolist()[0:7] == [
        "dataset_schema_version",
        "ee_feature_version",
        "label_schema_version",
        "weather_schema_version",
        "weather_source",
        "weather_location_mode",
        "pixel_id",
    ]
    assert parquet_frame.loc[1, "B12"] == pytest.approx(150.0)
    assert manifest["missing_value_strategy"]["B12"]["missing_count"] == 1
    assert '"rows": 2' in stdout


def test_assemble_dataset_supports_gcs_output(tmp_path: Path) -> None:
    features_path, labels_path, weather_path = write_inputs(tmp_path)
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=FakeGCSClient())
    storage.upload_file(features_path, "gs://openfire-bucket/openfire/features/test/features.csv")
    storage.upload_file(labels_path, "gs://openfire-bucket/openfire/datasets/raw/frap/test/labels.csv")
    storage.upload_file(weather_path, "gs://openfire-bucket/openfire/features/test/weather.csv")

    result = assemble_dataset(
        features_uri="gs://openfire-bucket/openfire/features/test/features.csv",
        labels_uri="gs://openfire-bucket/openfire/datasets/raw/frap/test/labels.csv",
        weather_uri="gs://openfire-bucket/openfire/features/test/weather.csv",
        dataset_uri="gs://openfire-bucket/openfire/datasets/processed/openfire/test/dataset.parquet",
        manifest_uri="gs://openfire-bucket/openfire/datasets/processed/openfire/test/manifest.json",
        storage=storage,
        emit_stdout=False,
    )

    assert result.dataset_uri.startswith("gs://openfire-bucket/")
    assert storage.exists(result.dataset_uri)
    assert storage.exists(result.manifest_uri)


def test_assemble_dataset_rejects_duplicate_rows(tmp_path: Path) -> None:
    features = build_feature_frame()
    features = pd.concat([features, features.iloc[[0]]], ignore_index=True)
    labels = build_label_frame()
    weather = build_weather_frame()

    features_path = tmp_path / "features.csv"
    labels_path = tmp_path / "labels.csv"
    weather_path = tmp_path / "weather.csv"
    features.to_csv(features_path, index=False)
    labels.to_csv(labels_path, index=False)
    weather.to_csv(weather_path, index=False)

    with pytest.raises(ValueError, match="duplicate_identity_rows"):
        assemble_dataset(
            features_uri=f"local://{features_path.as_posix()}",
            labels_uri=f"local://{labels_path.as_posix()}",
            weather_uri=f"local://{weather_path.as_posix()}",
            dataset_uri=f"local://{(tmp_path / 'assembled.parquet').as_posix()}",
            manifest_uri=f"local://{(tmp_path / 'manifest.json').as_posix()}",
            emit_stdout=False,
        )


def test_assemble_dataset_rejects_out_of_range_ndvi(tmp_path: Path) -> None:
    features_path, labels_path, weather_path = write_inputs(tmp_path)
    features = pd.read_csv(features_path)
    features.loc[0, "NDVI"] = 1.5
    features.to_csv(features_path, index=False)

    with pytest.raises(ValueError, match="out_of_range_ndvi_rows"):
        assemble_dataset(
            features_uri=f"local://{features_path.as_posix()}",
            labels_uri=f"local://{labels_path.as_posix()}",
            weather_uri=f"local://{weather_path.as_posix()}",
            dataset_uri=f"local://{(tmp_path / 'assembled.parquet').as_posix()}",
            manifest_uri=f"local://{(tmp_path / 'manifest.json').as_posix()}",
            emit_stdout=False,
        )


def test_assemble_dataset_rejects_impossible_lat_long(tmp_path: Path) -> None:
    features_path, labels_path, weather_path = write_inputs(tmp_path)
    features = pd.read_csv(features_path)
    features.loc[0, "latitude"] = 95.0
    features.to_csv(features_path, index=False)

    with pytest.raises(ValueError, match="impossible_lat_long_rows"):
        assemble_dataset(
            features_uri=f"local://{features_path.as_posix()}",
            labels_uri=f"local://{labels_path.as_posix()}",
            weather_uri=f"local://{weather_path.as_posix()}",
            dataset_uri=f"local://{(tmp_path / 'assembled.parquet').as_posix()}",
            manifest_uri=f"local://{(tmp_path / 'manifest.json').as_posix()}",
            emit_stdout=False,
        )


def test_assemble_dataset_rejects_missing_target(tmp_path: Path) -> None:
    features_path, labels_path, weather_path = write_inputs(tmp_path)
    labels = pd.read_csv(labels_path)
    labels.loc[0, "target_burned"] = None
    labels.to_csv(labels_path, index=False)

    with pytest.raises(ValueError, match="missing_target_rows"):
        assemble_dataset(
            features_uri=f"local://{features_path.as_posix()}",
            labels_uri=f"local://{labels_path.as_posix()}",
            weather_uri=f"local://{weather_path.as_posix()}",
            dataset_uri=f"local://{(tmp_path / 'assembled.parquet').as_posix()}",
            manifest_uri=f"local://{(tmp_path / 'manifest.json').as_posix()}",
            emit_stdout=False,
        )
