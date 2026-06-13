"""Database model tracking an image-variant generation job."""

from datetime import datetime
from enum import StrEnum
import uuid

from sqlalchemy import JSON, Column, DateTime, String
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class VariantJobStatus(StrEnum):
    """Lifecycle state for a variant-generation job."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class VariantJobBase(SQLModel):
    """Shared fields for a variant-generation job."""

    media_object_id: uuid.UUID = Field(
        foreign_key=f"{prefixed_tables('media_object')}.id",
        index=True,
    )
    owner_user_id: uuid.UUID = Field(index=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    status: VariantJobStatus = Field(
        default=VariantJobStatus.QUEUED,
        sa_column=Column(String(32), nullable=False, index=True),
    )
    requested_presets: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
    )
    variants_expected: int = Field(default=0, ge=0)
    variants_created: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=1024)


class VariantJob(VariantJobBase, SQLModel, table=True):
    """A unit of variant work whose ``id`` doubles as the ARQ job id."""

    __tablename__ = prefixed_tables("variant_job")
    __table_args__ = (
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
