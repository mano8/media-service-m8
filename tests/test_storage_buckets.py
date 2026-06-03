"""Tests for storage/buckets.py."""

import pytest

from media_service.db_models.media_objects import MediaVisibility
from media_service.storage.buckets import (
    StorageClass,
    bucket_for_storage_class,
    bucket_for_visibility,
)
from media_service.core.config import settings


def test_bucket_public():
    assert bucket_for_visibility(MediaVisibility.PUBLIC) == settings.MINIO_BUCKET_PUBLIC


def test_bucket_private():
    assert (
        bucket_for_visibility(MediaVisibility.PRIVATE) == settings.MINIO_BUCKET_PRIVATE
    )


def test_bucket_sensitive():
    assert (
        bucket_for_visibility(MediaVisibility.SENSITIVE)
        == settings.MINIO_BUCKET_SENSITIVE
    )


def test_bucket_tenant_falls_back_to_private():
    assert (
        bucket_for_visibility(MediaVisibility.TENANT) == settings.MINIO_BUCKET_PRIVATE
    )


def test_bucket_temp():
    assert bucket_for_storage_class(StorageClass.TEMP) == settings.MINIO_BUCKET_TEMP


def test_bucket_archive():
    assert (
        bucket_for_storage_class(StorageClass.ARCHIVE) == settings.MINIO_BUCKET_ARCHIVE
    )


def test_bucket_unknown_visibility_raises():
    with pytest.raises(KeyError):
        bucket_for_visibility("unknown")  # type: ignore[arg-type]
