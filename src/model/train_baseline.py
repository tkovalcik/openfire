from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import (
    ConfusionMatrixDisplay,
    PrecisionRecallDisplay,
    RocCurveDisplay,
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.utils.class_weight import compute_sample_weight

try:
    from common.paths import build_version_id, candidate_model_prefix
    from common.storage import StorageClient, normalize_storage_uri
    from config.settings import Settings, get_settings
    from data_pipeline.build_dataset import (
        DATASET_SCHEMA_VERSION,
        EE_FEATURE_COLUMNS,
        TARGET_COLUMN,
        WEATHER_NUMERIC_COLUMNS,
    )
    from data_pipeline.weather_features import WINDOW_END_COLUMN
    from model.registry import MlflowTrackingClient, PromotedModelLocations, TrackingClient, promote_serving_bundle
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.paths import build_version_id, candidate_model_prefix
    from src.common.storage import StorageClient, normalize_storage_uri
    from src.config.settings import Settings, get_settings
    from src.data_pipeline.build_dataset import (
        DATASET_SCHEMA_VERSION,
        EE_FEATURE_COLUMNS,
        TARGET_COLUMN,
        WEATHER_NUMERIC_COLUMNS,
    )
    from src.data_pipeline.weather_features import WINDOW_END_COLUMN
    from src.model.registry import (
        MlflowTrackingClient,
        PromotedModelLocations,
        TrackingClient,
        promote_serving_bundle,
    )


LOGGER = logging.getLogger(__name__)

DEFAULT_FEATURE_COLUMNS = [
    "latitude",
    "longitude",
    *EE_FEATURE_COLUMNS,
    *WEATHER_NUMERIC_COLUMNS,
]
REQUIRED_METADATA_COLUMNS = [
    "dataset_schema_version",
    "ee_feature_version",
    "label_schema_version",
    "weather_schema_version",
]
FIRE_EVENT_COLUMN_CANDIDATES = [
    "fire_event_id",
    "fire_event",
    "fire_name",
    "label_fire_name",
    "FIRE_NAME",
]
BEST_MODEL_METRIC = "average_precision"


def get_pyplot():
    os.environ.setdefault("MPLCONFIGDIR", str(Path(".cache") / "openfire" / "matplotlib"))
    Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)

    import matplotlib

    matplotlib.use("Agg")

    import matplotlib.pyplot as plt

    return plt


@dataclass(frozen=True)
class SplitResult:
    strategy_name: str
    train_frame: pd.DataFrame
    validation_frame: pd.DataFrame
    group_column: str
    validation_groups: list[str]


@dataclass(frozen=True)
class CandidateTrainingResult:
    candidate_name: str
    estimator: Any
    run_id: str
    model_uri: str
    metrics: dict[str, float]
    artifact_uris: dict[str, str]
    local_artifact_paths: dict[str, str]


@dataclass(frozen=True)
class TrainingResult:
    best_model_name: str
    best_model_uri: str
    output_dir: Path
    summary_path: Path
    summary_uri: str
    candidate_prefix_uri: str
    candidates: dict[str, CandidateTrainingResult]
    split_details: dict[str, Any]
    dataset_version_info: dict[str, list[str]]
    registered_model_uri: str
    registered_metadata_uri: str


def validate_training_schema(frame: pd.DataFrame, feature_columns: list[str]) -> None:
    required = [*REQUIRED_METADATA_COLUMNS, TARGET_COLUMN, WINDOW_END_COLUMN, *feature_columns]
    missing = sorted(column for column in required if column not in frame.columns)
    if missing:
        raise ValueError(f"Training dataset is missing required columns: {missing}")

    if frame.empty:
        raise ValueError("Training dataset is empty")

    if frame[TARGET_COLUMN].isna().any():
        raise ValueError("Training dataset has missing target_burned values")

    invalid_targets = sorted(set(frame[TARGET_COLUMN].astype(int).unique()) - {0, 1})
    if invalid_targets:
        raise ValueError(f"Training dataset target_burned must be binary; got {invalid_targets}")

    missing_features = [column for column in feature_columns if frame[column].isna().any()]
    if missing_features:
        raise ValueError(f"Training dataset has missing feature values: {missing_features}")

    if set(frame["dataset_schema_version"].astype(str).unique()) != {DATASET_SCHEMA_VERSION}:
        raise ValueError("Training dataset schema version does not match expected OpenFire dataset schema")


