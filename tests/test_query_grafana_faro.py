from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "query_grafana_faro.py"
SPEC = importlib.util.spec_from_file_location("query_grafana_faro", SCRIPT_PATH)
assert SPEC is not None
query_grafana_faro = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = query_grafana_faro
SPEC.loader.exec_module(query_grafana_faro)


def test_parse_logfmt_line_handles_quoted_faro_context() -> None:
    parsed = query_grafana_faro.parse_logfmt_line(
        'timestamp=2026-05-03T17:17:51.688Z kind=measurement '
        'type=openfire_ui_interaction context_source_label="z9 precomputed" '
        "context_ui_variant=leaflet-canvas paint_ready_ms=697.600000 "
        "value_snapshot_load_ms=349.59999990463257"
    )

    assert parsed["type"] == "openfire_ui_interaction"
    assert parsed["context_source_label"] == "z9 precomputed"
    assert parsed["context_ui_variant"] == "leaflet-canvas"
    assert parsed["paint_ready_ms"] == "697.600000"
    assert parsed["value_snapshot_load_ms"] == "349.59999990463257"


def test_summarize_records_groups_openfire_measurements() -> None:
    records = [
        query_grafana_faro.FaroRecord(
            timestamp_ns="1",
            labels={"service_version": "test"},
            fields={
                "context_ui_variant": "leaflet-canvas",
                "context_action": "snapshot",
                "context_source_label": "z8 precomputed",
                "paint_ready_ms": "100",
                "snapshot_load_ms": "40",
                "render_sync_ms": "20",
                "frame_wait_ms": "16",
                "rendered_feature_count": "10000",
            },
        ),
        query_grafana_faro.FaroRecord(
            timestamp_ns="2",
            labels={"service_version": "test"},
            fields={
                "context_ui_variant": "leaflet-canvas",
                "context_action": "snapshot",
                "context_source_label": "z8 precomputed",
                "value_paint_ready_ms": "300",
                "value_snapshot_load_ms": "80",
                "value_render_sync_ms": "50",
                "value_frame_wait_ms": "32",
                "value_rendered_feature_count": "12000",
            },
        ),
    ]

    summary = query_grafana_faro.summarize_records(records)

    assert len(summary) == 1
    row = summary[0]
    assert row["ui_variant"] == "leaflet-canvas"
    assert row["action"] == "snapshot"
    assert row["source_label"] == "z8 precomputed"
    assert row["samples"] == 2
    assert row["p95_paint_ready_ms"] == 300.0
    assert row["avg_rendered_feature_count"] == 11000.0
