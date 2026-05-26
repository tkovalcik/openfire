# ============================================================
# OPENFIRE — Feature Extraction Pipeline (Python GEE API)
# ============================================================
# Purpose: Extract Sentinel-2 indices + GRIDMET weather +
#          topography for every 1 km grid cell at 5-day cadence.
#          Exports directly to BigQuery with automated task
#          submission (no manual clicking in Code Editor).
#
# CLI usage (preferred):
#   python data_pipelines/03b_extract_gee_land_weather.py \
#       --start 2020-01-01 --end 2020-12-31
#
#   # Cancel any leftover GEE tasks from a prior run before submitting:
#   ... --cancel-existing
#
#   # Use a non-default AOI (must exist in src/pipelines/aoi.py::AOIS):
#   ... --aoi socal_4_county
#
# Notebook usage (legacy):
#   The functions below can still be called interactively from a Jupyter
#   session — `main()` just stitches them together for unattended runs.
#
# Run inputs are ALWAYS snapped onto the 5-day grid anchored at
# EPOCH_START = 2017-09-01. EPOCH_START is a training invariant — never
# change it; the model expects features at exactly EPOCH_START + 5n dates.
# ============================================================

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

import ee
from google.cloud import bigquery

# Allow running this script directly (python data_pipelines/03b_...py) by
# adding the repo root to sys.path so `src.pipelines.aoi` resolves.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.pipelines.aoi import DEFAULT_AOI, get_aoi_counties, get_grid_asset_id  # noqa: E402  (needs sys.path tweak above)
from src.pipelines.date_grid import (  # noqa: E402  (needs sys.path tweak above)
    EPOCH_START,
    STEP_DAYS,
    generate_date_list,
    years_in_range,
)

# ── Defaults that may be overridden by CLI args ──────────────
GRID_SCALE = 1000
REDUCE_SCALE = 1000
CLOUD_THRESH = 20

GCP_PROJECT = "msds603-mlops-project"
BQ_DATASET = "openfire_features"
BQ_TABLE_PFX = "silver_features"   # table: silver_features_<YYYY>

# SCL cloud mask: 3=shadow, 8=cloud med, 9=cloud high, 10=cirrus
SCL_MASK_VALUES = [3, 8, 9, 10]

# No-data sentinel: replaces masked pixels so BQ always gets FLOAT.
# Handle in SQL: SELECT NULLIF(B11, -9999) AS B11, ...
NODATA_SENTINEL = -9999

# Column schema (order must match what GEE exports).
FEATURE_COLUMNS = [
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
    "gridmet_temp_max", "gridmet_humidity_min",
    "gridmet_precip_sum", "gridmet_wind_max",
    "mean_elevation", "mean_slope", "mean_cos_aspect", "mean_sin_aspect",
]

EXPECTED_S2_BANDS = [
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
]


# ── Pure helpers (no GEE side effects at import time) ────────

