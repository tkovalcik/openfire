from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from .model_loader import LoadedModel, ModelLoadError, ServiceConfig, load_active_model, load_model_bundle_from_uri, load_registry_model_bundle
from .schemas import (
    FeatureRow,
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONPointGeometry,
    PredictionRequest,
    PredictionResponse,
    PredictionResult,
)


LOGGER = logging.getLogger("openfire.serving")


class PredictionInputError(ValueError):
    """Raised when a prediction request is invalid for the loaded model."""


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    LOGGER.info(json.dumps(payload, sort_keys=True))


def _rows_to_frame(rows: list[FeatureRow], feature_columns: list[str]) -> pd.DataFrame:
    values = [row.model_dump() for row in rows]
    frame = pd.DataFrame(values)
    missing = sorted(set(feature_columns).difference(frame.columns))
    if missing:
        raise PredictionInputError(f"Request rows are missing required feature columns: {missing}")
    return frame[feature_columns].copy()


def _predict_probabilities(model: Any, frame: pd.DataFrame) -> list[float]:
    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(frame)[:, 1]
    elif hasattr(model, "decision_function"):
        scores = model.decision_function(frame)
        probabilities = 1.0 / (1.0 + np.exp(-scores))
    else:
        raise ModelLoadError("Loaded model does not support probability inference.")
    return [float(value) for value in probabilities]


class PredictionService:
    def __init__(self, loaded_model: LoadedModel, *, max_batch_size: int) -> None:
        self.loaded_model = loaded_model
        self.max_batch_size = max_batch_size

    def validate_request(self, request: PredictionRequest) -> None:
        row_count = len(request.rows)
        if row_count == 0:
            raise PredictionInputError("Prediction request must contain at least one row.")
        if row_count > self.max_batch_size:
            raise PredictionInputError(
                f"Batch size {row_count} exceeds OPENFIRE_MAX_BATCH_SIZE={self.max_batch_size}."
            )

    def predict(self, request: PredictionRequest) -> PredictionResponse:
        self.validate_request(request)
        frame = _rows_to_frame(request.rows, self.loaded_model.feature_columns)
        probabilities = _predict_probabilities(self.loaded_model.model, frame)

        predictions = [
            PredictionResult(
                row_index=index,
                probability=probability,
                predicted_label=1 if probability >= self.loaded_model.decision_threshold else 0,
                model_version=self.loaded_model.model_version,
            )
            for index, probability in enumerate(probabilities)
        ]
        log_event(
            "prediction_batch",
            row_count=len(predictions),
            model_source=self.loaded_model.model_source,
            model_version=self.loaded_model.model_version,
            model_uri=self.loaded_model.model_uri,
        )
        return PredictionResponse(
            model_version=self.loaded_model.model_version,
            predictions=predictions,
        )

    def predict_geojson(self, request: PredictionRequest) -> tuple[PredictionResponse, GeoJSONFeatureCollection]:
        response = self.predict(request)
        features = []
        for row, prediction in zip(request.rows, response.predictions, strict=True):
            features.append(
                GeoJSONFeature(
                    geometry=GeoJSONPointGeometry(
                        coordinates=[row.longitude, row.latitude],
                    ),
                    properties={
                        "row_index": prediction.row_index,
                        "probability": prediction.probability,
                        "predicted_label": prediction.predicted_label,
                        "model_version": prediction.model_version,
                    },
                )
            )
        return response, GeoJSONFeatureCollection(features=features)


__all__ = [
    "LoadedModel",
    "ModelLoadError",
    "PredictionInputError",
    "PredictionService",
    "ServiceConfig",
    "load_active_model",
    "load_model_bundle_from_uri",
    "load_registry_model_bundle",
]
