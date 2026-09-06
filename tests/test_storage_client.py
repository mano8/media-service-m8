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


# ── S3_ENDPOINT validation (replaces MINIO_PORT's range check) ────────────────


def test_s3_endpoint_default_matches_the_legacy_host_port_pair():
    """The default netloc is exactly what MINIO_HOST/MINIO_PORT defaulted to."""
    assert _make_settings().S3_ENDPOINT == "minio:9000"


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
    """The port range MINIO_PORT enforced as an int field is still enforced."""
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


# ── MINIO_* → S3_* deprecation shim (removed in 3.0.0) ───────────────────────


def test_legacy_host_and_port_become_s3_endpoint():
    """An unmigrated deployment's MINIO_HOST/MINIO_PORT still resolve, and warn."""
    with pytest.warns(DeprecationWarning, match="removed in media-service-m8 3.0.0"):
        s = _make_settings(MINIO_HOST="legacy-storage", MINIO_PORT=9001)
    assert s.S3_ENDPOINT == "legacy-storage:9001"


def test_legacy_host_alone_keeps_the_default_port():
    """Half a legacy pair resolves exactly as the two separate fields did."""
    with pytest.warns(DeprecationWarning):
        s = _make_settings(MINIO_HOST="legacy-storage")
    assert s.S3_ENDPOINT == "legacy-storage:9000"


def test_legacy_port_alone_keeps_the_default_host():
    with pytest.warns(DeprecationWarning):
        s = _make_settings(MINIO_PORT=9002)
    assert s.S3_ENDPOINT == "minio:9002"


def test_legacy_scalar_aliases_are_applied():
    """Every renamed scalar still loads from its old name."""
    with pytest.warns(DeprecationWarning, match="MINIO_REGION -> S3_REGION"):
        s = _make_settings(
            MINIO_REGION="eu-central-1",
            MINIO_USE_SSL=True,
            MINIO_BUCKET_PUBLIC="legacy-public",
            MINIO_BUCKET_PRIVATE="legacy-private",
            MINIO_BUCKET_SENSITIVE="legacy-sensitive",
            MINIO_BUCKET_TEMP="legacy-temp",
            MINIO_BUCKET_ARCHIVE="legacy-archive",
            MINIO_PRESIGNED_URL_EXPIRE_SECONDS=600,
        )
    assert s.S3_REGION == "eu-central-1"
    assert s.S3_USE_SSL is True
    assert s.S3_BUCKET_PUBLIC == "legacy-public"
    assert s.S3_BUCKET_PRIVATE == "legacy-private"
    assert s.S3_BUCKET_SENSITIVE == "legacy-sensitive"
    assert s.S3_BUCKET_TEMP == "legacy-temp"
    assert s.S3_BUCKET_ARCHIVE == "legacy-archive"
    assert s.S3_PRESIGNED_URL_EXPIRE_SECONDS == 600


def test_new_names_win_over_legacy_ones():
    """A half-migrated .env resolves to the new vocabulary, never the old."""
    with pytest.warns(DeprecationWarning):
        s = _make_settings(
            S3_ENDPOINT="new-storage:9000",
            MINIO_HOST="legacy-storage",
            MINIO_PORT=9001,
            S3_REGION="eu-west-3",
            MINIO_REGION="eu-central-1",
            S3_SECRET_KEY="NewStorage!Secret1",
            MINIO_SECRET_KEY="LegacyStorage!Secret1",
        )
    assert s.S3_ENDPOINT == "new-storage:9000"
    assert s.S3_REGION == "eu-west-3"
    assert s.S3_SECRET_KEY == "NewStorage!Secret1"


def test_legacy_fields_are_cleared_on_the_instance():
    """The shim translates and drops: no value — least of all the secret — survives
    on a name the S3 vocabulary no longer reads."""
    with pytest.warns(DeprecationWarning):
        s = _make_settings(
            MINIO_HOST="legacy-storage",
            MINIO_PORT=9001,
            MINIO_REGION="eu-central-1",
            MINIO_SECRET_KEY="LegacyStorage!Secret1",
        )
    assert s.MINIO_HOST is None
    assert s.MINIO_PORT is None
    assert s.MINIO_REGION is None
    assert s.MINIO_SECRET_KEY is None
    # The values landed on the new names instead (the secret keeps the value the
    # test environment already supplies as S3_SECRET_KEY — new names win).
    assert s.S3_ENDPOINT == "legacy-storage:9001"
    assert s.S3_REGION == "eu-central-1"
    assert s.S3_SECRET_KEY


def test_no_deprecation_warning_for_a_fully_migrated_config():
    """The shim is silent once the old names are gone."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert _make_settings(S3_ENDPOINT="storage:9000").S3_ENDPOINT == "storage:9000"


def test_legacy_public_endpoint_still_hits_the_renamed_validator():
    """S7 is intact through the shim: the old name is validated by the same rules."""
    with pytest.warns(DeprecationWarning):
        with pytest.raises((ValueError, ValidationError), match="11.4"):
            _make_settings(MINIO_PUBLIC_ENDPOINT="storage.example.com")


def test_s3_secret_key_is_a_secret_field():
    """The renamed secret keeps the strength/placeholder guards (S8)."""
    assert "S3_SECRET_KEY" in Settings.secret_fields
    with pytest.raises((ValueError, ValidationError), match="Insecure default"):
        _make_settings(S3_SECRET_KEY="changethis")


def test_shim_passes_non_mapping_input_through_untouched():
    """The before-validator only translates mappings; anything else reaches
    pydantic's own type error unchanged."""
    with pytest.raises(ValidationError, match="valid dictionary"):
        Settings.model_validate("not-a-mapping")


def test_legacy_secret_file_mount_reaches_the_renamed_field(tmp_path, monkeypatch):
    """The hardened production overlay mounts the storage secret as
    ``MINIO_SECRET_KEY_FILE`` (S4/S8). The ``*_FILE`` settings source fills the
    legacy field and the shim carries it to ``S3_SECRET_KEY``, so the deployed
    credential path survives the rename without touching the compose files."""
    secret = tmp_path / "minio_secret_key"
    secret.write_text("FileMounted!Secret1", encoding="utf-8")
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    monkeypatch.setenv("MINIO_SECRET_KEY_FILE", str(secret))
    with pytest.warns(DeprecationWarning):
        s = _make_settings()
    assert s.S3_SECRET_KEY == "FileMounted!Secret1"
    assert s.MINIO_SECRET_KEY is None


def test_secret_file_mount_works_under_the_new_name(tmp_path, monkeypatch):
    """``S3_SECRET_KEY_FILE`` is what the compose files move to in T12."""
    secret = tmp_path / "s3_secret_key"
    secret.write_text("FileMounted!Secret2", encoding="utf-8")
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    monkeypatch.setenv("S3_SECRET_KEY_FILE", str(secret))
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        s = _make_settings()
    assert s.S3_SECRET_KEY == "FileMounted!Secret2"
