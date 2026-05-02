from __future__ import annotations

import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

try:
    from config.settings import Settings
except ImportError:  # pragma: no cover - exercised by python -m src...
    from src.config.settings import Settings


class StorageError(RuntimeError):
    """Raised when a storage operation cannot be completed."""


@dataclass(frozen=True)
class StorageURI:
    scheme: str
    bucket: str | None
    path: str
    raw_uri: str

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def local_path(self) -> Path:
        if self.scheme != "local":
            raise StorageError("local_path is only available for local:// URIs")
        return Path(self.path)


def normalize_storage_uri(uri: str | Path) -> str:
    if isinstance(uri, Path):
        return f"local://{uri.as_posix()}"
    uri_str = str(uri).strip()
    if uri_str.startswith(("gs://", "local://")):
        return uri_str
    return f"local://{Path(uri_str).as_posix()}"


def parse_storage_uri(uri: str | Path) -> StorageURI:
    normalized = normalize_storage_uri(uri)
    if normalized.startswith("gs://"):
        bucket_and_path = normalized[len("gs://") :]
        bucket, _, path = bucket_and_path.partition("/")
        if not bucket:
            raise StorageError(f"Invalid GCS URI without bucket: {normalized}")
        return StorageURI(scheme="gs", bucket=bucket, path=path.lstrip("/"), raw_uri=normalized)

    local_path = normalized[len("local://") :]
    return StorageURI(scheme="local", bucket=None, path=local_path, raw_uri=normalized)


class StorageClient:
    def __init__(
        self,
        *,
        local_cache_dir: str | Path = ".cache/openfire",
        gcp_project_id: str | None = None,
        gcs_client: Any | None = None,
    ) -> None:
        self.local_cache_dir = Path(local_cache_dir)
        self.gcp_project_id = gcp_project_id
        self._gcs_client = gcs_client

    @classmethod
    def from_settings(cls, settings: Settings) -> "StorageClient":
        return cls(
            local_cache_dir=settings.local_cache_dir,
            gcp_project_id=settings.gcp_project_id,
        )

    def parse_uri(self, uri: str | Path) -> StorageURI:
        return parse_storage_uri(uri)

    def exists(self, uri: str | Path) -> bool:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            return location.local_path.exists()
        return bool(self._gcs_blob(location).exists())

    def read_bytes(self, uri: str | Path) -> bytes:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            return location.local_path.read_bytes()
        return self._gcs_blob(location).download_as_bytes()

    def write_bytes(
        self,
        uri: str | Path,
        payload: bytes,
        *,
        content_type: str | None = None,
        content_encoding: str | None = None,
    ) -> str:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            location.local_path.parent.mkdir(parents=True, exist_ok=True)
            location.local_path.write_bytes(payload)
            return location.raw_uri

        blob = self._gcs_blob(location)
        if content_encoding:
            blob.content_encoding = content_encoding
        blob.upload_from_string(payload, content_type=content_type)
        return location.raw_uri

    def read_text(self, uri: str | Path, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(uri).decode(encoding)

    def write_text(
        self,
        uri: str | Path,
        payload: str,
        *,
        encoding: str = "utf-8",
        content_type: str = "text/plain",
    ) -> str:
        return self.write_bytes(uri, payload.encode(encoding), content_type=content_type)

    def read_json(self, uri: str | Path) -> dict[str, Any]:
        return json.loads(self.read_text(uri))

    def write_json(self, uri: str | Path, payload: dict[str, Any]) -> str:
        return self.write_text(
            uri,
            json.dumps(payload, indent=2, sort_keys=True),
            content_type="application/json",
        )

    def read_csv(self, uri: str | Path, **kwargs: Any) -> pd.DataFrame:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            return pd.read_csv(location.local_path, **kwargs)
        return pd.read_csv(io.StringIO(self.read_text(uri)), **kwargs)

    def write_csv(self, frame: pd.DataFrame, uri: str | Path, **kwargs: Any) -> str:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            location.local_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_csv(location.local_path, **kwargs)
            return location.raw_uri

        buffer = io.StringIO()
        frame.to_csv(buffer, **kwargs)
        return self.write_text(uri, buffer.getvalue(), content_type="text/csv")

    def read_parquet(self, uri: str | Path, **kwargs: Any) -> pd.DataFrame:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            return pd.read_parquet(location.local_path, **kwargs)
        return pd.read_parquet(io.BytesIO(self.read_bytes(uri)), **kwargs)

    def write_parquet(self, frame: pd.DataFrame, uri: str | Path, **kwargs: Any) -> str:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            location.local_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(location.local_path, **kwargs)
            return location.raw_uri

        buffer = io.BytesIO()
        frame.to_parquet(buffer, **kwargs)
        return self.write_bytes(uri, buffer.getvalue(), content_type="application/octet-stream")

    def upload_file(
        self,
        local_path: str | Path,
        destination_uri: str | Path,
        *,
        content_type: str | None = None,
    ) -> str:
        local_file = Path(local_path)
        if not local_file.exists():
            raise FileNotFoundError(f"Local file not found for upload: {local_file}")
        return self.write_bytes(destination_uri, local_file.read_bytes(), content_type=content_type)

    @contextmanager
    def localize(self, uri: str | Path) -> Iterator[Path]:
        location = self.parse_uri(uri)
        if location.scheme == "local":
            yield location.local_path
            return

        cache_path = self._cache_path(location)
        if not cache_path.exists():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(self.read_bytes(location.raw_uri))
        yield cache_path

    def _cache_path(self, location: StorageURI) -> Path:
        digest = hashlib.sha256(location.raw_uri.encode("utf-8")).hexdigest()[:16]
        safe_name = location.filename or "artifact.bin"
        return self.local_cache_dir / "downloads" / f"{digest}-{safe_name}"

    def _gcs_blob(self, location: StorageURI) -> Any:
        client = self._get_gcs_client()
        bucket = client.bucket(location.bucket)
        return bucket.blob(location.path)

    def _get_gcs_client(self) -> Any:
        if self._gcs_client is not None:
            return self._gcs_client
        try:
            from google.cloud import storage
        except ImportError as error:  # pragma: no cover - depends on local environment
            raise StorageError(
                "google-cloud-storage is required for gs:// storage URIs."
            ) from error
        self._gcs_client = storage.Client(project=self.gcp_project_id)
        return self._gcs_client
