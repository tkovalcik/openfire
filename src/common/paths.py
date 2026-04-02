from __future__ import annotations

from datetime import datetime, timezone

try:
    from config.settings import Settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.config.settings import Settings


def build_version_id(prefix: str, *, now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return f"{prefix}-{instant.strftime('%Y%m%dT%H%M%SZ')}"


def join_uri(base_uri: str, *parts: str) -> str:
    normalized_base = base_uri.rstrip("/")
    cleaned_parts = [part.strip("/") for part in parts if part]
    if not cleaned_parts:
        return normalized_base
    return f"{normalized_base}/{'/'.join(cleaned_parts)}"


def storage_root_uri(settings: Settings) -> str:
    if not settings.gcs_bucket:
        raise ValueError(
            "OPENFIRE_GCS_BUCKET must be configured to build canonical GCS storage URIs. "
            "Use explicit local:// URIs for local-only development."
        )
    return f"gs://{settings.gcs_bucket}/{settings.storage_root_prefix.strip('/')}"


def mlflow_artifact_root_uri(settings: Settings) -> str:
    return join_uri(storage_root_uri(settings), settings.mlflow_artifact_prefix)


def demo_prefix_uri(settings: Settings, *parts: str) -> str:
    return join_uri(storage_root_uri(settings), "demo", *parts)


def demo_model_bundle_uri(
    settings: Settings,
    model_name: str,
    version: str,
    *,
    filename: str = "model.joblib",
) -> str:
    return demo_prefix_uri(settings, "models", model_name, version, filename)


def demo_features_uri(
    settings: Settings,
    dataset_name: str,
    version: str,
    *,
    filename: str = "features.csv",
) -> str:
    return demo_prefix_uri(settings, "features", dataset_name, version, filename)


def demo_geojson_uri(
    settings: Settings,
    layer_name: str,
    version: str,
    *,
    filename: str = "risk.geojson",
) -> str:
    return demo_prefix_uri(settings, "geojson", layer_name, version, filename)


def raw_dataset_prefix(settings: Settings, dataset_name: str, version: str) -> str:
    return join_uri(storage_root_uri(settings), settings.dataset_prefix, "raw", dataset_name, version)


def processed_dataset_uri(
    settings: Settings,
    dataset_name: str,
    version: str,
    *,
    filename: str = "dataset.parquet",
) -> str:
    return join_uri(
        storage_root_uri(settings),
        settings.dataset_prefix,
        "processed",
        dataset_name,
        version,
        filename,
    )


def dataset_manifest_uri(settings: Settings, dataset_name: str, version: str) -> str:
    return processed_dataset_uri(
        settings,
        dataset_name=dataset_name,
        version=version,
        filename="manifest.json",
    )


def feature_run_prefix(settings: Settings, feature_set: str, extract_run_id: str) -> str:
    return join_uri(storage_root_uri(settings), settings.feature_prefix, feature_set, extract_run_id)


def training_run_prefix(settings: Settings, train_run_id: str) -> str:
    return join_uri(storage_root_uri(settings), settings.training_prefix, "runs", train_run_id)


def candidate_model_prefix(settings: Settings, model_name: str, train_run_id: str) -> str:
    return join_uri(storage_root_uri(settings), settings.model_prefix, "candidates", model_name, train_run_id)


def registered_model_uri(
    settings: Settings,
    model_name: str,
    stage: str,
    *,
    filename: str = "model.joblib",
) -> str:
    return join_uri(
        storage_root_uri(settings),
        settings.model_prefix,
        "registered",
        model_name,
        stage.lower(),
        filename,
    )


def monitoring_report_uri(
    settings: Settings,
    report_name: str,
    run_id: str,
    *,
    filename: str,
) -> str:
    return join_uri(
        storage_root_uri(settings),
        settings.monitoring_prefix,
        "reports",
        report_name,
        run_id,
        filename,
    )
