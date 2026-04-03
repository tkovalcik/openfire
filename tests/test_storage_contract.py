from __future__ import annotations
from pathlib import Path

import pandas as pd

from common.paths import (
    build_version_id,
    demo_features_uri,
    demo_geojson_uri,
    demo_model_bundle_uri,
    mlflow_artifact_root_uri,
    monitoring_report_uri,
    processed_dataset_uri,
    registered_model_uri,
)
from common.storage import StorageClient, normalize_storage_uri, parse_storage_uri
from config.settings import Settings
from monitoring.io_utils import write_monitoring_outputs
from conftest import FakeGCSClient


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        gcp_project_id="openfire-project",
        gcs_bucket="openfire-bucket",
        storage_root_prefix="openfire",
        dataset_prefix="datasets",
        feature_prefix="features",
        training_prefix="training",
        model_prefix="models",
        monitoring_prefix="monitoring",
        mlflow_artifact_prefix="mlflow-artifacts",
        local_cache_dir=tmp_path / ".cache",
        runtime_mode="demo",
        model_source="gcs",
        model_uri=None,
        demo_model_uri="gs://openfire-bucket/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib",
        demo_features_uri="gs://openfire-bucket/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv",
        demo_geojson_uri="gs://openfire-bucket/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson",
        mlflow_tracking_uri=None,
        mlflow_registered_model_name="openfire-baseline",
        mlflow_model_stage="production",
        mlflow_experiment_name="openfire",
        serving_max_batch_size=1000,
        cors_allowed_origins=("http://127.0.0.1:8080",),
        model_load_retries=3,
        model_load_backoff_seconds=2.0,
    )


def test_storage_uri_parsing_and_local_round_trip(tmp_path: Path) -> None:
    storage = StorageClient(local_cache_dir=tmp_path / ".cache")
    uri = normalize_storage_uri(tmp_path / "sample.json")
    storage.write_json(uri, {"ok": True})

    parsed = parse_storage_uri(uri)

    assert parsed.scheme == "local"
    assert storage.exists(uri)
    assert storage.read_json(uri) == {"ok": True}


def test_storage_gcs_round_trip_and_parquet_support(tmp_path: Path) -> None:
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=FakeGCSClient())
    frame = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    uri = "gs://openfire-bucket/openfire/datasets/processed/demo/ds-1/dataset.parquet"

    storage.write_parquet(frame, uri, index=False)
    round_trip = storage.read_parquet(uri)

    assert storage.exists(uri)
    assert round_trip.to_dict(orient="records") == frame.to_dict(orient="records")


def test_path_helpers_build_canonical_gcs_layout(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)

    dataset_uri = processed_dataset_uri(settings, "baseline", "ds-20260401T000000Z")
    model_uri = registered_model_uri(settings, "openfire-baseline", "production")
    report_uri = monitoring_report_uri(settings, "drift", "run-1", filename="report.html")
    demo_model_uri_value = demo_model_bundle_uri(settings, "openfire-baseline", "model-20260401T190000Z")
    demo_features_uri_value = demo_features_uri(settings, "sample-bayarea", "ds-20260401T190000Z")
    demo_geojson_uri_value = demo_geojson_uri(settings, "risk-layer", "demo-20260401T190000Z")

    assert dataset_uri == "gs://openfire-bucket/openfire/datasets/processed/baseline/ds-20260401T000000Z/dataset.parquet"
    assert model_uri == "gs://openfire-bucket/openfire/models/registered/openfire-baseline/production/model.joblib"
    assert report_uri == "gs://openfire-bucket/openfire/monitoring/reports/drift/run-1/report.html"
    assert demo_model_uri_value == "gs://openfire-bucket/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib"
    assert demo_features_uri_value == "gs://openfire-bucket/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv"
    assert demo_geojson_uri_value == "gs://openfire-bucket/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson"
    assert mlflow_artifact_root_uri(settings) == "gs://openfire-bucket/openfire/mlflow-artifacts"
    assert build_version_id("train").startswith("train-")


def test_monitoring_outputs_write_to_storage(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=FakeGCSClient())

    output = write_monitoring_outputs(
        report_name="drift",
        run_id="run-42",
        html_report="<html>ok</html>",
        metadata={"status": "ok"},
        settings=settings,
        storage=storage,
    )

    assert output["html_uri"].startswith("gs://openfire-bucket/openfire/monitoring/reports/drift/run-42/")
    assert storage.read_text(output["html_uri"]) == "<html>ok</html>"
    assert storage.read_json(output["metadata_uri"]) == {"status": "ok"}
