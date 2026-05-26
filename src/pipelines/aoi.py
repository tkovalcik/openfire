"""Canonical AOI definitions for the OpenFire pipelines.

The training pipeline extracts features over a fixed Area Of Interest; the
inference pipeline must use the same AOI to avoid out-of-distribution
predictions. Define the AOI here in exactly one place so both extractors
import the same constant.

The structure is a named-AOI dict so future expansion (adding a county,
swapping to a different region) is a small, explicit change rather than a
search-and-replace across the codebase.
"""
from __future__ import annotations


AOIS: dict[str, list[str]] = {
    # Production training AOI for openfire-gold v2.
    # Counties are matched against the TIGER/2018/Counties NAME field within
    # STATEFP=06 (California).
    "socal_4_county": [
        "Kern",
        "Los Angeles",
        "San Luis Obispo",
        "Santa Barbara",
    ],
}

DEFAULT_AOI = "socal_4_county"

# Pinned GEE FeatureCollection assets produced by data_pipelines/08_freeze_grid.py.
# When a key is present both extractors load the canonical grid from GEE Assets
# instead of recomputing via coveringGrid (which is non-deterministic across
# platform updates).  Add an entry here after running 08_freeze_grid.py and
# verifying the cell count.  AOIs without an entry fall back to dynamic computation.
GRID_ASSET_IDS: dict[str, str] = {
    "socal_4_county": "projects/msds603-mlops-project/assets/socal_4county_grid_v1",
}


def get_aoi_counties(name: str = DEFAULT_AOI) -> list[str]:
    """Return the list of California county names for a named AOI."""
    if name not in AOIS:
        valid = ", ".join(sorted(AOIS))
        raise ValueError(f"Unknown AOI {name!r}. Valid: {valid}")
    return list(AOIS[name])


def get_grid_asset_id(name: str = DEFAULT_AOI) -> str | None:
    """Return the pinned GEE asset ID for the named AOI's canonical grid, or None.

    None means the extractor falls back to dynamic coveringGrid computation,
    which is non-deterministic.  Always returns None until 08_freeze_grid.py
    has been run and GRID_ASSET_IDS has been populated.
    """
    return GRID_ASSET_IDS.get(name)
