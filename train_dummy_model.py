"""
train_dummy_model.py — OpenFire Dummy Model for Milestone Submission
====================================================================

Standalone script that:
  1. Generates synthetic wildfire-risk data matching the serving schema
  2. Trains a RandomForestClassifier on it
  3. Saves a joblib "serving bundle" (the format model_loader.py expects)
  4. Logs the bundle + metrics to the team's remote MLflow server
  5. Registers and promotes the model version to "Production" stage

Usage:
    pip install scikit-learn mlflow joblib numpy pandas
    python train_dummy_model.py --tracking-uri http://<EXTERNAL_IP>:5000

No dependencies on the rest of the OpenFire codebase — intentionally
self-contained so you can run it from anywhere.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
LOGGER = logging.getLogger("openfire.train_dummy")

# ---------------------------------------------------------------------------
# Feature columns — must exactly match src/serving/schemas.py FEATURE_COLUMNS
# so the serving layer accepts predictions from this model.
# ---------------------------------------------------------------------------
FEATURE_COLUMNS = [
    "latitude",
    "longitude",
    "NDVI",
    "EVI",
    "NDWI",
    "NBR",
    "B2",
    "B3",
    "B4",
    "B8",
    "B11",
    "B12",
    "elevation",
    "slope",
    "aspect",
    "mean_ndvi_100m",
    "mean_ndvi_500m",
    "precip_7d_sum",
    "precip_30d_sum",
    "temp_7d_mean",
    "temp_30d_mean",
    "humidity_7d_mean",
    "humidity_30d_mean",
    "wind_7d_max",
    "wind_30d_max",
]

TARGET_COLUMN = "target_burned"


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------
def generate_synthetic_data(
    n_samples: int = 2000,
    fire_ratio: float = 0.15,
    random_state: int = 42,
) -> pd.DataFrame:
    """
    Generate synthetic wildfire-risk features with realistic value ranges.

    The label is loosely correlated with the features to give the RF
    something to learn — high temp, low humidity, low NDVI, and high
    wind increase fire probability.  The model will overfit (it's
    synthetic data), but the serving pipeline doesn't care.

    Parameters
    ----------
    n_samples : int
        Number of rows to generate.
    fire_ratio : float
        Approximate fraction of positive (burned) labels.
    random_state : int
        Seed for reproducibility.
    """
    rng = np.random.default_rng(random_state)

    # --- Location: California bounding box (approx) ---
    lat = rng.uniform(32.5, 42.0, n_samples)        # degrees N
    lon = rng.uniform(-124.5, -114.0, n_samples)     # degrees W

    # --- Sentinel-2 spectral indices ---
    #   Ranges based on typical surface reflectance values
    ndvi = rng.uniform(0.05, 0.85, n_samples)        # veg index
    evi = ndvi * rng.uniform(0.6, 0.9, n_samples)    # correlated w/ NDVI
    ndwi = rng.uniform(-0.5, 0.5, n_samples)         # water index
    nbr = rng.uniform(-0.3, 0.7, n_samples)          # burn ratio

    # --- Sentinel-2 raw bands (surface reflectance, ~0–5000 scale) ---
    b2 = rng.uniform(100, 2500, n_samples)            # Blue
    b3 = rng.uniform(200, 3000, n_samples)            # Green
    b4 = rng.uniform(150, 3500, n_samples)            # Red
    b8 = rng.uniform(500, 5000, n_samples)            # NIR
    b11 = rng.uniform(200, 4000, n_samples)           # SWIR-1
    b12 = rng.uniform(100, 3500, n_samples)           # SWIR-2

    # --- Terrain (SRTM 30 m) ---
    elevation = rng.uniform(0, 3500, n_samples)       # meters
    slope = rng.uniform(0, 55, n_samples)             # degrees
    aspect = rng.uniform(0, 360, n_samples)           # degrees from N

    # --- Spatial context (neighborhood means) ---
    mean_ndvi_100m = ndvi + rng.normal(0, 0.05, n_samples)
    mean_ndvi_100m = np.clip(mean_ndvi_100m, -1.0, 1.0)
    mean_ndvi_500m = ndvi + rng.normal(0, 0.10, n_samples)
    mean_ndvi_500m = np.clip(mean_ndvi_500m, -1.0, 1.0)

    # --- Weather (Open-Meteo) ---
    temp_7d = rng.uniform(5, 42, n_samples)           # °C
    temp_30d = temp_7d + rng.normal(0, 3, n_samples)  # correlated
    humidity_7d = rng.uniform(10, 95, n_samples)      # %
    humidity_30d = humidity_7d + rng.normal(0, 5, n_samples)
    humidity_30d = np.clip(humidity_30d, 0, 100)
    humidity_7d = np.clip(humidity_7d, 0, 100)
    wind_7d = rng.uniform(0, 25, n_samples)           # m/s max
    wind_30d = wind_7d + rng.uniform(0, 10, n_samples)
    precip_7d = rng.exponential(5, n_samples)         # mm sum
    precip_30d = precip_7d * rng.uniform(2, 6, n_samples)

    # --- Build a "fire risk score" so the label has signal ---
    #   Higher temp, lower humidity, lower NDVI, higher wind → more risk
    risk_score = (
        0.30 * ((temp_7d - 5) / 37)             # normalized temp
        + 0.25 * (1 - humidity_7d / 100)          # inverse humidity
        + 0.20 * (1 - ndvi)                       # dry vegetation
        + 0.15 * (wind_7d / 25)                   # wind
        + 0.10 * rng.uniform(0, 1, n_samples)     # noise
    )
    # Convert to binary label at a threshold that gives ~fire_ratio
    threshold = np.quantile(risk_score, 1 - fire_ratio)
    target = (risk_score >= threshold).astype(int)

    df = pd.DataFrame(
        {
            "latitude": lat,
            "longitude": lon,
            "NDVI": ndvi,
            "EVI": evi,
            "NDWI": ndwi,
            "NBR": nbr,
            "B2": b2,
            "B3": b3,
            "B4": b4,
            "B8": b8,
            "B11": b11,
            "B12": b12,
            "elevation": elevation,
            "slope": slope,
            "aspect": aspect,
            "mean_ndvi_100m": mean_ndvi_100m,
            "mean_ndvi_500m": mean_ndvi_500m,
            "precip_7d_sum": precip_7d,
            "precip_30d_sum": precip_30d,
            "temp_7d_mean": temp_7d,
            "temp_30d_mean": temp_30d,
            "humidity_7d_mean": humidity_7d,
            "humidity_30d_mean": humidity_30d,
            "wind_7d_max": wind_7d,
            "wind_30d_max": wind_30d,
            TARGET_COLUMN: target,
        }
    )

    LOGGER.info(
        "Generated %d rows  |  %.1f%% positive (burned)",
        len(df),
        100 * target.mean(),
    )
    return df


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def train_model(
    df: pd.DataFrame,
    random_state: int = 42,
) -> tuple[RandomForestClassifier, dict[str, float], pd.DataFrame, pd.DataFrame]:
    """Train an RF and return (estimator, metrics_dict, X_test, y_test)."""

    X = df[FEATURE_COLUMNS]
    y = df[TARGET_COLUMN]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.25, random_state=random_state, stratify=y,
    )

    LOGGER.info(
        "Train: %d rows (%d pos)  |  Test: %d rows (%d pos)",
        len(X_train), y_train.sum(), len(X_test), y_test.sum(),
    )

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=None,
        min_samples_leaf=2,
        class_weight="balanced_subsample",
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    # --- Evaluation metrics ---
    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)

    metrics = {
        "roc_auc": float(roc_auc_score(y_test, y_prob)),
        "average_precision": float(average_precision_score(y_test, y_prob)),
        "f1": float(f1_score(y_test, y_pred)),
        "precision": float(precision_score(y_test, y_pred)),
        "recall": float(recall_score(y_test, y_pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, y_pred)),
    }

    for name, value in metrics.items():
        LOGGER.info("  %s = %.4f", name, value)

    return model, metrics, X_test, y_test


# ---------------------------------------------------------------------------
# MLflow logging + registration
# ---------------------------------------------------------------------------
def log_to_mlflow(
    model: RandomForestClassifier,
    metrics: dict[str, float],
    *,
    tracking_uri: str,
    experiment_name: str,
    model_name: str,
    promote_stage: str,
) -> str:
    """
    Log the trained model to the team MLflow server and register it.

    The serving layer (model_loader.py) expects:
      1. A registered model version with a matching name + stage
      2. An artifact called "model.joblib" on that version's run
      3. The joblib file contains a dict with at minimum a "model" key
         holding a sklearn estimator with a .predict() method

    Returns the registered model version string.
    """
    import mlflow
    from mlflow.tracking import MlflowClient

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)
    client = MlflowClient(tracking_uri=tracking_uri)

    LOGGER.info("Logging to MLflow at %s", tracking_uri)

    with mlflow.start_run(run_name="dummy-rf-synthetic") as run:
        # -- Log parameters --
        mlflow.log_param("model_type", "random_forest")
        mlflow.log_param("n_estimators", 200)
        mlflow.log_param("data_source", "synthetic")
        mlflow.log_param("n_features", len(FEATURE_COLUMNS))

        # -- Log metrics --
        for name, value in metrics.items():
            mlflow.log_metric(name, value)

        # -- Build the serving bundle dict --
        #    This matches the format that train_baseline.py produces
        #    and that model_loader.py._bundle_to_loaded_model() expects.
        bundle = {
            "model": model,
            "feature_columns": FEATURE_COLUMNS,
            "dataset_version_info": {
                "note": ["synthetic dummy data — replace with real pipeline"],
            },
            "decision_threshold": 0.5,
            "split_strategy": None,
            "split_group_column": None,
            "validation_groups": [],
            "model_version": f"dummy-rf-{run.info.run_id[:8]}",
            "model_name": model_name,
            "candidate_name": "random_forest",
            "bundle_schema_version": "openfire.serving_bundle.v1",
        }

        # -- Save bundle to a temp file and log as artifact --
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "model.joblib"
            joblib.dump(bundle, bundle_path)
            mlflow.log_artifact(str(bundle_path))
            LOGGER.info("Logged model.joblib artifact (run_id=%s)", run.info.run_id)

        run_id = run.info.run_id

    # -- Register the model version --
    #    Create the registered model if it doesn't exist yet.
    try:
        client.create_registered_model(model_name)
        LOGGER.info("Created registered model: %s", model_name)
    except Exception:
        LOGGER.info("Registered model '%s' already exists — reusing", model_name)

    # The "source" for create_model_version is just a reference URI.
    # The loader uses run_id to download artifacts, so the source value
    # is informational. We point it at the run's artifact directory.
    artifact_uri = f"runs:/{run_id}"
    version = client.create_model_version(
        name=model_name,
        source=artifact_uri,
        run_id=run_id,
    )
    LOGGER.info(
        "Registered model version %s for '%s'",
        version.version, model_name,
    )

    # -- Promote to the target stage (e.g., "Production") --
    client.transition_model_version_stage(
        name=model_name,
        version=version.version,
        stage=promote_stage,
        archive_existing_versions=True,
    )
    LOGGER.info(
        "Promoted version %s → stage '%s'",
        version.version, promote_stage,
    )

    return version.version


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train a dummy RF on synthetic data and register in MLflow.",
    )
    parser.add_argument(
        "--tracking-uri",
        required=True,
        help="MLflow tracking server URI, e.g. http://34.xxx.xxx.xxx:5000",
    )
    parser.add_argument(
        "--experiment-name",
        default="openfire-baseline",
        help="MLflow experiment name (default: openfire-baseline)",
    )
    parser.add_argument(
        "--model-name",
        default="openfire-baseline",
        help="Registered model name (default: openfire-baseline)",
    )
    parser.add_argument(
        "--promote-stage",
        default="Production",
        help="Model stage to promote to (default: Production)",
    )
    parser.add_argument(
        "--n-samples",
        type=int,
        default=2000,
        help="Number of synthetic training rows (default: 2000)",
    )
    parser.add_argument(
        "--random-state",
        type=int,
        default=42,
    )
    args = parser.parse_args()

    # Step 1: Generate synthetic data
    LOGGER.info("=== Step 1/3: Generating synthetic data ===")
    df = generate_synthetic_data(
        n_samples=args.n_samples,
        random_state=args.random_state,
    )

    # Step 2: Train the model
    LOGGER.info("=== Step 2/3: Training RandomForest ===")
    model, metrics, _, _ = train_model(df, random_state=args.random_state)

    # Step 3: Log to MLflow and register
    LOGGER.info("=== Step 3/3: Logging to MLflow ===")
    version = log_to_mlflow(
        model,
        metrics,
        tracking_uri=args.tracking_uri,
        experiment_name=args.experiment_name,
        model_name=args.model_name,
        promote_stage=args.promote_stage,
    )

    LOGGER.info("=== Done! ===")
    LOGGER.info("  Model version : %s", version)
    LOGGER.info("  Experiment    : %s", args.experiment_name)
    LOGGER.info("  Registry name : %s", args.model_name)
    LOGGER.info("  Stage         : %s", args.promote_stage)
    LOGGER.info("")
    LOGGER.info("Next steps:")
    LOGGER.info("  1. Open %s in your browser", args.tracking_uri)
    LOGGER.info("  2. Verify the run appears under experiment '%s'", args.experiment_name)
    LOGGER.info("  3. Check Models tab — '%s' should show version %s at Production", args.model_name, version)
    LOGGER.info("  4. Take screenshots for submission!")


if __name__ == "__main__":
    main()
