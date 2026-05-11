"""Utilities for preparing the ZIP centroid lookup table used by alerting."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from src.ui_socal.alert_worker import (
    DEFAULT_BQ_PROJECT,
    DEFAULT_ZIP_CENTROIDS_BQ_DATASET,
    DEFAULT_ZIP_CENTROIDS_BQ_TABLE,
    ZipCentroid,
    load_zip_centroids_from_json,
    zip_centroid_table_id,
)


def load_zip_centroids_from_csv(path: str | Path) -> dict[str, ZipCentroid]:
    """Load ZIP centroids from a CSV with zip, latitude, and longitude columns."""
    out: dict[str, ZipCentroid] = {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = {"zip", "latitude", "longitude"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")
        for row in reader:
            zip_code = str(row["zip"]).strip()
            if not zip_code:
                continue
            out[zip_code] = ZipCentroid(
                zip=zip_code,
                latitude=_coerce_float(row["latitude"], field_name="latitude"),
                longitude=_coerce_float(row["longitude"], field_name="longitude"),
            )
    return out


def zip_centroid_rows(centroids: Iterable[ZipCentroid]) -> list[dict[str, object]]:
    loaded_at = datetime.now(timezone.utc).isoformat()
    return [
        {**asdict(centroid), "loaded_at": loaded_at}
        for centroid in sorted(centroids, key=lambda item: item.zip)
    ]


def zip_centroid_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("zip", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("latitude", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("longitude", "FLOAT64", mode="REQUIRED"),
        bigquery.SchemaField("loaded_at", "TIMESTAMP", mode="REQUIRED"),
    ]


def ensure_zip_centroids_table(client: bigquery.Client, table_id: str) -> str:
    schema = zip_centroid_schema()
    try:
        table = client.get_table(table_id)
    except NotFound:
        table = bigquery.Table(table_id, schema=schema)
        table.clustering_fields = ["zip"]
        client.create_table(table, exists_ok=True)
    else:
        existing_fields = {field.name for field in table.schema}
        missing_fields = [field for field in schema if field.name not in existing_fields]
        if missing_fields:
            table.schema = list(table.schema) + missing_fields
            client.update_table(table, ["schema"])
    return table_id


def replace_zip_centroids_in_bigquery(
    centroids: Iterable[ZipCentroid],
    *,
    table_id: str,
    client: bigquery.Client | None = None,
) -> int:
    """Replace the ZIP centroid lookup table contents with the provided rows."""
    if client is None:
        project = table_id.split(".", 1)[0] if "." in table_id else None
        client = bigquery.Client(project=project)

    ensure_zip_centroids_table(client, table_id)
    rows = zip_centroid_rows(centroids)
    job_config = bigquery.LoadJobConfig(
        schema=zip_centroid_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    client.load_table_from_json(rows, table_id, job_config=job_config).result()
    return len(rows)


def _coerce_float(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric; got {value!r}") from exc


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load ZIP centroid lookup rows into BigQuery.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-csv")
    source.add_argument("--source-json")
    parser.add_argument("--project", default=DEFAULT_BQ_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_ZIP_CENTROIDS_BQ_DATASET)
    parser.add_argument("--table-name", default=DEFAULT_ZIP_CENTROIDS_BQ_TABLE)
    parser.add_argument("--table")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually replace the BigQuery table. Without this, only validates and summarizes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    centroids = (
        load_zip_centroids_from_csv(args.source_csv)
        if args.source_csv
        else load_zip_centroids_from_json(args.source_json)
    )
    table_id = args.table or zip_centroid_table_id(
        project=args.project,
        dataset=args.dataset,
        table=args.table_name,
    )
    written = 0
    if args.write:
        written = replace_zip_centroids_in_bigquery(centroids.values(), table_id=table_id)

    print(
        json.dumps(
            {
                "dry_run": not args.write,
                "table_id": table_id,
                "input_rows": len(centroids),
                "written_rows": written,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
