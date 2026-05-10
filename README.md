# OpenFire

OpenFire is a wildfire risk assessment ML platform for four Southern California counties (Kern, Los Angeles, San Luis Obispo, Santa Barbara). The capstone pipeline ingests Sentinel-2 and gridMET data via Google Earth Engine, engineers lag-delta features in BigQuery, trains an XGBoost classifier, and runs scheduled inference every 5 days — publishing predictions as GeoJSON snapshots consumed by a live Leaflet map.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full data-flow diagram and component guide.

---

## Demo Video

A 2:37 walkthrough of the OpenFire SoCal UI and pipeline.

https://github.com/tkovalcik/openfire/raw/dev/docs/demo.mp4

If the inline player above does not load, [download `docs/demo.mp4`](docs/demo.mp4) directly.

---

## ML pipeline — quickstart

### Prerequisites

- `openfire` conda env (or equivalent with dependencies below)
- GCP credentials: `gcloud auth application-default login`
- Environment variables set (copy `.env.example` → `.env` and populate):
  - `MLFLOW_TRACKING_URI` — MLflow server URL
  - `OPENFIRE_GCS_BUCKET` — GCS bucket name
  - `GCP_PROJECT_ID` — GCP project ID

### Training

Reads gold Parquet shards from GCS, trains XGBoost with 2024 as the holdout year, and registers the model as `openfire-gold` in MLflow.

```bash
conda activate openfire
python -m src.pipelines.train \
  --gcs-prefix "gs://${OPENFIRE_GCS_BUCKET}/openfire/datasets/gold/" \
  --validation-year 2024 \
  --mlflow-tracking-uri "${MLFLOW_TRACKING_URI}"
```

Typical runtime: ~25 min on a local machine with 16+ GiB RAM (or run via the `train.yml` GitHub Actions workflow on Cloud Run Job `openfire-train`).

### Inference — manual run

Process a single grid-aligned window (smoke test):

```bash
python -m src.pipelines.run_inference_pipeline \
  --mode window --date 2026-04-17
```

Process a historical range (skipping GEE if silver is already populated):

```bash
python -m src.pipelines.run_inference_pipeline \
  --mode backfill --start 2026-01-01 --end 2026-04-17 \
  --from-step engineer_gold
```

Catch up to the most recent unprocessed grid date (same mode Cloud Scheduler uses):

```bash
python -m src.pipelines.run_inference_pipeline --mode latest
```

Dry-run (prints the window plan, touches nothing):

```bash
python -m src.pipelines.run_inference_pipeline --mode latest --dry-run
```

### Viewing predictions

The live frontend reads `manifest.json` from GCS to find the latest GeoJSON snapshot.

Check the current frontier:

```bash
bq query --nouse_legacy_sql \
  "SELECT MAX(window_start_date) FROM \`${GCP_PROJECT_ID}.openfire_features.predictions_history\`"
```

### Scheduled inference

Cloud Scheduler fires daily at 08:00 UTC and triggers the inference Cloud Run Job in `--mode latest`. No manual action required for steady-state operation.

To force a run immediately:

```bash
gcloud scheduler jobs run openfire-inference-daily \
  --location us-central1
```

---

OpenFire is a cloud-storage-first geospatial ML repository for satellite-based wildfire risk
assessment. The repository is intentionally opinionated:

- canonical datasets live in GCS
- canonical model bundles live in GCS
- MLflow stores experiment metadata in its backend DB and artifacts in GCS
- serving reads the production model from GCS first, MLflow registry second, and `local://` only
  for development
- local disk is temporary cache only, never the source of truth for team workflows

## Recommended Storage Architecture

Primary approach:

- one class-project bucket: `gs://<OPENFIRE_GCS_BUCKET>/openfire/...`
- FastAPI loads the promoted serving bundle directly from GCS
- MLflow still tracks runs and model lineage, but serving does not depend on MLflow availability

Fallbacks:

- MLflow registry loading remains supported for teams with a reachable MLflow control plane
- `local://...` remains supported for local development and tests

Tradeoff:

