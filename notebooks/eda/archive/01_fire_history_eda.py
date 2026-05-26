# %% [markdown]
# # OpenFire EDA — Notebook 1: California Fire History
#
# **Purpose:** Load the Cal Fire FRAP historical fire perimeters dataset,
# explore data quality and patterns, build an interactive exploration map,
# and produce a polished animation of California's fire history.
#
# **Sections:**
# - A: Data Acquisition & Loading
# - B: Data Quality & Basic EDA
# - C: Interactive Exploration App (Panel + pydeck) — *coming next*
# - D: Polished Video Export (matplotlib → mp4) — *coming next*
#
# **Data source:** CAL FIRE FRAP firep24_1 (released April 2025)
# https://www.fire.ca.gov/what-we-do/fire-resource-assessment-program/fire-perimeters

# =============================================================================
# SECTION A — Data Acquisition & Loading
# =============================================================================

# %% Cell A1: Imports and setup
import geopandas as gpd
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.colors import LinearSegmentedColormap
import contextily as cx
from pathlib import Path
from zipfile import ZipFile
from tqdm import tqdm
import requests
import warnings
import sys
import os

# Add parent dir to path so we can import config
sys.path.insert(0, os.path.dirname(os.path.abspath("__file__")))
from config import *

warnings.filterwarnings("ignore", category=FutureWarning)

print(f"Data directory:   {DATA_DIR.resolve()}")
print(f"Output directory: {OUTPUT_DIR.resolve()}")
print(f"Raster resolution: {RASTER_RESOLUTION_M}m")

