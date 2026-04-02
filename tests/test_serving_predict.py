from __future__ import annotations

from pathlib import Path

import pytest

from common.storage import StorageClient
from serving.model_loader import load_active_model, load_model_bundle_from_uri
from serving.predict import (
    ModelLoadError,
    PredictionInputError,
    PredictionService,
    ServiceConfig,
)
from serving.schemas import FeatureRow, PredictionRequest
from conftest import FakeGCSClient, build_model_bundle_path


def build_request(row_count: int = 1) -> PredictionRequest:
    row = FeatureRow(
        latitude=37.7,
        longitude=-122.4,
        NDVI=0.3,
        EVI=0.2,
        NDWI=0.1,
        NBR=0.0,
        B2=100.0,
        B3=110.0,
        B4=120.0,
        B8=130.0,
        B11=140.0,
        B12=150.0,
        elevation=200.0,
        slope=4.0,
        aspect=180.0,
        mean_ndvi_100m=0.31,
        mean_ndvi_500m=0.29,
        precip_7d_sum=2.0,
        precip_30d_sum=10.0,
        temp_7d_mean=18.0,
        temp_30d_mean=17.0,
        humidity_7d_mean=55.0,
        humidity_30d_mean=60.0,
        wind_7d_max=9.0,
        wind_30d_max=11.0,
    )
    return PredictionRequest(rows=[row for _ in range(row_count)])


def test_load_model_bundle_from_local_uri_reads_metadata(tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)

    loaded = load_model_bundle_from_uri(
        f"local://{bundle_path.as_posix()}",
        storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
        model_source="local",
    )

    assert loaded.model_source == "local"
    assert loaded.model_version == "test-bundle-v1"
    assert loaded.feature_columns[0] == "latitude"
    assert loaded.dataset_version_info["dataset_schema_version"] == ["openfire.dataset.v1"]


def test_load_active_model_supports_gcs_source(tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)
    gcs_client = FakeGCSClient()
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=gcs_client)
    storage.upload_file(bundle_path, "gs://openfire-bucket/openfire/models/registered/openfire-baseline/production/model.joblib")

    config = ServiceConfig(
        runtime_mode="live",
        model_source="gcs",
        model_uri="gs://openfire-bucket/openfire/models/registered/openfire-baseline/production/model.joblib",
        demo_geojson_uri=None,
        mlflow_tracking_uri=None,
        mlflow_registered_model_name=None,
        mlflow_model_stage="production",
        max_batch_size=100,
        local_cache_dir=tmp_path / ".cache",
        model_load_retries=1,
        model_load_backoff_seconds=0.0,
    )

    loaded = load_active_model(config, storage=storage)

    assert loaded.model_source == "gcs"
    assert loaded.model_uri == config.model_uri


def test_load_active_model_uses_mlflow_when_configured(monkeypatch, tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)
    config = ServiceConfig(
        runtime_mode="live",
        model_source="mlflow",
        model_uri=None,
        demo_geojson_uri=None,
        mlflow_tracking_uri="http://mlflow.example",
        mlflow_registered_model_name="openfire-prod",
        mlflow_model_stage="production",
        max_batch_size=100,
        local_cache_dir=tmp_path / ".cache",
        model_load_retries=1,
        model_load_backoff_seconds=0.0,
    )

    called = {}

    def fake_registry_loader(*, tracking_uri: str, model_name: str, model_stage: str):
        called["tracking_uri"] = tracking_uri
        called["model_name"] = model_name
        called["model_stage"] = model_stage
        return load_model_bundle_from_uri(
            f"local://{bundle_path.as_posix()}",
            storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
            model_source="local",
        )

    monkeypatch.setattr("serving.model_loader.load_registry_model_bundle", fake_registry_loader)

    loaded = load_active_model(config, storage=StorageClient(local_cache_dir=tmp_path / ".cache"))

    assert called == {
        "tracking_uri": "http://mlflow.example",
        "model_name": "openfire-prod",
        "model_stage": "production",
    }
    assert loaded.model_version == "test-bundle-v1"


