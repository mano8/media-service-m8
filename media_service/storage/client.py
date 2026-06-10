"""MinIO client configuration boundary."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from media_service.core.config import settings


@dataclass(frozen=True)
class ObjectStorageConfig:
    """Connection settings for S3-compatible object storage."""

    endpoint: str
    access_key: str
    secret_key: str
    secure: bool
    region: str


def get_storage_config() -> ObjectStorageConfig:
    """Return the configured MinIO endpoint and credentials."""
    return ObjectStorageConfig(
        endpoint=f"{settings.MINIO_HOST}:{settings.MINIO_PORT}",
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_USE_SSL,
        region=settings.MINIO_REGION,
    )


def get_minio_client() -> Any:
    """Create a MinIO SDK client from settings."""
    from minio import Minio

    config = get_storage_config()
    return Minio(
        endpoint=config.endpoint,
        access_key=config.access_key,
        secret_key=config.secret_key,
        secure=config.secure,
        region=config.region,
    )


class ObjectStorage:
    """Small wrapper around MinIO operations used by the media service."""

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or get_minio_client()

    def stat_object(self, *, bucket: str, object_key: str) -> Any:
        """Return object metadata from storage."""
        return self.client.stat_object(bucket, object_key)

    def remove_object(self, *, bucket: str, object_key: str) -> None:
        """Remove an object from storage."""
        self.client.remove_object(bucket, object_key)

    def get_object_head(
        self, *, bucket: str, object_key: str, length: int = 512
    ) -> bytes:
        """Read the first *length* bytes of an object for content-type sniffing."""
        response = self.client.get_object(bucket, object_key, offset=0, length=length)
        try:
            return response.read(length)
        finally:
            response.close()
            response.release_conn()

    def get_object(self, *, bucket: str, object_key: str) -> bytes:
        """Download an entire object and return its raw bytes."""
        response = self.client.get_object(bucket, object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def set_object_content_type(
        self, *, bucket: str, object_key: str, content_type: str
    ) -> Any:
        """Rewrite an object's stored ``Content-Type`` in place.

        The presigned PUT lets the client choose the ``Content-Type`` sent to
        storage, and for public-read buckets that type is served verbatim on
        direct access. Forcing the server-validated type here (via a metadata-
        only server-side copy) prevents a client from having an object served
        as an active type — e.g. ``text/html`` declared as ``text/plain`` —
        regardless of what it sent on upload. Returns the write result so the
        caller can pick up the authoritative post-copy etag.
        """
        from minio.commonconfig import REPLACE, CopySource

        return self.client.copy_object(
            bucket,
            object_key,
            CopySource(bucket, object_key),
            metadata={"Content-Type": content_type},
            metadata_directive=REPLACE,
        )

    def copy_object(
        self,
        *,
        src_bucket: str,
        src_object_key: str,
        dest_bucket: str,
        dest_object_key: str,
    ) -> Any:
        """Server-side copy an object to another bucket/key.

        Used to relocate bytes when an object's visibility changes and it must
        move between the public/private/sensitive buckets. Returns the write
        result so the caller can pick up the post-copy etag.
        """
        from minio.commonconfig import CopySource

        return self.client.copy_object(
            dest_bucket,
            dest_object_key,
            CopySource(src_bucket, src_object_key),
        )

    def post_upload_url(self, *, bucket: str) -> str:
        """Return the POST endpoint URL for a bucket (path-style addressing).

        ``presigned_post_policy`` only returns the signed form fields, not the
        target URL; MinIO uses path-style addressing, so the form is POSTed to
        ``{scheme}://{host}:{port}/{bucket}``.
        """
        config = get_storage_config()
        scheme = "https" if config.secure else "http"
        return f"{scheme}://{config.endpoint}/{bucket}"

    def presigned_post_object(
        self,
        *,
        bucket: str,
        object_key: str,
        content_type: str,
        max_size_bytes: int,
        min_size_bytes: int = 1,
        expires_seconds: int | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Generate a presigned POST policy that constrains size and content-type.

        Unlike a presigned PUT — which lets the client write an object of any
        size and any ``Content-Type`` — an S3 POST policy is enforced by storage
        at upload time: the ``content-length-range`` and exact ``Content-Type``
        conditions cause MinIO to reject an oversized or wrong-typed body
        *before* it lands, closing the window in which garbage occupies a bucket
        until ``complete`` rejects it.

        Returns the POST URL and the form fields the client must submit
        alongside the ``file`` part (the ``key`` and ``Content-Type`` fields are
        pinned to the values the policy was signed for).
        """
        from minio.datatypes import PostPolicy

        expires = expires_seconds or settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        expiration = datetime.now(timezone.utc) + timedelta(seconds=expires)
        policy = PostPolicy(bucket, expiration)
        policy.add_equals_condition("key", object_key)
        policy.add_equals_condition("Content-Type", content_type)
        policy.add_content_length_range_condition(min_size_bytes, max_size_bytes)
        fields = self.client.presigned_post_policy(policy)
        # The policy only signs the conditions; echo the pinned values back so
        # the client submits them verbatim (any deviation fails the signature).
        fields["key"] = object_key
        fields["Content-Type"] = content_type
        return self.post_upload_url(bucket=bucket), fields

    def presigned_get_object(
        self,
        *,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
        response_headers: dict[str, str] | None = None,
    ) -> str:
        """Generate a presigned GET URL."""
        expires = timedelta(
            seconds=expires_seconds or settings.MINIO_PRESIGNED_URL_EXPIRE_SECONDS
        )
        return self.client.presigned_get_object(
            bucket,
            object_key,
            expires=expires,
            response_headers=response_headers,
        )
