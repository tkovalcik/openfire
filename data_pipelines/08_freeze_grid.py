"""One-shot script to freeze the canonical inference grid as a GEE asset.

GEE's coveringGrid("EPSG:4326", 1000) is non-deterministic across platform
updates — two runs of the same code can produce different tile offsets.  This
script computes the grid once using the production AOI, exports it as a GEE
FeatureCollection asset, and blocks until the export completes.  After this
script succeeds, update aoi.py::GRID_ASSET_IDS and both extractors will load
the pinned asset instead of recomputing.

Usage (run once, from the repo root, after GEE authentication):
    python data_pipelines/08_freeze_grid.py

Optional flags:
    --project   GCP project (default: msds603-mlops-project)
    --aoi       Named AOI from src/pipelines/aoi.py::AOIS (default: socal_4_county)
    --suffix    Version label appended to the asset name (default: v1)
    --authenticate  Run ee.Authenticate() before initializing
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import ee

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.pipelines.aoi import DEFAULT_AOI, get_aoi_counties  # noqa: E402

GCP_PROJECT = "msds603-mlops-project"
GRID_SCALE = 1000


# ── grid computation ──────────────────────────────────────────────────────────

def _compute_base_grid(aoi_counties: list[str]) -> "ee.FeatureCollection":
    """Compute the coveringGrid FeatureCollection for the given county list.

    This is intentionally *not* loading from a pinned asset — this function
    is the source that produces the asset.  Do not add a grid_asset_id branch
    here; that belongs in the inference/training extractors.
    """
    counties = ee.FeatureCollection("TIGER/2018/Counties")
    aoi = counties.filter(ee.Filter.And(
        ee.Filter.eq("STATEFP", "06"),
        ee.Filter.inList("NAME", aoi_counties),
    ))
    aoi_geom = aoi.geometry()
    grid = aoi_geom.coveringGrid("EPSG:4326", GRID_SCALE)
    return grid.map(lambda cell: ee.Feature(
        ee.Geometry.Point(cell.geometry().centroid(1).coordinates()),
        {
            "latitude":  cell.geometry().centroid(1).coordinates().get(1),
            "longitude": cell.geometry().centroid(1).coordinates().get(0),
        },
    ))


# ── task polling ─────────────────────────────────────────────────────────────

def _poll_until_done(
    task: "ee.batch.Task",
    *,
    poll_interval_sec: int = 30,
    timeout_sec: int = 1800,
) -> None:
    elapsed = 0
    while True:
        state = task.status()["state"]
        print(f"  state={state}  elapsed={elapsed}s")
        if state == "COMPLETED":
            return
        if state == "FAILED":
            raise RuntimeError(
                f"Asset export failed: {task.status().get('error_message', 'unknown')}"
            )
        if state == "CANCELLED":
            raise RuntimeError("Asset export was cancelled.")
        if elapsed >= timeout_sec:
            task.cancel()
            raise TimeoutError(
                f"Asset export did not complete within {timeout_sec}s; task cancelled."
            )
        time.sleep(poll_interval_sec)
        elapsed += poll_interval_sec


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=GCP_PROJECT,
                        help=f"GCP project for EE Initialize (default: {GCP_PROJECT})")
    parser.add_argument("--aoi", default=DEFAULT_AOI,
                        help=f"Named AOI from aoi.py::AOIS (default: {DEFAULT_AOI})")
    parser.add_argument("--suffix", default="v1",
                        help="Version suffix appended to the asset name (default: v1)")
    parser.add_argument("--authenticate", action="store_true",
                        help="Run ee.Authenticate() before initializing")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    aoi_counties = get_aoi_counties(args.aoi)

    # Asset ID mirrors the convention in aoi.py::GRID_ASSET_IDS.
    # socal_4_county + v1 → socal_4county_grid_v1
    aoi_slug = args.aoi.replace("_county", "county")  # socal_4_county → socal_4county
    asset_name = f"{aoi_slug}_grid_{args.suffix}"
    asset_id = f"projects/{args.project}/assets/{asset_name}"

    if args.authenticate:
        ee.Authenticate()
    ee.Initialize(project=args.project)

    print(f"AOI:          {args.aoi} → {aoi_counties}")
    print(f"Asset target: {asset_id}")
    print()

    print("Computing canonical grid …")
    base_grid = _compute_base_grid(aoi_counties)
    cell_count = base_grid.size().getInfo()
    print(f"  Grid cell count: {cell_count:,}")
    print()

    print(f"Exporting FeatureCollection → {asset_id} …")
    task = ee.batch.Export.table.toAsset(
        collection=base_grid,
        description=f"freeze_{asset_name}",
        assetId=asset_id,
    )
    task.start()
    print(f"  Task ID: {task.id}")
    _poll_until_done(task)
    print()
    print("Asset export complete.")
    print(f"  Asset: {asset_id}")
    print(f"  Canonical cell count: {cell_count:,}")
    print()
    print("Next steps:")
    print("  1. Verify in GEE Code Editor:")
    print(f"       ee.FeatureCollection('{asset_id}').size().getInfo()")
    print("  2. Update src/pipelines/aoi.py::GRID_ASSET_IDS:")
    print(f"       \"{args.aoi}\": \"{asset_id}\"")
    print("  3. Commit aoi.py — both extractors then load the pinned asset automatically.")
    print("  4. Smoke test: python -m src.pipelines.run_inference_pipeline")
    print("       --mode window --date 2026-04-17")
    print(f"     Expect row count = {cell_count:,} in gold_features_inference.")


if __name__ == "__main__":
    main()
