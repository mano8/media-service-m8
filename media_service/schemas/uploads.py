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
    tenant_id: uuid.UUID | None = None


class UploadInitiateResponse(SQLModel):
    """Response after a session is created."""

    session_id: uuid.UUID
    upload_url: str
    expires_at: datetime


class UploadCompleteRequest(SQLModel):
    """Optional payload for completing an upload."""

    sha256: str | None = None


class UploadCompleteResponse(SQLModel):
    """Response after an upload is confirmed in storage."""

    media_object: MediaObjectPublic
