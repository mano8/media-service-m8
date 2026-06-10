"""MinIO client configuration boundary."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from media_service.core.config import settings


@dataclass(frozen=True)
class ObjectStorageConfig:
    """Connection settings for S3-compatible object storage."""

    endpoint: str
    access_key: str
    secret_key: str
    secure: bool
    region: str


def get_storage_config() -> ObjectStorageConfig:
    """Return the configured MinIO endpoint and credentials."""
    return ObjectStorageConfig(
        endpoint=f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
        region=settings.MINIO_REGION,
    )


def get_minio_client() -> Any:
    """Create a MinIO SDK client from settings."""
    from minio import Minio

    config = get_storage_config()
    return Minio(
        endpoint=config.endpoint,
        access_key=config.access_key,
        secret_key=config.secret_key,
        secure=config.secure,
        region=config.region,
    )


class ObjectStorage:
    """Small wrapper around MinIO operations used by the media service."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or get_minio_client()

    def stat_object(self, *, bucket: str, object_key: str) -> Any:
        """Return object metadata from storage."""
        return self.client.stat_object(bucket, object_key)

    def remove_object(self, *, bucket: str, object_key: str) -> None:
        """Remove an object from storage."""
        self.client.remove_object(bucket, object_key)

    def get_object_head(
        self, *, bucket: str, object_key: str, length: int = 512
    ) -> bytes:
        """Read the first *length* bytes of an object for content-type sniffing."""
        response = self.client.get_object(bucket, object_key, offset=0, length=length)
        try:
            return response.read(length)
        finally:
            response.close()
            response.release_conn()

    def get_object(self, *, bucket: str, object_key: str) -> bytes:
        """Download an entire object and return its raw bytes."""
        response = self.client.get_object(bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def presigned_put_object(
        self,
        *,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
    ) -> str:
        """Generate a presigned PUT URL."""
        expires = timedelta(
            seconds=expires_seconds or settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        )
        return self.client.presigned_put_object(bucket, object_key, expires=expires)

    def presigned_get_object(
        self,
        *,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        """Generate a presigned GET URL."""
        expires = timedelta(
            seconds=expires_seconds or settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        )
        return self.client.presigned_get_object(
            bucket,
            object_key,
            expires=expires,
            response_headers=response_headers,
        )