def parse_iso_date(value: str) -> date:
    """Parse YYYY-MM-DD into a date; raise argparse-friendly error on failure."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date {value!r}; expected YYYY-MM-DD"
        ) from exc


# ── GEE-side helpers (call only after ee.Initialize) ─────────

def build_static_topo() -> "ee.Image":
    elev = ee.Image("USGS/3DEP/10m")
    topo = ee.Terrain.products(elev)
    return ee.Image([
        topo.select("elevation").rename("mean_elevation"),
        topo.select("slope").rename("mean_slope"),
        topo.select("aspect").multiply(3.14159265 / 180).cos().rename("mean_cos_aspect"),
        topo.select("aspect").multiply(3.14159265 / 180).sin().rename("mean_sin_aspect"),
    ]).toFloat()


def build_aoi_context(
    aoi_counties: list[str],
    grid_scale: int,
    *,
    grid_asset_id: str | None = None,
) -> tuple["ee.Geometry", "ee.FeatureCollection"]:
    counties = ee.FeatureCollection("TIGER/2018/Counties")
    aoi = counties.filter(ee.Filter.And(
        ee.Filter.eq("STATEFP", "06"),
        ee.Filter.inList("NAME", aoi_counties),
    ))
    aoi_geom = aoi.geometry()
    if grid_asset_id is not None:
        base_grid = ee.FeatureCollection(grid_asset_id)
    else:
        # Non-deterministic fallback — only used before 08_freeze_grid.py is run.
        grid = aoi_geom.coveringGrid("EPSG:4326", grid_scale)
        base_grid = grid.map(lambda cell: ee.Feature(
            ee.Geometry.Point(cell.geometry().centroid(1).coordinates()),
            {
                "latitude": cell.geometry().centroid(1).coordinates().get(1),
                "longitude": cell.geometry().centroid(1).coordinates().get(0),
            }
        ))
    return aoi_geom, base_grid


def mask_s2_clouds(img: "ee.Image") -> "ee.Image":
    """Pixel-level cloud/shadow mask using the SCL band."""
    scl = img.select("SCL")
    clear = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    return img.updateMask(clear)


def compute_s2_indices(img: "ee.Image") -> "ee.Image":
    """Compute vegetation/water/burn indices from optical bands."""
    b2  = img.select("B2").multiply(0.0001)
    b3  = img.select("B3").multiply(0.0001)
    b4  = img.select("B4").multiply(0.0001)
    b8  = img.select("B8").multiply(0.0001)
    b11 = img.select("B11").multiply(0.0001)
    b12 = img.select("B12").multiply(0.0001)

    ndvi = b8.subtract(b4).divide(b8.add(b4)).rename("mean_NDVI")
    ndwi = b3.subtract(b8).divide(b3.add(b8)).rename("mean_NDWI")
    nbr  = b8.subtract(b12).divide(b8.add(b12)).rename("mean_NBR")
    evi  = b8.subtract(b4).divide(
        b8.add(b4.multiply(6)).subtract(b2.multiply(7.5)).add(1)
    ).multiply(2.5).rename("mean_EVI")

    return ee.Image([b2, b3, b4, b8, b11, b12, ndvi, ndwi, nbr, evi])


def build_composite(t0: "ee.Date", t1: "ee.Date", aoi_geom: "ee.Geometry", static_topo: "ee.Image") -> "ee.Image":
    """Build the full multi-band raster for one 5-day window."""
    s2_col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
              .filterBounds(aoi_geom)
              .filterDate(t0, t1)
              .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUD_THRESH))
              .map(mask_s2_clouds)
              .map(compute_s2_indices))

    dummy_s2 = (ee.Image.constant([0] * len(EXPECTED_S2_BANDS))
                .rename(EXPECTED_S2_BANDS)
                .toFloat()
                .updateMask(0))

    s2 = ee.Image(ee.Algorithms.If(
        s2_col.size().gt(0),
        s2_col.median().toFloat(),
        dummy_s2,
    )).toFloat()

    gridmet = ee.ImageCollection("IDAHO_EPSCOR/GRIDMET").filterDate(t0, t1)
    weather = ee.Image([
        gridmet.select("tmmx").max().subtract(273.15).rename("gridmet_temp_max"),
        gridmet.select("rmin").min().rename("gridmet_humidity_min"),
        gridmet.select("pr").sum().rename("gridmet_precip_sum"),
        gridmet.select("vs").max().rename("gridmet_wind_max"),
    ]).toFloat()

    # .unmask(NODATA_SENTINEL) so reduceRegions returns a typed FLOAT
    # rather than an untyped null that BQ serializes as STRING.
    return ee.Image([s2, weather, static_topo]).toFloat().unmask(NODATA_SENTINEL)


def extract_features(
    start_date_str: str,
    *,
    aoi_geom: "ee.Geometry",
    base_grid: "ee.FeatureCollection",
    static_topo: "ee.Image",
    reduce_scale: int = REDUCE_SCALE,
) -> "ee.FeatureCollection":
    """For one 5-day window, reduce the composite into the grid."""
    t0 = ee.Date(start_date_str)
    t1 = t0.advance(STEP_DAYS, "day")
    composite = build_composite(t0, t1, aoi_geom, static_topo)
    sampled = composite.reduceRegions(
        collection=base_grid,
        reducer=ee.Reducer.mean(),
        scale=reduce_scale,
        tileScale=16,
    )
    return sampled.map(lambda f: f.set("timestamp", start_date_str))


# ── BQ-side helpers ──────────────────────────────────────────

def build_bq_schema() -> list[bigquery.SchemaField]:
    schema = [
        bigquery.SchemaField("geo",          "GEOGRAPHY"),  # GEE auto-exports
        bigquery.SchemaField("latitude",     "FLOAT"),
        bigquery.SchemaField("longitude",    "FLOAT"),
        bigquery.SchemaField("timestamp",    "STRING"),
        bigquery.SchemaField("system:index", "STRING"),     # GEE auto-includes
    ]
    schema.extend(bigquery.SchemaField(c, "FLOAT") for c in FEATURE_COLUMNS)
    return schema


def ensure_bq_tables(
    bq_client: bigquery.Client,
    *,
    project: str,
    dataset: str,
    table_prefix: str,
    years: list[int],
) -> int:
    """Create silver_features_<year> tables that don't exist; leave existing ones untouched."""
    schema = build_bq_schema()
    created = 0
    for year in years:
        table_id = f"{project}.{dataset}.{table_prefix}_{year}"
        try:
            bq_client.get_table(table_id)
            print(f"  Already exists: {table_id}")
        except Exception:
            bq_client.create_table(bigquery.Table(table_id, schema=schema))
            print(f"  Created: {table_id}")
            created += 1
    if created:
        print(f"\nCreated {created} new tables. Waiting 10s for BQ propagation…")
        time.sleep(10)
    else:
        print(f"\nAll {len(years)} tables already exist. Ready to append.")
    return created


