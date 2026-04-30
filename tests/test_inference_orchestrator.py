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
    WindowContext,
    WindowOutcome,
    _step_append_silver,
    _step_engineer_gold,
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


# ── process_window step wiring ───────────────────────────────────────────────

def test_process_window_all_steps_wired_dry_run() -> None:
    """In dry-run all steps are logged as [DRY]; none end up in skipped_steps."""
    grid_date = EPOCH_START + timedelta(days=10)
    outcome = process_window(grid_date, dry_run=True, no_write=False)
    assert isinstance(outcome, WindowOutcome)
    assert outcome.window == grid_date
    assert outcome.skipped_steps == []


def test_engineer_gold_step_requires_bq_client() -> None:
    """_step_engineer_gold raises RuntimeError when bq_client is None."""
    ctx = WindowContext(
        window=EPOCH_START + timedelta(days=10),
        bq_client=None,
        dry_run=False,
        no_write=False,
    )
    with pytest.raises(RuntimeError, match="requires a BigQuery client"):
        _step_engineer_gold(ctx)


def test_append_silver_step_requires_bq_client() -> None:
    """_step_append_silver raises RuntimeError when bq_client is None."""
    ctx = WindowContext(
        window=EPOCH_START + timedelta(days=10),
        bq_client=None,
        dry_run=False,
        no_write=False,
    )
    with pytest.raises(RuntimeError, match="requires a BigQuery client"):
        _step_append_silver(ctx)


def test_process_window_skips_remaining_steps_when_skip_reason_set(monkeypatch) -> None:
    """When a step sets ctx.skip_reason, subsequent steps must not run."""
    import pipelines.run_inference_pipeline as orch
    from pipelines.run_inference_pipeline import WindowStep

    ran: list[str] = []

    def abort_step(ctx):
        ran.append("extract_gee")
        ctx.skip_reason = "data not ready — test"

    patched = [
        WindowStep("extract_gee",   "...", func=abort_step),
        WindowStep("append_silver", "...", func=lambda ctx: ran.append("append_silver")),
        WindowStep("engineer_gold", "...", func=lambda ctx: ran.append("engineer_gold")),
        WindowStep("load_model",    "...", func=lambda ctx: ran.append("load_model")),
        WindowStep("predict",       "...", func=lambda ctx: ran.append("predict")),
        WindowStep("write_outputs", "...", func=lambda ctx: ran.append("write_outputs"), is_write_step=True),
        WindowStep("log_summary",   "...", func=lambda ctx: ran.append("log_summary")),
    ]
    monkeypatch.setattr(orch, "WINDOW_STEPS", patched)

    grid_date = EPOCH_START + timedelta(days=10)
    outcome = process_window(grid_date, dry_run=False, no_write=False, bq_client=MagicMock())

    assert ran == ["extract_gee"]
    assert outcome.skip_reason == "data not ready — test"


def test_process_window_write_steps_skipped_with_no_write(monkeypatch) -> None:
    # Use dry_run=False + no_write=True to exercise the actual SKIP path.
    # WINDOW_STEPS holds baked function references, so patch the list itself
    # with no-op versions of every non-write step.
    import pipelines.run_inference_pipeline as orch
    from pipelines.run_inference_pipeline import WindowStep

    noop = lambda ctx: None  # noqa: E731
    patched_steps = [
        WindowStep(s.name, s.description,
                   func=(noop if (s.func is not None and not s.is_write_step) else s.func),
                   is_write_step=s.is_write_step)
        for s in orch.WINDOW_STEPS
    ]
    monkeypatch.setattr(orch, "WINDOW_STEPS", patched_steps)

    grid_date = EPOCH_START + timedelta(days=10)
    outcome = process_window(grid_date, dry_run=False, no_write=True, bq_client=MagicMock())
    assert "write_outputs" in outcome.skipped_steps


# ── --from-step / --to-step (step filtering) ─────────────────────────────────

def test_select_steps_full_range_when_no_args() -> None:
    from pipelines.run_inference_pipeline import WINDOW_STEPS, select_steps
    assert select_steps(None, None) == WINDOW_STEPS


def test_select_steps_from_step_skips_earlier() -> None:
    from pipelines.run_inference_pipeline import select_steps
    selected = select_steps("engineer_gold", None)
    names = [s.name for s in selected]
    assert names[0] == "engineer_gold"
    assert "extract_gee" not in names
    assert "append_silver" not in names
    assert names[-1] == "log_summary"


def test_select_steps_to_step_truncates_later() -> None:
    from pipelines.run_inference_pipeline import select_steps
    selected = select_steps(None, "predict")
    names = [s.name for s in selected]
    assert names[0] == "extract_gee"
    assert names[-1] == "predict"
    assert "write_outputs" not in names


def test_validate_args_rejects_inverted_step_range() -> None:
    from pipelines.run_inference_pipeline import _validate_args
    parser = build_arg_parser()
    args = parser.parse_args(["--mode", "latest", "--from-step", "predict", "--to-step", "extract_gee"])
    with pytest.raises(SystemExit):
        _validate_args(args)


def test_process_window_runs_only_selected_steps(monkeypatch) -> None:
    """Smoke: process_window with a sliced steps list runs only those steps."""
    import pipelines.run_inference_pipeline as orch
    from pipelines.run_inference_pipeline import WindowStep, select_steps

    ran: list[str] = []
    patched = [
        WindowStep(s.name, s.description, func=(lambda ctx, n=s.name: ran.append(n)), is_write_step=s.is_write_step)
        for s in orch.WINDOW_STEPS
    ]
    monkeypatch.setattr(orch, "WINDOW_STEPS", patched)
    sliced = select_steps("engineer_gold", "predict")
    # select_steps reads from the patched WINDOW_STEPS, so re-slice from it
    sliced = patched[2:5]  # engineer_gold, load_model, predict

    grid_date = EPOCH_START + timedelta(days=10)
    process_window(grid_date, dry_run=False, no_write=False, bq_client=MagicMock(), steps=sliced)

    assert ran == ["engineer_gold", "load_model", "predict"]
