"""Routes for media object metadata and access URLs."""

from datetime import datetime
from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Query

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import CurrentUser, SessionDep, StorageDep
from media_service.controllers.objects import ObjectsController
from media_service.core.rate_limit import RateLimiter
from media_service.db_models.media_objects import (
    MediaCategory,
    MediaObjectPublic,
    MediaObjectStatus,
    MediaVisibility,
)
from media_service.schemas.objects import (
    DownloadUrlResponse,
    MediaObjectUpdate,
    ObjectListParams,
    ObjectListResponse,
    SortField,
    SortOrder,
)

router = APIRouter(prefix="/objects", tags=["objects"])

_download_limit = RateLimiter("objects:download-url", limit=60, window_seconds=60)
_list_limit = RateLimiter("objects:list", limit=120, window_seconds=60)


@router.get(
    "",
    response_model=ObjectListResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_list_limit)],
)
def list_objects(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    category: MediaCategory | None = None,
    visibility: MediaVisibility | None = None,
    status: MediaObjectStatus | None = None,
    mime_prefix: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    q: str | None = None,
    sort_by: SortField = "created_at",
    order: SortOrder = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
    owner_user_id: uuid.UUID | None = None,
    include_deleted: bool = False,
) -> ObjectListResponse:
    """Return a filtered, cursor-paginated page of media objects."""
    params = ObjectListParams(
        category=category,
        visibility=visibility,
        status=status,
        mime_prefix=mime_prefix,
        created_from=created_from,
        created_to=created_to,
        q=q,
        sort_by=sort_by,
        order=order,
        limit=limit,
        cursor=cursor,
        owner_user_id=owner_user_id,
        include_deleted=include_deleted,
    )
    return ObjectsController.list_objects(
        session=session,
        current_user=current_user,
        params=params,
    )


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
