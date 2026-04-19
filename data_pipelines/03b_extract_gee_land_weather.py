# ============================================================
# OPENFIRE — Feature Extraction Pipeline (Python GEE API)
# ============================================================
# Purpose: Extract Sentinel-2 indices + GRIDMET weather +
#          topography for every 1km grid cell at 5-day cadence.
#          Exports directly to BigQuery with automated task
#          submission (no manual clicking in Code Editor).
#
# Date range: 2017-09-01 through 2025-01-01
#
# Usage:  Run cells sequentially in VS Code / JupyterLab.
#         Cell 1  — authenticate
#         Cell 2  — cancel any leftover GEE tasks
#         Cell 3  — config
#         Cell 4  — build spatial baseline
#         Cell 5  — define feature functions
#         Cell 6  — pre-create BQ tables with explicit schema
#         Cell 7  — submit all export tasks
#         Cell 8  — monitor progress
#         Cell 9  — retry failures
# ============================================================

# %% 1. IMPORTS & AUTHENTICATION
import ee
import time
from datetime import date, timedelta
from google.cloud import bigquery

ee.Authenticate()  # one-time; comment out after first run
ee.Initialize(project='msds603-mlops-project')


# %% 2. CANCEL ALL PREVIOUS GEE TASKS
cancelled = 0
for task in ee.batch.Task.list():
    if task.state in ('READY', 'RUNNING'):
        task.cancel()
        cancelled += 1
print(f'Cancelled {cancelled} tasks.')


# %% 3. CONFIGURATION

# ── Global epoch — every 5-day window is aligned to this ────
EPOCH_START  = date(2017, 9, 1)   # never changes (YYYY,MM,DD)

# ── Per-run window — adjust these each run ──────────────────
RUN_START    = date(2018, 1, 1)   # approximate; snapped to grid below
RUN_END      = date(2018, 12, 31) # (YYYY,MM,DD)

STEP_DAYS    = 5
GRID_SCALE   = 1000
REDUCE_SCALE = 1000
CLOUD_THRESH = 20

GCP_PROJECT  = 'msds603-mlops-project'
BQ_DATASET   = 'openfire_features'
BQ_TABLE_PFX = 'silver_features'   # table: silver_features_YYYY

AOI_COUNTIES = ['Kern', 'Los Angeles', 'San Luis Obispo', 'Santa Barbara']

# SCL cloud mask: 3=shadow, 8=cloud med, 9=cloud high, 10=cirrus
SCL_MASK_VALUES = [3, 8, 9, 10]

# No-data sentinel: replaces masked pixels so BQ always gets FLOAT.
# Handle in SQL: SELECT NULLIF(B11, -9999) AS B11, ...
NODATA_SENTINEL = -9999

# ── Column schema (order must match what GEE exports) ───────
FEATURE_COLUMNS = [
    # S2 optical bands (scaled)
    'B2', 'B3', 'B4', 'B8', 'B11', 'B12',
    # S2 derived indices
    'mean_NDVI', 'mean_EVI', 'mean_NDWI', 'mean_NBR',
    # GRIDMET weather
    'gridmet_temp_max', 'gridmet_humidity_min',
    'gridmet_precip_sum', 'gridmet_wind_max',
    # Topography
    'mean_elevation', 'mean_slope', 'mean_cos_aspect', 'mean_sin_aspect',
]


# %% 4. SPATIAL BASELINE (built once, reused for every window)

counties = ee.FeatureCollection("TIGER/2018/Counties")
aoi = counties.filter(ee.Filter.And(
    ee.Filter.eq('STATEFP', '06'),
    ee.Filter.inList('NAME', AOI_COUNTIES)
))
aoi_geom = aoi.geometry()

grid = aoi_geom.coveringGrid('EPSG:4326', GRID_SCALE)

base_grid = grid.map(lambda cell: ee.Feature(
    ee.Geometry.Point(cell.geometry().centroid(1).coordinates()),
    {
        'latitude':  cell.geometry().centroid(1).coordinates().get(1),
        'longitude': cell.geometry().centroid(1).coordinates().get(0),
    }
))

print(f'Grid cells (approx): {grid.size().getInfo()}')


# %% 5. FEATURE EXTRACTION FUNCTIONS

# ── Static topography ───────────────────────────────────────
_elev = ee.Image("USGS/3DEP/10m")
_topo = ee.Terrain.products(_elev)

