"""Tests for storage/presign.py."""

from unittest.mock import MagicMock

from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url, create_upload_url


def _storage(return_url: str = "https://minio/presigned") -> MagicMock:
    s = MagicMock(spec=ObjectStorage)
    s.presigned_put_object.return_value = return_url
    s.presigned_get_object.return_value = return_url
    return s


def test_create_upload_url_delegates_to_storage():
    storage = _storage("https://minio/put")
    url = create_upload_url(
        storage=storage, bucket="b", object_key="k", expires_seconds=300
    )
    assert url == "https://minio/put"
    storage.presigned_put_object.assert_called_once_with(
        bucket="b", object_key="k", expires_seconds=300
    )


def test_create_download_url_without_filename():
    storage = _storage("https://minio/get")
    url = create_download_url(
        storage=storage, bucket="b", object_key="k", expires_seconds=60
    )
    assert url == "https://minio/get"
    storage.presigned_get_object.assert_called_once_with(
        bucket="b",
        object_key="k",
        expires_seconds=60,
        response_headers=None,
    )


def test_create_download_url_with_filename():
    storage = _storage("https://minio/get")
    create_download_url(
        storage=storage,
        bucket="b",
        object_key="k",
        expires_seconds=60,
        filename="report.pdf",
    )
    _, kwargs = storage.presigned_get_object.call_args
    assert kwargs["response_headers"] == {
        "response-content-disposition": 'attachment; filename="report.pdf"'
    }
