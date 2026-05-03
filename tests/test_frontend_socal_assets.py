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
    styles = (ROOT / "frontend-socal" / "styles.css").read_text(encoding="utf-8")

    assert "/data/manifest.json" in config
    assert "socal_demo_manifest.json" in config
    assert 'excludedWindowStartDates: ["2025-12-23", "2025-12-28"]' in config
    assert "excludedWindowStartDates" in app
    assert 'id="timeline-slider"' in html
    assert 'id="playback-toggle"' in html
    assert 'id="meta-render-mode"' in html
    assert 'id="perf-latest"' in html
    assert 'href="/metrics/ui/dashboard"' in html
    assert 'src="./runtime-config.js"' in html
    assert html.index("Snapshot") < html.index("Time") < html.index("Legend") < html.index("Performance") < html.index("Source")
    assert 'data-collapsible-panel' in html
    assert "bindCollapsiblePanels" in app
    assert "performanceTracking" in config
    assert 'uiVariant: window.OPENFIRE_UI_VARIANT || "leaflet-canvas"' in config
    assert 'telemetryEndpoint: "/metrics/ui/performance"' in config
    assert 'storageKey: "openfire-socal-ui-performance"' in config
    assert "webVitals" in config
    assert "faro" in config
    assert "OPENFIRE_FARO_COLLECTOR_URL" in config
    assert "GrafanaFaroWebSdk" in app
    assert "initializeFaroTelemetry" in app
    assert "pushFaroMeasurement" in app
    assert "pushMeasurement" in app
    assert "bindWebVitals" in app
    assert "recordWebVitalMetric" in app
    assert "bindBrowserErrorTelemetry" in app
    assert "recordBrowserError" in app
    assert "unhandledrejection" in app
    assert "error_stack_hash" in app
    assert "OPENFIRE_SOCAL_PERFORMANCE" in app
    assert "enqueuePerformanceTelemetry" in app
    assert "flushPerformanceTelemetry" in app
    assert "navigator.sendBeacon" in app
    assert "PerformanceObserver" in app
    assert "recordPerformanceSample" in app
    assert "schedulePerformanceSample" in app
    assert "paintReadyMs" in app
    assert "sessionStorage" in app
    assert "crypto.getRandomValues" in app
    assert "Math.random" not in app
    assert "performance-grid" in styles
    assert "panel-link" in styles
    assert "height: 100vh;" in styles
    assert "overflow-y: auto;" in styles
    assert "overscroll-behavior: contain;" in styles
    assert "gs://openfire/predictions/" in app
    assert "config.snapshotCacheSize" in app
    assert "lowZoomPerformance" in config
    assert "{ maxZoom: 7, sampleStride: 6" in config
    assert "{ maxZoom: 9, sampleStride: 3" in config
    assert "selectDisplayFeatures" in app
    assert "geojson_variants" in app
    assert "variantKey" in config
    assert "activeSnapshotRenderLabel" in app
    assert "client fallback" in app
    assert "precomputed" in app
    assert "renderActiveSnapshot" in app
    assert "configureMapPanes" in app
    assert '"riskPane", 410' in app
    assert '"aoiPane", 430' in app
    assert '"labelPane", 610' in app
    assert "pointVisualStyle" in app
    assert "animateRiskLayerOpacity" in app
    assert "LARGE_LAYER_TRANSITION_THRESHOLD" in app
    assert "requestAnimationFrame" in app
    assert "web_vital_name" in app
    assert config.count("Model risk") == 8
    assert "Model risk 0.000-0.125" in config
    assert "Model risk 0.875-1.000" in config
    assert '"#3b8f70"' in config
    assert '"#72a95d"' in config
    assert '"#a8bd51"' in config
    assert '"#e2b84b"' in config
    assert '"#df913f"' in config
    assert '"#d8643f"' in config
    assert '"#bd3f38"' in config
    assert '"#8f2430"' in config
    assert '"#2f6fba"' not in config


