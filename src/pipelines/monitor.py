"""Per-window drift monitoring using Evidently OSS (pinned to 0.4.x).

Compares the current inference window's gold features against a pre-built
training reference dataset. Generates per-window HTML reports and JSON
snapshots stored in GCS under gs://openfire/monitoring/.

Reference dataset:
    gs://openfire/monitoring/reference/gold_features_train_2017_2023.parquet
    Built once by scripts/build_monitoring_reference.py. ~500k rows sampled
    from the 2017–2023 training distribution, stratified by (year, label).
    Does NOT include risk_probability — prediction drift is computed here
    on-the-fly so the reference stays aligned with the deployed model without
    rebuilding the artifact on every promotion.

Per-window outputs:
    gs://openfire/monitoring/reports/report_YYYYMMDD.html   (for humans)
    gs://openfire/monitoring/snapshots/snapshot_YYYYMMDD.json  (for the dashboard)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from typing import Any

import pandas as pd

from src.common.storage import StorageClient
from src.pipelines.train import FEATURE_COLUMNS, TARGET_COLUMN


LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
INFERENCE_GOLD_TABLE = f"{PROJECT}.{DATASET}.gold_features_inference"

DEFAULT_REFERENCE_URI = (
    "gs://openfire/monitoring/reference/gold_features_train_2017_2023.parquet"
)
DEFAULT_MONITORING_PREFIX = "gs://openfire/monitoring"
PREDICTION_COLUMN = "risk_probability"

# DataDriftPreset flags dataset_drift=True when this share of features drift.
# Evidently default is 0.5; we lower it to catch subtler covariate shift.
DRIFT_THRESHOLD = 0.3


# ── URI helpers ───────────────────────────────────────────────────────────────

def report_uri(target_date: date, *, monitoring_prefix: str = DEFAULT_MONITORING_PREFIX) -> str:
    return f"{monitoring_prefix.rstrip('/')}/reports/report_{target_date.strftime('%Y%m%d')}.html"


def snapshot_uri(
    target_date: date, *, monitoring_prefix: str = DEFAULT_MONITORING_PREFIX
) -> str:
    return f"{monitoring_prefix.rstrip('/')}/snapshots/snapshot_{target_date.strftime('%Y%m%d')}.json"


def index_uri(*, monitoring_prefix: str = DEFAULT_MONITORING_PREFIX) -> str:
    return f"{monitoring_prefix.rstrip('/')}/index.json"


# ── core functions ────────────────────────────────────────────────────────────

def load_reference(
    storage: StorageClient,
    *,
    reference_uri: str = DEFAULT_REFERENCE_URI,
) -> pd.DataFrame:
    """Load the pre-built training reference dataset from GCS."""
    LOGGER.info("Loading monitoring reference from %s", reference_uri)
    return storage.read_parquet(reference_uri)


def _score_features(df: pd.DataFrame, loaded_model: Any) -> pd.Series:
    """Return risk_probability Series for each row in df using the loaded bundle."""
    x = df[loaded_model.feature_columns]
    proba = loaded_model.model.predict_proba(x)[:, 1]
    return pd.Series(proba, index=df.index, dtype="float64")


def build_drift_report(
    reference: pd.DataFrame,
    current: pd.DataFrame,
    *,
    feature_columns: list[str],
) -> tuple[str, dict[str, Any]]:
    """Build an Evidently data drift report over feature + prediction columns.

    Both DataFrames must already contain PREDICTION_COLUMN (risk_probability).
    Returns (html_string, summary_dict).

    We treat risk_probability as just another numerical column so a single
    DataDriftPreset covers both feature drift and prediction drift. This avoids
    needing a separate PredictionDriftPreset and keeps the Evidently import
    surface minimal.
    """
    from evidently.legacy.metric_preset import DataDriftPreset
    from evidently.legacy.pipeline.column_mapping import ColumnMapping
    from evidently.legacy.report import Report

    monitored_cols = feature_columns + [PREDICTION_COLUMN]
    col_map = ColumnMapping(numerical_features=monitored_cols)

    # Cast to float64 explicitly — days_since_last_burn is int64 from BQ, and
    # mixed int/float dtypes trigger a numpy 2.x AttributeError in evidently's
    # corrcoef path (scl returns as Python float, not ndarray).
    ref_data = reference[monitored_cols].astype("float64")
    cur_data = current[monitored_cols].astype("float64")

    report = Report(
        metrics=[DataDriftPreset(drift_share=DRIFT_THRESHOLD)],
    )
    report.run(
        reference_data=ref_data,
        current_data=cur_data,
        column_mapping=col_map,
    )

    html = _get_report_html(report)
    result = json.loads(report.json())
    summary = _extract_summary(result, n_features=len(monitored_cols))
    return html, summary


def _get_report_html(report: Any) -> str:
    """Extract the HTML string from a Report object using a temp file."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".html")
        os.close(fd)
        report.save_html(tmp)
        with open(tmp, encoding="utf-8") as fh:
            return fh.read()
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _extract_summary(result: dict[str, Any], *, n_features: int) -> dict[str, Any]:
    """Pull the key drift metrics out of Evidently's JSON result.

    Compatible with both the 0.4.x layout (DatasetDriftMetric carries
    drift_by_columns) and the 0.5.x layout (DatasetDriftMetric carries
    counts only; DataDriftTable carries drift_by_columns).
    """
    metrics = result.get("metrics", [])
    dataset_metric = next(
        (m for m in metrics if "DatasetDrift" in m.get("metric", "")),
        {},
    )
    dr = dataset_metric.get("result", {})

    n_drifted = dr.get("number_of_drifted_columns", 0)
    share = dr.get("share_of_drifted_columns", 0.0)
    dataset_drift = dr.get("dataset_drift", False)

    # 0.4.x: drift_by_columns is on DatasetDriftMetric.
    # 0.5.x+: drift_by_columns moved to DataDriftTable.
    per_col = dr.get("drift_by_columns") or {}
    if not per_col:
        table_metric = next(
            (m for m in metrics if "DataDriftTable" in m.get("metric", "")),
            {},
        )
        per_col = table_metric.get("result", {}).get("drift_by_columns") or {}

    drifted = sorted(
        [
            {"feature": col, "drift_score": float(info.get("drift_score", 0.0))}
            for col, info in per_col.items()
            if info.get("drift_detected", False)
        ],
        key=lambda x: x["drift_score"],
        reverse=True,
    )[:3]

    if dataset_drift:
        status = "red"
    elif share > 0.1:
        status = "yellow"
    else:
        status = "green"

    return {
        "dataset_drift": bool(dataset_drift),
        "n_drifted_features": int(n_drifted),
        "n_total_features": int(n_features),
        "share_drifted": float(share),
        "top_drifted_features": drifted,
        "drift_status": status,
    }


