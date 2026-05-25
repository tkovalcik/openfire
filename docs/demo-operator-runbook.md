# OpenFire Demo Operator Runbook

## Current Demo Backend

- Cloud Run URL: `https://openfire-api-ivdriimizq-uc.a.run.app`
- Region: `us-central1`
- Service: `openfire-api`
- Project: `${GCP_PROJECT_ID}`

## Frozen Demo Artifacts

- Model: `gs://${OPENFIRE_GCS_BUCKET}/openfire/demo/models/openfire-baseline/model-20260401T190000Z/model.joblib`
- Features: `gs://${OPENFIRE_GCS_BUCKET}/openfire/demo/features/sample-bayarea/ds-20260401T190000Z/features.csv`
- GeoJSON: `gs://${OPENFIRE_GCS_BUCKET}/openfire/demo/geojson/risk-layer/demo-20260401T190000Z/risk.geojson`

## Exact Deploy Command

```bash
gcloud run deploy openfire-api \
  --image=us-central1-docker.pkg.dev/${GCP_PROJECT_ID}/openfire/openfire-api:IMAGE_TAG \
  --project=${GCP_PROJECT_ID} \
  --region=us-central1 \
  --platform=managed \
  --port=8000 \
  --cpu=1 \
  --memory=1Gi \
  --concurrency=20 \
  --timeout=60s \
  --min-instances=1 \
  --max-instances=5 \
  --execution-environment=gen2 \
  --service-account=<API_RUNTIME_SERVICE_ACCOUNT> \
  --allow-unauthenticated \
  --env-vars-file=cloudrun.demo.env.yaml
```

## Exact Verification Commands

```bash
curl https://openfire-api-ivdriimizq-uc.a.run.app/health
curl https://openfire-api-ivdriimizq-uc.a.run.app/metadata
curl https://openfire-api-ivdriimizq-uc.a.run.app/demo/geojson
curl -X POST https://openfire-api-ivdriimizq-uc.a.run.app/predict \
  -H "Content-Type: application/json" \
  --data @frontend/data/sample_predict_request.json
```

## Exact Rollback Flow

List revisions:

```bash
gcloud run revisions list \
  --project=${GCP_PROJECT_ID} \
  --region=us-central1 \
  --service=openfire-api
```

Roll back to a known-good revision:

```bash
gcloud run services update-traffic openfire-api \
  --project=${GCP_PROJECT_ID} \
  --region=us-central1 \
  --to-revisions=REVISION_NAME=100
```

## Frontend Demo

```bash
python3 -m http.server 8080
```

Open:

```text
http://127.0.0.1:8080/frontend/?backend=https://openfire-api-ivdriimizq-uc.a.run.app
```
