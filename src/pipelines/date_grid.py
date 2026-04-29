"""Canonical 5-day grid math for the OpenFire pipelines.

The training pipeline emits one feature row per `(latitude, longitude,
window_start_date)` where `window_start_date` falls on a 5-day grid anchored
at `EPOCH_START = 2017-09-01`. The model assumes inputs at exactly these
dates — inference must respect the same grid.

This module is the single source of truth for `EPOCH_START`, `STEP_DAYS`,
and the helpers that reason about the grid. The GEE extractor and the
inference orchestrator both import from here so the two never drift.

Pure-function module — no I/O, no GEE / BQ / GCS dependencies.
"""
from __future__ import annotations

from datetime import date, timedelta


# Training invariants — DO NOT CHANGE. The model and all training data
# assume features are emitted on EPOCH_START + 5n dates.
EPOCH_START: date = date(2017, 9, 1)
STEP_DAYS: int = 5


def is_grid_date(d: date) -> bool:
    """True iff `d` falls on the 5-day grid anchored at EPOCH_START."""
    return d >= EPOCH_START and (d - EPOCH_START).days % STEP_DAYS == 0


def _snap_forward(d: date) -> date:
    """Smallest grid date >= d (clamped at EPOCH_START)."""
    if d <= EPOCH_START:
        return EPOCH_START
    days = (d - EPOCH_START).days
    if days % STEP_DAYS == 0:
        return d
    next_step = (days // STEP_DAYS) + 1
    return EPOCH_START + timedelta(days=next_step * STEP_DAYS)


def next_grid_date(after: date) -> date:
    """Smallest grid date strictly greater than `after`.

    If `after` is on the grid, returns `after + STEP_DAYS`. If `after` is
    before EPOCH_START, returns EPOCH_START.
    """
    return _snap_forward(after + timedelta(days=1))


def list_grid_dates(start: date, end: date) -> list[date]:
    """All grid dates in [start, end] inclusive.

    Returns an empty list if `end < start`, if both endpoints are before
    EPOCH_START, or if no grid date falls in the range.
    """
    if end < start:
        return []
    cursor = _snap_forward(start)
    out: list[date] = []
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=STEP_DAYS)
    return out


# ── Convenience wrappers used by the GEE extractor ───────────

def generate_date_list(
    epoch: date, run_start: date, run_end: date, step_days: int
) -> list[str]:
    """ISO-string variant of list_grid_dates with explicit epoch / step args.

    Kept for the GEE extractor, which passes the strings straight to
    `ee.batch.Export.table.toBigQuery` task descriptions. `epoch` and
    `step_days` are accepted as args (rather than read from the module
    constants) to keep the function pure and to preserve the legacy
    signature consumers were calling.
    """
    if step_days != STEP_DAYS or epoch != EPOCH_START:
        # Fallback to the explicit-args calculation for non-default callers.
        days_offset = (run_start - epoch).days
        first_step = (days_offset + step_days - 1) // step_days
        cursor = epoch + timedelta(days=first_step * step_days)
        out: list[str] = []
        while cursor <= run_end:
            out.append(cursor.isoformat())
            cursor += timedelta(days=step_days)
        return out
    return [d.isoformat() for d in list_grid_dates(run_start, run_end)]


def years_in_range(
    epoch: date, run_start: date, run_end: date, step_days: int
) -> list[int]:
    """Sorted set of calendar years touched by the run window's grid dates."""
    if step_days != STEP_DAYS or epoch != EPOCH_START:
        # Fallback — legacy signature with explicit args.
        return sorted({
            d.year
            for d in (
                epoch + timedelta(days=i * step_days)
                for i in range(((run_end - epoch).days // step_days) + 1)
            )
            if run_start <= d <= run_end
        })
    return sorted({d.year for d in list_grid_dates(run_start, run_end)})
