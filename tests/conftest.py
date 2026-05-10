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
    from serving.schemas import FEATURE_COLUMNS

    model = DummyClassifier(strategy="prior")
    # Two arbitrary rows shaped to FEATURE_COLUMNS — values don't matter for
    # DummyClassifier(strategy="prior"); it only uses the labels.
    training_rows = [[float(i) for i in range(len(FEATURE_COLUMNS))] for _ in range(2)]
    labels = [0, 1]
    model.fit(training_rows, labels)

    bundle = {
        "model": model,
        "feature_columns": list(FEATURE_COLUMNS),
        "dataset_version_info": {
            "dataset_schema_version": ["openfire.dataset.v1"],
            "bq_table": ["msds603-mlops-project.openfire_features.gold_features"],
            "gcs_parquet_prefix": ["gs://openfire/openfire/datasets/gold/"],
        },
        "decision_threshold": 0.5,
        "split_strategy": "year",
        "split_group_column": "window_start_date",
        "validation_groups": ["2024"],
        "model_version": "test-bundle-v1",
    }
    output_path = tmp_path / "model.joblib"
    joblib.dump(bundle, output_path)
    return output_path
