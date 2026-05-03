"""Cloud Run service for the SoCal static UI and private GCS data proxy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import mimetypes
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage
from pydantic import BaseModel, ConfigDict, Field


STATIC_ROOT = Path(os.getenv("OPENFIRE_SOCAL_STATIC_ROOT", "/app/frontend-socal")).resolve()
GCS_BUCKET = os.getenv("OPENFIRE_SOCAL_GCS_BUCKET", "openfire")
GCS_PREFIX = os.getenv("OPENFIRE_SOCAL_GCS_PREFIX", "predictions").strip("/")
UI_PERF_ENABLED = os.getenv("OPENFIRE_UI_PERF_ENABLED", "true").lower() in {"1", "true", "yes"}
UI_PERF_BQ_PROJECT = os.getenv("OPENFIRE_UI_PERF_BQ_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
UI_PERF_BQ_DATASET = os.getenv("OPENFIRE_UI_PERF_BQ_DATASET", "openfire_features")
UI_PERF_BQ_TABLE = os.getenv("OPENFIRE_UI_PERF_BQ_TABLE", "ui_performance_events")
UI_PERF_MAX_EVENTS = int(os.getenv("OPENFIRE_UI_PERF_MAX_EVENTS", "50"))
UI_VARIANT = os.getenv("OPENFIRE_UI_VARIANT", "leaflet-canvas")
UI_VERSION = os.getenv("OPENFIRE_UI_VERSION", "socal-ui")
WEB_VITALS_ENABLED = os.getenv("OPENFIRE_WEB_VITALS_ENABLED", "true").lower() in {"1", "true", "yes"}
WEB_VITALS_SCRIPT_URL = os.getenv(
    "OPENFIRE_WEB_VITALS_SCRIPT_URL",
    "https://unpkg.com/web-vitals@5/dist/web-vitals.iife.js",
)
FARO_COLLECTOR_URL = os.getenv("OPENFIRE_FARO_COLLECTOR_URL", "")
FARO_ENABLED = os.getenv("OPENFIRE_FARO_ENABLED", "true").lower() in {"1", "true", "yes"}
FARO_SCRIPT_URL = os.getenv(
    "OPENFIRE_FARO_SCRIPT_URL",
    "https://unpkg.com/@grafana/faro-web-sdk@^1.0.0/dist/bundle/faro-web-sdk.iife.js",
)
FARO_APP_NAME = os.getenv("OPENFIRE_FARO_APP_NAME", "openfire-ui-socal")
FARO_APP_NAMESPACE = os.getenv("OPENFIRE_FARO_APP_NAMESPACE", "openfire")
FARO_ENVIRONMENT = os.getenv("OPENFIRE_FARO_ENVIRONMENT", "production")

app = FastAPI(title="OpenFire SoCal UI", docs_url=None, redoc_url=None)
_storage_client: storage.Client | None = None
_bq_client: bigquery.Client | None = None
_ui_perf_table_ready = False


class UIPerformanceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = Field(min_length=1, max_length=64)
    timestamp: str | None = Field(default=None, max_length=64)
    window_start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    zoom: float | None = Field(default=None, ge=0, le=24)
    source_label: str | None = Field(default=None, max_length=128)
    source_url: str | None = Field(default=None, max_length=2048)
    display_mode: str | None = Field(default=None, max_length=256)
    cache_hit: bool | None = None
    total_feature_count: int | None = Field(default=None, ge=0)
    rendered_feature_count: int | None = Field(default=None, ge=0)
    snapshot_load_ms: float | None = Field(default=None, ge=0)
    render_sync_ms: float | None = Field(default=None, ge=0)
    select_ms: float | None = Field(default=None, ge=0)
    layer_swap_ms: float | None = Field(default=None, ge=0)
    paint_ready_ms: float | None = Field(default=None, ge=0)
    frame_wait_ms: float | None = Field(default=None, ge=0)
    long_task_count: int | None = Field(default=None, ge=0)
    long_task_total_ms: float | None = Field(default=None, ge=0)
    web_vital_name: str | None = Field(default=None, max_length=16)
    web_vital_id: str | None = Field(default=None, max_length=128)
    web_vital_value: float | None = Field(default=None, ge=0)
    web_vital_delta: float | None = Field(default=None)
    web_vital_rating: str | None = Field(default=None, max_length=32)
    web_vital_navigation_type: str | None = Field(default=None, max_length=64)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=512)
    error_source: str | None = Field(default=None, max_length=2048)
    error_line: int | None = Field(default=None, ge=0)
    error_column: int | None = Field(default=None, ge=0)
    error_stack_hash: str | None = Field(default=None, max_length=64)


class UIPerformancePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(default="ui_performance_v1", max_length=64)
    session_id: str = Field(min_length=8, max_length=128)
    ui_variant: str = Field(default="unknown", max_length=128)
    app_version: str = Field(default="unknown", max_length=128)
    page_path: str | None = Field(default=None, max_length=2048)
    viewport_width: int | None = Field(default=None, ge=0, le=20000)
    viewport_height: int | None = Field(default=None, ge=0, le=20000)
    device_pixel_ratio: float | None = Field(default=None, ge=0, le=10)
    hardware_concurrency: int | None = Field(default=None, ge=0, le=512)
    device_memory_gb: float | None = Field(default=None, ge=0, le=1024)
    connection_effective_type: str | None = Field(default=None, max_length=32)
    save_data: bool | None = None
    events: list[UIPerformanceEvent] = Field(default_factory=list, max_length=UI_PERF_MAX_EVENTS)


def _client() -> storage.Client:
    global _storage_client
    if _storage_client is None:
        _storage_client = storage.Client()
    return _storage_client


def _bigquery_client() -> bigquery.Client:
    global _bq_client
    if _bq_client is None:
        _bq_client = bigquery.Client(project=UI_PERF_BQ_PROJECT)
    return _bq_client


def _ui_perf_table_id(client: bigquery.Client) -> str:
    project = UI_PERF_BQ_PROJECT or client.project
    return f"{project}.{UI_PERF_BQ_DATASET}.{UI_PERF_BQ_TABLE}"


def _ui_perf_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("received_at", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("schema_version", "STRING"),
        bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("ui_variant", "STRING"),
        bigquery.SchemaField("app_version", "STRING"),
        bigquery.SchemaField("page_path", "STRING"),
        bigquery.SchemaField("viewport_width", "INT64"),
        bigquery.SchemaField("viewport_height", "INT64"),
        bigquery.SchemaField("device_pixel_ratio", "FLOAT64"),
        bigquery.SchemaField("hardware_concurrency", "INT64"),
        bigquery.SchemaField("device_memory_gb", "FLOAT64"),
        bigquery.SchemaField("connection_effective_type", "STRING"),
        bigquery.SchemaField("save_data", "BOOL"),
        bigquery.SchemaField("event_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("client_event_at", "TIMESTAMP"),
        bigquery.SchemaField("window_start_date", "DATE"),
        bigquery.SchemaField("zoom", "FLOAT64"),
        bigquery.SchemaField("render_mode", "STRING"),
        bigquery.SchemaField("display_mode", "STRING"),
        bigquery.SchemaField("source_label", "STRING"),
        bigquery.SchemaField("source_url", "STRING"),
        bigquery.SchemaField("cache_hit", "BOOL"),
        bigquery.SchemaField("total_feature_count", "INT64"),
        bigquery.SchemaField("rendered_feature_count", "INT64"),
        bigquery.SchemaField("snapshot_load_ms", "FLOAT64"),
        bigquery.SchemaField("render_sync_ms", "FLOAT64"),
        bigquery.SchemaField("select_ms", "FLOAT64"),
        bigquery.SchemaField("layer_swap_ms", "FLOAT64"),
        bigquery.SchemaField("paint_ready_ms", "FLOAT64"),
        bigquery.SchemaField("frame_wait_ms", "FLOAT64"),
        bigquery.SchemaField("long_task_count", "INT64"),
        bigquery.SchemaField("long_task_total_ms", "FLOAT64"),
        bigquery.SchemaField("web_vital_name", "STRING"),
        bigquery.SchemaField("web_vital_id", "STRING"),
        bigquery.SchemaField("web_vital_value", "FLOAT64"),
        bigquery.SchemaField("web_vital_delta", "FLOAT64"),
        bigquery.SchemaField("web_vital_rating", "STRING"),
        bigquery.SchemaField("web_vital_navigation_type", "STRING"),
        bigquery.SchemaField("error_type", "STRING"),
        bigquery.SchemaField("error_message", "STRING"),
        bigquery.SchemaField("error_source", "STRING"),
        bigquery.SchemaField("error_line", "INT64"),
        bigquery.SchemaField("error_column", "INT64"),
        bigquery.SchemaField("error_stack_hash", "STRING"),
    ]


def _ensure_ui_perf_table(client: bigquery.Client) -> str:
    global _ui_perf_table_ready
    table_id = _ui_perf_table_id(client)
    if _ui_perf_table_ready:
        return table_id

    schema = _ui_perf_schema()
    try:
        table = client.get_table(table_id)
    except NotFound:
        table = bigquery.Table(table_id, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(field="received_at")
        table.clustering_fields = ["ui_variant", "event_name", "window_start_date", "render_mode"]
        client.create_table(table, exists_ok=True)
    else:
        existing_fields = {field.name for field in table.schema}
        missing_fields = [field for field in schema if field.name not in existing_fields]
        if missing_fields:
            table.schema = list(table.schema) + missing_fields
            client.update_table(table, ["schema"])
    _ui_perf_table_ready = True
    return table_id


def _finite_float(value: float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _finite_int(value: int | None) -> int | None:
    if value is None:
        return None
    numeric = int(value)
    return numeric if numeric >= 0 else None


def _timestamp_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _ui_perf_row(payload: UIPerformancePayload, event: UIPerformanceEvent) -> dict[str, object | None]:
    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": payload.schema_version,
        "session_id": payload.session_id,
        "ui_variant": payload.ui_variant,
        "app_version": payload.app_version,
        "page_path": payload.page_path,
        "viewport_width": _finite_int(payload.viewport_width),
        "viewport_height": _finite_int(payload.viewport_height),
        "device_pixel_ratio": _finite_float(payload.device_pixel_ratio),
        "hardware_concurrency": _finite_int(payload.hardware_concurrency),
        "device_memory_gb": _finite_float(payload.device_memory_gb),
        "connection_effective_type": payload.connection_effective_type,
        "save_data": payload.save_data,
        "event_name": event.action,
        "client_event_at": _timestamp_or_none(event.timestamp),
        "window_start_date": event.window_start_date,
        "zoom": _finite_float(event.zoom),
        "render_mode": event.source_label,
        "display_mode": event.display_mode,
        "source_label": event.source_label,
        "source_url": event.source_url,
        "cache_hit": event.cache_hit,
        "total_feature_count": _finite_int(event.total_feature_count),
        "rendered_feature_count": _finite_int(event.rendered_feature_count),
        "snapshot_load_ms": _finite_float(event.snapshot_load_ms),
        "render_sync_ms": _finite_float(event.render_sync_ms),
        "select_ms": _finite_float(event.select_ms),
        "layer_swap_ms": _finite_float(event.layer_swap_ms),
        "paint_ready_ms": _finite_float(event.paint_ready_ms),
        "frame_wait_ms": _finite_float(event.frame_wait_ms),
        "long_task_count": _finite_int(event.long_task_count),
        "long_task_total_ms": _finite_float(event.long_task_total_ms),
        "web_vital_name": event.web_vital_name,
        "web_vital_id": event.web_vital_id,
        "web_vital_value": _finite_float(event.web_vital_value),
        "web_vital_delta": _finite_float(event.web_vital_delta),
        "web_vital_rating": event.web_vital_rating,
        "web_vital_navigation_type": event.web_vital_navigation_type,
        "error_type": event.error_type,
        "error_message": event.error_message,
        "error_source": event.error_source,
        "error_line": _finite_int(event.error_line),
        "error_column": _finite_int(event.error_column),
        "error_stack_hash": event.error_stack_hash,
    }


def _ui_perf_summary_rows(lookback_hours: int, limit: int) -> list[dict[str, object | None]]:
    client = _bigquery_client()
    table_id = _ensure_ui_perf_table(client)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    query = f"""
        SELECT
          ui_variant,
          event_name,
          render_mode,
          COUNT(*) AS sample_count,
          ROUND(AVG(paint_ready_ms), 1) AS avg_paint_ms,
          ROUND(APPROX_QUANTILES(paint_ready_ms, 100)[SAFE_OFFSET(50)], 1) AS p50_paint_ms,
          ROUND(APPROX_QUANTILES(paint_ready_ms, 100)[SAFE_OFFSET(95)], 1) AS p95_paint_ms,
          ROUND(APPROX_QUANTILES(snapshot_load_ms, 100)[SAFE_OFFSET(95)], 1) AS p95_snapshot_load_ms,
          ROUND(APPROX_QUANTILES(render_sync_ms, 100)[SAFE_OFFSET(95)], 1) AS p95_render_ms,
          ROUND(APPROX_QUANTILES(frame_wait_ms, 100)[SAFE_OFFSET(95)], 1) AS p95_frame_wait_ms,
          ROUND(AVG(rendered_feature_count), 0) AS avg_rendered_features,
          MAX(received_at) AS latest_received_at
        FROM `{table_id}`
        WHERE received_at >= @cutoff
        GROUP BY ui_variant, event_name, render_mode
        ORDER BY p95_paint_ms DESC, sample_count DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]
    )
    return [dict(row.items()) for row in client.query(query, job_config=job_config).result()]


