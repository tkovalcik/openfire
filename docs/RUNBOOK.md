# OpenFire — Operator Runbook

Operational reference for running OpenFire end to end. For project intent and architecture, see [README](../README.md) and [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Environment

OpenFire is cloud-storage-first: canonical datasets and model bundles live in GCS, MLflow stores experiment metadata, and serving reads from GCS by default. Local disk is a temporary cache, never a source of truth.

### Required environment variables

Copy `.env.example` → `.env` and populate:

| Variable | Purpose |
| --- | --- |
| `GCP_PROJECT_ID` | GCP project hosting BigQuery, GCS, and Cloud Run |
| `OPENFIRE_GCS_BUCKET` | Canonical bucket for datasets, models, predictions, and monitoring |
| `MLFLOW_TRACKING_URI` | MLflow tracking server URL |
| `OPENFIRE_MODEL_SOURCE` | `gcs` (recommended), `mlflow`, or `local` |
| `OPENFIRE_MODEL_URI` | URI of the model bundle (`gs://...` or `local://...`) |
| `MLFLOW_REGISTERED_MODEL_NAME` | Used when `OPENFIRE_MODEL_SOURCE=mlflow` |
| `MLFLOW_MODEL_STAGE` | `production`, `staging`, etc. |
| `OPENFIRE_CORS_ALLOWED_ORIGINS` | Comma-separated origins permitted to call the API |

On Cloud Run, prefer the runtime service account and Secret Manager over JSON key files. `GOOGLE_APPLICATION_CREDENTIALS` is for local development only.

### GCS bucket layout

```text
gs://${OPENFIRE_GCS_BUCKET}/openfire/
  datasets/raw/<dataset>/<version>/...
  datasets/processed/<dataset>/<version>/dataset.parquet
  datasets/gold/gold_features_*.parquet
  features/<feature_set>/<extract_run_id>/...
  training/runs/<train_run_id>/...
  models/candidates/<model_name>/<train_run_id>/...
  models/registered/<model_name>/<stage>/model.joblib
  monitoring/reports/<report_name>/<run_id>/report.html
  predictions/predictions_YYYYMMDD.geojson
  predictions/manifest.json
```

Version slugs use the pattern `ds-YYYYMMDDTHHMMSSZ`, `train-YYYYMMDDTHHMMSSZ`, `model-YYYYMMDDTHHMMSSZ`. Serving stages: `development`, `staging`, `production`.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # populate as above
```

For local model loading without GCS:

```bash
export OPENFIRE_MODEL_SOURCE=local
export OPENFIRE_MODEL_URI=local://artifacts/model.joblib
```

---

## Training

`src/pipelines/train.py` reads gold Parquet shards from GCS, trains XGBoost with `scale_pos_weight = negatives / positives`, logs ROC AUC, PR AUC, precision, accuracy, F1, and calibration metrics to MLflow, saves a `model.joblib` bundle, and promotes the registered model version to Production when registration is enabled.

```bash
python -m src.pipelines.train \
  --gcs-prefix "gs://${OPENFIRE_GCS_BUCKET}/openfire/datasets/gold/" \
  --validation-year 2024 \
  --mlflow-tracking-uri "${MLFLOW_TRACKING_URI}"
```

Typical runtime: ~25 min on 16+ GiB RAM. The `train.yml` GitHub Actions workflow runs the same command on the `openfire-train` Cloud Run Job.

---

## Inference

`src/pipelines/run_inference_pipeline.py` supports three modes:

| Mode | Use |
| --- | --- |
| `--mode latest` | Scheduler path. Catches up to the most recent unprocessed grid date. |
| `--mode backfill --start YYYY-MM-DD --end YYYY-MM-DD` | Historical reprocessing over a date range. |
| `--mode window --date YYYY-MM-DD` | One exact grid-aligned window (smoke tests, manual reruns). |

Per-window step chain:

```text
extract_gee → append_silver → engineer_gold → load_model → predict → write_outputs → monitor → log_summary
```

All steps are idempotent at the window level. If GEE extraction and silver append are already complete, resume from `engineer_gold`:

```bash
python -m src.pipelines.run_inference_pipeline \
  --mode backfill --start 2026-01-07 --end 2026-04-17 \
  --from-step engineer_gold
```

Dry-run plans the windows but touches nothing:

```bash
python -m src.pipelines.run_inference_pipeline --mode latest --dry-run
```

---

## Scheduled inference

Cloud Scheduler fires daily and triggers the `openfire-inference` Cloud Run Job in `--mode latest`. No manual action required for steady-state operation.

Force an immediate run:

```bash
gcloud scheduler jobs run openfire-inference-daily --location us-central1
```

Check the current frontier:

```bash
bq query --nouse_legacy_sql \
  "SELECT MAX(window_start_date) FROM \`${GCP_PROJECT_ID}.openfire_features.predictions_history\`"
```

---

## Serving

Model resolution order is explicit:

1. `OPENFIRE_MODEL_SOURCE=gcs` and `OPENFIRE_MODEL_URI=gs://...`
2. `OPENFIRE_MODEL_SOURCE=mlflow` with `MLFLOW_TRACKING_URI` and `MLFLOW_REGISTERED_MODEL_NAME`
3. `OPENFIRE_MODEL_SOURCE=local` with `OPENFIRE_MODEL_URI=local://...`

If model loading fails, the API starts in degraded mode: `GET /health` and `GET /metadata` still work, prediction endpoints return `503 model_unavailable`. Model loading is retried with bounded backoff before startup degrades.

Local smoke test:

```bash
export OPENFIRE_MODEL_SOURCE=local
export OPENFIRE_MODEL_URI=local://artifacts/model.joblib
PYTHONPATH=src uvicorn serving.app:app --host 0.0.0.0 --port 8000
```

Cloud-oriented example:

```bash
export OPENFIRE_MODEL_SOURCE=gcs
export OPENFIRE_MODEL_URI="gs://${OPENFIRE_GCS_BUCKET}/openfire/models/registered/openfire-gold/production/model.joblib"
PYTHONPATH=src uvicorn serving.app:app --host 0.0.0.0 --port 8000
```

---

## Monitoring

The inference `monitor` step compares each `gold_features_inference` window against a sampled training reference using Evidently. Outputs:

```text
gs://${OPENFIRE_GCS_BUCKET}/monitoring/reports/report_YYYYMMDD.html
gs://${OPENFIRE_GCS_BUCKET}/monitoring/snapshots/snapshot_YYYYMMDD.json
gs://${OPENFIRE_GCS_BUCKET}/monitoring/index.json
```

The `openfire-monitoring` Cloud Run service is read-only and serves the historical report index plus a `/summary/latest` endpoint.

---

## Frontend

The SoCal UI is static (HTML/CSS/JS, no build step). The FastAPI service in `src/ui_socal/app.py` serves the static files and proxies `/data/*` to private prediction objects in GCS using the runtime service account, so the browser does not need bucket CORS or public reads.

Local run:

```bash
PYTHONPATH=src uvicorn ui_socal.app:app --host 127.0.0.1 --port 8090
```

Then open <http://127.0.0.1:8090/>.

The UI reads `/data/manifest.json`, builds the timeline from `manifest.windows`, and fetches `full`, `z8`, or `z9` snapshots depending on zoom. Older manifests fall back to deterministic client-side downsampling.

---

## Output writer

`src/pipelines/output_writer.py` writes three prediction outputs per window:

1. `predictions_history`: partition-scoped `WRITE_TRUNCATE` into `predictions_history$YYYYMMDD`.
2. Gzipped GeoJSON snapshots: `predictions_YYYYMMDD.geojson`, `predictions_YYYYMMDD_z8.geojson`, `predictions_YYYYMMDD_z9.geojson`.
3. `manifest.json`: latest frontier plus sorted `windows` metadata, including available `geojson_variants`.

Helpers:

- `scripts/rebuild_manifest_from_bucket.py` — rebuild the manifest index from existing snapshots.
- `scripts/backfill_snapshot_variants.py` — create missing historical `z8` / `z9` variants without rerunning GEE, BigQuery, or model scoring.

---

## See also

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — data flow, contracts, design decisions
- [`docs/mlflow-team-server-runbook.md`](mlflow-team-server-runbook.md) — MLflow server provisioning
- [`docs/team_deploy.md`](team_deploy.md) — deployment walkthrough
- [`docs/demo-operator-runbook.md`](demo-operator-runbook.md) — demo-mode operations
- [`docs/alert_worker.md`](alert_worker.md) — alert worker setup
