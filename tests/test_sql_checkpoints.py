"""Lightweight tests for SQL checkpoint files.

These tests are intentionally offline: they only read the SQL files from
disk and check basic shape/safety. They do NOT connect to BigQuery and do
not require GCP credentials, so they are safe to run in CI on every push.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_DIR = REPO_ROOT / "sql" / "checkpoints"

CHECKPOINT_FILES = (
    "01_duplicate_check_by_year.sql",
    "02_deduplicate_2017.sql",
    "03_null_key_columns.sql",
    "04_target_label_distribution.sql",
)

DESTRUCTIVE_PATTERNS = (
    "DROP TABLE",
    "DELETE FROM",
    "TRUNCATE TABLE",
    "ALTER TABLE",
)


def _read(filename: str) -> str:
    path = CHECKPOINT_DIR / filename
    assert path.exists(), f"Missing checkpoint SQL file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", CHECKPOINT_FILES)
def test_checkpoint_file_exists(filename: str) -> None:
    path = CHECKPOINT_DIR / filename
    assert path.is_file(), f"Expected checkpoint SQL file at {path}"


@pytest.mark.parametrize("filename", CHECKPOINT_FILES)
def test_checkpoint_contains_select(filename: str) -> None:
    sql = _read(filename).upper()
    assert "SELECT" in sql, f"{filename} must contain a SELECT statement"


@pytest.mark.parametrize("filename", CHECKPOINT_FILES)
@pytest.mark.parametrize("pattern", DESTRUCTIVE_PATTERNS)
def test_checkpoint_has_no_destructive_statements(
    filename: str, pattern: str
) -> None:
    sql = _read(filename).upper()
    assert pattern not in sql, (
        f"{filename} must not contain destructive statement '{pattern}'."
        " Checkpoints are validation-only."
    )


def test_duplicate_check_has_required_clauses() -> None:
    """The duplicate-check checkpoint must group by lat/lon and use HAVING."""
    sql = _read("01_duplicate_check_by_year.sql")
    upper = sql.upper()

    assert "LATITUDE" in upper, "duplicate check must reference latitude"
    assert "LONGITUDE" in upper, "duplicate check must reference longitude"
    assert "GROUP BY" in upper, "duplicate check must include GROUP BY"
    assert "HAVING" in upper, "duplicate check must include HAVING"


def test_null_key_columns_checks_grain_keys() -> None:
    """03 must explicitly count NULLs in the row-grain keys."""
    sql = _read("03_null_key_columns.sql").upper()
    for column in ("TIMESTAMP", "LATITUDE", "LONGITUDE"):
        assert column in sql, (
            f"03_null_key_columns.sql should reference '{column}'"
        )
    assert "COUNTIF" in sql or "COUNT(" in sql, (
        "03_null_key_columns.sql should aggregate null counts"
    )


def test_target_distribution_uses_placeholders_and_percent() -> None:
    """04 must use placeholder table names and report a percent column."""
    sql = _read("04_target_label_distribution.sql")
    assert "PROJECT_ID.DATASET_ID" in sql, (
        "04_target_label_distribution.sql must use the PROJECT_ID.DATASET_ID"
        " placeholder so teammates know to substitute their own project."
    )
    assert "gold_features" in sql, (
        "04_target_label_distribution.sql should query a gold_features table"
    )
    assert "percent" in sql.lower(), (
        "04_target_label_distribution.sql should report a percent column"
    )
