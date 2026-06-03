"""Response schemas for admin endpoints."""

import uuid
from datetime import datetime

from sqlmodel import SQLModel

from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectStatus,
    MediaVisibility,
)


class StorageStatsByStatus(SQLModel):
    """Object counts and bytes for a single status bucket."""

    status: MediaObjectStatus
    count: int
    total_bytes: int


class StorageStatsByCategory(SQLModel):
    """Object counts and bytes for a single category."""

    category: MediaCategory
    count: int
    total_bytes: int


class StorageStatsResponse(SQLModel):
    """Aggregate storage statistics across all live objects."""

    by_status: list[StorageStatsByStatus]
    by_category: list[StorageStatsByCategory]
    total_objects: int
    total_bytes: int
    deleted_objects: int


class StaleUploadSession(SQLModel):
    """Minimal projection of an UploadSession for the stale-uploads report."""

    id: uuid.UUID
    owner_user_id: uuid.UUID
    category: MediaCategory
    visibility: MediaVisibility
    storage_bucket: str
    object_key: str
    expires_at: datetime
    created_at: datetime


class StaleUploadsResponse(SQLModel):
    """List of upload sessions that are past their expiry in INITIATED state."""

    count: int
    sessions: list[StaleUploadSession]


class PurgeStaleResponse(SQLModel):
    """Result of a bulk purge of stale upload sessions."""

    purged: int
