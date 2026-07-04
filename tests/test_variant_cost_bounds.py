"""Tests for P0.3 variant preset/job cost bounds.

Covers the per-preset schema ceilings (`PresetSpec`), the request-level dedupe
and count cap (`VariantGenerateRequest`), and the job-level output-count and
summed pixel-area cost bounds in `resolve_presets` / `_geometry_cost`.
"""

import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session

from media_service.core.presets import _geometry_cost, resolve_presets
from media_service.db_models.image_presets import ImagePreset
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
)
from media_service.schemas.presets import (
    MAX_JOB_PIXEL_AREA,
    MAX_OUTPUTS_PER_JOB,
    MAX_PRESET_DIMENSION,
    MAX_PRESET_MAX_BYTE_SIZE,
    ImageSizeSpec,
    PresetSpec,
)
from media_service.schemas.variants import VariantGenerateRequest

_ALL_FORMATS = [
    {"ext": "WEBP", "quality": 80},
    {"ext": "JPEG", "quality": 80},
    {"ext": "PNG", "quality": 100},
    {"ext": "GIF", "quality": 80},
    {"ext": "AVIF", "quality": 70},
]


def _image_object(session: Session, owner_id: uuid.UUID) -> MediaObject:
    oid = uuid.uuid4()
    obj = MediaObject(
        id=oid,
        owner_user_id=owner_id,
        category="asset",
        visibility="private",
        storage_bucket="private-media",
        object_key=f"users/{owner_id}/asset/{oid}/original/pic.png",
        original_filename="pic.png",
        mime_type="image/png",
        size_bytes=2048,
        status=MediaObjectStatus.READY,
        scan_status=ScanStatus.CLEAN,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _add_preset(session: Session, owner_id: uuid.UUID, name: str, spec: dict) -> None:
    session.add(ImagePreset(owner_user_id=owner_id, name=name, spec=spec))
    session.commit()


# ── Per-preset schema bounds ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "image_size",
    [
        {"fixed_width": MAX_PRESET_DIMENSION + 1},
        {"fixed_height": MAX_PRESET_DIMENSION + 1},
        {"fixed_size": MAX_PRESET_DIMENSION + 1},
    ],
)
def test_preset_dimension_over_max_rejected(image_size: dict):
    with pytest.raises(ValidationError, match="exceeds maximum"):
        PresetSpec.model_validate(
            {"image_size": image_size, "formats": [{"ext": "WEBP", "quality": 80}]}
        )


def test_preset_pixel_area_over_max_rejected():
    # Both dimensions are within the per-side cap but their product is not.
    with pytest.raises(ValidationError, match="output area"):
        PresetSpec.model_validate(
            {
                "image_size": {
                    "fixed_width": MAX_PRESET_DIMENSION,
                    "fixed_height": MAX_PRESET_DIMENSION,
                },
                "formats": [{"ext": "WEBP", "quality": 80}],
            }
        )


def test_preset_max_byte_size_over_max_rejected():
    with pytest.raises(ValidationError, match="max_byte_size"):
        PresetSpec.model_validate(
            {
                "image_size": {"fixed_width": 100},
                "formats": [{"ext": "WEBP", "quality": 80}],
                "max_byte_size": MAX_PRESET_MAX_BYTE_SIZE + 1,
            }
        )


def test_preset_duplicate_formats_rejected():
    with pytest.raises(ValidationError, match="must be unique"):
        PresetSpec.model_validate(
            {
                "image_size": {"fixed_width": 100},
                "formats": [
                    {"ext": "WEBP", "quality": 80},
                    {"ext": "WEBP", "quality": 70},
                ],
            }
        )


def test_preset_too_many_formats_rejected():
    with pytest.raises(ValidationError):
        PresetSpec.model_validate(
            {
                "image_size": {"fixed_width": 100},
                "formats": _ALL_FORMATS + [{"ext": "WEBP", "quality": 50}],
            }
        )


def test_preset_at_bounds_accepted():
    spec = PresetSpec.model_validate(
        {
            "image_size": {"fixed_width": MAX_PRESET_DIMENSION},
            "formats": _ALL_FORMATS,
            "max_byte_size": MAX_PRESET_MAX_BYTE_SIZE,
        }
    )
    assert len(spec.formats) == 5


# ── Request-level dedupe / count cap ────────────────────────────────────────


def test_request_dedupes_preset_names():
    req = VariantGenerateRequest.model_validate(
        {"presets": ["thumb", "large", "thumb", "small", "large"]}
    )
    assert req.presets == ["thumb", "large", "small"]


def test_request_rejects_too_many_presets():
    with pytest.raises(ValidationError):
        VariantGenerateRequest.model_validate({"presets": [f"p{i}" for i in range(17)]})


# ── Job-level output-count / pixel-area cost bounds ─────────────────────────


def test_resolve_rejects_too_many_outputs(session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    obj = _image_object(session, owner)
    # Seven five-format presets = 35 outputs > the 32-output cap.
    names = [f"multi{i}" for i in range(7)]
    for name in names:
        _add_preset(
            session,
            owner,
            name,
            {"image_size": {"fixed_width": 64}, "formats": _ALL_FORMATS},
        )
    with pytest.raises(HTTPException) as exc:
        resolve_presets(
            session, current_user=current_user, names=names, media_object=obj
        )
    assert exc.value.status_code == 422
    assert "outputs" in exc.value.detail
    assert len(names) * 5 > MAX_OUTPUTS_PER_JOB


def test_resolve_rejects_excessive_pixel_area(session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    obj = _image_object(session, owner)
    # Five single-format max-width presets stay under the output cap but their
    # summed per-output area cost (5 × MAX_PRESET_DIMENSION²) exceeds the budget.
    names = [f"big{i}" for i in range(5)]
    for name in names:
        _add_preset(
            session,
            owner,
            name,
            {
                "image_size": {"fixed_width": MAX_PRESET_DIMENSION},
                "formats": [{"ext": "WEBP", "quality": 80}],
            },
        )
    with pytest.raises(HTTPException) as exc:
        resolve_presets(
            session, current_user=current_user, names=names, media_object=obj
        )
    assert exc.value.status_code == 422
    assert "area cost" in exc.value.detail
    assert len(names) <= MAX_OUTPUTS_PER_JOB
    assert len(names) * MAX_PRESET_DIMENSION**2 > MAX_JOB_PIXEL_AREA


def test_resolve_within_bounds_succeeds(session: Session, current_user):
    obj = _image_object(session, uuid.UUID(str(current_user.id)))
    specs = resolve_presets(
        session,
        current_user=current_user,
        names=["thumb", "small", "medium", "large"],
        media_object=obj,
    )
    assert len(specs) == 4


# ── _geometry_cost ──────────────────────────────────────────────────────────


def test_geometry_cost_both_dimensions():
    assert _geometry_cost(ImageSizeSpec(fixed_width=100, fixed_height=50)) == 5000


def test_geometry_cost_width_only_charges_max_height():
    assert _geometry_cost(ImageSizeSpec(fixed_width=100)) == 100 * MAX_PRESET_DIMENSION


def test_geometry_cost_height_only_charges_max_width():
    assert _geometry_cost(ImageSizeSpec(fixed_height=50)) == MAX_PRESET_DIMENSION * 50


def test_geometry_cost_fixed_size_squares():
    assert _geometry_cost(ImageSizeSpec(fixed_size=200)) == 200 * 200


def test_geometry_cost_unspecified_charges_max_square():
    assert _geometry_cost(ImageSizeSpec()) == MAX_PRESET_DIMENSION**2
