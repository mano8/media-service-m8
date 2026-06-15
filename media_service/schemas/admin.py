"""Response schemas for admin endpoints."""

import uuid
from datetime import datetime

from pydantic import field_validator
from sqlmodel import Field, SQLModel

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


class StorageUsagePublic(SQLModel):
    """Accounting totals and effective quotas for one owner/tenant scope."""

    owner_user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    total_bytes: int
    object_count: int
    # Per-scope overrides as stored (``None`` = falls back to the default).
    quota_bytes: int | None
    quota_objects: int | None
    # Ceilings actually enforced (override else settings default; ``None`` =
    # unlimited).
    effective_quota_bytes: int | None
    effective_quota_objects: int | None


class QuotaUpdateRequest(SQLModel):
    """Admin payload to set per-scope quota overrides.

    Only the fields present in the request body are applied (``exclude_unset``),
    so one ceiling can be changed without disturbing the other. Send an explicit
    ``null`` to clear an override back to the settings default.
    """

    quota_bytes: int | None = None
    quota_objects: int | None = None


class StorageStatsResponse(SQLModel):
    """Aggregate storage statistics across all live objects."""

    by_status: list[StorageStatsByStatus]
    by_category: list[StorageStatsByCategory]
    total_objects: int
    total_bytes: int
    deleted_objects: int
    usage: list[StorageUsagePublic]


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


class SubscriptionCreateRequest(SQLModel):
    """Admin payload to register a webhook subscriber.

    ``event_types`` is the dotted-name filter; an empty list subscribes to every
    event type. ``secret`` is the per-subscriber HMAC key the delivery worker
    signs each POST with — held only here, never exposed in any response.
    """

    url: str = Field(min_length=1, max_length=2048)
    secret: str = Field(min_length=16, max_length=255)
    event_types: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def _require_http_scheme(cls, value: str) -> str:
        """Reject non-HTTP(S) callback URLs at request validation (422)."""
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class SubscriptionPublic(SQLModel):
    """Public view of a subscription — never includes the signing secret."""

    id: uuid.UUID
    url: str
    event_types: list[str]
    active: bool
    created_at: datetime


class SubscriptionListResponse(SQLModel):
    """List of registered webhook subscriptions."""

    count: int
    items: list[SubscriptionPublic]
