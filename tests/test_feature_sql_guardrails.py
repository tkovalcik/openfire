"""Lightweight CI tests for the feature-engineering SQL guardrails.

These tests are intentionally offline:
- pure file I/O via `pathlib.Path`
- no Google Cloud or BigQuery clients
- no network access
- no Python dependencies beyond the standard library + pytest

They protect three things we keep getting bitten by:
1. Exploration SQL must stay read-only.
2. The model-facing SELECT in the 08 interaction feature SQL must not
   project raw `historical_fire_dates` (or its `clean_fire_dates`
   intermediate). The column carries future fire dates relative to each
   row's timestamp; using it as a feature is a leakage bug.
3. Every fire-history feature in 08 must filter on
   `fire_date < window_start_date`, and the only forward-looking
   construct must be the target candidate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPLORATION_SQL_DIR = REPO_ROOT / "sql" / "exploration"
DOCS_EXPLORATION_DIR = REPO_ROOT / "docs" / "exploration"

SQL_08_PATH = EXPLORATION_SQL_DIR / "08_interaction_feature_candidates.sql"
DOC_07_5_PATH = DOCS_EXPLORATION_DIR / "07_5_fire_date_temporal_check.md"


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- line` and `/* block */` comments so destructive-keyword
    checks can't be fooled by reassuring prose in the comment headers."""
    sql_no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql_no_line = re.sub(r"--[^\n]*", "", sql_no_block)
    return sql_no_line


# Patterns are chosen so they only match destructive keywords used as
# *statements* (e.g. `DROP TABLE foo`, `CREATE OR REPLACE TABLE ...`),
# not stray words inside comments or column names. We still also strip
# comments above as a defence in depth.
DESTRUCTIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bDROP\s+(TABLE|VIEW|FUNCTION|SCHEMA|INDEX|DATABASE)\b", "DROP"),
    (r"\bDELETE\s+FROM\b", "DELETE FROM"),
    (r"\bINSERT\s+INTO\b", "INSERT INTO"),
    (r"\bUPDATE\s+[`\w]", "UPDATE <table>"),
    (r"\bALTER\s+(TABLE|VIEW|SCHEMA|DATABASE)\b", "ALTER"),
    (r"\bTRUNCATE\s+TABLE\b", "TRUNCATE TABLE"),
    (r"\bMERGE\s+INTO\b", "MERGE INTO"),
    (r"\bCREATE\s+OR\s+REPLACE\b", "CREATE OR REPLACE"),
)


def _exploration_sql_files() -> list[Path]:
    if not EXPLORATION_SQL_DIR.is_dir():
        pytest.fail(
            f"Expected exploration SQL directory at {EXPLORATION_SQL_DIR}"
        )
    files = sorted(EXPLORATION_SQL_DIR.glob("*.sql"))
    if not files:
        pytest.fail(
            f"No exploration SQL files found under {EXPLORATION_SQL_DIR}"
        )
    return files


def test_exploration_sql_is_read_only() -> None:
    """Every SQL file under sql/exploration/ must be read-only."""
    for sql_path in _exploration_sql_files():
        sql = _strip_sql_comments(sql_path.read_text(encoding="utf-8"))
        for pattern, label in DESTRUCTIVE_PATTERNS:
            match = re.search(pattern, sql, flags=re.IGNORECASE)
            assert match is None, (
                f"{sql_path.relative_to(REPO_ROOT)} contains destructive"
                f" statement '{label}' (matched: '{match.group(0)}'."
                " Exploration SQL must be read-only."
            )


def _section1_final_projection(sql: str) -> str:
    """Return the projection list of the outermost SELECT that drives off
    `FROM FeatureRows` (Section 1's model-facing final SELECT)."""
    from_match = re.search(r"\bFROM\s+FeatureRows\b", sql)
    assert from_match is not None, (
        "Could not locate `FROM FeatureRows` in 08 SQL; the file structure"
        " has drifted away from the documented Section 1 contract."
    )
    before_from = sql[: from_match.start()]
    select_starts = [
        m.start() for m in re.finditer(r"(?m)^SELECT\b", before_from)
    ]
    assert select_starts, (
        "Could not locate a top-level SELECT before `FROM FeatureRows`"
        " in 08 SQL."
    )
    return before_from[select_starts[-1] :]


