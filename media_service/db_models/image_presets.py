"""Database model for per-user dynamic image presets."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import JSON, Column, DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import utcnow


class ImagePresetBase(SQLModel):
    """Shared fields for a user-defined named image preset."""

    owner_user_id: uuid.UUID = Field(index=True)
    tenant_id: uuid.UUID | None = Field(default=None, index=True)
    name: str = Field(min_length=1, max_length=64, index=True)
    spec: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class ImagePreset(ImagePresetBase, SQLModel, table=True):
    """A named preset owned by a user; shadows a same-named built-in at resolve."""

    __tablename__ = prefixed_tables("image_preset")
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id",
            "tenant_id",
            "name",
            name="uq_image_preset_scope_name",
        ),
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
