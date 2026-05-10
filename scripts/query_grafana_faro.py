#!/usr/bin/env python3
"""Query Grafana Cloud Faro measurements from Loki.

The script reads Grafana Cloud Loki credentials from environment variables or
from the local ignored `.env` file:

- GRAFANA_CLOUD_LOKI_URL
- GRAFANA_CLOUD_LOKI_USER
- GRAFANA_CLOUD_LOKI_TOKEN

It is intentionally dependency-free so it can run in a plain Python
environment.
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_SERVICE_NAME = "openfire-ui-socal"
DEFAULT_SERVICE_NAMESPACE = "openfire"
DEFAULT_ENVIRONMENT = "production"
DEFAULT_MEASUREMENT_TYPE = "openfire_ui_interaction"
NUMERIC_FIELDS = (
    "snapshot_load_ms",
    "render_sync_ms",
    "select_ms",
    "layer_swap_ms",
    "paint_ready_ms",
    "frame_wait_ms",
    "rendered_feature_count",
    "total_feature_count",
    "zoom",
    "long_task_count",
    "long_task_total_ms",
)


@dataclass(frozen=True)
class FaroRecord:
    timestamp_ns: str
    labels: dict[str, str]
    fields: dict[str, str]


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def loki_request(path: str, params: dict[str, str]) -> dict:
    base_url = required_env("GRAFANA_CLOUD_LOKI_URL").rstrip("/")
    user = required_env("GRAFANA_CLOUD_LOKI_USER")
    token = required_env("GRAFANA_CLOUD_LOKI_TOKEN")
    credentials = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
    url = f"{base_url}{path}?{urlencode(params)}"
    request = Request(url, headers={"Authorization": f"Basic {credentials}"})
    with urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def quote_label(value: str) -> str:
    return json.dumps(value)


def build_selector(args: argparse.Namespace) -> str:
    matchers = [
        f'service_name={quote_label(args.service_name)}',
        f'service_namespace={quote_label(args.service_namespace)}',
        f'deployment_environment={quote_label(args.environment)}',
        f'kind={quote_label(args.kind)}',
    ]
    return "{" + ",".join(matchers) + "}"


def build_query(args: argparse.Namespace) -> str:
    query = build_selector(args)
    if args.measurement_type:
        query = f'{query} |= "type={args.measurement_type}"'
    return query


def parse_logfmt_line(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in shlex.split(line):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def query_records(args: argparse.Namespace) -> list[FaroRecord]:
    payload = loki_request(
        "/loki/api/v1/query_range",
        {
            "query": build_query(args),
            "since": args.since,
            "limit": str(args.limit),
            "direction": "backward",
        },
    )
    if payload.get("status") != "success":
        raise SystemExit(f"Loki query failed: {payload}")

    records: list[FaroRecord] = []
    for stream in payload.get("data", {}).get("result", []):
        labels = {str(key): str(value) for key, value in stream.get("stream", {}).items()}
        for timestamp_ns, line in stream.get("values", []):
            records.append(
                FaroRecord(
                    timestamp_ns=str(timestamp_ns),
                    labels=labels,
                    fields=parse_logfmt_line(line),
                )
            )
    return records


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = round((len(sorted_values) - 1) * quantile)
    return sorted_values[index]


def metric_value(fields: dict[str, str], name: str) -> float | None:
    return to_float(fields.get(f"value_{name}") or fields.get(name))


def group_key(record: FaroRecord) -> tuple[str, str, str]:
    fields = record.fields
    return (
        fields.get("context_ui_variant") or record.labels.get("service_version", "unknown"),
        fields.get("context_action", "unknown"),
        fields.get("context_source_label", "unknown"),
    )


def summarize_records(records: Iterable[FaroRecord]) -> list[dict[str, object]]:
    groups: dict[tuple[str, str, str], list[FaroRecord]] = defaultdict(list)
    for record in records:
        groups[group_key(record)].append(record)

    summaries: list[dict[str, object]] = []
    for (ui_variant, action, source_label), group_records in groups.items():
        summary: dict[str, object] = {
            "ui_variant": ui_variant,
            "action": action,
            "source_label": source_label,
            "samples": len(group_records),
        }
        for field in NUMERIC_FIELDS:
            values = [
                value
                for record in group_records
                if (value := metric_value(record.fields, field)) is not None
            ]
            if not values:
                continue
            summary[f"p50_{field}"] = percentile(values, 0.50)
            summary[f"p95_{field}"] = percentile(values, 0.95)
            summary[f"avg_{field}"] = sum(values) / len(values)
        summaries.append(summary)

    return sorted(
        summaries,
        key=lambda row: (
            float(row.get("p95_paint_ready_ms") or 0),
            int(row.get("samples") or 0),
        ),
        reverse=True,
    )


def format_ms(value: object) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):,.1f}"
    return "n/a"


def print_table(rows: list[dict[str, object]]) -> None:
    headers = (
        "ui_variant",
        "action",
        "source_label",
        "samples",
        "p95_paint_ms",
        "p95_load_ms",
        "p95_render_ms",
        "p95_frame_ms",
        "avg_points",
    )
    table = [headers]
    for row in rows:
        table.append(
            (
                str(row.get("ui_variant", "unknown")),
                str(row.get("action", "unknown")),
                str(row.get("source_label", "unknown")),
                str(row.get("samples", 0)),
                format_ms(row.get("p95_paint_ready_ms")),
                format_ms(row.get("p95_snapshot_load_ms")),
                format_ms(row.get("p95_render_sync_ms")),
                format_ms(row.get("p95_frame_wait_ms")),
                format_ms(row.get("avg_rendered_feature_count")),
            )
        )
    widths = [max(len(row[index]) for row in table) for index in range(len(headers))]
    for row_index, row in enumerate(table):
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
        if row_index == 0:
            print("  ".join("-" * width for width in widths))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="6h", help="Loki lookback window, e.g. 30m, 6h, 2d.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum Loki log entries to fetch.")
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    parser.add_argument("--service-namespace", default=DEFAULT_SERVICE_NAMESPACE)
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    parser.add_argument("--kind", default="measurement")
    parser.add_argument(
        "--measurement-type",
        default=DEFAULT_MEASUREMENT_TYPE,
        help="Faro measurement type to filter on. Use an empty string to include all measurements.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw summary JSON.")
    parser.add_argument("--raw", action="store_true", help="Print parsed raw records instead of summaries.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv or sys.argv[1:])
    records = query_records(args)
    if args.raw:
        print(
            json.dumps(
                [
                    {
                        "timestamp_ns": record.timestamp_ns,
                        "labels": record.labels,
                        "fields": record.fields,
                    }
                    for record in records
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    summaries = summarize_records(records)
    if args.json:
        print(json.dumps(summaries, indent=2, sort_keys=True))
    else:
        print_table(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
