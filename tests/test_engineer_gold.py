"""Tests for src/pipelines/engineer_gold.py and the inference SQL files.

Unit tests use a MagicMock BigQuery client — no live BQ required. Plus a
static guardrail that the inference SQL files don't contain destructive
statements (Phase 4 spec §10 / Tests).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipelines.engineer_gold import (
    DDL_FILE,
    INFERENCE_GOLD_TABLE,
    MERGE_FILE,
    engineer_inference_gold,
    ensure_inference_tables,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PIPELINES = REPO_ROOT / "data_pipelines"


# ── module surface ───────────────────────────────────────────────────────────

def test_inference_gold_table_constant() -> None:
    assert INFERENCE_GOLD_TABLE == (
        "msds603-mlops-project.openfire_features.gold_features_inference"
    )


def test_sql_files_exist() -> None:
    assert DDL_FILE.exists(), f"missing DDL file: {DDL_FILE}"
    assert MERGE_FILE.exists(), f"missing MERGE file: {MERGE_FILE}"


# ── ensure_inference_tables ──────────────────────────────────────────────────

def test_ensure_inference_tables_runs_ddl() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(job_id="job-1")
    ensure_inference_tables(client)
    client.query.assert_called_once()
    sql = client.query.call_args.args[0]
    # DDL only — no DML, no destructive verbs.
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
    assert "CREATE OR REPLACE TABLE" not in sql.upper()


# ── engineer_inference_gold ──────────────────────────────────────────────────

def test_engineer_inference_gold_binds_target_date_parameter() -> None:
    client = MagicMock()
    fake_job = MagicMock(num_dml_affected_rows=1234, total_bytes_billed=42)
    client.query.return_value = fake_job
    affected = engineer_inference_gold(client, target_date=date(2026, 4, 23))
    assert affected == 1234

    # The SQL was sent and a target_date parameter was bound.
    client.query.assert_called_once()
    job_config = client.query.call_args.kwargs["job_config"]
    params = {p.name: p for p in job_config.query_parameters}
    assert "target_date" in params
    assert params["target_date"].type_ == "DATE"
    assert params["target_date"].value == date(2026, 4, 23)


def test_engineer_inference_gold_uses_merge_into_inference_table() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(num_dml_affected_rows=0, total_bytes_billed=0)
    engineer_inference_gold(client, target_date=date(2025, 1, 2))
    sql = client.query.call_args.args[0]
    # MERGE keyed on the inference table — never touches gold_features (training).
    assert "MERGE `msds603-mlops-project.openfire_features.gold_features_inference`" in sql
    # Reads from silver_features_* (training silver) but in a SELECT, not DML.
    assert "FROM `msds603-mlops-project.openfire_features.silver_features_*`" in sql


def test_inference_sql_normalizes_coordinate_keys_for_lag_windows() -> None:
    sql = MERGE_FILE.read_text()
    assert 'FORMAT("%.6f", s.latitude)  AS lat_key' in sql
    assert 'FORMAT("%.6f", s.longitude) AS lon_key' in sql
    assert "PARTITION BY lat_key, lon_key, window_start_date" in sql
    assert "PARTITION BY lat_key, lon_key" in sql


def test_normalized_coordinate_keys_preserve_60d_lag_under_float_jitter() -> None:
    dates = pd.date_range("2025-10-29", periods=13, freq="5D").date
    rows = []
    for i, d in enumerate(dates):
        # Same visible centroid at 6 decimals, but exact float strings drift
        # across extraction runs.
        lat = 34.1234561 if i < 12 else 34.1234562
        lon = -118.6543211 if i < 12 else -118.6543212
        rows.append({
            "window_start_date": d,
            "latitude": lat,
            "longitude": lon,
            "mean_NDVI": 0.1 * (i + 1),
        })
    df = pd.DataFrame(rows)

    exact = df.sort_values("window_start_date").copy()
    exact["cell_key"] = exact["latitude"].astype(str) + "," + exact["longitude"].astype(str)
    exact["ndvi_change_60d"] = (
        exact["mean_NDVI"] - exact.groupby("cell_key")["mean_NDVI"].shift(12)
    )

    normalized = df.sort_values("window_start_date").copy()
    normalized["cell_key"] = (
        normalized["latitude"].map(lambda x: f"{x:.6f}")
        + ","
        + normalized["longitude"].map(lambda x: f"{x:.6f}")
    )
    normalized["ndvi_change_60d"] = (
        normalized["mean_NDVI"] - normalized.groupby("cell_key")["mean_NDVI"].shift(12)
    )

    assert pd.isna(exact.loc[exact.index[-1], "ndvi_change_60d"])
    assert normalized.loc[normalized.index[-1], "ndvi_change_60d"] == pytest.approx(1.2)


def test_engineer_inference_gold_handles_zero_affected_rows() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(num_dml_affected_rows=None, total_bytes_billed=None)
    affected = engineer_inference_gold(client, target_date=date(2025, 1, 2))
    assert affected == 0


# ── no-destructive-SQL guardrail ─────────────────────────────────────────────

INFERENCE_SQL_GLOB = "07_*.sql"

FORBIDDEN_INFERENCE_PATTERNS = [
    "DROP TABLE",
    "DELETE FROM",
    "TRUNCATE TABLE",
    "CREATE OR REPLACE TABLE",
]


def _inference_sql_files() -> list[Path]:
    files = sorted(DATA_PIPELINES.glob(INFERENCE_SQL_GLOB))
    assert files, (
        f"expected at least one inference SQL file matching {INFERENCE_SQL_GLOB} "
        f"in {DATA_PIPELINES}"
    )
    return files


@pytest.mark.parametrize("path", _inference_sql_files(), ids=lambda p: p.name)
def test_inference_sql_has_no_destructive_statements(path: Path) -> None:
    sql_upper = path.read_text().upper()
    for pattern in FORBIDDEN_INFERENCE_PATTERNS:
        assert pattern not in sql_upper, (
            f"{path.name} contains forbidden statement '{pattern}'. Inference "
            "SQL must only CREATE TABLE IF NOT EXISTS or MERGE — never DROP, "
            "DELETE, TRUNCATE, or CREATE OR REPLACE against any table."
        )


def test_inference_sql_does_not_write_to_training_gold() -> None:
    """Belt-and-suspenders: no inference SQL should MERGE/INSERT/UPDATE
    `gold_features` (the training table). MERGE INTO `gold_features_inference`
    is fine; we just require the `_inference` suffix in any DML target."""
    bad_targets = [
        "MERGE `msds603-mlops-project.openfire_features.gold_features` ",
        "INSERT INTO `msds603-mlops-project.openfire_features.gold_features` ",
        "UPDATE `msds603-mlops-project.openfire_features.gold_features` ",
    ]
    for path in _inference_sql_files():
        text = path.read_text()
        for marker in bad_targets:
            assert marker not in text, (
                f"{path.name} writes to the training gold_features table "
                f"({marker.strip()}). Inference must use gold_features_inference."
            )
