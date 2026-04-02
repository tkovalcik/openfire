from __future__ import annotations

import sys
from pathlib import Path

import joblib
from sklearn.dummy import DummyClassifier


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeGCSBlob:
    def __init__(self, store: dict[tuple[str, str], bytes], bucket: str, name: str) -> None:
        self.store = store
        self.bucket = bucket
        self.name = name

    def exists(self) -> bool:
        return (self.bucket, self.name) in self.store

    def download_as_bytes(self) -> bytes:
        return self.store[(self.bucket, self.name)]

    def upload_from_string(self, payload, content_type=None) -> None:  # noqa: ANN001
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.store[(self.bucket, self.name)] = payload


class FakeGCSBucket:
    def __init__(self, store: dict[tuple[str, str], bytes], bucket_name: str) -> None:
        self.store = store
        self.bucket_name = bucket_name

    def blob(self, name: str) -> FakeGCSBlob:
        return FakeGCSBlob(self.store, self.bucket_name, name)


class FakeGCSClient:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}

    def bucket(self, bucket_name: str) -> FakeGCSBucket:
        return FakeGCSBucket(self.store, bucket_name)


def build_model_bundle_path(tmp_path: Path) -> Path:
    model = DummyClassifier(strategy="prior")
    training_rows = [
        [37.70, -122.40, 0.3, 0.2, 0.1, 0.0, 100, 110, 120, 130, 140, 150, 200, 4, 180, 0.31, 0.29, 2, 10, 18, 17, 55, 60, 9, 11],
        [37.80, -122.30, 0.6, 0.4, 0.0, 0.2, 101, 111, 121, 131, 141, 151, 250, 6, 200, 0.62, 0.55, 1, 8, 24, 22, 35, 40, 13, 15],
    ]
    labels = [0, 1]
    model.fit(training_rows, labels)

    bundle = {
        "model": model,
        "feature_columns": [
            "latitude",
            "longitude",
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
            "precip_7d_sum",
            "precip_30d_sum",
            "temp_7d_mean",
            "temp_30d_mean",
            "humidity_7d_mean",
            "humidity_30d_mean",
            "wind_7d_max",
            "wind_30d_max",
        ],
        "dataset_version_info": {
            "dataset_schema_version": ["openfire.dataset.v1"],
            "ee_feature_version": ["openfire.ee.v1"],
            "label_schema_version": ["openfire.labels.frap.v1"],
            "weather_schema_version": ["openfire.weather.v1"],
        },
        "decision_threshold": 0.5,
        "split_strategy": "year",
        "split_group_column": "derived_year",
        "validation_groups": ["2024"],
        "model_version": "test-bundle-v1",
    }
    output_path = tmp_path / "model.joblib"
    joblib.dump(bundle, output_path)
    return output_path
