from __future__ import annotations

from datetime import date

import pandas as pd

from data_pipeline.weather_features import WEATHER_SCHEMA_VERSION, enrich_with_weather


class FakeWeatherClient:
    def __init__(self) -> None:
        self.requests = []

    def fetch_daily_weather(self, request):
        self.requests.append(request)
        dates = pd.date_range(request.start_date.isoformat(), request.end_date.isoformat(), freq="D")
        return pd.DataFrame(
            {
                "date": dates,
                "precipitation_sum": [1.0] * len(dates),
                "temperature_2m_mean": [20.0] * len(dates),
                "relative_humidity_2m_mean": [50.0] * len(dates),
                "wind_speed_10m_max": list(range(1, len(dates) + 1)),
            }
        )


def test_enrich_with_weather_joins_unique_date_windows() -> None:
    frame = pd.DataFrame(
        {
            "pixel_id": [1, 2, 3],
            "latitude": [37.70, 37.80, 37.90],
            "longitude": [-122.40, -122.30, -122.20],
            "date_window_start": ["2024-06-01", "2024-06-01", "2024-06-10"],
            "date_window_end": ["2024-06-30", "2024-06-30", "2024-07-09"],
        }
    )
    client = FakeWeatherClient()

    enriched = enrich_with_weather(frame, client=client)

    assert len(client.requests) == 2
    assert list(enriched["weather_schema_version"].unique()) == [WEATHER_SCHEMA_VERSION]
    assert enriched.loc[0, "precip_7d_sum"] == 7.0
    assert enriched.loc[0, "precip_30d_sum"] == 30.0
    assert enriched.loc[0, "temp_7d_mean"] == 20.0
    assert enriched.loc[0, "humidity_30d_mean"] == 50.0
    assert enriched.loc[0, "wind_7d_max"] == 30.0
    assert enriched.loc[0, "wind_30d_max"] == 30.0


def test_enrich_with_weather_uses_centroid_override() -> None:
    frame = pd.DataFrame(
        {
            "latitude": [36.0, 38.0],
            "longitude": [-121.0, -123.0],
            "date_window_start": ["2024-06-01", "2024-06-01"],
            "date_window_end": ["2024-06-30", "2024-06-30"],
        }
    )
    client = FakeWeatherClient()

    enrich_with_weather(frame, client=client, centroid_override=(37.5, -122.5))

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.latitude == 37.5
    assert request.longitude == -122.5
    assert request.start_date == date(2024, 6, 1)
    assert request.end_date == date(2024, 6, 30)
