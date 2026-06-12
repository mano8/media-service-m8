"""Pydantic schemas for the presigned upload flow."""

import uuid
from datetime import datetime

from sqlmodel import SQLModel

from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectPublic,
    MediaVisibility,
)


class UploadInitiateRequest(SQLModel):
    """Payload to initiate a presigned upload session."""

    category: MediaCategory
    visibility: MediaVisibility
    original_filename: str
    mime_type: str
    expected_size_bytes: int


class UploadInitiateResponse(SQLModel):
    """Response after a session is created.

    ``upload_url`` is an S3 POST endpoint: the client must send a multipart
    ``POST`` containing every entry of ``upload_fields`` followed by the
    ``file`` part. The signed policy caps the body size and pins the
    ``Content-Type``, so storage rejects an oversized or wrong-typed upload
    rather than letting it land.
    """

    session_id: uuid.UUID
    upload_url: str
    upload_fields: dict[str, str]
    expires_at: datetime


class UploadCompleteRequest(SQLModel):
    """Optional payload for completing an upload."""

    sha256: str | None = None


class UploadCompleteResponse(SQLModel):
    """Response after an upload is confirmed in storage."""

    media_object: MediaObjectPublic
