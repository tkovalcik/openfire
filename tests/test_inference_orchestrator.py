"""Tests for src/pipelines/run_inference_pipeline.py and date_grid additions.

Covers what the Phase 4 spec calls out for the orchestrator skeleton:
- Mode resolution: backfill / latest / window all return correct windows.
- `latest` with empty predictions_history → single most recent grid date
  (not a full backfill from EPOCH_START).
- `latest` when last_processed is already current → empty list.
- Off-grid `--date` for window mode is rejected.
- Backfill chunking: a 6-month range emits the right grid dates.
- Dry-run does not call the BQ provider (the latest-mode last_processed lookup).
- previous_grid_date helper edge cases.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from pipelines.date_grid import EPOCH_START, STEP_DAYS, previous_grid_date
from pipelines.run_inference_pipeline import (
    WindowOutcome,
    build_arg_parser,
    process_window,
    resolve_windows_backfill,
    resolve_windows_for_args,
    resolve_windows_latest,
    resolve_windows_window,
)


# ── previous_grid_date (date_grid module) ────────────────────────────────────

def test_previous_grid_date_strictly_before_grid_date() -> None:
    assert previous_grid_date(EPOCH_START + timedelta(days=10)) == EPOCH_START + timedelta(days=5)
    # Strictly before — input on-grid yields the prior grid date, not itself.
    assert previous_grid_date(EPOCH_START + timedelta(days=5)) == EPOCH_START


def test_previous_grid_date_off_grid_input() -> None:
    # 2020-02-29 (off-grid) → previous on-grid is 2020-02-28.
    assert previous_grid_date(date(2020, 2, 29)) == date(2020, 2, 28)


def test_previous_grid_date_at_or_before_epoch_raises() -> None:
    with pytest.raises(ValueError, match="No grid date exists strictly before"):
        previous_grid_date(EPOCH_START)
    with pytest.raises(ValueError):
        previous_grid_date(EPOCH_START - timedelta(days=10))


# ── resolve_windows_backfill ─────────────────────────────────────────────────

def test_backfill_returns_all_grid_dates_in_range() -> None:
    # 2025-01-02 is on-grid (2680 days from epoch = 536 * 5).
    assert (date(2025, 1, 2) - EPOCH_START).days % STEP_DAYS == 0
    out = resolve_windows_backfill(date(2025, 1, 2), date(2025, 2, 1))
    assert out[0] == date(2025, 1, 2)
    assert out[-1] <= date(2025, 2, 1)
    diffs = {(b - a).days for a, b in zip(out, out[1:])}
    assert diffs == {STEP_DAYS}


def test_backfill_six_month_range_emits_correct_count() -> None:
    # 6 months ≈ 182 days → ~36-37 grid dates.
    out = resolve_windows_backfill(date(2025, 1, 1), date(2025, 6, 30))
    assert 35 <= len(out) <= 37


def test_backfill_empty_when_end_before_start() -> None:
    assert resolve_windows_backfill(date(2025, 6, 1), date(2025, 1, 1)) == []


# ── resolve_windows_window ───────────────────────────────────────────────────

def test_window_mode_accepts_grid_date() -> None:
    grid_date = EPOCH_START + timedelta(days=20)
    assert resolve_windows_window(grid_date) == [grid_date]


def test_window_mode_rejects_off_grid_date() -> None:
    with pytest.raises(ValueError, match="not on the 5-day grid"):
        resolve_windows_window(EPOCH_START + timedelta(days=3))


# ── resolve_windows_latest ───────────────────────────────────────────────────

def test_latest_first_ever_run_returns_single_recent_date() -> None:
    # No predictions_history yet → single window only, no full backfill.
    today = date(2026, 4, 28)
    out = resolve_windows_latest(last_processed=None, today=today)
    assert len(out) == 1
    assert out[0] == previous_grid_date(today)


def test_latest_no_op_when_caught_up() -> None:
    today = date(2026, 4, 28)
    target = previous_grid_date(today)
    # last_processed already at or past target → empty.
    assert resolve_windows_latest(last_processed=target, today=today) == []
    assert resolve_windows_latest(last_processed=target + timedelta(days=5), today=today) == []


def test_latest_catches_up_after_missed_runs() -> None:
    today = date(2026, 4, 28)
    target = previous_grid_date(today)
    # Pretend we last processed 3 grid dates ago (15 days).
    last = target - timedelta(days=15)
    out = resolve_windows_latest(last_processed=last, today=today)
    # Should include the next 3 grid dates after `last`, ending at `target`.
    assert out[0] == last + timedelta(days=5)
    assert out[-1] == target
    assert len(out) == 3


def test_latest_does_not_include_today_itself() -> None:
    # If `today` happens to be on-grid, `target` is the prior grid date.
    today = date(2024, 1, 3)  # on-grid (2310 days)
    assert (today - EPOCH_START).days % STEP_DAYS == 0
    target = previous_grid_date(today)
    assert target == today - timedelta(days=5)
    out = resolve_windows_latest(last_processed=None, today=today)
    assert out == [target]
    assert today not in out


# ── resolve_windows_for_args (CLI dispatch) ──────────────────────────────────

def _ns(**kw) -> argparse.Namespace:
    defaults = dict(mode=None, start=None, end=None, date=None, dry_run=False)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_dispatch_backfill_does_not_call_provider() -> None:
    provider = MagicMock(side_effect=AssertionError("provider must not be called for backfill"))
    out = resolve_windows_for_args(
        _ns(mode="backfill", start=date(2025, 1, 1), end=date(2025, 1, 31)),
        last_processed_provider=provider,
    )
    assert out  # non-empty
    provider.assert_not_called()


def test_dispatch_window_does_not_call_provider() -> None:
    provider = MagicMock(side_effect=AssertionError("provider must not be called for window"))
    grid_date = EPOCH_START + timedelta(days=10)
    out = resolve_windows_for_args(
        _ns(mode="window", date=grid_date),
        last_processed_provider=provider,
    )
    assert out == [grid_date]
    provider.assert_not_called()


def test_dispatch_latest_calls_provider_and_today() -> None:
    today_date = date(2026, 4, 28)
    target = previous_grid_date(today_date)
    provider = MagicMock(return_value=target - timedelta(days=10))
    out = resolve_windows_for_args(
        _ns(mode="latest"),
        last_processed_provider=provider,
        today_provider=lambda: today_date,
    )
    provider.assert_called_once()
    assert out[-1] == target
    assert len(out) == 2  # 5 days then 10 days back → 2 windows


def test_dispatch_latest_dryrun_does_not_call_provider() -> None:
    """Dry-run latest should resolve via today only (no BQ); spec rule."""
    today_date = date(2026, 4, 28)
    provider = MagicMock(side_effect=AssertionError("provider must not run in dry-run"))
    out = resolve_windows_for_args(
        _ns(mode="latest", dry_run=True),
        last_processed_provider=provider,
        today_provider=lambda: today_date,
    )
    # With provider skipped, last_processed is treated as None → single window.
    assert len(out) == 1
    assert out[0] == previous_grid_date(today_date)


# ── argument parser ──────────────────────────────────────────────────────────

def test_parser_requires_mode() -> None:
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args([])


def test_parser_accepts_backfill_with_dates() -> None:
    parser = build_arg_parser()
    args = parser.parse_args([
        "--mode", "backfill", "--start", "2025-01-01", "--end", "2025-12-31",
    ])
    assert args.mode == "backfill"
    assert args.start.isoformat() == "2025-01-01"
    assert args.end.isoformat() == "2025-12-31"


def test_parser_accepts_window_with_date() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--mode", "window", "--date", "2025-01-03"])
    assert args.date.isoformat() == "2025-01-03"


def test_parser_rejects_invalid_date_format() -> None:
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(["--mode", "window", "--date", "2025/01/03"])


# ── process_window skeleton ──────────────────────────────────────────────────

def test_process_window_skips_unimplemented_steps() -> None:
    grid_date = EPOCH_START + timedelta(days=10)
    outcome = process_window(grid_date, dry_run=False, no_write=False)
    assert isinstance(outcome, WindowOutcome)
    assert outcome.window == grid_date
    # Today the implemented steps still skip (no real wiring yet) — verify
    # the unimplemented ones are reported. Once Steps 4/6 land, this test
    # changes shape.
    assert "extract_gee" in outcome.skipped_steps
    assert "engineer_gold" in outcome.skipped_steps
    assert "write_outputs" in outcome.skipped_steps


def test_process_window_dry_run_does_not_attempt_implemented_steps() -> None:
    grid_date = EPOCH_START + timedelta(days=10)
    outcome = process_window(grid_date, dry_run=True, no_write=False)
    # In dry-run, even implemented steps are logged as planned but not run.
    # We can't assert side effects directly here; the contract is that
    # process_window returns without raising (no MLflow / BQ access).
    assert outcome.window == grid_date
