"""Train XGBoost on the gold features dataset.

Reads gold Parquet shards from GCS (or a local directory), performs a temporal
year split, fits XGBoost with scale_pos_weight for class imbalance, logs all
metrics and artifacts to MLflow, and saves a model.joblib bundle.

Usage:
    python -m src.pipelines.train
    python -m src.pipelines.train --validation-year 2023
    python -m src.pipelines.train --parquet-prefix gs://openfire/openfire/datasets/gold/
    python -m src.pipelines.train --sample-frac 0.1   # 10% sample for quick iteration
    python -m src.pipelines.train --dry-run            # print plan, skip fit
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

LOGGER = logging.getLogger(__name__)

GOLD_GCS_PREFIX = "gs://openfire/openfire/datasets/gold/"
GOLD_BQ_TABLE = "msds603-mlops-project.openfire_features.gold_features"
GCP_PROJECT = "msds603-mlops-project"
MLFLOW_TRACKING_URI = "http://34.58.62.126:5000"
MLFLOW_EXPERIMENT_NAME = "openfire-gold-xgboost"
MLFLOW_MODEL_NAME = "openfire-gold"
DEFAULT_VALIDATION_YEAR = "2024"
TARGET_COLUMN = "burned_in_next_15_days"

FEATURE_COLUMNS: list[str] = [
    "days_since_last_burn",
    # NDVI lag deltas
    "ndvi_change_5d", "ndvi_change_15d", "ndvi_change_30d", "ndvi_change_60d",
    # NDWI lag deltas
    "ndwi_change_5d", "ndwi_change_15d", "ndwi_change_30d", "ndwi_change_60d",
    # Temperature lag deltas
    "temp_change_5d", "temp_change_15d", "temp_change_30d", "temp_change_60d",
    # Precipitation lag deltas
    "precip_change_15d", "precip_change_30d", "precip_change_60d",
    # Topography
    "mean_elevation", "mean_slope", "mean_cos_aspect", "mean_sin_aspect",
    # Sentinel-2 bands
    "B2", "B3", "B4", "B8", "B11", "B12",
    # Sentinel-2 indices
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
    # GRIDMET weather
    "gridmet_temp_max", "gridmet_humidity_min", "gridmet_precip_sum", "gridmet_wind_max",
]


# ── data loading ──────────────────────────────────────────────────────────────

def _parse_gcs_prefix(prefix: str) -> tuple[str, str]:
    """Return (bucket_name, blob_prefix) from a gs:// URI."""
    without_scheme = prefix[len("gs://"):]
    bucket, _, blob_prefix = without_scheme.partition("/")
    return bucket, blob_prefix