- direct GCS serving is simpler for Cloud Run and rollback
- MLflow registry fallback preserves experiment-driven model lookup

## Bucket Contract

Recommended layout:

- `gs://<bucket>/openfire/datasets/raw/<dataset_name>/<version>/...`
- `gs://<bucket>/openfire/datasets/processed/<dataset_name>/<version>/dataset.parquet`
- `gs://<bucket>/openfire/datasets/processed/<dataset_name>/<version>/manifest.json`
- `gs://<bucket>/openfire/features/<feature_set>/<extract_run_id>/...`
- `gs://<bucket>/openfire/training/runs/<train_run_id>/...`
- `gs://<bucket>/openfire/models/candidates/<model_name>/<train_run_id>/<candidate_name>/...`
- `gs://<bucket>/openfire/models/registered/<model_name>/<stage>/model.joblib`
- `gs://<bucket>/openfire/models/registered/<model_name>/<stage>/metadata.json`
- `gs://<bucket>/openfire/monitoring/reports/<report_name>/<run_id>/report.html`
- `gs://<bucket>/openfire/monitoring/reports/<report_name>/<run_id>/metadata.json`
- `gs://<bucket>/openfire/mlflow-artifacts/...`

Version defaults:

- dataset version: `ds-YYYYMMDDTHHMMSSZ`
- feature extraction run: `fx-YYYYMMDDTHHMMSSZ`
- training run: `train-YYYYMMDDTHHMMSSZ`
- model bundle version: `model-YYYYMMDDTHHMMSSZ`
- serving stages: `development`, `staging`, `production`

## Environment Contract

Create `.env` from `.env.example` and populate:

```dotenv
GCP_PROJECT_ID=
OPENFIRE_GCS_BUCKET=
OPENFIRE_STORAGE_ROOT_PREFIX=openfire
OPENFIRE_DATASET_PREFIX=datasets
OPENFIRE_FEATURE_PREFIX=features
OPENFIRE_TRAINING_PREFIX=training
OPENFIRE_MODEL_PREFIX=models
OPENFIRE_MONITORING_PREFIX=monitoring
OPENFIRE_MLFLOW_ARTIFACT_PREFIX=mlflow-artifacts
OPENFIRE_LOCAL_CACHE_DIR=.cache/openfire

OPENFIRE_RUNTIME_MODE=demo
OPENFIRE_MODEL_SOURCE=gcs
OPENFIRE_MODEL_URI=
OPENFIRE_DEMO_MODEL_URI=gs://<bucket>/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib
OPENFIRE_DEMO_FEATURES_URI=gs://<bucket>/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv
OPENFIRE_DEMO_GEOJSON_URI=gs://<bucket>/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson
OPENFIRE_CORS_ALLOWED_ORIGINS=http://127.0.0.1:8080,http://localhost:8080
OPENFIRE_MAX_BATCH_SIZE=1000
OPENFIRE_MODEL_LOAD_RETRIES=3
OPENFIRE_MODEL_LOAD_BACKOFF_SECONDS=2.0

MLFLOW_TRACKING_URI=
MLFLOW_REGISTERED_MODEL_NAME=openfire-baseline
MLFLOW_MODEL_STAGE=production
MLFLOW_EXPERIMENT_NAME=team-project

GOOGLE_APPLICATION_CREDENTIALS=
```

Notes:

- `GOOGLE_APPLICATION_CREDENTIALS` is for local development only.
- Cloud Run should use the runtime service account instead of JSON key files.
- `OPENFIRE_RUNTIME_MODE=demo` is the recommended demo-safe default.
- In demo mode, the frontend should use the frozen demo GeoJSON and the API should use the frozen
  demo model URI unless you explicitly override `OPENFIRE_MODEL_URI`.
- For Cloud Run and demos, prefer versioned `gs://` object URIs. Do not rely on overwriting the
  same object path and expecting a running service to pick up changes.

## Local Development Setup

