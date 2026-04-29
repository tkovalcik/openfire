"""GEE feature extraction for a single inference window.

Adapts data_pipelines/03b_extract_gee_land_weather.py for per-window
inference use. Two public functions wired into run_inference_pipeline:

- extract_gee_window: submit a GEE export task for one 5-day window into the
  fixed staging table silver_features_inference_import (overwrite=True), then
  poll until the GEE task reaches COMPLETED.
- append_silver_window: MERGE from the staging table into silver_features_<year>
  keyed on (latitude, longitude, timestamp). Idempotent: re-running for the same
  window updates existing rows rather than duplicating them.

GEE export uses overwrite=True (WRITE_TRUNCATE) on the staging table so it
always contains exactly the current window's rows, regardless of prior runs.
The subsequent MERGE ensures silver_features_<year> accumulates windows without
duplicates even if a window is re-processed.
"""
from __future__ import annotations

import logging
import time
from datetime import date

from google.cloud import bigquery

from .aoi import DEFAULT_AOI, get_aoi_counties
from .date_grid import STEP_DAYS

LOGGER = logging.getLogger(__name__)

PROJECT = "msds603-mlops-project"
DATASET = "openfire_features"
STAGING_TABLE_ID = f"{PROJECT}.{DATASET}.silver_features_inference_import"
SILVER_TABLE_PREFIX = f"{PROJECT}.{DATASET}.silver_features"

GRID_SCALE = 1000
REDUCE_SCALE = 1000
CLOUD_THRESH = 20
NODATA_SENTINEL = -9999

# Sentinel-2 scenes typically appear in COPERNICUS/S2_SR_HARMONIZED 2–4 days
# after acquisition. We add an extra day of buffer and round up to 3 days so
# that a window processed just after its end date uses fully-ingested imagery.
READINESS_BUFFER_DAYS = 3

_FEATURE_COLUMNS = [
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
    "gridmet_temp_max", "gridmet_humidity_min",
    "gridmet_precip_sum", "gridmet_wind_max",
    "mean_elevation", "mean_slope", "mean_cos_aspect", "mean_sin_aspect",
]

_S2_BANDS = [
    "B2", "B3", "B4", "B8", "B11", "B12",
    "mean_NDVI", "mean_EVI", "mean_NDWI", "mean_NBR",
]


# ── data-readiness gate ───────────────────────────────────────────────────────

def is_window_ready(
    window_date: date,
    *,
    today: date | None = None,
    buffer_days: int = READINESS_BUFFER_DAYS,
) -> tuple[bool, str]:
    """Return (ready, reason) for a window.

    A window is considered ready if today is at least buffer_days past the
    window end date (window_date + 5 days). This gates against Sentinel-2
    pipeline latency so the composite is built from fully-ingested imagery.
    If not ready, the orchestrator skips the window with a structured warning
    and retries on the next scheduled run.
    """
    from datetime import timedelta
    eff_today = today or date.today()
    window_end  = window_date + timedelta(days=STEP_DAYS)
    ready_after = window_end  + timedelta(days=buffer_days)
    if eff_today < ready_after:
        return False, (
            f"window_end={window_end.isoformat()} + {buffer_days}d buffer = "
            f"{ready_after.isoformat()}; today={eff_today.isoformat()}"
        )
    return True, ""


# ── silver table helpers ──────────────────────────────────────────────────────

def _silver_schema() -> list[bigquery.SchemaField]:
    fields: list[bigquery.SchemaField] = [
        bigquery.SchemaField("geo",          "GEOGRAPHY"),
        bigquery.SchemaField("latitude",     "FLOAT"),
        bigquery.SchemaField("longitude",    "FLOAT"),
        bigquery.SchemaField("timestamp",    "STRING"),
        bigquery.SchemaField("system:index", "STRING"),
    ]
    fields.extend(bigquery.SchemaField(c, "FLOAT") for c in _FEATURE_COLUMNS)
    return fields


def _ensure_silver_year_table(bq_client: bigquery.Client, year: int) -> None:
    """Create silver_features_<year> if it doesn't exist yet."""
    table_id = f"{SILVER_TABLE_PREFIX}_{year}"
    try:
        bq_client.get_table(table_id)
    except Exception:
        bq_client.create_table(bigquery.Table(table_id, schema=_silver_schema()))
        LOGGER.info("Created %s; sleeping 10s for BQ propagation", table_id)
        time.sleep(10)


