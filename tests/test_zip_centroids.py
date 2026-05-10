from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from src.ui_socal.alert_worker import ZipCentroid
from src.ui_socal.zip_centroids import (
    load_zip_centroids_from_csv,
    main,
    zip_centroid_rows,
    zip_centroid_schema,
)


def test_load_zip_centroids_from_csv(tmp_path: Path) -> None:
    path = tmp_path / "zip_centroids.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["zip", "latitude", "longitude"])
        writer.writeheader()
        writer.writerow({"zip": " 90001 ", "latitude": "34.01", "longitude": "-118.01"})

    assert load_zip_centroids_from_csv(path) == {
        "90001": ZipCentroid(zip="90001", latitude=34.01, longitude=-118.01)
    }


def test_load_zip_centroids_from_csv_requires_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("zip,latitude\n90001,34.01\n", encoding="utf-8")

    with pytest.raises(ValueError, match="longitude"):
        load_zip_centroids_from_csv(path)


def test_zip_centroid_rows_are_sorted_and_include_loaded_at() -> None:
    rows = zip_centroid_rows(
        [
            ZipCentroid(zip="90002", latitude=34.2, longitude=-118.2),
            ZipCentroid(zip="90001", latitude=34.1, longitude=-118.1),
        ]
    )

    assert [row["zip"] for row in rows] == ["90001", "90002"]
    assert rows[0]["loaded_at"]


def test_zip_centroid_schema_matches_worker_query_columns() -> None:
    fields = {field.name: field.field_type for field in zip_centroid_schema()}

    assert fields["zip"] == "STRING"
    assert fields["latitude"] == "FLOAT64"
    assert fields["longitude"] == "FLOAT64"
    assert fields["loaded_at"] == "TIMESTAMP"


def test_cli_dry_run_summarizes_json_source(tmp_path: Path, capsys) -> None:
    path = tmp_path / "zip_centroids.json"
    path.write_text(
        json.dumps({"90001": {"latitude": 34.01, "longitude": -118.01}}),
        encoding="utf-8",
    )

    assert main(["--source-json", str(path)]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["dry_run"] is True
    assert output["input_rows"] == 1
    assert output["written_rows"] == 0
    assert output["table_id"] == "msds603-mlops-project.openfire_features.zip_centroids"
