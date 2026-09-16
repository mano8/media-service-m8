"""Tests for the storage/client.py shim over media-sdk-m8.

The ``ObjectStorage`` wrapper itself lives in (and is tested by) media-sdk-m8;
here we only verify media-service's thin responsibility: building the SDK
config from ``settings`` and re-exporting the SDK primitives.
"""

import warnings
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
    assert config.endpoint == settings.S3_ENDPOINT
    assert config.access_key == settings.S3_ACCESS_KEY
    assert config.secret_key == settings.S3_SECRET_KEY
    assert config.secure is settings.S3_USE_SSL
    assert config.region == settings.S3_REGION
    assert config.presigned_expire_seconds == settings.S3_PRESIGNED_URL_EXPIRE_SECONDS


def test_get_storage_config_no_public_endpoint_leaves_none():
    """When S3_PUBLIC_ENDPOINT is unset, presign fields stay None (no behaviour change)."""
    from media_service.core.config import settings

    with patch.object(settings, "S3_PUBLIC_ENDPOINT", ""):
        config = get_storage_config()
    assert config.public_endpoint is None
    assert config.public_secure is None


def test_get_storage_config_http_public_endpoint():
    """http:// URL → public_endpoint netloc + public_secure=False."""
    from media_service.core.config import settings

    with patch.object(settings, "S3_PUBLIC_ENDPOINT", "http://127.0.0.1:9005"):
        config = get_storage_config()
    assert config.public_endpoint == "127.0.0.1:9005"
    assert config.public_secure is False


def test_get_storage_config_https_public_endpoint():
    """https:// URL → public_endpoint netloc + public_secure=True."""
    from media_service.core.config import settings

    with patch.object(settings, "S3_PUBLIC_ENDPOINT", "https://storage.example.com"):
        config = get_storage_config()
    assert config.public_endpoint == "storage.example.com"
    assert config.public_secure is True


# ── S3_PUBLIC_ENDPOINT settings-level validation (plan 11.4) ───────────────


def test_s3_public_endpoint_empty_allowed():
    """Empty S3_PUBLIC_ENDPOINT is valid — presign uses the internal endpoint."""
    s = _make_settings(S3_PUBLIC_ENDPOINT="")
    assert s.S3_PUBLIC_ENDPOINT == ""


def test_s3_public_endpoint_https_external_allowed():
    """https:// external endpoint is valid in all modes."""
    s = _make_settings(S3_PUBLIC_ENDPOINT="https://storage.example.com")
    assert s.S3_PUBLIC_ENDPOINT == "https://storage.example.com"


def test_s3_public_endpoint_bare_hostname_rejected():
    """Bare hostname without scheme is rejected in every environment (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(S3_PUBLIC_ENDPOINT="storage.example.com")


def test_s3_public_endpoint_bare_hostname_with_port_rejected():
    """Bare hostname:port without scheme is rejected (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(S3_PUBLIC_ENDPOINT="storage.example.com:9000")


def test_s3_public_endpoint_unsupported_scheme_rejected():
    """Non-http/https scheme is rejected in all modes (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(S3_PUBLIC_ENDPOINT="ftp://storage.example.com")


def test_s3_public_endpoint_http_non_loopback_production_rejected():
    """http:// for a non-loopback host is rejected when ENVIRONMENT=production (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(
            ENVIRONMENT="production",
            S3_PUBLIC_ENDPOINT="http://storage.example.com",
        )


def test_s3_public_endpoint_http_non_loopback_strict_mode_rejected():
    """http:// for a non-loopback host is rejected when STRICT_PRODUCTION_MODE=True (11.4)."""
    with pytest.raises((ValueError, ValidationError), match="11.4"):
        _make_settings(
            STRICT_PRODUCTION_MODE=True,
            S3_PUBLIC_ENDPOINT="http://storage.example.com",
        )


def test_s3_public_endpoint_http_localhost_production_allowed():
    """http://localhost is allowed in production — loopback is safe for a local storage backend (11.4)."""
    s = _make_settings(
        ENVIRONMENT="production",
        S3_PUBLIC_ENDPOINT="http://localhost:9000",
    )
    assert s.S3_PUBLIC_ENDPOINT == "http://localhost:9000"


def test_s3_public_endpoint_http_loopback_ip_production_allowed():
    """http://127.0.0.1 is allowed in production — loopback IP is safe (11.4)."""
    s = _make_settings(
        ENVIRONMENT="production",
        S3_PUBLIC_ENDPOINT="http://127.0.0.1:9000",
    )
    assert s.S3_PUBLIC_ENDPOINT == "http://127.0.0.1:9000"


def test_s3_public_endpoint_http_external_local_allowed():
    """http:// for an external host is allowed in local/development mode (11.4)."""
    s = _make_settings(
        ENVIRONMENT="local",
        S3_PUBLIC_ENDPOINT="http://storage.example.com",
    )
    assert s.S3_PUBLIC_ENDPOINT == "http://storage.example.com"


