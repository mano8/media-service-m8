"""Pydantic schemas for media object metadata endpoints."""

from datetime import datetime

from sqlmodel import SQLModel

from media_service.db_models.media_objects import MediaCategory, MediaVisibility


class MediaObjectUpdate(SQLModel):
    """Fields that may be updated on an existing media object."""

    visibility: MediaVisibility | None = None
    original_filename: str | None = None
    category: MediaCategory | None = None


class DownloadUrlResponse(SQLModel):
    """Presigned download URL with its expiry timestamp."""

    url: str
    expires_at: datetime