STATIC_TOPO = ee.Image([
    _topo.select('elevation').rename('mean_elevation'),
    _topo.select('slope').rename('mean_slope'),
    _topo.select('aspect').multiply(3.14159265 / 180).cos().rename('mean_cos_aspect'),
    _topo.select('aspect').multiply(3.14159265 / 180).sin().rename('mean_sin_aspect'),
]).toFloat()

# ── S2 band lists ───────────────────────────────────────────
EXPECTED_S2_BANDS = ['B2','B3','B4','B8','B11','B12',
                     'mean_NDVI','mean_EVI','mean_NDWI','mean_NBR']

DUMMY_S2 = (ee.Image.constant([0]*len(EXPECTED_S2_BANDS))
              .rename(EXPECTED_S2_BANDS)
              .toFloat()
              .updateMask(0))


def mask_s2_clouds(img):
    """Pixel-level cloud/shadow mask using the SCL band."""
    scl = img.select('SCL')
    clear = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    return img.updateMask(clear)


def compute_s2_indices(img):
    """Compute vegetation/water/burn indices from optical bands.
    Only scales the 6 bands actually used for index computation.
    """
    b2  = img.select('B2').multiply(0.0001)
    b3  = img.select('B3').multiply(0.0001)
    b4  = img.select('B4').multiply(0.0001)
    b8  = img.select('B8').multiply(0.0001)
    b11 = img.select('B11').multiply(0.0001)
    b12 = img.select('B12').multiply(0.0001)

    ndvi = b8.subtract(b4).divide(b8.add(b4)).rename('mean_NDVI')
    ndwi = b3.subtract(b8).divide(b3.add(b8)).rename('mean_NDWI')
    nbr  = b8.subtract(b12).divide(b8.add(b12)).rename('mean_NBR')
    evi  = b8.subtract(b4).divide(
        b8.add(b4.multiply(6)).subtract(b2.multiply(7.5)).add(1)
    ).multiply(2.5).rename('mean_EVI')

    return ee.Image([b2, b3, b4, b8, b11, b12,
                     ndvi, ndwi, nbr, evi])


def build_composite(t0, t1):
    """Build the full multi-band raster for one 5-day window."""
    # ── Sentinel-2 ──────────────────────────────────────────
    s2_col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
              .filterBounds(aoi_geom)
              .filterDate(t0, t1)
              .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', CLOUD_THRESH))
              .map(mask_s2_clouds)
              .map(compute_s2_indices))

    s2 = ee.Image(ee.Algorithms.If(
        s2_col.size().gt(0),
        s2_col.median().toFloat(),
        DUMMY_S2
    )).toFloat()

    # ── GRIDMET weather ─────────────────────────────────────
    gridmet = ee.ImageCollection("IDAHO_EPSCOR/GRIDMET").filterDate(t0, t1)
    weather = ee.Image([
        gridmet.select('tmmx').max().subtract(273.15).rename('gridmet_temp_max'),
        gridmet.select('rmin').min().rename('gridmet_humidity_min'),
        gridmet.select('pr').sum().rename('gridmet_precip_sum'),
        gridmet.select('vs').max().rename('gridmet_wind_max'),
    ]).toFloat()

    # ── Combine ─────────────────────────────────────────────
    # .unmask(-9999) replaces all masked pixels with a float sentinel
    # so that ee.Reducer.mean() always returns a typed FLOAT, never
    # an untyped null that BQ serializes as STRING.
    # Clean up downstream in SQL: NULLIF(column, -9999)
    return ee.Image([s2, weather, STATIC_TOPO]).toFloat().unmask(NODATA_SENTINEL)


def extract_features(start_date_str):
    """For one 5-day window, reduce the composite into the grid."""
    t0 = ee.Date(start_date_str)
    t1 = t0.advance(STEP_DAYS, 'day')

    composite = build_composite(t0, t1)

    sampled = composite.reduceRegions(
        collection=base_grid,
        reducer=ee.Reducer.mean(),
        scale=REDUCE_SCALE,
        tileScale=16
    )

    return sampled.map(lambda f: f.set('timestamp', start_date_str))


# %% 6. ENSURE BIGQUERY TABLES EXIST WITH CORRECT SCHEMA
# ── Creates only missing tables; leaves existing ones untouched ──
# ── For a fresh backfill, delete tables manually in BQ console first ──