def extract_dataset_version_info(frame: pd.DataFrame) -> dict[str, list[str]]:
    return {
        column: sorted(frame[column].dropna().astype(str).unique().tolist())
        for column in REQUIRED_METADATA_COLUMNS
    }


def determine_group_series(
    frame: pd.DataFrame,
    *,
    split_strategy: str,
    group_column: str | None = None,
) -> tuple[pd.Series, str]:
    if split_strategy == "year":
        year_series = pd.to_datetime(frame[WINDOW_END_COLUMN], errors="raise").dt.year.astype(str)
        return year_series, "derived_year"

    if split_strategy != "fire_event":
        raise ValueError(f"Unsupported split_strategy: {split_strategy}")

    selected_group_column = group_column
    if selected_group_column is None:
        for candidate in FIRE_EVENT_COLUMN_CANDIDATES:
            if candidate in frame.columns:
                selected_group_column = candidate
                break
    if selected_group_column is None or selected_group_column not in frame.columns:
        raise ValueError(
            "Fire-event split requires a fire event column. Provide --group-column or include one of: "
            + ", ".join(FIRE_EVENT_COLUMN_CANDIDATES)
        )

    groups = frame[selected_group_column].astype(str)
    if groups.isna().any() or (groups.str.len() == 0).any():
        raise ValueError(f"Grouping column {selected_group_column} contains missing values")
    return groups, selected_group_column


def split_dataset_by_strategy(
    frame: pd.DataFrame,
    *,
    split_strategy: str,
    validation_values: Iterable[str] | None = None,
    group_column: str | None = None,
) -> SplitResult:
    groups, resolved_group_column = determine_group_series(
        frame,
        split_strategy=split_strategy,
        group_column=group_column,
    )
    unique_groups = sorted(groups.dropna().astype(str).unique().tolist())
    if len(unique_groups) < 2:
        raise ValueError(f"Need at least two distinct groups for split strategy {split_strategy}")

    if validation_values:
        validation_groups = sorted({str(value) for value in validation_values})
    else:
        validation_groups = [unique_groups[-1]]

    missing_groups = sorted(set(validation_groups).difference(unique_groups))
    if missing_groups:
        raise ValueError(f"Validation groups not found in dataset: {missing_groups}")

    validation_mask = groups.astype(str).isin(validation_groups)
    train_mask = ~validation_mask
    if not validation_mask.any():
        raise ValueError("Validation split is empty")
    if not train_mask.any():
        raise ValueError("Training split is empty")

    train_groups = set(groups[train_mask].astype(str))
    val_groups = set(groups[validation_mask].astype(str))
    overlap = sorted(train_groups.intersection(val_groups))
    if overlap:
        raise ValueError(f"Leakage detected: group values present in both splits: {overlap}")

    train_frame = frame.loc[train_mask].reset_index(drop=True)
    validation_frame = frame.loc[validation_mask].reset_index(drop=True)

    if train_frame[TARGET_COLUMN].nunique() < 2:
        raise ValueError("Training split must contain both target classes")
    if validation_frame[TARGET_COLUMN].nunique() < 2:
        raise ValueError("Validation split must contain both target classes")

    return SplitResult(
        strategy_name=split_strategy,
        train_frame=train_frame,
        validation_frame=validation_frame,
        group_column=resolved_group_column,
        validation_groups=validation_groups,
    )


