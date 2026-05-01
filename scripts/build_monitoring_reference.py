"""Build the Evidently monitoring reference dataset.

One-shot script. Loads the gold Parquet shards from GCS, filters to the
training years (2017–2023 by default), takes a stratified sample, and writes
the result as a single Parquet file to:

    gs://openfire/monitoring/reference/gold_features_train_2017_2023.parquet

Stratification preserves the joint (year, burned_label) distribution exactly,
so feature distributions in the sample match the training distribution. We do
NOT oversample positives — that would bias the reference's *feature*
distribution (positives have systematically different features) and would
cause Evidently to flag spurious data drift on every inference window.

`risk_probability` is intentionally not included here: prediction drift is
computed in `src/pipelines/monitor.py` against on-the-fly scores from the
current Production bundle, so reference predictions stay aligned with the
deployed model without rebuilding this artifact on every promotion.

Usage:
    python -m scripts.build_monitoring_reference
    python -m scripts.build_monitoring_reference --sample-size 250000
    python -m scripts.build_monitoring_reference --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Iterable

import pandas as pd

from src.common.storage import StorageClient
from src.pipelines.train import (
    FEATURE_COLUMNS,
    GCP_PROJECT,
    GOLD_GCS_PREFIX,
    TARGET_COLUMN,
    load_gold_shards,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_OUTPUT_URI = "gs://openfire/monitoring/reference/gold_features_train_2017_2023.parquet"
DEFAULT_TRAIN_YEARS = (2017, 2018, 2019, 2020, 2021, 2022, 2023)
DEFAULT_SAMPLE_SIZE = 500_000
DEFAULT_RANDOM_STATE = 42


def stratified_sample(
    df: pd.DataFrame,
    *,
    target_size: int,
    random_state: int,
) -> pd.DataFrame:
    """Joint (year, label) stratified sample preserving original proportions.

    Computes a single shared sampling fraction p = target_size / len(df), then
    pulls p% from each stratum. Strata smaller than the resulting per-stratum
    floor are kept whole. The output's (year, label) marginals match the
    input's to within rounding.
    """
    if len(df) <= target_size:
        return df.copy()

    frac = target_size / len(df)
    LOGGER.info("Sampling fraction: %.6f (%s of %s rows)", frac, f"{target_size:,}", f"{len(df):,}")

    years = pd.to_datetime(df["window_start_date"]).dt.year
    strata_key = years.astype(str) + "_" + df[TARGET_COLUMN].astype(int).astype(str)
    pieces: list[pd.DataFrame] = []
    for key, group in df.groupby(strata_key, sort=True):
        n = max(1, round(len(group) * frac))
        n = min(n, len(group))
        pieces.append(group.sample(n=n, random_state=random_state))
        LOGGER.debug("  stratum %s: %s → %s rows", key, f"{len(group):,}", f"{n:,}")
    sampled = pd.concat(pieces, ignore_index=True)
    sampled = sampled.sample(frac=1.0, random_state=random_state).reset_index(drop=True)
    return sampled


def summarize(df: pd.DataFrame) -> dict:
    years = pd.to_datetime(df["window_start_date"]).dt.year
    by_year = years.value_counts().sort_index().to_dict()
    pos = int(df[TARGET_COLUMN].astype(int).sum())
    neg = len(df) - pos
    return {
        "rows": int(len(df)),
        "columns": int(len(df.columns)),
        "positives": pos,
        "negatives": neg,
        "positive_rate": float(pos / len(df)) if len(df) else 0.0,
        "rows_by_year": {str(int(k)): int(v) for k, v in by_year.items()},
    }


def build(
    *,
    parquet_prefix: str,
    output_uri: str,
    train_years: Iterable[int],
    sample_size: int,
    random_state: int,
    gcp_project: str,
    dry_run: bool,
) -> dict:
    train_year_set = set(int(y) for y in train_years)
    needed_cols = FEATURE_COLUMNS + [TARGET_COLUMN, "window_start_date"]

    if dry_run:
        LOGGER.info("[DRY RUN] Would load gold shards from: %s", parquet_prefix)
        LOGGER.info("[DRY RUN] Would filter to years: %s", sorted(train_year_set))
        LOGGER.info("[DRY RUN] Would stratify-sample to: %s rows", f"{sample_size:,}")
        LOGGER.info("[DRY RUN] Would write to: %s", output_uri)
        return {"dry_run": True, "output_uri": output_uri, "sample_size": sample_size}

    LOGGER.info("Loading gold shards from %s", parquet_prefix)
    df = load_gold_shards(
        parquet_prefix,
        project=gcp_project,
        columns=needed_cols,
        float32_features=True,
    )

    years = pd.to_datetime(df["window_start_date"]).dt.year
    df = df.loc[years.isin(train_year_set)].reset_index(drop=True)
    LOGGER.info("Filtered to train years %s: %s rows", sorted(train_year_set), f"{len(df):,}")
    if df.empty:
        raise RuntimeError(f"No gold rows fell within train years {sorted(train_year_set)}")

    sampled = stratified_sample(df, target_size=sample_size, random_state=random_state)
    summary = summarize(sampled)
    LOGGER.info(
        "Sampled %s rows (%s positives, %.4f%% positive rate)",
        f"{summary['rows']:,}",
        f"{summary['positives']:,}",
        summary["positive_rate"] * 100.0,
    )

    LOGGER.info("Writing reference to %s", output_uri)
    storage = StorageClient(gcp_project_id=gcp_project)
    storage.write_parquet(sampled, output_uri, compression="snappy", index=False)

    return {
        "output_uri": output_uri,
        "random_state": random_state,
        "train_years": sorted(train_year_set),
        "source_prefix": parquet_prefix,
        **summary,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--parquet-prefix", default=GOLD_GCS_PREFIX, help="GCS prefix or local dir of gold Parquet shards")
    parser.add_argument("--output-uri", default=DEFAULT_OUTPUT_URI, help="GCS URI for the reference Parquet file")
    parser.add_argument(
        "--train-years",
        type=int,
        nargs="+",
        default=list(DEFAULT_TRAIN_YEARS),
        help="Years to include in the reference (default: 2017–2023)",
    )
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--random-state", type=int, default=DEFAULT_RANDOM_STATE)
    parser.add_argument("--gcp-project", default=GCP_PROJECT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        result = build(
            parquet_prefix=args.parquet_prefix,
            output_uri=args.output_uri,
            train_years=args.train_years,
            sample_size=args.sample_size,
            random_state=args.random_state,
            gcp_project=args.gcp_project,
            dry_run=args.dry_run,
        )
    except Exception as error:
        LOGGER.exception("Reference build failed")
        raise SystemExit(str(error)) from error

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
