"""Routes for media share links (owner-managed; public resolution)."""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import CurrentUser, SessionDep, StorageDep
from media_service.controllers.shares import SharesController
from media_service.core.rate_limit import AnonRateLimiter
from media_service.schemas.objects import DownloadUrlResponse
from media_service.schemas.shares import (
    ShareTokenCreate,
    ShareTokenListResponse,
    ShareTokenPublic,
)

router = APIRouter(tags=["shares"])

_share_resolve_limit = AnonRateLimiter("shares:resolve", limit=60, window_seconds=60)


@router.post(
    "/objects/{object_id}/shares",
    response_model=ShareTokenPublic,
    status_code=201,
    responses=BaseController.get_error_responses(),
)
def create_share(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
    body: ShareTokenCreate,
) -> ShareTokenPublic:
    """Mint a share link for an object the caller owns."""
    return SharesController.create(
        session=session,
        current_user=current_user,
        object_id=object_id,
        body=body,
    )


@router.get(
    "/objects/{object_id}/shares",
    response_model=ShareTokenListResponse,
    responses=BaseController.get_error_responses(),
)
def list_shares(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    object_id: uuid.UUID,
) -> ShareTokenListResponse:
    """List the share links of an object the caller owns."""
    return SharesController.list_for_object(
        session=session,
        current_user=current_user,
        object_id=object_id,
    )


@router.delete(
    "/shares/{token_id}",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def revoke_share(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    token_id: uuid.UUID,
) -> None:
    """Revoke a share link (idempotent)."""
    SharesController.revoke(
        session=session,
        current_user=current_user,
        token_id=token_id,
    )


@router.get(
    "/shares/{token}",
    response_model=DownloadUrlResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_share_resolve_limit)],
)
def resolve_share(
    *,
    session: SessionDep,
    storage: StorageDep,
    token: str,
) -> DownloadUrlResponse:
    """Resolve a signed share token to a presigned download URL (public)."""
    return SharesController.resolve(
        session=session,
        token=token,
        storage=storage,
    )
