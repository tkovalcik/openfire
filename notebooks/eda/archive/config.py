# =============================================================================
# OpenFire EDA — Shared Configuration
# =============================================================================
# All tunable parameters live here so they're easy to change in one place.
# Both notebooks import from this file.

from pathlib import Path

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
DATA_DIR = Path("data")
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = Path("outputs")

# Create directories if they don't exist
for d in [RAW_DIR, PROCESSED_DIR, OUTPUT_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Shapefile paths (populated after download)
FIRE_PERIMETERS_ZIP = RAW_DIR / "fire241gdb.zip"
FIRE_PERIMETERS_DIR = RAW_DIR / "fire24_1.gdb"

# California state boundary
CA_BOUNDARY_PATH = RAW_DIR / "ca_boundary"

# -----------------------------------------------------------------------------
# FRAP Data Source
# -----------------------------------------------------------------------------
# Primary: CAL FIRE FRAP direct download (firep24_1 — released April 2025)
# If this URL breaks, check: https://www.fire.ca.gov/what-we-do/fire-resource-assessment-program/fire-perimeters
# or: https://frap.fire.ca.gov/mapping/gis-data/
# FRAP_DOWNLOAD_URL = "https://frap.fire.ca.gov/media/mn1f0t41/fire24_1.zip"
# https://34c031f8-c9fd-4018-8c5a-4159cdff6b0d-cdn-endpoint.azureedge.net/-/media/calfire-website/what-we-do/fire-resource-assessment-program---frap/gis-data/2025/fire241gdb.ash'?rev=51177a999fe84e83a7c03b7d5a66b93b
FRAP_DOWNLOAD_URL = "https://34c031f8-c9fd-4018-8c5a-4159cdff6b0d-cdn-endpoint.azureedge.net/-/media/calfire-website/what-we-do/fire-resource-assessment-program---frap/gis-data/2025/fire241gdb.ash'?rev=51177a999fe84e83a7c03b7d5a66b93b"

# Fallback: data.ca.gov hosts the same dataset
FRAP_FALLBACK_URL = "https://gis.data.ca.gov/datasets/CALFIRE-Forestry::california-fire-perimeters-all-1.zip"

# California boundary from Census TIGER/Line
CA_BOUNDARY_URL = "https://www2.census.gov/geo/tiger/TIGER2023/STATE/tl_2023_us_state.zip"

# -----------------------------------------------------------------------------
# Coordinate Reference Systems
# -----------------------------------------------------------------------------
CRS_WGS84 = "EPSG:4326"        # Lat/lon — for web maps, Folium, pydeck
CRS_CA_ALBERS = "EPSG:3310"    # California Albers Equal Area — for area calculations & rasterization

# -----------------------------------------------------------------------------
# Raster Resolution (meters) — for fire return interval analysis
# -----------------------------------------------------------------------------
# Change this single value to re-run at different resolutions.
# Rough cell counts for California's bounding box:
#   1000m → ~400K cells   (fast iteration, ~seconds)
#    500m → ~1.6M cells   (moderate, ~tens of seconds)
#    250m → ~6.4M cells   (final output quality, ~minutes)
#    100m → ~40M cells    (high detail, ~10-20 min, needs chunking)
#     30m → ~430M cells   (Sentinel-2 native, needs infrastructure)
RASTER_RESOLUTION_M = 1000  # Start at 1km for iteration; change to 250 for final

# -----------------------------------------------------------------------------
# Animation Settings
# -----------------------------------------------------------------------------
# Default start year for animation (fires before this are sparse/unreliable)
ANIMATION_START_YEAR = 2000

# Minimum fire acreage to label in animation
LABEL_MIN_ACRES = 5000

# Geometry simplification tolerance (meters, in EPSG:3310)
# Higher = faster rendering, lower fidelity. 500m is good for state-scale.
SIMPLIFY_TOLERANCE_M = 500

# Fade duration: how many frames a fire stays visible after its time bin
FADE_FRAMES = 5

# Video output settings
VIDEO_FPS = 15
VIDEO_DPI = 150
VIDEO_WIDTH_PX = 1920
VIDEO_HEIGHT_PX = 1080

# Dark theme colors
BG_COLOR = "#0a0a0a"
CA_OUTLINE_COLOR = "#2a2a2a"
FIRE_COLOR_ACTIVE = "#ff6600"
FIRE_COLOR_FADING = "#8b1a00"
FIRE_COLOR_GHOST = "#1a0500"
TEXT_COLOR = "#cccccc"
YEAR_TEXT_COLOR = "#ffffff"

# -----------------------------------------------------------------------------
# Fire Return Interval Settings
# -----------------------------------------------------------------------------
# Minimum number of burns to include a cell in FRI analysis
MIN_BURNS_FOR_FRI = 2

# Minimum number of burns to include a cell in trend analysis
MIN_BURNS_FOR_TREND = 3

# Regional boundaries (approximate lat/lon dividing line for NorCal/SoCal)
# Using the Tehachapi Mountains / ~35.5°N as a rough divider
NORCAL_SOCAL_LAT_DIVIDE = 35.5