# %% Cell A2: Download FRAP fire perimeters shapefile
def download_file(url: str, dest: Path, chunk_size: int = 8192) -> bool:
    """Download a file with progress bar. Returns True if successful."""
    if dest.exists():
        print(f"✓ Already downloaded: {dest.name}")
        return True

    print(f"Downloading from: {url}")
    try:
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, desc=dest.name
        ) as pbar:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                f.write(chunk)
                pbar.update(len(chunk))
        print(f"✓ Downloaded: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return True
    except Exception as e:
        print(f"✗ Download failed: {e}")
        if dest.exists():
            dest.unlink()  # Clean up partial download
        return False


# Try primary URL first, fall back to data.ca.gov
if not FIRE_PERIMETERS_ZIP.exists():
    success = download_file(FRAP_DOWNLOAD_URL, FIRE_PERIMETERS_ZIP)
    if not success:
        print("Trying fallback URL...")
        success = download_file(FRAP_FALLBACK_URL, FIRE_PERIMETERS_ZIP)
    if not success:
        print(
            "\n⚠️  Automatic download failed. Please download manually:"
            "\n   1. Go to: https://frap.fire.ca.gov/mapping/gis-data/"
            "\n   2. Download 'Fire Perimeters' shapefile"
            f"\n   3. Save as: {FIRE_PERIMETERS_ZIP.resolve()}"
        )
else:
    print(f"✓ Already downloaded: {FIRE_PERIMETERS_ZIP.name}")

# %% Cell A3: Unzip and discover shapefile
def unzip_shapefile(zip_path: Path, extract_dir: Path) -> Path | None:
    """Unzip and return the path to the .shp file."""
    if extract_dir.exists() and any(extract_dir.glob("*.shp")):
        shp_files = list(extract_dir.glob("*.shp"))
        print(f"✓ Already extracted: {len(shp_files)} .shp file(s) found")
        # Return the fire perimeters layer (firep, not rxburn)
        for f in shp_files:
            if "firep" in f.name.lower():
                return f
        return shp_files[0]

    print(f"Extracting {zip_path.name}...")
    extract_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    shp_files = list(extract_dir.rglob("*.shp"))
    print(f"✓ Extracted {len(shp_files)} .shp file(s):")
    for f in shp_files:
        print(f"   {f.relative_to(extract_dir)}")

    # Prefer the wildfire perimeters layer (firep*), not prescribed burns (rxburn*)
    for f in shp_files:
        if "firep" in f.name.lower():
            return f
    return shp_files[0] if shp_files else None


shp_path = unzip_shapefile(FIRE_PERIMETERS_ZIP, FIRE_PERIMETERS_DIR)
if shp_path:
    print(f"\nUsing: {shp_path.name}")
else:
    raise FileNotFoundError("No .shp file found after extraction!")

# %% Cell A4: Load into GeoDataFrame
print(f"Loading {shp_path.name}...")
gdf_raw = gpd.read_file(shp_path)

print(f"\n{'='*60}")
print(f"FRAP Fire Perimeters — Initial Load")
print(f"{'='*60}")
print(f"Records:    {len(gdf_raw):,}")
print(f"Columns:    {len(gdf_raw.columns)}")
print(f"CRS:        {gdf_raw.crs}")
print(f"Bounds:     {gdf_raw.total_bounds}")
print(f"Memory:     {gdf_raw.memory_usage(deep=True).sum() / 1e6:.1f} MB")
print(f"\nColumn names:\n{list(gdf_raw.columns)}")

# %% Cell A5: Inspect schema and first rows
print("Data types:")
print(gdf_raw.dtypes.to_string())
print(f"\nFirst 5 rows (non-geometry columns):")
gdf_raw.drop(columns="geometry").head()

# %% Cell A6: Check CRS and reproject
# We need two versions:
#   - EPSG:3310 (CA Albers Equal Area) for area calculations, rasterization, static maps
#   - EPSG:4326 (WGS84 lat/lon) for web maps, pydeck, Folium

print(f"Original CRS: {gdf_raw.crs}")

# Reproject to CA Albers for area-preserving analysis
gdf = gdf_raw.to_crs(CRS_CA_ALBERS)
print(f"Reprojected to: {gdf.crs}  (California Albers Equal Area)")
print(f"New bounds: {gdf.total_bounds}")


# =============================================================================
# SECTION B — Data Quality & Basic EDA
# =============================================================================

# %% Cell B1: Standardize column names and parse dates
# FRAP column names can vary between releases. Let's check what we have
# and standardize the key fields we need.

# Print all columns with sample values to understand what we're working with
print("Column inventory:")
print("-" * 70)
for col in gdf.columns:
    if col == "geometry":
        continue
    non_null = gdf[col].dropna()
    n_null = gdf[col].isna().sum()
    sample = non_null.iloc[0] if len(non_null) > 0 else "ALL NULL"
    print(f"  {col:<20} nulls={n_null:>5}  sample: {sample}")

# %% Cell B2: Parse and clean date fields
# Key fields we expect:
#   YEAR_ — fire year (integer, should always be present)
#   ALARM_DATE — date fire was reported (datetime, often NULL for old fires)
#   CONT_DATE — date fire was contained (datetime, often NULL)
#   FIRE_NAME — name of the fire
#   GIS_ACRES — acreage from GIS polygon measurement
#   CAUSE — fire cause code

# Ensure YEAR_ is integer
if "YEAR_" in gdf.columns:
    gdf["year"] = pd.to_numeric(gdf["YEAR_"], errors="coerce").astype("Int64")
elif "YEAR" in gdf.columns:
    gdf["year"] = pd.to_numeric(gdf["YEAR"], errors="coerce").astype("Int64")
else:
    print("⚠️  No YEAR column found! Available columns:", list(gdf.columns))

# Parse ALARM_DATE
date_col = None
for candidate in ["ALARM_DATE", "ALARMDATE", "alarm_date"]:
    if candidate in gdf.columns:
        date_col = candidate
        break

if date_col:
    gdf["alarm_date"] = pd.to_datetime(gdf[date_col], errors="coerce")
    gdf["alarm_month"] = gdf["alarm_date"].dt.to_period("M")
    print(f"Parsed alarm dates from '{date_col}'")
else:
    gdf["alarm_date"] = pd.NaT
    gdf["alarm_month"] = pd.NaT
    print("⚠️  No ALARM_DATE column found!")

# Parse CONT_DATE similarly
for candidate in ["CONT_DATE", "CONTDATE", "cont_date"]:
    if candidate in gdf.columns:
        gdf["cont_date"] = pd.to_datetime(gdf[candidate], errors="coerce")
        break
else:
    gdf["cont_date"] = pd.NaT

# Standardize fire name
for candidate in ["FIRE_NAME", "FIRENAME", "fire_name", "FIRE NAME"]:
    if candidate in gdf.columns:
        gdf["fire_name"] = gdf[candidate].fillna("UNNAMED")
        break
else:
    gdf["fire_name"] = "UNNAMED"

# Standardize acreage
for candidate in ["GIS_ACRES", "GISACRES", "gis_acres", "Shape_Area"]:
    if candidate in gdf.columns:
        gdf["acres"] = pd.to_numeric(gdf[candidate], errors="coerce")
        break
else:
    # Compute from geometry if no acreage column
    gdf["acres"] = gdf.geometry.area / 4046.86  # sq meters to acres
    print("Computed acreage from geometry (no GIS_ACRES column found)")

# Standardize cause
for candidate in ["CAUSE", "cause"]:
    if candidate in gdf.columns:
        gdf["cause"] = gdf[candidate]
        break
else:
    gdf["cause"] = np.nan

print(f"\nYear range: {gdf['year'].min()} — {gdf['year'].max()}")
print(f"Fires with exact alarm date: {gdf['alarm_date'].notna().sum():,} / {len(gdf):,} "
      f"({gdf['alarm_date'].notna().mean():.1%})")
print(f"Fires with acreage data:     {gdf['acres'].notna().sum():,} / {len(gdf):,}")

# %% Cell B3: Date completeness by decade — informs our adaptive time-step
decade_completeness = (
    gdf.assign(decade=lambda x: (x["year"] // 10) * 10)
    .groupby("decade")
    .agg(
        n_fires=("year", "size"),
        has_alarm_date=("alarm_date", lambda s: s.notna().sum()),
        total_acres=("acres", "sum"),
    )
    .assign(pct_with_date=lambda x: x["has_alarm_date"] / x["n_fires"] * 100)
)

print("\nFire records by decade:")
print("=" * 75)
print(f"{'Decade':<10} {'Fires':>8} {'With Date':>12} {'% Dated':>10} {'Total Acres':>15}")
print("-" * 75)
for decade, row in decade_completeness.iterrows():
    if pd.isna(decade):
        continue
    print(
        f"{int(decade)}s{'':<5} {int(row['n_fires']):>8,} "
        f"{int(row['has_alarm_date']):>12,} "
        f"{row['pct_with_date']:>9.1f}% "
        f"{row['total_acres']:>15,.0f}"
    )

# %% Cell B4: Create the adaptive time_bin column
# This is the key column for animation: month-level where we have dates,
# year-level where we don't.

def make_time_bin(row):
    """Return 'YYYY-MM' if alarm_date is available, else 'YYYY'."""
    if pd.notna(row["alarm_date"]):
        return row["alarm_date"].strftime("%Y-%m")
    elif pd.notna(row["year"]):
        return str(int(row["year"]))
    return None

gdf["time_bin"] = gdf.apply(make_time_bin, axis=1)

# Also create a sortable numeric key for animation ordering
def time_bin_sort_key(tb):
    """Convert time_bin to a float for sorting: YYYY.0 or YYYY.MM"""
    if tb is None:
        return 0.0
    if "-" in str(tb):
        parts = str(tb).split("-")
        return float(parts[0]) + float(parts[1]) / 100
    return float(tb)

gdf["time_sort"] = gdf["time_bin"].apply(time_bin_sort_key)

print(f"Unique time bins: {gdf['time_bin'].nunique():,}")
print(f"\nSample time bins (first 10 sorted):")
for tb in sorted(gdf["time_bin"].dropna().unique(), key=time_bin_sort_key)[:10]:
    print(f"  {tb}")
print("  ...")
for tb in sorted(gdf["time_bin"].dropna().unique(), key=time_bin_sort_key)[-5:]:
    print(f"  {tb}")

# %% Cell B5: Fires per year — bar chart
fig, ax = plt.subplots(figsize=(16, 5))

fires_per_year = gdf.groupby("year").size()
ax.bar(fires_per_year.index.astype(float), fires_per_year.values,
       color="#ff6600", alpha=0.8, width=0.8)
ax.set_xlabel("Year", fontsize=12)
ax.set_ylabel("Number of Fires Recorded", fontsize=12)
ax.set_title("Cal Fire FRAP: Recorded Fires per Year", fontsize=14, fontweight="bold")
ax.set_facecolor("#f5f5f5")
ax.grid(axis="y", alpha=0.3)
ax.annotate(
    "Note: Increase partly reflects\nimproved record-keeping over time",
    xy=(0.02, 0.95), xycoords="axes fraction", fontsize=9,
    va="top", color="gray", fontstyle="italic",
)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "fires_per_year.png", dpi=150, bbox_inches="tight")
plt.show()

# %% Cell B6: Acreage distribution — log scale histogram + top fires table
fig, axes = plt.subplots(1, 2, figsize=(16, 5))

# Histogram of fire sizes (log scale)
ax = axes[0]
valid_acres = gdf["acres"].dropna()
valid_acres = valid_acres[valid_acres > 0]
ax.hist(np.log10(valid_acres), bins=60, color="#ff6600", alpha=0.8, edgecolor="white", linewidth=0.3)
ax.set_xlabel("Fire Size (log₁₀ acres)", fontsize=12)
ax.set_ylabel("Count", fontsize=12)
ax.set_title("Distribution of Fire Sizes", fontsize=14, fontweight="bold")
# Add reference lines
for threshold, label in [(10, "10 ac"), (100, "100 ac"), (1000, "1K ac"),
                          (10000, "10K ac"), (100000, "100K ac")]:
    ax.axvline(np.log10(threshold), color="gray", linestyle="--", alpha=0.5, linewidth=0.8)
    ax.text(np.log10(threshold), ax.get_ylim()[1] * 0.95, f" {label}", fontsize=8,
            color="gray", va="top")
ax.set_facecolor("#f5f5f5")

# Top 20 largest fires
ax = axes[1]
top_fires = (
    gdf.nlargest(20, "acres")[["fire_name", "year", "acres"]]
    .reset_index(drop=True)
)
ax.barh(range(len(top_fires)), top_fires["acres"],
        color="#ff6600", alpha=0.8)
ax.set_yticks(range(len(top_fires)))
ax.set_yticklabels(
    [f"{row.fire_name} ({int(row.year)})" for _, row in top_fires.iterrows()],
    fontsize=8,
)
ax.set_xlabel("Acres", fontsize=12)
ax.set_title("Top 20 Largest Fires", fontsize=14, fontweight="bold")
ax.invert_yaxis()
ax.set_facecolor("#f5f5f5")
ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1000:.0f}K"))

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "fire_size_distribution.png", dpi=150, bbox_inches="tight")
plt.show()

