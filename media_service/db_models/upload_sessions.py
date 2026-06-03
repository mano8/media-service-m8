"""Database model for presigned upload sessions."""

from datetime import datetime
from enum import StrEnum
import uuid

from sqlalchemy import Column, DateTime, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import MediaCategory, MediaVisibility, utcnow


class UploadSessionStatus(StrEnum):
    """Lifecycle state for presigned upload sessions."""

    INITIATED = "initiated"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class UploadSessionBase(SQLModel):
    """Shared fields for upload session metadata."""

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
    expected_mime_type: str = Field(min_length=1, max_length=255)
    expected_size_bytes: int = Field(ge=0)
    status: UploadSessionStatus = Field(
        default=UploadSessionStatus.INITIATED,
        sa_column=Column(String(32), nullable=False, index=True),
    )


class UploadSession(UploadSessionBase, SQLModel, table=True):
    """Presigned upload session tracked before object completion."""

    __tablename__ = prefixed_tables("upload_session")
    __table_args__ = (
        UniqueConstraint("storage_bucket", "object_key", name="uq_upload_session_key"),
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
