from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from common.storage import StorageClient
from config.settings import Settings
from serving.app import create_app
from serving.model_loader import ServiceConfig
from conftest import build_model_bundle_path


def build_payload() -> dict:
    return {
        "rows": [
            {
                "latitude": 37.7,
                "longitude": -122.4,
                "days_since_last_burn": 9999,
                "ndvi_change_5d": 0.01,
                "ndvi_change_15d": 0.02,
                "ndvi_change_30d": 0.03,
                "ndvi_change_60d": 0.04,
                "ndwi_change_5d": -0.01,
                "ndwi_change_15d": -0.02,
                "ndwi_change_30d": -0.03,
                "ndwi_change_60d": -0.04,
                "temp_change_5d": 0.5,
                "temp_change_15d": 1.0,
                "temp_change_30d": 1.5,
                "temp_change_60d": 2.0,
                "precip_change_15d": -1.0,
                "precip_change_30d": -2.0,
                "precip_change_60d": -3.0,
                "mean_elevation": 200.0,
                "mean_slope": 4.0,
                "mean_cos_aspect": 0.5,
                "mean_sin_aspect": 0.5,
                "B2": 100.0,
                "B3": 110.0,
                "B4": 120.0,
                "B8": 130.0,
                "B11": 140.0,
                "B12": 150.0,
                "mean_NDVI": 0.3,
                "mean_EVI": 0.2,
                "mean_NDWI": 0.1,
                "mean_NBR": 0.0,
                "gridmet_temp_max": 300.0,
                "gridmet_humidity_min": 20.0,
                "gridmet_precip_sum": 2.0,
                "gridmet_wind_max": 9.0,
            }
        ]
    }


def build_settings(
    tmp_path: Path,
    *,
    runtime_mode: str = "live",
    model_source: str = "local",
    model_uri: str | None = None,
    demo_geojson_uri: str | None = None,
    cors_allowed_origins: tuple[str, ...] = (),
    max_batch_size: int = 10,
) -> Settings:
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
        runtime_mode=runtime_mode,  # type: ignore[arg-type]
        model_source=model_source,  # type: ignore[arg-type]
        model_uri=model_uri,
        demo_model_uri=None,
        demo_features_uri=None,
        demo_geojson_uri=demo_geojson_uri,
        mlflow_tracking_uri=None,
        mlflow_registered_model_name=None,
        mlflow_model_stage="production",
        mlflow_experiment_name="openfire",
        serving_max_batch_size=max_batch_size,
        cors_allowed_origins=cors_allowed_origins,
        model_load_retries=1,
        model_load_backoff_seconds=0.0,
    )


def build_test_client(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
) -> TestClient:
    resolved_settings = settings or build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
    )
    storage = StorageClient(local_cache_dir=resolved_settings.local_cache_dir)
    app = create_app(
        ServiceConfig.from_settings(resolved_settings),
        settings=resolved_settings,
        storage=storage,
    )
    return TestClient(app)


def test_app_health_metadata_and_predict(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        runtime_mode="live",
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
    )
    client = build_test_client(tmp_path, settings=settings)
    payload = build_payload()

    with client:
        root = client.get("/")
        health = client.get("/health")
        metadata = client.get("/metadata")
        prediction = client.post("/predict", json=payload)
        geojson = client.post("/predict_geojson", json=payload)

    assert root.status_code == 200
    assert root.json() == {
        "service": "openfire-api",
        "message": "OpenFire Serving API",
        "status": "ok",
        "runtime_mode": "live",
        "model_loaded": True,
        "model_source": "local",
        "model_version": "test-bundle-v1",
        "endpoints": {
            "health": "/health",
            "metadata": "/metadata",
            "demo_geojson": "/demo/geojson",
            "predict": "/predict",
            "predict_geojson": "/predict_geojson",
            "docs": "/docs",
        },
    }
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "model_loaded": True,
        "runtime_mode": "live",
        "model_source": "local",
        "model_version": "test-bundle-v1",
    }
    assert metadata.status_code == 200
    assert metadata.json()["model_loaded"] is True
    assert metadata.json()["runtime_mode"] == "live"
    assert metadata.json()["feature_columns"][0] == "days_since_last_burn"
    assert "model_uri" not in metadata.json()
    assert prediction.status_code == 200
    assert prediction.json()["model_version"] == "test-bundle-v1"
    assert len(prediction.json()["predictions"]) == 1
    assert geojson.status_code == 200
    assert geojson.json()["feature_collection"]["type"] == "FeatureCollection"


