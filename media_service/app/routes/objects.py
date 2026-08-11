"""Routes for media object metadata and access URLs.

Two routers, because this module spans two authorization surfaces (A16):

``read_router``
    The public read surface. Carries **no mounted floor** — deliberately, and
    it is the only router in the service that can say so honestly: an anonymous
    caller may read ``PUBLIC`` objects, so there is no dependency that admits
    every route here. Each handler takes ``OptionalPrincipal`` and the
    controller narrows an anonymous read to public rows.
``router``
    The owner mutation surface, mounted on ``require_writer`` so a route added
    here later inherits the writer floor instead of relying on its author to
    remember one.
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Query

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import (
    CurrentWriter,
    OptionalPrincipal,
    SessionDep,
    StorageDep,
    require_writer,
)
from media_service.controllers.objects import ObjectsController
from media_service.core.rate_limit import OptionalRateLimiter
from media_service.db_models.media_objects import MediaObjectPublic
from media_service.schemas.objects import (
    DownloadUrlResponse,
    MediaObjectUpdate,
    ObjectListParams,
    ObjectListResponse,
)

read_router = APIRouter(prefix="/objects", tags=["objects"])
router = APIRouter(
    prefix="/objects",
    tags=["objects"],
    dependencies=[Depends(require_writer)],
)

# Anonymous callers have no user key to count against, so the read surface uses
# the optional limiter: per-user + per-IP once identified, per-IP before that.
_download_limit = OptionalRateLimiter(
    "objects:download-url", limit=60, window_seconds=60
)
_list_limit = OptionalRateLimiter("objects:list", limit=120, window_seconds=60)


@read_router.get(
    "",
    response_model=ObjectListResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_list_limit)],
)
def list_objects(
    *,
    session: SessionDep,
    current_user: OptionalPrincipal,
    params: Annotated[ObjectListParams, Query()],
) -> ObjectListResponse:
    """Return a filtered, cursor-paginated page of media objects.

    Public: an anonymous caller sees the live ``PUBLIC`` catalogue.
    """
    return ObjectsController.list_objects(
        session=session,
        current_user=current_user,
        params=params,
    )


@read_router.get(
    "/{object_id}",
    response_model=MediaObjectPublic,
    responses=BaseController.get_error_responses(),
)
def get_object(
    *,
    session: SessionDep,
    current_user: OptionalPrincipal,
    object_id: uuid.UUID,
) -> MediaObjectPublic:
    """Return public metadata for a media object.

    Public: a ``PUBLIC`` object is readable without a token.
    """
    return ObjectsController.get_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )


@read_router.get(
    "/{object_id}/download-url",
    response_model=DownloadUrlResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_download_limit)],
)
def get_download_url(
    *,
    session: SessionDep,
    current_user: OptionalPrincipal,
    storage: StorageDep,
    object_id: uuid.UUID,
) -> DownloadUrlResponse:
    """Generate a presigned download URL for a media object.

    Public: the bytes of a ``PUBLIC``, scan-clean object are reachable without
    a token. The scan gate below is unchanged and applies to every caller.
    """
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
    current_user: CurrentWriter,
    storage: StorageDep,
    object_id: uuid.UUID,
    body: MediaObjectUpdate,
) -> MediaObjectPublic:
    """Patch allowed metadata fields on a media object."""
    return ObjectsController.update_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
        update=body,
        storage=storage,
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
    current_user: CurrentWriter,
    storage: StorageDep,
    object_id: uuid.UUID,
) -> None:
    """Soft-delete a media object."""
    ObjectsController.delete_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
        storage=storage,
    )
