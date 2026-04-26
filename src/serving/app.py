from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    from common.storage import StorageClient
    from config.settings import Settings, get_settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.storage import StorageClient
    from src.config.settings import Settings, get_settings

from .model_loader import ServiceConfig, build_internal_loader_metadata, load_active_model
from .predict import ModelLoadError, PredictionInputError, PredictionService
from .schemas import (
    GeoJSONPredictionResponse,
    HealthResponse,
    MetadataResponse,
    PredictionRequest,
    PredictionResponse,
)


LOGGER = logging.getLogger("openfire.serving")


def configure_logging() -> None:
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def log_event(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True))


def _model_unavailable_response(config: ServiceConfig) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "model_unavailable",
            "detail": "Prediction model is not loaded.",
            "model_source": config.model_source,
        },
    )


def create_app(
    config: ServiceConfig | None = None,
    *,
    settings: Settings | None = None,
    storage: StorageClient | None = None,
) -> FastAPI:
    configure_logging()
    resolved_settings = settings or get_settings()
    service_config = config or ServiceConfig.from_settings(resolved_settings)
    resolved_storage = storage or StorageClient.from_settings(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.prediction_service = None
        app.state.model_load_error = None
        try:
            loaded_model = load_active_model(
                service_config,
                storage=resolved_storage,
                settings=resolved_settings,
            )
            app.state.prediction_service = PredictionService(
                loaded_model,
                max_batch_size=service_config.max_batch_size,
            )
            app.state.operational_metadata = build_internal_loader_metadata(
                service_config,
                loaded_model=loaded_model,
            )
            log_event(
                "startup_complete",
                runtime_mode=service_config.runtime_mode,
                model_loaded=True,
                model_source=loaded_model.model_source,
                model_version=loaded_model.model_version,
                max_batch_size=service_config.max_batch_size,
            )
        except Exception as error:  # pragma: no cover - exercised through app tests
            app.state.model_load_error = "Model loading failed during startup."
            app.state.operational_metadata = build_internal_loader_metadata(
                service_config,
                load_error=str(error),
            )
            log_event(
                "startup_degraded",
                runtime_mode=service_config.runtime_mode,
                model_loaded=False,
                model_source=service_config.model_source,
                detail=str(error),
            )
        yield

    app = FastAPI(
        title="OpenFire Serving API",
        version="0.1.0",
        description="Stateless FastAPI service for wildfire risk predictions.",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.service_config = service_config
    app.state.storage = resolved_storage
    app.state.operational_metadata = build_internal_loader_metadata(service_config)
    if resolved_settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    @app.exception_handler(PredictionInputError)
    async def handle_prediction_input_error(
        request: Request,
        exc: PredictionInputError,
    ) -> JSONResponse:
        log_event("request_invalid", path=str(request.url.path), detail=str(exc))
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ModelLoadError)
    async def handle_model_load_error(request: Request, exc: ModelLoadError) -> JSONResponse:
        log_event("model_unavailable", path=str(request.url.path), detail=str(exc))
        return _model_unavailable_response(request.app.state.service_config)

    @app.get("/")
    async def root(request: Request) -> JSONResponse:
        config: ServiceConfig = request.app.state.service_config
        service: PredictionService | None = getattr(request.app.state, "prediction_service", None)
        status = "ok" if service is not None else "degraded"
        model_source = service.loaded_model.model_source if service is not None else config.model_source
        model_version = service.loaded_model.model_version if service is not None else None
        return JSONResponse(
            status_code=200,
            content={
                "service": "openfire-api",
                "message": "OpenFire Serving API",
                "status": status,
                "runtime_mode": config.runtime_mode,
                "model_loaded": service is not None,
                "model_source": model_source,
                "model_version": model_version,
                "endpoints": {
                    "health": "/health",
                    "metadata": "/metadata",
                    "demo_geojson": "/demo/geojson",
                    "predict": "/predict",
                    "predict_geojson": "/predict_geojson",
                    "docs": "/docs",
                },
            },
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        config: ServiceConfig = request.app.state.service_config
        service: PredictionService | None = getattr(request.app.state, "prediction_service", None)
        if service is None:
            return HealthResponse(
                status="degraded",
                model_loaded=False,
                runtime_mode=config.runtime_mode,
                model_source=config.model_source,
                model_version=None,
            )
        loaded = service.loaded_model
        return HealthResponse(
            status="ok",
            model_loaded=True,
            runtime_mode=config.runtime_mode,
            model_source=loaded.model_source,
            model_version=loaded.model_version,
        )

    @app.get("/metadata", response_model=MetadataResponse)
    async def metadata(request: Request) -> MetadataResponse:
        config: ServiceConfig = request.app.state.service_config
        service: PredictionService | None = getattr(request.app.state, "prediction_service", None)
        if service is None:
            return MetadataResponse(
                model_loaded=False,
                runtime_mode=config.runtime_mode,
                model_source=config.model_source,
                model_version=None,
                feature_columns=[],
                dataset_version_info={},
                decision_threshold=None,
                split_strategy=None,
                split_group_column=None,
                validation_groups=[],
            )
        loaded = service.loaded_model
        return MetadataResponse(
            model_loaded=True,
            runtime_mode=config.runtime_mode,
            model_source=loaded.model_source,
            model_version=loaded.model_version,
            feature_columns=loaded.feature_columns,
            dataset_version_info=loaded.dataset_version_info,
            decision_threshold=loaded.decision_threshold,
            split_strategy=loaded.split_strategy,
            split_group_column=loaded.split_group_column,
            validation_groups=loaded.validation_groups,
        )

    @app.get("/demo/geojson")
    async def demo_geojson(request: Request) -> JSONResponse:
        config: ServiceConfig = request.app.state.service_config
        storage_client: StorageClient = request.app.state.storage
        if not config.demo_geojson_uri:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "demo_artifact_unconfigured",
                    "detail": "Demo GeoJSON URI is not configured.",
                },
            )
        try:
            payload = storage_client.read_json(config.demo_geojson_uri)
        except FileNotFoundError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "demo_artifact_unavailable",
                    "detail": "Configured demo GeoJSON artifact was not found.",
                },
            )
        except Exception:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "demo_artifact_unavailable",
                    "detail": "Configured demo GeoJSON artifact could not be read.",
                },
            )
        return JSONResponse(status_code=200, content=payload)

    @app.post("/predict", response_model=PredictionResponse)
    async def predict(request: Request, payload: PredictionRequest) -> PredictionResponse:
        service: PredictionService | None = request.app.state.prediction_service
        if service is None:
            return _model_unavailable_response(request.app.state.service_config)
        return service.predict(payload)

    @app.post("/predict_geojson", response_model=GeoJSONPredictionResponse)
    async def predict_geojson(
        request: Request,
        payload: PredictionRequest,
    ) -> GeoJSONPredictionResponse:
        service: PredictionService | None = request.app.state.prediction_service
        if service is None:
            return _model_unavailable_response(request.app.state.service_config)
        response, feature_collection = service.predict_geojson(payload)
        return GeoJSONPredictionResponse(
            model_version=response.model_version,
            predictions=response.predictions,
            feature_collection=feature_collection,
        )

    return app


app = create_app()
