from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOCAL_DATA = ROOT / "frontend-socal" / "data"
EXPECTED_GEOIDS = {"06029", "06037", "06079", "06083"}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_socal_manifest_points_to_existing_geojson() -> None:
    manifest_path = SOCAL_DATA / "socal_demo_manifest.json"
    manifest = _load_json(manifest_path)

    risk_path = (ROOT / "frontend-socal" / manifest["latest_geojson_uri"]).resolve()
    assert risk_path.exists()

    risk = _load_json(risk_path)
    assert risk["type"] == "FeatureCollection"
    assert len(risk["features"]) == 65_000
    assert manifest["latest_window_start_date"] == "2024-07-26"
    assert all("risk_probability" in feature["properties"] for feature in risk["features"])


def test_socal_aoi_asset_contains_expected_four_counties() -> None:
    aoi = _load_json(SOCAL_DATA / "aoi_counties.geojson")

    assert aoi["type"] == "FeatureCollection"
    geoids = {feature["properties"]["GEOID"] for feature in aoi["features"]}
    names = {feature["properties"]["NAME"] for feature in aoi["features"]}

    assert geoids == EXPECTED_GEOIDS
    assert names == {"Kern", "Los Angeles", "San Luis Obispo", "Santa Barbara"}
