from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from common.storage import StorageClient
from config.settings import Settings
from model.train_baseline import DEFAULT_FEATURE_COLUMNS, split_dataset_by_strategy, train_baseline
from conftest import FakeGCSClient


def build_training_frame() -> pd.DataFrame:
    rows = []
    row_id = 0
    for fire_event, year, label_values in [
        ("Alpha", 2023, [0, 1]),
        ("Bravo", 2023, [0, 1]),
        ("Charlie", 2024, [0, 1]),
        ("Delta", 2024, [0, 1]),
    ]:
        for label in label_values:
            row_id += 1
            rows.append(
                {
                    "dataset_schema_version": "openfire.dataset.v1",
                    "ee_feature_version": "openfire.ee.v1",
                    "label_schema_version": "openfire.labels.frap.v1",
                    "weather_schema_version": "openfire.weather.v1",
                    "fire_event": fire_event,
                    "pixel_id": f"pixel-{row_id}",
                    "latitude": 37.0 + row_id * 0.01,
                    "longitude": -122.0 - row_id * 0.01,
                    "date_window_start": f"{year}-06-01",
                    "date_window_end": f"{year}-06-30",
                    "target_burned": label,
                    "NDVI": 0.2 + 0.4 * label,
                    "EVI": 0.1 + 0.3 * label,
                    "NDWI": -0.2 + 0.1 * label,
                    "NBR": 0.05 + 0.25 * label,
                    "B2": 100 + row_id,
                    "B3": 110 + row_id,
                    "B4": 120 + row_id,
                    "B8": 130 + row_id,
                    "B11": 140 + row_id,
                    "B12": 150 + row_id,
                    "elevation": 300 + row_id,
                    "slope": 5 + row_id * 0.1,
                    "aspect": 180 + row_id,
                    "mean_ndvi_100m": 0.21 + 0.4 * label,
                    "mean_ndvi_500m": 0.19 + 0.35 * label,
                    "precip_7d_sum": 2.0 - label * 0.5,
                    "precip_30d_sum": 12.0 - label,
                    "temp_7d_mean": 18.0 + label * 4.0,
                    "temp_30d_mean": 17.5 + label * 3.5,
                    "humidity_7d_mean": 65.0 - label * 15.0,
                    "humidity_30d_mean": 60.0 - label * 12.0,
                    "wind_7d_max": 8.0 + label * 4.0,
                    "wind_30d_max": 10.0 + label * 4.5,
                }
            )
    return pd.DataFrame(rows)


class FakeRun:
    def __init__(self, tracker: "FakeTracker", run_id: str) -> None:
        self.tracker = tracker
        self.run_id = run_id

    def __enter__(self) -> "FakeRun":
        self.tracker.active_run_id = self.run_id
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.tracker.active_run_id = None


class FakeTracker:
    def __init__(self) -> None:
        self.active_run_id: str | None = None
        self.started_runs: list[str] = []
        self.logged_params: list[dict] = []
        self.logged_metrics: list[dict] = []
        self.logged_artifacts: list[str] = []
        self.tags: list[dict] = []
        self.logged_models: list[str] = []
        self.registered_models: list[tuple[str, str]] = []

    def start_run(self, run_name: str) -> FakeRun:
        run_id = f"run-{len(self.started_runs) + 1}"
        self.started_runs.append(run_name)
        return FakeRun(self, run_id)

    def log_params(self, params: dict) -> None:
        self.logged_params.append(params)

    def log_metrics(self, metrics: dict) -> None:
        self.logged_metrics.append(metrics)

    def log_artifact(self, path: Path) -> None:
        self.logged_artifacts.append(str(path))

    def set_tags(self, tags: dict[str, str]) -> None:
        self.tags.append(tags)

    def log_model(self, model, artifact_path: str) -> str:  # noqa: ANN001
        model_uri = f"runs:/{self.active_run_id}/{artifact_path}"
        self.logged_models.append(model_uri)
        return model_uri

    def register_model(self, model_uri: str, model_name: str) -> None:
        self.registered_models.append((model_uri, model_name))


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
        runtime_mode="live",
        model_source="gcs",
        model_uri=None,
        demo_model_uri=None,
        demo_features_uri=None,
        demo_geojson_uri=None,
        mlflow_tracking_uri="http://mlflow.example",
        mlflow_registered_model_name="openfire-baseline",
        mlflow_model_stage="production",
        mlflow_experiment_name="openfire",
        serving_max_batch_size=1000,
        cors_allowed_origins=(),
        model_load_retries=3,
        model_load_backoff_seconds=2.0,
    )


