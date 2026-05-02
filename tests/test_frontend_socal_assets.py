from __future__ import annotations

import json
import gzip
from pathlib import Path

from fastapi.testclient import TestClient

from src.ui_socal import app as ui_app


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
    assert [item["window_start_date"] for item in manifest["windows"]] == [
        "2024-07-16",
        "2024-07-21",
        "2024-07-26",
    ]
    assert all("risk_probability" in feature["properties"] for feature in risk["features"])


def test_socal_aoi_asset_contains_expected_four_counties() -> None:
    aoi = _load_json(SOCAL_DATA / "aoi_counties.geojson")

    assert aoi["type"] == "FeatureCollection"
    geoids = {feature["properties"]["GEOID"] for feature in aoi["features"]}
    names = {feature["properties"]["NAME"] for feature in aoi["features"]}

    assert geoids == EXPECTED_GEOIDS
    assert names == {"Kern", "Los Angeles", "San Luis Obispo", "Santa Barbara"}


def test_socal_ui_has_live_manifest_and_timeline_controls() -> None:
    config = (ROOT / "frontend-socal" / "config.js").read_text(encoding="utf-8")
    html = (ROOT / "frontend-socal" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "frontend-socal" / "app.js").read_text(encoding="utf-8")

    assert "/data/manifest.json" in config
    assert "socal_demo_manifest.json" in config
    assert 'id="timeline-slider"' in html
    assert 'id="playback-toggle"' in html
    assert "gs://openfire/predictions/" in app
    assert "config.snapshotCacheSize" in app
    assert "lowZoomPerformance" in config
    assert "selectDisplayFeatures" in app
    assert "geojson_variants" in app
    assert "variantKey" in config
    assert "renderActiveSnapshot" in app


def test_socal_ui_service_serves_static_files(monkeypatch) -> None:
    monkeypatch.setattr(ui_app, "STATIC_ROOT", ROOT / "frontend-socal")
    client = TestClient(ui_app.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "OpenFire SoCal AOI" in response.text


def test_socal_ui_data_proxy_reads_private_gcs(monkeypatch) -> None:
    class FakeBlob:
        content_type = "application/json"

        def exists(self) -> bool:
            return True

        def download_as_bytes(self) -> bytes:
            return b'{"ok": true}'

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            assert name == "predictions/manifest.json"
            return FakeBlob()

    class FakeClient:
        def bucket(self, name: str) -> FakeBucket:
            assert name == "openfire"
            return FakeBucket()

    monkeypatch.setattr(ui_app, "_storage_client", FakeClient())
    client = TestClient(ui_app.app)

    response = client.get("/data/manifest.json")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_socal_ui_data_proxy_preserves_gzip_encoding(monkeypatch) -> None:
    class FakeBlob:
        content_type = "application/geo+json"
        content_encoding = "gzip"

        def exists(self) -> bool:
            return True

        def download_as_bytes(self, *, raw_download: bool = False) -> bytes:
            assert raw_download is True
            return gzip.compress(b'{"ok": true}')

    class FakeBucket:
        def blob(self, name: str) -> FakeBlob:
            assert name == "predictions/predictions_20260427_z8.geojson"
            return FakeBlob()

    class FakeClient:
        def bucket(self, name: str) -> FakeBucket:
            assert name == "openfire"
            return FakeBucket()

    monkeypatch.setattr(ui_app, "_storage_client", FakeClient())
    client = TestClient(ui_app.app)

    response = client.get("/data/predictions_20260427_z8.geojson")

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.json() == {"ok": True}


def test_socal_ui_data_route_serves_static_aoi(monkeypatch) -> None:
    monkeypatch.setattr(ui_app, "STATIC_ROOT", ROOT / "frontend-socal")
    client = TestClient(ui_app.app)

    response = client.get("/data/aoi_counties.geojson")

    assert response.status_code == 200
    assert response.json()["type"] == "FeatureCollection"
