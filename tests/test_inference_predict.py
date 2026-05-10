"""Tests for src/pipelines/predict.py.

Validates the inference predict module against a fake LoadedModel — no
MLflow access required. Covers:
- Output schema matches predictions_history contract.
- model_version is stamped from the loaded bundle (provenance test).
- model_name is parsed from mlflow_registry source string.
- Decision threshold from the bundle is used (not hardcoded 0.5).
- Empty input returns empty output with correct columns.
- Missing identity / feature columns raise.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from pipelines.predict import KEY_COLUMNS, predict_window
from serving.model_loader import LoadedModel


class _FakeModel:
    """Minimal sklearn-like model that returns a fixed positive-class probability."""

    def __init__(self, prob: float = 0.7) -> None:
        self.prob = prob

    def predict_proba(self, x: pd.DataFrame) -> np.ndarray:
        n = len(x)
        return np.column_stack([np.full(n, 1 - self.prob), np.full(n, self.prob)])


def _make_loaded(
    *,
    feature_columns: list[str] | None = None,
    threshold: float = 0.5,
    version: str = "7",
    source: str = "mlflow_registry:openfire-gold/Production",
    prob: float = 0.7,
) -> LoadedModel:
    cols = feature_columns or ["B2", "B3", "mean_NDVI"]
    return LoadedModel(
        model=_FakeModel(prob=prob),
        model_source=source,
        model_version=version,
        feature_columns=cols,
        dataset_version_info={},
        decision_threshold=threshold,
        split_strategy="year",
        split_group_column="window_start_date",
        validation_groups=["2024"],
        model_uri="models:/openfire-gold/Production",
    )


def _make_features(n: int = 3, *, columns: list[str] | None = None) -> pd.DataFrame:
    cols = columns or ["B2", "B3", "mean_NDVI"]
    df = pd.DataFrame({c: np.linspace(0, 1, n) for c in cols})
    df["latitude"] = np.linspace(34.0, 35.0, n)
    df["longitude"] = np.linspace(-119.0, -118.0, n)
    df["window_start_date"] = pd.to_datetime(["2025-01-03"] * n).date
    return df


def test_predict_window_output_schema() -> None:
    loaded = _make_loaded()
    features = _make_features(3)
    out = predict_window(features, loaded_model=loaded)
    assert list(out.columns) == KEY_COLUMNS + [
        "risk_probability", "predicted_label",
        "model_name", "model_version", "inference_run_at",
    ]
    assert len(out) == 3


def test_predict_window_stamps_provenance() -> None:
    loaded = _make_loaded(version="42", source="mlflow_registry:openfire-gold/Production")
    out = predict_window(_make_features(2), loaded_model=loaded)
    assert (out["model_version"] == "42").all()
    assert (out["model_name"] == "openfire-gold").all()


def test_predict_window_uses_bundle_threshold() -> None:
    loaded_high = _make_loaded(threshold=0.9, prob=0.7)
    loaded_low = _make_loaded(threshold=0.1, prob=0.7)
    out_high = predict_window(_make_features(2), loaded_model=loaded_high)
    out_low = predict_window(_make_features(2), loaded_model=loaded_low)
    # prob=0.7 — below 0.9 threshold (label=0), above 0.1 threshold (label=1).
    assert (out_high["predicted_label"] == 0).all()
    assert (out_low["predicted_label"] == 1).all()


def test_predict_window_uses_inference_run_at_override() -> None:
    fixed = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    out = predict_window(
        _make_features(1),
        loaded_model=_make_loaded(),
        inference_run_at=fixed,
    )
    assert (out["inference_run_at"] == fixed).all()


def test_predict_window_empty_input_returns_empty_with_schema() -> None:
    empty = pd.DataFrame(columns=KEY_COLUMNS + ["B2", "B3", "mean_NDVI"])
    out = predict_window(empty, loaded_model=_make_loaded())
    assert len(out) == 0
    assert "risk_probability" in out.columns
    assert "model_version" in out.columns


def test_predict_window_missing_identity_columns_raises() -> None:
    features = _make_features(2).drop(columns=["latitude"])
    with pytest.raises(ValueError, match="missing identity columns"):
        predict_window(features, loaded_model=_make_loaded())


def test_predict_window_missing_feature_columns_raises() -> None:
    features = _make_features(2).drop(columns=["mean_NDVI"])
    with pytest.raises(ValueError, match="missing model input columns"):
        predict_window(features, loaded_model=_make_loaded())


def test_predict_window_extra_columns_are_ignored() -> None:
    features = _make_features(2)
    features["unrelated_col"] = "ignore-me"
    out = predict_window(features, loaded_model=_make_loaded())
    assert "unrelated_col" not in out.columns
    assert len(out) == 2


def test_predict_window_falls_back_to_default_model_name_for_local_source() -> None:
    """When source isn't an mlflow_registry URI, model_name falls back to default."""
    loaded = _make_loaded(source="local:./model.joblib")
    out = predict_window(_make_features(1), loaded_model=loaded)
    assert (out["model_name"] == "openfire-gold").all()
