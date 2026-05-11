"""Lightweight CI tests for the feature-engineering artifact contract.

These tests are intentionally offline:
- pure file I/O via `pathlib.Path`
- no Google Cloud or BigQuery clients
- no network access
- no Python dependencies beyond the standard library + pytest

They protect three things:
1. The expected exploration SQL and doc artifacts exist on disk.
2. The 08 interaction-feature SQL still defines the candidate features
   the rest of the team (and the docs) reference by name.
3. The 08 SQL still references the real Silver-layer column names
   (`mean_NDVI`, `gridmet_temp_max`, etc.), so the query keeps running
   when copy-pasted into BigQuery.
4. The exploration docs explain the medallion layers and the leakage
   rule so a new teammate can find the rationale.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPLORATION_SQL_DIR = REPO_ROOT / "sql" / "exploration"
DOCS_EXPLORATION_DIR = REPO_ROOT / "docs" / "exploration"

SQL_08_PATH = EXPLORATION_SQL_DIR / "08_interaction_feature_candidates.sql"

REQUIRED_ARTIFACTS: tuple[Path, ...] = (
    EXPLORATION_SQL_DIR / "07_silver_feature_inventory.sql",
    EXPLORATION_SQL_DIR / "07_5_fire_date_temporal_check.sql",
    EXPLORATION_SQL_DIR / "08_interaction_feature_candidates.sql",
    DOCS_EXPLORATION_DIR / "07_silver_feature_inventory.md",
    DOCS_EXPLORATION_DIR / "07_5_fire_date_temporal_check.md",
    DOCS_EXPLORATION_DIR / "08_interaction_feature_candidates.md",
)

EXPECTED_FEATURE_NAMES: tuple[str, ...] = (
    "weather_stress_score",
    "fuel_load_score",
    "fuel_x_weather_stress",
    "vegetation_dryness_score",
    "prior_fire_count_10yr",
    "days_since_last_burn_candidate",
    "prior_fire_x_weather_stress",
    "ndvi_change_from_prev",
    "burned_in_next_15_days_candidate",
)

EXPECTED_SILVER_COLUMNS: tuple[str, ...] = (
    "mean_NDVI",
    "mean_EVI",
    "mean_NDWI",
    "mean_NBR",
    "gridmet_temp_max",
    "gridmet_humidity_min",
    "gridmet_precip_sum",
    "gridmet_wind_max",
    "mean_elevation",
    "mean_slope",
)

DOC_REQUIRED_TERMS: tuple[str, ...] = (
    "Bronze",
    "Silver",
    "Gold",
    "leakage",
    "fire_date < window_start_date",
)


def test_required_feature_exploration_artifacts_exist() -> None:
    """Every documented exploration SQL / doc artifact must exist on
    disk. If a file gets renamed or deleted, this test pins it down."""
    missing: list[str] = []
    for path in REQUIRED_ARTIFACTS:
        if not path.is_file():
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "Missing required feature exploration artifacts: "
        + ", ".join(missing)
    )


def _read_sql_08() -> str:
    if not SQL_08_PATH.is_file():
        pytest.fail(f"Missing 08 SQL at {SQL_08_PATH}")
    return SQL_08_PATH.read_text(encoding="utf-8")


def test_08_contains_expected_feature_candidates() -> None:
    """The 08 SQL must define each documented feature candidate so that
    rest-of-team docs that reference these names by string keep working."""
    sql = _read_sql_08()
    missing = [name for name in EXPECTED_FEATURE_NAMES if name not in sql]
    assert not missing, (
        "08 SQL is missing expected feature candidate column names: "
        + ", ".join(missing)
    )


def test_08_uses_real_silver_column_names() -> None:
    """The 08 SQL must reference the actual Silver-layer column names
    documented in `docs/exploration/07_silver_feature_inventory.md` so
    a copy-paste into BigQuery doesn't immediately fail."""
    sql = _read_sql_08()
    missing = [col for col in EXPECTED_SILVER_COLUMNS if col not in sql]
    assert not missing, (
        "08 SQL is missing references to expected Silver-layer columns: "
        + ", ".join(missing)
    )


def _combined_exploration_doc_text() -> str:
    if not DOCS_EXPLORATION_DIR.is_dir():
        pytest.fail(
            f"Expected exploration docs directory at {DOCS_EXPLORATION_DIR}"
        )
    docs = sorted(DOCS_EXPLORATION_DIR.glob("*.md"))
    if not docs:
        pytest.fail(
            f"No exploration markdown docs found under {DOCS_EXPLORATION_DIR}"
        )
    return "\n\n".join(p.read_text(encoding="utf-8") for p in docs)


def test_docs_explain_medallion_and_leakage_rules() -> None:
    """The combined exploration docs corpus must explain the Bronze /
    Silver / Gold layers, the word 'leakage', and the safe past-only
    filter `fire_date < window_start_date`."""
    corpus = _combined_exploration_doc_text()
    missing = [term for term in DOC_REQUIRED_TERMS if term not in corpus]
    assert not missing, (
        "Exploration docs corpus is missing expected terms: "
        + ", ".join(missing)
    )