Install the API/runtime dependencies including explicit GCS support:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install \
  fastapi==0.115.0 \
  uvicorn[standard]==0.32.0 \
  pydantic==2.10.3 \
  pandas==2.2.3 \
  numpy==2.1.3 \
  scikit-learn==1.6.1 \
  joblib==1.4.2 \
  mlflow-skinny==2.17.2 \
  google-cloud-storage==2.18.2 \
  pytest
cp .env.example .env
```

Local development can use explicit `local://` URIs:

```bash
export OPENFIRE_MODEL_SOURCE=local
export OPENFIRE_MODEL_URI=local://artifacts/model.joblib
```

## Frozen Demo Path

Tier 1 demo reliability depends on one known-good frozen path in GCS rather than the most fragile
live pipeline. The recommended contract is:

- `gs://<bucket>/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib`
- `gs://<bucket>/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv`
- `gs://<bucket>/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson`

Manual population example:

```bash
gcloud storage cp artifacts/model.joblib \
  "gs://$OPENFIRE_GCS_BUCKET/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib"

gcloud storage cp data/sample_features.csv \
  "gs://$OPENFIRE_GCS_BUCKET/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv"

gcloud storage cp frontend/data/sample_risk.geojson \
  "gs://$OPENFIRE_GCS_BUCKET/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson"
```

The repo now includes a matching demo feature CSV at
[sample_features.csv](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/data/sample_features.csv), so
you do not need to generate it manually for the frozen demo contract.

Recommended demo config:

```bash
export OPENFIRE_RUNTIME_MODE=demo
export OPENFIRE_MODEL_SOURCE=gcs
export OPENFIRE_DEMO_MODEL_URI="gs://$OPENFIRE_GCS_BUCKET/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib"
export OPENFIRE_DEMO_GEOJSON_URI="gs://$OPENFIRE_GCS_BUCKET/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson"
```

## Weather Enrichment

The weather module uses Open-Meteo archive APIs and now accepts storage URIs.

Local example:

```bash
PYTHONPATH=src python -m data_pipeline.weather_features \
  --input-uri local://data/pixel_features.csv \
  --output-uri local://data/pixel_features_weather.csv \
  --location-mode aoi_centroid
```

GCS example:

```bash
PYTHONPATH=src python -m data_pipeline.weather_features \
  --input-uri gs://$OPENFIRE_GCS_BUCKET/openfire/features/pixels/fx-20260401T000000Z/features.csv \
  --output-uri gs://$OPENFIRE_GCS_BUCKET/openfire/features/pixels/fx-20260401T000000Z/features_weather.csv \
  --location-mode aoi_centroid
```

## Dataset Assembly

The dataset assembler reads feature, label, and weather CSVs from storage URIs and writes a
canonical parquet dataset plus manifest.

Local example:

```bash
PYTHONPATH=src python -m data_pipeline.build_dataset \
  --features-uri local://data/ee_features.csv \
  --labels-uri local://data/frap_labels.csv \
  --weather-uri local://data/weather_features.csv \
  --out-parquet-uri local://data/openfire_baseline.parquet \
  --out-manifest-uri local://data/openfire_baseline_manifest.json
```

Canonical GCS example:

```bash
PYTHONPATH=src python -m data_pipeline.build_dataset \
  --features-uri gs://$OPENFIRE_GCS_BUCKET/openfire/features/sentinel2/fx-20260401T000000Z/features.csv \
  --labels-uri gs://$OPENFIRE_GCS_BUCKET/openfire/datasets/raw/frap_labels/ds-20260401T000000Z/labels.csv \
  --weather-uri gs://$OPENFIRE_GCS_BUCKET/openfire/features/weather/fx-20260401T000000Z/weather.csv \
  --out-parquet-uri gs://$OPENFIRE_GCS_BUCKET/openfire/datasets/processed/openfire_baseline/ds-20260401T000000Z/dataset.parquet \
  --out-manifest-uri gs://$OPENFIRE_GCS_BUCKET/openfire/datasets/processed/openfire_baseline/ds-20260401T000000Z/manifest.json
```

## Baseline Training

The training pipeline uses a grouped temporal or fire-event split baseline and avoids naive random
pixel splits:

