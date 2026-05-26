"""Inference predict module — load openfire-gold from MLflow and score a window.

Reuses `src/serving/model_loader.py::load_registry_model_bundle` (already
tested) so the inference path can't drift from the serving path on bundle
loading. Skips `src/serving/predict.py::PredictionService` because that's
request-scoped Pydantic-per-row code, far too slow for batch scoring of
millions of rows per window. We score the DataFrame directly.

Output schema is the columns the inference pipeline writes to
`predictions_history`:
    latitude, longitude, window_start_date,
    risk_probability, predicted_label,
    model_name, model_version, inference_run_at

`model_version` is stamped from the loaded MLflow registry version so
every row has provenance back to a specific run. When a new model is
promoted to Production, subsequent runs pick it up automatically; old
predictions retain their original version. That's the A/B story.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.serving.model_loader import LoadedModel, load_registry_model_bundle


LOGGER = logging.getLogger(__name__)

# Canonical MLflow + registered-model defaults — must match Phase 3 (train.py).
MLFLOW_TRACKING_URI = "http://34.58.62.126:5000"
MLFLOW_MODEL_NAME = "openfire-gold"
MLFLOW_MODEL_STAGE = "Production"

# Identity columns carried through to predictions_history (not features).
KEY_COLUMNS = ["latitude", "longitude", "window_start_date"]


def load_production_bundle(
    *,
    tracking_uri: str = MLFLOW_TRACKING_URI,
    model_name: str = MLFLOW_MODEL_NAME,
    stage: str = MLFLOW_MODEL_STAGE,
) -> LoadedModel:
    """Fetch the current Production bundle for openfire-gold from MLflow.

    Wraps `serving.model_loader.load_registry_model_bundle` so we have one
    importable entry point for the inference orchestrator. Failures
    propagate as `ModelLoadError` — do not silently fall back to a previous
    version (provenance must stay accurate; see PROGRESS_phase4_additions §9).
    """
    return load_registry_model_bundle(
        tracking_uri=tracking_uri,
        model_name=model_name,
        model_stage=stage,
    )


def _predict_proba(model: Any, x: pd.DataFrame) -> pd.Series:
    if not hasattr(model, "predict_proba"):
        raise RuntimeError(
            "Loaded model does not expose predict_proba; inference requires "
            "probability outputs."
        )
    return pd.Series(model.predict_proba(x)[:, 1], index=x.index, dtype="float64")


def predict_window(
    features: pd.DataFrame,
    *,
    loaded_model: LoadedModel,
    inference_run_at: datetime | None = None,
) -> pd.DataFrame:
    """Score a feature DataFrame and return predictions in the predictions_history schema.

    `features` must contain at least the model's `feature_columns` plus the
    KEY_COLUMNS (latitude, longitude, window_start_date). Extra columns are
    ignored. We do NOT validate per-row with Pydantic — that's request-time
    serving behavior; in batch we trust the BQ schema.
    """
    missing_keys = [c for c in KEY_COLUMNS if c not in features.columns]
    if missing_keys:
        raise ValueError(f"features is missing identity columns: {missing_keys}")

    missing_features = [c for c in loaded_model.feature_columns if c not in features.columns]
    if missing_features:
        raise ValueError(
            f"features is missing model input columns: {missing_features}"
        )

    if features.empty:
        LOGGER.warning("predict_window called on empty DataFrame; returning empty result.")
        return pd.DataFrame(columns=KEY_COLUMNS + [
            "risk_probability", "predicted_label",
            "model_name", "model_version", "inference_run_at",
        ])

    x = features[loaded_model.feature_columns]
    probabilities = _predict_proba(loaded_model.model, x)
    threshold = loaded_model.decision_threshold

    run_at = inference_run_at or datetime.now(timezone.utc)

    out = features[KEY_COLUMNS].copy()
    out["risk_probability"] = probabilities.values
    out["predicted_label"] = (probabilities.values >= threshold).astype("int8")
    # Defer to the loaded model's source for the "name" — when we promote a
    # different registered model later, this still tracks correctly.
    model_name = MLFLOW_MODEL_NAME
    if loaded_model.model_source.startswith("mlflow_registry:"):
        # source is "mlflow_registry:<name>/<stage>" — extract the name.
        model_name = loaded_model.model_source.split(":", 1)[1].split("/", 1)[0]
    out["model_name"] = model_name
    out["model_version"] = loaded_model.model_version
    out["inference_run_at"] = run_at

    LOGGER.info(
        "predict_window: scored %d rows; positive rate at threshold %.3f = %.4f",
        len(out),
        threshold,
        float(out["predicted_label"].mean()) if len(out) else 0.0,
    )
    return out


__all__ = [
    "MLFLOW_TRACKING_URI",
    "MLFLOW_MODEL_NAME",
    "MLFLOW_MODEL_STAGE",
    "KEY_COLUMNS",
    "load_production_bundle",
    "predict_window",
]
