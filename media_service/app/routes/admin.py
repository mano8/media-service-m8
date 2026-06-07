"""Admin routes — superuser-only storage inspection and housekeeping."""

from fastapi import APIRouter, Depends

from auth_sdk_m8.controllers.base import BaseController

from media_service.app.deps import SessionDep
from media_service.controllers.admin import AdminController
from media_service.core.deps import auth
from media_service.schemas.admin import (
    PurgeStaleResponse,
    StaleUploadsResponse,
    StorageStatsResponse,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(auth.get_current_active_superuser)],
)


@router.get(
    "/storage/stats",
    response_model=StorageStatsResponse,
    responses=BaseController.get_error_responses(),
)
def get_storage_stats(*, session: SessionDep) -> StorageStatsResponse:
    """Return object counts and bytes grouped by status and category."""
    return AdminController.get_storage_stats(session=session)


@router.get(
    "/uploads/stale",
    response_model=StaleUploadsResponse,
    responses=BaseController.get_error_responses(),
)
def get_stale_uploads(*, session: SessionDep) -> StaleUploadsResponse:
    """List upload sessions that are past expiry and still in INITIATED state."""
    return AdminController.get_stale_uploads(session=session)


@router.post(
    "/uploads/purge-stale",
    response_model=PurgeStaleResponse,
    responses=BaseController.get_error_responses(),
)
def purge_stale_uploads(*, session: SessionDep) -> PurgeStaleResponse:
    """Mark all stale INITIATED sessions as EXPIRED and return the count purged."""
    return AdminController.purge_stale_uploads(session=session)