print("\nTop 20 Largest Fires:")
print(top_fires.to_string(index=False))

# %% Cell B7: Cumulative acres burned over time
cumulative = (
    gdf.groupby("year")["acres"]
    .sum()
    .sort_index()
    .cumsum()
)

fig, ax = plt.subplots(figsize=(16, 5))
ax.fill_between(cumulative.index.astype(float), cumulative.values,
                color="#ff6600", alpha=0.3)
ax.plot(cumulative.index.astype(float), cumulative.values,
        color="#ff6600", linewidth=1.5)
ax.set_xlabel("Year", fontsize=12)
ax.set_ylabel("Cumulative Acres Burned", fontsize=12)
ax.set_title("Cumulative Recorded Acres Burned in California", fontsize=14, fontweight="bold")
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))
ax.set_facecolor("#f5f5f5")
ax.grid(axis="y", alpha=0.3)
plt.tight_layout()
plt.savefig(OUTPUT_DIR / "cumulative_acres.png", dpi=150, bbox_inches="tight")
plt.show()

# %% Cell B8: Fire seasonality (for fires with exact dates)
dated_fires = gdf[gdf["alarm_date"].notna()].copy()
dated_fires["month"] = dated_fires["alarm_date"].dt.month

fig, axes = plt.subplots(1, 2, figsize=(16, 5))

