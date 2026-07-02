"""Tests for the storage/client.py shim over media-sdk-m8.

The ``ObjectStorage`` wrapper itself lives in (and is tested by) media-sdk-m8;
here we only verify media-service's thin responsibility: building the SDK
config from ``settings`` and re-exporting the SDK primitives.
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

import media_sdk_m8

from media_service.core.config import Settings
from media_service.storage.client import (
    ObjectStorage,
    ObjectStorageConfig,
    get_minio_client,
    get_storage_config,
)


def _make_settings(**overrides) -> Settings:
    """Construct Settings from env (seeded by conftest) + overrides, bypassing dotenv."""
    return Settings(_env_file=None, **overrides)


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


# ── MINIO_PUBLIC_ENDPOINT settings-level validation (plan 11.4) ───────────────


def test_minio_public_endpoint_empty_allowed():
    """Empty MINIO_PUBLIC_ENDPOINT is valid — presign uses the internal endpoint."""
    s = _make_settings(MINIO_PUBLIC_ENDPOINT="")
    assert s.MINIO_PUBLIC_ENDPOINT == ""


def test_minio_public_endpoint_https_external_allowed():
    """https:// external endpoint is valid in all modes."""
    s = _make_settings(MINIO_PUBLIC_ENDPOINT="https://storage.example.com")
    assert s.MINIO_PUBLIC_ENDPOINT == "https://storage.example.com"


def test_minio_public_endpoint_bare_hostname_rejected():
    """Bare hostname without scheme is rejected in every environment (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(MINIO_PUBLIC_ENDPOINT="storage.example.com")


def test_minio_public_endpoint_bare_hostname_with_port_rejected():
    """Bare hostname:port without scheme is rejected (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(MINIO_PUBLIC_ENDPOINT="storage.example.com:9000")


def test_minio_public_endpoint_unsupported_scheme_rejected():
    """Non-http/https scheme is rejected in all modes (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(MINIO_PUBLIC_ENDPOINT="ftp://storage.example.com")


def test_minio_public_endpoint_http_non_loopback_production_rejected():
    """http:// for a non-loopback host is rejected when ENVIRONMENT=production (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(
            ENVIRONMENT="production",
            MINIO_PUBLIC_ENDPOINT="http://storage.example.com",
        )


def test_minio_public_endpoint_http_non_loopback_strict_mode_rejected():
    """http:// for a non-loopback host is rejected when STRICT_PRODUCTION_MODE=True (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(
            STRICT_PRODUCTION_MODE=True,
            MINIO_PUBLIC_ENDPOINT="http://storage.example.com",
        )


def test_minio_public_endpoint_http_localhost_production_allowed():
    """http://localhost is allowed in production — loopback is safe for local MinIO (11.4)."""
    s = _make_settings(
        ENVIRONMENT="production",
        MINIO_PUBLIC_ENDPOINT="http://localhost:9000",
    )
    assert s.MINIO_PUBLIC_ENDPOINT == "http://localhost:9000"


def test_minio_public_endpoint_http_loopback_ip_production_allowed():
    """http://127.0.0.1 is allowed in production — loopback IP is safe (11.4)."""
    s = _make_settings(
        ENVIRONMENT="production",
        MINIO_PUBLIC_ENDPOINT="http://127.0.0.1:9000",
    )
    assert s.MINIO_PUBLIC_ENDPOINT == "http://127.0.0.1:9000"


def test_minio_public_endpoint_http_external_local_allowed():
    """http:// for an external host is allowed in local/development mode (11.4)."""
    s = _make_settings(
        ENVIRONMENT="local",
        MINIO_PUBLIC_ENDPOINT="http://storage.example.com",
    )
    assert s.MINIO_PUBLIC_ENDPOINT == "http://storage.example.com"