def test_app_starts_degraded_when_model_loading_fails(tmp_path: Path) -> None:
    missing_model_uri = f"local://{(tmp_path / 'missing.joblib').as_posix()}"
    settings = build_settings(tmp_path, runtime_mode="demo", model_uri=missing_model_uri)
    client = build_test_client(tmp_path, settings=settings)
    payload = build_payload()

    with client:
        root = client.get("/")
        health = client.get("/health")
        metadata = client.get("/metadata")
        prediction = client.post("/predict", json=payload)
        geojson = client.post("/predict_geojson", json=payload)

    assert root.status_code == 200
    assert root.json() == {
        "service": "openfire-api",
        "message": "OpenFire Serving API",
        "status": "degraded",
        "runtime_mode": "demo",
        "model_loaded": False,
        "model_source": "local",
        "model_version": None,
        "endpoints": {
            "health": "/health",
            "metadata": "/metadata",
            "demo_geojson": "/demo/geojson",
            "predict": "/predict",
            "predict_geojson": "/predict_geojson",
            "docs": "/docs",
        },
    }
    assert health.status_code == 200
    assert health.json() == {
        "status": "degraded",
        "model_loaded": False,
        "runtime_mode": "demo",
        "model_source": "local",
        "model_version": None,
    }
    assert metadata.status_code == 200
    assert metadata.json()["model_loaded"] is False
    assert metadata.json()["runtime_mode"] == "demo"
    assert "model_uri" not in metadata.json()
    assert prediction.status_code == 503
    assert prediction.json() == {
        "error": "model_unavailable",
        "detail": "Prediction model is not loaded.",
        "model_source": "local",
    }
    assert geojson.status_code == 503


def test_demo_geojson_endpoint_returns_payload(tmp_path: Path) -> None:
    demo_geojson_path = tmp_path / "demo_risk.geojson"
    demo_geojson_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "metadata": {
                    "model_version": "demo-model-v1",
                    "inference_date": "2026-04-01",
                    "data_window": "2024-06-01 to 2024-06-30",
                },
                "features": [],
            }
        ),
        encoding="utf-8",
    )
    settings = build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
        demo_geojson_uri=f"local://{demo_geojson_path.as_posix()}",
    )
    client = build_test_client(tmp_path, settings=settings)

    with client:
        response = client.get("/demo/geojson")

    assert response.status_code == 200
    assert response.json()["metadata"]["model_version"] == "demo-model-v1"


def test_demo_geojson_endpoint_returns_404_when_unconfigured(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
        demo_geojson_uri=None,
    )
    client = build_test_client(tmp_path, settings=settings)

    with client:
        response = client.get("/demo/geojson")

    assert response.status_code == 404
    assert response.json()["error"] == "demo_artifact_unconfigured"


def test_app_returns_400_for_batch_limit(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
        max_batch_size=1,
    )
    client = build_test_client(tmp_path, settings=settings)
    payload = build_payload()
    payload["rows"].append(payload["rows"][0])

    with client:
        response = client.post("/predict", json=payload)

    assert response.status_code == 400
    assert "OPENFIRE_MAX_BATCH_SIZE" in response.json()["detail"]


def test_app_returns_422_for_schema_errors(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
    )
    client = build_test_client(tmp_path, settings=settings)
    payload = build_payload()
    del payload["rows"][0]["mean_NDVI"]

    with client:
        response = client.post("/predict", json=payload)

    assert response.status_code == 422


def test_app_cors_is_env_driven(tmp_path: Path) -> None:
    settings = build_settings(
        tmp_path,
        model_uri=f"local://{build_model_bundle_path(tmp_path).as_posix()}",
        cors_allowed_origins=("http://127.0.0.1:8080",),
    )
    client = build_test_client(tmp_path, settings=settings)

    with client:
        allowed = client.options(
            "/metadata",
            headers={
                "Origin": "http://127.0.0.1:8080",
                "Access-Control-Request-Method": "GET",
            },
        )
        blocked = client.options(
            "/metadata",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:8080"
    assert "access-control-allow-origin" not in blocked.headers