def build_estimator(candidate_name: str, *, random_state: int, y_train: pd.Series) -> Any:
    negative_count = int((y_train == 0).sum())
    positive_count = int((y_train == 1).sum())
    scale_pos_weight = float(negative_count / positive_count) if positive_count else 1.0

    if candidate_name == "random_forest":
        return RandomForestClassifier(
            n_estimators=300,
            max_depth=None,
            min_samples_leaf=1,
            random_state=random_state,
            n_jobs=-1,
            class_weight="balanced_subsample",
        )
    if candidate_name == "hist_gradient_boosting":
        return HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=300,
            max_depth=None,
            min_samples_leaf=20,
            random_state=random_state,
        )
    if candidate_name == "xgboost":
        try:
            from xgboost import XGBClassifier
        except ImportError as error:
            raise RuntimeError("XGBoost is not installed but model_type=xgboost was requested") from error
        return XGBClassifier(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.9,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=random_state,
            n_jobs=1,
            scale_pos_weight=scale_pos_weight,
        )
    raise ValueError(f"Unsupported candidate model: {candidate_name}")


def resolve_candidate_models(model_type: str) -> list[str]:
    if model_type in {"random_forest", "hist_gradient_boosting", "xgboost"}:
        return [model_type]
    if model_type != "auto":
        raise ValueError(f"Unsupported model_type: {model_type}")

    candidates = ["random_forest", "hist_gradient_boosting"]
    try:
        import xgboost  # noqa: F401
    except ImportError:
        pass
    else:
        candidates.append("xgboost")
    return candidates


def get_prediction_scores(estimator: Any, features: pd.DataFrame) -> np.ndarray:
    if hasattr(estimator, "predict_proba"):
        return estimator.predict_proba(features)[:, 1]
    if hasattr(estimator, "decision_function"):
        scores = estimator.decision_function(features)
        return 1.0 / (1.0 + np.exp(-scores))
    raise ValueError("Estimator does not support probability or decision score output")


def compute_metrics(y_true: pd.Series, y_score: np.ndarray, threshold: float = 0.5) -> dict[str, float]:
    y_pred = (y_score >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel()
    return {
        "roc_auc": float(roc_auc_score(y_true, y_score)),
        "average_precision": float(average_precision_score(y_true, y_score)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, y_pred)),
        "true_negatives": float(tn),
        "false_positives": float(fp),
        "false_negatives": float(fn),
        "true_positives": float(tp),
    }


def ensure_plot_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def save_confusion_matrix_plot(y_true: pd.Series, y_score: np.ndarray, output_path: Path) -> None:
    ensure_plot_parent(output_path)
    y_pred = (y_score >= 0.5).astype(int)
    plt = get_pyplot()
    fig, ax = plt.subplots(figsize=(5, 4))
    ConfusionMatrixDisplay.from_predictions(y_true, y_pred, ax=ax, colorbar=False)
    ax.set_title("Validation Confusion Matrix")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_roc_curve_plot(y_true: pd.Series, y_score: np.ndarray, output_path: Path) -> None:
    ensure_plot_parent(output_path)
    plt = get_pyplot()
    fig, ax = plt.subplots(figsize=(5, 4))
    RocCurveDisplay.from_predictions(y_true, y_score, ax=ax)
    ax.set_title("Validation ROC Curve")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_pr_curve_plot(y_true: pd.Series, y_score: np.ndarray, output_path: Path) -> None:
    ensure_plot_parent(output_path)
    plt = get_pyplot()
    fig, ax = plt.subplots(figsize=(5, 4))
    PrecisionRecallDisplay.from_predictions(y_true, y_score, ax=ax)
    ax.set_title("Validation Precision-Recall Curve")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def compute_feature_importance(
    estimator: Any,
    x_validation: pd.DataFrame,
    y_validation: pd.Series,
    *,
    random_state: int,
) -> pd.Series:
    if hasattr(estimator, "feature_importances_"):
        values = np.asarray(estimator.feature_importances_, dtype=float)
    else:
        result = permutation_importance(
            estimator,
            x_validation,
            y_validation,
            n_repeats=5,
            random_state=random_state,
            scoring="average_precision",
        )
        values = np.asarray(result.importances_mean, dtype=float)
    return pd.Series(values, index=x_validation.columns).sort_values(ascending=False)


