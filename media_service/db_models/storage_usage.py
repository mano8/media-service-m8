"""Database model for per-owner storage accounting and quotas."""

from datetime import datetime
import uuid

from sqlalchemy import BigInteger, Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class StorageUsageBase(SQLModel):
    """Shared accounting fields for a single ``(owner_user_id, tenant_id)`` scope."""

    owner_user_id: uuid.UUID = Field(index=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    total_bytes: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
    )
    object_count: int = Field(
        default=0,
        sa_column=Column(BigInteger, nullable=False, default=0),
    )
    # Per-scope overrides; ``None`` falls back to the settings default.
    quota_bytes: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )
    quota_objects: int | None = Field(
        default=None,
        sa_column=Column(BigInteger, nullable=True),
    )


class StorageUsage(StorageUsageBase, SQLModel, table=True):
    """Running byte/object totals and quota overrides per owner/tenant scope."""

    __tablename__ = prefixed_tables("storage_usage")
    __table_args__ = (
        UniqueConstraint("owner_user_id", "tenant_id", name="uq_storage_usage_scope"),
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
