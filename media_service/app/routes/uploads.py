"""Routes for the presigned upload flow."""

import uuid

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_sdk_m8 import ScanJobPayload

from media_service.app.deps import CurrentUser, SessionDep, StorageDep
from media_service.controllers.uploads import UploadsController
from media_service.core.arq import ArqPoolDep, enqueue_scan
from media_service.core.rate_limit import RateLimiter
from media_service.schemas.uploads import (
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitiateRequest,
    UploadInitiateResponse,
)

router = APIRouter(prefix="/uploads", tags=["uploads"])

_initiate_limit = RateLimiter("uploads:initiate", limit=20, window_seconds=60)
_complete_limit = RateLimiter("uploads:complete", limit=20, window_seconds=60)


@router.post(
    "/initiate",
    response_model=UploadInitiateResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_initiate_limit)],
)
def initiate_upload(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    storage: StorageDep,
    body: UploadInitiateRequest,
) -> UploadInitiateResponse:
    """Create a presigned PUT URL and an upload session record."""
    return UploadsController.initiate_upload(
        session=session,
        current_user=current_user,
        req=body,
        storage=storage,
    )


@router.post(
    "/{session_id}/complete",
    response_model=UploadCompleteResponse,
    responses=BaseController.get_error_responses(),
    dependencies=[Depends(_complete_limit)],
)
async def complete_upload(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    storage: StorageDep,
    arq_pool: ArqPoolDep,
    session_id: uuid.UUID,
    body: UploadCompleteRequest,
) -> UploadCompleteResponse:
    """Verify the object exists in storage and promote it to a MediaObject.

    On success the object is ``PENDING`` scan; enqueue an antivirus job so the
    worker can clear (or quarantine) it before it becomes downloadable.
    """
    response = UploadsController.complete_upload(
        session=session,
        current_user=current_user,
        session_id=session_id,
        req=body,
        storage=storage,
    )
    media_object = response.media_object
    await enqueue_scan(
        arq_pool,
        ScanJobPayload(
            object_id=media_object.id,
            bucket=media_object.storage_bucket,
            object_key=media_object.object_key,
            owner_user_id=media_object.owner_user_id,
        ),
    )
    return response


@router.post(
    "/{session_id}/abort",
    response_model=None,
    status_code=204,
    responses=BaseController.get_error_responses(),
)
def abort_upload(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    storage: StorageDep,
    session_id: uuid.UUID,
) -> None:
    """Cancel an upload session and remove any partial object from storage."""
    UploadsController.abort_upload(
        session=session,
        current_user=current_user,
        session_id=session_id,
        storage=storage,
    )