def save_feature_importance_plot(
    feature_importance: pd.Series,
    output_path: Path,
    *,
    top_n: int = 20,
) -> None:
    ensure_plot_parent(output_path)
    top_features = feature_importance.head(top_n).sort_values(ascending=True)
    plt = get_pyplot()
    fig, ax = plt.subplots(figsize=(7, max(4, len(top_features) * 0.35)))
    ax.barh(top_features.index, top_features.values)
    ax.set_title("Validation Feature Importance")
    ax.set_xlabel("Importance")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_model_bundle(
    estimator: Any,
    feature_columns: list[str],
    dataset_version_info: dict[str, list[str]],
    split_result: SplitResult,
    output_path: Path,
    *,
    model_version: str,
    dataset_uri: str,
    train_run_id: str,
    model_name: str,
    candidate_name: str,
) -> None:
    ensure_plot_parent(output_path)
    bundle = {
        "model": estimator,
        "feature_columns": feature_columns,
        "dataset_version_info": dataset_version_info,
        "split_strategy": split_result.strategy_name,
        "split_group_column": split_result.group_column,
        "validation_groups": split_result.validation_groups,
        "decision_threshold": 0.5,
        "model_version": model_version,
        "model_name": model_name,
        "candidate_name": candidate_name,
        "train_run_id": train_run_id,
        "dataset_uri": dataset_uri,
        "bundle_schema_version": "openfire.serving_bundle.v1",
    }
    joblib.dump(bundle, output_path)


def upload_candidate_artifacts(
    *,
    storage: StorageClient,
    artifact_paths: dict[str, Path],
    candidate_root_uri: str,
) -> dict[str, str]:
    uploaded: dict[str, str] = {}
    for artifact_name, local_path in artifact_paths.items():
        destination_uri = f"{candidate_root_uri.rstrip('/')}/{local_path.name}"
        content_type = "application/octet-stream" if local_path.suffix == ".joblib" else None
        if local_path.suffix == ".png":
            content_type = "image/png"
        storage.upload_file(local_path, destination_uri, content_type=content_type)
        uploaded[artifact_name] = destination_uri
    return uploaded