bq_client = bigquery.Client(project=GCP_PROJECT)

# Build schema: matches what GEE auto-created for 2017
bq_schema = [
    bigquery.SchemaField('geo',            'GEOGRAPHY'),  # GEE auto-exports this
    bigquery.SchemaField('latitude',       'FLOAT'),
    bigquery.SchemaField('longitude',      'FLOAT'),
    bigquery.SchemaField('timestamp',      'STRING'),
    bigquery.SchemaField('system:index',   'STRING'),     # GEE auto-includes this
]
for col in FEATURE_COLUMNS:
    bq_schema.append(bigquery.SchemaField(col, 'FLOAT'))

# Determine which years we need tables for
all_years = sorted(set(
    (EPOCH_START + timedelta(days=i * STEP_DAYS)).year
    for i in range(((RUN_END - EPOCH_START).days // STEP_DAYS) + 1)
    if EPOCH_START + timedelta(days=i * STEP_DAYS) >= RUN_START
    and EPOCH_START + timedelta(days=i * STEP_DAYS) <= RUN_END
))

created = 0
for year in all_years:
    table_id = f'{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PFX}_{year}'
    table = bigquery.Table(table_id, schema=bq_schema)
    try:
        bq_client.get_table(table_id)
        print(f'  Already exists: {table_id}')
    except Exception:
        # Table doesn't exist — create it
        bq_client.create_table(table)
        print(f'  Created: {table_id}')
        created += 1

if created > 0:
    print(f'\nCreated {created} new tables. Waiting 10s for BQ propagation...')
    time.sleep(10)
else:
    print(f'\nAll {len(all_years)} tables already exist. Ready to append.')


# %% 7. GENERATE DATE LIST & SUBMIT EXPORT TASKS

def generate_date_list(epoch, run_start, run_end, step_days):
    """Generate dates aligned to the global 5-day grid starting at epoch."""
    # How many steps from epoch to reach/pass run_start?
    days_offset = (run_start - epoch).days
    first_step = (days_offset + step_days - 1) // step_days  # ceiling division
    cursor = epoch + timedelta(days=first_step * step_days)

    dates = []
    while cursor <= run_end:
        dates.append(cursor.isoformat())
        cursor += timedelta(days=step_days)
    return dates

all_dates = generate_date_list(EPOCH_START, RUN_START, RUN_END, STEP_DAYS)
print(f'First date: {all_dates[0]}, Last date: {all_dates[-1]}')
print(f'Total 5-day windows to export: {len(all_dates)}')

tasks = []
for start_str in all_dates:
    year = start_str[:4]
    bq_table = f'{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PFX}_{year}'

    fc = extract_features(start_str)

    task = ee.batch.Export.table.toBigQuery(
        collection=fc,
        description=f'features_{start_str}',
        table=bq_table,
        append=True
    )
    task.start()
    tasks.append((start_str, task))

print(f'Submitted {len(tasks)} export tasks.')
print('First task ID:', tasks[0][1].id)
print('Last  task ID:', tasks[-1][1].id)


# %% 8. MONITOR TASK PROGRESS

def check_progress(tasks):
    states = {}
    for date_str, task in tasks:
        status = task.status()['state']
        states[status] = states.get(status, 0) + 1
    
    total = len(tasks)
    for state, count in sorted(states.items()):
        print(f'  {state}: {count}/{total}')
    
    for date_str, task in tasks:
        status = task.status()
        if status['state'] == 'FAILED':
            print(f'  FAILED: {date_str} — {status.get("error_message", "unknown")}')

check_progress(tasks)


# %% 9. RETRY FAILED TASKS

def retry_failed(tasks):
    retried = []
    for date_str, task in tasks:
        if task.status()['state'] == 'FAILED':
            year = date_str[:4]
            bq_table = f'{GCP_PROJECT}.{BQ_DATASET}.{BQ_TABLE_PFX}_{year}'
            
            fc = extract_features(date_str)
            new_task = ee.batch.Export.table.toBigQuery(
                collection=fc,
                description=f'features_{date_str}_retry',
                table=bq_table,
                append=True
            )
            new_task.start()
            retried.append((date_str, new_task))
            print(f'  Retried: {date_str}')
    
    if not retried:
        print('  No failed tasks to retry.')
    return retried

# retried_tasks = retry_failed(tasks)