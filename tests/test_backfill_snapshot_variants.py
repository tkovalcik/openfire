from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

from src.common.storage import StorageClient
from scripts.backfill_snapshot_variants import (
    backfill_snapshot_variants,
    build_variant_payload,
)


def _storage(tmp_path: Path) -> StorageClient:
    return StorageClient(local_cache_dir=tmp_path / ".cache")


def _feature(latitude: float, longitude: float, probability: float = 0.5) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
        "properties": {
            "risk_probability": probability,
            "predicted_label": int(probability >= 0.5),
            "window_start_date": "2026-04-27",
            "model_version": "7",
        },
    }


def _payload() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            _feature(1.0, -120.0, 0.1),
            _feature(3.0, -122.0, 0.2),
            _feature(2.0, -121.0, 0.3),
            _feature(3.0, -121.0, 0.4),
            _feature(1.0, -119.0, 0.5),
            _feature(2.0, -120.0, 0.6),
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_gzip_json(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def test_build_variant_payload_uses_deterministic_spatial_sample() -> None:
    sampled = build_variant_payload(_payload(), sample_stride=3, sample_offset=1)

    assert [feature["geometry"]["coordinates"] for feature in sampled["features"]] == [
        [-121.0, 3.0],
        [-120.0, 1.0],
    ]


def test_backfill_snapshot_variants_writes_gzipped_variants_and_manifest(tmp_path: Path) -> None:
    prefix_path = tmp_path / "predictions"
    _write_json(prefix_path / "predictions_20260427.geojson", _payload())

    results = backfill_snapshot_variants(
        gcs_prefix=f"local://{prefix_path}",
        storage=_storage(tmp_path),
    )

    assert len(results) == 1
    assert results[0].window_start_date == date(2026, 4, 27)
    assert set(results[0].variants_written) == {"low", "medium"}

    z8 = _read_gzip_json(prefix_path / "predictions_20260427_z8.geojson")
    z9 = _read_gzip_json(prefix_path / "predictions_20260427_z9.geojson")
    assert len(z8["features"]) == 1
    assert len(z9["features"]) == 2

    manifest = json.loads((prefix_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["latest_window_start_date"] == "2026-04-27"
    variants = manifest["windows"][0]["geojson_variants"]
    assert variants["low"]["geojson_uri"].endswith("predictions_20260427_z8.geojson")
    assert variants["medium"]["geojson_uri"].endswith("predictions_20260427_z9.geojson")
    assert manifest["latest_geojson_variants"] == variants


def test_backfill_snapshot_variants_skips_existing_by_default(tmp_path: Path) -> None:
    prefix_path = tmp_path / "predictions"
    _write_json(prefix_path / "predictions_20260427.geojson", _payload())
    existing_z8 = prefix_path / "predictions_20260427_z8.geojson"
    existing_z9 = prefix_path / "predictions_20260427_z9.geojson"
    existing_z8.write_bytes(b"existing-z8")
    existing_z9.write_bytes(b"existing-z9")

    results = backfill_snapshot_variants(
        gcs_prefix=f"local://{prefix_path}",
        storage=_storage(tmp_path),
    )

    assert results[0].variants_written == ()
    assert set(results[0].variants_skipped) == {"low", "medium"}
    assert existing_z8.read_bytes() == b"existing-z8"
    assert existing_z9.read_bytes() == b"existing-z9"


def test_backfill_snapshot_variants_dry_run_writes_nothing(tmp_path: Path) -> None:
    prefix_path = tmp_path / "predictions"
    _write_json(prefix_path / "predictions_20260427.geojson", _payload())

    results = backfill_snapshot_variants(
        gcs_prefix=f"local://{prefix_path}",
        storage=_storage(tmp_path),
        dry_run=True,
    )

    assert results[0].dry_run is True
    assert set(results[0].variants_skipped) == {"low", "medium"}
    assert not (prefix_path / "predictions_20260427_z8.geojson").exists()
    assert not (prefix_path / "predictions_20260427_z9.geojson").exists()
    assert not (prefix_path / "manifest.json").exists()


def test_backfill_snapshot_variants_force_rewrites_existing(tmp_path: Path) -> None:
    prefix_path = tmp_path / "predictions"
    _write_json(prefix_path / "predictions_20260427.geojson", _payload())
    (prefix_path / "predictions_20260427_z8.geojson").write_bytes(b"existing-z8")
    (prefix_path / "predictions_20260427_z9.geojson").write_bytes(b"existing-z9")

    results = backfill_snapshot_variants(
        gcs_prefix=f"local://{prefix_path}",
        storage=_storage(tmp_path),
        force=True,
        update_manifest=False,
    )

    assert set(results[0].variants_written) == {"low", "medium"}
    assert _read_gzip_json(prefix_path / "predictions_20260427_z8.geojson")["type"] == "FeatureCollection"
    assert _read_gzip_json(prefix_path / "predictions_20260427_z9.geojson")["type"] == "FeatureCollection"
    assert not (prefix_path / "manifest.json").exists()