- `year` split keeps all pixels from the holdout year in validation only
- `fire_event` split keeps all pixels from the holdout fire event in validation only

Full spatial holdout is not yet implemented in this branch.

Training reads the assembled dataset from `local://` or `gs://`, writes local temporary work
artifacts, uploads canonical candidate artifacts, and promotes the best serving bundle to the
registered model prefix.

Local example:

```bash
PYTHONPATH=src python -m model.train_baseline \
  --dataset-uri local://data/openfire_baseline.parquet \
  --output-dir .cache/openfire/train_baseline \
  --model-type auto \
  --split-strategy year \
  --validation-values 2024 \
  --model-name openfire-baseline
```

GCS-backed example:

```bash
PYTHONPATH=src python -m model.train_baseline \
  --dataset-uri gs://$OPENFIRE_GCS_BUCKET/openfire/datasets/processed/openfire_baseline/ds-20260401T000000Z/dataset.parquet \
  --output-dir .cache/openfire/train_baseline \
  --model-type auto \
  --split-strategy year \
  --validation-values 2024 \
  --model-name openfire-baseline \
  --tracking-uri "$MLFLOW_TRACKING_URI" \
  --experiment-name openfire \
  --promote-stage production
```

Expected outputs:

- candidate artifacts uploaded to `models/candidates/...`
- best serving bundle promoted to `models/registered/<model>/<stage>/model.joblib`
- serving metadata promoted to `models/registered/<model>/<stage>/metadata.json`
- evaluation summary written locally and uploaded to the candidate prefix

## Serving

Serving is stateless and Cloud Run friendly. Model resolution order is explicit:

1. `OPENFIRE_MODEL_SOURCE=gcs` and `OPENFIRE_MODEL_URI=gs://...`
2. `OPENFIRE_MODEL_SOURCE=mlflow` with `MLFLOW_TRACKING_URI` and `MLFLOW_REGISTERED_MODEL_NAME`
3. `OPENFIRE_MODEL_SOURCE=local` with `OPENFIRE_MODEL_URI=local://...`

Tier 1 serving behavior is intentionally reliability-first:

- the app starts in degraded mode if model loading fails
- `GET /health` and `GET /metadata` still work in degraded mode
- prediction endpoints return a clean `503` with `error=model_unavailable`
- public metadata does not expose raw internal storage URIs
- model loading is retried with bounded backoff before startup degrades

Local smoke test:

```bash
export OPENFIRE_MODEL_SOURCE=local
export OPENFIRE_MODEL_URI=local://artifacts/model.joblib
PYTHONPATH=src uvicorn serving.app:app --host 0.0.0.0 --port 8000
```

Cloud-oriented example:

```bash
export OPENFIRE_MODEL_SOURCE=gcs
export OPENFIRE_MODEL_URI=gs://$OPENFIRE_GCS_BUCKET/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib
PYTHONPATH=src uvicorn serving.app:app --host 0.0.0.0 --port 8000
```

## Frontend

The frontend lives in [frontend/index.html](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/index.html) and stays deliberately static:

- plain HTML/CSS/JS
- Leaflet for mapping
- no build step
- explicit `demo` and `live` modes in `frontend/config.js`
- demo mode uses the backend `/demo/geojson` endpoint by default
- live mode uses the API-backed `/predict_geojson` flow

Files:

- [frontend/index.html](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/index.html)
- [frontend/styles.css](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/styles.css)
- [frontend/config.js](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/config.js)
- [frontend/app.js](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/app.js)
- [frontend/data/sample_risk.geojson](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/data/sample_risk.geojson)
- [frontend/data/sample_predict_request.json](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/data/sample_predict_request.json)

### Local serve instructions

Serve the frontend from the repo root:

```bash
python3 -m http.server 8080
```

Then open:

```text
http://127.0.0.1:8080/frontend/
```

### SoCal AOI UI

A separate static UI for the GEE AOI MVP lives in
[frontend-socal/index.html](/Users/tomas/Documents/02d-USF-MSDS/MSDS603/openfire/frontend-socal/index.html).
It is isolated from the existing Bay Area demo UI and uses checked-in static assets:

