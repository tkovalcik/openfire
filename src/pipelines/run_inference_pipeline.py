"""Orchestrate the OpenFire inference pipeline.

Three modes (one orchestrator, one CLI):

- ``backfill --start YYYY-MM-DD --end YYYY-MM-DD``
    Process every 5-day grid window in [start, end]. Used for initial 2025+
    backfill and operator-driven re-runs over a historical range.

- ``latest``
    Self-determining. Queries ``MAX(window_start_date)`` from
    ``predictions_history``; processes every grid window in
    ``(last_processed, last_grid_date_strictly_before(today_utc())]``. If the
    table is empty (first ever run), processes only the most recent grid
    date — does NOT auto-trigger a full backfill from EPOCH_START. If
    ``last_processed >= target``, no work to do; logs and exits cleanly.
    This is the mode Cloud Scheduler will fire daily once Step 8 is wired.

- ``window --date YYYY-MM-DD``
    Process exactly one window. Errors out if the date isn't on the 5-day grid.

Per-window flow:

    1. extract_gee   — TODO: call GEE Python API to pull Sentinel-2 + gridMET
                       for the window and stage into a BQ import table.
                       Until this is wired, the pipeline requires silver tables
                       to already contain rows for the requested window (i.e.
                       pre-extracted via data_pipelines/03b). This means new
                       windows (2026-present) cannot be processed until this
                       step is implemented. Tracked as a known gap.
    2. append_silver — TODO: MERGE staged import rows into silver_features_<year>.
                       Depends on step 1.
    3. engineer_gold — MERGE engineered features into gold_features_inference.
    4. load_model    — fetch openfire-gold Production bundle from MLflow.
    5. predict       — read gold_features_inference for the window, score rows.
    6. write_outputs — BQ partition-scoped truncate, GeoJSON snapshot, manifest.
    7. log_summary   — row counts, runtime, model version.

Provenance / safety guarantees this skeleton already enforces:
- Mode resolution is timezone-correct (UTC; matches training's date semantics).
- Windows are always grid-aligned via src.pipelines.date_grid.
- Dry-run never opens a BQ session, never loads the model, never calls GEE.
- ``--no-write`` runs everything *except* the BQ/GCS writes (useful once
  Step 6 wires those in; today it's a no-op since writes aren't implemented).
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable

from .date_grid import (
    EPOCH_START,
    is_grid_date,
    list_grid_dates,
    next_grid_date,
    previous_grid_date,
)


LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
PREDICTIONS_TABLE = f"{PROJECT}.{DATASET}.predictions_history"


# ── mode resolution ──────────────────────────────────────────────────────────

def today_utc() -> date:
    """UTC current date. Centralized so tests can monkeypatch."""
    return datetime.now(timezone.utc).date()


def resolve_windows_backfill(start: date, end: date) -> list[date]:
    """Backfill mode: every grid window in [start, end]."""
    return list_grid_dates(start, end)


def resolve_windows_window(target: date) -> list[date]:
    """Window mode: exactly one date; errors if off-grid."""
    if not is_grid_date(target):
        raise ValueError(
            f"--date {target.isoformat()} is not on the 5-day grid "
            f"anchored at {EPOCH_START.isoformat()}."
        )
    return [target]


def resolve_windows_latest(
    *,
    last_processed: date | None,
    today: date | None = None,
) -> list[date]:
    """Latest mode: self-determining catch-up.

    Returns grid dates in (last_processed, target] where ``target`` is the
    largest grid date strictly before ``today`` (UTC). If
    ``last_processed >= target``, returns an empty list (no work to do). If
    ``last_processed`` is ``None`` (first ever run), returns ``[target]`` —
    a single window — to avoid an accidental full-history backfill.
    """
    eff_today = today or today_utc()
    target = previous_grid_date(eff_today)

    if last_processed is None:
        return [target]
    if last_processed >= target:
        return []
    # All grid dates strictly after last_processed, up to and including target.
    return list_grid_dates(next_grid_date(last_processed), target)


# ── per-window flow ──────────────────────────────────────────────────────────

@dataclass
class WindowContext:
    """Shared per-window state passed into each step's func."""
    window: date
    bq_client: object | None  # bigquery.Client; lazy import keeps tests light
    dry_run: bool
    no_write: bool
    rows_engineered: int = 0
    rows_predicted: int = 0
    loaded_model: object | None = None   # serving.model_loader.LoadedModel
    predictions: object | None = None   # pd.DataFrame in predictions_history schema


