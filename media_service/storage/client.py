"""Object-storage client configuration boundary — thin shim over media-sdk-m8.

The reusable ``ObjectStorage`` wrapper and its config/client factory now live in
``media_sdk_m8`` (shared by media-service and media-worker). This module keeps a
single media-service-specific responsibility: building an
:class:`ObjectStorageConfig` from the service ``settings`` so call sites and
tests can keep importing ``ObjectStorage`` from here.
"""

from urllib.parse import urlparse

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
    public_endpoint: str | None = None
    public_secure: bool | None = None
    if settings.S3_PUBLIC_ENDPOINT:
        parsed = urlparse(settings.S3_PUBLIC_ENDPOINT)
        public_endpoint = parsed.netloc
        public_secure = parsed.scheme == "https"

    return ObjectStorageConfig(
        endpoint=settings.S3_ENDPOINT,
        access_key=settings.S3_ACCESS_KEY,
        secret_key=settings.S3_SECRET_KEY,
        secure=settings.S3_USE_SSL,
        region=settings.S3_REGION,
        presigned_expire_seconds=settings.S3_PRESIGNED_URL_EXPIRE_SECONDS,
        public_endpoint=public_endpoint,
        public_secure=public_secure,
    )
