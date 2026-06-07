"""Presigned URL helpers."""

from media_service.storage.client import ObjectStorage


def create_upload_url(
    *,
    storage: ObjectStorage,
    bucket: str,
    object_key: str,
    expires_seconds: int,
) -> str:
    """Create a presigned upload URL."""
    return storage.presigned_put_object(
        bucket=bucket,
        object_key=object_key,
        expires_seconds=expires_seconds,
    )


def create_download_url(
    *,
    storage: ObjectStorage,
    bucket: str,
    object_key: str,
    expires_seconds: int,
    filename: str | None = None,
) -> str:
    """Create a presigned download URL."""
    headers = None
    if filename:
        headers = {"response-content-disposition": f'attachment; filename="{filename}"'}
    return storage.presigned_get_object(
        bucket=bucket,
        object_key=object_key,
        expires_seconds=expires_seconds,
        response_headers=headers,
    )
