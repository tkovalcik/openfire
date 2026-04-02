from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import joblib

try:
    from common.storage import StorageClient, normalize_storage_uri
    from config.settings import Settings, get_settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.storage import StorageClient, normalize_storage_uri
    from src.config.settings import Settings, get_settings

from .schemas import FEATURE_COLUMNS


LOGGER = logging.getLogger("openfire.serving")


class ModelLoadError(RuntimeError):
    """Raised when the serving model cannot be loaded."""


@dataclass(frozen=True)
class ServiceConfig:
    runtime_mode: str
    model_source: str
    model_uri: str | None
    demo_geojson_uri: str | None
    mlflow_tracking_uri: str | None
    mlflow_registered_model_name: str | None
    mlflow_model_stage: str
    max_batch_size: int
    local_cache_dir: Path
    model_load_retries: int
    model_load_backoff_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "ServiceConfig":
        model_uri = settings.model_uri
        if settings.runtime_mode == "demo" and settings.model_source == "gcs" and model_uri is None:
            model_uri = settings.demo_model_uri
        return cls(
            runtime_mode=settings.runtime_mode,
            model_source=settings.model_source,
            model_uri=model_uri,
            demo_geojson_uri=settings.demo_geojson_uri,
            mlflow_tracking_uri=settings.mlflow_tracking_uri,
            mlflow_registered_model_name=settings.mlflow_registered_model_name,
            mlflow_model_stage=settings.mlflow_model_stage,
            max_batch_size=settings.serving_max_batch_size,
            local_cache_dir=settings.local_cache_dir,
            model_load_retries=settings.model_load_retries,
            model_load_backoff_seconds=settings.model_load_backoff_seconds,
        )

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        return cls.from_settings(get_settings())


@dataclass(frozen=True)
class LoadedModel:
    model: Any
    model_source: str
    model_version: str
    feature_columns: list[str]
    dataset_version_info: dict[str, list[str]]
    decision_threshold: float
    split_strategy: str | None
    split_group_column: str | None
    validation_groups: list[str]
    model_uri: str | None


def log_event(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True))


def build_internal_loader_metadata(
    config: ServiceConfig,
    *,
    loaded_model: LoadedModel | None = None,
    load_error: str | None = None,
) -> dict[str, Any]:
    return {
        "runtime_mode": config.runtime_mode,
        "configured_model_source": config.model_source,
        "configured_model_uri": config.model_uri,
        "configured_demo_geojson_uri": config.demo_geojson_uri,
        "mlflow_tracking_uri": config.mlflow_tracking_uri,
        "mlflow_registered_model_name": config.mlflow_registered_model_name,
        "mlflow_model_stage": config.mlflow_model_stage,
        "model_load_retries": config.model_load_retries,
        "model_load_backoff_seconds": config.model_load_backoff_seconds,
        "model_loaded": loaded_model is not None,
        "loaded_model_source": loaded_model.model_source if loaded_model else None,
        "loaded_model_version": loaded_model.model_version if loaded_model else None,
        "loaded_model_uri": loaded_model.model_uri if loaded_model else None,
        "load_error": load_error,
    }


def _coerce_dataset_version_info(value: Any) -> dict[str, list[str]]:
    if isinstance(value, dict):
        return {
            str(key): [str(item) for item in values]
            for key, values in value.items()
        }
    return {}


def _bundle_to_loaded_model(
    bundle: Any,
    *,
    model_source: str,
    model_version: str,
    model_uri: str | None,
) -> LoadedModel:
    if isinstance(bundle, dict) and "model" in bundle:
        model = bundle["model"]
        feature_columns = list(bundle.get("feature_columns", FEATURE_COLUMNS))
        dataset_version_info = _coerce_dataset_version_info(bundle.get("dataset_version_info", {}))
        decision_threshold = float(bundle.get("decision_threshold", 0.5))
        split_strategy = bundle.get("split_strategy")
        split_group_column = bundle.get("split_group_column")
        validation_groups = [str(value) for value in bundle.get("validation_groups", [])]
        explicit_version = bundle.get("model_version")
    else:
        model = bundle
        feature_columns = list(FEATURE_COLUMNS)
        dataset_version_info = {}
        decision_threshold = 0.5
        split_strategy = None
        split_group_column = None
        validation_groups = []
        explicit_version = None

    if not hasattr(model, "predict"):
        raise ModelLoadError("Loaded artifact does not expose a scikit-learn compatible predict interface.")

    unknown_columns = sorted(set(feature_columns).difference(FEATURE_COLUMNS))
    if unknown_columns:
        raise ModelLoadError(
            "Loaded model requires unsupported feature columns for this service: "
            + ", ".join(unknown_columns)
        )

    return LoadedModel(
        model=model,
        model_source=model_source,
        model_version=str(explicit_version or model_version),
        feature_columns=feature_columns,
        dataset_version_info=dataset_version_info,
        decision_threshold=decision_threshold,
        split_strategy=split_strategy,
        split_group_column=split_group_column,
        validation_groups=validation_groups,
        model_uri=model_uri,
    )