def test_socal_ui_service_serves_static_files(monkeypatch) -> None:
    monkeypatch.setattr(ui_app, "STATIC_ROOT", ROOT / "frontend-socal")
    client = TestClient(ui_app.app)

    response = client.get("/")

    assert response.status_code == 200
    assert "OpenFire SoCal AOI" in response.text


def test_socal_ui_runtime_config_exposes_observability_flags(monkeypatch) -> None:
    monkeypatch.setattr(ui_app, "UI_VARIANT", "deckgl-prototype")
    monkeypatch.setattr(ui_app, "UI_VERSION", "test-sha")
    monkeypatch.setattr(ui_app, "WEB_VITALS_ENABLED", True)
    monkeypatch.setattr(ui_app, "FARO_ENABLED", True)
    monkeypatch.setattr(ui_app, "FARO_COLLECTOR_URL", "https://faro.example.test/collect/app-key")
    monkeypatch.setattr(ui_app, "FARO_ENVIRONMENT", "staging")
    client = TestClient(ui_app.app)

    response = client.get("/runtime-config.js")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "application/javascript" in response.headers["content-type"]
    assert '"OPENFIRE_UI_VARIANT":"deckgl-prototype"' in response.text
    assert '"OPENFIRE_UI_VERSION":"test-sha"' in response.text
    assert '"OPENFIRE_WEB_VITALS_ENABLED":true' in response.text
    assert '"OPENFIRE_FARO_ENABLED":true' in response.text
    assert '"OPENFIRE_FARO_COLLECTOR_URL":"https://faro.example.test/collect/app-key"' in response.text
    assert '"OPENFIRE_FARO_ENVIRONMENT":"staging"' in response.text


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