# ── GEE compute helpers ───────────────────────────────────────────────────────
# All functions below import `ee` lazily so the module can be imported in unit
# tests without the earthengine-api package installed.

def _build_static_topo() -> "ee.Image":
    import ee
    elev = ee.Image("USGS/3DEP/10m")
    topo = ee.Terrain.products(elev)
    return ee.Image([
        topo.select("elevation").rename("mean_elevation"),
        topo.select("slope").rename("mean_slope"),
        topo.select("aspect").multiply(3.14159265 / 180).cos().rename("mean_cos_aspect"),
        topo.select("aspect").multiply(3.14159265 / 180).sin().rename("mean_sin_aspect"),
    ]).toFloat()


def _build_aoi_context(
    aoi_counties: list[str],
) -> tuple["ee.Geometry", "ee.FeatureCollection"]:
    import ee
    counties = ee.FeatureCollection("TIGER/2018/Counties")
    aoi = counties.filter(ee.Filter.And(
        ee.Filter.eq("STATEFP", "06"),
        ee.Filter.inList("NAME", aoi_counties),
    ))
    aoi_geom = aoi.geometry()
    grid = aoi_geom.coveringGrid("EPSG:4326", GRID_SCALE)
    base_grid = grid.map(lambda cell: ee.Feature(
        ee.Geometry.Point(cell.geometry().centroid(1).coordinates()),
        {
            "latitude":  cell.geometry().centroid(1).coordinates().get(1),
            "longitude": cell.geometry().centroid(1).coordinates().get(0),
        },
    ))
    return aoi_geom, base_grid


def _mask_s2_clouds(img: "ee.Image") -> "ee.Image":
    """Pixel-level cloud/shadow mask using the SCL quality band."""
    scl = img.select("SCL")
    clear = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    return img.updateMask(clear)


def _compute_s2_indices(img: "ee.Image") -> "ee.Image":
    import ee
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


def _build_composite(
    t0: "ee.Date",
    t1: "ee.Date",
    aoi_geom: "ee.Geometry",
    static_topo: "ee.Image",
) -> "ee.Image":
    import ee
    s2_col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi_geom)
        .filterDate(t0, t1)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUD_THRESH))
        .map(_mask_s2_clouds)
        .map(_compute_s2_indices)
    )
    # Preserve schema when no S2 images are available for the window.
    dummy_s2 = (
        ee.Image.constant([0] * len(_S2_BANDS))
        .rename(_S2_BANDS)
        .toFloat()
        .updateMask(0)
    )
    s2 = ee.Image(
        ee.Algorithms.If(s2_col.size().gt(0), s2_col.median().toFloat(), dummy_s2)
    ).toFloat()
    gridmet = ee.ImageCollection("IDAHO_EPSCOR/GRIDMET").filterDate(t0, t1)
    weather = ee.Image([
        gridmet.select("tmmx").max().subtract(273.15).rename("gridmet_temp_max"),
        gridmet.select("rmin").min().rename("gridmet_humidity_min"),
        gridmet.select("pr").sum().rename("gridmet_precip_sum"),
        gridmet.select("vs").max().rename("gridmet_wind_max"),
    ]).toFloat()
    # unmask replaces masked pixels with NODATA_SENTINEL so BQ always gets FLOAT.
    return ee.Image([s2, weather, static_topo]).toFloat().unmask(NODATA_SENTINEL)


def _build_feature_collection(
    window_date: date,
    *,
    aoi_geom: "ee.Geometry",
    base_grid: "ee.FeatureCollection",
    static_topo: "ee.Image",
) -> "ee.FeatureCollection":
    import ee
    start_str = window_date.isoformat()
    t0 = ee.Date(start_str)
    t1 = t0.advance(STEP_DAYS, "day")
    composite = _build_composite(t0, t1, aoi_geom, static_topo)
    sampled = composite.reduceRegions(
        collection=base_grid,
        reducer=ee.Reducer.mean(),
        scale=REDUCE_SCALE,
        tileScale=16,
    )
    return sampled.map(lambda f: f.set("timestamp", start_str))


