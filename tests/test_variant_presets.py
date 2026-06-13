"""Tests for core/presets.py — built-in/user merge, shadowing, expansion."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import Session

from media_service.core.presets import (
    BUILTIN_PRESETS,
    merged_presets,
    resolve_presets,
)
from media_service.db_models.image_presets import ImagePreset
from media_service.db_models.media_objects import (
    MediaObject,
    MediaObjectStatus,
    ScanStatus,
)


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
        status=MediaObjectStatus.UPLOADED,
        scan_status=ScanStatus.CLEAN,
    )
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return obj


def _add_preset(
    session: Session, owner_id: uuid.UUID, name: str, spec: dict
) -> ImagePreset:
    row = ImagePreset(owner_user_id=owner_id, name=name, spec=spec)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_builtin_expands_one_spec_per_format(session: Session, current_user):
    obj = _image_object(session, uuid.UUID(str(current_user.id)))
    specs = resolve_presets(
        session, current_user=current_user, names=["thumb"], media_object=obj
    )
    assert len(specs) == 1
    spec = specs[0]
    assert spec.variant_name == "thumb_webp"
    assert spec.output_options["name"] == "thumb_webp"
    assert spec.output_options["formats"] == [{"ext": "WEBP", "quality": 80}]
    assert spec.target_bucket == "private-media"
    assert "/variants/thumb_webp/" in spec.target_key


def test_multiple_presets_accumulate(session: Session, current_user):
    obj = _image_object(session, uuid.UUID(str(current_user.id)))
    specs = resolve_presets(
        session,
        current_user=current_user,
        names=["thumb", "large"],
        media_object=obj,
    )
    assert {s.variant_name for s in specs} == {"thumb_webp", "large_webp"}


def test_user_preset_new_name_multi_format(session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    obj = _image_object(session, owner)
    _add_preset(
        session,
        owner,
        "web",
        {
            "image_size": {"fixed_width": 1024},
            "formats": [
                {"ext": "WEBP", "quality": 82},
                {"ext": "JPEG", "quality": 90},
            ],
            "allow_upscale": False,
            "max_byte_size": None,
        },
    )
    specs = resolve_presets(
        session, current_user=current_user, names=["web"], media_object=obj
    )
    assert {s.variant_name for s in specs} == {"web_webp", "web_jpeg"}


def test_user_preset_shadows_builtin(session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    obj = _image_object(session, owner)
    _add_preset(
        session,
        owner,
        "thumb",
        {
            "image_size": {"fixed_width": 64},
            "formats": [
                {"ext": "PNG", "quality": 100},
                {"ext": "AVIF", "quality": 70},
            ],
        },
    )
    specs = resolve_presets(
        session, current_user=current_user, names=["thumb"], media_object=obj
    )
    # The user's two-format "thumb" wins over the single-format built-in.
    assert {s.variant_name for s in specs} == {"thumb_png", "thumb_avif"}


def test_unknown_preset_raises_422(session: Session, current_user):
    obj = _image_object(session, uuid.UUID(str(current_user.id)))
    with pytest.raises(HTTPException) as exc:
        resolve_presets(
            session, current_user=current_user, names=["nope"], media_object=obj
        )
    assert exc.value.status_code == 422


def test_merged_presets_contains_builtins_and_user(session: Session, current_user):
    owner = uuid.UUID(str(current_user.id))
    _add_preset(
        session,
        owner,
        "social",
        {
            "image_size": {"fixed_width": 600},
            "formats": [{"ext": "WEBP", "quality": 80}],
        },
    )
    merged = merged_presets(session, current_user)
    assert set(BUILTIN_PRESETS).issubset(merged)
    assert "social" in merged


def test_resolve_uses_tenant_scoped_preset(session: Session):
    tenant = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), is_superuser=False, tenant_id=tenant)
    owner = uuid.UUID(str(user.id))
    obj = _image_object(session, owner)
    session.add(
        ImagePreset(
            owner_user_id=owner,
            tenant_id=tenant,
            name="web",
            spec={
                "image_size": {"fixed_width": 1024},
                "formats": [{"ext": "WEBP", "quality": 80}],
            },
        )
    )
    session.commit()
    specs = resolve_presets(session, current_user=user, names=["web"], media_object=obj)
    assert specs[0].variant_name == "web_webp"
