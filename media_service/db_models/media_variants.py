"""Database model for media variants."""

from datetime import datetime
import uuid

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from media_service.core.config import settings
from media_service.core.db_models import prefixed_tables
from media_service.db_models.media_objects import MediaObject, utcnow


class MediaVariantBase(SQLModel):
    """Shared fields for generated media variants."""

    media_object_id: uuid.UUID = Field(
        foreign_key=f"{prefixed_tables('media_object')}.id",
        index=True,
    )
    variant_name: str = Field(min_length=1, max_length=64, index=True)
    storage_bucket: str = Field(min_length=3, max_length=63)
    object_key: str = Field(min_length=1, max_length=1024)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    size_bytes: int = Field(ge=0)
    format: str = Field(min_length=1, max_length=32)


class MediaVariant(MediaVariantBase, SQLModel, table=True):
    """Generated or derived object variant."""

    __tablename__ = prefixed_tables("media_variant")
    __table_args__ = (
        UniqueConstraint(
            "media_object_id",
            "variant_name",
            name="uq_media_variant_name",
        ),
        UniqueConstraint(
            "storage_bucket",
            "object_key",
            name="uq_media_variant_key",
        ),
        {"mysql_engine": settings.DB_ENGINE, "mysql_charset": settings.DB_CHARSET},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    media_object: MediaObject | None = Relationship()
