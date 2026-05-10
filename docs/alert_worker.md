# OpenFire Alert Worker

The alert worker evaluates captured UI subscriptions against the latest SoCal
risk window. By default it runs in dry-run mode and prints the email alerts it
would send.

## Local dry-run

```bash
.venv/bin/python -m src.ui_socal.alert_worker \
  --manifest-uri frontend-socal-deckgl/data/socal_demo_manifest.json \
  --subscriptions-json /tmp/openfire_subscriptions.json \
  --zip-centroids-json /tmp/openfire_zip_centroids.json
```

## BigQuery inputs

Subscriptions default to:

```text
msds603-mlops-project.openfire_features.ui_subscriptions
```

Expected columns:

```text
created_at TIMESTAMP
email STRING
zip STRING
risk_threshold FLOAT64
```

ZIP centroids default to:

```text
msds603-mlops-project.openfire_features.zip_centroids
```

Expected columns:

```text
zip STRING
latitude FLOAT64
longitude FLOAT64
```

If either table is empty, the worker exits successfully and emits zero alert
candidates.

Alert delivery history defaults to:

```text
msds603-mlops-project.openfire_features.ui_alert_deliveries
```

Expected columns are auto-created by the worker when real sending is enabled:

```text
delivered_at TIMESTAMP
email STRING
zip STRING
window_start_date STRING
risk_probability FLOAT64
threshold FLOAT64
provider STRING
model_version STRING
```

The worker uses this table as a cooldown log. With the default
`--alert-cooldown-hours 120`, the same email + ZIP + prediction window will not
be sent again within 5 days.

## Prepare ZIP centroids

Validate a local ZIP centroid CSV or JSON before writing:

```bash
.venv/bin/python -m src.ui_socal.zip_centroids \
  --source-csv path/to/zip_centroids.csv
```

Expected CSV columns:

```text
zip,latitude,longitude
```

JSON can use either supported worker fixture shape:

```json
{"90001": {"latitude": 34.01, "longitude": -118.01}}
```

or:

```json
[{"zip": "90001", "latitude": 34.01, "longitude": -118.01}]
```

Replace the BigQuery table after validation:

```bash
.venv/bin/python -m src.ui_socal.zip_centroids \
  --source-csv path/to/zip_centroids.csv \
  --write
```

## Deployment

Manual deployment is in:

```text
.github/workflows/alert_worker.yml
```

It builds `docker/Dockerfile.alert_worker`, creates or updates the Cloud Run Job
`openfire-alert-worker`, and can optionally execute the job.

Real email sending is disabled unless the workflow input `send=true` is set.
That path requires:

```text
OPENFIRE_ALERT_FROM_EMAIL
CLOUD_RUN_SECRET_SENDGRID_API_KEY
```

`CLOUD_RUN_SECRET_SENDGRID_API_KEY` should be the name of a GCP Secret Manager
secret containing the SendGrid API key.
