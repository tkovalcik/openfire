import os
import zipfile
import requests
import re
import urllib.parse
import tempfile
import fiona
import geopandas as gpd
from google.cloud import storage

# --- Configuration ---
AZURE_URL = "https://34c031f8-c9fd-4018-8c5a-4159cdff6b0d-cdn-endpoint.azureedge.net/-/media/calfire-website/what-we-do/fire-resource-assessment-program---frap/gis-data/2025/fire241gdb.ashx"
BUCKET_NAME = "openfire"

# Medallion Architecture Paths
BRONZE_PREFIX_ZIP = "openfire/datasets/bronze/fire/cal-frap/raw_zips"
BRONZE_PREFIX_GDB = "openfire/datasets/bronze/fire/cal-frap/raw_gdb"
SILVER_PREFIX_SHP = "openfire/datasets/silver/fire/cal-frap-shapefiles"

def download_and_extract(url, extract_dir):
    """Downloads the zip, attempts to keep original filename, and extracts."""
    print("Downloading dataset...")
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        
        cd = r.headers.get('content-disposition')
        filename = None
        if cd:
            fname_match = re.findall('filename="?([^"]+)"?', cd)
            if fname_match:
                filename = fname_match[0]
                
        if not filename:
            parsed_url = urllib.parse.urlparse(url)
            base_name = os.path.basename(parsed_url.path).split('.')[0] 
            filename = f"{base_name}.zip"
            
        local_zip = os.path.join(extract_dir, filename)
        with open(local_zip, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
                
    print(f"Dataset downloaded as: {filename}")
    
    print("Extracting dataset...")
    with zipfile.ZipFile(local_zip, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)
    
    gdb_dirs = [d for d in os.listdir(extract_dir) if d.endswith('.gdb')]
    if not gdb_dirs:
        raise FileNotFoundError("No .gdb directory found in the extracted files.")
    
    return os.path.join(extract_dir, gdb_dirs[0]), local_zip

def upload_directory_to_gcs(bucket, local_dir, gcs_prefix):
    """Recursively uploads a local directory to GCS."""
    uploaded_uris = []
    for root, _, files in os.walk(local_dir):
        for file in files:
            local_path = os.path.join(root, file)
            relative_path = os.path.relpath(local_path, local_dir)
            blob_path = f"{gcs_prefix}/{relative_path}".replace("\\", "/")
            
            blob = bucket.blob(blob_path)
            blob.upload_from_filename(local_path)
            uploaded_uris.append(f"gs://{bucket.name}/{blob_path}")
            
    return uploaded_uris

def main():
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        # 1. Download and Extract
        gdb_path, local_zip = download_and_extract(AZURE_URL, tmp_dir)
        
        # Extract the base filename (e.g., "fire241gdb" from "fire241gdb.zip")
        zip_filename = os.path.basename(local_zip)
        base_filename = os.path.splitext(zip_filename)[0] 
        
        # 2. Upload Bronze Zip
        print("Uploading Bronze ZIP to GCS...")
        zip_blob = bucket.blob(f"{BRONZE_PREFIX_ZIP}/{zip_filename}")
        zip_blob.upload_from_filename(local_zip)
        
        # 3. Upload Bronze GDB
        print("Uploading Bronze GDB to GCS...")
        gdb_folder_name = os.path.basename(gdb_path)
        upload_directory_to_gcs(bucket, gdb_path, f"{BRONZE_PREFIX_GDB}/{gdb_folder_name}")
        
        # 4. Convert to Shapefile
        print("Converting to Shapefile...")
        layers = fiona.listlayers(gdb_path)
        target_layer = next((layer for layer in layers if 'firep' in layer.lower()), layers[0])
        
        gdf = gpd.read_file(gdb_path, layer=target_layer)
        gdf = gdf.to_crs("EPSG:4326") 
        
        shapefile_dir = os.path.join(tmp_dir, "shapefiles")
        os.makedirs(shapefile_dir, exist_ok=True)
        
        # Use the dynamic base filename for the shapefile exports
        shapefile_out_path = os.path.join(shapefile_dir, f"{base_filename}.shp")
        gdf.to_file(shapefile_out_path, driver='ESRI Shapefile')
        
        # 5. Upload Silver Shapefiles
        print("Uploading Silver Shapefiles to GCS...")
        gcs_uris = upload_directory_to_gcs(bucket, shapefile_dir, SILVER_PREFIX_SHP)
        
        print("\n=== STAGING COMPLETE ===")
        print("Use these URIs in your Earth Engine ingestion script:")
        for uri in gcs_uris:
            print(uri)

if __name__ == "__main__":
    main()
