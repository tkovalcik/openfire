from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import pandas as pd


LOGGER = logging.getLogger(__name__)

ARCHIVE_BASE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY_VARIABLES = [
    "precipitation_sum",
    "temperature_2m_mean",
    "relative_humidity_2m_mean",
    "wind_speed_10m_max",
]


class OpenMeteoError(RuntimeError):
    """Raised when an Open-Meteo request fails or returns an invalid payload."""


@dataclass(frozen=True)
class OpenMeteoRequest:
    latitude: float
    longitude: float
    start_date: date
    end_date: date

    def to_query_params(self) -> dict[str, str]:
        return {
            "latitude": f"{self.latitude:.6f}",
            "longitude": f"{self.longitude:.6f}",
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "daily": ",".join(DAILY_VARIABLES),
            "timezone": "UTC",
        }


class OpenMeteoClient:
    """Thin Open-Meteo archive client with retries and local response caching."""

    def __init__(
        self,
        *,
        base_url: str = ARCHIVE_BASE_URL,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        cache_dir: str | Path = ".cache/open_meteo",
        use_cache: bool = True,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        self._opener = opener or urlopen
        self._sleeper = sleeper

    def build_url(self, request: OpenMeteoRequest) -> str:
        return f"{self.base_url}?{urlencode(request.to_query_params())}"

    def fetch_daily_weather(self, request: OpenMeteoRequest) -> pd.DataFrame:
        payload = self._get_json(request)
        self._validate_payload(payload)
        daily = payload["daily"]
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(daily["time"], utc=False),
                "precipitation_sum": daily["precipitation_sum"],
                "temperature_2m_mean": daily["temperature_2m_mean"],
                "relative_humidity_2m_mean": daily["relative_humidity_2m_mean"],
                "wind_speed_10m_max": daily["wind_speed_10m_max"],
            }
        )
        return frame.sort_values("date").reset_index(drop=True)

    def _cache_path(self, request: OpenMeteoRequest) -> Path:
        key = hashlib.sha256(self.build_url(request).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _get_json(self, request: OpenMeteoRequest) -> dict[str, Any]:
        cache_path = self._cache_path(request)
        if self.use_cache and cache_path.exists():
            LOGGER.debug("Loading Open-Meteo response from cache: %s", cache_path)
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = self.build_url(request)
        last_error: Exception | None = None
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, self.max_retries + 1):
            try:
                LOGGER.info(
                    "Fetching Open-Meteo archive data (attempt %s/%s) for lat=%s lon=%s %s..%s",
                    attempt,
                    self.max_retries,
                    request.latitude,
                    request.longitude,
                    request.start_date,
                    request.end_date,
                )
                with self._opener(url, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if self.use_cache:
                    cache_path.write_text(json.dumps(payload), encoding="utf-8")
                return payload
            except (TimeoutError, URLError, HTTPError, json.JSONDecodeError) as error:
                last_error = error
                LOGGER.warning("Open-Meteo request failed on attempt %s: %s", attempt, error)
                if attempt == self.max_retries:
                    break
                self._sleeper(self.backoff_seconds * attempt)

        raise OpenMeteoError(f"Open-Meteo request failed after {self.max_retries} attempts") from last_error

    @staticmethod
    def _validate_payload(payload: dict[str, Any]) -> None:
        if "daily" not in payload:
            raise OpenMeteoError("Open-Meteo payload missing 'daily'")
        daily = payload["daily"]
        required_keys = {"time", *DAILY_VARIABLES}
        missing = sorted(required_keys.difference(daily))
        if missing:
            raise OpenMeteoError(f"Open-Meteo payload missing required daily keys: {missing}")
        row_counts = {key: len(daily[key]) for key in required_keys}
        if len(set(row_counts.values())) != 1:
            raise OpenMeteoError(f"Open-Meteo daily series lengths do not match: {row_counts}")