def test_load_active_model_retries_before_failing(monkeypatch, tmp_path: Path) -> None:
    config = ServiceConfig(
        runtime_mode="live",
        model_source="gcs",
        model_uri="gs://openfire-bucket/openfire/models/demo/model.joblib",
        demo_geojson_uri=None,
        mlflow_tracking_uri=None,
        mlflow_registered_model_name=None,
        mlflow_model_stage="production",
        max_batch_size=100,
        local_cache_dir=tmp_path / ".cache",
        model_load_retries=3,
        model_load_backoff_seconds=0.5,
    )
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=FakeGCSClient())
    attempts = {"count": 0}
    sleeps: list[float] = []

    def fake_loader(model_uri: str, *, storage: StorageClient, model_source: str):  # noqa: ARG001
        attempts["count"] += 1
        raise ModelLoadError("missing")

    monkeypatch.setattr("serving.model_loader.load_model_bundle_from_uri", fake_loader)

    with pytest.raises(ModelLoadError, match="missing"):
        load_active_model(config, storage=storage, sleep_fn=sleeps.append)

    assert attempts["count"] == 3
    assert sleeps == [0.5, 0.5]


def test_load_active_model_honors_configured_source_priority(monkeypatch, tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)
    storage = StorageClient(local_cache_dir=tmp_path / ".cache")
    called = {"gcs": 0, "mlflow": 0}

    def fake_local_or_gcs_loader(model_uri: str, *, storage: StorageClient, model_source: str):
        called["gcs"] += 1
        return load_model_bundle_from_uri(
            f"local://{bundle_path.as_posix()}",
            storage=storage,
            model_source=model_source,
        )

    def fake_registry_loader(*, tracking_uri: str, model_name: str, model_stage: str):  # noqa: ARG001
        called["mlflow"] += 1
        raise AssertionError("MLflow loader should not be called for gcs source")

    monkeypatch.setattr("serving.model_loader.load_model_bundle_from_uri", fake_local_or_gcs_loader)
    monkeypatch.setattr("serving.model_loader.load_registry_model_bundle", fake_registry_loader)

    config = ServiceConfig(
        runtime_mode="live",
        model_source="gcs",
        model_uri="gs://openfire-bucket/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib",
        demo_geojson_uri=None,
        mlflow_tracking_uri="http://mlflow.example",
        mlflow_registered_model_name="openfire-prod",
        mlflow_model_stage="production",
        max_batch_size=100,
        local_cache_dir=tmp_path / ".cache",
        model_load_retries=1,
        model_load_backoff_seconds=0.0,
    )

    loaded = load_active_model(config, storage=storage)

    assert loaded.model_source == "gcs"
    assert called == {"gcs": 1, "mlflow": 0}


def test_prediction_service_rejects_oversized_batch(tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)
    loaded = load_model_bundle_from_uri(
        f"local://{bundle_path.as_posix()}",
        storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
        model_source="local",
    )
    service = PredictionService(loaded, max_batch_size=1)

    with pytest.raises(PredictionInputError, match="exceeds OPENFIRE_MAX_BATCH_SIZE"):
        service.predict(build_request(row_count=2))


def test_prediction_service_geojson_output(tmp_path: Path) -> None:
    bundle_path = build_model_bundle_path(tmp_path)
    loaded = load_model_bundle_from_uri(
        f"local://{bundle_path.as_posix()}",
        storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
        model_source="local",
    )
    service = PredictionService(loaded, max_batch_size=10)

    response, feature_collection = service.predict_geojson(build_request())

    assert response.model_version == "test-bundle-v1"
    assert len(response.predictions) == 1
    assert feature_collection.type == "FeatureCollection"
    assert feature_collection.features[0].geometry.coordinates == [-122.4, 37.7]


def test_load_model_bundle_errors_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ModelLoadError, match="Model artifact not found"):
        load_model_bundle_from_uri(
            f"local://{(tmp_path / 'missing.joblib').as_posix()}",
            storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
            model_source="local",
        )
