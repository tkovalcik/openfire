# Data

This directory contains local data files used in the OpenFire project. Raw and processed data files are **not tracked in version control** (see `.gitignore`). This README documents all data sources and how to reproduce them.

---

## Directory Structure

```
data/
├── raw/            # Unprocessed source data — never modify these files
├── processed/      # Cleaned, transformed, and feature-engineered data
└── README.md       # This file
```

---

## Data Sources

### Sentinel-2 Satellite Imagery

- **Source**: [Copernicus Sentinel-2](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) via Google Earth Engine
- **Collection**: `COPERNICUS/S2_SR_HARMONIZED` (Surface Reflectance, atmospherically corrected)
- **Bands of interest**: B2 (Blue), B3 (Green), B4 (Red), B8 (NIR), B11 (SWIR1), B12 (SWIR2)
- **Derived indices**: NDVI, NBR, NDMI (computed server-side in GEE)
- **Temporal compositing**: 30-day median composites to handle cloud coverage
- **Spatial features**: Neighborhood statistics computed via `reduceNeighborhood()` in GEE
- **How to reproduce**: Run `src/data/gee_export.py` (requires authenticated GEE access)

### Cal Fire FRAP Burn Perimeters (Ground Truth Labels)

- **Source**: [CAL FIRE Fire and Resource Assessment Program (FRAP)](https://frap.fire.ca.gov/frap-projects/fire-perimeters/)
- **Dataset**: Fire perimeters — historical burn area boundaries
- **Use**: Binary labels for supervised classification (burned vs. not burned)
- **Format**: Shapefile / GeoJSON
- **How to reproduce**: Download from FRAP website or run `src/data/download_frap.py`

### Weather Data

- **Source**: TBD (likely GRIDMET via GEE or NOAA)
- **Variables**: Temperature, humidity, wind speed, precipitation
- **How to reproduce**: TBD

### Terrain Data

- **Source**: TBD (likely USGS SRTM via GEE)
- **Variables**: Elevation, slope, aspect
- **How to reproduce**: TBD

---

## Reproduction Steps

1. **Authenticate with Google Earth Engine**
   ```bash
   earthengine authenticate
   ```

2. **Export Sentinel-2 composites**
   ```bash
   python src/data/gee_export.py --region <region> --start-date <date> --end-date <date>
   ```
   Exported files will be saved to `data/raw/`.

3. **Download FRAP burn perimeters**
   ```bash
   python src/data/download_frap.py
   ```
   Or download manually from the [FRAP website](https://frap.fire.ca.gov/frap-projects/fire-perimeters/) and place in `data/raw/`.

4. **Run preprocessing**
   ```bash
   python src/data/preprocess.py
   ```
   Outputs cleaned and merged data to `data/processed/`.

---

## Important Notes

- **Do not commit data files.** All files in `raw/` and `processed/` are excluded via `.gitignore`. This includes `.csv`, `.tif`, `.geotiff`, `.parquet`, and other data formats.
- **Most processing happens server-side in GEE.** Local data files are primarily for cached exports, EDA, and model training. The production pipeline reads from GEE directly.
- **Reproducibility matters.** If you add a new data source, update this README with the source, format, and reproduction steps before merging your PR.
