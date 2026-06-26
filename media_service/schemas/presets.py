"""Pydantic schemas for image presets.

These mirror the ``imgtools_m8`` option shape *locally* so media-service stays
imgtools-free: callers describe a preset, and the resolver expands it into the
``output_options`` dicts carried to the worker. ``ext``/``quality`` bounds match
imgtools' accepted values.
"""

from datetime import datetime
from typing import Literal
import uuid

from pydantic import model_validator
from sqlmodel import Field, SQLModel

ImageFormat = Literal["WEBP", "JPEG", "PNG", "GIF", "AVIF"]

# ── Variant cost bounds (P0.3) ───────────────────────────────────────────────
# Fixed request-policy ceilings that bound the CPU, memory, queue time, and
# storage writes one authenticated caller can demand from variant generation.
# media-service is the request-policy owner; media-worker-m8 carries its own
# independent runtime ceilings as defense in depth (no duplicated config — these
# are the user-facing policy, those are local worker safety limits). They are
# fixed constants, not per-deployment tunables, so every recipe — built-in or
# user-defined — is expanded through one validated path.

#: Hard ceiling (px) on any single fixed dimension or ``fixed_size``.
MAX_PRESET_DIMENSION = 8192
#: Ceiling on ``fixed_width * fixed_height`` when both dimensions are fixed
#: (2**25 ≈ 32 megapixels).
MAX_PRESET_PIXEL_AREA = 33_554_432
#: Ceiling on a preset's optional ``max_byte_size`` encode budget (25 MiB).
MAX_PRESET_MAX_BYTE_SIZE = 26_214_400
#: Max distinct output formats per preset (one VariantSpec/output each). There
#: are only five supported formats, so each must also be unique.
MAX_FORMATS_PER_PRESET = 5
#: Max preset names accepted on a single ``:generate`` request (pre-dedupe).
MAX_PRESETS_PER_REQUEST = 16
#: Max expanded outputs (preset × format) one job may enqueue.
MAX_OUTPUTS_PER_JOB = 32
#: Ceiling on the summed per-output pixel-area cost of one job (2**28 ≈ 256
#: megapixels), an upper bound charging unspecified dimensions at the max.
MAX_JOB_PIXEL_AREA = 268_435_456


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
    formats: list[FormatSpec] = Field(min_length=1, max_length=MAX_FORMATS_PER_PRESET)
    allow_upscale: bool = False
    max_byte_size: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _enforce_cost_bounds(self) -> "PresetSpec":
        """Reject recipes whose per-preset cost exceeds the fixed ceilings.

        Applied on every construction path — user create/update *and* loading a
        stored row or a built-in default — so no recipe can demand unbounded
        geometry, encode budget, or duplicate-format fan-out.
        """
        size = self.image_size
        for value in (size.fixed_width, size.fixed_height, size.fixed_size):
            if value is not None and value > MAX_PRESET_DIMENSION:
                raise ValueError(
                    f"Preset dimension {value} exceeds maximum {MAX_PRESET_DIMENSION}."
                )
        if (
            size.fixed_width is not None
            and size.fixed_height is not None
            and size.fixed_width * size.fixed_height > MAX_PRESET_PIXEL_AREA
        ):
            raise ValueError(
                f"Preset output area {size.fixed_width * size.fixed_height} "
                f"exceeds maximum {MAX_PRESET_PIXEL_AREA}."
            )
        if (
            self.max_byte_size is not None
            and self.max_byte_size > MAX_PRESET_MAX_BYTE_SIZE
        ):
            raise ValueError(
                f"Preset max_byte_size {self.max_byte_size} exceeds maximum "
                f"{MAX_PRESET_MAX_BYTE_SIZE}."
            )
        exts = [fmt.ext for fmt in self.formats]
        if len(set(exts)) != len(exts):
            raise ValueError("Preset formats must be unique.")
        return self


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