def test_08_does_not_project_raw_historical_fire_dates_in_final_select() -> None:
    """The Section 1 final SELECT must not expose the raw fire-date
    column (or its NULLIF-cleaned alias) to downstream consumers."""
    assert SQL_08_PATH.is_file(), f"Missing 08 SQL at {SQL_08_PATH}"
    sql = SQL_08_PATH.read_text(encoding="utf-8")

    projection = _strip_sql_comments(_section1_final_projection(sql))

    forbidden_in_projection = ("historical_fire_dates", "clean_fire_dates")
    for token in forbidden_in_projection:
        assert token not in projection, (
            f"08 final SELECT projects '{token}', which carries future fire"
            " dates relative to each row. It must remain inside the parsing"
            " CTEs and never reach the model-facing output."
        )


def test_08_fire_history_features_use_past_only_filter() -> None:
    """All fire-history feature aggregations must gate on
    `fire_date < window_start_date`, and the forward-looking BETWEEN
    must be associated only with the target candidate."""
    assert SQL_08_PATH.is_file(), f"Missing 08 SQL at {SQL_08_PATH}"
    sql = SQL_08_PATH.read_text(encoding="utf-8")
    sql_no_comments = _strip_sql_comments(sql)

    past_only_pattern = re.compile(
        r"\bfire_date\s*<\s*[a-zA-Z_]\w*\.?window_start_date\b",
        flags=re.IGNORECASE,
    )
    assert past_only_pattern.search(sql_no_comments), (
        "08 SQL is missing a `fire_date < window_start_date` (or aliased"
        " variant) past-only filter. Every fire-history feature must use"
        " this guard."
    )

    assert "burned_in_next_15_days_candidate" in sql_no_comments, (
        "08 SQL must define the target candidate `burned_in_next_15_days_candidate`."
    )

    forward_between_pattern = re.compile(
        r"BETWEEN\s+DATE_ADD\([^)]*window_start_date[^)]*INTERVAL\s+1\s+DAY\)"
        r"\s+AND\s+"
        r"DATE_ADD\([^)]*window_start_date[^)]*INTERVAL\s+15\s+DAY\)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    forward_matches = list(forward_between_pattern.finditer(sql_no_comments))
    assert forward_matches, (
        "08 SQL is missing the forward-looking BETWEEN pattern that"
        " constructs the target candidate."
    )
    for m in forward_matches:
        # Look ahead a small window after the BETWEEN match for the target alias.
        lookahead = sql_no_comments[m.end() : m.end() + 400]
        assert "burned_in_next_15_days_candidate" in lookahead, (
            "Forward-looking BETWEEN at SQL offset"
            f" {m.start()} is not immediately followed by the alias"
            " `burned_in_next_15_days_candidate`. The forward window must"
            " only be used to construct the target, never a feature."
        )


def test_07_5_documents_future_fire_date_risk() -> None:
    """The 07.5 doc must explicitly call out the future-fire-date leakage
    risk and the past-only filter teammates need to apply."""
    assert DOC_07_5_PATH.is_file(), (
        f"Expected fire-date temporal-check doc at {DOC_07_5_PATH}"
    )
    body = DOC_07_5_PATH.read_text(encoding="utf-8")

    required_substrings = (
        "future",
        "leakage",
        "fire_date < window_start_date",
        "historical_fire_dates",
    )
    for needle in required_substrings:
        assert needle in body, (
            f"{DOC_07_5_PATH.relative_to(REPO_ROOT)} must mention '{needle}'"
            " so teammates understand the leakage risk and the safe filter."
        )