# ── GEE task lifecycle ───────────────────────────────────────

def cancel_existing_tasks() -> int:
    """Cancel any READY/RUNNING GEE tasks in the current account."""
    cancelled = 0
    for task in ee.batch.Task.list():
        if task.state in ("READY", "RUNNING"):
            task.cancel()
            cancelled += 1
    print(f"Cancelled {cancelled} tasks.")
    return cancelled


def submit_export_tasks(
    date_list: list[str],
    *,
    aoi_geom: "ee.Geometry",
    base_grid: "ee.FeatureCollection",
    static_topo: "ee.Image",
    project: str,
    dataset: str,
    table_prefix: str,
    reduce_scale: int = REDUCE_SCALE,
) -> list[tuple[str, "ee.batch.Task"]]:
    tasks: list[tuple[str, ee.batch.Task]] = []
    for start_str in date_list:
        year = start_str[:4]
        bq_table = f"{project}.{dataset}.{table_prefix}_{year}"

        fc = extract_features(
            start_str,
            aoi_geom=aoi_geom,
            base_grid=base_grid,
            static_topo=static_topo,
            reduce_scale=reduce_scale,
        )
        task = ee.batch.Export.table.toBigQuery(
            collection=fc,
            description=f"features_{start_str}",
            table=bq_table,
            append=True,
        )
        task.start()
        tasks.append((start_str, task))
    return tasks


def check_progress(tasks: list[tuple[str, "ee.batch.Task"]]) -> dict[str, int]:
    states: dict[str, int] = {}
    for _date_str, task in tasks:
        status = task.status()["state"]
        states[status] = states.get(status, 0) + 1
    total = len(tasks)
    for state, count in sorted(states.items()):
        print(f"  {state}: {count}/{total}")
    for date_str, task in tasks:
        status = task.status()
        if status["state"] == "FAILED":
            print(f"  FAILED: {date_str} — {status.get('error_message', 'unknown')}")
    return states


def retry_failed(
    tasks: list[tuple[str, "ee.batch.Task"]],
    *,
    aoi_geom: "ee.Geometry",
    base_grid: "ee.FeatureCollection",
    static_topo: "ee.Image",
    project: str,
    dataset: str,
    table_prefix: str,
    reduce_scale: int = REDUCE_SCALE,
) -> list[tuple[str, "ee.batch.Task"]]:
    retried: list[tuple[str, ee.batch.Task]] = []
    for date_str, task in tasks:
        if task.status()["state"] != "FAILED":
            continue
        year = date_str[:4]
        bq_table = f"{project}.{dataset}.{table_prefix}_{year}"
        fc = extract_features(
            date_str,
            aoi_geom=aoi_geom,
            base_grid=base_grid,
            static_topo=static_topo,
            reduce_scale=reduce_scale,
        )
        new_task = ee.batch.Export.table.toBigQuery(
            collection=fc,
            description=f"features_{date_str}_retry",
            table=bq_table,
            append=True,
        )
        new_task.start()
        retried.append((date_str, new_task))
        print(f"  Retried: {date_str}")
    if not retried:
        print("  No failed tasks to retry.")
    return retried


