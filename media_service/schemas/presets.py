"""Pydantic schemas for image presets.

These mirror the ``imgtools_m8`` option shape *locally* so media-service stays
imgtools-free: callers describe a preset, and the resolver expands it into the
``output_options`` dicts carried to the worker. ``ext``/``quality`` bounds match
imgtools' accepted values.
"""

from datetime import datetime
from typing import Literal
import uuid

from sqlmodel import Field, SQLModel

ImageFormat = Literal["WEBP", "JPEG", "PNG", "GIF", "AVIF"]


class FormatSpec(SQLModel):
    """A single output format and its encode quality."""

    ext: ImageFormat
    quality: int = Field(ge=1, le=100)


class ImageSizeSpec(SQLModel):
    """Target geometry; all optional so a preset can fix one dimension only."""

    fixed_width: int | None = Field(default=None, ge=1)
    fixed_height: int | None = Field(default=None, ge=1)
    fixed_size: int | None = Field(default=None, ge=1)


class PresetSpec(SQLModel):
    """A reusable variant recipe: one geometry rendered into one+ formats."""

    image_size: ImageSizeSpec
    formats: list[FormatSpec] = Field(min_length=1)
    allow_upscale: bool = False
    max_byte_size: int | None = Field(default=None, ge=1)


class ImagePresetCreate(SQLModel):
    """Payload to create a user-owned named preset."""

    name: str = Field(min_length=1, max_length=64)
    spec: PresetSpec


class ImagePresetUpdate(SQLModel):
    """Payload to replace an existing preset's recipe."""

    spec: PresetSpec


class ImagePresetPublic(SQLModel):
    """Public view of a preset (built-in or user-defined)."""

    id: uuid.UUID | None = None
    name: str
    spec: PresetSpec
    builtin: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
