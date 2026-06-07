"""Logical bucket resolution for media objects."""

from enum import StrEnum

from media_service.core.config import settings
from media_service.db_models.media_objects import MediaVisibility


class StorageClass(StrEnum):
    """Storage classes that map to lifecycle-oriented buckets."""

    TEMP = "temp"
    ARCHIVE = "archive"


def bucket_for_visibility(visibility: MediaVisibility) -> str:
    """Return the configured bucket for a media visibility."""
    return {
        MediaVisibility.PUBLIC: settings.MINIO_BUCKET_PUBLIC,
        MediaVisibility.PRIVATE: settings.MINIO_BUCKET_PRIVATE,
        MediaVisibility.SENSITIVE: settings.MINIO_BUCKET_SENSITIVE,
        MediaVisibility.TENANT: settings.MINIO_BUCKET_PRIVATE,
    }[visibility]


def bucket_for_storage_class(storage_class: StorageClass) -> str:
    """Return the configured bucket for non-primary storage classes."""
    return {
        StorageClass.TEMP: settings.MINIO_BUCKET_TEMP,
        StorageClass.ARCHIVE: settings.MINIO_BUCKET_ARCHIVE,
    }[storage_class]