- [frontend-socal/data/aoi_counties.geojson](/Users/tomas/Documents/02d-USF-MSDS/MSDS603/openfire/frontend-socal/data/aoi_counties.geojson)
- [frontend-socal/data/socal_demo_manifest.json](/Users/tomas/Documents/02d-USF-MSDS/MSDS603/openfire/frontend-socal/data/socal_demo_manifest.json)
- [frontend-socal/data/socal_20240726_risk.geojson](/Users/tomas/Documents/02d-USF-MSDS/MSDS603/openfire/frontend-socal/data/socal_20240726_risk.geojson)

From the same repo-root static server, open:

```text
http://127.0.0.1:8080/frontend-socal/
```

The SoCal UI renders the canonical four-county training AOI from `src/pipelines/aoi.py`
and includes a time slider/playback control. Local development uses the checked-in
synthetic 65,000-point fixture; the deployed `openfire-ui-socal` Cloud Run service
reads `/data/manifest.json` and `/data/predictions_YYYYMMDD.geojson` through its
same-origin GCS proxy to `gs://openfire/predictions/`.

Deployments are handled by `.github/workflows/ui_socal.yml`. The service is separate
from Sebastian's `openfire-api` production demo so UI iteration does not affect the
serving API.

The GitHub deployer service account must be able to act as the SoCal UI runtime
service account. Grant `roles/iam.serviceAccountUser` on the runtime service
account configured by `SERVICE_ACCOUNT` in `.github/workflows/ui_socal.yml` to
the GitHub deployer service account configured by `GCP_DEPLOYER_SERVICE_ACCOUNT`;
without that binding the workflow can build and push the image, but Cloud Run
deployment fails with `iam.serviceaccounts.actAs` denied.

If you also want API-backed fetches, run the backend in a second terminal:

```bash
PYTHONPATH=src uvicorn serving.app:app --host 0.0.0.0 --port 8000
```

For local frontend access, set `OPENFIRE_CORS_ALLOWED_ORIGINS` to the exact origins you want, for
example `http://127.0.0.1:8080,http://localhost:8080`.

### Demo mode

Default config in [frontend/config.js](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/config.js):

```javascript
runtimeMode: "demo",
sources: {
  demo: {
    mode: "api-geojson",
    url: "http://127.0.0.1:8000/demo/geojson",
    metadataUrl: "http://127.0.0.1:8000/metadata",
    requestBodyUrl: "./data/sample_predict_request.json",
    fallbackInferenceDate: "2026-04-01",
    fallbackDataWindow: "2024-06-01 to 2024-06-30",
  },
}
```

This mode fetches the frozen backend GeoJSON layer and reads:

- feature probabilities for styling
- top-level `metadata.model_version`
- top-level `metadata.inference_date`
- top-level `metadata.data_window`

### Switch to API-backed fetches

You have two clean options in [frontend/config.js](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/config.js):

1. Backend already returns GeoJSON:

```javascript
runtimeMode: "demo",
sources: {
  demo: {
    mode: "api-geojson",
    url: "http://127.0.0.1:8000/some_geojson_endpoint",
    metadataUrl: "http://127.0.0.1:8000/metadata",
  },
}
```

Use this when your backend exposes a `FeatureCollection` directly, or returns
`{ feature_collection: ... }`.

2. POST rows to the existing FastAPI prediction endpoint:

```javascript
runtimeMode: "live",
sources: {
  live: {
    mode: "predict-geojson",
    url: "http://127.0.0.1:8000/predict_geojson",
    metadataUrl: "http://127.0.0.1:8000/metadata",
    requestBodyUrl: "./data/sample_predict_request.json",
    fallbackInferenceDate: "2026-04-01",
    fallbackDataWindow: "2024-06-01 to 2024-06-30",
  },
}
```

This mode:

- loads rows from [frontend/data/sample_predict_request.json](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/frontend/data/sample_predict_request.json)
- posts them to `/predict_geojson`
- renders `response.feature_collection`
- pulls model version from the API response and `/metadata`

