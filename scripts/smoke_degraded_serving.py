from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from common.storage import StorageClient  # noqa: E402
from config.settings import Settings  # noqa: E402
from serving.app import create_app  # noqa: E402
from serving.model_loader import ServiceConfig  # noqa: E402


def build_settings(tmp_dir: Path) -> Settings:
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
        local_cache_dir=tmp_dir / ".cache",
        runtime_mode="demo",
        model_source="local",
        model_uri=f"local://{(tmp_dir / 'missing-model.joblib').as_posix()}",
        demo_model_uri=None,
        demo_features_uri=None,
        demo_geojson_uri=None,
        mlflow_tracking_uri=None,
        mlflow_registered_model_name=None,
        mlflow_model_stage="production",
        mlflow_experiment_name="openfire",
        serving_max_batch_size=10,
        cors_allowed_origins=("http://127.0.0.1:8080",),
        model_load_retries=1,
        model_load_backoff_seconds=0.0,
    )


def main() -> None:
    tmp_dir = ROOT / ".cache" / "smoke_degraded_serving"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    settings = build_settings(tmp_dir)
    storage = StorageClient(local_cache_dir=settings.local_cache_dir)
    app = create_app(
        ServiceConfig.from_settings(settings),
        settings=settings,
        storage=storage,
    )

    payload = {
        "rows": [
            {
                "latitude": 37.7,
                "longitude": -122.4,
                "NDVI": 0.3,
                "EVI": 0.2,
                "NDWI": 0.1,
                "NBR": 0.0,
                "B2": 100.0,
                "B3": 110.0,
                "B4": 120.0,
                "B8": 130.0,
                "B11": 140.0,
                "B12": 150.0,
                "elevation": 200.0,
                "slope": 4.0,
                "aspect": 180.0,
                "mean_ndvi_100m": 0.31,
                "mean_ndvi_500m": 0.29,
                "precip_7d_sum": 2.0,
                "precip_30d_sum": 10.0,
                "temp_7d_mean": 18.0,
                "temp_30d_mean": 17.0,
                "humidity_7d_mean": 55.0,
                "humidity_30d_mean": 60.0,
                "wind_7d_max": 9.0,
                "wind_30d_max": 11.0,
            }
        ]
    }

    with TestClient(app) as client:
        health = client.get("/health")
        metadata = client.get("/metadata")
        prediction = client.post("/predict", json=payload)

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert metadata.status_code == 200
    assert "model_uri" not in metadata.json()
    assert prediction.status_code == 503
    assert prediction.json()["error"] == "model_unavailable"

    print(
        json.dumps(
            {
                "health": health.json(),
                "metadata": metadata.json(),
                "prediction": prediction.json(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
