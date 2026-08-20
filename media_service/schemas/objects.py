"""Pydantic schemas for media object metadata endpoints."""

from datetime import datetime
from typing import Literal
import uuid

from sqlmodel import Field, SQLModel

from media_service.db_models.categories import MAX_CATEGORY_ASSIGNMENTS
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
    ScanStatus,
)

SortField = Literal[
    "original_filename", "category", "status", "size_bytes", "created_at"
]
SortOrder = Literal["asc", "desc"]


class MediaObjectUpdate(SQLModel):
    """Fields that may be updated on an existing media object."""

    visibility: MediaVisibility | None = None
    original_filename: str | None = None
    category: MediaCategory | None = None
    # Set semantics (`U4`): a body carrying ``category_ids`` replaces the
    # object's whole filing — ``[]`` unfiles it — while omitting the field
    # leaves the existing filing alone. That is what ``None`` means here, so it
    # cannot default to ``[]``. Not a column on ``MediaObject``: the filing is
    # link rows, which is what lets one object sit in several categories.
    category_ids: list[int] | None = Field(
        default=None,
        max_length=MAX_CATEGORY_ASSIGNMENTS,
        description="Replaces every user category this object is filed into",
    )


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
    sort_by: SortField = "original_filename"
    order: SortOrder = "asc"
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None
    owner_user_id: uuid.UUID | None = None
    include_deleted: bool = False


class ObjectListResponse(SQLModel):
    """A page of media objects with an opaque cursor to the next page."""

    items: list[MediaObjectPublic]
    next_cursor: str | None = None
    count: int


class ScanResultRequest(SQLModel):
    """Internal worker callback carrying an antivirus verdict for an object."""

    scan_status: ScanStatus


class DownloadNotAvailableDetail(SQLModel):
    """Structured 409 detail raised when a non-clean object is downloaded.

    Same flat-object shape as ``UploadRejectDetail``
    (``schemas/uploads.py``): a stable ``code``, a machine-readable field —
    here ``scan_status`` rather than ``reason`` — and a human ``message``, so
    a quarantined/infected download reads "…failed the virus scan" instead of
    the generic "not available for download" string.
    """

    code: Literal["scan_not_clean"] = "scan_not_clean"
    scan_status: ScanStatus
    message: str