# Count by month
ax = axes[0]
month_counts = dated_fires.groupby("month").size()
month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
colors = ["#ff6600" if m in [6, 7, 8, 9, 10] else "#ffa500" for m in range(1, 13)]
ax.bar(range(1, 13), [month_counts.get(m, 0) for m in range(1, 13)],
       color=colors, alpha=0.8)
ax.set_xticks(range(1, 13))
ax.set_xticklabels(month_names)
ax.set_ylabel("Number of Fires", fontsize=12)
ax.set_title("Fire Count by Month", fontsize=14, fontweight="bold")
ax.set_facecolor("#f5f5f5")

# Acres by month
ax = axes[1]
month_acres = dated_fires.groupby("month")["acres"].sum()
ax.bar(range(1, 13), [month_acres.get(m, 0) for m in range(1, 13)],
       color=colors, alpha=0.8)
ax.set_xticks(range(1, 13))
ax.set_xticklabels(month_names)
ax.set_ylabel("Total Acres Burned", fontsize=12)
ax.set_title("Acres Burned by Month", fontsize=14, fontweight="bold")
ax.set_facecolor("#f5f5f5")
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))

plt.tight_layout()
plt.savefig(OUTPUT_DIR / "fire_seasonality.png", dpi=150, bbox_inches="tight")
plt.show()

