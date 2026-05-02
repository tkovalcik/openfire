"""Tests for src/pipelines/output_writer.py.

Mocked-client tests covering the three sinks:
- write_predictions_to_bq: partition decorator, schema, WRITE_TRUNCATE, schema
  mismatch / wrong-window guards, idempotency.
- build_geojson_payload + write_geojson_snapshot: GeoJSON validity, coord
  precision capped, properties present, atomic single-PUT to GCS.
- update_manifest: payload schema, atomic single-write.
- predictions_history DDL guardrail: still no destructive statements after
  Step 6's additions.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from common.storage import StorageClient
from pipelines.output_writer import (
    MANIFEST_FILENAME,
    PREDICTION_COLUMNS,
    PREDICTIONS_TABLE,
    build_geojson_payload,
    manifest_uri,
    snapshot_uri,
    update_manifest,
    write_geojson_snapshot,
    write_predictions_to_bq,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ── helpers ──────────────────────────────────────────────────────────────────

def _sample_predictions(n: int = 3, *, target_date: date = date(2025, 1, 2)) -> pd.DataFrame:
    # First three rows have known high-precision coords/probs to exercise rounding;
    # rows beyond n=3 fall back to filler values.
    base_lats = [34.123456789, 34.234, 34.345]
    base_lons = [-119.123456789, -119.234, -119.345]
    base_probs = [0.123456789, 0.5, 0.987654321]
    base_labels = [0, 1, 1]
    lats = [base_lats[i] if i < 3 else 34.0 + 0.001 * i for i in range(n)]
    lons = [base_lons[i] if i < 3 else -119.0 - 0.001 * i for i in range(n)]
    probs = [base_probs[i] if i < 3 else 0.5 for i in range(n)]
    labels = [base_labels[i] if i < 3 else 1 for i in range(n)]
    return pd.DataFrame({
        "latitude":          lats,
        "longitude":         lons,
        "window_start_date": [target_date] * n,
        "risk_probability":  probs,
        "predicted_label":   labels,
        "model_name":        ["openfire-gold"] * n,
        "model_version":     ["7"] * n,
        "inference_run_at":  [datetime(2026, 4, 28, tzinfo=timezone.utc)] * n,
    })


def _local_storage(tmp_path: Path) -> StorageClient:
    return StorageClient(local_cache_dir=str(tmp_path / ".cache"))


# ── BigQuery write ───────────────────────────────────────────────────────────

def test_write_predictions_uses_partition_decorator() -> None:
    client = MagicMock()
    fake_job = MagicMock(job_id="load-1")
    client.load_table_from_dataframe.return_value = fake_job

    rows = write_predictions_to_bq(
        client, _sample_predictions(3), target_date=date(2025, 1, 2)
    )
    assert rows == 3

    args, kwargs = client.load_table_from_dataframe.call_args
    destination = args[1]
    assert destination == f"{PREDICTIONS_TABLE}$20250102"


def test_write_predictions_uses_write_truncate() -> None:
    from google.cloud import bigquery

    client = MagicMock()
    client.load_table_from_dataframe.return_value = MagicMock(job_id="x")
    write_predictions_to_bq(client, _sample_predictions(2), target_date=date(2025, 1, 2))

    job_config = client.load_table_from_dataframe.call_args.kwargs["job_config"]
    assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE


def test_write_predictions_load_payload_has_only_canonical_columns() -> None:
    client = MagicMock()
    client.load_table_from_dataframe.return_value = MagicMock(job_id="x")
    df = _sample_predictions(2)
    df["unrelated_extra_col"] = "ignored"
    write_predictions_to_bq(client, df, target_date=date(2025, 1, 2))

    payload = client.load_table_from_dataframe.call_args.args[0]
    # payload = PREDICTION_COLUMNS + lat_bin + lon_bin (computed at write time)
    assert list(payload.columns) == PREDICTION_COLUMNS + ["lat_bin", "lon_bin"]
    assert payload["lat_bin"].dtype == "int64"
    assert payload["lon_bin"].dtype == "int64"


def test_write_predictions_rejects_wrong_window_rows() -> None:
    df = _sample_predictions(3, target_date=date(2025, 1, 2))
    df.loc[0, "window_start_date"] = date(2025, 1, 7)  # one mismatched row
    with pytest.raises(ValueError, match="window_start_date != target_date"):
        write_predictions_to_bq(MagicMock(), df, target_date=date(2025, 1, 2))


def test_write_predictions_rejects_missing_columns() -> None:
    df = _sample_predictions(2).drop(columns=["model_version"])
    with pytest.raises(ValueError, match="missing required columns"):
        write_predictions_to_bq(MagicMock(), df, target_date=date(2025, 1, 2))


def test_write_predictions_empty_dataframe_skips_load() -> None:
    client = MagicMock()
    rows = write_predictions_to_bq(client, _sample_predictions(0), target_date=date(2025, 1, 2))
    assert rows == 0
    client.load_table_from_dataframe.assert_not_called()


# ── GeoJSON snapshot ─────────────────────────────────────────────────────────

def test_geojson_payload_is_valid_feature_collection() -> None:
    payload = build_geojson_payload(_sample_predictions(2))
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 2
    feat = payload["features"][0]
    assert feat["type"] == "Feature"
    assert feat["geometry"]["type"] == "Point"
    assert len(feat["geometry"]["coordinates"]) == 2  # [lon, lat]


def test_geojson_caps_coordinate_precision_at_5_decimals() -> None:
    payload = build_geojson_payload(_sample_predictions(1))  # default 5 decimals
    lon, lat = payload["features"][0]["geometry"]["coordinates"]
    # Source had 9 decimal places; capped at 5.
    assert lon == round(-119.123456789, 5)
    assert lat == round(34.123456789, 5)


def test_geojson_caps_probability_precision() -> None:
    payload = build_geojson_payload(_sample_predictions(1))
    prob = payload["features"][0]["properties"]["risk_probability"]
    assert prob == round(0.123456789, 5)


def test_geojson_properties_include_required_fields() -> None:
    payload = build_geojson_payload(_sample_predictions(1))
    props = payload["features"][0]["properties"]
    for required in ("risk_probability", "predicted_label", "window_start_date", "model_version"):
        assert required in props
    assert props["window_start_date"] == "2025-01-02"


def test_snapshot_uri_format() -> None:
    assert snapshot_uri(date(2025, 1, 2)) == "gs://openfire/predictions/predictions_20250102.geojson"


def test_write_geojson_snapshot_uploads_once_to_correct_uri(tmp_path: Path) -> None:
    storage = _local_storage(tmp_path)
    # Use a local prefix so we can read back what was written.
    prefix = f"local://{tmp_path}/predictions"

    written_uri = write_geojson_snapshot(
        _sample_predictions(2), target_date=date(2025, 1, 2),
        storage=storage, gcs_prefix=prefix,
    )
    assert written_uri.endswith("predictions_20250102.geojson")
    written_path = Path(written_uri.removeprefix("local://"))
    assert written_path.exists()

    # Round-trip the JSON to confirm it's valid + has expected shape.
    data = json.loads(written_path.read_text())
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2


# ── manifest ─────────────────────────────────────────────────────────────────

def test_manifest_uri_format() -> None:
    assert manifest_uri() == f"gs://openfire/predictions/{MANIFEST_FILENAME}"


def test_update_manifest_writes_required_schema(tmp_path: Path) -> None:
    storage = _local_storage(tmp_path)
    prefix = f"local://{tmp_path}/predictions"

    written_uri = update_manifest(
        latest_window_start_date=date(2026, 4, 23),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260423.geojson",
        model_version="7",
        updated_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )
    assert written_uri.endswith(MANIFEST_FILENAME)
    payload = json.loads(Path(written_uri.removeprefix("local://")).read_text())
    assert payload == {
        "latest_window_start_date": "2026-04-23",
        "latest_geojson_uri": "gs://openfire/predictions/predictions_20260423.geojson",
        "model_version": "7",
        "updated_at": "2026-04-28T12:00:00+00:00",
    }


def test_update_manifest_does_not_regress_to_older_window(tmp_path: Path) -> None:
    """A re-run on an older window must not regress the manifest pointer."""
    storage = _local_storage(tmp_path)
    prefix = f"local://{tmp_path}/predictions"

    update_manifest(
        latest_window_start_date=date(2026, 4, 17),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260417.geojson",
        model_version="m1",
        updated_at=datetime(2026, 4, 30, 8, 15, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )
    update_manifest(
        latest_window_start_date=date(2026, 4, 12),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260412.geojson",
        model_version="m1",
        updated_at=datetime(2026, 4, 30, 10, 1, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )

    payload = json.loads(Path(f"{tmp_path}/predictions/{MANIFEST_FILENAME}").read_text())
    assert payload["latest_window_start_date"] == "2026-04-17"
    assert payload["latest_geojson_uri"].endswith("predictions_20260417.geojson")


def test_update_manifest_advances_to_newer_window(tmp_path: Path) -> None:
    storage = _local_storage(tmp_path)
    prefix = f"local://{tmp_path}/predictions"

    update_manifest(
        latest_window_start_date=date(2026, 4, 12),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260412.geojson",
        model_version="m1",
        updated_at=datetime(2026, 4, 30, 8, 0, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )
    update_manifest(
        latest_window_start_date=date(2026, 4, 17),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260417.geojson",
        model_version="m1",
        updated_at=datetime(2026, 4, 30, 8, 15, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )

    payload = json.loads(Path(f"{tmp_path}/predictions/{MANIFEST_FILENAME}").read_text())
    assert payload["latest_window_start_date"] == "2026-04-17"


def test_update_manifest_rewrites_on_same_window(tmp_path: Path) -> None:
    """Same-window re-run refreshes model_version / updated_at (e.g., a model
    promotion landed)."""
    storage = _local_storage(tmp_path)
    prefix = f"local://{tmp_path}/predictions"

    update_manifest(
        latest_window_start_date=date(2026, 4, 17),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260417.geojson",
        model_version="m1",
        updated_at=datetime(2026, 4, 30, 8, 15, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )
    update_manifest(
        latest_window_start_date=date(2026, 4, 17),
        latest_geojson_uri="gs://openfire/predictions/predictions_20260417.geojson",
        model_version="m2",
        updated_at=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
        storage=storage, gcs_prefix=prefix,
    )

    payload = json.loads(Path(f"{tmp_path}/predictions/{MANIFEST_FILENAME}").read_text())
    assert payload["model_version"] == "m2"
    assert payload["updated_at"] == "2026-04-30T12:00:00+00:00"


def test_update_manifest_uses_single_atomic_write() -> None:
    """Manifest writer makes exactly one upload — the GCS object PUT is atomic
    on its own; no temp-then-rename ceremony is needed (or done)."""
    storage = MagicMock(spec=StorageClient)
    storage.exists.return_value = False
    storage.write_json.return_value = "gs://x/manifest.json"
    update_manifest(
        latest_window_start_date=date(2026, 4, 23),
        latest_geojson_uri="gs://x/predictions_20260423.geojson",
        model_version="7",
        updated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        storage=storage,
    )
    # Exactly one write_json call, no write_text/write_bytes preamble for a
    # temp file.
    storage.write_json.assert_called_once()
    storage.write_text.assert_not_called()
    storage.write_bytes.assert_not_called()


# ── DDL still passes the no-destructive-SQL guardrail after Step 6 additions ──

def test_predictions_history_ddl_in_create_file() -> None:
    sql = (REPO_ROOT / "data_pipelines" / "07_create_inference_tables.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS `msds603-mlops-project.openfire_features.predictions_history`" in sql
    assert "PARTITION BY window_start_date" in sql
    # BQ cannot CLUSTER BY FLOAT64; lat_bin/lon_bin (INT64, 0.1 degree bins) are used instead.
    assert "CLUSTER BY lat_bin, lon_bin" in sql
    assert "lat_bin" in sql and "lon_bin" in sql
    # No destructive verbs should have crept into the DDL file.
    upper = sql.upper()
    for forbidden in ("DROP TABLE", "DELETE FROM", "TRUNCATE TABLE", "CREATE OR REPLACE TABLE"):
        assert forbidden not in upper