# ── S3_ENDPOINT validation ───────────────────────────────────────────────────


def test_s3_endpoint_default_is_the_storage_service_not_the_retired_backend():
    """T30: the field default names the fleet's ``storage`` service on its S3
    port — the backend the compose stacks actually run — not the retired
    backend's former ``minio:9000`` pair. Every stack sets ``S3_ENDPOINT``
    explicitly, so this default is only ever seen by a bare ``Settings()``; it
    still must not point at a host nothing in the fleet runs."""
    assert _make_settings().S3_ENDPOINT == "storage:8333"


def test_s3_endpoint_with_scheme_rejected():
    """A URL belongs in S3_PUBLIC_ENDPOINT; the internal endpoint is scheme-less."""
    with pytest.raises((ValueError, ValidationError), match="scheme-less"):
        _make_settings(S3_ENDPOINT="https://storage.example.com:9000")


def test_s3_endpoint_empty_rejected():
    """An empty internal endpoint fails at config load, not at the first request."""
    with pytest.raises((ValueError, ValidationError), match="must not be empty"):
        _make_settings(S3_ENDPOINT="  ")


@pytest.mark.parametrize("endpoint", ["storage:0", "storage:65536", "storage:nine"])
def test_s3_endpoint_invalid_port_rejected(endpoint):
    """The port range is enforced on the netloc's port half, as an int field would."""
    with pytest.raises((ValueError, ValidationError), match="port must be a number"):
        _make_settings(S3_ENDPOINT=endpoint)


def test_s3_endpoint_without_host_rejected():
    """':9000' names no host — rejected rather than silently resolved."""
    with pytest.raises((ValueError, ValidationError), match="must include a host"):
        _make_settings(S3_ENDPOINT=":9000")


@pytest.mark.parametrize("endpoint", ["storage", "storage:9000", "[::1]", "[::1]:9000"])
def test_s3_endpoint_accepted_forms(endpoint):
    """Bare host, host:port and bracketed IPv6 (with or without a port) all load."""
    assert _make_settings(S3_ENDPOINT=endpoint).S3_ENDPOINT == endpoint


# ── Retired vocabulary is refused (3.0.0) ────────────────────────────────────


# The retired vocabulary, sampled: the host/port pair that used to collapse
# into ``S3_ENDPOINT``, the secret, and one bucket name.
_RETIRED_STORAGE_KEYS = (  # retired in 3.0.0 — refused, not translated
    "MINIO_HOST",  # retired
    "MINIO_PORT",  # retired
    "MINIO_SECRET_KEY",  # retired
    "MINIO_BUCKET_PUBLIC",  # retired
)


@pytest.mark.parametrize("retired_key", _RETIRED_STORAGE_KEYS)
def test_retired_storage_keys_are_refused(retired_key):
    """3.0.0 removed the ``MINIO_*`` → ``S3_*`` deprecation shim. ``Settings``
    is ``extra="forbid"``, so a stray old name in a ``media.env`` fails at boot
    with pydantic's own ``extra_forbidden`` error naming the key — the intended
    major-version behaviour, with no softer path."""
    with pytest.raises(ValidationError) as excinfo:
        _make_settings(**{retired_key: "legacy-value"})
    errors = excinfo.value.errors()
    assert [e["type"] for e in errors] == ["extra_forbidden"]
    assert errors[0]["loc"] == (retired_key,)


def test_retired_storage_keys_are_not_fields():
    """No retired ``MINIO_*`` name is declared as a field or listed as a
    secret, so nothing could silently accept one."""
    retired_prefix = _RETIRED_STORAGE_KEYS[0].split("_")[0] + "_"
    assert not [f for f in Settings.model_fields if f.startswith(retired_prefix)]
    assert not [f for f in Settings.secret_fields if f.startswith(retired_prefix)]


def test_s3_secret_key_is_a_secret_field():
    """The renamed secret keeps the strength/placeholder guards (S8)."""
    assert "S3_SECRET_KEY" in Settings.secret_fields
    with pytest.raises((ValueError, ValidationError), match="Insecure default"):
        _make_settings(S3_SECRET_KEY="changethis")


def test_secret_file_mount_works_under_the_new_name(tmp_path, monkeypatch):
    """``S3_SECRET_KEY_FILE`` is the hardened stacks' Docker-secret path (S4/S8)."""
    secret = tmp_path / "s3_secret_key"
    secret.write_text("FileMounted!Secret2", encoding="utf-8")
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    monkeypatch.setenv("S3_SECRET_KEY_FILE", str(secret))
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        s = _make_settings()
    assert s.S3_SECRET_KEY == "FileMounted!Secret2"
