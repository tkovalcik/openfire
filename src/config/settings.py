from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal


ModelSource = Literal["gcs", "mlflow", "local"]
RuntimeMode = Literal["demo", "live"]


def _read_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped if stripped else default


def _read_int_env(name: str, default: int) -> int:
    raw_value = _read_env(name)
    if raw_value is None:
        return default
    return int(raw_value)


def _read_float_env(name: str, default: float) -> float:
    raw_value = _read_env(name)
    if raw_value is None:
        return default
    return float(raw_value)


def _read_path_env(name: str, default: str) -> Path:
    return Path(_read_env(name, default) or default)


def _read_csv_env(name: str) -> tuple[str, ...]:
    raw_value = _read_env(name)
    if raw_value is None:
        return ()
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    gcp_project_id: str | None
    gcs_bucket: str | None
    storage_root_prefix: str
    dataset_prefix: str
    feature_prefix: str
    training_prefix: str
    model_prefix: str
    monitoring_prefix: str
    mlflow_artifact_prefix: str
    local_cache_dir: Path
    runtime_mode: RuntimeMode
    model_source: ModelSource
    model_uri: str | None
    demo_model_uri: str | None
    demo_features_uri: str | None
    demo_geojson_uri: str | None
    mlflow_tracking_uri: str | None
    mlflow_registered_model_name: str | None
    mlflow_model_stage: str
    mlflow_experiment_name: str
    serving_max_batch_size: int
    cors_allowed_origins: tuple[str, ...]
    model_load_retries: int
    model_load_backoff_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        runtime_mode = (_read_env("OPENFIRE_RUNTIME_MODE", "live") or "live").lower()
        if runtime_mode not in {"demo", "live"}:
            raise ValueError(
                "OPENFIRE_RUNTIME_MODE must be one of: demo, live. "
                f"Got {runtime_mode!r}."
            )

        model_source = (_read_env("OPENFIRE_MODEL_SOURCE", "local") or "local").lower()
        if model_source not in {"gcs", "mlflow", "local"}:
            raise ValueError(
                "OPENFIRE_MODEL_SOURCE must be one of: gcs, mlflow, local. "
                f"Got {model_source!r}."
            )

        return cls(
            gcp_project_id=_read_env("GCP_PROJECT_ID"),
            gcs_bucket=_read_env("OPENFIRE_GCS_BUCKET"),
            storage_root_prefix=_read_env("OPENFIRE_STORAGE_ROOT_PREFIX", "openfire") or "openfire",
            dataset_prefix=_read_env("OPENFIRE_DATASET_PREFIX", "datasets") or "datasets",
            feature_prefix=_read_env("OPENFIRE_FEATURE_PREFIX", "features") or "features",
            training_prefix=_read_env("OPENFIRE_TRAINING_PREFIX", "training") or "training",
            model_prefix=_read_env("OPENFIRE_MODEL_PREFIX", "models") or "models",
            monitoring_prefix=_read_env("OPENFIRE_MONITORING_PREFIX", "monitoring") or "monitoring",
            mlflow_artifact_prefix=(
                _read_env("OPENFIRE_MLFLOW_ARTIFACT_PREFIX", "mlflow-artifacts")
                or "mlflow-artifacts"
            ),
            local_cache_dir=_read_path_env("OPENFIRE_LOCAL_CACHE_DIR", ".cache/openfire"),
            runtime_mode=runtime_mode,
            model_source=model_source,
            model_uri=_read_env("OPENFIRE_MODEL_URI"),
            demo_model_uri=_read_env("OPENFIRE_DEMO_MODEL_URI"),
            demo_features_uri=_read_env("OPENFIRE_DEMO_FEATURES_URI"),
            demo_geojson_uri=_read_env("OPENFIRE_DEMO_GEOJSON_URI"),
            mlflow_tracking_uri=_read_env("MLFLOW_TRACKING_URI"),
            mlflow_registered_model_name=(
                _read_env("MLFLOW_REGISTERED_MODEL_NAME")
                or _read_env("OPENFIRE_MODEL_NAME")
                or _read_env("MLFLOW_MODEL_NAME")
            ),
            mlflow_model_stage=(
                _read_env("MLFLOW_MODEL_STAGE")
                or _read_env("OPENFIRE_MODEL_STAGE", "production")
                or "production"
            ),
            mlflow_experiment_name=_read_env("MLFLOW_EXPERIMENT_NAME", "openfire") or "openfire",
            serving_max_batch_size=_read_int_env("OPENFIRE_MAX_BATCH_SIZE", 1000),
            cors_allowed_origins=_read_csv_env("OPENFIRE_CORS_ALLOWED_ORIGINS"),
            model_load_retries=max(1, _read_int_env("OPENFIRE_MODEL_LOAD_RETRIES", 3)),
            model_load_backoff_seconds=max(
                0.0,
                _read_float_env("OPENFIRE_MODEL_LOAD_BACKOFF_SECONDS", 2.0),
            ),
        )

    @property
    def local_tmp_dir(self) -> Path:
        return self.local_cache_dir / "tmp"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
