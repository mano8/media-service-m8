"""Tests for storage/presign.py."""

from unittest.mock import MagicMock

from media_service.storage.client import ObjectStorage
from media_service.storage.presign import create_download_url, create_upload_url


def _storage(return_url: str = "https://minio/presigned") -> MagicMock:
    s = MagicMock(spec=ObjectStorage)
    s.presigned_post_object.return_value = (return_url, {"key": "k"})
    s.presigned_get_object.return_value = return_url
    return s


def test_create_upload_url_delegates_to_storage():
    storage = _storage("https://minio/post")
    url, fields = create_upload_url(
        storage=storage,
        bucket="b",
        object_key="k",
        content_type="application/pdf",
        max_size_bytes=2048,
        expires_seconds=300,
    )
    assert url == "https://minio/post"
    assert fields == {"key": "k"}
    storage.presigned_post_object.assert_called_once_with(
        bucket="b",
        object_key="k",
        content_type="application/pdf",
        max_size_bytes=2048,
        expires_seconds=300,
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
        "response-content-disposition": (
            "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        )
    }


def test_create_download_url_filename_with_quote_is_sanitized():
    storage = _storage("https://minio/get")
    create_download_url(
        storage=storage,
        bucket="b",
        object_key="k",
        expires_seconds=60,
        filename='evil".pdf',
    )
    _, kwargs = storage.presigned_get_object.call_args
    disposition = kwargs["response_headers"]["response-content-disposition"]
    # The raw quote must not survive into the ASCII fallback (no header breakout).
    assert 'filename="evil".pdf"' not in disposition
    assert 'filename="evil_.pdf"' in disposition
    assert "filename*=UTF-8''evil%22.pdf" in disposition


def test_create_download_url_filename_with_crlf_is_sanitized():
    storage = _storage("https://minio/get")
    create_download_url(
        storage=storage,
        bucket="b",
        object_key="k",
        expires_seconds=60,
        filename="a\r\nX-Injected: 1.pdf",
    )
    _, kwargs = storage.presigned_get_object.call_args
    disposition = kwargs["response_headers"]["response-content-disposition"]
    # No bare CR/LF may reach the header value.
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert "%0D%0A" in disposition
