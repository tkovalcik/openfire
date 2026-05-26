"""Unit tests for src/pipelines/date_grid.py.

Pure-function module; no GEE / BQ / GCS dependencies. Covers the edge cases
called out in the Phase 4 spec: epoch boundary, leap day, off-grid range,
single-date range, empty range, range fully before epoch.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from pipelines.date_grid import (
    EPOCH_START,
    STEP_DAYS,
    is_grid_date,
    list_grid_dates,
    next_grid_date,
)


# ── is_grid_date ─────────────────────────────────────────────

def test_epoch_itself_is_a_grid_date() -> None:
    assert is_grid_date(EPOCH_START)


def test_epoch_plus_5_is_grid_date() -> None:
    assert is_grid_date(EPOCH_START + timedelta(days=5))


def test_epoch_plus_4_is_not_grid_date() -> None:
    assert not is_grid_date(EPOCH_START + timedelta(days=4))


def test_date_before_epoch_is_not_grid_date() -> None:
    assert not is_grid_date(EPOCH_START - timedelta(days=1))
    assert not is_grid_date(EPOCH_START - timedelta(days=5))  # would be on-grid if ran backward


def test_leap_day_2020_grid_alignment() -> None:
    # 2020-02-29 is 911 days after EPOCH_START (2017-09-01).
    # 911 % 5 == 1, so Feb 29 2020 is NOT a grid date; the surrounding
    # grid dates are 2020-02-28 (910 days) and 2020-03-04 (915 days).
    assert (date(2020, 2, 29) - EPOCH_START).days == 911
    assert not is_grid_date(date(2020, 2, 29))
    assert is_grid_date(date(2020, 2, 28))
    assert is_grid_date(date(2020, 3, 4))


# ── next_grid_date ───────────────────────────────────────────

def test_next_grid_date_strictly_after_grid_date() -> None:
    assert next_grid_date(EPOCH_START) == EPOCH_START + timedelta(days=5)


def test_next_grid_date_off_grid_input() -> None:
    # Input 2020-02-29 (off-grid) → next on-grid is 2020-03-04.
    assert next_grid_date(date(2020, 2, 29)) == date(2020, 3, 4)


def test_next_grid_date_before_epoch_returns_epoch() -> None:
    assert next_grid_date(EPOCH_START - timedelta(days=10)) == EPOCH_START
    assert next_grid_date(EPOCH_START - timedelta(days=1)) == EPOCH_START


# ── list_grid_dates ──────────────────────────────────────────

def test_list_grid_dates_empty_range_when_end_before_start() -> None:
    assert list_grid_dates(date(2024, 6, 1), date(2024, 5, 1)) == []


def test_list_grid_dates_single_grid_date_in_range() -> None:
    # Tight range around exactly one grid date.
    assert list_grid_dates(EPOCH_START, EPOCH_START) == [EPOCH_START]
    assert list_grid_dates(
        EPOCH_START + timedelta(days=4),
        EPOCH_START + timedelta(days=6),
    ) == [EPOCH_START + timedelta(days=5)]


def test_list_grid_dates_off_grid_range_with_no_grid_inside() -> None:
    # 2017-09-02 → 2017-09-04 contains no grid date (epoch is 09-01, next is 09-06).
    assert list_grid_dates(date(2017, 9, 2), date(2017, 9, 4)) == []


def test_list_grid_dates_range_fully_before_epoch_is_empty() -> None:
    assert list_grid_dates(date(2017, 1, 1), date(2017, 8, 31)) == []


def test_list_grid_dates_range_starting_before_epoch_clips_to_epoch() -> None:
    # Range 2017-08-01 → 2017-09-11 clips to [EPOCH_START, 2017-09-11].
    assert list_grid_dates(date(2017, 8, 1), date(2017, 9, 11)) == [
        EPOCH_START,
        EPOCH_START + timedelta(days=5),
        EPOCH_START + timedelta(days=10),
    ]


def test_list_grid_dates_step_size_is_5() -> None:
    dates = list_grid_dates(date(2024, 1, 1), date(2024, 1, 31))
    diffs = {(b - a).days for a, b in zip(dates, dates[1:])}
    assert diffs == {STEP_DAYS}


def test_list_grid_dates_one_year_count() -> None:
    # 2018 has 365 days; 365 / 5 = 73 grid dates if both endpoints are grid-aligned.
    # 2018-01-04 is on-grid (125d from epoch), 2018-12-30 is on-grid (485d from epoch).
    dates = list_grid_dates(date(2018, 1, 4), date(2018, 12, 30))
    assert len(dates) == 73
    assert dates[0] == date(2018, 1, 4)
    assert dates[-1] == date(2018, 12, 30)


# ── extractor still imports the legacy helpers from this module ──────────

def test_legacy_generate_date_list_returns_iso_strings() -> None:
    from pipelines.date_grid import generate_date_list

    # 2024-01-03 is on-grid (2190 days from 2017-09-01 = 438 * 5).
    out = generate_date_list(EPOCH_START, date(2024, 1, 1), date(2024, 1, 14), STEP_DAYS)
    assert all(isinstance(s, str) for s in out)
    assert out == ["2024-01-03", "2024-01-08", "2024-01-13"]


def test_legacy_years_in_range_spans_calendar_boundary() -> None:
    from pipelines.date_grid import years_in_range

    years = years_in_range(EPOCH_START, date(2019, 12, 1), date(2020, 2, 1), STEP_DAYS)
    assert years == [2019, 2020]


def test_legacy_helpers_with_custom_epoch_use_fallback() -> None:
    """If a caller passes a non-default epoch, the function still works."""
    from pipelines.date_grid import generate_date_list

    # Custom epoch 2020-01-01, step 7 days.
    out = generate_date_list(date(2020, 1, 1), date(2020, 1, 1), date(2020, 1, 22), 7)
    assert out == ["2020-01-01", "2020-01-08", "2020-01-15", "2020-01-22"]


# ── parametrize sweep ────────────────────────────────────────

@pytest.mark.parametrize(
    "d, expected",
    [
        (EPOCH_START, True),
        (EPOCH_START + timedelta(days=1), False),
        (EPOCH_START + timedelta(days=5), True),
        (EPOCH_START + timedelta(days=365), (365 % 5 == 0)),  # 2018-09-01
        (EPOCH_START + timedelta(days=1825), (1825 % 5 == 0)),  # 5 years out
    ],
)
def test_is_grid_date_sweep(d: date, expected: bool) -> None:
    assert is_grid_date(d) == expected
