"""Tests for storage/buckets.py."""

import inspect

import pytest

from media_service.db_models.media_objects import MediaVisibility
from media_service.storage.buckets import (
    StorageClass,
    bucket_for_storage_class,
    bucket_for_visibility,
)
from media_service.core.config import settings


def test_bucket_public():
    assert bucket_for_visibility(MediaVisibility.PUBLIC) == settings.S3_BUCKET_PUBLIC


def test_bucket_private():
    assert bucket_for_visibility(MediaVisibility.PRIVATE) == settings.S3_BUCKET_PRIVATE


def test_bucket_sensitive():
    assert (
        bucket_for_visibility(MediaVisibility.SENSITIVE) == settings.S3_BUCKET_SENSITIVE
    )


def test_bucket_tenant_falls_back_to_private():
    assert bucket_for_visibility(MediaVisibility.TENANT) == settings.S3_BUCKET_PRIVATE


def test_bucket_temp():
    assert bucket_for_storage_class(StorageClass.TEMP) == settings.S3_BUCKET_TEMP


def test_bucket_archive():
    assert bucket_for_storage_class(StorageClass.ARCHIVE) == settings.S3_BUCKET_ARCHIVE


def test_bucket_unknown_visibility_raises():
    with pytest.raises(KeyError):
        bucket_for_visibility("unknown")  # type: ignore[arg-type]


def test_archive_storage_class_has_a_writer():
    """`StorageClass.ARCHIVE` resolves a bucket somebody actually writes to.

    `T28` (M27) measured `archive-media` as a bucket with no producing code
    path; `T32` gave it one — the soft-delete cold move in
    `ObjectsController.delete_object`. This pins the caller by name so the
    tier cannot silently go back to being reserved-and-empty: if the writer
    is removed, remove the storage class and the bucket with it.
    """
    from media_service.controllers import objects

    writer = objects._archive_deleted_bytes
    assert "bucket_for_storage_class(StorageClass.ARCHIVE)" in inspect.getsource(writer)
    assert writer.__name__ in inspect.getsource(objects.ObjectsController.delete_object)