The current FastAPI response does not include inference date or data window, so those fields use the
frontend fallback values until you decide to add them to backend metadata or the GeoJSON payload.

## Monitoring

Monitoring outputs should be written through `src/monitoring/io_utils.py`, which writes report HTML
and metadata JSON to canonical storage URIs under:

- `openfire/monitoring/reports/<report_name>/<run_id>/report.html`
- `openfire/monitoring/reports/<report_name>/<run_id>/metadata.json`

## Weekly Inference Workflow Status

The GitHub Actions weekly inference workflow is manual-only on this branch.

- scheduled automation is intentionally disabled
- the branch does not yet contain a complete Earth Engine extractor and labeler path
- manual `workflow_dispatch` with a frozen or pre-extracted feature CSV is the supported demo-safe
  path

## Course MLflow Team Server

The course MLflow requirement is intentionally handled as a **separate Compute Engine VM** from the
deployed OpenFire demo.

- Cloud Run demo serving stays `OPENFIRE_MODEL_SOURCE=gcs`
- the MLflow VM is used for experiment tracking and shared team runs
- the VM uses SQLite and local VM artifacts because that is what the course requirement expects

Course-simple MLflow server shape:

- VM name: `mlflow-server`
- zone: `us-central1-a`
- machine type: `e2-small`
- OS: Ubuntu 22.04
- MLflow UI: `http://EXTERNAL_IP:5000`
- current team MLflow URL: `http://34.58.62.126:5000`

Do not point the deployed Cloud Run demo at MLflow. Keep serving on the frozen GCS-backed demo
artifacts, and use MLflow only for training and experiment tracking.

Full step-by-step instructions live in
[docs/mlflow-team-server-runbook.md](/Users/sebastiansteen/Desktop/MSDS/MLOps/openfire/docs/mlflow-team-server-runbook.md).

To smoke-test remote tracking after the VM is up:

```bash
set -a
source .env
set +a
conda run -n mlop python scripts/mlflow_remote_smoke.py
```

## Minimal Manual GCP Setup

Create the canonical bucket:

```bash
export GCP_PROJECT_ID="YOUR_PROJECT_ID"
export REGION="us-central1"
export OPENFIRE_GCS_BUCKET="YOUR_BUCKET_NAME"

gcloud storage buckets create "gs://${OPENFIRE_GCS_BUCKET}" \
  --project="${GCP_PROJECT_ID}" \
  --location="${REGION}" \
  --uniform-bucket-level-access
```

Optional placeholder prefixes:

```bash
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/datasets/raw/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/datasets/processed/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/features/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/training/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/models/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/monitoring/.keep"
printf '' | gcloud storage cp - "gs://${OPENFIRE_GCS_BUCKET}/openfire/mlflow-artifacts/.keep"
```

Create the API runtime service account:

```bash
gcloud iam service-accounts create openfire-api-runner \
  --display-name="OpenFire API runtime"
```

Grant minimum IAM:

```bash
gcloud storage buckets add-iam-policy-binding "gs://${OPENFIRE_GCS_BUCKET}" \
  --member="serviceAccount:openfire-api-runner@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
```

Only if you inject runtime values from Secret Manager:

```bash
printf '%s' "$MLFLOW_TRACKING_URI" | \
gcloud secrets create openfire-mlflow-tracking-uri --data-file=-

printf '%s' "$OPENFIRE_MODEL_URI" | \
gcloud secrets create openfire-model-uri --data-file=-

gcloud secrets add-iam-policy-binding openfire-mlflow-tracking-uri \
  --member="serviceAccount:openfire-api-runner@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding openfire-model-uri \
  --member="serviceAccount:openfire-api-runner@${GCP_PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

Artifact Registry is part of deployment, not the storage foundation itself, so it is not required
for this step.

## Acceptance Checks

- dataset assembly can read `local://` and `gs://` URIs
- training uploads candidate artifacts and promotes a serving bundle
- serving loads the configured model source without local hardcoded paths
- monitoring helpers write report outputs to canonical URIs
- local development still works with explicit `local://` URIs
