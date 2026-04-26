"""Phase 2 capstone pipeline tests.

Unit tests cover:
- burned_in_next_15_days target label edge cases
- days_since_last_burn fill logic (NULL → 9999)
- Lag feature correctness on a synthetic silver DataFrame
- Orchestrator step resolution (resolve_step_index)
- Orchestrator dry-run (no BQ calls made)
- BQ utils with a fake BigQuery client

Integration tests (require BQ credentials, skip with -m "not integration"):
- Gold schema contract (columns, types, row count)
- days_since_last_burn NULL check in the live gold table
"""
from __future__ import annotations

import datetime
import sys
import unittest.mock as mock
from unittest.mock import MagicMock

import pandas as pd
import pytest

from pipelines.run_training_pipeline import STEPS, resolve_step_index


# ── helpers mirroring the SQL logic ──────────────────────────────────────────

def _burned_in_next_15_days(window_start: datetime.date, fire_dates_str: str | None) -> bool:
    """Python equivalent of the BQ burned_in_next_15_days expression."""
    if not fire_dates_str or fire_dates_str == "None":
        return False
    lo = window_start + datetime.timedelta(days=1)
    hi = window_start + datetime.timedelta(days=15)
    for token in fire_dates_str.split(","):
        try:
            d = datetime.date.fromisoformat(token.strip())
        except ValueError:
            continue
        if lo <= d <= hi:
            return True
    return False


def _days_since_last_burn(window_start: datetime.date, fire_dates_str: str | None) -> int:
    """Python equivalent of days_since_last_burn with the 9999 COALESCE fill."""
    if not fire_dates_str or fire_dates_str == "None":
        return 9999
    past = []
    for token in fire_dates_str.split(","):
        try:
            d = datetime.date.fromisoformat(token.strip())
        except ValueError:
            continue
        if d < window_start:
            past.append(d)
    return (window_start - max(past)).days if past else 9999


# ── burned_in_next_15_days edge cases ────────────────────────────────────────

def test_burned_fire_1_day_after_is_true() -> None:
    assert _burned_in_next_15_days(datetime.date(2023, 6, 1), "2023-06-02") is True


def test_burned_fire_15_days_after_is_true() -> None:
    assert _burned_in_next_15_days(datetime.date(2023, 6, 1), "2023-06-16") is True


def test_burned_fire_16_days_after_is_false() -> None:
    assert _burned_in_next_15_days(datetime.date(2023, 6, 1), "2023-06-17") is False


def test_burned_no_fire_dates_is_false() -> None:
    assert _burned_in_next_15_days(datetime.date(2023, 6, 1), None) is False
    assert _burned_in_next_15_days(datetime.date(2023, 6, 1), "None") is False


# ── days_since_last_burn fill ─────────────────────────────────────────────────

def test_days_since_last_burn_no_history_fills_9999() -> None:
    assert _days_since_last_burn(datetime.date(2023, 6, 1), None) == 9999
    assert _days_since_last_burn(datetime.date(2023, 6, 1), "None") == 9999


def test_days_since_last_burn_past_fire_is_correct() -> None:
    # Two past fires; most recent is 2023-01-10, which is 141 days before 2023-06-01
    result = _days_since_last_burn(datetime.date(2023, 6, 1), "2022-08-15,2023-01-10")
    expected = (datetime.date(2023, 6, 1) - datetime.date(2023, 1, 10)).days
    assert result == expected


# ── lag feature correctness on synthetic silver ───────────────────────────────

def _build_synthetic_silver() -> pd.DataFrame:
    """Thirteen 5-day observations for one cell — enough to produce a 60d lag."""
    dates = [datetime.date(2023, 1, 1) + datetime.timedelta(days=5 * i) for i in range(13)]
    n = len(dates)
    return pd.DataFrame({
        "latitude": [34.0] * n,
        "longitude": [-118.0] * n,
        "window_start_date": dates,
        "mean_NDVI": [0.1 * (i + 1) for i in range(n)],
        "gridmet_temp_max": [20.0 + i for i in range(n)],
        "gridmet_precip_sum": [1.0 + 0.5 * i for i in range(n)],
    })


def _apply_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """Replicate the BQ LAG window logic in pandas."""
    df = df.sort_values(["latitude", "longitude", "window_start_date"]).copy()
    grp = df.groupby(["latitude", "longitude"])
    df["ndvi_change_5d"] = df["mean_NDVI"] - grp["mean_NDVI"].shift(1)
    df["ndvi_change_15d"] = df["mean_NDVI"] - grp["mean_NDVI"].shift(3)
    df["ndvi_change_60d"] = df["mean_NDVI"] - grp["mean_NDVI"].shift(12)
    df["temp_change_5d"] = df["gridmet_temp_max"] - grp["gridmet_temp_max"].shift(1)
    df["precip_change_15d"] = df["gridmet_precip_sum"] - grp["gridmet_precip_sum"].shift(3)
    return df