def _poll_until_done(
    task: "ee.batch.Task",
    *,
    poll_interval_sec: int,
    timeout_sec: int,
    window_date: date,
) -> None:
    elapsed = 0
    while True:
        status = task.status()
        state = status["state"]
        LOGGER.info(
            "  GEE task state=%s elapsed=%ss window=%s",
            state, elapsed, window_date.isoformat(),
        )
        if state == "COMPLETED":
            return
        if state == "FAILED":
            msg = status.get("error_message", "unknown")
            raise RuntimeError(
                f"GEE export failed for window={window_date.isoformat()}: {msg}"
            )
        if state == "CANCELLED":
            raise RuntimeError(
                f"GEE export was cancelled for window={window_date.isoformat()}."
            )
        if elapsed >= timeout_sec:
            task.cancel()
            raise TimeoutError(
                f"GEE export did not complete within {timeout_sec}s "
                f"for window={window_date.isoformat()}; task cancelled."
            )
        time.sleep(poll_interval_sec)
        elapsed += poll_interval_sec


# ── public API ────────────────────────────────────────────────────────────────

def extract_gee_window(
    window_date: date,
    *,
    project: str = PROJECT,
    dataset: str = DATASET,
    aoi: str = DEFAULT_AOI,
    poll_interval_sec: int = 30,
    timeout_sec: int = 3600,
) -> None:
    """Export Sentinel-2 + gridMET features for one 5-day window to BQ.

    Calls ee.Initialize with the ambient ADC (service account on Cloud Run),
    builds the composite for [window_date, window_date + 5d), and exports the
    sampled grid to silver_features_inference_import with overwrite=True so the
    staging table always holds exactly the current window's rows.

    Blocks until the GEE export task reaches COMPLETED, or raises RuntimeError
    on task failure or TimeoutError after timeout_sec seconds.
    """
    import ee

    aoi_counties = get_aoi_counties(aoi)
    ee.Initialize(project=project)

    aoi_geom, base_grid = _build_aoi_context(aoi_counties)
    static_topo = _build_static_topo()
    fc = _build_feature_collection(
        window_date, aoi_geom=aoi_geom, base_grid=base_grid, static_topo=static_topo
    )

    staging = f"{project}.{dataset}.silver_features_inference_import"
    task = ee.batch.Export.table.toBigQuery(
        collection=fc,
        description=f"inference_{window_date.isoformat()}",
        table=staging,
        overwrite=True,
    )
    task.start()
    LOGGER.info(
        "Submitted GEE export task_id=%s window=%s → %s",
        task.id, window_date.isoformat(), staging,
    )
    _poll_until_done(
        task,
        poll_interval_sec=poll_interval_sec,
        timeout_sec=timeout_sec,
        window_date=window_date,
    )
    LOGGER.info("GEE export complete for window=%s", window_date.isoformat())


def append_silver_window(
    window_date: date,
    *,
    bq_client: bigquery.Client,
    project: str = PROJECT,
    dataset: str = DATASET,
) -> int:
    """MERGE silver_features_inference_import → silver_features_<year>.

    Creates silver_features_<year> if it doesn't exist yet. Idempotent: re-running
    for the same window updates existing rows rather than creating duplicates.
    Returns the number of rows inserted or updated.
    """
    year = window_date.year
    _ensure_silver_year_table(bq_client, year)

    staging = f"`{project}.{dataset}.silver_features_inference_import`"
    silver  = f"`{project}.{dataset}.silver_features_{year}`"
    cols     = ", ".join(_FEATURE_COLUMNS)
    src_cols = ", ".join(f"src.{c}" for c in _FEATURE_COLUMNS)
    update_set = ",\n          ".join(f"dst.{c} = src.{c}" for c in _FEATURE_COLUMNS)

    sql = f"""
        MERGE {silver} AS dst
        USING (
          SELECT latitude, longitude, timestamp, geo, `system:index`,
                 {cols}
          FROM {staging}
        ) AS src
        ON  dst.latitude  = src.latitude
        AND dst.longitude = src.longitude
        AND dst.timestamp = src.timestamp
        WHEN MATCHED THEN UPDATE SET
          dst.geo = src.geo,
          {update_set}
        WHEN NOT MATCHED THEN INSERT (
          latitude, longitude, timestamp, geo, `system:index`, {cols}
        ) VALUES (
          src.latitude, src.longitude, src.timestamp, src.geo, src.`system:index`,
          {src_cols}
        )
    """
    job = bq_client.query(sql)
    job.result()
    affected = job.num_dml_affected_rows or 0
    LOGGER.info(
        "Merged silver window=%s year=%s rows_affected=%s job_id=%s",
        window_date.isoformat(), year, f"{affected:,}", job.job_id,
    )
    return affected


__all__ = [
    "READINESS_BUFFER_DAYS",
    "STAGING_TABLE_ID",
    "SILVER_TABLE_PREFIX",
    "is_window_ready",
    "extract_gee_window",
    "append_silver_window",
]
