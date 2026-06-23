"""Tests for the storage/client.py shim over media-sdk-m8.

The ``ObjectStorage`` wrapper itself lives in (and is tested by) media-sdk-m8;
here we only verify media-service's thin responsibility: building the SDK
config from ``settings`` and re-exporting the SDK primitives.
"""

from unittest.mock import patch

import media_sdk_m8

from media_service.storage.client import (
    ObjectStorage,
    ObjectStorageConfig,
    get_minio_client,
    get_storage_config,
)


def test_reexports_are_the_sdk_primitives():
    assert ObjectStorage is media_sdk_m8.ObjectStorage
    assert ObjectStorageConfig is media_sdk_m8.ObjectStorageConfig
    assert get_minio_client is media_sdk_m8.get_minio_client


def test_get_storage_config_maps_settings_fields():
    from media_service.core.config import settings

    config = get_storage_config()
    assert isinstance(config, ObjectStorageConfig)
    assert config.endpoint == f"{settings.MINIO_HOST}:{settings.MINIO_PORT}"
    assert config.access_key == settings.MINIO_ACCESS_KEY
    assert config.secret_key == settings.MINIO_SECRET_KEY
    assert config.secure is settings.MINIO_USE_SSL
    assert config.region == settings.MINIO_REGION
    assert (
        config.presigned_expire_seconds == settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
    )


def test_get_storage_config_no_public_endpoint_leaves_none():
    """When MINIO_PUBLIC_ENDPOINT is unset, presign fields stay None (no behaviour change)."""
    from media_service.core.config import settings

    with patch.object(settings, "MINIO_PUBLIC_ENDPOINT", ""):
        config = get_storage_config()
    assert config.public_endpoint is None
    assert config.public_secure is None


def test_get_storage_config_http_public_endpoint():
    """http:// URL → public_endpoint netloc + public_secure=False."""
    from media_service.core.config import settings

    with patch.object(settings, "MINIO_PUBLIC_ENDPOINT", "http://127.0.0.1:9005"):
        config = get_storage_config()
    assert config.public_endpoint == "127.0.0.1:9005"
    assert config.public_secure is False


def test_get_storage_config_https_public_endpoint():
    """https:// URL → public_endpoint netloc + public_secure=True."""
    from media_service.core.config import settings

    with patch.object(settings, "MINIO_PUBLIC_ENDPOINT", "https://storage.example.com"):
        config = get_storage_config()
    assert config.public_endpoint == "storage.example.com"
    assert config.public_secure is True
