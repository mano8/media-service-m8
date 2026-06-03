"""Routes for media object metadata and access URLs."""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import CurrentUser, SessionDep, StorageDep
from media_service.controllers.objects import ObjectsController
from media_service.core.rate_limit import RateLimiter
from media_service.db_models.media_objects import MediaObjectPublic
from media_service.schemas.objects import DownloadUrlResponse, MediaObjectUpdate

router = APIRouter(prefix="/objects", tags=["objects"])

_download_limit = RateLimiter("objects:download-url", limit=60, window_seconds=60)


@router.get(
    "/{object_id}",
    response_model=MediaObjectPublic,
    responses=BaseController.get_error_responses(),
)
def get_object(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
) -> MediaObjectPublic:
    """Return public metadata for a media object."""
    return ObjectsController.get_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )


@router.get(
    "/{object_id}/download-url",
    response_model=DownloadUrlResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_download_limit)],
)
def get_download_url(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    storage: StorageDep,
    object_id: uuid.UUID,
) -> DownloadUrlResponse:
    """Generate a presigned download URL for a media object."""
    return ObjectsController.download_url(
        session=session,
        current_user=current_user,
        object_id=object_id,
        storage=storage,
    )


@router.patch(
    "/{object_id}",
    response_model=MediaObjectPublic,
    responses=BaseController.get_error_responses(),
)
def update_object(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
    body: MediaObjectUpdate,
) -> MediaObjectPublic:
    """Patch allowed metadata fields on a media object."""
    return ObjectsController.update_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
        update=body,
    )


@router.delete(
    "/{object_id}",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def delete_object(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
) -> None:
    """Soft-delete a media object."""
    ObjectsController.delete_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )
