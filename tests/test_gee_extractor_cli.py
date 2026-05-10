"""Smoke tests for data_pipelines/03b_extract_gee_land_weather.py CLI + AOI module.

Covers what's testable without GEE auth or BigQuery credentials:
- Argument parser accepts well-formed dates and rejects malformed input.
- --aoi flag rejects unknown AOI names.
- --end before --start is rejected.
- AOI module exposes the canonical 4-county Southern California list.

`earthengine-api` is not installed in CI, so we stub `ee` in sys.modules
before importing the script. The script's import-time side effects
(ee.Authenticate / ee.Initialize / task.cancel) are gated behind main() now,
so importing it under the stub is harmless.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR_PATH = REPO_ROOT / "data_pipelines" / "03b_extract_gee_land_weather.py"


@pytest.fixture(scope="module")
def extractor_module() -> types.ModuleType:
    """Import the extractor script under stubbed `ee`."""
    if "ee" not in sys.modules:
        sys.modules["ee"] = MagicMock()
    spec = importlib.util.spec_from_file_location(
        "openfire_gee_extractor", EXTRACTOR_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── argument parser ──────────────────────────────────────────────────────────

def test_parser_accepts_well_formed_dates(extractor_module) -> None:
    parser = extractor_module.build_arg_parser()
    args = parser.parse_args(["--start", "2024-01-01", "--end", "2024-12-31"])
    assert args.start.isoformat() == "2024-01-01"
    assert args.end.isoformat() == "2024-12-31"


def test_parser_rejects_malformed_start_date(extractor_module) -> None:
    parser = extractor_module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "2024/01/01", "--end", "2024-12-31"])


def test_parser_rejects_nonsense_date(extractor_module) -> None:
    parser = extractor_module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "not-a-date", "--end", "2024-12-31"])


def test_parser_requires_start_and_end(extractor_module) -> None:
    parser = extractor_module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_defaults_aoi_and_project(extractor_module) -> None:
    parser = extractor_module.build_arg_parser()
    args = parser.parse_args(["--start", "2024-01-01", "--end", "2024-01-31"])
    assert args.aoi == "socal_4_county"
    assert args.project == "msds603-mlops-project"
    assert args.cancel_existing is False
    assert args.authenticate is False


def test_main_rejects_end_before_start(extractor_module) -> None:
    with pytest.raises(SystemExit):
        extractor_module.main(["--start", "2024-12-31", "--end", "2024-01-01"])


def test_main_rejects_unknown_aoi(extractor_module) -> None:
    # Unknown AOI is rejected by get_aoi_counties before any GEE / BQ call.
    with pytest.raises(ValueError, match="Unknown AOI"):
        extractor_module.main([
            "--start", "2024-01-01",
            "--end", "2024-12-31",
            "--aoi", "does_not_exist",
        ])


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_generate_date_list_snaps_to_grid(extractor_module) -> None:
    import datetime as dt

    epoch = dt.date(2017, 9, 1)
    # 2018-01-04 is on the 5-day grid (125 days = 25*5 from epoch); 2018-01-03
    # is not, so a range starting on 2018-01-03 must snap forward to 2018-01-04.
    dates = extractor_module.generate_date_list(
        epoch, dt.date(2018, 1, 3), dt.date(2018, 1, 16), 5
    )
    assert dates == ["2018-01-04", "2018-01-09", "2018-01-14"]
    # If the range starts exactly on a grid date, that date is included.
    on_grid = extractor_module.generate_date_list(
        epoch, dt.date(2018, 1, 4), dt.date(2018, 1, 14), 5
    )
    assert on_grid == ["2018-01-04", "2018-01-09", "2018-01-14"]


def test_years_in_range_spans_calendar_boundary(extractor_module) -> None:
    import datetime as dt

    years = extractor_module.years_in_range(
        dt.date(2017, 9, 1), dt.date(2019, 12, 1), dt.date(2020, 2, 1), 5
    )
    assert years == [2019, 2020]


# ── AOI module ───────────────────────────────────────────────────────────────

def test_aoi_module_exposes_default_4_county_socal() -> None:
    from pipelines.aoi import AOIS, DEFAULT_AOI, get_aoi_counties

    assert DEFAULT_AOI == "socal_4_county"
    assert AOIS[DEFAULT_AOI] == [
        "Kern", "Los Angeles", "San Luis Obispo", "Santa Barbara",
    ]
    # Accessor returns a copy — mutating the result doesn't mutate the source.
    counties = get_aoi_counties()
    counties.append("Ventura")
    assert "Ventura" not in AOIS[DEFAULT_AOI]


def test_aoi_module_rejects_unknown_name() -> None:
    from pipelines.aoi import get_aoi_counties

    with pytest.raises(ValueError, match="Unknown AOI"):
        get_aoi_counties("not_a_real_aoi")
