"""MinIO client configuration boundary — thin shim over media-sdk-m8.

The reusable ``ObjectStorage`` wrapper and its config/client factory now live in
``media_sdk_m8`` (shared by media-service and media-worker). This module keeps a
single media-service-specific responsibility: building an
:class:`ObjectStorageConfig` from the service ``settings`` so call sites and
tests can keep importing ``ObjectStorage`` from here.
"""

from media_sdk_m8 import ObjectStorage, ObjectStorageConfig, get_minio_client

from media_service.core.config import settings

__all__ = [
    "ObjectStorage",
    "ObjectStorageConfig",
    "get_minio_client",
    "get_storage_config",
]


def get_storage_config() -> ObjectStorageConfig:
    """Build the shared SDK storage config from media-service settings."""
    return ObjectStorageConfig(
        endpoint=f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
        region=settings.MINIO_REGION,
        presigned_expire_seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS,
    )