# ── orchestrator-facing entry point ──────────────────────────────────────────

def run_window_monitoring(
    target_date: date,
    *,
    bq_client: Any,
    loaded_model: Any,
    storage: StorageClient,
    reference_uri: str = DEFAULT_REFERENCE_URI,
    monitoring_prefix: str = DEFAULT_MONITORING_PREFIX,
    feature_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Run drift monitoring for one inference window.

    Loads the reference parquet and the current window's gold features from BQ,
    scores both with the Production bundle, builds an Evidently report, writes
    HTML + snapshot JSON to GCS, and returns a metadata dict suitable for
    update_monitoring_index.

    Idempotent: re-running the same window overwrites that window's report.
    """
    from google.cloud import bigquery

    feat_cols = feature_columns or FEATURE_COLUMNS

    reference = load_reference(storage, reference_uri=reference_uri)

    sql = f"""
        SELECT *
        FROM `{INFERENCE_GOLD_TABLE}`
        WHERE window_start_date = @target_date
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_date", "DATE", target_date)
        ]
    )
    current = bq_client.query(sql, job_config=job_config).to_dataframe()
    LOGGER.info(
        "Loaded %s current gold rows for monitoring (window=%s)",
        f"{len(current):,}",
        target_date.isoformat(),
    )

    if current.empty:
        LOGGER.warning(
            "No gold features found for window %s; skipping monitoring.",
            target_date.isoformat(),
        )
        return {"skipped": True, "reason": "no_current_features", "window_start_date": target_date.isoformat()}

    ref = reference.copy()
    ref[PREDICTION_COLUMN] = _score_features(ref, loaded_model)

    cur = current.copy()
    cur[PREDICTION_COLUMN] = _score_features(cur, loaded_model)

    html, summary = build_drift_report(ref, cur, feature_columns=feat_cols)

    html_out = report_uri(target_date, monitoring_prefix=monitoring_prefix)
    snap_out = snapshot_uri(target_date, monitoring_prefix=monitoring_prefix)

    storage.write_text(html_out, html, content_type="text/html")
    LOGGER.info("Wrote monitoring report to %s", html_out)

    metadata: dict[str, Any] = {
        "window_start_date": target_date.isoformat(),
        "model_version": str(loaded_model.model_version),
        "reference_uri": reference_uri,
        "reference_rows": int(len(ref)),
        "current_rows": int(len(cur)),
        "report_uri": html_out,
        "snapshot_uri": snap_out,
        **summary,
    }
    storage.write_json(snap_out, metadata)
    LOGGER.info("Wrote monitoring snapshot to %s", snap_out)

    if summary.get("dataset_drift"):
        LOGGER.error(
            "DRIFT_DETECTED window=%s n_drifted=%s/%s top_features=%s",
            target_date.isoformat(),
            summary["n_drifted_features"],
            summary["n_total_features"],
            [f["feature"] for f in summary["top_drifted_features"]],
        )
    else:
        LOGGER.info(
            "No dataset drift for window=%s (%s/%s features drifted, status=%s)",
            target_date.isoformat(),
            summary["n_drifted_features"],
            summary["n_total_features"],
            summary["drift_status"],
        )

    return metadata


__all__ = [
    "DEFAULT_REFERENCE_URI",
    "DEFAULT_MONITORING_PREFIX",
    "PREDICTION_COLUMN",
    "report_uri",
    "snapshot_uri",
    "index_uri",
    "load_reference",
    "build_drift_report",
    "run_window_monitoring",
]
