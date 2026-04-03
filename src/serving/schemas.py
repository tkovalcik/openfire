from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


FEATURE_COLUMNS = [
    "latitude",
    "longitude",
    "NDVI",
    "EVI",
    "NDWI",
    "NBR",
    "B2",
    "B3",
    "B4",
    "B8",
    "B11",
    "B12",
    "elevation",
    "slope",
    "aspect",
    "mean_ndvi_100m",
    "mean_ndvi_500m",
    "precip_7d_sum",
    "precip_30d_sum",
    "temp_7d_mean",
    "temp_30d_mean",
    "humidity_7d_mean",
    "humidity_30d_mean",
    "wind_7d_max",
    "wind_30d_max",
]


class FeatureRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    NDVI: float = Field(..., ge=-1.0, le=1.0)
    EVI: float
    NDWI: float
    NBR: float
    B2: float
    B3: float
    B4: float
    B8: float
    B11: float
    B12: float
    elevation: float
    slope: float = Field(..., ge=0.0)
    aspect: float = Field(..., ge=0.0, le=360.0)
    mean_ndvi_100m: float = Field(..., ge=-1.0, le=1.0)
    mean_ndvi_500m: float = Field(..., ge=-1.0, le=1.0)
    precip_7d_sum: float = Field(..., ge=0.0)
    precip_30d_sum: float = Field(..., ge=0.0)
    temp_7d_mean: float
    temp_30d_mean: float
    humidity_7d_mean: float = Field(..., ge=0.0, le=100.0)
    humidity_30d_mean: float = Field(..., ge=0.0, le=100.0)
    wind_7d_max: float = Field(..., ge=0.0)
    wind_30d_max: float = Field(..., ge=0.0)


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
