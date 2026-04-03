# OpenFire MLflow Team Server Runbook

This runbook sets up the **course-required shared MLflow tracking server** on a single
Google Compute Engine VM inside the existing `msds603-mlops-project` project.

This MLflow server is **separate** from the deployed OpenFire demo:

- the demo API stays on Cloud Run
- the demo model stays GCS-backed
- MLflow is used for experiment tracking and team collaboration

## Architecture

- VM name: `mlflow-server`
- Zone: `us-central1-a`
- Machine type: `e2-small`
- OS: Ubuntu 22.04 LTS
- Disk: `20GB`
- MLflow port: `5000`
- Backend store: local SQLite on the VM
- Artifact root: local directory on the VM

Storage layout on the VM:

- DB: `/home/$USER/mlflow-data/mlflow.db`
- Artifacts: `/home/$USER/mlflow-data/artifacts`
- Logs: `/home/$USER/mlflow.log`

Current team tracking URL:

- `http://34.58.62.126:5000`

## 1. Create the VM and firewall rule

Run on your laptop:

```bash
gcloud auth login
gcloud config set project msds603-mlops-project
gcloud services enable compute.googleapis.com

gcloud compute instances create mlflow-server \
  --zone=us-central1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=mlflow-server

gcloud compute firewall-rules create allow-mlflow \
  --allow=tcp:5000 \
  --target-tags=mlflow-server \
  --description="Allow MLflow server traffic"
```

Verify:

```bash
gcloud compute instances list
gcloud compute firewall-rules list --filter="name=allow-mlflow"
```

## 2. Install MLflow on the VM

SSH into the VM:

```bash
gcloud compute ssh mlflow-server --zone=us-central1-a
```

Run on the VM:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install python3-pip python3-venv -y

python3 --version
pip3 --version

python3 -m venv ~/mlflow-venv
source ~/mlflow-venv/bin/activate
python -m pip install --upgrade pip
python -m pip install mlflow==2.17.2

mkdir -p ~/mlflow-data/artifacts
```

## 3. Start the MLflow tracking server

Run on the VM:

```bash
source ~/mlflow-venv/bin/activate

nohup mlflow server \
  --backend-store-uri sqlite:////home/$USER/mlflow-data/mlflow.db \
  --default-artifact-root /home/$USER/mlflow-data/artifacts \
  --host 0.0.0.0 \
  --port 5000 > ~/mlflow.log 2>&1 &
```

Verify on the VM:

```bash
ps aux | grep mlflow
tail -n 50 ~/mlflow.log
```

Expected log output includes a listener on `0.0.0.0:5000`.

## 4. Find the external IP

Run on your laptop:

```bash
gcloud compute instances list
```

Use the `EXTERNAL_IP` from the `mlflow-server` row. The current VM IP is `34.58.62.126`.

MLflow UI URL:

```text
http://EXTERNAL_IP:5000
```

## 5. Point OpenFire training at the remote MLflow server

In your local `.env`, set:

```dotenv
MLFLOW_TRACKING_URI=http://EXTERNAL_IP:5000
MLFLOW_EXPERIMENT_NAME=team-project
```

Do not change the deployed demo settings:

```dotenv
OPENFIRE_RUNTIME_MODE=demo
OPENFIRE_MODEL_SOURCE=gcs
```

## 6. Smoke-test remote tracking

Run:

```bash
set -a
source .env
set +a
conda run -n mlop python scripts/mlflow_remote_smoke.py
```

This should create a run in the shared experiment and print the run ID.

## 7. Train against the remote MLflow server

Example:

```bash
set -a
source .env
set +a

PYTHONPATH=src python -m model.train_baseline \
  --dataset-uri gs://$OPENFIRE_GCS_BUCKET/openfire/datasets/processed/openfire_baseline/ds-20260401T000000Z/dataset.parquet \
  --output-dir .cache/openfire/train_baseline \
  --model-type auto \
  --split-strategy year \
  --validation-values 2024 \
  --model-name openfire-baseline \
  --tracking-uri "$MLFLOW_TRACKING_URI" \
  --experiment-name "$MLFLOW_EXPERIMENT_NAME"
```

## 8. Submission checklist

Capture all of these:

- tracking server address: `http://EXTERNAL_IP:5000`
- screenshot of the MLflow UI with the external IP visible in the browser URL
- experiment name visible
- at least one run visible with logged parameters or metrics

## Notes

- This VM setup is intentionally **course-simple**, not production-hardened.
- It uses local SQLite and local artifact storage on the VM because that is what the course asks for.
- The OpenFire Cloud Run demo remains GCS-backed for reliability and does not depend on MLflow at runtime.
