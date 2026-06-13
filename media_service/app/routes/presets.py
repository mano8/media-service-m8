"""Public routes for user-managed image presets."""

import uuid

from fastapi import APIRouter

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import CurrentUser, SessionDep
from media_service.controllers.presets import PresetsController
from media_service.schemas.presets import (
    ImagePresetCreate,
    ImagePresetPublic,
    ImagePresetUpdate,
)

router = APIRouter(prefix="/presets", tags=["presets"])


@router.get(
    "",
    response_model=list[ImagePresetPublic],
    responses=BaseController.get_error_responses(),
)
def list_presets(
    *,
    session: SessionDep,
    current_user: CurrentUser,
) -> list[ImagePresetPublic]:
    """Return built-in presets merged with the caller's named presets."""
    return PresetsController.list_presets(session=session, current_user=current_user)


@router.post(
    "",
    response_model=ImagePresetPublic,
    status_code=201,
    responses=BaseController.get_error_responses(),
)
def create_preset(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    body: ImagePresetCreate,
) -> ImagePresetPublic:
    """Create a user-owned named preset."""
    return PresetsController.create_preset(
        session=session, current_user=current_user, req=body
    )


@router.patch(
    "/{preset_id}",
    response_model=ImagePresetPublic,
    responses=BaseController.get_error_responses(),
)
def update_preset(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    preset_id: uuid.UUID,
    body: ImagePresetUpdate,
) -> ImagePresetPublic:
    """Replace an existing user preset's recipe."""
    return PresetsController.update_preset(
        session=session,
        current_user=current_user,
        preset_id=preset_id,
        req=body,
    )


@router.delete(
    "/{preset_id}",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def delete_preset(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    preset_id: uuid.UUID,
) -> None:
    """Delete a user preset."""
    PresetsController.delete_preset(
        session=session, current_user=current_user, preset_id=preset_id
    )