# %% Cell B9: Fire cause breakdown
if gdf["cause"].notna().any():
    # FRAP cause codes: 1=Lightning, 2=Equipment Use, 4=Campfire, 5=Debris,
    # 6=Railroad, 7=Arson, 9=Miscellaneous, 10=Vehicle, 11=Power Line,
    # 14=Unknown/Unidentified, etc.
    cause_map = {
        1: "Lightning", 2: "Equipment", 3: "Smoking", 4: "Campfire",
        5: "Debris Burning", 6: "Railroad", 7: "Arson", 8: "Playing with Fire",
        9: "Miscellaneous", 10: "Vehicle", 11: "Power Line", 12: "Firefighter Training",
        13: "Non-Firefighter Training", 14: "Unknown", 15: "Structure",
        16: "Aircraft", 17: "Volcanic", 18: "Escaped Prescribed Burn", 19: "Illegal Alien Campfire"
    }

    cause_counts = (
        gdf["cause"]
        .dropna()
        .astype(int)
        .map(cause_map)
        .fillna("Other")
        .value_counts()
        .head(12)
    )

    fig, ax = plt.subplots(figsize=(10, 6))
    cause_counts.plot(kind="barh", ax=ax, color="#ff6600", alpha=0.8)
    ax.set_xlabel("Number of Fires", fontsize=12)
    ax.set_title("Fire Causes (Top 12)", fontsize=14, fontweight="bold")
    ax.invert_yaxis()
    ax.set_facecolor("#f5f5f5")
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "fire_causes.png", dpi=150, bbox_inches="tight")
    plt.show()
else:
    print("No cause data available in this release.")

# %% Cell B10: Download California boundary for basemap
def get_california_boundary() -> gpd.GeoDataFrame:
    """Download and extract California state boundary."""
    ca_shp = CA_BOUNDARY_PATH / "california.shp"

    if ca_shp.exists():
        print(f"✓ California boundary already cached")
        return gpd.read_file(ca_shp)

    # Try downloading Census TIGER state boundaries
    tiger_zip = RAW_DIR / "tl_2023_us_state.zip"
    success = download_file(CA_BOUNDARY_URL, tiger_zip)

    if success:
        states = gpd.read_file(f"zip://{tiger_zip}")
        ca = states[states["STUSPS"] == "CA"].copy()
        CA_BOUNDARY_PATH.mkdir(parents=True, exist_ok=True)
        ca.to_file(ca_shp)
        print(f"✓ Extracted California boundary")
        return ca

    # Fallback: extract CA outline from fire perimeters convex hull
    print("⚠️  Using convex hull of fire perimeters as fallback boundary")
    return gpd.GeoDataFrame(
        geometry=[gdf.to_crs(CRS_WGS84).unary_union.convex_hull],
        crs=CRS_WGS84,
    )


ca_boundary = get_california_boundary().to_crs(CRS_CA_ALBERS)
print(f"California boundary CRS: {ca_boundary.crs}")

# %% Cell B11: Static overview map — all fire perimeters colored by decade
fig, ax = plt.subplots(figsize=(10, 14))
ax.set_facecolor(BG_COLOR)
fig.patch.set_facecolor(BG_COLOR)

# Draw California outline
ca_boundary.boundary.plot(ax=ax, color=CA_OUTLINE_COLOR, linewidth=1)

