"""Pydantic schemas for media variant generation, query, and internal upserts."""

from datetime import datetime
import uuid

from sqlmodel import Field, SQLModel

from media_service.db_models.variant_jobs import VariantJobStatus


class VariantGenerateRequest(SQLModel):
    """Public request to generate variants for an object from named presets."""

    presets: list[str] = Field(min_length=1)


class VariantPublic(SQLModel):
    """Public view of a single generated variant."""

    id: uuid.UUID
    media_object_id: uuid.UUID
    variant_name: str
    storage_bucket: str
    object_key: str
    width: int | None = None
    height: int | None = None
    size_bytes: int
    format: str
    created_at: datetime


class VariantListResponse(SQLModel):
    """A list of variants for one media object."""

    items: list[VariantPublic]
    count: int


class VariantJobPublic(SQLModel):
    """Public view of a variant-generation job's progress."""

    id: uuid.UUID
    media_object_id: uuid.UUID
    owner_user_id: uuid.UUID
    status: VariantJobStatus
    requested_presets: list[str]
    variants_expected: int
    variants_created: int
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class VariantRegisterRequest(SQLModel):
    """Internal request from the worker to register a written variant."""

    variant_name: str = Field(min_length=1, max_length=64)
    storage_bucket: str = Field(min_length=3, max_length=63)
    object_key: str = Field(min_length=1, max_length=1024)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    size_bytes: int = Field(ge=0)
    format: str = Field(min_length=1, max_length=32)


class VariantJobUpdate(SQLModel):
    """Internal request from the worker to advance a job's status."""

    status: VariantJobStatus
    variants_created: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=1024)
