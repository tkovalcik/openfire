"""Cloud Run service for the SoCal static UI and private GCS data proxy."""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from google.cloud import storage


STATIC_ROOT = Path(os.getenv("OPENFIRE_SOCAL_STATIC_ROOT", "/app/frontend-socal")).resolve()
GCS_BUCKET = os.getenv("OPENFIRE_SOCAL_GCS_BUCKET", "openfire")
GCS_PREFIX = os.getenv("OPENFIRE_SOCAL_GCS_PREFIX", "predictions").strip("/")

app = FastAPI(title="OpenFire SoCal UI", docs_url=None, redoc_url=None)
_storage_client: storage.Client | None = None


def _client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _static_path(path: str) -> Path:
    requested = (STATIC_ROOT / (path or "index.html")).resolve()
    if STATIC_ROOT not in requested.parents and requested != STATIC_ROOT:
        raise HTTPException(status_code=404)
    if requested.is_dir():
        requested = requested / "index.html"
    if not requested.exists() or not requested.is_file():
        requested = STATIC_ROOT / "index.html"
    return requested


@app.get("/_health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/data/{object_name:path}", response_model=None)
def data_proxy(object_name: str):
    if not object_name or "/" in object_name or object_name.startswith("."):
        raise HTTPException(status_code=404)

    static_asset = (STATIC_ROOT / "data" / object_name).resolve()
    if (
        static_asset.exists()
        and static_asset.is_file()
        and static_asset.parent == (STATIC_ROOT / "data").resolve()
        and object_name != "manifest.json"
    ):
        return FileResponse(static_asset)

    blob_name = f"{GCS_PREFIX}/{object_name}" if GCS_PREFIX else object_name
    blob = _client().bucket(GCS_BUCKET).blob(blob_name)
    if not blob.exists():
        raise HTTPException(status_code=404)

    payload = blob.download_as_bytes()
    content_type = blob.content_type or mimetypes.guess_type(object_name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "public, max-age=300"}
    return Response(content=payload, media_type=content_type, headers=headers)


@app.get("/{path:path}")
def static(path: str = "") -> FileResponse:
    response = FileResponse(_static_path(path))
    response.headers["Cache-Control"] = "no-cache"
    return response
