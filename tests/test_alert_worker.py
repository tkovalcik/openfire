from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ui_socal.alert_worker import (
    Subscription,
    ZipCentroid,
    evaluate_alerts,
    haversine_km,
    latest_geojson_uri,
    load_subscriptions_from_bigquery,
    load_risk_cells,
    resolve_manifest_asset_uri,
    subscription_table_id,
    subscriptions_query,
)


def test_haversine_zero_for_same_point() -> None:
    assert haversine_km(34.0, -118.0, 34.0, -118.0) == pytest.approx(0.0)


def test_evaluate_alerts_uses_nearest_cell_and_threshold() -> None:
    cells = [
        # Deliberately farther but higher risk.
        _cell(latitude=35.0, longitude=-119.0, risk=0.99),
        # Nearest cell; should be used for the decision.
        _cell(latitude=34.0, longitude=-118.0, risk=0.7),
    ]
    subscriptions = [
        Subscription(email="a@example.com", zip="90001", risk_threshold=0.6),
        Subscription(email="b@example.com", zip="90001", risk_threshold=0.8),
        Subscription(email="c@example.com", zip="99999", risk_threshold=0.1),
    ]
    centroids = {"90001": ZipCentroid(zip="90001", latitude=34.01, longitude=-118.01)}

    candidates = evaluate_alerts(subscriptions, centroids, cells)

    assert [candidate.subscription.email for candidate in candidates] == ["a@example.com"]
    assert candidates[0].risk_cell.risk_probability == pytest.approx(0.7)


def test_latest_geojson_uri_resolves_local_relative_manifest_path(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "latest_window_start_date": "2026-04-17",
                "latest_geojson_uri": "./predictions_20260417.geojson",
            }
        ),
        encoding="utf-8",
    )

    out = latest_geojson_uri(
        json.loads(manifest_path.read_text(encoding="utf-8")),
        manifest_uri=f"local://{manifest_path}",
    )

    assert out == f"local://{tmp_path / 'predictions_20260417.geojson'}"


def test_subscription_table_id_uses_default_bigquery_location() -> None:
    assert (
        subscription_table_id()
        == "msds603-mlops-project.openfire_features.ui_subscriptions"
    )


def test_subscriptions_query_deduplicates_by_email_and_zip() -> None:
    query = subscriptions_query("project.dataset.table")

    assert "FROM `project.dataset.table`" in query
    assert "PARTITION BY LOWER(TRIM(email)), TRIM(zip)" in query
    assert "ORDER BY created_at DESC" in query


def test_load_subscriptions_from_bigquery_uses_query_rows() -> None:
    client = _FakeBigQueryClient(
        [
            {"email": " A@EXAMPLE.COM ", "zip": " 90001 ", "risk_threshold": "0.7"},
            {"email": "b@example.com", "zip": "90002", "risk_threshold": None},
        ]
    )

    subscriptions = load_subscriptions_from_bigquery(
        "project.dataset.ui_subscriptions",
        client=client,
    )

    assert client.queries == [subscriptions_query("project.dataset.ui_subscriptions")]
    assert subscriptions == [
        Subscription(email="a@example.com", zip="90001", risk_threshold=0.7),
        Subscription(email="b@example.com", zip="90002", risk_threshold=None),
    ]


def test_load_subscriptions_from_bigquery_handles_empty_table() -> None:
    subscriptions = load_subscriptions_from_bigquery(
        "project.dataset.ui_subscriptions",
        client=_FakeBigQueryClient([]),
    )

    assert subscriptions == []


def test_resolve_manifest_asset_uri_preserves_gcs_uri() -> None:
    assert (
        resolve_manifest_asset_uri(
            "gs://openfire/predictions/predictions_20260417.geojson",
            manifest_uri="gs://openfire/predictions/manifest.json",
        )
        == "gs://openfire/predictions/predictions_20260417.geojson"
    )


def test_resolve_manifest_asset_uri_handles_deckgl_legacy_demo_path(tmp_path: Path) -> None:
    manifest_path = tmp_path / "frontend-socal-deckgl" / "data" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")

    out = resolve_manifest_asset_uri(
        "../frontend-socal/data/socal_20240726_risk.geojson",
        manifest_uri=f"local://{manifest_path}",
    )

    assert out == f"local://{tmp_path / 'frontend-socal/data/socal_20240726_risk.geojson'}"


def test_load_risk_cells_parses_geojson(tmp_path: Path) -> None:
    path = tmp_path / "risk.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [-118.1, 34.2],
                        },
                        "properties": {
                            "risk_probability": 0.42,
                            "predicted_label": 1,
                            "window_start_date": "2026-04-17",
                            "model_version": "abc123",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cells = load_risk_cells(f"local://{path}")

    assert len(cells) == 1
    assert cells[0].latitude == pytest.approx(34.2)
    assert cells[0].longitude == pytest.approx(-118.1)
    assert cells[0].risk_probability == pytest.approx(0.42)


def _cell(*, latitude: float, longitude: float, risk: float):
    from src.ui_socal.alert_worker import RiskCell

    return RiskCell(
        latitude=latitude,
        longitude=longitude,
        risk_probability=risk,
        predicted_label=None,
        window_start_date="2026-04-17",
        model_version="test",
    )


class _FakeQueryJob:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def result(self) -> list[dict[str, object]]:
        return self._rows


class _FakeBigQueryClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    def query(self, query: str) -> _FakeQueryJob:
        self.queries.append(query)
        return _FakeQueryJob(self._rows)