def test_lag_features_correct_on_synthetic_silver() -> None:
    df = _apply_lag_features(_build_synthetic_silver()).reset_index(drop=True)

    # Row 1: ndvi_change_5d = ndvi[1] - ndvi[0] = 0.2 - 0.1 = 0.1
    assert abs(df.loc[1, "ndvi_change_5d"] - 0.1) < 1e-9

    # Row 3: ndvi_change_15d = ndvi[3] - ndvi[0] = 0.4 - 0.1 = 0.3
    assert abs(df.loc[3, "ndvi_change_15d"] - 0.3) < 1e-9

    # First row has no prior — should be NaN
    assert pd.isna(df.loc[0, "ndvi_change_5d"])

    # ndvi_change_60d is non-null only at the last row (shift=12, need 13 rows)
    non_null = df["ndvi_change_60d"].dropna()
    assert len(non_null) == 1
    assert abs(non_null.iloc[0] - (1.3 - 0.1)) < 1e-9

    # The WHERE ndvi_change_60d IS NOT NULL filter keeps only 1 of 13 rows
    filtered = df[df["ndvi_change_60d"].notna()]
    assert len(filtered) == 1


# ── orchestrator step resolution ─────────────────────────────────────────────

def test_resolve_step_index_by_prefix() -> None:
    idx = resolve_step_index("04")
    assert STEPS[idx].name == "04_merge_silver"


def test_resolve_step_index_by_full_name() -> None:
    idx = resolve_step_index("04_merge_silver")
    assert STEPS[idx].name == "04_merge_silver"


def test_resolve_step_index_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown step"):
        resolve_step_index("99_not_a_step")


# ── orchestrator dry-run ──────────────────────────────────────────────────────

def test_orchestrator_dry_run_calls_no_bq() -> None:
    """--dry-run must log the plan and return without ever calling get_client."""
    from pipelines import run_training_pipeline as rtp

    with mock.patch.object(sys, "argv", ["run_training_pipeline", "--dry-run"]):
        with mock.patch.object(rtp, "get_client") as mock_client:
            rtp.main()
            mock_client.assert_not_called()


# ── BQ utils with a fake client ──────────────────────────────────────────────

def _make_fake_bq_client(*, exists: bool, row_count: int = 0) -> MagicMock:
    from google.cloud.exceptions import NotFound

    client = MagicMock()
    if exists:
        fake_table = MagicMock()
        fake_table.num_rows = row_count
        client.get_table.return_value = fake_table
    else:
        client.get_table.side_effect = NotFound("not found")
    return client


def test_bq_utils_table_exists_false_for_missing_table() -> None:
    from pipelines.bq_utils import table_exists

    client = _make_fake_bq_client(exists=False)
    assert table_exists(client, "project.dataset.missing") is False


def test_bq_utils_get_row_count_returns_integer() -> None:
    from pipelines.bq_utils import get_row_count

    client = _make_fake_bq_client(exists=True, row_count=42)
    count = get_row_count(client, "project.dataset.existing")
    assert isinstance(count, int)
    assert count == 42


# ── integration tests (require live BQ credentials) ──────────────────────────

_GOLD_TABLE = "msds603-mlops-project.openfire_features.gold_features"

_EXPECTED_COLUMNS = {
    "window_start_date", "latitude", "longitude",
    "burned_in_next_15_days", "days_since_last_burn",
    "ndvi_change_5d", "ndvi_change_15d", "ndvi_change_30d", "ndvi_change_60d",
    "ndwi_change_5d", "ndwi_change_15d", "ndwi_change_30d", "ndwi_change_60d",
    "temp_change_5d", "temp_change_15d", "temp_change_30d", "temp_change_60d",
    "precip_change_15d", "precip_change_30d", "precip_change_60d",
    "mean_elevation", "mean_slope", "mean_cos_aspect", "mean_sin_aspect",
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
    "gridmet_temp_max", "gridmet_humidity_min", "gridmet_precip_sum", "gridmet_wind_max",
}


@pytest.mark.integration
def test_gold_schema_contract() -> None:
    """Gold table has all expected columns and >10M rows."""
    from google.cloud import bigquery

    client = bigquery.Client(project="msds603-mlops-project")
    table = client.get_table(_GOLD_TABLE)
    actual = {f.name for f in table.schema}
    missing = _EXPECTED_COLUMNS - actual
    assert not missing, f"Missing columns in gold_features: {missing}"
    assert table.num_rows > 10_000_000, f"Suspiciously low row count: {table.num_rows:,}"


@pytest.mark.integration
def test_days_since_last_burn_no_nulls_in_gold() -> None:
    """COALESCE(days_since_last_burn, 9999) in step 05 must leave zero NULLs."""
    from google.cloud import bigquery

    client = bigquery.Client(project="msds603-mlops-project")
    rows = client.query(
        f"SELECT COUNTIF(days_since_last_burn IS NULL) AS null_count FROM `{_GOLD_TABLE}`"
    ).result()
    null_count = next(iter(rows))["null_count"]
    assert null_count == 0, f"Found {null_count} NULL values in days_since_last_burn"
