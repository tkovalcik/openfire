from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Mirrors src/pipelines/train.py::FEATURE_COLUMNS — the 34-column gold-feature
# input vector consumed by the XGBoost model. Keep this list in lockstep with
# the training pipeline; the serving bundle's feature_columns must match.
FEATURE_COLUMNS = [
    "days_since_last_burn",
    "ndvi_change_5d", "ndvi_change_15d", "ndvi_change_30d", "ndvi_change_60d",
    "ndwi_change_5d", "ndwi_change_15d", "ndwi_change_30d", "ndwi_change_60d",
    "temp_change_5d", "temp_change_15d", "temp_change_30d", "temp_change_60d",
    "precip_change_15d", "precip_change_30d", "precip_change_60d",
    "mean_elevation", "mean_slope", "mean_cos_aspect", "mean_sin_aspect",
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
    "gridmet_temp_max", "gridmet_humidity_min", "gridmet_precip_sum", "gridmet_wind_max",
]


class FeatureRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Geometry — not part of FEATURE_COLUMNS; required for GeoJSON output.
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)

    # 9999 is a magic "never burned" sentinel; keep the upper bound permissive.
    days_since_last_burn: int = Field(..., ge=0)

    ndvi_change_5d: float
    ndvi_change_15d: float
    ndvi_change_30d: float
    ndvi_change_60d: float

    ndwi_change_5d: float
    ndwi_change_15d: float
    ndwi_change_30d: float
    ndwi_change_60d: float

    temp_change_5d: float
    temp_change_15d: float
    temp_change_30d: float
    temp_change_60d: float

    precip_change_15d: float
    precip_change_30d: float
    precip_change_60d: float

    mean_elevation: float
    mean_slope: float = Field(..., ge=0.0)
    mean_cos_aspect: float = Field(..., ge=-1.0, le=1.0)
    mean_sin_aspect: float = Field(..., ge=-1.0, le=1.0)

    B2: float
    B3: float
    B4: float
    B8: float
    B11: float
    B12: float

    mean_NDVI: float = Field(..., ge=-1.0, le=1.0)
    mean_EVI: float
    mean_NDWI: float = Field(..., ge=-1.0, le=1.0)
    mean_NBR: float

    gridmet_temp_max: float
    gridmet_humidity_min: float = Field(..., ge=0.0, le=100.0)
    gridmet_precip_sum: float = Field(..., ge=0.0)
    gridmet_wind_max: float = Field(..., ge=0.0)


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[FeatureRow] = Field(..., min_length=1)


class PredictionResult(BaseModel):
    row_index: int
    probability: float = Field(..., ge=0.0, le=1.0)
    predicted_label: Literal[0, 1]
    model_version: str


class GeoJSONPointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: list[float]

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(cls, value: list[float]) -> list[float]:
        if len(value) != 2:
            raise ValueError("GeoJSON Point coordinates must contain [longitude, latitude].")
        return value


class GeoJSONFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: GeoJSONPointGeometry
    properties: dict[str, Any]


class GeoJSONFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[GeoJSONFeature]


class PredictionResponse(BaseModel):
    model_version: str
    predictions: list[PredictionResult]


class GeoJSONPredictionResponse(PredictionResponse):
    feature_collection: GeoJSONFeatureCollection


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_loaded: bool
    runtime_mode: Literal["demo", "live"]
    model_source: str | None
    model_version: str | None


class MetadataResponse(BaseModel):
    model_loaded: bool
    runtime_mode: Literal["demo", "live"]
    model_source: str | None
    model_version: str | None
    feature_columns: list[str]
    dataset_version_info: dict[str, list[str]]
    decision_threshold: float | None
    split_strategy: str | None
    split_group_column: str | None
    validation_groups: list[str]