def train_single_candidate(
    *,
    candidate_name: str,
    split_result: SplitResult,
    feature_columns: list[str],
    output_dir: Path,
    tracker: TrackingClient,
    dataset_version_info: dict[str, list[str]],
    dataset_uri: str,
    candidate_prefix_uri: str,
    model_name: str,
    train_run_id: str,
    random_state: int,
    storage: StorageClient,
) -> CandidateTrainingResult:
    candidate_dir = output_dir / candidate_name
    candidate_dir.mkdir(parents=True, exist_ok=True)

    x_train = split_result.train_frame[feature_columns]
    y_train = split_result.train_frame[TARGET_COLUMN].astype(int)
    x_validation = split_result.validation_frame[feature_columns]
    y_validation = split_result.validation_frame[TARGET_COLUMN].astype(int)
    sample_weight = compute_sample_weight(class_weight="balanced", y=y_train)

    estimator = build_estimator(candidate_name, random_state=random_state, y_train=y_train)
    fit_kwargs = {"sample_weight": sample_weight}
    bundle_version = f"{candidate_name}-{train_run_id}"

    run_name = f"{model_name}-{candidate_name}"
    with tracker.start_run(run_name=run_name) as run:
        estimator.fit(x_train, y_train, **fit_kwargs)
        y_score = get_prediction_scores(estimator, x_validation)
        metrics = compute_metrics(y_validation, y_score)
        feature_importance = compute_feature_importance(
            estimator,
            x_validation,
            y_validation,
            random_state=random_state,
        )

        confusion_path = candidate_dir / "confusion_matrix.png"
        roc_path = candidate_dir / "roc_curve.png"
        pr_path = candidate_dir / "pr_curve.png"
        importance_path = candidate_dir / "feature_importance.png"
        model_path = candidate_dir / "model.joblib"

        save_confusion_matrix_plot(y_validation, y_score, confusion_path)
        save_roc_curve_plot(y_validation, y_score, roc_path)
        save_pr_curve_plot(y_validation, y_score, pr_path)
        save_feature_importance_plot(feature_importance, importance_path)
        save_model_bundle(
            estimator,
            feature_columns,
            dataset_version_info,
            split_result,
            model_path,
            model_version=bundle_version,
            dataset_uri=dataset_uri,
            train_run_id=train_run_id,
            model_name=model_name,
            candidate_name=candidate_name,
        )

        local_artifact_paths = {
            "confusion_matrix": str(confusion_path),
            "roc_curve": str(roc_path),
            "pr_curve": str(pr_path),
            "feature_importance": str(importance_path),
            "model_bundle": str(model_path),
        }
        artifact_uris = upload_candidate_artifacts(
            storage=storage,
            artifact_paths={
                "confusion_matrix": confusion_path,
                "roc_curve": roc_path,
                "pr_curve": pr_path,
                "feature_importance": importance_path,
                "model_bundle": model_path,
            },
            candidate_root_uri=f"{candidate_prefix_uri.rstrip('/')}/{candidate_name}",
        )

        tracker.log_params(
            {
                "candidate_name": candidate_name,
                "feature_columns": feature_columns,
                "split_strategy": split_result.strategy_name,
                "split_group_column": split_result.group_column,
                "validation_groups": split_result.validation_groups,
                "dataset_row_count": len(split_result.train_frame) + len(split_result.validation_frame),
                "train_rows": len(split_result.train_frame),
                "validation_rows": len(split_result.validation_frame),
                "dataset_uri": dataset_uri,
                "candidate_artifact_prefix": f"{candidate_prefix_uri.rstrip('/')}/{candidate_name}",
                "candidate_model_bundle_uri": artifact_uris["model_bundle"],
                **dataset_version_info,
            }
        )
        tracker.set_tags(
            {
                "model_family": candidate_name,
                "split_validation_note": (
                    "Grouped temporal or fire-event split baseline. This avoids naive random pixel "
                    "splits, but full spatial holdout is not yet implemented."
                ),
            }
        )
        tracker.log_metrics(metrics)
        for local_artifact_path in local_artifact_paths.values():
            tracker.log_artifact(Path(local_artifact_path))
        model_uri = tracker.log_model(estimator, artifact_path="model")

        return CandidateTrainingResult(
            candidate_name=candidate_name,
            estimator=estimator,
            run_id=run.run_id,
            model_uri=model_uri,
            metrics=metrics,
            artifact_uris=artifact_uris,
            local_artifact_paths=local_artifact_paths,
        )


def choose_best_candidate(results: dict[str, CandidateTrainingResult]) -> CandidateTrainingResult:
    return max(
        results.values(),
        key=lambda result: (
            result.metrics.get(BEST_MODEL_METRIC, float("-inf")),
            result.metrics.get("roc_auc", float("-inf")),
            result.metrics.get("f1", float("-inf")),
        ),
    )


def build_evaluation_summary(
    *,
    results: dict[str, CandidateTrainingResult],
    best_result: CandidateTrainingResult,
    split_result: SplitResult,
    dataset_version_info: dict[str, list[str]],
    feature_columns: list[str],
    model_name: str,
    dataset_uri: str,
    candidate_prefix_uri: str,
) -> dict[str, Any]:
    return {
        "model_name": model_name,
        "best_candidate": best_result.candidate_name,
        "best_model_uri": best_result.model_uri,
        "dataset_uri": dataset_uri,
        "candidate_prefix_uri": candidate_prefix_uri,
        "dataset_version_info": dataset_version_info,
        "feature_columns": feature_columns,
        "split": {
            "strategy_name": split_result.strategy_name,
            "group_column": split_result.group_column,
            "validation_groups": split_result.validation_groups,
            "train_rows": int(len(split_result.train_frame)),
            "validation_rows": int(len(split_result.validation_frame)),
        },
        "candidates": {
            name: {
                "run_id": result.run_id,
                "model_uri": result.model_uri,
                "metrics": result.metrics,
                "artifact_uris": result.artifact_uris,
            }
            for name, result in results.items()
        },
    }


