"""Database model for time-boxed, signed share links to a media object."""

from datetime import datetime
import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class ShareTokenBase(SQLModel):
    """Shared fields for an owner-issued share link to a media object.

    Multi-tenant scope mirrors :class:`StorageUsage`: ``owner_user_id`` plus an
    optional ``tenant_id``. The ``ON DELETE CASCADE`` foreign key is required —
    Phase 14's hard-purge issues a real ``DELETE`` on the parent object, so the
    database must drop dependent tokens itself rather than strand them.
    """

    media_object_id: uuid.UUID = Field(
        foreign_key=f"{prefixed_tables('media_object')}.id",
        ondelete="CASCADE",
        index=True,
    )
    owner_user_id: uuid.UUID = Field(index=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # ``None`` means unlimited uses; otherwise the link resolves only while
    # ``uses < max_uses``.
    max_uses: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    uses: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, default=0),
    )
    revoked: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, default=False),
    )


class ShareToken(ShareTokenBase, SQLModel, table=True):
    """Source-of-truth record for a shareable download link."""

    __tablename__ = prefixed_tables("share_token")
    __table_args__ = (
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