def test_socal_ui_performance_metrics_persist_to_bigquery(monkeypatch) -> None:
    class FakeBigQueryClient:
        project = "test-project"

        def __init__(self) -> None:
            self.created_table = None
            self.inserted_table_id = ""
            self.inserted_rows = []

        def get_table(self, table_id: str):
            raise ui_app.NotFound("missing table")

        def create_table(self, table, *, exists_ok: bool = False):
            assert exists_ok is True
            self.created_table = table
            return table

        def insert_rows_json(self, table_id: str, rows: list[dict]):
            self.inserted_table_id = table_id
            self.inserted_rows = rows
            return []

    fake_client = FakeBigQueryClient()
    monkeypatch.setattr(ui_app, "_bq_client", fake_client)
    monkeypatch.setattr(ui_app, "_ui_perf_table_ready", False)
    monkeypatch.setattr(ui_app, "UI_PERF_ENABLED", True)
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_PROJECT", "test-project")
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_DATASET", "openfire_features")
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_TABLE", "ui_performance_events")
    client = TestClient(ui_app.app)

    response = client.post(
        "/metrics/ui/performance",
        json={
            "schema_version": "ui_performance_v1",
            "session_id": "session-12345",
            "ui_variant": "deckgl-prototype",
            "app_version": "test-build",
            "page_path": "/",
            "viewport_width": 1440,
            "viewport_height": 900,
            "device_pixel_ratio": 2,
            "connection_effective_type": "4g",
            "events": [
                {
                    "action": "snapshot",
                    "timestamp": "2026-05-03T15:00:00Z",
                    "window_start_date": "2026-04-27",
                    "zoom": 8,
                    "source_label": "z8 precomputed",
                    "display_mode": "precomputed:/data/predictions_20260427_z8.geojson",
                    "cache_hit": False,
                    "total_feature_count": 10948,
                    "rendered_feature_count": 10948,
                    "snapshot_load_ms": 420.4,
                    "render_sync_ms": 88.2,
                    "paint_ready_ms": 560.1,
                    "web_vital_name": "INP",
                    "web_vital_value": 122.5,
                    "web_vital_rating": "needs-improvement",
                    "error_type": "TypeError",
                    "error_message": "Smoke test error",
                    "error_source": "app.js",
                    "error_line": 10,
                    "error_column": 20,
                    "error_stack_hash": "abcd1234",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "accepted": 1}
    assert fake_client.inserted_table_id == "test-project.openfire_features.ui_performance_events"
    assert fake_client.created_table.time_partitioning.field == "received_at"
    assert fake_client.created_table.clustering_fields[:2] == ["ui_variant", "event_name"]
    row = fake_client.inserted_rows[0]
    assert row["ui_variant"] == "deckgl-prototype"
    assert row["app_version"] == "test-build"
    assert row["event_name"] == "snapshot"
    assert row["window_start_date"] == "2026-04-27"
    assert row["snapshot_load_ms"] == 420.4
    assert row["paint_ready_ms"] == 560.1
    assert row["web_vital_name"] == "INP"
    assert row["web_vital_value"] == 122.5
    assert row["web_vital_rating"] == "needs-improvement"
    assert row["error_type"] == "TypeError"
    assert row["error_message"] == "Smoke test error"
    assert row["error_source"] == "app.js"
    assert row["error_line"] == 10
    assert row["error_column"] == 20
    assert row["error_stack_hash"] == "abcd1234"


def test_socal_ui_performance_dashboard_queries_bigquery(monkeypatch) -> None:
    class FakeQueryJob:
        def result(self):
            return [
                {
                    "ui_variant": "leaflet-canvas",
                    "event_name": "snapshot",
                    "render_mode": "full snapshot",
                    "sample_count": 12,
                    "p95_paint_ms": 1440.5,
                    "p95_snapshot_load_ms": 900.2,
                    "p95_render_ms": 410.0,
                    "p95_frame_wait_ms": 45.0,
                    "avg_rendered_features": 65687.0,
                }
            ]

    class FakeBigQueryClient:
        project = "test-project"

        def __init__(self) -> None:
            self.query_text = ""

        def get_table(self, table_id: str):
            raise ui_app.NotFound("missing table")

        def create_table(self, table, *, exists_ok: bool = False):
            return table

        def query(self, query: str, *, job_config):
            self.query_text = query
            assert "GROUP BY ui_variant, event_name, render_mode" in query
            assert len(job_config.query_parameters) == 2
            return FakeQueryJob()

    fake_client = FakeBigQueryClient()
    monkeypatch.setattr(ui_app, "_bq_client", fake_client)
    monkeypatch.setattr(ui_app, "_ui_perf_table_ready", False)
    monkeypatch.setattr(ui_app, "UI_PERF_ENABLED", True)
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_PROJECT", "test-project")
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_DATASET", "openfire_features")
    monkeypatch.setattr(ui_app, "UI_PERF_BQ_TABLE", "ui_performance_events")
    client = TestClient(ui_app.app)

    response = client.get("/metrics/ui/dashboard?lookback_hours=48&limit=10")

    assert response.status_code == 200
    assert "OpenFire UI Performance" in response.text
    assert "leaflet-canvas" in response.text
    assert "1,440.5 ms" in response.text


def test_socal_ui_performance_dashboard_escapes_html_text() -> None:
    html = ui_app._ui_perf_dashboard_html(
        [
            {
                "ui_variant": '<script>alert("variant")</script>',
                "event_name": '<img src=x onerror=alert("event")>',
                "render_mode": 'mode "quoted"',
                "sample_count": 1,
            }
        ],
        lookback_hours_text=ui_app.escape("<24>", quote=True),
    )

    assert '<script>alert("variant")</script>' not in html
    assert '<img src=x onerror=alert("event")>' not in html
    assert "&lt;script&gt;alert(&quot;variant&quot;)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(&quot;event&quot;)&gt;" in html
    assert "Lookback: &lt;24&gt; hours" in html


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