def load_gold_shards(
    prefix: str,
    *,
    project: str | None = None,
    columns: list[str] | None = None,
    sample_frac: float | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    """Load Parquet shards from a GCS prefix or local directory.

    columns: optional allow-list pushed down to the Parquet reader, reducing I/O
    and peak memory. sample_frac is applied per-shard before accumulation.
    """
    if prefix.startswith("gs://"):
        df = _load_from_gcs(prefix, project=project, columns=columns, sample_frac=sample_frac, random_state=random_state)
    else:
        path = Path(prefix)
        files = sorted(path.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No Parquet files found in {path}")
        frames = []
        for f in files:
            shard = pd.read_parquet(f, columns=columns)
            if sample_frac is not None and 0.0 < sample_frac < 1.0:
                shard = shard.sample(frac=sample_frac, random_state=random_state)
            frames.append(shard)
        df = pd.concat(frames, ignore_index=True)

    LOGGER.info("Loaded %s rows × %d columns", f"{len(df):,}", len(df.columns))
    return df


def _load_from_gcs(
    prefix: str,
    *,
    project: str | None = None,
    columns: list[str] | None = None,
    sample_frac: float | None = None,
    random_state: int = 42,
) -> pd.DataFrame:
    from google.cloud import storage as gcs

    bucket_name, blob_prefix = _parse_gcs_prefix(prefix)
    client = gcs.Client(project=project)
    blobs = [b for b in client.list_blobs(bucket_name, prefix=blob_prefix) if b.name.endswith(".parquet")]
    if not blobs:
        raise FileNotFoundError(f"No Parquet shards found at {prefix}")

    LOGGER.info("Loading %d Parquet shards from %s (sample_frac=%s)", len(blobs), prefix, sample_frac)
    frames = []
    for i, blob in enumerate(blobs, 1):
        if i % 10 == 0:
            LOGGER.info("  shard %d / %d", i, len(blobs))
        shard = pd.read_parquet(io.BytesIO(blob.download_as_bytes()), columns=columns)
        if sample_frac is not None and 0.0 < sample_frac < 1.0:
            shard = shard.sample(frac=sample_frac, random_state=random_state)
        frames.append(shard)
    return pd.concat(frames, ignore_index=True)


# ── temporal split ────────────────────────────────────────────────────────────

def temporal_split(df: pd.DataFrame, *, validation_year: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split by calendar year extracted from window_start_date."""
    years = pd.to_datetime(df["window_start_date"]).dt.year.astype(str)
    val_mask = years == validation_year
    train_mask = ~val_mask

    if not val_mask.any():
        available = sorted(years.unique())
        raise ValueError(f"No rows for validation year {validation_year!r}. Available: {available}")
    if not train_mask.any():
        raise ValueError("Training split is empty after removing the validation year.")

    train_df = df.loc[train_mask].reset_index(drop=True)
    val_df = df.loc[val_mask].reset_index(drop=True)

    train_years = sorted(pd.to_datetime(train_df["window_start_date"]).dt.year.unique())
    LOGGER.info(
        "Split: train %s years (%s rows) | val %s (%s rows)",
        train_years,
        f"{len(train_df):,}",
        validation_year,
        f"{len(val_df):,}",
    )
    return train_df, val_df


# ── model ─────────────────────────────────────────────────────────────────────

def build_xgboost(*, y_train: pd.Series, random_state: int = 42, n_jobs: int = -1) -> Any:
    from xgboost import XGBClassifier

    negative = int((y_train == 0).sum())
    positive = int((y_train == 1).sum())
    scale_pos_weight = float(negative / positive) if positive else 1.0
    LOGGER.info(
        "Class balance: %s positive / %s negative → scale_pos_weight=%.1f",
        f"{positive:,}",
        f"{negative:,}",
        scale_pos_weight,
    )
    return XGBClassifier(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        objective="binary:logistic",
        eval_metric="logloss",
        scale_pos_weight=scale_pos_weight,
        random_state=random_state,
        n_jobs=n_jobs,
    )


# ── metrics and plots ─────────────────────────────────────────────────────────

def compute_metrics(y_true: pd.Series, y_score: np.ndarray, *, threshold: float = 0.5) -> dict[str, float]:
    from sklearn.metrics import (
        accuracy_score,
        average_precision_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y_pred = (y_score >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y_true, y_pred).ravel()
    return {
        "roc_auc": float(roc_auc_score(y_true, y_score)),
        "pr_auc": float(average_precision_score(y_true, y_score)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "true_positives": float(tp),
        "false_positives": float(fp),
        "true_negatives": float(tn),
        "false_negatives": float(fn),
    }


def _get_pyplot():
    os.environ.setdefault("MPLCONFIGDIR", str(Path(".cache") / "openfire" / "matplotlib"))
    Path(os.environ["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def save_plots(
    y_true: pd.Series,
    y_score: np.ndarray,
    output_dir: Path,
    *,
    feature_importances: pd.Series | None = None,
    threshold: float = 0.5,
) -> dict[str, Path]:
    from sklearn.calibration import calibration_curve
    from sklearn.metrics import (
        ConfusionMatrixDisplay,
        PrecisionRecallDisplay,
        RocCurveDisplay,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    plt = _get_pyplot()
    y_pred = (y_score >= threshold).astype(int)
    paths: dict[str, Path] = {}

    # Confusion matrix
    p = output_dir / "confusion_matrix.png"
    fig, ax = plt.subplots(figsize=(5, 4))
    ConfusionMatrixDisplay.from_predictions(y_true, y_pred, ax=ax, colorbar=False)
    ax.set_title("Validation Confusion Matrix")
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths["confusion_matrix"] = p

    # ROC curve
    p = output_dir / "roc_curve.png"
    fig, ax = plt.subplots(figsize=(5, 4))
    RocCurveDisplay.from_predictions(y_true, y_score, ax=ax)
    ax.set_title("Validation ROC Curve")
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths["roc_curve"] = p

    # PR curve
    p = output_dir / "pr_curve.png"
    fig, ax = plt.subplots(figsize=(5, 4))
    PrecisionRecallDisplay.from_predictions(y_true, y_score, ax=ax)
    ax.set_title("Validation Precision-Recall Curve")
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths["pr_curve"] = p

    # Calibration curve
    p = output_dir / "calibration.png"
    frac_pos, mean_pred = calibration_curve(y_true, y_score, n_bins=10, strategy="quantile")
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(mean_pred, frac_pos, marker="o", label="Model")
    ax.plot([0, 1], [0, 1], linestyle="--", label="Perfectly calibrated")
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Fraction of positives")
    ax.set_title("Calibration Curve (validation)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(p, dpi=150)
    plt.close(fig)
    paths["calibration"] = p

    # Feature importance
    if feature_importances is not None:
        p = output_dir / "feature_importance.png"
        top = feature_importances.head(30).sort_values(ascending=True)
        fig, ax = plt.subplots(figsize=(7, max(4, len(top) * 0.35)))
        ax.barh(top.index, top.values)
        ax.set_title("Feature Importance (gain)")
        ax.set_xlabel("Importance")
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        paths["feature_importance"] = p

    return paths


# ── bundle ────────────────────────────────────────────────────────────────────

def build_bundle(
    model: Any,
    *,
    run_id: str,
    validation_year: str,
    parquet_prefix: str,
) -> dict[str, Any]:
    return {
        "model": model,
        "feature_columns": FEATURE_COLUMNS,
        "decision_threshold": 0.5,
        "dataset_version_info": {
            "bq_table": [GOLD_BQ_TABLE],
            "gcs_parquet_prefix": [parquet_prefix.rstrip("/") + "/"],
        },
        "split_strategy": "year",
        "split_group_column": "window_start_date",
        "validation_groups": [validation_year],
        "model_version": run_id,
    }


# ── training entry point ──────────────────────────────────────────────────────

def train(
    *,
    parquet_prefix: str = GOLD_GCS_PREFIX,
    validation_year: str = DEFAULT_VALIDATION_YEAR,
    output_dir: Path,
    mlflow_tracking_uri: str = MLFLOW_TRACKING_URI,
    mlflow_experiment: str = MLFLOW_EXPERIMENT_NAME,
    mlflow_model_name: str = MLFLOW_MODEL_NAME,
    gcp_project: str = GCP_PROJECT,
    sample_frac: float | None = None,
    random_state: int = 42,
    register: bool = True,
    dry_run: bool = False,
    n_jobs: int = -1,
) -> dict[str, Any]:
    if dry_run:
        LOGGER.info("[DRY RUN] Would load gold shards from: %s", parquet_prefix)
        LOGGER.info("[DRY RUN] Validation year: %s", validation_year)
        LOGGER.info("[DRY RUN] MLflow: %s / experiment=%s", mlflow_tracking_uri, mlflow_experiment)
        LOGGER.info("[DRY RUN] Feature columns (%d): %s", len(FEATURE_COLUMNS), FEATURE_COLUMNS)
        return {"dry_run": True}

    import mlflow
    import mlflow.xgboost

    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"train-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # Load — restrict to only the columns training needs to reduce peak memory
    _needed_cols = FEATURE_COLUMNS + [TARGET_COLUMN, "window_start_date"]
    LOGGER.info("Loading gold shards from %s", parquet_prefix)
    df = load_gold_shards(parquet_prefix, project=gcp_project, columns=_needed_cols, sample_frac=sample_frac, random_state=random_state)

    # Split
    train_df, val_df = temporal_split(df, validation_year=validation_year)
    del df  # free memory before fitting

    x_train = train_df[FEATURE_COLUMNS]
    y_train = train_df[TARGET_COLUMN].astype(int)
    x_val = val_df[FEATURE_COLUMNS]
    y_val = val_df[TARGET_COLUMN].astype(int)

    # Model
    model = build_xgboost(y_train=y_train, random_state=random_state, n_jobs=n_jobs)

    # MLflow run
    mlflow.set_tracking_uri(mlflow_tracking_uri)
    mlflow.set_experiment(mlflow_experiment)

    with mlflow.start_run(run_name=run_id) as active_run:
        mlflow_run_id = active_run.info.run_id

        LOGGER.info("Fitting XGBoost …")
        model.fit(x_train, y_train, verbose=False)

        y_score = model.predict_proba(x_val)[:, 1]
        metrics = compute_metrics(y_val, y_score)

        for name, value in metrics.items():
            LOGGER.info("  %s: %.4f", name, value)

        mlflow.log_params({
            "validation_year": validation_year,
            "train_rows": len(x_train),
            "val_rows": len(x_val),
            "feature_columns": json.dumps(FEATURE_COLUMNS),
            "parquet_prefix": parquet_prefix,
            "sample_frac": str(sample_frac),
            "n_estimators": model.n_estimators,
            "max_depth": model.max_depth,
            "learning_rate": model.learning_rate,
            "subsample": model.subsample,
            "colsample_bytree": model.colsample_bytree,
            "scale_pos_weight": model.scale_pos_weight,
        })
        mlflow.log_metrics(metrics)
        mlflow.set_tags({
            "split_strategy": "temporal_year",
            "model_family": "xgboost",
            "gold_bq_table": GOLD_BQ_TABLE,
        })

        # Plots
        fi_series: pd.Series | None = None
        if hasattr(model, "feature_importances_"):
            fi_series = pd.Series(
                model.feature_importances_,
                index=FEATURE_COLUMNS,
            ).sort_values(ascending=False)

        plot_paths = save_plots(y_val, y_score, output_dir / "plots", feature_importances=fi_series)
        for path in plot_paths.values():
            mlflow.log_artifact(str(path))

        # Model bundle
        bundle = build_bundle(model, run_id=mlflow_run_id, validation_year=validation_year, parquet_prefix=parquet_prefix)
        bundle_path = output_dir / "model.joblib"
        joblib.dump(bundle, bundle_path)
        mlflow.log_artifact(str(bundle_path))

        # XGBoost-native model artifact (enables mlflow models serve)
        mlflow.xgboost.log_model(model, artifact_path="xgboost_model")

        # Feature importance JSON
        if fi_series is not None:
            fi_path = output_dir / "feature_importance.json"
            fi_path.write_text(fi_series.to_json(), encoding="utf-8")
            mlflow.log_artifact(str(fi_path))

        model_uri = f"runs:/{mlflow_run_id}/xgboost_model"

    # Register and promote
    if register:
        LOGGER.info("Registering model as %r …", mlflow_model_name)
        mv = mlflow.register_model(model_uri=model_uri, name=mlflow_model_name)
        _transition_to_production(mlflow_tracking_uri=mlflow_tracking_uri, model_name=mlflow_model_name, version=mv.version)
        LOGGER.info("Registered model version %s → Production", mv.version)

    result = {
        "run_id": mlflow_run_id,
        "mlflow_run_id": mlflow_run_id,
        "model_uri": model_uri,
        "bundle_path": str(bundle_path),
        "metrics": metrics,
        "feature_columns": FEATURE_COLUMNS,
        "validation_year": validation_year,
    }
    summary_path = output_dir / "training_summary.json"
    summary_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    LOGGER.info("Summary written to %s", summary_path)
    return result


def _transition_to_production(*, mlflow_tracking_uri: str, model_name: str, version: str) -> None:
    from mlflow.tracking import MlflowClient
    client = MlflowClient(tracking_uri=mlflow_tracking_uri)
    client.transition_model_version_stage(
        name=model_name,
        version=str(version),
        stage="Production",
        archive_existing_versions=True,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--parquet-prefix", default=GOLD_GCS_PREFIX, help="GCS prefix or local directory of gold Parquet shards")
    parser.add_argument("--validation-year", default=DEFAULT_VALIDATION_YEAR, help="Year to hold out for validation (default: 2024)")
    parser.add_argument("--output-dir", type=Path, default=Path(".cache/openfire/train"), help="Local directory for artifacts")
    parser.add_argument("--mlflow-tracking-uri", default=MLFLOW_TRACKING_URI)
    parser.add_argument("--mlflow-experiment", default=MLFLOW_EXPERIMENT_NAME)
    parser.add_argument("--mlflow-model-name", default=MLFLOW_MODEL_NAME)
    parser.add_argument("--gcp-project", default=GCP_PROJECT)
    parser.add_argument("--sample-frac", type=float, default=None, help="Downsample fraction (0–1) for quick iteration")
    parser.add_argument("--random-state", type=int, default=42)
    parser.add_argument("--no-register", action="store_true", help="Skip MLflow model registration")
    parser.add_argument("--n-jobs", type=int, default=-1, help="XGBoost parallel threads (-1 = all cores)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without loading data or fitting")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        result = train(
            parquet_prefix=args.parquet_prefix,
            validation_year=args.validation_year,
            output_dir=args.output_dir,
            mlflow_tracking_uri=args.mlflow_tracking_uri,
            mlflow_experiment=args.mlflow_experiment,
            mlflow_model_name=args.mlflow_model_name,
            gcp_project=args.gcp_project,
            sample_frac=args.sample_frac,
            random_state=args.random_state,
            register=not args.no_register,
            dry_run=args.dry_run,
            n_jobs=args.n_jobs,
        )
    except Exception as error:
        LOGGER.exception("Training failed")
        raise SystemExit(str(error)) from error

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