def load_model_bundle_from_uri(
    model_uri: str,
    *,
    storage: StorageClient,
    model_source: str,
) -> LoadedModel:
    normalized_uri = normalize_storage_uri(model_uri)
    with storage.localize(normalized_uri) as localized_path:
        if not localized_path.exists():
            raise ModelLoadError(f"Model artifact not found at {normalized_uri}")
        bundle = joblib.load(localized_path)
    return _bundle_to_loaded_model(
        bundle,
        model_source=model_source,
        model_version=f"{model_source}:{Path(normalized_uri).name}",
        model_uri=normalized_uri,
    )


def _load_once(
    config: ServiceConfig,
    *,
    storage: StorageClient,
) -> LoadedModel:
    if config.model_source == "gcs":
        if not config.model_uri:
            raise ModelLoadError("OPENFIRE_MODEL_URI must be set when OPENFIRE_MODEL_SOURCE=gcs.")
        return load_model_bundle_from_uri(config.model_uri, storage=storage, model_source="gcs")

    if config.model_source == "mlflow":
        if not config.mlflow_tracking_uri or not config.mlflow_registered_model_name:
            raise ModelLoadError(
                "MLflow model loading requires MLFLOW_TRACKING_URI and MLFLOW_REGISTERED_MODEL_NAME."
            )
        return load_registry_model_bundle(
            tracking_uri=config.mlflow_tracking_uri,
            model_name=config.mlflow_registered_model_name,
            model_stage=config.mlflow_model_stage,
        )

    local_model_uri = config.model_uri or "local://model.joblib"
    return load_model_bundle_from_uri(local_model_uri, storage=storage, model_source="local")


def load_registry_model_bundle(
    *,
    tracking_uri: str,
    model_name: str,
    model_stage: str,
) -> LoadedModel:
    try:
        import mlflow
        from mlflow.tracking import MlflowClient
    except ImportError as error:  # pragma: no cover - depends on runtime environment
        raise ModelLoadError(
            "MLflow registry loading is configured, but mlflow is not installed in the serving environment."
        ) from error

    desired_stage = model_stage.strip().lower()
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions(f"name = '{model_name}'")
    matching_versions = [
        version
        for version in versions
        if str(getattr(version, "current_stage", "")).strip().lower() == desired_stage
    ]
    if not matching_versions:
        raise ModelLoadError(
            f"No MLflow model version found for model={model_name!r} stage={model_stage!r}"
        )

    selected_version = max(matching_versions, key=lambda version: int(version.version))
    try:
        local_path = client.download_artifacts(selected_version.run_id, "model.joblib")
    except Exception as error:  # pragma: no cover - depends on mlflow artifact store behavior
        raise ModelLoadError(
            "Failed to download model.joblib from the selected MLflow run. "
            "The training pipeline must log the serving bundle artifact."
        ) from error

    bundle = joblib.load(local_path)
    return _bundle_to_loaded_model(
        bundle,
        model_source=f"mlflow_registry:{model_name}/{desired_stage}",
        model_version=str(selected_version.version),
        model_uri=f"models:/{model_name}/{desired_stage}",
    )


def load_active_model(
    config: ServiceConfig,
    *,
    storage: StorageClient | None = None,
    settings: Settings | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> LoadedModel:
    resolved_settings = settings or get_settings()
    resolved_storage = storage or StorageClient.from_settings(resolved_settings)
    log_event(
        "model_loader_configured",
        runtime_mode=config.runtime_mode,
        source=config.model_source,
        model_uri=config.model_uri,
        demo_geojson_uri=config.demo_geojson_uri,
        mlflow_registered_model_name=config.mlflow_registered_model_name,
        mlflow_model_stage=config.mlflow_model_stage,
        retries=config.model_load_retries,
        backoff_seconds=config.model_load_backoff_seconds,
    )

    last_error: ModelLoadError | None = None
    for attempt in range(1, config.model_load_retries + 1):
        try:
            log_event(
                "model_load_attempt",
                attempt=attempt,
                source=config.model_source,
                model_uri=config.model_uri,
                mlflow_registered_model_name=config.mlflow_registered_model_name,
                mlflow_model_stage=config.mlflow_model_stage,
            )
            loaded = _load_once(config, storage=resolved_storage)
            log_event(
                "model_load_complete",
                attempt=attempt,
                source=loaded.model_source,
                model_version=loaded.model_version,
                model_uri=loaded.model_uri,
            )
            return loaded
        except ModelLoadError as error:
            last_error = error
            log_event(
                "model_load_attempt_failed",
                attempt=attempt,
                source=config.model_source,
                detail=str(error),
            )
            if attempt < config.model_load_retries and config.model_load_backoff_seconds > 0:
                sleep_fn(config.model_load_backoff_seconds)

    assert last_error is not None
    log_event(
        "model_load_failed",
        source=config.model_source,
        detail=str(last_error),
        retries=config.model_load_retries,
    )
    raise last_error
