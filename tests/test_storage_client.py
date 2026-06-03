"""Tests for storage/client.py ObjectStorage wrapper."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from media_service.storage.client import ObjectStorage, get_storage_config


def _client(mock_minio: MagicMock) -> ObjectStorage:
    return ObjectStorage(client=mock_minio)


def test_get_storage_config_returns_correct_fields():
    from media_service.core.config import settings

    config = get_storage_config()
    assert config.access_key == settings.MINIO_ACCESS_KEY
    assert config.secret_key == settings.MINIO_SECRET_KEY
    assert config.secure is False


def test_stat_object_delegates():
    minio = MagicMock()
    storage = _client(minio)
    storage.stat_object(bucket="b", object_key="k")
    minio.stat_object.assert_called_once_with("b", "k")


def test_remove_object_delegates():
    minio = MagicMock()
    storage = _client(minio)
    storage.remove_object(bucket="b", object_key="k")
    minio.remove_object.assert_called_once_with("b", "k")


def test_presigned_put_object_uses_settings_expiry():
    from media_service.core.config import settings

    minio = MagicMock()
    storage = _client(minio)
    storage.presigned_put_object(bucket="b", object_key="k")
    minio.presigned_put_object.assert_called_once_with(
        "b", "k", expires=timedelta(seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS)
    )


def test_presigned_put_object_uses_custom_expiry():
    minio = MagicMock()
    storage = _client(minio)
    storage.presigned_put_object(bucket="b", object_key="k", expires_seconds=120)
    minio.presigned_put_object.assert_called_once_with(
        "b", "k", expires=timedelta(seconds=120)
    )


def test_presigned_get_object_uses_settings_expiry():
    from media_service.core.config import settings

    minio = MagicMock()
    storage = _client(minio)
    storage.presigned_get_object(bucket="b", object_key="k")
    minio.presigned_get_object.assert_called_once_with(
        "b",
        "k",
        expires=timedelta(seconds=settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS),
        response_headers=None,
    )


def test_presigned_get_object_passes_response_headers():
    minio = MagicMock()
    storage = _client(minio)
    headers = {"response-content-disposition": 'attachment; filename="f.pdf"'}
    storage.presigned_get_object(
        bucket="b", object_key="k", expires_seconds=60, response_headers=headers
    )
    minio.presigned_get_object.assert_called_once_with(
        "b", "k", expires=timedelta(seconds=60), response_headers=headers
    )


def test_default_constructor_creates_minio_client():
    fake_minio = MagicMock()
    with patch(
        "media_service.storage.client.get_minio_client", return_value=fake_minio
    ):
        storage = ObjectStorage()
    assert storage.client is fake_minio


def test_get_minio_client_constructs_minio_instance():
    import sys

    mock_minio_mod = MagicMock()
    with patch.dict(sys.modules, {"minio": mock_minio_mod}):
        from media_service.storage.client import get_minio_client

        get_minio_client()
    mock_minio_mod.Minio.assert_called_once()