# ── CLI ──────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Submit per-window GEE export tasks that populate the silver_features_<year> "
            "BigQuery tables. Run dates are snapped to the 5-day grid anchored at EPOCH_START."
        ),
    )
    parser.add_argument(
        "--start",
        type=parse_iso_date,
        required=True,
        help="Run window start date (YYYY-MM-DD); snapped forward to the next 5-day grid date.",
    )
    parser.add_argument(
        "--end",
        type=parse_iso_date,
        required=True,
        help="Run window end date (YYYY-MM-DD), inclusive.",
    )
    parser.add_argument(
        "--aoi",
        default=DEFAULT_AOI,
        help=f"Named AOI from src/pipelines/aoi.py::AOIS (default: {DEFAULT_AOI}).",
    )
    parser.add_argument(
        "--project",
        default=GCP_PROJECT,
        help=f"GCP project for both EE Initialize and BigQuery (default: {GCP_PROJECT}).",
    )
    parser.add_argument(
        "--dataset",
        default=BQ_DATASET,
        help=f"BigQuery dataset (default: {BQ_DATASET}).",
    )
    parser.add_argument(
        "--table-prefix",
        default=BQ_TABLE_PFX,
        help=f"BigQuery silver table prefix (default: {BQ_TABLE_PFX}).",
    )
    parser.add_argument(
        "--cancel-existing",
        action="store_true",
        help="Cancel any READY/RUNNING GEE tasks in the account before submitting.",
    )
    parser.add_argument(
        "--authenticate",
        action="store_true",
        help="Run ee.Authenticate() before initializing (only needed on a new machine).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)

    if args.end < args.start:
        raise SystemExit(f"--end ({args.end}) must be >= --start ({args.start}).")

    aoi_counties = get_aoi_counties(args.aoi)

    if args.authenticate:
        ee.Authenticate()
    ee.Initialize(project=args.project)

    if args.cancel_existing:
        cancel_existing_tasks()

    grid_asset_id = get_grid_asset_id(args.aoi)
    if grid_asset_id:
        print(f"Using pinned grid asset: {grid_asset_id}")
    else:
        print(f"WARNING: No pinned grid for AOI={args.aoi}; using dynamic coveringGrid (non-deterministic)")
        print("         Run data_pipelines/08_freeze_grid.py to fix this.")
    aoi_geom, base_grid = build_aoi_context(aoi_counties, GRID_SCALE, grid_asset_id=grid_asset_id)
    static_topo = build_static_topo()
    print(f"Grid cells (approx): {base_grid.size().getInfo()}")

    bq_client = bigquery.Client(project=args.project)
    years = years_in_range(EPOCH_START, args.start, args.end, STEP_DAYS)
    ensure_bq_tables(
        bq_client,
        project=args.project,
        dataset=args.dataset,
        table_prefix=args.table_prefix,
        years=years,
    )

    date_list = generate_date_list(EPOCH_START, args.start, args.end, STEP_DAYS)
    if not date_list:
        raise SystemExit(
            f"No 5-day grid dates fall in [{args.start}, {args.end}]; nothing to do."
        )
    print(f"First date: {date_list[0]}, Last date: {date_list[-1]}")
    print(f"Total 5-day windows to export: {len(date_list)}")

    tasks = submit_export_tasks(
        date_list,
        aoi_geom=aoi_geom,
        base_grid=base_grid,
        static_topo=static_topo,
        project=args.project,
        dataset=args.dataset,
        table_prefix=args.table_prefix,
    )
    print(f"Submitted {len(tasks)} export tasks.")
    if tasks:
        print("First task ID:", tasks[0][1].id)
        print("Last  task ID:", tasks[-1][1].id)
        check_progress(tasks)


if __name__ == "__main__":
    main()
