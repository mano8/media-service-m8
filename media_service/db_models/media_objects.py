"""Database model for media object metadata."""

from datetime import datetime, timezone
from enum import StrEnum
import uuid

from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class MediaCategory(StrEnum):
    """Initial generic media categories."""

    AVATAR = "avatar"
    DOCUMENT = "document"
    ASSET = "asset"
    CHAT_ATTACHMENT = "chat_attachment"
    EXPORT = "export"
    RECEIPT = "receipt"


class MediaVisibility(StrEnum):
    """Object visibility policy."""

    PUBLIC = "public"
    PRIVATE = "private"
    TENANT = "tenant"
    SENSITIVE = "sensitive"


class MediaObjectStatus(StrEnum):
    """Lifecycle state for a media object."""

    PENDING_UPLOAD = "pending_upload"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"
    REJECTED = "rejected"


class ScanStatus(StrEnum):
    """Antivirus scanning state."""

    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"


class ModerationStatus(StrEnum):
    """Content moderation state."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class MediaObjectBase(SQLModel):
    """Shared fields for media object metadata."""

    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    owner_user_id: uuid.UUID = Field(index=True)
    category: MediaCategory = Field(
        sa_column=Column(String(64), nullable=False, index=True),
    )
    visibility: MediaVisibility = Field(
        sa_column=Column(String(32), nullable=False, index=True),
    )
    storage_bucket: str = Field(min_length=3, max_length=63, index=True)
    object_key: str = Field(min_length=1, max_length=1024, index=True)
    original_filename: str | None = Field(default=None, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255, index=True)
    extension: str | None = Field(default=None, max_length=32)
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64, index=True)
    etag: str | None = Field(default=None, max_length=255)
    storage_class: str = Field(default="standard", max_length=64)
    status: MediaObjectStatus = Field(
        default=MediaObjectStatus.PENDING_UPLOAD,
        sa_column=Column(String(32), nullable=False, index=True),
    )
    scan_status: ScanStatus = Field(
        default=ScanStatus.PENDING,
        sa_column=Column(String(32), nullable=False, index=True),
    )
    moderation_status: ModerationStatus = Field(
        default=ModerationStatus.PENDING,
        sa_column=Column(String(32), nullable=False, index=True),
    )


class MediaObject(MediaObjectBase, SQLModel, table=True):
    """Source-of-truth record for a stored media object."""

    __tablename__ = prefixed_tables("media_object")
    __table_args__ = (
        UniqueConstraint("storage_bucket", "object_key", name="uq_media_object_key"),
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    deleted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class MediaObjectPublic(MediaObjectBase):
    """Public representation of media object metadata."""

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
