import ee
from google.cloud import storage
import sys

# --- Configuration ---
GCP_PROJECT_ID = "msds603-mlops-project"
BUCKET_NAME = "openfire"
SILVER_PREFIX = "openfire/datasets/silver/fire/cal-frap-shapefiles"

# Change these two variables next year (e.g., "fire25", "2025")
TARGET_FILE_IDENTIFIER = "fire24" 
ASSET_YEAR_SUFFIX = "2024"

EE_ASSET_ID = f"projects/{GCP_PROJECT_ID}/assets/calfire_frap_perimeters_{ASSET_YEAR_SUFFIX}"

def get_shapefile_uris(bucket_name, prefix, identifier):
    """Scans GCS and returns the required shapefile URIs matching the identifier."""
    print(f"Scanning gs://{bucket_name}/{prefix} for '{identifier}'...")
    
    storage_client = storage.Client()
    blobs = storage_client.list_blobs(bucket_name, prefix=prefix)
    
    uris = []
    required_extensions = ('.shp', '.shx', '.dbf', '.prj')
    
    for blob in blobs:
        # Check if the file matches our target year and is one of the required extensions
        if identifier in blob.name and blob.name.endswith(required_extensions):
            uris.append(f"gs://{bucket_name}/{blob.name}")
            
    # Validation: Earth Engine requires exactly these 4 files to build the table
    if len(uris) != 4:
        print(f"Error: Found {len(uris)} matching files. Earth Engine requires exactly 4 (.shp, .shx, .dbf, .prj).")
        print(f"Files found: {uris}")
        sys.exit(1)
        
    print("Found complete shapefile set:")
    for uri in uris:
        print(f" - {uri}")
        
    return uris

def ingest_to_gee(uris):
    """Triggers the Earth Engine ingestion task."""
    ee.Initialize(project=GCP_PROJECT_ID)
    print(f"\nTriggering Earth Engine ingestion for asset: {EE_ASSET_ID}...")
    
    task_id = ee.data.newTaskId()[0]
    
    # Isolate strictly the .shp file for the API payload
    shp_uri = next(uri for uri in uris if uri.endswith('.shp'))
    
    request = {
        'id': EE_ASSET_ID,
        'sources': [
            {'uris': [shp_uri]} # Pass ONLY the .shp URI
        ]
    }
    
    try:
        task = ee.data.startTableIngestion(task_id, request)
        print(f"Earth Engine ingestion task started successfully: {task['id']}")
        print("You can monitor the task in the Earth Engine Code Editor Tasks tab.")
    except Exception as e:
        print(f"Failed to start ingestion task: {e}")

def main():
    # 1. Dynamically locate the staged files
    target_uris = get_shapefile_uris(BUCKET_NAME, SILVER_PREFIX, TARGET_FILE_IDENTIFIER)
    
    # 2. Trigger the ingestion
    ingest_to_gee(target_uris)

if __name__ == "__main__":
    main()