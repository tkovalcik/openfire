from __future__ import annotations

from typing import Any

try:
    from common.paths import monitoring_report_uri
    from common.storage import StorageClient, normalize_storage_uri
    from config.settings import Settings, get_settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.common.paths import monitoring_report_uri
    from src.common.storage import StorageClient, normalize_storage_uri
    from src.config.settings import Settings, get_settings


def write_monitoring_outputs(
    *,
    report_name: str,
    run_id: str,
    html_report: str,
    metadata: dict[str, Any],
    settings: Settings | None = None,
    storage: StorageClient | None = None,
) -> dict[str, str]:
    resolved_settings = settings or get_settings()
    resolved_storage = storage or StorageClient.from_settings(resolved_settings)

    if resolved_settings.gcs_bucket:
        html_uri = monitoring_report_uri(
            resolved_settings,
            report_name=report_name,
            run_id=run_id,
            filename="report.html",
        )
        metadata_uri = monitoring_report_uri(
            resolved_settings,
            report_name=report_name,
            run_id=run_id,
            filename="metadata.json",
        )
    else:
        base_uri = normalize_storage_uri(
            resolved_settings.local_cache_dir
            / resolved_settings.monitoring_prefix
            / "reports"
            / report_name
            / run_id
        )
        html_uri = f"{base_uri.rstrip('/')}/report.html"
        metadata_uri = f"{base_uri.rstrip('/')}/metadata.json"

    resolved_storage.write_text(html_uri, html_report, content_type="text/html")
    resolved_storage.write_json(metadata_uri, metadata)
    return {"html_uri": html_uri, "metadata_uri": metadata_uri}
