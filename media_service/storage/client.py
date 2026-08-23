"""MinIO client configuration boundary — thin shim over media-sdk-m8.

The reusable ``ObjectStorage`` wrapper and its config/client factory now live in
``media_sdk_m8`` (shared by media-service and media-worker). This module keeps a
single media-service-specific responsibility: building an
:class:`ObjectStorageConfig` from the service ``settings`` so call sites and
tests can keep importing ``ObjectStorage`` from here.
"""

from typing import IO, Any
from urllib.parse import urlparse

from media_sdk_m8 import ObjectStorage, ObjectStorageConfig, get_minio_client

from media_service.core.config import settings

__all__ = [
    "ObjectStorage",
    "ObjectStorageConfig",
    "get_minio_client",
    "get_storage_config",
    "put_object_stream",
]


def get_storage_config() -> ObjectStorageConfig:
    """Build the shared SDK storage config from media-service settings."""
    public_endpoint: str | None = None
    public_secure: bool | None = None
    if settings.MINIO_PUBLIC_ENDPOINT:
        parsed = urlparse(settings.MINIO_PUBLIC_ENDPOINT)
        public_endpoint = parsed.netloc
        public_secure = parsed.scheme == "https"

    return ObjectStorageConfig(
        endpoint=f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
        region=settings.MINIO_REGION,
        presigned_expire_seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS,
        public_endpoint=public_endpoint,
        public_secure=public_secure,
    )


def put_object_stream(
    storage: ObjectStorage,
    *,
    bucket: str,
    object_key: str,
    data: IO[bytes],
    length: int,
    content_type: str,
) -> Any:
    """Write a file-like object to storage without buffering it in memory.

    The shared SDK wrapper's ``put_object`` takes ``bytes``, which is right for
    a generated image variant but wrong for an assembled archive export
    (`U9`): the whole zip would have to be resident in RAM to write it. The
    equivalent streaming put belongs in ``media-sdk-m8`` next to the rest of
    ``ObjectStorage``, but adding it there means publishing a new SDK version
    and raising both services' floors — a cross-repository step this one does
    not own — so the single call that needs it lives at this boundary module,
    which already exists to keep media-service-specific storage concerns in one
    place. Nothing else in the service reaches past ``ObjectStorage``.
    """
    return storage.client.put_object(
        bucket,
        object_key,
        data,
        length=length,
        content_type=content_type,
    )