def test_split_dataset_by_fire_event_prevents_leakage() -> None:
    frame = build_training_frame()

    split = split_dataset_by_strategy(
        frame,
        split_strategy="fire_event",
        validation_values=["Delta"],
        group_column="fire_event",
    )

    assert split.strategy_name == "fire_event"
    assert set(split.train_frame["fire_event"]) == {"Alpha", "Bravo", "Charlie"}
    assert set(split.validation_frame["fire_event"]) == {"Delta"}
    assert set(split.train_frame["fire_event"]).isdisjoint(set(split.validation_frame["fire_event"]))


def test_train_baseline_rejects_schema_mismatch(tmp_path: Path) -> None:
    dataset = build_training_frame().drop(columns=["NDVI"])
    dataset_path = tmp_path / "training.parquet"
    dataset.to_parquet(dataset_path, index=False)

    with pytest.raises(ValueError, match="missing required columns"):
        train_baseline(
            dataset_uri=f"local://{dataset_path.as_posix()}",
            output_dir=tmp_path / "artifacts",
            model_type="random_forest",
            split_strategy="year",
            validation_values=["2024"],
            tracker=FakeTracker(),
            register_best_model=False,
            storage=StorageClient(local_cache_dir=tmp_path / ".cache"),
            settings=build_settings(tmp_path),
        )


def test_train_baseline_logs_mlflow_boundaries_and_uploads_to_storage(tmp_path: Path) -> None:
    dataset = build_training_frame()
    dataset_path = tmp_path / "training.parquet"
    dataset.to_parquet(dataset_path, index=False)
    tracker = FakeTracker()
    gcs_client = FakeGCSClient()
    storage = StorageClient(local_cache_dir=tmp_path / ".cache", gcs_client=gcs_client)

    result = train_baseline(
        dataset_uri=f"local://{dataset_path.as_posix()}",
        output_dir=tmp_path / "artifacts",
        model_type="random_forest",
        split_strategy="year",
        validation_values=["2024"],
        model_name="openfire-baseline",
        tracker=tracker,
        register_best_model=True,
        storage=storage,
        settings=build_settings(tmp_path),
    )

    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))

    assert result.best_model_name == "random_forest"
    assert result.best_model_uri == "runs:/run-1/model"
    assert result.summary_uri.startswith("gs://openfire-bucket/openfire/models/candidates/openfire-baseline/")
    assert result.registered_model_uri == "gs://openfire-bucket/openfire/models/registered/openfire-baseline/production/model.joblib"
    assert storage.exists(result.summary_uri)
    assert storage.exists(result.registered_model_uri)
    assert storage.exists(result.registered_metadata_uri)
    assert tracker.started_runs == ["openfire-baseline-random_forest"]
    assert len(tracker.logged_params) == 1
    assert len(tracker.logged_metrics) == 1
    assert len(tracker.tags) == 1
    assert len(tracker.logged_models) == 1
    assert len(tracker.logged_artifacts) >= 5
    assert tracker.registered_models == [("runs:/run-1/model", "openfire-baseline")]
    assert tracker.logged_params[0]["dataset_uri"].startswith("local://")
    assert tracker.logged_params[0]["candidate_artifact_prefix"].startswith("gs://openfire-bucket/")
    assert "Grouped temporal or fire-event split baseline" in tracker.tags[0]["split_validation_note"]
    assert "full spatial holdout is not yet implemented" in tracker.tags[0]["split_validation_note"]
    assert summary["best_candidate"] == "random_forest"
    assert summary["split"]["strategy_name"] == "year"
    assert summary["split"]["validation_groups"] == ["2024"]
    assert summary["feature_columns"] == DEFAULT_FEATURE_COLUMNS