# Color fires by decade
decade_cmap = plt.cm.YlOrRd
gdf_plot = gdf[gdf["year"].notna()].copy()
gdf_plot["decade"] = (gdf_plot["year"] // 10) * 10

# Normalize decades to colormap range
min_decade = gdf_plot["decade"].min()
max_decade = gdf_plot["decade"].max()

for decade, group in gdf_plot.groupby("decade"):
    if pd.isna(decade):
        continue
    norm_val = (decade - min_decade) / (max_decade - min_decade + 1)
    color = decade_cmap(norm_val)
    group.plot(ax=ax, color=color, alpha=0.4, linewidth=0)

# Legend
unique_decades = sorted(gdf_plot["decade"].dropna().unique())
# Show every other decade to avoid clutter
legend_decades = unique_decades[::2]
legend_patches = [
    mpatches.Patch(
        color=decade_cmap((d - min_decade) / (max_decade - min_decade + 1)),
        label=f"{int(d)}s",
        alpha=0.7,
    )
    for d in legend_decades
]
ax.legend(
    handles=legend_patches, loc="lower left", fontsize=8,
    facecolor="#1a1a1a", edgecolor="#333", labelcolor=TEXT_COLOR,
    title="Decade", title_fontsize=9,
)

ax.set_title(
    "California Wildfire History\nAll Recorded Fire Perimeters",
    fontsize=16, fontweight="bold", color=YEAR_TEXT_COLOR, pad=15,
)
ax.set_axis_off()
plt.tight_layout()
plt.savefig(
    OUTPUT_DIR / "all_fires_by_decade.png", dpi=200, bbox_inches="tight",
    facecolor=BG_COLOR,
)
plt.show()

# %% Cell B12: Summary statistics for deciding animation start year
print("\n" + "=" * 60)
print("SUMMARY: Fires by era (to help choose animation start year)")
print("=" * 60)

eras = [
    ("Pre-1900", gdf[gdf["year"] < 1900]),
    ("1900–1919", gdf[(gdf["year"] >= 1900) & (gdf["year"] < 1920)]),
    ("1920–1949", gdf[(gdf["year"] >= 1920) & (gdf["year"] < 1950)]),
    ("1950–1979", gdf[(gdf["year"] >= 1950) & (gdf["year"] < 1980)]),
    ("1980–1999", gdf[(gdf["year"] >= 1980) & (gdf["year"] < 2000)]),
    ("2000–2024", gdf[gdf["year"] >= 2000]),
]

for label, subset in eras:
    dated_pct = subset["alarm_date"].notna().mean() * 100 if len(subset) > 0 else 0
    print(
        f"  {label:<12}  {len(subset):>6,} fires  |  "
        f"{subset['acres'].sum():>12,.0f} acres  |  "
        f"{dated_pct:>5.1f}% with exact dates"
    )

print(f"\n→ Current animation start year in config: {ANIMATION_START_YEAR}")
print(f"  (Change ANIMATION_START_YEAR in config.py to adjust)")

# %% Cell B13: Simplify geometries and save processed data for Sections C & D
print("Simplifying geometries for faster rendering...")
gdf_simplified = gdf.copy()
gdf_simplified["geometry"] = gdf.geometry.simplify(
    tolerance=SIMPLIFY_TOLERANCE_M, preserve_topology=True
)

# Calculate how much we saved
original_pts = sum(g.coords.__len__() if hasattr(g, 'coords') else 0
                   for g in gdf.geometry if g is not None)
simplified_pts = sum(g.coords.__len__() if hasattr(g, 'coords') else 0
                     for g in gdf_simplified.geometry if g is not None)

# Save to processed dir for use by other notebooks
processed_path = PROCESSED_DIR / "fire_perimeters_processed.gpkg"
gdf_simplified.to_file(processed_path, driver="GPKG")
print(f"✓ Saved processed data: {processed_path}")
print(f"  File size: {processed_path.stat().st_size / 1e6:.1f} MB")

# Also save a WGS84 version for web maps
gdf_wgs84 = gdf_simplified.to_crs(CRS_WGS84)
wgs84_path = PROCESSED_DIR / "fire_perimeters_wgs84.gpkg"
gdf_wgs84.to_file(wgs84_path, driver="GPKG")
print(f"✓ Saved WGS84 version: {wgs84_path}")

print(f"\n{'='*60}")
print(f"Section B complete! Ready for interactive exploration (Section C).")
print(f"{'='*60}")
print(f"\nKey numbers:")
print(f"  Total fire records:    {len(gdf):,}")
print(f"  Year range:            {gdf['year'].min()} — {gdf['year'].max()}")
print(f"  With exact dates:      {gdf['alarm_date'].notna().sum():,}")
print(f"  Unique time bins:      {gdf['time_bin'].nunique():,}")
print(f"  Total acres in dataset: {gdf['acres'].sum():,.0f}")
