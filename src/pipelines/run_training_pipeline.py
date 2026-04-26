"""Orchestrate the OpenFire training data pipeline.

Steps 04-06 (BigQuery SQL) run by default. Steps 01-03b are GEE-side ingestion
that have already been run; they're registered but skipped unless you pass
--include-gee-steps. Phase 4 inference will extend this to automate fresh
Sentinel-2 + gridMET fetches.

Usage:
    python -m src.pipelines.run_training_pipeline                    # runs 04 -> 06
    python -m src.pipelines.run_training_pipeline --from-step 05     # runs 05 -> 06
    python -m src.pipelines.run_training_pipeline --to-step 05       # runs 04 -> 05
    python -m src.pipelines.run_training_pipeline --dry-run          # print plan only

Step names accept either the prefix ("04") or the full name ("04_merge_silver").

Project/dataset are hardcoded constants below. Promote to env vars if/when a
second environment (staging, CI) is needed.
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from .bq_utils import exec_sql_file, get_client, get_row_count, table_exists


LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
PIPELINE_ROOT = Path(__file__).parents[2] / "data_pipelines"


@dataclass(frozen=True)
class Step:
    name: str  # "04_merge_silver"
    description: str
    sql_file: str | None  # filename in data_pipelines/, or None for non-SQL steps
    expected_table: str | None  # BQ table to verify after the step, or None
    default_skip: bool = False
    skip_reason: str = ""

    @property
    def prefix(self) -> str:
        # "04_merge_silver" -> "04"
        return self.name.split("_", 1)[0]


STEPS: list[Step] = [
    Step(
        name="01_stage_calfire",
        description="Stage CalFire FRAP data (Python)",
        sql_file=None,
        expected_table=None,
        default_skip=True,
        skip_reason="Pre-existing GEE asset; run manually if regenerating.",
    ),
    Step(
        name="02_ingest_calfire_gee",
        description="Ingest CalFire labels into GEE (Python)",
        sql_file=None,
        expected_table=None,
        default_skip=True,
        skip_reason="Pre-existing GEE asset; run manually if regenerating.",
    ),
    Step(
        name="03_extract_gee_features",
        description="Extract GEE features (JavaScript, runs in GEE Code Editor)",
        sql_file=None,
        expected_table=None,
        default_skip=True,
        skip_reason="JavaScript script for the GEE Code Editor — not orchestratable from Python.",
    ),
    Step(
        name="03b_extract_gee_land_weather",
        description="Extract GEE land + weather features into BQ silver tables (Python)",
        sql_file=None,
        expected_table=None,
        default_skip=True,
        skip_reason="Long-running interactive GEE export; pre-existing silver tables.",
    ),
    Step(
        name="04_merge_silver",
        description="Merge yearly silver tables into silver_features_all_years",
        sql_file="04_merge_silver_years.sql",
        expected_table=f"{PROJECT}.{DATASET}.silver_features_all_years",
    ),
    Step(
        name="05_engineer_gold",
        description="Engineer gold features (target + lag deltas + days_since_last_burn)",
        sql_file="05_engineer_gold_features.sql",
        expected_table=f"{PROJECT}.{DATASET}.gold_features",
    ),
    Step(
        name="06_export_gcs",
        description="Export gold table to GCS as Parquet shards",
        sql_file="06_export_gold_to_gcs.sql",
        expected_table=None,  # exports to GCS, not BQ
    ),
]


def resolve_step_index(identifier: str) -> int:
    """Look up a step by full name or by numeric prefix ("04" or "04_merge_silver")."""
    for i, step in enumerate(STEPS):
        if step.name == identifier or step.prefix == identifier:
            return i
    valid = ", ".join(f"{s.prefix} ({s.name})" for s in STEPS)
    raise ValueError(f"Unknown step '{identifier}'. Valid: {valid}")


def run_step(step: Step, *, client, dry_run: bool) -> None:
    if step.sql_file is None:
        LOGGER.warning("Step %s has no executable SQL file; skipping.", step.name)
        return

    sql_path = PIPELINE_ROOT / step.sql_file
    if dry_run:
        LOGGER.info("[DRY RUN] Would execute %s", sql_path)
        return

    exec_sql_file(client, sql_path)

    if step.expected_table:
        if not table_exists(client, step.expected_table):
            raise RuntimeError(
                f"Step {step.name} completed but expected table not found: {step.expected_table}"
            )
        rows = get_row_count(client, step.expected_table)
        LOGGER.info("Step %s -> %s rows in %s", step.name, f"{rows:,}", step.expected_table)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from-step", default="04", help="First step to run (default: 04)")
    parser.add_argument("--to-step", default="06", help="Last step to run, inclusive (default: 06)")
    parser.add_argument(
        "--include-gee-steps",
        action="store_true",
        help="Run steps 01-03b too (default: skip; they're already done)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without executing any SQL",
    )
    parser.add_argument("--project", default=PROJECT, help=f"GCP project (default: {PROJECT})")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    start_idx = resolve_step_index(args.from_step)
    end_idx = resolve_step_index(args.to_step)
    if start_idx > end_idx:
        raise SystemExit(
            f"--from-step ({args.from_step}) comes after --to-step ({args.to_step})"
        )

    selected = STEPS[start_idx:end_idx + 1]
    plan = [step for step in selected if not (step.default_skip and not args.include_gee_steps)]

    LOGGER.info("Pipeline plan:")
    for step in selected:
        will_run = step in plan
        marker = "RUN " if will_run else "SKIP"
        LOGGER.info("  [%s] %s — %s", marker, step.name, step.description)
        if not will_run:
            LOGGER.info("        reason: %s", step.skip_reason)

    if args.dry_run:
        LOGGER.info("Dry run complete; no SQL executed.")
        return

    if not plan:
        LOGGER.warning("Nothing to run. Use --include-gee-steps if you meant to run 01-03b.")
        return

    client = get_client(args.project)
    for step in plan:
        LOGGER.info("=== %s: %s ===", step.name, step.description)
        run_step(step, client=client, dry_run=False)
    LOGGER.info("Pipeline complete.")


if __name__ == "__main__":
    main()
