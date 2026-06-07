"""Pydantic schemas for media object metadata endpoints."""

from datetime import datetime
from typing import Literal
import uuid

from sqlmodel import SQLModel

from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
)

SortField = Literal["created_at", "size_bytes"]
SortOrder = Literal["asc", "desc"]


class MediaObjectUpdate(SQLModel):
    """Fields that may be updated on an existing media object."""

    visibility: MediaVisibility | None = None
    original_filename: str | None = None
    category: MediaCategory | None = None


class DownloadUrlResponse(SQLModel):
    """Presigned download URL with its expiry timestamp."""

    url: str
    expires_at: datetime


class ObjectListParams(SQLModel):
    """Resolved query parameters for listing media objects."""

    category: MediaCategory | None = None
    visibility: MediaVisibility | None = None
    status: MediaObjectStatus | None = None
    mime_prefix: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    q: str | None = None
    sort_by: SortField = "created_at"
    order: SortOrder = "desc"
    limit: int = 50
    cursor: str | None = None
    owner_user_id: uuid.UUID | None = None
    include_deleted: bool = False


class ObjectListResponse(SQLModel):
    """A page of media objects with an opaque cursor to the next page."""

    items: list[MediaObjectPublic]
    next_cursor: str | None = None
    count: int
