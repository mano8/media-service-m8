"""Business logic for user-managed image presets."""

import uuid

from fastapi import HTTPException, status
from sqlmodel import Session, col, select

from auth_sdk_m8.schemas.user import UserModel

from media_service.core.presets import BUILTIN_PRESETS, _user_tenant_id
from media_service.db_models.image_presets import ImagePreset
from media_service.db_models.media_objects import utcnow
from media_service.schemas.presets import (
    ImagePresetCreate,
    ImagePresetPublic,
    ImagePresetUpdate,
    PresetSpec,
)


def _row_public(row: ImagePreset) -> ImagePresetPublic:
    """Map a stored preset row to its public, validated representation."""
    return ImagePresetPublic(
        id=row.id,
        name=row.name,
        spec=PresetSpec.model_validate(row.spec),
        builtin=False,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _load_owned_preset(
    session: Session, current_user: UserModel, preset_id: uuid.UUID
) -> ImagePreset:
    """Fetch a preset, enforcing ownership for non-superusers."""
    row = session.get(ImagePreset, preset_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Preset not found."
        )
    owner_id = uuid.UUID(str(current_user.id))
    if not current_user.is_superuser and row.owner_user_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions."
        )
    return row


class PresetsController:
    """Handle the merged built-in ⊕ user preset catalog and user CRUD."""

    @staticmethod
    def list_presets(
        *, session: Session, current_user: UserModel
    ) -> list[ImagePresetPublic]:
        """Return built-ins (minus shadowed names) followed by the user's rows."""
        owner_id = uuid.UUID(str(current_user.id))
        tenant_id = _user_tenant_id(current_user)
        statement = select(ImagePreset).where(
            col(ImagePreset.owner_user_id) == owner_id
        )
        if tenant_id is None:
            statement = statement.where(col(ImagePreset.tenant_id).is_(None))
        else:
            statement = statement.where(col(ImagePreset.tenant_id) == tenant_id)
        rows = list(session.exec(statement).all())
        shadowed = {row.name for row in rows}
        builtins = [
            ImagePresetPublic(name=name, spec=spec, builtin=True)
            for name, spec in BUILTIN_PRESETS.items()
            if name not in shadowed
        ]
        return builtins + [_row_public(row) for row in rows]

    @staticmethod
    def create_preset(
        *,
        session: Session,
        current_user: UserModel,
        req: ImagePresetCreate,
    ) -> ImagePresetPublic:
        """Create a user-owned preset; a duplicate name in scope raises 409."""
        owner_id = uuid.UUID(str(current_user.id))
        tenant_id = _user_tenant_id(current_user)
        statement = select(ImagePreset).where(
            col(ImagePreset.owner_user_id) == owner_id,
            col(ImagePreset.name) == req.name,
        )
        if tenant_id is None:
            statement = statement.where(col(ImagePreset.tenant_id).is_(None))
        else:
            statement = statement.where(col(ImagePreset.tenant_id) == tenant_id)
        if session.exec(statement).first() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Preset already exists: {req.name}",
            )
        row = ImagePreset(
            owner_user_id=owner_id,
            tenant_id=tenant_id,
            name=req.name,
            spec=req.spec.model_dump(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_public(row)

    @staticmethod
    def update_preset(
        *,
        session: Session,
        current_user: UserModel,
        preset_id: uuid.UUID,
        req: ImagePresetUpdate,
    ) -> ImagePresetPublic:
        """Replace a user preset's recipe."""
        row = _load_owned_preset(session, current_user, preset_id)
        row.spec = req.spec.model_dump()
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_public(row)

    @staticmethod
    def delete_preset(
        *,
        session: Session,
        current_user: UserModel,
        preset_id: uuid.UUID,
    ) -> None:
        """Delete a user preset (built-ins have no id and cannot be targeted)."""
        row = _load_owned_preset(session, current_user, preset_id)
        session.delete(row)
        session.commit()
