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


def get_aoi_counties(name: str = DEFAULT_AOI) -> list[str]:
    """Return the list of California county names for a named AOI."""
    if name not in AOIS:
        valid = ", ".join(sorted(AOIS))
        raise ValueError(f"Unknown AOI {name!r}. Valid: {valid}")
    return list(AOIS[name])
