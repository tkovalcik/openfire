from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def main() -> None:
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI")
    experiment_name = os.getenv("MLFLOW_EXPERIMENT_NAME", "team-project")

    if not tracking_uri:
        raise SystemExit("MLFLOW_TRACKING_URI must be set before running this smoke test.")

    try:
        import mlflow
    except ImportError as error:  # pragma: no cover - depends on local environment
        raise SystemExit(
            "mlflow is not installed in the current environment. "
            "Run: python -m pip install mlflow==2.17.2"
        ) from error

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)

    run_name = f"smoke-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_param("smoke_test", True)
        mlflow.log_param("project", "openfire")
        mlflow.log_metric("ping", 1.0)
        mlflow.set_tag("source", "scripts/mlflow_remote_smoke.py")
        print(f"tracking_uri={tracking_uri}")
        print(f"experiment_name={experiment_name}")
        print(f"run_id={run.info.run_id}")
        experiment_id = run.info.experiment_id
        print(f"run_url={tracking_uri}/#/experiments/{experiment_id}/runs/{run.info.run_id}")
        print(f"experiment_url={tracking_uri}/#/experiments/{experiment_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise
