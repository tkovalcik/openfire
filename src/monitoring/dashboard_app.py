"""Evidently monitoring dashboard — read-only FastAPI service.

Reads pre-computed Evidently reports and snapshots from GCS.
Does no computation — keeps the serving path fast and lets us redeploy
the dashboard without touching report history.

Routes:
    GET /                    → HTML index listing all windows, drift scores, links
    GET /reports/{date}      → stream the per-window HTML report from GCS
    GET /summary/latest      → JSON: latest window, drift status, top drifted features
    GET /healthz             → liveness probe (returns {"status": "ok"})
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from src.common.storage import StorageClient, StorageError
from src.pipelines.monitor import DEFAULT_MONITORING_PREFIX, index_uri, report_uri


GCP_PROJECT = os.environ.get("GCP_PROJECT", "msds603-mlops-project")
MONITORING_PREFIX = os.environ.get("MONITORING_PREFIX", DEFAULT_MONITORING_PREFIX)

app = FastAPI(title="OpenFire Monitoring Dashboard", docs_url=None, redoc_url=None)

_storage: StorageClient | None = None


def _get_storage() -> StorageClient:
    global _storage
    if _storage is None:
        _storage = StorageClient(gcp_project_id=GCP_PROJECT)
    return _storage


def _load_index() -> dict[str, Any]:
    storage = _get_storage()
    uri = index_uri(monitoring_prefix=MONITORING_PREFIX)
    try:
        if not storage.exists(uri):
            return {"windows": []}
        return storage.read_json(uri)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot read monitoring index: {exc}") from exc


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    data = _load_index()
    windows = data.get("windows", [])
    rows = ""
    for w in reversed(windows):  # newest first
        wd = w.get("window_start_date", "")
        status = w.get("drift_status", "unknown")
        n_drifted = w.get("n_drifted_features", "?")
        n_total = w.get("n_total_features", "?")
        model_ver = w.get("model_version", "")[:8]
        color = {"green": "#2e7d32", "yellow": "#f57f17", "red": "#c62828"}.get(status, "#555")
        rows += (
            f"<tr>"
            f"<td>{wd}</td>"
            f"<td style='color:{color};font-weight:bold'>{status.upper()}</td>"
            f"<td>{n_drifted}/{n_total}</td>"
            f"<td><code>{model_ver}</code></td>"
            f"<td><a href='/reports/{wd}'>View report</a></td>"
            f"</tr>\n"
        )
    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>OpenFire Monitoring</title>
  <style>
    body {{font-family: sans-serif; margin: 2rem; background: #fafafa;}}
    h1 {{color: #1a237e;}}
    table {{border-collapse: collapse; width: 100%;}}
    th,td {{border: 1px solid #ddd; padding: 8px 12px; text-align: left;}}
    th {{background: #e8eaf6;}}
    tr:hover {{background: #f5f5f5;}}
  </style>
</head>
<body>
  <h1>OpenFire Monitoring Dashboard</h1>
  <p>{len(windows)} inference window(s) tracked</p>
  <table>
    <thead>
      <tr><th>Window</th><th>Drift status</th><th>Drifted features</th><th>Model</th><th>Report</th></tr>
    </thead>
    <tbody>
      {rows if rows else "<tr><td colspan='5'>No windows tracked yet.</td></tr>"}
    </tbody>
  </table>
</body>
</html>"""
    return html


@app.get("/reports/{window_date}", response_class=HTMLResponse)
def get_report(window_date: str) -> str:
    try:
        target = date.fromisoformat(window_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {window_date!r}; use YYYY-MM-DD") from exc

    storage = _get_storage()
    uri = report_uri(target, monitoring_prefix=MONITORING_PREFIX)
    try:
        if not storage.exists(uri):
            raise HTTPException(status_code=404, detail=f"No report found for window {window_date}")
        return storage.read_text(uri)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=f"Cannot read report: {exc}") from exc


@app.get("/summary/latest")
def summary_latest() -> JSONResponse:
    data = _load_index()
    windows = data.get("windows", [])
    if not windows:
        raise HTTPException(status_code=404, detail="No monitoring windows available yet.")

    latest = windows[-1]  # index is sorted ascending; last = newest
    return JSONResponse({
        "window_start_date": latest.get("window_start_date"),
        "drift_status": latest.get("drift_status"),
        "dataset_drift": latest.get("dataset_drift"),
        "n_drifted_features": latest.get("n_drifted_features"),
        "n_total_features": latest.get("n_total_features"),
        "share_drifted": latest.get("share_drifted"),
        "top_drifted_features": latest.get("top_drifted_features", []),
        "model_version": latest.get("model_version"),
        "report_uri": latest.get("report_uri"),
    })
