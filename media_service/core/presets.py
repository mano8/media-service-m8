"""Built-in image presets and the resolver that expands them into VariantSpecs.

Built-in defaults (``thumb``/``small``/``medium``/``large``) ship as code; users
add named presets in the DB. At resolve time the two merge, a user row shadows a
same-named built-in, and each preset is expanded *per format* into the
:class:`~media_sdk_m8.VariantSpec` units carried by the worker job.
"""

from typing import Any
import uuid

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from auth_sdk_m8.schemas.user import UserModel

from media_sdk_m8 import VariantSpec

from media_service.db_models.image_presets import ImagePreset
from media_service.db_models.media_objects import MediaObject
from media_service.schemas.presets import (
    MAX_JOB_PIXEL_AREA,
    MAX_OUTPUTS_PER_JOB,
    MAX_PRESET_DIMENSION,
    FormatSpec,
    ImageSizeSpec,
    PresetSpec,
)
from media_service.storage.keys import build_variant_key


def _webp(width: int | None = None, height: int | None = None) -> PresetSpec:
    """Build a single-format WEBP preset bounded by width/height."""
    return PresetSpec(
        image_size=ImageSizeSpec(fixed_width=width, fixed_height=height),
        formats=[FormatSpec(ext="WEBP", quality=80)],
    )


#: Code-shipped defaults available to every user (shadowable by a user row).
BUILTIN_PRESETS: dict[str, PresetSpec] = {
    "thumb": _webp(width=150),
    "small": _webp(width=320),
    "medium": _webp(width=800),
    "large": _webp(width=1600),
}


def _user_tenant_id(current_user: UserModel) -> uuid.UUID | None:
    """Return the caller's tenant as a UUID, or ``None`` when untenanted."""
    raw = getattr(current_user, "tenant_id", None)
    return uuid.UUID(str(raw)) if raw is not None else None


def _user_presets(
    session: Session, owner_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> dict[str, PresetSpec]:
    """Load this owner/tenant scope's named presets as validated specs."""
    statement = select(ImagePreset).where(col(ImagePreset.owner_user_id) == owner_id)
    if tenant_id is None:
        statement = statement.where(col(ImagePreset.tenant_id).is_(None))
    else:
        statement = statement.where(col(ImagePreset.tenant_id) == tenant_id)
    rows = session.exec(statement).all()
    return {row.name: PresetSpec.model_validate(row.spec) for row in rows}


def merged_presets(session: Session, current_user: UserModel) -> dict[str, PresetSpec]:
    """Return built-ins overlaid with the caller's named presets (user wins)."""
    owner_id = uuid.UUID(str(current_user.id))
    tenant_id = _user_tenant_id(current_user)
    return {**BUILTIN_PRESETS, **_user_presets(session, owner_id, tenant_id)}


def _output_options(
    *, spec: PresetSpec, fmt: FormatSpec, variant_name: str
) -> dict[str, Any]:
    """Build the imgtools-shaped single-format options dict for the worker."""
    return {
        "name": variant_name,
        "image_size": spec.image_size.model_dump(exclude_none=True),
        "formats": [fmt.model_dump()],
        "allow_upscale": spec.allow_upscale,
        "max_byte_size": spec.max_byte_size,
    }


def _expand(
    *, name: str, spec: PresetSpec, media_object: MediaObject
) -> list[VariantSpec]:
    """Expand one named preset into one VariantSpec per output format."""
    specs: list[VariantSpec] = []
    for fmt in spec.formats:
        ext = fmt.ext.lower()
        variant_name = f"{name}_{ext}"
        target_key = build_variant_key(
            owner_user_id=media_object.owner_user_id,
            media_id=media_object.id,
            category=str(media_object.category),
            variant_name=variant_name,
            filename=f"{variant_name}.{ext}",
            tenant_id=media_object.tenant_id,
        )
        specs.append(
            VariantSpec(
                variant_name=variant_name,
                output_options=_output_options(
                    spec=spec, fmt=fmt, variant_name=variant_name
                ),
                target_bucket=media_object.storage_bucket,
                target_key=target_key,
            )
        )
    return specs


def _geometry_cost(size: ImageSizeSpec) -> int:
    """Upper-bound pixel area one output of this geometry can render to.

    An unspecified dimension is charged at :data:`MAX_PRESET_DIMENSION` so the
    cost is a safe ceiling regardless of the source's aspect ratio.
    """
    width = size.fixed_width or size.fixed_size or MAX_PRESET_DIMENSION
    height = size.fixed_height or size.fixed_size or MAX_PRESET_DIMENSION
    return width * height


def resolve_presets(
    session: Session,
    *,
    current_user: UserModel,
    names: list[str],
    media_object: MediaObject,
) -> list[VariantSpec]:
    """Resolve requested preset names into the VariantSpecs for a job.

    Unknown names (after merging built-ins with the caller's presets) raise 422,
    so a job is never enqueued with an unresolvable preset. The expanded job is
    also bounded by output count (:data:`MAX_OUTPUTS_PER_JOB`) and summed
    per-output pixel-area cost (:data:`MAX_JOB_PIXEL_AREA`); either overrun
    raises 422 before any job is created or enqueued.
    """
    available = merged_presets(session, current_user)
    specs: list[VariantSpec] = []
    total_cost = 0
    for name in names:
        spec = available.get(name)
        if spec is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown preset: {name}",
            )
        expanded = _expand(name=name, spec=spec, media_object=media_object)
        specs.extend(expanded)
        total_cost += _geometry_cost(spec.image_size) * len(expanded)
    if len(specs) > MAX_OUTPUTS_PER_JOB:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Variant job expands to {len(specs)} outputs, exceeding the "
                f"maximum {MAX_OUTPUTS_PER_JOB}."
            ),
        )
    if total_cost > MAX_JOB_PIXEL_AREA:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Variant job output area cost {total_cost} exceeds the maximum "
                f"{MAX_JOB_PIXEL_AREA}."
            ),
        )
    return specs
