from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

try:
    from common.paths import registered_model_uri
    from common.storage import StorageClient
    from config.settings import Settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.paths import registered_model_uri
    from src.common.storage import StorageClient
    from src.config.settings import Settings


class TrackerRun(AbstractContextManager["TrackerRun"], Protocol):
    run_id: str

    def __exit__(self, exc_type, exc, tb) -> None:
        ...


class TrackingClient(Protocol):
    def start_run(self, run_name: str) -> TrackerRun:
        ...

    def log_params(self, params: dict[str, Any]) -> None:
        ...

    def log_metrics(self, metrics: dict[str, float]) -> None:
        ...

    def log_artifact(self, path: Path) -> None:
        ...

    def set_tags(self, tags: dict[str, str]) -> None:
        ...

    def log_model(self, model: Any, artifact_path: str) -> str:
        ...

    def register_model(self, model_uri: str, model_name: str) -> None:
        ...


class _MlflowRun:
    def __init__(self, mlflow_run: Any) -> None:
        self._mlflow_run = mlflow_run
        self.run_id = ""

    def __enter__(self) -> "_MlflowRun":
        entered = self._mlflow_run.__enter__()
        self.run_id = entered.info.run_id
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._mlflow_run.__exit__(exc_type, exc, tb)


class MlflowTrackingClient:
    def __init__(self, *, tracking_uri: str | None = None, experiment_name: str | None = None) -> None:
        try:
            import mlflow
            import mlflow.sklearn
        except ImportError as error:  # pragma: no cover - depends on local environment
            raise RuntimeError(
                "MLflow is required for training runs. Install mlflow before executing train_baseline."
            ) from error

        self.mlflow = mlflow
        self.mlflow_sklearn = mlflow.sklearn
        if tracking_uri:
            self.mlflow.set_tracking_uri(tracking_uri)
        if experiment_name:
            self.mlflow.set_experiment(experiment_name)

    def start_run(self, run_name: str) -> TrackerRun:
        return _MlflowRun(self.mlflow.start_run(run_name=run_name))

    def log_params(self, params: dict[str, Any]) -> None:
        serialized = {
            key: value if isinstance(value, (int, float, str, bool)) else json.dumps(value, sort_keys=True)
            for key, value in params.items()
        }
        self.mlflow.log_params(serialized)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        self.mlflow.log_metrics(metrics)

    def log_artifact(self, path: Path) -> None:
        self.mlflow.log_artifact(str(path))

    def set_tags(self, tags: dict[str, str]) -> None:
        self.mlflow.set_tags(tags)

    def log_model(self, model: Any, artifact_path: str) -> str:
        self.mlflow_sklearn.log_model(model, artifact_path=artifact_path)
        run = self.mlflow.active_run()
        if run is None:
            raise RuntimeError("No active MLflow run while logging model")
        return f"runs:/{run.info.run_id}/{artifact_path}"

    def register_model(self, model_uri: str, model_name: str) -> None:
        self.mlflow.register_model(model_uri=model_uri, name=model_name)


@dataclass(frozen=True)
class PromotedModelLocations:
    model_uri: str
    metadata_uri: str


def normalize_model_stage(stage: str) -> str:
    normalized = stage.strip().lower()
    if normalized not in {"development", "staging", "production"}:
        raise ValueError(
            "Model stage must be one of: development, staging, production. "
            f"Got {stage!r}."
        )
    return normalized


def promote_serving_bundle(
    *,
    storage: StorageClient,
    settings: Settings,
    model_name: str,
    stage: str,
    local_model_bundle_path: Path,
    metadata_payload: dict[str, Any],
) -> PromotedModelLocations:
    normalized_stage = normalize_model_stage(stage)
    model_uri = registered_model_uri(settings, model_name, normalized_stage, filename="model.joblib")
    metadata_uri = registered_model_uri(settings, model_name, normalized_stage, filename="metadata.json")
    storage.upload_file(local_model_bundle_path, model_uri, content_type="application/octet-stream")
    storage.write_json(metadata_uri, metadata_payload)
    return PromotedModelLocations(model_uri=model_uri, metadata_uri=metadata_uri)
