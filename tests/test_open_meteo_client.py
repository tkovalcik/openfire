from __future__ import annotations

import json
from datetime import date

import pytest

from data_pipeline.open_meteo_client import OpenMeteoClient, OpenMeteoError, OpenMeteoRequest


class DummyResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "DummyResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def build_payload(days: int = 30) -> dict:
    times = [f"2024-06-{day:02d}" for day in range(1, days + 1)]
    return {
        "daily": {
            "time": times,
            "precipitation_sum": [1.0] * days,
            "temperature_2m_mean": [20.0] * days,
            "relative_humidity_2m_mean": [55.0] * days,
            "wind_speed_10m_max": [8.0] * days,
        }
    }


def test_open_meteo_client_retries_then_uses_cache(tmp_path) -> None:
    calls: list[str] = []

    def opener(url: str, timeout: float):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("slow response")
        return DummyResponse(build_payload())

    sleeps: list[float] = []
    client = OpenMeteoClient(
        cache_dir=tmp_path / "cache",
        max_retries=2,
        opener=opener,
        sleeper=sleeps.append,
    )
    request = OpenMeteoRequest(
        latitude=37.5,
        longitude=-122.2,
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 30),
    )

    frame = client.fetch_daily_weather(request)
    cached_frame = client.fetch_daily_weather(request)

    assert len(frame) == 30
    assert len(cached_frame) == 30
    assert len(calls) == 2
    assert sleeps == [1.0]


def test_open_meteo_client_rejects_incomplete_payload(tmp_path) -> None:
    client = OpenMeteoClient(
        cache_dir=tmp_path / "cache",
        opener=lambda url, timeout: DummyResponse({"daily": {"time": []}}),
    )
    request = OpenMeteoRequest(
        latitude=37.5,
        longitude=-122.2,
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 30),
    )

    with pytest.raises(OpenMeteoError):
        client.fetch_daily_weather(request)