def _local_fallback_registered_locations(
    *,
    storage: StorageClient,
    output_dir: Path,
    model_name: str,
    stage: str,
    local_model_bundle_path: Path,
    metadata_payload: dict[str, Any],
) -> PromotedModelLocations:
    base_uri = normalize_storage_uri(output_dir / "registered" / model_name / stage.lower())
    model_uri = f"{base_uri.rstrip('/')}/model.joblib"
    metadata_uri = f"{base_uri.rstrip('/')}/metadata.json"
    storage.upload_file(local_model_bundle_path, model_uri, content_type="application/octet-stream")
    storage.write_json(metadata_uri, metadata_payload)
    return PromotedModelLocations(model_uri=model_uri, metadata_uri=metadata_uri)


def train_baseline(
    *,
    dataset_uri: str | Path,
    output_dir: Path,
    model_type: str = "auto",
    split_strategy: str = "year",
    validation_values: Iterable[str] | None = None,
    group_column: str | None = None,
    model_name: str = "openfire-baseline",
    feature_columns: list[str] | None = None,
    random_state: int = 42,
    tracker: TrackingClient | None = None,
    tracking_uri: str | None = None,
    experiment_name: str | None = None,
    register_best_model: bool = True,
    promote_stage: str = "production",
    storage: StorageClient | None = None,
    settings: Settings | None = None,
) -> TrainingResult:
    resolved_settings = settings or get_settings()
    resolved_storage = storage or StorageClient.from_settings(resolved_settings)
    normalized_dataset_uri = normalize_storage_uri(dataset_uri)

    dataset = resolved_storage.read_parquet(normalized_dataset_uri)
    selected_feature_columns = feature_columns or DEFAULT_FEATURE_COLUMNS
    validate_training_schema(dataset, selected_feature_columns)
    dataset_version_info = extract_dataset_version_info(dataset)
    split_result = split_dataset_by_strategy(
        dataset,
        split_strategy=split_strategy,
        validation_values=validation_values,
        group_column=group_column,
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    train_run_id = build_version_id("train")
    if resolved_settings.gcs_bucket:
        candidate_prefix_uri = candidate_model_prefix(resolved_settings, model_name, train_run_id)
    else:
        candidate_prefix_uri = normalize_storage_uri(output_dir / "canonical" / "models" / "candidates" / model_name / train_run_id)

    tracker = tracker or MlflowTrackingClient(
        tracking_uri=tracking_uri or resolved_settings.mlflow_tracking_uri,
        experiment_name=experiment_name or resolved_settings.mlflow_experiment_name,
    )

    candidates: dict[str, CandidateTrainingResult] = {}
    for candidate_name in resolve_candidate_models(model_type):
        LOGGER.info("Training candidate model: %s", candidate_name)
        candidates[candidate_name] = train_single_candidate(
            candidate_name=candidate_name,
            split_result=split_result,
            feature_columns=selected_feature_columns,
            output_dir=output_dir,
            tracker=tracker,
            dataset_version_info=dataset_version_info,
            dataset_uri=normalized_dataset_uri,
            candidate_prefix_uri=candidate_prefix_uri,
            model_name=model_name,
            train_run_id=train_run_id,
            random_state=random_state,
            storage=resolved_storage,
        )

    best_result = choose_best_candidate(candidates)
    if register_best_model:
        tracker.register_model(best_result.model_uri, model_name)

    summary_payload = build_evaluation_summary(
        results=candidates,
        best_result=best_result,
        split_result=split_result,
        dataset_version_info=dataset_version_info,
        feature_columns=selected_feature_columns,
        model_name=model_name,
        dataset_uri=normalized_dataset_uri,
        candidate_prefix_uri=candidate_prefix_uri,
    )
    summary_path = output_dir / "evaluation_summary.json"
    summary_path.write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    summary_uri = f"{candidate_prefix_uri.rstrip('/')}/evaluation_summary.json"
    resolved_storage.write_json(summary_uri, summary_payload)

    best_bundle_path = Path(best_result.local_artifact_paths["model_bundle"])
    promotion_metadata = {
        "model_name": model_name,
        "candidate_name": best_result.candidate_name,
        "model_version": f"{best_result.candidate_name}-{train_run_id}",
        "registered_stage": promote_stage.lower(),
        "serving_contract_note": (
            "Promoted serving bundles should be referenced as immutable versioned objects for "
            "deployments and demos; do not rely on mutable overwrite semantics."
        ),
        "source_dataset_uri": normalized_dataset_uri,
        "summary_uri": summary_uri,
        "summary": summary_payload,
        "dataset_version_info": dataset_version_info,
    }
    if resolved_settings.gcs_bucket:
        promoted = promote_serving_bundle(
            storage=resolved_storage,
            settings=resolved_settings,
            model_name=model_name,
            stage=promote_stage,
            local_model_bundle_path=best_bundle_path,
            metadata_payload=promotion_metadata,
        )
    else:
        promoted = _local_fallback_registered_locations(
            storage=resolved_storage,
            output_dir=output_dir,
            model_name=model_name,
            stage=promote_stage,
            local_model_bundle_path=best_bundle_path,
            metadata_payload=promotion_metadata,
        )

    split_details = {
        "split_strategy": split_strategy,
        "group_column": split_result.group_column,
        "validation_groups": split_result.validation_groups,
        "train_rows": len(split_result.train_frame),
        "validation_rows": len(split_result.validation_frame),
    }
    return TrainingResult(
        best_model_name=best_result.candidate_name,
        best_model_uri=best_result.model_uri,
        output_dir=output_dir,
        summary_path=summary_path,
        summary_uri=summary_uri,
        candidate_prefix_uri=candidate_prefix_uri,
        candidates=candidates,
        split_details=split_details,
        dataset_version_info=dataset_version_info,
        registered_model_uri=promoted.model_uri,
        registered_metadata_uri=promoted.metadata_uri,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Train OpenFire baseline models and log to MLflow.")
    parser.add_argument(
        "--dataset-uri",
        "--dataset",
        dest="dataset_uri",
        required=True,
        help="Input assembled parquet dataset URI. Supports local:// and gs://.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=settings.local_tmp_dir / "train_baseline",
        help="Local temporary work directory for plots and model bundles before upload.",
    )
    parser.add_argument(
        "--model-type",
        choices=["auto", "random_forest", "hist_gradient_boosting", "xgboost"],
        default="auto",
    )
    parser.add_argument("--split-strategy", choices=["year", "fire_event"], default="year")
    parser.add_argument(
        "--validation-values",
        nargs="*",
        default=None,
        help="Explicit holdout years or fire events. If omitted, the latest sorted group is held out.",
    )
    parser.add_argument("--group-column", default=None, help="Explicit grouping column for fire_event splits.")
    parser.add_argument("--model-name", default=settings.mlflow_registered_model_name or "openfire-baseline")
    parser.add_argument("--tracking-uri", default=settings.mlflow_tracking_uri)
    parser.add_argument("--experiment-name", default=settings.mlflow_experiment_name)
    parser.add_argument("--promote-stage", default=settings.mlflow_model_stage)
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--no-register", action="store_true", help="Skip MLflow model registry registration.")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    try:
        result = train_baseline(
            dataset_uri=args.dataset_uri,
            output_dir=args.output_dir,
            model_type=args.model_type,
            split_strategy=args.split_strategy,
            validation_values=args.validation_values,
            group_column=args.group_column,
            model_name=args.model_name,
            random_state=args.random_state,
            tracking_uri=args.tracking_uri,
            experiment_name=args.experiment_name,
            register_best_model=not args.no_register,
            promote_stage=args.promote_stage,
        )
    except Exception as error:
        LOGGER.exception("Baseline training failed")
        raise SystemExit(str(error)) from error

    print(
        json.dumps(
            {
                "best_model_name": result.best_model_name,
                "best_model_uri": result.best_model_uri,
                "registered_model_uri": result.registered_model_uri,
                "summary_path": str(result.summary_path),
                "summary_uri": result.summary_uri,
                "split_details": result.split_details,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