@dataclass(frozen=True)
class WindowStep:
    name: str
    description: str
    # None ⇒ unimplemented stub (logged as TODO). Otherwise mutates ctx in place.
    func: object | None = None
    # If True, this step is skipped when --no-write is set.
    is_write_step: bool = False


def _step_engineer_gold(ctx: WindowContext) -> None:
    from .engineer_gold import ensure_inference_tables, engineer_inference_gold

    if ctx.bq_client is None:
        raise RuntimeError(
            "engineer_gold step requires a BigQuery client; pass one through "
            "process_window(bq_client=...) or run with --dry-run."
        )
    ensure_inference_tables(ctx.bq_client)
    ctx.rows_engineered = engineer_inference_gold(
        ctx.bq_client, target_date=ctx.window
    )


def _step_load_model(ctx: WindowContext) -> None:
    from .predict import load_production_bundle
    ctx.loaded_model = load_production_bundle()
    LOGGER.info(
        "  loaded model %s version=%s threshold=%.3f",
        ctx.loaded_model.model_source,
        ctx.loaded_model.model_version,
        ctx.loaded_model.decision_threshold,
    )


def _step_predict(ctx: WindowContext) -> None:
    from google.cloud import bigquery

    import pandas as pd

    from .engineer_gold import INFERENCE_GOLD_TABLE
    from .predict import predict_window

    sql = f"""
        SELECT *
        FROM `{INFERENCE_GOLD_TABLE}`
        WHERE window_start_date = @target_date
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("target_date", "DATE", ctx.window),
        ]
    )
    features: pd.DataFrame = ctx.bq_client.query(sql, job_config=job_config).to_dataframe()
    LOGGER.info("  read %s gold feature rows for window %s", f"{len(features):,}", ctx.window.isoformat())

    ctx.predictions = predict_window(features, loaded_model=ctx.loaded_model)
    ctx.rows_predicted = len(ctx.predictions)


def _step_write_outputs(ctx: WindowContext) -> None:
    from datetime import datetime, timezone

    from common.storage import StorageClient

    from .output_writer import update_manifest, write_geojson_snapshot, write_predictions_to_bq

    run_at = datetime.now(timezone.utc)
    storage = StorageClient(gcp_project_id=PROJECT)

    write_predictions_to_bq(ctx.bq_client, ctx.predictions, target_date=ctx.window)
    geojson_uri = write_geojson_snapshot(ctx.predictions, target_date=ctx.window, storage=storage)
    update_manifest(
        latest_window_start_date=ctx.window,
        latest_geojson_uri=geojson_uri,
        model_version=ctx.loaded_model.model_version,
        updated_at=run_at,
        storage=storage,
    )


def _step_log_summary(ctx: WindowContext) -> None:
    LOGGER.info(
        "  window=%s rows_engineered=%s rows_predicted=%s",
        ctx.window.isoformat(),
        f"{ctx.rows_engineered:,}",
        f"{ctx.rows_predicted:,}",
    )


WINDOW_STEPS: list[WindowStep] = [
    WindowStep("extract_gee",   "Extract GEE features for the window",             func=None),
    WindowStep("append_silver", "Append the window row to silver_features_<year>", func=None),
    WindowStep("engineer_gold", "MERGE engineered features into gold_features_inference", func=_step_engineer_gold),
    WindowStep("load_model",    "Load openfire-gold Production from MLflow",       func=_step_load_model),
    WindowStep("predict",       "Score the window's gold features",                func=_step_predict),
    WindowStep("write_outputs", "BQ partition truncate + GeoJSON snapshot + manifest", func=_step_write_outputs, is_write_step=True),
    WindowStep("log_summary",   "Log row counts, runtime, model version",          func=_step_log_summary),
]


@dataclass
class WindowOutcome:
    window: date
    rows_in: int
    rows_predicted: int
    skipped_steps: list[str]


def process_window(
    window: date,
    *,
    dry_run: bool,
    no_write: bool,
    bq_client: object | None = None,
) -> WindowOutcome:
    """Run the per-window pipeline. Steps with no implementation are logged as TODO."""
    skipped: list[str] = []
    ctx = WindowContext(
        window=window,
        bq_client=bq_client,
        dry_run=dry_run,
        no_write=no_write,
    )
    LOGGER.info("=== window %s ===", window.isoformat())
    for step in WINDOW_STEPS:
        if step.func is None:
            LOGGER.info("  [TODO ] %s — %s", step.name, step.description)
            skipped.append(step.name)
            continue
        if dry_run:
            LOGGER.info("  [DRY  ] %s — %s", step.name, step.description)
            continue
        if no_write and step.is_write_step:
            LOGGER.info("  [SKIP ] %s — --no-write set", step.name)
            skipped.append(step.name)
            continue
        LOGGER.info("  [RUN  ] %s — %s", step.name, step.description)
        step.func(ctx)
    return WindowOutcome(
        window=window,
        rows_in=ctx.rows_engineered,
        rows_predicted=ctx.rows_predicted,
        skipped_steps=skipped,
    )


# ── orchestrator entry point ─────────────────────────────────────────────────

def resolve_windows_for_args(
    args: argparse.Namespace,
    *,
    last_processed_provider: Callable[[], date | None] | None = None,
    today_provider: Callable[[], date] | None = None,
) -> list[date]:
    """Dispatch to the right mode-resolver based on parsed args."""
    if args.mode == "backfill":
        return resolve_windows_backfill(args.start, args.end)
    if args.mode == "window":
        return resolve_windows_window(args.date)
    if args.mode == "latest":
        provider = last_processed_provider or _default_last_processed_provider
        last = None if args.dry_run else provider()
        today = (today_provider or today_utc)()
        return resolve_windows_latest(last_processed=last, today=today)
    raise ValueError(f"Unknown mode: {args.mode!r}")


def _default_last_processed_provider() -> date | None:
    """Query MAX(window_start_date) FROM predictions_history.

    Returns None if the table doesn't exist yet (first ever run). Imported
    lazily so dry-run / mode resolution doesn't pull in google-cloud-bigquery.
    """
    from google.cloud import bigquery
    from google.cloud.exceptions import NotFound

    client = bigquery.Client(project=PROJECT)
    try:
        client.get_table(PREDICTIONS_TABLE)
    except NotFound:
        LOGGER.info(
            "predictions_history not found — treating as first-ever run; latest mode "
            "will process only the most recent grid date."
        )
        return None
    job = client.query(
        f"SELECT MAX(window_start_date) AS max_d FROM `{PREDICTIONS_TABLE}`"
    )
    row = next(iter(job.result()))
    return row.max_d  # python date or None


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("backfill", "latest", "window"),
        help="Which set of windows to process.",
    )
    parser.add_argument("--start", type=parse_iso_date, help="backfill: window range start (YYYY-MM-DD).")
    parser.add_argument("--end",   type=parse_iso_date, help="backfill: window range end (YYYY-MM-DD).")
    parser.add_argument("--date",  type=parse_iso_date, help="window: single grid date (YYYY-MM-DD).")
    parser.add_argument("--dry-run", action="store_true", help="Print plan; touch no external service.")
    parser.add_argument("--no-write", action="store_true", help="Run everything except BQ/GCS writes.")
    parser.add_argument("--project", default=PROJECT, help=f"GCP project (default: {PROJECT}).")
    parser.add_argument("--log-level", default="INFO")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.mode == "backfill":
        if args.start is None or args.end is None:
            raise SystemExit("--start and --end are required for --mode backfill.")
        if args.end < args.start:
            raise SystemExit(f"--end ({args.end}) must be >= --start ({args.start}).")
    elif args.mode == "window":
        if args.date is None:
            raise SystemExit("--date is required for --mode window.")
    elif args.mode == "latest":
        if any(v is not None for v in (args.start, args.end, args.date)):
            raise SystemExit("--start/--end/--date are not valid for --mode latest.")


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    _validate_args(args)

    windows = resolve_windows_for_args(args)
    LOGGER.info(
        "mode=%s resolved %d window(s)%s",
        args.mode,
        len(windows),
        f": {windows[0].isoformat()}…{windows[-1].isoformat()}" if windows else "",
    )
    if not windows:
        LOGGER.info("Nothing to do; exiting cleanly.")
        return

    if args.dry_run:
        for w in windows:
            LOGGER.info("[DRY RUN] would process window %s", w.isoformat())
        return

    from .bq_utils import get_client
    bq_client = get_client(args.project)
    outcomes = [
        process_window(w, dry_run=False, no_write=args.no_write, bq_client=bq_client)
        for w in windows
    ]
    LOGGER.info("Inference complete. Windows processed: %d.", len(outcomes))


if __name__ == "__main__":
    main()