def _format_metric(value: object | None, suffix: str = " ms") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.1f}{suffix}"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return f"{value}{suffix}"


def _ui_perf_dashboard_html(rows: list[dict[str, object | None]], lookback_hours: int) -> str:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    table_rows = "\n".join(
        "<tr>"
        f"<td>{escape(str(row.get('ui_variant') or 'unknown'))}</td>"
        f"<td>{escape(str(row.get('event_name') or 'unknown'))}</td>"
        f"<td>{escape(str(row.get('render_mode') or 'unknown'))}</td>"
        f"<td>{int(row.get('sample_count') or 0):,}</td>"
        f"<td>{escape(_format_metric(row.get('p95_paint_ms')))}</td>"
        f"<td>{escape(_format_metric(row.get('p95_snapshot_load_ms')))}</td>"
        f"<td>{escape(_format_metric(row.get('p95_render_ms')))}</td>"
        f"<td>{escape(_format_metric(row.get('p95_frame_wait_ms')))}</td>"
        f"<td>{escape(_format_metric(row.get('avg_rendered_features'), ' pts'))}</td>"
        "</tr>"
        for row in rows
    )
    if not table_rows:
        table_rows = '<tr><td colspan="9">No UI performance samples in this window.</td></tr>'

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OpenFire UI Performance</title>
    <style>
      body {{ margin: 0; font-family: Avenir Next, Segoe UI, sans-serif; color: #1f2c27; background: #f4f2ee; }}
      main {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
      h1 {{ margin: 0 0 6px; font-size: 28px; }}
      p {{ margin: 0 0 22px; color: #5c6762; }}
      table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid rgba(31, 44, 39, 0.16); }}
      th, td {{ padding: 10px 12px; border-bottom: 1px solid rgba(31, 44, 39, 0.12); text-align: right; font-size: 14px; }}
      th {{ background: #24392f; color: #fff; font-weight: 700; position: sticky; top: 0; }}
      th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:nth-child(3), td:nth-child(3) {{ text-align: left; }}
      tr:nth-child(even) td {{ background: #faf9f6; }}
      .meta {{ display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 16px; font-size: 14px; color: #5c6762; }}
    </style>
  </head>
  <body>
    <main>
      <h1>OpenFire UI Performance</h1>
      <div class="meta">
        <span>Lookback: {lookback_hours} hours</span>
        <span>Generated: {generated_at}</span>
        <span>Grouped by UI variant, event, and render mode</span>
      </div>
      <table>
        <thead>
          <tr>
            <th>UI variant</th>
            <th>Event</th>
            <th>Render mode</th>
            <th>Samples</th>
            <th>P95 paint</th>
            <th>P95 load</th>
            <th>P95 render</th>
            <th>P95 frame wait</th>
            <th>Avg points</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </main>
  </body>
</html>"""


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


@app.get("/runtime-config.js")
def runtime_config() -> Response:
    runtime_values = {
        "OPENFIRE_UI_VARIANT": UI_VARIANT,
        "OPENFIRE_UI_VERSION": UI_VERSION,
        "OPENFIRE_WEB_VITALS_ENABLED": WEB_VITALS_ENABLED,
        "OPENFIRE_WEB_VITALS_SCRIPT_URL": WEB_VITALS_SCRIPT_URL,
        "OPENFIRE_FARO_ENABLED": FARO_ENABLED and bool(FARO_COLLECTOR_URL),
        "OPENFIRE_FARO_COLLECTOR_URL": FARO_COLLECTOR_URL if FARO_ENABLED else "",
        "OPENFIRE_FARO_SCRIPT_URL": FARO_SCRIPT_URL,
        "OPENFIRE_FARO_APP_NAME": FARO_APP_NAME,
        "OPENFIRE_FARO_APP_VERSION": UI_VERSION,
        "OPENFIRE_FARO_APP_NAMESPACE": FARO_APP_NAMESPACE,
        "OPENFIRE_FARO_ENVIRONMENT": FARO_ENVIRONMENT,
    }
    body = (
        "window.OPENFIRE_RUNTIME_CONFIG = "
        f"{json.dumps(runtime_values, separators=(',', ':'))};\n"
        "Object.assign(window, window.OPENFIRE_RUNTIME_CONFIG);\n"
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/metrics/ui/performance")
def collect_ui_performance(payload: UIPerformancePayload) -> dict[str, object]:
    if not UI_PERF_ENABLED:
        return {"status": "disabled", "accepted": 0}
    if not payload.events:
        return {"status": "ok", "accepted": 0}

    rows = [_ui_perf_row(payload, event) for event in payload.events]
    client = _bigquery_client()
    table_id = _ensure_ui_perf_table(client)
    errors = client.insert_rows_json(table_id, rows)
    if errors:
        raise HTTPException(status_code=503, detail="Failed to persist UI performance metrics")
    return {"status": "ok", "accepted": len(rows)}


@app.get("/metrics/ui/performance/summary")
def ui_performance_summary(
    lookback_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, object]:
    if not UI_PERF_ENABLED:
        return {"status": "disabled", "lookback_hours": lookback_hours, "rows": []}
    rows = _ui_perf_summary_rows(lookback_hours=lookback_hours, limit=limit)
    return {
        "status": "ok",
        "lookback_hours": lookback_hours,
        "rows": rows,
    }


@app.get("/metrics/ui/dashboard", response_class=HTMLResponse)
def ui_performance_dashboard(
    lookback_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=50, ge=1, le=500),
) -> HTMLResponse:
    if not UI_PERF_ENABLED:
        return HTMLResponse(_ui_perf_dashboard_html([], lookback_hours=lookback_hours))
    rows = _ui_perf_summary_rows(lookback_hours=lookback_hours, limit=limit)
    return HTMLResponse(_ui_perf_dashboard_html(rows, lookback_hours=lookback_hours))


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

    content_encoding = getattr(blob, "content_encoding", None)
    try:
        payload = blob.download_as_bytes(raw_download=bool(content_encoding))
    except TypeError:  # test doubles and older clients may not accept raw_download
        payload = blob.download_as_bytes()
    content_type = blob.content_type or mimetypes.guess_type(object_name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "public, max-age=300"}
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    return Response(content=payload, media_type=content_type, headers=headers)


@app.get("/{path:path}")
def static(path: str = "") -> FileResponse:
    response = FileResponse(_static_path(path))
    response.headers["Cache-Control"] = "no-cache"
    return response
